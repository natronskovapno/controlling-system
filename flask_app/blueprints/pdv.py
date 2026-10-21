"""
blueprints/pdv.py
=================
PDV (VAT) module — Garden Stage Controlling System

TABS:
  pregled     — Monthly breakdown by entity (Garden Stage / Small Brewery / Ukupno)
  stavke      — Full transaction-level list, sortable and filterable
  metodologija — Live methodology reference (rendered from this docstring)

STREAMS:

  1. Ulazni PDV (pretporez — deductible input VAT)
     Source:  invoices_in.pdv_amount
     Period:  date_received (prijem) — DeliveryDate from SEF API
     Why:     Serbian PDV rules require the receipt date (prijem), not the
              invoice issue date, to determine the correct filing period.
              date_received = SEF DeliveryDate field.
     Entities: main + brewery (separate columns)

  2. Izlazni PDV (output VAT — B2B invoices issued)
     Source:  invoices_out.pdv_amount
     Period:  date_issued
     Rate:    20% (stored correctly in pdv_rate column)
     Entities: main + brewery

  3. Pazar PDV (output VAT — POS bar sales)
     Source:  bills_raw.amount (100% complete — authoritative revenue source)
     Period:  bill date
     Formula: (SUM(bills_raw.amount) - ticket_gross) x 20 / 120
     Tickets: 'Tikets karta - ulaznica' items are 0% VAT pass-through and are
              subtracted before applying the 20% rate. Ticket subtotal sourced
              from bill_items_raw (product-level) to avoid double-counting.
     Rate:    20% flat (virtually all POS items; tickets excluded explicitly)
     Entity:  main only

  4. Parking PDV (output VAT — parking machine income)
     Source:  invoice_fixed_cost_links where tag type = income AND vat_rate > 0
     Period:  year + month from fixed cost link record
     Formula: amount x vat_rate / (100 + vat_rate)  [gross-to-PDV extraction]
     Why:     Parking revenue collected via separate POS (not POS). Gross
              amounts entered manually in Fixed Costs module with VAT rate set
              per link. PDV extracted here for inclusion in the VAT summary.
     Entity:  main only

  5. Small Brewery Pazar PDV (output VAT — bank-settled retail/delivery sales)
     Source:  brewery_transactions, direction = 'credit'
     Period:  transaction date
     Formula: SUM(amount) x 20 / 120  [gross-to-PDV extraction, 20% flat rate]
     Why:     Small Brewery / Brewery has no fiscal POS (POS) integration, so
              bank settlement is the only available revenue record for retail
              taproom sales and Wolt delivery. All non-internal-transfer credit
              inflows are treated as taxable sales revenue.
     Exclusions: internal capital transfers (owner loans/injections) are
              excluded using the same rule as the Transakcije tab in
              brewery.py — purposecode = '290' OR counterparty contains
              'CENTRALA'.
     Entity:  brewery only

FORMULA:
  Neto = Pazar + Izlazni + Parking - Ulazni
  (Pazar now includes both Garden Stage POS sales and Small Brewery bank-settled
  sales; see streams 3 and 5 above.)
  Positive value = liability to state (you owe PDV)
  Negative value = refund position (state owes you)

KNOWN ISSUES:
  pdv_rate column in invoices_in is stored as 0.0 for all rows — SEF parser
  does not extract the rate field. PDV amounts are correct; rate display shows
  implied rate (pdv / net x 100) as a workaround.

  Small Brewery bank statement coverage has known gaps (e.g. Feb/Mar/Jun 2026
  show no sales inflow at all, and Jan 2026 is unusually low) — these months
  are missing source statement files, not zero-revenue months. Figures will
  update automatically once the missing statements are ingested.

PENDING BOOKKEEPER VERIFICATION:
  - Confirm date_received is the correct prijem date for all supplier types
  - Confirm parking VAT rate (currently set per-link in Fixed Costs module)
  - Confirm no other zero-rate or reduced-rate items exist in POS sales
  - Confirm 20% flat-rate treatment is correct for all Small Brewery bank-settled
    sales (no zero-rate or reduced-rate items currently excluded, unlike the
    Garden Stage Pazar stream which excludes 0%-VAT ticket sales)
"""

