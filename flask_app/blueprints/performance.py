from flask import Blueprint, render_template, request
from db import get_db
from datetime import date, datetime
from calendar import monthrange
from itertools import groupby
from i18n import month_name, day_name, months as month_list, tr

bp = Blueprint("performance", __name__, url_prefix="/performance")

# Month names come from i18n so they follow the active language.
VENUE_DISPLAY = {"Garden": "Garden", "Corner": "Corner", "both": "Both", "both_venues": "Both venues"}
CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}

# Streams that are NOT allocated to a venue. In the UI they are presented as
# "Dodatni prihodi i troškovi" — the bridge between the venue result and the
# company result. They are never part of the fixed-cost allocation, which
# only ever touches submodule='operativni' (see daily_log._get_daily_fixed_costs).
EXTRA_SUBMODULES = ('parking', 'podnajam', 'sponzori', 'ostalo')

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

def _shift_year(iso_date, delta):
    """'2026-10-31' + delta years, as an ISO string. None on bad input."""
    try:
        return str(int(iso_date[:4]) + delta) + iso_date[4:]
    except:
        return None

def _prev_year_key(month_key):
    """'2026-07' -> '2025-07'."""
    try:
        y, m = month_key.split('-')
        return f"{int(y)-1}-{m}"
    except:
        return None

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

    NOTE on the two profit measures:
      gross_profit  = revenue - variable costs
      net_profit    = gross_profit + allocated fixed costs   → THE VENUE RESULT
    net_profit deliberately excludes parking/podnajam/sponzori/ostalo, which
    belong to no venue. The company result is net_profit + those streams and
    is assembled in index(), never here.
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

    # How many of those open days actually carry a visitor count. Without this
    # the "prihod po posetiocu" ratio divides a full period's revenue by
    # visitors counted on a fraction of its days.
    visitor_dates = set(e['date'] for e in open_events if (e['visitors'] or 0) > 0)

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
        'visitor_days':         len(visitor_dates),
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
    yoy_from = _shift_year(date_from, -1)
    yoy_to   = _shift_year(date_to,   -1)

    yoy_events = []
    if yoy_from and yoy_to:
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

    # ── Is a period-level YoY comparison meaningful at all? ────────────
    #
    # The comparison window is the filter shifted back one year. For a filter
    # longer than 12 months that window OVERLAPS the filter itself, so the
    # "previous period" is partly made of the same months as the current one
    # and the percentage is nonsense. (A 22-month filter was comparing 22
    # months of revenue against 10 of its own months: +154%.)
    #
    # Per-month YoY is unaffected — each month still compares to the same
    # month a year earlier — so only the period tiles are gated here.
    period_yoy_overlaps = bool(yoy_to and date_from and yoy_to >= date_from)
    period_yoy_empty    = not yoy_events
    period_yoy_valid    = not (period_yoy_overlaps or period_yoy_empty)

    if period_yoy_empty:
        period_yoy_note = tr("Nema podataka za uporedni period ({a} – {b}).",
                             a=yoy_from, b=yoy_to)
    elif period_yoy_overlaps:
        period_yoy_note = tr("Uporedni period se preklapa sa izabranim — "
                             "filter je duži od 12 meseci. Suzite period na "
                             "najviše godinu dana za ispravno poređenje.")
    else:
        period_yoy_note = None

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

    # ── Dodatni prihodi i troškovi, by month ──────────────────────────
    # Queried across BOTH windows at once so the same streams can be added to
    # the prior-year figures. Previously the YoY compared venue-only results
    # against a headline that already included these streams.
    sa_window_from = min(x for x in (date_from[:7], (yoy_from or date_from)[:7]))
    sa_window_to   = max(x for x in (date_to[:7],   (yoy_to   or date_to)[:7]))

    standalone_rows = db.execute(f"""
        SELECT l.year, l.month,
               t.submodule,
               ROUND(SUM(CASE WHEN t.type='income'  THEN l.amount/(1+COALESCE(l.vat_rate,0)/100.0) ELSE 0 END), 0) AS inc,
               ROUND(SUM(CASE WHEN t.type='expense' THEN l.amount/(1+COALESCE(l.vat_rate,0)/100.0) ELSE 0 END), 0) AS exp
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE t.submodule IN ({",".join("?" * len(EXTRA_SUBMODULES))})
          AND l.year || '-' || printf('%02d', l.month) >= ?
          AND l.year || '-' || printf('%02d', l.month) <= ?
        GROUP BY l.year, l.month, t.submodule
    """, list(EXTRA_SUBMODULES) + [sa_window_from, sa_window_to]).fetchall()

    # Every month in both windows, unfiltered — the raw source.
    # Income and expense are kept apart so a submodule that holds both (parking
    # carries its rent and security alongside its takings) can be shown
    # broken down. A row labelled "Parking prihod" showing a negative net was
    # the single most confusing thing on the old screen.
    standalone_all = {}
    standalone_detail_all = {}
    for r in standalone_rows:
        ym  = f"{r['year']}-{r['month']:02d}"
        inc = float(r['inc'] or 0)
        exp = float(r['exp'] or 0)
        standalone_all.setdefault(ym, {})[r['submodule']] = inc - exp
        standalone_detail_all.setdefault(ym, {})[r['submodule']] = {
            'income': inc, 'expense': exp,
        }

    def _sa_map(month_key):
        """Stream-filtered map for one month: {sub: net or None}."""
        raw = standalone_all.get(month_key, {})
        out = {}
        for sub in EXTRA_SUBMODULES:
            out[sub] = raw[sub] if (sub in raw and sub in selected_streams) else None
        return out

    def _sa_detail(month_key):
        """Stream-filtered income/expense split, only where both are non-zero."""
        raw = standalone_detail_all.get(month_key, {})
        out = {}
        for sub in EXTRA_SUBMODULES:
            d = raw.get(sub)
            if d and sub in selected_streams and d['income'] and d['expense']:
                out[sub] = d
        return out

    def _sa_total(month_key):
        return sum(v for v in _sa_map(month_key).values() if v is not None)

    # Only the months inside the current filter are handed to the template.
    current_months = sorted({e['month'] for e in events})
    standalone_by_month = {mk: _sa_map(mk) for mk in current_months}
    standalone_detail_by_month = {mk: _sa_detail(mk) for mk in current_months}

    period_standalone_total = sum(_sa_total(mk) for mk in current_months)

    # Same streams over the comparison window, for a like-for-like YoY.
    yoy_months = sorted({e['month'] for e in yoy_events})
    period_yoy_standalone = sum(_sa_total(mk) for mk in yoy_months)

    # ── Group and aggregate ───────────────────────────────────────────
    if granularity == "monthly":
        # Group by YYYY-MM
        grouped_data = []
        prev_item = None
        for month_key, month_iter in groupby(events, key=lambda e: e['month']):
            month_list = list(month_iter)
            agg = _aggregate_events(month_list)
            venue_split = _build_venue_split(month_list)

            # Venue result = bruto + allocated fixed. If the fiksni stream is
            # switched off the venue result is simply the gross profit.
            venue_result = agg['gross_profit'] if 'fiksni' not in selected_streams else agg['net_profit']

            standalone_m       = standalone_by_month.get(month_key, {})
            standalone_total_m = sum(v for v in standalone_m.values() if v is not None)
            total_net          = venue_result + standalone_total_m

            # YoY comparison — same measure on both sides
            yoy = yoy_by_month.get(month_key)
            yoy_prev_key   = _prev_year_key(month_key)
            yoy_standalone = _sa_total(yoy_prev_key) if (yoy and yoy_prev_key) else 0
            if yoy:
                yoy_venue_result = (yoy['gross_profit'] if 'fiksni' not in selected_streams
                                    else yoy['net_profit'])
                yoy_total_net = yoy_venue_result + yoy_standalone
            else:
                yoy_venue_result = None
                yoy_total_net    = None

            yoy_revenue = _trend_arrow(agg['total_revenue'], yoy['total_revenue'] if yoy else None)
            yoy_venue   = _trend_arrow(venue_result, yoy_venue_result)
            yoy_profit  = _trend_arrow(total_net,    yoy_total_net)

            # MoM trend vs previous card — on the same company measure as the
            # headline, not on the venue-only figure.
            mom_revenue = _trend_arrow(agg['total_revenue'],
                                       prev_item['agg']['total_revenue'] if prev_item else None)
            mom_profit  = _trend_arrow(total_net,
                                       prev_item['total_net'] if prev_item else None)

            # Collect distinct event types for this month
            event_type_counts = {}
            for e in month_list:
                et = (e.get('event_type') or '').strip()
                if et and et.lower() not in CLOSED_TYPES:
                    event_type_counts[et] = event_type_counts.get(et, 0) + 1

            item = {
                'key':               month_key,
                'label':             _month_label(month_key),
                'standalone_detail': standalone_detail_by_month.get(month_key, {}),
                'events':            month_list,
                'agg':               agg,
                'venue_split':       venue_split,
                'yoy':               yoy,
                'yoy_revenue_pct':   yoy_revenue,
                'yoy_venue_pct':     yoy_venue,
                'yoy_profit_pct':    yoy_profit,
                'yoy_venue_result':  yoy_venue_result,
                'yoy_standalone':    yoy_standalone,
                'yoy_total_net':     yoy_total_net,
                'mom_revenue_pct':   mom_revenue,
                'mom_profit_pct':    mom_profit,
                'event_type_counts': event_type_counts,
                'standalone':        standalone_m,
                'standalone_total':  standalone_total_m,
                'venue_result':      venue_result,
                'total_net':         total_net,
            }
            grouped_data.append(item)
            prev_item = item

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
                'venue_result':  agg['net_profit'],
                # A day never carries dodatni prihodi — those are monthly.
                'standalone':        {},
                'standalone_detail': {},
                'standalone_total':  0,
                'total_net':     agg['net_profit'],
                'has_data':      agg['total_event_rows'] > 0,
            })

    # ── Overall period summary (top KPIs) ─────────────────────────────
    period_agg = _aggregate_events(events) if events else None
    period_venue_split = _build_venue_split(events) if events else None
    period_yoy = _aggregate_events(yoy_events) if yoy_events else None

    # Three measures, kept apart on purpose:
    #   venue result  = what Garden + Corner made
    #   standalone    = dodatni prihodi i troškovi (no venue)
    #   company net   = the two added together — the headline
    period_venue_result = None
    period_company_net  = None
    period_margin       = None
    if period_agg:
        period_venue_result = (period_agg['gross_profit'] if 'fiksni' not in selected_streams
                               else period_agg['net_profit'])
        period_company_net  = period_venue_result + period_standalone_total
        if period_agg['total_revenue']:
            period_margin = period_company_net / period_agg['total_revenue'] * 100

    period_yoy_venue_result = None
    period_yoy_company_net  = None
    if period_yoy:
        period_yoy_venue_result = (period_yoy['gross_profit'] if 'fiksni' not in selected_streams
                                   else period_yoy['net_profit'])
        period_yoy_company_net  = period_yoy_venue_result + period_yoy_standalone

    # Percentages only where the comparison window is sound.
    period_yoy_revenue_pct = None
    period_yoy_venue_pct   = None
    period_yoy_profit_pct  = None
    if period_yoy_valid and period_agg and period_yoy:
        period_yoy_revenue_pct = _trend_arrow(period_agg['total_revenue'], period_yoy['total_revenue'])
        period_yoy_venue_pct   = _trend_arrow(period_venue_result, period_yoy_venue_result)
        period_yoy_profit_pct  = _trend_arrow(period_company_net,  period_yoy_company_net)

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
        period_yoy_valid=period_yoy_valid,
        period_yoy_note=period_yoy_note,
        period_yoy_from=yoy_from,
        period_yoy_to=yoy_to,
        period_yoy_revenue_pct=period_yoy_revenue_pct,
        period_yoy_venue_pct=period_yoy_venue_pct,
        period_yoy_profit_pct=period_yoy_profit_pct,
        period_venue_result=period_venue_result,
        period_company_net=period_company_net,
        period_margin=period_margin,
        period_yoy_venue_result=period_yoy_venue_result,
        period_yoy_standalone=period_yoy_standalone,
        period_yoy_company_net=period_yoy_company_net,
        venue_display=VENUE_DISPLAY,
        today=today.isoformat(),
        standalone_by_month=standalone_by_month,
        standalone_detail_by_month=standalone_detail_by_month,
        period_standalone_total=period_standalone_total,
        selected_streams=selected_streams,
        all_streams=all_streams,
    )
