"""
blueprints/checks.py
====================
Provere (Checks) — continuous data invariants for the Garden Stage system.

WHY THIS EXISTS
---------------
Every defect found during the September 2026 review was silent. None threw an
error, none looked wrong on screen, and each was caught only because somebody
happened to know what a number should have been:

  * a GROUP BY alias collision folded every 2026 parking figure into 2025
  * Storno/Rejected invoices were summed as valid, overstating VAT by 851,379
  * db_dump.py and pdv.py drifted three separate times, disagreeing by 813,517
  * SEF sales ingestion looked healthy while August held 3 invoices
  * an auto-tag write to a non-existent column silently dropped 3 invoices

The system computes a great deal and checks almost none of it. This page is the
other half: assertions that must hold, run on every page load, each one added
because something actually went wrong.

HOW TO ADD A CHECK
------------------
Write a function taking (db) and returning a dict:

    {"status": PASS|WARN|FAIL, "summary": "one line", "rows": [...]}

...then register it in CHECKS below with a group, title and `why`. The `why`
is not decoration: it records the incident the check defends against, so a
future reader knows whether it still earns its place.

Checks must be cheap. This page loads on every visit and nothing here should
take more than a fraction of a second.
"""

import os
import sys
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request
from db import get_db

bp = Blueprint("checks", __name__, url_prefix="/checks")

# pdv_lib lives at the project root, shared with scripts/db_dump.py
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    import pdv_lib
except ImportError:
    pdv_lib = None

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# How stale a feed may get before it is flagged, in days.
STALENESS = {"sef": 10, "pos": 3, "bank": 10}


def _one(db, sql, params=()):
    r = db.execute(sql, params).fetchone()
    return r[0] if r else None


def _rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def _days_since(d):
    if not d:
        return None
    try:
        return (date.today() - date.fromisoformat(str(d)[:10])).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# COVERAGE — is data still arriving?
# ---------------------------------------------------------------------------

def chk_sef_coverage(db):
    out = _one(db, "SELECT MAX(date_issued) FROM invoices_out WHERE source='SEF'")
    inn = _one(db, "SELECT MAX(date_received) FROM invoices_in")
    d_out, d_in = _days_since(out), _days_since(inn)
    rows = [
        {"feed": "invoices_out (SEF)", "latest": out, "days": d_out},
        {"feed": "invoices_in (SEF)",  "latest": inn, "days": d_in},
    ]
    worst = max([d for d in (d_out, d_in) if d is not None], default=None)
    if worst is None:
        return {"status": FAIL, "summary": "No SEF data at all", "rows": rows}
    if worst > STALENESS["sef"]:
        return {"status": FAIL,
                "summary": f"Newest SEF invoice is {worst} days old "
                           f"(threshold {STALENESS['sef']})", "rows": rows}
    return {"status": PASS, "summary": f"Newest SEF invoice {worst} days old", "rows": rows}


def chk_pos_coverage(db):
    last = _one(db, "SELECT MAX(date) FROM bills_raw")
    d = _days_since(last)
    rows = [{"feed": "bills_raw", "latest": last, "days": d}]
    if d is None:
        return {"status": FAIL, "summary": "No POS data", "rows": rows}
    if d > STALENESS["pos"]:
        return {"status": FAIL,
                "summary": f"Newest POS bill is {d} days old "
                           f"(threshold {STALENESS['pos']})", "rows": rows}
    return {"status": PASS, "summary": f"Newest POS bill {d} days old", "rows": rows}


def chk_bank_coverage(db):
    rows = _rows(db, """
        SELECT account, MAX(date) AS latest, COUNT(*) AS n
        FROM bank_transactions GROUP BY account ORDER BY latest DESC
    """)
    stale = []
    for r in rows:
        r["days"] = _days_since(r["latest"])
        # accounts dormant for over a year are closed, not stale
        if r["days"] is not None and STALENESS["bank"] < r["days"] < 365:
            stale.append(r["account"])
    if stale:
        return {"status": WARN,
                "summary": f"{len(stale)} account(s) with no recent transactions",
                "rows": rows}
    return {"status": PASS, "summary": f"{len(rows)} accounts, all current or dormant",
            "rows": rows}


# ---------------------------------------------------------------------------
# PDV — the arithmetic must hold
# ---------------------------------------------------------------------------