from flask import Blueprint, render_template, request
from db import get_db

import os
import sys

bp = Blueprint("pdv", __name__, url_prefix="/pdv")

# ---------------------------------------------------------------------------
# Shared PDV computation
#
# The monthly pregled, the stream SQL, and the constants below all live in
# pdv_lib.py at the project root, shared with scripts/db_dump.py. Both used to
# carry independent copies that drifted three times -- most recently the
# Storno/Rejected filter landed here but not in db_dump.py, leaving the CSV
# overstating VAT by 813,517 across eleven months.
#
# _get_stavke() below is dashboard-only (transaction-level list) and stays here,
# but it reuses the same constants so the two views cannot diverge.
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    import pdv_lib
except ImportError:
    raise SystemExit(
        "\nERROR: cannot import pdv_lib.py\n"
        f"  expected at: {os.path.join(_ROOT, 'pdv_lib.py')}\n"
        "  The PDV module now shares its computation with scripts/db_dump.py.\n"
    )

DATA_FROM            = pdv_lib.DATA_FROM
LINK_ENTITY_JOIN     = pdv_lib.LINK_ENTITY_JOIN
LINK_ENTITY_EXPR     = pdv_lib.LINK_ENTITY_EXPR
VALID_INVOICE_STATUS = pdv_lib.VALID_INVOICE_STATUS

_month_label = pdv_lib._month_label
_ef          = pdv_lib._ef
_get_pregled = pdv_lib.get_pregled


