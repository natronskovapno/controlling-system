"""
blueprints/fs_report.py
=======================
Finansijski izveštaji — report assembly for the statements view and Excel export.

Columns: every closing snapshot (year-end, ascending) → selected interim snapshot (YTD, estimates
applied) → projected full year (income statement only).

Projection (per income-statement line):
    YTD value + base-year annual value × share of base-year POS sales falling in the remaining months
    Depreciation: YTD monthly run-rate × 12.  Profit tax: tax_rate × positive pre-tax result.
"""
from datetime import date

from blueprints import fs_engine as fe
from i18n import month_short, tr

BS_ROWS = [
    ("sec", "Aktiva", None),
    ("sub", "Stalna imovina", ["A_NEMAT", "A_NEKR", "A_DUGFIN"]),
    ("item", None, "A_NEMAT"), ("item", None, "A_NEKR"), ("item", None, "A_DUGFIN"),
    ("sub", "Obrtna imovina", ["A_ZALIHE", "A_AVANSI", "A_KUPCI", "A_DRUGA_POTR", "A_KRATKFIN", "A_GOTOVINA", "A_PDV_AVR"]),
    ("item", None, "A_ZALIHE"), ("item", None, "A_AVANSI"), ("item", None, "A_KUPCI"),
    ("item", None, "A_DRUGA_POTR"), ("item", None, "A_KRATKFIN"), ("item", None, "A_GOTOVINA"),
    ("item", None, "A_PDV_AVR"),
    ("tot", "Ukupna aktiva", "AKTIVA"),
    ("sec", "Pasiva", None),
    ("sub", "Kapital", ["P_OSN_KAP", "P_NERASP", "P_REZULTAT"]),
    ("item", None, "P_OSN_KAP"), ("item", None, "P_NERASP"), ("item", None, "P_REZULTAT"),
    ("sub", "Dugoročne obaveze", ["P_DUG_KRED"]),
    ("item", None, "P_DUG_KRED"),
    ("sub", "Kratkoročne obaveze", ["P_KRATK_KRED", "P_POZAJMICE", "P_OST_OBAV", "P_AVANSI",
                                    "P_DOBAVLJACI", "P_ZARADE", "P_POREZI", "P_PVR"]),
    ("item", None, "P_KRATK_KRED"), ("item", None, "P_POZAJMICE"), ("item", None, "P_OST_OBAV"),
    ("item", None, "P_AVANSI"), ("item", None, "P_DOBAVLJACI"), ("item", None, "P_ZARADE"),
    ("item", None, "P_POREZI"), ("item", None, "P_PVR"),
    ("tot", "Ukupna pasiva", "PASIVA"),
]

IS_ROWS = [
    ("sub", "Poslovni prihodi", "POSL_PRIH"),
    ("item", None, "I_ROBA"), ("item", None, "I_USLUGE"), ("item", None, "I_OST_POSL"),
    ("sub", "Poslovni rashodi", "POSL_RASH"),
    ("item", None, "R_NABAVNA"), ("item", None, "R_MATERIJAL"), ("item", None, "R_ZARADE"),
    ("item", None, "R_ZAKUP"), ("item", None, "R_USLUGE"), ("item", None, "R_AMORT"), ("item", None, "R_NEMAT"),
    ("tot", "Poslovni rezultat (EBIT)", "EBIT"),
    ("item", None, "I_FIN"), ("item", None, "R_FIN"), ("item", None, "I_OST"), ("item", None, "R_OST"),
    ("sub", "Rezultat pre oporezivanja", "EBT"),
    ("item", "Porez na dobit", "TAX"),
    ("tot", "Neto rezultat", "NET"),
    ("sub", "EBITDA", "EBITDA"),
]

POSL_R = ["R_NABAVNA", "R_MATERIJAL", "R_ZARADE", "R_ZAKUP", "R_USLUGE", "R_AMORT", "R_NEMAT"]


