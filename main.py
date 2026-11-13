"""Configurable family gift exchange (Secret Santa) server.

FastAPI + SQLite, single-file backend:
  * admin API (config, roster, draw execution) guarded by ADMIN_PASSWORD or a
    short-lived signed session token
  * public per-participant API guarded only by a UUID token
  * a randomized slot-allocation + backtracking matching engine

The organizer is deliberately kept blind: no admin endpoint ever reads the
`assignments` table.

Security posture (see PLAN.md "Security hardening"): per-IP rate limits, a request
size cap, security headers with a CDN-pinning CSP, HMAC session tokens, constant-time
comparisons, input length caps, serialised draw/reset, and an audit trail.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_ATTEMPTS = 20000
MAX_DRAW_QUOTA = 10

# Field length caps: mirrored by the frontend so nobody hits a 422 blind.
MAX_NAME = 80
MAX_FAMILY_GROUP = 60
MAX_WISHLIST = 1000
MAX_EVENT_TITLE = 120
MAX_BUDGET_NOTE = 500

EMPTY_TARGET_GROUP = "Cannot assign gifts to an empty target group"
UNSOLVABLE = (
    "Constraints are mathematically unsolvable with current family groups "
    "and draw counts."
)
LOCKED_MSG = "The draw is locked. Reset it before making changes."
ALREADY_LOCKED = "The draw is already locked. Reset it before drawing again."
TOO_LARGE = "Request body too large"

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
        "img-src 'self' data:; "
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
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    is_locked INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL,
    wishlist TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    viewed_at TEXT DEFAULT NULL
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
        conn.executescript(SCHEMA)
    finally:
        conn.close()


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
    """Family-group key: participants sharing this value can never draw each other."""
    return (value or "").strip().lower()


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

    def eligible(self, role: str, group: str) -> list[int]:
        """Slots a giver in `group` may take — same-household buckets are never offered."""
        out: list[int] = []
        for (slot_role, slot_group), pids in self.by_group.items():
            if slot_role == role and slot_group != group and pids:
                out.extend(pids)
        return out

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
    t_adult: int,
    t_child: int,
) -> bool:
    """Cheap necessary conditions.

    False means "stop now"; True does not prove a solution exists.
    """

    def counts_of(members: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for member in members:
            key = norm_group(member["family_group"])
            out[key] = out.get(key, 0) + 1
        return out

    adult_groups = counts_of(adults)
    child_groups = counts_of(children)

    # Every giver needs enough receivers outside their own household.
    for giver in givers:
        need_adult, need_child = quota[giver["role"]]
        group = norm_group(giver["family_group"])
        if need_adult > len(adults) - adult_groups.get(group, 0):
            return False
        if need_child > len(children) - child_groups.get(group, 0):
            return False

    # Household supply/demand balance: slots located inside a household can only
    # be filled by givers outside it, and givers inside it can only be served
    # by slots outside it.
    def balanced(
        members: list[dict[str, Any]],
        groups: dict[str, int],
        total_slots: int,
        slot_index: int,
    ) -> bool:
        if total_slots <= 0 or not members:
            return True
        base, _remainder = divmod(total_slots, len(members))
        for group, size in groups.items():
            slots_inside_at_least = base * size
            demand_inside = sum(
                quota[g["role"]][slot_index]
                for g in givers
                if norm_group(g["family_group"]) == group
            )
            demand_outside = sum(
                quota[g["role"]][slot_index]
                for g in givers
                if norm_group(g["family_group"]) != group
            )
            if demand_inside > total_slots - slots_inside_at_least:
                return False
            if demand_outside < slots_inside_at_least:
                return False
        return True

    if not balanced(adults, adult_groups, t_adult, 0):
        return False
    if not balanced(children, child_groups, t_child, 1):
        return False
    return True


def _solve_attempt(
    order: list[dict[str, Any]],
    quota: dict[str, tuple[int, int]],
    slots: _Slots,
    rng: random.Random,
    max_nodes: int = 50000,
) -> Optional[list[tuple[int, int]]]:
    """Depth-first deal of every giver's slots, backtracking when boxed in.

    Returns the assignment pairs, or None when this attempt (including its
    backtracking budget) failed.
    """
    pairs: list[tuple[int, int]] = []
    nodes = {"used": 0}

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
        group = norm_group(giver["family_group"])
        chosen: list[int] = []
        used: set[int] = set()

        def take_adult(remaining: int) -> bool:
            if nodes["used"] > max_nodes:
                return False
            if remaining == 0:
                return take_child(need_child)
            candidates = slots.eligible("ADULT", group)
            rng.shuffle(candidates)
            for pid in candidates:
                if not slots.available(pid) or pid in used:
                    continue
                used.add(pid)
                slots.dec(pid)
                chosen.append(pid)
                nodes["used"] += 1
                if take_adult(remaining - 1):
                    return True
                chosen.pop()
                slots.inc(pid)
                used.discard(pid)
            return False

        def take_child(remaining: int) -> bool:
            if nodes["used"] > max_nodes:
                return False
            if remaining == 0:
                return place()
            candidates = slots.eligible("CHILD", group)
            rng.shuffle(candidates)
            for pid in candidates:
                if not slots.available(pid) or pid in used:
                    continue
                used.add(pid)
                slots.dec(pid)
                chosen.append(pid)
                nodes["used"] += 1
                if take_child(remaining - 1):
                    return True
                chosen.pop()
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

        return take_adult(need_adult)

    if assign(0) and slots.total() == 0:
        return pairs
    return None


def generate_draw(
    config: dict[str, Any],
    participants: list[dict[str, Any]],
    max_attempts: int = MAX_ATTEMPTS,
    seed: Optional[int] = None,
) -> list[tuple[int, int]]:
    """Produce (giver_id, receiver_id) pairs honouring every constraint.

    Raises HTTPException(400) for an empty target group and HTTPException(422)
    when the constraints cannot be satisfied.
    """
    adult_draws_adult = int(config["adult_draws_adult"])
    adult_draws_child = int(config["adult_draws_child"])
    child_draws_adult = int(config["child_draws_adult"])
    child_draws_child = int(config["child_draws_child"])

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
        return []

    group_of = {int(p["id"]): norm_group(p["family_group"]) for p in participants}
    role_of = {int(p["id"]): p["role"] for p in participants}

    quota = {
        "ADULT": (adult_draws_adult, adult_draws_child),
        "CHILD": (child_draws_adult, child_draws_child),
    }
    givers = [p for p in participants if sum(quota[p["role"]]) > 0]

    if not _feasible(givers, quota, adults, children, t_adult, t_child):
        raise HTTPException(status_code=422, detail=UNSOLVABLE)

    def tightness(giver: dict[str, Any]) -> int:
        """Lower = fewer options, so it is dealt first."""
        group = norm_group(giver["family_group"])
        available_adults = sum(
            1 for p in adults if norm_group(p["family_group"]) != group
        )
        available_children = sum(
            1 for p in children if norm_group(p["family_group"]) != group
        )
        need_adult, need_child = quota[giver["role"]]
        return min(need_adult + need_child, available_adults + available_children)

    total_needed = len(adult_pool) + len(child_pool)

    for _ in range(max(1, max_attempts)):
        # most-constrained givers first; ties re-shuffled so attempts differ
        order = sorted(givers, key=lambda g: (tightness(g), rng.random()))
        slots = _Slots(adult_pool, child_pool, group_of, role_of)
        pairs = _solve_attempt(order, quota, slots, rng)
        if pairs is not None:
            return pairs

    raise HTTPException(status_code=422, detail=UNSOLVABLE)


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

    @field_validator("adult_draws_adult", "adult_draws_child", "child_draws_adult", "child_draws_child")
    @classmethod
    def _quota_in_range(cls, value: Optional[int]) -> Optional[int]:
        # The lower bound is checked explicitly: a bare `ge` is dropped on Optional[int].
        if value is not None and not 0 <= value <= MAX_DRAW_QUOTA:
            raise ValueError(f"must be between 0 and {MAX_DRAW_QUOTA}")
        return value


class ParticipantCreate(BaseModel):
    name: str = Field(max_length=MAX_NAME)
    role: Literal["ADULT", "CHILD"]
    family_group: str = Field(max_length=MAX_FAMILY_GROUP)
    wishlist: str = Field(default="", max_length=MAX_WISHLIST)

    @field_validator("name", "family_group")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class WishlistUpdate(BaseModel):
    wishlist: str = Field(max_length=MAX_WISHLIST)


class LimitsOut(BaseModel):
    name: int
    family_group: int
    wishlist: int
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
    for header, value in SECURITY_HEADERS.items():
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
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    audit("server_error", ip=client_ip(request), path=request.url.path, error=type(exc).__name__)
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
        "is_locked": row["is_locked"],
    }


def _load_config(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM config WHERE id = 1").fetchone()


def _reject_if_locked(row: sqlite3.Row) -> None:
    if row["is_locked"]:
        raise HTTPException(status_code=400, detail=LOCKED_MSG)


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
        "wishlist": MAX_WISHLIST,
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
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Config + roster. Never touches the assignments table (blind organizer)."""
    participants = [
        {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "family_group": row["family_group"],
            "wishlist": row["wishlist"],
            "token": row["token"],
            "viewed_at": row["viewed_at"],
        }
        for row in conn.execute(
            "SELECT id, name, role, family_group, wishlist, token, viewed_at "
            "FROM participants ORDER BY id"
        )
    ]
    return {"config": _config_dict(_load_config(conn)), "participants": participants}


