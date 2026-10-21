"""The remaining modules: second legal entity, foreign currency, POS
documents, host expenses, trial balance and the ingestion audit log.
"""

from __future__ import annotations

import collections
import datetime as dt
import random

from . import catalog as C
from .helpers import (
    days_in_month, money, months_between, net_from_gross, now_stamp,
)

BREWERY_ACCOUNT = "160-0000000005678-90"

BREWERY_SUPPLIERS = [
    ("Malt & Grain Import d.o.o.", "103000011", 420000, 20),
    ("Hops Trading d.o.o.", "103000012", 180000, 20),
    ("Yeast Lab Supply d.o.o.", "103000013", 64000, 20),
    ("Keg & Bottle Works d.o.o.", "103000014", 230000, 20),
    ("Label Print House d.o.o.", "103000015", 88000, 20),
    ("Brewery Utilities JP", "103000016", 190000, 20),
]

BREWERY_CLIENTS = [
    ("Craft Beer Wholesale d.o.o.", "104000011", 680000, 20),
    ("Hospitality Group North d.o.o.", "104000012", 430000, 20),
    ("Specialty Bottle Shop d.o.o.", "104000013", 210000, 20),
    ("Export Partner GmbH", "104000014", 0, 0),  # EU export, zero-rated
]


def generate_brewery(con, start: dt.date, end: dt.date, seed: int) -> dict:
    """The brewery branch: its own invoice ledger and bank account.

    Kept in separate tables because it is a separate legal entity with its own
    VAT return, which is why the consolidated views have to be entity-aware.
    """
    rng = random.Random(seed + 71)
    inv_in, inv_out, txs = [], [], []
    iid = oid = 1

    for (y, m) in months_between(start, end):
        dim = days_in_month(y, m)
        season = 1.35 if m in (5, 6, 7, 8) else (0.75 if m in (12, 1, 2) else 1.0)

        for name, pib, base, vat in BREWERY_SUPPLIERS:
            for _ in range(rng.randint(1, 2)):
                issued = dt.date(y, m, rng.randint(1, dim))
                if issued > end:
                    continue
                net = money(round(base * season * rng.uniform(0.85, 1.15) / 100) * 100)
                vat_amt = money(net * vat / 100.0)
                due = issued + dt.timedelta(days=30)
                paid = (end - due).days > 5
                inv_in.append((iid, issued.isoformat(), issued.isoformat(),
                               due.isoformat(), name, pib, f"BIN-{400000 + iid}",
                               net, vat_amt, float(vat),
                               "Paid" if paid else "Unpaid", None))
                if paid:
                    pd = due + dt.timedelta(days=rng.randint(-4, 6))
                    if pd <= end:
                        txs.append((pd.isoformat(), "debit", name, money(net + vat_amt),
                                    f"Payment of invoice {iid}", f"BRW-{iid}",
                                    BREWERY_ACCOUNT, None, None, "221", "RSD",
                                    "brewery", "PDF", f"BOUT-{iid}", now_stamp(pd), 0))
                iid += 1

        for name, pib, base, vat in BREWERY_CLIENTS:
            n = rng.randint(1, 3) if base else 1
            for _ in range(n):
                issued = dt.date(y, m, rng.randint(1, dim))
                if issued > end:
                    continue
                if base == 0:  # export invoice, invoiced in EUR, zero-rated
                    net = money(round(rng.uniform(380000, 920000) / 100) * 100)
                    vat_amt, rate = 0.0, 0.0
                else:
                    net = money(round(base * season * rng.uniform(0.8, 1.2) / 100) * 100)
                    vat_amt, rate = money(net * vat / 100.0), float(vat)
                due = issued + dt.timedelta(days=rng.choice([15, 30, 45]))
                paid = (end - due).days > 5
                inv_out.append((oid, issued.isoformat(), due.isoformat(), name, pib,
                                f"BOUT-{600000 + oid}", net, vat_amt, rate,
                                "Paid" if paid else "Unpaid",
                                "EU export, zero-rated" if base == 0 else None))
                if paid:
                    pd = due + dt.timedelta(days=rng.randint(-3, 8))
                    if pd <= end:
                        txs.append((pd.isoformat(), "credit", name, money(net + vat_amt),
                                    f"Settlement of invoice {oid}", f"BRW-O-{oid}",
                                    BREWERY_ACCOUNT, None, None, "221", "RSD",
                                    "brewery", "PDF", f"BIN-{oid}", now_stamp(pd), 0))
                oid += 1

    # statement numbers
    txs.sort(key=lambda r: r[0])
    counters, last = 0, None
    numbered = []
    for t in txs:
        if t[0] != last:
            counters += 1
            last = t[0]
        numbered.append((t[0], t[1], t[2], t[3], t[4], t[5], t[6], counters,
                         None, t[9], t[10], t[11], t[12], t[13], t[14], t[16]
                         if len(t) > 16 else 0))

    con.executemany(
        "INSERT INTO brewery_invoices_in (invoice_id, date_issued, date_received, "
        "due_date, supplier_name, supplier_pib, invoice_number_sef, net_amount, "
        "pdv_amount, pdv_rate, payment_status, notes) "
        "VALUES (" + ",".join("?" * 12) + ")", inv_in)
    con.executemany(
        "INSERT INTO brewery_invoices_out (invoice_id, date_issued, due_date, client_name, "
        "client_pib, invoice_number_sef, net_amount, pdv_amount, pdv_rate, payment_status, "
        "notes) VALUES (" + ",".join("?" * 11) + ")", inv_out)
    con.executemany(
        "INSERT INTO brewery_transactions (date, direction, counterparty, amount, purpose, "
        "reference, account, statement_number, bank_tx_id, purposecode, currency, entity, "
        "source, fitid, created_at, is_internal_transfer) "
        "VALUES (" + ",".join("?" * 16) + ")", numbered)

    return {"brewery_invoices": len(inv_in) + len(inv_out),
            "brewery_transactions": len(numbered)}


