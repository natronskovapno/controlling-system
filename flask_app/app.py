"""
Garden Stage — Controlling Dashboard
Flask application entry point

Usage:
    python app.py

Then open: http://localhost:5000
"""

import os
import sys
import tempfile
import threading
from datetime import date, datetime, timezone

from flask import Flask, render_template, jsonify, request
from db import get_db, close_db
import i18n

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Paths, the Flask secret and the interpreter used to launch helper scripts all
# come from config.py at the PROJECT ROOT (one level above flask_app/), so the
# drive letter and folder name no longer matter.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    import config
except ImportError:
    raise SystemExit(
        "\nERROR: cannot import config.py\n"
        f"  expected at: {os.path.join(_ROOT, 'config.py')}\n"
        "  The dashboard now reads all paths and settings from that file.\n"
        "  Copy config.py to the project root and run it once to verify:\n"
        "      python config.py\n"
    )

DB_PATH = config.DB_PATH

app = Flask(__name__)
app.config["DB_PATH"] = DB_PATH
app.secret_key = config.FLASK_SECRET_KEY

app.teardown_appcontext(close_db)

# Bilingual UI (Serbian default, English available). Registers the t() helper
# as a Jinja global and the /lang/<code> switch.
i18n.init_app(app)

# ---------------------------------------------------------------------------
# Register blueprints
# ---------------------------------------------------------------------------

from blueprints.daily_log   import bp as daily_log_bp
from blueprints.cash        import bp as cash_bp
from blueprints.invoices    import bp as invoices_bp
from blueprints.bank        import bp as bank_bp
from blueprints.fixed_costs import bp as fixed_costs_bp
from blueprints.wages       import bp as wages_bp
from blueprints.brewery import bp as brewery_bp
from blueprints.ingestion   import bp as ingestion_bp
from blueprints.checks      import bp as checks_bp
from blueprints.sales       import bp as sales_bp
from blueprints.debit_cards import bp as debit_cards_bp
from blueprints.event_type_analysis import bp as event_type_analysis_bp
from blueprints.projections import bp as projections_bp
from blueprints.performance import bp as performance_bp
from blueprints.pdv        import bp as pdv_bp
from blueprints.api        import bp as api_bp
from blueprints.discounts  import bp as discounts_bp
from blueprints.prenosi    import bp as prenosi_bp
from blueprints.inventory  import bp as inventory_bp
from blueprints.web_analytics import bp as web_analytics_bp
from blueprints.fin_statements import bp as fin_statements_bp

app.register_blueprint(daily_log_bp,   url_prefix="/daily-log")
app.register_blueprint(cash_bp,        url_prefix="/cash")
app.register_blueprint(invoices_bp,    url_prefix="/invoices")
app.register_blueprint(bank_bp,        url_prefix="/bank")
app.register_blueprint(fixed_costs_bp, url_prefix="/fixed-costs")
app.register_blueprint(wages_bp,       url_prefix="/wages")
app.register_blueprint(brewery_bp, url_prefix="/brewery")
app.register_blueprint(ingestion_bp,   url_prefix="/ingestion")
app.register_blueprint(sales_bp,       url_prefix="/sales")
app.register_blueprint(debit_cards_bp, url_prefix="/debit-cards")
app.register_blueprint(event_type_analysis_bp, url_prefix="/event-type-analysis")
app.register_blueprint(projections_bp, url_prefix="/projections")
app.register_blueprint(performance_bp, url_prefix="/performance")
app.register_blueprint(pdv_bp,        url_prefix="/pdv")
app.register_blueprint(api_bp,        url_prefix="/api")
app.register_blueprint(discounts_bp,   url_prefix="/discounts")
app.register_blueprint(prenosi_bp,     url_prefix="/prenosi")
app.register_blueprint(inventory_bp,   url_prefix="/inventory")
app.register_blueprint(checks_bp,      url_prefix="/checks")
app.register_blueprint(web_analytics_bp, url_prefix="/web-analytics")
app.register_blueprint(fin_statements_bp, url_prefix="/finansijski-izvestaji")