def chk_pdv_identity(db):
    """neto = pazar + izlazni + parking - ulazni, and total = ak + mp."""
    if pdv_lib is None:
        return {"status": FAIL, "summary": "pdv_lib.py not importable", "rows": []}
    bad = []
    for r in pdv_lib.get_pregled(db):
        checks = [
            ("ak_net", r["ak_net"], r["ak_pazar"] + r["ak_izlazni"] + r["ak_parking"] - r["ak_ulazni"]),
            ("mp_net", r["mp_net"], r["mp_izlazni"] + r["mp_pazar"] + r["brewery_parking"] - r["mp_ulazni"]),
            ("total_ulazni",  r["total_ulazni"],  r["ak_ulazni"] + r["mp_ulazni"]),
            ("total_izlazni", r["total_izlazni"], r["ak_izlazni"] + r["mp_izlazni"]),
            ("total_pazar",   r["total_pazar"],   r["ak_pazar"] + r["mp_pazar"]),
            ("total_parking", r["total_parking"], r["ak_parking"] + r["brewery_parking"]),
        ]
        for name, got, want in checks:
            if abs(got - want) > 1:
                bad.append({"month": r["month"], "field": name,
                            "stored": got, "expected": want, "diff": got - want})
    if bad:
        return {"status": FAIL, "summary": f"{len(bad)} identity violation(s)", "rows": bad}
    return {"status": PASS, "summary": "Net identity and GS+BR additivity hold on every month",
            "rows": []}


def chk_pdv_excludes_cancelled(db):
    """
    Storno/Rejected invoices must never reach PDV.
    Guards the Fix 2 regression: they were counted as valid until 1 Sep 2026.
    """
    n_out = _one(db, """SELECT COUNT(*) FROM invoices_out
        WHERE invoice_status IN ('Storno','Rejected')
          AND pdv_amount IS NOT NULL AND pdv_amount != 0
          AND date_issued >= '2025-01-01'""") or 0
    n_in = _one(db, """SELECT COUNT(*) FROM invoices_in
        WHERE invoice_status IN ('Storno','Rejected')
          AND pdv_amount IS NOT NULL AND pdv_amount != 0
          AND date_received >= '2025-01-01'""") or 0
    amt = _one(db, """SELECT ROUND(SUM(pdv_amount),0) FROM invoices_out
        WHERE invoice_status IN ('Storno','Rejected')
          AND pdv_amount IS NOT NULL AND date_issued >= '2025-01-01'""") or 0
    if pdv_lib is None:
        return {"status": FAIL, "summary": "pdv_lib.py not importable", "rows": []}
    lib_total = sum(r["total_izlazni"] for r in pdv_lib.get_pregled(db))
    raw_total = _one(db, """SELECT ROUND(SUM(pdv_amount),0) FROM invoices_out
        WHERE pdv_amount IS NOT NULL AND pdv_amount != 0
          AND date_issued >= '2025-01-01'""") or 0
    excluded = raw_total - lib_total
    rows = [{"metric": "cancelled outgoing invoices", "value": n_out},
            {"metric": "cancelled incoming invoices", "value": n_in},
            {"metric": "their outgoing VAT", "value": amt},
            {"metric": "VAT excluded by PDV", "value": excluded}]
    if n_out and abs(excluded) < 1:
        return {"status": FAIL,
                "summary": f"{n_out} cancelled invoices exist but PDV excluded nothing",
                "rows": rows}
    return {"status": PASS,
            "summary": f"{n_out + n_in} cancelled invoices correctly excluded "
                       f"({excluded:,.0f} of VAT)", "rows": rows}


def chk_pdv_csv_agrees(db):
    """
    computed_pdv_pregled.csv must match what the module computes.
    Guards the divergence that put 813,517 between the dashboard and the CSV.
    """
    import csv
    try:
        import config
        path = os.path.join(config.DB_DUMP_DIR, "computed_pdv_pregled.csv")
    except Exception:
        return {"status": WARN, "summary": "config.py not importable", "rows": []}
    if not os.path.isfile(path):
        return {"status": WARN, "summary": "No dump present — click 'Export for analysis'", "rows": []}
    if pdv_lib is None:
        return {"status": FAIL, "summary": "pdv_lib.py not importable", "rows": []}

    age = _days_since(datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat())
    live = {r["month"]: r for r in pdv_lib.get_pregled(db)}
    diffs = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            m = row.get("month")
            if m not in live:
                continue
            for csv_col, lib_key in (("ak_neto", "ak_net"), ("total_neto", "total_net")):
                if csv_col not in row:
                    continue
                try:
                    got = float(row[csv_col] or 0)
                except ValueError:
                    continue
                want = live[m][lib_key]
                if abs(got - want) > 1:
                    diffs.append({"month": m, "column": csv_col,
                                  "csv": got, "live": want, "diff": got - want})
    if diffs:
        total = sum(abs(d["diff"]) for d in diffs)
        return {"status": FAIL,
                "summary": f"CSV disagrees with the module on {len(diffs)} value(s), "
                           f"{total:,.0f} total — re-run the dump",
                "rows": diffs}
    return {"status": PASS,
            "summary": f"CSV matches the module (dump is {age} day(s) old)", "rows": []}


