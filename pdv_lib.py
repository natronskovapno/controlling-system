"""
pdv_lib.py — shared PDV (VAT) computation
=========================================

SINGLE SOURCE OF TRUTH for the PDV monthly overview.

WHY IT EXISTS
-------------
The PDV pregled was previously computed independently in two places:

    flask_app/blueprints/pdv.py   -> the dashboard
    scripts/db_dump.py            -> computed_pdv_pregled.csv

The two copies drifted three separate times. Most recently the Storno/Rejected
filter was added to pdv.py but not to db_dump.py, so the dashboard and the
exported CSV disagreed by 813,517 RSD across eleven months, and the CSV was the
wrong one. db_dump.py also never gained the entity-aware parking split, so a
brewery income link would have vanished from the CSV.

Both now call get_pregled() from this module. There is one implementation.

WHERE IT LIVES
--------------
Project root, alongside config.py, because the two consumers sit in different
folders (flask_app/blueprints/ and scripts/) and both already put the project
root on sys.path.

USAGE
-----
    import pdv_lib
    rows = pdv_lib.get_pregled(conn)                       # all months
    rows = pdv_lib.get_pregled(conn, "2026-01-01", "2026-08-31")

The connection must have row_factory = sqlite3.Row.

Each returned row is a dict with these keys:
    month, label,
    ak_ulazni, ak_izlazni, ak_pazar, ak_parking, ak_net,
    mp_ulazni, mp_izlazni, mp_pazar, brewery_parking, mp_net,
    total_ulazni, total_izlazni, total_pazar, total_parking, total_net

NOTE ON KEY NAMES
-----------------
pdv.py has always used "ak_net"/"mp_net"/"total_net" internally while
db_dump.py writes CSV columns named ak_neto/mp_neto/total_neto. The library
keeps the pdv.py names; db_dump.py maps them when writing the CSV, so the
existing CSV column headers are unchanged.

STREAMS
-------
  ulazni   invoices_in.pdv_amount           by date_received
  izlazni  invoices_out.pdv_amount          by date_issued
  pazar    bills_raw gross minus zero-VAT ticket items, x 20/120 (main)
  parking  invoice_fixed_cost_links, income tags with vat_rate > 0
  mp pazar brewery_transactions credits, excluding internal transfers

  neto = pazar + izlazni + parking - ulazni
"""

# Month names resolve through i18n so they follow the active language.
# Imported lazily: this module is also used by scripts that run outside Flask.
def _months():
    try:
        from i18n import months
        return months()
    except Exception:
        return ["januar", "februar", "mart", "april", "maj", "jun",
                "jul", "avgust", "septembar", "oktobar", "novembar", "decembar"]

DATA_FROM = "2025-01-01"  # Hard floor — no pre-2025 data

# ---------------------------------------------------------------------------
# Entity resolution for invoice_fixed_cost_links
#
# A link hangs off one of four source tables (source_type / source_id). None of
# them is guaranteed to be main, so the entity must be read from the
# source row instead of assumed. Both the pregled and stavke parking queries
# use these two fragments so the join and the expression can never drift apart.
#
# The fallback to 'main' applies only to orphaned links whose source row
# no longer exists, which reproduces the old hardcoded behaviour for that case.
# ---------------------------------------------------------------------------
LINK_ENTITY_JOIN = """
        LEFT JOIN bank_transactions lbt
               ON l.source_type = 'bank_transaction'
              AND lbt.transaction_id = l.source_id
        LEFT JOIN invoices_out lio
               ON l.source_type = 'invoice_out'
              AND lio.invoice_id = l.source_id
        LEFT JOIN invoices_in lii
               ON l.source_type = 'invoice_in'
              AND lii.invoice_id = l.source_id
"""

LINK_ENTITY_EXPR = (
    "COALESCE(lbt.entity, lio.entity, lii.entity, 'main')"
)

# ---------------------------------------------------------------------------
# Cancelled-invoice filter
#
# invoices_in / invoices_out retain rows for invoices that were cancelled
# (Storno) or refused by the counterparty (Rejected). Neither creates a VAT
# obligation or a deduction right, so both must be excluded from every PDV
# stream. Without this the cancelled original AND its reissued replacement are
# both counted — e.g. SIKA Rc 47/26 (Storno, 98,168) alongside AR 7/26 +
# Rc 48/26 which together carry the same 98,168.
#
# COALESCE keeps manually-entered invoices, which carry an empty status and are
# legitimate. 'Approved', 'New', 'Seen' and 'Sent' are all retained.
# ---------------------------------------------------------------------------
VALID_INVOICE_STATUS = (
    "COALESCE(invoice_status, '') NOT IN ('Storno', 'Rejected')"
)


def _month_label(ym):
    try:
        y, m = ym.split("-")
        return f"{_months()[int(m)-1]} {y}"
    except Exception:
        return ym


