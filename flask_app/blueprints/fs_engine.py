"""
blueprints/fs_engine.py
=======================
Finansijski izveštaji — calculation engine.

- Report line definitions (LINES) and default konto mapping (DEFAULT_MAPPING)
- Longest-prefix konto → report line resolution (table fs_mapping, user-editable)
- build(db, snapshot_id) -> balance sheet + income statement for one bruto bilans,
  with estimates for interim (non-closing) snapshots:
    * COGS (501+511)   = base-year ratio × revenue (60+61), offset against inventory
    * Depreciation (54) = base-year annual ÷ 12 × months, offset against fixed assets
    * Profit tax        = tax_rate × positive pre-tax result, offset against tax liabilities
  Base year = latest closing snapshot ending before the interim period starts.
"""
from datetime import date
from i18n import tr

EXCLUDE = "EXCLUDE"

# key, statement, label, group   (order = display order)
LINES = [
    # ── Bilans stanja: aktiva
    ("A_NEMAT",      "BS", "Nematerijalna imovina",                    "Stalna imovina"),
    ("A_NEKR",       "BS", "Nekretnine, postrojenja i oprema",         "Stalna imovina"),
    ("A_DUGFIN",     "BS", "Dugoročni finansijski plasmani",           "Stalna imovina"),
    ("A_ZALIHE",     "BS", "Zalihe",                                   "Obrtna imovina"),
    ("A_AVANSI",     "BS", "Dati avansi",                              "Obrtna imovina"),
    ("A_KUPCI",      "BS", "Potraživanja od kupaca",                   "Obrtna imovina"),
    ("A_DRUGA_POTR", "BS", "Druga potraživanja",                       "Obrtna imovina"),
    ("A_KRATKFIN",   "BS", "Kratkoročni finansijski plasmani",         "Obrtna imovina"),
    ("A_GOTOVINA",   "BS", "Gotovina i gotovinski ekvivalenti",        "Obrtna imovina"),
    ("A_PDV_AVR",    "BS", "PDV i aktivna vremenska razgraničenja",    "Obrtna imovina"),
    # ── Bilans stanja: pasiva
    ("P_OSN_KAP",    "BS", "Osnovni kapital",                          "Kapital"),
    ("P_NERASP",     "BS", "Neraspoređeni dobitak / gubitak ranijih godina", "Kapital"),
    ("P_REZULTAT",   "BS", "Rezultat tekuće godine",                   "Kapital"),
    ("P_DUG_KRED",   "BS", "Dugoročni krediti",                        "Dugoročne obaveze"),
    ("P_KRATK_KRED", "BS", "Kratkoročni krediti",                      "Kratkoročne obaveze"),
    ("P_POZAJMICE",  "BS", "Pozajmice vlasnika",                       "Kratkoročne obaveze"),
    ("P_OST_OBAV",   "BS", "Ostale kratkoročne obaveze",               "Kratkoročne obaveze"),
    ("P_AVANSI",     "BS", "Primljeni avansi",                         "Kratkoročne obaveze"),
    ("P_DOBAVLJACI", "BS", "Obaveze prema dobavljačima",               "Kratkoročne obaveze"),
    ("P_ZARADE",     "BS", "Obaveze za zarade",                        "Kratkoročne obaveze"),
    ("P_POREZI",     "BS", "PDV i ostali porezi",                      "Kratkoročne obaveze"),
    ("P_PVR",        "BS", "Pasivna vremenska razgraničenja",          "Kratkoročne obaveze"),
    # ── Bilans uspeha
    ("I_ROBA",       "IS", "Prihodi od prodaje robe",                  "Poslovni prihodi"),
    ("I_USLUGE",     "IS", "Prihodi od usluga",                        "Poslovni prihodi"),
    ("I_OST_POSL",   "IS", "Ostali poslovni prihodi",                  "Poslovni prihodi"),
    ("R_NABAVNA",    "IS", "Nabavna vrednost prodate robe",            "Poslovni rashodi"),
    ("R_MATERIJAL",  "IS", "Troškovi materijala i energije",           "Poslovni rashodi"),
    ("R_ZARADE",     "IS", "Troškovi zarada",                          "Poslovni rashodi"),
    ("R_ZAKUP",      "IS", "Troškovi zakupa",                          "Poslovni rashodi"),
    ("R_USLUGE",     "IS", "Ostali troškovi proizvodnih usluga",       "Poslovni rashodi"),
    ("R_AMORT",      "IS", "Amortizacija i rezervisanja",              "Poslovni rashodi"),
    ("R_NEMAT",      "IS", "Nematerijalni troškovi",                   "Poslovni rashodi"),
    ("I_FIN",        "IS", "Finansijski prihodi",                      "Finansijski"),
    ("R_FIN",        "IS", "Finansijski rashodi",                      "Finansijski"),
    ("I_OST",        "IS", "Ostali prihodi",                           "Ostali"),
    ("R_OST",        "IS", "Ostali rashodi",                           "Ostali"),
]
LINE_LABEL = {k: lbl for k, _, lbl, _ in LINES}
LINE_STATEMENT = {k: st for k, st, _, _ in LINES}
ASSET_KEYS = [k for k, st, _, _ in LINES if k.startswith("A_")]
LIAB_KEYS = [k for k, st, _, _ in LINES if k.startswith("P_")]
REV_KEYS = [k for k, st, _, _ in LINES if k.startswith("I_")]
EXP_KEYS = [k for k, st, _, _ in LINES if k.startswith("R_")]

