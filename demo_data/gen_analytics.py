"""Website analytics, as delivered by an aggregate-only analytics API.

The upstream API exposes counts per dimension, not per visitor, so each
breakdown is its own independent aggregate and dimensions cannot be crossed.
Bot traffic is kept as a flag rather than filtered on ingestion, so the UI can
include or exclude it without re-ingesting.
"""

from __future__ import annotations

import datetime as dt
import math
import random

from . import catalog as C
from .helpers import daterange, now_stamp

HOUR_SHARE = [
    0.012, 0.007, 0.005, 0.004, 0.004, 0.006, 0.012, 0.024,
    0.038, 0.049, 0.054, 0.056, 0.058, 0.057, 0.056, 0.058,
    0.062, 0.068, 0.074, 0.078, 0.080, 0.070, 0.044, 0.024,
]


def generate(con, plan, start: dt.date, end: dt.date, seed: int) -> dict:
    """Tracking starts partway through the period: the site launched late."""
    rng = random.Random(seed + 67)
    track_start = max(start, end - dt.timedelta(days=185))

    # event days drive traffic spikes the day before and the day of
    big_days = {}
    for day in plan:
        if day["event_type"] in ("Concert", "DJ night", "Festival day", "Theatre"):
            big_days[day["date"]] = max(big_days.get(day["date"], 0.0), day["demand"])

    daily, hourly, breakdown = [], [], []
    tag, host = C.DEMO_SITE_TAG, C.DEMO_SITE_HOST
    days_count = 0

    for d in daterange(track_start, end):
        days_count += 1
        base = 210 * (1.0 + 0.5 * math.sin((d.timetuple().tm_yday - 150) / 365 * 2 * math.pi))
        base *= 1.0 + 0.25 * (d.weekday() >= 4)
        lift = big_days.get(d, 0.0) + big_days.get(d + dt.timedelta(days=1), 0.0) * 0.6
        base *= 1.0 + 0.45 * lift
        # slow organic growth since launch
        base *= 1.0 + 0.0022 * (d - track_start).days
        base *= rng.uniform(0.82, 1.2)

        for bot in (0, 1):
            factor = 1.0 if bot == 0 else rng.uniform(0.18, 0.42)
            pv = max(0, int(base * factor * rng.uniform(1.6, 2.4)))
            visits = max(0, int(pv / rng.uniform(1.6, 2.3)))
            if pv == 0:
                continue
            daily.append((tag, d.isoformat(), bot, pv, visits, 1.0, now_stamp(d)))

            for h in range(24):
                hpv = int(pv * HOUR_SHARE[h] * rng.uniform(0.7, 1.3))
                if hpv <= 0:
                    continue
                hv = max(1, int(hpv / rng.uniform(1.6, 2.3)))
                hourly.append((tag, d.isoformat(), h, bot, hpv, hv, now_stamp(d)))

            for dim_type, table in (("country", C.ANALYTICS_COUNTRIES),
                                    ("referrer", C.ANALYTICS_REFERRERS),
                                    ("path", C.ANALYTICS_PATHS),
                                    ("device", C.ANALYTICS_DEVICES)):
                for value, share in table:
                    dpv = int(pv * share * rng.uniform(0.85, 1.15))
                    if dpv <= 0:
                        continue
                    dv = max(1, int(dpv / rng.uniform(1.6, 2.3)))
                    breakdown.append((tag, d.isoformat(), dim_type, value, bot,
                                      dpv, dv, now_stamp(d)))

    con.executemany(
        "INSERT INTO web_analytics_daily (site_tag, date, bot, pageviews, visits, "
        "sample_interval, ingested_at) VALUES (?,?,?,?,?,?,?)", daily)
    con.executemany(
        "INSERT INTO web_analytics_hourly (site_tag, date, hour, bot, pageviews, visits, "
        "ingested_at) VALUES (?,?,?,?,?,?,?)", hourly)
    con.executemany(
        "INSERT INTO web_analytics_breakdown (site_tag, date, dim_type, dim_value, bot, "
        "pageviews, visits, ingested_at) VALUES (?,?,?,?,?,?,?,?)", breakdown)
    con.execute(
        "INSERT INTO web_analytics_cursor (site_tag, host, last_date, last_run_at, "
        "last_status, last_error, days_ingested) VALUES (?,?,?,?,?,?,?)",
        (tag, host, end.isoformat(), now_stamp(end), "ok", None, days_count))

    return {"analytics_days": days_count, "analytics_rows":
            len(daily) + len(hourly) + len(breakdown)}
