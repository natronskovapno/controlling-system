"""
bb_parser.py — Bruto bilans parser (PDF + XLSX) for the Finansijski izveštaji module.

parse_file(path) -> dict:
  period_from, period_to (ISO), company, pib, fmt,
  lines: [{konto, naziv, ps_d, ps_p, tp_d, tp_p, uk_d, uk_p}]   (analytic kontos only)
  checks: [{name, ok, detail, args}], warnings: [(key, args)]

  Diagnostics are returned as a catalogue key plus its arguments rather than
  a finished sentence, so the upload preview can render them in either
  language. Older snapshots stored a plain string, which the templates still
  accept.
Saldo is not stored from the file; it is derived as uk_d - uk_p.

Two PDF layouts are recognised:

  A — "legacy": POČETNO STANJE | TEKUĆI PROMET | UKUPAN PROMET | SALDO.
      Parsed positionally from the sequence of numbers on the line.

  B — "TP/SALDO" (bookkeeper changed format in 2026): TEKUĆI PROMET | SALDO only.
      There is no opening-balance column and no separate cumulative-turnover
      column; the period runs from 1 January, so TEKUĆI PROMET already *is*
      the cumulative turnover and saldo = promet D − promet P on every row.
      Mapped as ps = 0, tp = uk = the two promet columns.

      Layout B leaves blanks in the middle of a line (a konto with only a
      credit saldo prints nothing in the saldo-duguje column), so the numbers
      cannot be read in sequence — each one is assigned to a column by where
      it ENDS on the line. The column right edges come from the
      "KONTO OPIS DUGUJE POTRAŽUJE ..." header, which repeats on every page.
"""
import re
from datetime import date

NUM_RE = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d{2}')
KONTO_RE = re.compile(r'^\s*(\d{1,6})\b')
PERIOD_RE = re.compile(r'(\d{2})\.(\d{2})\.(\d{2,4})\s*-\s*(\d{2})\.(\d{2})\.(\d{2,4})')
CLOSING_KONTOS = ('599', '699')          # plus whole class 7

# Column header of layout B. Some pages render Ž as @ (font artefact in the
# bookkeeper's export), so the cedilla character is matched loosely.
COLHDR_RE = re.compile(r'KONTO\s+OPIS', re.I)
COLTOK_RE = re.compile(r'DUGUJE|POTRA[ŽZ@]UJE')
COL_TOL = 3                              # how far a number may end from its column edge

LAYOUT_LEGACY = 'PDF'
LAYOUT_TP_SALDO = 'PDF (promet + saldo)'


def _num(s):
    return float(s.replace('.', '').replace(',', '.'))


def _iso(d, m, y):
    y = int(y)
    if y < 100:
        y += 2000
    return date(y, int(m), int(d)).isoformat()


def _header_meta(text):
    meta = {'period_from': None, 'period_to': None, 'company': None, 'pib': None}
    flat = re.sub(r'\s+', ' ', text)
    m = PERIOD_RE.search(flat)
    if m:
        meta['period_from'] = _iso(*m.group(1, 2, 3))
        meta['period_to'] = _iso(*m.group(4, 5, 6))
    p = re.search(r'PIB:\s*(\d{9})', flat)
    if p:
        meta['pib'] = p.group(1)
    c = re.search(r'([A-ZČĆŠĐŽ][A-ZČĆŠĐŽ ]+DOO)', flat)
    if c:
        meta['company'] = re.sub(r'(?<=[ČĆŠĐŽ]) (?=[A-Z])', '', c.group(1)).strip()
    return meta


# ── PDF ──────────────────────────────────────────────────────────────
def _pdf_text(path):
    try:
        import pypdf
        r = pypdf.PdfReader(path)
        return '\n'.join(p.extract_text(extraction_mode='layout') or '' for p in r.pages)
    except ImportError:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return '\n'.join(p.extract_text(layout=True) or '' for p in pdf.pages)
    except ImportError:
        raise RuntimeError('Za PDF je potreban paket pypdf (pip install pypdf) ili pdfplumber.')


