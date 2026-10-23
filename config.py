"""Central configuration. Importing this module loads `.env` once for the whole app."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# --- Models -----------------------------------------------------------------
INTAKE_MODEL = env("INTAKE_MODEL", "claude-sonnet-5-5")
# Supplier-eye review of the finished request (a judgement task, so same tier as intake).
REVIEW_MODEL = env("REVIEW_MODEL", INTAKE_MODEL)
# 0 = only ask essential/important questions (default). 1 = also ask minor nice-to-know ones.
REVIEW_THOROUGH = env("REVIEW_THOROUGH", "0") == "1"
# 1 = ask for photos BEFORE any questions, and let the reviewer study them so it only asks what they can't answer.
PHOTOS_FIRST = env("PHOTOS_FIRST", "1") == "1"
# Photos shown to the reviewer (downscaled, roughly 1,000 tokens each): a cost cap.
MAX_REVIEW_PHOTOS = int(env("MAX_REVIEW_PHOTOS", "4"))
# Rough measurements/counts/conditions read from the customer's photos. Cheapest model: one small call per tap.
ESTIMATE_MODEL = env("ESTIMATE_MODEL", "claude-haiku-5-5")
# Photo estimates allowed per request (each is one vision call): a cost cap.
MAX_ESTIMATES_PER_REQUEST = int(env("MAX_ESTIMATES_PER_REQUEST", "3"))

# --- Website -----------------------------------------------------------------
# Public https URL of the site (no trailing slash), used for customers' progress links, e.g. https://quoteschmote.com
PUBLIC_BASE_URL = (env("PUBLIC_BASE_URL", "") or "").rstrip("/")

# --- Website chat limits ----------------------------------------------------
# Quote requests one mobile number may confirm per rolling 24h (the paid tier raises this later).
DAILY_QUOTE_LIMIT = int(env("DAILY_QUOTE_LIMIT", "10"))
# Across all customers: stops a flood (or a bug) from running up costs or emailing too many suppliers.
GLOBAL_DAILY_JOBS = int(env("GLOBAL_DAILY_JOBS", "50"))
# New chats one IP may open per rolling 24h. Needed because mobile numbers aren't SMS-verified yet.
IP_DAILY_SESSIONS = int(env("IP_DAILY_SESSIONS", "20"))
MAX_MESSAGES_PER_SESSION = int(env("MAX_MESSAGES_PER_SESSION", "60"))
# Chats now start before we know who the customer is, so cap total website messages per rolling 24h.
# Each message costs roughly half a cent of AI usage, so the default bounds worst-case spend to a few dollars a day.
GLOBAL_DAILY_MESSAGES = int(env("GLOBAL_DAILY_MESSAGES", "1500"))

# --- Safety -----------------------------------------------------------------
# dry_run: nothing is sent to anyone (messages are only recorded). live: real sends.
OUTBOUND_MODE = env("OUTBOUND_MODE", "dry_run")
