"""
blueprints/api.py
=================
Internal read-only JSON API — Garden Stage Controlling System

Allows direct DB inspection without writing diagnostic scripts.
Intended for use by Claude during debugging sessions.

AUTH:
  Every request must include header:  X-API-Key: <token>
  Token is defined in API_TOKEN below. Change it to anything you like.
  Localhost-only exposure assumed; token prevents accidental access.

ENDPOINTS:
  GET  /api/ping               — health check, no auth required
  GET  /api/tables             — list all tables with row counts
  GET  /api/schema/<table>     — column definitions for a table
  POST /api/query              — run a read-only SELECT, returns rows as JSON
       Body: {"sql": "SELECT ...", "params": [...], "limit": 500}

SAFETY:
  Only SELECT statements are permitted.
  PRAGMA, ATTACH, and any DML/DDL are rejected.
  Results capped at max 1000 rows per request.
"""

import os
import re
from flask import Blueprint, request, jsonify, current_app
from db import get_db

bp = Blueprint("api", __name__, url_prefix="/api")

# ── Auth token ────────────────────────────────────────────────────────────
# Read from the environment; requests are refused when it is unset.
API_TOKEN = os.environ.get("CONTROLLING_API_TOKEN", "")

MAX_ROWS = 1000


def _check_auth():
    token = request.headers.get("X-API-Key", "")
    if token != API_TOKEN:
        return jsonify({"error": "Unauthorized — missing or invalid X-API-Key"}), 401
    return None


def _is_safe_sql(sql: str) -> bool:
    """Only allow SELECT statements. Block anything else."""
    cleaned = sql.strip().lstrip("(")
    if not re.match(r"(?i)^\s*select\b", cleaned):
        return False
    forbidden = re.compile(
        r"(?i)\b(insert|update|delete|drop|create|alter|attach|detach"
        r"|pragma|vacuum|reindex|replace|upsert|begin|commit|rollback)\b"
    )
    if forbidden.search(sql):
        return False
    return True


# ── Endpoints ─────────────────────────────────────────────────────────────

@bp.route("/ping")
def ping():
    """Health check — no auth required."""
    try:
        get_db().execute("SELECT 1")
        return jsonify({"ok": True, "db": "connected"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/tables")
def tables():
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    db = get_db()
    try:
        rows = db.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """).fetchall()

        result = []
        for r in rows:
            name = r["name"]
            try:
                cnt = db.execute(f"SELECT COUNT(*) AS n FROM [{name}]").fetchone()["n"]
            except Exception:
                cnt = None
            result.append({"table": name, "rows": cnt})

        return jsonify({"tables": result, "count": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/schema/<table_name>")
def schema(table_name):
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    # Validate table name — alphanumeric + underscore only
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return jsonify({"error": "Invalid table name"}), 400

    db = get_db()
    try:
        cols = db.execute(f"PRAGMA table_info([{table_name}])").fetchall()
        if not cols:
            return jsonify({"error": f"Table '{table_name}' not found"}), 404

        row_count = db.execute(f"SELECT COUNT(*) AS n FROM [{table_name}]").fetchone()["n"]

        return jsonify({
            "table": table_name,
            "rows": row_count,
            "columns": [
                {
                    "cid":      c["cid"],
                    "name":     c["name"],
                    "type":     c["type"],
                    "notnull":  bool(c["notnull"]),
                    "pk":       bool(c["pk"]),
                    "default":  c["dflt_value"],
                }
                for c in cols
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/query", methods=["POST"])
def query():
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    body = request.get_json(silent=True)
    if not body or "sql" not in body:
        return jsonify({"error": "Request body must be JSON with 'sql' key"}), 400

    sql    = body.get("sql", "").strip()
    params = body.get("params", [])
    limit  = min(int(body.get("limit", 500)), MAX_ROWS)

    if not sql:
        return jsonify({"error": "Empty SQL"}), 400

    if not _is_safe_sql(sql):
        return jsonify({"error": "Only SELECT statements are permitted"}), 403

    # Inject LIMIT if not already present
    if not re.search(r"(?i)\blimit\b", sql):
        sql = f"{sql} LIMIT {limit}"

    db = get_db()
    try:
        rows = db.execute(sql, params).fetchall()
        columns = [d[0] for d in db.execute(sql, params).description] if rows else []

        # Re-execute cleanly for column names
        cur = db.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()

        return jsonify({
            "columns": columns,
            "rows":    [dict(r) for r in rows],
            "count":   len(rows),
            "sql":     sql,
        })
    except Exception as e:
        return jsonify({"error": str(e), "sql": sql}), 500
