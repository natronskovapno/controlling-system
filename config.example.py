"""Configuration. Copy to config.py and edit.

    cp config.example.py config.py

config.py is git-ignored, so credentials never reach the repository. Nothing
here is secret by default: the paths are resolved relative to this file, and
every credential is read from the environment and falls back to empty.

Verify a setup by running it:

    python config.py

It prints the resolved paths, says whether each exists, and masks the
credentials so the output is safe to paste into an issue.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ===========================================================================
# 1. PATHS
# ===========================================================================
# The project root is this file's own directory, so the checkout can live
# anywhere, on any drive, under any folder name. Override with an environment
# variable if you keep the data somewhere else.

PROJECT_ROOT_PATH = Path(
    os.environ.get("CONTROLLING_ROOT") or Path(__file__).resolve().parent
).resolve()
PROJECT_ROOT = str(PROJECT_ROOT_PATH)


def _p(*parts) -> str:
    return str(PROJECT_ROOT_PATH.joinpath(*parts))


# The demo database the generator writes. Point this at a real database to run
# the system against live data.
DB_PATH = os.environ.get("CONTROLLING_DB") or _p("demo.db")

SCRIPTS_DIR = _p("ingestion")
DATA_DIR = _p("data")
DB_DUMP_DIR = _p("db_dump")

# The interpreter currently running, used when one script launches another.
PYTHON_EXE = sys.executable


# ===========================================================================
# 2. DEMO MODE
# ===========================================================================
# With DEMO_MODE on, the app refuses every write and every outbound API call.
# That is what makes a public demo safe to leave running: visitors can explore
# every screen but cannot change the data or burn through an API quota.
#
# Turn it off for a real deployment.

DEMO_MODE = os.environ.get("CONTROLLING_DEMO_MODE", "1") not in ("0", "false", "False")


# ===========================================================================
# 3. FLASK
# ===========================================================================

# Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY = os.environ.get("CONTROLLING_SECRET_KEY") or "change-me-before-deploying"
FLASK_PORT = int(os.environ.get("PORT", "5000"))
FLASK_DEBUG = os.environ.get("CONTROLLING_DEBUG", "0") in ("1", "true", "True")

# Token for the read-only SQL API blueprint. Requests are refused when unset.
API_TOKEN = os.environ.get("CONTROLLING_API_TOKEN", "")


# ===========================================================================
# 4. POINT OF SALE
# ===========================================================================
# The POS back office exposes a REST API. Sales, stock documents and closing
# stock levels are pulled from it one day at a time.

POS_BASE_URL = os.environ.get("POS_BASE_URL", "https://pos.example.internal/api")
POS_USERNAME = os.environ.get("POS_USERNAME", "")
POS_PASSWORD = os.environ.get("POS_PASSWORD", "")
POS_TIMEOUT = 30

# Which till maps to which venue. The POS reports a machine name per receipt.
POS_VENUE_MAP = {
    "POS-PC": "Corner",
    "POS-PC2": "Garden",
    "POS-PC3": "Garden",
    "POS-PC4": "Garden",
}

# Guest account IDs used to flag entertainment/host bills, which are revenue
# in the POS but a non-deductible expense for VAT.
POS_HOST_ACCOUNT_IDS = {1035, 1037, 1038, 1039, 1040, 1041, 1042}

POS_DOCUMENT_TYPES = ["InputInvoice", "WriteOff", "Inventory"]
POS_COGS_INVOICE_TYPES = ["Normal"]
POS_FETCH_INPUT_PRICES = True


# ===========================================================================
# 5. E-INVOICING
# ===========================================================================
# Serbia's national e-invoicing platform (SEF). Every incoming and outgoing
# invoice is fetched from here, which is what makes the VAT return computable
# rather than transcribed.

SEF_API_KEY = os.environ.get("SEF_API_KEY", "")
SEF_BASE_URL = os.environ.get("SEF_BASE_URL", "https://efaktura.mfin.gov.rs")
SEF_DATE_FROM = "2022-01-01T00:00:00"

# The SEF endpoint drops the connection under sustained polling, so requests
# are retried with a backoff rather than failing the whole backfill.
SEF_MAX_RETRIES = 5
SEF_BACKOFF_SECONDS = 2.0


# ===========================================================================
# 6. WEB ANALYTICS
# ===========================================================================
# Cloudflare Web Analytics, read through the GraphQL API.
#
# Never widen the query range: past roughly seven days Cloudflare silently
# switches to a 1:10 sampled dataset and does not scale the counts back up, so
# a wide query understates traffic by about 4x. The ingestion script asks for
# one day at a time for exactly this reason.

CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_SITE_TAGS = {
    # "<site tag>": "<hostname>",
}
CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
CF_TIMEOUT = 60

# Nothing exists before tracking was installed.
CF_FIRST_DATE = os.environ.get("CF_FIRST_DATE", "2026-04-15")

# The free plan drops data after about 30 days; 28 leaves a safety margin.
CF_MAX_BACKFILL_DAYS = 28

# Trailing days re-pulled on every run, because a day's figures keep settling
# for a while after midnight. Ingestion is delete-then-insert per date, so
# re-running a day corrects it instead of duplicating it.
CF_RESETTLE_DAYS = 2

# When true, the first page load of each day fires a background catch-up.
# Preferred over a scheduled task because the machine is not always on.
CF_AUTO_INGEST = not DEMO_MODE


# ===========================================================================
# 7. SELF-CHECK
# ===========================================================================

_EXPECTED = [
    ("scripts", SCRIPTS_DIR),
    ("data", DATA_DIR),
    ("db_dump", DB_DUMP_DIR),
]


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(not set)"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def describe() -> bool:
    db_ok = os.path.isfile(DB_PATH)
    size = f"{os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB" if db_ok else "MISSING"

    print("=" * 72)
    print("  CONTROLLING SYSTEM — RESOLVED CONFIGURATION")
    print("=" * 72)
    print(f"  project root : {PROJECT_ROOT}")
    print(f"  python       : {PYTHON_EXE}")
    print(f"  demo mode    : {'ON (writes and API calls disabled)' if DEMO_MODE else 'off'}")
    print("-" * 72)
    print(f"  {'OK ' if db_ok else '!! '} database   {DB_PATH}  ({size})")
    for label, path in _EXPECTED:
        print(f"  {'OK ' if os.path.isdir(path) else '-- '} {label:10} {path}")
    print("-" * 72)
    print(f"  POS          : {POS_USERNAME or '(not set)'} @ {POS_BASE_URL}")
    print(f"  SEF key      : {_mask(SEF_API_KEY, 8)}")
    print(f"  CF token     : {_mask(CF_API_TOKEN, 6)}")
    print(f"  Flask secret : {_mask(FLASK_SECRET_KEY, 3)}")
    print("=" * 72)
    if not db_ok:
        print("  No database yet. Build the demo one with:")
        print("      python -m demo_data.generate")
        print("=" * 72)
    return db_ok


if __name__ == "__main__":
    sys.exit(0 if describe() else 1)
