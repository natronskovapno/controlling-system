from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from i18n import tr

bp = Blueprint("invoices", __name__)

PER_PAGE = 40

def _anchor_redirect(url, invoice_id):
    """Append #row-{id} anchor to a redirect URL so the browser scrolls back to the edited row."""
    base = url.split('#')[0]
    return redirect(f"{base}#row-{invoice_id}")

def _referrer_anchor(invoice_id, fallback):
    """Redirect to referrer URL with row anchor, falling back to fallback URL."""
    base = (request.referrer or fallback).split('#')[0]
    return redirect(f"{base}#row-{invoice_id}")

# ── helpers ──────────────────────────────────────────────────────────

def _ensure_amount_paid_column(db):
    """Add amount_paid column to both tables if not present."""
    for table in ("invoices_in", "invoices_out"):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if "amount_paid" not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN amount_paid REAL DEFAULT 0")
    db.commit()


def _ensure_payment_log_table(db):
    """Create invoice_payment_log table for tracking individual payments."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_payment_log (
            log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            direction    TEXT    NOT NULL,
            invoice_id   INTEGER NOT NULL,
            payment_date TEXT    NOT NULL,
            amount       REAL    NOT NULL,
            notes        TEXT,
            created_at   TEXT    DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_payment_log_invoice
        ON invoice_payment_log(direction, invoice_id)
    """)
    db.commit()


def _ensure_payment_date_column(db):
    """Add payment_date column to both invoice tables if not present."""
    for table in ("invoices_in", "invoices_out"):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        if "payment_date" not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN payment_date TEXT")
    db.commit()


def _build_filter_clause(direction, search, payment_status, date_from, date_to,
                         assigned_filter=""):
    table    = "invoices_out" if direction == "out" else "invoices_in"
    name_col = "client_name"   if direction == "out" else "supplier_name"
    source_type = "invoice_out" if direction == "out" else "invoice_in"
    clause   = f"FROM {table} WHERE 1=1"
    params   = []
    if search:
        clause += f" AND {name_col} LIKE ?"; params.append(f"%{search}%")
    if payment_status:
        clause += " AND payment_status = ?"; params.append(payment_status)
    if date_from:
        clause += " AND date_issued >= ?"; params.append(date_from)
    if date_to:
        clause += " AND date_issued <= ?"; params.append(date_to)
    if assigned_filter == "assigned":
        clause += f""" AND invoice_id IN (
            SELECT source_id FROM invoice_fixed_cost_links
            WHERE source_type = '{source_type}'
        )"""
    elif assigned_filter == "unassigned":
        clause += f""" AND invoice_id NOT IN (
            SELECT source_id FROM invoice_fixed_cost_links
            WHERE source_type = '{source_type}'
        )"""
    return table, clause, params


def _get_latest_ingestion_timestamp(db, table_name):
    """
    Get the most recent ingestion timestamp from table (identifies newest batch).
    Checks for both 'created_at' and 'ingested_at' columns to handle naming inconsistency.
    
    Returns:
        ISO timestamp string of latest ingestion, or None if no data
    """
    # Check which timestamp column exists
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table_name})").fetchall()]
    
    timestamp_col = None
    if "created_at" in cols:
        timestamp_col = "created_at"
    elif "ingested_at" in cols:
        timestamp_col = "ingested_at"
    else:
        # No timestamp column exists
        return None
    
    # Get MAX timestamp
    row = db.execute(f"SELECT MAX({timestamp_col}) as latest FROM {table_name}").fetchone()
    return row["latest"] if row and row["latest"] else None


# ── bulk endpoints ────────────────────────────────────────────────────

@bp.route("/bulk-set-paid", methods=["POST"])
def bulk_set_paid():
    db = get_db()
    _ensure_amount_paid_column(db)
    direction  = request.form.get("direction", "in")
    table      = "invoices_out" if direction == "out" else "invoices_in"
    new_status = request.form.get("new_status", "Paid")
    select_all = request.form.get("select_all") == "1"

    if select_all:
        search          = request.form.get("f_search", "")
        payment_status  = request.form.get("f_payment_status", "")
        date_from       = request.form.get("f_date_from", "")
        date_to         = request.form.get("f_date_to", "")
        assigned_filter = request.form.get("f_assigned_filter", "")
        exclude_ids     = set(request.form.getlist("exclude_ids"))
        tbl, clause, params = _build_filter_clause(
            direction, search, payment_status, date_from, date_to, assigned_filter)
        rows = db.execute(f"SELECT invoice_id, net_amount, pdv_amount {clause}",
                          params).fetchall()
        ids = [(r["invoice_id"], (r["net_amount"] or 0) + (r["pdv_amount"] or 0))
               for r in rows if str(r["invoice_id"]) not in exclude_ids]
    else:
        raw_ids = request.form.getlist("invoice_ids")
        ids = []
        for inv_id in raw_ids:
            row = db.execute(
                f"SELECT invoice_id, net_amount + COALESCE(pdv_amount,0) AS gross "
                f"FROM {table} WHERE invoice_id=?", [inv_id]
            ).fetchone()
            if row:
                ids.append((row["invoice_id"], row["gross"] or 0))

    for inv_id, gross in ids:
        amount_paid = gross if new_status == "Paid" else 0.0
        db.execute(
            f"UPDATE {table} SET payment_status=?, amount_paid=? WHERE invoice_id=?",
            [new_status, amount_paid, inv_id]
        )
    db.commit()
    flash(tr("{a0} faktura označeno kao '{new_status}'.", a0=len(ids), new_status=new_status), "success")
    first_id = ids[0][0] if ids else None
    if first_id:
        return _referrer_anchor(first_id, url_for("invoices.index", direction=direction))
    return redirect(request.referrer or url_for("invoices.index", direction=direction))


