"""
blueprints/brewery.py
=========================
Small Brewery / Brewery module — Garden Stage Controlling System

Tabs:
  - Transakcije: bank movements from brewery_transactions,
                 aggregated by counterparty+direction (pivot view) + monthly breakdown
  - Fakture:     invoices from main invoices_in/out tables filtered by entity='brewery'

INTERNAL TRANSFER DETECTION:
  purposecode=290 and counterparty containing 'Garden Stage HQ' are
  internal capital movements — excluded from inflow/outflow totals and shown
  separately in a dedicated KPI tile.

COUNTERPARTY NORMALISATION:
  Inflows (_CP_NORM_IN):
    Second Bank garbles ART GARDEN STAGE GARDEN settlements — normalised to
    'Garden Stage — prodaja'. WOLT DOO → 'Wolt — dostava'.

  Outflows (_CP_NORM_OUT):
    MUSIC_ROYALTIES_A, OFPS, EBAR Software duplicates collapsed. GWS banking fees merged.
    Odrzavanje racuna (monthly account maintenance) merged.

EXPANDABLE AGGREGATION:
  Aggregated view now returns detail rows grouped under each normalised
  counterparty — passed to template as tx_inflows_detail / tx_outflows_detail
  dicts keyed by normalised counterparty name.

FIXES (2026-05-20 v2):
  - Outflow normalisation (_CP_NORM_OUT) added
  - _CP_NORM_IN catch-all tightened (removed GARDEN%STAGE% which matched
    omladinska zadruga salary payments)
  - Aggregated view expandable: click row to reveal individual transactions
  - invoices_in date filter uses date_received
  - r.outstanding bug fixed in invoices_out template
"""

from flask import Blueprint, render_template, request
from db import get_db

bp = Blueprint("brewery", __name__)

# ── Internal transfer exclusion ───────────────────────────────────────
_EXCL_INTERNAL = """
    AND NOT (
        COALESCE(purposecode, '') = '290'
        OR UPPER(COALESCE(counterparty, '')) LIKE '%CENTRALA%'
    )
"""

# ── Inflow counterparty normalisation ─────────────────────────────────
# Second Bank garbles ART GARDEN STAGE GARDEN card settlement batches.
# Catch-all removed — only explicit patterns used to avoid false positives.
_CP_NORM_IN = """
    CASE
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%WOLT%'
        THEN 'Wolt — dostava'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%GARDEN STAGE%'
         AND (   UPPER(COALESCE(counterparty, purpose, '')) LIKE '%GARDEN%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%GARDEN%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%Brewery%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%BEOGRAD%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%30030776000%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%BOOKING%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%+IZV%'
              OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%SECOND BANK%')
        THEN 'Garden Stage — prodaja'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%Brewery%'
        THEN 'Garden Stage — prodaja'
        ELSE COALESCE(counterparty, purpose, '—')
    END
""".strip()

# ── Outflow counterparty normalisation ────────────────────────────────
_CP_NORM_OUT = """
    CASE
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%MUSIC_ROYALTIES_A%'
        THEN 'MUSIC_ROYALTIES_A'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%OFPS%'
        THEN 'OFPS Srbije'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%EBAR%'
        THEN 'EBAR Software doo'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%GWS%'
          OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%INTERNI NALOG%'
          OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%MOB SME KLIR%'
        THEN 'GWS — bankarske naknade'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%ODRZAVANJE%'
          OR UPPER(COALESCE(counterparty, purpose, '')) LIKE '%ODRŽAVANJE%'
        THEN 'Održavanje računa'
        WHEN UPPER(COALESCE(counterparty, purpose, '')) LIKE '%POS NAKNADA%'
        THEN 'POS naknada'
        ELSE COALESCE(counterparty, purpose, '—')
    END
""".strip()


# ── helpers ───────────────────────────────────────────────────────────

def _date_where(alias, date_from, date_to):
    where, params = "", []
    col = f"{alias}.date" if alias else "date"
    if date_from:
        where += f" AND {col} >= ?"
        params.append(date_from)
    if date_to:
        where += f" AND {col} <= ?"
        params.append(date_to)
    return where, params


def _cp_norm_expr(direction):
    return _CP_NORM_IN if direction == "credit" else _CP_NORM_OUT


# ── Transactions ──────────────────────────────────────────────────────

def _get_tx_kpis(db, date_from, date_to):
    dw, dp = _date_where("", date_from, date_to)
    row = db.execute(f"""
        SELECT
            COUNT(*)                                                          AS tx_count,
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) AS total_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) AS total_out,
            MIN(date) AS date_min,
            MAX(date) AS date_max
        FROM brewery_transactions
        WHERE 1=1 {dw} {_EXCL_INTERNAL}
    """, dp).fetchone()

    int_row = db.execute(f"""
        SELECT
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) AS int_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) AS int_out
        FROM brewery_transactions
        WHERE 1=1 {dw}
          AND (
            COALESCE(purposecode, '') = '290'
            OR UPPER(COALESCE(counterparty, '')) LIKE '%CENTRALA%'
          )
    """, dp).fetchone()

    result = dict(row) if row else {}
    if int_row:
        result['int_in']  = int_row['int_in']  or 0
        result['int_out'] = int_row['int_out'] or 0
    return result


