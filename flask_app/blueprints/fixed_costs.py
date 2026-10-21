from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from datetime import date
from i18n import month_name, months as month_list, tr

bp = Blueprint("fixed_costs", __name__)

def _anchor_redirect(url, row_id):
    """Append #row-{id} anchor so the browser scrolls back to the edited row."""
    base = url.split('#')[0]
    return redirect(f"{base}#row-{row_id}")

def _referrer_anchor(row_id, fallback):
    base = (request.referrer or fallback).split('#')[0]
    return redirect(f"{base}#row-{row_id}")


# Month names come from i18n so they follow the active language.

# Default tags created on first run
DEFAULT_TAGS = [
    ("Kirija",            "expense", "Zakup prostora"),
    ("Struja",            "expense", "Električna energija (zajednička)"),
    ("Struja — Garden",   "expense", "Električna energija — Garden"),
    ("Struja — Corner",    "expense", "Električna energija — Corner"),
    ("Voda",              "expense", "Vodosnabdevanje"),
    ("Zarade",            "expense", "Plate i honorari"),
    ("Knjigovođa",        "expense", "Računovodstvene usluge"),
    ("MUSIC_ROYALTIES_A",             "expense", "Autorske naknade MUSIC_ROYALTIES_A"),
    ("Music royalties B",          "expense", "Music royalties Bska naknada"),
    ("Čišćenje",          "expense", "Usluge čišćenja"),
    ("Marketing",         "expense", "Oglašavanje i marketing"),
    ("POS",           "expense", "POS sistem pretplata"),
    ("maintenance",            "expense", "Maintenance fiksni trošak"),
    ("led",               "expense", "LED/rasveta fiksni trošak"),
    ("fruit",             "expense", "Voće/namirnice fiksni trošak"),
    ("ramp",              "expense", "Rampa fiksni trošak"),
    ("debit_card_fixed",  "expense", "Debitna kartica fiksne naknade"),
    ("Ostali troškovi",   "expense", "Ostali fiksni troškovi"),
    ("Parking prihod",    "income",  "Prihod od parkinga"),
    ("Podnajam",          "income",  "Prihod od podnajma"),
    ("Ostali prihodi",    "income",  "Ostali fiksni prihodi"),
]

