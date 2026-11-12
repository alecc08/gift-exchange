"""Configurable family gift exchange (Secret Santa) server.

FastAPI + SQLite, single-file backend:
  * admin API (config, roster, draw execution) guarded by ADMIN_PASSWORD
  * public per-participant API guarded only by a UUID token
  * a randomized slot-allocation + backtracking matching engine

The organizer is deliberately kept blind: no admin endpoint ever reads the
`assignments` table.
"""

from __future__ import annotations

import hmac
import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_ATTEMPTS = 20000
MAX_DRAW_QUOTA = 10

EMPTY_TARGET_GROUP = "Cannot assign gifts to an empty target group"
UNSOLVABLE = (
    "Constraints are mathematically unsolvable with current family groups "
    "and draw counts."
)
LOCKED_MSG = "The draw is locked. Reset it before making changes."

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


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "admin")


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; multi-statement writes use explicit BEGIN.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
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
    password: str


class ConfigUpdate(BaseModel):
    event_title: Optional[str] = None
    adult_draws_adult: Optional[int] = Field(default=None, ge=0, le=MAX_DRAW_QUOTA)
    adult_draws_child: Optional[int] = Field(default=None, ge=0, le=MAX_DRAW_QUOTA)
    child_draws_adult: Optional[int] = Field(default=None, ge=0, le=MAX_DRAW_QUOTA)
    child_draws_child: Optional[int] = Field(default=None, le=MAX_DRAW_QUOTA)
    budget_note: Optional[str] = None


class ParticipantCreate(BaseModel):
    name: str
    role: Literal["ADULT", "CHILD"]
    family_group: str
    wishlist: str = ""

    @field_validator("name", "family_group")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class WishlistUpdate(BaseModel):
    wishlist: str


# --------------------------------------------------------------------------- #
# App + auth
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Family Gift Exchange", lifespan=lifespan)


def require_admin(x_admin_password: Optional[str] = Header(default=None)) -> None:
    if x_admin_password is None or not hmac.compare_digest(
        str(x_admin_password), admin_password()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/admin/login")
def login(payload: LoginRequest) -> dict[str, str]:
    if not hmac.compare_digest(payload.password, admin_password()):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "OK"}


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


@app.get("/api/admin/state")
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


@app.put("/api/admin/config")
def update_config(
    payload: ConfigUpdate,
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
    return {"status": "OK", "config": _config_dict(_load_config(conn))}


@app.post("/api/admin/participants", status_code=201)
def add_participant(
    payload: ParticipantCreate,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    _reject_if_locked(_load_config(conn))
    cursor = conn.execute(
        "INSERT INTO participants (name, role, family_group, wishlist, token) "
        "VALUES (?, ?, ?, ?, ?)",
        (payload.name, payload.role, payload.family_group, payload.wishlist, str(uuid.uuid4())),
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


@app.delete("/api/admin/participants/{participant_id}")
def delete_participant(
    participant_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    _reject_if_locked(_load_config(conn))
    cursor = conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Participant not found")
    return {"status": "OK", "deleted": participant_id}


@app.post("/api/admin/draw")
def run_draw(
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    config_row = _load_config(conn)
    participants = [
        {"id": row["id"], "role": row["role"], "family_group": row["family_group"]}
        for row in conn.execute("SELECT id, role, family_group FROM participants ORDER BY id")
    ]
    if not participants:
        raise HTTPException(status_code=400, detail="Cannot run a draw with no participants")

    # Matching runs before any write, so a rejected draw cannot corrupt state.
    pairs = generate_draw(_config_dict(config_row), participants)

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
    return {"status": "OK", "assignments_created": len(pairs), "is_locked": 1}


@app.post("/api/admin/reset")
def reset_draw(
    conn: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM assignments")
        conn.execute("UPDATE participants SET viewed_at = NULL")
        conn.execute("UPDATE config SET is_locked = 0 WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
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


@app.get("/api/draw/{token}")
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

    if participant["viewed_at"] is None:
        conn.execute(
            "UPDATE participants SET viewed_at = ? WHERE id = ?",
            (utc_now(), participant["id"]),
        )

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


@app.put("/api/draw/{token}/wishlist")
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

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
