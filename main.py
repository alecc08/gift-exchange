"""Configurable family gift exchange (Secret Santa) server.

FastAPI + SQLite, single-file backend:
  * admin API (config, roster, draw execution) guarded by ADMIN_PASSWORD or a
    short-lived signed session token
  * public per-participant API guarded only by a UUID token
  * a randomized slot-allocation + backtracking matching engine

The organizer is deliberately kept blind: no admin endpoint returns a pairing, and the only thing
read from the `assignments` table is one COUNT of same-group pairs — enough to admit that a draw had
to relax, never enough to name one.

Security posture (see PLAN.md "Security hardening"): per-IP rate limits, a request
size cap, security headers with a CDN-pinning CSP, HMAC session tokens, constant-time
comparisons, input length caps, serialised draw/reset, and an audit trail.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Starlette picks content types from `mimetypes`, and the stdlib map does not know every
# extension this app ships. A manifest served as application/octet-stream is a silently broken
# "add to home screen" on Android, so both are pinned rather than guessed.
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/svg+xml", ".svg")

MAX_ATTEMPTS = 20000
MAX_DRAW_QUOTA = 10

# Field length caps: mirrored by the frontend so nobody hits a 422 blind.
MAX_NAME = 80
MAX_FAMILY_GROUP = 60
MAX_GROUP_KEY = 60
MAX_WISHLIST = 1000
MAX_EVENT_TITLE = 120
MAX_BUDGET_NOTE = 500
MAX_EMAIL = 320

# Deliberately permissive on the local part (unicode allowed) and strict on the shape: the typos a
# family roster actually produces are a missing @, a stray space and a domain with no dot. Real
# deliverability is answered by Resend's send response and by bounces, not by a stronger regex.
EMAIL_RE = re.compile(
    r"^[^@\s]{1,120}@[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$",
    re.UNICODE,
)

EMPTY_TARGET_GROUP = "Cannot assign gifts to an empty target group"
UNSOLVABLE = (
    "The draw counts cannot be satisfied with this roster: somebody has more "
    "gifts to give than there are people allowed to receive them."
)
LOCKED_MSG = "The draw is locked. Reset it before making changes."
ALREADY_LOCKED = "The draw is already locked. Reset it before drawing again."
NO_PARTICIPANTS = "Cannot run a draw with no participants"
DRAW_NOT_FOUND = "This draw does not exist"
DRAW_ID_REQUIRED = "No draw selected"
DRAW_NONE = "There is no draw yet"
DRAW_TITLE_REQUIRED = "A draw needs a name"
TOO_LARGE = "Request body too large"
EMAIL_NOT_CONFIGURED = "Email sending is not configured on this server"
EMAIL_NO_ADDRESS = "This participant has no email address"
EMAIL_SEND_FAILED = "The email provider refused the send"
# A mail whose links point at 127.0.0.1 is worse than no mail: it looks delivered, and every link in
# it is dead for the person holding it. Seen for real — an admin call made from inside the container
# carries `Host: 127.0.0.1:8000`, and that is what `public_base_url()` echoes into the links.
EMAIL_BAD_BASE_URL = ("This server cannot name its own public address, so the links it would send "
                      "are not reachable: set PUBLIC_BASE_URL")

DEFAULT_ADMIN_PASSWORD = "admin"

# --------------------------------------------------------------------------- #
# Security configuration
# --------------------------------------------------------------------------- #


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def admin_password() -> str:
    """The organizer password.

    Unset falls back to the spec default (and is flagged everywhere as such). A **blank**
    value is not a password: it returns "" so nothing can authenticate. That case is not
    hypothetical — a compose file expanding `${MAYBE_UNSET_SECRET}` hands the process an
    empty string, and `compare_digest("", "")` would otherwise hand anyone the console.
    """
    raw = os.environ.get("ADMIN_PASSWORD")
    if raw is None:
        return DEFAULT_ADMIN_PASSWORD
    return "" if raw.strip() == "" else raw


def using_default_password() -> bool:
    """True when the organizer console is not properly configured (default or blank)."""
    return admin_password() in (DEFAULT_ADMIN_PASSWORD, "")


def trust_proxy() -> bool:
    return os.environ.get("TRUST_PROXY", "").lower() in {"1", "true", "yes", "on"}


LOGIN_LIMIT = env_int("LOGIN_RATE_LIMIT", 10)        # login attempts / minute / client
LOGIN_LOCKOUT_THRESHOLD = env_int("LOGIN_LOCKOUT_THRESHOLD", 5)
LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 300)
DRAW_LIMIT = env_int("DRAW_RATE_LIMIT", 120)         # participant lookups / minute / client
API_LIMIT = env_int("API_RATE_LIMIT", 300)           # everything else / minute / client
MAX_BODY_BYTES = env_int("MAX_BODY_BYTES", 16_384)
SESSION_TTL_SECONDS = env_int("SESSION_TTL_SECONDS", 12 * 3600)

# --------------------------------------------------------------------------- #
# Email delivery (Resend)
# --------------------------------------------------------------------------- #
#
# Read per call, like db_path(), so tests (and a container restart with new env) are honest.
# A blank RESEND_API_KEY means the feature is OFF: every send fails closed with a clear message,
# the same shape as a blank ADMIN_PASSWORD closing the console. The key is read here and in
# send_email only — it never reaches a log line, an API response or a git-managed file.

def resend_api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def email_enabled() -> bool:
    return bool(resend_api_key())


def resend_from() -> str:
    return os.environ.get("RESEND_FROM", "").strip()


def resend_base_url() -> str:
    """Resend's API root. RESEND_BASE_URL exists so tests can point sends at a local stub
    instead of the internet; the default is the real API."""
    return os.environ.get("RESEND_BASE_URL", "https://api.resend.com").rstrip("/")


EMAIL_TIMEOUT = env_int("RESEND_TIMEOUT", 15)
EMAIL_SEND_LIMIT = env_int("EMAIL_RATE_LIMIT", 60)   # sends / minute / organizer client


def email_send_interval_ms() -> int:
    """Spacing between ANY two sends. Resend's free tier answers 429 above ~1 req/s."""
    return env_int("EMAIL_SEND_INTERVAL_MS", 1200)


def email_same_address_gap_s() -> int:
    """Minimum spacing between two sends to the SAME mailbox. A family shares addresses — four
    kids' links land at one parent — and four emails in one inbox inside two seconds reads like
    abuse to the receiver's spam filter even when the sender is fully authenticated."""
    return env_int("EMAIL_SAME_ADDRESS_GAP_S", 4)


def email_retry_after_s() -> int:
    """Sleep after a Resend 429, then retry once."""
    return env_int("EMAIL_RETRY_AFTER_S", 3)


_email_gate = threading.Lock()          # one send at a time across the whole process
_email_gate_at = {"global": 0.0, "by_address": {}}


def _throttle_before_send(address: str) -> None:
    """Block until this address is allowed to receive its next mail. Held across the lock so two
    overlapping bulk actions cannot jointly outrun either spacing."""
    with _email_gate:
        now = time.monotonic()
        wait = email_send_interval_ms() / 1000.0 - (now - _email_gate_at["global"])
        last = _email_gate_at["by_address"].get(address)
        if last is not None:
            wait = max(wait, email_same_address_gap_s() - (now - last))
        if wait > 0:
            time.sleep(wait)
        sent_at = time.monotonic()
        _email_gate_at["global"] = sent_at
        _email_gate_at["by_address"][address] = sent_at


# Resend sits behind Cloudflare, and that edge answers `403 / error code: 1010` to Python's
# default `Python-urllib/3.x` agent — measured, not folklore. The fix is the boring one: send the
# same ordinary agent string any browser sends. Still overridable with `RESEND_USER_AGENT` in case
# a future edge decides a browser UA is itself suspicious.
EMAIL_USER_AGENT = os.environ.get(
    "RESEND_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def _group_by_address(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Participants bundled by the address they gave, in first-seen roster order.

    One mailbox, one email. At a family event the kids' links all go to one parent, and four
    near-identical mails landing in one inbox inside a minute read as abuse — to the receiver's spam
    filter and to the parent. Bundling lives here, in one place, so that *every* send path (bulk,
    per-row, auto-on-draw) obeys the same rule instead of each remembering to.

    A blank or unsendable address stays a group of one: the report still has to name the person who
    was skipped, and a skipped uncle must not be hidden inside his neighbour's row.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for person in people:
        address = (person.get("email") or "").strip()
        key = address.lower() if address and _sendable_email(address) else f"@skip:{person['id']}"
        if key not in groups:
            groups[key] = {"address": address, "people": []}
            order.append(key)
        groups[key]["people"].append(person)
    return [groups[key] for key in order]


def _skip_rows(people: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
    """One report row per skipped person — never one error that hides who was skipped."""
    return [{"id": person["id"], "name": person["name"], "ok": False, "reason": reason,
             "message_id": None, "shared_with": []} for person in people]


def deliver_household_email(conn: sqlite3.Connection, group: dict[str, Any], event_title: str,
                            language: str, base_url: str, ready: bool) -> list[dict[str, Any]]:
    """Send one email covering everyone at one address, and stamp every one of them.

    Returns a row per participant, because that is how the roster is read; each row carries
    `shared_with` so the console can say "one mail, three people" instead of implying three sends.

    `ready` (has this draw been drawn?) is an argument, not a re-read of a settings row: with
    several draws on one server there is no single row to read, and the caller knows which draw it
    is mailing.

    Never raises: a batch of twelve must not die on the fourth address.
    """
    people = group["people"]
    address = group["address"]
    if not email_enabled():
        return _skip_rows(people, EMAIL_NOT_CONFIGURED)
    if not base_url:
        # A mail whose links are relative or point at the server's own machine is a mail nobody can
        # open, so it is not sent at all. The organizer sees why in the report.
        return _skip_rows(people, EMAIL_BAD_BASE_URL)
    if not address or not _sendable_email(address):
        return _skip_rows(people, EMAIL_NO_ADDRESS)

    # Spacing happens right before the wire: the organizer may fire a single "Envoyer" in the
    # middle of a bulk run, and one shared process gate keeps both honest.
    _throttle_before_send(address.lower())
    links = [{"name": person["name"], "link": participant_link(base_url, person["token"])}
             for person in people]
    content = build_links_email(links, event_title, language, ready, assets_base=base_url)
    ids = [person["id"] for person in people]
    try:
        message_id = send_email(to=address, subject=content["subject"], html_body=content["html"],
                                text_body=content["text"],
                                ref_id="+".join(f"gift-exchange-{person_id}" for person_id in ids))
    except EmailSendError as error:
        audit("email_send_failed", participant_ids=",".join(map(str, ids)), status=error.status,
              error=error.message[:120])
        return _skip_rows(people, EMAIL_SEND_FAILED)

    stamp = utc_now()
    for person in people:
        conn.execute("UPDATE participants SET email_sent_at = ? WHERE id = ?", (stamp, person["id"]))
    # The address itself stays out of the log: participant ids + the provider's message id are
    # enough to trace a send through Resend's own event feed.
    audit("email_sent", participant_ids=",".join(map(str, ids)), people=len(people),
          message_id=message_id or None)
    names_by_id = {person["id"]: person["name"] for person in people}
    return [{"id": person["id"], "name": person["name"], "ok": True, "reason": None,
             "message_id": message_id or None,
             "shared_with": [name for other_id, name in names_by_id.items()
                             if other_id != person["id"]]}
            for person in people]


def _clean_email_value(value: str) -> str:
    """Normalize one address: trim, lowercase, cap the length. Blank stays blank — the field is
    opt-out. No format rejection: the organizer asked for free-text, a weird address is not a
    reason to 422 a parent mid-typing. It stays weird in the cell and the UI flags it softly;
    deliver_household_email only refuses to spend an API call on it at send time.
    (Lowercasing is still worth it: no provider people actually use honors case, and
    "same person, two spellings" is how a relative gets two invites.)"""
    return value.strip().lower()[:MAX_EMAIL]


def _sendable_email(value: str) -> bool:
    """Does this look like it can carry mail? Checked only at send time — see _clean_email_value."""
    return bool(EMAIL_RE.match(value or ""))

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.tailwindcss.com https://cdn.jsdelivr.net 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://tirage-noel.alserv.org; "
        "connect-src 'self'; "
        "base-uri 'self'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
    ),
}

