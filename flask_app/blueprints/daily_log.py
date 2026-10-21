from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from datetime import date, datetime
from itertools import groupby
from i18n import tr
from i18n import month_name, day_name, months as month_list

bp = Blueprint("daily_log", __name__)

def _anchor_redirect(url, row_id):
    """Append #row-{id} anchor so the browser scrolls back to the edited row."""
    base = url.split('#')[0]
    return redirect(f"{base}#row-{row_id}")

def _referrer_anchor(row_id, fallback):
    base = (request.referrer or fallback).split('#')[0]
    return redirect(f"{base}#row-{row_id}")


VENUES = ["Garden", "Corner"]
VENUE_DISPLAY = {"Garden": "Garden", "Corner": "Corner", "both": "Both", "both_venues": "Both venues"}
# Month names come from i18n so they follow the active language.

# ── helpers ───────────────────────────────────────────────────────────

def _sf(v):
    try: return float(v) if v not in (None,"","None") else None
    except: return None

def _si(v):
    try: return int(v) if v not in (None,"","None") else None
    except: return None

def _month_year(d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{month_name(dt.month)} {dt.year}"
    except: return d[:7]

def _vbadge(venue):
    v = (venue or "").lower()
    if v == "Garden": return "badge-blue"
    if v == "Corner": return "badge-amber"
    if v == "both":  return "badge-green"
    return "badge-gray"

def _vlabel(venue):
    return VENUE_DISPLAY.get((venue or "").lower(), venue or "—")

def _get_event_types(db):
    try:
        return [r["name"] for r in db.execute(
            "SELECT name FROM event_type_tags ORDER BY name"
        ).fetchall()]
    except:
        return [
            "Concert", "DJ night", "Festival day", "Private rental",
            "Theatre", "Film screening", "Market day", "Conference",
            "Regular day",
        ]

def _ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS event_type_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS passthrough_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        note TEXT, created_at TEXT DEFAULT (datetime('now')))""")
    for t in [
        "Concert", "DJ night", "Festival day", "Private rental",
        "Theatre", "Film screening", "Market day", "Conference",
        "Regular day",
    ]:
        db.execute("INSERT OR IGNORE INTO event_type_tags (name) VALUES (?)", [t])
    db.execute("INSERT OR IGNORE INTO passthrough_products (name) VALUES (?)",
               ["Tikets karta - ulaznica"])
    db.commit()

def _get_daily_fixed_costs(target_date, venue, all_day_events, db):
    """
    Calculate per-event fixed profit allocation (venue-aware, 2026-06-05).

    Shared operativni costs split 50/50 between Garden and Corner upfront.
    Each venue's half + venue-specific costs divided by that venue's open days,
    then by open events at that venue today.
    Non-operativni submodules excluded. Closed events return None.
    """
    CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}

    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(target_date, "%Y-%m-%d")
        year  = dt.year
        month = dt.month
        venue_lower = (venue or '').lower().strip()

        if venue_lower not in ('garden', 'corner'):
            return None

        month_start = f"{year:04d}-{month:02d}-01"
        month_end   = f"{year+1:04d}-01-01" if month == 12 else f"{year:04d}-{month+1:02d}-01"

        def _monthly_net(extra_where, params):
            row = db.execute(f"""
                SELECT
                    SUM(CASE WHEN t.type='income'  THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) as inc,
                    SUM(CASE WHEN t.type='expense' THEN ROUND(l.amount/(1+COALESCE(l.vat_rate,0)/100.0),2) ELSE 0 END) as exp
                FROM invoice_fixed_cost_links l
                JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
                WHERE l.year=? AND l.month=? AND t.submodule='operativni' {extra_where}
            """, [year, month] + params).fetchone()
            if row and (row['inc'] or row['exp']):
                return float(row['inc'] or 0) - float(row['exp'] or 0)
            return 0.0

        shared_net  = _monthly_net("AND LOWER(t.venue)='shared'", [])
        venue_net   = _monthly_net("AND LOWER(t.venue)=?", [venue_lower])
        venue_total = (shared_net * 0.5) + venue_net

        if venue_total == 0:
            return None

        # Open days for this venue this month
        venue_open_days = db.execute("""
            SELECT COUNT(DISTINCT date) as c FROM events_daily_log
            WHERE date >= ? AND date < ?
              AND LOWER(COALESCE(venue,'')) = ?
              AND LOWER(COALESCE(event_type,'')) NOT IN ('lokal ne radi','zatvoreno','closed')
        """, [month_start, month_end, venue_lower]).fetchone()['c'] or 0

        if venue_open_days == 0:
            return None

        venue_daily = venue_total / venue_open_days

        # Open events at this venue today
        venue_open_today = sum(
            1 for e in (all_day_events or [])
            if (e.get('event_type') or '').strip().lower() not in CLOSED_TYPES
               and (e.get('venue') or '').strip().lower() == venue_lower
        )

        if venue_open_today == 0:
            return None

        per_event = venue_daily / venue_open_today
        return round(per_event, 2) if per_event != 0 else None

    except Exception:
        return None

# ── financial calculation engine ──────────────────────────────────────

def _hour_cutoff(event_end_str):
    """Convert event_end time string to hour integer cutoff."""
    if not event_end_str:
        return None
    try:
        t = event_end_str.strip()[:5]
        h = int(t.split(":")[0])
        return h
    except:
        return None

def _effective_date_hours(target_date, venue, db):
    """
    Return sales_hourly rows for an effective day (03:00 to 02:59 next day).
    Midnight rule: hours 0,1,2 belong to the PREVIOUS calendar day.
    So for target_date, we pull:
      - hours 3-23 on target_date
      - hours 0,1,2 on target_date + 1 day
    """
    from datetime import timedelta, date as date_cls
    try:
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        next_d = (d + timedelta(days=1)).isoformat()
    except:
        return []

    rows = db.execute("""
        SELECT hour_slot, product_name, category_id,
               quantity, revenue_net, revenue_gross
        FROM sales_hourly
        WHERE (date = ? AND hour_slot >= 3 AND LOWER(venue) = LOWER(?))
           OR (date = ? AND hour_slot < 3  AND LOWER(venue) = LOWER(?))
        ORDER BY hour_slot
    """, [target_date, venue, next_d, venue]).fetchall()
    return [dict(r) for r in rows]

def _effective_bills(target_date, venue, db):
    """
    Return bills_raw rows for an effective day (03:00 to 02:59 next day).
    PRIMARY DATA SOURCE for revenue calculation (2026-04-16 fix).
    bills_raw.amount is authoritative - POS API line items are incomplete.
    """
    from datetime import timedelta
    try:
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        next_d = (d + timedelta(days=1)).isoformat()
    except:
        return []

    rows = db.execute("""
        SELECT b.bill_id, b.hour_slot, b.amount, b.table_name, 
               b.business_partner_name
        FROM bills_raw b
        WHERE b.is_revoked = 0
          AND (
            (b.date = ? AND b.hour_slot >= 3 AND LOWER(b.venue) = LOWER(?))
            OR (b.date = ? AND b.hour_slot < 3 AND LOWER(b.venue) = LOWER(?))
          )
    """, [target_date, venue, next_d, venue]).fetchall()
    return [dict(r) for r in rows]

def _effective_cogs_hours(target_date, venue, db):
    """Same midnight logic but from bill_items_raw."""
    from datetime import timedelta
    try:
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        next_d = (d + timedelta(days=1)).isoformat()
    except:
        return []
    rows = db.execute("""
        SELECT b.hour_slot, i.total_cogs_net, i.total_price
        FROM bill_items_raw i
        JOIN bills_raw b ON b.bill_id = i.bill_id
        WHERE b.is_revoked = 0
          AND (
            (b.date = ? AND b.hour_slot >= 3 AND LOWER(b.venue) = LOWER(?))
            OR (b.date = ? AND b.hour_slot < 3 AND LOWER(b.venue) = LOWER(?))
          )
    """, [target_date, venue, next_d, venue]).fetchall()
    return [dict(r) for r in rows]

def _get_daily_cogs(target_date, db):
    """
    Get total COGS for a date from bill_items_raw table.
    
    bill_items_raw from /BillCumulative/TO/Search is more accurate than cogs_daily
    (154K vs 135K RSD on test date, -1.6% vs -14.0% variance to Lager lista).
    
    Note: This captures POS sales COGS only (~98.4% of total inventory movement).
    The remaining 1.6% represents non-sale movements (writeoffs, spoilage, internal use)
    which appear in Lager lista but not in POS transactions - this is expected.
    
    Returns total cogs_net for the date across all warehouses.
    COGS is not split by venue since items come from shared warehouse.
    """
    try:
        row = db.execute("""
            SELECT ROUND(SUM(total_cogs_net), 2) AS total_cogs
            FROM bill_items_raw
            WHERE date = ?
        """, [target_date]).fetchone()
        return float(row['total_cogs']) if row and row['total_cogs'] else None
    except Exception as e:
        # Table might not exist or query failed
        return None

def _split_by_cutoff(rows, cutoffs, hour_key='hour_slot'):
    """
    Split rows into buckets by hour cutoffs.
    cutoffs: sorted list of (event_id, cutoff_hour) in time order.
    Last event gets all remaining hours.
    Hours 0,1,2 are treated as hour 24,25,26 for sorting purposes.
    """
    def effective_hour(h):
        return h + 24 if h < 3 else h

    buckets = {eid: [] for eid, _ in cutoffs}
    buckets['last'] = []

    for row in rows:
        h = effective_hour(row[hour_key])
        assigned = False
        for eid, cutoff in cutoffs[:-1]:
            eff_cutoff = effective_hour(cutoff)
            if h <= eff_cutoff:
                buckets[eid].append(row)
                assigned = True
                break
        if not assigned:
            last_eid = cutoffs[-1][0]
            buckets[last_eid].append(row)

    return buckets

def _calc_financials(event, db, all_day_events=None):
    """
    Calculate all auto financial values for an event.
    all_day_events: list of ALL events on same day (all venues) — used for wage splitting.
    Sales cutoff logic uses only same-venue events.
    Returns dict of auto values.
    """
    target_date = event['date']
    venue = event['venue']
    event_id = event['event_id']

    # Passthrough product names
    try:
        passthrough = {r['name'] for r in db.execute(
            "SELECT name FROM passthrough_products"
        ).fetchall()}
    except:
        passthrough = {'Tikets karta - ulaznica'}

    # Same-venue events only — used for sales cutoff split
    same_venue_events = [e for e in (all_day_events or [])
                         if (e.get('venue') or '').lower() == venue.lower()]

    # Determine if multi-event day (same venue, for sales cutoff)
    is_multi = same_venue_events and len(same_venue_events) > 1

    if is_multi:
        # Build cutoffs from event_end times, sorted (same-venue only)
        cutoffs = []
        for e in sorted(same_venue_events, key=lambda x: (
            _hour_cutoff(x['event_end']) + 24
            if (_hour_cutoff(x['event_end']) or 24) < 3
            else (_hour_cutoff(x['event_end']) or 24)
        )):
            cutoffs.append((e['event_id'], _hour_cutoff(e['event_end'])))

        # Get all data for the day
        all_bills = _effective_bills(target_date, venue, db)
        all_sh    = _effective_date_hours(target_date, venue, db)
        all_cogs  = _effective_cogs_hours(target_date, venue, db)

        # Split by cutoff
        valid_cutoffs = [(eid, c) for eid, c in cutoffs if c is not None]
        if len(valid_cutoffs) == len(cutoffs):
            bills_buckets = _split_by_cutoff(all_bills, cutoffs)
            sh_buckets    = _split_by_cutoff(all_sh, cutoffs)
            cogs_buckets  = _split_by_cutoff(all_cogs, cutoffs)
            
            my_bills = bills_buckets.get(event_id, [])
            my_sh    = sh_buckets.get(event_id, [])
            my_cogs  = cogs_buckets.get(event_id, [])
        else:
            # Some events missing event_end — cannot split, flag it
            return {
                'sales_net_auto': None,
                'cogs_auto': None,
                'wages_auto': None,
                'laborers_auto': None,
                'business_bills_net_auto': None,
                'third_party_auto': None,
                '_cutoff_warning': True,
            }
    else:
        my_bills = _effective_bills(target_date, venue, db)
        my_sh    = _effective_date_hours(target_date, venue, db)
        my_cogs  = _effective_cogs_hours(target_date, venue, db)

    # ═══════════════════════════════════════════════════════════════════
    # PRIMARY REVENUE SOURCE: bills_raw.amount (AUTHORITATIVE)
    # Changed 2026-04-16: POS API returns incomplete line items
    # bill_items_raw missing 5-9% of items → use bills_raw.amount instead
    # ═══════════════════════════════════════════════════════════════════
    
    # Total gross revenue from all bills (filtered for event hours via cutoff)
    total_gross = sum(b['amount'] for b in my_bills) or 0
    
    # Organizacija (B2B bills) — identified by table_name or business_partner_name
    org_gross = sum(
        b['amount'] for b in my_bills
        if (
            (b['table_name'] or '').lower().startswith('organizacija')
            or (b['business_partner_name'] and b['business_partner_name'].strip())
        )
    ) or 0
    
    # Bar revenue = Total - Organizacija (both gross)
    bar_gross = total_gross - org_gross
    
    # Convert to net (÷ 1.2 for 20% VAT)
    bar_net = round(bar_gross / 1.2, 2) if bar_gross else 0
    org_net = round(org_gross / 1.2, 2) if org_gross else 0
    
    # Final values
    sales_net = bar_net or None
    business_bills = org_net or None

    # On multi-event days, split business bills equally
    if is_multi and business_bills:
        business_bills = round(business_bills / len(same_venue_events), 2)

    # Third-party pass-through: STILL from sales_hourly (product-specific)
    # (bills_raw doesn't have product detail, need sales_hourly for this)
    third_party = sum(
        r['revenue_net'] for r in my_sh
        if r['product_name'] in passthrough
    ) or None

    # Event type checks (used for both COGS and wages)
    CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}
    event_type_lower = (event.get('event_type') or '').strip().lower()

    # ═══════════════════════════════════════════════════════════════════
    # FIXED COSTS ALLOCATION (UPDATED 2026-04-23):
    # Allocate monthly fixed costs equally across all NON-CLOSED events
    # Closed venues ("lokal ne radi") receive NO fixed cost allocation
    # ═══════════════════════════════════════════════════════════════════
    
    if event_type_lower in CLOSED_TYPES:
        # Closed venues do not receive fixed cost allocation
        fixed_profit = None
    else:
        # Open venues receive equal share of monthly fixed costs
        fixed_profit = _get_daily_fixed_costs(target_date, event.get('venue'), all_day_events, db)

    # ═══════════════════════════════════════════════════════════════════
    # COGS CALCULATION (2026-04-16 FIX):
    # Use cogs_daily table (from dedicated API endpoint, ~97.5% accuracy)
    # Allocate proportionally across ALL company events based on revenue
    # Exclude closed venues from COGS allocation
    # ═══════════════════════════════════════════════════════════════════
    
    # Check if this event is a closed venue
    if event_type_lower in CLOSED_TYPES:
        # Closed venues have no sales activity, therefore no COGS
        cogs = None
    else:
        # Get total daily COGS from authoritative source (entire company)
        daily_cogs = _get_daily_cogs(target_date, db)
        
        if daily_cogs and all_day_events:
            # Calculate total company revenue from ALL open events (all venues)
            # We need to get bills for each event to calculate their revenue
            total_company_revenue = 0
            
            for e in all_day_events:
                e_type_lower = (e.get('event_type') or '').strip().lower()
                # Skip closed venues
                if e_type_lower in CLOSED_TYPES:
                    continue
                    
                # Get bills for this event
                e_venue = e.get('venue')
                e_date = e.get('date')
                
                # Check if this is a multi-event day at this venue
                same_venue_events_for_e = [ev for ev in all_day_events 
                                          if (ev.get('venue') or '').lower() == (e_venue or '').lower()]
                
                if len(same_venue_events_for_e) > 1:
                    # Multi-event at this venue - need to use cutoff logic
                    # For simplicity, if we're calculating for THIS event, use total_gross
                    # For other events, we need to calculate their revenue too
                    if e['event_id'] == event_id:
                        # This is the current event we're calculating
                        e_revenue = total_gross
                    else:
                        # Different event - need to get its bills with cutoff
                        # Build cutoffs for that venue
                        e_cutoffs = []
                        for ev in sorted(same_venue_events_for_e, key=lambda x: (
                            _hour_cutoff(x.get('event_end')) + 24
                            if (_hour_cutoff(x.get('event_end')) or 24) < 3
                            else (_hour_cutoff(x.get('event_end')) or 24)
                        )):
                            e_cutoffs.append((ev['event_id'], _hour_cutoff(ev.get('event_end'))))
                        
                        # Get all bills for that venue/date
                        e_all_bills = _effective_bills(e_date, e_venue, db)
                        
                        # Split by cutoff if all events have event_end
                        if all(c[1] is not None for c in e_cutoffs):
                            e_bills_buckets = _split_by_cutoff(e_all_bills, e_cutoffs)
                            e_bills = e_bills_buckets.get(e['event_id'], [])
                            e_revenue = sum(b['amount'] for b in e_bills) or 0
                        else:
                            # Can't split - skip this event's revenue (warning case)
                            e_revenue = 0
                else:
                    # Single event at this venue
                    e_bills = _effective_bills(e_date, e_venue, db)
                    e_revenue = sum(b['amount'] for b in e_bills) or 0
                
                total_company_revenue += e_revenue
            
            # This event's revenue
            event_revenue = total_gross
            
            # Proportional allocation across entire company
            if total_company_revenue > 0:
                revenue_share = event_revenue / total_company_revenue
                cogs = round(daily_cogs * revenue_share, 2)
            else:
                # Edge case: no revenue but COGS exists
                cogs = None
        elif daily_cogs:
            # Single event day (only this event exists)
            cogs = daily_cogs
        else:
            # No COGS data available for this date
            cogs = None

    # Wages — skip for closed venues; split only among open events on multi-event days
    if event_type_lower in CLOSED_TYPES:
        wages_v = laborers_v = None
    else:
        try:
            # Count open (non-closed) events on this day for correct split denominator
            open_events = [e for e in (all_day_events or [event])
                           if (e.get('event_type') or '').strip().lower() not in CLOSED_TYPES]
            n_open = max(len(open_events), 1)

            w_row = db.execute("""
                SELECT
                    SUM(CASE WHEN worker_type='konobar'   THEN COALESCE(gross_cost, net_amount) ELSE 0 END) as w,
                    SUM(CASE WHEN worker_type='fizikalac' THEN COALESCE(gross_cost, net_amount) ELSE 0 END) as l
                FROM wages_daily WHERE date = ?
            """, [target_date]).fetchone()
            wages_v    = float(w_row['w']) if w_row and w_row['w'] else None
            laborers_v = float(w_row['l']) if w_row and w_row['l'] else None

            if n_open > 1:
                wages_v    = round(wages_v / n_open, 2)    if wages_v    else None
                laborers_v = round(laborers_v / n_open, 2) if laborers_v else None
        except:
            wages_v = laborers_v = None

    return {
        'sales_net_auto':          sales_net,
        'business_bills_net_auto': business_bills,
        'third_party_auto':        third_party,
        'cogs_auto':               cogs,
        'wages_auto':              wages_v,
        'laborers_auto':           laborers_v,
        'fixed_profit':            fixed_profit,
        '_cutoff_warning':         False,
    }

def _effective_val(event, auto_key, override_key):
    """Return override if set, else auto."""
    ov = event.get(override_key)
    if ov is not None:
        return ov, True  # (value, is_override)
    au = event.get(auto_key)
    return au, False

def _compute_totals(event):
    """Compute total revenues, expenses, gross profit, net profit."""
    def ev(auto_k, ov_k):
        v, _ = _effective_val(event, auto_k, ov_k)
        return v or 0

    sales_net   = ev('sales_net_auto',          'sales_net_override')
    biz_bills   = ev('business_bills_net_auto', 'business_bills_override')
    third_party = ev('third_party_auto',        'third_party_override')
    venue_rent  = event.get('venue_rent_override') or event.get('venue_rent') or 0
    ticket_own  = event.get('ticket_sales_own_override') or event.get('ticket_sales_own') or 0
    other_rev   = event.get('other_revenues') or 0

    total_rev = sales_net + biz_bills - third_party + venue_rent + ticket_own + other_rev

    cogs        = ev('cogs_auto',      'cogs_override')
    wages       = ev('wages_auto',     'wages_override')
    laborers    = ev('laborers_auto',  'laborers_override')
    musicians   = event.get('musicians_fee') or 0
    equipment   = event.get('equipment_expenses') or 0
    advertising = event.get('advertising') or 0
    debit_card  = event.get('debit_card_expenses_variable') or 0
    other_exp   = event.get('other_expenses') or 0

    total_exp = cogs + wages + laborers + musicians + equipment + advertising + debit_card + other_exp

    gross_profit = (total_rev - total_exp) if (total_rev or total_exp) else None
    fixed = event.get('fixed_profit')
    net_profit = (gross_profit + fixed) if (gross_profit is not None and fixed is not None) else None

    return {
        'total_revenues': total_rev or None,
        'total_expenses': total_exp or None,
        'gross_profit':   gross_profit,
        'net_profit':     net_profit,
    }

def _save_auto_values(db, event_id, auto_vals):
    """Persist auto-calculated values to DB."""
    # Extract values, handling missing keys gracefully
    params = {
        'sales_net_auto': auto_vals.get('sales_net_auto'),
        'business_bills_net_auto': auto_vals.get('business_bills_net_auto'),
        'third_party_auto': auto_vals.get('third_party_auto'),
        'cogs_auto': auto_vals.get('cogs_auto'),
        'wages_auto': auto_vals.get('wages_auto'),
        'laborers_auto': auto_vals.get('laborers_auto'),
        'fixed_profit': auto_vals.get('fixed_profit'),
        'eid': event_id
    }
    
    db.execute("""
        UPDATE events_daily_log SET
            sales_net_auto          = :sales_net_auto,
            business_bills_net_auto = :business_bills_net_auto,
            third_party_auto        = :third_party_auto,
            cogs_auto               = :cogs_auto,
            wages_auto              = :wages_auto,
            laborers_auto           = :laborers_auto,
            fixed_profit            = :fixed_profit,
            last_modified_at        = datetime('now')
        WHERE event_id = :eid
    """, params)
    db.commit()

def _recalc_event(event_id, db):
    """Recalculate and save auto values for one event."""
    event = db.execute(
        "SELECT * FROM events_daily_log WHERE event_id=?", [event_id]
    ).fetchone()
    if not event:
        return
    event = dict(event)
    # Pass ALL events for the day (all venues) so wages split correctly
    # across total open events, not just same-venue events
    all_day = [dict(r) for r in db.execute("""
        SELECT * FROM events_daily_log
        WHERE date=?
        ORDER BY venue, event_id
    """, [event['date']]).fetchall()]

    auto_vals = _calc_financials(event, db, all_day)
    _save_auto_values(db, event_id, auto_vals)

# ── index — date-first grouped list ──────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    _ensure_tables(db)

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    venue_f   = request.args.get("venue", "")
    status_f  = request.args.get("status", "")
    search    = request.args.get("search", "")
    view      = request.args.get("view", "compact")  # 'compact' or 'detailed'

    sql = "SELECT * FROM events_daily_log WHERE 1=1"
    params = []
    if date_from: sql += " AND date >= ?";             params.append(date_from)
    if date_to:   sql += " AND date <= ?";             params.append(date_to)
    if venue_f:   sql += " AND LOWER(venue)=LOWER(?)"; params.append(venue_f)
    if status_f:  sql += " AND status=?";              params.append(status_f)
    if search:
        sql += " AND (organiser LIKE ? OR event_name LIKE ? OR event_type LIKE ?)"
        params += [f"%{search}%"]*3

    sql += " ORDER BY date DESC, LOWER(venue), event_id"

    page     = max(1, int(request.args.get("page", 1)))
    per_page = 60
    count    = db.execute(sql.replace("SELECT *","SELECT COUNT(*)"), params).fetchone()[0]
    total_pages = max(1, (count + per_page - 1) // per_page)
    offset   = (page - 1) * per_page

    events_raw = db.execute(sql + f" LIMIT {per_page} OFFSET {offset}", params).fetchall()
    events = [dict(e) for e in events_raw]

    # Attach computed totals
    for e in events:
        t = _compute_totals(e)
        e.update(t)
        e['_vbadge'] = _vbadge(e['venue'])
        e['_vlabel'] = _vlabel(e['venue'])

    # Group: date → venue → events
    date_groups = []
    for d, date_iter in groupby(events, key=lambda e: e['date']):
        date_events = list(date_iter)
        venue_groups = []
        for v, venue_iter in groupby(date_events, key=lambda e: (e['venue'] or '').lower()):
            venue_events = list(venue_iter)
            venue_groups.append({
                'venue':  v,
                'badge':  _vbadge(v),
                'label':  _vlabel(v),
                'events': venue_events,
            })
        date_groups.append({'date': d, 'venues': venue_groups})

    # For detailed view: attach effective (auto/override resolved) values per event
    if view == 'detailed':
        for e in events:
            def _ev(a, o, ev=e): v, _ = _effective_val(ev, a, o); return v or 0
            e['_sales_net']   = _ev('sales_net_auto',          'sales_net_override')
            e['_biz_bills']   = _ev('business_bills_net_auto', 'business_bills_override')
            e['_third_party'] = _ev('third_party_auto',        'third_party_override')
            e['_cogs']        = _ev('cogs_auto',               'cogs_override')
            e['_wages']       = _ev('wages_auto',              'wages_override')
            e['_laborers']    = _ev('laborers_auto',           'laborers_override')

    return render_template("daily_log/index.html",
        date_groups=date_groups, venues=VENUES,
        event_types=_get_event_types(db),
        filters=dict(date_from=date_from, date_to=date_to,
                     venue=venue_f, status=status_f, search=search),
        page=page, total_pages=total_pages, count=count,
        view=view, events=events)

# ── recalculate ───────────────────────────────────────────────────────

@bp.route("/<int:event_id>/recalc", methods=["POST"])
def recalc(event_id):
    db = get_db()
    _recalc_event(event_id, db)
    flash(tr("Finansijski podaci su ažurirani iz izvora."), "success")
    back = request.form.get("back_to", "")
    return redirect(back or url_for("daily_log.detail", event_id=event_id))

@bp.route("/recalc-all", methods=["POST"])
def recalc_all():
    db = get_db()
    events = db.execute("SELECT event_id FROM events_daily_log").fetchall()
    for e in events:
        _recalc_event(e['event_id'], db)
    flash(tr("Ažurirani finansijski podaci za {a0} događaja.", a0=len(events)), "success")
    return redirect(url_for("daily_log.index"))

@bp.route("/recalc-new", methods=["POST"])
def recalc_new():
    """Recalculate only events where all auto columns are NULL (never calculated)."""
    db = get_db()
    events = db.execute("""
        SELECT event_id FROM events_daily_log
        WHERE sales_net_auto IS NULL
          AND cogs_auto IS NULL
          AND wages_auto IS NULL
    """).fetchall()
    for e in events:
        _recalc_event(e['event_id'], db)
    flash(tr("Preračunato {a0} novih događaja.", a0=len(events)), "success")
    return redirect(url_for("daily_log.index"))

@bp.route("/recalc-period", methods=["POST"])
def recalc_period():
    """Recalculate events within a date range."""
    db = get_db()
    date_from = request.form.get("period_from", "")
    date_to   = request.form.get("period_to", "")
    if not date_from or not date_to:
        flash(tr("Unesite oba datuma za period."), "error")
        return redirect(url_for("daily_log.index"))
    events = db.execute("""
        SELECT event_id FROM events_daily_log
        WHERE date >= ? AND date <= ?
    """, [date_from, date_to]).fetchall()
    for e in events:
        _recalc_event(e['event_id'], db)
    flash(tr("Preračunato {a0} događaja ({date_from} → {date_to}).", a0=len(events), date_from=date_from, date_to=date_to), "success")
    return redirect(url_for("daily_log.index"))

@bp.route("/recalc-fixed-costs", methods=["POST"])
def recalc_fixed_costs():
    """
    Recalculate ONLY fixed_profit for all events.
    Useful after updating fixed costs module allocations.
    """
    db = get_db()
    events = db.execute("SELECT event_id, date, event_type, venue FROM events_daily_log ORDER BY date").fetchall()
    
    CLOSED_TYPES = {'lokal ne radi', 'zatvoreno', 'closed'}
    updated_count = 0
    
    for e in events:
        # Skip closed events - they should never have fixed costs
        if (e['event_type'] or '').strip().lower() in CLOSED_TYPES:
            # Ensure it's NULL in database
            db.execute("""
                UPDATE events_daily_log 
                SET fixed_profit = NULL,
                    last_modified_at = datetime('now')
                WHERE event_id = ?
            """, [e['event_id']])
            updated_count += 1
            continue
        
        # Get all events on same day for proper allocation
        all_day = [dict(r) for r in db.execute("""
            SELECT * FROM events_daily_log
            WHERE date=?
            ORDER BY venue, event_id
        """, [e['date']]).fetchall()]
        
        # Calculate fixed profit only
        fixed_profit = _get_daily_fixed_costs(e['date'], e['venue'], all_day, db)
        
        # Update just this field
        db.execute("""
            UPDATE events_daily_log 
            SET fixed_profit = ?,
                last_modified_at = datetime('now')
            WHERE event_id = ?
        """, [fixed_profit, e['event_id']])
        updated_count += 1
    
    db.commit()
    flash(tr("Ažurirani fiksni troškovi za {updated_count} događaja.", updated_count=updated_count), "success")
    return redirect(url_for("daily_log.index"))

# ── sales breakdown ───────────────────────────────────────────────────

@bp.route("/<int:event_id>/sales")
def sales_breakdown(event_id):
    db = get_db()
    event = db.execute(
        "SELECT * FROM events_daily_log WHERE event_id=?", [event_id]
    ).fetchone()
    if not event:
        flash(tr("Događaj nije pronađen."), "error")
        return redirect(url_for("daily_log.index"))
    event = dict(event)

    from datetime import timedelta
    try:
        next_d = (datetime.strptime(event['date'], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except:
        next_d = event['date']

    # Get all hourly rows for this event (midnight rule applied)
    try:
        rows = db.execute("""
            SELECT product_name, category_id,
                   SUM(quantity) as qty,
                   SUM(revenue_net) as net,
                   SUM(revenue_gross) as gross,
                   hour_slot
            FROM sales_hourly
            WHERE (date=? AND hour_slot>=3 AND LOWER(venue)=LOWER(?))
               OR (date=? AND hour_slot<3  AND LOWER(venue)=LOWER(?))
            GROUP BY product_name, hour_slot
            ORDER BY gross DESC
        """, [event['date'], event['venue'], next_d, event['venue']]).fetchall()
        rows = [dict(r) for r in rows]
    except:
        rows = []

    # Totals
    total_net   = sum(r['net']   for r in rows)
    total_gross = sum(r['gross'] for r in rows)
    total_qty   = sum(r['qty']   for r in rows)

    # Group by product for summary
    by_product = {}
    for r in rows:
        p = r['product_name']
        if p not in by_product:
            by_product[p] = {'qty': 0, 'net': 0, 'gross': 0}
        by_product[p]['qty']   += r['qty']
        by_product[p]['net']   += r['net']
        by_product[p]['gross'] += r['gross']
    products = sorted(by_product.items(), key=lambda x: -x[1]['gross'])

    return render_template("daily_log/sales_breakdown.html",
        event=event,
        rows=rows,
        products=products,
        total_net=total_net,
        total_gross=total_gross,
        total_qty=total_qty,
        venue_badge=_vbadge(event['venue']),
        venue_label=_vlabel(event['venue']))

# ── new event ─────────────────────────────────────────────────────────

@bp.route("/new", methods=["GET","POST"])
def new():
    db = get_db()
    _ensure_tables(db)
    event_types = _get_event_types(db)
    prefill_date  = request.args.get("date", "")
    prefill_venue = request.args.get("venue", "")

    if request.method == "POST":
        f  = request.form
        d  = f.get("date","")
        db.execute("""
            INSERT INTO events_daily_log (
                date, month_year, venue, organiser, event_name, event_type,
                event_start, event_end, number_of_visitors,
                venue_rent, ticket_sales_own, other_revenues,
                musicians_fee, equipment_expenses, laborers,
                advertising, debit_card_expenses_variable, other_expenses,
                additional_details, status, created_at, last_modified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        """, [d, _month_year(d),
              f.get("venue"), f.get("organiser"), f.get("event_name"), f.get("event_type"),
              f.get("event_start") or None, f.get("event_end") or None,
              _si(f.get("number_of_visitors")),
              _sf(f.get("venue_rent")), _sf(f.get("ticket_sales_own")),
              _sf(f.get("other_revenues")), _sf(f.get("musicians_fee")),
              _sf(f.get("equipment_expenses")), _sf(f.get("laborers")),
              _sf(f.get("advertising")), _sf(f.get("debit_card_expenses_variable")),
              _sf(f.get("other_expenses")),
              f.get("additional_details") or None,
              f.get("status","Confirmed")])
        db.commit()
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        _recalc_event(new_id, db)
        flash(tr("Događaj je dodat."), "success")
        back = f.get("back_to","")
        return _anchor_redirect(back or url_for("daily_log.index"), new_id)

    return render_template("daily_log/form.html",
        event=None, venues=VENUES, event_types=event_types,
        today=prefill_date or date.today().isoformat(),
        prefill_venue=prefill_venue, title=tr("Novi događaj"),
        back_to=request.args.get("back_to",""),
        passthrough_products=_get_passthrough(db))

def _get_passthrough(db):
    try:
        return [r['name'] for r in db.execute(
            "SELECT name FROM passthrough_products ORDER BY name"
        ).fetchall()]
    except:
        return []

# ── group edit ────────────────────────────────────────────────────────

@bp.route("/group/<date_str>/<venue_str>", methods=["GET"])
def group_edit(date_str, venue_str):
    db = get_db()
    _ensure_tables(db)
    events_raw = db.execute("""
        SELECT * FROM events_daily_log
        WHERE date=? AND LOWER(venue)=LOWER(?)
        ORDER BY event_id
    """, [date_str, venue_str]).fetchall()
    events = [dict(e) for e in events_raw]
    if not events:
        flash(tr("Nema događaja za ovaj datum i lokaciju."), "error")
        return redirect(url_for("daily_log.index"))

    for e in events:
        t = _compute_totals(e)
        e.update(t)

    back_to = url_for("daily_log.group_edit", date_str=date_str, venue_str=venue_str)
    return render_template("daily_log/group_edit.html",
        events=events, event_types=_get_event_types(db),
        venues=VENUES, date_str=date_str, venue_str=venue_str,
        venue_label=_vlabel(venue_str), venue_badge=_vbadge(venue_str),
        back_to=back_to,
        new_event_url=url_for("daily_log.new", date=date_str, venue=venue_str,
                              back_to=back_to),
        passthrough_products=_get_passthrough(db),
        invoices_out=_recent_invoices(db, 'out'),
        invoices_in=_recent_invoices(db, 'in'),
        bank_credits=_recent_bank_credits(db))

def _recent_invoices(db, direction):
    table = 'invoices_out' if direction == 'out' else 'invoices_in'
    name_col = 'client_name' if direction == 'out' else 'supplier_name'
    try:
        rows = db.execute(f"""
            SELECT invoice_id_sef,
                   {name_col} as name,
                   net_amount, date_issued, invoice_number_sef
            FROM {table}
            ORDER BY date_issued DESC LIMIT 200
        """).fetchall()
        return [dict(r) for r in rows]
    except:
        return []

def _recent_bank_credits(db):
    try:
        rows = db.execute("""
            SELECT transaction_id, date, amount, counterparty, purpose
            FROM bank_transactions
            WHERE direction='credit' AND COALESCE(allocated,0)=0
            ORDER BY date DESC LIMIT 200
        """).fetchall()
        return [dict(r) for r in rows]
    except:
        return []

# ── edit ──────────────────────────────────────────────────────────────

@bp.route("/<int:event_id>/edit", methods=["GET","POST"])
def edit(event_id):
    db = get_db()
    _ensure_tables(db)
    event = db.execute(
        "SELECT * FROM events_daily_log WHERE event_id=?", [event_id]
    ).fetchone()
    if not event:
        flash(tr("Događaj nije pronađen."), "error")
        return redirect(url_for("daily_log.index"))
    event = dict(event)

    if request.method == "POST":
        f = request.form
        d = f.get("date","")
        db.execute("""
            UPDATE events_daily_log SET
                date=?, month_year=?, venue=?, organiser=?, event_name=?, event_type=?,
                event_start=?, event_end=?, number_of_visitors=?,
                venue_rent=?, ticket_sales_own=?, other_revenues=?,
                musicians_fee=?, equipment_expenses=?,
                advertising=?, debit_card_expenses_variable=?, other_expenses=?,
                sales_net_override=?, business_bills_override=?,
                third_party_override=?, cogs_override=?,
                wages_override=?, laborers_override=?,
                venue_rent_override=?, ticket_sales_own_override=?,
                musicians_fee_invoice_id=?, equipment_invoice_id=?,
                advertising_invoice_id=?, venue_rent_invoice_id=?,
                ticket_sales_bank_tx_id=?,
                additional_details=?, status=?,
                last_modified_at=datetime('now')
            WHERE event_id=?
        """, [
            d, _month_year(d),
            f.get("venue"), f.get("organiser"), f.get("event_name"), f.get("event_type"),
            f.get("event_start") or None, f.get("event_end") or None,
            _si(f.get("number_of_visitors")),
            _sf(f.get("venue_rent")), _sf(f.get("ticket_sales_own")),
            _sf(f.get("other_revenues")), _sf(f.get("musicians_fee")),
            _sf(f.get("equipment_expenses")),
            _sf(f.get("advertising")), _sf(f.get("debit_card_expenses_variable")),
            _sf(f.get("other_expenses")),
            _sf(f.get("sales_net_override")), _sf(f.get("business_bills_override")),
            _sf(f.get("third_party_override")), _sf(f.get("cogs_override")),
            _sf(f.get("wages_override")), _sf(f.get("laborers_override")),
            _sf(f.get("venue_rent_override")), _sf(f.get("ticket_sales_own_override")),
            _si(f.get("musicians_fee_invoice_id")) or None,
            _si(f.get("equipment_invoice_id")) or None,
            _si(f.get("advertising_invoice_id")) or None,
            _si(f.get("venue_rent_invoice_id")) or None,
            _si(f.get("ticket_sales_bank_tx_id")) or None,
            f.get("additional_details") or None,
            f.get("status","Confirmed"),
            event_id])
        db.commit()
        _recalc_event(event_id, db)
        flash(tr("Izmene su sačuvane."), "success")
        back = f.get("back_to","")
        return _anchor_redirect(back or url_for("daily_log.index"), event_id)

    return render_template("daily_log/form.html",
        event=event, venues=VENUES, event_types=_get_event_types(db),
        today=date.today().isoformat(), prefill_venue="",
        title=tr("Izmeni") + f" — {event.get('event_name') or event.get('organiser') or '#' + str(event_id)}",
        back_to=request.args.get("back_to",""),
        passthrough_products=_get_passthrough(db),
        invoices_out=_recent_invoices(db,'out'),
        invoices_in=_recent_invoices(db,'in'),
        bank_credits=_recent_bank_credits(db))

# ── toggle status ─────────────────────────────────────────────────────

@bp.route("/<int:event_id>/toggle-status", methods=["POST"])
def toggle_status(event_id):
    db = get_db()
    e = db.execute("SELECT status FROM events_daily_log WHERE event_id=?", [event_id]).fetchone()
    if e:
        ns = "Confirmed" if e["status"] != "Confirmed" else "Preliminary"
        db.execute("UPDATE events_daily_log SET status=?,last_modified_at=datetime('now') WHERE event_id=?", [ns,event_id])
        db.commit()
        flash(tr("Status: {ns}", ns=ns), "success")
    back = request.form.get("back_to","")
    return _anchor_redirect(back or url_for("daily_log.index"), event_id)

# ── bulk confirm ──────────────────────────────────────────────────────

@bp.route("/bulk-confirm", methods=["POST"])
def bulk_confirm():
    db = get_db()
    r = db.execute("""UPDATE events_daily_log SET status='Confirmed',
        last_modified_at=datetime('now') WHERE status!='Confirmed'""")
    db.commit()
    flash(tr("Potvrđeno {a0} događaja.", a0=r.rowcount), "success")
    return redirect(url_for("daily_log.index"))

# ── delete ────────────────────────────────────────────────────────────

@bp.route("/<int:event_id>/delete", methods=["POST"])
def delete(event_id):
    db = get_db()
    db.execute("DELETE FROM events_daily_log WHERE event_id=?", [event_id])
    db.commit()
    flash(tr("Događaj je obrisan."), "success")
    back = request.form.get("back_to","")
    return _anchor_redirect(back or url_for("daily_log.index"), event_id)

# ── detail ────────────────────────────────────────────────────────────

@bp.route("/<int:event_id>")
def detail(event_id):
    db = get_db()
    event = db.execute(
        "SELECT * FROM events_daily_log WHERE event_id=?", [event_id]
    ).fetchone()
    if not event:
        flash(tr("Događaj nije pronađen."), "error")
        return redirect(url_for("daily_log.index"))
    event = dict(event)

    totals = _compute_totals(event)

    # Effective values with override flags
    def ev(ak, ok):
        v, is_ov = _effective_val(event, ak, ok)
        return v, is_ov

    fields = {
        'sales_net':      ev('sales_net_auto',          'sales_net_override'),
        'business_bills': ev('business_bills_net_auto', 'business_bills_override'),
        'third_party':    ev('third_party_auto',        'third_party_override'),
        'cogs':           ev('cogs_auto',                'cogs_override'),
        'wages':          ev('wages_auto',               'wages_override'),
        'laborers':       ev('laborers_auto',            'laborers_override'),
    }

    # POS detail for this day
    try:
        oc = db.execute("""
            SELECT SUM(revenue_net) as net, SUM(revenue_gross) as gross,
                   SUM(quantity) as qty, COUNT(DISTINCT product_name) as products
            FROM sales_hourly
            WHERE date=? AND LOWER(venue)=LOWER(?)
        """, [event['date'], event['venue']]).fetchone()
        pos = dict(oc) if oc else None
    except:
        pos = None

    # Business bills detail
    try:
        biz_bills = db.execute("""
            SELECT bill_id, amount, business_partner_name, time_created
            FROM bills_raw
            WHERE date=? AND LOWER(venue)=LOWER(?)
              AND is_revoked=0
              AND (
                  LOWER(TRIM(COALESCE(table_name,''))) LIKE 'organizacija%'
                  OR (
                      business_partner_name IS NOT NULL
                      AND business_partner_name != ''
                  )
              )
        """, [event['date'], event['venue']]).fetchall()
        biz_bills = [dict(r) for r in biz_bills]
    except:
        biz_bills = []

    # Adjustments
    try:
        adjustments = [dict(r) for r in db.execute("""
            SELECT * FROM adjustments WHERE event_id=?
            ORDER BY adjustment_date DESC
        """, [event_id]).fetchall()]
    except:
        adjustments = []

    # Linked invoice info
    inv_links = {}
    for col, table, name_col in [
        ('venue_rent_invoice_id',    'invoices_out', 'client_name'),
        ('musicians_fee_invoice_id', 'invoices_in',  'supplier_name'),
        ('equipment_invoice_id',     'invoices_in',  'supplier_name'),
        ('advertising_invoice_id',   'invoices_in',  'supplier_name'),
    ]:
        inv_id = event.get(col)
        if inv_id:
            try:
                r = db.execute(
                    f"SELECT {name_col} as name, net_amount, invoice_number_sef FROM {table} WHERE invoice_id_sef=?",
                    [inv_id]
                ).fetchone()
                if r: inv_links[col] = dict(r)
            except:
                pass

    all_day_events = db.execute("""
        SELECT * FROM events_daily_log WHERE date=? AND LOWER(venue)=LOWER(?)
        ORDER BY event_id
    """, [event['date'], event['venue']]).fetchall()
    cutoff_warning = len(all_day_events) > 1 and not event.get('event_end')

    return render_template("daily_log/detail.html",
        event=event, totals=totals, fields=fields,
        adjustments=adjustments, pos=pos,
        biz_bills=biz_bills, inv_links=inv_links,
        cutoff_warning=cutoff_warning,
        venue_badge=_vbadge(event['venue']),
        venue_label=_vlabel(event['venue']))

# ── add adjustment ────────────────────────────────────────────────────

@bp.route("/<int:event_id>/adjustment", methods=["POST"])
def add_adjustment(event_id):
    db = get_db()
    f = request.form
    try:
        db.execute("""
            INSERT INTO adjustments
                (event_id, field_adjusted, original_value, corrected_value,
                 reason, source, adjustment_date)
            VALUES (?,?,?,?,?,?,datetime('now'))
        """, [event_id, f.get("field_name"),
              f.get("old_value") or None, f.get("new_value") or None,
              f.get("reason") or None, f.get("source") or None])
        db.commit()
        flash(tr("Adjustment dodat."), "success")
    except Exception as e:
        flash(tr("Greška: {e}", e=e), "error")
    return redirect(url_for("daily_log.detail", event_id=event_id))

# ── event type tags ───────────────────────────────────────────────────

@bp.route("/event-types", methods=["GET","POST"])
def event_types_manage():
    db = get_db()
    _ensure_tables(db)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = (request.form.get("name") or "").strip().lower()
            if name:
                try:
                    db.execute("INSERT INTO event_type_tags (name) VALUES (?)", [name])
                    db.commit(); flash(tr("Tip '{name}' dodat.", name=name), "success")
                except: flash(tr("Tip '{name}' već postoji.", name=name), "error")
        elif action == "delete":
            tid = request.form.get("tag_id")
            if tid:
                db.execute("DELETE FROM event_type_tags WHERE id=?", [tid])
                db.commit(); flash(tr("Tip obrisan."), "success")
        return redirect(url_for("daily_log.event_types_manage"))
    tags = db.execute("SELECT * FROM event_type_tags ORDER BY name").fetchall()
    return render_template("daily_log/event_types.html", tags=tags)

# ── pass-through products ─────────────────────────────────────────────

@bp.route("/passthrough", methods=["GET","POST"])
def passthrough_manage():
    db = get_db()
    _ensure_tables(db)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = (request.form.get("name") or "").strip()
            note = (request.form.get("note") or "").strip() or None
            if name:
                try:
                    db.execute("INSERT INTO passthrough_products (name,note) VALUES (?,?)", [name,note])
                    db.commit(); flash(tr("'{name}' dodat.", name=name), "success")
                except: flash(tr("'{name}' već postoji.", name=name), "error")
        elif action == "delete":
            pid = request.form.get("product_id")
            if pid:
                db.execute("DELETE FROM passthrough_products WHERE id=?", [pid])
                db.commit(); flash(tr("Proizvod uklonjen."), "success")
        return redirect(url_for("daily_log.passthrough_manage"))
    products = db.execute("SELECT * FROM passthrough_products ORDER BY name").fetchall()
    return render_template("daily_log/passthrough.html", products=products)
