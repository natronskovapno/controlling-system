from flask import Blueprint, render_template, request
from db import get_db
from datetime import date
from i18n import month_name, day_name, months as month_list

bp = Blueprint("event_type_analysis", __name__, url_prefix="/event-type-analysis")

# Month names come from i18n so they follow the active language.

def _month_label(month_str):
    """Convert YYYY-MM to 'mesec YYYY' in Serbian."""
    try:
        year, month = month_str.split('-')
        month_idx = int(month) - 1
        return f"{month_name(month_idx + 1)} {year}"
    except:
        return month_str

@bp.route("/")
def index():
    db = get_db()
    
    # Filters
    date_from = request.args.get("date_from", "2024-01-01")
    date_to = request.args.get("date_to", date.today().isoformat())
    venue_filter = request.args.get("venue", "all")
    min_count = int(request.args.get("min_count", 0))
    
    # Build SQL for fetching raw data
    # CRITICAL FIX: Use TOTAL REVENUE, not just bar sales
    # Total revenue = sales_net + business_bills_net - third_party + venue_rent + tickets + other
    sql = """
        SELECT 
            strftime('%Y-%m', date) as month,
            LOWER(TRIM(event_type)) as event_type,
            COUNT(*) as event_count,
            
            -- TOTAL REVENUES (matching Daily Log calculation)
            AVG(
                COALESCE(sales_net_override, sales_net_auto, 0) +
                COALESCE(business_bills_override, business_bills_net_auto, 0) -
                COALESCE(third_party_override, third_party_auto, 0) +
                COALESCE(venue_rent_override, venue_rent, 0) +
                COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                COALESCE(other_revenues, 0)
            ) as avg_total_revenue,
            
            SUM(
                COALESCE(sales_net_override, sales_net_auto, 0) +
                COALESCE(business_bills_override, business_bills_net_auto, 0) -
                COALESCE(third_party_override, third_party_auto, 0) +
                COALESCE(venue_rent_override, venue_rent, 0) +
                COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
                COALESCE(other_revenues, 0)
            ) as total_revenue_sum,
            
            -- COGS
            AVG(COALESCE(cogs_override, cogs_auto)) as avg_cogs,
            SUM(COALESCE(cogs_override, cogs_auto)) as total_cogs,
            
            -- Wages (bar workers - konobar)
            AVG(COALESCE(wages_override, wages_auto)) as avg_wages,
            SUM(COALESCE(wages_override, wages_auto)) as total_wages,
            
            -- Laborers (physical workers - fizikalac)
            AVG(COALESCE(laborers_override, laborers_auto)) as avg_laborers,
            SUM(COALESCE(laborers_override, laborers_auto)) as total_laborers,
            
            -- Fixed costs - override to 0 for closed events
            AVG(CASE 
                WHEN LOWER(TRIM(event_type)) IN ('lokal ne radi', 'zatvoreno', 'closed') 
                THEN 0 
                ELSE COALESCE(fixed_profit, 0)
            END) as avg_fixed_costs,
            SUM(CASE 
                WHEN LOWER(TRIM(event_type)) IN ('lokal ne radi', 'zatvoreno', 'closed') 
                THEN 0 
                ELSE COALESCE(fixed_profit, 0)
            END) as total_fixed_costs
        FROM events_daily_log
        WHERE date >= ? AND date <= ?
          AND event_type IS NOT NULL
          AND TRIM(event_type) != ''
    """
    
    params = [date_from, date_to]
    
    if venue_filter != "all":
        sql += " AND LOWER(venue) = LOWER(?)"
        params.append(venue_filter)
    
    sql += """
        GROUP BY month, LOWER(TRIM(event_type))
        ORDER BY month, LOWER(TRIM(event_type))
    """
    
    rows = db.execute(sql, params).fetchall()
    
    # Convert to dictionaries
    data = [dict(r) for r in rows]
    
    # Calculate derived metrics for each row
    for row in data:
        # Use total revenue instead of just sales
        revenue = row['avg_total_revenue'] or 0
        cogs = row['avg_cogs'] or 0
        wages = row['avg_wages'] or 0
        laborers = row['avg_laborers'] or 0
        fixed = row['avg_fixed_costs'] or 0
        
        row['avg_gross_profit'] = revenue - cogs - wages - laborers
        row['avg_net_profit'] = row['avg_gross_profit'] + fixed
        
        # Total metrics
        total_revenue = row['total_revenue_sum'] or 0
        total_cogs = row['total_cogs'] or 0
        total_wages = row['total_wages'] or 0
        total_laborers = row['total_laborers'] or 0
        total_fixed = row['total_fixed_costs'] or 0
        
        row['total_gross_profit'] = total_revenue - total_cogs - total_wages - total_laborers
        row['total_net_profit'] = row['total_gross_profit'] + total_fixed
        
        # Percentages (avoid division by zero)
        if revenue > 0:
            row['cogs_pct'] = (cogs / revenue) * 100
            row['labor_pct'] = ((wages + laborers) / revenue) * 100
            row['gross_margin_pct'] = (row['avg_gross_profit'] / revenue) * 100
        else:
            row['cogs_pct'] = None
            row['labor_pct'] = None
            row['gross_margin_pct'] = None
    
    # Get unique months and event types (now already normalized to lowercase)
    months = sorted(list(set(r['month'] for r in data)))
    event_types = sorted(list(set(r['event_type'] for r in data if r['event_type'])))
    
    # Apply min_count filter to event types
    if min_count > 0:
        # Count total events per type across all months
        type_counts = {}
        for row in data:
            et = row['event_type']
            if et:
                type_counts[et] = type_counts.get(et, 0) + row['event_count']
        
        # Filter event types
        event_types = [et for et in event_types if type_counts.get(et, 0) >= min_count]
    
    # Build pivot tables for each metric
    def build_pivot(metric_key, is_total=False):
        """Build a pivot table: months × event_types."""
        pivot = {}
        
        for month in months:
            pivot[month] = {}
            month_totals = {'count': 0, 'value': 0}
            
            for et in event_types:
                # Find the row for this month + event_type
                matching = [r for r in data if r['month'] == month and r['event_type'] == et]
                
                if matching:
                    row = matching[0]
                    value = row[metric_key]
                    count = row['event_count']
                    
                    pivot[month][et] = value
                    
                    if is_total:
                        # For totals, sum the values
                        month_totals['value'] += (value or 0)
                        month_totals['count'] += count
                    else:
                        # For averages, we need weighted average
                        # Store both sum and count, calculate average later
                        if 'weighted_sum' not in month_totals:
                            month_totals['weighted_sum'] = {}
                        
                        if value is not None:
                            month_totals['weighted_sum'][et] = {
                                'sum': (value or 0) * count,
                                'count': count
                            }
                else:
                    pivot[month][et] = None
            
            # Calculate month grand total
            if is_total:
                pivot[month]['_total'] = month_totals['value']
            else:
                # Weighted average across all event types in this month
                total_sum = sum(v['sum'] for v in month_totals.get('weighted_sum', {}).values())
                total_count = sum(v['count'] for v in month_totals.get('weighted_sum', {}).values())
                pivot[month]['_total'] = (total_sum / total_count) if total_count > 0 else None
        
        # Calculate grand totals (bottom row)
        grand_total = {}
        for et in event_types:
            if is_total:
                # Sum across all months
                grand_total[et] = sum(pivot[m].get(et) or 0 for m in months)
            else:
                # Weighted average across all months
                total_sum = 0
                total_count = 0
                for month in months:
                    matching = [r for r in data if r['month'] == month and r['event_type'] == et]
                    if matching:
                        row = matching[0]
                        value = row[metric_key]
                        count = row['event_count']
                        if value is not None:
                            total_sum += value * count
                            total_count += count
                
                grand_total[et] = (total_sum / total_count) if total_count > 0 else None
        
        # Grand total of grand totals
        if is_total:
            grand_total['_total'] = sum(grand_total.get(et) or 0 for et in event_types)
        else:
            # Overall weighted average
            all_sum = sum(grand_total.get(et, 0) * sum(r['event_count'] for r in data if r['event_type'] == et) 
                         for et in event_types if grand_total.get(et) is not None)
            all_count = sum(sum(r['event_count'] for r in data if r['event_type'] == et) 
                           for et in event_types if grand_total.get(et) is not None)
            grand_total['_total'] = (all_sum / all_count) if all_count > 0 else None
        
        pivot['_grand_total'] = grand_total
        
        return pivot
    
    # Build all pivot tables
    pivots = {
        'event_count': build_pivot('event_count', is_total=True),
        'avg_revenue': build_pivot('avg_total_revenue'),  # Changed from avg_sales
        'avg_cogs': build_pivot('avg_cogs'),
        'avg_wages': build_pivot('avg_wages'),
        'avg_laborers': build_pivot('avg_laborers'),
        'avg_fixed_costs': build_pivot('avg_fixed_costs'),
        'avg_gross_profit': build_pivot('avg_gross_profit'),
        'avg_net_profit': build_pivot('avg_net_profit'),
        'total_net_profit': build_pivot('total_net_profit', is_total=True),
        'cogs_pct': build_pivot('cogs_pct'),
        'labor_pct': build_pivot('labor_pct'),
        'gross_margin_pct': build_pivot('gross_margin_pct'),
    }
    
    # Get all event types for the event type selector (normalized)
    all_event_types = db.execute("""
        SELECT DISTINCT LOWER(TRIM(event_type)) as event_type
        FROM events_daily_log 
        WHERE event_type IS NOT NULL 
          AND TRIM(event_type) != ''
        ORDER BY LOWER(TRIM(event_type))
    """).fetchall()
    all_event_types = [r['event_type'] for r in all_event_types]
    
    return render_template("event_type_analysis/index.html",
        pivots=pivots,
        months=months,
        event_types=event_types,
        all_event_types=all_event_types,
        month_label=_month_label,
        filters={
            'date_from': date_from,
            'date_to': date_to,
            'venue': venue_filter,
            'min_count': min_count
        })

