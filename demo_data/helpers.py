"""Shared helpers for the demo data generator."""

from __future__ import annotations

import datetime as dt
import math
import random

RSD = 1.0  # all amounts are in RSD unless a currency column says otherwise

# Serbian month names, used by date_table
MONTHS_SR = [
    "januar", "februar", "mart", "april", "maj", "jun",
    "jul", "avgust", "septembar", "oktobar", "novembar", "decembar",
]
DAYS_SR = ["ponedeljak", "utorak", "sreda", "cetvrtak", "petak", "subota", "nedelja"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def daterange(start: dt.date, end: dt.date):
    """Yield every date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def iso(d: dt.date) -> str:
    return d.isoformat()


def month_key(d: dt.date) -> tuple[int, int]:
    return d.year, d.month


def months_between(start: dt.date, end: dt.date):
    """Yield (year, month) for every month touched by the range."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    return (nxt - dt.date(year, month, 1)).days


def money(x: float) -> float:
    """Round to 2 decimals the way the accounting side expects."""
    return round(x + 1e-9, 2)


def net_from_gross(gross: float, vat_rate: float) -> float:
    return money(gross / (1.0 + vat_rate / 100.0))


def vat_from_gross(gross: float, vat_rate: float) -> float:
    return money(gross - net_from_gross(gross, vat_rate))


def weighted_choice(rng: random.Random, pairs):
    """pairs is a sequence of (value, weight)."""
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    upto = 0.0
    for value, w in pairs:
        upto += w
        if r <= upto:
            return value
    return pairs[-1][0]


def jitter(rng: random.Random, value: float, pct: float) -> float:
    """Multiply value by a random factor within +/- pct."""
    return value * (1.0 + rng.uniform(-pct, pct))


def seasonal_factor(d: dt.date, outdoor: bool) -> float:
    """Demand multiplier by time of year.

    The outdoor venue is strongly seasonal (dead in winter, packed in July);
    the indoor venue is mildly counter-seasonal.
    """
    doy = d.timetuple().tm_yday
    # peak around mid-July (day 196)
    summer = math.cos((doy - 196) / 365.0 * 2 * math.pi)
    if outdoor:
        return max(0.0, 0.55 + 0.75 * summer)
    return 1.0 - 0.18 * summer


def weekday_factor(d: dt.date, outdoor: bool) -> float:
    """Demand multiplier by day of week (Mon=0)."""
    if outdoor:
        table = [0.42, 0.46, 0.58, 0.74, 1.35, 1.55, 0.88]
    else:
        table = [0.55, 0.62, 0.72, 0.86, 1.42, 1.48, 0.70]
    return table[d.weekday()]


def growth_factor(d: dt.date, start: dt.date, annual_growth: float = 0.11) -> float:
    """Slow underlying growth so year-on-year comparisons are meaningful."""
    years = (d - start).days / 365.0
    return (1.0 + annual_growth) ** years


def is_open(d: dt.date, venue: str) -> bool:
    """The outdoor venue closes for the winter; the indoor one closes Mondays
    in the off season and for a short summer break."""
    if venue == "Garden":
        # open April through October
        if d.month < 4 or d.month > 10:
            return False
        # closed Mondays and Tuesdays in the shoulder months
        if d.month in (4, 10) and d.weekday() in (0, 1):
            return False
        return True
    # Corner
    if d.month in (7, 8) and d.weekday() == 0:
        return False  # summer Mondays off
    if d.month in (1, 2) and d.weekday() == 0:
        return False
    if (d.month, d.day) in ((1, 1), (1, 2), (12, 25)):
        return False
    return True


def hour_curve(hour: int, outdoor: bool) -> float:
    """Share of the day's trade falling in a given hour slot."""
    if outdoor:
        curve = {
            10: 0.01, 11: 0.02, 12: 0.03, 13: 0.04, 14: 0.04, 15: 0.05,
            16: 0.06, 17: 0.07, 18: 0.09, 19: 0.11, 20: 0.12, 21: 0.12,
            22: 0.10, 23: 0.07, 0: 0.04, 1: 0.02, 2: 0.01,
        }
    else:
        curve = {
            9: 0.02, 10: 0.03, 11: 0.04, 12: 0.05, 13: 0.05, 14: 0.05,
            15: 0.05, 16: 0.06, 17: 0.07, 18: 0.08, 19: 0.10, 20: 0.11,
            21: 0.11, 22: 0.09, 23: 0.06, 0: 0.03,
        }
    return curve.get(hour, 0.0)


def open_hours(outdoor: bool):
    if outdoor:
        return [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2]
    return [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0]


def now_stamp(d: dt.date | None = None) -> str:
    """A plausible ingestion timestamp: the morning after the given date."""
    if d is None:
        return "2026-10-20 08:00:00"
    stamp = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(7, 30))
    return stamp.strftime("%Y-%m-%d %H:%M:%S")
