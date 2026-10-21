"""
inventory.py — Lager / Inventory tracking

Data source: inventory_stock_daily, populated by
scripts/09_pos_ingestion.py's pull_inventory_stock_snapshots() via the
confirmed-live GET /Report/Stock endpoint (real point-in-time quantities,
keyed by shift_id -> resolved to a calendar date via /Shift/{id}). Single
shared warehouse ("Glavni") across both venues -- no venue split at the
item level, matching the physical reality of one shared inventory.

Scope (current phase): flow/snapshot tracking only. Manual physical-count
import + variance comparison is a deliberately separate, later phase -- not
built here.
"""

from flask import Blueprint, render_template, request
from db import get_db

bp = Blueprint("inventory", __name__)

PER_PAGE = 60


def _ensure_inventory_table(db):
    """Flask has its own DB connection and never runs the ingestion script's
    _ensure_schema() -- this table must be creatable here too, or a fresh
    deploy / pre-backfill visit to this module 500s with 'no such table'."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS inventory_stock_daily (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            date               TEXT NOT NULL,
            product_id         INTEGER,
            product_code       TEXT,
            product_name       TEXT,
            category_name      TEXT,
            group_name         TEXT,
            unit_measure       TEXT,
            closing_quantity   REAL DEFAULT 0,
            shift_id           INTEGER,
            warehouse_name     TEXT,
            ingested_at        TEXT,
            UNIQUE(date, product_id)
        )
    """)
    db.commit()


@bp.route("/")
def index():
    db = get_db()
    _ensure_inventory_table(db)

    latest_date = db.execute(
        "SELECT MAX(date) FROM inventory_stock_daily"
    ).fetchone()[0]

    selected_date = request.args.get("date") or latest_date
    category = request.args.get("category", "")
    search = request.args.get("search", "")
    page = max(int(request.args.get("page", 1) or 1), 1)

    if not selected_date:
        return render_template(
            "inventory/index.html", rows=[], categories=[], dates=[],
            selected_date=None, latest_date=None, count=0,
            page=1, total_pages=1,
            filters=dict(category=category, search=search),
        )

    where = "WHERE date = ?"
    params = [selected_date]
    if category:
        where += " AND category_name = ?"; params.append(category)
    if search:
        where += " AND product_name LIKE ?"; params.append(f"%{search}%")

    all_rows = db.execute(f"""
        SELECT product_id, product_code, product_name, category_name,
               group_name, unit_measure, closing_quantity, shift_id
        FROM inventory_stock_daily
        {where}
        ORDER BY category_name, group_name, product_name
    """, params).fetchall()

    categories = [r[0] for r in db.execute(
        "SELECT DISTINCT category_name FROM inventory_stock_daily "
        "WHERE date = ? ORDER BY category_name", [selected_date]
    ).fetchall()]

    dates = [r[0] for r in db.execute(
        "SELECT DISTINCT date FROM inventory_stock_daily ORDER BY date DESC LIMIT 90"
    ).fetchall()]

    total = len(all_rows)
    total_pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(page, total_pages)
    rows = all_rows[(page - 1) * PER_PAGE: page * PER_PAGE]

    return render_template(
        "inventory/index.html",
        rows=rows, categories=categories, dates=dates,
        selected_date=selected_date, latest_date=latest_date,
        count=total, page=page, total_pages=total_pages,
        filters=dict(category=category, search=search),
    )


@bp.route("/product/<int:product_id>")
def product_detail(product_id):
    db = get_db()
    _ensure_inventory_table(db)

    history = db.execute("""
        SELECT date, closing_quantity, shift_id
        FROM inventory_stock_daily
        WHERE product_id = ?
        ORDER BY date DESC
        LIMIT 180
    """, [product_id]).fetchall()

    meta = db.execute("""
        SELECT product_name, product_code, category_name, group_name, unit_measure
        FROM inventory_stock_daily WHERE product_id = ?
        ORDER BY date DESC LIMIT 1
    """, [product_id]).fetchone()

    consumption = db.execute("""
        SELECT date, quantity, cogs_net, unit_cost_net
        FROM cogs_daily
        WHERE product_id = ?
        ORDER BY date DESC
        LIMIT 180
    """, [product_id]).fetchall()
    consumption_by_date = {r["date"]: r for r in consumption}

    combined = []
    for h in history:
        c = consumption_by_date.get(h["date"])
        combined.append({
            "date": h["date"],
            "closing_quantity": h["closing_quantity"],
            "shift_id": h["shift_id"],
            "consumed": c["quantity"] if c else None,
            "cogs_net": c["cogs_net"] if c else None,
        })

    return render_template(
        "inventory/product_detail.html",
        meta=meta, combined=combined, product_id=product_id,
    )
