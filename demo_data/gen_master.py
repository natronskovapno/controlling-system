"""Master data and dimension tables."""

from __future__ import annotations

import datetime as dt

from . import catalog as C
from .helpers import DAYS_EN, DAYS_SR, MONTHS_SR, daterange, iso, now_stamp


def build_date_table(con, start: dt.date, end: dt.date) -> None:
    rows = []
    for d in daterange(start - dt.timedelta(days=400), end + dt.timedelta(days=400)):
        rows.append((
            iso(d),
            DAYS_EN[d.weekday()],
            DAYS_SR[d.weekday()],
            int(d.strftime("%W")),
            d.month,
            MONTHS_SR[d.month - 1],
            (d.month - 1) // 3 + 1,
            d.year,
            1 if d.weekday() >= 5 else 0,
        ))
    con.executemany(
        "INSERT OR REPLACE INTO date_table (date, day_of_week, day_of_week_sr, "
        "week_number, month, month_name_sr, quarter, year, is_weekend) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)


def build_products(con) -> dict:
    """Insert categories, products, recipes and warehouses.

    Returns a lookup {product_name: dict(...)} used by the sales generator.
    """
    con.executemany(
        "INSERT INTO product_categories (category_id, name, is_active, updated_at, parent_id) "
        "VALUES (?,?,1,?,?)",
        [(cid, name, now_stamp(), parent) for cid, name, parent in C.CATEGORIES])

    con.executemany(
        "INSERT INTO stock_warehouses (warehouse_id, code, name, is_active, updated_at, location_name) "
        "VALUES (?,?,?,1,?,?)",
        [(wid, code, name, now_stamp(), loc) for wid, code, name, loc in C.WAREHOUSES])

    lookup = {}
    rows = []
    for pid, (cat, name, unit, vat, gross, cost, pop) in enumerate(C.PRODUCTS, start=101):
        code = f"P{pid}"
        cat_name = dict((c[0], c[1]) for c in C.CATEGORIES)[cat]
        rows.append((pid, name, code, cat, cat_name, unit, vat, gross, 1, now_stamp(), cost))
        lookup[name] = {
            "product_id": pid, "code": code, "category_id": cat, "category_name": cat_name,
            "unit": unit, "vat": vat, "gross": gross, "cost_net": cost, "pop": pop,
            "name": name,
        }
    con.executemany(
        "INSERT INTO products (product_id, name, code, category_id, category_name, "
        "unit_measure, tax_percent, selling_price, is_active, updated_at, input_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    comp_rows = []
    for parent, child, qty, shrink in C.RECIPES:
        p, ch = lookup[parent], lookup[child]
        comp_rows.append((p["product_id"], p["code"], ch["product_id"], ch["code"],
                          qty, shrink, now_stamp(), ch["product_id"], ch["unit"]))
    con.executemany(
        "INSERT INTO product_components (parent_id, parent_code, child_id, child_code, "
        "quantity, shrinkage_pct, updated_at, component_id, unit) VALUES (?,?,?,?,?,?,?,?,?)",
        comp_rows)

    con.executemany(
        "INSERT INTO passthrough_products (name, note) VALUES (?,?)",
        [(n, "Sold on behalf of a third party; excluded from margin analysis.")
         for n in C.PASSTHROUGH])

    return lookup


def build_people(con, start: dt.date) -> list[dict]:
    """Insert employees, bracket history and salary records."""
    staff = []
    emp_rows, hist_rows, sal_rows = [], [], []
    for eid, (name, role, bracket, salary) in enumerate(C.EMPLOYEES, start=1):
        emp_rows.append((eid, name, bracket, role, None))
        hist_rows.append((name, bracket, iso(start), "Initial record"))
        if salary:
            sal_rows.append((name, role, salary, iso(start), None,
                             round(salary * C.BRACKET_MULTIPLIER[bracket])))
        staff.append({"id": eid, "name": name, "role": role,
                      "bracket": bracket, "salary": salary})

    con.executemany(
        "INSERT INTO employees (id, name, tax_bracket, notes, birth_date) VALUES (?,?,?,?,?)",
        emp_rows)
    con.executemany(
        "INSERT INTO employee_bracket_history (employee_name, bracket, valid_from, notes) "
        "VALUES (?,?,?,?)", hist_rows)
    con.executemany(
        "INSERT INTO salaries_monthly (employee_name, role, monthly_amount, valid_from, "
        "valid_to, monthly_gross) VALUES (?,?,?,?,?,?)", sal_rows)
    return staff


def build_tags(con) -> dict:
    """Insert event type tags and fixed cost tags; return {tag_name: tag_id}."""
    con.executemany("INSERT INTO event_type_tags (name) VALUES (?)",
                    [(t,) for t in C.EVENT_TYPES])

    tag_ids = {}
    for tid, (name, kind, submodule, venue) in enumerate(C.FIXED_COST_TAGS, start=1):
        con.execute(
            "INSERT INTO fixed_cost_tags (tag_id, name, type, description, active, "
            "submodule, venue) VALUES (?,?,?,?,1,?,?)",
            (tid, name, kind, f"{kind.title()} tag: {name}", submodule, venue))
        tag_ids[name] = tid
    return tag_ids
