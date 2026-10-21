"""Invoices, bank movements, fixed costs and VAT.

Everything here is tied back to the trading figures produced by the sales and
event generators, so purchases track turnover, bank movements settle real
invoices, and the VAT working ties to the invoice ledger.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import random

from . import catalog as C
from .helpers import (
    days_in_month, money, months_between, net_from_gross, now_stamp,
)

ACCOUNTS = {
    "main": "160-0000000001234-56",
    "main_2": "265-0000000009876-54",
    "brewery": "160-0000000005678-90",
}
ACCOUNT_LABELS = {
    "160-0000000001234-56": "Primary current account",
    "265-0000000009876-54": "Secondary current account",
    "160-0000000005678-90": "Brewery current account",
}

VAT_STREAMS_OUT = ["pos_sales", "event_invoices", "sponsorship", "parking", "sublease"]
VAT_STREAMS_IN = ["goods_20", "goods_10", "services_20", "utilities", "rent", "equipment"]


def _monthly_trading(plan) -> dict:
    """Aggregate the plan into per-month trading figures."""
    out = collections.defaultdict(lambda: {"gross": 0.0, "cogs": 0.0, "wages": 0.0,
                                           "days": 0, "net": 0.0})
    for day in plan:
        k = (day["date"].year, day["date"].month)
        m = out[k]
        m["gross"] += day["sales_gross"]
        m["cogs"] += day["cogs_net"]
        m["wages"] += day["wages_gross"]
        m["net"] += net_from_gross(day["sales_gross"], 19.2)
        m["days"] += 1
    return out


def generate(con, plan, tag_ids: dict, start: dt.date, end: dt.date, seed: int) -> dict:
    rng = random.Random(seed + 41)
    trading = _monthly_trading(plan)

    invoices_in, invoices_out, line_items = [], [], []
    fixed_links = []
    in_id = out_id = 1
    sef_in = 500000
    sef_out = 700000

    # ---------------------------------------------------------------- inbound
    # Stock purchases are driven by the actual cost of sales for the month, so
    # the purchase ledger stays in a believable relationship with turnover. A
    # small uplift represents the stock build rather than exact consumption.
    STOCK_CATEGORIES = ("Drinks", "Food", "Consumables")
    stock_weight_total = sum(s[3] * s[5] for s in C.SUPPLIERS
                             if s[2] in STOCK_CATEGORIES)

    for (y, m) in months_between(start, end):
        t = trading.get((y, m), {"gross": 0, "cogs": 0, "wages": 0, "days": 0})
        dim = days_in_month(y, m)
        # how busy the month was, used only for the demand-sensitive overheads
        activity = max(0.25, min(1.6, (t["gross"] or 0) / 7_500_000.0))
        stock_budget = (t["cogs"] or 0) * rng.uniform(1.02, 1.12)

        for name, pib, category, base, vat, cadence in C.SUPPLIERS:
            if category in STOCK_CATEGORIES:
                # this supplier's share of the month's stock purchases
                share = (base * cadence) / stock_weight_total
                budget = stock_budget * share
                n = max(1, round(cadence * (0.6 + 0.6 * activity)))
                per_invoice = budget / n
            else:
                n = cadence
                per_invoice = base
                if category == "Utilities":
                    per_invoice = base * (0.6 + 0.5 * activity)
                elif category in ("Equipment", "Marketing"):
                    per_invoice = base * (0.7 + 0.4 * activity)

            for k in range(n):
                day = min(dim, rng.randint(1, dim))
                issued = dt.date(y, m, day)
                if issued > end:
                    continue
                net = money(round(per_invoice * rng.uniform(0.82, 1.18) / 100) * 100)
                if net <= 0:
                    continue
                vat_amt = money(net * vat / 100.0)
                due = issued + dt.timedelta(days=rng.choice([15, 30, 30, 45]))
                received = issued + dt.timedelta(days=rng.choice([0, 0, 1, 2]))

                status, paid, pay_date = "Unpaid", 0.0, None
                age = (end - due).days
                if age > 10:
                    status, paid = "Paid", money(net + vat_amt)
                    pay_date = (due + dt.timedelta(days=rng.randint(-6, 9))).isoformat()
                elif age > -5 and rng.random() < 0.65:
                    status, paid = "Paid", money(net + vat_amt)
                    pay_date = (due + dt.timedelta(days=rng.randint(-4, 3))).isoformat()
                elif rng.random() < 0.08:
                    status, paid = "Partially paid", money((net + vat_amt) * 0.5)
                    pay_date = (due - dt.timedelta(days=2)).isoformat()

                inv_status = "Approved"
                if rng.random() < 0.015:
                    inv_status = rng.choice(["Storno", "Rejected"])
                    status, paid, pay_date = "Unpaid", 0.0, None

                invoices_in.append((
                    in_id, issued.isoformat(), received.isoformat(), due.isoformat(),
                    name, pib, f"IN-{sef_in}", f"{category[:3].upper()}-{y}{m:02d}-{k+1:03d}",
                    net, vat_amt, float(vat), f"S{int(vat)}", status, "main",
                    None, "SEF", now_stamp(issued), inv_status, category,
                    "Invoice", "RSD", None, now_stamp(issued), sef_in, paid, pay_date,
                ))
                line_items += _invoice_lines(rng, sef_in, "purchase", category, net, vat)

                tag = {
                    "Rent": "Rent", "Utilities": "Electricity", "Professional": "Bookkeeping",
                    "Services": "Cleaning", "Marketing": "Marketing retainer",
                    "IT": "POS licence", "Royalties": "Music royalties",
                    "Equipment": "Equipment maintenance",
                }.get(category)
                if tag and tag in tag_ids:
                    fixed_links.append(("invoice_in", in_id, tag_ids[tag], y, m,
                                        net, None, now_stamp(issued), float(vat)))
                in_id += 1
                sef_in += 1

    # --------------------------------------------------------------- outbound
    # Event-driven invoices
    for day in plan:
        d = day["date"]
        rent = None
        con_rows = con.execute(
            "SELECT venue_rent, third_party_ticket_revenue FROM events_daily_log "
            "WHERE event_id = ?", (day["event_id"],)).fetchone()
        if con_rows:
            rent = con_rows[0]
            third = con_rows[1]
        else:
            rent, third = 0.0, 0.0

        if rent and rent > 0 and day["organiser"] != "In-house":
            client = next((c for c in C.CLIENTS if c[0] == day["organiser"]), None)
            if client is None:
                client = rng.choice(C.CLIENTS)
            net = money(rent / 1.2)
            vat_amt = money(rent - net)
            issued = d + dt.timedelta(days=rng.randint(0, 4))
            if issued > end:
                continue
            due = issued + dt.timedelta(days=rng.choice([8, 15, 30]))
            paidq = (end - due).days > 3 or rng.random() < 0.6
            invoices_out.append((
                out_id, issued.isoformat(), due.isoformat(), client[0], client[1],
                f"OUT-{sef_out}", f"VR-{issued.year}-{out_id:04d}", net, vat_amt, 20.0,
                "S20", "Paid" if paidq else "Unpaid", "main",
                f"Venue rental: {day['event_name']} ({day['venue']})", "SEF",
                now_stamp(issued), "Approved", "Invoice", "RSD", None, now_stamp(issued),
                sef_out, money(net + vat_amt) if paidq else 0.0,
                (due + dt.timedelta(days=rng.randint(-3, 7))).isoformat() if paidq else None,
            ))
            line_items += _invoice_lines(rng, sef_out, "sale", "Venue rental", net, 20)
            out_id += 1
            sef_out += 1

    # Recurring outbound: parking, sublease, sponsorship
    for (y, m) in months_between(start, end):
        for client, pib, kind, base, vat in C.CLIENTS:
            if kind == "Parking":
                n, day_of = 1, 5
            elif kind == "Sublease":
                n, day_of = 1, 3
            elif kind == "Sponsor":
                n, day_of = (1 if m in (4, 7) else 0), 10
            elif kind == "Other":
                # Occasional, the way a one-off recharge actually arrives.
                n, day_of = (1 if m % 4 == 0 else 0), 18
            else:
                continue
            for _ in range(n):
                issued = dt.date(y, m, min(day_of, days_in_month(y, m)))
                if issued > end or issued < start:
                    continue
                net = money(round(base * rng.uniform(0.95, 1.07) / 100) * 100)
                vat_amt = money(net * vat / 100.0)
                due = issued + dt.timedelta(days=15)
                paidq = (end - due).days > 3
                invoices_out.append((
                    out_id, issued.isoformat(), due.isoformat(), client, pib,
                    f"OUT-{sef_out}", f"{kind[:3].upper()}-{y}{m:02d}", net, vat_amt,
                    float(vat), f"S{int(vat)}", "Paid" if paidq else "Unpaid", "main",
                    f"{kind} {y}-{m:02d}", "SEF", now_stamp(issued), "Approved",
                    "Invoice", "RSD", None, now_stamp(issued), sef_out,
                    money(net + vat_amt) if paidq else 0.0,
                    (due + dt.timedelta(days=rng.randint(-2, 6))).isoformat() if paidq else None,
                ))
                line_items += _invoice_lines(rng, sef_out, "sale", kind, net, vat)
                tag = {"Parking": "Parking income", "Sublease": "Sublease income",
                       "Sponsor": "Sponsorship income", "Other": "Other income"}[kind]
                fixed_links.append(("invoice_out", out_id, tag_ids[tag], y, m, net,
                                    None, now_stamp(issued), float(vat)))
                out_id += 1
                sef_out += 1

    con.executemany(
        "INSERT INTO invoices_in (invoice_id, date_issued, date_received, due_date, "
        "supplier_name, supplier_pib, invoice_number_sef, invoice_number_ext, net_amount, "
        "pdv_amount, pdv_rate, pdv_category, payment_status, entity, notes, source, "
        "created_at, invoice_status, supplier_category, document_type, currency, raw_json, "
        "ingested_at, invoice_id_sef, amount_paid, payment_date) "
        "VALUES (" + ",".join("?" * 26) + ")", invoices_in)

    con.executemany(
        "INSERT INTO invoices_out (invoice_id, date_issued, due_date, client_name, "
        "client_pib, invoice_number_sef, invoice_number_ext, net_amount, pdv_amount, "
        "pdv_rate, pdv_category, payment_status, entity, notes, source, created_at, "
        "invoice_status, document_type, currency, raw_json, ingested_at, invoice_id_sef, "
        "amount_paid, payment_date) VALUES (" + ",".join("?" * 24) + ")", invoices_out)

    con.executemany(
        "INSERT INTO invoice_line_items (invoice_id_sef, invoice_type, row_id, order_no, "
        "code, description, unit, unit_price, quantity, discount_percentage, "
        "discount_amount, net_amount, vat_rate, vat_amount, gross_amount, created_at) "
        "VALUES (" + ",".join("?" * 16) + ")", line_items)

    con.executemany(
        "INSERT INTO invoice_fixed_cost_links (source_type, source_id, tag_id, year, month, "
        "amount, notes, created_at, vat_rate) VALUES (?,?,?,?,?,?,?,?,?)", fixed_links)

    return {"invoices_in": len(invoices_in), "invoices_out": len(invoices_out),
            "line_items": len(line_items), "trading": trading}


def _invoice_lines(rng, sef_id, kind, category, net_total, vat):
    """Split an invoice into 1-4 plausible lines summing to the header total."""
    descriptions = {
        "Drinks": ["Draught lager keg 30l", "Bottled beer case", "Spirits assortment",
                   "Wine case 6x0.75", "Tonic water case", "Soft drinks mixed pallet"],
        "Food": ["Fresh produce delivery", "Frozen goods", "Bakery items",
                 "Dairy and cheese", "Meat and poultry"],
        "Utilities": ["Electricity supply", "Water supply and sewerage"],
        "Rent": ["Monthly lease: venue premises"],
        "Professional": ["Bookkeeping services"],
        "Services": ["Cleaning services", "Security staffing", "Signage maintenance"],
        "Equipment": ["Sound equipment rental", "Lighting rig rental", "Stage modules"],
        "Marketing": ["Social media management", "Print and outdoor advertising"],
        "IT": ["POS software licence"],
        "Consumables": ["Disposable cups and napkins", "Ice delivery"],
        "Royalties": ["Public performance royalties"],
        "Venue rental": ["Venue hire incl. basic technical setup"],
        "Sponsor": ["Sponsorship package: season visibility"],
        "Parking": ["Parking area operating fee"],
        "Sublease": ["Sublease of commercial space"],
        "Event organiser": ["Event cooperation fee"],
    }.get(category, ["Goods and services"])

    n = rng.randint(1, min(4, len(descriptions) + 1))
    weights = [rng.uniform(0.5, 1.5) for _ in range(n)]
    total_w = sum(weights)
    rows = []
    allocated = 0.0
    for i in range(n):
        if i == n - 1:
            net = money(net_total - allocated)
        else:
            net = money(net_total * weights[i] / total_w)
            allocated = money(allocated + net)
        if net <= 0:
            continue
        qty = float(rng.choice([1, 1, 2, 3, 5, 10, 12, 24]))
        unit_price = money(net / qty)
        vat_amt = money(net * vat / 100.0)
        rows.append((
            sef_id, kind, i + 1, i + 1, f"ART-{rng.randint(1000, 9999)}",
            rng.choice(descriptions), rng.choice(["pcs", "pcs", "kg", "l", "svc"]),
            unit_price, qty, 0.0, 0.0, net, float(vat), vat_amt,
            money(net + vat_amt), now_stamp(),
        ))
    return rows


def generate_bank(con, plan, start: dt.date, end: dt.date, seed: int) -> dict:
    """Bank movements: invoice settlement, cash deposits, card settlement,
    payroll, taxes and internal transfers. Then daily closing balances."""
    rng = random.Random(seed + 53)
    rows = []
    payments_made, payments_received = [], []
    pay_log = []

    # --- settle inbound invoices
    for (iid, pib, name, net, vat, pay_date, paid) in con.execute(
            "SELECT invoice_id, supplier_pib, supplier_name, net_amount, pdv_amount, "
            "payment_date, amount_paid FROM invoices_in WHERE payment_date IS NOT NULL"):
        d = dt.date.fromisoformat(pay_date)
        if d > end:
            continue
        ref = f"{rng.randint(100, 999)}-{rng.randint(10000, 99999)}"
        rows.append((d.isoformat(), ACCOUNTS["main"], None, paid, "debit", name,
                     f"Payment of invoice {iid}", None, ref, "RSD", 1, "main", "XML",
                     None, now_stamp(d), f"OUT-{iid}", "supplier_payment", None, None,
                     "221", 0, "Beograd", f"{pib}-ACC", "Demo Bank",
                     f"{d.year}-{d.month:02d}"))
        payments_made.append((d.isoformat(), name, f"Invoice {iid}", ref, paid, iid, None))
        pay_log.append(("in", iid, d.isoformat(), paid, "Settled from primary account"))

    # --- collect outbound invoices
    for (iid, pib, name, pay_date, paid) in con.execute(
            "SELECT invoice_id, client_pib, client_name, payment_date, amount_paid "
            "FROM invoices_out WHERE payment_date IS NOT NULL"):
        d = dt.date.fromisoformat(pay_date)
        if d > end:
            continue
        ref = f"{rng.randint(100, 999)}-{rng.randint(10000, 99999)}"
        rows.append((d.isoformat(), ACCOUNTS["main"], None, paid, "credit", name,
                     f"Settlement of invoice {iid}", ref, None, "RSD", 1, "main", "XML",
                     None, now_stamp(d), f"IN-{iid}", "customer_receipt", None, None,
                     "221", 0, "Beograd", f"{pib}-ACC", "Demo Bank",
                     f"{d.year}-{d.month:02d}"))
        payments_received.append((d.isoformat(), name, f"Invoice {iid}", ref, paid, iid, None))
        pay_log.append(("out", iid, d.isoformat(), paid, "Received on primary account"))

    # --- POS cash deposits and card settlement
    by_day = collections.defaultdict(lambda: {"cash": 0.0, "card": 0.0})
    for (d, ptype, amount) in con.execute(
            "SELECT date, payment_type_name, SUM(amount) FROM payment_hourly "
            "GROUP BY date, payment_type_name"):
        if ptype == "Cash":
            by_day[d]["cash"] += amount
        elif ptype == "Card":
            by_day[d]["card"] += amount

    for d_str, v in sorted(by_day.items()):
        d = dt.date.fromisoformat(d_str)
        if v["cash"] > 0:
            dep = d + dt.timedelta(days=1 if d.weekday() < 5 else (7 - d.weekday()))
            if dep <= end:
                amt = money(round(v["cash"] / 1000) * 1000)
                if amt > 0:
                    rows.append((dep.isoformat(), ACCOUNTS["main"], None, amt, "credit",
                                 "Cash deposit", f"Daily takings {d_str}", None, None,
                                 "RSD", 1, "main", "XML", None, now_stamp(dep),
                                 f"CASH-{d_str}", "cash_deposit", None, None, "289", 0,
                                 "Beograd", None, "Demo Bank", f"{dep.year}-{dep.month:02d}"))
        if v["card"] > 0:
            settle = d + dt.timedelta(days=2)
            if settle <= end:
                fee = money(v["card"] * 0.0145)
                rows.append((settle.isoformat(), ACCOUNTS["main"], None,
                             money(v["card"] - fee), "credit", "Card acquirer",
                             f"Card settlement {d_str}", None, None, "RSD", 1, "main",
                             "XML", None, now_stamp(settle), f"CARD-{d_str}",
                             "card_settlement", None, None, "289", 0, "Beograd", None,
                             "Demo Bank", f"{settle.year}-{settle.month:02d}"))

    # --- payroll and taxes
    wages_by_month = collections.defaultdict(float)
    for day in plan:
        wages_by_month[(day["date"].year, day["date"].month)] += day["wages_net"]
    salaried_net = sum(s[3] for s in C.EMPLOYEES if s[3])

    for (y, m) in months_between(start, end):
        pay_day = dt.date(y, m, min(10, days_in_month(y, m)))
        if m == 12:
            nxt = dt.date(y + 1, 1, 10)
        else:
            nxt = dt.date(y, m + 1, min(10, days_in_month(y, m + 1)))
        if nxt > end:
            continue
        casual = money(wages_by_month.get((y, m), 0.0))
        if casual:
            rows.append((nxt.isoformat(), ACCOUNTS["main"], None, casual, "debit",
                         "Payroll: temporary staff", f"Shift wages {y}-{m:02d}", None,
                         None, "RSD", 1, "main", "XML", None, now_stamp(nxt),
                         f"WG-{y}{m:02d}", "wages", None, None, "241", 0, "Beograd",
                         None, "Demo Bank", f"{y}-{m:02d}"))
        rows.append((nxt.isoformat(), ACCOUNTS["main"], None, float(salaried_net), "debit",
                     "Payroll: salaried staff", f"Salaries {y}-{m:02d}", None, None,
                     "RSD", 1, "main", "XML", None, now_stamp(nxt), f"SAL-{y}{m:02d}",
                     "wages", None, None, "241", 0, "Beograd", None, "Demo Bank",
                     f"{y}-{m:02d}"))
        taxes = money(salaried_net * 0.63 + casual * 0.26)
        rows.append((nxt.isoformat(), ACCOUNTS["main"], None, taxes, "debit",
                     "Tax Administration", f"Payroll taxes and contributions {y}-{m:02d}",
                     None, None, "RSD", 1, "main", "XML", None, now_stamp(nxt),
                     f"TAX-{y}{m:02d}", "payroll_tax", None, None, "254", 0, "Beograd",
                     None, "Demo Bank", f"{y}-{m:02d}"))

        # internal transfer between the two own accounts
        tdate = dt.date(y, m, min(20, days_in_month(y, m)))
        if tdate <= end:
            amt = money(round(rng.uniform(200000, 900000) / 1000) * 1000)
            rows.append((tdate.isoformat(), ACCOUNTS["main"], None, amt, "debit",
                         "Own account transfer", "Internal transfer", None, None, "RSD",
                         1, "main", "XML", None, now_stamp(tdate), f"TRF-O-{y}{m:02d}",
                         "internal_transfer", None, None, "290", 1, "Beograd",
                         ACCOUNTS["main_2"], "Demo Bank", f"{y}-{m:02d}"))
            rows.append((tdate.isoformat(), ACCOUNTS["main_2"], None, amt, "credit",
                         "Own account transfer", "Internal transfer", None, None, "RSD",
                         1, "main", "XML", None, now_stamp(tdate), f"TRF-I-{y}{m:02d}",
                         "internal_transfer", None, None, "290", 1, "Beograd",
                         ACCOUNTS["main"], "Demo Bank", f"{y}-{m:02d}"))

        # equipment loan: level monthly repayment
        loan_day = dt.date(y, m, min(25, days_in_month(y, m)))
        if loan_day <= end:
            rows.append((loan_day.isoformat(), ACCOUNTS["main"], None, 318000.0, "debit",
                         "Demo Bank a.d.", "Equipment loan instalment", None, None,
                         "RSD", 1, "main", "XML", None, now_stamp(loan_day),
                         f"LOAN-{y}{m:02d}", "loan_repayment", None, None, "233", 0,
                         "Beograd", None, "Demo Bank", f"{y}-{m:02d}"))

        # owner distribution, taken quarterly out of the summer surplus
        if m in (3, 6, 9, 12):
            dist = dt.date(y, m, min(28, days_in_month(y, m)))
            if dist <= end:
                amt = money(round(rng.uniform(600000, 2400000) / 1000) * 1000)
                rows.append((dist.isoformat(), ACCOUNTS["main"], None, amt, "debit",
                             "Shareholder distribution", "Profit distribution", None,
                             None, "RSD", 1, "main", "XML", None, now_stamp(dist),
                             f"DIV-{y}{m:02d}", "owner_distribution", None, None,
                             "289", 0, "Beograd", None, "Demo Bank", f"{y}-{m:02d}"))

        # bank charges
        rows.append((nxt.isoformat(), ACCOUNTS["main"], None,
                     money(rng.uniform(4000, 11000)), "debit", "Demo Bank a.d.",
                     f"Account maintenance and charges {y}-{m:02d}", None, None, "RSD",
                     1, "main", "XML", None, now_stamp(nxt), f"FEE-{y}{m:02d}",
                     "bank_charges", None, None, "290", 0, "Beograd", None, "Demo Bank",
                     f"{y}-{m:02d}"))

    # VAT remittance, paid on the 15th for the prior month
    for (y, m) in months_between(start, end):
        due = dt.date(y, m, 15)
        if due > end or due < start + dt.timedelta(days=40):
            continue
        amt = money(round(rng.uniform(180000, 900000) / 100) * 100)
        rows.append((due.isoformat(), ACCOUNTS["main"], None, amt, "debit",
                     "Tax Administration", "VAT liability for the previous period",
                     None, None, "RSD", 1, "main", "XML", None, now_stamp(due),
                     f"VAT-{y}{m:02d}", "vat_payment", None, None, "253", 0, "Beograd",
                     None, "Demo Bank", f"{y}-{m:02d}"))

    # assign statement numbers per account per day order
    rows.sort(key=lambda r: (r[1], r[0]))
    numbered = []
    counters = collections.Counter()
    last_date = {}
    for r in rows:
        acct, d = r[1], r[0]
        if last_date.get(acct) != d:
            counters[acct] += 1
            last_date[acct] = d
        numbered.append((r[0], r[1], counters[acct]) + r[3:])

    con.executemany(
        "INSERT INTO bank_transactions (date, account, statement_number, amount, direction, "
        "counterparty, purpose, reference_in, reference_out, currency, allocated, entity, "
        "source, raw_xml_row, created_at, fitid, auto_tag, manual_tag, notes, purposecode, "
        "is_internal_transfer, trnplace, counterparty_account, counterparty_bank, "
        "payment_period) VALUES (" + ",".join("?" * 25) + ")", numbered)

    con.executemany(
        "INSERT INTO payments_made (date, receiver, purpose, reference, amount_paid, "
        "invoice_id, transaction_id) VALUES (?,?,?,?,?,?,?)", payments_made)
    con.executemany(
        "INSERT INTO payments_received (date, sender, purpose, reference, amount_received, "
        "invoice_id, transaction_id) VALUES (?,?,?,?,?,?,?)", payments_received)
    con.executemany(
        "INSERT INTO invoice_payment_log (direction, invoice_id, payment_date, amount, "
        "notes) VALUES (?,?,?,?,?)", pay_log)

    _generate_card_spend(con, start, end, rng)
    # balances are derived last, so they reflect every movement above
    _generate_balances(con, start, end)

    return {"bank_transactions": len(numbered)}


def _generate_balances(con, start: dt.date, end: dt.date) -> None:
    """Daily closing balance per account, derived from the transaction ledger."""
    # Opening cash is set so the winter trough stays positive but tight: the
    # seasonal squeeze is the whole reason the projections module exists.
    opening = {ACCOUNTS["main"]: 7400000.0, ACCOUNTS["main_2"]: 420000.0}
    moves = collections.defaultdict(float)
    for (d, acct, direction, amount) in con.execute(
            "SELECT date, account, direction, SUM(amount) FROM bank_transactions "
            "GROUP BY date, account, direction"):
        moves[(acct, d)] += amount if direction == "credit" else -amount

    rows = []
    for acct, open_bal in opening.items():
        bal = open_bal
        stmt = 0
        d = start
        while d <= end:
            delta = moves.get((acct, d.isoformat()), 0.0)
            if delta:
                stmt += 1
                bal = money(bal + delta)
                rows.append((acct, ACCOUNT_LABELS[acct], bal, d.isoformat(), stmt,
                             f"statement_{acct[-5:]}_{stmt}.xml", now_stamp(d)))
            d += dt.timedelta(days=1)

    con.executemany(
        "INSERT INTO account_balances (account_number, account_label, closing_balance, "
        "balance_date, statement_number, source_file, ingested_at) VALUES (?,?,?,?,?,?,?)",
        rows)


def _generate_card_spend(con, start: dt.date, end: dt.date, rng) -> None:
    """Small operational purchases on the company debit card."""
    vendors = ["Supermarket Central", "Hardware Store", "Fuel Station North",
               "Office Supplies Direct", "Courier Express", "Pharmacy Plus",
               "Online Ads Platform", "Cleaning Supplies Depot", "Flower Market"]
    classes = ["Bar consumables", "Repairs", "Transport", "Office", "Marketing",
               "Unclassified"]
    rows, bank_rows = [], []
    d = start
    while d <= end:
        if rng.random() < 0.55:
            for _ in range(rng.randint(1, 3)):
                amt = money(round(rng.uniform(900, 28000) / 10) * 10)
                v = rng.choice(vendors)
                bank_rows.append((d.isoformat(), ACCOUNTS["main"], None, amt, "debit", v,
                                  f"Card purchase: {v}", None, None, "RSD", 1, "main",
                                  "XML", None, now_stamp(d),
                                  f"CRD-{d.isoformat()}-{rng.randint(1000,9999)}",
                                  "card_purchase", None, None, "289", 0, "Beograd",
                                  None, "Demo Bank", f"{d.year}-{d.month:02d}"))
                rows.append((d.isoformat(), v, amt,
                             rng.choices(classes, [30, 14, 12, 10, 10, 24])[0], None, None))
        d += dt.timedelta(days=1)

    con.executemany(
        "INSERT INTO bank_transactions (date, account, statement_number, amount, direction, "
        "counterparty, purpose, reference_in, reference_out, currency, allocated, entity, "
        "source, raw_xml_row, created_at, fitid, auto_tag, manual_tag, notes, purposecode, "
        "is_internal_transfer, trnplace, counterparty_account, counterparty_bank, "
        "payment_period) VALUES (" + ",".join("?" * 25) + ")", bank_rows)

    # link each card line to its bank row
    ids = {r[0]: r[1] for r in con.execute(
        "SELECT fitid, transaction_id FROM bank_transactions WHERE auto_tag='card_purchase'")}
    linked = []
    for i, (d_str, v, amt, cls, _ev, _n) in enumerate(rows):
        fit = bank_rows[i][15]
        linked.append((ids.get(fit, 0), d_str, v, amt, cls, None, None))
    con.executemany(
        "INSERT INTO debit_card_transactions (transaction_id, date, counterparty, amount, "
        "classification, event_id, notes) VALUES (?,?,?,?,?,?,?)", linked)


def generate_fixed_costs(con, start: dt.date, end: dt.date, trading: dict, seed: int) -> None:
    """Monthly fixed cost, income and other-cost tables, plus the electricity split."""
    rng = random.Random(seed + 59)
    fc, fi, fo, el = [], [], [], []

    for (y, m) in months_between(start, end):
        t = trading.get((y, m), {"gross": 0.0})
        activity = max(0.25, min(2.0, (t["gross"] or 0) / 9000000.0))
        dim = days_in_month(y, m)

        garden_kwh = round(2600 * (0.45 + 1.1 * activity) * rng.uniform(0.9, 1.1))
        brewery_kwh = round(4100 * rng.uniform(0.88, 1.12))
        rate = rng.uniform(14.5, 17.2)
        el.append((y, m, garden_kwh, brewery_kwh, garden_kwh + brewery_kwh,
                   money(garden_kwh * rate), money(brewery_kwh * rate),
                   money((garden_kwh + brewery_kwh) * rate)))

        fc.append((y, m,
                   money(garden_kwh * rate * 0.72),
                   money(garden_kwh * rate * 0.28),
                   480000.0,
                   money(34000 * (0.6 + 0.7 * activity)),
                   money(sum(s[3] for s in C.EMPLOYEES if s[3]) * 1.63),
                   money(round(rng.uniform(20000, 95000) / 1000) * 1000),
                   dim))

        fi.append((y, m,
                   money(round(175000 * rng.uniform(0.95, 1.07) / 100) * 100),
                   money(round(95000 * rng.uniform(0.98, 1.03) / 100) * 100)))

        fo.append((y, m,
                   52000.0,
                   money(round(rng.uniform(15000, 21000) / 100) * 100),
                   money(round(rng.uniform(10000, 14000) / 100) * 100),
                   money(44000 * (0.7 + 0.5 * activity)),
                   money(round(rng.uniform(12000, 48000) / 1000) * 1000),
                   45000.0,
                   28000.0,
                   money(round(rng.uniform(0, 19000) / 1000) * 1000),
                   money(round(rng.uniform(8000, 56000) / 1000) * 1000),
                   money(round(rng.uniform(6000, 14000) / 1000) * 1000),
                   money(round(rng.uniform(9000, 26000) / 1000) * 1000),
                   money(round(rng.uniform(3000, 31000) / 1000) * 1000)))

    con.executemany(
        "INSERT INTO electricity_split (year, month, garden_kwh, brewery_kwh, total_kwh, "
        "garden_amount, brewery_amount, total_amount) VALUES (?,?,?,?,?,?,?,?)", el)
    con.executemany(
        "INSERT INTO fixed_costs_monthly (year, month, electricity_garden, "
        "electricity_corner, rent, water, salaries, other, days_in_month) "
        "VALUES (?,?,?,?,?,?,?,?,?)", fc)
    con.executemany(
        "INSERT INTO fixed_income_monthly (year, month, parking_income, sublease_income) "
        "VALUES (?,?,?,?)", fi)
    con.executemany(
        "INSERT INTO fixed_other_monthly (year, month, bookkeeper, music_royalties_a, "
        "music_royalties_b, cleaning, maintenance, social_media, pos_provider, led_signage, "
        "produce, parking_ramp, debit_card_fixed, other) "
        "VALUES (" + ",".join("?" * 14) + ")", fo)


def generate_vat(con, start: dt.date, end: dt.date) -> None:
    """VAT working: output and input VAT per period, broken down by stream.

    Mirrors the real module: Storno and Rejected invoices are excluded, and
    each stream is attributed to the entity it belongs to.
    """
    rows = []
    for (y, m) in months_between(start, end):
        period = f"{y}-{m:02d}"
        first = dt.date(y, m, 1).isoformat()
        last = dt.date(y, m, days_in_month(y, m)).isoformat()

        pos_vat = con.execute(
            "SELECT COALESCE(SUM(bi.total_price - bi.total_price / (1 + bi.tax_percent/100.0)), 0) "
            "FROM bill_items_raw bi JOIN bills_raw b ON b.bill_id = bi.bill_id "
            "WHERE bi.date BETWEEN ? AND ? AND b.is_revoked = 0", (first, last)).fetchone()[0]
        rows.append((period, "pos_sales", money(pos_vat), "output",
                     "Fiscal receipts, revoked bills excluded"))

        for stream, like in (("event_invoices", "Venue rental%"),
                             ("sponsorship", "Sponsor%"),
                             ("parking", "Parking%"),
                             ("sublease", "Sublease%")):
            v = con.execute(
                "SELECT COALESCE(SUM(pdv_amount), 0) FROM invoices_out "
                "WHERE date_issued BETWEEN ? AND ? AND entity = 'main' "
                "AND COALESCE(invoice_status,'Approved') NOT IN ('Storno','Rejected') "
                "AND notes LIKE ?", (first, last, like)).fetchone()[0]
            rows.append((period, stream, money(v), "output", "From the invoice ledger"))

        for stream, cats, rate in (
                ("goods_20", ("Drinks",), 20),
                ("goods_10", ("Food", "Consumables"), None),
                ("services_20", ("Services", "Professional", "Marketing", "IT",
                                 "Royalties"), 20),
                ("utilities", ("Utilities",), None),
                ("rent", ("Rent",), 20),
                ("equipment", ("Equipment",), 20)):
            placeholders = ",".join("?" * len(cats))
            v = con.execute(
                f"SELECT COALESCE(SUM(pdv_amount), 0) FROM invoices_in "
                f"WHERE date_received BETWEEN ? AND ? AND entity = 'main' "
                f"AND COALESCE(invoice_status,'Approved') NOT IN ('Storno','Rejected') "
                f"AND supplier_category IN ({placeholders})",
                (first, last, *cats)).fetchone()[0]
            rows.append((period, stream, money(v), "input",
                         "Deductible input VAT, Storno/Rejected excluded"))

    con.executemany(
        "INSERT INTO pdv_working (period, stream, pdv_amount, direction, notes) "
        "VALUES (?,?,?,?,?)", rows)
    return len(rows)