def _is_values(v, tax):
    out = dict(v)
    out["POSL_PRIH"] = v["I_ROBA"] + v["I_USLUGE"] + v["I_OST_POSL"]
    out["POSL_RASH"] = sum(v[k] for k in POSL_R)
    out["EBIT"] = out["POSL_PRIH"] - out["POSL_RASH"]
    out["EBT"] = out["EBIT"] + v["I_FIN"] - v["R_FIN"] + v["I_OST"] - v["R_OST"]
    out["TAX"] = tax
    out["NET"] = out["EBT"] - tax
    out["EBITDA"] = out["EBIT"] + v["R_AMORT"]
    return out


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days + 1


def _column_from_build(res, kind, title, sub):
    v = res["values"]
    iv = _is_values(v, res["income"]["tax"])
    bs = dict(v)
    bs["AKTIVA"], bs["PASIVA"] = res["aktiva"], res["pasiva"]
    est_keys = {k for k, x in res["estimated"].items() if abs(x) > 0.5}
    if kind == "ytd" and res["income"]["tax"]:
        est_keys |= {"TAX", "P_POREZI"}
    s = res["snapshot"]
    return {"kind": kind, "title": title, "sub": sub, "bs": bs, "is": iv, "est": est_keys,
            "days": _days(s["period_from"], s["period_to"]), "checks": res["checks"],
            "estimates": res["estimates"], "period_to": s["period_to"]}


def _projection(db, ytd_res, base_res, params):
    s = ytd_res["snapshot"]
    end = date.fromisoformat(s["period_to"])
    base_year = int(base_res["snapshot"]["period_to"][:4]) if base_res else None
    share, basis = (12 - end.month) / 12, "linearno"
    if base_year:
        monthly = fe.pos_monthly_sales(db, base_year)
        total = sum(monthly.values())
        if total:
            share = sum(val for m, val in monthly.items() if m > end.month) / total
            basis = f"POS {base_year}"
    y = ytd_res["values"]
    b = base_res["values"] if base_res else {k: 0.0 for k in y}
    p = {}
    for k, *_ in fe.LINES:
        if fe.LINE_STATEMENT[k] != "IS":
            continue
        p[k] = y[k] + b[k] * share
    p["R_AMORT"] = y["R_AMORT"] / end.month * 12 if end.month else y["R_AMORT"]
    tmp = _is_values(p, 0.0)
    tax = max(tmp["EBT"], 0.0) * float(params.get("tax_rate") or 0.15)
    iv = _is_values(p, tax)
    return {"kind": "proj", "title": f"Proj. {end.year}",
            "sub": f"YTD + {share*100:.0f}% {base_year or ''} ({basis})",
            "bs": None, "is": iv, "est": set(iv.keys()), "days": 365, "checks": [], "estimates": [],
            "share": share, "period_to": f"{end.year}-12-31"}


