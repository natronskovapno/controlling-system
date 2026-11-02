"""
blueprints/wages.py
===================
Wages module — Garden Stage Controlling System

Landing view: records grouped by month (most recent first)
Secondary:    upload Excel templates (bookkeeper A/B, casual-labour C) with preview before commit
Stalni:       permanent-employee salary periods, fully editable (net, gross, dates)

Schema: wages_daily
  id, date, employee_name, worker_type, venue, net_amount,
  tax_bracket, gross_cost, source_file, notes, created_at, fitid

Schema: salaries_monthly
  id, employee_name, role, monthly_amount (net), monthly_gross,
  valid_from, valid_to, needs_sync, updated_at
  The gross factor is NOT stored — it is derived for display as gross / net.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
import os, hashlib, subprocess, sys
from i18n import tr

bp = Blueprint("wages", __name__)

# ── helpers ──────────────────────────────────────────────────────────

def _money(val):
    """Format number with dot thousands separator."""
    try:
        return f"{int(round(float(val))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _get_monthly_summary(db, filters):
    """
    Returns rows grouped by year-month, with totals.
    Optionally filtered by employee, venue, worker_type.
    """
    where = "WHERE 1=1"
    params = []

    if filters.get("employee"):
        where += " AND employee_name = ?"
        params.append(filters["employee"])
    if filters.get("venue"):
        where += " AND venue = ?"
        params.append(filters["venue"])
    if filters.get("worker_type"):
        where += " AND worker_type = ?"
        params.append(filters["worker_type"])
    if filters.get("date_from"):
        where += " AND date >= ?"
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where += " AND date <= ?"
        params.append(filters["date_to"])

    rows = db.execute(f"""
        SELECT
            strftime('%Y-%m', date)          AS month_key,
            strftime('%m/%Y', date)          AS month_label,
            COUNT(DISTINCT employee_name)    AS employee_count,
            COUNT(*)                         AS row_count,
            ROUND(SUM(net_amount), 0)        AS total_net,
            ROUND(SUM(gross_cost), 0)        AS total_gross
        FROM wages_daily
        {where}
        GROUP BY month_key
        ORDER BY month_key DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_month_detail(db, month_key, filters):
    """Individual rows for a given YYYY-MM month."""
    where = "WHERE strftime('%Y-%m', date) = ?"
    params = [month_key]

    if filters.get("employee"):
        where += " AND employee_name = ?"
        params.append(filters["employee"])
    if filters.get("venue"):
        where += " AND venue = ?"
        params.append(filters["venue"])

    rows = db.execute(f"""
        SELECT id, date, employee_name, worker_type, venue,
               COALESCE(net_amount, 0)  AS net_amount,
               tax_bracket,
               COALESCE(gross_cost, 0) AS gross_cost,
               source_file, notes
        FROM wages_daily
        {where}
        ORDER BY date, employee_name
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_month_aggregated(db, month_key, filters):
    """Per-employee aggregate for a given YYYY-MM month."""
    where = "WHERE strftime('%Y-%m', date) = ?"
    params = [month_key]

    if filters.get("employee"):
        where += " AND employee_name = ?"
        params.append(filters["employee"])
    if filters.get("venue"):
        where += " AND venue = ?"
        params.append(filters["venue"])

    rows = db.execute(f"""
        SELECT employee_name, worker_type,
               COUNT(*)                      AS stavki,
               ROUND(SUM(net_amount), 0)     AS total_net,
               ROUND(SUM(gross_cost), 0)     AS total_gross,
               MIN(tax_bracket)              AS tax_bracket
        FROM wages_daily
        {where}
        GROUP BY employee_name, worker_type
        ORDER BY employee_name
    """, params).fetchall()
    return [dict(r) for r in rows]


def _ensure_indexes(db):
    """Create indexes on wages_daily if they don't exist yet (one-time, fast after first run)."""
    db.execute("CREATE INDEX IF NOT EXISTS idx_wages_date ON wages_daily(date)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_wages_emp ON wages_daily(employee_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_wages_date_emp ON wages_daily(date, employee_name, net_amount, gross_cost)")
    # No commit needed — CREATE INDEX is auto-committed by SQLite


def _get_filter_options(db):
    """Distinct values for filter dropdowns — single query instead of three."""
    rows = db.execute("""
        SELECT DISTINCT employee_name, venue, worker_type FROM wages_daily
        WHERE employee_name IS NOT NULL
        ORDER BY employee_name
    """).fetchall()
    seen_emp, seen_venue, seen_wtype = set(), set(), set()
    employees, venues, worker_types = [], [], []
    for r in rows:
        if r[0] and r[0] not in seen_emp:
            seen_emp.add(r[0]); employees.append(r[0])
        if r[1] and r[1] not in seen_venue:
            seen_venue.add(r[1]); venues.append(r[1])
        if r[2] and r[2] not in seen_wtype:
            seen_wtype.add(r[2]); worker_types.append(r[2])
    venues.sort(); worker_types.sort()
    return employees, venues, worker_types


def _kpis_from_monthly(monthly):
    """Derive KPI totals from already-fetched monthly summary rows — no extra DB query."""
    if not monthly:
        return {}
    return {
        "employees":   max((m["employee_count"] for m in monthly), default=0),
        "records":     sum(m["row_count"]   for m in monthly),
        "total_net":   sum(m["total_net"]   for m in monthly),
        "total_gross": sum(m["total_gross"] for m in monthly),
        "date_min":    min(m["month_key"]   for m in monthly),
        "date_max":    max(m["month_key"]   for m in monthly),
    }


def _aggregated_from_rows(expanded_rows):
    """
    Build per-employee aggregates from already-fetched expanded_rows in Python.
    Eliminates a second DB round-trip on Google Drive.
    """
    from collections import defaultdict
    buckets = defaultdict(lambda: {"stavki": 0, "total_net": 0.0, "total_gross": 0.0, "tax_bracket": None})
    for r in expanded_rows:
        key = (r["employee_name"], r["worker_type"] or "")
        b = buckets[key]
        b["stavki"]     += 1
        b["total_net"]  += float(r["net_amount"]  or 0)
        b["total_gross"] += float(r["gross_cost"] or 0)
        if b["tax_bracket"] is None:
            b["tax_bracket"] = r.get("tax_bracket")
    result = []
    for (emp, wtype), b in sorted(buckets.items(), key=lambda x: x[0][0]):
        result.append({
            "employee_name": emp,
            "worker_type":   wtype or None,
            "stavki":        b["stavki"],
            "total_net":     round(b["total_net"],  0),
            "total_gross":   round(b["total_gross"], 0),
            "tax_bracket":   b["tax_bracket"],
        })
    return result



def _get_kpis(db, filters):
    where = "WHERE 1=1"
    params = []
    if filters.get("employee"):
        where += " AND employee_name = ?"; params.append(filters["employee"])
    if filters.get("venue"):
        where += " AND venue = ?"; params.append(filters["venue"])
    if filters.get("worker_type"):
        where += " AND worker_type = ?"; params.append(filters["worker_type"])
    if filters.get("date_from"):
        where += " AND date >= ?"; params.append(filters["date_from"])
    if filters.get("date_to"):
        where += " AND date <= ?"; params.append(filters["date_to"])

    row = db.execute(f"""
        SELECT
            COUNT(DISTINCT employee_name)  AS employees,
            COUNT(*)                       AS records,
            ROUND(SUM(net_amount), 0)      AS total_net,
            ROUND(SUM(gross_cost), 0)      AS total_gross,
            MIN(date)                      AS date_min,
            MAX(date)                      AS date_max
        FROM wages_daily {where}
    """, params).fetchone()
    return dict(row) if row else {}


# ── Excel parsing ─────────────────────────────────────────────────────


# ── File parsing (openpyxl-based, matches 07b_ingest_wages_historical.py) ──────

def _parse_date(value):
    import re
    from datetime import date as _d, datetime as _dt
    if value is None:
        return None
    if isinstance(value, (_d, _dt)):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, float):
        value = f"{value:.10g}."
    s = str(value).strip()
    for pat in [r'^(\d{1,2})[./](\d{1,2})[./](\d{4})$', r'^(\d{4})-(\d{2})-(\d{2})$']:
        m = re.match(pat, s)
        if m:
            try:
                g = m.groups()
                if len(g[2]) == 4:
                    d = _dt(int(g[2]), int(g[1]), int(g[0]))
                else:
                    d = _dt(int(g[0]), int(g[1]), int(g[2]))
                return d.strftime('%Y-%m-%d')
            except (ValueError, IndexError):
                continue
    # Handle double-dot like "09.01..2025" → strip extra dots first
    s_clean = re.sub(r'\.{2,}', '.', s)
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{3,4})', s_clean)
    if m:
        try:
            day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if yr < 100: yr += 2000
            return _dt(yr, mon, day).strftime('%Y-%m-%d')
        except ValueError:
            pass
    m2 = re.match(r'^(\d{1,2})\.(\d{1,2})\.$', s)
    if m2:
        return f"PARTIAL-{m2.group(2).zfill(2)}-{m2.group(1).zfill(2)}"
    return None