LOGGER = logging.getLogger("gift-exchange")


def _configure_logging() -> None:
    """Make the audit trail actually reach the container log.

    `logging.basicConfig` only runs in the `__main__` branch below, so under
    `uvicorn main:app` (the image's CMD) this logger had no handler: every INFO line —
    the startup warning, the auth failures, the draw/reset audit — was silently dropped
    by the root logger's last-resort WARNING handler.
    """
    if not LOGGER.handlers:
        handler = logging.StreamHandler()              # stderr, like uvicorn's own lines
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False                           # no duplicates if a root handler exists


_configure_logging()

SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    avoid_same_group INTEGER NOT NULL DEFAULT 1,
    email_language TEXT NOT NULL DEFAULT 'fr',
    auto_email_on_draw INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    drawn_at TEXT DEFAULT NULL
);

-- The first draw of a fresh install is seeded after the migrations (see _ensure_default_draw), not
-- here: on an upgraded database these columns may not exist yet when this script runs.

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draw_id INTEGER NOT NULL DEFAULT 1 REFERENCES draws(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL,
    group_key TEXT NOT NULL DEFAULT '',
    wishlist TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    viewed_at TEXT DEFAULT NULL,
    email_sent_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    FOREIGN KEY (giver_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES participants(id) ON DELETE CASCADE,
    UNIQUE(giver_id, receiver_id)
);
"""

# Columns added after the first release. A fresh database gets them from SCHEMA above; an existing
# one gets them here, and `group_key` inherits what `family_group` used to mean so a roster built
# before this existed keeps drawing exactly as it did. See "Grouping" in PLAN.md for why the two
# fields are separate now: a surname is not a household.
# The rules that belong to one draw. Named once because three things have to agree on the list:
# the API payload (_config_dict), the column migrations below, and the rebuild that turns the old
# single-event `config` table into `draws`.
DRAW_RULE_COLUMNS = (
    "event_title", "adult_draws_adult", "adult_draws_child", "child_draws_adult",
    "child_draws_child", "budget_note", "avoid_same_group", "email_language",
    "auto_email_on_draw", "is_locked",
)

MIGRATIONS = (
    ("participants", "group_key", "ALTER TABLE participants ADD COLUMN group_key TEXT NOT NULL DEFAULT ''",
     "UPDATE participants SET group_key = family_group WHERE group_key = ''"),
    ("draws", "avoid_same_group",
     "ALTER TABLE draws ADD COLUMN avoid_same_group INTEGER NOT NULL DEFAULT 1", None),
    # The email round: an address and a delivery stamp per participant, a language and an
    # auto-send switch per draw. No backfill for `email` on purpose — an address nobody typed
    # is nobody's address, and guessing one is how family feuds start.
    ("participants", "email", "ALTER TABLE participants ADD COLUMN email TEXT NOT NULL DEFAULT ''", None),
    ("participants", "email_sent_at",
     "ALTER TABLE participants ADD COLUMN email_sent_at TEXT DEFAULT NULL", None),
    ("draws", "email_language",
     "ALTER TABLE draws ADD COLUMN email_language TEXT NOT NULL DEFAULT 'fr'", None),
    ("draws", "auto_email_on_draw",
     "ALTER TABLE draws ADD COLUMN auto_email_on_draw INTEGER NOT NULL DEFAULT 0", None),
    # The multi-draw round. `draw_id` carries DEFAULT 1, so every participant that already exists
    # lands on the draw it was always part of; there is nothing to guess.
    ("participants", "draw_id", "ALTER TABLE participants ADD COLUMN draw_id INTEGER NOT NULL DEFAULT 1", None),
    ("draws", "created_at", "ALTER TABLE draws ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
     "UPDATE draws SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') WHERE created_at = ''"),
    # NULL on a draw that was already locked before this column existed: the console then says
    # "verrouillé" without inventing a date it never recorded.
    ("draws", "drawn_at", "ALTER TABLE draws ADD COLUMN drawn_at TEXT DEFAULT NULL", None),
)


def migrate_draws_table(conn: sqlite3.Connection) -> list[str]:
    """`config` (one row, one event) becomes `draws` (one row per event).

    A rebuild, not an `ALTER TABLE ... RENAME`, and the reason is a constraint nobody would think to
    look for: the original table was `id INTEGER PRIMARY KEY CHECK (id = 1)`. Renamed, that CHECK
    comes along for the ride, and creating a second draw — the entire point of this change — would
    fail with a constraint error on a live database. Rebuilding also lets any vintage of legacy
    table be copied column by column (`avoid_same_group` may not exist yet), and leaves the two
    newest columns to apply_migrations below, which is where their backfill lives.

    Ids survive, so `participants.draw_id = 1` is true by construction for a live deployment and a
    family whose links are already in their inbox notices nothing except a new screen.
    """
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "config" not in tables or "draws" in tables:
        return []

    legacy = _columns(conn, "config")
    keep = [column for column in DRAW_RULE_COLUMNS if column in legacy]
    conn.execute(
        """
        CREATE TABLE draws_new (
            id INTEGER PRIMARY KEY,
            event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
            adult_draws_adult INTEGER NOT NULL DEFAULT 1,
            adult_draws_child INTEGER NOT NULL DEFAULT 2,
            child_draws_adult INTEGER NOT NULL DEFAULT 0,
            child_draws_child INTEGER NOT NULL DEFAULT 0,
            budget_note TEXT NOT NULL DEFAULT '',
            avoid_same_group INTEGER NOT NULL DEFAULT 1,
            email_language TEXT NOT NULL DEFAULT 'fr',
            auto_email_on_draw INTEGER NOT NULL DEFAULT 0,
            is_locked INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        f"INSERT INTO draws_new (id, {', '.join(keep)}) "
        f"SELECT id, {', '.join(keep)} FROM config"
    )
    conn.execute("DROP TABLE config")
    conn.execute("ALTER TABLE draws_new RENAME TO draws")
    return ["config -> draws"]


def _ensure_default_draw(conn: sqlite3.Connection) -> None:
    """The first draw of a fresh install, after the migrations have given the table its columns.

    The timestamp is bound from utc_now() rather than strftime() in SQL, so a seeded draw and a
    draw created by the API carry the same format — the list screen sorts and prints these.
    """
    conn.execute("INSERT OR IGNORE INTO draws (id, created_at) VALUES (1, ?)", (utc_now(),))


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """Indexes that depend on migrated columns have to be created after the migrations."""
    conn.execute("CREATE INDEX IF NOT EXISTS participants_draw_id ON participants(draw_id)")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Idempotent column additions. Returns the names of what was actually added."""
    applied: list[str] = []
    for table, column, ddl, backfill in MIGRATIONS:
        if column in _columns(conn, table):
            continue
        conn.execute(ddl)
        if backfill:
            conn.execute(backfill)
        applied.append(f"{table}.{column}")
    return applied


def db_path() -> Path:
    """Database location, resolved per call so DB_PATH can be changed in tests."""
    return Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "exchange.db")))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; multi-statement writes use explicit BEGIN.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        renamed = migrate_draws_table(conn)
        conn.executescript(SCHEMA)
        applied = apply_migrations(conn)
        _ensure_default_draw(conn)
        _ensure_indexes(conn)
    finally:
        conn.close()
    if renamed:
        audit("schema_migrated", tables=renamed)
    if applied:
        audit("schema_migrated", columns=applied)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(event: str, **fields: Any) -> None:
    """One line per security-relevant event. Never logs passwords or tokens."""
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    LOGGER.info("%s %s", event, details)


# A participant's URL is the only credential they have: whoever holds it holds the draw.
TOKEN_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def redacted_path(request: Request) -> str:
    """Path for the audit trail, with any participant token replaced by `<token>`.

    `GET /api/draw/<uuid>` is the one route whose *path* is a secret. Logging it verbatim wrote
    every family member's private link into the container log — which on a real host means the
    journal, next to the same permissions that can already read the database. Redacting keeps the
    useful part (which route, which status, how slow) and drops the credential.
    """
    return "/".join("<token>" if TOKEN_SEGMENT.match(part) else part
                    for part in request.url.path.split("/"))


def _loopback_host(host: str) -> bool:
    """A host only the machine itself can open: localhost, 127.x, ::1, 0.0.0.0.

    The port comes off first (`127.0.0.1:8000`, `[::1]:8000`), and only when what follows the last
    colon is a port — an IPv6 address has more than one colon and no port at all.
    """
    name = host
    if ":" in name:
        head, _, tail = name.rpartition(":")
        if tail.isdigit():
            name = head
    name = name.strip("[]").lower()
    return name in {"localhost", "0.0.0.0", "::1", "::"} or name.startswith("127.")


def mail_base_url(request: Request) -> str:
    """The origin to build *mailed* links from, or "" when nothing reachable is known.

    The Host header is a shakier answer than it looks: an admin call made from inside the container,
    a health check, or a port-forward carries a host nobody outside can open — `Host:
    127.0.0.1:8000` mailed the family an invitation to visit the server's own machine, which is what
    happened to the first test sends here (and blank art in Gmail was the visible symptom of it, on
    top of the SVG problem). `PUBLIC_BASE_URL` states the truth once, in one place; without it a
    loopback host is refused rather than mailed, because a mail whose every link is dead on arrival is
    worse than an error on the organizer's screen.
    """
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    base_url = public_base_url(request)
    if not base_url:
        return ""
    if _loopback_host(base_url.partition("://")[2]):
        audit("mail_base_url_refused", host=base_url[:80])
        return ""
    return base_url


def participant_link(base_url: str, token: str) -> str:
    """The participant's own URL, built in one place because it is a credential *and* a route.

    The SPA reads its token from the query string (`/?t=<uuid>`), and that is what the console's
    « Copier le lien » hands out. An email that instead reuses the API's path shape — `/draw/<uuid>`
    — lands on a route the server does not serve, and the family reads `{"detail":"Not Found"}`.
    That happened, twelve inboxes, on the first real send; this function is the seam that keeps the
    console, the mail template and the sample preview saying the same URL.
    """
    return f"{base_url}/?t={token}"


def client_ip(request: Request) -> str:
    """Client address. X-Forwarded-For is only honoured behind a trusted proxy."""
    if trust_proxy():
        header = request.headers.get("x-forwarded-for", "")
        if header:
            return header.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed-window per-client counters, keyed by scope so one endpoint cannot
    exhaust another's budget."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], tuple[int, int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, scope: str, key: str, limit: int, window: int = 60) -> tuple[bool, int]:
        now = int(time.time())
        bucket = now // window
        identity = (scope, key)
        with self._lock:
            seen_bucket, count, blocked_until = self._hits.get(identity, (bucket, 0, 0))
            if seen_bucket != bucket:
                seen_bucket, count, blocked_until = bucket, 0, 0
            if blocked_until > now:
                return False, blocked_until - now
            count += 1
            self._hits[identity] = (seen_bucket, count, blocked_until)
            if limit > 0 and count > limit:
                return False, (seen_bucket + 1) * window - now
        return True, 0

    def count(self, scope: str, key: str, window: int = 60) -> int:
        now = int(time.time())
        with self._lock:
            seen_bucket, count, _blocked = self._hits.get((scope, key), (now // window, 0, 0))
            return count if seen_bucket == now // window else 0

    def block(self, scope: str, key: str, seconds: int) -> None:
        with self._lock:
            bucket, count, _ = self._hits.get((scope, key), (int(time.time()) // 60, 0, 0))
            self._hits[(scope, key)] = (bucket, count, int(time.time()) + seconds)

    def reset(self, scope: str, key: str) -> None:
        with self._lock:
            self._hits.pop((scope, key), None)

    def prune(self) -> None:
        cutoff = int(time.time()) - 300
        with self._lock:
            for identity, (bucket, _count, blocked) in list(self._hits.items()):
                if bucket * 60 < cutoff and blocked < int(time.time()):
                    self._hits.pop(identity, None)


LIMITER = RateLimiter()
_draw_lock = threading.Lock()


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many requests. Please wait before trying again.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


def json_error(content: dict, status_code: int) -> JSONResponse:
    """Plain JSON error outside the routing path (middleware), with security headers."""
    return JSONResponse(content, status_code=status_code, headers=dict(SECURITY_HEADERS))


def enforce_api_limit(request: Request) -> None:
    allowed, retry = LIMITER.hit("api", client_ip(request), API_LIMIT)
    if not allowed:
        raise _too_many(retry)


def enforce_draw_limit(request: Request) -> None:
    allowed, retry = LIMITER.hit("draw", client_ip(request), DRAW_LIMIT)
    if not allowed:
        raise _too_many(retry)


# --- signed, revocable session tokens -------------------------------------- #


def _session_secret() -> bytes:
    explicit = os.environ.get("SESSION_SECRET")
    if explicit:
        return explicit.encode()
    salt = os.environ.get("DB_PATH", str(db_path())).encode()
    return hashlib.pbkdf2_hmac("sha256", admin_password().encode(), salt, 50_000)


_sessions: dict[str, int] = {}          # token -> expiry (epoch seconds)
_sessions_lock = threading.Lock()


def issue_session() -> tuple[str, int]:
    payload = {"i": secrets.token_urlsafe(12), "e": int(time.time()) + SESSION_TTL_SECONDS}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    signature = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()
    token = f"{body}.{signature}"
    with _sessions_lock:
        _sessions[token] = payload["e"]
    return token, SESSION_TTL_SECONDS


def valid_session(token: str) -> bool:
    body, _, signature = token.partition(".")
    if not body or not signature:
        return False
    expected = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
        expires = int(payload["e"])
    except Exception:
        return False
    if expires < int(time.time()):
        with _sessions_lock:
            _sessions.pop(token, None)
        return False
    with _sessions_lock:
        stored = _sessions.get(token)          # revoked by logout / cleared by restart
        if stored is None or stored < int(time.time()):
            _sessions.pop(token, None)
            return False
        _sessions[token] = expires
    return True


def revoke_session(token: str) -> bool:
    with _sessions_lock:
        return _sessions.pop(token, None) is not None


def require_admin(
    request: Request,
    x_admin_password: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Accepts the spec's X-Admin-Password header, or a bearer session token."""
    password = admin_password()
    if not password:
        # Nothing is configured to authenticate against. Fail closed: no header and no
        # bearer token, because with a blank password the signing key is derivable from
        # public defaults (PBKDF2 over "" with the DB path as salt).
        audit("admin_auth_refused_unconfigured", ip=client_ip(request))
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured on the server")
    supplied_password = x_admin_password is not None and hmac.compare_digest(
        str(x_admin_password), password
    )
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if supplied_password or (bearer and valid_session(bearer)):
        return
    audit("admin_auth_denied", ip=client_ip(request))
    raise HTTPException(status_code=401, detail="Unauthorized")


def current_bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


# --------------------------------------------------------------------------- #
# Matching engine
# --------------------------------------------------------------------------- #


def norm_group(value: str) -> str:
    """Case/whitespace-insensitive key for a grouping label."""
    return (value or "").strip().lower()


def group_key_of(participant: dict[str, Any], respect_groups: bool) -> str:
    """The key the matching engine treats as "same household".

    Deliberately **not** the family name: two households can carry the same surname, and a surname is
    then useless as the unit of the rule. `group_key` is what the rule works on; `family_group` is
    only ever a label for the organizer.

    An empty `group_key` means "in no group", and with `respect_groups` off everyone gets a private
    key. Both cases make the exclusion a no-op without a second code path — and the private key is
    also what stops a participant being dealt to themselves once group exclusion is switched off.
    """
    key = norm_group(participant.get("group_key", "")) if respect_groups else ""
    return key or f"#solo:{participant['id']}"


def _balanced_pool(
    members: list[dict[str, Any]], total_slots: int, rng: random.Random
) -> list[int]:
    """Give the first `rem` members one extra incoming slot, then flatten.

    Mirrors section 4.1 "Balanced Receiver Pool Construction" exactly.
    """
    if total_slots <= 0 or not members:
        return []
    base, remainder = divmod(total_slots, len(members))
    order = list(members)
    rng.shuffle(order)
    pool: list[int] = []
    for index, member in enumerate(order):
        pool.extend([int(member["id"])] * (base + (1 if index < remainder else 0)))
    return pool


class _Slots:
    """Receiver slots still to be handed out: pid -> how many gifts it still owes."""

    def __init__(
        self,
        adult_slots: list[int],
        child_slots: list[int],
        group_of: dict[int, str],
        role_of: dict[int, str],
    ):
        self.count: dict[int, int] = {}
        self.group_of = group_of
        self.role_of = role_of
        self.by_group: dict[tuple[str, str], list[int]] = {}
        for pid in list(adult_slots) + list(child_slots):
            self.count[pid] = self.count.get(pid, 0) + 1
        for pid in self.count:
            self._list(pid).append(pid)

    def _list(self, pid: int) -> list[int]:
        return self.by_group.setdefault((self.role_of[pid], self.group_of[pid]), [])

    def available(self, pid: int) -> bool:
        return self.count.get(pid, 0) > 0

    def eligible(self, role: str, group: str, allow_same: bool = False) -> list[int]:
        """Slots a giver in `group` may take — outsiders first, always.

        `allow_same` only appends the giver's own group *after* every outside option, so a relaxed
        draw stays as relaxed as the budget allows instead of scattering crossings through it.
        """
        outside: list[int] = []
        inside: list[int] = []
        for (slot_role, slot_group), pids in self.by_group.items():
            if slot_role != role or not pids:
                continue
            (inside if slot_group == group else outside).extend(pids)
        return outside + inside if allow_same else outside

    def dec(self, pid: int) -> None:
        self.count[pid] -= 1
        if self.count[pid] == 0:
            self._list(pid).remove(pid)

    def inc(self, pid: int) -> None:
        if self.count[pid] == 0:
            self._list(pid).append(pid)
        self.count[pid] += 1

    def total(self) -> int:
        return sum(self.count.values())


def _feasible(
    givers: list[dict[str, Any]],
    quota: dict[str, tuple[int, int]],
    adults: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> bool:
    """Cheap necessary conditions on the **hard** constraints only.

    False means "stop now"; True does not prove a solution exists. Same-group exclusions are not part
    of this on purpose: they are a soft constraint, so a giver boxed in by them is not unsolvable —
    it only costs a crossing. What stays here is the arithmetic that cannot be negotiated: a giver
    needs that many distinct receivers, and nobody can receive their own gift.
    """
    for giver in givers:
        need_adult, need_child = quota[giver["role"]]
        available_adults = len(adults) - (1 if giver["role"] == "ADULT" else 0)
        available_children = len(children) - (1 if giver["role"] == "CHILD" else 0)
        if need_adult > available_adults or need_child > available_children:
            return False
    return True


def _min_crossings(
    givers: list[dict[str, Any]],
    quota: dict[str, tuple[int, int]],
    adults: list[dict[str, Any]],
    children: list[dict[str, Any]],
    group_of: dict[int, str],
    t_adult: int,
    t_child: int,
) -> int:
    """Lower bound on how many gifts must cross a group.

    Receivers of one role are owed `base` or `base+1` gifts each, so at least `base * size` of the
    slots for that role sit inside any group of `size`, and those can only be filled by givers
    outside it. Symmetrically, givers inside that group want `demand_inside` gifts and at most
    `total - base * size` of them can come from outside. Whichever side comes up short, the
    difference has to be paid with a same-group gift.

    Only a bound, and a loose one. The search starts here so a roster that provably needs k crossings
    does not spend its whole attempt budget trying k-1 impossible configurations.
    """
    total = 0
    for members, index, slots_total in ((adults, 0, t_adult), (children, 1, t_child)):
        if slots_total <= 0 or not members:
            continue
        base, _remainder = divmod(slots_total, len(members))
        sizes: dict[str, int] = {}
        for member in members:
            key = group_of[int(member["id"])]
            sizes[key] = sizes.get(key, 0) + 1
        demand: dict[str, int] = {}
        demand_total = 0
        for giver in givers:
            key = group_of[int(giver["id"])]
            demand[key] = demand.get(key, 0) + quota[giver["role"]][index]
            demand_total += quota[giver["role"]][index]
        for key, size in sizes.items():
            inside_slots = base * size
            want_inside = demand.get(key, 0)
            receivers_short = max(0, want_inside - (slots_total - inside_slots))
            givers_short = max(0, inside_slots - (demand_total - want_inside))
            total += max(receivers_short, givers_short)
    return total


def _solve_attempt(
    order: list[dict[str, Any]],
    quota: dict[str, tuple[int, int]],
    slots: _Slots,
    rng: random.Random,
    max_nodes: int = 50000,
    same_budget: int = 0,
) -> Optional[list[tuple[int, int]]]:
    """Depth-first deal of every giver's slots, backtracking when boxed in.

    `same_budget` is how many gifts may land inside the giver's own group; 0 is the strict rule.
    Returns the assignment pairs, or None when this attempt (including its backtracking budget)
    failed.
    """
    pairs: list[tuple[int, int]] = []
    nodes = {"used": 0}
    same_used = {"n": 0}
    allow_same = same_budget > 0

    def assign(index: int) -> bool:
        """Deal for every giver from `index` on; leaves `slots` intact on failure."""
        nodes["used"] += 1
        if nodes["used"] > max_nodes:
            return False
        if index == len(order):
            return True

        giver = order[index]
        giver_id = int(giver["id"])
        need_adult, need_child = quota[giver["role"]]
        group = slots.group_of[giver_id]
        chosen: list[int] = []
        used: set[int] = set()

        def take(remaining: int, role: str, then) -> bool:
            """Give this giver `remaining` receivers of `role`, then continue with `then`."""
            if nodes["used"] > max_nodes:
                return False
            if remaining == 0:
                return then()
            candidates = slots.eligible(role, group, allow_same)
            rng.shuffle(candidates)
            for pid in candidates:
                if not slots.available(pid) or pid in used or pid == giver_id:
                    continue
                crosses = slots.group_of[pid] == group
                if crosses and same_used["n"] >= same_budget:
                    continue
                used.add(pid)
                slots.dec(pid)
                if crosses:
                    same_used["n"] += 1
                chosen.append(pid)
                nodes["used"] += 1
                if take(remaining - 1, role, then):
                    return True
                chosen.pop()
                if crosses:
                    same_used["n"] -= 1
                slots.inc(pid)
                used.discard(pid)
            return False

        def place() -> bool:
            start = len(pairs)
            pairs.extend((giver_id, pid) for pid in chosen)
            if assign(index + 1):
                return True
            del pairs[start:]
            return False

        def take_child() -> bool:
            """Zero-arg on purpose: `then` callbacks are always called with no arguments."""
            return take(need_child, "CHILD", place)

        return take(need_adult, "ADULT", take_child)

    if assign(0) and slots.total() == 0:
        return pairs
    return None


def generate_draw(
    config: dict[str, Any],
    participants: list[dict[str, Any]],
    max_attempts: int = MAX_ATTEMPTS,
    seed: Optional[int] = None,
) -> tuple[list[tuple[int, int]], int]:
    """Produce ((giver_id, receiver_id) pairs, same-group pairs used).

    `avoid_same_group` is a preference, not an axiom: the solver spends its budget on a draw with no
    crossings, and only when none exists does it allow crossings — starting at a proven lower bound
    and going up one at a time, so the relaxation is as small as it can be. With the preference off,
    groups are not consulted at all.

    Raises HTTPException(400) for an empty target group and HTTPException(422) when the draw counts
    themselves cannot be satisfied at all.
    """
    adult_draws_adult = int(config["adult_draws_adult"])
    adult_draws_child = int(config["adult_draws_child"])
    child_draws_adult = int(config["child_draws_adult"])
    child_draws_child = int(config["child_draws_child"])
    respect_groups = bool(int(config.get("avoid_same_group", 1)))

    adults = [p for p in participants if p["role"] == "ADULT"]
    children = [p for p in participants if p["role"] == "CHILD"]
    num_adults, num_children = len(adults), len(children)

    t_adult = num_adults * adult_draws_adult + num_children * child_draws_adult
    t_child = num_adults * adult_draws_child + num_children * child_draws_child

    if (t_adult > 0 and num_adults == 0) or (t_child > 0 and num_children == 0):
        raise HTTPException(status_code=400, detail=EMPTY_TARGET_GROUP)

    rng = random.Random(seed)

    adult_pool = _balanced_pool(adults, t_adult, rng)
    child_pool = _balanced_pool(children, t_child, rng)
    if not adult_pool and not child_pool:
        return [], 0

    group_of = {int(p["id"]): group_key_of(p, respect_groups) for p in participants}
    role_of = {int(p["id"]): p["role"] for p in participants}

    quota = {
        "ADULT": (adult_draws_adult, adult_draws_child),
        "CHILD": (child_draws_adult, child_draws_child),
    }
    givers = [p for p in participants if sum(quota[p["role"]]) > 0]

    if not _feasible(givers, quota, adults, children):
        raise HTTPException(status_code=422, detail=UNSOLVABLE)

    def tightness(giver: dict[str, Any]) -> int:
        """Lower = fewer options, so it is dealt first."""
        group = group_of[int(giver["id"])]
        available_adults = sum(1 for p in adults if group_of[int(p["id"])] != group)
        available_children = sum(1 for p in children if group_of[int(p["id"])] != group)
        need_adult, need_child = quota[giver["role"]]
        return min(need_adult + need_child, available_adults + available_children)

    total_slots = len(adult_pool) + len(child_pool)
    if respect_groups:
        lower = _min_crossings(givers, quota, adults, children, group_of, t_adult, t_child)
        budgets = list(range(lower, total_slots + 1))
    else:
        budgets = [0]

    for budget in budgets:
        # A boxed-in roster is worth more attempts than a wide-open one, but the budget ladder must
        # not multiply the worst case: the strict pass keeps the full budget, a relaxation a slice.
        attempts = max(1, max_attempts) if budget == 0 else max(8, max_attempts // 10)
        for _ in range(attempts):
            # most-constrained givers first; ties re-shuffled so attempts differ
            order = sorted(givers, key=lambda g: (tightness(g), rng.random()))
            slots = _Slots(adult_pool, child_pool, group_of, role_of)
            pairs = _solve_attempt(order, quota, slots, rng, same_budget=budget)
            if pairs is not None:
                # Counted from the deal, not from the budget: what the console reports is what the
                # draw actually did, so the number always agrees with the assignments on disk.
                crossed = sum(1 for giver, receiver in pairs if group_of[giver] == group_of[receiver])
                return pairs, crossed

    raise HTTPException(status_code=422, detail=UNSOLVABLE)

# --------------------------------------------------------------------------- #
# Email delivery: transport (Resend) + themed FR/EN templates
# --------------------------------------------------------------------------- #


class EmailSendError(Exception):
    """One failed send. `status` is Resend's HTTP code; 0 means network failure or the feature
    being unconfigured, which the caller must not paper over with a generic 500."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def send_email(*, to: str, subject: str, html_body: str, text_body: str, ref_id: str) -> str:
    """POST /emails on Resend; returns the message id ("" when the response carried none).

    Stdlib urllib on purpose: the image ships fastapi+uvicorn only, and a send is one POST.
    The API key goes into a header and nowhere else — not into a log line, not into an error
    message, not into a response.
    """
    key, sender = resend_api_key(), resend_from()
    if not key or not sender:
        raise EmailSendError(0, EMAIL_NOT_CONFIGURED)

    payload = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
        # Lets the receiver's mail client thread repeats of the same link together; keyed by
        # participant id, so a resend after a reset stays recognizably the same thread.
        "headers": {"X-Entity-Ref-ID": ref_id},
    }
    body: dict = {}
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(
            resend_base_url() + "/emails",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                # See EMAIL_USER_AGENT: Cloudflare 403/1010s the default Python-urllib agent.
                "User-Agent": EMAIL_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=EMAIL_TIMEOUT) as response:
                body = json.loads(response.read() or b"{}")
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            try:
                detail = json.loads(detail).get("message") or detail
            except (ValueError, AttributeError):
                pass
            # One polite retry on 429 — the bulk throttle should make this rare, but a manual
            # "Envoyer" pressed twice can still brush the provider's ceiling.
            if error.code == 429 and attempt == 1:
                audit("email_provider_throttled", retry_in=email_retry_after_s())
                time.sleep(email_retry_after_s())
                continue
            raise EmailSendError(error.code, detail or error.reason) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", None) or error.__class__.__name__
            raise EmailSendError(0, str(reason)) from None
    return str(body.get("id") or "")


# The mail follows the site: pine header, ivory card, gold rules, Fraunces/Inter with the same
# serif/sans fallbacks the CSS uses (mail clients rarely have the webfonts, the fallbacks are the
# plan, not the excuse). Inline styles only — that is the only CSS email clients agree on.
# Copy rule: plain Quebec French, the words the interface already uses (« liste d'idées »,
# « qui gâtes », « jamais dans la même famille »), never the caricature version.
EMAIL_STRINGS = {
    "fr": {
        "kicker": "Tirage de Noël · Père Noël secret",
        "title_ready": "Ton lien secret est prêt",
        "title_pending": "Ton lien pour le tirage de Noël",
        "hello": "Salut {name},",
        "body_ready": ("Le tirage est fait. Ouvre ton lien pour voir qui tu gâtes. "
                       "Personne d’autre ne le voit, pas même l’organisateur."),
        "body_pending": ("Le tirage n’a pas encore été fait. Ton lien fonctionne déjà : ajoute ta "
                         "liste d’idées, et tu verras qui tu gâtes dès que le tirage sera lancé."),
        "button": "Ouvrir mon lien",
        "link_note": "Si le bouton ne s’affiche pas, copie ce lien dans ton navigateur :",
        "privacy": "Garde ton lien pour toi : celui qui le tient voit ton tirage.",
        # Grammar pieces, so a list of names reads as a sentence instead of a comma pile.
        "and": "et",
        "and_more": "et {n} autre(s)",
        "footer": "❄ Échange de cadeaux · tirage automatique, jamais dans la même famille",
        "subject_ready": "{name} — ton lien secret · {event}",
        "subject_pending": "{name} — ton lien pour l’échange de cadeaux · {event}",
        # One mail per mailbox, not per person: at a family event the kids’ links all go to one
        # parent, and six near-identical mails arriving in one inbox reads like abuse. So the plural
        # set exists — the singular copy (“Salut X”, “ton lien”) cannot be stretched to three people
        # without reading like a bug.
        "hello_many": "Salut {names},",
        "title_many_ready": "Vos liens secrets sont prêts",
        "title_many_pending": "Vos liens pour le tirage de Noël",
        "body_many_ready": ("Vous êtes {n} à recevoir ce courriel, et chacun a son propre lien. "
                            "Chaque lien ne montre que le tirage de la personne qu’il nomme — "
                            "personne d’autre ne peut le voir, pas même l’organisateur."),
        "body_many_pending": ("Le tirage n’a pas encore été fait. Chacun ici a déjà son lien : "
                              "ajoutez vos listes d’idées, et vous verrez qui vous gâtez dès que "
                              "le tirage sera lancé."),
        "links_note": "Un lien par personne — chaque bouton ouvre celle qui est écrite dessus :",
        "link_button": "Voir le lien de {name}",
        "link_label": "Lien de {name} :",
        "privacy_many": "Gardez ces liens dans la famille : celui qui les tient voit les tirages "
                        "des personnes qu’ils nomment.",
        "subject_many": "{names} — vos liens · {event}",
        "subject_many_pending": "{names} — vos liens pour l’échange de cadeaux · {event}",
        # The hero art is not decoration for a screen reader: it is the whole idea of the mail
        # (le résultat est dans la boîte, et toi seul peux l’ouvrir), so it gets described.
        "gift_alt": "Un cadeau bien fermé : le couvercle a sauté et quelqu’un à l’intérieur "
                    "pousse pour sortir.",
    },
    "en": {
        "kicker": "Secret Santa draw · Family gift exchange",
        "title_ready": "Your secret link is ready",
        "title_pending": "Your link for the gift exchange",
        "hello": "Hi {name},",
        "body_ready": ("The draw is done. Open your link to see who you are shopping for. "
                       "Nobody else can see it — not even the organizer."),
        "body_pending": ("The draw has not happened yet. Your link already works: add your "
                         "wishlist, and you will see who you are shopping for as soon as the "
                         "organizer runs the draw."),
        "button": "Open my link",
        "link_note": "If the button does not show up, copy this link into your browser:",
        "privacy": "Keep your link to yourself: whoever holds it sees your draw.",
        "and": "and",
        "and_more": "and {n} other(s)",
        "footer": "❄ Gift exchange · automatic draw, never within the same household",
        "subject_ready": "{name} — your secret link · {event}",
        "subject_pending": "{name} — your link for the gift exchange · {event}",
        "hello_many": "Hi {names},",
        "title_many_ready": "Your secret links are ready",
        "title_many_pending": "Your links for the gift exchange",
        "body_many_ready": ("There are {n} of you at this address, and each person has their own "
                            "link. Each link shows only the draw of the person named on it — "
                            "nobody else can see it, not even the organizer."),
        "body_many_pending": ("The draw has not happened yet. Everyone here already has their own "
                              "link: add your wishlists, and you will see who you are shopping for "
                              "as soon as the organizer runs the draw."),
        "links_note": "One link per person — each button opens the person written on it:",
        "link_button": "Open {name}’s link",
        "link_label": "{name}’s link:",
        "privacy_many": "Keep these links inside the family: whoever holds them sees the draws of "
                        "the people named on them.",
        "subject_many": "{names} — your links · {event}",
        "subject_many_pending": "{names} — your links for the gift exchange · {event}",
        "gift_alt": "A tightly closed gift: the lid has popped off and somebody inside is "
                    "pushing to get out.",
    },
}

# Site palette, repeated here because inline email styles cannot read the SPA's Tailwind config.
_PINE_DARK, _PINE, _PINE_SOFT, _GOLD, _GOLD_SOFT, _IVORY = (
    "#143c2e", "#2f6b52", "#b3d0c2", "#d4af62", "#e6c98a", "#faf7f2")
# Sample names for the preview: never a real participant, and enough of them to show the household
# template with its links labelled.
PREVIEW_NAMES = ("Aline", "Gabrielle", "Hugo")

_EMAIL_SANS = "Inter,'Segoe UI',Helvetica,Arial,sans-serif"
_EMAIL_SERIF = "Fraunces,Georgia,'Times New Roman',serif"
# One button style, because the singular and the household template must not drift apart — a mail
# whose second link looks disabled is read as "this one is broken".
_BUTTON = (f"display:inline-block;background:{_PINE_DARK};color:#ffffff;text-decoration:none;"
           "padding:12px 22px;border-radius:999px;font-weight:600;font-size:15px")

# Names a subject line is allowed to carry before it stops doing its job: a phone's list view cuts
# around 70 characters, and the point of the subject is to be scanned, not read.
MAX_SUBJECT_NAMES = 3


def _email_copy(language: str) -> dict[str, str]:
    """The FR/EN string table, French for anything unrecognized."""
    return EMAIL_STRINGS.get((language or "fr").strip().lower(), EMAIL_STRINGS["fr"])


def _join_names(names: list[str], copy: dict[str, str]) -> str:
    """"Alec, Gabrielle et Hugo" — and a cut for a big household, because the subject is what a
    family scans on a phone and an unbounded list pushes the words « vos liens » off the screen."""
    if len(names) == 1:
        return names[0]
    if len(names) > MAX_SUBJECT_NAMES:
        rest = len(names) - MAX_SUBJECT_NAMES
        return ", ".join(names[:MAX_SUBJECT_NAMES]) + " " + copy["and_more"].format(n=rest)
    return ", ".join(names[:-1]) + f" {copy['and']} " + names[-1]


def _email_assets(assets_base: str, copy: dict[str, str]) -> dict[str, str]:
    """The three `<img>` references, or three empty strings when there is no absolute base.

    **PNG, not SVG, and that is not a preference.** The mail fetched its art from `/static` as SVG,
    the files served fine (`200`, `image/svg+xml`), and Gmail showed nothing at all: Google's image
    proxy does not serve SVG, which it has said publicly it has no plans to change. Measured in a
    real inbox on 2026-11-13, after a README that confidently claimed the opposite. The SVGs stay
    the source art and the site's own vectors; `tools/build_email_art.py` rasterizes the copies the
    mail loads, at 2x the displayed size so a phone shows them sharp.

    Inline `<svg>` is stripped by Gmail, so the art is `<img>`-referenced, never inline. A relative
    src in an email is a broken image with extra steps, so no base means no art. Static on purpose:
    Outlook ignores `@keyframes` and Gmail strips them, so art that freezes in half the family's
    inboxes is worse than no art.
    """
    base = assets_base.rstrip("/")
    if not base:
        return {"flake": "", "gift": "", "strip": ""}
    flake_url = html.escape(f"{base}/static/email-flake.png", quote=True)
    gift_url = html.escape(f"{base}/static/email-gift.png", quote=True)
    strip_url = html.escape(f"{base}/static/email-gift-strip.png", quote=True)
    alt = html.escape(copy["gift_alt"], quote=True)
    gift = (f'<div style="padding:26px 28px 2px;text-align:center"><img src="{gift_url}" width="330" '
            f'height="190" alt="{alt}" style="display:block;width:100%;max-width:330px;margin:0 auto">'
            f'</div>')
    strip = (f'<div style="padding:4px 28px 20px"><img src="{strip_url}" width="504" height="36" alt="" '
             f'style="display:block;width:100%;max-width:504px"></div>')
    flake = f'<img src="{flake_url}" width="56" height="56" alt="" style="display:block">'
    return {"flake": flake, "gift": gift, "strip": strip}


def _email_shell(*, copy: dict[str, str], title: str, heading: str, subject: str,
                 assets_base: str, privacy: str, content_html: str,
                 text_lines: list[str]) -> dict[str, str]:
    """The chrome both email templates share: pine header, hero gift, ivory card, strip, footer.

    Shared so that a change to the art, the header or the privacy line lands in both the single-link
    and the household mail at once — two templates that drift are two templates where one of them
    quietly stops saying "your link is secret".
    """
    art = _email_assets(assets_base, copy)
    header_text = f"""\
<div style="font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:{_GOLD}">\
{html.escape(copy['kicker'])}</div>
      <div style="font-family:{_EMAIL_SERIF};font-size:26px;line-height:1.25;margin-top:8px">\
{html.escape(heading)}</div>
      <div style="font-size:13px;color:{_PINE_SOFT};margin-top:6px">{html.escape(title)}</div>"""

    html_body = f"""\
<div style="background:#eef5f1;padding:28px 16px;font-family:{_EMAIL_SANS}">
  <div style="max-width:560px;margin:0 auto;background:{_IVORY};border:1px solid #d7e7de;\
border-radius:20px;overflow:hidden">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" \
style="background:{_PINE_DARK}">
      <tr>
        <td style="padding:24px 12px 24px 28px;color:#ffffff">{header_text}</td>
        <td width="84" align="right" valign="top" style="padding:20px 22px 0 0">{art['flake']}</td>
      </tr>
    </table>
    {art['gift']}
    <div style="padding:28px;color:#1f5340;font-size:15px;line-height:1.65">
{content_html}
    </div>
    {art['strip']}
    <div style="padding:16px 28px;border-top:1px solid {_GOLD_SOFT};color:{_PINE};\
font-size:12px;line-height:1.7">{html.escape(privacy)}<br>{html.escape(copy['footer'])}</div>
  </div>
</div>"""

    text_body = (f"{copy['kicker']}\n\n{heading}\n{title}\n\n"
                 + "\n\n".join(text_lines)
                 + f"\n\n{privacy}\n{copy['footer']}\n")
    return {"subject": subject, "html": html_body, "text": text_body}


def build_email(name: str, event_title: str, link: str, language: str, ready: bool,
                assets_base: str = "") -> dict[str, str]:
    """Render the single-link email: one person, one link, one "ton lien".

    The household case (several participants sharing one mailbox) is `build_links_email`, and it has
    its own copy on purpose: "Salut Alec," and "ton lien" cannot be stretched over three people
    without reading like a bug.
    """
    copy = _email_copy(language)
    title = event_title or copy["kicker"]
    # The name comes first, deliberately: a family shares one inbox, and six mails from the same
    # sender with one subject are impossible to tell apart in a phone's list view — which is where
    # the truncation happens anyway. Subject order is the only thing the mailbox sorts by.
    subject = copy["subject_ready" if ready else "subject_pending"].format(event=title, name=name)
    heading = copy["title_ready" if ready else "title_pending"]
    hello = copy["hello"].format(name=name)
    body = copy["body_ready" if ready else "body_pending"]
    link_note = copy["link_note"]
    safe_link = html.escape(link, quote=True)

    content = f"""\
      <p style="margin:0 0 14px">{html.escape(hello)}</p>
      <p style="margin:0 0 24px">{html.escape(body)}</p>
      <p style="margin:0 0 24px">
        <a href="{safe_link}" style="{_BUTTON}">{html.escape(copy['button'])}</a>
      </p>
      <p style="margin:0 0 6px;color:{_PINE};font-size:13px">{html.escape(link_note)}</p>
      <p style="margin:0;font-size:13px;word-break:break-all;color:{_PINE}">\
<a href="{safe_link}" style="color:{_PINE}">{html.escape(link)}</a></p>"""
    return _email_shell(copy=copy, title=title, heading=heading, subject=subject,
                        assets_base=assets_base, privacy=copy["privacy"], content_html=content,
                        text_lines=[hello, body, f"{copy['button']}: {link}",
                                    f"{link_note}\n{link}"])


def build_links_email(people: list[dict[str, str]], event_title: str, language: str, ready: bool,
                      assets_base: str = "") -> dict[str, str]:
    """One email covering several people at one address: each person's link, each one labelled.

    `people` is `[{"name": …, "link": …}, …]` in roster order, so a parent reading it aloud can tell
    which link belongs to which child. One mail instead of N matters twice: the family's mailbox is
    one inbox, and a burst of near-identical mails from one sender is what a spam filter — and a
    tired parent — reads as abuse.

    With a single person this defers to `build_email`, so the address that happens to hold one
    participant still gets the singular, friendlier copy.
    """
    if len(people) == 1:
        only = people[0]
        return build_email(only["name"], event_title, only["link"], language, ready, assets_base)

    copy = _email_copy(language)
    title = event_title or copy["kicker"]
    names = [person["name"] for person in people]
    joined = _join_names(names, copy)
    subject = copy["subject_many" if ready else "subject_many_pending"].format(event=title,
                                                                              names=joined)
    heading = copy["title_many_ready" if ready else "title_many_pending"]
    hello = copy["hello_many"].format(names=joined)
    body = copy["body_many_ready" if ready else "body_many_pending"].format(n=len(people))

    blocks, text_lines = [], [hello, body, copy["links_note"]]
    for person in people:
        safe_link = html.escape(person["link"], quote=True)
        blocks.append(f"""\
      <div style="margin:0 0 18px">
        <p style="margin:0 0 6px;font-size:14px;font-weight:600;color:{_PINE_DARK}">\
{html.escape(person['name'])}</p>
        <p style="margin:0 0 6px">
          <a href="{safe_link}" style="{_BUTTON}">\
{html.escape(copy['link_button'].format(name=person['name']))}</a>
        </p>
        <p style="margin:0;font-size:12px;word-break:break-all;color:{_PINE}">\
<a href="{safe_link}" style="color:{_PINE}">{html.escape(person['link'])}</a></p>
      </div>""")
        text_lines.append(f"{copy['link_label'].format(name=person['name'])}\n{person['link']}")

    content = (f'      <p style="margin:0 0 14px">{html.escape(hello)}</p>\n'
               f'      <p style="margin:0 0 22px">{html.escape(body)}</p>\n'
               f'      <p style="margin:0 0 16px;color:{_PINE};font-size:13px">'
               f'{html.escape(copy["links_note"])}</p>\n' + "\n".join(blocks))
    return _email_shell(copy=copy, title=title, heading=heading, subject=subject,
                        assets_base=assets_base, privacy=copy["privacy_many"],
                        content_html=content, text_lines=text_lines)


def _all_participants(conn: sqlite3.Connection, draw_id: int | None = None) -> list[dict[str, Any]]:
    target = select_draw_id(conn, draw_id)
    return [
        _participant_dict(row)
        for row in conn.execute(
            f"SELECT {PARTICIPANT_COLUMNS} FROM participants WHERE draw_id = ? ORDER BY id",
            (target,),
        )
    ]


def send_link_emails(conn: sqlite3.Connection, participants: list[dict[str, Any]],
                     config_row: sqlite3.Row, base_url: str) -> dict[str, Any]:
    """Mail a set of participants: at most one email per address, all links inside it.

    The report stays one row per participant — that is how the roster is read, including the people
    with no address, so the console can show who was skipped instead of silently omitting an uncle —
    and it adds `messages`, the number of sends that actually went out. Twelve people at five
    mailboxes is five sends, and the console should say so rather than imply twelve.
    """
    results, messages = [], 0
    for group in _group_by_address(participants):
        rows = deliver_household_email(conn, group, config_row["event_title"],
                                       config_row["email_language"], base_url,
                                       ready=bool(config_row["is_locked"]))
        if any(row["ok"] for row in rows):
            messages += 1
        results += rows
    return {
        "sent": sum(1 for row in results if row["ok"]),
        "failed": sum(1 for row in results if not row["ok"]),
        "messages": messages,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


class ConfigUpdate(BaseModel):
    event_title: Optional[str] = Field(default=None, max_length=MAX_EVENT_TITLE)
    budget_note: Optional[str] = Field(default=None, max_length=MAX_BUDGET_NOTE)
    adult_draws_adult: Optional[int] = Field(default=None, le=MAX_DRAW_QUOTA)
    adult_draws_child: Optional[int] = Field(default=None, le=MAX_DRAW_QUOTA)
    child_draws_adult: Optional[int] = Field(default=None, le=MAX_DRAW_QUOTA)
    child_draws_child: Optional[int] = Field(default=None, le=MAX_DRAW_QUOTA)
    # Preference, not an axiom: when on, the solver avoids same-group picks and only uses them if no
    # draw exists otherwise. When off, groups are not consulted at all.
    avoid_same_group: Optional[bool] = None
    # One language for the whole event, not per participant: at a family event the organizer picks
    # once. Unknown values are rejected by the Literal, so the template lookup can never miss.
    email_language: Optional[Literal["fr", "en"]] = None
    # Off by default: an accidental click should not mail the whole family.
    auto_email_on_draw: Optional[bool] = None

    @field_validator("adult_draws_adult", "adult_draws_child", "child_draws_adult", "child_draws_child")
    @classmethod
    def _quota_in_range(cls, value: Optional[int]) -> Optional[int]:
        # The lower bound is checked explicitly: a bare `ge` is dropped on Optional[int].
        if value is not None and not 0 <= value <= MAX_DRAW_QUOTA:
            raise ValueError(f"must be between 0 and {MAX_DRAW_QUOTA}")
        return value


class DrawCreate(BaseModel):
    """A new draw. Rules come from the defaults and are edited on the console afterwards; what
    has to be said here is the name, because that is what the list screen is sorted and scanned by."""
    event_title: str = Field(max_length=MAX_EVENT_TITLE)
    budget_note: str = Field(default="", max_length=MAX_BUDGET_NOTE)
    # Same people, next event: the family roster is re-typed by hand otherwise. The copy takes the
    # people, never their history — fresh tokens, no viewed_at, no email_sent_at.
    copy_participants_from: Optional[int] = None

    @field_validator("event_title")
    @classmethod
    def _titled(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ParticipantCreate(BaseModel):
    name: str = Field(max_length=MAX_NAME)
    role: Literal["ADULT", "CHILD"]
    family_group: str = Field(max_length=MAX_FAMILY_GROUP)
    group_key: str = Field(default="", max_length=MAX_GROUP_KEY)
    wishlist: str = Field(default="", max_length=MAX_WISHLIST)
    # Optional: plenty of relatives get their link the old way, on the kitchen table.
    email: str = Field(default="", max_length=MAX_EMAIL)

    @field_validator("name", "family_group")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("group_key")
    @classmethod
    def _strip_group(cls, value: str) -> str:
        # Blank on purpose: a participant with no group is in no group, so the same-group preference
        # never binds for them. This one is deliberately NOT _not_blank.
        return value.strip()

    @field_validator("email")
    @classmethod
    def _clean_email(cls, value: str) -> str:
        return _clean_email_value(value)


class ParticipantUpdate(BaseModel):
    """Edit a roster row. Every field is optional; omitted fields keep their value.

    While the draw is locked only `email` is accepted — see update_participant.
    """
    name: Optional[str] = Field(default=None, max_length=MAX_NAME)
    role: Optional[Literal["ADULT", "CHILD"]] = None
    family_group: Optional[str] = Field(default=None, max_length=MAX_FAMILY_GROUP)
    group_key: Optional[str] = Field(default=None, max_length=MAX_GROUP_KEY)
    email: Optional[str] = Field(default=None, max_length=MAX_EMAIL)

    @field_validator("name", "family_group")
    @classmethod
    def _not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("group_key")
    @classmethod
    def _strip_group(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else value.strip()

    @field_validator("email")
    @classmethod
    def _clean_email(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_email_value(value)


class WishlistUpdate(BaseModel):
    wishlist: str = Field(max_length=MAX_WISHLIST)


class EmailPreviewRequest(BaseModel):
    """Sample-render the template. There is deliberately no participant field: the preview is
    meant to be screenshot-able, and real names should not end up in one. `links` picks the
    template — one link, or the household mail that carries several people's links in one mail."""
    language: Optional[Literal["fr", "en"]] = None
    state: Literal["pending", "ready"] = "ready"
    links: int = Field(default=1, ge=1, le=len(PREVIEW_NAMES))


class LimitsOut(BaseModel):
    name: int
    family_group: int
    group_key: int
    wishlist: int
    email: int
    event_title: int
    budget_note: int
    max_quota: int


# --------------------------------------------------------------------------- #
# App, middleware, auth plumbing
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if using_default_password():
        audit("warning", message="ADMIN_PASSWORD is unset, blank, or the default 'admin'. Set a real "
                                 "password before sharing links; while it is blank the organizer "
                                 "console refuses every login")
    yield


app = FastAPI(title="Family Gift Exchange", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    response = await call_next(request)
    headers = SECURITY_HEADERS
    if request.url.path.startswith("/static/"):
        # `/static` is the only thing on this server another origin has to be able to load: an email
        # client fetching the art (Apple Mail and friends fetch it directly; Gmail re-serves it from
        # its own proxy), and the social-image crawler that reads /static/og-image.png. With
        # `same-origin` those loads are blocked in the browser (`ERR_BLOCKED_BY_RESPONSE.
        # NotSameOrigin`) and the mail shows a broken-image icon. Nothing personal lives in /static —
        # the participant's data only ever comes from the API — so the art is explicitly shareable.
        # No Cache-Control here on purpose: StaticFiles' ETag/Last-Modified already give the right
        # revalidation window, and a max-age would pin stale art across a redeploy.
        headers = {**SECURITY_HEADERS, "Cross-Origin-Resource-Policy": "cross-origin"}
    for header, value in headers.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def size_middleware(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return json_error({"detail": TOO_LARGE}, 413)
    if request.method in {"POST", "PUT", "PATCH"}:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return json_error({"detail": TOO_LARGE}, 413)
    return await call_next(request)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        audit(
            "request",
            ip=client_ip(request),
            method=request.method,
            path=redacted_path(request),
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    audit("server_error", ip=client_ip(request), path=redacted_path(request), error=type(exc).__name__)
    return json_error({"detail": "Internal server error"}, 500)


def _config_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_title": row["event_title"],
        "adult_draws_adult": row["adult_draws_adult"],
        "adult_draws_child": row["adult_draws_child"],
        "child_draws_adult": row["child_draws_adult"],
        "child_draws_child": row["child_draws_child"],
        "budget_note": row["budget_note"],
        "avoid_same_group": row["avoid_same_group"],
        "email_language": row["email_language"],
        "auto_email_on_draw": row["auto_email_on_draw"],
        "is_locked": row["is_locked"],
        # When this draw was opened and when it was drawn. The console shows both, and the list
        # screen sorts by them; `drawn_at` is NULL for a draw that was locked before the column
        # existed, which the UI renders as "verrouillé" with no date rather than an invented one.
        "created_at": row["created_at"],
        "drawn_at": row["drawn_at"],
    }


# One projection for every admin-facing participant read (state, create, update). Three copies of
# the column list is how group_key nearly drifted when it was added.
PARTICIPANT_COLUMNS = ("id, name, role, family_group, group_key, wishlist, email, token, "
                       "viewed_at, email_sent_at")


def _participant_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "family_group": row["family_group"],
        "group_key": row["group_key"],
        "wishlist": row["wishlist"],
        "email": row["email"],
        "token": row["token"],
        "viewed_at": row["viewed_at"],
        "email_sent_at": row["email_sent_at"],
    }


def _draw_row(conn: sqlite3.Connection, draw_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=DRAW_NOT_FOUND)
    return row


def select_draw_id(conn: sqlite3.Connection, requested: int | None) -> int:
    """Which draw an admin call acts on.

    An omitted `draw_id` means "the only draw there is" — the shape every deployment had before
    there was more than one, so an old client and the whole existing test suite keep working.
    Once a second draw exists, asking stops being optional: guessing which family event a click
    referred to is exactly the ambiguity that ends with relatives mailed from the wrong draw.
    """
    if requested is not None:
        _draw_row(conn, requested)
        return requested
    rows = conn.execute("SELECT id FROM draws ORDER BY id").fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    raise HTTPException(status_code=400, detail=DRAW_NONE if not rows else DRAW_ID_REQUIRED)


def _load_config(conn: sqlite3.Connection, draw_id: int | None = None) -> sqlite3.Row:
    """The rules of one draw. `draw_id=None` follows select_draw_id's single-draw rule."""
    return _draw_row(conn, select_draw_id(conn, draw_id))


def _reject_if_locked(row: sqlite3.Row) -> None:
    if row["is_locked"]:
        raise HTTPException(status_code=400, detail=LOCKED_MSG)


def _participant_row(conn: sqlite3.Connection, participant_id: int,
                     draw_id: int | None) -> sqlite3.Row:
    """A participant, but only if it belongs to the draw being acted on.

    Participant ids are global, so without this a console pointed at one draw could edit, mail or
    delete a person from another one by id. The roster already reads through a `draw_id` filter;
    this closes the same hole on the write paths.
    """
    row = conn.execute(
        f"SELECT {PARTICIPANT_COLUMNS} FROM participants WHERE id = ? AND draw_id = ?",
        (participant_id, select_draw_id(conn, draw_id)),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    return row


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/limits", dependencies=[Depends(enforce_api_limit)])
def limits() -> dict[str, Any]:
    """Server-side limits the frontend mirrors, so validation can't drift apart."""
    return {
        "name": MAX_NAME,
        "family_group": MAX_FAMILY_GROUP,
        "group_key": MAX_GROUP_KEY,
        "wishlist": MAX_WISHLIST,
        "email": MAX_EMAIL,
        "event_title": MAX_EVENT_TITLE,
        "budget_note": MAX_BUDGET_NOTE,
        "max_quota": MAX_DRAW_QUOTA,
        "default_password": using_default_password(),
    }


@app.post("/api/admin/login", dependencies=[Depends(enforce_api_limit)])
def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    ip = client_ip(request)
    allowed, retry = LIMITER.hit("login", ip, LOGIN_LIMIT)
    if not allowed:
        audit("login_rate_limited", ip=ip)
        raise _too_many(retry)

    password = admin_password()
    if not password:
        audit("login_refused_unconfigured", ip=ip)
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured on the server")

    if not hmac.compare_digest(payload.password, password):
        attempts = LIMITER.count("login", ip)
        if LOGIN_LOCKOUT_THRESHOLD > 0 and attempts >= LOGIN_LOCKOUT_THRESHOLD:
            LIMITER.block("login", ip, LOGIN_LOCKOUT_SECONDS)
            audit("login_lockout", ip=ip, seconds=LOGIN_LOCKOUT_SECONDS)
            raise _too_many(LOGIN_LOCKOUT_SECONDS)
        audit("login_failed", ip=ip)
        raise HTTPException(status_code=401, detail="Unauthorized")

    LIMITER.reset("login", ip)
    token, ttl = issue_session()
    audit("login_ok", ip=ip, session_ttl=ttl)
    return {"status": "OK", "token": token, "expires_in": ttl}


@app.post("/api/admin/logout", dependencies=[Depends(enforce_api_limit)])
def logout(request: Request, _: None = Depends(require_admin)) -> dict[str, str]:
    revoke_session(current_bearer(request))
    audit("logout", ip=client_ip(request))
    return {"status": "OK"}


@app.get("/api/admin/state", dependencies=[Depends(enforce_api_limit)])
def admin_state(
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """One draw: its rules + its roster.

    The organizer stays blind: this reads no pairing. The one contact with the `assignments` table is
    a single COUNT of same-group pairs, which says "N gifts crossed a group" and cannot name a single
    one of them. That number is what lets the console admit a relaxed draw instead of hiding it.
    """
    target = select_draw_id(conn, draw_id)
    participants = [
        _participant_dict(row)
        for row in conn.execute(
            f"SELECT {PARTICIPANT_COLUMNS} FROM participants WHERE draw_id = ? ORDER BY id",
            (target,),
        )
    ]
    # A COUNT, never a pairing — and only while the preference is on. The number answers "how far did
    # the rule have to bend", which has no meaning when the rule was switched off on purpose: counting
    # same-group pairs then would report a relaxation that was never forced. Scoped to this draw, so
    # a second family's pairings cannot tint this one's numbers.
    config = _config_dict(_load_config(conn, target))
    crossed = 0
    if config["avoid_same_group"]:
        crossed = conn.execute(
            "SELECT COUNT(*) FROM assignments a "
            "JOIN participants g ON g.id = a.giver_id "
            "JOIN participants r ON r.id = a.receiver_id "
            "WHERE g.draw_id = ? AND r.draw_id = ? "
            "AND g.group_key <> '' AND lower(trim(g.group_key)) = lower(trim(r.group_key))",
            (target, target),
        ).fetchone()[0]
    config["same_group_crossings"] = crossed
    # The console needs to know whether emailing is even possible before offering the buttons —
    # a deployment without RESEND_API_KEY shows a clear "not configured" note instead of twelve
    # buttons that all fail.
    return {"config": config, "participants": participants, "email_enabled": email_enabled()}


@app.get("/api/admin/draws", dependencies=[Depends(enforce_api_limit)])
def list_draws(
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Every draw with the counts the list screen needs — never a pairing.

    The status line is what an organizer scans: has this one been drawn, how many people are in it,
    how many have opened their link, how many have been mailed. `assignments` is touched only as a
    COUNT, and only to answer "is there a result yet", which is the same question is_locked answers
    from the other side.
    """
    rows = conn.execute(
        "SELECT id, event_title, is_locked, created_at, drawn_at FROM draws ORDER BY id"
    ).fetchall()
    counts = {
        row["draw_id"]: row
        for row in conn.execute(
            "SELECT draw_id, COUNT(*) AS people, "
            "SUM(CASE WHEN viewed_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
            "SUM(CASE WHEN email_sent_at IS NOT NULL THEN 1 ELSE 0 END) AS mailed, "
            "SUM(CASE WHEN email <> '' THEN 1 ELSE 0 END) AS with_email "
            "FROM participants GROUP BY draw_id"
        )
    }
    draws = []
    for draw in rows:
        tally = counts.get(draw["id"])
        draws.append({
            "id": draw["id"],
            "event_title": draw["event_title"],
            "is_locked": draw["is_locked"],
            "created_at": draw["created_at"],
            "drawn_at": draw["drawn_at"],
            "participants": tally["people"] if tally else 0,
            "opened": tally["opened"] if tally else 0,
            "mailed": tally["mailed"] if tally else 0,
            "with_email": tally["with_email"] if tally else 0,
        })
    return {"draws": draws, "email_enabled": email_enabled()}


@app.post("/api/admin/draws", status_code=201, dependencies=[Depends(enforce_api_limit)])
def create_draw(
    payload: DrawCreate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Open a new draw. Existing draws are untouched — that is the whole point of the screen.

    `copy_participants_from` re-seeds the roster for the next event. It copies the *people*: the new
    rows get brand new tokens and no `viewed_at` / `email_sent_at`, because a link that already went
    out to last year's pairing must not silently become this year's credential. Two draws sharing a
    token would also break the one property the participant URL relies on: whoever holds a token
    holds exactly one draw.
    """
    created = conn.execute(
        "INSERT INTO draws (event_title, budget_note, created_at) VALUES (?, ?, ?)",
        (payload.event_title.strip(), payload.budget_note, utc_now()),
    )
    new_id = created.lastrowid
    copied = 0
    if payload.copy_participants_from is not None:
        source = _draw_row(conn, payload.copy_participants_from)
        people = conn.execute(
            "SELECT name, role, family_group, group_key, wishlist, email FROM participants "
            "WHERE draw_id = ? ORDER BY id",
            (source["id"],),
        ).fetchall()
        conn.executemany(
            "INSERT INTO participants (draw_id, name, role, family_group, group_key, wishlist, "
            "email, token) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(new_id, person["name"], person["role"], person["family_group"], person["group_key"],
              person["wishlist"], person["email"], str(uuid.uuid4())) for person in people],
        )
        copied = len(people)
    audit("draw_created", ip=client_ip(request), draw_id=new_id, copied_participants=copied)
    return {"status": "OK", "draw": _config_dict(_draw_row(conn, new_id)),
            "copied_participants": copied}


@app.delete("/api/admin/draws/{draw_id}", dependencies=[Depends(enforce_api_limit)])
def delete_draw(
    draw_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a draw, its roster and its pairings. Locked draws are refused, not forced.

    A locked draw is a result: relatives hold links that reveal it. Deleting it should not be one
    mis-click away from unlinking them, so the sequence is reset (which clears the pairings and the
    delivery stamps and says so out loud) and then delete. The cascade is written out rather than
    left to the FK because a database migrated from the single-draw schema has no FK on
    `participants.draw_id` — SQLite cannot add one to an existing table.
    """
    row = _draw_row(conn, draw_id)
    _reject_if_locked(row)
    with _draw_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM assignments WHERE giver_id IN "
                "(SELECT id FROM participants WHERE draw_id = ?) "
                "OR receiver_id IN (SELECT id FROM participants WHERE draw_id = ?)",
                (draw_id, draw_id),
            )
            gone = conn.execute("DELETE FROM participants WHERE draw_id = ?", (draw_id,)).rowcount
            conn.execute("DELETE FROM draws WHERE id = ?", (draw_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    audit("draw_deleted", ip=client_ip(request), draw_id=draw_id, participants_removed=gone)
    return {"status": "OK", "deleted": draw_id, "participants_removed": gone}


@app.put("/api/admin/config", dependencies=[Depends(enforce_api_limit)])
def update_config(
    payload: ConfigUpdate,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    target = select_draw_id(conn, draw_id)
    _reject_if_locked(_load_config(conn, target))
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    allowed = (
        "event_title",
        "adult_draws_adult",
        "adult_draws_child",
        "child_draws_adult",
        "child_draws_child",
        "budget_note",
        "avoid_same_group",
        "email_language",
        "auto_email_on_draw",
    )
    updates = {key: value for key, value in updates.items() if key in allowed}
    if updates:
        clauses = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE draws SET {clauses} WHERE id = ?", list(updates.values()) + [target])
    audit("config_updated", ip=client_ip(request), draw_id=target, fields=sorted(updates))
    return {"status": "OK", "config": _config_dict(_load_config(conn, target))}


@app.post("/api/admin/participants", status_code=201, dependencies=[Depends(enforce_api_limit)])
def add_participant(
    payload: ParticipantCreate,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    target = select_draw_id(conn, draw_id)
    _reject_if_locked(_load_config(conn, target))
    cursor = conn.execute(
        "INSERT INTO participants (draw_id, name, role, family_group, group_key, wishlist, email, "
        "token) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            payload.name,
            payload.role,
            payload.family_group,
            payload.group_key,
            payload.wishlist,
            payload.email,
            str(uuid.uuid4()),
        ),
    )
    row = conn.execute(
        f"SELECT {PARTICIPANT_COLUMNS} FROM participants WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return {"status": "OK", "participant": _participant_dict(row)}


@app.put("/api/admin/participants/{participant_id}", dependencies=[Depends(enforce_api_limit)])
def update_participant(
    participant_id: int,
    payload: ParticipantUpdate,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Fix a roster row without delete-and-recreate — which would burn the participant's token.

    The locked case is the one this endpoint exists for: the draw has run, an email comes back
    wrong, and the address is the only thing that can still be fixed. Changing name/role/group
    while locked would rewrite what an existing pairing means, so those stay locked too, with
    LOCKED_MSG the UI already explains. Editing an email clears email_sent_at: a correction means
    the old send went somewhere else.
    """
    target = select_draw_id(conn, draw_id)
    config_row = _load_config(conn, target)
    if config_row["is_locked"]:
        updates = payload.model_dump(exclude_unset=True, exclude_none=True)
        if set(updates) - {"email"}:
            raise HTTPException(status_code=400, detail=LOCKED_MSG)
    else:
        updates = {key: value for key, value in
                   payload.model_dump(exclude_unset=True).items() if value is not None}
        # group_key may be cleared deliberately; role/name/family_group cannot, see ParticipantUpdate.
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    existed = _participant_row(conn, participant_id, target)

    clauses = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values())
    if "email" in updates and updates["email"] != (existed["email"] or ""):
        clauses += ", email_sent_at = NULL"
    conn.execute(f"UPDATE participants SET {clauses} WHERE id = ? AND draw_id = ?",
                 values + [participant_id, target])
    audit("participant_updated", ip=client_ip(request), participant_id=participant_id,
          draw_id=target, fields=sorted(updates))
    row = _participant_row(conn, participant_id, target)
    return {"status": "OK", "participant": _participant_dict(row)}


@app.delete("/api/admin/participants/{participant_id}", dependencies=[Depends(enforce_api_limit)])
def delete_participant(
    participant_id: int,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    target = select_draw_id(conn, draw_id)
    _reject_if_locked(_load_config(conn, target))
    _participant_row(conn, participant_id, target)
    cursor = conn.execute("DELETE FROM participants WHERE id = ? AND draw_id = ?",
                          (participant_id, target))
    audit("participant_deleted", ip=client_ip(request), participant_id=participant_id,
          draw_id=target)
    return {"status": "OK", "deleted": participant_id}


@app.post("/api/admin/draw", dependencies=[Depends(enforce_api_limit)])
def run_draw(
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    target = select_draw_id(conn, draw_id)
    config_row = _load_config(conn, target)
    if config_row["is_locked"]:
        raise HTTPException(status_code=409, detail=ALREADY_LOCKED)
    participants = [
        {"id": row["id"], "role": row["role"], "group_key": row["group_key"]}
        for row in conn.execute(
            "SELECT id, role, group_key FROM participants WHERE draw_id = ? ORDER BY id", (target,))
    ]
    if not participants:
        raise HTTPException(status_code=400, detail=NO_PARTICIPANTS)

    # Matching runs before any write, so a rejected draw cannot corrupt state.
    pairs, crossed = generate_draw(_config_dict(config_row), participants)

    with _draw_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Scoped to this draw: re-drawing the office exchange must not erase the family's
            # pairings, and a reset on one draw cannot clear another one's "already mailed" stamps.
            conn.execute(
                "DELETE FROM assignments WHERE giver_id IN "
                "(SELECT id FROM participants WHERE draw_id = ?) "
                "OR receiver_id IN (SELECT id FROM participants WHERE draw_id = ?)",
                (target, target),
            )
            conn.executemany(
                "INSERT INTO assignments (giver_id, receiver_id) VALUES (?, ?)", pairs
            )
            conn.execute("UPDATE draws SET is_locked = 1, drawn_at = ? WHERE id = ?",
                         (utc_now(), target))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    audit("draw_locked", ip=client_ip(request), draw_id=target, assignments=len(pairs),
          same_group_crossings=crossed)

    # Opt-in auto-delivery (the config checkbox). It runs strictly after the lock committed: a
    # slow or dead Resend can never leave a half-drawn event, and a failed send never undoes the
    # draw — the console response reports exactly who did not get mail.
    emailed = None
    if config_row["auto_email_on_draw"]:
        base_url = mail_base_url(request)
        with_email = [p for p in _all_participants(conn, target) if p["email"]]
        emailed = send_link_emails(conn, with_email, config_row, base_url)
        audit("auto_email_on_draw", ip=client_ip(request), draw_id=target, sent=emailed["sent"],
              failed=emailed["failed"])

    response = {"status": "OK", "draw_id": target, "assignments_created": len(pairs),
                "same_group_crossings": crossed, "is_locked": 1}
    if emailed is not None:
        response["emails"] = emailed
    return response


@app.post("/api/admin/reset", dependencies=[Depends(enforce_api_limit)])
def reset_draw(
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    target = select_draw_id(conn, draw_id)
    with _draw_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM assignments WHERE giver_id IN "
                "(SELECT id FROM participants WHERE draw_id = ?) "
                "OR receiver_id IN (SELECT id FROM participants WHERE draw_id = ?)",
                (target, target),
            )
            # Both stamps describe the current draw, not the person: a re-draw means the links
            # reveal something new AND the old mail quotes an obsolete draw. The addresses
            # themselves survive — only the "this draw went out" stamps reset.
            conn.execute("UPDATE participants SET viewed_at = NULL, email_sent_at = NULL "
                         "WHERE draw_id = ?", (target,))
            conn.execute("UPDATE draws SET is_locked = 0, drawn_at = NULL WHERE id = ?", (target,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    audit("draw_reset", ip=client_ip(request), draw_id=target)
    return {"status": "OK"}


# --------------------------------------------------------------------------- #
# Email endpoints (admin) — send one, send all, preview the template
# --------------------------------------------------------------------------- #


def _email_rate_check(request: Request, attempts: int) -> None:
    """Sends are the one admin action with a blast radius (twelve relatives, one mis-click),
    so they get their own budget on top of the general API limit."""
    ip = client_ip(request)
    for _ in range(attempts):
        allowed, retry = LIMITER.hit("email", ip, EMAIL_SEND_LIMIT)
        if not allowed:
            audit("email_rate_limited", ip=ip)
            raise _too_many(retry)


def _send_rows_or_422(request: Request, conn: sqlite3.Connection, people: list[dict[str, Any]],
                      config_row: sqlite3.Row) -> dict[str, Any]:
    # The limit counts *sends*, not people: twelve relatives at five mailboxes cost five provider
    # calls, and billing the organizer for twelve would throttle the wrong thing.
    _email_rate_check(request, max(1, len(_group_by_address(people))))
    if not people:
        raise HTTPException(status_code=422, detail=EMAIL_NO_ADDRESS)
    if not email_enabled():
        raise HTTPException(status_code=503, detail=EMAIL_NOT_CONFIGURED)
    base_url = mail_base_url(request)
    if not base_url:
        raise HTTPException(status_code=503, detail=EMAIL_BAD_BASE_URL)
    return send_link_emails(conn, people, config_row, base_url)


@app.post("/api/admin/participants/{participant_id}/email", dependencies=[Depends(enforce_api_limit)])
def email_participant_link(
    participant_id: int,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Send this person's link — as one mail per mailbox, like every other send.

    Clicking « Envoyer » on one row of a family of three at one address used to mean three mails in
    one inbox, and clicking it three times meant six. So the row sends the *household*: one mail
    carrying every link at that address in this draw, and the response names the others so the
    console can say what actually went out.

    Works locked or unlocked: before the draw the mail says "the draw has not happened yet, here is
    your link to add ideas" — the token is already theirs to keep.
    """
    target = select_draw_id(conn, draw_id)
    row = _participant_row(conn, participant_id, target)
    address = (row["email"] or "").strip().lower()
    if not address:
        raise HTTPException(status_code=422, detail=EMAIL_NO_ADDRESS)
    household = [person for person in _all_participants(conn, target)
                 if (person["email"] or "").strip().lower() == address]
    report = _send_rows_or_422(request, conn, household, _load_config(conn, target))
    outcome = next(item for item in report["results"] if item["id"] == participant_id)
    if not outcome["ok"]:
        server_side = outcome["reason"] in (EMAIL_NOT_CONFIGURED, EMAIL_BAD_BASE_URL)
        raise HTTPException(status_code=503 if server_side else 422, detail=outcome["reason"])
    return {"status": "OK", "sent": outcome, "messages": report["messages"],
            "with": [person["name"] for person in household if person["id"] != participant_id]}


@app.post("/api/admin/emails", dependencies=[Depends(enforce_api_limit)])
def email_all_links(
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Send every participant of this draw who has an address. Participants with no address are
    skipped and listed, not failed: the point is a per-row report the console can show, not one
    error that hides who got mail. Scoped by draw — "send the links" must never mean "mail every
    relative in every event this server has ever held".
    """
    target = select_draw_id(conn, draw_id)
    report = _send_rows_or_422(request, conn, _all_participants(conn, target),
                               _load_config(conn, target))
    audit("emails_bulk", ip=client_ip(request), draw_id=target, sent=report["sent"],
          failed=report["failed"])
    return {"status": "OK", **report}


@app.post("/api/admin/emails/preview", dependencies=[Depends(enforce_api_limit)])
def email_preview(
    payload: EmailPreviewRequest,
    request: Request,
    draw_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Render the template with sample names and sample links. No participant data goes into the
    response, so the preview is safe to screenshot and cannot leak a roster. `links` chooses which
    of the two templates to show: one link, or the household mail that carries several.
    """
    config_row = _load_config(conn, draw_id)
    language = payload.language or config_row["email_language"]
    # The preview shows what a real send would contain, so it resolves its origin exactly the way a
    # send does — `PUBLIC_BASE_URL` first. Otherwise the modal shows a caller-dependent URL (a Host of
    # 127.0.0.1 while the mail itself carries the real hostname), and the preview stops being proof.
    base_url = mail_base_url(request) or "https://tirage-noel.alserv.org"
    sample = "00000000-0000-0000-0000-000000000000"
    people = [{"name": name, "link": participant_link(base_url, sample)}
              for name in PREVIEW_NAMES[:max(1, payload.links)]]
    content = build_links_email(people, config_row["event_title"], language,
                                ready=payload.state == "ready", assets_base=base_url)
    audit("email_previewed", ip=client_ip(request), language=language, state=payload.state,
          links=len(people))
    return {"status": "OK", "language": language, "state": payload.state,
            "links": len(people), "email_enabled": email_enabled(), **content}


# --------------------------------------------------------------------------- #
# Participant (public, token-scoped) endpoints
# --------------------------------------------------------------------------- #


def _participant_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, draw_id, name, role, family_group, wishlist, token, viewed_at "
        "FROM participants WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid link")
    return row


@app.get("/api/draw/{token}", dependencies=[Depends(enforce_draw_limit)])
def get_draw(
    token: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    participant = _participant_by_token(conn, token)
    # The link decides which draw this is, not a server-wide "current" one: with several draws open
    # at once, the event title and the locked/unlocked state a relative sees must come from the
    # draw their token belongs to.
    config = conn.execute(
        "SELECT event_title, budget_note, is_locked FROM draws WHERE id = ?",
        (participant["draw_id"],),
    ).fetchone()

    if not config["is_locked"]:
        return {
            "status": "PENDING",
            "event_title": config["event_title"],
            "participant": {
                "name": participant["name"],
                "role": participant["role"],
                "wishlist": participant["wishlist"],
            },
        }

    first_view = participant["viewed_at"] is None
    if first_view:
        conn.execute(
            "UPDATE participants SET viewed_at = ? WHERE id = ?",
            (utc_now(), participant["id"]),
        )
        audit("link_opened", participant_id=participant["id"])

    rows = conn.execute(
        "SELECT p.name, p.role, p.wishlist FROM assignments a "
        "JOIN participants p ON p.id = a.receiver_id "
        "WHERE a.giver_id = ? ORDER BY p.id",
        (participant["id"],),
    ).fetchall()
    return {
        "status": "READY",
        "event_title": config["event_title"],
        "budget_note": config["budget_note"],
        "participant": {
            "name": participant["name"],
            "role": participant["role"],
            "wishlist": participant["wishlist"],
        },
        "assignments": [
            {"name": row["name"], "role": row["role"], "wishlist": row["wishlist"]}
            for row in rows
        ],
    }


@app.put("/api/draw/{token}/wishlist", dependencies=[Depends(enforce_draw_limit)])
def update_wishlist(
    token: str,
    payload: WishlistUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    participant = _participant_by_token(conn, token)
    conn.execute(
        "UPDATE participants SET wishlist = ? WHERE id = ?",
        (payload.wishlist, participant["id"]),
    )
    return {"status": "OK", "wishlist": payload.wishlist}


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #

SHARE_TITLE = "Échange de cadeaux · Père Noël secret"
SHARE_DESCRIPTION = ("Chacun reçoit son lien secret. Personne sait qui pige qui, même pas "
                     "l’organisateur. Adultes et enfants, jamais dans la même famille.")
SHARE_IMAGE = "/static/og-image.png"
SHARE_IMAGE_ALT = "Un cadeau doré et un flocon sur fond de sapin"
SHARE_META_MARKER = "<!--share-meta-->"

# The Host header only has to be echoed into og:url and og:image, so accept what a hostname or an
# address (with optional port) can look like and fall back to the parsed URL otherwise: a preview
# pointing at the wrong host is cosmetic, a preview carrying injected quotes is not.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-\[\]:]{1,253}$")

INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def public_base_url(request: Request) -> str:
    """Absolute origin for the share tags, or "" when the request carries no usable one.

    There is no clean fallback to fall back to: Starlette builds `request.url` from the same Host
    header, so a hostile header is hostile in both places. When the host is not a host, the
    URL-dependent tags are skipped — a preview that loses its image beats a preview that points a
    crawler at a host the client chose.

    Behind your own proxy (TRUST_PROXY=1) `X-Forwarded-Proto` is the only way to learn the visit
    arrived over https; without it the tags would advertise `http://` and several crawlers quietly
    drop the card.
    """
    host = request.headers.get("host", "")
    if not _HOST_RE.match(host):
        audit("share_meta_rejected_host", host=host[:40])
        return ""
    scheme = request.url.scheme
    if trust_proxy():
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        if forwarded in ("http", "https"):
            scheme = forwarded
    return f"{scheme}://{host}"


def share_meta(request: Request) -> str:
    """Open Graph / Twitter tags so a shared link shows the gift card and the description.

    Deliberately the same block for "/" and for a private "/?t=<uuid>": crawlers read this, so it
    carries the generic card only — never a name, an event title, or a token.
    """
    pairs = [
        ('property="og:type"', "website"),
        ('property="og:site_name"', "Échange de cadeaux"),
        ('property="og:title"', SHARE_TITLE),
        ('property="og:description"', SHARE_DESCRIPTION),
        ('property="og:locale"', "fr_CA"),
        ('property="og:locale:alternate"', "en_US"),
        ('name="twitter:card"', "summary_large_image"),
        ('name="twitter:title"', SHARE_TITLE),
        ('name="twitter:description"', SHARE_DESCRIPTION),
    ]
    base = public_base_url(request)
    if base:
        image = f"{base}{SHARE_IMAGE}"
        pairs += [
            ('property="og:url"', f"{base}/"),
            ('property="og:image"', image),
            ('property="og:image:width"', "1200"),
            ('property="og:image:height"', "630"),
            ('property="og:image:alt"', SHARE_IMAGE_ALT),
            ('name="twitter:image"', image),
        ]
    return "\n".join(f'<meta {attr} content="{html.escape(value, quote=True)}">' for attr, value in pairs)


@app.get("/")
def index(request: Request) -> HTMLResponse:
    """The SPA shell.

    Injected instead of served as a file because the share tags have to name the host the family
    was actually given — the public domain, a LAN override, or an IP:port — and only the request
    knows that. `no-cache` for the same reason: the document's head differs per host.
    """
    body = INDEX_HTML.replace(SHARE_META_MARKER, share_meta(request))
    return HTMLResponse(body, headers={"Cache-Control": "no-cache"})


@app.get("/draw/{token}", dependencies=[Depends(enforce_draw_limit)])
def legacy_draw_link(token: str) -> RedirectResponse:
    """`/draw/<uuid>` -> the SPA route, for links that are already sitting in inboxes.

    The first batch of link emails was built from the API's path shape instead of the SPA's, so
    twelve people were handed a URL that answered `{"detail":"Not Found"}`. Emails cannot be
    unsent, and asking a grandmother to wait for a rebuild is not a fix: this turns every such
    link into a bounce to `/?t=<uuid>`, the URL the interface actually reads.

    Only a UUID-shaped token is redirected — anything else stays a 404 rather than advertising
    that some other path shape is meaningful. The redirect itself reveals nothing (it echoes the
    token back to the same browser that supplied it), and the path is already redacted to
    `/draw/<token>` in the audit log by redacted_path(). `enforce_draw_limit` keeps it off the
    cheap-enumeration path anyway.
    """
    if not TOKEN_SEGMENT.match(token):
        raise HTTPException(status_code=404, detail="Link not found")
    return RedirectResponse(f"/?t={token}", status_code=302)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