def _ratios(col):
    bs, iv, d = col["bs"], col["is"], col["days"]
    ann = 365 / d if d else 1
    out = {}

    def div(a, b):
        return a / b if b else None

    if bs:
        obrtna = sum(bs[k] for k in ["A_ZALIHE", "A_AVANSI", "A_KUPCI", "A_DRUGA_POTR", "A_KRATKFIN", "A_GOTOVINA", "A_PDV_AVR"])
        kratko = sum(bs[k] for k in ["P_KRATK_KRED", "P_POZAJMICE", "P_OST_OBAV", "P_AVANSI", "P_DOBAVLJACI", "P_ZARADE", "P_POREZI", "P_PVR"])
        kapital = bs["P_OSN_KAP"] + bs["P_NERASP"] + bs["P_REZULTAT"]
        fin_dug = bs["P_DUG_KRED"] + bs["P_KRATK_KRED"] + bs["P_POZAJMICE"]
        nabavke = iv["R_NABAVNA"] + iv["R_MATERIJAL"] + iv["R_USLUGE"] + iv["R_ZAKUP"] + iv["R_NEMAT"]
        out.update({
            "tekuci": div(obrtna, kratko),
            "ubrzani": div(obrtna - bs["A_ZALIHE"], kratko),
            "nok": obrtna - kratko,
            "kapital_ucesce": div(kapital, bs["PASIVA"]),
            "dug_ebitda": div(fin_dug, iv["EBITDA"] * ann) if iv["EBITDA"] > 0 else None,
            "dso": div(bs["A_KUPCI"], iv["POSL_PRIH"] * 1.2) * d if iv["POSL_PRIH"] else None,
            "dpo": div(bs["P_DOBAVLJACI"], nabavke * 1.2) * d if nabavke else None,
            "dio": div(bs["A_ZALIHE"], iv["R_NABAVNA"] + iv["R_MATERIJAL"]) * d if (iv["R_NABAVNA"] + iv["R_MATERIJAL"]) else None,
            "roa": div(iv["NET"] * ann, bs["AKTIVA"]),
            "roe": div(iv["NET"] * ann, kapital),
        })
        if None not in (out["dso"], out["dpo"], out["dio"]):
            out["ccc"] = out["dso"] + out["dio"] - out["dpo"]
    out["ebitda_marza"] = div(iv["EBITDA"], iv["POSL_PRIH"])
    out["neto_marza"] = div(iv["NET"], iv["POSL_PRIH"])
    out["bruto_marza"] = div(iv["POSL_PRIH"] - iv["R_NABAVNA"] - iv["R_MATERIJAL"], iv["POSL_PRIH"])
    return out


RATIO_ROWS = [
    ("sec", "Likvidnost", None, None),
    ("item", "Tekući racio", "tekuci", "x"),
    ("item", "Ubrzani racio", "ubrzani", "x"),
    ("item", "Neto obrtni kapital", "nok", "amount"),
    ("sec", "Zaduženost", None, None),
    ("item", "Učešće kapitala u pasivi", "kapital_ucesce", "pct"),
    ("item", "Finansijski dug / EBITDA", "dug_ebitda", "x"),
    ("sec", "Efikasnost (dani)", None, None),
    ("item", "Dani naplate potraživanja", "dso", "days"),
    ("item", "Dani plaćanja dobavljačima", "dpo", "days"),
    ("item", "Dani vezivanja zaliha", "dio", "days"),
    ("item", "Ciklus konverzije gotovine", "ccc", "days"),
    ("sec", "Profitabilnost", None, None),
    ("item", "Bruto marža", "bruto_marza", "pct"),
    ("item", "EBITDA marža", "ebitda_marza", "pct"),
    ("item", "Neto marža", "neto_marza", "pct"),
    ("item", "ROA (godišnje)", "roa", "pct"),
    ("item", "ROE (godišnje)", "roe", "pct"),
]


