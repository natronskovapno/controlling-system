from flask import Blueprint, render_template, request
from db import get_db
from collections import defaultdict
from i18n import month_name, day_name, months as month_list

bp = Blueprint("discounts", __name__)

# Month names come from i18n so they follow the active language.

def _fmt_month(ym):
    try:
        y, m = ym.split("-")
        return f"{month_name(int(m))} {y}"
    except Exception:
        return ym


def _line_discount(total_price, discount_amount, quantity):
    """
    CRITICAL: bill_items_raw.discount_amount is recorded as a PER-UNIT
    discount rate, not the total discount for the line. A line with
    quantity=3 and discount_amount=205 means 205 RSD off PER UNIT, i.e.
    615 RSD off the whole line — not 205 RSD total.

    Verified against fiscal receipt 2QNGLZGL-2QNGLZGL-6747 (bill_id 56136,
    26.06.2026): treating discount_amount as a flat per-line total under-
    counted the bill's true original price by ~2000 RSD (9.860 vs the
    confirmed 11.980). Multiplying by quantity reconstructs the original
    exactly, and makes every product on that receipt resolve to a clean
    50% discount — consistent with the business's two real discount types
    (50% or 100%).

    total_price is already the actual CHARGED amount for the line (it
    matches the fiscal receipt exactly) — it is NOT pre-discount. The true
    pre-discount original is total_price + (discount_amount * quantity).
    """
    return discount_amount * quantity if quantity else discount_amount


def _classify_row(total_price, discount_amount, quantity):
    """
    Classify a single discounted line item as one of the two real discount
    types the business uses (50% off, or 100% off / fully comped), or
    'ostalo' for anything that doesn't fit either pattern.

    pct is computed against the TRUE original price (total_price + the
    quantity-corrected discount), not total_price alone.

    total_price == 0 with discount_amount > 0 is a genuine 100% comp (the
    item was given away free, so nothing was charged) — this must resolve
    to "100%", not be discarded.
    """
    if total_price < 0:
        return "ostalo"

    line_discount = _line_discount(total_price, discount_amount, quantity)
    original = total_price + line_discount
    if original <= 0:
        return "ostalo"

    pct = line_discount / original * 100

    if abs(pct - 50.0) < 0.5:
        return "50%"
    if abs(pct - 100.0) < 0.5:
        return "100%"
    return "ostalo"


def _classify_bill(row_types):
    """
    A bill's overall class is only meaningful if every discounted line in
    it agrees. Mixed or ambiguous bills fall to 'ostalo' so they surface
    for manual review rather than being silently folded into either tier.
    """
    distinct = set(row_types)
    if distinct == {"50%"}:
        return "50%"
    if distinct == {"100%"}:
        return "100%"
    return "ostalo"