def generate_fx(con, start: dt.date, end: dt.date, seed: int) -> None:
    """Foreign currency account: EUR receipts from the export customer."""
    rng = random.Random(seed + 73)
    stmts, lines, foreign = [], [], []
    sid = 1
    balance = 4200.0

    for (y, m) in months_between(start, end):
        d = dt.date(y, m, min(22, days_in_month(y, m)))
        if d > end:
            continue
        rate = rng.uniform(117.0, 117.6)
        credit = money(round(rng.uniform(2400, 9800) / 10) * 10)
        debit = money(round(rng.uniform(0, 2200) / 10) * 10)
        prev = balance
        balance = money(balance + credit - debit)
        stmts.append((sid, "FX-EUR-001", "RS35160000000000112233", "EUR", "001",
                      "Foreign currency account", f"{y}{m:02d}", d.isoformat(),
                      prev, balance, f"fx_statement_{y}{m:02d}.pdf", now_stamp(d)))
        lines.append((sid, 1, "Export invoice settlement", 0.0, credit,
                      money(credit * rate), d.isoformat(), money(rate),
                      f"N{rng.randint(1000,9999)}", f"P{rng.randint(1000,9999)}"))
        if debit:
            lines.append((sid, 2, "Conversion to RSD", debit, 0.0,
                          money(debit * rate), d.isoformat(), money(rate),
                          f"N{rng.randint(1000,9999)}", f"P{rng.randint(1000,9999)}"))
        foreign.append((d.isoformat(), "FX-EUR-001", "credit", credit, "EUR", money(rate),
                        money(credit * rate), "Export Partner GmbH",
                        "Export invoice settlement", f"REF{rng.randint(10000,99999)}",
                        f"{y}{m:02d}", None, "brewery", now_stamp(d), "demo_generator"))
        sid += 1

    con.executemany(
        "INSERT INTO fx_bank_statements (id, account, iban, currency, subaccount_code, "
        "subaccount_name, statement_number, statement_date, previous_balance, new_balance, "
        "source_file, uploaded_at) VALUES (" + ",".join("?" * 12) + ")", stmts)
    con.executemany(
        "INSERT INTO fx_bank_transactions (statement_id, line_no, description, debit, "
        "credit, rsd_equivalent, booking_date, kurs, nalog_br, predmet_br) "
        "VALUES (" + ",".join("?" * 10) + ")", lines)
    con.executemany(
        "INSERT INTO foreign_transactions (date, account, direction, amount_foreign, "
        "currency, exchange_rate, amount_rsd, counterparty, purpose, reference_number, "
        "statement_number, notes, entity, created_at, created_by) "
        "VALUES (" + ",".join("?" * 15) + ")", foreign)


