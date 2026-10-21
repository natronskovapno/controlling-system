"""
blueprints/fin_statements.py
============================
Finansijski izveštaji — Garden Stage Controlling System

Sub-modules (sidebar):
  statements      Bilans stanja i uspeha      (phase 3 — placeholder)
  cashflow        Tokovi gotovine             (phase 3 — placeholder)
  reconciliation  Usaglašavanje sa BB         (phase 5 — placeholder)
  snapshots       Bruto bilansi — import (upload → preview → commit), list, detail, delete
  settings        Podešavanja — konto mapping, estimate parameters, one-off exclusions, per-snapshot test

Schema:
  bb_snapshots  one row per imported bruto bilans (period, file, checks)
  bb_lines      analytic konto lines; saldo = uk_d - uk_p (derived, not stored)
"""

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, send_file)
from db import get_db
from blueprints.bb_parser import parse_file
from blueprints import fs_engine as fe
from blueprints import fs_report as fr

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import config  # noqa: E402
from i18n import tr

bp = Blueprint("fin_statements", __name__)

BB_DIR = os.path.join(config.DATA_DIR, "bruto_bilans")
PENDING_DIR = os.path.join(BB_DIR, "_pending")


# ── schema ───────────────────────────────────────────────────────────

def _ensure_bb_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS bb_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            period_from  TEXT NOT NULL,
            period_to    TEXT NOT NULL,
            company      TEXT,
            pib          TEXT,
            fmt          TEXT,
            source_file  TEXT,
            stored_path  TEXT,
            file_hash    TEXT UNIQUE,
            is_closing   INTEGER DEFAULT 0,
            line_count   INTEGER,
            checks_json  TEXT,
            warnings_json TEXT,
            checks_ok    INTEGER,
            notes        TEXT,
            imported_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(period_from, period_to)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS bb_lines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES bb_snapshots(id) ON DELETE CASCADE,
            konto       TEXT NOT NULL,
            naziv       TEXT,
            ps_d        REAL DEFAULT 0,
            ps_p        REAL DEFAULT 0,
            tp_d        REAL DEFAULT 0,
            tp_p        REAL DEFAULT 0,
            uk_d        REAL DEFAULT 0,
            uk_p        REAL DEFAULT 0
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_bb_lines_snap ON bb_lines(snapshot_id, konto)")
    db.commit()


# ── helpers ──────────────────────────────────────────────────────────