def _resolve_partial(partial, year):
    from datetime import datetime as _dt
    if not partial or not partial.startswith('PARTIAL-'):
        return partial
    try:
        _, mm, dd = partial.split('-')
        return _dt(year, int(mm), int(dd)).strftime('%Y-%m-%d')
    except ValueError:
        return None


def _get_bracket_for_date(db, employee_name, date_str):
    """Look up the applicable tax bracket from employee_bracket_history by date."""
    row = db.execute("""
        SELECT bracket FROM employee_bracket_history
        WHERE employee_name = ? AND valid_from <= ?
        ORDER BY valid_from DESC LIMIT 1
    """, [employee_name, date_str]).fetchone()
    return row["bracket"] if row else None


def _apply_tax(rows, db):
    """
    Add tax_bracket and gross_cost to each parsed row.
    Looks up bracket from employee_bracket_history by date.
    Bracket is a percentage string (e.g. '29', '76').
    gross_cost = net_amount * (1 + pct/100)
    """
    for r in rows:
        bracket = _get_bracket_for_date(db, r["employee_name"], r["date"])
        if bracket:
            try:
                pct = float(str(bracket).replace("%", "").strip())
                r["tax_bracket"] = str(bracket)
                r["gross_cost"]  = round(r["net_amount"] * (1 + pct / 100), 2)
            except (ValueError, TypeError):
                r["tax_bracket"] = "unknown"
                r["gross_cost"]  = 0
        else:
            r["tax_bracket"] = "unknown"
            r["gross_cost"]  = 0
        fitid_str = f"{r['date']}-{r['employee_name']}-{r['net_amount']}-{r['worker_type']}"
        r["fitid"] = hashlib.md5(fitid_str.encode()).hexdigest()[:16]
    return rows


def _detect_format_ws(ws):
    import re
    for r in range(1, min(ws.max_row+1, 15)):
        row_str = ' '.join(str(ws.cell(r, c).value or '') for c in range(1, ws.max_column+1))
        if any(kw in row_str for kw in ['Datumi', 'Cena sata']):
            return 'C'
        if re.search(r'Za\s+u[lp]?latu', row_str):
            return 'C'
    row2 = [ws.cell(2, c).value for c in range(1, min(ws.max_column+1, 25))]
    row2_str = ' '.join(str(v) for v in row2 if v)
    if re.search(r'(Ponedeljak|Utorak|Sreda|Četvrtak|Petak|Subota|Nedeljak)', row2_str):
        return 'B'
    if 'UKUPNO' in row2_str.upper():
        return 'B'
    row1 = [ws.cell(1, c).value for c in range(1, min(ws.max_column+1, 20))]
    names = sum(1 for v in row1[1:] if v and isinstance(v, str) and len(v.strip()) > 3)
    if names >= 2:
        return 'A'
    return 'A'


