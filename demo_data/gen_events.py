"""Shift wages and the event-level P&L log."""

from __future__ import annotations

import random

from . import catalog as C
from .helpers import money, net_from_gross, now_stamp

# Net pay per shift by role band
SHIFT_PAY = {
    "Bartender": 5200,
    "Waiter": 4800,
    "Security": 5600,
    "Host": 4400,
    "Technician": 7200,
    "Cleaner": 3800,
    "Runner": 4000,
}

SALARIED_ROLES = {"Operations manager", "Bar manager", "Accountant (part-time)",
                  "Head bartender"}


def _crew_for(day, rng: random.Random) -> list[tuple[str, str]]:
    """Return the (name, role) list rostered for a given event day."""
    demand = day["demand"]
    if day["venue"] == "Garden":
        bartenders = max(2, min(7, round(2 + demand * 2.2)))
        waiters = max(1, min(6, round(1 + demand * 1.8)))
        security = 0 if demand < 0.6 else max(1, min(4, round(demand * 1.6)))
        runners = 1 if demand > 1.1 else 0
    else:
        bartenders = max(1, min(3, round(1 + demand * 0.9)))
        waiters = max(0, min(2, round(demand * 0.8)))
        security = 1 if (demand > 1.2 and day["event_type"] in ("DJ night", "Concert")) else 0
        runners = 0

    tech = 1 if day["event_type"] in ("Concert", "DJ night", "Theatre", "Conference",
                                      "Festival day") else 0
    cleaner = 1 if day["venue"] == "Garden" else (1 if rng.random() < 0.4 else 0)

    pools = {
        "Bartender": [e[0] for e in C.EMPLOYEES if e[1] == "Bartender"],
        "Waiter": [e[0] for e in C.EMPLOYEES if e[1] == "Waiter"],
        "Security": [e[0] for e in C.EMPLOYEES if e[1] == "Security"],
        "Technician": [e[0] for e in C.EMPLOYEES if e[1] == "Technician"],
        "Cleaner": [e[0] for e in C.EMPLOYEES if e[1] == "Cleaner"],
        "Runner": [e[0] for e in C.EMPLOYEES if e[1] == "Runner"],
        "Host": [e[0] for e in C.EMPLOYEES if e[1] == "Host"],
    }
    crew = []
    for role, n in (("Bartender", bartenders), ("Waiter", waiters), ("Security", security),
                    ("Technician", tech), ("Cleaner", cleaner), ("Runner", runners)):
        pool = pools[role]
        if not pool or n <= 0:
            continue
        picked = rng.sample(pool, min(n, len(pool)))
        crew += [(name, role) for name in picked]
    if day["demand"] > 1.3 and pools["Host"]:
        crew.append((pools["Host"][0], "Host"))
    return crew