def _get_tx_aggregated(db, direction, date_from, date_to):
    """Aggregated view + detail rows for expand/collapse."""
    dw, dp = _date_where("", date_from, date_to)
    norm = _cp_norm_expr(direction)

    # Aggregated totals
    agg_rows = db.execute(f"""
        SELECT
            ({norm})              AS counterparty,
            COUNT(*)              AS tx_count,
            ROUND(SUM(amount), 0) AS total_amount,
            MIN(date)             AS first_date,
            MAX(date)             AS last_date
        FROM brewery_transactions
        WHERE direction = ? {dw} {_EXCL_INTERNAL}
        GROUP BY ({norm})
        ORDER BY total_amount DESC
    """, [direction] + dp).fetchall()

    # Detail rows for expand — all individual transactions grouped by normalised name
    detail_rows = db.execute(f"""
        SELECT
            ({norm})   AS norm_cp,
            date,
            counterparty AS raw_cp,
            amount,
            purpose,
            reference
        FROM brewery_transactions
        WHERE direction = ? {dw} {_EXCL_INTERNAL}
        ORDER BY ({norm}), date DESC, transaction_id DESC
    """, [direction] + dp).fetchall()

    # Group detail rows by normalised counterparty
    from collections import defaultdict
    detail_map = defaultdict(list)
    for r in detail_rows:
        detail_map[r['norm_cp']].append(dict(r))

    return [dict(r) for r in agg_rows], dict(detail_map)


def _get_tx_detailed(db, direction, date_from, date_to):
    dw, dp = _date_where("", date_from, date_to)
    norm = _cp_norm_expr(direction)
    rows = db.execute(f"""
        SELECT date,
               ({norm}) AS counterparty,
               amount, purpose, reference, account, currency, purposecode
        FROM brewery_transactions
        WHERE direction = ? {dw} {_EXCL_INTERNAL}
        ORDER BY date DESC, transaction_id DESC
    """, [direction] + dp).fetchall()
    return [dict(r) for r in rows]


def _get_tx_monthly(db, date_from, date_to):
    dw, dp = _date_where("", date_from, date_to)
    rows = db.execute(f"""
        SELECT
            SUBSTR(date, 1, 7)                                                AS month,
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) AS total_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) AS total_out,
            COUNT(CASE WHEN direction='credit' THEN 1 END)                    AS cnt_in,
            COUNT(CASE WHEN direction='debit'  THEN 1 END)                    AS cnt_out
        FROM brewery_transactions
        WHERE 1=1 {dw} {_EXCL_INTERNAL}
        GROUP BY SUBSTR(date, 1, 7)
        ORDER BY month DESC
    """, dp).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['net'] = (d['total_in'] or 0) - (d['total_out'] or 0)
        result.append(d)
    return result


# ── Invoices ──────────────────────────────────────────────────────────

def _get_invoice_kpis(db, date_from, date_to):
    dp_in  = ([date_from] if date_from else []) + ([date_to] if date_to else [])
    dp_out = ([date_from] if date_from else []) + ([date_to] if date_to else [])
    dw_in  = (" AND date_received >= ?" if date_from else "") + \
             (" AND date_received <= ?" if date_to   else "")
    dw_out = (" AND date_issued >= ?" if date_from else "") + \
             (" AND date_issued <= ?" if date_to   else "")

    row_in = db.execute(f"""
        SELECT COUNT(*) AS cnt,
               ROUND(SUM(net_amount), 0) AS total_net,
               ROUND(SUM(COALESCE(pdv_amount, 0)), 0) AS total_pdv,
               ROUND(SUM(net_amount + COALESCE(pdv_amount, 0)), 0) AS total_gross,
               ROUND(SUM(CASE WHEN payment_status IN ('Unpaid','Partial')
                         THEN net_amount + COALESCE(pdv_amount, 0) ELSE 0 END),0) AS outstanding
        FROM invoices_in WHERE entity = 'brewery' {dw_in}
    """, dp_in).fetchone()

    row_out = db.execute(f"""
        SELECT COUNT(*) AS cnt,
               ROUND(SUM(net_amount), 0) AS total_net,
               ROUND(SUM(COALESCE(pdv_amount, 0)), 0) AS total_pdv,
               ROUND(SUM(net_amount + COALESCE(pdv_amount, 0)), 0) AS total_gross,
               ROUND(SUM(CASE WHEN payment_status IN ('Unpaid','Partial')
                         THEN net_amount + COALESCE(pdv_amount, 0) ELSE 0 END),0) AS outstanding
        FROM invoices_out WHERE entity = 'brewery' {dw_out}
    """, dp_out).fetchone()

    return dict(row_in) if row_in else {}, dict(row_out) if row_out else {}