def generate_pos_documents(con, start: dt.date, end: dt.date, seed: int) -> None:
    """Goods-received notes and POS-issued invoices, read from the POS back office.

    These are the stock-side counterpart to the e-invoice ledger: the same
    delivery appears here as a warehouse document and there as an invoice,
    which is what the reconciliation view compares.
    """
    rng = random.Random(seed + 79)
    docs, items, out_inv = [], [], []
    did = 1

    products = list(con.execute(
        "SELECT product_id, code, name, category_name, unit_measure, tax_percent, "
        "input_price FROM products"))

    for (y, m) in months_between(start, end):
        dim = days_in_month(y, m)
        for _ in range(rng.randint(14, 26)):
            d = dt.date(y, m, rng.randint(1, dim))
            if d > end:
                continue
            supplier = rng.choice(C.SUPPLIERS)
            dtype = rng.choices(
                [(1, "Goods received"), (2, "Stock transfer"), (3, "Write-off")],
                [78, 16, 6])[0]
            picked = rng.sample(products, rng.randint(2, 7))
            net_total = vat_total = 0.0
            for i, (pid, code, name, cat, unit, vat, cost) in enumerate(picked, start=1):
                qty = float(rng.choice([6, 12, 24, 30, 48, 60]))
                price = money((cost or 50) * rng.uniform(0.95, 1.08))
                line_net = money(price * qty)
                items.append((did, d.isoformat(), pid, code, name, cat, qty, price,
                              line_net, vat, unit, now_stamp(d)))
                net_total += line_net
                vat_total += line_net * (vat or 0) / 100.0

            docs.append((did, dtype[0], dtype[1],
                         f"{dtype[0]}-{y}{m:02d}-{did:04d}", d.isoformat(),
                         (d + dt.timedelta(days=30)).isoformat(), "warehouse_user",
                         supplier[0], supplier[1], "Garden stock",
                         "Garden bar" if dtype[0] == 2 else None,
                         money(net_total), money(vat_total),
                         1 if dtype[0] != 3 else 0, 0,
                         f"{dtype[1]} from {supplier[0]}", now_stamp(d)))
            did += 1

    # POS-issued (non-fiscal) invoices to companies, matching business bills
    for (bid, d, partner, tax, amount) in con.execute(
            "SELECT bill_id, date, business_partner_name, business_partner_tax, amount "
            "FROM bills_raw WHERE invoice_type = 'business' AND is_revoked = 0"):
        net = net_from_gross(amount, 20)
        out_inv.append((bid, f"OUT-POS-{bid}", d,
                        (dt.date.fromisoformat(d) + dt.timedelta(days=15)).isoformat(),
                        partner, tax, f"B{bid}", net, money(amount - net), amount,
                        amount if rng.random() < 0.7 else 0.0,
                        "Consumption on the premises", 0, now_stamp()))

    con.executemany(
        "INSERT INTO documents_raw (document_id, document_type_id, document_type_name, "
        "full_number, document_date, due_date, entered_user, business_partner_name, "
        "business_partner_tax, stock_warehouse_name, to_stock_warehouse_name, "
        "amount_without_tax, amount_tax, affect_stock, is_deleted, description, "
        "ingested_at) VALUES (" + ",".join("?" * 17) + ")", docs)
    con.executemany(
        "INSERT INTO document_items_raw (document_id, document_date, product_id, "
        "product_code, product_name, category_name, quantity, price, price_total, "
        "tax_percent, unit_measure, ingested_at) VALUES (" + ",".join("?" * 12) + ")",
        items)
    con.executemany(
        "INSERT INTO output_invoices_raw (document_id, full_number, document_date, "
        "due_date, business_partner_name, business_partner_tax, fiscal_receipt_number, "
        "amount_net, amount_vat, amount_gross, payed_amount, description, is_deleted, "
        "ingested_at) VALUES (" + ",".join("?" * 14) + ")", out_inv)


