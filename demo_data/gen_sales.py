"""POS sales: bills, bill lines and the daily/hourly aggregates built from them.

The bill lines are the single source of truth. Every aggregate table in here is
derived from them, exactly as the real ingestion pipeline derives them from the
POS API, so the demo dashboard's totals reconcile at every level.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import random

from . import catalog as C
from .helpers import (
    hour_curve, money, net_from_gross, now_stamp, open_hours, weighted_choice,
)

PAYMENT_TYPES = [("Cash", 46), ("Card", 44), ("Voucher", 4), ("Transfer", 6)]

# Staff who can appear as the operator on a bill
BAR_STAFF = ["Jelena Stankovic", "Nikola Savic", "Teodora Markovic",
             "Luka Djordjevic", "Sara Pavlovic", "Lazar Ilic", "Marko Ilic"]


def _basket_size(rng: random.Random, etype: str) -> int:
    if etype == "Private rental":
        return rng.choices([1, 2, 3, 4, 5, 6, 8], [10, 18, 22, 20, 14, 10, 6])[0]
    return rng.choices([1, 2, 3, 4, 5, 6], [26, 30, 20, 12, 8, 4])[0]


def generate(con, plan, products: dict, seed: int) -> None:
    rng = random.Random(seed + 7)

    # product pool weighted by popularity, with food skewed to daytime
    pool = [(p["name"], p["pop"]) for p in products.values()]
    food_names = {p["name"] for p in products.values() if p["category_id"] == 8}
    coffee_names = {p["name"] for p in products.values() if p["category_id"] == 7}

    bill_id = 10001
    receipt_counter = collections.Counter()

    bills_rows, item_rows = [], []
    # aggregates
    sales_hourly = collections.defaultdict(lambda: [0.0, 0.0, 0.0])      # qty, net, gross
    payment_hourly = collections.defaultdict(lambda: [0.0, 0])           # amount, bills
    inventory_daily = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # qty, gross, disc, cogs
    cogs_daily = collections.defaultdict(lambda: [0.0] * 6)
    reprez_rows = []

    for day in plan:
        d, venue, outdoor = day["date"], day["venue"], day["outdoor"]
        hours = open_hours(outdoor)
        wh = 1 if venue == "Garden" else 3
        computer = f"{venue.upper()[:3]}-POS-{rng.randint(1, 2)}"

        # distribute the day's bills across the open hours
        weights = [(h, hour_curve(h, outdoor)) for h in hours]
        day_gross = 0.0
        day_cogs = 0.0
        day_business_net = 0.0

        for _ in range(day["bills"]):
            hour = weighted_choice(rng, weights)
            minute, second = rng.randrange(60), rng.randrange(60)
            # a bill logged after midnight still belongs to the trading day
            stamp_date = d + dt.timedelta(days=1) if hour < 6 else d
            time_created = f"{stamp_date.isoformat()} {hour:02d}:{minute:02d}:{second:02d}"

            lines = []
            for seq in range(1, _basket_size(rng, day["event_type"]) + 1):
                name = weighted_choice(rng, pool)
                # nudge the catalogue toward what people actually order when
                if hour >= 20 and (name in coffee_names) and rng.random() < 0.75:
                    name = weighted_choice(rng, pool)
                if hour <= 14 and name in food_names and rng.random() < 0.3:
                    pass  # lunch food is fine
                elif hour >= 23 and name in food_names and rng.random() < 0.5:
                    name = weighted_choice(rng, pool)

                p = products[name]
                qty = 1.0
                if p["category_id"] in (1, 2, 3, 6) and rng.random() < 0.3:
                    qty = float(rng.randint(2, 4))

                gross_unit = p["gross"]
                discount = 0.0
                r = rng.random()
                if r < 0.03:
                    discount = money(gross_unit * qty * 0.10)   # staff/loyalty discount
                elif r < 0.045:
                    discount = money(gross_unit * qty * 0.20)   # promo
                total_gross = money(gross_unit * qty - discount)
                cogs_net = money(p["cost_net"] * qty)

                lines.append({
                    "p": p, "qty": qty, "price": gross_unit, "discount": discount,
                    "total": total_gross, "cogs": cogs_net, "seq": seq,
                })

            amount = money(sum(l["total"] for l in lines))
            if amount <= 0:
                continue

            revoked = 1 if rng.random() < 0.012 else 0

            # ~2% of bills are issued to a company (invoice_type = business)
            is_business = rng.random() < 0.02
            partner_name = partner_tax = None
            invoice_type = "normal"
            if is_business:
                client = rng.choice(C.CLIENTS)
                partner_name, partner_tax = client[0], client[1]
                invoice_type = "business"

            # ~0.6% are host/entertainment bills (reprezentacija): no payment taken
            is_reprez = (not is_business) and rng.random() < 0.006

            if is_reprez:
                payments = []
            else:
                n_pay = 1 if rng.random() < 0.94 else 2
                remaining = amount
                payments = []
                for i in range(n_pay):
                    ptype = weighted_choice(rng, PAYMENT_TYPES)
                    amt = remaining if i == n_pay - 1 else money(remaining * rng.uniform(0.3, 0.7))
                    payments.append({"name": ptype, "amount": amt})
                    remaining = money(remaining - amt)

            receipt_counter[(venue, d.year)] += 1
            rc = receipt_counter[(venue, d.year)]
            fiscal_no = f"{venue[:2].upper()}{d.year}-{rc:06d}"

            bills_rows.append((
                bill_id, fiscal_no, str(rc), time_created, d.isoformat(), hour,
                computer, venue, f"T{rng.randint(1, 42)}", rng.choice(BAR_STAFF),
                amount, revoked, None, invoice_type, partner_tax, partner_name,
                json.dumps(payments, ensure_ascii=False),
                1 if is_reprez else 0, now_stamp(d),
            ))

            for l in lines:
                p = l["p"]
                item_rows.append((
                    bill_id, d.isoformat(), venue, p["code"], p["name"], p["category_id"],
                    l["qty"], l["price"], p["vat"], l["total"], l["discount"],
                    p["cost_net"], money(p["cost_net"] * (1 + p["vat"] / 100.0)),
                    l["cogs"], dict((w[0], w[2]) for w in C.WAREHOUSES)[wh], now_stamp(d),
                    l["seq"],
                ))

                if revoked:
                    continue

                net = net_from_gross(l["total"], p["vat"])
                key = (d.isoformat(), venue, hour, p["name"], p["category_id"])
                agg = sales_hourly[key]
                agg[0] += l["qty"]; agg[1] += net; agg[2] += l["total"]

                ikey = (d.isoformat(), p["code"], p["name"], p["category_id"], venue, p["vat"])
                inv = inventory_daily[ikey]
                inv[0] += l["qty"]; inv[1] += l["total"]; inv[2] += l["discount"]; inv[3] += l["cogs"]

                ckey = (d.isoformat(), wh, p["product_id"], p["name"])
                cg = cogs_daily[ckey]
                cg[0] += l["qty"]
                cg[1] += l["total"]
                cg[2] += net
                cg[3] += l["cogs"] * (1 + p["vat"] / 100.0)
                cg[4] += l["cogs"]
                cg[5] = p["vat"]

                day_gross += l["total"]
                day_cogs += l["cogs"]
                if is_business:
                    day_business_net += net

            if not revoked:
                for pay in payments:
                    pk = (d.isoformat(), venue, hour, pay["name"])
                    ph = payment_hourly[pk]
                    ph[0] += pay["amount"]
                    ph[1] += 1

            if is_reprez and not revoked:
                vat_mix = 20.0
                reprez_rows.append((
                    bill_id, d.isoformat(), time_created, rng.choice(BAR_STAFF),
                    f"T{rng.randint(1, 42)}", computer, venue, amount,
                    json.dumps([], ensure_ascii=False), revoked, now_stamp(d),
                    net_from_gross(amount, vat_mix),
                    money(amount - net_from_gross(amount, vat_mix)),
                    None, None, 0.0,
                ))

            bill_id += 1

        day["sales_gross"] = money(day_gross)
        day["cogs_net"] = money(day_cogs)
        day["business_bills_net"] = money(day_business_net)

    # --- write bills and lines ---------------------------------------------
    con.executemany(
        "INSERT INTO bills_raw (bill_id, fiscal_receipt_number, fiscal_receipt_counter, "
        "time_created, date, hour_slot, computer_name, venue, table_name, user_full_name, "
        "amount, is_revoked, original_id, invoice_type, business_partner_tax, "
        "business_partner_name, payment_types_json, is_reprezentacija, ingested_at) "
        "VALUES (" + ",".join("?" * 19) + ")", bills_rows)

    con.executemany(
        "INSERT INTO bill_items_raw (bill_id, date, venue, product_code, product_name, "
        "category_id, quantity, price, tax_percent, total_price, discount_amount, "
        "input_price_net, input_price_gross, total_cogs_net, stock_warehouse, ingested_at, "
        "line_sequence) VALUES (" + ",".join("?" * 17) + ")", item_rows)

    # --- aggregates ---------------------------------------------------------
    con.executemany(
        "INSERT INTO sales_hourly (date, venue, hour_slot, product_name, category_id, "
        "quantity, revenue_net, revenue_gross, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(k[0], k[1], k[2], k[3], k[4], round(v[0], 3), money(v[1]), money(v[2]),
          now_stamp()) for k, v in sales_hourly.items()])

    con.executemany(
        "INSERT INTO payment_hourly (date, venue, hour_slot, payment_type_name, amount, "
        "bill_count, ingested_at) VALUES (?,?,?,?,?,?,?)",
        [(k[0], k[1], k[2], k[3], money(v[0]), v[1], now_stamp())
         for k, v in payment_hourly.items()])

    con.executemany(
        "INSERT INTO inventory_daily (date, product_code, product_name, category_id, venue, "
        "quantity_sold, revenue_gross, discount_amount, cogs_net, tax_percent, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(k[0], k[1], k[2], k[3], k[4], round(v[0], 3), money(v[1]), money(v[2]),
          money(v[3]), k[5], now_stamp()) for k, v in inventory_daily.items()])

    con.executemany(
        "INSERT INTO cogs_daily (date, warehouse_id, product_id, product_name, quantity, "
        "sales_gross, sales_net, sales_price_gross, sales_price_net, cogs_gross, cogs_net, "
        "unit_cost_gross, unit_cost_net, ingested_at) VALUES (" + ",".join("?" * 14) + ")",
        [(k[0], k[1], k[2], k[3], round(v[0], 3), money(v[1]), money(v[2]),
          money(v[1] / v[0]) if v[0] else 0, money(v[2] / v[0]) if v[0] else 0,
          money(v[3]), money(v[4]),
          money(v[3] / v[0]) if v[0] else 0, money(v[4] / v[0]) if v[0] else 0,
          now_stamp()) for k, v in cogs_daily.items()])

    con.executemany(
        "INSERT INTO reprezentacija (bill_id, date, time_created, user_full_name, table_name, "
        "computer_name, venue, amount_gross, payment_types_json, is_revoked, ingested_at, "
        "amount_net, pdv, vip_guest_id, vip_guest_name, amount_paid) "
        "VALUES (" + ",".join("?" * 16) + ")", reprez_rows)

    return len(bills_rows), len(item_rows)


def generate_stock_snapshots(con, plan, products: dict, seed: int) -> None:
    """Closing stock per product, taken at the end of each trading day.

    Modelled as a slow drift around a target level rather than a true ledger:
    the real system reads these from the POS, it does not compute them.
    """
    rng = random.Random(seed + 11)
    level = {name: rng.uniform(40, 320) for name in products}
    rows = []
    by_day = collections.defaultdict(list)
    for day in plan:
        by_day[day["date"]].append(day)

    for d in sorted(by_day):
        venues = by_day[d]
        busy = sum(x["demand"] for x in venues)
        for name, p in products.items():
            if p["category_id"] == 5:
                continue  # cocktails are built to order, not stocked
            sold = busy * p["pop"] * 0.06 * rng.uniform(0.7, 1.3)
            level[name] -= sold
            if level[name] < 25:
                level[name] += rng.uniform(120, 420)  # delivery arrives
            rows.append((
                d.isoformat(), p["product_id"], p["code"], p["name"], p["category_name"],
                p["category_name"], p["unit"], round(max(0.0, level[name]), 2),
                2, "Garden stock", now_stamp(d),
            ))

    con.executemany(
        "INSERT INTO inventory_stock_daily (date, product_id, product_code, product_name, "
        "category_name, group_name, unit_measure, closing_quantity, shift_id, warehouse_name, "
        "ingested_at) VALUES (" + ",".join("?" * 11) + ")", rows)

    last = max(by_day)
    con.execute(
        "INSERT INTO inventory_shift_cursor (last_completed_date, last_completed_shift, "
        "updated_at) VALUES (?,?,?)", (last.isoformat(), 2, now_stamp(last)))