@bp.route("/bulk-assign-fixed-cost", methods=["POST"])
def bulk_assign_fixed_cost():
    db = get_db()
    direction   = request.form.get("direction", "in")
    source_type = "invoice_out" if direction == "out" else "invoice_in"
    tag_id      = request.form.get("tag_id")
    select_all  = request.form.get("select_all") == "1"

    table    = "invoices_out" if direction == "out" else "invoices_in"
    date_col = "date_received" if direction == "in" else "date_issued"

    if select_all:
        search          = request.form.get("f_search", "")
        payment_status  = request.form.get("f_payment_status", "")
        date_from       = request.form.get("f_date_from", "")
        date_to         = request.form.get("f_date_to", "")
        assigned_filter = request.form.get("f_assigned_filter", "")
        exclude_ids     = set(request.form.getlist("exclude_ids"))
        _, clause, params = _build_filter_clause(
            direction, search, payment_status, date_from, date_to, assigned_filter)
        rows = db.execute(
            f"SELECT invoice_id, net_amount, {date_col} AS tx_date {clause}",
            params
        ).fetchall()
        invoice_rows = [dict(r) for r in rows
                        if str(r["invoice_id"]) not in exclude_ids]
    else:
        raw_ids = request.form.getlist("invoice_ids")
        invoice_rows = []
        for inv_id in raw_ids:
            row = db.execute(
                f"SELECT invoice_id, net_amount, {date_col} AS tx_date "
                f"FROM {table} WHERE invoice_id=?", [inv_id]
            ).fetchone()
            if row:
                invoice_rows.append(dict(row))

    if not invoice_rows:
        flash(tr("Nema selektovanih faktura."), "warning")
        return redirect(request.referrer or url_for("invoices.index", direction=direction))

    # Empty tag_id = remove all fixed cost links for selected invoices
    if not tag_id:
        removed = 0
        for row in invoice_rows:
            c = db.execute(
                "DELETE FROM invoice_fixed_cost_links WHERE source_type=? AND source_id=?",
                [source_type, int(row["invoice_id"])]
            )
            removed += c.rowcount
            # Also clear fixed_cost_tag_id if column exists
            try:
                db.execute(
                    f"UPDATE {table} SET fixed_cost_tag_id=NULL WHERE invoice_id=?",
                    [int(row["invoice_id"])]
                )
            except Exception:
                pass
        db.commit()
        flash(tr("Tagovi uklonjeni sa {a0} faktura ({removed} veza).", a0=len(invoice_rows), removed=removed), "success")
        first_id = invoice_rows[0]["invoice_id"] if invoice_rows else None
        if first_id:
            return _referrer_anchor(first_id, url_for("invoices.index", direction=direction))
        return redirect(request.referrer or url_for("invoices.index", direction=direction))

    assigned = skipped = 0
    for row in invoice_rows:
        tx_date = row.get("tx_date")
        if not tx_date:
            skipped += 1
            continue
        try:
            year   = int(tx_date[:4])
            month  = int(tx_date[5:7])
            amount = float(row["net_amount"]) if row["net_amount"] else 0.0
            already = db.execute("""
                SELECT 1 FROM invoice_fixed_cost_links
                WHERE source_type=? AND source_id=? AND tag_id=?
            """, [source_type, int(row["invoice_id"]), int(tag_id)]).fetchone()
            if already:
                skipped += 1
                continue
            db.execute("""
                INSERT INTO invoice_fixed_cost_links
                (source_type, source_id, tag_id, year, month, amount)
                VALUES (?,?,?,?,?,?)
            """, [source_type, int(row["invoice_id"]), int(tag_id), year, month, amount])
            assigned += 1
        except Exception:
            skipped += 1
    db.commit()
    msg = f"{assigned} " + tr("faktura dodeljeno fiksnom trošku (period iz datuma prijema).")
    if skipped:
        msg += f" {skipped} " + tr("preskočeno (već dodeljeno ili nema datuma).")
    flash(msg, "success")
    return redirect(request.referrer or url_for("invoices.index", direction=direction))