def build_report(db, ytd_id=None):
    fe.ensure_fs_tables(db)
    params = fe.load_params(db)
    snaps = [dict(r) for r in db.execute(
        "SELECT id, period_from, period_to, is_closing FROM bb_snapshots ORDER BY period_to").fetchall()]
    closings = [s for s in snaps if s["is_closing"]]
    interims = [s for s in snaps if not s["is_closing"]]
    ytd = next((s for s in interims if s["id"] == ytd_id), None) or (interims[-1] if interims else None)
    if ytd and closings and closings[-1]["period_to"] > ytd["period_to"]:
        ytd = None  # a newer closing bilans supersedes older interim ones

    cols, last_closing_res = [], None
    for s in closings:
        res = fe.build(db, s["id"])
        cols.append(_column_from_build(res, "closing", s["period_to"][:4], "zaključni bilans"))
        last_closing_res = res
    checks, estimates = [], []
    if ytd:
        res = fe.build(db, ytd["id"])
        d = date.fromisoformat(ytd["period_to"])
        cols.append(_column_from_build(res, "ytd", f"{d.day:02d}.{d.month:02d}.{d.year}", "YTD · BB + procene"))
        base = next((c for c in reversed(closings) if c["period_to"] < ytd["period_from"]), None)
        base_res = fe.build(db, base["id"]) if base else None
        cols.append(_projection(db, res, base_res, params))
        checks, estimates = list(res["checks"]), res["estimates"]
        cc = fe.cash_control(db, ytd["id"])
        checks.append({"name": tr("Gotovina: BB = bankovni izvodi (povezani računi)"),
                       "ok": abs(cc["diff"]) < 1000 and not any(x["stale"] for x in cc["linked"]),
                       "detail": tr("razlika") + f" {fe.sr(cc['diff'])}"
                                 + (" · zastareli izvodi" if any(x["stale"] for x in cc["linked"]) else "")})
    for c in cols:
        c["ratios"] = _ratios(c)

    def rows_for(defs, section):
        out = []
        for kind, label, key in defs:
            if kind == "sec":
                out.append({"kind": kind, "label": label, "cells": []})
                continue
            lbl = label or fe.LINE_LABEL[key]
            cells = []
            for c in cols:
                src = c[section]
                if src is None:
                    cells.append(None)
                    continue
                if isinstance(key, list):
                    val = sum(src[k] for k in key)
                    est = any(k in c["est"] for k in key)
                else:
                    val = src[key]
                    est = key in c["est"]
                base = src["AKTIVA"] if section == "bs" else src["POSL_PRIH"]
                cells.append({"v": val, "pct": (val / base * 100) if base else None,
                              "est": est and c["kind"] != "proj"})
            out.append({"kind": kind, "label": lbl, "cells": cells})
        return out

    ratio_rows = []
    for kind, label, key, fmt in RATIO_ROWS:
        cells = [] if kind == "sec" else [{"v": c["ratios"].get(key), "fmt": fmt} for c in cols]
        ratio_rows.append({"kind": kind, "label": label, "cells": cells, "fmt": fmt})

    cash = [{"title": c["title"], "sub": c["sub"], **fe.cash_control(db, s["id"])}
            for c, s in zip([c for c in cols if c["kind"] != "proj"], closings + ([ytd] if ytd else []))]

    return {"cash": cash, "cols": cols, "bs_rows": rows_for(BS_ROWS, "bs"), "is_rows": rows_for(IS_ROWS, "is"),
            "ratio_rows": ratio_rows, "interims": interims, "ytd": ytd, "checks": checks,
            "estimates": estimates}


# ── Tokovi gotovine (indirektna metoda) ──────────────────────────────

CF_ROWS = [
    ("sec", "Tokovi iz poslovnih aktivnosti", None),
    ("item", "Neto rezultat", "net"),
    ("item", "Amortizacija i rezervisanja", "amort"),
    ("item", "(Povećanje) / smanjenje zaliha", "d_zalihe"),
    ("item", "(Povećanje) / smanjenje datih avansa", "d_avansi_dati"),
    ("item", "(Povećanje) / smanjenje potraživanja od kupaca", "d_kupci"),
    ("item", "(Povećanje) / smanjenje drugih potraživanja, PDV i AVR", "d_druga"),
    ("item", "Povećanje / (smanjenje) obaveza prema dobavljačima", "d_dobavljaci"),
    ("item", "Povećanje / (smanjenje) primljenih avansa", "d_avansi_primljeni"),
    ("item", "Povećanje / (smanjenje) obaveza za zarade", "d_zarade"),
    ("item", "Povećanje / (smanjenje) poreskih i ostalih obaveza, PVR", "d_porezi"),
    ("tot", "Neto tok iz poslovnih aktivnosti", "op"),
    ("sec", "Tokovi iz investicionih aktivnosti", None),
    ("item", "Ulaganja u nekretnine, opremu i nematerijalnu imovinu", "capex"),
    ("item", "Finansijski plasmani", "d_plasmani"),
    ("tot", "Neto tok iz investicionih aktivnosti", "inv"),
    ("sec", "Tokovi iz aktivnosti finansiranja", None),
    ("item", "Dugoročni krediti", "d_dug_kred"),
    ("item", "Kratkoročni krediti", "d_kratk_kred"),
    ("item", "Pozajmice vlasnika", "d_pozajmice"),
    ("item", "Promene kapitala (raspodela, ispravke ranijih godina)", "d_kapital"),
    ("tot", "Neto tok iz aktivnosti finansiranja", "fin"),
    ("sec", "Promena gotovine", None),
    ("tot", "Neto promena gotovine", "total"),
    ("item", "Gotovina na početku perioda (BB)", "cash_open"),
    ("tot", "Gotovina na kraju perioda (BB)", "cash_close"),
    ("sub", "Kontrola: tok = promena salda gotovine", "check_diff"),
]