@bp.route("/")
def index():
    db = get_db()

    # ── filters ──────────────────────────────────────────────────────
    date_from     = request.args.get("date_from", "")
    date_to       = request.args.get("date_to",   "")
    venue         = request.args.get("venue",      "")
    disc_class    = request.args.get("disc_class", "")   # 50% | 100% | ostalo | ""

    where  = "WHERE b.is_revoked = 0 AND b.bill_id IN (SELECT DISTINCT bill_id FROM bill_items_raw WHERE discount_amount > 0)"
    params = []
    if date_from:
        where += " AND b.date >= ?"; params.append(date_from)
    if date_to:
        where += " AND b.date <= ?"; params.append(date_to)
    if venue:
        where += " AND b.venue = ?"; params.append(venue)

    # ── fetch ALL items for any bill that has at least one discounted
    #    line — not just the discounted rows. A bill can mix discounted
    #    and full-price items in the same ticket; excluding the full-price
    #    rows here was the source of both symptoms reported: diluted
    #    percentages (the denominator only covered part of the bill) and
    #    "paid > original" (amount_paid comes from the bill header and
    #    reflects the WHOLE bill, while amount_original was only being
    #    built from the discounted portion).
    item_rows = db.execute(f"""
        SELECT
            b.bill_id, b.date, b.time_created, b.venue,
            b.user_full_name, b.table_name,
            ROUND(b.amount, 0) AS amount_paid,
            bi.total_price, bi.discount_amount, bi.quantity
        FROM bills_raw b
        JOIN bill_items_raw bi ON bi.bill_id = b.bill_id
        {where}
        ORDER BY b.date DESC, b.time_created DESC
    """, params).fetchall()

    # ── group rows by bill, classify each row, then classify the bill ──
    rows_by_bill = defaultdict(list)
    bill_header = {}
    for r in item_rows:
        d = dict(r)
        rows_by_bill[d["bill_id"]].append(d)
        bill_header[d["bill_id"]] = d

    bills = []
    for bill_id, rows in rows_by_bill.items():
        header = bill_header[bill_id]

        discounted_rows = [r for r in rows if r["discount_amount"] > 0]
        if not discounted_rows:
            continue  # bill matched the IN-subquery but has no discount in this filtered window (shouldn't happen, but be safe)

        line_discounts  = [_line_discount(r["total_price"], r["discount_amount"], r["quantity"]) for r in discounted_rows]
        total_discount  = sum(line_discounts)
        amount_original = sum(r["total_price"] for r in rows) + total_discount  # ALL rows' total_price, + only the discount correction
        if total_discount <= 0:
            continue

        row_types = [
            _classify_row(r["total_price"], r["discount_amount"], r["quantity"])
            for r in discounted_rows
        ]
        discount_class = _classify_bill(row_types)
        discount_pct = round(total_discount / amount_original * 100, 1) if amount_original > 0 else 0

        bills.append({
            "bill_id":         bill_id,
            "date":            header["date"],
            "time_created":    header["time_created"],
            "venue":           header["venue"],
            "user_full_name":  header["user_full_name"],
            "table_name":      header["table_name"],
            "amount_paid":     header["amount_paid"],
            "total_discount":  round(total_discount, 0),
            "amount_original": round(amount_original, 0),
            "discount_pct":    discount_pct,
            "discount_class":  discount_class,
            "month":           header["date"][:7],
        })

    # ── Reprezentacija (RP-numbered document series) merged in as the
    # "100%" bucket — these are fully-comped representation expense
    # documents, not POS bill discounts, but the owners want them shown
    # in the same monthly Popust 50% / 100% / Ostalo structure rather
    # than as a separate section. Mapped onto the same bill-shaped fields
    # the table macro and monthly aggregation already expect:
    #   amount_original = amount_gross  (the full comped value)
    #   total_discount  = amount_gross  (100% of it was discounted)
    #   amount_paid     = 0             (fully comped, nothing charged)
    #   discount_pct    = 100
    #   venue           = category      ("Reprezentacija" / "Reprezentacija V" /
    #                       "Konob Repra" / a named profile)
    #   table_name      = external_number  (the RP document number, e.g. "RP 316-2026")
    #   user_full_name  = employee_name
    rp_where = "WHERE is_deleted = 0"
    rp_params = []
    if date_from:
        rp_where += " AND date >= ?"; rp_params.append(date_from)
    if date_to:
        rp_where += " AND date <= ?"; rp_params.append(date_to)

    try:
        rp_rows = db.execute(f"""
            SELECT external_number, date, category, employee_name,
                   amount_net, amount_vat, amount_gross
            FROM reprezentacija_documents
            {rp_where}
            ORDER BY date DESC
        """, rp_params).fetchall()
    except Exception:
        rp_rows = []  # table may not exist yet on older databases

    for r in rp_rows:
        gross = round(r["amount_gross"] or 0, 0)
        bills.append({
            "bill_id":         r["external_number"],
            "date":            r["date"],
            "time_created":    "",
            "venue":           r["category"],
            "user_full_name":  r["employee_name"],
            "table_name":      r["external_number"],
            "amount_paid":     0,
            "total_discount":  gross,
            "amount_original": gross,
            "discount_pct":    100.0,
            "discount_class":  "100%",
            "month":           r["date"][:7],
        })

    bills.sort(key=lambda b: (b["date"], b["time_created"]), reverse=True)

    # apply discount class filter after classification
    if disc_class:
        bills = [b for b in bills if b["discount_class"] == disc_class]

    # ── monthly aggregation ──────────────────────────────────────────
    monthly = defaultdict(lambda: {
        "50%":   {"count": 0, "discount": 0.0, "original": 0.0},
        "100%":  {"count": 0, "discount": 0.0, "original": 0.0},
        "ostalo":{"count": 0, "discount": 0.0, "original": 0.0},
    })
    for b in bills:
        slot = monthly[b["month"]][b["discount_class"]]
        slot["count"]    += 1
        slot["discount"] += b["total_discount"] or 0
        slot["original"] += b["amount_original"] or 0

    months_sorted = sorted(monthly.keys(), reverse=True)
    monthly_rows = []
    for mo in months_sorted:
        d  = monthly[mo]
        mo_bills = [b for b in bills if b["month"] == mo]
        monthly_rows.append({
            "month":      mo,
            "month_sr":   _fmt_month(mo),
            "d50":        d["50%"],
            "d100":       d["100%"],
            "dost":       d["ostalo"],
            "total_count":    d["50%"]["count"]    + d["100%"]["count"]    + d["ostalo"]["count"],
            "total_discount": d["50%"]["discount"] + d["100%"]["discount"] + d["ostalo"]["discount"],
            "total_original": d["50%"]["original"] + d["100%"]["original"] + d["ostalo"]["original"],
            "bills_50":   [b for b in mo_bills if b["discount_class"] == "50%"],
            "bills_100":  [b for b in mo_bills if b["discount_class"] == "100%"],
            "bills_ost":  [b for b in mo_bills if b["discount_class"] == "ostalo"],
        })

    # ── KPIs ─────────────────────────────────────────────────────────
    all_bills = bills  # already filtered
    kpi = {
        "total_bills":     len(all_bills),
        "total_discount":  sum(b["total_discount"] or 0  for b in all_bills),
        "total_original":  sum(b["amount_original"] or 0 for b in all_bills),
        "n_50":            sum(1 for b in all_bills if b["discount_class"] == "50%"),
        "n_100":           sum(1 for b in all_bills if b["discount_class"] == "100%"),
        "n_ost":           sum(1 for b in all_bills if b["discount_class"] == "ostalo"),
        "disc_50_amt":     sum(b["total_discount"] or 0 for b in all_bills if b["discount_class"] == "50%"),
        "disc_100_amt":    sum(b["total_discount"] or 0 for b in all_bills if b["discount_class"] == "100%"),
    }

    venues = [r[0] for r in db.execute(
        "SELECT DISTINCT venue FROM bills_raw WHERE is_revoked=0 ORDER BY venue"
    ).fetchall()]

    return render_template(
        "discounts/index.html",
        kpi=kpi,
        monthly_rows=monthly_rows,
        venues=venues,
        filters=dict(date_from=date_from, date_to=date_to,
                     venue=venue, disc_class=disc_class),
    )