def _ensure_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS fixed_cost_tags (
            tag_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            type        TEXT NOT NULL DEFAULT 'expense',
            description TEXT,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_fixed_cost_links (
            link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,  -- 'invoice_in','invoice_out','bank_transaction','salary_monthly'
            source_id   INTEGER NOT NULL,
            tag_id      INTEGER NOT NULL REFERENCES fixed_cost_tags(tag_id),
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            amount      REAL NOT NULL,
            vat_rate    REAL NOT NULL DEFAULT 0,
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # Migrate: add vat_rate column if missing (existing DBs)
    cols_ifcl = {row[1] for row in db.execute("PRAGMA table_info(invoice_fixed_cost_links)")}
    if 'vat_rate' not in cols_ifcl:
        db.execute("ALTER TABLE invoice_fixed_cost_links ADD COLUMN vat_rate REAL NOT NULL DEFAULT 0")
    # Migrate: add submodule + venue columns if missing
    cols_fct = {row[1] for row in db.execute("PRAGMA table_info(fixed_cost_tags)")}
    if 'submodule' not in cols_fct:
        db.execute("ALTER TABLE fixed_cost_tags ADD COLUMN submodule TEXT NOT NULL DEFAULT 'operativni'")
    if 'venue' not in cols_fct:
        db.execute("ALTER TABLE fixed_cost_tags ADD COLUMN venue TEXT NOT NULL DEFAULT 'shared'")
    # Insert defaults if table is empty
    count = db.execute("SELECT COUNT(*) FROM fixed_cost_tags").fetchone()[0]
    if count == 0:
        db.executemany(
            "INSERT OR IGNORE INTO fixed_cost_tags (name, type, description) VALUES (?,?,?)",
            DEFAULT_TAGS
        )
    db.commit()

# ── salary sync ───────────────────────────────────────────────────────

@bp.route("/sync-salaries", methods=["POST"])
def sync_salaries():
    """Trigger salary allocation to fixed costs"""
    import subprocess
    import sys
    import os
    
    try:
        # Use absolute path to script
        script_path = r"C:\controlling-system\scripts\allocate_salaries_to_fixed_costs.py"
        
        if not os.path.exists(script_path):
            flash(tr("Skripta nije pronađena: {script_path}", script_path=script_path), "error")
            return redirect(url_for("fixed_costs.index"))
        
        # Run the allocation script
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            flash(tr("✓ Plate uspešno sinhronizovane sa fiksnim troškovima!"), "success")
        else:
            error_msg = result.stderr or result.stdout or "Nepoznata greška"
            flash(tr("Greška pri sinhronizaciji: {a0}", a0=error_msg[:500]), "error")
    except subprocess.TimeoutExpired:
        flash(tr("Greška: Sinhronizacija je prekoračila 60 sekundi."), "error")
    except Exception as e:
        flash(tr("Greška pri pokretanju skripte: {e}", e=e), "error")
    
    return redirect(url_for("fixed_costs.index"))

# ── index — monthly overview ──────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    _ensure_tables(db)

    year = int(request.args.get("year", date.today().year))

    # Enforce 2025 as the earliest reportable year — data before this is
    # legacy/pre-system and should not be surfaced in the module.
    FIXED_COSTS_START_YEAR = 2025
    if year < FIXED_COSTS_START_YEAR:
        from flask import redirect, url_for
        return redirect(url_for("fixed_costs.index", year=FIXED_COSTS_START_YEAR))

    # Aggregate links by month, submodule, tag
    rows = db.execute("""
        SELECT
            l.month,
            t.type,
            t.submodule,
            t.venue,
            t.name as tag_name,
            ROUND(SUM(ROUND(l.amount / (1 + COALESCE(l.vat_rate,0)/100.0), 2)), 0) as total
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE l.year = ?
        GROUP BY l.month, t.tag_id
        ORDER BY l.month, t.submodule, t.venue, t.name
    """, [year]).fetchall()

    SUBMODULES = ['operativni', 'parking', 'podnajam', 'sponzori', 'ostalo']
    VENUES     = ['Garden', 'Corner', 'shared', 'Brewery']

    # Build month summaries with submodule breakdown
    months = []
    for m in range(1, 13):
        month_rows = [r for r in rows if r["month"] == m]
        # Per-submodule totals
        sub_totals = {}
        for sub in SUBMODULES:
            sub_rows = [r for r in month_rows if r["submodule"] == sub]
            sub_totals[sub] = {
                "expense": sum(r["total"] for r in sub_rows if r["type"] == "expense"),
                "income":  sum(r["total"] for r in sub_rows if r["type"] == "income"),
                "rows":    sub_rows,
            }
        # Operativni venue split
        op_rows = sub_totals["operativni"]["rows"]
        venue_totals = {}
        for v in VENUES:
            v_rows = [r for r in op_rows if r["venue"] == v]
            venue_totals[v] = sum(r["total"] for r in v_rows if r["type"] == "expense")

        total_expense = sum(r["total"] for r in month_rows if r["type"] == "expense")
        total_income  = sum(r["total"] for r in month_rows if r["type"] == "income")
        months.append({
            "month":       m,
            "label":       month_name(m, capitalized=True),
            "has_data":    len(month_rows) > 0,
            "total_expense": total_expense,
            "total_income":  total_income,
            "sub_totals":  sub_totals,
            "venue_totals": venue_totals,
            "breakdown":   month_rows,
        })

    annual_expense = sum(m["total_expense"] for m in months)
    annual_income  = sum(m["total_income"]  for m in months)
    # Annual per-submodule
    annual_sub = {}
    for sub in SUBMODULES:
        annual_sub[sub] = {
            "expense": sum(m["sub_totals"][sub]["expense"] for m in months),
            "income":  sum(m["sub_totals"][sub]["income"]  for m in months),
        }

    # Available years — 2025 is the hard floor (pre-2025 data is legacy/noise)
    years_from_data = [r[0] for r in db.execute(
        "SELECT DISTINCT year FROM invoice_fixed_cost_links WHERE year >= 2025 ORDER BY year DESC"
    ).fetchall()]
    # Ensure all years from 2025 to current are always present
    all_years = sorted(
        set(years_from_data) | set(range(2025, date.today().year + 1)),
        reverse=True
    )
    years = all_years or [date.today().year]

    tags = db.execute(
        "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
    ).fetchall()

    return render_template("fixed_costs/index.html",
        months=months, year=year, years=years,
        annual_expense=annual_expense, annual_income=annual_income,
        annual_sub=annual_sub,
        tags=tags, months_sr=month_list(capitalized=True),
        submodules=SUBMODULES,
    )

# ── manual link entry ─────────────────────────────────────────────────

@bp.route("/link/new", methods=["GET", "POST"])
def new_link():
    db = get_db()
    _ensure_tables(db)
    tags = db.execute(
        "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
    ).fetchall()

    if request.method == "POST":
        f = request.form
        try:
            db.execute("""
                INSERT INTO invoice_fixed_cost_links
                (source_type, source_id, tag_id, year, month, amount, notes)
                VALUES ('manual', 0, ?, ?, ?, ?, ?)
            """, [
                int(f["tag_id"]),
                int(f["year"]),
                int(f["month"]),
                float(f["amount"]),
                f.get("notes") or None,
            ])
            db.commit()
            flash(tr("Stavka je dodana."), "success")
            return redirect(url_for("fixed_costs.index", year=f["year"]))
        except Exception as e:
            flash(tr("Greška: {e}", e=e), "error")

    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))

    return render_template("fixed_costs/new_link.html",
        tags=tags, year=year, month=month,
        months_sr=month_list(capitalized=True),
        years=list(range(2025, date.today().year + 2)),
    )