def _parse_format_a(ws, source_file):
    """Format A: names as column headers (row 1), dates as rows."""
    names = {}
    for c in range(2, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v and isinstance(v, str) and v.strip():
            names[c] = v.strip()
    rows = []
    for r in range(2, ws.max_row + 1):
        date_cell = ws.cell(r, 1).value
        if str(date_cell or '').strip().lower() in ('total', 'ukupno', 'porezi i doprinosi',
                                                     'ukupni trosak', 'ukupni trošak'):
            break
        parsed = _parse_date(date_cell)
        if not parsed or 'PARTIAL' in parsed:
            continue
        for col, name in names.items():
            val = ws.cell(r, col).value
            if val and isinstance(val, (int, float)) and float(val) > 0:
                rows.append({'date': parsed, 'employee_name': name,
                             'worker_type': 'konobar', 'venue': '',
                             'net_amount': float(val), 'source_file': source_file})
    return rows


def _parse_format_b(ws, source_file):
    """Format B: dates as column headers (row 2), names as rows.
    All non-date numeric columns are attributed to the nearest preceding date column.
    This handles second-shift amounts that land in unpaired columns.
    """
    import re
    date_cols = {}
    for c in range(4, ws.max_column + 1):
        v = ws.cell(2, c).value
        if v:
            d = _parse_date(v)
            if d and 'PARTIAL' not in d:
                date_cols[c] = d

    # Map every column to the nearest preceding date column
    col_to_date = {}
    last_date = None
    for c in range(4, ws.max_column + 1):
        if c in date_cols:
            last_date = date_cols[c]
        if last_date:
            col_to_date[c] = last_date

    # UKUPNO column = first col after last date col that has 'UKUPNO' header
    ukupno_col = None
    for c in range(4, ws.max_column + 1):
        v = str(ws.cell(2, c).value or '').upper()
        if 'UKUPNO' in v:
            ukupno_col = c
            break

    rows = []
    for r in range(5, ws.max_row + 1):
        name_cell = ws.cell(r, 2).value
        if not name_cell or not isinstance(name_cell, str) or not name_cell.strip():
            continue
        name = re.sub(r'[﻿\n]', '', name_cell).strip()
        if not name:
            continue
        for col, date_str in col_to_date.items():
            if ukupno_col and col >= ukupno_col:
                continue
            val = ws.cell(r, col).value
            if val and isinstance(val, (int, float)) and float(val) > 0:
                rows.append({'date': date_str, 'employee_name': name,
                             'worker_type': 'konobar', 'venue': '',
                             'net_amount': float(val), 'source_file': source_file})
    return rows


def _parse_format_c(ws, source_file):
    """Format C: casual labour — find header row with Datumi/Za uplatu."""
    import re
    from datetime import date as _d
    header_row = col_date = col_name = col_amount = None
    for r in range(1, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            v = str(ws.cell(r, c).value or '').strip()
            if v in ('Datumi', 'Datum'):
                header_row = r; col_date = c
            elif v in ('Imena', 'Ime'):
                col_name = c
            elif re.match(r'Za\s+u\S+atu', v, re.IGNORECASE) or v in ('Za isplatu', 'Iznos', 'Cena', 'Iznos', 'Ukupno'):
                col_amount = c
        if header_row:
            break
    if not header_row or not col_amount:
        return []
    rows = []
    current_date = None
    import re as _re
    m = _re.search(r'(\d{4})', source_file or '')
    year = int(m.group(1)) if m else _d.today().year
    for r in range(header_row + 1, ws.max_row + 1):
        row_vals = [ws.cell(r, c).value for c in range(1, ws.max_column+1)]
        if all(v is None for v in row_vals):
            continue
        if col_date:
            d_val = ws.cell(r, col_date).value
            if d_val:
                parsed = _parse_date(d_val)
                if parsed and 'PARTIAL' in parsed:
                    parsed = _resolve_partial(parsed, year)
                if parsed:
                    current_date = parsed
        amt_val = ws.cell(r, col_amount).value
        if not amt_val or not isinstance(amt_val, (int, float)) or float(amt_val) <= 0:
            continue
        name = None
        if col_name:
            n = ws.cell(r, col_name).value
            if n:
                name = str(n).strip().rstrip(',').strip()
        # Skip rows with no name — these are totals/summary rows
        if current_date and name:
            rows.append({'date': current_date, 'employee_name': name,
                         'worker_type': 'fizikalac', 'venue': '',
                         'net_amount': float(amt_val), 'source_file': source_file})
    return rows


def _parse_file(filepath, source_file, db):
    """Parse uploaded Excel file using openpyxl, detect format, return rows."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(filepath, data_only=True)
        ws = wb.active
    except Exception as e:
        return [], tr("Greška pri čitanju fajla:") + f" {e}"

    fmt = _detect_format_ws(ws)
    if fmt == 'A':
        rows = _parse_format_a(ws, source_file)
    elif fmt == 'B':
        rows = _parse_format_b(ws, source_file)
    else:
        rows = _parse_format_c(ws, source_file)

    if not rows:
        return [], tr("Fajl prepoznat kao Format {a0}, ali nisu pronađeni podaci.", a0=fmt)

    rows = _apply_tax(rows, db)
    return rows, fmt


# ── routes ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    _ensure_indexes(db)

    filters = {
        "employee":    request.args.get("employee", ""),
        "venue":       request.args.get("venue", ""),
        "worker_type": request.args.get("worker_type", ""),
        "date_from":   request.args.get("date_from", ""),
        "date_to":     request.args.get("date_to", ""),
    }
    expand_month = request.args.get("expand", "")
    view_mode    = request.args.get("view_mode", "daily")

    monthly                   = _get_monthly_summary(db, filters)
    kpis                      = _kpis_from_monthly(monthly)   # no extra DB query
    employees, venues, worker_types = _get_filter_options(db) # 1 query instead of 3

    expanded_rows   = []
    aggregated_rows = []
    if expand_month:
        expanded_rows   = _get_month_detail(db, expand_month, filters)
        aggregated_rows = _aggregated_from_rows(expanded_rows)  # pure Python, no DB

    return render_template(
        "wages/index.html",
        tab="dnevnice",
        kpis=kpis,
        monthly=monthly,
        employees=employees,
        employees_list=employees,   # the filter dropdown reads this name
        venues=venues,
        worker_types=worker_types,
        filters=filters,
        expand_month=expand_month,
        expanded_rows=expanded_rows,
        aggregated_rows=aggregated_rows,
        view_mode=view_mode,
    )


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    db = get_db()

    if request.method == "GET":
        return render_template("wages/upload.html")

    f = request.files.get("file")
    if not f or not f.filename:
        flash(tr("Nije izabran fajl."), "error")
        return redirect(url_for("wages.upload"))

    # Save to temp location
    import tempfile
    suffix = os.path.splitext(f.filename)[1]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp.name)
    tmp.close()

    rows, fmt_or_error = _parse_file(tmp.name, f.filename, db)
    os.unlink(tmp.name)

    if not rows:
        flash(tr("Nije moguće parsirati fajl: {fmt_or_error}", fmt_or_error=fmt_or_error), "error")
        return redirect(url_for("wages.upload"))

    # Store rows in server-side session to avoid JSON encoding issues in forms
    from flask import session
    import json
    session["wages_preview"] = json.dumps(rows, ensure_ascii=False)
    session["wages_source"]  = f.filename

    return render_template(
        "wages/preview.html",
        rows=rows,
        source_file=f.filename,
        fmt=fmt_or_error,
        row_count=len(rows),
    )


@bp.route("/commit", methods=["POST"])
def commit():
    """Commit previewed rows to wages_daily."""
    import json
    from flask import session
    db = get_db()

    rows_json = session.pop("wages_preview", None)
    if not rows_json:
        flash(tr("Sesija je istekla. Molimo ponovo učitajte fajl."), "error")
        return redirect(url_for("wages.upload"))
    try:
        rows = json.loads(rows_json)
    except Exception:
        flash(tr("Greška pri čitanju podataka."), "error")
        return redirect(url_for("wages.upload"))

    inserted = 0
    skipped  = 0
    now      = _date.today().isoformat()

    for r in rows:
        try:
            db.execute("""
                INSERT OR IGNORE INTO wages_daily
                  (date, employee_name, worker_type, venue,
                   net_amount, tax_bracket, gross_cost,
                   source_file, notes, created_at, fitid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, [
                r["date"], r["employee_name"], r.get("worker_type", ""),
                r.get("venue", ""), r["net_amount"],
                r.get("tax_bracket", "76%"), r.get("gross_cost", 0),
                r.get("source_file", ""), r.get("notes", ""),
                now, r.get("fitid", ""),
            ])
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            flash(tr("Greška pri upisu: {e}", e=e), "error")
            db.rollback()
            return redirect(url_for("wages.upload"))

    db.commit()
    flash(tr("Upisano {inserted} redova. Preskočeno (duplikati): {skipped}.", inserted=inserted, skipped=skipped), "success")
    return redirect(url_for("wages.index"))


# ══════════════════════════════════════════════════════════════════════
# STALNI ZAPOSLENI (salaries_monthly)
# ══════════════════════════════════════════════════════════════════════
#
# Every period is fully editable: net, gross, valid_from, valid_to.
# The gross factor is derived (gross / net), never stored.
#
# Any change sets a "needs sync" marker. Nothing is re-allocated
# automatically — the operator runs, in this order:
#   1. scripts/allocate_salaries_to_fixed_costs.py
#   2. Daily Log -> Preračunaj -> Fiksni troškovi
# The allocation script clears the marker when it finishes.

FAR_FUTURE = "9999-12-31"

SYNC_STEPS = "Dugme „Sinhronizuj sada“ pokreće alokaciju i preračun Daily Loga."

ALLOC_SCRIPT = "allocate_salaries_to_fixed_costs.py"


def _ensure_salary_columns(db):
    """Add the columns and the sync-state row used by the salary editor."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(salaries_monthly)").fetchall()]
    if "monthly_gross" not in cols:
        db.execute("ALTER TABLE salaries_monthly ADD COLUMN monthly_gross REAL DEFAULT 0")
    if "needs_sync" not in cols:
        db.execute("ALTER TABLE salaries_monthly ADD COLUMN needs_sync INTEGER DEFAULT 0")
    if "updated_at" not in cols:
        db.execute("ALTER TABLE salaries_monthly ADD COLUMN updated_at TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS salary_sync_state (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            dirty      INTEGER DEFAULT 0,
            updated_at TEXT,
            detail     TEXT
        )
    """)
    db.execute("INSERT OR IGNORE INTO salary_sync_state (id, dirty) VALUES (1, 0)")
    db.commit()


def _amount(raw, default=None):
    """Parse an amount from a form field. Accepts 158644, 158.644 and 158644,50."""
    s = (raw or "").strip().replace(" ", "")
    if s == "":
        return default
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return round(float(s), 2)


def _factor(net, gross):
    """Derived gross factor. None when it cannot be computed."""
    try:
        net = float(net or 0)
        gross = float(gross or 0)
    except (TypeError, ValueError):
        return None
    if net <= 0 or gross <= 0:
        return None
    return round(gross / net, 4)


def _last_factor(periods):
    """Most recent usable factor for an employee (periods ordered newest first)."""
    for p in periods:
        f = _factor(p["monthly_net"], p["monthly_gross"])
        if f:
            return f
    return None


def _mark_dirty(db, salary_ids, detail):
    """Flag the changed rows and the global marker. Caller commits."""
    now = _datetime.now().strftime("%Y-%m-%d %H:%M")
    for sid in salary_ids or []:
        db.execute("UPDATE salaries_monthly SET needs_sync = 1, updated_at = ? WHERE id = ?",
                   [now, sid])
    db.execute("UPDATE salary_sync_state SET dirty = 1, updated_at = ?, detail = ? WHERE id = 1",
               [now, detail])


def _calc_gross(net, db):
    """Fallback gross estimate. Only used when no gross is supplied at all."""
    try:
        return round(float(net) * 1.76, 2)
    except (TypeError, ValueError):
        return 0.0


def _overlap_error(db, employee_name, valid_from, valid_to, exclude_id=None):
    """Message if the period collides with another period of the same employee."""
    sql = "SELECT id, valid_from, valid_to FROM salaries_monthly WHERE employee_name = ?"
    params = [employee_name]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    end = valid_to or FAR_FUTURE
    for r in db.execute(sql, params).fetchall():
        other_end = r["valid_to"] or FAR_FUTURE
        if valid_from <= other_end and end >= r["valid_from"]:
            mine = valid_to or tr("otvoren")
            theirs = r["valid_to"] or tr("otvoren")
            return tr("Period {a} – {b} se preklapa sa postojećim {c} – {d}.",
                      a=valid_from, b=mine, c=r["valid_from"], d=theirs)
    return None


def _gap_warning(db, employee_name):
    """Flash a notice when the employee's periods leave an uncovered stretch."""
    rows = db.execute(
        "SELECT valid_from, valid_to FROM salaries_monthly "
        "WHERE employee_name = ? ORDER BY valid_from", [employee_name]).fetchall()
    gaps = []
    for prev, nxt in zip(rows, rows[1:]):
        if not prev["valid_to"]:
            continue
        gap_start = (_datetime.fromisoformat(prev["valid_to"]) + _timedelta(days=1)).strftime("%Y-%m-%d")
        if nxt["valid_from"] > gap_start:
            gap_end = (_datetime.fromisoformat(nxt["valid_from"]) - _timedelta(days=1)).strftime("%Y-%m-%d")
            gaps.append(gap_start + " – " + gap_end)
    if gaps:
        flash(tr("Napomena: {name} ima period bez plate: {gaps}.",
                 name=employee_name, gaps=", ".join(gaps)), "warning")


def _validate_dates(valid_from, valid_to):
    """Return an error message, or None when the pair is usable."""
    if not valid_from:
        return tr("Datum „važi od“ je obavezan.")
    if valid_to and valid_to < valid_from:
        return tr("Datum „važi do“ ne može biti pre datuma „važi od“.")
    return None


def _get_salaries_with_history(db):
    """All employees with their salary periods, newest first."""
    rows = db.execute("""
        SELECT
            id, employee_name, role,
            monthly_amount AS monthly_net,
            monthly_gross,
            valid_from,
            valid_to,
            COALESCE(needs_sync, 0) AS needs_sync,
            updated_at,
            CASE WHEN valid_to IS NULL OR valid_to = '' THEN 1 ELSE 0 END AS is_active
        FROM salaries_monthly
        ORDER BY employee_name, valid_from DESC
    """).fetchall()

    from collections import defaultdict
    employees = defaultdict(lambda: {"periods": [], "role": None})

    for r in rows:
        d = dict(r)
        d["factor"] = _factor(d["monthly_net"], d["monthly_gross"])
        emp = employees[d["employee_name"]]
        if emp["role"] is None:
            emp["role"] = d["role"]
        emp["periods"].append(d)

    result = []
    for name, data in employees.items():
        periods = data["periods"]
        active = next((p for p in periods if p["is_active"]), None)
        result.append({
            "employee_name":  name,
            "role":           data["role"],
            "periods":        periods,
            "current_net":    active["monthly_net"] if active else 0,
            "current_gross":  active["monthly_gross"] if active else 0,
            "current_factor": _factor(active["monthly_net"], active["monthly_gross"]) if active else None,
            "last_factor":    _last_factor(periods),
            "is_ended":       active is None,
            "needs_sync":     any(p["needs_sync"] for p in periods),
            "ended_on":       max((p["valid_to"] for p in periods if p["valid_to"]), default=None) if active is None else None,
        })

    return sorted(result, key=lambda x: x["employee_name"])


@bp.route("/stalni")
def stalni():
    db = get_db()
    _ensure_salary_columns(db)

    employees = _get_salaries_with_history(db)
    total_net   = sum(e["current_net"]   or 0 for e in employees)
    total_gross = sum(e["current_gross"] or 0 for e in employees)

    state = db.execute("SELECT dirty, updated_at, detail FROM salary_sync_state WHERE id = 1").fetchone()
    dirty_rows = [dict(r) for r in db.execute("""
        SELECT employee_name, valid_from, valid_to, updated_at
        FROM salaries_monthly WHERE COALESCE(needs_sync, 0) = 1
        ORDER BY employee_name, valid_from
    """).fetchall()]
    sync = {
        "dirty":      bool(state["dirty"]) if state else False,
        "updated_at": state["updated_at"] if state else None,
        "detail":     state["detail"] if state else None,
        "rows":       dirty_rows,
        "from_month": min((d["valid_from"][:7] for d in dirty_rows), default=None),
        "steps":      tr(SYNC_STEPS),
    }

    return render_template(
        "wages/index.html",
        tab="stalni",
        employees=employees,
        total_net=total_net,
        total_gross=total_gross,
        sync=sync,
        today=_date.today().isoformat(),
        kpis={}, monthly=[], employees_list=[], venues=[],
        worker_types=[], filters={}, expand_month="", expanded_rows=[],
        aggregated_rows=[], view_mode="daily"
    )


# ── in-app sync: allocation script + Daily Log recalculation ─────────
#   Both steps used to be run by hand from a terminal. They are now one
#   POST. Step 1 is the allocation script as a subprocess (it owns the
#   salary -> invoice_fixed_cost_links logic, including the stale-link
#   cleanup). Step 2 calls daily_log.recalc_fixed_costs() directly — it is
#   an ordinary function, so the route reuses it instead of duplicating the
#   fixed-cost rules here.

def _now_stamp():
    return _datetime.now().strftime("%Y-%m-%d %H:%M")


def _tail_lines(text, limit=25):
    lines = [l.rstrip() for l in (text or "").splitlines() if l.strip()]
    if len(lines) > limit:
        lines = [tr("… ({n} linija izostavljeno)", n=len(lines) - limit)] + lines[-limit:]
    return "\n".join(lines)


def _run_allocation(dry_run=False):
    """
    Run the allocation script as a subprocess.
    Returns (ok: bool, output: str).
    """
    try:
        import config as _cfg
    except ImportError:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import config as _cfg

    script = os.path.join(_cfg.SCRIPTS_DIR, ALLOC_SCRIPT)
    if not os.path.exists(script):
        return False, tr("Skripta nije pronađena: {script}", script=script)

    cmd = [_cfg.PYTHON_EXE, script]
    if dry_run:
        cmd.append("--dry-run")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    cwd = os.path.dirname(os.path.abspath(_cfg.__file__))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=600, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return False, tr("Skripta je prekoračila vreme izvršavanja (10 minuta).")
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)

    out = (proc.stdout or "")
    if proc.stderr:
        out = out + "\n" + proc.stderr
    return proc.returncode == 0, out.strip()


