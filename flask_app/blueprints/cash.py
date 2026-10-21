"""
blueprints/cash.py
==================
Cash Position module — Garden Stage Controlling System
Replaces: Payments_to_be_made.xlsx

Data sources:
  bank_transactions  — net position per account (credits minus debits)
  invoices_in        — outstanding payables   (Unpaid / Partial)
  invoices_out       — outstanding receivables (Unpaid / Partial)
"""

from flask import Blueprint, render_template
from db import get_db
from datetime import date
from i18n import tr

bp = Blueprint("cash", __name__)

ACCOUNT_LABELS = {
    "160-0000000001234-56": "Primary current account",
    "265-0000000009876-54": "Secondary current account",
    "160-0000000005678-90": "Brewery current account",
    "160-0000000007777-11": "Loan servicing account",
}


def _ensure_invoice_columns(db):
    """Ensure amount_paid column exists on both invoice tables."""
    for table in ("invoices_in", "invoices_out"):
        try:
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
            if "amount_paid" not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN amount_paid REAL DEFAULT 0")
            db.commit()
        except Exception:
            pass


def _get_account_balances(db):
    """
    Returns actual closing balance from account_balances table (from XML statements).
    Falls back to computed saldo from bank_transactions if no balance data available.
    """
    result = []

    # Try account_balances table first (actual closing balance from XML)
    try:
        actual = db.execute("""
            SELECT ab.account_number, ab.closing_balance, ab.balance_date,
                   bt.total_in, bt.total_out
            FROM (
                SELECT account_number,
                       closing_balance, balance_date
                FROM account_balances
                WHERE (account_number, balance_date) IN (
                    SELECT account_number, MAX(balance_date)
                    FROM account_balances
                    GROUP BY account_number
                )
            ) ab
            LEFT JOIN (
                SELECT account,
                    ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) AS total_in,
                    ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) AS total_out
                FROM bank_transactions GROUP BY account
            ) bt ON bt.account = ab.account_number
            ORDER BY ab.account_number
        """).fetchall()

        if actual:
            for r in actual:
                d = dict(r)
                d["account"] = d["account_number"]
                d["balance"] = d["closing_balance"]
                d["last_date"] = d["balance_date"]
                d["label"] = ACCOUNT_LABELS.get(d["account"], d["account"])
                d["source"] = "actual"
                result.append(d)
            return result
    except Exception:
        pass

    # Fallback: compute from transactions
    rows = db.execute("""
        SELECT
            account,
            MAX(date)  AS last_date,
            ROUND(SUM(CASE WHEN direction = 'credit' THEN amount ELSE 0 END), 0) AS total_in,
            ROUND(SUM(CASE WHEN direction = 'debit'  THEN amount ELSE 0 END), 0) AS total_out,
            ROUND(SUM(CASE WHEN direction = 'credit' THEN amount
                           WHEN direction = 'debit'  THEN -amount
                           ELSE 0 END), 0) AS balance
        FROM bank_transactions
        GROUP BY account
        ORDER BY account
    """).fetchall()

    for r in rows:
        d = dict(r)
        d["label"] = ACCOUNT_LABELS.get(d["account"], d["account"])
        d["source"] = "computed"
        result.append(d)
    return result


def _get_payables(db):
    """Outstanding payables aggregated by supplier, with total and overdue breakdown."""
    rows = db.execute("""
        SELECT
            supplier_name,
            COUNT(*)                                                                       AS invoice_count,
            ROUND(SUM((net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)), 0) AS total_gross,
            ROUND(SUM((net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)), 0) AS outstanding,
            ROUND(SUM(CASE
                WHEN CAST(julianday('now') - julianday(COALESCE(due_date, date_issued)) AS INTEGER) > 0
                THEN (net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)
                ELSE 0 END), 0)                                                             AS overdue_amount,
            SUM(CASE
                WHEN CAST(julianday('now') - julianday(COALESCE(due_date, date_issued)) AS INTEGER) > 0
                THEN 1 ELSE 0 END)                                                         AS overdue_count,
            MIN(due_date)                                                                   AS earliest_due
        FROM invoices_in
        WHERE payment_status IN ('Unpaid', 'Partial')
          AND (net_amount + COALESCE(pdv_amount, 0)) > 0
          AND COALESCE(invoice_status, 'New') NOT IN ('Rejected', 'Storno')
        GROUP BY supplier_name
        ORDER BY outstanding DESC
    """).fetchall()
    return [dict(r) for r in rows]


