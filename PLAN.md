# Agent Implementation Specification: Configurable Family Gift Exchange App

This is the main plan for the project. It is the specification the implementation is
verified against, plus an explicit **Implementation notes & deviations** section at the
bottom listing every place where the implementation intentionally differs from or
tightens the original spec.

---

## 1. Objective & Scope

Build a self-contained, single-page web application backed by a Python FastAPI server and
a local SQLite database. The application orchestrates a Secret Santa gift exchange with
configurable participant types (ADULT, CHILD), household exclusions, and customizable
draw quotas per participant type. Each active participant receives a unique UUID URL to
view their assigned recipients without revealing the global draw to the organizer.

## 2. File Structure & Deliverables

```
gift-exchange/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py
└── static/
    └── index.html
```

* `requirements.txt`: Pin `fastapi==0.115.0` and `uvicorn==0.30.6` (use Python's built-in
  `sqlite3` and `uuid` modules; do not add ORM dependencies).
* `Dockerfile`: Use `python:3.12-slim`, install requirements, copy `main.py` and `static/`,
  expose port 8000, and run `uvicorn main:app --host 0.0.0.0 --port 8000`.
* `docker-compose.yml`: Map host port `8085:8000`, mount `./data:/app/data`, and define
  environment variables `ADMIN_PASSWORD` and `DB_PATH=/app/data/exchange.db`.

## 3. Database Schema (SQLite)

Initialize the database automatically on FastAPI startup (`@app.on_event("startup")` or
lifespan) if tables do not exist. Enable `PRAGMA foreign_keys = ON;` and
`PRAGMA journal_mode = WAL;`.

```sql
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    is_locked INTEGER NOT NULL DEFAULT 0 -- 0 = unlocked/editable, 1 = drawn/locked
);

INSERT OR IGNORE INTO config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL, -- Participants sharing this exact string cannot draw each other
    wishlist TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL, -- UUIDv4 generated on creation
    viewed_at TEXT DEFAULT NULL -- ISO-8601 timestamp set on first GET /api/draw/{token}
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    FOREIGN KEY (giver_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES participants(id) ON DELETE CASCADE,
    UNIQUE(giver_id, receiver_id) -- Prevents a giver from drawing the exact same person twice
);
```

## 4. Constraint-Satisfaction Matching Engine (main.py)

Implement `generate_draw(config, participants)` using a randomized slot-allocation and
backtracking/retry loop (`max_attempts = 20000`).

### 4.1 Algorithmic Rules

* **Supply Calculation:**
  * Let A be participants where `role == 'ADULT'` (|A| = N_A) and C be participants where
    `role == 'CHILD'` (|C| = N_C).
  * Total gifts needed for Adults: `T_A = (N_A * adult_draws_adult) + (N_C * child_draws_adult)`.
  * Total gifts needed for Children: `T_C = (N_A * adult_draws_child) + (N_C * child_draws_child)`.
  * If `T_A > 0` and `N_A == 0`, or `T_C > 0` and `N_C == 0`, raise HTTP 400
    ("Cannot assign gifts to an empty target group").
* **Balanced Receiver Pool Construction:**
  * Distribute `T_A` slots evenly across all members of A: compute `base, rem = divmod(T_A, N_A)`.
    Shuffle A; the first `rem` adults receive `base + 1` incoming gifts, and the rest receive
    `base` incoming gifts. Flatten into `adult_receiver_pool`.
  * Repeat identically for `T_C` across C to create `child_receiver_pool`.
* **Validation Constraints per Attempt:**
  * Shuffle `adult_receiver_pool` and `child_receiver_pool`.
  * Deal the required number of Adult receivers and Child receivers to each participant `p`
    based on their role quotas.
  * Reject the attempt immediately if any assignment (giver, receiver) violates any of these rules:
    * `giver.id == receiver.id` (no self-draw).
    * `giver.family_group.strip().lower() == receiver.family_group.strip().lower()`
      (no spouse/parent/sibling draw).
    * Duplicate `receiver.id` for the same `giver.id` (a giver never buys 2 gifts for the same recipient).
* **Failure Handling:** If 20,000 attempts fail to find a valid permutation, return HTTP 422
  with message: `"Constraints are mathematically unsolvable with current family groups and draw counts."`

## 5. Backend API Specification (main.py)

### 5.1 Authentication

* Admin endpoints check HTTP header `X-Admin-Password` against
  `os.environ.get("ADMIN_PASSWORD", "admin")`. Return 401 Unauthorized on mismatch.
* Participant endpoints authenticate solely via the `{token}` path parameter.

### 5.2 Endpoints

* `GET /`: Serve `static/index.html`.
* `POST /api/admin/login`: Body `{"password": "..."}`. Verifies against `ADMIN_PASSWORD` and
  returns 200 OK or 401.
* `GET /api/admin/state`: (Requires Admin Auth)
  * Returns config row and all participants (including `id`, `name`, `role`, `family_group`,
    `wishlist`, `token`, `viewed_at`).
  * **CRITICAL:** Never query or return rows from the `assignments` table in any admin endpoint
    so the organizer remains blind to the draw results.
