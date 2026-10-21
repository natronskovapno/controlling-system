from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
import re
from i18n import tr

bp = Blueprint("bank", __name__)

# ── Fx (international) bank statements — Prenosi na racun... no wait, separate ──
# Standalone submodule: manual PDF drag-and-drop upload + parse + preview + commit,
# same UX pattern as invoices.py's Bele fakture PDF importer. NOT part of the
# automated ingestion pipeline (09_pos_ingestion.py / Demo Bank RSD XML
# parser) — deliberately isolated per scope decision, since these statements
# are infrequent (foreign-currency + non-resident RSD sub-accounts).

FX_DATE_RE = re.compile(r'(\d{2})\.(\d{2})\.(\d{4})')


def _ensure_fx_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS fx_bank_statements (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            account            TEXT,
            iban               TEXT,
            currency           TEXT,
            subaccount_code    TEXT,
            subaccount_name    TEXT,
            statement_number   TEXT,
            statement_date     TEXT,
            previous_balance   REAL,
            new_balance        REAL,
            source_file        TEXT,
            uploaded_at        TEXT DEFAULT (datetime('now')),
            UNIQUE(account, currency, subaccount_code, statement_number)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fx_bank_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id    INTEGER NOT NULL REFERENCES fx_bank_statements(id),
            line_no         INTEGER,
            description     TEXT,
            debit           REAL DEFAULT 0,
            credit          REAL DEFAULT 0,
            rsd_equivalent  REAL DEFAULT 0,
            booking_date    TEXT,
            kurs            REAL,
            nalog_br        TEXT,
            predmet_br      TEXT
        )
    """)
    db.commit()


def _fx_parse_amount(s):
    """Serbian format: '.' thousands, ',' decimal (e.g. '9.990,00' -> 9990.0).
    Handles the PDF's inconsistent '0.00' (plain zero) and leading '-' signs."""
    if s is None:
        return 0.0
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fx_parse_kurs(s):
    """Kurs uses plain decimal notation with '.' (e.g. '117.9000'), NOT the
    thousands-separator convention used elsewhere in the statement."""
    if not s:
        return None
    try:
        return float(s.strip().replace(",", "."))
    except ValueError:
        return None