# ── index ─────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    _ensure_amount_paid_column(db)
    _ensure_payment_date_column(db)

    direction      = request.args.get("direction", "in")
    search         = request.args.get("search", "")
    payment_status = request.args.get("payment_status", "")
    invoice_status = request.args.get("invoice_status", "")
    date_from      = request.args.get("date_from", "")
    date_to        = request.args.get("date_to", "")
    assigned_filter= request.args.get("assigned_filter", "")
    source_filter  = request.args.get("source_filter", "")
    show_latest    = request.args.get("show_latest", "")
    page           = max(1, int(request.args.get("page", 1)))

    if direction == "out":
        table    = "invoices_out"
        name_col = "client_name"
    else:
        table    = "invoices_in"
        name_col = "supplier_name"

    base = f"FROM {table} WHERE 1=1"
    params = []

    if search:
        base += f" AND {name_col} LIKE ?"; params.append(f"%{search}%")
    if payment_status:
        base += " AND payment_status = ?"; params.append(payment_status)
    if date_from:
        base += " AND date_issued >= ?"; params.append(date_from)
    if date_to:
        base += " AND date_issued <= ?"; params.append(date_to)
    if assigned_filter == "assigned":
        source_type = "invoice_out" if direction == "out" else "invoice_in"
        base += f""" AND invoice_id IN (
            SELECT source_id FROM invoice_fixed_cost_links
            WHERE source_type = '{source_type}'
        )"""
    elif assigned_filter == "unassigned":
        source_type = "invoice_out" if direction == "out" else "invoice_in"
        base += f""" AND invoice_id NOT IN (
            SELECT source_id FROM invoice_fixed_cost_links
            WHERE source_type = '{source_type}'
        )"""
    if invoice_status:
        base += " AND COALESCE(invoice_status, '') = ?"; params.append(invoice_status)
    else:
        base += " AND COALESCE(invoice_status, 'New') NOT IN ('Storno', 'Rejected')"
    if source_filter == "manual":
        base += " AND source = 'manual'"
    elif source_filter == "sef":
        base += " AND source != 'manual'"
    if show_latest:
        base += f" AND DATE(created_at) = (SELECT DATE(MAX(created_at)) FROM {table})"

    count = db.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
    total_pages = max(1, (count + PER_PAGE - 1) // PER_PAGE)
    offset = (page - 1) * PER_PAGE

    rows = db.execute(
        f"SELECT *, {name_col} AS counterparty " + base +
        " ORDER BY date_issued DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset]
    ).fetchall()

    # Fetch fixed-cost tag links for this page's invoices
    source_type = "invoice_out" if direction == "out" else "invoice_in"
    page_ids = [r["invoice_id"] for r in rows]
    invoice_tags = {}
    if page_ids:
        try:
            ph = ",".join("?" * len(page_ids))
            tag_rows = db.execute(f"""
                SELECT l.source_id, t.name, t.type
                FROM invoice_fixed_cost_links l
                JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
                WHERE l.source_type = ? AND l.source_id IN ({ph})
            """, [source_type] + page_ids).fetchall()
            for tr in tag_rows:
                invoice_tags.setdefault(tr["source_id"], []).append(dict(tr))
        except Exception:
            pass

    kpi = db.execute(
        "SELECT "
        "ROUND(SUM(CASE WHEN payment_status='Unpaid' THEN net_amount ELSE 0 END) + "
        "       SUM(CASE WHEN payment_status='Partial' THEN net_amount - COALESCE(amount_paid,0)/(1+COALESCE(pdv_amount,0)/NULLIF(net_amount,0)) ELSE 0 END),0) AS total_net, "
        "ROUND(SUM(CASE WHEN payment_status='Unpaid' THEN COALESCE(pdv_amount,0) ELSE 0 END) + "
        "       SUM(CASE WHEN payment_status='Partial' THEN COALESCE(pdv_amount,0) - (COALESCE(amount_paid,0) - net_amount + COALESCE(amount_paid,0)/(1+COALESCE(pdv_amount,0)/NULLIF(net_amount,0))) ELSE 0 END),0) AS total_pdv, "
        "ROUND(SUM(CASE WHEN payment_status='Unpaid' THEN net_amount+COALESCE(pdv_amount,0) ELSE 0 END) + "
        "       SUM(CASE WHEN payment_status='Partial' THEN (net_amount+COALESCE(pdv_amount,0)) - COALESCE(amount_paid,0) ELSE 0 END),0) AS total_gross, "
        "COUNT(*) AS cnt, "
        "ROUND(SUM(CASE WHEN payment_status='Unpaid' THEN net_amount+COALESCE(pdv_amount,0) ELSE 0 END),0) AS unpaid_gross, "
        "ROUND(SUM(CASE WHEN payment_status='Partial' THEN (net_amount+COALESCE(pdv_amount,0)) - COALESCE(amount_paid,0) ELSE 0 END),0) AS partial_remaining, "
        "ROUND(SUM(CASE WHEN payment_status='Unpaid' THEN net_amount+COALESCE(pdv_amount,0) ELSE 0 END) + "
        "SUM(CASE WHEN payment_status='Partial' THEN (net_amount+COALESCE(pdv_amount,0)) - COALESCE(amount_paid,0) ELSE 0 END),0) AS total_outstanding "
        + base, params
    ).fetchone()

    try:
        status_counts = {}
        for row in db.execute(
            f"SELECT COALESCE(invoice_status,'Unknown') AS s, COUNT(*) AS n {base}",
            params
        ).fetchall():
            status_counts[row['s']] = row['n']
    except Exception:
        status_counts = {}

    try:
        fixed_tags = db.execute(
            "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
        ).fetchall()
    except Exception:
        fixed_tags = []

    # FIXED: Get latest ingestion timestamp for "NEW" badge
    latest_ingestion = _get_latest_ingestion_timestamp(db, table)
    
    from datetime import date as _date
    return render_template(
        "invoices/index.html",
        rows=rows, kpi=kpi, direction=direction,
        fixed_tags=fixed_tags,
        invoice_tags=invoice_tags,
        status_counts=status_counts,
        latest_ingestion_timestamp=latest_ingestion,  # FIXED: Added this parameter
        today_year=_date.today().year,
        today_month=_date.today().month,
        filters=dict(search=search, payment_status=payment_status,
                     date_from=date_from, date_to=date_to, direction=direction,
                     assigned_filter=assigned_filter,
                     invoice_status=invoice_status,
                     source_filter=source_filter,
                     show_latest=show_latest),
        page=page, total_pages=total_pages, count=count,
        job_running=False,
    )


# ── payment endpoints ─────────────────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/set-payment", methods=["POST"])
def set_payment(direction, invoice_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    payment_status = request.form.get("payment_status", "Unpaid")
    try:
        amount_paid = float(request.form.get("amount_paid") or 0)
    except ValueError:
        amount_paid = 0.0
    db.execute(
        f"UPDATE {table} SET payment_status = ?, amount_paid = ? WHERE invoice_id = ?",
        [payment_status, amount_paid, invoice_id]
    )
    db.commit()
    flash(tr("Status plaćanja ažuriran."), "success")
    return _referrer_anchor(invoice_id, url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


@bp.route("/<direction>/<int:invoice_id>/add-payment", methods=["POST"])
def add_payment(direction, invoice_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    _ensure_payment_log_table(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    payment_date = request.form.get("payment_date", "").strip()
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        amount = 0.0
    notes = request.form.get("notes", "").strip() or None

    if not payment_date or amount <= 0:
        flash(tr("Unesite datum i iznos plaćanja."), "error")
        return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))

    db.execute("""
        INSERT INTO invoice_payment_log (direction, invoice_id, payment_date, amount, notes)
        VALUES (?, ?, ?, ?, ?)
    """, [direction, invoice_id, payment_date, amount, notes])

    total_paid = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM invoice_payment_log
        WHERE direction=? AND invoice_id=?
    """, [direction, invoice_id]).fetchone()[0]

    row = db.execute(
        f"SELECT net_amount + COALESCE(pdv_amount,0) AS gross FROM {table} WHERE invoice_id=?",
        [invoice_id]
    ).fetchone()
    gross = row["gross"] if row else 0.0

    new_status = "Unpaid" if total_paid <= 0 else ("Paid" if total_paid >= gross else "Partial")
    db.execute(
        f"UPDATE {table} SET amount_paid=?, payment_status=? WHERE invoice_id=?",
        [total_paid, new_status, invoice_id]
    )
    db.commit()
    flash(tr("Uplata {amount:,.0f} RSD zabeležena. Status: {new_status}.", amount=amount, new_status=new_status), "success")
    return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


@bp.route("/<direction>/<int:invoice_id>/delete-payment/<int:log_id>", methods=["POST"])
def delete_payment(direction, invoice_id, log_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    _ensure_payment_log_table(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    db.execute("DELETE FROM invoice_payment_log WHERE log_id=? AND direction=? AND invoice_id=?",
               [log_id, direction, invoice_id])
    total_paid = db.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM invoice_payment_log
        WHERE direction=? AND invoice_id=?
    """, [direction, invoice_id]).fetchone()[0]
    row = db.execute(
        f"SELECT net_amount + COALESCE(pdv_amount,0) AS gross FROM {table} WHERE invoice_id=?",
        [invoice_id]
    ).fetchone()
    gross = row["gross"] if row else 0.0
    new_status = "Unpaid" if total_paid <= 0 else ("Paid" if total_paid >= gross else "Partial")
    db.execute(
        f"UPDATE {table} SET amount_paid=?, payment_status=? WHERE invoice_id=?",
        [total_paid, new_status, invoice_id]
    )
    db.commit()
    flash(tr("Uplata uklonjena."), "success")
    return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


@bp.route("/<direction>/<int:invoice_id>/toggle-paid", methods=["POST"])
def toggle_paid(direction, invoice_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    row = db.execute(
        f"SELECT payment_status FROM {table} WHERE invoice_id = ?", [invoice_id]
    ).fetchone()
    if row:
        new_status = "Unpaid" if row["payment_status"] == "Paid" else "Paid"
        amount_paid = 0.0
        if new_status == "Paid":
            full = db.execute(
                f"SELECT net_amount + COALESCE(pdv_amount,0) AS gross FROM {table} WHERE invoice_id=?",
                [invoice_id]
            ).fetchone()
            amount_paid = full["gross"] if full else 0.0
        db.execute(
            f"UPDATE {table} SET payment_status=?, amount_paid=? WHERE invoice_id=?",
            [new_status, amount_paid, invoice_id]
        )
        db.commit()
        flash(tr("Status promenjen u '{new_status}'.", new_status=new_status), "success")
    # Build redirect URL preserving all active filter params so the
    # quick-toggle button doesn't reset the current search/filter state.
    redirect_url = url_for(
        "invoices.index",
        direction=direction,
        search=request.form.get("f_search", ""),
        payment_status=request.form.get("f_payment_status", ""),
        date_from=request.form.get("f_date_from", ""),
        date_to=request.form.get("f_date_to", ""),
        assigned_filter=request.form.get("f_assigned_filter", ""),
        invoice_status=request.form.get("f_invoice_status", ""),
        page=request.form.get("f_page", "1"),
    )
    return _anchor_redirect(redirect_url, invoice_id)


# ── bulk assign to small brewery ─────────────────────────────────────

@bp.route("/bulk-assign-brewery", methods=["POST"])
def bulk_assign_brewery():
    """Bulk assign selected invoices to brewery entity."""
    db = get_db()
    f = request.form
    direction = f.get("direction", "in")
    table = "invoices_out" if direction == "out" else "invoices_in"
    
    select_all = f.get("select_all") == "1"
    
    if select_all:
        # Build filter query same as index
        search          = f.get("f_search", "")
        payment_status  = f.get("f_payment_status", "")
        date_from       = f.get("f_date_from", "")
        date_to         = f.get("f_date_to", "")
        assigned_filter = f.get("f_assigned_filter", "")
        invoice_status  = f.get("f_invoice_status", "")
        
        table, where_clause, params = _build_filter_clause(
            direction, search, payment_status, date_from, date_to, assigned_filter
        )
        
        # Add invoice_status filter
        if invoice_status:
            where_clause += " AND invoice_status = ?"
            params.append(invoice_status)
        
        # Only assign invoices that are currently main (don't re-assign MP invoices)
        where_clause += " AND entity = 'main'"
        
        # Get all matching invoice_ids
        query = f"SELECT invoice_id {where_clause}"
        rows = db.execute(query, params).fetchall()
        invoice_ids = [r["invoice_id"] for r in rows]
    else:
        # Individual checkboxes
        invoice_ids = [int(x) for x in f.getlist("invoice_ids")]
    
    if not invoice_ids:
        flash(tr("Nema odabranih faktura."), "error")
    else:
        # Update all selected invoices
        placeholders = ",".join(["?"] * len(invoice_ids))
        db.execute(
            f"UPDATE {table} SET entity = 'brewery' WHERE invoice_id IN ({placeholders})",
            invoice_ids
        )
        db.commit()
        flash(tr("{a0} faktura dodeljeno the Brewery.", a0=len(invoice_ids)), "success")
    
    return redirect(request.referrer or url_for("invoices.index", direction=direction))


# ── payment date (inline) ────────────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/set-payment-date", methods=["POST"])
def set_payment_date(direction, invoice_id):
    """Inline update of payment_date from the invoice list."""
    db = get_db()
    _ensure_payment_date_column(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    payment_date = request.form.get("payment_date", "").strip() or None
    db.execute(
        f"UPDATE {table} SET payment_date = ? WHERE invoice_id = ?",
        [payment_date, invoice_id]
    )
    db.commit()
    # Preserve filter state in redirect
    redirect_url = url_for(
        "invoices.index",
        direction=direction,
        search=request.form.get("f_search", ""),
        payment_status=request.form.get("f_payment_status", ""),
        date_from=request.form.get("f_date_from", ""),
        date_to=request.form.get("f_date_to", ""),
        assigned_filter=request.form.get("f_assigned_filter", ""),
        invoice_status=request.form.get("f_invoice_status", ""),
        page=request.form.get("f_page", "1"),
    )
    return _anchor_redirect(redirect_url, invoice_id)


# ── detail ────────────────────────────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>")
def detail(direction, invoice_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    _ensure_payment_log_table(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    row = db.execute(
        f"SELECT * FROM {table} WHERE invoice_id = ?", [invoice_id]
    ).fetchone()
    if not row:
        flash(tr("Faktura nije pronađena."), "error")
        return redirect(url_for("invoices.index"))

    invoice_type_filter = "sale" if direction == "out" else "purchase"
    try:
        items = db.execute("""
            SELECT * FROM invoice_line_items
            WHERE invoice_id_sef = ? AND invoice_type = ?
            ORDER BY order_no, row_id
        """, [row["invoice_id_sef"], invoice_type_filter]).fetchall()
    except Exception:
        items = []

    try:
        fixed_tags = db.execute(
            "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
        ).fetchall()
        existing_links = db.execute("""
            SELECT l.*, t.name as tag_name, t.type as tag_type
            FROM invoice_fixed_cost_links l
            JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
            WHERE l.source_type = ? AND l.source_id = ?
        """, ["invoice_out" if direction=="out" else "invoice_in", invoice_id]).fetchall()
    except Exception:
        fixed_tags = []
        existing_links = []

    payment_log = db.execute("""
        SELECT * FROM invoice_payment_log
        WHERE direction=? AND invoice_id=?
        ORDER BY payment_date DESC, log_id DESC
    """, [direction, invoice_id]).fetchall()

    from datetime import date as _date
    return render_template(
        "invoices/detail.html",
        row=row, direction=direction, items=items,
        fixed_tags=fixed_tags, existing_links=existing_links,
        payment_log=payment_log,
        today_year=_date.today().year,
        today_month=_date.today().month,
        today=_date.today().isoformat(),
    )


# ── new ───────────────────────────────────────────────────────────────

@bp.route("/new/<direction>", methods=["GET", "POST"])
def new(direction):
    db = get_db()
    _ensure_amount_paid_column(db)
    table = "invoices_out" if direction == "out" else "invoices_in"

    if request.method == "POST":
        f = request.form
        try:
            net = float(f.get("net_amount") or 0)
            pdv_rate = float(f.get("pdv_rate") or 0)
            pdv_amount = round(net * pdv_rate / 100, 2)
            if direction == "out":
                db.execute("""
                    INSERT INTO invoices_out
                    (date_issued, due_date, client_name, client_pib,
                     invoice_number_ext, net_amount, pdv_amount, pdv_rate,
                     payment_status, amount_paid, notes, source, created_at)
                    VALUES (?,?,?,?,?,?,?,?,'Unpaid',0,?,'manual',datetime('now'))
                """, [f.get("date_issued"), f.get("due_date"),
                      f.get("counterparty"), f.get("counterparty_pib"),
                      f.get("invoice_number_ext"), net, pdv_amount, pdv_rate,
                      f.get("notes")])
            else:
                db.execute("""
                    INSERT INTO invoices_in
                    (date_issued, date_received, due_date, supplier_name, supplier_pib,
                     invoice_number_ext, net_amount, pdv_amount, pdv_rate,
                     payment_status, amount_paid, notes, source, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'Unpaid',0,?,'manual',datetime('now'))
                """, [f.get("date_issued"), f.get("date_received"), f.get("due_date"),
                      f.get("counterparty"), f.get("counterparty_pib"),
                      f.get("invoice_number_ext"), net, pdv_amount, pdv_rate,
                      f.get("notes")])
            db.commit()
            flash(tr("Faktura je dodata."), "success")
            return redirect(url_for("invoices.index", direction=direction))
        except Exception as e:
            flash(tr("Greška: {e}", e=e), "error")

    return render_template("invoices/new.html", direction=direction)


# ── edit (manual invoices only) ───────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/edit", methods=["GET", "POST"])
def edit(direction, invoice_id):
    db = get_db()
    _ensure_amount_paid_column(db)
    table = "invoices_out" if direction == "out" else "invoices_in"
    row = db.execute(f"SELECT * FROM {table} WHERE invoice_id = ?", [invoice_id]).fetchone()
    if not row:
        flash(tr("Faktura nije pronađena."), "error")
        return redirect(url_for("invoices.index", direction=direction))
    if row["source"] != "manual":
        flash(tr("Mogu se menjati samo ručno unete fakture (bele fakture)."), "error")
        return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))

    if request.method == "POST":
        f = request.form
        try:
            net = float(f.get("net_amount") or 0)
            pdv_rate = float(f.get("pdv_rate") or 0)
            pdv_amount = round(net * pdv_rate / 100, 2)
            if direction == "out":
                db.execute("""
                    UPDATE invoices_out SET
                        date_issued = ?, due_date = ?,
                        client_name = ?, client_pib = ?,
                        invoice_number_ext = ?,
                        net_amount = ?, pdv_amount = ?, pdv_rate = ?,
                        notes = ?
                    WHERE invoice_id = ?
                """, [f.get("date_issued"), f.get("due_date"),
                      f.get("counterparty"), f.get("counterparty_pib"),
                      f.get("invoice_number_ext"),
                      net, pdv_amount, pdv_rate,
                      f.get("notes"), invoice_id])
            else:
                db.execute("""
                    UPDATE invoices_in SET
                        date_issued = ?, date_received = ?, due_date = ?,
                        supplier_name = ?, supplier_pib = ?,
                        invoice_number_ext = ?,
                        net_amount = ?, pdv_amount = ?, pdv_rate = ?,
                        notes = ?
                    WHERE invoice_id = ?
                """, [f.get("date_issued"), f.get("date_received"), f.get("due_date"),
                      f.get("counterparty"), f.get("counterparty_pib"),
                      f.get("invoice_number_ext"),
                      net, pdv_amount, pdv_rate,
                      f.get("notes"), invoice_id])
            db.commit()
            flash(tr("Faktura je izmenjena."), "success")
            return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))
        except Exception as e:
            flash(tr("Greška: {e}", e=e), "error")

    return render_template(
        "invoices/edit.html",
        row=row, direction=direction,
    )


# ── delete ────────────────────────────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/delete", methods=["POST"])
def delete(direction, invoice_id):
    db = get_db()
    table = "invoices_out" if direction == "out" else "invoices_in"
    row = db.execute(
        f"SELECT source FROM {table} WHERE invoice_id = ?", [invoice_id]
    ).fetchone()
    if not row:
        flash(tr("Faktura nije pronađena."), "error")
        return redirect(url_for("invoices.index", direction=direction))
    if row["source"] != "manual":
        flash(tr("Mogu se brisati samo ručno unete fakture (bele fakture)."), "error")
        return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))
    db.execute(f"DELETE FROM {table} WHERE invoice_id = ?", [invoice_id])
    db.commit()
    flash(tr("Faktura je obrisana."), "success")
    return redirect(url_for("invoices.index", direction=direction))


