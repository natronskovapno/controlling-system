"""
blueprints/ingestion.py
=======================
Ingestion Control module — Garden Stage Controlling System

Supports:
  - Live script execution for POS and SEF (background subprocess)
  - Job status polling via /ingestion/status/<job_id>
  - Copyable terminal commands for Demo Bank / Second Bank (need file prompts)
  - Source status dashboard + POS ingestion log
"""

from flask import (Blueprint, render_template, request, jsonify,
                   flash, redirect, url_for)
from db import get_db
from datetime import date, timedelta
import subprocess, threading, tempfile, os, sys, uuid, time
from i18n import tr

bp = Blueprint("ingestion", __name__)

# Paths come from the central config.py at the PROJECT ROOT. This blueprint sits
# at flask_app/blueprints/, so the root is three levels up. app.py already puts
# the root on sys.path before blueprints are imported, but the explicit insert
# below keeps this module importable on its own too.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    import config
except ImportError:
    raise SystemExit(
        "\nERROR: cannot import config.py\n"
        f"  expected at: {os.path.join(_ROOT, 'config.py')}\n"
        "  The ingestion module now reads its script path from that file.\n"
        "  Copy config.py to the project root and run it once to verify:\n"
        "      python config.py\n"
    )

SCRIPTS_DIR = config.SCRIPTS_DIR
SCRIPT_NAME = "20_ingest_all.py"

# ── Job registry (in-memory, lives as long as Flask process runs) ─────
# { job_id: { "log_path": str, "running": bool, "returncode": int|None } }
_jobs = {}
_lock = threading.Lock()  # prevents concurrent runs


def _is_running():
    """True if any job is currently running."""
    return any(j["running"] for j in _jobs.values())


def _run_job(job_id, cmd_args, stdin_input=None):
    """
    Execute the ingestion script in a background thread.
    stdout + stderr are written to a temp log file.
    Uses shell=True on Windows to correctly handle spaces in paths.
    stdin_input: optional string piped to the process stdin (for interactive prompts).
    """
    log_path    = _jobs[job_id]["log_path"]
    script_path = os.path.join(SCRIPTS_DIR, SCRIPT_NAME)

    # Build shell command string — quoted paths handle spaces correctly
    args_str  = " ".join(cmd_args)
    shell_cmd = f'"{sys.executable}" "{script_path}" {args_str}'

    try:
        # Force UTF-8 output encoding — prevents UnicodeEncodeError on Windows
        # when the script prints non-cp1252 characters (e.g. ═══ separators)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"]       = "1"
        env["PYTHONUNBUFFERED"] = "1"

        with open(log_path, "w", encoding="utf-8", buffering=1) as f:
            f.write(f"[START] {shell_cmd}\n")
            f.write(f"[TIME]  {date.today().isoformat()}\n")
            f.write("-" * 60 + "\n")
            f.flush()
            stdin_pipe = subprocess.PIPE if stdin_input else None
            proc = subprocess.Popen(
                shell_cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                stdin=stdin_pipe,
                cwd=SCRIPTS_DIR,
                shell=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            if stdin_input:
                try:
                    proc.stdin.write(stdin_input)
                    proc.stdin.close()
                except Exception:
                    pass
            proc.wait()
            rc = proc.returncode
            f.write("\n" + "-" * 60 + "\n")
            f.write(f"[END] Return code: {rc}\n")
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[ERROR] Failed to start process: {e}\n")
        rc = -1

    with _lock:
        _jobs[job_id]["running"]    = False
        _jobs[job_id]["returncode"] = rc


def _start_job(cmd_args, stdin_input=None):
    """
    Register a new job, start background thread, return job_id.
    Returns None if another job is already running.
    """
    with _lock:
        if _is_running():
            return None
        job_id   = str(uuid.uuid4())[:8]
        log_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".log",
            prefix=f"ingest_{job_id}_", mode="w", encoding="utf-8"
        )
        log_file.close()
        _jobs[job_id] = {
            "log_path":   log_file.name,
            "running":    True,
            "returncode": None,
            "cmd_args":   cmd_args,
                "stdin_input": stdin_input,
            "started_at": time.time(),
        }

    t = threading.Thread(target=_run_job, args=(job_id, cmd_args, stdin_input), daemon=True)
    t.start()
    return job_id


# ── Status helpers ────────────────────────────────────────────────────