def _column_edges(text):
    """
    Right edges of the numeric columns, taken from the first column-header line.
    Returns [] when the file has no such header (then layout A is assumed).
    """
    for line in text.splitlines():
        if COLHDR_RE.search(line):
            edges = [m.end() + 1 for m in COLTOK_RE.finditer(line)]
            if edges:
                return edges
    return []


def _parse_pdf_legacy(text):
    """Layout A — read the numbers in sequence; unchanged behaviour."""
    rows, subtotals, classes, ukupno = [], {}, {}, None
    for line in text.splitlines():
        nums = NUM_RE.findall(line)
        if line.strip().upper().startswith('UKUPNO') and len(nums) >= 6:
            ukupno = [_num(x) for x in nums[:8]]
            continue
        km = KONTO_RE.match(line)
        if not km or len(nums) < 6:
            continue
        konto = km.group(1)
        first_num = NUM_RE.search(line)
        naziv = line[km.end():first_num.start()].strip()
        vals = [_num(x) for x in nums]
        if len(konto) == 1 and len(vals) == 8:
            classes[konto] = vals
        elif len(konto) == 3 and len(vals) == 8 and not naziv:
            subtotals[konto] = vals
        elif len(vals) in (6, 7):
            rows.append({'konto': konto, 'naziv': naziv or None,
                         'ps_d': vals[0], 'ps_p': vals[1], 'tp_d': vals[2],
                         'tp_p': vals[3], 'uk_d': vals[4], 'uk_p': vals[5]})
    return text[:3000], rows, subtotals, classes, ukupno, LAYOUT_LEGACY, {}


def _columns_on_line(line, edges):
    """
    Place every number on the line into its column by right edge.
    Returns (values, placed, stray) — stray counts numbers that landed
    nowhere, which would mean the header offsets do not fit this line.
    """
    vals = [0.0] * len(edges)
    placed = stray = 0
    for m in NUM_RE.finditer(line):
        end = m.end()
        ci = min(range(len(edges)), key=lambda i: abs(end - edges[i]))
        if abs(end - edges[ci]) > COL_TOL:
            stray += 1
            continue
        vals[ci] = _num(m.group(0))
        placed += 1
    return vals, placed, stray


def _parse_pdf_tp_saldo(text, edges):
    """
    Layout B — TEKUĆI PROMET (duguje, potražuje) + SALDO (duguje, potražuje).

    Normalised onto the legacy shape so everything downstream is unchanged:
        ps_d = ps_p = 0
        tp_d = uk_d = promet duguje
        tp_p = uk_p = promet potražuje
    The saldo columns are not stored (the engine derives saldo as uk_d − uk_p);
    they are used here only to verify the file.
    """
    rows, subtotals, classes, ukupno = [], {}, {}, None
    saldo_d_sum = saldo_p_sum = 0.0
    saldo_mismatch = []
    stray_lines = 0

    for line in text.splitlines():
        if COLHDR_RE.search(line):          # repeated page header
            continue
        vals, placed, stray = _columns_on_line(line, edges)
        if not placed:
            continue
        if stray:
            stray_lines += 1
        tp_d, tp_p = vals[0], vals[1]
        s_d, s_p = (vals[2], vals[3]) if len(vals) > 3 else (0.0, 0.0)

        if line.strip().upper().startswith('UKUPNO'):
            # pad onto the 8-slot legacy vector: ps, tp, uk, saldo
            ukupno = [0.0, 0.0, tp_d, tp_p, tp_d, tp_p, s_d, s_p]
            continue

        km = KONTO_RE.match(line)
        if not km:
            continue
        konto = km.group(1)
        first_num = NUM_RE.search(line)
        naziv = line[km.end():first_num.start()].strip() if first_num else ''
        six = [0.0, 0.0, tp_d, tp_p, tp_d, tp_p]

        if len(konto) == 1:
            classes[konto] = six
        elif len(konto) == 3 and not naziv:
            # a 3-digit konto with no description is the synthetic subtotal.
            # A 3-digit konto that HAS one (e.g. 499 Pasiv.vrem.razgran.) is a
            # real analytic account and falls through to the branch below.
            subtotals[konto] = six
        else:
            rows.append({'konto': konto, 'naziv': naziv or None,
                         'ps_d': 0.0, 'ps_p': 0.0, 'tp_d': tp_d, 'tp_p': tp_p,
                         'uk_d': tp_d, 'uk_p': tp_p})
            saldo_d_sum += s_d
            saldo_p_sum += s_p
            if abs((tp_d - tp_p) - (s_d - s_p)) > 0.5:
                saldo_mismatch.append(konto)

    extra = {'saldo_d': saldo_d_sum, 'saldo_p': saldo_p_sum,
             'saldo_mismatch': saldo_mismatch, 'stray_lines': stray_lines,
             'ukupno_saldo': (ukupno[6], ukupno[7]) if ukupno else None}
    return text[:3000], rows, subtotals, classes, ukupno, LAYOUT_TP_SALDO, extra


