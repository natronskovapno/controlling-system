from flask import Blueprint, render_template, request
from db import get_db
from datetime import date, datetime
from calendar import monthrange
from itertools import groupby
from i18n import month_name, day_name, months as month_list

bp = Blueprint("performance", __name__, url_prefix="/performance")

# Month names come from i18n so they follow the active language.
VENUE_DISPLAY = {"Garden": "Garden", "Corner": "Corner", "both": "Both", "both_venues": "Both venues"}
CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}

# ── helpers ───────────────────────────────────────────────────────────

def _month_label(month_str):
    try:
        year, month = month_str.split('-')
        return f"{month_name(int(month))} {year}"
    except:
        return month_str

def _day_label(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{day_name(dt.weekday())}, {dt.day}. {month_name(dt.month)} {dt.year}"
    except:
        return date_str

def _trend_arrow(current, previous):
    """Return trend indicator comparing current to previous period."""
    if current is None or previous is None or previous == 0:
        return None
    pct = ((current - previous) / abs(previous)) * 100
    return pct

def _fmt_rsd(val):
    """Format value as RSD string."""
    if val is None:
        return "—"
    return "{:,.0f}".format(val).replace(",", ".")

def _aggregate_events(events_list):
    """
    Aggregate a list of event dicts into summary financials.
    Returns dict with totals, open days, event counts, etc.
    Excludes closed event types from averages.
    """
    open_events = [e for e in events_list
                   if (e.get('event_type') or '').strip().lower() not in CLOSED_TYPES]
    closed_events = [e for e in events_list
                     if (e.get('event_type') or '').strip().lower() in CLOSED_TYPES]

    total_revenue        = sum(e['total_revenue']   or 0 for e in events_list)
    # Revenue sub-components
    total_sales_net      = sum(e['sales_net']        or 0 for e in events_list)
    total_business_bills = sum(e['business_bills']   or 0 for e in events_list)
    total_third_party    = sum(e['third_party']      or 0 for e in events_list)
    total_venue_rent     = sum(e['venue_rent']       or 0 for e in events_list)
    total_ticket_own     = sum(e['ticket_sales_own'] or 0 for e in events_list)
    total_other_rev      = sum(e['other_revenues']   or 0 for e in events_list)
    # Costs
    total_cogs           = sum(e['cogs']            or 0 for e in events_list)
    total_wages          = sum(e['wages']           or 0 for e in events_list)
    total_laborers       = sum(e['laborers']        or 0 for e in events_list)
    total_musicians      = sum(e['musicians_fee']   or 0 for e in events_list)
    total_equipment      = sum(e['equipment']       or 0 for e in events_list)
    total_advertising    = sum(e['advertising']     or 0 for e in events_list)
    total_debit          = sum(e['debit_variable']  or 0 for e in events_list)
    total_other_exp      = sum(e['other_expenses']  or 0 for e in events_list)
    total_fixed          = sum(e['fixed_profit']    or 0 for e in open_events)

    total_variable_costs = (total_cogs + total_wages + total_laborers +
                            total_musicians + total_equipment +
                            total_advertising + total_debit + total_other_exp)

    gross_profit = total_revenue - total_variable_costs
    net_profit   = gross_profit + total_fixed

    total_visitors = sum(e['visitors'] or 0 for e in open_events)
    rev_per_visitor = (total_revenue / total_visitors) if total_visitors > 0 else None

    # Distinct open days (dates with at least one non-closed event)
    open_dates = set(e['date'] for e in open_events)
    closed_dates = set(e['date'] for e in closed_events) - open_dates

    # Count real events (not redovan dan, not closed)
    real_event_types = {'redovan dan', 'lokal ne radi', 'zatvoreno', 'closed'}
    special_events = [e for e in events_list
                      if (e.get('event_type') or '').strip().lower() not in real_event_types]

    return {
        'total_revenue':        total_revenue,
        'total_sales_net':      total_sales_net,
        'total_business_bills': total_business_bills,
        'total_third_party':    total_third_party,
        'total_venue_rent':     total_venue_rent,
        'total_ticket_own':     total_ticket_own,
        'total_other_rev':      total_other_rev,
        'total_cogs':           total_cogs,
        'total_wages':          total_wages,
        'total_laborers':       total_laborers,
        'total_musicians':      total_musicians,
        'total_equipment':      total_equipment,
        'total_advertising':    total_advertising,
        'total_debit':          total_debit,
        'total_other_exp':      total_other_exp,
        'total_variable_costs': total_variable_costs,
        'total_fixed':          total_fixed,
        'gross_profit':         gross_profit,
        'net_profit':           net_profit,
        'total_visitors':       total_visitors,
        'rev_per_visitor':      rev_per_visitor,
        'open_days':            len(open_dates),
        'closed_days':          len(closed_dates),
        'special_event_count':  len(special_events),
        'total_event_rows':     len(events_list),
    }

def _build_venue_split(events_list):
    """Build per-venue aggregates from event list."""
    Garden_events = [e for e in events_list if (e.get('venue') or '').lower() == 'garden']
    Corner_events = [e for e in events_list if (e.get('venue') or '').lower() == 'corner']
    return {
        'Garden': _aggregate_events(Garden_events) if Garden_events else None,
        'Corner': _aggregate_events(Corner_events) if Corner_events else None,
        'total': _aggregate_events(events_list),
    }

# ── main route ────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    today = date.today()

    # ── Filters ──────────────────────────────────────────────────────
    default_from  = "2025-01-01"
    last_day      = monthrange(today.year, today.month)[1]
    default_to    = date(today.year, today.month, last_day).isoformat()

    date_from     = request.args.get("date_from",    default_from)
    date_to       = request.args.get("date_to",      default_to)
    venue_filter  = request.args.get("venue",        "all")
    event_types_f = request.args.getlist("event_type")   # multiselect
    granularity   = request.args.get("granularity",  "monthly")  # monthly | daily
    all_streams   = ['parking', 'podnajam', 'sponzori', 'ostalo', 'fiksni']
    selected_streams = request.args.getlist("stream") or all_streams

    # ── Event types for filter dropdown ──────────────────────────────
    all_event_types = [r[0] for r in db.execute(
        "SELECT DISTINCT event_type FROM events_daily_log "
        "WHERE event_type IS NOT NULL AND TRIM(event_type) != '' "
        "ORDER BY event_type"
    ).fetchall()]

    # ── Main data query ───────────────────────────────────────────────
    sql = """
        SELECT
            event_id,
            date,
            strftime('%Y-%m', date)  AS month,
            venue,
            event_type,
            event_name,
            organiser,
            COALESCE(number_of_visitors, 0) AS visitors,
            status,

            -- Total revenue (matching Daily Log / Event Type Analysis pattern)
            (
                COALESCE(sales_net_override,          sales_net_auto,          0) +
                COALESCE(business_bills_override,     business_bills_net_auto, 0) -
                COALESCE(third_party_override,        third_party_auto,        0) +
                COALESCE(venue_rent_override,         venue_rent,              0) +
                COALESCE(ticket_sales_own_override,   ticket_sales_own,        0) +
                COALESCE(other_revenues, 0)
            ) AS total_revenue,

            -- Revenue components
            COALESCE(sales_net_override,          sales_net_auto,          0) AS sales_net,
            COALESCE(business_bills_override,     business_bills_net_auto, 0) AS business_bills,
            COALESCE(third_party_override,        third_party_auto,        0) AS third_party,
            COALESCE(venue_rent_override,         venue_rent,              0) AS venue_rent,
            COALESCE(ticket_sales_own_override,   ticket_sales_own,        0) AS ticket_sales_own,
            COALESCE(other_revenues, 0)                                        AS other_revenues,

            -- Costs
            COALESCE(cogs_override,      cogs_auto,      0) AS cogs,
            COALESCE(wages_override,     wages_auto,     0) AS wages,
            COALESCE(laborers_override,  laborers_auto,  0) AS laborers,
            COALESCE(musicians_fee,    0)                   AS musicians_fee,
            COALESCE(equipment_expenses, 0)                 AS equipment,
            COALESCE(advertising,        0)                 AS advertising,
            COALESCE(debit_card_expenses_variable, 0)       AS debit_variable,
            COALESCE(other_expenses,     0)                 AS other_expenses,

            -- Fixed costs (NULL for closed events → treat as 0)
            CASE
                WHEN LOWER(TRIM(event_type)) IN ('lokal ne radi','zatvoreno','closed')
                THEN 0
                ELSE COALESCE(fixed_profit, 0)
            END AS fixed_profit

        FROM events_daily_log
        WHERE date >= ? AND date <= ?
    """
    params = [date_from, date_to]

    if venue_filter != "all":
        sql += " AND LOWER(venue) = LOWER(?)"
        params.append(venue_filter)

    if event_types_f:
        placeholders = ",".join("?" * len(event_types_f))
        sql += f" AND LOWER(TRIM(event_type)) IN ({placeholders})"
        params.extend([et.lower() for et in event_types_f])

    sql += " ORDER BY date DESC, event_id DESC"

    rows = db.execute(sql, params).fetchall()
    events = [dict(r) for r in rows]

    # ── YoY lookup: same period previous year ─────────────────────────
    try:
        yoy_from = str(int(date_from[:4]) - 1) + date_from[4:]
        yoy_to   = str(int(date_to[:4])   - 1) + date_to[4:]
    except:
        yoy_from = yoy_to = None

    yoy_events = []
    if yoy_from and yoy_to:
        yoy_sql = sql.replace("WHERE date >= ? AND date <= ?",
                              "WHERE date >= ? AND date <= ?", 1)
        yoy_params = [yoy_from, yoy_to] + params[2:]
        try:
            yoy_rows = db.execute(
                """
                SELECT
                    strftime('%Y-%m', date) AS month,
                    (
                        COALESCE(sales_net_override, sales_net_auto, 0) +
                        COALESCE(business_bills_override, business_bills_net_auto, 0) -
                        COALESCE(third_party_override, third_party_auto, 0) +
                        COALESCE(venue_rent_override, venue_rent, 0) +
                        COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                        COALESCE(other_revenues, 0)
                    ) AS total_revenue,
                    COALESCE(sales_net_override,        sales_net_auto,          0) AS sales_net,
                    COALESCE(business_bills_override,   business_bills_net_auto, 0) AS business_bills,
                    COALESCE(third_party_override,      third_party_auto,        0) AS third_party,
                    COALESCE(venue_rent_override,       venue_rent,              0) AS venue_rent,
                    COALESCE(ticket_sales_own_override, ticket_sales_own,        0) AS ticket_sales_own,
                    COALESCE(other_revenues, 0)                                     AS other_revenues,
                    CASE
                        WHEN LOWER(TRIM(event_type)) IN ('lokal ne radi','zatvoreno','closed')
                        THEN 0 ELSE COALESCE(fixed_profit, 0)
                    END AS fixed_profit,
                    COALESCE(cogs_override, cogs_auto, 0) AS cogs,
                    COALESCE(wages_override, wages_auto, 0) AS wages,
                    COALESCE(laborers_override, laborers_auto, 0) AS laborers,
                    COALESCE(musicians_fee, 0) AS musicians_fee,
                    COALESCE(equipment_expenses, 0) AS equipment,
                    COALESCE(advertising, 0) AS advertising,
                    COALESCE(debit_card_expenses_variable, 0) AS debit_variable,
                    COALESCE(other_expenses, 0) AS other_expenses,
                    COALESCE(number_of_visitors, 0) AS visitors,
                    venue,
                    event_type,
                    date
                FROM events_daily_log
                WHERE date >= ? AND date <= ?
                """ + (" AND LOWER(venue) = LOWER(?)" if venue_filter != "all" else "") +
                (" AND LOWER(TRIM(event_type)) IN ({})".format(",".join("?" * len(event_types_f))) if event_types_f else ""),
                [yoy_from, yoy_to] +
                ([venue_filter] if venue_filter != "all" else []) +
                ([et.lower() for et in event_types_f] if event_types_f else [])
            ).fetchall()
            yoy_events = [dict(r) for r in yoy_rows]
        except:
            yoy_events = []

    # Build YoY month-keyed aggregates (shift month key by +1 year for comparison)
    yoy_by_month = {}
    for yoy_month, yoy_month_events in groupby(
        sorted(yoy_events, key=lambda e: e['month']), key=lambda e: e['month']
    ):
        yoy_list = list(yoy_month_events)
        # Shift key to current year for matching
        try:
            y, m = yoy_month.split('-')
            current_year_key = f"{int(y)+1}-{m}"
        except:
            current_year_key = yoy_month
        agg = _aggregate_events(yoy_list)
        # Also build venue split for YoY
        agg['venue_split'] = _build_venue_split(yoy_list)
        yoy_by_month[current_year_key] = agg

    # ── Standalone income (parking, podnajam, sponzori, ostalo) by month ─
    standalone_rows = db.execute("""
        SELECT l.year, l.month,
               t.submodule,
               ROUND(SUM(CASE WHEN t.type='income'  THEN l.amount/(1+COALESCE(l.vat_rate,0)/100.0) ELSE 0 END) -
                     SUM(CASE WHEN t.type='expense' THEN l.amount/(1+COALESCE(l.vat_rate,0)/100.0) ELSE 0 END), 0) AS net
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE t.submodule IN ('parking','podnajam','sponzori','ostalo')
          AND l.year || '-' || printf('%02d', l.month) >= ? AND l.year || '-' || printf('%02d', l.month) <= ?
        GROUP BY l.year, l.month, t.submodule
    """, [date_from[:7], date_to[:7]]).fetchall()

    standalone_by_month = {}
    for r in standalone_rows:
        ym = f"{r['year']}-{r['month']:02d}"
        if ym not in standalone_by_month:
            standalone_by_month[ym] = {'parking': None, 'podnajam': None, 'sponzori': None, 'ostalo': None}
        if r['submodule'] in selected_streams:
            standalone_by_month[ym][r['submodule']] = float(r['net'] or 0)

    period_standalone_total = sum(
        v for m in standalone_by_month.values() for v in m.values() if v is not None
    ) if standalone_by_month else 0

    # ── Group and aggregate ───────────────────────────────────────────
    if granularity == "monthly":
        # Group by YYYY-MM
        grouped_data = []
        prev_agg = None
        for month_key, month_iter in groupby(events, key=lambda e: e['month']):
            month_list = list(month_iter)
            agg = _aggregate_events(month_list)
            venue_split = _build_venue_split(month_list)

            # MoM trend vs previous card
            mom_revenue = _trend_arrow(agg['total_revenue'], prev_agg['total_revenue'] if prev_agg else None)
            mom_profit  = _trend_arrow(agg['net_profit'],    prev_agg['net_profit']    if prev_agg else None)

            # YoY comparison
            yoy = yoy_by_month.get(month_key)
            yoy_revenue = _trend_arrow(agg['total_revenue'], yoy['total_revenue'] if yoy else None)
            yoy_profit  = _trend_arrow(agg['net_profit'],    yoy['net_profit']    if yoy else None)

            # Collect distinct event types for this month
            event_type_counts = {}
            for e in month_list:
                et = (e.get('event_type') or '').strip()
                if et and et.lower() not in CLOSED_TYPES:
                    event_type_counts[et] = event_type_counts.get(et, 0) + 1

            standalone_m = standalone_by_month.get(month_key, {})
            standalone_total_m = sum(v for v in standalone_m.values() if v is not None)
            # If fiksni excluded, use gross_profit as base instead of net_profit
            base_net = agg['gross_profit'] if 'fiksni' not in selected_streams else agg['net_profit']
            grouped_data.append({
                'key':               month_key,
                'label':             _month_label(month_key),
                'events':            month_list,
                'agg':               agg,
                'venue_split':       venue_split,
                'yoy':               yoy,
                'yoy_revenue_pct':   yoy_revenue,
                'yoy_profit_pct':    yoy_profit,
                'mom_revenue_pct':   mom_revenue,
                'mom_profit_pct':    mom_profit,
                'event_type_counts': event_type_counts,
                'standalone':        standalone_m,
                'standalone_total':  standalone_total_m,
                'total_net':         base_net + standalone_total_m,
            })
            prev_agg = agg

    else:  # daily
        grouped_data = []
        # Group events by date
        for date_key, date_iter in groupby(events, key=lambda e: e['date']):
            day_list = list(date_iter)
            agg = _aggregate_events(day_list)
            venue_split = _build_venue_split(day_list)

            # Collect event types for display
            event_types_day = [
                (e.get('event_type') or '—', e.get('venue') or '')
                for e in day_list
            ]

            grouped_data.append({
                'key':           date_key,
                'label':         _day_label(date_key),
                'events':        day_list,
                'agg':           agg,
                'venue_split':   venue_split,
                'event_types':   event_types_day,
                'has_data':      agg['total_event_rows'] > 0,
            })

    # ── Overall period summary (top KPIs) ─────────────────────────────
    period_agg = _aggregate_events(events) if events else None
    period_venue_split = _build_venue_split(events) if events else None

    # YoY for entire period
    period_yoy = _aggregate_events(yoy_events) if yoy_events else None

    # Pre-compute period-level YoY percentages for template use
    period_yoy_revenue_pct = None
    period_yoy_profit_pct  = None
    if period_agg and period_yoy:
        period_yoy_revenue_pct = _trend_arrow(period_agg['total_revenue'], period_yoy['total_revenue'])
        period_yoy_profit_pct  = _trend_arrow(period_agg['net_profit'],    period_yoy['net_profit'])

    return render_template(
        "performance/index.html",
        grouped_data=grouped_data,
        granularity=granularity,
        filters={
            'date_from':   date_from,
            'date_to':     date_to,
            'venue':       venue_filter,
            'event_types': event_types_f,
        },
        all_event_types=all_event_types,
        period_agg=period_agg,
        period_venue_split=period_venue_split,
        period_yoy=period_yoy,
        period_yoy_revenue_pct=period_yoy_revenue_pct,
        period_yoy_profit_pct=period_yoy_profit_pct,
        venue_display=VENUE_DISPLAY,
        today=today.isoformat(),
        standalone_by_month=standalone_by_month,
        period_standalone_total=period_standalone_total,
        selected_streams=selected_streams,
        all_streams=all_streams,
    )
