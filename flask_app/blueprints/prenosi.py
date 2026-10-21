"""
prenosi.py — Prenosi na racun (B2B / POS outgoing invoices)

Standalone module, not linked to invoices_in/invoices_out (SEF) or any
other blueprint. Two POS-native sources are reconciled here:

  bills_raw          — fiscal POS bills flagged as B2B (tr("Prenos na racun")):
                        business_partner_name populated, or table_name
                        starts with 'organizacija'. bills_raw.amount is
                        authoritative revenue (bill_items_raw is often
                        incomplete/zero-priced for these bills).
  output_invoices_raw — POS's native OutputInvoice document series
                        (GET /Document/Search?documentType=OutputInvoice).
                        Carries its own payment status (payed_amount) and
                        a fiscal_receipt_number parsed from Description,
                        which is the join key back to bills_raw.

Each row in the list is one of:
  - matched invoice+bill (amounts agree within tolerance)
  - matched invoice+bill, amount mismatch
  - invoice with no corresponding B2B bill found
  - B2B bill with no corresponding invoice found ("missing invoice")
"""

from flask import Blueprint, render_template, request
from db import get_db
from i18n import tr

bp = Blueprint("prenosi", __name__)

PER_PAGE = 40
AMOUNT_TOLERANCE = 1.0  # RSD, floating point / rounding slack


def _ensure_manual_payments_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS prenosi_manual_payments (
            fiscal_receipt_number TEXT PRIMARY KEY,
            paid_amount           REAL DEFAULT 0,
            paid_date             TEXT,
            notes                 TEXT,
            updated_at            TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _b2b_bills(db, date_from, date_to, venue):
    where = """
        WHERE is_revoked = 0
          AND (
            LOWER(TRIM(COALESCE(table_name,''))) LIKE 'organizacija%'
            OR (business_partner_name IS NOT NULL AND business_partner_name != '')
          )
    """
    params = []
    if date_from:
        where += " AND date >= ?"; params.append(date_from)
    if date_to:
        where += " AND date <= ?"; params.append(date_to)
    if venue:
        where += " AND venue = ?"; params.append(venue)

    rows = db.execute(f"""
        SELECT bill_id, fiscal_receipt_number, date, time_created, venue,
               table_name, business_partner_name, business_partner_tax,
               amount
        FROM bills_raw
        {where}
        ORDER BY date DESC, time_created DESC
    """, params).fetchall()
    return {r["fiscal_receipt_number"]: dict(r) for r in rows if r["fiscal_receipt_number"]}


def _output_invoices(db, date_from, date_to):
    where = "WHERE is_deleted = 0"
    params = []
    if date_from:
        where += " AND document_date >= ?"; params.append(date_from)
    if date_to:
        where += " AND document_date <= ?"; params.append(date_to)

    try:
        rows = db.execute(f"""
            SELECT document_id, full_number, document_date, due_date,
                   business_partner_name, business_partner_tax,
                   fiscal_receipt_number, amount_net, amount_vat, amount_gross,
                   payed_amount, description
            FROM output_invoices_raw
            {where}
            ORDER BY document_date DESC
        """, params).fetchall()
    except Exception:
        rows = []  # table may not exist yet on older databases
    return [dict(r) for r in rows]


def _payment_status(gross, payed):
    if not payed or payed <= 0:
        return "Unpaid"
    if payed >= gross - AMOUNT_TOLERANCE:
        return "Paid"
    return "Partial"


@bp.route("/")
def index():
    db = get_db()
    _ensure_manual_payments_table(db)

    manual = {r["fiscal_receipt_number"]: dict(r) for r in db.execute(
        "SELECT * FROM prenosi_manual_payments"
    ).fetchall()}

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    venue     = request.args.get("venue", "")
    search    = request.args.get("search", "")
    match_filter   = request.args.get("match_filter", "")     # matched|mismatch|no_bill|no_invoice
    payment_filter = request.args.get("payment_filter", "")   # Unpaid|Partial|Paid
    page = max(int(request.args.get("page", 1) or 1), 1)

    bills_by_receipt = _b2b_bills(db, date_from, date_to, venue)
    invoices = _output_invoices(db, date_from, date_to)

    rows = []
    matched_receipts = set()

    for inv in invoices:
        receipt = inv["fiscal_receipt_number"]
        bill = bills_by_receipt.get(receipt) if receipt else None
        if bill:
            matched_receipts.add(receipt)
            diff = round((inv["amount_gross"] or 0) - (bill["amount"] or 0), 2)
            match_status = "matched" if abs(diff) <= AMOUNT_TOLERANCE else "mismatch"
        else:
            diff = None
            match_status = "no_bill"

        rows.append({
            "full_number":     inv["full_number"],
            "document_date":   inv["document_date"],
            "due_date":        inv["due_date"],
            "partner_name":    inv["business_partner_name"],
            "partner_tax":     inv["business_partner_tax"],
            "amount_net":      inv["amount_net"],
            "amount_vat":      inv["amount_vat"],
            "amount_gross":    inv["amount_gross"],
            "payed_amount":    inv["payed_amount"],
            "payment_status":  _payment_status(inv["amount_gross"] or 0, inv["payed_amount"] or 0),
            "fiscal_receipt_number": receipt,
            "bill_id":         bill["bill_id"] if bill else None,
            "bill_venue":      bill["venue"] if bill else None,
            "bill_table":      bill["table_name"] if bill else None,
            "bill_amount":     bill["amount"] if bill else None,
            "amount_diff":     diff,
            "match_status":    match_status,
            "manual_paid":     False,
            "manual_notes":    None,
            "manual_paid_date": None,
        })
    # B2B bills with no matching invoice at all — surfaced as gaps
    for receipt, bill in bills_by_receipt.items():
        if receipt in matched_receipts:
            continue
        rows.append({
            "full_number":     None,
            "document_date":   bill["date"],
            "due_date":        None,
            "partner_name":    bill["business_partner_name"] or bill["table_name"],
            "partner_tax":     bill["business_partner_tax"],
            "amount_net":      None,
            "amount_vat":      None,
            "amount_gross":    None,
            "payed_amount":    None,
            "payment_status":  "N/A",
            "fiscal_receipt_number": receipt,
            "bill_id":         bill["bill_id"],
            "bill_venue":      bill["venue"],
            "bill_table":      bill["table_name"],
            "bill_amount":     bill["amount"],
            "amount_diff":     None,
            "match_status":    "no_invoice",
            "manual_paid":     False,
            "manual_notes":    None,
            "manual_paid_date": None,
        })

    # ── apply manual "mark as paid" overrides (bank-transfer reconciliation) ──
    for r in rows:
        m = manual.get(r["fiscal_receipt_number"])
        if not m:
            continue
        r["manual_paid"]  = True
        r["manual_notes"] = m["notes"]
        r["manual_paid_date"] = m["paid_date"]
        r["payed_amount"] = m["paid_amount"]
        reference = r["amount_gross"] if r["amount_gross"] else r["bill_amount"]
        r["payment_status"] = _payment_status(reference or 0, m["paid_amount"] or 0)

    if search:
        s = search.lower()
        rows = [r for r in rows if s in (r["partner_name"] or "").lower()]
    if match_filter:
        rows = [r for r in rows if r["match_status"] == match_filter]
    if payment_filter:
        rows = [r for r in rows if r["payment_status"] == payment_filter]

    rows.sort(key=lambda r: r["document_date"] or "", reverse=True)

    kpi = {
        "count":          len(rows),
        "total_gross":    sum(r["amount_gross"] or 0 for r in rows),
        "total_bill_amount": sum(r["bill_amount"] or 0 for r in rows),
        "total_unpaid":   sum((r["amount_gross"] or 0) - (r["payed_amount"] or 0)
                              for r in rows if r["payment_status"] in ("Unpaid", "Partial")),
        "n_mismatch":     sum(1 for r in rows if r["match_status"] == "mismatch"),
        "n_no_invoice":   sum(1 for r in rows if r["match_status"] == "no_invoice"),
        "n_no_bill":      sum(1 for r in rows if r["match_status"] == "no_bill"),
    }

    total = len(rows)
    total_pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * PER_PAGE: page * PER_PAGE]

    venues = [r[0] for r in db.execute(
        "SELECT DISTINCT venue FROM bills_raw WHERE is_revoked=0 ORDER BY venue"
    ).fetchall()]

    return render_template(
        "prenosi/index.html",
        rows=page_rows,
        kpi=kpi,
        venues=venues,
        page=page,
        total_pages=total_pages,
        count=total,
        filters=dict(date_from=date_from, date_to=date_to, venue=venue,
                     search=search, match_filter=match_filter,
                     payment_filter=payment_filter),
    )