# ── delete link ───────────────────────────────────────────────────────

@bp.route("/link/<int:link_id>/delete", methods=["POST"])
def delete_link(link_id):
    db = get_db()
    row = db.execute(
        "SELECT year FROM invoice_fixed_cost_links WHERE link_id=?", [link_id]
    ).fetchone()
    db.execute("DELETE FROM invoice_fixed_cost_links WHERE link_id=?", [link_id])
    db.commit()
    flash(tr("Stavka je obrisana."), "success")
    year = row["year"] if row else date.today().year
    return _referrer_anchor(link_id, url_for("fixed_costs.index", year=year))

# ── month detail ──────────────────────────────────────────────────────

@bp.route("/detail")
def detail():
    db = get_db()
    _ensure_tables(db)

    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))

    # Get all items
    submodule_filter = request.args.get("submodule", None)
    base_params = [year, month]
    sub_clause  = ""
    if submodule_filter:
        sub_clause  = " AND t.submodule = ?"
        base_params = [year, month, submodule_filter]

    all_links = db.execute(f"""
        SELECT l.*, t.name as tag_name, t.type as tag_type,
               t.submodule as tag_submodule, t.venue as tag_venue
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE l.year=? AND l.month=? {sub_clause}
        ORDER BY t.submodule, t.type, t.name, l.created_at
    """, base_params).fetchall()

    # Enrich each link with data from its source table
    enriched = []
    for link in all_links:
        item = dict(link)
        st = item["source_type"]
        sid = item["source_id"]
        try:
            if st == "invoice_in":
                row = db.execute(
                    "SELECT supplier_name, invoice_number_sef, date_issued FROM invoices_in WHERE invoice_id=?", [sid]
                ).fetchone()
                if row:
                    item["counterparty"] = row["supplier_name"]
                    item["ref"] = row["invoice_number_sef"]
                    item["doc_date"] = row["date_issued"]
                item["detail_url"] = f"/invoices/in/{sid}"
            elif st == "invoice_out":
                row = db.execute(
                    "SELECT client_name, invoice_number_sef, date_issued FROM invoices_out WHERE invoice_id=?", [sid]
                ).fetchone()
                if row:
                    item["counterparty"] = row["client_name"]
                    item["ref"] = row["invoice_number_sef"]
                    item["doc_date"] = row["date_issued"]
                item["detail_url"] = f"/invoices/out/{sid}"
            elif st == "bank_transaction":
                row = db.execute(
                    "SELECT counterparty, purpose, date, payment_period FROM bank_transactions WHERE transaction_id=?", [sid]
                ).fetchone()
                if row:
                    item["counterparty"] = row["counterparty"]
                    item["ref"] = row["payment_period"] or row["purpose"]
                    item["doc_date"] = row["date"]
                item["detail_url"] = f"/bank/{sid}"
            elif st == "salary_monthly":
                item["counterparty"] = item.get("notes", "")
                item["ref"] = None
                item["doc_date"] = None
                item["detail_url"] = None
        except Exception:
            pass
        enriched.append(item)

    # Compute net_amount per item and aggregate by tag using net
    for item in enriched:
        vat = float(item.get("vat_rate") or 0)
        item["net_amount"] = round(item["amount"] / (1 + vat / 100.0), 2)

    # Aggregate by tag
    from collections import defaultdict
    tag_groups = defaultdict(lambda: {"breakdown": [], "total": 0, "type": None, "submodule": "operativni", "venue": "shared"})
    
    for link in enriched:
        tag = link["tag_name"]
        tag_groups[tag]["breakdown"].append(dict(link))
        tag_groups[tag]["total"]      += link["net_amount"]
        tag_groups[tag]["type"]        = link["tag_type"]
        tag_groups[tag]["submodule"]   = link.get("tag_submodule", "operativni")
        tag_groups[tag]["venue"]       = link.get("tag_venue", "shared")
    
    # Convert to sorted list
    aggregated = [
        {
            "tag_name":      tag,
            "tag_type":      data["type"],
            "tag_submodule": data["submodule"],
            "tag_venue":     data["venue"],
            "total":         data["total"],
            "count":         len(data["breakdown"]),
            "breakdown":     data["breakdown"]
        }
        for tag, data in tag_groups.items()
    ]
    aggregated.sort(key=lambda x: (x["tag_type"], x["tag_name"]))

    return render_template("fixed_costs/detail.html",
        aggregated=aggregated, year=year, month=month,
        month_label=month_name(month, capitalized=True),
        submodule_filter=submodule_filter,
    )