# ── assign to fixed cost (multi-row / advanced) ───────────────────────────

@bp.route("/<direction>/<int:invoice_id>/assign-fixed-cost-multi", methods=["POST"])
def assign_fixed_cost_multi(direction, invoice_id):
    """Multi-row advanced assignment: splits one invoice across multiple periods/tags."""
    import re as _re
    db = get_db()
    f = request.form
    source_type = "invoice_out" if direction == "out" else "invoice_in"

    entries = {}
    for key, val in f.items():
        m = _re.match(r'^rows\[(\d+)\]\[(.+)\]$', key)
        if m:
            entry_idx, field = int(m.group(1)), m.group(2)
            entries.setdefault(entry_idx, {})[field] = val

    if not entries:
        flash(tr("Nije pronađen nijedan red za dodelu."), "error")
        return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))

    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_fixed_cost_links (
            link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT    NOT NULL,
            source_id   INTEGER NOT NULL,
            tag_id      INTEGER NOT NULL,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            amount      REAL    NOT NULL,
            notes       TEXT,
            created_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(source_type, source_id, tag_id, year, month)
        )
    """)

    assigned = updated = skipped = 0
    for entry_idx in sorted(entries.keys()):
        row_data = entries[entry_idx]
        try:
            tag_id = int(row_data.get("tag_id", 0))
            year   = int(row_data.get("year", 0))
            month  = int(row_data.get("month", 0))
            amount = float(row_data.get("amount") or 0)
            notes  = row_data.get("notes") or None
            if not tag_id or not year or not month or amount <= 0:
                skipped += 1
                continue
            existing = db.execute("""
                SELECT link_id FROM invoice_fixed_cost_links
                WHERE source_type=? AND source_id=? AND tag_id=? AND year=? AND month=?
            """, [source_type, invoice_id, tag_id, year, month]).fetchone()
            if existing:
                # Update amount instead of skipping — allows re-submission to fix amounts
                db.execute("""
                    UPDATE invoice_fixed_cost_links
                    SET amount=?, notes=?
                    WHERE link_id=?
                """, [amount, notes, existing["link_id"]])
                updated += 1
            else:
                db.execute("""
                    INSERT INTO invoice_fixed_cost_links
                    (source_type, source_id, tag_id, year, month, amount, notes)
                    VALUES (?,?,?,?,?,?,?)
                """, [source_type, invoice_id, tag_id, year, month, amount, notes])
                assigned += 1
        except Exception as e:
            flash(tr("Red {entry_idx} greška: {e}", entry_idx=entry_idx, e=e), "error")
            skipped += 1
    db.commit()

    parts = []
    if assigned: parts.append(f"{assigned} " + tr("dodato"))
    if updated:  parts.append(f"{updated} " + tr("ažurirano"))
    if skipped:  parts.append(f"{skipped} " + tr("preskočeno"))
    flash(tr("Alokacija: ") + ", ".join(parts) + ".", "success" if (assigned or updated) else "error")
    return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


# ── assign to fixed cost (single / quick) ───────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/assign-fixed-cost", methods=["POST"])
def assign_fixed_cost(direction, invoice_id):
    db = get_db()
    f = request.form
    try:
        tag_id = int(f["tag_id"])
        year   = int(f["year"])
        month  = int(f["month"])
        amount = float(f.get("amount") or 0)
        source_type = "invoice_out" if direction == "out" else "invoice_in"

        db.execute("""
            CREATE TABLE IF NOT EXISTS invoice_fixed_cost_links (
                link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT    NOT NULL,
                source_id   INTEGER NOT NULL,
                tag_id      INTEGER NOT NULL,
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                amount      REAL    NOT NULL,
                notes       TEXT,
                created_at  TEXT    DEFAULT (datetime('now')),
                UNIQUE(source_type, source_id, tag_id, year, month)
            )
        """)
        db.execute("""
            INSERT INTO invoice_fixed_cost_links
            (source_type, source_id, tag_id, year, month, amount, notes)
            VALUES (?,?,?,?,?,?,?)
        """, [source_type, invoice_id, tag_id, year, month, amount,
              f.get("notes") or None])
        db.commit()
        flash(tr("Faktura je dodeljena fiksnom trošku/prihodu."), "success")
    except Exception as e:
        flash(tr("Greška: {e}", e=e), "error")
    return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


# ── assign to small brewery ────────────────────────────────────────────

@bp.route("/<direction>/<int:invoice_id>/assign-brewery", methods=["POST"])
def assign_brewery(direction, invoice_id):
    """Quick action: Transfer invoice from main to brewery entity."""
    db = get_db()
    table = "invoices_out" if direction == "out" else "invoices_in"
    
    # Update entity to brewery
    db.execute(
        f"UPDATE {table} SET entity = 'brewery' WHERE invoice_id = ?",
        [invoice_id]
    )
    db.commit()
    
    flash(tr("Faktura je dodeljena the Brewery."), "success")
    
    # Preserve filter state in redirect
    redirect_url = url_for(
        "invoices.index",
        direction=direction,
        search=request.form.get("f_search", ""),
        payment_status=request.form.get("f_payment_status", ""),
        date_from=request.form.get("f_date_from", ""),
        date_to=request.form.get("f_date_to", ""),
        assigned_filter=request.form.get("f_assigned_filter", ""),
        invoice_status=request.form.get("f_invoice_status", ""),
        page=request.form.get("f_page", "1"),
    )
    return _anchor_redirect(redirect_url, invoice_id)


@bp.route("/<direction>/<int:invoice_id>/unassign-brewery", methods=["POST"])
def unassign_brewery(direction, invoice_id):
    """Manual correction (detail page only): revert an invoice from brewery
    back to main. Mirrors assign_brewery in reverse. Deliberately not
    exposed on the list page / bulk actions since this is a rare, one-off fix."""
    db = get_db()
    table = "invoices_out" if direction == "out" else "invoices_in"

    db.execute(
        f"UPDATE {table} SET entity = 'main' WHERE invoice_id = ?",
        [invoice_id]
    )
    db.commit()

    flash(tr("Faktura je vraćena na Garden Stage."), "success")
    return redirect(url_for("invoices.detail", direction=direction, invoice_id=invoice_id))


# ── PDF parser — outgoing bela faktura (RČ format) ────────────────────

def _parse_rc_pdf_text(text):
    """Parse text extracted from an Garden Stage RČ-format outgoing invoice PDF."""
    import re

    def _parse_amount(s):
        s = s.strip().replace(' RSD', '').replace(' ', '')
        # Detect thousands separator style:
        # If dot appears before exactly 3 digits (e.g. "120.940") → dot is thousands sep
        # If comma appears before exactly 3 digits (e.g. "120,940") → comma is thousands sep
        # Normal decimal: "120.94" or "120,94" (< 3 digits after separator)
        import re as _re
        if _re.search(r'\.\d{3}(?:[,\.]|$)', s):
            # dot = thousands separator (e.g. 120.940 or 1.234.567)
            s = s.replace('.', '').replace(',', '.')
        else:
            # comma = thousands separator (e.g. 120,940) or decimal comma
            s = s.replace(',', '')
        try:
            return float(s)
        except ValueError:
            return 0.0

    def _parse_date(s):
        s = s.strip().rstrip('.')
        parts = s.split('.')
        if len(parts) == 3:
            return f'{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}'
        return s

    result = {}

    # Invoice number
    m = re.search(r'Faktura:?\s*(RČ\s*\d+/\d+)', text, re.IGNORECASE)
    if m:
        result['invoice_number_ext'] = m.group(1).strip()

    # Date issued
    m = re.search(r'datum izdavanja[:\s]+[A-ZČŠĆŽĐ\s\-]+\s+(\d{2}\.\d{2}\.\d{4})', text, re.IGNORECASE)
    if m:
        result['date_issued'] = _parse_date(m.group(1))

    # Due date
    m = re.search(r'Datum dospe[ćc]a[:\s]+(\d{2}\.\d{2}\.\d{4})', text, re.IGNORECASE)
    if m:
        result['due_date'] = _parse_date(m.group(1))

    # Client block: between 'Primalac:' and 'Faktura:'
    m = re.search(r'Primalac:\n(.+?)\nFaktura:?', text, re.DOTALL)
    if m:
        block = m.group(1).strip().splitlines()
        result['client_name'] = block[0].strip()
        for line in block:
            pm = re.search(r'PIB[:\s]+(\d+)', line)
            if pm:
                result['client_pib'] = pm.group(1)

    # Description — first stavka line
    m = re.search(r'Stavka:\n-\s*(.+)', text)
    if m:
        result['description'] = m.group(1).strip()

    # Amounts
    m = re.search(r'Bez PDV[:\s]+([\d,\.]+)\s*RSD', text)
    if m:
        result['net_amount'] = _parse_amount(m.group(1))
    m = re.search(r'- PDV[:\s]+([\d,\.]+)\s*RSD', text)
    if m:
        result['pdv_amount'] = _parse_amount(m.group(1))
    m = re.search(r'Stopa PDV[:\s]+(\d+)%', text)
    if m:
        result['pdv_rate'] = float(m.group(1))

    return result


@bp.route("/upload-pdf", methods=["GET", "POST"])
def upload_pdf():
    """Upload one or more RČ PDF invoices, parse, show preview."""
    if request.method == "GET":
        return render_template("invoices/upload_pdf.html")

    files = request.files.getlist("files")
    if not files or not any(f.filename for f in files):
        flash(tr("Nije izabran nijedan fajl."), "error")
        return redirect(url_for("invoices.upload_pdf"))

    try:
        import pdfplumber
    except ImportError:
        flash(tr("pdfplumber nije instaliran. Pokrenite: pip install pdfplumber"), "error")
        return redirect(url_for("invoices.upload_pdf"))

    import tempfile, os, json
    from flask import session

    parsed = []
    errors = []

    for f in files:
        if not f.filename:
            continue
        suffix = os.path.splitext(f.filename)[1].lower()
        if suffix != '.pdf':
            errors.append(f"{f.filename}: nije PDF fajl.")
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        f.save(tmp.name)
        tmp.close()
        try:
            with pdfplumber.open(tmp.name) as pdf:
                text = pdf.pages[0].extract_text() or ""
            result = _parse_rc_pdf_text(text)
            result['source_file'] = f.filename
            if not result.get('invoice_number_ext'):
                errors.append(f"{f.filename}: " + tr("nije prepoznat kao RČ faktura."))
            else:
                parsed.append(result)
        except Exception as e:
            errors.append(f"{f.filename}: " + tr("greška pri parsiranju —") + f" {e}")
        finally:
            os.unlink(tmp.name)

    for err in errors:
        flash(err, "error")

    if not parsed:
        return redirect(url_for("invoices.upload_pdf"))

    session["rc_pdf_preview"] = json.dumps(parsed, ensure_ascii=False)
    total_net = sum(r.get('net_amount', 0) for r in parsed)
    total_pdv = sum(r.get('pdv_amount', 0) for r in parsed)
    return render_template("invoices/preview_pdf.html", rows=parsed, count=len(parsed),
                           total_net=total_net, total_pdv=total_pdv)


@bp.route("/commit-pdf", methods=["POST"])
def commit_pdf():
    """Commit parsed PDF invoices to invoices_out."""
    import json
    from flask import session
    db = get_db()
    _ensure_amount_paid_column(db)

    rows_json = session.pop("rc_pdf_preview", None)
    if not rows_json:
        flash(tr("Sesija je istekla. Molimo ponovo učitajte fajl."), "error")
        return redirect(url_for("invoices.upload_pdf"))

    try:
        rows = json.loads(rows_json)
    except Exception:
        flash(tr("Greška pri čitanju podataka iz sesije."), "error")
        return redirect(url_for("invoices.upload_pdf"))

    from datetime import date as _date
    inserted = skipped = 0
    for r in rows:
        try:
            db.execute("""
                INSERT OR IGNORE INTO invoices_out
                    (date_issued, due_date, client_name, client_pib,
                     invoice_number_ext, net_amount, pdv_amount, pdv_rate,
                     payment_status, amount_paid, notes, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,'Unpaid',0,?,'manual',datetime('now'))
            """, [
                r.get('date_issued'), r.get('due_date'),
                r.get('client_name'), r.get('client_pib'),
                r.get('invoice_number_ext'),
                r.get('net_amount', 0), r.get('pdv_amount', 0), r.get('pdv_rate', 20),
                r.get('description', ''),
            ])
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            flash(tr("Greška pri upisu {a0}: {e}", a0=r.get('invoice_number_ext','?'), e=e), "error")
            db.rollback()
            return redirect(url_for("invoices.upload_pdf"))

    db.commit()
    flash(tr("Upisano {inserted} faktura. Preskočeno (duplikati): {skipped}.", inserted=inserted, skipped=skipped), "success")
    return redirect(url_for("invoices.index", direction="out"))