def generate_host_expenses(con, start: dt.date, end: dt.date, seed: int) -> None:
    """Entertainment/host expense documents, and the manual payment register for
    company bills settled outside the POS."""
    rng = random.Random(seed + 83)
    rows = []
    did = 1
    for (y, m) in months_between(start, end):
        dim = days_in_month(y, m)
        for _ in range(rng.randint(2, 6)):
            d = dt.date(y, m, rng.randint(1, dim))
            if d > end:
                continue
            net = money(round(rng.uniform(4000, 48000) / 100) * 100)
            vat = money(net * 0.2)
            rows.append((did, f"REP-{y}{m:02d}-{did:03d}", d.isoformat(),
                         rng.choice(["Client hosting", "Partner meeting",
                                     "Press visit", "Artist hospitality"]),
                         rng.choice([e[0] for e in C.EMPLOYEES[:4]]),
                         rng.choice([c[0] for c in C.CLIENTS]),
                         net, vat, money(net + vat),
                         "Hospitality expense, input VAT not deductible", 0,
                         now_stamp(d)))
            did += 1

    con.executemany(
        "INSERT INTO reprezentacija_documents (document_id, external_number, date, "
        "category, employee_name, business_partner_name, amount_net, amount_vat, "
        "amount_gross, description, is_deleted, ingested_at) "
        "VALUES (" + ",".join("?" * 12) + ")", rows)

    manual = []
    for (fr, amount, d) in con.execute(
            "SELECT fiscal_receipt_number, amount, date FROM bills_raw "
            "WHERE invoice_type = 'business' AND is_revoked = 0 LIMIT 400"):
        if rng.random() < 0.55:
            pd = dt.date.fromisoformat(d) + dt.timedelta(days=rng.randint(5, 40))
            if pd > end:
                continue
            manual.append((fr, amount, pd.isoformat(),
                           "Settled by bank transfer", now_stamp(pd)))
    con.executemany(
        "INSERT INTO prenosi_manual_payments (fiscal_receipt_number, paid_amount, "
        "paid_date, notes, updated_at) VALUES (?,?,?,?,?)", manual)

    # a working payment proposal over the oldest unpaid invoices
    proposal = []
    for (iid, net, vat) in con.execute(
            "SELECT invoice_id, net_amount, pdv_amount FROM invoices_in "
            "WHERE payment_status = 'Unpaid' "
            "AND COALESCE(invoice_status,'Approved') NOT IN ('Storno','Rejected') "
            "ORDER BY due_date LIMIT 25"):
        proposal.append((iid, money(net + vat), now_stamp(end)))
    con.executemany(
        "INSERT INTO payment_proposal (invoice_id, proposed_amount, updated_at) "
        "VALUES (?,?,?)", proposal)