def _cf_column(db, snap_id, res, title, sub):
    o = fe.opening_bs(db, snap_id)
    c = res["values"]

    def d(k):
        return c[k] - o[k]

    net = res["income"]["net"]
    amort = c["R_AMORT"]
    v = {
        "net": net, "amort": amort,
        "d_zalihe": -d("A_ZALIHE"), "d_avansi_dati": -d("A_AVANSI"), "d_kupci": -d("A_KUPCI"),
        "d_druga": -(d("A_DRUGA_POTR") + d("A_PDV_AVR")),
        "d_dobavljaci": d("P_DOBAVLJACI"), "d_avansi_primljeni": d("P_AVANSI"), "d_zarade": d("P_ZARADE"),
        "d_porezi": d("P_POREZI") + d("P_PVR") + d("P_OST_OBAV"),
        "capex": -(d("A_NEKR") + d("A_NEMAT") + amort),
        "d_plasmani": -(d("A_DUGFIN") + d("A_KRATKFIN")),
        "d_dug_kred": d("P_DUG_KRED"), "d_kratk_kred": d("P_KRATK_KRED"), "d_pozajmice": d("P_POZAJMICE"),
        "d_kapital": d("P_OSN_KAP") + d("P_NERASP") + d("P_REZULTAT") - net,
        "cash_open": o["A_GOTOVINA"], "cash_close": c["A_GOTOVINA"],
    }
    v["op"] = sum(v[k] for k in ("net", "amort", "d_zalihe", "d_avansi_dati", "d_kupci", "d_druga",
                                 "d_dobavljaci", "d_avansi_primljeni", "d_zarade", "d_porezi"))
    v["inv"] = v["capex"] + v["d_plasmani"]
    v["fin"] = v["d_dug_kred"] + v["d_kratk_kred"] + v["d_pozajmice"] + v["d_kapital"]
    v["total"] = v["op"] + v["inv"] + v["fin"]
    v["check_diff"] = v["total"] - (v["cash_close"] - v["cash_open"])
    est = {"net", "d_zalihe", "d_porezi", "amort", "capex", "op", "inv", "total"} if res["estimates"] else set()
    s = res["snapshot"]
    return {"title": title, "sub": sub, "v": v, "est": est, "period_from": s["period_from"],
            "period_to": s["period_to"], "snapshot_id": snap_id}


def _bank_flows(db, date_from, date_to, accounts):
    """Tagged bank inflows / outflows for the period (information only)."""
    if not accounts:
        return None
    ph = ",".join("?" * len(accounts))
    try:
        rows = db.execute(f"""
            SELECT direction,
                   COALESCE(NULLIF(manual_tag, ''), NULLIF(auto_tag, ''), '(bez oznake)') AS tag,
                   COUNT(*) AS n, SUM(amount) AS total
            FROM bank_transactions
            WHERE date BETWEEN ? AND ? AND account IN ({ph}) AND COALESCE(is_internal_transfer, 0) = 0
            GROUP BY direction, tag ORDER BY direction, total DESC
        """, [date_from, date_to, *accounts]).fetchall()
    except Exception:  # noqa: BLE001
        return None
    out = {"credit": [], "debit": [], "credit_total": 0.0, "debit_total": 0.0,
           "credit_untagged": 0.0, "debit_untagged": 0.0}
    for r in rows:
        out[r["direction"]].append({"tag": r["tag"], "n": r["n"], "total": r["total"] or 0.0})
        out[r["direction"] + "_total"] += r["total"] or 0.0
        if r["tag"] == tr("(bez oznake)"):
            out[r["direction"] + "_untagged"] += r["total"] or 0.0
    return out