def _get_receivables(db):
    """Outstanding receivables aggregated by client, with total and overdue breakdown."""
    rows = db.execute("""
        SELECT
            client_name                                                                    AS buyer_name,
            COUNT(*)                                                                       AS invoice_count,
            ROUND(SUM((net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)), 0) AS total_gross,
            ROUND(SUM((net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)), 0) AS outstanding,
            ROUND(SUM(CASE
                WHEN CAST(julianday('now') - julianday(COALESCE(due_date, date_issued)) AS INTEGER) > 0
                THEN (net_amount + COALESCE(pdv_amount, 0)) - COALESCE(amount_paid, 0)
                ELSE 0 END), 0)                                                             AS overdue_amount,
            SUM(CASE
                WHEN CAST(julianday('now') - julianday(COALESCE(due_date, date_issued)) AS INTEGER) > 0
                THEN 1 ELSE 0 END)                                                         AS overdue_count,
            MIN(due_date)                                                                   AS earliest_due
        FROM invoices_out
        WHERE payment_status IN ('Unpaid', 'Partial')
          AND (net_amount + COALESCE(pdv_amount, 0)) > 0
          AND COALESCE(invoice_status, 'Sent') NOT IN ('Rejected', 'Storno')
        GROUP BY client_name
        ORDER BY outstanding DESC
    """).fetchall()
    return [dict(r) for r in rows]