* `PUT /api/admin/config`: (Requires Admin Auth; fails with 400 if `is_locked == 1`)
  * Updates `event_title`, `adult_draws_adult`, `adult_draws_child`, `child_draws_adult`,
    `child_draws_child`, and `budget_note`.
* `POST /api/admin/participants`: (Requires Admin Auth; fails with 400 if `is_locked == 1`)
  * Body: `{"name": str, "role": "ADULT"|"CHILD", "family_group": str, "wishlist": str}`.
  * Generates `token = str(uuid.uuid4())` and inserts row.
* `DELETE /api/admin/participants/{id}`: (Requires Admin Auth; fails with 400 if `is_locked == 1`)
  * Deletes participant by id.
* `POST /api/admin/draw`: (Requires Admin Auth)
  * Runs `generate_draw()`, wraps `DELETE FROM assignments`, bulk `INSERT INTO assignments`,
    and `UPDATE config SET is_locked = 1` in a single atomic SQLite transaction.
* `POST /api/admin/reset`: (Requires Admin Auth)
  * Executes `DELETE FROM assignments`, `UPDATE participants SET viewed_at = NULL`, and
    `UPDATE config SET is_locked = 0` in a single transaction.
* `GET /api/draw/{token}`: (Public via UUID)
  * Looks up participant by token (returns 404 if invalid).
  * If `config.is_locked == 0`, returns `{"status": "PENDING", "participant": ..., "event_title": ...}`.
  * If `config.is_locked == 1`:
    * If `viewed_at IS NULL`, updates `viewed_at` to current UTC ISO timestamp.
    * Queries assignments joined with participants for `giver_id == participant.id`.
    * Returns `{"status": "READY", "event_title": ..., "budget_note": ...,
      "participant": {"name", "role", "wishlist"},
      "assignments": [{"name", "role", "wishlist"}, ...]}` (note: if a participant's draw
      quota is 0, `assignments` is an empty list `[]`).
* `PUT /api/draw/{token}/wishlist`: (Public via UUID)
  * Body: `{"wishlist": str}`. Updates wishlist for the participant matching token (allows
    updating gift ideas even after the draw is locked).

## 6. Single-Page Frontend Requirements (static/index.html)

Use Tailwind CSS CDN (`https://cdn.tailwindcss.com`) and Alpine.js CDN
(`https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js`) with zero build steps.

### 6.1 Routing & View Switching (Client-Side)

Inspect `window.location.search` on load:

* **If `?t=<uuid>` is present → Participant View:**
  * Fetch `GET /api/draw/<uuid>`.
  * State PENDING: Show a clean message: "Hi {name}, the draw hasn't been finalized yet.
    Check back soon!" plus an input box to save their own wishlist notes
    (`PUT /api/draw/{token}/wishlist`).
  * State READY: Display the `event_title`, `budget_note`, and a card for each assigned
    recipient showing their Name, a badge for Adult or Child, and their Wishlist notes. If
    `assignments.length === 0` (e.g., a child whose draw count is set to 0), display "You
    don't need to buy any gifts this year—just get ready to receive yours!" Include an
    editable box at the bottom so the user can update their own wishlist live.
* **If `?t=<uuid>` is NOT present → Admin View:**
  * Auth Gate: If `sessionStorage.getItem('admin_pw')` is missing or rejected with 401,
    display a centered password login card. On submit, validate via `POST /api/admin/login`,
    save password to `sessionStorage`, and load admin state.
  * **Panel 1 — Rules & Draw Quotas:**
    * Inputs for Event Title and Budget Note.
    * 2x2 numeric grid (`min="0"`, `max="10"`) for draw rules:
      * Each Adult draws: `[ X ]` Adults and `[ Y ]` Children
      * Each Child draws: `[ X ]` Adults and `[ Y ]` Children
    * Disable inputs when `is_locked == 1`.
  * **Panel 2 — Participant Roster:**
    * Form to add a person: Name, Role (ADULT / CHILD), Family Group (with helper text:
      "People in the same family group will never draw each other"), and optional Wishlist.
    * Table listing all added participants with columns: Name, Role, Family Group, Status
      (green "Viewed" badge with timestamp if `viewed_at` is set, gray "Unopened" otherwise),
      a "Copy Link" button that copies `${window.location.origin}/?t=${p.token}` to clipboard
      with visual "Copied!" feedback (only enabled/shown for participants where draws > 0 or
      shown for all with a note), and a Delete button (hidden when locked).
  * **Panel 3 — Execution Controls:**
    * When `is_locked == 0`: Primary "Generate & Lock Draw" button (`POST /api/admin/draw`).
      Display any 400/422 validation error banner clearly if constraints cannot be satisfied.
    * When `is_locked == 1`: Danger "Reset Draw" button with a `confirm()` dialog
      (`POST /api/admin/reset`).