@app.put("/api/admin/config", dependencies=[Depends(enforce_api_limit)])
def update_config(
    payload: ConfigUpdate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    _reject_if_locked(_load_config(conn))
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    allowed = (
        "event_title",
        "adult_draws_adult",
        "adult_draws_child",
        "child_draws_adult",
        "child_draws_child",
        "budget_note",
    )
    updates = {key: value for key, value in updates.items() if key in allowed}
    if updates:
        clauses = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE config SET {clauses} WHERE id = 1", list(updates.values()))
    audit("config_updated", ip=client_ip(request), fields=sorted(updates))
    return {"status": "OK", "config": _config_dict(_load_config(conn))}


@app.post("/api/admin/participants", status_code=201, dependencies=[Depends(enforce_api_limit)])
def add_participant(
    payload: ParticipantCreate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    _reject_if_locked(_load_config(conn))
    cursor = conn.execute(
        "INSERT INTO participants (name, role, family_group, wishlist, token) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            payload.name,
            payload.role,
            payload.family_group,
            payload.wishlist,
            str(uuid.uuid4()),
        ),
    )
    row = conn.execute(
        "SELECT id, name, role, family_group, wishlist, token, viewed_at "
        "FROM participants WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return {
        "status": "OK",
        "participant": {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "family_group": row["family_group"],
            "wishlist": row["wishlist"],
            "token": row["token"],
            "viewed_at": row["viewed_at"],
        },
    }


@app.delete("/api/admin/participants/{participant_id}", dependencies=[Depends(enforce_api_limit)])
def delete_participant(
    participant_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    _reject_if_locked(_load_config(conn))
    cursor = conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Participant not found")
    audit("participant_deleted", ip=client_ip(request), participant_id=participant_id)
    return {"status": "OK", "deleted": participant_id}


@app.post("/api/admin/draw", dependencies=[Depends(enforce_api_limit)])
def run_draw(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    config_row = _load_config(conn)
    if config_row["is_locked"]:
        raise HTTPException(status_code=409, detail=ALREADY_LOCKED)
    participants = [
        {"id": row["id"], "role": row["role"], "family_group": row["family_group"]}
        for row in conn.execute("SELECT id, role, family_group FROM participants ORDER BY id")
    ]
    if not participants:
        raise HTTPException(status_code=400, detail="Cannot run a draw with no participants")

    # Matching runs before any write, so a rejected draw cannot corrupt state.
    pairs = generate_draw(_config_dict(config_row), participants)

    with _draw_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM assignments")
            conn.executemany(
                "INSERT INTO assignments (giver_id, receiver_id) VALUES (?, ?)", pairs
            )
            conn.execute("UPDATE config SET is_locked = 1 WHERE id = 1")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    audit("draw_locked", ip=client_ip(request), assignments=len(pairs))
    return {"status": "OK", "assignments_created": len(pairs), "is_locked": 1}


@app.post("/api/admin/reset", dependencies=[Depends(enforce_api_limit)])
def reset_draw(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    with _draw_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM assignments")
            conn.execute("UPDATE participants SET viewed_at = NULL")
            conn.execute("UPDATE config SET is_locked = 0 WHERE id = 1")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    audit("draw_reset", ip=client_ip(request))
    return {"status": "OK"}


# --------------------------------------------------------------------------- #
# Participant (public, token-scoped) endpoints
# --------------------------------------------------------------------------- #


def _participant_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, role, family_group, wishlist, token, viewed_at "
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
    config = conn.execute(
        "SELECT event_title, budget_note, is_locked FROM config WHERE id = 1"
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