@bp.route("/mark-paid", methods=["POST"])
def mark_paid():
    from flask import redirect, request as req
    db = get_db()
    _ensure_manual_payments_table(db)

    receipt = req.form.get("fiscal_receipt_number", "").strip()
    amount  = req.form.get("paid_amount", "").strip()
    paid_date = req.form.get("paid_date", "").strip()
    if not paid_date:
        from datetime import date as _date
        paid_date = _date.today().isoformat()
    notes   = req.form.get("notes", "").strip()

    if receipt:
        if amount:
            try:
                amount_f = float(amount)
            except ValueError:
                amount_f = 0
        else:
            # Fallback: recompute canonical amount server-side instead of
            # trusting a possibly-corrupted client round-trip.
            ref = db.execute(
                "SELECT amount_gross FROM output_invoices_raw WHERE fiscal_receipt_number = ?",
                (receipt,)
            ).fetchone()
            if ref and ref["amount_gross"]:
                amount_f = ref["amount_gross"]
            else:
                ref = db.execute(
                    "SELECT amount FROM bills_raw WHERE fiscal_receipt_number = ? LIMIT 1",
                    (receipt,)
                ).fetchone()
                amount_f = ref["amount"] if ref else 0
        db.execute("""
            INSERT INTO prenosi_manual_payments
                (fiscal_receipt_number, paid_amount, paid_date, notes, updated_at)
            VALUES (?,?,?,?, datetime('now'))
            ON CONFLICT(fiscal_receipt_number) DO UPDATE SET
                paid_amount=excluded.paid_amount,
                paid_date=excluded.paid_date,
                notes=excluded.notes,
                updated_at=excluded.updated_at
        """, (receipt, amount_f, paid_date or None, notes or None))
        db.commit()

    return redirect(req.referrer or "/prenosi/")


@bp.route("/unmark-paid", methods=["POST"])
def unmark_paid():
    from flask import redirect, request as req
    db = get_db()
    _ensure_manual_payments_table(db)
    receipt = req.form.get("fiscal_receipt_number", "").strip()
    if receipt:
        db.execute("DELETE FROM prenosi_manual_payments WHERE fiscal_receipt_number = ?", (receipt,))
        db.commit()
    return redirect(req.referrer or "/prenosi/")
