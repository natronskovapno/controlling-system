"""
blueprints/web_analytics.py
===========================
Web Analytics module — Garden Stage Controlling System.

Displays the Cloudflare Web Analytics figures ingested by
scripts/12_cloudflare_analytics.py.

TWO THINGS WORTH KNOWING BEFORE READING THE QUERIES
---------------------------------------------------
1. Dates are UTC. Cloudflare's `date` dimension is UTC midnight-to-midnight
   and is stored as-is, so a visit just after local midnight in Belgrade
   belongs to the previous day here. Accepted simplification.

2. Breakdown totals will not exactly equal the daily total in every case.
   They come from separate grouped queries against a sampled API. Do not
   "correct" this by deriving one from the other — the daily figure is the
   authoritative one and the breakdowns are proportions.

Bot traffic is stored, never discarded, so `?bots=1` can include it without
needing a re-ingest.
"""

from flask import Blueprint, render_template, request
from db import get_db
from datetime import date, timedelta
from i18n import tr

bp = Blueprint("web_analytics", __name__)


# ── schema guard ──────────────────────────────────────────────────────
# Flask never runs the ingestion script's _ensure_schema(), so every
# blueprint carries its own guard. Without this the page 500s on a
# database where ingestion has not run yet.

def _ensure_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS web_analytics_daily (
            site_tag        TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            bot             INTEGER NOT NULL,
            pageviews       INTEGER NOT NULL DEFAULT 0,
            visits          INTEGER NOT NULL DEFAULT 0,
            sample_interval REAL,
            ingested_at     TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (site_tag, date, bot)
        );
        CREATE TABLE IF NOT EXISTS web_analytics_hourly (
            site_tag        TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            hour            INTEGER NOT NULL,
            bot             INTEGER NOT NULL,
            pageviews       INTEGER NOT NULL DEFAULT 0,
            visits          INTEGER NOT NULL DEFAULT 0,
            ingested_at     TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (site_tag, date, hour, bot)
        );
        CREATE TABLE IF NOT EXISTS web_analytics_breakdown (
            site_tag        TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            dim_type        TEXT    NOT NULL,
            dim_value       TEXT    NOT NULL,
            bot             INTEGER NOT NULL,
            pageviews       INTEGER NOT NULL DEFAULT 0,
            visits          INTEGER NOT NULL DEFAULT 0,
            ingested_at     TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (site_tag, date, dim_type, dim_value, bot)
        );
        CREATE TABLE IF NOT EXISTS web_analytics_cursor (
            site_tag      TEXT PRIMARY KEY,
            host          TEXT,
            last_date     TEXT,
            last_run_at   TEXT,
            last_status   TEXT,
            last_error    TEXT,
            days_ingested INTEGER DEFAULT 0
        );
    """)
    db.commit()


# Month names come from i18n so they follow the active language.

COUNTRY_SR = {
    "RS": "Srbija", "ME": "Crna Gora", "HR": "Hrvatska", "BA": "Bosna i Herc.",
    "MK": "Severna Makedonija", "SI": "Slovenija", "DE": "Nemačka",
    "AT": "Austrija", "CH": "Švajcarska", "IT": "Italija", "FR": "Francuska",
    "GB": "Velika Britanija", "US": "SAD", "NL": "Holandija", "SE": "Švedska",
    "ES": "Španija", "GR": "Grčka", "HU": "Mađarska", "BG": "Bugarska",
    "RO": "Rumunija", "TR": "Turska", "CN": "Kina", "RU": "Rusija",
    "SK": "Slovačka", "CZ": "Češka", "PL": "Poljska", "BE": "Belgija",
    "DK": "Danska", "NO": "Norveška", "PT": "Portugal", "IE": "Irska",
    "CA": "Kanada", "AU": "Australija", "IL": "Izrael", "AE": "UAE",
    "CY": "Kipar", "AL": "Albanija", "FI": "Finska", "EE": "Estonija",
    "LV": "Letonija", "LT": "Litvanija", "LU": "Luksemburg", "MT": "Malta",
    "IS": "Island", "UA": "Ukrajina", "BY": "Belorusija", "MD": "Moldavija",
    "GE": "Gruzija", "AM": "Jermenija", "AZ": "Azerbejdžan", "KZ": "Kazahstan",
    "JP": "Japan", "KR": "Južna Koreja", "IN": "Indija", "BR": "Brazil",
    "AR": "Argentina", "MX": "Meksiko", "ZA": "JAR", "EG": "Egipat",
    "MA": "Maroko", "TN": "Tunis", "NZ": "Novi Zeland", "SG": "Singapur",
    "TH": "Tajland", "XK": "Kosovo",
}


def _country_sr(code):
    return tr(COUNTRY_SR.get((code or "").upper(), code or "Nepoznato"))


def _channel(host):
    """
    Group a referrer host into a traffic channel.

    Classification is by HOST ONLY. refererPath is stored but cannot be
    joined to the host per-event — the API returns each dimension as its
    own aggregate — so Google Maps referrals cannot be separated from
    ordinary Google search here. Both land in "Google".

    Also note: an empty host means no referrer was sent. That is genuinely
    direct traffic AND Instagram bio-link taps, which strip the referrer.
    The two are not distinguishable.
    """
    h = (host or "").lower().strip()
    if not h:
        return tr("Direktan / bez izvora")
    if "instagram" in h:
        return "Instagram"
    if "facebook" in h or h.startswith("lm.") or "fb.me" in h:
        return "Facebook"
    if "google" in h:
        return "Google"
    if any(s in h for s in ("bing.", "yahoo.", "duckduckgo", "yandex", "ecosia")):
        return "Druge pretrage"
    if any(s in h for s in ("t.co", "twitter.", "x.com")):
        return "X / Twitter"
    if "tiktok" in h:
        return "TikTok"
    if "youtube" in h or "youtu.be" in h:
        return "YouTube"
    if "linkedin" in h:
        return "LinkedIn"
    if "demo-venue" in h:
        return tr("Interno (sa sajta)")
    return tr("Drugi sajtovi")


def _resolve_period(preset, date_from, date_to):
    """Return (date_from, date_to) as ISO strings for the chosen preset."""
    today = date.today()
    if preset == "7d":
        return (today - timedelta(days=7)).isoformat(), today.isoformat()
    if preset == "30d":
        return (today - timedelta(days=30)).isoformat(), today.isoformat()
    if preset == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if preset == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    if preset == "year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if preset == "all":
        return "2000-01-01", today.isoformat()
    return date_from or (today - timedelta(days=30)).isoformat(), date_to or today.isoformat()


@bp.route("/")
def index():
    db = get_db()
    _ensure_tables(db)

    preset = request.args.get("preset", "30d")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    include_bots = request.args.get("bots", "0") == "1"
    hour_mode = request.args.get("hour_mode", "avg")   # avg | total
    metric = request.args.get("metric", "visits")      # visits | pageviews

    date_from, date_to = _resolve_period(preset, date_from, date_to)

    bot_clause = "" if include_bots else " AND bot = 0"
    params = [date_from, date_to]

    # ── daily series ─────────────────────────────────────────────────
    daily = db.execute(f"""
        SELECT date,
               SUM(pageviews) AS pageviews,
               SUM(visits)    AS visits
        FROM web_analytics_daily
        WHERE date >= ? AND date <= ?{bot_clause}
        GROUP BY date
        ORDER BY date
    """, params).fetchall()
    daily = [dict(r) for r in daily]

    total_pv = sum(d["pageviews"] or 0 for d in daily)
    total_vs = sum(d["visits"] or 0 for d in daily)
    n_days = len(daily)

    peak = max(daily, key=lambda d: d["visits"] or 0) if daily else None
    max_daily = max((d[metric] or 0) for d in daily) if daily else 0
    for d in daily:
        d["pct"] = round((d[metric] or 0) / max_daily * 100, 1) if max_daily else 0
        d["label"] = d["date"][8:10] + "." + d["date"][5:7] + "."

    kpi = {
        "visits": total_vs,
        "pageviews": total_pv,
        "per_visit": round(total_pv / total_vs, 2) if total_vs else 0,
        "avg_daily": round(total_vs / n_days, 1) if n_days else 0,
        "days": n_days,
        "peak_date": peak["date"] if peak else None,
        "peak_visits": peak["visits"] if peak else 0,
    }

    # Bot volume is always computed, regardless of the toggle, so the page
    # can show what is being excluded rather than hiding it silently.
    bot_row = db.execute("""
        SELECT SUM(pageviews) AS pv, SUM(visits) AS vs
        FROM web_analytics_daily
        WHERE date >= ? AND date <= ? AND bot = 1
    """, params).fetchone()
    kpi["bot_visits"] = (bot_row["vs"] or 0) if bot_row else 0

    # ── hourly 24h cycle ─────────────────────────────────────────────
    # n_active_days counts days that actually have data, not calendar days
    # in the range — averaging over empty days would understate every bar.
    hourly_rows = db.execute(f"""
        SELECT hour,
               SUM(pageviews) AS pageviews,
               SUM(visits)    AS visits,
               COUNT(DISTINCT date) AS days
        FROM web_analytics_hourly
        WHERE date >= ? AND date <= ?{bot_clause}
        GROUP BY hour
    """, params).fetchall()

    active_days_row = db.execute(f"""
        SELECT COUNT(DISTINCT date) AS d FROM web_analytics_hourly
        WHERE date >= ? AND date <= ?{bot_clause}
    """, params).fetchone()
    active_days = (active_days_row["d"] or 0) if active_days_row else 0

    by_hour = {r["hour"]: dict(r) for r in hourly_rows}
    hourly = []
    for h in range(24):
        row = by_hour.get(h)
        total = (row[metric] or 0) if row else 0
        value = round(total / active_days, 2) if (hour_mode == "avg" and active_days) else total
        hourly.append({"hour": h, "total": total, "value": value})

    max_hour = max((h["value"] for h in hourly), default=0)
    for h in hourly:
        h["pct"] = round(h["value"] / max_hour * 100, 1) if max_hour else 0
    busiest = max(hourly, key=lambda h: h["value"]) if max_hour else None

    # ── breakdowns ───────────────────────────────────────────────────
    def _dim(dim_type, limit=None):
        rows = db.execute(f"""
            SELECT dim_value,
                   SUM(pageviews) AS pageviews,
                   SUM(visits)    AS visits
            FROM web_analytics_breakdown
            WHERE date >= ? AND date <= ? AND dim_type = ?{bot_clause}
            GROUP BY dim_value
            ORDER BY visits DESC
        """, params + [dim_type]).fetchall()
        out = [dict(r) for r in rows]
        total = sum(r["visits"] or 0 for r in out)
        for r in out:
            r["pct"] = round((r["visits"] or 0) / total * 100, 1) if total else 0
        return out[:limit] if limit else out

    countries_raw = _dim("countryName")
    countries = []
    for c in countries_raw:
        countries.append({**c, "name": _country_sr(c["dim_value"]),
                          "code": c["dim_value"]})
    rs = next((c for c in countries if (c["code"] or "").upper() == "RS"), None)
    rs_visits = rs["visits"] if rs else 0
    foreign_visits = sum(c["visits"] or 0 for c in countries
                         if (c["code"] or "").upper() != "RS")
    country_total = rs_visits + foreign_visits
    kpi["rs_pct"] = round(rs_visits / country_total * 100, 1) if country_total else 0
    kpi["rs_visits"] = rs_visits
    kpi["foreign_visits"] = foreign_visits

    # traffic channels — aggregated from refererHost
    chan_totals = {}
    for r in _dim("refererHost"):
        ch = _channel(r["dim_value"])
        slot = chan_totals.setdefault(ch, {"channel": ch, "visits": 0, "pageviews": 0, "hosts": []})
        slot["visits"] += r["visits"] or 0
        slot["pageviews"] += r["pageviews"] or 0
        if r["dim_value"]:
            slot["hosts"].append((r["dim_value"], r["visits"] or 0))
    channels = sorted(chan_totals.values(), key=lambda c: c["visits"], reverse=True)
    chan_total = sum(c["visits"] for c in channels)
    for c in channels:
        c["pct"] = round(c["visits"] / chan_total * 100, 1) if chan_total else 0
        c["hosts"] = sorted(c["hosts"], key=lambda h: h[1], reverse=True)[:6]

    # Number of rows each breakdown list shows before "Prikaži sve". These
    # lists only grow, so the default view stays readable as traffic builds.
    COLLAPSE_AT = 5

    devices = _dim("deviceType")
    browsers = _dim("userAgentBrowser", limit=40)
    systems = _dim("userAgentOS", limit=40)
    pages = _dim("requestPath", limit=60)

    # ── ingestion state ──────────────────────────────────────────────
    cursors = [dict(r) for r in db.execute("""
        SELECT host, site_tag, last_date, last_run_at, last_status, last_error
        FROM web_analytics_cursor ORDER BY host
    """).fetchall()]

    stale_days = None
    newest = db.execute("SELECT MAX(date) AS d FROM web_analytics_daily").fetchone()
    if newest and newest["d"]:
        stale_days = (date.today() - date.fromisoformat(newest["d"])).days

    return render_template(
        "web_analytics/index.html",
        kpi=kpi,
        daily=daily,
        hourly=hourly,
        busiest=busiest,
        active_days=active_days,
        countries=countries[:60],
        channels=channels,
        devices=devices,
        browsers=browsers,
        systems=systems,
        pages=pages,
        cursors=cursors,
        collapse_at=COLLAPSE_AT,
        stale_days=stale_days,
        filters=dict(preset=preset, date_from=date_from, date_to=date_to,
                     bots="1" if include_bots else "0",
                     hour_mode=hour_mode, metric=metric),
    )
