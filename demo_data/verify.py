"""Consistency checks over a generated demo database.

    python -m demo_data.verify demo.db

The point of these checks is that the aggregates the dashboard reads must tie
back to the transaction-level tables they were derived from. If a generator
drifts, this fails loudly rather than producing a plausible-looking demo with
numbers that do not add up.
"""

from __future__ import annotations

import sqlite3
import sys

TOLERANCE = 1.0  # RSD; rounding per line accumulates


def _q(con, sql, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row and row[0] is not None else 0


def run(path: str) -> int:
    con = sqlite3.connect(path)
    failures, checks = [], 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        if ok:
            print(f"  pass  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            failures.append(name)

    print(f"Verifying {path}\n")

    # --- every table has rows (except the few that are legitimately empty) ---
    # fs_mapping is seeded by the application on first run, not by the generator.
    expected_empty = {"fs_mapping"}
    empty = []
    for (t,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        n = _q(con, f"SELECT COUNT(*) FROM {t}")
        if n == 0 and t not in expected_empty:
            empty.append(t)
    check("every table is populated", not empty, f"empty: {', '.join(empty)}")

    # --- bills tie to their line items ------------------------------------
    diff = _q(con, """
        SELECT COALESCE(MAX(ABS(d)), 0) FROM (
            SELECT b.amount - SUM(i.total_price) AS d
            FROM bills_raw b JOIN bill_items_raw i ON i.bill_id = b.bill_id
            GROUP BY b.bill_id)""")
    check("bill header equals the sum of its lines", diff < TOLERANCE, f"max diff {diff:.2f}")

    orphans = _q(con, "SELECT COUNT(*) FROM bill_items_raw i "
                      "LEFT JOIN bills_raw b ON b.bill_id = i.bill_id WHERE b.bill_id IS NULL")
    check("no orphaned bill lines", orphans == 0, f"{orphans} orphans")

    # --- hourly sales tie to the line items (revoked bills excluded) -------
    a = _q(con, "SELECT SUM(revenue_gross) FROM sales_hourly")
    b = _q(con, "SELECT SUM(i.total_price) FROM bill_items_raw i "
                "JOIN bills_raw bl ON bl.bill_id = i.bill_id WHERE bl.is_revoked = 0")
    check("sales_hourly ties to bill lines", abs(a - b) < max(TOLERANCE, b * 1e-6),
          f"{a:.2f} vs {b:.2f}")

    # --- daily inventory ties to the line items ---------------------------
    a = _q(con, "SELECT SUM(revenue_gross) FROM inventory_daily")
    check("inventory_daily ties to bill lines", abs(a - b) < max(TOLERANCE, b * 1e-6),
          f"{a:.2f} vs {b:.2f}")

    # --- cogs_daily ties to the line items --------------------------------
    a = _q(con, "SELECT SUM(cogs_net) FROM cogs_daily")
    c = _q(con, "SELECT SUM(i.total_cogs_net) FROM bill_items_raw i "
                "JOIN bills_raw bl ON bl.bill_id = i.bill_id WHERE bl.is_revoked = 0")
    check("cogs_daily ties to bill lines", abs(a - c) < max(TOLERANCE, c * 1e-6),
          f"{a:.2f} vs {c:.2f}")

    # --- event log ties to POS --------------------------------------------
    a = _q(con, "SELECT SUM(sales_gross) FROM events_daily_log")
    check("event log sales tie to POS gross (revoked bills excluded)",
          abs(a - b) < max(TOLERANCE, b * 1e-6), f"{a:.2f} vs {b:.2f}")

    a = _q(con, "SELECT SUM(wages) FROM events_daily_log")
    b2 = _q(con, "SELECT SUM(gross_cost) FROM wages_daily")
    check("event log wages tie to the wage register",
          abs(a - b2) < max(TOLERANCE, b2 * 1e-6), f"{a:.2f} vs {b2:.2f}")

    # --- invoice headers tie to their lines -------------------------------
    for table, key in (("invoices_in", "purchase"), ("invoices_out", "sale")):
        diff = _q(con, f"""
            SELECT COALESCE(MAX(ABS(d)), 0) FROM (
                SELECT v.net_amount - SUM(l.net_amount) AS d
                FROM {table} v JOIN invoice_line_items l
                  ON l.invoice_id_sef = v.invoice_id_sef AND l.invoice_type = ?
                GROUP BY v.invoice_id)""", (key,))
        check(f"{table} header equals the sum of its lines", diff < TOLERANCE,
              f"max diff {diff:.2f}")

    # --- VAT arithmetic on every invoice ----------------------------------
    for table in ("invoices_in", "invoices_out"):
        bad = _q(con, f"SELECT COUNT(*) FROM {table} "
                      f"WHERE ABS(pdv_amount - net_amount * pdv_rate / 100.0) > 0.51")
        check(f"{table}: VAT equals net x rate", bad == 0, f"{bad} rows off")

    # --- VAT working ties to the ledgers it is built from -----------------
    a = _q(con, "SELECT SUM(pdv_amount) FROM pdv_working WHERE direction='output' "
                "AND stream='pos_sales'")
    b2 = _q(con, "SELECT SUM(i.total_price - i.total_price / (1 + i.tax_percent/100.0)) "
                 "FROM bill_items_raw i JOIN bills_raw bl ON bl.bill_id = i.bill_id "
                 "WHERE bl.is_revoked = 0")
    check("output VAT on POS sales ties to the receipts",
          abs(a - b2) < max(5.0, b2 * 1e-5), f"{a:.2f} vs {b2:.2f}")

    excluded = _q(con, "SELECT COUNT(*) FROM invoices_in "
                       "WHERE invoice_status IN ('Storno','Rejected')")
    check("the ledger contains cancelled invoices to exclude", excluded > 0,
          "none generated, so the exclusion filter is untested")

    a = _q(con, "SELECT SUM(pdv_amount) FROM pdv_working WHERE direction='input'")
    b2 = _q(con, "SELECT SUM(pdv_amount) FROM invoices_in WHERE entity='main' "
                 "AND COALESCE(invoice_status,'Approved') NOT IN ('Storno','Rejected')")
    check("input VAT does not exceed the purchase ledger", a <= b2 + TOLERANCE,
          f"{a:.2f} vs {b2:.2f}")

    # --- bank -------------------------------------------------------------
    bad = _q(con, "SELECT COUNT(*) FROM bank_transactions WHERE amount <= 0")
    check("no non-positive bank amounts", bad == 0, f"{bad} rows")

    bad = _q(con, "SELECT COUNT(*) FROM bank_transactions "
                  "WHERE direction NOT IN ('credit','debit')")
    check("bank direction uses the statement vocabulary", bad == 0, f"{bad} rows")

    transfers_out = _q(con, "SELECT SUM(amount) FROM bank_transactions "
                            "WHERE is_internal_transfer=1 AND direction='debit'")
    transfers_in = _q(con, "SELECT SUM(amount) FROM bank_transactions "
                           "WHERE is_internal_transfer=1 AND direction='credit'")
    check("internal transfers net to zero across own accounts",
          abs(transfers_out - transfers_in) < TOLERANCE,
          f"{transfers_out:.2f} vs {transfers_in:.2f}")

    # closing balance must equal opening plus all movements
    for (acct,) in con.execute("SELECT DISTINCT account_number FROM account_balances"):
        last = con.execute(
            "SELECT closing_balance FROM account_balances WHERE account_number=? "
            "ORDER BY balance_date DESC, statement_number DESC LIMIT 1", (acct,)).fetchone()[0]
        moved = _q(con, "SELECT SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END) "
                        "FROM bank_transactions WHERE account=?", (acct,))
        first = con.execute(
            "SELECT closing_balance FROM account_balances WHERE account_number=? "
            "ORDER BY balance_date, statement_number LIMIT 1", (acct,)).fetchone()[0]
        first_move = _q(con, "SELECT SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END) "
                             "FROM bank_transactions WHERE account=? AND date=("
                             "SELECT MIN(date) FROM bank_transactions WHERE account=?)",
                        (acct, acct))
        opening = first - first_move
        check(f"balance ledger reconciles ({acct[-5:]})",
              abs((opening + moved) - last) < TOLERANCE,
              f"opening {opening:.2f} + moves {moved:.2f} != {last:.2f}")

    # --- payment status is consistent with the amount paid ----------------
    for table in ("invoices_in", "invoices_out"):
        bad = _q(con, f"SELECT COUNT(*) FROM {table} "
                      f"WHERE payment_status='Paid' AND amount_paid <= 0")
        check(f"{table}: paid invoices carry an amount", bad == 0, f"{bad} rows")
        bad = _q(con, f"SELECT COUNT(*) FROM {table} "
                      f"WHERE payment_status='Unpaid' AND amount_paid > 0")
        check(f"{table}: unpaid invoices carry no amount", bad == 0, f"{bad} rows")

    # --- trial balance ----------------------------------------------------
    for (sid,) in con.execute("SELECT id FROM bb_snapshots"):
        d = _q(con, "SELECT SUM(uk_d) FROM bb_lines WHERE snapshot_id=?", (sid,))
        p = _q(con, "SELECT SUM(uk_p) FROM bb_lines WHERE snapshot_id=?", (sid,))
        check(f"trial balance {sid}: debits and credits are both present",
              d > 0 and p > 0, f"debit {d:.0f} credit {p:.0f}")

    # --- analytics --------------------------------------------------------
    bad = _q(con, "SELECT COUNT(*) FROM web_analytics_daily WHERE visits > pageviews")
    check("analytics: visits never exceed pageviews", bad == 0, f"{bad} rows")

    for dim in ("country", "referrer", "path", "device"):
        n = _q(con, "SELECT COUNT(*) FROM web_analytics_breakdown WHERE dim_type=?", (dim,))
        check(f"analytics: {dim} breakdown present", n > 0)

    n = _q(con, "SELECT COUNT(DISTINCT bot) FROM web_analytics_daily")
    check("analytics: bot and human traffic are both recorded", n == 2)

    # --- fixed cost allocation --------------------------------------------
    # fixed_profit is the event's share of the month's operating fixed costs,
    # netted against operating fixed income. The generator once wrote the
    # event's own gross profit into this column instead, which made every net
    # profit in the dashboard come out as twice its gross profit.
    missing = _q(con, """
        SELECT COUNT(*) FROM events_daily_log
        WHERE fixed_profit IS NULL
          AND LOWER(COALESCE(event_type, '')) NOT IN ('lokal ne radi', 'zatvoreno', 'closed')
    """)
    check("events: every open event carries a fixed cost allocation", missing == 0,
          f"{missing} events without one")

    positive = _q(con, "SELECT COUNT(*) FROM events_daily_log WHERE fixed_profit > 0")
    check("events: the fixed cost allocation is a cost, not a profit", positive == 0,
          f"{positive} events with a positive allocation")

    # The allocation is only computed from tags marked operativni, so a
    # submodule the application does not know about is silently dropped.
    unknown = _q(con, """
        SELECT COUNT(*) FROM fixed_cost_tags
        WHERE submodule NOT IN ('operativni', 'parking', 'podnajam', 'sponzori', 'ostalo')
    """)
    check("fixed cost tags: submodules match the application's vocabulary", unknown == 0,
          f"{unknown} tags with an unrecognised submodule")

    # --- dates stay inside the generated window ---------------------------
    lo = _q(con, "SELECT MIN(date) FROM bills_raw")
    hi = _q(con, "SELECT MAX(date) FROM bills_raw")
    for table, col in (("invoices_in", "date_issued"), ("invoices_out", "date_issued"),
                       ("wages_daily", "date"), ("events_daily_log", "date"),
                       ("web_analytics_daily", "date")):
        over = _q(con, f"SELECT COUNT(*) FROM {table} WHERE {col} > ?", (hi,))
        check(f"{table}: no dates after the end of the period", over == 0,
              f"{over} rows after {hi}")

    print(f"\n{checks - len(failures)}/{checks} checks passed  "
          f"({lo} .. {hi})")
    if failures:
        print("Failed: " + ", ".join(failures))
    con.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else "demo.db"))