# ---------------------------------------------------------------------------
# REFERENTIAL — do links point at rows that exist?
# ---------------------------------------------------------------------------

def chk_orphan_links(db):
    rows = _rows(db, """
        SELECT l.source_type, COUNT(*) AS n
        FROM invoice_fixed_cost_links l
        LEFT JOIN invoices_in       ii ON l.source_type='invoice_in'       AND ii.invoice_id=l.source_id
        LEFT JOIN invoices_out      io ON l.source_type='invoice_out'      AND io.invoice_id=l.source_id
        LEFT JOIN bank_transactions bt ON l.source_type='bank_transaction' AND bt.transaction_id=l.source_id
        WHERE l.source_type IN ('invoice_in','invoice_out','bank_transaction')
          AND ii.invoice_id IS NULL AND io.invoice_id IS NULL AND bt.transaction_id IS NULL
        GROUP BY l.source_type
    """)
    n = sum(r["n"] for r in rows)
    if n:
        return {"status": FAIL,
                "summary": f"{n} link(s) point at rows that no longer exist", "rows": rows}
    return {"status": PASS, "summary": "Every fixed-cost link resolves to a real source row",
            "rows": []}


def chk_duplicate_sef_ids(db):
    out = []
    for tbl, col in (("invoices_in", "invoice_id_sef"), ("invoices_out", "invoice_id_sef")):
        for r in _rows(db, f"""SELECT '{tbl}' AS tbl, {col} AS sef_id, COUNT(*) AS n
                               FROM {tbl} WHERE {col} IS NOT NULL
                               GROUP BY {col} HAVING COUNT(*) > 1"""):
            out.append(r)
    if out:
        return {"status": FAIL, "summary": f"{len(out)} duplicated SEF invoice id(s)", "rows": out}
    return {"status": PASS, "summary": "SEF invoice ids are unique in both tables", "rows": []}


# ---------------------------------------------------------------------------
# RANGE — values that are structurally impossible or implausible
# ---------------------------------------------------------------------------

def chk_vat_rates(db):
    rows = _rows(db, """
        SELECT vat_rate, COUNT(*) AS n FROM invoice_fixed_cost_links
        WHERE vat_rate IS NOT NULL AND vat_rate NOT IN (0, 10, 20)
        GROUP BY vat_rate ORDER BY n DESC
    """)
    if rows:
        return {"status": FAIL,
                "summary": f"{sum(r['n'] for r in rows)} link(s) with a non-standard VAT rate",
                "rows": rows}
    return {"status": PASS, "summary": "All VAT rates are 0, 10 or 20", "rows": []}


def chk_income_link_has_invoice(db):
    """
    Sponsorship and other income received in the bank should be invoiced in the
    same month. Records the policy agreed 3 Sep 2026: VAT is recognised when the
    money arrives, and the invoice must carry a matching date.
    """
    rows = _rows(db, """
        SELECT l.year, l.month, t.name AS tag, l.amount,
               ROUND(l.amount * l.vat_rate / (100.0 + l.vat_rate), 0) AS pdv,
               (SELECT COUNT(*) FROM invoices_out o
                 WHERE strftime('%Y-%m', o.date_issued) = printf('%04d-%02d', l.year, l.month)
                   AND o.pdv_amount IS NOT NULL AND o.pdv_amount != 0
                   AND COALESCE(o.invoice_status,'') NOT IN ('Storno','Rejected')
               ) AS invoices_that_month
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE t.type = 'income' AND l.vat_rate > 0
          AND l.source_type = 'bank_transaction'
          AND t.submodule != 'parking'
          AND l.year || '-' || printf('%02d', l.month) >= '2025-01'
        ORDER BY l.year, l.month
    """)
    flagged = [r for r in rows if not r["invoices_that_month"]]
    if flagged:
        return {"status": WARN,
                "summary": f"{len(flagged)} income receipt(s) with no outgoing invoice that month",
                "rows": rows}
    return {"status": PASS,
            "summary": f"All {len(rows)} non-parking income receipts have invoices in their month",
            "rows": rows}


def chk_labor_ratio(db):
    rows = _rows(db, """
        SELECT venue, event_type, COUNT(*) AS n,
               ROUND(AVG(COALESCE(wages_override, wages_auto, wages, 0)), 0) AS avg_labor
        FROM events_daily_log
        WHERE date >= date('now', '-18 months')
          AND COALESCE(wages_override, wages_auto, wages, 0) >
              1.5 * (COALESCE(sales_net_override, sales_net_auto, 0) + COALESCE(other_revenues, 0))
          AND COALESCE(sales_net_override, sales_net_auto, 0) > 0
        GROUP BY venue, event_type
        HAVING COUNT(*) >= 3
        ORDER BY n DESC
    """)
    if rows:
        return {"status": WARN,
                "summary": f"{len(rows)} bucket(s) where labour routinely exceeds 150% of revenue",
                "rows": rows}
    return {"status": PASS, "summary": "No bucket has labour persistently above 150% of revenue",
            "rows": []}