def _ef(date_from):
    return max(date_from, DATA_FROM) if date_from else DATA_FROM


def get_pregled(db, date_from=None, date_to=None):
    ef = _ef(date_from)

    # Ulazni — invoices_in bucketed by date_received (prijem)
    w_in = ("WHERE pdv_amount IS NOT NULL AND pdv_amount != 0 "
            f"AND {VALID_INVOICE_STATUS} AND date_received >= ?")
    p_in = [ef]
    if date_to: w_in += " AND date_received <= ?"; p_in.append(date_to)

    rows_in = db.execute(f"""
        SELECT strftime('%Y-%m', date_received) AS month,
               entity,
               ROUND(SUM(pdv_amount), 0) AS pdv
        FROM invoices_in {w_in}
        GROUP BY month, entity ORDER BY month
    """, p_in).fetchall()

    # Izlazni — invoices_out by date_issued
    # Outgoing VAT is periodised by the MANUAL TAG when one exists.
    #
    # Policy of 9 Sep 2026: an outgoing invoice carrying a fixed-cost link is
    # assigned to the link's year/month, which overrides date_issued. The link
    # is a deliberate human statement about which period the money belongs to;
    # date_issued is only a fallback for untagged invoices.
    #
    # The case this exists for: a drinks invoice with a supply date of 5 Aug
    # issued on 1 Sep. Tagged to 2026-08, its VAT lands in August rather than
    # September.
    #
    # An invoice may carry SEVERAL links across different months -- a parking
    # invoice covering a quarter, split evenly, say. Its VAT is allocated
    # PROPORTIONALLY by link amount, so it is never double-counted.
    # Where the link amounts sum to zero the split is equal instead.
    #
    # NOTE: this applies to invoices_out only. Incoming invoices also carry
    # links, but those assign the COST period for fixed-cost reporting, which
    # is not the same question as when input VAT may be deducted. Ulazni still
    # periodises by date_received.
    alloc = """
        LEFT JOIN (
            SELECT l.source_id                                AS inv_id,
                   printf('%04d-%02d', l.year, l.month)       AS tag_month,
                   l.amount                                   AS w,
                   (SELECT SUM(l2.amount) FROM invoice_fixed_cost_links l2
                     WHERE l2.source_type = 'invoice_out'
                       AND l2.source_id = l.source_id)        AS wtot,
                   (SELECT COUNT(*) FROM invoice_fixed_cost_links l3
                     WHERE l3.source_type = 'invoice_out'
                       AND l3.source_id = l.source_id)        AS n_links
            FROM invoice_fixed_cost_links l
            WHERE l.source_type = 'invoice_out'
        ) la ON la.inv_id = o.invoice_id
    """
    # share of the invoice's VAT that belongs to this link's month
    share = ("CASE WHEN la.inv_id IS NULL THEN 1.0 "
             "     WHEN la.wtot > 0       THEN la.w / la.wtot "
             "     ELSE 1.0 / la.n_links END")
    eff_month = "COALESCE(la.tag_month, strftime('%Y-%m', o.date_issued))"

    w_out = ("WHERE o.pdv_amount IS NOT NULL AND o.pdv_amount != 0 "
             f"AND {VALID_INVOICE_STATUS.replace('invoice_status', 'o.invoice_status')}")
    p_out = []

    # The period floor/ceiling applies to the EFFECTIVE month, not date_issued,
    # so an invoice tagged into a period inside the window is not dropped just
    # because it happens to have been issued outside it.
    having = f"HAVING {eff_month} >= ?"
    p_out.append(ef[:7])
    if date_to:
        having += f" AND {eff_month} <= ?"
        p_out.append(date_to[:7])

    rows_out = db.execute(f"""
        SELECT {eff_month} AS month,
               o.entity     AS entity,
               ROUND(SUM(o.pdv_amount * ({share})), 0) AS pdv
        FROM invoices_out o
        {alloc}
        {w_out}
        GROUP BY {eff_month}, o.entity
        {having}
        ORDER BY month
    """, p_out).fetchall()

    # Pazar — POS sales (main only)
    # Source: bills_raw (100% complete) minus zero-VAT ticket items from bill_items_raw
    # PDV = (bills_raw gross - ticket gross) x 20/120
    w_pos = "WHERE b.is_revoked = 0 AND b.date >= ?"
    p_pos = [ef]
    if date_to: w_pos += " AND b.date <= ?"; p_pos.append(date_to)

    rows_pos = db.execute(f"""
        SELECT strftime('%Y-%m', b.date) AS month,
               ROUND(
                 (SUM(b.amount) - COALESCE((
                   SELECT SUM(i.total_price)
                   FROM bill_items_raw i
                   JOIN bills_raw b2 ON b2.bill_id = i.bill_id
                   WHERE i.product_name = 'Tikets karta - ulaznica'
                     AND b2.is_revoked = 0
                     AND strftime('%Y-%m', b2.date) = strftime('%Y-%m', b.date)
                 ), 0)) * 20.0 / 120.0
               , 0) AS pdv
        FROM bills_raw b
        {w_pos}
        GROUP BY month ORDER BY month
    """, p_pos).fetchall()

    # Parking — fixed cost income links with vat_rate > 0
    # Entity is resolved from the linked source row rather than assumed to be
    # main: a link may hang off a bank transaction or an invoice belonging
    # to either entity. COALESCE falls back to 'main' only when the source
    # row cannot be resolved (orphaned link), preserving previous behaviour.
    w_park = f"""JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
        {LINK_ENTITY_JOIN}
        WHERE t.type = 'income' AND l.vat_rate > 0
          AND l.year || '-' || printf('%02d', l.month) >= ?"""
    p_park = [ef[:7]]
    if date_to:
        w_park += " AND l.year || '-' || printf('%02d', l.month) <= ?"
        p_park.append(date_to[:7])

    rows_park = db.execute(f"""
        SELECT printf('%04d-%02d', l.year, l.month) AS month,
               {LINK_ENTITY_EXPR} AS entity,
               ROUND(SUM(l.amount * l.vat_rate / (100.0 + l.vat_rate)), 0) AS pdv
        FROM invoice_fixed_cost_links l
        {w_park}
        GROUP BY l.year, l.month, {LINK_ENTITY_EXPR} ORDER BY l.year, l.month
    """, p_park).fetchall()

    # Small Brewery Pazar — all non-internal-transfer bank credit inflows are
    # retail/delivery sales revenue (no POS/fiscal POS integration exists
    # for this branch, so bank settlement is the only revenue record).
    # Internal capital transfers (loans, owner injections) are excluded via
    # the same purposecode=290 / 'CENTRALA' rule used in brewery.py.
    w_mppz = "WHERE entity = 'brewery' AND direction = 'credit' AND date >= ?"
    p_mppz = [ef]
    if date_to: w_mppz += " AND date <= ?"; p_mppz.append(date_to)
    w_mppz += """
        AND NOT (
            COALESCE(purposecode, '') = '290'
            OR UPPER(COALESCE(counterparty, '')) LIKE '%CENTRALA%'
        )"""

    rows_mppz = db.execute(f"""
        SELECT strftime('%Y-%m', date) AS month,
               ROUND(SUM(amount) * 20.0 / 120.0, 0) AS pdv
        FROM brewery_transactions
        {w_mppz}
        GROUP BY month ORDER BY month
    """, p_mppz).fetchall()

    months = sorted(set(
        [r["month"] for r in rows_in] +
        [r["month"] for r in rows_out] +
        [r["month"] for r in rows_pos] +
        [r["month"] for r in rows_park] +
        [r["month"] for r in rows_mppz]
    ))

    def idx(rows, key1, key2=None):
        d = {}
        for r in rows:
            k = (r[key1], r[key2]) if key2 else r[key1]
            d[k] = float(r["pdv"] or 0)
        return d

    in_idx   = idx(rows_in,   "month", "entity")
    out_idx  = idx(rows_out,  "month", "entity")
    pos_idx  = idx(rows_pos,  "month")
    park_idx = idx(rows_park, "month", "entity")
    mppz_idx = idx(rows_mppz, "month")

    result = []
    for m in months:
        ak_ul = in_idx.get((m, "main"), 0)
        ak_iz = out_idx.get((m, "main"), 0)
        ak_pz = pos_idx.get(m, 0)
        ak_pk = park_idx.get((m, "main"), 0)
        mp_ul = in_idx.get((m, "brewery"), 0)
        mp_iz = out_idx.get((m, "brewery"), 0)
        mp_pz = mppz_idx.get(m, 0)
        mp_pk = park_idx.get((m, "brewery"), 0)

        result.append({
            "month":  m,
            "label":  _month_label(m),
            "ak_ulazni":  ak_ul,
            "ak_izlazni": ak_iz,
            "ak_pazar":   ak_pz,
            "ak_parking": ak_pk,
            "ak_net":     ak_pz + ak_iz + ak_pk - ak_ul,
            "mp_ulazni":  mp_ul,
            "mp_izlazni": mp_iz,
            "mp_pazar":   mp_pz,
            "brewery_parking": mp_pk,
            "mp_net":     mp_iz + mp_pz + mp_pk - mp_ul,
            "total_ulazni":  ak_ul + mp_ul,
            "total_izlazni": ak_iz + mp_iz,
            "total_pazar":   ak_pz + mp_pz,
            "total_parking": ak_pk + mp_pk,
            "total_net":     (ak_pz + ak_iz + ak_pk + mp_iz + mp_pz + mp_pk) - (ak_ul + mp_ul),
        })

    return result