DEFAULT_MAPPING = [
    # class-level fallbacks (flagged as "samo klasa" for review)
    ("0", "A_NEKR"), ("1", "A_ZALIHE"), ("2", "A_DRUGA_POTR"), ("3", "P_NERASP"),
    ("4", "P_OST_OBAV"), ("5", "R_NEMAT"), ("6", "I_OST"), ("7", EXCLUDE),
    # class 0
    ("01", "A_NEMAT"), ("02", "A_NEKR"), ("03", "A_NEKR"), ("04", "A_DUGFIN"), ("05", "A_DUGFIN"),
    # class 1
    ("10", "A_ZALIHE"), ("11", "A_ZALIHE"), ("12", "A_ZALIHE"), ("13", "A_ZALIHE"), ("14", "A_ZALIHE"),
    ("15", "A_AVANSI"),
    # class 2
    ("20", "A_KUPCI"), ("21", "A_DRUGA_POTR"), ("22", "A_DRUGA_POTR"), ("23", "A_KRATKFIN"),
    ("24", "A_GOTOVINA"), ("27", "A_PDV_AVR"), ("28", "A_PDV_AVR"),
    # class 3
    ("30", "P_OSN_KAP"), ("34", "P_NERASP"), ("35", "P_NERASP"),
    # class 4
    ("40", "P_DUG_KRED"), ("41", "P_DUG_KRED"), ("42", "P_KRATK_KRED"), ("429", "P_OST_OBAV"),
    ("4292", "P_POZAJMICE"), ("43", "P_DOBAVLJACI"), ("430", "P_AVANSI"), ("44", "P_OST_OBAV"),
    ("45", "P_ZARADE"), ("46", "P_ZARADE"), ("47", "P_POREZI"), ("48", "P_POREZI"), ("49", "P_PVR"),
    # class 5
    ("50", "R_NABAVNA"), ("51", "R_MATERIJAL"), ("52", "R_ZARADE"), ("53", "R_USLUGE"),
    ("533", "R_ZAKUP"), ("54", "R_AMORT"), ("55", "R_NEMAT"), ("56", "R_FIN"),
    ("57", "R_OST"), ("58", "R_OST"), ("59", "R_OST"), ("599", EXCLUDE),
    # class 6
    ("60", "I_ROBA"), ("61", "I_USLUGE"), ("62", "I_OST_POSL"), ("63", "I_OST_POSL"),
    ("64", "I_OST_POSL"), ("65", "I_OST_POSL"), ("66", "I_FIN"), ("67", "I_OST"),
    ("68", "I_OST"), ("69", "I_OST"), ("699", EXCLUDE),
]