# ---------------------------------------------------------------------------
# Cloudflare Web Analytics — lazy daily catch-up
# ---------------------------------------------------------------------------
# The free Cloudflare plan discards Web Analytics data after ~30 days, so a
# missed run becomes a permanent hole in the history. A Windows scheduled
# task was rejected because this machine is off at weekends; instead the
# first request of each calendar day triggers a catch-up.
#
# Nothing blocks the request: the staleness check runs in a worker thread and
# the ingestion itself is a detached subprocess. The thread opens its own
# sqlite3 connection rather than using get_db(), because Flask's connection
# is bound to the request context and must not cross into another thread.

_cf_seen_date = None
_cf_lock = threading.Lock()


def _cf_needs_run():
    """True if the newest ingested date is older than yesterday (UTC)."""
    import sqlite3
    from datetime import timedelta as _td
    yesterday = (datetime.now(timezone.utc).date() - _td(days=1)).isoformat()
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        row = conn.execute("SELECT MAX(last_date) FROM web_analytics_cursor").fetchone()
        conn.close()
    except Exception:
        return True          # table absent => never ingested => run it
    return not row or not row[0] or row[0] < yesterday


def _cf_worker():
    import subprocess
    try:
        if not _cf_needs_run():
            return
        script = os.path.join(config.SCRIPTS_DIR, "12_cloudflare_analytics.py")
        if not os.path.exists(script):
            return
        log_path = os.path.join(tempfile.gettempdir(), "cf_autoingest.log")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n=== auto-ingest %s ===\n"
                     % datetime.now().isoformat(timespec="seconds"))
            fh.flush()
            subprocess.Popen([sys.executable, script],
                             stdout=fh, stderr=subprocess.STDOUT,
                             cwd=config.SCRIPTS_DIR, env=env)
    except Exception:
        pass                 # analytics must never break a page load