# ── VAT rate on bank transaction links ───────────────────────────────

@bp.route("/set-vat-rate", methods=["POST"])
def set_vat_rate():
    db = get_db()
    _ensure_tables(db)
    link_id  = request.form.get("link_id")
    vat_rate = request.form.get("vat_rate", "0")
    year     = request.form.get("year")
    month    = request.form.get("month")
    try:
        db.execute(
            "UPDATE invoice_fixed_cost_links SET vat_rate=? WHERE link_id=?",
            [float(vat_rate), int(link_id)]
        )
        db.commit()
    except Exception as e:
        flash(tr("Greška: {e}", e=e), "error")
    return redirect(url_for("fixed_costs.detail", year=year, month=month))

# ── tag management ────────────────────────────────────────────────────

@bp.route("/tags")
def tags():
    db = get_db()
    _ensure_tables(db)
    tags = db.execute(
        "SELECT t.*, COUNT(l.link_id) as usage_count "
        "FROM fixed_cost_tags t "
        "LEFT JOIN invoice_fixed_cost_links l ON l.tag_id = t.tag_id "
        "GROUP BY t.tag_id ORDER BY t.type, t.name"
    ).fetchall()
    return render_template("fixed_costs/tags.html", tags=tags)

@bp.route("/tags/new", methods=["POST"])
def new_tag():
    db = get_db()
    _ensure_tables(db)
    name = request.form.get("name", "").strip()
    tag_type = request.form.get("type", "expense")
    description = request.form.get("description", "").strip() or None
    submodule = request.form.get("submodule", "operativni")
    venue = request.form.get("venue", "shared") if submodule == "operativni" else "shared"
    if not name:
        flash(tr("Naziv taga je obavezan."), "error")
        return redirect(url_for("fixed_costs.tags"))
    try:
        db.execute(
            "INSERT INTO fixed_cost_tags (name, type, description, submodule, venue) VALUES (?,?,?,?,?)",
            [name, tag_type, description, submodule, venue]
        )
        db.commit()
        flash(tr("Tag '{name}' je dodat.", name=name), "success")
    except Exception:
        flash(tr("Tag '{name}' već postoji.", name=name), "error")
    return redirect(url_for("fixed_costs.tags"))

@bp.route("/tags/<int:tag_id>/toggle", methods=["POST"])
def toggle_tag(tag_id):
    db = get_db()
    tag = db.execute("SELECT active FROM fixed_cost_tags WHERE tag_id=?", [tag_id]).fetchone()
    if tag:
        db.execute("UPDATE fixed_cost_tags SET active=? WHERE tag_id=?",
                   [0 if tag["active"] else 1, tag_id])
        db.commit()
    return redirect(url_for("fixed_costs.tags"))

@bp.route("/tags/<int:tag_id>/delete", methods=["POST"])
def delete_tag(tag_id):
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM invoice_fixed_cost_links WHERE tag_id=?", [tag_id]
    ).fetchone()[0]
    if count > 0:
        flash(tr("Ne možeš obrisati tag koji se koristi u {count} stavki.", count=count), "error")
        return redirect(url_for("fixed_costs.tags"))
    db.execute("DELETE FROM fixed_cost_tags WHERE tag_id=?", [tag_id])
    db.commit()
    flash(tr("Tag je obrisan."), "success")
    return redirect(url_for("fixed_costs.tags"))

@bp.route("/tags/<int:tag_id>/edit", methods=["POST"])
def edit_tag(tag_id):
    db = get_db()
    submodule = request.form.get("submodule", "operativni")
    venue     = request.form.get("venue", "shared") if submodule == "operativni" else "shared"
    db.execute(
        "UPDATE fixed_cost_tags SET submodule=?, venue=? WHERE tag_id=?",
        [submodule, venue, tag_id]
    )
    db.commit()
    flash(tr("Tag ažuriran."), "success")
    return redirect(url_for("fixed_costs.tags"))