def _parse_pdf(path):
    text = _pdf_text(path)
    edges = _column_edges(text)
    # Layout B has exactly two DUGUJE/POTRAŽUJE pairs (promet + saldo).
    # Layout A has more column groups, or no such header at all.
    if len(edges) == 4:
        head, rows, subs, classes, ukupno, layout, extra = _parse_pdf_tp_saldo(text, edges)
        if len(rows) > 20:
            return head, rows, subs, classes, ukupno, layout, extra
        # column reading found almost nothing — fall back rather than fail
    return _parse_pdf_legacy(text)


# ── XLSX ─────────────────────────────────────────────────────────────
def _parse_xlsx(path):
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True).active
    head = ' '.join(''.join(str(c) for c in r if c is not None)
                    for r in ws.iter_rows(min_row=1, max_row=10, values_only=True))
    rows, subtotals, classes, ukupno = [], {}, {}, None
    for r in ws.iter_rows(min_row=1, values_only=True):
        k = r[0]
        if k is None:
            continue
        k = str(k).strip()
        vals = [float(v) if isinstance(v, (int, float)) else 0.0 for v in r[2:10]]
        if k.upper().startswith('UKUPNO'):
            ukupno = vals
            continue
        if not k.isdigit():
            continue
        naziv = (str(r[1]).strip() if r[1] else '') or None
        # xlsx has no blank-description marker for subtotals; a 3-digit konto whose
        # 4-digit children precede it is a subtotal — resolved after the loop.
        rows.append({'konto': k, 'naziv': naziv, 'ps_d': vals[0], 'ps_p': vals[1],
                     'tp_d': vals[2], 'tp_p': vals[3], 'uk_d': vals[4], 'uk_p': vals[5]})
    out = []
    for i, row in enumerate(rows):
        k = row['konto']
        if len(k) == 1:
            classes[k] = [row[c] for c in ('ps_d', 'ps_p', 'tp_d', 'tp_p', 'uk_d', 'uk_p')]
            continue
        if len(k) == 3:
            has_children = any(o['konto'].startswith(k) and len(o['konto']) > 3 for o in out)
            dup_prev = out and out[-1]['konto'] == k
            if has_children or dup_prev:
                subtotals[k] = [row[c] for c in ('ps_d', 'ps_p', 'tp_d', 'tp_p', 'uk_d', 'uk_p')]
                continue
        out.append(row)
    return head, out, subtotals, classes, ukupno, 'XLSX', {}


# ── validation ───────────────────────────────────────────────────────
def _sr(x):
    return f'{x:,.2f}'.replace(',', ' ').replace('.', ',').replace(' ', '.')


def _close(a, b, tol=1.0):
    return abs(a - b) <= tol