@app.before_request
def _cf_autoingest():
    global _cf_seen_date
    if not getattr(config, "CF_AUTO_INGEST", False):
        return
    if request.path.startswith("/static") or request.path.startswith("/api/ping"):
        return
    today = date.today().isoformat()
    with _cf_lock:
        if _cf_seen_date == today:
            return
        _cf_seen_date = today
    threading.Thread(target=_cf_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Template context — inject today's date for overdue highlighting
# ---------------------------------------------------------------------------

@app.context_processor
def inject_now():
    from datetime import date
    return {
        "now_date": date.today().isoformat(),
        "now_month": date.today().month,
        "enumerate": enumerate,
    }

# ---------------------------------------------------------------------------
# Home / dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    from datetime import date, timedelta
    from blueprints.cash import _get_account_balances, _get_payables, _get_receivables, _build_summary, _ensure_invoice_columns
    db = get_db()

    # ── Date range filter ────────────────────────────────────────────
    preset    = request.args.get("preset", "ytd")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    today     = date.today()

    if preset == "today":
        date_from = date_to = today.isoformat()
    elif preset == "week":
        date_from = (today - timedelta(days=today.weekday())).isoformat()
        date_to   = today.isoformat()
    elif preset == "month":
        date_from = today.replace(day=1).isoformat()
        date_to   = today.isoformat()
    elif preset == "last_month":
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        date_from  = last_prev.replace(day=1).isoformat()
        date_to    = last_prev.isoformat()
    elif preset == "ytd":
        date_from = today.replace(month=1, day=1).isoformat()
        date_to   = today.isoformat()
    elif preset == "all":
        date_from = "2025-01-01"
        date_to   = today.isoformat()
    # preset == "custom" uses whatever date_from/date_to were passed

    where_bills  = "WHERE is_revoked = 0"
    params_bills = []
    if date_from:
        where_bills += " AND date >= ?"; params_bills.append(date_from)
    if date_to:
        where_bills += " AND date <= ?"; params_bills.append(date_to)

    # ── Sales KPIs (date-filtered) ───────────────────────────────────
    try:
        row = db.execute(
            f"SELECT ROUND(SUM(amount)/1000000.0, 2) AS total_m FROM bills_raw {where_bills}",
            params_bills
        ).fetchone()
        total_sales_m = row["total_m"] if row and row["total_m"] else 0.0
    except Exception:
        total_sales_m = None

    try:
        row = db.execute(
            f"SELECT COUNT(*) AS cnt FROM bills_raw {where_bills}", params_bills
        ).fetchone()
        total_bills = row["cnt"] if row else None
    except Exception:
        total_bills = None

    try:
        row = db.execute(
            f"SELECT ROUND(AVG(amount), 0) AS avg_bill FROM bills_raw {where_bills}", params_bills
        ).fetchone()
        avg_bill = int(row["avg_bill"]) if row and row["avg_bill"] else None
    except Exception:
        avg_bill = None

    try:
        row = db.execute("SELECT MAX(ingested_at) AS last_run FROM bills_raw").fetchone()
        last_ingest = row["last_run"][:16] if row and row["last_run"] else "---"
    except Exception:
        last_ingest = "---"

    # ── Top products (date-filtered, from sales_hourly) ───────────────
    try:
        where_sh  = "WHERE 1=1"
        params_sh = []
        if date_from:
            where_sh += " AND date >= ?"; params_sh.append(date_from)
        if date_to:
            where_sh += " AND date <= ?"; params_sh.append(date_to)
        top_products = db.execute(f"""
            SELECT product_name,
                   ROUND(SUM(revenue_gross), 0) AS gross,
                   ROUND(SUM(quantity), 0)       AS qty
            FROM sales_hourly {where_sh}
            GROUP BY product_name
            ORDER BY gross DESC
            LIMIT 10
        """, params_sh).fetchall()
        top_products = [dict(r) for r in top_products]
    except Exception:
        top_products = []

    # ── Cash position KPIs (not date-filtered — always current) ──────
    try:
        _ensure_invoice_columns(db)
        balances    = _get_account_balances(db)
        payables    = _get_payables(db)
        receivables = _get_receivables(db)
        cash_summary = _build_summary(balances, payables, receivables)
    except Exception:
        balances = []; payables = []; receivables = []
        cash_summary = {}

    kpis = {
        "total_sales_m": total_sales_m,
        "total_bills":   total_bills,
        "avg_bill":      avg_bill,
        "last_ingest":   last_ingest,
    }

    return render_template("index.html",
        kpis=kpis,
        cash_summary=cash_summary,
        balances=balances,
        payables=payables,
        top_products=top_products,
        preset=preset,
        date_from=date_from,
        date_to=date_to,
    )


@app.route("/api/ping")
def api_ping():
    try:
        get_db().execute("SELECT 1")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/db-dump", methods=["POST"])
def api_db_dump():
    import subprocess, time
    python = config.PYTHON_EXE
    script = os.path.join(config.SCRIPTS_DIR, "db_dump.py")
    t0 = time.time()
    try:
        result = subprocess.run(
            [python, script],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
        )
        duration = round(time.time() - t0, 1)
        if result.returncode == 0:
            # Count dumped tables from output
            lines = [l for l in result.stdout.splitlines() if l.strip().startswith("OK") and "tables.csv" not in l and "schema.csv" not in l]
            return jsonify({"ok": True, "tables": len(lines), "duration": duration})
        else:
            return jsonify({"ok": False, "error": result.stderr[:200] or "Unknown error"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout (>120s)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/generate-snapshot", methods=["POST"])
def api_generate_snapshot():
    import subprocess, time
    python = config.PYTHON_EXE
    script = os.path.join(config.SCRIPTS_DIR, "generate_snapshot.py")
    t0 = time.time()
    try:
        result = subprocess.run(
            [python, script],
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
        )
        duration = round(time.time() - t0, 1)
        if result.returncode == 0:
            size_line = next((l for l in result.stdout.splitlines() if "KB" in l), "")
            return jsonify({"ok": True, "duration": duration, "size": size_line.strip()})
        else:
            return jsonify({"ok": False, "error": result.stderr[:300] or result.stdout[:300] or "Unknown error"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout (>180s)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=config.FLASK_DEBUG, port=config.FLASK_PORT)
