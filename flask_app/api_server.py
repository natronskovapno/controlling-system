"""
api_server.py
=============
Standalone read-only JSON API server — Garden Stage Controlling System
Runs on port 5001, separate from the main Flask app (port 5000).

Expose this port via cloudflared:
    cloudflared tunnel --url http://localhost:5001

The main dashboard on port 5000 remains localhost-only and unreachable.

ENDPOINTS:
  GET  /api/ping               — health check, no auth required
  GET  /api/tables             — list all tables with row counts
  GET  /api/schema/<table>     — column definitions for a table
  POST /api/query              — run a SELECT, returns JSON

AUTH:
  Header: X-API-Key: <API_TOKEN>
  Change API_TOKEN below to something only you know.

USAGE:
  python api_server.py
  (keep running in a separate terminal while Flask app is running)
"""

import re
import sqlite3
from flask import Flask, request, jsonify

# ── Config ─────────────────────────────────────────────────────────────────
import os

# Both are read from the environment: nothing sensitive belongs in the
# source tree. CONTROLLING_API_TOKEN must be set for the API to answer.
DB_PATH   = os.environ.get("CONTROLLING_DB", "demo.db")
API_TOKEN = os.environ.get("CONTROLLING_API_TOKEN", "")
MAX_ROWS  = 1000
PORT      = 5001
# ──────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def check_auth():
    token = request.headers.get("X-API-Key", "")
    if token != API_TOKEN:
        return jsonify({"error": "Unauthorized — missing or invalid X-API-Key"}), 401
    return None


def is_safe_sql(sql: str) -> bool:
    cleaned = sql.strip().lstrip("(")
    if not re.match(r"(?i)^\s*select\b", cleaned):
        return False
    forbidden = re.compile(
        r"(?i)\b(insert|update|delete|drop|create|alter|attach|detach"
        r"|pragma|vacuum|reindex|replace|upsert|begin|commit|rollback)\b"
    )
    return not forbidden.search(sql)


@app.route("/api/ping")
def ping():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"ok": True, "db": "connected"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tables")
def tables():
    err = check_auth()
    if err: return err

    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """).fetchall()

        result = []
        for r in rows:
            name = r["name"]
            try:
                cnt = conn.execute(f"SELECT COUNT(*) AS n FROM [{name}]").fetchone()["n"]
            except Exception:
                cnt = None
            result.append({"table": name, "rows": cnt})

        return jsonify({"tables": result, "count": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/schema/<table_name>")
def schema(table_name):
    err = check_auth()
    if err: return err

    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return jsonify({"error": "Invalid table name"}), 400

    conn = get_db()
    try:
        cols = conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
        if not cols:
            return jsonify({"error": f"Table '{table_name}' not found"}), 404

        row_count = conn.execute(f"SELECT COUNT(*) AS n FROM [{table_name}]").fetchone()["n"]

        return jsonify({
            "table": table_name,
            "rows": row_count,
            "columns": [
                {
                    "cid":     c["cid"],
                    "name":    c["name"],
                    "type":    c["type"],
                    "notnull": bool(c["notnull"]),
                    "pk":      bool(c["pk"]),
                    "default": c["dflt_value"],
                }
                for c in cols
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/query", methods=["POST"])
def query():
    err = check_auth()
    if err: return err

    body = request.get_json(silent=True)
    if not body or "sql" not in body:
        return jsonify({"error": "Body must be JSON with 'sql' key"}), 400

    sql    = body.get("sql", "").strip()
    params = body.get("params", [])
    limit  = min(int(body.get("limit", 500)), MAX_ROWS)

    if not sql:
        return jsonify({"error": "Empty SQL"}), 400
    if not is_safe_sql(sql):
        return jsonify({"error": "Only SELECT statements are permitted"}), 403

    if not re.search(r"(?i)\blimit\b", sql):
        sql = f"{sql} LIMIT {limit}"

    conn = get_db()
    try:
        cur = conn.execute(sql, params)
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
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"\n  Garden Stage API Server")
    print(f"  Running on http://localhost:{PORT}")
    print(f"  Token: {API_TOKEN}")
    print(f"  DB: {DB_PATH}\n")
    # Bind to localhost only — cloudflared tunnels to it, nothing else can reach it
    app.run(host="127.0.0.1", port=PORT, debug=False)