@bp.route("/stalni/sinhronizuj", methods=["POST"])
def stalni_sync():
    """
    mode=dry  → allocation with --dry-run, nothing written, output shown
    mode=full → allocation for real, then Daily Log fixed-cost recalculation,
                then the sync markers are cleared
    """
    db = get_db()
    _ensure_salary_columns(db)
    dry = (request.form.get("mode") or "full").strip().lower() == "dry"

    # Hand the write lock to the subprocess before it opens the same file.
    db.commit()

    ok, out = _run_allocation(dry_run=dry)
    tail = _tail_lines(out)

    if not ok:
        db.execute("UPDATE salary_sync_state SET detail = ? WHERE id = 1",
                   ["Greška (%s):\n%s" % (_now_stamp(), tail)])
        db.commit()
        flash(tr("Alokacija nije uspela — ništa nije preneto. "
                 "Detalji su u kartici „Kako funkcioniše“."), "error")
        return redirect(url_for("wages.stalni"))

    if dry:
        db.execute("UPDATE salary_sync_state SET detail = ? WHERE id = 1",
                   ["Proba bez upisa (%s):\n%s" % (_now_stamp(), tail)])
        db.commit()
        flash(tr("Proba je prošla — ništa nije upisano. Pregledajte rezultat, "
                 "pa pokrenite „Sinhronizuj sada“."), "info")
        return redirect(url_for("wages.stalni"))

    # Step 2 — Daily Log fixed costs. The view function flashes its own count;
    # its redirect is discarded because this route issues its own.
    try:
        from blueprints import daily_log as _dl
        _dl.recalc_fixed_costs()
        step2 = tr("Daily Log je preračunat.")
    except Exception as exc:
        step2 = tr("Daily Log NIJE preračunat ({kind}: {err}) — pokrenite "
                   "Daily Log → Preračunaj → Fiksni troškovi.",
                   kind=type(exc).__name__, err=exc)

    db = get_db()
    db.execute("UPDATE salaries_monthly SET needs_sync = 0 "
               "WHERE COALESCE(needs_sync, 0) = 1")
    db.execute("UPDATE salary_sync_state SET dirty = 0, updated_at = ?, detail = ? "
               "WHERE id = 1",
               [_now_stamp(), "Sinhronizovano (%s). %s\n%s" % (_now_stamp(), step2, tail)])
    db.commit()

    flash(tr("Alokacija je završena. {step2}", step2=step2), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/add", methods=["POST"])
def stalni_add():
    """Create a new permanent employee with their first salary period."""
    db = get_db()
    _ensure_salary_columns(db)
    f = request.form
    name = f.get("employee_name", "").strip()
    role = f.get("role", "").strip()
    valid_from = (f.get("valid_from", "") or "").strip() or _date.today().isoformat()

    if not name:
        flash(tr("Ime zaposlenog je obavezno."), "error")
        return redirect(url_for("wages.stalni"))
    if db.execute("SELECT 1 FROM salaries_monthly WHERE employee_name = ?", [name]).fetchone():
        flash(tr("Zaposleni „{name}“ već postoji — dodajte mu novi period "
                 "umesto novog zaposlenog.", name=name), "error")
        return redirect(url_for("wages.stalni"))

    try:
        net_val   = _amount(f.get("monthly_net"), 0.0)
        gross_val = _amount(f.get("monthly_gross"), None)
    except ValueError:
        flash(tr("Neispravan iznos."), "error")
        return redirect(url_for("wages.stalni"))
    if gross_val is None:
        gross_val = _calc_gross(net_val, db)

    err = _validate_dates(valid_from, None)
    if err:
        flash(err, "error")
        return redirect(url_for("wages.stalni"))

    cur = db.execute("""
        INSERT INTO salaries_monthly (employee_name, role, monthly_amount, monthly_gross, valid_from, valid_to)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, [name, role, net_val, gross_val, valid_from])
    _mark_dirty(db, [cur.lastrowid], f"Dodat zaposleni: {name}")
    db.commit()
    flash(tr("Dodat zaposleni: {name}.", name=name), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/add-period", methods=["POST"])
def stalni_add_period():
    """Add a new salary period for an existing employee."""
    db = get_db()
    _ensure_salary_columns(db)
    f = request.form
    employee_name = f.get("employee_name", "").strip()
    valid_from    = (f.get("valid_from", "") or "").strip()
    valid_to      = (f.get("valid_to", "") or "").strip() or None

    if not employee_name or not valid_from:
        flash(tr("Ime zaposlenog i datum početka su obavezni."), "error")
        return redirect(url_for("wages.stalni"))

    err = _validate_dates(valid_from, valid_to)
    if err:
        flash(err, "error")
        return redirect(url_for("wages.stalni"))

    try:
        net_val   = _amount(f.get("monthly_net"), 0.0)
        gross_val = _amount(f.get("monthly_gross"), None)
    except ValueError:
        flash(tr("Neispravan iznos."), "error")
        return redirect(url_for("wages.stalni"))
    if gross_val is None:
        gross_val = _calc_gross(net_val, db)

    # An open-ended new period closes the open period that starts BEFORE it.
    # Without that "starts before" guard a back-dated entry would stamp an
    # earlier end date onto a later period. A period entered WITH an end date
    # is a historical insert and closes nothing.
    prev_end = (_datetime.fromisoformat(valid_from) - _timedelta(days=1)).strftime("%Y-%m-%d")
    to_close = [] if valid_to else db.execute("""
        SELECT id FROM salaries_monthly
        WHERE employee_name = ? AND (valid_to IS NULL OR valid_to = '') AND valid_from < ?
    """, [employee_name, valid_from]).fetchall()

    # Validate against everything except the rows we are about to close
    closing_ids = [r["id"] for r in to_close]
    new_end = valid_to or FAR_FUTURE
    sql = "SELECT id, valid_from, valid_to FROM salaries_monthly WHERE employee_name = ?"
    for r in db.execute(sql, [employee_name]).fetchall():
        if r["id"] in closing_ids:
            continue
        other_end = r["valid_to"] or FAR_FUTURE
        if valid_from <= other_end and new_end >= r["valid_from"]:
            theirs = r["valid_to"] or "otvoren"
            mine = valid_to or "otvoren"
            flash(tr("Novi period {a} – {b} se preklapa sa postojećim {c} – {d}. "
                     "Izmenite postojeći period ili unesite „važi do“.",
                     a=valid_from, b=mine, c=r["valid_from"], d=theirs), "error")
            return redirect(url_for("wages.stalni"))

    for r in to_close:
        db.execute("UPDATE salaries_monthly SET valid_to = ? WHERE id = ?", [prev_end, r["id"]])

    role_row = db.execute(
        "SELECT role FROM salaries_monthly WHERE employee_name = ? LIMIT 1", [employee_name]).fetchone()
    role = role_row["role"] if role_row else ""

    cur = db.execute("""
        INSERT INTO salaries_monthly
        (employee_name, role, monthly_amount, monthly_gross, valid_from, valid_to)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [employee_name, role, net_val, gross_val, valid_from, valid_to])

    _mark_dirty(db, closing_ids + [cur.lastrowid], f"Nova plata: {employee_name} od {valid_from}")
    db.commit()
    _gap_warning(db, employee_name)
    flash(tr("Nova plata za {name} od {d}: {amount} RSD.",
             name=employee_name, d=valid_from, amount=_money(net_val)), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/<int:salary_id>/edit", methods=["POST"])
def stalni_edit(salary_id):
    """Edit any salary period, historical ones included."""
    db = get_db()
    _ensure_salary_columns(db)
    row = db.execute("SELECT * FROM salaries_monthly WHERE id = ?", [salary_id]).fetchone()
    if not row:
        flash(tr("Period nije pronađen."), "error")
        return redirect(url_for("wages.stalni"))

    f = request.form
    try:
        net_val   = _amount(f.get("monthly_net"), 0.0)
        gross_val = _amount(f.get("monthly_gross"), 0.0)
    except ValueError:
        flash(tr("Neispravan iznos."), "error")
        return redirect(url_for("wages.stalni"))

    valid_from = (f.get("valid_from", "") or "").strip()
    valid_to   = (f.get("valid_to", "") or "").strip() or None

    err = _validate_dates(valid_from, valid_to)
    if err:
        flash(err, "error")
        return redirect(url_for("wages.stalni"))

    err = _overlap_error(db, row["employee_name"], valid_from, valid_to, exclude_id=salary_id)
    if err:
        flash(err, "error")
        return redirect(url_for("wages.stalni"))

    db.execute("""
        UPDATE salaries_monthly
        SET monthly_amount = ?, monthly_gross = ?, valid_from = ?, valid_to = ?
        WHERE id = ?
    """, [net_val, gross_val, valid_from, valid_to, salary_id])
    _mark_dirty(db, [salary_id], f"Izmenjen period: {row['employee_name']} {valid_from}")
    db.commit()
    _gap_warning(db, row["employee_name"])
    flash(tr("Izmenjen period za {name} ({d}).",
             name=row["employee_name"], d=valid_from), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/rename", methods=["POST"])
def stalni_rename():
    """Rename an employee or change their role across all of their periods."""
    db = get_db()
    _ensure_salary_columns(db)
    f = request.form
    old_name = f.get("old_name", "").strip()
    new_name = f.get("employee_name", "").strip()
    role     = f.get("role", "").strip()

    if not old_name or not new_name:
        flash(tr("Ime zaposlenog je obavezno."), "error")
        return redirect(url_for("wages.stalni"))
    if new_name != old_name and db.execute(
            "SELECT 1 FROM salaries_monthly WHERE employee_name = ?", [new_name]).fetchone():
        flash(tr("Zaposleni „{name}“ već postoji.", name=new_name), "error")
        return redirect(url_for("wages.stalni"))

    ids = [r["id"] for r in db.execute(
        "SELECT id FROM salaries_monthly WHERE employee_name = ?", [old_name]).fetchall()]
    db.execute("UPDATE salaries_monthly SET employee_name = ?, role = ? WHERE employee_name = ?",
               [new_name, role, old_name])
    _mark_dirty(db, ids, f"Preimenovan zaposleni: {old_name} → {new_name}")
    db.commit()
    flash(tr("Ažurirano: {name}", name=new_name)
          + (tr(" (ranije {old})", old=old_name) if new_name != old_name else "")
          + ".", "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/end", methods=["POST"])
def stalni_end():
    """End employment: close the open period on the given date."""
    db = get_db()
    _ensure_salary_columns(db)
    f = request.form
    employee_name = f.get("employee_name", "").strip()
    end_date      = (f.get("end_date", "") or "").strip()

    if not employee_name or not end_date:
        flash(tr("Ime zaposlenog i datum prestanka su obavezni."), "error")
        return redirect(url_for("wages.stalni"))

    row = db.execute("""
        SELECT id, valid_from FROM salaries_monthly
        WHERE employee_name = ? AND (valid_to IS NULL OR valid_to = '')
        ORDER BY valid_from DESC LIMIT 1
    """, [employee_name]).fetchone()
    if not row:
        flash(tr("{name} nema otvoren period — radni odnos je već završen.",
                 name=employee_name), "error")
        return redirect(url_for("wages.stalni"))
    if end_date < row["valid_from"]:
        flash(tr("Datum prestanka ne može biti pre početka poslednjeg perioda."), "error")
        return redirect(url_for("wages.stalni"))

    db.execute("UPDATE salaries_monthly SET valid_to = ? WHERE id = ?", [end_date, row["id"]])
    _mark_dirty(db, [row["id"]], f"Završen radni odnos: {employee_name} na {end_date}")
    db.commit()
    flash(tr("Radni odnos za {name} završen na {d}.",
             name=employee_name, d=end_date), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/<int:salary_id>/reopen", methods=["POST"])
def stalni_reopen(salary_id):
    """Clear valid_to on the employee's last period — undoes an end-of-employment."""
    db = get_db()
    _ensure_salary_columns(db)
    row = db.execute("SELECT employee_name, valid_from FROM salaries_monthly WHERE id = ?",
                     [salary_id]).fetchone()
    if not row:
        flash(tr("Period nije pronađen."), "error")
        return redirect(url_for("wages.stalni"))
    later = db.execute("""
        SELECT 1 FROM salaries_monthly
        WHERE employee_name = ? AND valid_from > ?
    """, [row["employee_name"], row["valid_from"]]).fetchone()
    if later:
        flash(tr("Period se ne može otvoriti — postoji kasniji period za istog zaposlenog."), "error")
        return redirect(url_for("wages.stalni"))

    db.execute("UPDATE salaries_monthly SET valid_to = NULL WHERE id = ?", [salary_id])
    _mark_dirty(db, [salary_id], f"Ponovo otvoren period: {row['employee_name']}")
    db.commit()
    flash(tr("Period za {name} je ponovo aktivan.", name=row["employee_name"]), "success")
    return redirect(url_for("wages.stalni"))


@bp.route("/stalni/<int:salary_id>/delete", methods=["POST"])
def stalni_delete(salary_id):
    """Delete a period and repair the chain if the open period was removed."""
    db = get_db()
    _ensure_salary_columns(db)
    row = db.execute(
        "SELECT employee_name, valid_from, valid_to FROM salaries_monthly WHERE id = ?",
        [salary_id]).fetchone()
    if not row:
        flash(tr("Period nije pronađen."), "error")
        return redirect(url_for("wages.stalni"))

    name    = row["employee_name"]
    was_open = not row["valid_to"]
    db.execute("DELETE FROM salaries_monthly WHERE id = ?", [salary_id])

    touched = []
    reopened = None
    if was_open:
        prev = db.execute("""
            SELECT id FROM salaries_monthly
            WHERE employee_name = ? AND valid_from < ?
            ORDER BY valid_from DESC LIMIT 1
        """, [name, row["valid_from"]]).fetchone()
        if prev:
            db.execute("UPDATE salaries_monthly SET valid_to = NULL WHERE id = ?", [prev["id"]])
            touched.append(prev["id"])
            reopened = True

    _mark_dirty(db, touched, f"Obrisan period: {name} {row['valid_from']}")
    db.commit()

    msg = tr("Obrisan period za {name} ({d}).", name=name, d=row["valid_from"])
    if reopened:
        msg += " " + tr("Prethodni period je ponovo aktivan.")
    flash(msg, "success")
    return redirect(url_for("wages.stalni"))


# ══════════════════════════════════════════════════════════════════════
# ZAPOSLENI (Employees) — Tax bracket management
# ══════════════════════════════════════════════════════════════════════
 
def _ensure_bracket_tables(db):
    """Create employee_bracket_history table if not exists."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS employee_bracket_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT    NOT NULL,
            bracket       TEXT    NOT NULL,
            valid_from    TEXT    NOT NULL,
            notes         TEXT,
            created_at    TEXT    DEFAULT (datetime('now')),
            UNIQUE(employee_name, valid_from)
        )
    """)
    db.commit()
 
 
def _gross_cost(net, bracket_pct_str):
    """Compute gross cost from net amount and bracket percentage string."""
    try:
        pct = float(str(bracket_pct_str).replace('%','').strip())
        return round(float(net) * (1 + pct / 100), 2)
    except (TypeError, ValueError):
        return 0.0
 
 
def _get_employees_with_brackets(db):
    """Get employees from wages_daily with their current bracket."""
    wage_rows = db.execute("""
        SELECT employee_name, worker_type,
               COUNT(id) as wage_rows,
               SUM(net_amount) as total_net,
               SUM(gross_cost) as total_gross
        FROM wages_daily
        GROUP BY employee_name, worker_type
        ORDER BY employee_name
    """).fetchall()
 
    bracket_rows = db.execute("""
        SELECT employee_name, bracket, valid_from
        FROM employee_bracket_history
        ORDER BY employee_name, valid_from DESC
    """).fetchall()
    
    brackets = {}
    for r in bracket_rows:
        name = r['employee_name']
        if name not in brackets:
            brackets[name] = dict(r)
 
    result = []
    for w in wage_rows:
        d = dict(w)
        b = brackets.get(d['employee_name'], {})
        d['bracket']    = b.get('bracket')
        d['valid_from'] = b.get('valid_from')
        result.append(d)
    return result
 
 
def _get_bracket_history(db):
    """Get all bracket history grouped by employee."""
    employees = _get_employees_with_brackets(db)
    history = {}
    for e in employees:
        name = e['employee_name']
        rows = db.execute("""
            SELECT * FROM employee_bracket_history
            WHERE employee_name = ?
            ORDER BY valid_from DESC
        """, [name]).fetchall()
        history[name] = [dict(r) for r in rows]
    return history
 
 
@bp.route("/zaposleni")
def zaposleni():
    """Employees tab — tax bracket management."""
    from datetime import date
    db = get_db()
    _ensure_bracket_tables(db)
    
    employees_data = _get_employees_with_brackets(db)
    history = _get_bracket_history(db)
    
    return render_template(
        "wages/index.html",
        tab="zaposleni",
        employees_data=employees_data,
        history=history,
        today=date.today().isoformat(),
        # Empty defaults for other tabs
        kpis={}, monthly=[], employees=[], employees_list=[], venues=[],
        worker_types=[], filters={}, expand_month="", expanded_rows=[],
        aggregated_rows=[], view_mode="daily",
        total_net=0, total_gross=0
    )
 
 
@bp.route("/zaposleni/set-bracket", methods=["POST"])
def zaposleni_set_bracket():
    """Set or update employee tax bracket."""
    db = get_db()
    _ensure_bracket_tables(db)
    
    f             = request.form
    employee_name = f.get("employee_name","").strip()
    bracket_raw   = f.get("bracket","").strip().replace('%','')
    valid_from    = f.get("valid_from","")
    notes         = f.get("notes","").strip() or None
 
    try:
        bracket_pct = float(bracket_raw)
        if bracket_pct <= 0 or bracket_pct > 200:
            raise ValueError
    except ValueError:
        flash(tr("Unesite ispravnu stopu (broj između 1 i 200)."), "error")
        return redirect(url_for("wages.zaposleni"))
 
    if not employee_name or not valid_from:
        flash(tr("Popunite sva polja."), "error")
        return redirect(url_for("wages.zaposleni"))
 
    bracket_str = str(int(bracket_pct)) if bracket_pct == int(bracket_pct) else str(bracket_pct)
 
    try:
        db.execute("""
            INSERT INTO employee_bracket_history
                (employee_name, bracket, valid_from, notes)
            VALUES (?,?,?,?)
            ON CONFLICT(employee_name, valid_from) DO UPDATE SET
                bracket=excluded.bracket, notes=excluded.notes
        """, [employee_name, bracket_str, valid_from, notes])
        db.commit()
        flash(tr("Stopa {bracket_str}% za '{employee_name}' važi od {valid_from}.", bracket_str=bracket_str, employee_name=employee_name, valid_from=valid_from), "success")
    except Exception as e:
        flash(tr("Greška: {e}", e=e), "error")
        return redirect(url_for("wages.zaposleni"))
 
    # Recalculate gross_cost for this employee from valid_from onwards
    wages = db.execute("""
        SELECT id, date, net_amount FROM wages_daily
        WHERE employee_name = ? AND date >= ?
    """, [employee_name, valid_from]).fetchall()
 
    updated = 0
    for w in wages:
        b = _get_bracket_for_date(db, employee_name, w['date'])
        if b:
            gc = _gross_cost(w['net_amount'], b)
            db.execute("UPDATE wages_daily SET gross_cost=?, tax_bracket=? WHERE id=?",
                       [gc, b, w['id']])
            updated += 1
    db.commit()
    flash(tr("Preračunato {updated} stavki zarada.", updated=updated), "success")
    return redirect(url_for("wages.zaposleni"))
 
 
@bp.route("/zaposleni/recalc-all", methods=["POST"])
def zaposleni_recalc_all():
    """Recalculate all wage gross costs based on current brackets."""
    db = get_db()
    _ensure_bracket_tables(db)
    
    wages = db.execute("SELECT id, date, employee_name, net_amount FROM wages_daily").fetchall()
    updated = skipped = 0
    
    for w in wages:
        b = _get_bracket_for_date(db, w['employee_name'], w['date'])
        if b:
            gc = _gross_cost(w['net_amount'], b)
            db.execute("UPDATE wages_daily SET gross_cost=?, tax_bracket=? WHERE id=?",
                       [gc, b, w['id']])
            updated += 1
        else:
            skipped += 1
    
    db.commit()
    flash(tr("Preračunato {updated} stavki | {skipped} bez definisane stope.", updated=updated, skipped=skipped), "success")
    return redirect(url_for("wages.zaposleni"))
 
 
@bp.route("/zaposleni/delete-bracket/<int:bracket_id>", methods=["POST"])
def zaposleni_delete_bracket(bracket_id):
    """Delete a bracket history entry."""
    db = get_db()
    db.execute("DELETE FROM employee_bracket_history WHERE id=?", [bracket_id])
    db.commit()
    flash(tr("Stopa obrisana."), "success")
    return redirect(url_for("wages.zaposleni"))