def generate_wages(con, plan, staff, seed: int) -> None:
    rng = random.Random(seed + 23)
    bracket = {s["name"]: s["bracket"] for s in staff}
    rows = []

    for day in plan:
        total_net = 0.0
        total_gross = 0.0
        for name, role in _crew_for(day, rng):
            net = SHIFT_PAY[role] * rng.uniform(0.94, 1.12)
            # a long festival shift is paid at a premium
            if day["event_type"] == "Festival day":
                net *= 1.3
            net = money(round(net / 100) * 100)
            br = bracket.get(name, "casual_a")
            gross = money(net * C.BRACKET_MULTIPLIER[br])
            rows.append((
                day["date"].isoformat(), name, "casual" if br != "full" else "employed",
                day["venue"], net, br, gross, "demo_generator", None,
                now_stamp(day["date"]),
                f"{day['date'].isoformat()}|{name}|{day['venue']}",
            ))
            total_net += net
            total_gross += gross
        day["wages_net"] = money(total_net)
        day["wages_gross"] = money(total_gross)

    con.executemany(
        "INSERT INTO wages_daily (date, employee_name, worker_type, venue, net_amount, "
        "tax_bracket, gross_cost, source_file, notes, created_at, fitid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def generate_event_log(con, plan, seed: int) -> None:
    """Write events_daily_log, mirroring how the real module stores both the
    automatically derived figures and any manual override."""
    rng = random.Random(seed + 29)
    rows = []

    for day in plan:
        etype = day["event_type"]
        sales_gross = day["sales_gross"]
        sales_net = net_from_gross(sales_gross, 19.2)  # blended 20/10 mix

        venue_rent = 0.0
        ticket_own = 0.0
        third_party = 0.0
        musicians = 0.0
        equipment = 0.0
        advertising = 0.0
        laborers = 0.0
        other_rev = 0.0
        other_exp = 0.0

        cap = 450 if day["outdoor"] else 90
        if etype == "Private rental":
            venue_rent = money(round(cap * rng.uniform(700, 1500) / 1000) * 1000)
        elif etype == "Conference":
            venue_rent = money(round(cap * rng.uniform(500, 1100) / 1000) * 1000)
        elif etype in ("Concert", "DJ night", "Festival day", "Theatre"):
            if day["organiser"] == "In-house":
                ticket_own = money(day["visitors"] * rng.uniform(0.35, 0.8)
                                   * rng.choice([600, 800, 1000, 1200]))
                # The artist fee is negotiated against what the night is
                # expected to take, not picked out of the air: roughly a
                # quarter to a half of bar plus ticket revenue, with a floor
                # for the smallest bookings. A night can still lose money —
                # that is the point of tracking it — but a 60-person acoustic
                # set never carries a headline fee.
                expected = sales_net + ticket_own
                share = {"Theatre": 0.22, "DJ night": 0.26,
                         "Concert": 0.34, "Festival day": 0.40}[etype]
                musicians = money(round(max(15000, expected * share
                                            * rng.uniform(0.75, 1.3)) / 1000) * 1000)
                equipment = money(round(expected * rng.uniform(0.0, 0.10) / 1000) * 1000)
                advertising = money(round(expected * rng.uniform(0.0, 0.08) / 1000) * 1000)
            else:
                third_party = money(day["visitors"] * rng.uniform(0.3, 0.7)
                                    * rng.choice([700, 900, 1100]))
                venue_rent = money(round(rng.uniform(60000, 180000) / 1000) * 1000)
        elif etype == "Market day":
            other_rev = money(round(rng.uniform(30000, 120000) / 1000) * 1000)
        elif etype == "Film screening":
            equipment = money(round(sales_net * rng.uniform(0.0, 0.12) / 1000) * 1000)

        if day["demand"] > 1.0 and rng.random() < 0.25:
            laborers = money(round(sales_net * rng.uniform(0.02, 0.08) / 1000) * 1000)
        if rng.random() < 0.12:
            other_exp = money(round(sales_net * rng.uniform(0.01, 0.06) / 1000) * 1000)

        status = "Final"
        # the most recent fortnight is still being closed out
        if (max(p["date"] for p in plan) - day["date"]).days < 14:
            status = rng.choice(["Preliminary", "Preliminary", "Final"])

        # Filled by allocate_fixed_profit() once the fixed cost links exist:
        # it is the event's share of the month's fixed costs, not its own
        # profit, and the two were being conflated here.
        fixed_profit = None

        rows.append((
            day["event_id"], day["date"].isoformat(),
            f"{day['date'].year}-{day['date'].month:02d}", day["venue"], day["organiser"],
            day["event_name"], etype, day["start"], day["end"], day["visitors"],
            sales_gross, third_party, venue_rent, ticket_own, other_rev,
            day["cogs_net"], day["wages_gross"], musicians, equipment, laborers,
            advertising, 0.0, other_exp, fixed_profit, status, day["note"],
            now_stamp(day["date"]), now_stamp(day["date"]), "demo_generator",
            0.0, f"POS:{day['date'].isoformat()}:{day['venue']}",
            sales_net, day["business_bills_net"], third_party, day["cogs_net"],
            day["wages_gross"], laborers,
            None, None, None, None, None, None, None, None,
            None, None, None, None, None,
        ))

    con.executemany(
        "INSERT INTO events_daily_log (event_id, date, month_year, venue, organiser, "
        "event_name, event_type, event_start, event_end, number_of_visitors, sales_gross, "
        "third_party_ticket_revenue, venue_rent, ticket_sales_own, other_revenues, cogs, "
        "wages, musicians_fee, equipment_expenses, laborers, advertising, "
        "debit_card_expenses_variable, other_expenses, fixed_profit, status, "
        "additional_details, created_at, last_modified_at, modified_by, bank_linked_costs, "
        "source_ref, sales_net_auto, business_bills_net_auto, third_party_auto, cogs_auto, "
        "wages_auto, laborers_auto, sales_net_override, business_bills_override, "
        "third_party_override, cogs_override, wages_override, laborers_override, "
        "venue_rent_override, ticket_sales_own_override, venue_rent_invoice_id, "
        "musicians_fee_invoice_id, equipment_invoice_id, advertising_invoice_id, "
        "ticket_sales_bank_tx_id) VALUES (" + ",".join("?" * 50) + ")", rows)

    # A handful of manual corrections, as the real log accumulates
    adj = []
    sample = rng.sample(plan, min(18, len(plan)))
    for day in sample:
        field = rng.choice(["sales_gross", "cogs", "wages", "number_of_visitors"])
        orig = day["sales_gross"] if field == "sales_gross" else day["cogs_net"]
        adj.append((
            day["event_id"], field, orig, money(orig * rng.uniform(0.9, 1.1)),
            rng.choice(["POS outage, figures entered from the till roll.",
                        "Duplicate shift removed.",
                        "Visitor count corrected from the door clicker.",
                        "Supplier credit note applied retroactively."]),
            day["date"].isoformat(), "manual", "Operations manager",
        ))
    con.executemany(
        "INSERT INTO adjustments (event_id, field_adjusted, original_value, "
        "corrected_value, reason, adjustment_date, source, recorded_by) "
        "VALUES (?,?,?,?,?,?,?,?)", adj)

    return len(rows)


# ---------------------------------------------------------------------------
# Fixed cost allocation
# ---------------------------------------------------------------------------

CLOSED_TYPES = {"lokal ne radi", "zatvoreno", "closed"}


def allocate_fixed_profit(con) -> int:
    """Fill events_daily_log.fixed_profit the way the dashboard derives it.

    The column is not an event's own profit: it is the event's share of the
    month's operating fixed costs netted against operating fixed income, which
    is normally negative. The dashboard recomputes it on every render from
    invoice_fixed_cost_links, so the seeded value has to agree with that
    formula or the stored and derived figures drift apart.

    Shared costs are split evenly between the two venues, each venue's share is
    divided by the days that venue was open in the month, and the day's share
    is divided across the events held there. Must run after the fixed cost
    links exist.
    """
    cur = con.cursor()

    # Net operating position per (year, month, venue scope), VAT excluded.
    monthly: dict[tuple[int, int, str], float] = {}
    for year, month, venue, kind, amount in cur.execute(
        """
        SELECT l.year, l.month, LOWER(t.venue), t.type,
               SUM(ROUND(l.amount / (1 + COALESCE(l.vat_rate, 0) / 100.0), 2))
        FROM invoice_fixed_cost_links l
        JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        WHERE t.submodule = 'operativni'
        GROUP BY l.year, l.month, LOWER(t.venue), t.type
        """
    ):
        signed = float(amount or 0) * (1 if kind == "income" else -1)
        monthly[(year, month, venue)] = monthly.get((year, month, venue), 0.0) + signed

    # Open events per venue per day, and open days per venue per month.
    events: dict[tuple[str, str], list[int]] = {}
    for event_id, date, venue, etype in cur.execute(
        "SELECT event_id, date, LOWER(venue), LOWER(COALESCE(event_type, '')) "
        "FROM events_daily_log"
    ):
        if etype in CLOSED_TYPES:
            continue
        events.setdefault((date, venue), []).append(event_id)

    open_days: dict[tuple[int, int, str], set[str]] = {}
    for (date, venue) in events:
        year, month = int(date[:4]), int(date[5:7])
        open_days.setdefault((year, month, venue), set()).add(date)

    updates = []
    for (date, venue), ids in events.items():
        year, month = int(date[:4]), int(date[5:7])
        shared = monthly.get((year, month, "shared"), 0.0)
        own = monthly.get((year, month, venue), 0.0)
        venue_total = shared * 0.5 + own
        days = len(open_days.get((year, month, venue), ()))
        if not venue_total or not days:
            continue
        per_event = venue_total / days / len(ids)
        for event_id in ids:
            updates.append((round(per_event, 2), event_id))

    cur.executemany(
        "UPDATE events_daily_log SET fixed_profit = ? WHERE event_id = ?", updates)
    return len(updates)
