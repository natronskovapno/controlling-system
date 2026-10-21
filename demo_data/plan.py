"""Builds the day-by-day operating plan that everything else derives from.

One entry per (date, venue) the business was open. Each entry carries the
event booked that day and the expected trading volume, so sales, wages,
inventory and the event P&L all stay consistent with each other.
"""

from __future__ import annotations

import datetime as dt
import random

from . import catalog as C
from .helpers import (
    daterange, growth_factor, is_open, jitter, seasonal_factor,
    weekday_factor, weighted_choice,
)

# Base bills per day at demand factor 1.0
BASE_BILLS = {"Garden": 190, "Corner": 48}

# Event type probability by venue and whether it is a weekend
EVENT_WEIGHTS = {
    ("Garden", False): [
        ("Regular day", 34), ("DJ night", 16), ("Concert", 14), ("Film screening", 8),
        ("Theatre", 6), ("Private rental", 9), ("Conference", 5), ("Market day", 4),
        ("Festival day", 4),
    ],
    ("Garden", True): [
        ("Concert", 26), ("DJ night", 24), ("Festival day", 10), ("Market day", 8),
        ("Regular day", 14), ("Private rental", 10), ("Theatre", 5), ("Film screening", 3),
    ],
    ("Corner", False): [
        ("Regular day", 66), ("DJ night", 14), ("Concert", 8),
        ("Private rental", 7), ("Theatre", 5),
    ],
    ("Corner", True): [
        ("Regular day", 44), ("DJ night", 28), ("Concert", 16), ("Private rental", 12),
    ],
}

# How much an event type lifts (or suppresses) bar trade
EVENT_LIFT = {
    "Regular day": 1.00,
    "DJ night": 1.38,
    "Concert": 1.46,
    "Festival day": 2.20,
    "Private rental": 0.72,
    "Theatre": 0.88,
    "Film screening": 0.80,
    "Market day": 1.12,
    "Conference": 0.75,
}


def build_plan(start: dt.date, end: dt.date, seed: int, scale: float = 1.0) -> list[dict]:
    rng = random.Random(seed)
    plan: list[dict] = []
    event_id = 1

    for d in daterange(start, end):
        for venue, meta in C.VENUES.items():
            if not is_open(d, venue):
                continue
            outdoor = not meta["indoor"]
            weekend = d.weekday() >= 4

            demand = (
                seasonal_factor(d, outdoor)
                * weekday_factor(d, outdoor)
                * growth_factor(d, start)
            )
            if demand <= 0.02:
                continue

            etype = weighted_choice(rng, EVENT_WEIGHTS[(venue, weekend)])
            # A festival day only makes sense in high summer at the outdoor venue
            if etype == "Festival day" and (d.month not in (6, 7, 8) or not outdoor):
                etype = "Concert"

            demand *= EVENT_LIFT[etype]
            demand = jitter(rng, demand, 0.22)
            # the odd washout / exceptional night
            roll = rng.random()
            if roll < 0.04:
                demand *= rng.uniform(0.25, 0.55)   # rain, cancellation
                weather_note = "Heavy rain, trade well below plan."
            elif roll > 0.97:
                demand *= rng.uniform(1.35, 1.75)   # sold out
                weather_note = "Sold out; exceptional night."
            else:
                weather_note = None

            bills = max(3, int(BASE_BILLS[venue] * demand * scale))
            visitors = int(bills * rng.uniform(1.25, 1.9))
            if etype == "Private rental":
                visitors = int(visitors * rng.uniform(0.6, 0.9))

            name = rng.choice(C.EVENT_NAME_PARTS[etype])
            if etype == "Regular day":
                organiser = "In-house"
            elif etype in ("Private rental", "Conference"):
                organiser = rng.choice([o for o in C.ORGANISERS if o != "In-house"])
            else:
                organiser = weighted_choice(rng, [("In-house", 6)] +
                                            [(o, 1) for o in C.ORGANISERS[1:]])

            start_h = 20 if etype in ("Concert", "DJ night") else 18
            if etype in ("Market day", "Conference"):
                start_h = 11
            end_h = 2 if outdoor and etype in ("Concert", "DJ night") else 23

            plan.append({
                "event_id": event_id,
                "date": d,
                "venue": venue,
                "outdoor": outdoor,
                "entity": meta["entity"],
                "event_type": etype,
                "event_name": name,
                "organiser": organiser,
                "visitors": visitors,
                "bills": bills,
                "demand": demand,
                "start": f"{start_h:02d}:00",
                "end": f"{end_h:02d}:00",
                "note": weather_note,
                # filled in by later generators
                "sales_gross": 0.0, "sales_net": 0.0, "cogs_net": 0.0,
                "wages_net": 0.0, "wages_gross": 0.0, "laborers": 0.0,
                "business_bills_net": 0.0,
            })
            event_id += 1

    return plan