def _fx_parse_date(s):
    m = FX_DATE_RE.match(s.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _parse_fx_statement_text(text, source_file=""):
    """Parses one Demo Bank Fx Account Statement PDF page's extracted text.
    Returns a dict with header fields and a list of currency 'blocks', each
    with previous/new balance and a list of transactions.
    Confirmed against 20 real samples (2025-01 through 2026-07) — 1 required
    a negative-amount regex fix (loan repayment/reversal entries)."""
    result = {"source_file": source_file}

    m = re.search(r'Izvod deviznog ra[cč]una br\.\s*([\d/]+)', text)
    if m: result["statement_number"] = m.group(1)
    m = re.search(r'Za datum:\s*/\s*Date:\s*(\d{2}\.\d{2}\.\d{4})', text)
    if m: result["statement_date"] = _fx_parse_date(m.group(1))
    m = re.search(r'Partija:\s*/Account\s*([\d.\-]+)', text)
    if m: result["account"] = m.group(1)
    m = re.search(r'IBAN:\s*(\S+)', text)
    if m: result["iban"] = m.group(1)

    blocks = []
    for cm in re.finditer(r'^(\w{3})\s*/\s*Konto\s*:\s*/SubAccount:\s*(\d+)\s*-\s*(.+)$', text, re.MULTILINE):
        blocks.append({
            "currency": cm.group(1), "subaccount_code": cm.group(2),
            "subaccount_name": cm.group(3).strip(), "start": cm.end(),
        })

    for i, b in enumerate(blocks):
        end = blocks[i + 1]["start"] if i + 1 < len(blocks) else len(text)
        full_chunk = text[b["start"]:end]

        pm = re.search(r'Po[cč]etno stanje:\s*([\d.,\-]+)', full_chunk)
        b["previous_balance"] = _fx_parse_amount(pm.group(1)) if pm else None
        nm = re.search(r'Novo stanje:\s*([\d.,\-]+)', full_chunk)
        b["new_balance"] = _fx_parse_amount(nm.group(1)) if nm else None

        tx_chunk = full_chunk.split("Novo stanje:")[0]
        tx_starts = list(re.finditer(r'(?m)^(\d+)\.\s+', tx_chunk))
        txs = []
        for j, tm in enumerate(tx_starts):
            tstart = tm.start()
            tend = tx_starts[j + 1].start() if j + 1 < len(tx_starts) else len(tx_chunk)
            block_text = tx_chunk[tstart:tend]
            lines = [l for l in block_text.split("\n") if l.strip()]
            first = lines[0]
            fm = re.match(
                r'^\d+\.\s+(.*?)\s+([\-\d.,]+)\s+([\-\d.,]+)\s+([\-\d.,]+)\s+(\d{2}\.\d{2}\.\d{4})\s*$',
                first)
            tx = {"line_no": int(tm.group(1))}
            if fm:
                tx["desc1"] = fm.group(1)
                tx["debit"] = _fx_parse_amount(fm.group(2))
                tx["credit"] = _fx_parse_amount(fm.group(3))
                tx["rsd_equiv"] = _fx_parse_amount(fm.group(4))
                tx["booking_date"] = _fx_parse_date(fm.group(5))
            else:
                tx["desc1"] = first
                tx["debit"] = tx["credit"] = tx["rsd_equiv"] = 0.0
                tx["booking_date"] = None
                tx["_unparsed"] = True

            nalog_m = re.search(r'Nalog br\.\s*(\S+)\s+Predmet br\.\s*(\S+)', block_text)
            if nalog_m:
                tx["nalog_br"] = nalog_m.group(1)
                tx["predmet_br"] = nalog_m.group(2)
            kurs_m = re.search(r'Kurs:\s*([\d.,]+)', block_text)
            if kurs_m:
                tx["kurs"] = _fx_parse_kurs(kurs_m.group(1))

            desc_lines = []
            for l in lines[1:]:
                ls = l.strip()
                if ls.startswith("Nalog br."): continue
                if ls.startswith("Kurs:"): continue
                if re.match(r'^\d{2}\.\d{2}\.\d{4}$', ls): continue
                if re.match(r'^\S+\s+[\d.,\-]+\s+[\d.,\-]+$', ls) and "br." not in ls:
                    continue  # GL classification footer line
                l2 = re.sub(r'\s+\d{2}\.\d{2}\.\d{4}\s*$', '', ls)
                if l2:
                    desc_lines.append(l2)
            tx["description"] = (tx.get("desc1", "") + " | " + " | ".join(desc_lines)).strip(" |")
            txs.append(tx)
        b["transactions"] = txs

    result["blocks"] = blocks
    return result

def _anchor_redirect(url, row_id):
    """Append #row-{id} anchor so the browser scrolls back to the edited row."""
    base = url.split('#')[0]
    return redirect(f"{base}#row-{row_id}")

def _referrer_anchor(row_id, fallback):
    base = (request.referrer or fallback).split('#')[0]
    return redirect(f"{base}#row-{row_id}")


PER_PAGE = 50

ACCOUNT_LABELS = {
    "160-0000000001234-56": "Primary current account",
    "265-0000000009876-54": "Secondary current account",
    "160-0000000005678-90": "Brewery current account",
    "160-0000000007777-11": "Loan servicing account",
}

# ── ensure tag column exists ─────────────────────────────────────────

def _ensure_columns(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(bank_transactions)").fetchall()]
    if "manual_tag" not in cols:
        db.execute("ALTER TABLE bank_transactions ADD COLUMN manual_tag TEXT")
    if "notes" not in cols:
        db.execute("ALTER TABLE bank_transactions ADD COLUMN notes TEXT")
    db.commit()


def _get_latest_ingestion_timestamp(db):
    """Get the most recent created_at timestamp from bank_transactions (identifies newest batch)."""
    row = db.execute(
        "SELECT MAX(created_at) as latest FROM bank_transactions"
    ).fetchone()
    return row["latest"] if row and row["latest"] else None

# ── daily balances ───────────────────────────────────────────────────

@bp.route("/daily-balances")
def daily_balances():
    """
    Daily view of bank account balances.
    
    Uses account_balances for closing balance, bank_transactions for inflows/outflows.
    """
    db = get_db()
    _ensure_columns(db)

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    account   = request.args.get("account", "")

    where  = "WHERE 1=1"
    params = []
    if account:
        where += " AND account_number = ?"; params.append(account)
    if date_from:
        where += " AND balance_date >= ?"; params.append(date_from)
    if date_to:
        where += " AND balance_date <= ?"; params.append(date_to)

    # Get closing balances from account_balances
    balance_rows = db.execute(f"""
        SELECT balance_date, account_number, closing_balance
        FROM account_balances
        {where}
        ORDER BY balance_date DESC, account_number
    """, params).fetchall()

    # Get daily inflows/outflows from bank_transactions
    tx_where = where.replace("account_number", "account").replace("balance_date", "date")
    tx_rows = db.execute(f"""
        SELECT
            date,
            account,
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END), 0) AS day_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END), 0) AS day_out
        FROM bank_transactions
        {tx_where}
        GROUP BY date, account
        ORDER BY date DESC, account
    """, params).fetchall()

    # Get all accounts in range
    accounts_in_range = db.execute(f"""
        SELECT DISTINCT account_number FROM account_balances {where}
        ORDER BY account_number
    """, params).fetchall()
    account_list = [r["account_number"] for r in accounts_in_range]

    # Build daily data: {date: {account: {in, out, balance}}}
    from collections import defaultdict
    daily = defaultdict(lambda: defaultdict(lambda: {"in": 0.0, "out": 0.0, "balance": 0.0}))
    all_dates = set()

    # Add closing balances
    for r in balance_rows:
        daily[r["balance_date"]][r["account_number"]]["balance"] = float(r["closing_balance"] or 0)
        all_dates.add(r["balance_date"])

    # Add inflows/outflows
    for r in tx_rows:
        daily[r["date"]][r["account"]]["in"] = float(r["day_in"] or 0)
        daily[r["date"]][r["account"]]["out"] = float(r["day_out"] or 0)
        all_dates.add(r["date"])

    all_dates = sorted(all_dates, reverse=True)

    # Forward-fill balances: for days with no closing balance entry,
    # carry forward the last known balance (sorted ascending for fill, then reverse for display)
    last_known = {acct: 0.0 for acct in account_list}
    for date_str in sorted(all_dates):  # ascending for fill pass
        for acct in account_list:
            if daily[date_str][acct]["balance"] != 0.0:
                last_known[acct] = daily[date_str][acct]["balance"]
            else:
                daily[date_str][acct]["balance"] = last_known[acct]

    # Build output rows
    output = []
    for date_str in all_dates:
        row_data = {"date": date_str, "accounts": {}}
        day_total_in = day_total_out = 0.0
        for acct in account_list:
            d = daily[date_str][acct]
            row_data["accounts"][acct] = d
            day_total_in  += d["in"]
            day_total_out += d["out"]
        row_data["total_in"]  = day_total_in
        row_data["total_out"] = day_total_out
        output.append(row_data)

    accounts = db.execute(
        "SELECT DISTINCT account_number FROM account_balances ORDER BY account_number"
    ).fetchall()

    return render_template(
        "bank/daily_balances.html",
        output=output,
        account_list=account_list,
        account_labels=ACCOUNT_LABELS,
        accounts=accounts,
        filters=dict(date_from=date_from, date_to=date_to, account=account),
    )


# ── index ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    db = get_db()
    _ensure_columns(db)

    # filters
    account    = request.args.get("account", "")
    direction  = request.args.get("direction", "")
    allocated  = request.args.get("allocated", "")
    search     = request.args.get("search", "")
    date_from  = request.args.get("date_from", "")
    date_to    = request.args.get("date_to", "")
    tag         = request.args.get("tag", "")
    show_latest = request.args.get("show_latest", "")
    page        = max(1, int(request.args.get("page", 1)))

    where = "WHERE 1=1"
    params = []

    if account:
        where += " AND account = ?"; params.append(account)
    if direction:
        where += " AND direction = ?"; params.append(direction)
    if allocated == "0":
        where += " AND (allocated = 0 OR allocated IS NULL)"
    elif allocated == "1":
        where += " AND allocated = 1"
    if search:
        digits_only = re.sub(r'[^\d]', '', search)
        if digits_only:
            where += " AND (counterparty LIKE ? OR purpose LIKE ? OR CAST(ROUND(amount) AS TEXT) LIKE ?)"
            params += [f"%{search}%", f"%{search}%", f"%{digits_only}%"]
        else:
            where += " AND (counterparty LIKE ? OR purpose LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
    if date_from:
        where += " AND date >= ?"; params.append(date_from)
    if date_to:
        where += " AND date <= ?"; params.append(date_to)
    if tag:
        where += " AND (auto_tag = ? OR manual_tag = ?)"; params += [tag, tag]
    if show_latest:
        where += " AND DATE(created_at) = (SELECT DATE(MAX(created_at)) FROM bank_transactions)"

    count = db.execute("SELECT COUNT(*) FROM bank_transactions " + where, params).fetchone()[0]
    total_pages = max(1, (count + PER_PAGE - 1) // PER_PAGE)
    offset = (page - 1) * PER_PAGE

    rows = db.execute(
        "SELECT * FROM bank_transactions " + where +
        " ORDER BY date DESC, transaction_id DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset]
    ).fetchall()

    # KPIs
    kpi = db.execute("""
        SELECT
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) AS total_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) AS total_out,
            COUNT(*) AS cnt,
            SUM(CASE WHEN allocated=0 OR allocated IS NULL THEN 1 ELSE 0 END) AS unallocated
        FROM bank_transactions
    """ + where, params).fetchone()

    # Per-account summary (always unfiltered for sidebar)
    account_summary = db.execute("""
        SELECT account,
            COUNT(*) as cnt,
            ROUND(SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END),0) as total_in,
            ROUND(SUM(CASE WHEN direction='debit'  THEN amount ELSE 0 END),0) as total_out
        FROM bank_transactions
        GROUP BY account
        ORDER BY cnt DESC
    """).fetchall()

    # Available tags
    tags = db.execute("""
        SELECT COALESCE(auto_tag, manual_tag) as tag, COUNT(*) as cnt
        FROM bank_transactions
        WHERE auto_tag IS NOT NULL OR manual_tag IS NOT NULL
        GROUP BY tag ORDER BY cnt DESC
    """).fetchall()

    # Available accounts
    accounts = db.execute(
        "SELECT DISTINCT account FROM bank_transactions ORDER BY account"
    ).fetchall()


    # Get latest ingestion timestamp for "NEW" badge
    latest_ingestion = _get_latest_ingestion_timestamp(db)

    try:
        fixed_tags = db.execute(
            "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
        ).fetchall()
    except Exception:
        fixed_tags = []

    return render_template(
        "bank/index.html",
        rows=rows, kpi=kpi,
        account_summary=account_summary,
        accounts=accounts,
        account_labels=ACCOUNT_LABELS,
        tags=tags,
        fixed_tags=fixed_tags,
        latest_ingestion=latest_ingestion,
        filters=dict(account=account, direction=direction, allocated=allocated,
                     search=search, date_from=date_from, date_to=date_to, tag=tag,
                     show_latest=show_latest),
        page=page, total_pages=total_pages, count=count,
    )

# ── tag / allocate single transaction ────────────────────────────────

@bp.route("/<int:transaction_id>/tag", methods=["POST"])
def tag_transaction(transaction_id):
    db = get_db()
    _ensure_columns(db)
    manual_tag = request.form.get("manual_tag", "").strip() or None
    notes      = request.form.get("notes", "").strip() or None
    allocated  = 1 if request.form.get("allocated") == "1" else 0

    db.execute("""
        UPDATE bank_transactions
        SET manual_tag = ?, notes = ?, allocated = ?
        WHERE transaction_id = ?
    """, [manual_tag, notes, allocated, transaction_id])
    db.commit()
    flash(tr("Transakcija je ažurirana."), "success")
    return _referrer_anchor(transaction_id, url_for("bank.index"))

# ── bulk allocate ─────────────────────────────────────────────────────

@bp.route("/bulk-allocate", methods=["POST"])
def bulk_allocate():
    db = get_db()
    _ensure_columns(db)
    
    select_all = request.form.get("select_all") == "1"
    tag = request.form.get("bulk_tag", "").strip()
    
    # Handle new tag creation
    if not tag:
        new_tag = request.form.get("bulk_tag_new", "").strip()
        if new_tag:
            tag = new_tag
    
    # Empty string means remove tag (set to NULL)
    if tag == "":
        tag = None
    
    if select_all:
        # Build filter clause from form parameters
        account    = request.form.get("f_account", "")
        direction  = request.form.get("f_direction", "")
        allocated  = request.form.get("f_allocated", "")
        date_from  = request.form.get("f_date_from", "")
        date_to    = request.form.get("f_date_to", "")
        search     = request.form.get("f_search", "")
        exclude_ids = set(request.form.getlist("exclude_ids"))
        
        where = "WHERE 1=1"
        params = []
        
        if account:
            where += " AND account = ?"; params.append(account)
        if direction:
            where += " AND direction = ?"; params.append(direction)
        if allocated == "0":
            where += " AND (allocated = 0 OR allocated IS NULL)"
        elif allocated == "1":
            where += " AND allocated = 1"
        if search:
            where += " AND (counterparty LIKE ? OR purpose LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if date_from:
            where += " AND date >= ?"; params.append(date_from)
        if date_to:
            where += " AND date <= ?"; params.append(date_to)
        
        # Get all matching transaction IDs
        rows = db.execute(
            f"SELECT transaction_id FROM bank_transactions {where}", 
            params
        ).fetchall()
        
        # Filter out excluded IDs
        ids = [str(r["transaction_id"]) for r in rows 
               if str(r["transaction_id"]) not in exclude_ids]
    else:
        # Standard mode: use explicitly selected IDs
        ids = request.form.getlist("tx_ids")
    
    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(
            f"UPDATE bank_transactions SET allocated=1, manual_tag=? "
            f"WHERE transaction_id IN ({placeholders})",
            [tag] + ids
        )
        db.commit()
        if tag:
            flash(tr("{a0} transakcija označeno kao alocirano sa tagom '{tag}'.", a0=len(ids), tag=tag), "success")
        else:
            flash(tr("{a0} transakcija označeno kao alocirano (bez taga).", a0=len(ids)), "success")
    else:
        flash(tr("Nema selektovanih transakcija."), "warning")
    
    return redirect(request.referrer or url_for("bank.index"))

# ── detail ────────────────────────────────────────────────────────────

@bp.route("/<int:transaction_id>")
def detail(transaction_id):
    db = get_db()
    _ensure_columns(db)
    row = db.execute(
        "SELECT * FROM bank_transactions WHERE transaction_id = ?", [transaction_id]
    ).fetchone()
    if not row:
        flash(tr("Transakcija nije pronađena."), "error")
        return redirect(url_for("bank.index"))
    existing_tags = [r[0] for r in db.execute("""
        SELECT DISTINCT COALESCE(manual_tag, auto_tag) as tag
        FROM bank_transactions
        WHERE manual_tag IS NOT NULL OR auto_tag IS NOT NULL
        ORDER BY tag
    """).fetchall() if r[0]]

    try:
        fixed_tags = db.execute(
            "SELECT * FROM fixed_cost_tags WHERE active=1 ORDER BY type, name"
        ).fetchall()
        existing_links = db.execute("""
            SELECT l.*, t.name as tag_name, t.type as tag_type
            FROM invoice_fixed_cost_links l
            JOIN fixed_cost_tags t ON t.tag_id = l.tag_id
            WHERE l.source_type = 'bank_transaction' AND l.source_id = ?
        """, [transaction_id]).fetchall()
    except Exception:
        fixed_tags = []
        existing_links = []

    from datetime import date as _date
    return render_template("bank/detail.html", row=row, account_labels=ACCOUNT_LABELS,
                           existing_tags=existing_tags, fixed_tags=fixed_tags,
                           existing_links=existing_links,
                           today_year=_date.today().year, today_month=_date.today().month)


# ── assign to fixed cost ──────────────────────────────────────────────

@bp.route("/<int:transaction_id>/assign-fixed-cost", methods=["POST"])
def assign_fixed_cost(transaction_id):
    db = get_db()
    f = request.form
    try:
        tag_id = int(f["tag_id"])
        db.execute("""
            INSERT INTO invoice_fixed_cost_links
            (source_type, source_id, tag_id, year, month, amount, notes)
            VALUES ('bank_transaction', ?, ?, ?, ?, ?, ?)
        """, [
            transaction_id, tag_id,
            int(f["year"]), int(f["month"]),
            float(f.get("amount") or 0),
            f.get("notes") or None,
        ])
        # Sync tag name and allocated status back to main row
        tag_row = db.execute("SELECT name FROM fixed_cost_tags WHERE tag_id=?", [tag_id]).fetchone()
        if tag_row:
            db.execute("""
                UPDATE bank_transactions SET manual_tag=?, allocated=1
                WHERE transaction_id=?
            """, [tag_row["name"], transaction_id])
        else:
            db.execute("UPDATE bank_transactions SET allocated=1 WHERE transaction_id=?", [transaction_id])
        db.commit()
        flash(tr("Transakcija je dodeljena fiksnom trošku/prihodu."), "success")
    except Exception as e:
        flash(tr("Greška: {e}", e=e), "error")
    return redirect(request.referrer or url_for("bank.detail", transaction_id=transaction_id))

# ── inline tag + fixed cost from list ────────────────────────────────

@bp.route("/bulk-assign-fixed-cost", methods=["POST"])
def bulk_assign_fixed_cost():
    """
    Bulk assign bank transactions to fixed cost allocations.
    Year/month determined from transaction date.
    """
    db = get_db()
    _ensure_columns(db)
    f = request.form
    
    tag_id = f.get("fixed_cost_tag_id")
    if not tag_id:
        flash(tr("Izaberite fiksni trošak tag."), "error")
        return redirect(request.referrer or url_for("bank.index"))
    
    tag_id = int(tag_id)

    # Fetch tag name once for writeback
    tag_row = db.execute("SELECT name FROM fixed_cost_tags WHERE tag_id=?", [tag_id]).fetchone()
    tag_name = tag_row["name"] if tag_row else None
    
    # Determine which transactions to process
    select_all = f.get("select_all_results") == "1"
    
    if select_all:
        # Select all matching filter, excluding specific IDs
        account    = f.get("f_account", "")
        direction  = f.get("f_direction", "")
        allocated  = f.get("f_allocated", "")
        tag        = f.get("f_tag", "")
        date_from  = f.get("f_date_from", "")
        date_to    = f.get("f_date_to", "")
        search     = f.get("f_search", "")
        exclude_ids = set(f.get("exclude_ids", "").split(",")) if f.get("exclude_ids") else set()
        
        where = "WHERE 1=1"
        params = []
        
        if account:
            where += " AND account = ?"; params.append(account)
        if direction:
            where += " AND direction = ?"; params.append(direction)
        if allocated == "0":
            where += " AND (allocated = 0 OR allocated IS NULL)"
        elif allocated == "1":
            where += " AND allocated = 1"
        if search:
            where += " AND (counterparty LIKE ? OR purpose LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if date_from:
            where += " AND date >= ?"; params.append(date_from)
        if date_to:
            where += " AND date <= ?"; params.append(date_to)
        if tag:
            where += " AND (manual_tag = ? OR auto_tag = ?)"; params += [tag, tag]
        
        # Get all matching transactions with date info
        rows = db.execute(
            f"SELECT transaction_id, date, amount FROM bank_transactions {where}", 
            params
        ).fetchall()
        
        # Filter out excluded IDs
        transactions = [r for r in rows if str(r["transaction_id"]) not in exclude_ids]
    else:
        # Standard mode: use explicitly selected IDs
        ids_str = f.get("selected_ids", "")
        if not ids_str:
            flash(tr("Nema selektovanih transakcija."), "warning")
            return redirect(request.referrer or url_for("bank.index"))
        
        ids = ids_str.split(",")
        placeholders = ",".join("?" * len(ids))
        transactions = db.execute(
            f"SELECT transaction_id, date, amount FROM bank_transactions "
            f"WHERE transaction_id IN ({placeholders})",
            ids
        ).fetchall()
    
    if not transactions:
        flash(tr("Nema transakcija za obradu."), "warning")
        return redirect(request.referrer or url_for("bank.index"))
    
    # Ensure table exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_fixed_cost_links (
            link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT    NOT NULL,
            source_id   INTEGER NOT NULL,
            tag_id      INTEGER NOT NULL,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            amount      REAL    NOT NULL,
            notes       TEXT,
            created_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(source_type, source_id, tag_id, year, month)
        )
    """)
    
    # Process each transaction
    added = updated = skipped = 0
    for tx in transactions:
        try:
            # Parse year/month from transaction date
            date_parts = tx["date"].split("-")
            year = int(date_parts[0])
            month = int(date_parts[1])
            amount = float(tx["amount"])
            
            # Check if already exists
            existing = db.execute("""
                SELECT link_id FROM invoice_fixed_cost_links
                WHERE source_type='bank_transaction' 
                  AND source_id=? AND tag_id=? AND year=? AND month=?
            """, [tx["transaction_id"], tag_id, year, month]).fetchone()
            
            if existing:
                # Update amount (allow re-submission to fix amounts)
                db.execute("""
                    UPDATE invoice_fixed_cost_links
                    SET amount=?
                    WHERE link_id=?
                """, [amount, existing["link_id"]])
                updated += 1
            else:
                # Insert new allocation
                db.execute("""
                    INSERT INTO invoice_fixed_cost_links
                    (source_type, source_id, tag_id, year, month, amount)
                    VALUES ('bank_transaction', ?, ?, ?, ?, ?)
                """, [tx["transaction_id"], tag_id, year, month, amount])
                added += 1
        except Exception as e:
            skipped += 1
            print(f"Error processing transaction {tx['transaction_id']}: {e}")
    
    # Sync manual_tag and allocated=1 back to bank_transactions for all processed transactions
    if transactions and (added or updated):
        processed_ids = [tx["transaction_id"] for tx in transactions]
        ph = ",".join("?" * len(processed_ids))
        db.execute(
            f"UPDATE bank_transactions SET manual_tag=?, allocated=1 WHERE transaction_id IN ({ph})",
            [tag_name] + processed_ids
        )

    db.commit()
    
    # Build success message
    parts = []
    if added: parts.append(f"{added} " + tr("dodato"))
    if updated: parts.append(f"{updated} " + tr("ažurirano"))
    if skipped: parts.append(f"{skipped} " + tr("preskočeno"))
    
    flash(tr("Fiksni troškovi: {a0}.", a0=', '.join(parts)), "success" if (added or updated) else "warning")
    return redirect(request.referrer or url_for("bank.index"))

@bp.route("/inline-tag", methods=["POST"])
def inline_tag():
    db = get_db()
    _ensure_columns(db)
    f = request.form
    transaction_id = int(f["transaction_id"])

    # Determine what was submitted
    tag_submitted   = "manual_tag" in f
    alloc_submitted = "allocated" in f

    manual_tag    = f.get("manual_tag", "").strip() or None
    allocated_val = f.get("allocated", "").strip()
    allocated     = 1 if allocated_val == "1" else 0

    if tag_submitted and alloc_submitted:
        # Both fields present (tag dropdown submit) — write both; tagging always sets allocated=1
        db.execute("""
            UPDATE bank_transactions
            SET manual_tag=?, allocated=?
            WHERE transaction_id=?
        """, [manual_tag, allocated, transaction_id])
    elif tag_submitted:
        # Tag-only update — always mark as allocated
        db.execute("""
            UPDATE bank_transactions
            SET manual_tag=?, allocated=1
            WHERE transaction_id=?
        """, [manual_tag, transaction_id])
    elif alloc_submitted:
        # Allocation toggle only — preserve existing tag
        db.execute("""
            UPDATE bank_transactions
            SET allocated=?
            WHERE transaction_id=?
        """, [allocated, transaction_id])

    # Optionally also assign to fixed cost
    tag_id = f.get("tag_id")
    year   = f.get("fc_year")
    month  = f.get("fc_month")
    amount = f.get("fc_amount")

    # If tag_id not explicitly provided, look it up from fixed_cost_tags by name
    if not tag_id and manual_tag:
        tag_row = db.execute(
            "SELECT tag_id FROM fixed_cost_tags WHERE name = ? AND active = 1",
            [manual_tag]
        ).fetchone()
        if tag_row:
            tag_id = tag_row["tag_id"]

    if tag_id and year and month and amount:
        try:
            # Get the transaction date to use as the correct year/month if fc_year/fc_month
            # weren't passed (fallback: use the transaction's own date)
            tx_row = db.execute(
                "SELECT date FROM bank_transactions WHERE transaction_id=?",
                [transaction_id]
            ).fetchone()
            if not year or not month:
                tx_date = (tx_row["date"] if tx_row else "")
                year  = year  or (tx_date[:4] if tx_date else str(__import__("datetime").date.today().year))
                month = month or (str(int(tx_date[5:7])) if tx_date and len(tx_date) >= 7 else str(__import__("datetime").date.today().month))

            # Upsert: delete existing link for this source then re-insert
            # (avoids duplicates when user re-tags the same transaction)
            db.execute("""
                DELETE FROM invoice_fixed_cost_links
                WHERE source_type='bank_transaction' AND source_id=?
            """, [transaction_id])
            db.execute("""
                INSERT INTO invoice_fixed_cost_links
                (source_type, source_id, tag_id, year, month, amount)
                VALUES ('bank_transaction', ?, ?, ?, ?, ?)
            """, [transaction_id, int(tag_id), int(year), int(month), float(amount)])
        except Exception:
            pass

    db.commit()
    return _referrer_anchor(transaction_id, url_for("bank.index"))


# ── Fx statements routes ─────────────────────────────────────────────

@bp.route("/fx")
def fx_index():
    db = get_db()
    _ensure_fx_tables(db)

    account = request.args.get("account", "")
    currency = request.args.get("currency", "")

    where = "WHERE 1=1"
    params = []
    if account:
        where += " AND s.account = ?"; params.append(account)
    if currency:
        where += " AND s.currency = ?"; params.append(currency)

    rows = db.execute(f"""
        SELECT
            t.id as tx_id, t.line_no, t.description, t.debit, t.credit,
            t.rsd_equivalent, t.booking_date, t.kurs, t.nalog_br, t.predmet_br,
            s.id as statement_id, s.statement_number, s.statement_date,
            s.account, s.currency, s.subaccount_name
        FROM fx_bank_transactions t
        JOIN fx_bank_statements s ON s.id = t.statement_id
        {where}
        ORDER BY s.statement_date DESC, t.line_no
    """, params).fetchall()

    accounts = [r[0] for r in db.execute(
        "SELECT DISTINCT account FROM fx_bank_statements ORDER BY account"
    ).fetchall()]
    currencies = [r[0] for r in db.execute(
        "SELECT DISTINCT currency FROM fx_bank_statements ORDER BY currency"
    ).fetchall()]

    return render_template(
        "bank/fx_index.html",
        rows=rows, accounts=accounts, currencies=currencies,
        filters=dict(account=account, currency=currency),
        count=len(rows),
    )


@bp.route("/fx/<int:statement_id>")
def fx_detail(statement_id):
    db = get_db()
    _ensure_fx_tables(db)
    stmt = db.execute("SELECT * FROM fx_bank_statements WHERE id=?", [statement_id]).fetchone()
    if not stmt:
        flash(tr("Izvod nije pronađen."), "error")
        return redirect(url_for("bank.fx_index"))
    txs = db.execute("""
        SELECT * FROM fx_bank_transactions WHERE statement_id=? ORDER BY line_no
    """, [statement_id]).fetchall()
    return render_template("bank/fx_detail.html", stmt=stmt, txs=txs)


@bp.route("/fx/upload", methods=["GET", "POST"])
def fx_upload():
    """Upload one or more Fx Account Statement PDFs, parse, show preview."""
    if request.method == "GET":
        return render_template("bank/fx_upload.html")

    files = request.files.getlist("files")
    if not files or not any(f.filename for f in files):
        flash(tr("Nije izabran nijedan fajl."), "error")
        return redirect(url_for("bank.fx_upload"))

    try:
        import pdfplumber
    except ImportError:
        flash(tr("pdfplumber nije instaliran. Pokrenite: pip install pdfplumber"), "error")
        return redirect(url_for("bank.fx_upload"))

    import tempfile, os, json
    from flask import session

    parsed = []
    errors = []

    for f in files:
        if not f.filename:
            continue
        suffix = os.path.splitext(f.filename)[1].lower()
        if suffix != ".pdf":
            errors.append(f"{f.filename}: nije PDF fajl.")
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        f.save(tmp.name)
        tmp.close()
        try:
            with pdfplumber.open(tmp.name) as pdf:
                text = pdf.pages[0].extract_text() or ""
            result = _parse_fx_statement_text(text, source_file=f.filename)
            if not result.get("statement_number") or not result.get("blocks"):
                errors.append(f"{f.filename}: nije prepoznat kao devizni izvod.")
            else:
                parsed.append(result)
        except Exception as e:
            errors.append(f"{f.filename}: " + tr("greška pri parsiranju —") + f" {e}")
        finally:
            os.unlink(tmp.name)

    for err in errors:
        flash(err, "error")

    if not parsed:
        return redirect(url_for("bank.fx_upload"))

    # Stage parsed data server-side (a temp file, not the session cookie) —
    # Flask's default session is a client-side signed cookie capped at ~4KB;
    # multiple statements' transaction data reliably exceeds that and gets
    # silently dropped, causing "session expired" on commit.
    import tempfile
    fd, stage_path = tempfile.mkstemp(prefix="fx_pdf_preview_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(parsed, fh, ensure_ascii=False)
    session["fx_pdf_preview_path"] = stage_path
    total_tx = sum(len(b["transactions"]) for r in parsed for b in r["blocks"])
    return render_template("bank/fx_preview.html", statements=parsed,
                           count=len(parsed), total_tx=total_tx)


@bp.route("/fx/commit", methods=["POST"])
def fx_commit():
    """Commit parsed Fx statements + transactions to the DB."""
    import json, os
    from flask import session
    db = get_db()
    _ensure_fx_tables(db)

    stage_path = session.pop("fx_pdf_preview_path", None)
    if not stage_path or not os.path.exists(stage_path):
        flash(tr("Sesija je istekla. Molimo ponovo učitajte fajl."), "error")
        return redirect(url_for("bank.fx_upload"))

    try:
        with open(stage_path, "r", encoding="utf-8") as fh:
            statements = json.load(fh)
    except Exception:
        flash(tr("Greška pri čitanju podataka iz sesije."), "error")
        return redirect(url_for("bank.fx_upload"))
    finally:
        try:
            os.remove(stage_path)
        except OSError:
            pass

    inserted_stmts = skipped_stmts = inserted_tx = 0

    for r in statements:
        for b in r.get("blocks", []):
            try:
                db.execute("""
                    INSERT OR IGNORE INTO fx_bank_statements
                        (account, iban, currency, subaccount_code, subaccount_name,
                         statement_number, statement_date, previous_balance,
                         new_balance, source_file)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, [
                    r.get("account"), r.get("iban"), b.get("currency"),
                    b.get("subaccount_code"), b.get("subaccount_name"),
                    r.get("statement_number"), r.get("statement_date"),
                    b.get("previous_balance"), b.get("new_balance"),
                    r.get("source_file"),
                ])
                if db.execute("SELECT changes()").fetchone()[0] == 0:
                    skipped_stmts += 1
                    continue
                inserted_stmts += 1

                stmt_id = db.execute("""
                    SELECT id FROM fx_bank_statements
                    WHERE account=? AND currency=? AND subaccount_code=? AND statement_number=?
                """, [r.get("account"), b.get("currency"), b.get("subaccount_code"),
                      r.get("statement_number")]).fetchone()["id"]

                for tx in b.get("transactions", []):
                    db.execute("""
                        INSERT INTO fx_bank_transactions
                            (statement_id, line_no, description, debit, credit,
                             rsd_equivalent, booking_date, kurs, nalog_br, predmet_br)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, [
                        stmt_id, tx.get("line_no"), tx.get("description"),
                        tx.get("debit", 0), tx.get("credit", 0),
                        tx.get("rsd_equiv", 0), tx.get("booking_date"),
                        tx.get("kurs"), tx.get("nalog_br"), tx.get("predmet_br"),
                    ])
                    inserted_tx += 1
            except Exception as e:
                flash(tr("Greška pri upisu {a0}: {e}", a0=r.get('statement_number','?'), e=e), "error")
                db.rollback()
                return redirect(url_for("bank.fx_upload"))

    db.commit()
    flash(tr("Upisano {inserted_stmts} izvoda ({inserted_tx} transakcija). "
            "Preskočeno (duplikati): {skipped_stmts}.",
            inserted_stmts=inserted_stmts, inserted_tx=inserted_tx,
            skipped_stmts=skipped_stmts), "success")
    return redirect(url_for("bank.fx_index"))