def parse_file(path):
    ext = path.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        head, rows, subtotals, classes, ukupno, layout, extra = _parse_pdf(path)
        fmt = layout
    elif ext in ('xlsx', 'xlsm'):
        head, rows, subtotals, classes, ukupno, layout, extra = _parse_xlsx(path)
        fmt = layout
    else:
        raise RuntimeError('Podržani formati: PDF, XLSX.')

    meta = _header_meta(head)
    checks, warnings = [], []
    cols = ('ps_d', 'ps_p', 'tp_d', 'tp_p', 'uk_d', 'uk_p')

    checks.append({'name': 'Period prepoznat', 'ok': bool(meta['period_to']),
                   'detail': f"{meta['period_from']} → {meta['period_to']}"})
    checks.append({'name': 'Analitička konta', 'ok': len(rows) > 20,
                   'detail': '{a0} redova', 'args': {'a0': len(rows)}})

    if ukupno:
        tot = [sum(r[c] for r in rows) for c in cols]
        ok = all(_close(tot[i], ukupno[i], 5) for i in range(6))
        checks.append({'name': 'Zbir analitike = UKUPNO', 'ok': ok,
                       'detail': 'ukupan promet D {a0} / UKUPNO {a1}',
                       'args': {'a0': _sr(tot[4]), 'a1': _sr(ukupno[4])}})
        checks.append({'name': 'UKUPNO duguje = potražuje', 'ok': _close(ukupno[4], ukupno[5], 5),
                       'detail': f'{_sr(ukupno[4])} / {_sr(ukupno[5])}'})
    else:
        checks.append({'name': 'UKUPNO red pronađen', 'ok': False, 'detail': 'nije pronađen'})

    bad_classes = []
    for k, v in classes.items():
        s = [sum(r[c] for r in rows if r['konto'].startswith(k)) for c in cols]
        if not all(_close(s[i], v[i], 5) for i in range(6)):
            bad_classes.append(k)
    if bad_classes:
        class_detail = {'detail': 'odstupanje: klase {a0}', 'args': {'a0': ', '.join(bad_classes)}}
    else:
        class_detail = {'detail': 'sve klase se slažu'}
    checks.append({'name': 'Zbir po klasama', 'ok': not bad_classes and bool(classes),
                   **class_detail})

    bad_sub = []
    for k, v in subtotals.items():
        s = [sum(r[c] for r in rows if r['konto'][:3] == k) for c in cols]
        if not all(_close(s[i], v[i], 5) for i in range(6)):
            bad_sub.append(k)
    if bad_sub:
        warnings.append(('Sintetička konta sa odstupanjem: {a0}', {'a0': ', '.join(bad_sub[:15])}))

    # ── layout B only ────────────────────────────────────────────────
    if fmt == LAYOUT_TP_SALDO:
        mism = extra.get('saldo_mismatch') or []
        if mism:
            _detail, _dargs = ('{a0} konta odstupa: {a1}',
                               {'a0': len(mism), 'a1': ', '.join(mism[:10])})
        else:
            _detail, _dargs = ('svi redovi', {})
        checks.append({
            'name': 'Saldo = promet (D − P)',
            'ok': not mism,
            'detail': _detail,
            'args': _dargs,
        })
        uk_s = extra.get('ukupno_saldo')
        if uk_s:
            checks.append({
                'name': 'Zbir salda = UKUPNO saldo',
                'ok': _close(extra['saldo_d'], uk_s[0], 5) and _close(extra['saldo_p'], uk_s[1], 5),
                'detail': 'D {a0} / {a1} · P {a2} / {a3}',
                'args': {'a0': _sr(extra['saldo_d']), 'a1': _sr(uk_s[0]),
                         'a2': _sr(extra['saldo_p']), 'a3': _sr(uk_s[1])},
            })
        if extra.get('stray_lines'):
            warnings.append(('{a0} redova sa brojem van kolone — proverite raspored u PDF-u.',
                             {'a0': extra['stray_lines']}))
        warnings.append(
            ('Format bez početnog stanja: kolone su tekući promet i saldo, pa je početno '
             'stanje upisano kao nula. Period počinje 1. januara, tako da bilans stanja i '
             'bilans uspeha nisu pogođeni, ali Tokovi gotovine za ovaj snimak nemaju '
             'početnu kolonu.', {}))

    is_closing = any(r['konto'].startswith('7') or r['konto'][:3] in CLOSING_KONTOS for r in rows)
    if is_closing:
        warnings.append(('Zaključni bilans: klase 5 i 6 su zatvorene — rezultat se čita iz prometa.', {}))
    no_name = sum(1 for r in rows if not r['naziv'])
    if no_name:
        warnings.append(('{a0} konta bez naziva u fajlu (naziv se preuzima iz drugih uvezenih bilansa).',
                         {'a0': no_name}))

    return {**meta, 'fmt': fmt, 'lines': rows, 'checks': checks, 'warnings': warnings,
            'is_closing': is_closing, 'ok': all(c['ok'] for c in checks)}