DEFAULT_CASH_ACCOUNTS = [
    ("2410", "160-0000000001234-56", "Bank 1"),
    ("24100", "265-0000000009876-54", "Bank 2"),
    ("24102", "160-0000000005678-90", "Bank 3"),
]

CASH_GROUPS = [          # (prefix, label) for BB cash kontos without a bank statement link
    ("2419", "Prelazni računi — uplate pazara"),
    ("243", "Blagajne"),
    ("244", "Devizni računi"),
    ("248", "Ostala novčana sredstva"),
]

DEFAULT_PARAMS = [
    ("cogs_ratio_override", None, "COGS (501+511) / prihodi (60+61) — prazno = računa se iz zaključnog bilansa"),
    ("depreciation_annual_override", None, "Godišnja amortizacija — prazno = iz zaključnog bilansa"),
    ("tax_rate", "0.15", "Stopa poreza na dobit"),
]


# ── schema ───────────────────────────────────────────────────────────

def ensure_fs_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS fs_mapping (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        konto_prefix TEXT NOT NULL UNIQUE,
        report_line TEXT NOT NULL,
        note TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS fs_params (
        key TEXT PRIMARY KEY, value TEXT, note TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS fs_exclusions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL, konto TEXT NOT NULL, amount REAL NOT NULL,
        reason TEXT, created_at TEXT DEFAULT (datetime('now','localtime')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS fs_cash_accounts (
        konto TEXT PRIMARY KEY, account_number TEXT NOT NULL, note TEXT)""")
    if db.execute("SELECT COUNT(*) FROM fs_cash_accounts").fetchone()[0] == 0:
        db.executemany("INSERT OR IGNORE INTO fs_cash_accounts (konto, account_number, note) VALUES (?,?,?)",
                       DEFAULT_CASH_ACCOUNTS)
    if db.execute("SELECT COUNT(*) FROM fs_mapping").fetchone()[0] == 0:
        db.executemany("INSERT INTO fs_mapping (konto_prefix, report_line) VALUES (?,?)", DEFAULT_MAPPING)
    for k, v, n in DEFAULT_PARAMS:
        db.execute("INSERT OR IGNORE INTO fs_params (key, value, note) VALUES (?,?,?)", [k, v, n])
    db.commit()


def load_mapping(db):
    return {r["konto_prefix"]: r["report_line"] for r in
            db.execute("SELECT konto_prefix, report_line FROM fs_mapping").fetchall()}


def load_params(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM fs_params").fetchall()}


def resolve(konto, mapping):
    """Longest matching prefix → (report_line, matched_prefix)."""
    for n in range(len(konto), 0, -1):
        p = konto[:n]
        if p in mapping:
            return mapping[p], p
    return None, None


# ── helpers ──────────────────────────────────────────────────────────

def sr(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def _months(period_from, period_to):
    a, b = date.fromisoformat(period_from), date.fromisoformat(period_to)
    return (b.year - a.year) * 12 + b.month - a.month + 1


def _konto_is_value(line, is_closing):
    """Income-statement value of an analytic line: expense positive, revenue positive."""
    k = line["konto"]
    if k[0] == "5":
        return line["tp_d"] if is_closing else line["uk_d"] - line["uk_p"]
    return line["tp_p"] if is_closing else line["uk_p"] - line["uk_d"]


def opening_bs(db, snapshot_id):
    """Balance sheet from the opening-balance (PS) columns of a snapshot: {line_key: value}."""
    mapping = load_mapping(db)
    vals = {k: 0.0 for k, *_ in LINES}
    for ln in db.execute("SELECT konto, ps_d, ps_p FROM bb_lines WHERE snapshot_id=?", [snapshot_id]).fetchall():
        if ln["konto"][0] not in "01234":
            continue
        key, _ = resolve(ln["konto"], mapping)
        if key in (None, EXCLUDE):
            continue
        saldo = (ln["ps_d"] or 0) - (ln["ps_p"] or 0)
        vals[key] += saldo if key.startswith("A_") else -saldo
    vals["AKTIVA"] = sum(vals[k] for k in ASSET_KEYS)
    vals["PASIVA"] = sum(vals[k] for k in LIAB_KEYS)
    return vals


def _raw(db, snapshot_id, mapping):
    snap = dict(db.execute("SELECT * FROM bb_snapshots WHERE id=?", [snapshot_id]).fetchone())
    closing = bool(snap["is_closing"])
    lines = [dict(r) for r in db.execute(
        "SELECT konto, naziv, ps_d, ps_p, tp_d, tp_p, uk_d, uk_p FROM bb_lines WHERE snapshot_id=?",
        [snapshot_id]).fetchall()]
    vals = {k: 0.0 for k, *_ in LINES}
    konto_is = {}        # konto -> IS value (for ratio bases)
    detail = {k: [] for k, *_ in LINES}
    unmapped, class_only = [], []
    konto_341 = 0.0
    for ln in lines:
        key, prefix = resolve(ln["konto"], mapping)
        saldo = (ln["uk_d"] or 0) - (ln["uk_p"] or 0)
        if key is None:
            unmapped.append({**ln, "saldo": saldo})
            continue
        if key == EXCLUDE:
            continue
        if prefix is not None and len(prefix) == 1:
            class_only.append({**ln, "saldo": saldo, "line": key})
        if ln["konto"][:3] == "341":
            konto_341 += -saldo
        if ln["konto"][0] in "56":
            v = _konto_is_value(ln, closing)
            konto_is[ln["konto"]] = v
        elif key.startswith("A_"):
            v = saldo
        else:
            v = -saldo
        # current-year result account: in a closing snapshot 341 holds this year's result
        if closing and ln["konto"][:3] == "341":
            key = "P_REZULTAT"
        vals[key] += v
        detail[key].append({"konto": ln["konto"], "naziv": ln["naziv"], "value": v})
    return snap, vals, konto_is, detail, unmapped, class_only, konto_341


def _exclusions(db, year):
    return {r["konto"]: r["total"] for r in db.execute(
        "SELECT konto, SUM(amount) AS total FROM fs_exclusions WHERE year=? GROUP BY konto", [year]).fetchall()}


def _sum_prefix(konto_is, prefixes, excl=None):
    total = 0.0
    for k, v in konto_is.items():
        if any(k.startswith(p) for p in prefixes):
            total += v
    for k, amt in (excl or {}).items():
        if any(k.startswith(p) for p in prefixes):
            total -= amt
    return total


def base_year_params(db, mapping, params, interim_snap):
    """COGS ratio / split and annual depreciation from the latest closing snapshot before the period."""
    base = db.execute("""SELECT id, period_to FROM bb_snapshots
                         WHERE is_closing=1 AND period_to < ? ORDER BY period_to DESC LIMIT 1""",
                      [interim_snap["period_from"]]).fetchone()
    out = {"base_snapshot": None, "cogs_ratio": None, "share_501": None, "bb_cogs_base": None,
           "depr_annual": None, "notes": []}
    if base:
        _, _, kis, *_ = _raw(db, base["id"], mapping)
        year = int(base["period_to"][:4])
        excl = _exclusions(db, year)
        rev = _sum_prefix(kis, ("60", "61"), excl)
        c501 = _sum_prefix(kis, ("501",), excl)
        c511 = _sum_prefix(kis, ("511",), excl)
        out["base_snapshot"] = dict(base)
        if rev:
            out["cogs_ratio"] = (c501 + c511) / rev
        if c501 + c511:
            out["share_501"] = c501 / (c501 + c511)
        out["bb_cogs_base"] = c501 + c511
        out["depr_annual"] = _sum_prefix(kis, ("54",), excl)
        if excl:
            out["notes"].append(tr("Primenjena isključenja za {year}: {n} konta.", year=year, n=len(excl)))
    else:
        out["notes"].append("Nema zaključnog bilansa pre ovog perioda — procene nisu moguće bez ručnih parametara.")
    if params.get("cogs_ratio_override"):
        out["cogs_ratio"] = float(params["cogs_ratio_override"])
        out["notes"].append("COGS stopa: ručno podešena.")
    if params.get("depreciation_annual_override"):
        out["depr_annual"] = float(params["depreciation_annual_override"])
        out["notes"].append("Godišnja amortizacija: ručno podešena.")
    if out["share_501"] is None:
        out["share_501"] = 0.5
    return out



# ── POS (system) data ────────────────────────────────────────────

def pos_totals(db, date_from, date_to):
    """POS net sales and net COGS between two ISO dates (inclusive). None if table missing."""
    try:
        r = db.execute("""SELECT COALESCE(SUM(sales_net),0) AS s, COALESCE(SUM(cogs_net),0) AS c
                          FROM cogs_daily WHERE date BETWEEN ? AND ?""", [date_from, date_to]).fetchone()
    except Exception:  # noqa: BLE001  (table absent on a fresh DB)
        return None
    return {"sales": r["s"] or 0.0, "cogs": r["c"] or 0.0}


def pos_monthly_sales(db, year):
    """{month: net sales} for a calendar year from POS."""
    try:
        rows = db.execute("""SELECT CAST(strftime('%m', date) AS INTEGER) AS m, SUM(sales_net) AS s
                             FROM cogs_daily WHERE strftime('%Y', date) = ? GROUP BY m""", [str(year)]).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {r["m"]: r["s"] or 0.0 for r in rows}



# ── cash control ─────────────────────────────────────────────────────

def bank_accounts(db):
    try:
        return [r[0] for r in db.execute(
            "SELECT DISTINCT account_number FROM account_balances ORDER BY account_number").fetchall()]
    except Exception:  # noqa: BLE001
        return []


def cash_control(db, snapshot_id):
    """BB cash kontos (24x) against bank statement closing balances at the snapshot date."""
    ensure_fs_tables(db)
    snap = db.execute("SELECT period_to FROM bb_snapshots WHERE id=?", [snapshot_id]).fetchone()
    d = snap["period_to"]
    links = {r["konto"]: dict(r) for r in db.execute("SELECT * FROM fs_cash_accounts").fetchall()}
    lines = [dict(r) for r in db.execute(
        "SELECT konto, naziv, uk_d - uk_p AS saldo FROM bb_lines WHERE snapshot_id=? AND konto LIKE '24%' ORDER BY konto",
        [snapshot_id]).fetchall()]
    linked, groups, other = [], {}, []
    used_accounts = set()
    for ln in lines:
        link = links.get(ln["konto"])
        if link:
            used_accounts.add(link["account_number"])
            try:
                b = db.execute("""SELECT closing_balance, balance_date FROM account_balances
                                  WHERE account_number=? AND balance_date<=? ORDER BY balance_date DESC LIMIT 1""",
                               [link["account_number"], d]).fetchone()
            except Exception:  # noqa: BLE001
                b = None
            bank = b["closing_balance"] if b else None
            stale = (bool(b) and (date.fromisoformat(d) - date.fromisoformat(b["balance_date"])).days > 7
                     and max(abs(b["closing_balance"]), abs(ln["saldo"])) >= 1000)   # dormant near-zero accounts are fine
            linked.append({**ln, "account": link["account_number"], "label": link["note"],
                           "bank": bank, "bank_date": b["balance_date"] if b else None,
                           "diff": (ln["saldo"] - bank) if bank is not None else None, "stale": stale})
            continue
        grp = next((lbl for p, lbl in CASH_GROUPS if ln["konto"].startswith(p)), None)
        if grp:
            g = groups.setdefault(grp, {"label": grp, "saldo": 0.0, "kontos": []})
            g["saldo"] += ln["saldo"]
            g["kontos"].append(ln["konto"])
        else:
            other.append(ln)
    # bank accounts with a balance at the date but not linked to any konto
    unlinked = []
    for acc in bank_accounts(db):
        if acc in used_accounts:
            continue
        b = db.execute("""SELECT closing_balance, balance_date FROM account_balances
                          WHERE account_number=? AND balance_date<=? ORDER BY balance_date DESC LIMIT 1""",
                       [acc, d]).fetchone()
        if b and abs(b["closing_balance"]) >= 1:
            unlinked.append({"account": acc, "bank": b["closing_balance"], "bank_date": b["balance_date"]})
    diff = sum(x["diff"] for x in linked if x["diff"] is not None)
    return {"date": d, "linked": linked, "groups": list(groups.values()), "other": other,
            "unlinked": unlinked, "diff": diff,
            "bb_total": sum(x["saldo"] for x in lines),
            "bank_total": sum(x["bank"] or 0 for x in linked)}

# ── main entry ───────────────────────────────────────────────────────

def build(db, snapshot_id, apply_estimates=True):
    ensure_fs_tables(db)
    mapping = load_mapping(db)
    params = load_params(db)
    snap, vals, konto_is, detail, unmapped, class_only, konto_341 = _raw(db, snapshot_id, mapping)
    closing = bool(snap["is_closing"])
    est = {k: 0.0 for k in vals}
    estimates, checks, notes = [], [], []

    rev_core = vals["I_ROBA"] + vals["I_USLUGE"]
    bp = None
    if not closing and apply_estimates:
        bp = base_year_params(db, mapping, params, snap)
        notes += bp["notes"]
        months = _months(snap["period_from"], snap["period_to"])
        booked_cogs = _sum_prefix(konto_is, ("501", "511"))
        if bp["cogs_ratio"] is not None:
            target = bp["cogs_ratio"] * rev_core
            add = max(target - booked_cogs, 0.0)
            if add:
                est["R_NABAVNA"] += add * bp["share_501"]
                est["R_MATERIJAL"] += add * (1 - bp["share_501"])
                est["A_ZALIHE"] -= add
                estimates.append({"name": "Nabavna vrednost robe i materijala",
                                  "amount": add, "basis": f"{bp['cogs_ratio']*100:.1f}% × prihodi (60+61), umanjeno za već proknjiženo",
                                  "offset": "Zalihe"})
        # control: POS COGS × (base-year BB COGS / base-year POS COGS)
        if bp["base_snapshot"] and bp["cogs_ratio"] is not None:
            base = db.execute("SELECT period_from, period_to FROM bb_snapshots WHERE id=?",
                              [bp["base_snapshot"]["id"]]).fetchone()
            pos_base = pos_totals(db, base["period_from"], base["period_to"])
            pos_now = pos_totals(db, snap["period_from"], snap["period_to"])
            if pos_base and pos_now and pos_base["cogs"] and bp.get("bb_cogs_base"):
                mult = bp["bb_cogs_base"] / pos_base["cogs"]
                alt = pos_now["cogs"] * mult
                primary = max(bp["cogs_ratio"] * rev_core, booked_cogs)
                dev = (primary - alt) / alt if alt else 0.0
                checks.append({"name": tr("COGS: kontrola preko POS-a"), "ok": abs(dev) <= 0.10,
                               "detail": tr("procena {est} / POS {pos} × {mult} = {alt} (odstupanje {dev})",
                                            est=sr(primary), pos=sr(pos_now["cogs"]),
                                            mult=f"{mult:.2f}", alt=sr(alt),
                                            dev=f"{dev*100:+.1f}%")})
            else:
                checks.append({"name": tr("COGS: kontrola preko POS-a"), "ok": False,
                               "detail": tr("nema POS podataka za osnovnu godinu ili period")})
        booked_depr = _sum_prefix(konto_is, ("54",))
        if bp["depr_annual"]:
            add = max(bp["depr_annual"] / 12 * months - booked_depr, 0.0)
            if add:
                est["R_AMORT"] += add
                est["A_NEKR"] -= add
                estimates.append({"name": "Amortizacija", "amount": add,
                                  "basis": f"godišnje {sr(bp['depr_annual'])} ÷ 12 × {months} mes.",
                                  "offset": "Nekretnine, postrojenja i oprema"})

    tot = {k: vals[k] + est[k] for k in vals}
    rev = sum(tot[k] for k in REV_KEYS)
    exp = sum(tot[k] for k in EXP_KEYS)
    posl_prih = tot["I_ROBA"] + tot["I_USLUGE"] + tot["I_OST_POSL"]
    posl_rash = sum(tot[k] for k in ("R_NABAVNA", "R_MATERIJAL", "R_ZARADE", "R_ZAKUP", "R_USLUGE", "R_AMORT", "R_NEMAT"))
    ebt = rev - exp
    tax_rate = float(params.get("tax_rate") or 0.15)

    if closing:
        net = konto_341
        tax = ebt - net
        checks.append({"name": "Neto rezultat = konto 341", "ok": True,
                       "detail": tr("porez na dobit izveden kao razlika:") + f" {sr(tax)}"})
    else:
        tax = max(ebt, 0.0) * tax_rate if apply_estimates else 0.0
        net = ebt - tax
        if tax:
            est["P_POREZI"] += tax
            tot["P_POREZI"] += tax
            estimates.append({"name": tr("Porez na dobit"), "amount": tax,
                              "basis": tr("{rate} × rezultat pre oporezivanja",
                                          rate=f"{tax_rate*100:.0f}%"),
                              "offset": tr("PDV i ostali porezi")})
        tot["P_REZULTAT"] += net
        vals["P_REZULTAT"] += ebt   # unestimated result for the "no estimates" view

    aktiva = sum(tot[k] for k in ASSET_KEYS)
    pasiva = sum(tot[k] for k in LIAB_KEYS)
    checks.insert(0, {"name": tr("Aktiva = Pasiva"), "ok": abs(aktiva - pasiva) < 5,
                      "detail": tr("{a} / {p} (razlika {d})", a=sr(aktiva), p=sr(pasiva),
                                   d=sr(aktiva - pasiva))})
    checks.append({"name": tr("Nemapirana konta"), "ok": not unmapped,
                   "detail": tr("{n} konta", n=len(unmapped))})
    if not closing and estimates:
        checks.append({"name": tr("Zalihe posle procene ≥ 0"), "ok": tot["A_ZALIHE"] >= 0,
                       "detail": tr("{v} — negativno znači da procenjeni COGS premašuje nabavke",
                                    v=sr(tot["A_ZALIHE"]))})

    income = {
        "posl_prihodi": posl_prih, "posl_rashodi": posl_rash, "posl_rezultat": posl_prih - posl_rash,
        "fin_neto": tot["I_FIN"] - tot["R_FIN"], "ost_neto": tot["I_OST"] - tot["R_OST"],
        "ebt": ebt, "tax": tax, "net": net, "ebitda": posl_prih - posl_rash + tot["R_AMORT"],
        "revenue_total": rev,
    }
    return {
        "snapshot": snap, "closing": closing, "values": tot, "raw": vals, "estimated": est,
        "detail": detail, "income": income, "aktiva": aktiva, "pasiva": pasiva,
        "estimates": estimates, "base": bp, "checks": checks, "notes": notes,
        "unmapped": unmapped, "class_only": class_only,
    }
