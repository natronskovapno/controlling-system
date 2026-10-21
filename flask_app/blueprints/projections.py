from flask import Blueprint, render_template, request
from db import get_db
from datetime import date, datetime, timedelta
from calendar import monthrange
from i18n import month_name, day_name, months as month_list

bp = Blueprint("projections", __name__, url_prefix="/projections")

# Month names come from i18n so they follow the active language.
VENUE_DISPLAY = {"Garden": "Garden", "Corner": "Corner", "both": "Both", "both_venues": "Both venues"}

# ── Constants ─────────────────────────────────────────────────────────

# Minimum historical sample size to show a projection.
# Below this threshold the average is unreliable → show "Nedovoljno podataka".
MIN_SAMPLE_SIZE = 3  # Lowered from 5: post-taxonomy Garden types have smaller pools

# Corner was barely operational before May 2025 (ramp-up period).
# Including that early data produces artificially low baselines.
# FIX 3: Use May 2025 as the hard floor for Corner historical data.
Corner_BASELINE_START = "2025-05-01"

# Closed event types — never receive fixed cost allocation.
CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}

# ── Tiered projection constants ───────────────────────────────────────

# B2B/corporate types: revenue is contractually set by invoice.
# Flag these for manual input rather than projecting from history.
B2B_TYPES = {
    'korporativni — konferencija',
    'korporativni — proslava',
    'zurka — velika',
    'koncert — veliki',
}

# CV threshold: above this the bucket is considered volatile.
# Volatile buckets use recency-weighted median (COMBO_2 logic).
# Stable buckets use plain median (current logic).
CV_VOLATILE_THRESHOLD = 1.0

# Recency half-life for volatile buckets (days).
RECENCY_HALF_LIFE = 180

# Organiser minimum events to use organiser-specific history.
MIN_ORG_EVENTS = 3

# ── Helpers ───────────────────────────────────────────────────────────

def _month_label(month_str):
    try:
        year, month = month_str.split('-')
        month_idx = int(month) - 1
        return f"{month_name(month_idx + 1)} {year}"
    except:
        return month_str

def _variance_badge(variance):
    if variance is None:
        return "badge-gray"
    if variance >= 0:
        return "badge-green"
    elif variance >= -5:
        return "badge-yellow"
    else:
        return "badge-red"


def _get_projected_fixed_costs(db, event_date, venue):
    """
    Projected per-event fixed cost allocation (venue-aware, 50/50 shared split).
    Matches daily_log._get_daily_fixed_costs() logic:
      - Each venue gets 50% of shared operativni costs + 100% of its own
      - Divided by that venue's open days in the month
      - Divided by 1 (single-event assumption for future events)
    Falls back to most recent month with data for future months.
    Non-operativni submodules excluded.
    """
    try:
        venue_lower = (venue or '').lower().strip()
        if venue_lower not in ('Garden', 'Corner'):
            return None

        dt = datetime.strptime(event_date, "%Y-%m-%d")
        year, month = dt.year, dt.month

        def _venue_monthly_net(y, m, v):
            def _net(venue_filter):
                row = db.execute("""
                    SELECT
                        SUM(CASE WHEN t.type='income'  THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) as inc,
                        SUM(CASE WHEN t.type='expense' THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) as exp
                    FROM invoice_fixed_cost_links l
                    JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
                    WHERE l.year=? AND l.month=? AND t.submodule='operativni' AND t.venue=?
                """, [y, m, venue_filter]).fetchone()
                if row and (row['inc'] or row['exp']):
                    return float(row['inc'] or 0) - float(row['exp'] or 0)
                return 0.0
            shared = _net('shared')
            specific = _net(v)
            total = shared * 0.5 + specific
            return total if (shared != 0 or specific != 0) else None

        def _venue_open_days(y, m, v):
            ms = f"{y:04d}-{m:02d}-01"
            me = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
            return db.execute("""
                SELECT COUNT(DISTINCT date) as c FROM events_daily_log
                WHERE date >= ? AND date < ?
                  AND LOWER(COALESCE(venue,'')) = ?
                  AND LOWER(COALESCE(event_type,'')) NOT IN ('lokal ne radi','zatvoreno','closed')
            """, [ms, me, v]).fetchone()['c'] or 0

        venue_net = _venue_monthly_net(year, month, venue_lower)

        # Fallback to most recent month with operativni data
        if venue_net is None:
            fallback = db.execute("""
                SELECT DISTINCT l.year, l.month
                FROM invoice_fixed_cost_links l
                JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
                WHERE t.submodule = 'operativni'
                ORDER BY l.year DESC, l.month DESC LIMIT 1
            """).fetchone()
            if not fallback:
                return None
            venue_net = _venue_monthly_net(fallback['year'], fallback['month'], venue_lower)
            if venue_net is None:
                return None
            open_days = _venue_open_days(fallback['year'], fallback['month'], venue_lower)
        else:
            open_days = _venue_open_days(year, month, venue_lower)

        if not open_days or venue_net == 0:
            return None

        daily_rate = venue_net / open_days

        # Count open events today at this venue (for multi-event days)
        n_today = db.execute("""
            SELECT COUNT(*) as n FROM events_daily_log
            WHERE date = ?
              AND LOWER(COALESCE(venue,'')) = ?
              AND LOWER(COALESCE(event_type,'')) NOT IN ('lokal ne radi','zatvoreno','closed')
        """, [event_date, venue_lower]).fetchone()['n'] or 1

        return round(daily_rate / n_today, 2)

    except Exception:
        return None