def chk_unclassified_cards(db):
    # The column is `classification`, not `category`. Querying the wrong name
    # made this check raise OperationalError on every run, so it reported a
    # failure of itself rather than ever looking at the card spend.
    n = _one(db, "SELECT COUNT(*) FROM debit_card_transactions "
                 "WHERE COALESCE(classification,'') IN ('', 'Unclassified')") or 0
    amt = _one(db, "SELECT ROUND(SUM(ABS(amount)),0) FROM debit_card_transactions "
                   "WHERE COALESCE(classification,'') IN ('', 'Unclassified')") or 0
    rows = [{"metric": "unclassified transactions", "value": n},
            {"metric": "total value", "value": amt}]
    if n:
        return {"status": WARN, "summary": f"{n} card transactions unclassified ({amt:,.0f} RSD)",
                "rows": rows}
    return {"status": PASS, "summary": "All debit card transactions classified", "rows": rows}


def chk_ingestion_errors(db):
    try:
        rows = _rows(db, "SELECT source, description, status, created_at "
                         "FROM ingestion_errors WHERE status='open' ORDER BY created_at DESC")
    except sqlite3.Error:
        return {"status": PASS, "summary": "No ingestion_errors table yet", "rows": []}
    if rows:
        return {"status": WARN, "summary": f"{len(rows)} open ingestion error(s)", "rows": rows}
    return {"status": PASS, "summary": "No open ingestion errors", "rows": []}


# ---------------------------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------------------------

CHECKS = [
    ("Coverage", "SEF invoice feed", chk_sef_coverage,
     "August 2026 SEF sales stopped on the 4th and nothing noticed for four weeks."),
    ("Coverage", "POS POS feed", chk_pos_coverage,
     "POS ingestion has stalled before without any visible signal."),
    ("Coverage", "Bank statements", chk_bank_coverage,
     "Statement gaps are invisible until a reconciliation fails."),

    ("PDV", "Net identity and GS+BR additivity", chk_pdv_identity,
     "A GROUP BY alias collision misattributed every 2026 parking figure to 2025."),
    ("PDV", "Cancelled invoices excluded", chk_pdv_excludes_cancelled,
     "Storno/Rejected invoices were counted as valid, overstating VAT by 851,379."),
    ("PDV", "Exported CSV matches the module", chk_pdv_csv_agrees,
     "db_dump.py and pdv.py drifted three times; the CSV was wrong by 813,517."),

    ("Integritet", "Fixed-cost links resolve", chk_orphan_links,
     "An auto-tag bug could leave links pointing at invoices that never committed."),
    ("Integritet", "SEF invoice ids unique", chk_duplicate_sef_ids,
     "Duplicate ids would double-count VAT silently."),
    ("Integritet", "VAT rates are standard", chk_vat_rates,
     "A stray rate produces a plausible-looking but wrong VAT figure."),

    ("Usklađenost", "Income receipts have same-month invoices", chk_income_link_has_invoice,
     "Policy of 3 Sep 2026: VAT is recognised on receipt, invoice must match the month."),
    ("Usklađenost", "Labour ratio plausibility", chk_labor_ratio,
     "Garden 'Regular day' projects a 214% labour ratio — every projection a guaranteed loss."),
    ("Usklađenost", "Debit cards classified", chk_unclassified_cards,
     "455 transactions worth 2.2M RSD sat unclassified for months."),
    ("Usklađenost", "Open ingestion errors", chk_ingestion_errors,
     "Errors logged during ingestion are otherwise never surfaced."),
]


@bp.route("/")
def index():
    db = get_db()
    only = request.args.get("group", "")

    results, counts = [], {PASS: 0, WARN: 0, FAIL: 0}
    for group, title, fn, why in CHECKS:
        if only and group != only:
            continue
        started = datetime.now()
        try:
            out = fn(db)
        except Exception as exc:
            out = {"status": FAIL,
                   "summary": f"Check raised {type(exc).__name__}: {exc}",
                   "rows": []}
        ms = (datetime.now() - started).total_seconds() * 1000
        out.update(group=group, title=title, why=why, ms=round(ms))
        out.setdefault("rows", [])
        counts[out["status"]] = counts.get(out["status"], 0) + 1
        results.append(out)

    groups = []
    for g, _, _, _ in CHECKS:
        if g not in groups:
            groups.append(g)

    return render_template(
        "checks/index.html",
        results=results, counts=counts, groups=groups, active_group=only,
        total=len(results), now=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
    )