def _get_transactions_aggregated(db, direction, date_from="", date_to=""):
    """Transactions grouped by counterparty+direction — pivot-style view."""
    where = "WHERE direction = ?"
    params = [direction]
    if date_from:
        where += " AND date >= ?"
        params.append(date_from)
    if date_to:
        where += " AND date <= ?"
        params.append(date_to)
    rows = db.execute(f"""
        SELECT
            COALESCE(counterparty, purpose, '—') AS counterparty,
            direction,
            COUNT(*)                              AS tx_count,
            ROUND(SUM(amount), 0)                 AS total_amount,
            MIN(date)                             AS first_date,
            MAX(date)                             AS last_date
        FROM bank_transactions
        {where}
        GROUP BY COALESCE(counterparty, purpose, '—'), direction
        ORDER BY total_amount DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_transactions_detailed(db, direction, date_from="", date_to=""):
    """Individual transactions for detailed view."""
    where = "WHERE direction = ?"
    params = [direction]
    if date_from:
        where += " AND date >= ?"
        params.append(date_from)
    if date_to:
        where += " AND date <= ?"
        params.append(date_to)
    rows = db.execute(f"""
        SELECT date, account, direction, amount,
               COALESCE(counterparty, purpose, '—') AS counterparty,
               manual_tag, auto_tag
        FROM bank_transactions
        {where}
        ORDER BY date DESC, transaction_id DESC
    """, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["account_label"] = ACCOUNT_LABELS.get(d["account"], d["account"])
        result.append(d)
    return result


def _build_summary(balances, payables, receivables):
    total_balance     = sum(b.get("balance") or 0 for b in balances)
    total_payables    = sum(p["outstanding"] for p in payables)
    total_receivables = sum(r["outstanding"] for r in receivables)
    net_position      = total_balance - total_payables
    overdue_count     = sum(p["overdue_count"] for p in payables)
    overdue_amount    = sum(p["overdue_amount"] for p in payables)
    return {
        "total_balance":     total_balance,
        "total_payables":    total_payables,
        "total_receivables": total_receivables,
        "net_position":      net_position,
        "overdue_count":     overdue_count,
        "overdue_amount":    overdue_amount,
        "today":             date.today().isoformat(),
    }


def _parse_amount(raw):
    """
    Parse a user-entered amount defensively. Handles plain numbers ("15703"),
    Serbian-style thousands-dot ("15.703"), and thousands-dot + decimal-comma
    ("15.703,50"). A bare "." is always treated as a thousands separator here
    (never a decimal point) since proposed amounts are whole RSD — this is
    what fixed the bug where "15.703" was being parsed as 15.703 instead of
    15703.
    """
    if not raw:
        return 0
    raw = raw.strip().replace(" ", "")
    if "," in raw:
        # Serbian format: dot = thousands, comma = decimal
        raw = raw.replace(".", "").replace(",", ".")
    else:
        # No comma present: any dots are thousands separators, not decimals
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return 0


def _ensure_proposal_table(db):
    """Single current proposal — no history. Re-saving replaces it entirely."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS payment_proposal (
            invoice_id      INTEGER PRIMARY KEY,
            proposed_amount REAL NOT NULL,
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _get_proposal_candidates(db):
    """
    Outstanding Garden Stage payables at invoice level, grouped by supplier,
    each carrying its already-saved proposed_amount (0 if not yet selected).
    """
    rows = db.execute("""
        SELECT
            i.invoice_id, i.supplier_name, i.invoice_number_sef,
            i.due_date, i.date_issued,
            ROUND(i.net_amount + COALESCE(i.pdv_amount, 0), 0)              AS gross_amount,
            ROUND((i.net_amount + COALESCE(i.pdv_amount, 0))
                  - COALESCE(i.amount_paid, 0), 0)                          AS remaining,
            i.payment_status,
            COALESCE(pp.proposed_amount, 0)                                 AS proposed_amount
        FROM invoices_in i
        LEFT JOIN payment_proposal pp ON pp.invoice_id = i.invoice_id
        WHERE i.entity = 'main'
          AND i.payment_status IN ('Unpaid', 'Partial')
          AND (i.net_amount + COALESCE(i.pdv_amount, 0)) > 0
          AND COALESCE(i.invoice_status, 'New') NOT IN ('Rejected', 'Storno')
        ORDER BY i.supplier_name, COALESCE(i.due_date, i.date_issued)
    """).fetchall()

    suppliers = {}
    for r in rows:
        d = dict(r)
        sup = d["supplier_name"] or "—"
        suppliers.setdefault(sup, {"supplier_name": sup, "invoices": [], "remaining_total": 0, "proposed_total": 0})
        suppliers[sup]["invoices"].append(d)
        suppliers[sup]["remaining_total"] += d["remaining"]
        suppliers[sup]["proposed_total"]  += d["proposed_amount"]

    return sorted(suppliers.values(), key=lambda s: -s["remaining_total"])


@bp.route("/predlog-placanja")
def payment_proposal():
    db = get_db()
    _ensure_invoice_columns(db)
    _ensure_proposal_table(db)

    suppliers = _get_proposal_candidates(db)
    grand_remaining = sum(s["remaining_total"] for s in suppliers)
    grand_proposed  = sum(s["proposed_total"]  for s in suppliers)
    grand_count     = sum(
        1 for s in suppliers for inv in s["invoices"] if inv["proposed_amount"] > 0
    )

    return render_template(
        "cash/payment_proposal.html",
        suppliers=suppliers,
        grand_remaining=grand_remaining,
        grand_proposed=grand_proposed,
        grand_count=grand_count,
    )


@bp.route("/predlog-placanja/save", methods=["POST"])
def payment_proposal_save():
    from flask import request, redirect, url_for
    db = get_db()
    _ensure_proposal_table(db)

    # Replace-all: this is "the current proposal", not a log of past ones.
    db.execute("DELETE FROM payment_proposal")

    invoice_ids = request.form.getlist("invoice_id")
    for inv_id in invoice_ids:
        raw = (request.form.get(f"amount_{inv_id}") or "").strip()
        amount = _parse_amount(raw)
        if amount > 0:
            db.execute(
                "INSERT INTO payment_proposal (invoice_id, proposed_amount) VALUES (?, ?)",
                [int(inv_id), amount]
            )
    db.commit()
    return redirect(url_for("cash.payment_proposal"))


@bp.route("/predlog-placanja/clear", methods=["POST"])
def payment_proposal_clear():
    from flask import redirect, url_for
    db = get_db()
    _ensure_proposal_table(db)
    db.execute("DELETE FROM payment_proposal")
    db.commit()
    return redirect(url_for("cash.payment_proposal"))


@bp.route("/predlog-placanja/pdf")
def payment_proposal_pdf():
    from flask import send_file
    import io
    from pathlib import Path
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Helvetica (ReportLab's built-in font) has no glyphs for Serbian Latin
    # diacritics (č, ć, š, đ, ž) — they render as solid boxes. This system
    # only ever runs on Windows, which always ships Arial — use it directly
    # instead of bundling a separate font file. Falls back to Helvetica
    # (no diacritics) if Arial isn't found for any reason.
    font_regular = Path(r"C:\Windows\Fonts\arial.ttf")
    font_bold = Path(r"C:\Windows\Fonts\arialbd.ttf")
    if font_regular.exists() and font_bold.exists():
        pdfmetrics.registerFont(TTFont("BodyFont", str(font_regular)))
        pdfmetrics.registerFont(TTFont("BodyFont-Bold", str(font_bold)))
        font_name, font_bold_name = "BodyFont", "BodyFont-Bold"
    else:
        font_name, font_bold_name = "Helvetica", "Helvetica-Bold"

    db = get_db()
    _ensure_proposal_table(db)

    rows = db.execute("""
        SELECT i.supplier_name, ROUND(SUM(pp.proposed_amount), 0) AS proposed_total
        FROM payment_proposal pp
        JOIN invoices_in i ON i.invoice_id = pp.invoice_id
        WHERE pp.proposed_amount > 0
        GROUP BY i.supplier_name
        ORDER BY proposed_total DESC
    """).fetchall()

    def fmt(n):
        return "{:,.0f}".format(n or 0).replace(",", ".") + " RSD"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             topMargin=20*mm, bottomMargin=20*mm,
                             leftMargin=20*mm, rightMargin=20*mm)
    styles = getSampleStyleSheet()
    styles["Title"].fontName = font_bold_name
    styles["Normal"].fontName = font_name
    story = []

    story.append(Paragraph(tr("Predlog plaćanja — Garden Stage"), styles["Title"]))
    story.append(Paragraph(date.today().strftime("%d.%m.%Y."), styles["Normal"]))
    story.append(Spacer(1, 14*mm))

    if rows:
        cell_style = styles["Normal"].clone("CellStyle")
        cell_style.fontName = font_name
        cell_style.fontSize = 9
        cell_style.leading = 11

        header_style = styles["Normal"].clone("HeaderCellStyle")
        header_style.fontName = font_bold_name
        header_style.fontSize = 9
        header_style.textColor = colors.white

        total_style = styles["Normal"].clone("TotalCellStyle")
        total_style.fontName = font_bold_name
        total_style.fontSize = 9

        data = [[Paragraph("Dobavljač", header_style), Paragraph("Predloženi iznos", header_style)]]
        grand_total = 0
        for r in rows:
            name = r["supplier_name"] or "—"
            data.append([Paragraph(name, cell_style), Paragraph(fmt(r["proposed_total"]), cell_style)])
            grand_total += r["proposed_total"] or 0
        data.append([Paragraph("UKUPNO", total_style), Paragraph(fmt(grand_total), total_style)])

        table = Table(data, colWidths=[110*mm, 50*mm])
        table.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#2b2b28")),
            ("ALIGN",        (1, 0), (1, -1), "RIGHT"),
            ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
            ("LINEABOVE",    (0, -1), (-1, -1), 0.75, colors.black),
            ("BACKGROUND",   (0, -1), (-1, -1), colors.HexColor("#f0ece4")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f5f0")]),
            ("GRID",         (0, 0), (-1, -1), 0.4, colors.HexColor("#cfc9bd")),
            ("TOPPADDING",   (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
            ("LEFTPADDING",  (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(table)
    else:
        story.append(Paragraph(tr("Nema sačuvanog predloga plaćanja."), styles["Normal"]))

    doc.build(story)
    buf.seek(0)

    filename = f"Predlog_placanja_{date.today().isoformat()}.pdf"
    return send_file(buf, mimetype="application/pdf",
                      as_attachment=True, download_name=filename)


@bp.route("/")
def index():
    from flask import request
    db = get_db()
    _ensure_invoice_columns(db)

    # Transaction filter params
    date_from  = request.args.get("date_from", "")
    date_to    = request.args.get("date_to", "")
    tx_view    = request.args.get("tx_view", "aggregated")  # "aggregated" or "detailed"

    balances    = _get_account_balances(db)
    payables    = _get_payables(db)
    receivables = _get_receivables(db)
    summary     = _build_summary(balances, payables, receivables)

    if tx_view == "detailed":
        inflows  = _get_transactions_detailed(db, "credit", date_from, date_to)
        outflows = _get_transactions_detailed(db, "debit",  date_from, date_to)
    else:
        inflows  = _get_transactions_aggregated(db, "credit", date_from, date_to)
        outflows = _get_transactions_aggregated(db, "debit",  date_from, date_to)

    inflow_total  = sum(r["total_amount"] for r in inflows)  if tx_view == "aggregated" else sum(r["amount"] for r in inflows)
    outflow_total = sum(r["total_amount"] for r in outflows) if tx_view == "aggregated" else sum(r["amount"] for r in outflows)

    return render_template(
        "cash/index.html",
        balances=balances,
        payables=payables,
        receivables=receivables,
        summary=summary,
        account_labels=ACCOUNT_LABELS,
        inflows=inflows,
        outflows=outflows,
        inflow_total=inflow_total,
        outflow_total=outflow_total,
        tx_view=tx_view,
        tx_filters=dict(date_from=date_from, date_to=date_to),
    )


@bp.route("/report")
def report():
    """Standalone printable HTML cash position report (opens in new tab)."""
    from flask import request
    db = get_db()
    _ensure_invoice_columns(db)

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")

    balances    = _get_account_balances(db)
    payables    = _get_payables(db)
    receivables = _get_receivables(db)
    summary     = _build_summary(balances, payables, receivables)

    # Always aggregated in the report
    inflows  = _get_transactions_aggregated(db, "credit", date_from, date_to)
    outflows = _get_transactions_aggregated(db, "debit",  date_from, date_to)
    inflow_total  = sum(r["total_amount"] for r in inflows)
    outflow_total = sum(r["total_amount"] for r in outflows)

    return render_template(
        "cash/report.html",
        balances=balances,
        payables=payables,
        receivables=receivables,
        summary=summary,
        account_labels=ACCOUNT_LABELS,
        inflows=inflows,
        outflows=outflows,
        inflow_total=inflow_total,
        outflow_total=outflow_total,
        date_from=date_from,
        date_to=date_to,
    )