def _write_bytes(path, data):
    """Single-operation write (Google Drive is unreliable with incremental writes)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _class_summary(lines):
    """Per konto class: saldo debit / credit totals."""
    out = {}
    for ln in lines:
        k = ln["konto"][0]
        s = (ln["uk_d"] or 0) - (ln["uk_p"] or 0)
        c = out.setdefault(k, {"klasa": k, "count": 0, "saldo_d": 0.0, "saldo_p": 0.0,
                               "tp_d": 0.0, "tp_p": 0.0})
        c["count"] += 1
        c["tp_d"] += ln["tp_d"] or 0
        c["tp_p"] += ln["tp_p"] or 0
        if s >= 0:
            c["saldo_d"] += s
        else:
            c["saldo_p"] += -s
    return [out[k] for k in sorted(out)]


def _placeholder(title, subtitle, phase):
    db = get_db()
    _ensure_bb_tables(db)
    n = db.execute("SELECT COUNT(*) FROM bb_snapshots").fetchone()[0]
    return render_template("fin_statements/placeholder.html", title=title,
                           subtitle=subtitle, phase=phase, snapshot_count=n)


# ── sub-modules (placeholders until later phases) ────────────────────

@bp.route("/")
def statements():
    db = get_db()
    _ensure_bb_tables(db)
    rep = fr.build_report(db, request.args.get("ytd", type=int))
    return render_template("fin_statements/statements.html", rep=rep)


@bp.route("/izvoz.xlsx")
def statements_export():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.workbook.defined_name import DefinedName

    db = get_db()
    _ensure_bb_tables(db)
    rep = fr.build_report(db, request.args.get("ytd", type=int))
    rate = request.args.get("kurs", type=float) or 117.2
    cols = rep["cols"]

    wb = Workbook()
    ws = wb.active
    ws.title = "RSD"
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="EFE9DE")
    ws["A1"] = tr("Finansijski izveštaji — Garden Stage (konsolidovano)")
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"], ws["B2"] = "Kurs EUR (NBS srednji)", rate
    ws["B2"].font = bold
    wb.defined_names["Kurs"] = DefinedName("Kurs", attr_text="RSD!$B$2")
    ws["A3"] = "e = procena · projekcija = YTD + preostali meseci prethodne godine"

    amount_rows = []

    def header(row, title):
        ws.cell(row, 1, title).font = bold
        for i, c in enumerate(cols):
            cell = ws.cell(row, 2 + i, f"{c['title']} ({c['sub']})")
            cell.font = bold
            cell.alignment = Alignment(wrap_text=True)
        for col in range(1, 2 + len(cols)):
            ws.cell(row, col).fill = head_fill
        return row + 1

    def block(row, rows, amounts=True):
        for rw in rows:
            label = rw["label"]
            c0 = ws.cell(row, 1, ("   " if rw["kind"] == "item" else "") + label)
            if rw["kind"] in ("sub", "tot", "sec"):
                c0.font = bold
            for i, cell in enumerate(rw["cells"]):
                if cell is None or cell.get("v") is None:
                    continue
                val = cell["v"]
                fmt = cell.get("fmt", "amount")
                target = ws.cell(row, 2 + i, round(val, 4) if fmt != "amount" else round(val))
                if fmt == "pct":
                    target.number_format = "0.0%"
                elif fmt in ("x",):
                    target.number_format = "0.00"
                elif fmt == "days":
                    target.number_format = "0"
                else:
                    target.number_format = "#,##0"
                    amount_rows.append((row, 2 + i))
                if cell.get("est"):
                    ws.cell(row, 2 + len(cols), "e")
            row += 1
        return row + 1

    r = header(5, "BILANS STANJA")
    r = block(r, rep["bs_rows"])
    r = header(r, "BILANS USPEHA")
    r = block(r, rep["is_rows"])
    r = header(r, "FINANSIJSKI POKAZATELJI")
    block(r, rep["ratio_rows"])
    ws.column_dimensions["A"].width = 46
    for i in range(len(cols)):
        ws.column_dimensions[chr(66 + i)].width = 20

    # EUR sheet: same layout, amounts as formulas against the rate cell
    we = wb.create_sheet("EUR")
    amount_set = set(amount_rows)
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            tgt = we.cell(cell.row, cell.column)
            if (cell.row, cell.column) in amount_set:
                tgt.value = f"=RSD!{cell.coordinate}/Kurs"
                tgt.number_format = "#,##0"
            elif cell.coordinate == "B2":
                tgt.value = "=Kurs"
            else:
                tgt.value = f"=RSD!{cell.coordinate}" if isinstance(cell.value, (int, float)) else cell.value
                tgt.number_format = cell.number_format
            tgt.font = cell.font.copy()
            tgt.fill = cell.fill.copy()
    we["A2"] = tr("Kurs EUR (menja se na listu RSD, ćelija B2)")
    we.column_dimensions["A"].width = 46
    for i in range(len(cols)):
        we.column_dimensions[chr(66 + i)].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    end = cols[-1]["period_to"] if cols else "prazno"
    return send_file(buf, as_attachment=True, download_name=f"Finansijski_izvestaji_{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/tokovi-gotovine")
def cashflow():
    db = get_db()
    _ensure_bb_tables(db)
    cf = fr.build_cashflow(db, request.args.get("ytd", type=int))
    return render_template("fin_statements/cashflow.html", cf=cf)


@bp.route("/po-periodima")
def periods():
    db = get_db()
    _ensure_bb_tables(db)
    data = fr.build_periods(db, request.args.get("godina", type=int))
    view = "mesecno" if request.args.get("prikaz") == "mesecno" else "periodi"
    return render_template("fin_statements/periods.html", d=data, view=view)


@bp.route("/usaglasavanje")
def reconciliation():
    db = get_db()
    _ensure_bb_tables(db)
    data = fr.build_reconciliation(db, request.args.get("godina", type=int))
    return render_template("fin_statements/reconciliation.html", d=data)


@bp.route("/usaglasavanje/zamrzni/<int:snapshot_id>", methods=["POST"])
def reconciliation_refreeze(snapshot_id):
    db = get_db()
    _ensure_bb_tables(db)
    if fr.freeze_estimates(db, snapshot_id, force=True):
        flash(tr("Procene su ponovo zamrznute sa trenutnim podešavanjima."), "success")
    return redirect(url_for("fin_statements.reconciliation", godina=request.form.get("godina")))


# ── Bruto bilansi ────────────────────────────────────────────────────

@bp.route("/bruto-bilansi")
def snapshots():
    db = get_db()
    _ensure_bb_tables(db)
    rows = [dict(r) for r in db.execute("""
        SELECT id, period_from, period_to, company, fmt, source_file, is_closing,
               line_count, checks_ok, imported_at, warnings_json
        FROM bb_snapshots ORDER BY period_to DESC
    """).fetchall()]
    for r in rows:
        r["warnings"] = json.loads(r.pop("warnings_json") or "[]")
    return render_template("fin_statements/snapshots.html", snapshots=rows)


@bp.route("/bruto-bilansi/upload", methods=["GET", "POST"])
def snapshot_upload():
    db = get_db()
    _ensure_bb_tables(db)

    if request.method == "GET":
        return render_template("fin_statements/upload.html")

    f = request.files.get("file")
    if not f or not f.filename:
        flash(tr("Nije izabran fajl."), "error")
        return redirect(url_for("fin_statements.snapshot_upload"))

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".pdf", ".xlsx", ".xlsm"):
        flash(tr("Podržani formati: PDF i XLSX."), "error")
        return redirect(url_for("fin_statements.snapshot_upload"))

    data = f.read()
    token = uuid.uuid4().hex
    tmp_path = os.path.join(PENDING_DIR, token + ext)
    _write_bytes(tmp_path, data)

    try:
        parsed = parse_file(tmp_path)
    except Exception as e:  # noqa: BLE001
        os.unlink(tmp_path)
        flash(tr("Nije moguće parsirati fajl: {e}", e=e), "error")
        return redirect(url_for("fin_statements.snapshot_upload"))

    file_hash = hashlib.sha256(data).hexdigest()
    dup = db.execute("SELECT period_to FROM bb_snapshots WHERE file_hash=?", [file_hash]).fetchone()
    existing = None
    if parsed.get("period_to"):
        existing = db.execute(
            "SELECT id, source_file, imported_at FROM bb_snapshots WHERE period_from=? AND period_to=?",
            [parsed["period_from"], parsed["period_to"]]).fetchone()

    meta = {
        "token": token, "ext": ext, "source_file": f.filename, "file_hash": file_hash,
        "period_from": parsed["period_from"], "period_to": parsed["period_to"],
        "company": parsed["company"], "pib": parsed["pib"], "fmt": parsed["fmt"],
        "is_closing": parsed["is_closing"], "checks": parsed["checks"],
        "warnings": parsed["warnings"], "ok": parsed["ok"], "lines": parsed["lines"],
    }
    _write_bytes(os.path.join(PENDING_DIR, token + ".json"),
                 json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    session["bb_pending"] = token

    return render_template(
        "fin_statements/preview.html", m=meta,
        classes=_class_summary(parsed["lines"]),
        duplicate=dup["period_to"] if dup else None,
        existing=dict(existing) if existing else None,
    )


@bp.route("/bruto-bilansi/commit", methods=["POST"])
def snapshot_commit():
    db = get_db()
    _ensure_bb_tables(db)

    token = session.pop("bb_pending", None)
    if not token or token != request.form.get("token"):
        flash(tr("Sesija je istekla. Ponovo učitajte fajl."), "error")
        return redirect(url_for("fin_statements.snapshot_upload"))

    meta_path = os.path.join(PENDING_DIR, token + ".json")
    try:
        with open(meta_path, encoding="utf-8") as fh:
            m = json.load(fh)
    except OSError:
        flash(tr("Privremeni podaci nisu pronađeni. Ponovo učitajte fajl."), "error")
        return redirect(url_for("fin_statements.snapshot_upload"))

    if not m["ok"] and request.form.get("force") != "1":
        flash(tr("Kontrole nisu prošle — upis nije izvršen."), "error")
        return redirect(url_for("fin_statements.snapshots"))

    tmp_file = os.path.join(PENDING_DIR, token + m["ext"])
    stored_name = f"BB_{m['period_from']}_{m['period_to']}{m['ext']}"
    stored_path = os.path.join(BB_DIR, stored_name)

    try:
        db.execute("DELETE FROM bb_lines WHERE snapshot_id IN "
                   "(SELECT id FROM bb_snapshots WHERE (period_from=? AND period_to=?) OR file_hash=?)",
                   [m["period_from"], m["period_to"], m["file_hash"]])
        db.execute("DELETE FROM bb_snapshots WHERE (period_from=? AND period_to=?) OR file_hash=?",
                   [m["period_from"], m["period_to"], m["file_hash"]])
        cur = db.execute("""
            INSERT INTO bb_snapshots (period_from, period_to, company, pib, fmt, source_file,
                stored_path, file_hash, is_closing, line_count, checks_json, warnings_json, checks_ok)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [m["period_from"], m["period_to"], m["company"], m["pib"], m["fmt"],
              m["source_file"], stored_path, m["file_hash"], int(m["is_closing"]),
              len(m["lines"]), json.dumps(m["checks"], ensure_ascii=False),
              json.dumps(m["warnings"], ensure_ascii=False), int(m["ok"])])
        sid = cur.lastrowid
        db.executemany("""
            INSERT INTO bb_lines (snapshot_id, konto, naziv, ps_d, ps_p, tp_d, tp_p, uk_d, uk_p)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [(sid, ln["konto"], ln["naziv"], ln["ps_d"], ln["ps_p"], ln["tp_d"],
               ln["tp_p"], ln["uk_d"], ln["uk_p"]) for ln in m["lines"]])
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(tr("Greška pri upisu: {e}", e=e), "error")
        return redirect(url_for("fin_statements.snapshots"))

    try:
        with open(tmp_file, "rb") as fh:
            _write_bytes(stored_path, fh.read())
        os.unlink(tmp_file)
        os.unlink(meta_path)
    except OSError:
        flash(tr("Upisano u bazu, ali originalni fajl nije arhiviran."), "warning")

    try:
        fr.freeze_estimates(db, sid, force=True)   # interim: record estimates as of import
    except Exception:  # noqa: BLE001  (never block an import on the estimate log)
        pass
    flash(tr("Bruto bilans {a0} → {a1} upisan: {a2} konta.", a0=m['period_from'], a1=m['period_to'], a2=len(m['lines'])), "success")
    return redirect(url_for("fin_statements.snapshot_detail", snapshot_id=sid))


@bp.route("/bruto-bilansi/<int:snapshot_id>")
def snapshot_detail(snapshot_id):
    db = get_db()
    _ensure_bb_tables(db)
    snap = db.execute("SELECT * FROM bb_snapshots WHERE id=?", [snapshot_id]).fetchone()
    if not snap:
        flash(tr("Bruto bilans nije pronađen."), "error")
        return redirect(url_for("fin_statements.snapshots"))
    snap = dict(snap)
    snap["checks"] = json.loads(snap.get("checks_json") or "[]")
    snap["warnings"] = json.loads(snap.get("warnings_json") or "[]")

    klasa = request.args.get("klasa", "")
    params = [snapshot_id]
    where = "WHERE l.snapshot_id = ?"
    if klasa:
        where += " AND substr(l.konto,1,1) = ?"
        params.append(klasa)
    lines = [dict(r) for r in db.execute(f"""
        SELECT l.konto,
               COALESCE(l.naziv, (SELECT o.naziv FROM bb_lines o
                                  WHERE o.konto = l.konto AND o.naziv IS NOT NULL
                                  ORDER BY length(o.naziv) DESC LIMIT 1)) AS naziv,
               l.ps_d - l.ps_p AS ps_saldo, l.tp_d, l.tp_p, l.uk_d - l.uk_p AS saldo
        FROM bb_lines l {where}
        ORDER BY l.konto
    """, params).fetchall()]
    all_lines = [dict(r) for r in db.execute(
        "SELECT konto, tp_d, tp_p, uk_d, uk_p FROM bb_lines WHERE snapshot_id=?", [snapshot_id]).fetchall()]
    return render_template("fin_statements/detail.html", s=snap, lines=lines,
                           classes=_class_summary(all_lines), klasa=klasa)


@bp.route("/bruto-bilansi/<int:snapshot_id>/delete", methods=["POST"])
def snapshot_delete(snapshot_id):
    db = get_db()
    _ensure_bb_tables(db)
    row = db.execute("SELECT period_from, period_to FROM bb_snapshots WHERE id=?", [snapshot_id]).fetchone()
    if row:
        db.execute("DELETE FROM bb_lines WHERE snapshot_id=?", [snapshot_id])
        db.execute("DELETE FROM bb_snapshots WHERE id=?", [snapshot_id])
        db.commit()
        flash(tr("Obrisan bruto bilans {a0} → {a1} (arhivirani fajl ostaje).", a0=row['period_from'], a1=row['period_to']), "success")
    return redirect(url_for("fin_statements.snapshots"))


# ── Podešavanja ──────────────────────────────────────────────────────

@bp.route("/podesavanja")
def settings():
    db = get_db()
    _ensure_bb_tables(db)
    fe.ensure_fs_tables(db)

    snaps = [dict(r) for r in db.execute(
        "SELECT id, period_from, period_to, is_closing FROM bb_snapshots ORDER BY period_to DESC").fetchall()]
    sel = request.args.get("snapshot", type=int) or (snaps[0]["id"] if snaps else None)
    result = fe.build(db, sel) if sel else None

    mapping = [dict(r) for r in db.execute(
        "SELECT id, konto_prefix, report_line, note FROM fs_mapping ORDER BY konto_prefix").fetchall()]
    # how many analytic kontos (across all snapshots) each prefix currently resolves
    kontos = [r[0] for r in db.execute("SELECT DISTINCT konto FROM bb_lines").fetchall()]
    mdict = {m["konto_prefix"]: m["report_line"] for m in mapping}
    usage = {}
    for k in kontos:
        _, p = fe.resolve(k, mdict)
        if p:
            usage[p] = usage.get(p, 0) + 1
    for m in mapping:
        m["label"] = "Isključeno (zaključna knjiženja)" if m["report_line"] == fe.EXCLUDE else fe.LINE_LABEL.get(m["report_line"], m["report_line"])
        m["used"] = usage.get(m["konto_prefix"], 0)

    params = [dict(r) for r in db.execute("SELECT key, value, note FROM fs_params ORDER BY key").fetchall()]
    cash_links = [dict(r) for r in db.execute("SELECT * FROM fs_cash_accounts ORDER BY konto").fetchall()]
    cash_kontos = [r[0] for r in db.execute(
        "SELECT DISTINCT konto FROM bb_lines WHERE konto LIKE '24%' ORDER BY konto").fetchall()]
    exclusions = [dict(r) for r in db.execute("SELECT * FROM fs_exclusions ORDER BY year DESC, konto").fetchall()]

    return render_template(
        "fin_statements/settings.html", snaps=snaps, sel=sel, r=result, lines=fe.LINES,
        mapping=mapping, params=params, exclusions=exclusions, sr=fe.sr, EXCLUDE=fe.EXCLUDE,
        cash_links=cash_links, cash_kontos=cash_kontos, bank_accounts=fe.bank_accounts(db),
    )


@bp.route("/podesavanja/mapiranje", methods=["POST"])
def mapping_save():
    db = get_db()
    fe.ensure_fs_tables(db)
    prefix = (request.form.get("konto_prefix") or "").strip()
    line = request.form.get("report_line") or ""
    note = (request.form.get("note") or "").strip() or None
    valid = {k for k, *_ in fe.LINES} | {fe.EXCLUDE}
    if not prefix.isdigit() or line not in valid:
        flash(tr("Konto (samo cifre) i stavka izveštaja su obavezni."), "error")
    else:
        db.execute("""INSERT INTO fs_mapping (konto_prefix, report_line, note) VALUES (?,?,?)
                      ON CONFLICT(konto_prefix) DO UPDATE SET report_line=excluded.report_line,
                      note=excluded.note, updated_at=datetime('now','localtime')""", [prefix, line, note])
        db.commit()
        flash(tr("Mapiranje {prefix} sačuvano.", prefix=prefix), "success")
    return redirect(url_for("fin_statements.settings", snapshot=request.form.get("snapshot") or None) + "#mapiranje")


@bp.route("/podesavanja/mapiranje/<int:mid>/delete", methods=["POST"])
def mapping_delete(mid):
    db = get_db()
    fe.ensure_fs_tables(db)
    db.execute("DELETE FROM fs_mapping WHERE id=?", [mid])
    db.commit()
    flash(tr("Mapiranje obrisano."), "success")
    return redirect(url_for("fin_statements.settings") + "#mapiranje")


@bp.route("/podesavanja/parametri", methods=["POST"])
def params_save():
    db = get_db()
    fe.ensure_fs_tables(db)
    for key in ("cogs_ratio_override", "depreciation_annual_override", "tax_rate"):
        raw = (request.form.get(key) or "").strip().replace(",", ".")
        if raw == "":
            val = None
        else:
            try:
                val = str(float(raw))
            except ValueError:
                flash(tr("Neispravna vrednost za {key}.", key=key), "error")
                return redirect(url_for("fin_statements.settings") + "#parametri")
        db.execute("UPDATE fs_params SET value=? WHERE key=?", [val, key])
    db.commit()
    flash(tr("Parametri sačuvani."), "success")
    return redirect(url_for("fin_statements.settings") + "#parametri")


@bp.route("/podesavanja/iskljucenja", methods=["POST"])
def exclusion_add():
    db = get_db()
    fe.ensure_fs_tables(db)
    try:
        year = int(request.form.get("year"))
        konto = request.form.get("konto", "").strip()
        amount = float((request.form.get("amount") or "").replace(".", "").replace(",", "."))
        if not konto.isdigit():
            raise ValueError
    except (TypeError, ValueError):
        flash(tr("Godina, konto i iznos su obavezni."), "error")
        return redirect(url_for("fin_statements.settings") + "#iskljucenja")
    db.execute("INSERT INTO fs_exclusions (year, konto, amount, reason) VALUES (?,?,?,?)",
               [year, konto, amount, (request.form.get("reason") or "").strip() or None])
    db.commit()
    flash(tr("Isključenje dodato."), "success")
    return redirect(url_for("fin_statements.settings") + "#iskljucenja")


@bp.route("/podesavanja/iskljucenja/<int:eid>/delete", methods=["POST"])
def exclusion_delete(eid):
    db = get_db()
    fe.ensure_fs_tables(db)
    db.execute("DELETE FROM fs_exclusions WHERE id=?", [eid])
    db.commit()
    flash(tr("Isključenje obrisano."), "success")
    return redirect(url_for("fin_statements.settings") + "#iskljucenja")


@bp.route("/podesavanja/racuni", methods=["POST"])
def cash_link_save():
    db = get_db()
    fe.ensure_fs_tables(db)
    konto = (request.form.get("konto") or "").strip()
    acc = (request.form.get("account_number") or "").strip()
    if not konto.isdigit() or not acc:
        flash(tr("Konto i račun su obavezni."), "error")
    else:
        db.execute("""INSERT INTO fs_cash_accounts (konto, account_number, note) VALUES (?,?,?)
                      ON CONFLICT(konto) DO UPDATE SET account_number=excluded.account_number, note=excluded.note""",
                   [konto, acc, (request.form.get("note") or "").strip() or None])
        db.commit()
        flash(tr("Konto {konto} povezan sa računom {acc}.", konto=konto, acc=acc), "success")
    return redirect(url_for("fin_statements.settings") + "#racuni")


@bp.route("/podesavanja/racuni/<konto>/delete", methods=["POST"])
def cash_link_delete(konto):
    db = get_db()
    fe.ensure_fs_tables(db)
    db.execute("DELETE FROM fs_cash_accounts WHERE konto=?", [konto])
    db.commit()
    flash(tr("Veza za konto {konto} obrisana.", konto=konto), "success")
    return redirect(url_for("fin_statements.settings") + "#racuni")