@bp.route("/drill/<month>/<event_type>")
def drill_down(month, event_type):
    """Show individual events for a specific month and event type."""
    db = get_db()
    
    venue_filter = request.args.get("venue", "all")
    
    # Parse month (YYYY-MM)
    year, month_num = month.split('-')
    month_start = f"{year}-{month_num}-01"
    
    # Calculate month_end
    if month_num == "12":
        month_end = f"{int(year)+1}-01-01"
    else:
        month_end = f"{year}-{int(month_num)+1:02d}-01"
    
    # Match event_type case-insensitively
    sql = """
        SELECT 
            event_id,
            date,
            venue,
            organiser,
            event_name,
            event_type,
            -- Calculate total revenue (same as Daily Log)
            (COALESCE(sales_net_override, sales_net_auto, 0) +
             COALESCE(business_bills_override, business_bills_net_auto, 0) -
             COALESCE(third_party_override, third_party_auto, 0) +
             COALESCE(venue_rent_override, venue_rent, 0) +
             COALESCE(ticket_sales_own_override, ticket_sales_own, 0) +
             COALESCE(other_revenues, 0)) as total_revenue,
            COALESCE(cogs_override, cogs_auto) as cogs,
            COALESCE(wages_override, wages_auto) as wages,
            COALESCE(laborers_override, laborers_auto) as laborers,
            fixed_profit
        FROM events_daily_log
        WHERE date >= ? AND date < ?
          AND LOWER(TRIM(event_type)) = LOWER(TRIM(?))
    """
    
    params = [month_start, month_end, event_type]
    
    if venue_filter != "all":
        sql += " AND LOWER(venue) = LOWER(?)"
        params.append(venue_filter)
    
    sql += " ORDER BY date"
    
    events = db.execute(sql, params).fetchall()
    events = [dict(e) for e in events]
    
    # Calculate derived metrics
    for evt in events:
        revenue = evt['total_revenue'] or 0
        cogs = evt['cogs'] or 0
        wages = evt['wages'] or 0
        laborers = evt['laborers'] or 0
        fixed = evt['fixed_profit'] or 0
        
        evt['gross_profit'] = revenue - cogs - wages - laborers
        evt['net_profit'] = evt['gross_profit'] + fixed
    
    return render_template("event_type_analysis/drill_down.html",
        events=events,
        month=_month_label(month),
        event_type=event_type,
        venue_filter=venue_filter)