def build_cashflow(db, ytd_id=None):
    fe.ensure_fs_tables(db)
    snaps = [dict(r) for r in db.execute(
        "SELECT id, period_from, period_to, is_closing FROM bb_snapshots ORDER BY period_to").fetchall()]
    closings = [s for s in snaps if s["is_closing"]]
    interims = [s for s in snaps if not s["is_closing"]]
    ytd = next((s for s in interims if s["id"] == ytd_id), None) or (interims[-1] if interims else None)
    if ytd and closings and closings[-1]["period_to"] > ytd["period_to"]:
        ytd = None

    cols = []
    for s in closings:
        cols.append(_cf_column(db, s["id"], fe.build(db, s["id"]), s["period_to"][:4], "zaključni bilans"))
    if ytd:
        d = date.fromisoformat(ytd["period_to"])
        cols.append(_cf_column(db, ytd["id"], fe.build(db, ytd["id"]),
                               f"01.01–{d.day:02d}.{d.month:02d}.{d.year}", "YTD · BB + procene"))

    rows = []
    for kind, label, key in CF_ROWS:
        cells = [] if kind == "sec" else [{"v": c["v"][key], "est": key in c["est"]} for c in cols]
        rows.append({"kind": kind, "label": label, "cells": cells, "key": key})

    accounts = [r[0] for r in db.execute("SELECT DISTINCT account_number FROM fs_cash_accounts").fetchall()]
    bank = [{"title": c["title"], **(_bank_flows(db, c["period_from"], c["period_to"], accounts) or {})}
            for c in cols]
    checks = [{"name": c['title'] + ": " + tr("tok = promena gotovine"),
               "ok": abs(c["v"]["check_diff"]) < 5,
               "detail": tr("razlika") + f" {fe.sr(c['v']['check_diff'])}"} for c in cols]
    return {"cols": cols, "rows": rows, "bank": bank, "checks": checks, "interims": interims, "ytd": ytd}


# ── Po periodima i mesecima ──────────────────────────────────────────

PROFILE_LINES = {"I_ROBA", "I_USLUGE", "R_NABAVNA", "R_MATERIJAL"}   # follow POS monthly sales
# Month abbreviations resolve through i18n so the column headers follow the
# active language; see month_short().
IS_LINE_KEYS = [k for k, *_ in fe.LINES if fe.LINE_STATEMENT[k] == "IS"]


def _spread(total_by_line, tax, months, profile):
    """Split period totals over months: profile lines by POS sales weights, others evenly."""
    w_total = sum(profile.get(m, 0.0) for m in months)
    out = {}
    for m in months:
        even = 1 / len(months)
        w = (profile.get(m, 0.0) / w_total) if w_total else even
        vals = {k: total_by_line[k] * (w if k in PROFILE_LINES else even) for k in IS_LINE_KEYS}
        out[m] = (vals, tax * even)
    return out