def _source_status(db):
    sources = []

    # POS — bills_raw
    try:
        r = db.execute("""
            SELECT COUNT(*) AS cnt, MAX(date) AS last_date,
                   MAX(ingested_at) AS last_ingest
            FROM bills_raw WHERE is_revoked = 0
        """).fetchone()
        sources.append({
            "name": "POS POS", "step": "pos",
            "table": "bills_raw",
            "rows": r["cnt"] or 0,
            "last_date": r["last_date"],
            "last_ingest": (r["last_ingest"] or "")[:16],
            "needs_prompt": False, "can_run": True,
        })
    except Exception:
        sources.append({"name": "POS POS", "step": "pos",
                        "rows": None, "last_date": None, "last_ingest": None,
                        "needs_prompt": False, "can_run": True})

    # SEF — invoices_in (use created_at as last run proxy)
    try:
        r = db.execute("""
            SELECT COUNT(*) AS cnt,
                   MAX(date_issued)  AS last_date,
                   MAX(created_at)   AS last_ingest
            FROM invoices_in
        """).fetchone()
        sources.append({
            "name": "SEF eFaktura", "step": "sef",
            "table": "invoices_in + invoices_out",
            "rows": r["cnt"] or 0,
            "last_date": r["last_date"],
            "last_ingest": (r["last_ingest"] or "")[:16],
            "needs_prompt": False, "can_run": True,
        })
    except Exception:
        sources.append({"name": "SEF eFaktura", "step": "sef",
                        "rows": None, "last_date": None, "last_ingest": None,
                        "needs_prompt": False, "can_run": True})

    # Demo Bank — bank_transactions (fixed: was reading wrong column)
    try:
        r = db.execute("""
            SELECT COUNT(*) AS cnt,
                   MAX(date)       AS last_date,
                   MAX(created_at) AS last_ingest
            FROM bank_transactions
        """).fetchone()
        sources.append({
            "name": "Demo Bank", "step": "banca_demo_bank",
            "table": "bank_transactions",
            "rows": r["cnt"] or 0,
            "last_date": r["last_date"],
            "last_ingest": (r["last_ingest"] or "")[:16],
            "needs_prompt": True, "can_run": False,
        })
    except Exception:
        sources.append({"name": "Demo Bank", "step": "banca_demo_bank",
                        "rows": None, "last_date": None, "last_ingest": None,
                        "needs_prompt": True, "can_run": False})

    # Second Bank — brewery_transactions
    try:
        r = db.execute("""
            SELECT COUNT(*) AS cnt,
                   MAX(date)       AS last_date,
                   MAX(created_at) AS last_ingest
            FROM brewery_transactions
        """).fetchone()
        sources.append({
            "name": "Second Bank (Small Brewery)", "step": "second_bank",
            "table": "brewery_transactions",
            "rows": r["cnt"] or 0,
            "last_date": r["last_date"],
            "last_ingest": (r["last_ingest"] or "")[:16],
            "needs_prompt": True, "can_run": False,
        })
    except Exception:
        sources.append({"name": "Second Bank (Small Brewery)", "step": "second_bank",
                        "rows": None, "last_date": None, "last_ingest": None,
                        "needs_prompt": True, "can_run": False})


    # Cloudflare Web Analytics — web_analytics_daily
    try:
        r = db.execute("""
            SELECT COUNT(*) AS cnt, MAX(date) AS last_date,
                   MAX(ingested_at) AS last_ingest
            FROM web_analytics_daily WHERE bot = 0
        """).fetchone()
        sources.append({
            "name": "Cloudflare Web Analytics", "step": "cloudflare",
            "table": "web_analytics_daily",
            "rows": r["cnt"] or 0,
            "last_date": r["last_date"],
            "last_ingest": (r["last_ingest"] or "")[:16],
            "needs_prompt": False, "can_run": True,
        })
    except Exception:
        sources.append({"name": "Cloudflare Web Analytics", "step": "cloudflare",
                        "rows": None, "last_date": None, "last_ingest": None,
                        "needs_prompt": False, "can_run": True})

    return sources