def generate_trial_balance(con, start: dt.date, end: dt.date, seed: int) -> None:
    """Trial balance snapshots imported from the bookkeeper, plus the mapping
    that turns account numbers into financial statement lines."""
    rng = random.Random(seed + 89)
    # fs_mapping is deliberately left empty: the statements engine seeds it
    # from its own built-in chart of accounts on first run, and that bootstrap
    # is worth exercising rather than bypassing.
    con.executemany(
        "INSERT INTO fs_params (key, value, note) VALUES (?,?,?)",
        [("corporate_tax_rate", "15", "Statutory rate, percent"),
         ("reporting_currency", "RSD", None),
         ("rounding", "1", "Statements are presented in whole RSD"),
         ("depreciation_method", "straight_line", None),
         ("fiscal_year_end", "12-31", None)])

    years = sorted({y for y, _ in months_between(start, end)})
    sid = 1
    for y in years:
        period_from = dt.date(y, 1, 1)
        period_to = min(dt.date(y, 12, 31), end)
        if period_to < start:
            continue
        is_closing = 1 if period_to.month == 12 and period_to.day == 31 else 0

        revenue = con.execute(
            "SELECT COALESCE(SUM(sales_net_auto), 0) + COALESCE(SUM(venue_rent), 0) "
            "+ COALESCE(SUM(ticket_sales_own), 0) + COALESCE(SUM(other_revenues), 0) "
            "FROM events_daily_log WHERE substr(date,1,4) = ?", (str(y),)).fetchone()[0]
        cogs = con.execute(
            "SELECT COALESCE(SUM(cogs), 0) FROM events_daily_log "
            "WHERE substr(date,1,4) = ?", (str(y),)).fetchone()[0]
        staff = con.execute(
            "SELECT COALESCE(SUM(gross_cost), 0) FROM wages_daily "
            "WHERE substr(date,1,4) = ?", (str(y),)).fetchone()[0]

        lines = [
            ("600", "Revenue from services", 0.0, money(revenue * 0.82)),
            ("604", "Revenue from goods sold", 0.0, money(revenue * 0.18)),
            ("501", "Cost of goods sold", money(cogs), 0.0),
            ("520", "Staff costs: wages", money(staff), 0.0),
            ("521", "Staff costs: contributions", money(staff * 0.21), 0.0),
            ("540", "Depreciation", money(rng.uniform(900000, 1400000)), 0.0),
            ("550", "Other operating expenses", money(revenue * 0.21), 0.0),
            ("562", "Interest expense", money(rng.uniform(60000, 180000)), 0.0),
            ("241", "Cash at bank", money(rng.uniform(1200000, 3400000)), 0.0),
            ("204", "Trade receivables", money(rng.uniform(800000, 2200000)), 0.0),
            ("132", "Inventory", money(rng.uniform(900000, 1800000)), 0.0),
            ("023", "Equipment", money(rng.uniform(5000000, 7000000)), 0.0),
            ("270", "Input VAT", money(rng.uniform(200000, 700000)), 0.0),
            ("300", "Share capital", 0.0, 500000.0),
            ("340", "Retained earnings", 0.0, money(rng.uniform(1500000, 4200000))),
            ("435", "Trade payables", 0.0, money(rng.uniform(1400000, 3600000))),
            ("470", "Output VAT", 0.0, money(rng.uniform(300000, 900000))),
            ("465", "Other liabilities", 0.0, money(rng.uniform(200000, 800000))),
        ]
        total_d = money(sum(l[2] for l in lines))
        total_p = money(sum(l[3] for l in lines))

        con.execute(
            "INSERT INTO bb_snapshots (id, period_from, period_to, company, pib, fmt, "
            "source_file, stored_path, file_hash, is_closing, line_count, checks_json, "
            "warnings_json, checks_ok, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, period_from.isoformat(), period_to.isoformat(),
             C.ENTITIES["main"]["name"], C.ENTITIES["main"]["pib"], "xlsx",
             f"trial_balance_{y}.xlsx", f"imports/trial_balance_{y}.xlsx",
             f"{abs(hash((y, sid))):032x}"[:32], is_closing, len(lines),
             '{"debit_credit_balanced": true}',
             '[]' if abs(total_d - total_p) < 1 else '["Debit and credit totals differ"]',
             1 if abs(total_d - total_p) < 1 else 0,
             "Imported from the bookkeeper's year-end pack."))

        con.executemany(
            "INSERT INTO bb_lines (snapshot_id, konto, naziv, ps_d, ps_p, tp_d, tp_p, "
            "uk_d, uk_p) VALUES (?,?,?,?,?,?,?,?,?)",
            [(sid, k, n, 0.0, 0.0, d, p, d, p) for k, n, d, p in lines])
        sid += 1

    con.executemany(
        "INSERT INTO fs_exclusions (year, konto, amount, reason) VALUES (?,?,?,?)",
        [(years[0], "550", 240000.0, "One-off legal settlement, excluded from the "
                                     "comparable operating result."),
         (years[-1], "562", 95000.0, "Interest on a shareholder loan, presented "
                                     "separately.")])


