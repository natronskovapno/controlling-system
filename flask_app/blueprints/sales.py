from flask import Blueprint, render_template, request
from db import get_db
from i18n import month_name

bp = Blueprint("sales", __name__)

# ── helpers ──────────────────────────────────────────────────────────

def _fmt_month(ym):
    """'2025-06' -> 'jun 2025' / 'June 2025', following the active language."""
    try:
        y, m = ym.split("-")
        return f"{month_name(int(m))} {y}"
    except Exception:
        return ym

# ── index ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()

    # --- Filter params ---
    date_from  = request.args.get("date_from", "")
    date_to    = request.args.get("date_to", "")
    venue      = request.args.get("venue", "")
    category   = request.args.get("category", "")
    group_by   = request.args.get("group_by", "month")   # day | month | year | venue | hour | category | product

    # --- Available venues (from bills_raw - authoritative source) ---
    venues = [r[0] for r in db.execute(
        "SELECT DISTINCT venue FROM bills_raw WHERE is_revoked = 0 ORDER BY venue"
    ).fetchall()]

    # --- Available categories (from sales_hourly for product detail) ---
    try:
        cats = db.execute("""
            SELECT pc.category_id, pc.name, COUNT(*) as cnt
            FROM sales_hourly sh
            LEFT JOIN product_categories pc ON sh.category_id = pc.category_id
            GROUP BY sh.category_id
            ORDER BY pc.name
        """).fetchall()
    except Exception:
        cats = db.execute("""
            SELECT category_id, category_id as name, COUNT(*) as cnt
            FROM sales_hourly
            GROUP BY category_id
            ORDER BY category_id
        """).fetchall()

    # ═══════════════════════════════════════════════════════════════════════
    # REVENUE CALCULATION FIX (2026-04-16):
    # Use bills_raw.amount as PRIMARY source (100% complete, authoritative)
    # Use sales_hourly ONLY for product/category breakdowns (91-95% complete)
    # ═══════════════════════════════════════════════════════════════════════

    # --- Base WHERE clause for bills_raw (authoritative revenue) ---
    where_bills = "WHERE b.is_revoked = 0"
    params_bills = []
    if date_from:
        where_bills += " AND b.date >= ?"; params_bills.append(date_from)
    if date_to:
        where_bills += " AND b.date <= ?"; params_bills.append(date_to)
    if venue:
        where_bills += " AND b.venue = ?"; params_bills.append(venue)

    # --- Base WHERE clause for sales_hourly (product detail) ---
    where_sh = "WHERE 1=1"
    params_sh = []
    if date_from:
        where_sh += " AND sh.date >= ?"; params_sh.append(date_from)
    if date_to:
        where_sh += " AND sh.date <= ?"; params_sh.append(date_to)
    if venue:
        where_sh += " AND sh.venue = ?"; params_sh.append(venue)
    if category:
        where_sh += " AND sh.category_id = ?"; params_sh.append(category)

    # --- KPIs from bills_raw (authoritative) ---
    kpi = db.execute(f"""
        SELECT
            ROUND(SUM(b.amount), 0)           AS total_gross,
            ROUND(SUM(b.amount) / 1.2, 0)     AS total_net,
            COUNT(DISTINCT b.date)             AS total_days,
            COUNT(b.bill_id)                   AS total_bills
        FROM bills_raw b {where_bills}
    """, params_bills).fetchone()

    # Get quantity from sales_hourly (product detail not in bills_raw)
    qty_row = db.execute(f"""
        SELECT ROUND(SUM(sh.quantity), 0) AS total_qty
        FROM sales_hourly sh {where_sh}
    """, params_sh).fetchone()
    
    # Convert to dict and add quantity
    kpi = dict(kpi) if kpi else {}
    kpi['total_qty'] = qty_row['total_qty'] if qty_row and qty_row['total_qty'] else 0

    # --- Main grouping query (use bills_raw for revenue) ---
    if group_by == "year":
        # Yearly totals from bills_raw
        sql = f"""
            SELECT
                SUBSTR(b.date, 1, 4)              AS label,
                ROUND(SUM(b.amount), 0)           AS gross,
                ROUND(SUM(b.amount) / 1.2, 0)     AS net,
                COUNT(DISTINCT b.date)             AS days,
                COUNT(b.bill_id)                   AS bills
            FROM bills_raw b {where_bills}
            GROUP BY SUBSTR(b.date, 1, 4)
            ORDER BY label DESC
        """
        rows = db.execute(sql, params_bills).fetchall()
        rows = [dict(r) for r in rows]
        
        # Add quantity from sales_hourly per year
        for r in rows:
            qty = db.execute(f"""
                SELECT ROUND(SUM(sh.quantity), 0) AS qty
                FROM sales_hourly sh
                WHERE SUBSTR(sh.date, 1, 4) = ?
                  {(' AND sh.venue = ?' if venue else '')}
                  {(' AND sh.category_id = ?' if category else '')}
            """, [r['label']] + ([venue] if venue else []) + ([category] if category else [])).fetchone()
            r['qty'] = qty['qty'] if qty and qty['qty'] else 0
            r['label_sr'] = r['label']

    elif group_by == "month":
        # Monthly totals from bills_raw
        sql = f"""
            SELECT
                SUBSTR(b.date, 1, 7)              AS label,
                ROUND(SUM(b.amount), 0)           AS gross,
                ROUND(SUM(b.amount) / 1.2, 0)     AS net,
                COUNT(DISTINCT b.date)             AS days,
                COUNT(b.bill_id)                   AS bills
            FROM bills_raw b {where_bills}
            GROUP BY SUBSTR(b.date, 1, 7)
            ORDER BY label DESC
        """
        rows = db.execute(sql, params_bills).fetchall()
        rows = [dict(r) for r in rows]
        
        # Add quantity from sales_hourly per month
        for r in rows:
            qty = db.execute(f"""
                SELECT ROUND(SUM(sh.quantity), 0) AS qty
                FROM sales_hourly sh
                WHERE SUBSTR(sh.date, 1, 7) = ?
                  {(' AND sh.venue = ?' if venue else '')}
                  {(' AND sh.category_id = ?' if category else '')}
            """, [r['label']] + ([venue] if venue else []) + ([category] if category else [])).fetchone()
            r['qty'] = qty['qty'] if qty and qty['qty'] else 0
            r['label_sr'] = _fmt_month(r['label'])

    elif group_by == "venue":
        # Venue totals from bills_raw
        sql = f"""
            SELECT
                b.venue                           AS label,
                ROUND(SUM(b.amount), 0)           AS gross,
                ROUND(SUM(b.amount) / 1.2, 0)     AS net,
                COUNT(DISTINCT b.date)             AS days,
                COUNT(b.bill_id)                   AS bills
            FROM bills_raw b {where_bills}
            GROUP BY b.venue
            ORDER BY gross DESC
        """
        rows = db.execute(sql, params_bills).fetchall()
        rows = [dict(r) for r in rows]
        
        # Add quantity from sales_hourly per venue
        for r in rows:
            qty = db.execute(f"""
                SELECT ROUND(SUM(sh.quantity), 0) AS qty
                FROM sales_hourly sh
                WHERE sh.venue = ?
                  {(' AND sh.date >= ?' if date_from else '')}
                  {(' AND sh.date <= ?' if date_to else '')}
                  {(' AND sh.category_id = ?' if category else '')}
            """, [r['label']] + 
                ([date_from] if date_from else []) + 
                ([date_to] if date_to else []) + 
                ([category] if category else [])).fetchone()
            r['qty'] = qty['qty'] if qty and qty['qty'] else 0
            r['label_sr'] = r['label']

    elif group_by == "hour":
        # Hourly breakdown ONLY from sales_hourly (bills_raw doesn't aggregate by hour easily)
        # Note: This will show 5-9% less revenue due to incomplete line items
        sql = f"""
            SELECT
                sh.hour_slot                     AS label,
                ROUND(SUM(sh.revenue_gross), 0)  AS gross,
                ROUND(SUM(sh.revenue_net), 0)    AS net,
                ROUND(SUM(sh.quantity), 0)        AS qty,
                COUNT(DISTINCT sh.date)           AS days
            FROM sales_hourly sh {where_sh}
            GROUP BY sh.hour_slot
            ORDER BY sh.hour_slot
        """
        rows = db.execute(sql, params_sh).fetchall()
        rows = [dict(r) for r in rows]
        for r in rows:
            r['label_sr'] = r['label']
            r['bills'] = None  # Not available in hourly view

    elif group_by == "category":
        # Category breakdown from sales_hourly (product detail not in bills_raw)
        try:
            sql = f"""
                SELECT
                    COALESCE(pc.name, 'Kategorija ' || sh.category_id) AS label,
                    ROUND(SUM(sh.revenue_gross), 0)  AS gross,
                    ROUND(SUM(sh.revenue_net), 0)    AS net,
                    ROUND(SUM(sh.quantity), 0)        AS qty,
                    COUNT(DISTINCT sh.date)           AS days
                FROM sales_hourly sh
                LEFT JOIN product_categories pc ON sh.category_id = pc.category_id
                {where_sh}
                GROUP BY sh.category_id
                ORDER BY gross DESC
            """
        except Exception:
            sql = f"""
                SELECT
                    'Kategorija ' || sh.category_id  AS label,
                    ROUND(SUM(sh.revenue_gross), 0)  AS gross,
                    ROUND(SUM(sh.revenue_net), 0)    AS net,
                    ROUND(SUM(sh.quantity), 0)        AS qty,
                    COUNT(DISTINCT sh.date)           AS days
                FROM sales_hourly sh {where_sh}
                GROUP BY sh.category_id
                ORDER BY gross DESC
            """
        rows = db.execute(sql, params_sh).fetchall()
        rows = [dict(r) for r in rows]
        for r in rows:
            r['label_sr'] = r['label']
            r['bills'] = None

    elif group_by == "product":
        # Product breakdown from sales_hourly (product detail not in bills_raw)
        sql = f"""
            SELECT
                sh.product_name                  AS label,
                ROUND(SUM(sh.revenue_gross), 0)  AS gross,
                ROUND(SUM(sh.revenue_net), 0)    AS net,
                ROUND(SUM(sh.quantity), 0)        AS qty,
                COUNT(DISTINCT sh.date)           AS days
            FROM sales_hourly sh {where_sh}
            GROUP BY sh.product_name
            ORDER BY gross DESC
            LIMIT 100
        """
        rows = db.execute(sql, params_sh).fetchall()
        rows = [dict(r) for r in rows]
        for r in rows:
            r['label_sr'] = r['label']
            r['bills'] = None

    else:  # day (default)
        # Daily totals from bills_raw (authoritative)
        sql = f"""
            SELECT
                b.date                            AS label,
                ROUND(SUM(b.amount), 0)           AS gross,
                ROUND(SUM(b.amount) / 1.2, 0)     AS net,
                COUNT(DISTINCT b.venue)            AS venues,
                COUNT(b.bill_id)                   AS bills
            FROM bills_raw b {where_bills}
            GROUP BY b.date
            ORDER BY b.date DESC
            LIMIT 200
        """
        rows = db.execute(sql, params_bills).fetchall()
        rows = [dict(r) for r in rows]
        
        # Add quantity from sales_hourly per day
        for r in rows:
            qty = db.execute(f"""
                SELECT ROUND(SUM(sh.quantity), 0) AS qty
                FROM sales_hourly sh
                WHERE sh.date = ?
                  {(' AND sh.venue = ?' if venue else '')}
                  {(' AND sh.category_id = ?' if category else '')}
            """, [r['label']] + ([venue] if venue else []) + ([category] if category else [])).fetchone()
            r['qty'] = qty['qty'] if qty and qty['qty'] else 0
            r['label_sr'] = r['label']
            r['days'] = r['venues']  # For template compatibility

    # --- Top products from sales_hourly (product detail) ---
    top_products = db.execute(f"""
        SELECT
            sh.product_name,
            ROUND(SUM(sh.revenue_gross), 0) AS gross,
            ROUND(SUM(sh.quantity), 0)       AS qty
        FROM sales_hourly sh {where_sh}
        GROUP BY sh.product_name
        ORDER BY gross DESC
        LIMIT 10
    """, params_sh).fetchall()

    # --- Venue split from bills_raw (authoritative) ---
    venue_split = db.execute(f"""
        SELECT
            b.venue,
            ROUND(SUM(b.amount), 0) AS gross
        FROM bills_raw b {where_bills}
        GROUP BY b.venue
        ORDER BY gross DESC
    """, params_bills).fetchall()

    # --- Data quality indicator ---
    # For product/category/hour views, show warning about incomplete data
    show_data_warning = group_by in ['hour', 'category', 'product']

    return render_template(
        "sales/index.html",
        rows=rows,
        kpi=kpi,
        venues=venues,
        cats=cats,
        top_products=top_products,
        venue_split=venue_split,
        show_data_warning=show_data_warning,
        filters=dict(date_from=date_from, date_to=date_to,
                     venue=venue, category=category, group_by=group_by),
    )