def _get_pos_log(db, limit=30):
    """Recent POS activity grouped by ingestion date — from bills_raw.
    Note: pos_ingestion_log is only populated by the old script, not the new one.
    """
    try:
        rows = db.execute("""
            SELECT
                substr(ingested_at, 1, 10)  AS run_date,
                COUNT(*)                    AS bills_count,
                SUM(CASE WHEN is_revoked=0 THEN 1 ELSE 0 END) AS bills_active,
                MIN(date)                   AS date_min,
                MAX(date)                   AS date_max,
                MAX(ingested_at)            AS run_at
            FROM bills_raw
            WHERE ingested_at IS NOT NULL
            GROUP BY substr(ingested_at, 1, 10)
            ORDER BY run_date DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_sef_log(db, limit=30):
    """Recent SEF activity — grouped by created_at date since no log table exists."""
    try:
        rows = db.execute("""
            SELECT
                substr(created_at, 1, 10)  AS run_date,
                COUNT(*)                   AS invoices_in_count,
                MIN(date_issued)           AS date_min,
                MAX(date_issued)           AS date_max,
                MAX(created_at)            AS run_at
            FROM invoices_in
            WHERE created_at IS NOT NULL
            GROUP BY substr(created_at, 1, 10)
            ORDER BY run_date DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_bank_log(db, limit=30):
    """Recent Demo Bank activity — grouped by created_at date."""
    try:
        rows = db.execute("""
            SELECT
                substr(created_at, 1, 10)  AS run_date,
                COUNT(*)                   AS tx_count,
                SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) AS total_in,
                SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END) AS total_out,
                MIN(date)                  AS date_min,
                MAX(date)                  AS date_max,
                MAX(created_at)            AS run_at
            FROM bank_transactions
            WHERE created_at IS NOT NULL
            GROUP BY substr(created_at, 1, 10)
            ORDER BY run_date DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_second_bank_log(db, limit=30):
    """Recent Second Bank activity — grouped by created_at date."""
    try:
        rows = db.execute("""
            SELECT
                substr(created_at, 1, 10)  AS run_date,
                COUNT(*)                   AS tx_count,
                SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) AS total_in,
                SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END) AS total_out,
                MIN(date)                  AS date_min,
                MAX(date)                  AS date_max,
                MAX(created_at)            AS run_at
            FROM brewery_transactions
            WHERE created_at IS NOT NULL
            GROUP BY substr(created_at, 1, 10)
            ORDER BY run_date DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_data_gaps(db):
    gaps = []
    try:
        last_bill = db.execute(
            "SELECT MAX(date) AS d FROM bills_raw WHERE is_revoked=0"
        ).fetchone()["d"]
        if last_bill:
            days_since = (date.today() - date.fromisoformat(last_bill)).days
            if days_since > 3:
                gaps.append({
                    "source": "POS",
                    "description": f"Poslednji bill: {last_bill} ({days_since} dana bez podataka)",
                    "severity": "warning" if days_since < 7 else "error",
                })
    except Exception:
        pass
    return gaps


# ── Routes ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    pos_from = request.args.get("pos_from", yesterday)
    pos_to   = request.args.get("pos_to",   yesterday)
    sef_from     = request.args.get("sef_from", "2021-01-01")
    sef_to       = request.args.get("sef_to",   today)

    sources      = _source_status(db)
    pos_log  = _get_pos_log(db)
    sef_log      = _get_sef_log(db)
    bank_log     = _get_bank_log(db)
    second_bank_log= _get_second_bank_log(db)
    gaps         = _get_data_gaps(db)
    job_running  = _is_running()
    _ensure_errors_table(db)
    errors       = _get_errors(db)
    open_count   = sum(1 for e in errors if e["status"] == "open")

    scripts_dir = SCRIPTS_DIR
    commands = {
        "all":           f'cd "{scripts_dir}" && python {SCRIPT_NAME}',
        "pos":       f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only pos',
        "pos_range": f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only pos --from {pos_from} --to {pos_to}',
        "sef":           f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only sef',
        "sef_range":     f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only sef --from {sef_from} --to {sef_to}',
        "banca_demo_bank":  f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only banca_demo_bank',
        "second_bank":     f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only second_bank',
        "sales_hourly":  f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only sales_hourly',
        "cloudflare":    f'cd "{scripts_dir}" && python {SCRIPT_NAME} --only cloudflare',
        # NOTE: Feb 20-21 gap resolved — 122 bills confirmed in DB.
        # Active issue: BillCumulative/TO/Search returning HTTP 404 from ~March 5.
        # No new POS data can be ingested until resolved with vendor support.
    }

    return render_template(
        "ingestion/index.html",
        sources=sources,
        pos_log=pos_log,
        sef_log=sef_log,
        bank_log=bank_log,
        second_bank_log=second_bank_log,
        gaps=gaps,
        commands=commands,
        pos_from=pos_from,
        pos_to=pos_to,
        sef_from=sef_from,
        sef_to=sef_to,
        today=today,
        yesterday=yesterday,
        job_running=job_running,
        errors=errors,
        open_count=open_count,
    )