def build_periods(db, year=None):
    fe.ensure_fs_tables(db)
    params = fe.load_params(db)
    snaps = [dict(r) for r in db.execute(
        "SELECT id, period_from, period_to, is_closing FROM bb_snapshots ORDER BY period_to").fetchall()]
    years = sorted({int(s["period_to"][:4]) for s in snaps})
    if not years:
        return {"years": [], "year": None}
    year = year if year in years else years[-1]
    ys = [s for s in snaps if s["period_to"][:4] == str(year)]
    closing = next((s for s in ys if s["is_closing"]), None)
    chain = [closing] if closing else [s for s in ys if not s["is_closing"]]
    profile = fe.pos_monthly_sales(db, year)

    periods, prev_vals, prev_tax, prev_month = [], {k: 0.0 for k in IS_LINE_KEYS}, 0.0, 0
    last_res = None
    for s in chain:
        res = fe.build(db, s["id"])
        last_res = res
        cum = res["values"]
        tax = res["income"]["tax"]
        end_m = date.fromisoformat(s["period_to"]).month
        vals = {k: cum[k] - prev_vals[k] for k in IS_LINE_KEYS}
        periods.append({"title": f"{month_short(prev_month + 1)}–{month_short(end_m)}",
                        "sub": tr("BB") + (tr(" + procene") if res["estimates"] else ""),
                        "kind": "actual", "months": list(range(prev_month + 1, end_m + 1)),
                        "vals": vals, "tax": tax - prev_tax, "est": bool(res["estimates"])})
        prev_vals, prev_tax, prev_month = {k: cum[k] for k in IS_LINE_KEYS}, tax, end_m

    if not closing and last_res and prev_month < 12:
        base = next((c for c in reversed([x for x in snaps if x["is_closing"]])
                     if c["period_to"] < last_res["snapshot"]["period_from"]), None)
        proj = _projection(db, last_res, fe.build(db, base["id"]) if base else None, params)
        vals = {k: proj["is"][k] - prev_vals[k] for k in IS_LINE_KEYS}
        periods.append({"title": f"{month_short(prev_month + 1)}–{month_short(12)}",
                        "sub": tr("projekcija"), "kind": "proj",
                        "months": list(range(prev_month + 1, 13)), "vals": vals,
                        "tax": proj["is"]["TAX"] - prev_tax, "est": True})

    monthly = {}
    for p in periods:
        prof = profile
        if p["kind"] == "proj" and not any(profile.get(m) for m in p["months"]):
            prof = fe.pos_monthly_sales(db, year - 1)   # future months: prior-year seasonality
        for m, (vals, tax) in _spread(p["vals"], p["tax"], p["months"], prof).items():
            monthly[m] = {"title": month_short(m),
                          "sub": tr("proj.") if p["kind"] == "proj" else tr("raspodela"),
                          "kind": p["kind"], "vals": vals, "tax": tax}

    def rows(cols):
        ivs = [_is_values(c["vals"], c["tax"]) for c in cols]
        out = []
        for kind, label, key in IS_ROWS:
            cells = [{"v": iv[key], "pct": (iv[key] / iv["POSL_PRIH"] * 100) if iv["POSL_PRIH"] else None,
                      "est": False} for iv in ivs]
            out.append({"kind": kind, "label": label or fe.LINE_LABEL[key], "cells": cells})
        return out

    total = {"title": tr("Ukupno {year}", year=year), "sub": tr("BB + projekcija") if any(p["kind"] == "proj" for p in periods) else tr("BB"),
             "kind": "total", "vals": {k: sum(p["vals"][k] for p in periods) for k in IS_LINE_KEYS},
             "tax": sum(p["tax"] for p in periods)}
    pcols = periods + [total]
    mcols = [monthly[m] for m in sorted(monthly)] + [total]
    return {"years": years, "year": year, "pcols": pcols, "mcols": mcols,
            "prows": rows(pcols), "mrows": rows(mcols), "has_profile": bool(profile)}


# ── Usaglašavanje: zamrznute procene naspram zaključnog bilansa ──────

import json as _json


def _ensure_log(db):
    db.execute("""CREATE TABLE IF NOT EXISTS fs_estimate_log (
        snapshot_id INTEGER PRIMARY KEY,
        period_to TEXT, frozen_at TEXT DEFAULT (datetime('now','localtime')),
        payload TEXT NOT NULL)""")
    db.commit()