def _median(values):
    """Return median of a list of numbers. Returns 0 for empty list."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _cv(values):
    """Coefficient of variation (stdev/mean). Returns inf for <2 values or mean=0."""
    import math
    if len(values) < 2:
        return float('inf')
    mean = sum(values) / len(values)
    if mean <= 0:
        return float('inf')
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance) / mean


def _recency_weight(event_date_str, ref_date_str, half_life_days=RECENCY_HALF_LIFE):
    """Exponential decay weight: 1.0 for same day, 0.5 at half_life_days ago."""
    import math
    try:
        from datetime import datetime as _dt
        d1 = _dt.strptime(ref_date_str, '%Y-%m-%d')
        d2 = _dt.strptime(event_date_str, '%Y-%m-%d')
        days = (d1 - d2).days
        if days < 0:
            return 0.0
        return math.exp(-0.693 * days / half_life_days)
    except Exception:
        return 1.0


def _weighted_median(values, weights):
    """Weighted median: smallest value where cumulative weight >= 50%."""
    if not values:
        return 0
    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return _median(values)
    cumsum = 0
    for v, w in pairs:
        cumsum += w
        if cumsum >= total / 2:
            return v
    return pairs[-1][0]


def _get_organiser_history(db, event_type, venue, organiser, ref_date):
    """
    Revenue history for the same organiser + event_type + venue,
    excluding ref_date itself. Returns list of (date_str, revenue) tuples.
    Only non-zero revenue events included.
    """
    if not organiser:
        return []
    if venue.lower() == 'corner':
        date_condition = f"AND date >= '{Corner_BASELINE_START}'"
    else:
        date_condition = "AND date >= date('now', '-18 months')"
    rows = db.execute(f"""
        SELECT date,
               COALESCE(sales_net_override, sales_net_auto, 0) +
               COALESCE(business_bills_override, business_bills_net_auto, 0) -
               COALESCE(third_party_override, third_party_auto, 0) +
               COALESCE(venue_rent_override, venue_rent, 0) +
               COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
               COALESCE(other_revenues, 0) AS revenue
        FROM events_daily_log
        WHERE LOWER(TRIM(event_type)) = LOWER(TRIM(?))
          AND LOWER(venue)            = LOWER(?)
          AND LOWER(TRIM(COALESCE(organiser, ''))) = LOWER(TRIM(?))
          {date_condition}
          AND date < ?
          AND date < date('now')
          AND LOWER(TRIM(COALESCE(event_type, '')))
              NOT IN ('lokal ne radi', 'zatvoreno', 'closed')
          AND status != 'Cancelled'
    """, [event_type, venue, organiser, ref_date]).fetchall()
    return [(r['date'], float(r['revenue'])) for r in rows if float(r['revenue']) > 0]


def _get_historical_average(db, event_type, venue):
    """
    Calculate historical average for given event_type + venue.

    FIX 1: avg_fixed_costs removed — replaced by _get_projected_fixed_costs().
    FIX 3: Corner baselines start from Corner_BASELINE_START (2025-05-01),
            not a rolling 12-month window, to exclude the ramp-up period.
    FIX 9: Revenue baseline uses MEDIAN instead of AVG to reduce distortion
            from outlier events (e.g. single 3.5M koncert — veliki inflating
            the mean far above the typical event value). Cost ratios remain
            AVG-based (ratios are naturally bounded and less outlier-sensitive).

    Returns dict with averages, sample_size, and no_historical_data flag.
    """
    # FIX 3: Venue-specific baseline start date
    if venue.lower() == 'corner':
        date_condition = f"AND date >= '{Corner_BASELINE_START}'"
    else:
        date_condition = "AND date >= date('now', '-18 months')"

    # ── Step 1: fetch per-event values and compute medians in Python ──
    # FIX 9: median revenue; FIX 10: median labor (abs and ratio)
    rev_sql = f"""
        SELECT
            date,
            COALESCE(sales_net_override, sales_net_auto, 0) +
            COALESCE(business_bills_override, business_bills_net_auto, 0) -
            COALESCE(third_party_override, third_party_auto, 0) +
            COALESCE(venue_rent_override, venue_rent, 0) +
            COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
            COALESCE(other_revenues, 0) as revenue,
            COALESCE(wages_override, wages_auto, 0) +
            COALESCE(laborers_override, laborers_auto, 0) as labor
        FROM events_daily_log
        WHERE LOWER(TRIM(event_type)) = LOWER(TRIM(?))
          AND LOWER(venue) = LOWER(?)
          {date_condition}
          AND date < date('now')
          AND LOWER(TRIM(COALESCE(event_type,''))) NOT IN ('lokal ne radi', 'zatvoreno', 'closed')
          AND status != 'Cancelled'
          AND (
              COALESCE(sales_net_override, sales_net_auto, 0) +
              COALESCE(business_bills_override, business_bills_net_auto, 0) +
              COALESCE(venue_rent_override, venue_rent, 0) +
              COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
              COALESCE(other_revenues, 0)
          ) > 0
    """
    rev_rows = db.execute(rev_sql, [event_type, venue]).fetchall()
    if not rev_rows:
        return {
            'sample_size': 0,
            'avg_total_revenue': 0,
            'avg_cogs_ratio': 0,
            'avg_labor_ratio': 0,
            'avg_labor_abs': 0,
            'avg_labor_ratio_sq': 0,
            'no_historical_data': True
        }
    rev_vals   = [float(r['revenue']) for r in rev_rows]
    labor_vals = [float(r['labor'])   for r in rev_rows]
    ratio_vals = [float(r['labor']) / float(r['revenue'])
                  for r in rev_rows if float(r['revenue']) > 0]

    median_revenue     = _median(rev_vals)
    median_labor_abs   = _median(labor_vals)
    median_labor_ratio = _median(ratio_vals) if ratio_vals else 0

    # ── Step 2: cost ratios and absolute labor via SQL AVG ─────────────
    sql = f"""
        SELECT
            COUNT(*) as sample_size,

            -- Revenue placeholder: median is computed above in Python.
            -- We keep avg here only as a fallback reference (not used for projection).
            AVG(
                COALESCE(sales_net_override, sales_net_auto, 0) +
                COALESCE(business_bills_override, business_bills_net_auto, 0) -
                COALESCE(third_party_override, third_party_auto, 0) +
                COALESCE(venue_rent_override, venue_rent, 0) +
                COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                COALESCE(other_revenues, 0)
            ) as avg_total_revenue,

            -- FIX: cost ratios as % of revenue (more stable than fixed amounts)
            -- When revenue varies widely within a type, a fixed avg amount is misleading.
            -- A ratio applied to projected revenue tracks actual costs much better.
            AVG(
                CASE WHEN (
                    COALESCE(sales_net_override, sales_net_auto, 0) +
                    COALESCE(business_bills_override, business_bills_net_auto, 0) -
                    COALESCE(third_party_override, third_party_auto, 0) +
                    COALESCE(venue_rent_override, venue_rent, 0) +
                    COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                    COALESCE(other_revenues, 0)
                ) > 0 THEN
                    COALESCE(cogs_override, cogs_auto, 0) / (
                        COALESCE(sales_net_override, sales_net_auto, 0) +
                        COALESCE(business_bills_override, business_bills_net_auto, 0) -
                        COALESCE(third_party_override, third_party_auto, 0) +
                        COALESCE(venue_rent_override, venue_rent, 0) +
                        COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                        COALESCE(other_revenues, 0)
                    )
                ELSE NULL END
            ) as avg_cogs_ratio,

            AVG(
                CASE WHEN (
                    COALESCE(sales_net_override, sales_net_auto, 0) +
                    COALESCE(business_bills_override, business_bills_net_auto, 0) -
                    COALESCE(third_party_override, third_party_auto, 0) +
                    COALESCE(venue_rent_override, venue_rent, 0) +
                    COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                    COALESCE(other_revenues, 0)
                ) > 0 THEN
                    (COALESCE(wages_override, wages_auto, 0) +
                     COALESCE(laborers_override, laborers_auto, 0)) / (
                        COALESCE(sales_net_override, sales_net_auto, 0) +
                        COALESCE(business_bills_override, business_bills_net_auto, 0) -
                        COALESCE(third_party_override, third_party_auto, 0) +
                        COALESCE(venue_rent_override, venue_rent, 0) +
                        COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                        COALESCE(other_revenues, 0)
                    )
                ELSE NULL END
            ) as avg_labor_ratio,

            -- FIX: also compute absolute avg labor and stdev of labor ratio
            -- for the hybrid labor projection logic
            AVG(
                COALESCE(wages_override, wages_auto, 0) +
                COALESCE(laborers_override, laborers_auto, 0)
            ) as avg_labor_abs,

            -- Variance of labor ratio = AVG(ratio²) - AVG(ratio)²
            -- Used to detect when ratio is unreliable (high variance = use absolute)
            AVG(
                CASE WHEN (
                    COALESCE(sales_net_override, sales_net_auto, 0) +
                    COALESCE(business_bills_override, business_bills_net_auto, 0) -
                    COALESCE(third_party_override, third_party_auto, 0) +
                    COALESCE(venue_rent_override, venue_rent, 0) +
                    COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                    COALESCE(other_revenues, 0)
                ) > 0 THEN
                    ((COALESCE(wages_override, wages_auto, 0) +
                      COALESCE(laborers_override, laborers_auto, 0)) / (
                        COALESCE(sales_net_override, sales_net_auto, 0) +
                        COALESCE(business_bills_override, business_bills_net_auto, 0) -
                        COALESCE(third_party_override, third_party_auto, 0) +
                        COALESCE(venue_rent_override, venue_rent, 0) +
                        COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                        COALESCE(other_revenues, 0)
                    )) * ((COALESCE(wages_override, wages_auto, 0) +
                      COALESCE(laborers_override, laborers_auto, 0)) / (
                        COALESCE(sales_net_override, sales_net_auto, 0) +
                        COALESCE(business_bills_override, business_bills_net_auto, 0) -
                        COALESCE(third_party_override, third_party_auto, 0) +
                        COALESCE(venue_rent_override, venue_rent, 0) +
                        COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                        COALESCE(other_revenues, 0)
                    ))
                ELSE NULL END
            ) as avg_labor_ratio_sq

        FROM events_daily_log
        WHERE LOWER(TRIM(event_type)) = LOWER(TRIM(?))
          AND LOWER(venue) = LOWER(?)
          {date_condition}
          AND date < date('now')
          AND LOWER(TRIM(COALESCE(event_type,''))) NOT IN ('lokal ne radi', 'zatvoreno', 'closed')
          AND status != 'Cancelled'
          AND (
              COALESCE(sales_net_override, sales_net_auto, 0) +
              COALESCE(business_bills_override, business_bills_net_auto, 0) +
              COALESCE(venue_rent_override, venue_rent, 0) +
              COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
              COALESCE(other_revenues, 0)
          ) > 0
    """

    row = db.execute(sql, [event_type, venue]).fetchone()

    if not row or row['sample_size'] == 0:
        return {
            'sample_size': 0,
            'avg_total_revenue': 0,
            'avg_cogs_ratio': 0,
            'avg_labor_ratio': 0,
            'avg_labor_abs': 0,
            'avg_labor_ratio_sq': 0,
            'no_historical_data': True
        }

    result = dict(row)
    for key in result:
        if result[key] is None and key != 'sample_size':
            result[key] = 0

    # FIX 9: override avg revenue with median revenue
    result['avg_total_revenue'] = median_revenue
    # FIX 10: override labor with median values — eliminates distortion from
    # low-revenue events where labor/rev ratio is artificially high.
    # avg_labor_ratio_sq is set to 0 so _labor_projection stdev = 0,
    # meaning ratio method stays for low-stdev types (correct behaviour —
    # median ratio is now reliable enough to use directly).
    result['avg_labor_ratio']    = median_labor_ratio
    result['avg_labor_abs']      = median_labor_abs
    result['avg_labor_ratio_sq'] = median_labor_ratio * median_labor_ratio  # stdev→0 for ratio method
    result['no_historical_data'] = False
    # Tiered projection extras: CV and raw (date, revenue) pairs for recency weighting
    result['bucket_cv']       = _cv(rev_vals)
    result['rev_vals']        = rev_vals
    result['rev_dates']       = [r['date'] for r in rev_rows]
    # P25/P75 for confidence range display on high-CV buckets
    if len(rev_vals) >= 4:
        import statistics as _stats
        qs = _stats.quantiles(rev_vals, n=4)
        result['p25'] = qs[0]
        result['p75'] = qs[2]
    else:
        result['p25'] = median_revenue
        result['p75'] = median_revenue
    return result



def _get_monthly_fixed_cost(db, year, month):
    """
    Get net operativni fixed cost for a month (excludes parking/podnajam/sponzori/ostalo).
    Used for month-level P&L projection summary.
    Returns total operativni net (shared + Garden + Corner combined).
    Falls back to 3-month rolling average for future months.
    """
    row = db.execute("""
        SELECT
            SUM(CASE WHEN t.type = 'income'  THEN ROUND(l.amount / (1 + COALESCE(l.vat_rate,0)/100.0), 2) ELSE 0 END) AS total_income,
            SUM(CASE WHEN t.type = 'expense' THEN ROUND(l.amount / (1 + COALESCE(l.vat_rate,0)/100.0), 2) ELSE 0 END) AS total_expense
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE l.year = ? AND l.month = ? AND t.submodule = 'operativni'
    """, [year, month]).fetchone()

    if row and (row['total_income'] or row['total_expense']):
        return float(row['total_income'] or 0) - float(row['total_expense'] or 0)

    # No data for this month (future) — rolling average of last 3 months
    recent = db.execute("""
        SELECT
            SUM(CASE WHEN t.type = 'income'  THEN ROUND(l.amount / (1 + COALESCE(l.vat_rate,0)/100.0), 2) ELSE 0 END) -
            SUM(CASE WHEN t.type = 'expense' THEN ROUND(l.amount / (1 + COALESCE(l.vat_rate,0)/100.0), 2) ELSE 0 END) AS net_fixed
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE (l.year * 12 + l.month) < (? * 12 + ?) AND t.submodule = 'operativni'
        GROUP BY l.year, l.month
        ORDER BY l.year DESC, l.month DESC
        LIMIT 3
    """, [year, month]).fetchall()

    if recent:
        return sum(float(r['net_fixed'] or 0) for r in recent) / len(recent)
    return None


def _get_standalone_income(db, year, month):
    """
    Get standalone income (parking, podnajam, sponzori) for a month.
    Parking + podnajam: rolling avg of last 3 months if no data for this month.
    Sponzori: actuals only, None for future months.
    Returns dict: {'parking': float|None, 'podnajam': float|None, 'sponzori': float|None}
    """
    result = {'parking': None, 'podnajam': None, 'sponzori': None}
    for sub in ('parking', 'podnajam', 'sponzori'):
        row = db.execute("""
            SELECT
                SUM(CASE WHEN t.type='income'  THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) -
                SUM(CASE WHEN t.type='expense' THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) AS net
            FROM invoice_fixed_cost_links l
            JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
            WHERE l.year = ? AND l.month = ? AND t.submodule = ?
        """, [year, month, sub]).fetchone()
        if row and row['net'] is not None:
            result[sub] = float(row['net'])
        elif sub in ('parking', 'podnajam'):
            recent = db.execute("""
                SELECT
                    SUM(CASE WHEN t.type='income'  THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) -
                    SUM(CASE WHEN t.type='expense' THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) AS net
                FROM invoice_fixed_cost_links l
                JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
                WHERE (l.year*12+l.month) < (?*12+?) AND t.submodule = ?
                GROUP BY l.year, l.month
                ORDER BY l.year DESC, l.month DESC LIMIT 3
            """, [year, month, sub]).fetchall()
            if recent:
                result[sub] = sum(float(r['net'] or 0) for r in recent) / len(recent)
    return result


# Labor ratio stdev threshold above which we switch to absolute average.
# If ratio stdev > 50pp, revenue variance is too high for ratio to be meaningful.
LABOR_RATIO_STDEV_THRESHOLD = 0.50


def _labor_projection(revenue, avg_labor_ratio, avg_labor_abs, avg_labor_ratio_sq):
    """
    Hybrid labor projection:
    - If labor ratio stdev is LOW  (< threshold): use ratio × projected revenue
    - If labor ratio stdev is HIGH (≥ threshold): use absolute average labor amount
    
    Stdev of ratio = sqrt(E[ratio²] - E[ratio]²)
    High stdev means labor is a fixed cost regardless of revenue (pozoriste, kultura,
    low-revenue konferencija) — in that case the absolute amount is more accurate.
    """
    import math
    ratio    = avg_labor_ratio    or 0
    ratio_sq = avg_labor_ratio_sq or 0
    variance = max(ratio_sq - ratio * ratio, 0)  # clamp floating point negatives
    stdev    = math.sqrt(variance)

    if stdev >= LABOR_RATIO_STDEV_THRESHOLD:
        # High variance — use fixed absolute amount
        return round(avg_labor_abs or 0, 0)
    else:
        # Low variance — ratio scales reliably with revenue
        return round((revenue or 0) * ratio, 0)


def _calculate_profit_metrics(revenue, cogs_ratio, labor_ratio, fixed_costs,
                               avg_labor_abs=0, avg_labor_ratio_sq=0):
    """
    Projected profit calculation.
    - COGS: always ratio-based (scales well with revenue across all types)
    - Labor: hybrid — ratio when stdev is low, absolute when stdev is high
    """
    proj_cogs  = round((revenue or 0) * (cogs_ratio or 0), 0)
    proj_labor = _labor_projection(revenue, labor_ratio, avg_labor_abs, avg_labor_ratio_sq)
    operational_costs = proj_cogs + proj_labor
    gross_profit = (revenue or 0) - operational_costs
    net_profit   = gross_profit + (fixed_costs or 0)
    return {
        'gross_profit':     gross_profit,
        'net_profit':       net_profit,
        'total_expenses':   operational_costs,
        'proj_cogs':        proj_cogs,
        'proj_labor':       proj_labor,
    }


@bp.route("/")
def index():
    db = get_db()

    # ── Filters ──────────────────────────────────────────────────────
    today = date.today()

    default_from = "2025-01-01"
    last_day = monthrange(today.year, today.month)[1]
    default_to = date(today.year, today.month, last_day).isoformat()

    date_from    = request.args.get("date_from", default_from)
    date_to      = request.args.get("date_to", default_to)
    venue_filter = request.args.get("venue", "all")

    # ── Fetch events ─────────────────────────────────────────────────
    sql = """
        SELECT
            event_id,
            date,
            strftime('%Y-%m', date) as month,
            venue,
            organiser,
            event_name,
            event_type,
            event_start,
            event_end,
            status,

            (COALESCE(sales_net_override, sales_net_auto, 0) +
             COALESCE(business_bills_override, business_bills_net_auto, 0) -
             COALESCE(third_party_override, third_party_auto, 0) +
             COALESCE(venue_rent_override, venue_rent, 0) +
             COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
             COALESCE(other_revenues, 0)) as actual_revenue,

            COALESCE(cogs_override, cogs_auto)              as actual_cogs,
            COALESCE(wages_override, wages_auto)            as actual_wages,
            COALESCE(laborers_override, laborers_auto)      as actual_laborers,
            COALESCE(musicians_fee, 0)                      as actual_musicians,
            COALESCE(equipment_expenses, 0)                 as actual_equipment,
            COALESCE(advertising, 0)                        as actual_advertising,
            COALESCE(debit_card_expenses_variable, 0)       as actual_debit,
            COALESCE(other_expenses, 0)                     as actual_other,
            fixed_profit                                    as actual_fixed_costs,

            CASE
                WHEN status = 'Confirmed' THEN 1
                WHEN (COALESCE(sales_net_override, sales_net_auto, 0) +
                      COALESCE(business_bills_override, business_bills_net_auto, 0)) > 0 THEN 1
                ELSE 0
            END as has_actuals

        FROM events_daily_log
        WHERE date >= ? AND date <= ?
    """

    params = [date_from, date_to]
    if venue_filter != "all":
        sql += " AND LOWER(venue) = LOWER(?)"
        params.append(venue_filter)
    sql += " ORDER BY date DESC, event_id DESC"

    events_raw = db.execute(sql, params).fetchall()
    events = [dict(e) for e in events_raw]

    # ── Calculate projections ─────────────────────────────────────────
    for evt in events:
        event_type = evt['event_type']
        venue      = evt['venue']

        # Always set is_past and actual_net_profit regardless of event_type/venue,
        # so these events are included in month actual totals even if unprojectable.
        evt['is_past'] = evt['date'] < today.isoformat()
        if evt['is_past']:
            act_rev_pre   = evt['actual_revenue'] or 0
            act_all_pre   = (
                (evt['actual_cogs']         or 0) +
                (evt['actual_wages']        or 0) +
                (evt['actual_laborers']     or 0) +
                (evt['actual_musicians']    or 0) +
                (evt['actual_equipment']    or 0) +
                (evt['actual_advertising']  or 0) +
                (evt['actual_debit']        or 0) +
                (evt['actual_other']        or 0)
            )
            act_fixed_pre = evt['actual_fixed_costs'] or 0
            evt['actual_gross_profit'] = act_rev_pre - act_all_pre
            evt['actual_net_profit']   = act_rev_pre - act_all_pre + act_fixed_pre
            evt['actual_total_costs']  = act_all_pre
            evt['actual_revenue']      = act_rev_pre
        else:
            evt['actual_gross_profit'] = None
            evt['actual_net_profit']   = None

        if not event_type or not venue:
            evt['projected'] = None
            evt['revenue_variance'] = None
            evt['profit_variance']  = None
            continue

        event_type_lower = event_type.strip().lower()
        is_closed = event_type_lower in CLOSED_TYPES

        # Closed events: no projection needed
        if is_closed:
            evt['projected'] = {
                'revenue': 0, 'cogs': 0, 'wages': 0, 'laborers': 0,
                'musicians': 0, 'equipment': 0, 'advertising': 0,
                'debit': 0, 'other': 0, 'fixed_costs': 0,
                'gross_profit': 0, 'net_profit': 0,
                'sample_size': 0, 'no_data': False, 'insufficient_data': False
            }
            evt['revenue_variance'] = None
            evt['profit_variance'] = None
            continue

        # Get historical averages (FIX 1: no fixed costs here)
        hist = _get_historical_average(db, event_type, venue)

        # FIX 2: Minimum sample size check
        # Below MIN_SAMPLE_SIZE → mark insufficient, don't show projection numbers
        insufficient = hist['sample_size'] < MIN_SAMPLE_SIZE and not hist.get('no_historical_data')
        no_data = hist.get('no_historical_data', False)

        # FIX 3: Forward fixed costs calculation
        projected_fixed = _get_projected_fixed_costs(db, evt['date'], venue)

        # ── Tiered revenue projection ─────────────────────────────────
        # Tier 1: organiser has >= MIN_ORG_EVENTS → organiser-specific median
        # Tier 2: volatile bucket (CV > threshold) → recency-weighted median
        # Tier 3: stable bucket → plain median (existing logic, unchanged)
        #
        # B2B types are flagged for manual input regardless of tier.

        event_type_lower_b2b = event_type.strip().lower()
        is_b2b = event_type_lower_b2b in B2B_TYPES

        proj_tier      = 'bucket'   # default — for template display
        proj_rev       = hist['avg_total_revenue']
        bucket_cv      = hist.get('bucket_cv', 0)
        p25            = hist.get('p25', proj_rev)
        p75            = hist.get('p75', proj_rev)

        if not (insufficient or no_data):
            organiser = evt.get('organiser') or ''
            org_history = _get_organiser_history(
                db, event_type, venue, organiser, evt['date']
            )

            if len(org_history) >= MIN_ORG_EVENTS:
                # Tier 1: organiser-specific median
                org_vals = [v for _, v in org_history]
                proj_rev = _median(org_vals)
                proj_tier = 'organiser'
                # Tighter P25/P75 from organiser history
                if len(org_vals) >= 4:
                    import statistics as _stats
                    qs = _stats.quantiles(org_vals, n=4)
                    p25, p75 = qs[0], qs[2]
                else:
                    p25 = p75 = proj_rev

            elif bucket_cv > CV_VOLATILE_THRESHOLD:
                # Tier 2: volatile bucket → recency-weighted median
                rev_vals  = hist.get('rev_vals', [])
                rev_dates = hist.get('rev_dates', [])
                if rev_vals and rev_dates:
                    weights = [
                        _recency_weight(d, evt['date'], RECENCY_HALF_LIFE)
                        for d in rev_dates
                    ]
                    proj_rev  = _weighted_median(rev_vals, weights)
                proj_tier = 'recency'
            # else: Tier 3 — proj_rev already set to plain median above

        cogs_ratio         = hist['avg_cogs_ratio']      or 0
        labor_ratio        = hist['avg_labor_ratio']     or 0
        avg_labor_abs      = hist['avg_labor_abs']       or 0
        avg_labor_ratio_sq = hist['avg_labor_ratio_sq']  or 0

        proj_cogs  = round(proj_rev * cogs_ratio, 0)
        proj_labor = _labor_projection(proj_rev, labor_ratio, avg_labor_abs, avg_labor_ratio_sq)

        # Reliability indicator based on CV (displayed in template)
        if bucket_cv < 0.5:
            reliability = 'high'
        elif bucket_cv < 1.0:
            reliability = 'medium'
        else:
            reliability = 'low'

        evt['projected'] = {
            'revenue':     proj_rev,
            'cogs':        proj_cogs,
            'labor':       proj_labor,
            'cogs_ratio':  round(cogs_ratio  * 100, 1),
            'labor_ratio': round(labor_ratio * 100, 1),
            'fixed_costs': projected_fixed or 0,
            'sample_size': hist['sample_size'],
            'no_data':           no_data,
            'insufficient_data': insufficient or no_data,
            # Tiered projection extras
            'proj_tier':   proj_tier,
            'is_b2b':      is_b2b,
            'bucket_cv':   round(bucket_cv, 2),
            'reliability': reliability,
            'p25':         round(p25, 0),
            'p75':         round(p75, 0),
        }

        # Calculate projected profit
        if not (insufficient or no_data):
            proj_metrics = _calculate_profit_metrics(
                proj_rev, cogs_ratio, labor_ratio, projected_fixed or 0,
                avg_labor_abs, avg_labor_ratio_sq
            )
            evt['projected']['gross_profit'] = proj_metrics['gross_profit']
            evt['projected']['net_profit']   = proj_metrics['net_profit']
        else:
            evt['projected']['gross_profit'] = None
            evt['projected']['net_profit']   = None

        # Per-event variance (only when sufficient data AND has bar/B2B revenue)
        proj_rev    = evt['projected'].get('revenue')
        proj_profit = evt['projected'].get('net_profit')

        if evt['has_actuals'] and not (insufficient or no_data):
            if proj_rev and proj_rev != 0:
                evt['revenue_variance'] = (
                    (evt['actual_revenue'] - proj_rev) / proj_rev
                ) * 100
            else:
                evt['revenue_variance'] = None

            if proj_profit is not None and proj_profit != 0 \
                    and evt.get('actual_net_profit') is not None:
                if proj_profit < 0:
                    evt['profit_variance'] = (
                        (evt['actual_net_profit'] - proj_profit) / abs(proj_profit)
                    ) * 100
                else:
                    evt['profit_variance'] = (
                        (evt['actual_net_profit'] - proj_profit) / proj_profit
                    ) * 100
            else:
                evt['profit_variance'] = None
        else:
            evt['revenue_variance'] = None
            evt['profit_variance']  = None

    # ── Group by month ────────────────────────────────────────────────
    from itertools import groupby

    months_data = []
    for month_key, month_events in groupby(events, key=lambda e: e['month']):
        month_events_list = list(month_events)

        # Only sum projected figures from events that have valid projections
        valid_proj = [
            e for e in month_events_list
            if e['projected'] and not e['projected'].get('insufficient_data')
        ]

        month_proj_revenue = sum(e['projected']['revenue'] or 0 for e in valid_proj)

        # FIX: use actual fc_net for the month, not sum of per-event proj_fixed.
        # Per-event proj_fixed only covers n_ok events; insufficient events carry
        # their real share in actuals but zero in projection, causing a systematic gap.
        # Fixed costs are a monthly business property — use the invoice total directly.
        try:
            year_int  = int(month_key[:4])
            month_int = int(month_key[5:7])
            month_fc_net = _get_monthly_fixed_cost(db, year_int, month_int) or 0
        except Exception:
            month_fc_net = 0

        # Projected variable costs (COGS + labor) from events with sufficient data
        month_proj_var_costs = sum(
            (e['projected'].get('cogs') or 0) + (e['projected'].get('labor') or 0)
            for e in valid_proj
        )
        # Scale variable costs to full month if coverage < 100%
        # (insufficient events are assumed to have same avg cost ratios as OK events)
        n_ok    = len(valid_proj)
        n_total_open = sum(
            1 for e in month_events_list
            if (e.get('event_type') or '').lower() not in CLOSED_TYPES
        )
        if n_ok > 0 and n_total_open > n_ok:
            scale = n_total_open / n_ok
            month_proj_var_costs = round(month_proj_var_costs * scale, 0)

        # Standalone income (parking, podnajam, sponzori) — added to profit, not expenses
        standalone = _get_standalone_income(db, year_int, month_int)
        standalone_total = sum(v for v in standalone.values() if v is not None)

        month_proj_expenses = month_proj_var_costs + abs(month_fc_net)
        month_proj_profit   = month_proj_revenue - month_proj_expenses + standalone_total

        # Actual totals — all past events regardless of has_actuals
        past_events = [e for e in month_events_list if e.get('is_past')]
        month_actual_revenue  = sum(e.get('actual_revenue') or 0 for e in past_events)
        month_actual_profit   = sum(e.get('actual_net_profit') or 0 for e in past_events)
        month_actual_expenses = month_actual_revenue - month_actual_profit

        # Month variance
        if past_events and month_proj_revenue > 0:
            month_revenue_variance = (
                (month_actual_revenue - month_proj_revenue) / month_proj_revenue
            ) * 100
        else:
            month_revenue_variance = None

        if past_events and month_proj_profit != 0:
            if month_proj_profit < 0:
                month_profit_variance = (
                    (month_actual_profit - month_proj_profit) / abs(month_proj_profit)
                ) * 100
            else:
                month_profit_variance = (
                    (month_actual_profit - month_proj_profit) / month_proj_profit
                ) * 100
        else:
            month_profit_variance = None

        real_events = [
            e for e in month_events_list
            if e.get('event_type')
            and e['event_type'].lower() not in ['redovan dan', 'lokal ne radi']
        ]

        months_data.append({
            'month':              month_key,
            'month_label':        _month_label(month_key),
            'events':             month_events_list,
            'event_count':        len(real_events),
            'projected_revenue':  month_proj_revenue,
            'projected_expenses': month_proj_expenses,
            'projected_profit':   month_proj_profit,
            'projected_fixed':    month_fc_net,
            'standalone_income':  standalone,
            'standalone_total':   standalone_total,
            'actual_revenue':     month_actual_revenue  if past_events else None,
            'actual_expenses':    month_actual_expenses if past_events else None,
            'actual_profit':      month_actual_profit   if past_events else None,
            'revenue_variance':   month_revenue_variance,
            'profit_variance':    month_profit_variance,
            'has_any_actuals':    len(past_events) > 0
        })

    # ── Venues for filter ─────────────────────────────────────────────
    venues = db.execute("""
        SELECT DISTINCT LOWER(venue) as venue
        FROM events_daily_log
        WHERE venue IS NOT NULL
        ORDER BY venue
    """).fetchall()
    venues = [v['venue'] for v in venues]

    return render_template("projections/index.html",
        months=months_data,
        filters={
            'date_from': date_from,
            'date_to':   date_to,
            'venue':     venue_filter
        },
        venues=venues,
        variance_badge=_variance_badge,
        venue_display=VENUE_DISPLAY,
        today=today.isoformat(),
        min_sample_size=MIN_SAMPLE_SIZE
    )