@bp.route("/run", methods=["POST"])
def run():
    """Start a background ingestion job. Returns JSON {job_id} or {error}."""
    step       = request.form.get("step", "")
    date_from  = request.form.get("date_from", "")
    date_to    = request.form.get("date_to", "")
    file_path  = request.form.get("file_path", "").strip().strip('"').strip("'").strip()

    # Only allow safe steps
    allowed = {"pos", "pos_range", "sef", "sef_range", "sales_hourly",
               "banca_demo_bank", "second_bank", "cloudflare", "cloudflare_range"}
    if step not in allowed:
        return jsonify({"error": f"Step '{step}' cannot be run from Flask."}), 400

    _range_map = {"pos_range": "pos", "sef_range": "sef",
                  "cloudflare_range": "cloudflare"}
    real_step = _range_map.get(step, step)
    cmd_args = ["--only", real_step]
    if step in ("pos", "pos_range", "sef_range", "cloudflare_range") \
            and date_from and date_to:
        cmd_args += ["--from", date_from, "--to", date_to]

    # banca_demo_bank and second_bank require a file path via stdin
    stdin_input = None
    if step in ("banca_demo_bank", "second_bank"):
        if not file_path:
            return jsonify({"error": tr("Putanja do fajla je obavezna za ovaj korak.")}), 400
        stdin_input = file_path + "\n"

    job_id = _start_job(cmd_args, stdin_input=stdin_input)
    if job_id is None:
        return jsonify({"error": tr("Drugi job je već u toku. Sačekajte da završi.")}), 409

    return jsonify({"job_id": job_id})


@bp.route("/status/<job_id>")
def status(job_id):
    """Poll job status. Returns JSON {running, output, returncode}."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": tr("Job nije pronađen.")}), 404

    output = ""
    try:
        with open(job["log_path"], "r", encoding="utf-8", errors="replace") as f:
            output = f.read()
    except Exception:
        pass

    return jsonify({
        "running":    job["running"],
        "returncode": job["returncode"],
        "output":     output,
    })


# ── Ingestion errors registry ─────────────────────────────────────────

def _ensure_errors_table(db):
    """Create ingestion_errors table if it doesn't exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_errors (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date_detected TEXT NOT NULL,
            source       TEXT NOT NULL,
            description  TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open',
            resolved_at  TEXT,
            notes        TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _get_errors(db):
    rows = db.execute("""
        SELECT id, date_detected, source, description, status, resolved_at, notes
        FROM ingestion_errors
        ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, date_detected DESC
    """).fetchall()
    return [dict(r) for r in rows]


@bp.route("/errors/add", methods=["POST"])
def errors_add():
    db = get_db()
    _ensure_errors_table(db)
    source      = request.form.get("source", "").strip()
    description = request.form.get("description", "").strip()
    date_det    = request.form.get("date_detected", date.today().isoformat())
    notes       = request.form.get("notes", "").strip()

    if not source or not description:
        flash(tr("Izvor i opis su obavezni."), "error")
        return redirect(url_for("ingestion.index"))

    db.execute("""
        INSERT INTO ingestion_errors (date_detected, source, description, notes)
        VALUES (?, ?, ?, ?)
    """, [date_det, source, description, notes])
    db.commit()
    flash(tr("Greška dodata."), "success")
    return redirect(url_for("ingestion.index") + "#errors")


@bp.route("/errors/<int:error_id>/resolve", methods=["POST"])
def errors_resolve(error_id):
    db = get_db()
    _ensure_errors_table(db)
    notes = request.form.get("notes", "").strip()
    db.execute("""
        UPDATE ingestion_errors
        SET status='resolved', resolved_at=datetime('now'), notes=COALESCE(NULLIF(?, ''), notes)
        WHERE id=?
    """, [notes, error_id])
    db.commit()
    flash(tr("Označeno kao rešeno."), "success")
    return redirect(url_for("ingestion.index") + "#errors")


@bp.route("/errors/<int:error_id>/reopen", methods=["POST"])
def errors_reopen(error_id):
    db = get_db()
    _ensure_errors_table(db)
    db.execute("""
        UPDATE ingestion_errors SET status='open', resolved_at=NULL WHERE id=?
    """, [error_id])
    db.commit()
    flash(tr("Ponovo otvoreno."), "success")
    return redirect(url_for("ingestion.index") + "#errors")


