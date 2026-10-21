"""Build a demo database full of synthetic data.

    python -m demo_data.generate --out demo.db

The output is a complete, self-consistent SQLite database that the application
runs against unchanged. No real business data is involved at any point: every
figure is generated here, from the catalogue of invented venues, products,
staff, suppliers and customers in `catalog.py`.

The generator is deterministic. The same seed always produces the same
database, so screenshots, tests and the hosted demo all agree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time

if __package__ in (None, ""):  # allow running the file directly
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "demo_data"

from . import gen_analytics, gen_events, gen_finance, gen_master, gen_sales, gen_support
from .plan import build_plan

DEFAULT_END = dt.date(2026, 10, 19)
DEFAULT_MONTHS = 18


def build(out_path: str, months: int, end: dt.date, seed: int, scale: float,
          schema_path: str, quiet: bool = False) -> dict:
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    start = (end.replace(day=1) - dt.timedelta(days=31 * (months - 1))).replace(day=1)

    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    con.executescript(open(schema_path, encoding="utf-8").read())

    t0 = time.time()
    stats: dict[str, object] = {"period": f"{start} .. {end}", "seed": seed}

    say(f"Period: {start} to {end}  (seed {seed}, scale {scale})")

    say("  master data ...")
    gen_master.build_date_table(con, start, end)
    products = gen_master.build_products(con)
    staff = gen_master.build_people(con, start)
    tag_ids = gen_master.build_tags(con)

    say("  operating plan ...")
    plan = build_plan(start, end, seed, scale)
    stats["event_days"] = len(plan)

    say(f"  point of sale ({len(plan)} trading days) ...")
    bills, items = gen_sales.generate(con, plan, products, seed)
    stats["bills"] = bills
    stats["bill_items"] = items

    say("  stock snapshots ...")
    gen_sales.generate_stock_snapshots(con, plan, products, seed)

    say("  wages ...")
    stats["wage_shifts"] = gen_events.generate_wages(con, plan, staff, seed)

    say("  event log ...")
    stats["events"] = gen_events.generate_event_log(con, plan, seed)

    say("  invoices ...")
    fin = gen_finance.generate(con, plan, tag_ids, start, end, seed)
    stats.update({k: v for k, v in fin.items() if k != "trading"})

    say("  bank movements ...")
    stats.update(gen_finance.generate_bank(con, plan, start, end, seed))

    say("  fixed costs ...")
    gen_finance.generate_fixed_costs(con, start, end, fin["trading"], seed)

    # Depends on the fixed cost links above, so it cannot run with the events.
    say("  fixed cost allocation ...")
    gen_events.allocate_fixed_profit(con)

    say("  VAT working ...")
    stats["vat_rows"] = gen_finance.generate_vat(con, start, end)

    say("  second entity ...")
    stats.update(gen_support.generate_brewery(con, start, end, seed))

    say("  foreign currency ...")
    gen_support.generate_fx(con, start, end, seed)

    say("  warehouse documents ...")
    gen_support.generate_pos_documents(con, start, end, seed)

    say("  host expenses ...")
    gen_support.generate_host_expenses(con, start, end, seed)

    say("  trial balance ...")
    gen_support.generate_trial_balance(con, start, end, seed)

    say("  ingestion audit log ...")
    gen_support.generate_ingestion_log(con, start, end, seed)

    say("  website analytics ...")
    stats.update(gen_analytics.generate(con, plan, start, end, seed))

    say("  indexes ...")
    _create_indexes(con)

    con.commit()
    con.execute("ANALYZE")
    con.execute("VACUUM")
    con.close()

    stats["seconds"] = round(time.time() - t0, 1)
    stats["size_mb"] = round(os.path.getsize(out_path) / 1024 / 1024, 1)
    return stats


def _create_indexes(con) -> None:
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_bills_date ON bills_raw(date, venue)",
        "CREATE INDEX IF NOT EXISTS ix_bill_items_date ON bill_items_raw(date, venue)",
        "CREATE INDEX IF NOT EXISTS ix_bill_items_bill ON bill_items_raw(bill_id)",
        "CREATE INDEX IF NOT EXISTS ix_sales_hourly_date ON sales_hourly(date, venue)",
        "CREATE INDEX IF NOT EXISTS ix_payment_hourly_date ON payment_hourly(date, venue)",
        "CREATE INDEX IF NOT EXISTS ix_inventory_daily_date ON inventory_daily(date)",
        "CREATE INDEX IF NOT EXISTS ix_cogs_daily_date ON cogs_daily(date)",
        "CREATE INDEX IF NOT EXISTS ix_stock_daily_date ON inventory_stock_daily(date)",
        "CREATE INDEX IF NOT EXISTS ix_events_date ON events_daily_log(date, venue)",
        "CREATE INDEX IF NOT EXISTS ix_wages_date ON wages_daily(date)",
        "CREATE INDEX IF NOT EXISTS ix_in_issued ON invoices_in(date_issued)",
        "CREATE INDEX IF NOT EXISTS ix_in_received ON invoices_in(date_received)",
        "CREATE INDEX IF NOT EXISTS ix_out_issued ON invoices_out(date_issued)",
        "CREATE INDEX IF NOT EXISTS ix_lines_sef ON invoice_line_items(invoice_id_sef)",
        "CREATE INDEX IF NOT EXISTS ix_bank_date ON bank_transactions(date, account)",
        "CREATE INDEX IF NOT EXISTS ix_bank_fitid ON bank_transactions(fitid)",
        "CREATE INDEX IF NOT EXISTS ix_balances_date ON account_balances(balance_date)",
        "CREATE INDEX IF NOT EXISTS ix_pdv_period ON pdv_working(period, stream)",
        "CREATE INDEX IF NOT EXISTS ix_wa_daily_date ON web_analytics_daily(date)",
        "CREATE INDEX IF NOT EXISTS ix_wa_break ON web_analytics_breakdown(date, dim_type)",
    ):
        con.execute(stmt)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default=os.path.join(here, "demo.db"),
                    help="path of the database to write (default: ./demo.db)")
    ap.add_argument("--schema", default=os.path.join(here, "schema.sql"),
                    help="path to schema.sql")
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                    help=f"months of history to generate (default: {DEFAULT_MONTHS})")
    ap.add_argument("--end", default=DEFAULT_END.isoformat(),
                    help="last day of generated data, YYYY-MM-DD")
    ap.add_argument("--seed", type=int, default=20261020,
                    help="random seed; the same seed gives the same database")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiplier on transaction volume (0.3 builds a small, fast "
                         "database; 1.0 is life-size)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    stats = build(args.out, args.months, dt.date.fromisoformat(args.end), args.seed,
                  args.scale, args.schema, args.quiet)

    if not args.quiet:
        print(f"\nWrote {args.out}  ({stats['size_mb']} MB in {stats['seconds']}s)")
        for key in ("period", "event_days", "bills", "bill_items", "wage_shifts",
                    "invoices_in", "invoices_out", "line_items", "bank_transactions",
                    "brewery_invoices", "vat_rows", "analytics_days"):
            if key in stats:
                print(f"  {key:<20} {stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