def freeze_estimates(db, snapshot_id, force=False):
    """Store the interim snapshot's estimates and full-year projection as they are now."""
    _ensure_log(db)
    snap = db.execute("SELECT * FROM bb_snapshots WHERE id=?", [snapshot_id]).fetchone()
    if not snap or snap["is_closing"]:
        return False
    if not force and db.execute("SELECT 1 FROM fs_estimate_log WHERE snapshot_id=?", [snapshot_id]).fetchone():
        return False
    params = fe.load_params(db)
    res = fe.build(db, snapshot_id)
    base = db.execute("""SELECT id FROM bb_snapshots WHERE is_closing=1 AND period_to < ?
                         ORDER BY period_to DESC LIMIT 1""", [snap["period_from"]]).fetchone()
    proj = _projection(db, res, fe.build(db, base["id"]) if base else None, params)
    months = date.fromisoformat(snap["period_to"]).month
    payload = {
        "ytd": _is_values(res["values"], res["income"]["tax"]),
        "proj": proj["is"],
        "estimates": res["estimates"],
        "cogs_ratio": res["base"]["cogs_ratio"] if res["base"] else None,
        "depr_monthly": (res["base"]["depr_annual"] / 12) if res["base"] and res["base"]["depr_annual"] else None,
        "share_remaining": proj.get("share"),
        "months": months,
        "checks": res["checks"],
    }
    db.execute("INSERT OR REPLACE INTO fs_estimate_log (snapshot_id, period_to, payload, frozen_at) "
               "VALUES (?,?,?, datetime('now','localtime'))",
               [snapshot_id, snap["period_to"], _json.dumps(payload, ensure_ascii=False)])
    db.commit()
    return True


def build_reconciliation(db, year=None):
    fe.ensure_fs_tables(db)
    _ensure_log(db)
    snaps = [dict(r) for r in db.execute(
        "SELECT id, period_from, period_to, is_closing FROM bb_snapshots ORDER BY period_to").fetchall()]
    for s in snaps:                      # older interim imports: freeze lazily once
        if not s["is_closing"]:
            freeze_estimates(db, s["id"])
    years = sorted({int(s["period_to"][:4]) for s in snaps if not s["is_closing"]})
    if not years:
        return {"years": [], "year": None}
    year = year if year in years else years[-1]
    interims = [s for s in snaps if not s["is_closing"] and s["period_to"][:4] == str(year)]
    logs = []
    for s in interims:
        row = db.execute("SELECT frozen_at, payload FROM fs_estimate_log WHERE snapshot_id=?", [s["id"]]).fetchone()
        logs.append({**s, "frozen_at": row["frozen_at"], "p": _json.loads(row["payload"])})
    closing = next((s for s in snaps if s["is_closing"] and s["period_to"][:4] == str(year)), None)

    actual, actual_params = None, {}
    if closing:
        res = fe.build(db, closing["id"])
        actual = _is_values(res["values"], res["income"]["tax"])
        rev = actual["I_ROBA"] + actual["I_USLUGE"]
        kis = fe._raw(db, closing["id"], fe.load_mapping(db))[2]
        cogs = fe._sum_prefix(kis, ("501", "511"))
        actual_params = {"cogs_ratio": cogs / rev if rev else None,
                         "depr_monthly": fe._sum_prefix(kis, ("54",)) / 12}

    rows = []
    for kind, label, key in IS_ROWS:
        cells = [l["p"]["proj"].get(key) for l in logs]
        a = actual.get(key) if actual else None
        devs = [(c - a) if (a is not None and c is not None) else None for c in cells]
        rows.append({"kind": kind, "label": label or fe.LINE_LABEL[key], "proj": cells, "actual": a, "devs": devs})

    param_rows = [
        {"label": tr("COGS stopa (501+511 / prihodi 60+61)"), "fmt": "pct",
         "est": [l["p"]["cogs_ratio"] for l in logs], "actual": actual_params.get("cogs_ratio")},
        {"label": tr("Mesečna amortizacija"), "fmt": "amount",
         "est": [l["p"]["depr_monthly"] for l in logs], "actual": actual_params.get("depr_monthly")},
    ]
    return {"years": years, "year": year, "logs": logs, "closing": closing, "rows": rows,
            "param_rows": param_rows, "has_actual": actual is not None}