def _get_stavke(db, date_from, date_to, direction, entity, sort, order):
    rows = []
    ef = _ef(date_from)

    # Ulazni — invoices_in (prijem date)
    if direction in ("", "ulazni"):
        w = ("WHERE pdv_amount IS NOT NULL AND pdv_amount != 0 "
             f"AND {VALID_INVOICE_STATUS} AND date_received >= ?")
        p = [ef]
        if date_to: w += " AND date_received <= ?"; p.append(date_to)
        if entity:  w += " AND entity = ?";          p.append(entity)
        for r in db.execute(f"""
            SELECT date_received AS date, supplier_name AS counterparty,
                   invoice_number_sef AS reference, entity,
                   net_amount, pdv_amount,
                   ROUND(pdv_amount * 100.0 / NULLIF(net_amount, 0), 0) AS rate,
                   payment_status, 'ulazni' AS direction
            FROM invoices_in {w}
        """, p).fetchall():
            rows.append(dict(r))

    # Izlazni — invoices_out
    if direction in ("", "izlazni"):
        w = ("WHERE pdv_amount IS NOT NULL AND pdv_amount != 0 "
             f"AND {VALID_INVOICE_STATUS} AND date_issued >= ?")
        p = [ef]
        if date_to: w += " AND date_issued <= ?"; p.append(date_to)
        if entity:  w += " AND entity = ?";        p.append(entity)
        for r in db.execute(f"""
            SELECT date_issued AS date, client_name AS counterparty,
                   invoice_number_sef AS reference, entity,
                   net_amount, pdv_amount, pdv_rate AS rate,
                   payment_status, 'izlazni' AS direction
            FROM invoices_out {w}
        """, p).fetchall():
            rows.append(dict(r))

    # Pazar — monthly POS aggregates (main only)
    # Source: bills_raw gross minus zero-VAT ticket items, x 20/120
    if direction in ("", "pazar") and entity in ("", "main"):
        w = "WHERE b.is_revoked = 0 AND b.date >= ?"
        p = [ef]
        if date_to: w += " AND b.date <= ?"; p.append(date_to)
        for r in db.execute(f"""
            SELECT strftime('%Y-%m-01', b.date) AS date,
                   'POS / Pazar' AS counterparty,
                   strftime('%Y-%m', b.date) AS reference,
                   'main' AS entity,
                   ROUND(
                     (SUM(b.amount) - COALESCE((
                       SELECT SUM(i.total_price)
                       FROM bill_items_raw i
                       JOIN bills_raw b2 ON b2.bill_id = i.bill_id
                       WHERE i.product_name = 'Tikets karta - ulaznica'
                         AND b2.is_revoked = 0
                         AND strftime('%Y-%m', b2.date) = strftime('%Y-%m', b.date)
                     ), 0)) / 1.2
                   , 0) AS net_amount,
                   ROUND(
                     (SUM(b.amount) - COALESCE((
                       SELECT SUM(i.total_price)
                       FROM bill_items_raw i
                       JOIN bills_raw b2 ON b2.bill_id = i.bill_id
                       WHERE i.product_name = 'Tikets karta - ulaznica'
                         AND b2.is_revoked = 0
                         AND strftime('%Y-%m', b2.date) = strftime('%Y-%m', b.date)
                     ), 0)) * 20.0 / 120.0
                   , 0) AS pdv_amount,
                   20.0 AS rate,
                   'N/A' AS payment_status,
                   'pazar' AS direction
            FROM bills_raw b
            {w}
            GROUP BY strftime('%Y-%m', b.date)
        """, p).fetchall():
            rows.append(dict(r))

    # Parking — fixed cost income links with vat_rate > 0
    # Entity resolved from the linked source row (see LINK_ENTITY_JOIN); the
    # previous literal 'main' silently dropped any brewery link.
    if direction in ("", "parking"):
        w = f"""JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
            {LINK_ENTITY_JOIN}
            WHERE t.type = 'income' AND l.vat_rate > 0
              AND l.year || '-' || printf('%02d', l.month) >= ?"""
        p = [ef[:7]]
        if date_to:
            w += " AND l.year || '-' || printf('%02d', l.month) <= ?"
            p.append(date_to[:7])
        if entity:
            w += f" AND {LINK_ENTITY_EXPR} = ?"
            p.append(entity)
        for r in db.execute(f"""
            SELECT printf('%04d-%02d-01', l.year, l.month) AS date,
                   COALESCE(t.name, 'Parking') AS counterparty,
                   printf('%04d-%02d', l.year, l.month) AS reference,
                   {LINK_ENTITY_EXPR} AS entity,
                   ROUND(SUM(l.amount / (1.0 + l.vat_rate/100.0)), 0) AS net_amount,
                   ROUND(SUM(l.amount * l.vat_rate / (100.0 + l.vat_rate)), 0) AS pdv_amount,
                   l.vat_rate AS rate,
                   'N/A' AS payment_status,
                   'parking' AS direction
            FROM invoice_fixed_cost_links l
            {w}
            GROUP BY printf('%04d-%02d', l.year, l.month), t.name, {LINK_ENTITY_EXPR}
        """, p).fetchall():
            rows.append(dict(r))

    # Small Brewery Pazar — bank credit inflows (no fiscal POS for this branch;
    # bank settlement is the only revenue record). Same exclusion rule for
    # internal capital transfers as brewery.py.
    if direction in ("", "pazar") and entity in ("", "brewery"):
        w = "WHERE entity = 'brewery' AND direction = 'credit' AND date >= ?"
        p = [ef]
        if date_to: w += " AND date <= ?"; p.append(date_to)
        w += """
            AND NOT (
                COALESCE(purposecode, '') = '290'
                OR UPPER(COALESCE(counterparty, '')) LIKE '%CENTRALA%'
            )"""
        for r in db.execute(f"""
            SELECT strftime('%Y-%m-01', date) AS date,
                   'Prodaja (bankovni priliv)' AS counterparty,
                   strftime('%Y-%m', date) AS reference,
                   'brewery' AS entity,
                   ROUND(SUM(amount) / 1.2, 0) AS net_amount,
                   ROUND(SUM(amount) * 20.0 / 120.0, 0) AS pdv_amount,
                   20.0 AS rate,
                   'N/A' AS payment_status,
                   'pazar' AS direction
            FROM brewery_transactions
            {w}
            GROUP BY strftime('%Y-%m', date)
        """, p).fetchall():
            rows.append(dict(r))

    valid_sorts = ("date","counterparty","entity","net_amount","pdv_amount","rate","direction")
    sort_key = sort if sort in valid_sorts else "date"
    rows.sort(key=lambda r: (r.get(sort_key) or ""), reverse=(order == "desc"))

    return rows