## 7. Acceptance Criteria for Verification

Before completing the task, verify the following scenarios:

1. Setting Adult draws: 1 Adult, 2 Children and Child draws: 0 Adults, 0 Children with 10
   adults and 10 children across 5 family groups generates exactly 30 rows in `assignments`
   (3 per adult, 0 per child), with every child appearing as a `receiver_id` exactly twice
   and zero same-`family_group` pairings.
2. Calling `GET /api/admin/state` returns no assignment mappings anywhere in the JSON payload.
3. Opening `/?t=<adult_uuid>` transitions that adult's `viewed_at` field from `null` to a
   valid timestamp in the admin dashboard.
4. Attempting to add/delete participants or change draw quotas while `is_locked == 1` returns
   HTTP 400.

---

## Implementation notes & deviations

Deliberate changes to the spec, with reasons. Everything not listed here follows the spec
literally.

1. **Extra files: `tests/`, `run_tests.sh`, `.gitignore`, `README.md`.** The spec's tree only
   lists runtime files, but the task also requires unit tests. Tests live in `tests/` and are
   excluded from the Docker image. No runtime file layout was changed.
2. **Lifespan instead of `@app.on_event("startup")`.** `on_event` is deprecated in
   FastAPI 0.115 (emits `DeprecationWarning`); the spec explicitly allows `lifespan`.
3. **Testing dependencies.** `httpx` (FastAPI's `TestClient` requirement) and `pytest` are
   installed only by `run_tests.sh` into a throwaway virtualenv, keeping `requirements.txt`
   exactly as pinned: `fastapi==0.115.0`, `uvicorn==0.30.6`.
4. **Deterministic, seedable engine API.** `generate_draw(config, participants,
   max_attempts=20000, seed=None)`. The spec's signature is kept as the public surface, and a
   seed is added so tests can prove the engine is correct rather than merely lucky.
5. **Backtracking inside an attempt.** The spec describes "randomized slot-allocation and
   backtracking/retry loop". Pure reject-and-restart over the whole deal (as 4.1 literally
   reads) is exponential when family groups are large: with 10 adults in 5 groups of 2 and
   3 draws per giver, the first-attempt success rate is only a few percent. The
   implementation deals slot-by-slot with eligibility filtering and bounded backtracking,
   then retries the whole allocation up to `max_attempts` times. Constraints checked are
   exactly the three in 4.1.
6. **Oversubscribed receiver slots.** A receiver slot handed to an ineligible giver triggers
   backtracking (the slot is returned to the pool and offered to a different giver) instead
   of always rejecting the whole attempt. The **balanced pool from 4.1 is never changed**, so
   the invariant verified by acceptance criterion 1 — every child receives exactly
   `T_C / N_C` gifts — is preserved exactly.
7. **Early feasibility check.** Before the retry loop, cheap necessary conditions are checked
   (per-giver eligible-receiver count, and per-family-group supply/demand balance). If they
   fail, the endpoint returns the same HTTP 422 with the spec's exact message immediately,
   instead of burning 20,000 attempts. This is an optimization of failure handling, not a
   change of contract.
8. **Reset also clears stale lock state on empty rosters.** `POST /api/admin/draw` returns
   HTTP 400 when there are no participants, which the spec leaves implicit (drawing an empty
   roster would silently "lock" nothing).
9. **Frontend uses `fetch` with a small shared helper** and no build step, exactly as
   specified. Clipboard copy falls back to a hidden textarea + `document.execCommand('copy')`
   because `navigator.clipboard` is unavailable on non-secure (plain-HTTP, non-localhost)
   origins — the compose file publishes the app on port 8085, where the async clipboard API
   is blocked.
10. **Copy Link is shown for every participant**, with a muted "no gifts assigned" note when
    that participant's draw quota is 0 — the spec allows either variant ("only enabled/shown
    for participants where draws > 0 **or** shown for all with a note"). This keeps links
    copyable for participants who still need to submit a wishlist.
11. **Numeric inputs use `max="10"`** per spec, and the backend clamps/rejects draw values
    outside 0–10 with HTTP 422 (FastAPI validation) so a hand-crafted request cannot create
    an absurd roster.

## Verification log

Executed on 2026-11-12 (see `tests/` for the automated part):

* Unit/integration suite: `bash run_tests.sh` — 22 tests, all passing. Covers the matching
  engine invariants (exact 30 rows, each child received exactly twice, zero same-family
  pairings, no self-draws, no duplicate receivers, determinism under a fixed seed,
  unsolvable-config handling), auth (401 on wrong password), lock enforcement (400 while
  locked), blind-organizer guarantee (no assignment data in admin state), token flow,
  `viewed_at` transition, and wishlist updates after lock.
* Live app in Docker (`docker compose up`, host port 8085) driven with Playwright: admin
  login, roster entry, 2x2 quota grid, copy-link feedback, participant PENDING view,
  READY view with recipient cards and badges, locked-state input disabling, reset flow.
