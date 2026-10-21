from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from collections import defaultdict
from datetime import datetime
from i18n import tr

bp = Blueprint("debit_cards", __name__)

def _anchor_redirect(url, row_id):
    """Append #row-{id} anchor so the browser scrolls back to the edited row."""
    base = url.split('#')[0]
    return redirect(f"{base}#row-{row_id}")

def _referrer_anchor(row_id, fallback):
    base = (request.referrer or fallback).split('#')[0]
    return redirect(f"{base}#row-{row_id}")


@bp.route("/")
def index():
    """Pivot table view of debit card spending aggregated by counterparty."""
    db = get_db()
    
    # Filters
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    classification = request.args.get("classification", "")
    counterparty = request.args.get("counterparty", "")
    
    # Build WHERE clause
    where = "WHERE 1=1"
    params = []
    
    if date_from:
        where += " AND date >= ?"; params.append(date_from)
    if date_to:
        where += " AND date <= ?"; params.append(date_to)
    if classification:
        where += " AND classification = ?"; params.append(classification)
    if counterparty:
        where += " AND counterparty LIKE ?"; params.append(f"%{counterparty}%")
    
    # Get all transactions for pivot
    transactions = db.execute(f"""
        SELECT 
            date,
            counterparty,
            amount,
            classification,
            strftime('%Y', date) AS year,
            strftime('%m', date) AS month
        FROM debit_card_transactions
        {where}
        ORDER BY date DESC
    """, params).fetchall()
    
    # Build pivot: {counterparty: {month: amount}}
    pivot = defaultdict(lambda: defaultdict(float))
    counterparty_totals = defaultdict(float)
    month_totals = defaultdict(float)
    all_months = set()
    
    for tx in transactions:
        cp = tx["counterparty"] or "Unknown"
        month_key = f"{tx['year']}-{tx['month']}"
        amount = float(tx["amount"] or 0)
        
        pivot[cp][month_key] += amount
        counterparty_totals[cp] += amount
        month_totals[month_key] += amount
        all_months.add(month_key)
    
    # Sort months chronologically
    sorted_months = sorted(list(all_months))
    
    # Build table rows
    rows = []
    for cp in sorted(pivot.keys(), key=lambda x: counterparty_totals[x], reverse=True):
        row = {
            "counterparty": cp,
            "months": {m: pivot[cp].get(m, 0) for m in sorted_months},
            "total": counterparty_totals[cp]
        }
        rows.append(row)
    
    # KPIs
    total_spending = sum(float(tx["amount"] or 0) for tx in transactions)
    transaction_count = len(transactions)
    unique_counterparties = len(pivot.keys())
    avg_transaction = total_spending / transaction_count if transaction_count > 0 else 0
    
    # Get classification counts for sidebar
    classification_counts = db.execute(f"""
        SELECT classification, COUNT(*) as cnt, ROUND(SUM(amount),0) as total
        FROM debit_card_transactions
        {where}
        GROUP BY classification
        ORDER BY total DESC
    """, params).fetchall()
    
    # Get all unique counterparties for filter
    all_counterparties = db.execute(f"""
        SELECT DISTINCT counterparty
        FROM debit_card_transactions
        WHERE counterparty IS NOT NULL
        ORDER BY counterparty
    """).fetchall()
    
    # Get all classifications
    all_classifications = db.execute("""
        SELECT DISTINCT classification
        FROM debit_card_transactions
        WHERE classification IS NOT NULL AND classification != ''
        ORDER BY classification
    """).fetchall()
    
    return render_template(
        "debit_cards/index.html",
        rows=rows,
        sorted_months=sorted_months,
        month_totals=month_totals,
        total_spending=total_spending,
        transaction_count=transaction_count,
        unique_counterparties=unique_counterparties,
        avg_transaction=avg_transaction,
        classification_counts=classification_counts,
        all_counterparties=all_counterparties,
        all_classifications=all_classifications,
        filters=dict(date_from=date_from, date_to=date_to, 
                    classification=classification, counterparty=counterparty)
    )


@bp.route("/transactions")
def transactions():
    """Detailed transaction list view."""
    db = get_db()
    
    # Filters
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    classification = request.args.get("classification", "")
    counterparty_filter = request.args.get("counterparty", "")
    page = max(1, int(request.args.get("page", 1)))
    PER_PAGE = 50
    
    where = "WHERE 1=1"
    params = []
    
    if date_from:
        where += " AND date >= ?"; params.append(date_from)
    if date_to:
        where += " AND date <= ?"; params.append(date_to)
    if classification:
        where += " AND classification = ?"; params.append(classification)
    if counterparty_filter:
        where += " AND counterparty LIKE ?"; params.append(f"%{counterparty_filter}%")
    
    # Get count
    count = db.execute(f"SELECT COUNT(*) FROM debit_card_transactions {where}", params).fetchone()[0]
    total_pages = max(1, (count + PER_PAGE - 1) // PER_PAGE)
    offset = (page - 1) * PER_PAGE
    
    # Get transactions
    transactions = db.execute(f"""
        SELECT *
        FROM debit_card_transactions
        {where}
        ORDER BY date DESC
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, offset]).fetchall()
    
    # KPIs
    kpi = db.execute(f"""
        SELECT 
            COUNT(*) as cnt,
            ROUND(SUM(amount),0) as total,
            ROUND(AVG(amount),0) as avg_tx
        FROM debit_card_transactions
        {where}
    """, params).fetchone()
    
    # Get all classifications
    all_classifications = db.execute("""
        SELECT DISTINCT classification
        FROM debit_card_transactions
        WHERE classification IS NOT NULL AND classification != ''
        ORDER BY classification
    """).fetchall()
    
    # Get all counterparties
    all_counterparties = db.execute("""
        SELECT DISTINCT counterparty
        FROM debit_card_transactions
        WHERE counterparty IS NOT NULL
        ORDER BY counterparty
    """).fetchall()
    
    return render_template(
        "debit_cards/transactions.html",
        transactions=transactions,
        kpi=kpi,
        all_classifications=all_classifications,
        all_counterparties=all_counterparties,
        filters=dict(date_from=date_from, date_to=date_to,
                    classification=classification, counterparty=counterparty_filter),
        page=page, total_pages=total_pages, count=count
    )


@bp.route("/<int:card_tx_id>/update", methods=["POST"])
def update_transaction(card_tx_id):
    """Update classification or notes for a transaction."""
    db = get_db()
    
    classification = request.form.get("classification", "").strip() or "Unclassified"
    notes = request.form.get("notes", "").strip() or None
    
    db.execute("""
        UPDATE debit_card_transactions
        SET classification = ?, notes = ?
        WHERE card_tx_id = ?
    """, [classification, notes, card_tx_id])
    db.commit()
    
    flash(tr("Transakcija ažurirana."), "success")
    return _referrer_anchor(card_tx_id, url_for("debit_cards.transactions"))