def _parse_metodologija():
    """Parse module docstring into sections for the Metodologija tab."""
    import re as _re
    import sys as _sys
    raw = (_sys.modules[__name__].__doc__ or "").strip()
    sections = []
    current_title = None
    current_lines = []
    for ln in raw.splitlines():
        m = _re.match(r"^([A-Z][A-Z/ ]{2,}):?\s*$", ln.rstrip())
        if m:
            if current_title is not None:
                sections.append({
                    "title": current_title,
                    "body": "\n".join(current_lines).strip()
                })
            current_title = m.group(1).strip().rstrip(":")
            current_lines = []
        elif current_title is not None:
            current_lines.append(ln)
    if current_title is not None:
        sections.append({
            "title": current_title,
            "body": "\n".join(current_lines).strip()
        })
    return sections


@bp.route("/")
def index():
    db = get_db()

    tab       = request.args.get("tab", "pregled")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    direction = request.args.get("direction", "")
    entity    = request.args.get("entity", "")
    sort      = request.args.get("sort", "date")
    order     = request.args.get("order", "desc")

    pregled = _get_pregled(db, date_from, date_to) if tab == "pregled" else []
    stavke  = _get_stavke(db, date_from, date_to, direction, entity, sort, order) if tab == "stavke" else []

    totals = {
        "ak_ulazni":    sum(r["ak_ulazni"]    for r in pregled),
        "ak_izlazni":   sum(r["ak_izlazni"]   for r in pregled),
        "ak_pazar":     sum(r["ak_pazar"]      for r in pregled),
        "ak_parking":   sum(r["ak_parking"]    for r in pregled),
        "ak_net":       sum(r["ak_net"]        for r in pregled),
        "mp_ulazni":    sum(r["mp_ulazni"]     for r in pregled),
        "mp_izlazni":   sum(r["mp_izlazni"]    for r in pregled),
        "mp_pazar":     sum(r["mp_pazar"]      for r in pregled),
        "brewery_parking":   sum(r["brewery_parking"]    for r in pregled),
        "mp_net":       sum(r["mp_net"]        for r in pregled),
        "total_ulazni": sum(r["total_ulazni"]  for r in pregled),
        "total_izlazni":sum(r["total_izlazni"] for r in pregled),
        "total_pazar":  sum(r["total_pazar"]   for r in pregled),
        "total_parking":sum(r["total_parking"] for r in pregled),
        "total_net":    sum(r["total_net"]     for r in pregled),
    }

    stavke_totals = {
        "net":     sum(float(r.get("net_amount") or 0) for r in stavke),
        "pdv":     sum(float(r.get("pdv_amount") or 0) for r in stavke),
        "ulazni":  sum(float(r.get("pdv_amount") or 0) for r in stavke if r["direction"] == "ulazni"),
        "izlazni": sum(float(r.get("pdv_amount") or 0) for r in stavke if r["direction"] == "izlazni"),
        "pazar":   sum(float(r.get("pdv_amount") or 0) for r in stavke if r["direction"] == "pazar"),
        "parking": sum(float(r.get("pdv_amount") or 0) for r in stavke if r["direction"] == "parking"),
    }

    def sort_url(col):
        new_order = "asc" if (sort == col and order == "desc") else "desc"
        return f"?tab=stavke&date_from={date_from}&date_to={date_to}&direction={direction}&entity={entity}&sort={col}&order={new_order}"

    metodologija = _parse_metodologija()

    return render_template("pdv/index.html",
        tab=tab, date_from=date_from, date_to=date_to,
        direction=direction, entity=entity, sort=sort, order=order,
        pregled=pregled, totals=totals,
        stavke=stavke, stavke_totals=stavke_totals,
        sort_url=sort_url,
        metodologija=metodologija,
    )
