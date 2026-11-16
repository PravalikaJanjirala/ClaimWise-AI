"""Central configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLAIMWISE_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("CLAIMWISE_DB", ROOT / "storage" / "claimwise.db"))
FILE_STORE = Path(os.environ.get("CLAIMWISE_FILES", ROOT / "storage" / "files"))
CARRIER_DATA = Path(os.environ.get("CLAIMWISE_CARRIER_DATA", DATA_DIR / "carrier_policies.json"))
SAMPLES_DIR = DATA_DIR / "samples"

# Optional mock carrier REST API (see mock_api/server.py). Empty = in-process lookup.
POLICY_API_URL = os.environ.get("CLAIMWISE_POLICY_API_URL", "")

# Optional LLM-assisted extraction (fills only fields OCR rules could not read reliably).
USE_LLM = os.environ.get("CLAIMWISE_USE_LLM", "0") == "1"
LLM_MODEL = os.environ.get("CLAIMWISE_LLM_MODEL", "claude-sonnet-4-6")
OCR_LANG = os.environ.get("CLAIMWISE_OCR_LANG", "eng")

# Verification thresholds
NAME_MATCH = 0.88          # similarity >= this: names match
NAME_REVIEW = 0.70         # between REVIEW and MATCH: ask a human to look
FIELD_CONF_REVIEW = 0.75   # OCR/extraction confidence below this: flag for review
LOW_CONF_DOWNGRADE = 0.60  # a mismatch read at lower confidence is shown as REVIEW, not MISMATCH
ESTIMATE_TOLERANCE = 0.10  # claimed vs garage estimate
SIMILAR_DAYS = 3           # similar-claim window around incident date
MAX_UPLOAD_MB = 10
ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".pdf")

# Demo employee accounts (prototype only; NOT for production use)
DEMO_PASSWORD = os.environ.get("CLAIMWISE_DEMO_PASSWORD", "demo-pass")
DEMO_USERS = ("officer1", "officer2")