def generate_ingestion_log(con, start: dt.date, end: dt.date, seed: int) -> None:
    """The audit trail the real pipeline writes on every run, including the
    failures that make a backfill necessary."""
    rng = random.Random(seed + 97)
    rows, pos_rows, errors = [], [], []
    d = start
    eid = 1
    while d <= end:
        for source in ("pos_bills", "pos_items", "e_invoices_in", "e_invoices_out",
                       "bank_xml", "stock_levels"):
            found = rng.randint(0, 600)
            new = int(found * rng.uniform(0.6, 1.0))
            status, detail = "ok", None
            if rng.random() < 0.015:
                status = "error"
                detail = rng.choice([
                    "ConnectionResetError(10054): connection forcibly closed by remote host",
                    "HTTP 502 from upstream gateway",
                    "Read timeout after 60s",
                    "Unexpected empty payload for the requested date range",
                ])
                new = 0
                errors.append((d.isoformat(), source, detail, "open", None,
                               "Retried on the next scheduled run.", now_stamp(d)))
                eid += 1
            elif rng.random() < 0.02:
                status = "partial"
                detail = "Some records skipped: duplicate primary keys"
            rows.append((now_stamp(d), source, d.isoformat(), d.isoformat(), found, new,
                         found - new, 0 if status == "ok" else rng.randint(1, 12),
                         status, detail, None))
        if d.toordinal() % 3 == 0:
            for endpoint in ("/bills", "/bill-items", "/documents", "/stock"):
                total = rng.randint(50, 900)
                pos_rows.append((d.isoformat(), endpoint, total,
                                 int(total * rng.uniform(0.7, 1.0)),
                                 rng.randint(0, 20), rng.randint(0, 2000),
                                 "ok", now_stamp(d)))
        d += dt.timedelta(days=1)

    # close out most of the historical errors
    resolved = []
    for e in errors:
        if (end - dt.date.fromisoformat(e[0])).days > 21 and rng.random() < 0.85:
            resolved.append((e[0], e[1], e[2], "resolved",
                             (dt.date.fromisoformat(e[0]) + dt.timedelta(
                                 days=rng.randint(1, 5))).isoformat(),
                             "Resolved by the retry wrapper added to the connector.",
                             e[6]))
        else:
            resolved.append(e)

    con.executemany(
        "INSERT INTO ingestion_log (run_at, source, date_from, date_to, records_found, "
        "records_new, records_skipped, records_error, status, error_detail, notes) "
        "VALUES (" + ",".join("?" * 11) + ")", rows)
    con.executemany(
        "INSERT INTO pos_ingestion_log (date, endpoint, records_total, records_new, "
        "records_updated, items_written, status, run_at) VALUES (?,?,?,?,?,?,?,?)",
        pos_rows)
    con.executemany(
        "INSERT INTO ingestion_errors (date_detected, source, description, status, "
        "resolved_at, notes, created_at) VALUES (?,?,?,?,?,?,?)", resolved)