def _get_invoices_aggregated(db, direction, date_from, date_to):
    dp = ([date_from] if date_from else []) + ([date_to] if date_to else [])
    if direction == "in":
        table, name_col, date_col = "invoices_in",  "supplier_name", "date_received"
    else:
        table, name_col, date_col = "invoices_out", "client_name",   "date_issued"
    dw = (f" AND {date_col} >= ?" if date_from else "") + \
         (f" AND {date_col} <= ?" if date_to   else "")
    rows = db.execute(f"""
        SELECT COALESCE({name_col}, '—') AS counterparty,
               COUNT(*) AS invoice_count,
               ROUND(SUM(net_amount), 0) AS total_net,
               ROUND(SUM(COALESCE(pdv_amount, 0)), 0) AS total_pdv,
               ROUND(SUM(net_amount + COALESCE(pdv_amount, 0)), 0) AS total_gross,
               ROUND(SUM(CASE WHEN payment_status IN ('Unpaid','Partial')
                         THEN net_amount + COALESCE(pdv_amount, 0) ELSE 0 END),0) AS outstanding,
               MIN({date_col}) AS first_date, MAX({date_col}) AS last_date
        FROM {table}
        WHERE entity = 'brewery' {dw}
        GROUP BY COALESCE({name_col}, '—')
        ORDER BY total_gross DESC
    """, dp).fetchall()
    return [dict(r) for r in rows]


def _get_invoices_detailed(db, direction, date_from, date_to):
    dp = ([date_from] if date_from else []) + ([date_to] if date_to else [])
    if direction == "in":
        table, name_col, date_col = "invoices_in",  "supplier_name", "date_received"
    else:
        table, name_col, date_col = "invoices_out", "client_name",   "date_issued"
    dw = (f" AND {date_col} >= ?" if date_from else "") + \
         (f" AND {date_col} <= ?" if date_to   else "")
    rows = db.execute(f"""
        SELECT invoice_id, {date_col} AS date_doc, due_date,
               COALESCE({name_col}, '—') AS counterparty,
               invoice_number_sef,
               ROUND(net_amount, 0) AS net_amount,
               ROUND(COALESCE(pdv_amount, 0), 0) AS pdv_amount,
               pdv_rate, payment_status, notes
        FROM {table}
        WHERE entity = 'brewery' {dw}
        ORDER BY {date_col} DESC
    """, dp).fetchall()
    return [dict(r) for r in rows]


# ── routes ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()

    tab       = request.args.get("tab", "transakcije")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    tx_view   = request.args.get("tx_view", "aggregated")
    inv_view  = request.args.get("inv_view", "aggregated")

    tx_kpis         = _get_tx_kpis(db, date_from, date_to)
    kpi_in, kpi_out = _get_invoice_kpis(db, date_from, date_to)

    tx_inflows_detail = {}
    tx_outflows_detail = {}

    if tx_view == "detailed":
        tx_inflows  = _get_tx_detailed(db, "credit", date_from, date_to)
        tx_outflows = _get_tx_detailed(db, "debit",  date_from, date_to)
    elif tx_view == "monthly":
        tx_inflows  = []
        tx_outflows = []
    else:
        tx_inflows,  tx_inflows_detail  = _get_tx_aggregated(db, "credit", date_from, date_to)
        tx_outflows, tx_outflows_detail = _get_tx_aggregated(db, "debit",  date_from, date_to)

    tx_monthly = _get_tx_monthly(db, date_from, date_to) if tx_view == "monthly" else []

    tx_inflow_total  = sum(r["total_amount"] for r in tx_inflows)  if tx_view == "aggregated" else sum(r.get("amount", 0) for r in tx_inflows)
    tx_outflow_total = sum(r["total_amount"] for r in tx_outflows) if tx_view == "aggregated" else sum(r.get("amount", 0) for r in tx_outflows)

    if inv_view == "detailed":
        inv_in  = _get_invoices_detailed(db, "in",  date_from, date_to)
        inv_out = _get_invoices_detailed(db, "out", date_from, date_to)
    else:
        inv_in  = _get_invoices_aggregated(db, "in",  date_from, date_to)
        inv_out = _get_invoices_aggregated(db, "out", date_from, date_to)

    return render_template(
        "brewery/index.html",
        tab=tab,
        date_from=date_from,
        date_to=date_to,
        tx_view=tx_view,
        inv_view=inv_view,
        tx_kpis=tx_kpis,
        kpi_in=kpi_in,
        kpi_out=kpi_out,
        tx_inflows=tx_inflows,
        tx_outflows=tx_outflows,
        tx_inflows_detail=tx_inflows_detail,
        tx_outflows_detail=tx_outflows_detail,
        tx_monthly=tx_monthly,
        tx_inflow_total=tx_inflow_total,
        tx_outflow_total=tx_outflow_total,
        inv_in=inv_in,
        inv_out=inv_out,
    )
