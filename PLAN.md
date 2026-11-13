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

## 8. UI & i18n requirements (addendum)

Requested after the original spec, and implemented in `static/index.html`:

1. **Modern, elegant design, zero build steps.** Still Tailwind CDN + Alpine.js CDN only.
   Design language: Fraunces (display serif) + Inter (UI) from Google Fonts, a pine / holly-red /
   gold palette, `rounded-3xl` cards on a warm ivory background with soft radial glows, small
   uppercase section kickers ("Panel 1/2/3"), pill chips for role and status, and one accent
   illustration per panel. No component library, no bundler.
2. **Christmas artwork, vendored inline (no runtime CDN dependency for art).**
   A crystalline 6-fold snowflake built from first principles (arm path + five rotations) is used
   for the falling-snow ambience, the empty-roster hint and the decorative corner motifs; festive
   outline icons (Christmas tree, gift, candy, star, bauble, link, refresh, check, alert) come
   from Tabler Icons (MIT) and are inlined in one SVG sprite. The hero gift box is a bespoke SVG
   with separately transformable `box` / `lid` / `steam` groups so it can be animated.
3. **The reveal effect.** The participant view never dumps the assignments: it shows the wrapped
   gift plus a single uppercase **REVEAL** button (`#reveal-btn`, sheen on hover). Clicking it
   lifts and tilts the lid, fades in a gold halo and steam wisps, bursts 14 snowflakes outward
   from the box, adds an extra flurry of falling snow, and staggers the recipient cards in
   (`animation-delay: index * 120ms`). Everything is CSS-only and collapses under
   `prefers-reduced-motion: reduce`.
4. **French by default, English on request.** Every string lives in an `I18N.fr` / `I18N.en`
   table; `lang="fr-CA"` is the default, the FR/EN segmented control in the header switches both
   views, the choice persists in `localStorage`, and `<html lang>` follows. Dates use
   `Intl.DateTimeFormat` with `fr-CA` / `en-CA` (see the Québécois addendum below). Backend
   messages stay exactly as specified
   (English `detail` strings); the frontend maps the four known failure details to the current
   language so the organizer's error banner is never half-translated.

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
11. **Numeric inputs use `max="10"`** per spec, and the backend rejects draw values outside
    0–10 with HTTP 422 (Pydantic validates the lower bound explicitly: a bare `ge` is dropped
    on `Optional[int]` in Pydantic 2.14), so a hand-crafted request cannot create an absurd
    roster.

## Security hardening (addendum)

Everything in §5 keeps its contract (same routes, same `X-Admin-Password` header, same status
codes); the additions below only narrow what an attacker or a mistake can do.

| Threat | Counter-measure |
|---|---|
| Password guessing on `POST /api/admin/login` | Fixed-window, per-IP rate limit (default 10/min) returning **429 + `Retry-After`**, and a temporary lockout after repeated failures. The client shows the remaining wait instead of a bare "wrong password". |
| Token guessing on the public `GET /api/draw/{token}` | Per-IP budget (default 120/min, separate from the admin budget); over budget → 429. Tokens are UUIDv4 and 404s are indistinguishable. |
| Volume abuse / oversized payloads | One shared budget for the remaining endpoints (default 300/min), and a request-body cap (default 16 KB) → 413. |
| Password riding on every admin request | `POST /api/admin/login` also issues a short-lived HMAC-SHA256 session token (12 h default, signed with `SESSION_SECRET`, else PBKDF2 over the admin password). Admin routes accept either the spec header or `Authorization: Bearer`; `POST /api/admin/logout` revokes server-side. The frontend stores the **token**, not the password. |
| Referrer leakage of `/?t=<uuid>` | `Referrer-Policy: no-referrer`, `<meta name="referrer" content="no-referrer">`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and a CSP that pins the CDNs. |
| CDN supply-chain XSS | CSP restricts scripts to `cdn.tailwindcss.com` / `cdn.jsdelivr.net` (Alpine compiles expressions, hence `unsafe-eval`), styles to the same origins plus inline (Tailwind Play + Alpine `:style` bindings), with `base-uri 'self'`, `form-action 'none'`, `object-src 'none'`. |
| Unbounded text stored in the DB | Pydantic length caps: name 80, family group 60, wishlist 1000, event title 120, budget note 500. The frontend mirrors them with live counters so the user never hits a 422 blind. |
| Two concurrent draws / torn write | Draws and resets are serialised by a process lock and each runs inside one SQLite transaction; a rejected draw writes nothing. |
| Accidentally re-drawing a locked event (invalidating links people already opened) | `POST /api/admin/draw` on a locked event → **409** ("reset first"), and the organizer must confirm the lock in the UI. |
| Blind debugging | Requests are logged (method, path, status, latency, client IP, bucket) and login/draw/reset/logout events are logged without secrets. Client IP only honours `X-Forwarded-For` when `TRUST_PROXY=1` (behind Traefik). |
| Default password in production | Startup logs a warning when `ADMIN_PASSWORD` is unset or equals the spec default `admin`; the UI shows the same warning to the organizer. |

Deliberate non-changes, so the trade-offs are on record: no CSRF tokens (there are no cookies —
auth is a header/bearer value, and `Origin` checks plus CSP `form-action 'none'` already block
cross-site form posts); no TLS inside the container (published on a LAN, terminate it in Traefik);
tokens are **not** rotated by `POST /api/admin/reset`, because that would invalidate links the
organizer already printed — reset wipes assignments and viewed markers only.

## UX polish (addendum)

The follow-up request was "improve UX and tighten backend security". The API contract from §5 is
untouched; everything below is additive and lives in `static/index.html` plus `GET /api/limits`.

| Surface | Change |
|---|---|
| Feedback | Toast stack (`#toasts`, `aria-live="polite"`) for saves, copies, draw results and rate limits; the inline notice/banner pair was replaced by one persistent, dismissible `adminError` block so a failure is never lost when the view re-renders. |
| Login | Busy state ("Vérification…"), disabled until a password is typed, and the server's `429`/lockout is shown as the remaining wait instead of a bare "wrong password". |
| Rules | Live "total gifts to prepare" row per target group; Save disabled until something actually changed (`rulesDirty` chip); character counters on title/budget; quota `max` read from the server. |
| Feasibility | The engine's necessary conditions are mirrored client-side (`warnings`), so an impossible quota is explained **before** the draw is attempted, and "Generate" is blocked with « Régle les alertes en haut avant ». The server still validates independently. |
| Draw | `confirm()` before locking (states the gift count and that it locks the rules), `409` handled as "already locked", and the locked panel counts opened links (`3/10 lien(s) ouvert(s)`). |
| Roster | Family-group `datalist` + clickable household filter chips with counts; per-row « {n} cadeau(s) à acheter » / « tire pas de cadeau »; wishlist preview truncated with `title`; relative « Consulté · il y a 5 minutes » (`fr-CA`, full timestamp in `title`); empty-roster guidance; **Copy all links** (name, role, household, URL) with a fallback for insecure origins. |
| Participant | PENDING page checks again automatically (25 s) and offers a manual « Vérifier encore »; a toast announces the switch to READY; the reveal is remembered per link in `sessionStorage` so a refresh doesn't replay the surprise; "Copier ma liste"; wishlist autosave (1.2 s debounce) with a « Pas enregistré » chip and Ctrl/⌘+Enter to save now. |
| Limits | `GET /api/limits` publishes the Pydantic caps; the SPA binds `maxlength` and counters to them, and surfaces `default_password: true` as a visible organizer warning. |
| Auth UX | The browser keeps the **session token** (`sessionStorage.ge_admin_token`) rather than the password; a `401` anywhere clears it and drops back to the gate with an "expired" toast. |
| i18n | Every new string exists in FR and EN (the two tables stay key-symmetric; audited by script on each pass). FR remains the default. |

## Québécois French and CAD (addendum)

The FR table was rewritten in **Quebec French** (`fr-CA`), not metropolitan French, and all money
is expressed in **Canadian dollars**. Keys are unchanged, and the FR/EN tables are audited to stay
key-symmetric (118 keys each).

| Aspect | What changed |
|---|---|
| Register | Warm spoken Québécois for the friendly copy: « Règles pis quotas », « Le monde participant », « Le monde que tu vas gâter », « Ouvre donc ça », « même toé », « Garde-le pour toé », « Ça s'en vient, repasse tantôt ! ». |
| Lock vocabulary | `state.locked` → « Tirage fretté », the header button → « Fretter », « Générer pis fretter le tirage ». (« fretté » is what a Quebecer says for a locked file/account.) |
| Softened errors | « Le tirage a capoté », « Là, c'est pas faisable avec les familles pis les quotas d'astheure », « Mot de passe pas correct. », « Réessa dans {s} seconde(s). » |
| Removed Gallicisms | « Liste d'envies » → « Liste de souhaits »; « pull bleu marine » → « tuque en laine / patins / jeu de société »; « groupe familial » → « Famille »; household example « Martin » → « Tremblay ». |
| Locale | `locale()` returns `fr-CA` (and `en-CA`), so timestamps and the relative chip format as « il y a 5 minutes » with the 24-hour clock; `<html lang>` is `fr-CA`. |
| Currency | No amount field exists in the schema — money only lives in the free-text `budget_note`. Every default, placeholder and demo value now uses CAD: « Ex. 25 $ par personne, taxes incluses », demo note « 25 $ par personne, taxes incluses — les cadeaux maison, c'est correct », EN placeholder « e.g. $25 CAD each, taxes included ». `€` no longer appears anywhere in the repo. |
| Tooling | `tools/seed_demo.py` seeds a Quebec family (Tremblay / Gagnon / Bouchard / Roy, wishlists with tuques, mitaines, raquettes, chaussettes à orignaux); `tools/verify_acceptance.py` uses Quebec households (Tremblay, Gagnon, Bouchard, Roy, Ouellet) and a « 25 $ par personne » budget; `tests/test_api.py` fixtures use `25 $ maximum`. |
| Deliberate limit | The technical/warning copy stays explicit (« mais y en a juste {available} en dehors de sa propre famille ») rather than full joual: an error message still has to be unambiguous, and no vulgarisms are used. |

## Verification log

Run on 2026-11-12 against the committed code.

### Automated — `bash run_tests.sh`

49 tests, all passing (Python 3.12 venv, `fastapi==0.115.0`, `uvicorn==0.30.6`, `httpx` +
`pytest` installed only by the runner).

* `tests/test_engine.py` — acceptance scenario 1 (exactly 30 rows, 3 per adult, every child
  received exactly twice, zero same-`family_group` pairings) across 12 seeds; balanced receiver
  quotas for four other quota sets; case/whitespace-insensitive household matching;
  seed determinism; zero-quota roster returns `[]`; `HTTPException(400)` for an empty target
  group; `HTTPException(422)` with the exact spec message for two unsolvable configurations;
  a maximal two-household case (each of 3 adults draws 2 adults) solved for 6 seeds.
* `tests/test_api.py` — login and `X-Admin-Password` 401s; default config row; quota range
  validation; blank name / bad role rejection; **criterion 2** (no `assignment`/`giver`/
  `receiver`/`pairs` substring anywhere in `GET /api/admin/state` after a 30-row draw);
  DB-level check of the 30 rows (`0` same-family or self pairings, every child received twice,
  every adult gave 3, no child gave); **criterion 4** (`400` for config PUT, participant POST
  and participant DELETE while locked); **criterion 3** (`viewed_at` `null` → timestamp after
  `GET /api/draw/<uuid>`, visible in admin state, `PENDING` before the draw, stable assignments
  across reloads, `[]` for a zero-quota child, `404` for a bad token); wishlist editable through
  the token after locking; reset clearing assignments, `viewed_at` and `is_locked` while the
  roster survives; `400` draw with an empty roster, `422` on an unsolvable roster leaving
  `is_locked = 0` and no rows written.
* `tests/test_security.py` — login rate limit + lockout (`429` with `Retry-After`, correct
  password refused during the lockout), lockouts scoped per client, a forged `X-Forwarded-For`
  cannot buy extra budget unless `TRUST_PROXY=1`, the participant-link budget is separate from
  the admin one, session token issue → use → tamper → revoke (`POST /api/admin/logout`), tokens
  signed per `SESSION_SECRET`, expired sessions rejected, the spec's `X-Admin-Password` header
  still accepted, `413` over `MAX_BODY_BYTES`, `422` on each field-length cap (name, family
  group, wishlist, event title, budget note, participant-wishlist), the security header set
  (CSP with `frame-ancestors 'none'` / `base-uri 'self'`, `nosniff`, `X-Frame-Options`,
  `Referrer-Policy`), non-leaky error bodies, and a `409` re-draw leaving `assignments`
  byte-identical.

### Live instance — `python tools/verify_acceptance.py`

The tool now seeds criterion 1's scenario itself (10 adults + 10 children in 5 households),
locks the draw, and then checks all four criteria against the HTTP API **and** the SQLite file,
so the run is reproducible. Against the Docker container on `127.0.0.1:8085`:

```
seeded acceptance scenario: 10 adults + 10 children in 5 family groups -> 30 assignments
1) assignments rows            : 30
   self / same-family pairings : 0
   ADULT receives        : [1]      CHILD receives : [2]
   ADULT gives           : [0, 3]   CHILD gives  : [0]
2) GET /api/admin/state        : 200 keys: ['config', 'participants']
   leaked assignment vocabulary: none
3) GET /api/draw/<uuid> (Nicolas): 200 READY with 3 assignments
   viewed_at                   : None -> 2026-11-12T22:25:52+00:00
4) while locked (PUT/POST/DELETE): 400 400 400

ACCEPTANCE: PASS        (exit code 0)
```

### Visual — Playwright against `http://127.0.0.1:8085` (screenshots in `screenshots/`, git-ignored)

| # | What was checked | Result |
|---|---|---|
| 01–02 | French login card by default; wrong password shows "Mot de passe incorrect." and stays on the gate | OK |
| 03 | Admin dashboard in FR: three panels, 2×2 quota grid defaults 1/2/0/0, snowfall rendering | OK |
| 04 | Roster built through the UI form + 19 API adds: 20 rows, role chips, "Non ouvert" status, Copy link / Supprimer | OK |
| 05 | EN switch: every label, chip, status, summary and forecast translated; choice persisted | OK |
| 06 | Participant PENDING: "Bonjour Camille", "le tirage n'est pas encore finalisé. Reviens bientôt !", wishlist editor + "Enregistré" feedback | OK |
| 07 | After "Générer et verrouiller le tirage": banner "Tirage généré et verrouillé", 9 inputs disabled, delete buttons hidden, "Réinitialiser le tirage" shown | OK |
| 08–10 | Participant READY: wrapped gift + "RÉVÉLER MES CADEAUX"; reveal lifts the lid, gold halo, 14-snowflake burst, steam, then 3 staggered recipient cards ("Les cadeaux que tu offres") with role badges and "SES ENVIES" | OK |
| 11–12 | Zero-quota child: "Tu n'as aucun cadeau à offrir cette année — prépare-toi à recevoir les tiens !" and in EN the exact spec sentence | OK |
| 13 | Organizer sees the green "Consulté" badge with a French-formatted timestamp on the opened link only | OK |
| 14 | Impossible quota (adult draws 9 adults across 5 households): red banner "Le tirage a échoué — Les contraintes sont impossibles à satisfaire…" and the event stays "Tirage ouvert" | OK |

Also confirmed in the browser: clipboard "Lien copié !" feedback, `confirm()` dialog before
reset (FR copy), and that no organizer screen ever renders pairings.

### Container check

`docker build` + `docker compose up -d` (image `python:3.12-slim`, host port 8085, `./data`
mounted) was re-audited with `tools/verify_acceptance.py` → `ACCEPTANCE: PASS`
(30 rows, 0 same-family pairings, no leak in admin state, `viewed_at` flips, `400 400 400`
while locked), and the container-served page was driven through Playwright (FR participant view,
reveal animation, 3 recipient cards).

### Re-verification after the Québécois / CAD pass — 2026-11-12

* `bash run_tests.sh` → **49 passed** (currency fixture is now `25 $ maximum`).
* `python tools/verify_acceptance.py` against the rebuilt container → **ACCEPTANCE: PASS**, with
  the Quebec households seeded by the tool (Tremblay, Gagnon, Bouchard, Roy, Ouellet):
  `30` assignments, `0` self/same-family pairings, `ADULT receives [1]`, `CHILD receives [2]`,
  `ADULT gives [0, 3]`, `CHILD gives [0]`, no assignment vocabulary in `GET /api/admin/state`,
  `viewed_at: None -> 2026-11-13T00:02:15+00:00`, and `400 400 400` while locked.
* i18n audit script: FR and EN tables are key-symmetric (118 keys each), every `t('…')` used in
  the markup exists in both, and no `€` byte remains in the repository.
* Playwright re-shoot of the whole gallery (15 stills + the two GIFs) against the container, so
  the README media shows the Québécois copy — header « TEMPS DES FÊTES », « Règles pis quotas »,
  « Le monde participant », « Fretter », participant card « Bonjour Mario » /
  « Le monde que tu vas gâter » / « Ses souhaits » with « 25 $ par personne, taxes incluses ».
* Repeated wrong password in the browser now reads « Mot de passe pas correct. » and, once the
  limiter trips, « Trop de demandes. Réessa dans 300 seconde(s). » (previously the raw
  `Unauthorized` detail). Mobile (390 px) still has no horizontal overflow:
  `documentElement.scrollWidth == window.innerWidth == 390` on both the participant and the
  organizer views.

### Hardened deployment verified — 2026-11-13

`docker-compose.hardened.yml` + `.env` (generated `ADMIN_PASSWORD`, generated `SESSION_SECRET`)
was actually brought up and inspected, not just written:

```
docker compose -f docker-compose.hardened.yml up -d
GET  /api/health           -> {"status":"ok"}
GET  /api/limits           -> "default_password": false
POST /api/admin/login {"password":"admin"}                -> 401
POST /api/admin/login {"password":"<the .env passphrase>"} -> 200
docker inspect             -> CapDrop=[ALL], SecurityOpt=[no-new-privileges:true],
                              ReadonlyRootfs=true, PidsLimit=128, published on 127.0.0.1:8085
inside the container       -> id -u = 1000;  touch /app/hack.txt -> Read-only file system
ADMIN_PASSWORD=<pw> python3 tools/verify_acceptance.py     -> ACCEPTANCE: PASS
ADMIN_PASSWORD=<pw> python3 tools/seed_demo.py --draw      -> 18 assignments, is_locked 1
```

The read-only-filesystem requirement is why the file runs as uid `1000` and documents the
one-time `chown -R 1000:1000 data`: with every capability dropped, the process needs to own the
only writable path (`./data`, where SQLite keeps its WAL sidecars). `.env` is git-ignored;
`.env.example` carries the documented defaults and the optional tuning knobs.

### Pre-publication review — 2026-11-13 (two real defects, found by testing the deploy path)

The app was about to be published on a public hostname, so the failure modes of a *misconfigured*
deployment were probed directly instead of only the configured ones. Two of them were real:

1. **`ADMIN_PASSWORD=""` authenticated the empty string.** `os.environ.get("ADMIN_PASSWORD",
   "admin")` returns `""` when the variable is set-but-empty — which is exactly what
   `ADMIN_PASSWORD: ${SOME_NAME}` does in Compose when the name is missing from `.env` — and
   `hmac.compare_digest("", "")` is `True`. Measured on a live instance before the fix:
   `POST /api/admin/login {"password":""}` → **200**. Fixed by making a blank value mean
   "not configured": `admin_password()` returns `""`, `require_admin` and `login` answer `503`
   before any comparison, and `using_default_password()` treats blank like the default so the
   banner, the log line and `/api/limits.default_password` all say so. The `503` also covers the
   bearer path: with a blank password *and* no `SESSION_SECRET`, the signing key is
   `pbkdf2_hmac(sha256, b"", DB_PATH, 50_000)` — a value an attacker can compute, since the image
   ships a fixed `DB_PATH`. Regression test:
   `tests/test_security.py::test_blank_admin_password_disables_the_organizer_console`.
2. **The audit trail never reached the log.** `logging.basicConfig` lived inside
   `if __name__ == "__main__"`, so under the image's `CMD ["uvicorn", "main:app", …]` the
   `gift-exchange` logger had no handler and the root logger's last-resort handler (WARNING)
   dropped every INFO line: the startup password warning, `login_failed`, `draw_locked`, the
   per-request lines. `_configure_logging()` now attaches a stderr handler at import. The image's
   CMD also gained `--no-access-log`, because uvicorn's own access line records the *socket*
   address — the proxy behind Traefik — while the app's line records the forwarded client address
   that the rate limiters actually key on.

Verified after the fix:

```
pytest -q                            -> 51 passed
ADMIN_PASSWORD="" uvicorn main:app -> POST /api/admin/login {"password":""}  503
                                      GET /api/admin/state (X-Admin-Password: "")  503
                                      GET /api/limits  "default_password": true
docker compose logs                  -> INFO gift-exchange login_failed ip=172.19.0.1
                                         INFO gift-exchange request ip=… status=401 ms=4.4
```

Also measured on the way, and worth keeping straight for anyone wiring this behind Traefik:
`HEAD /api/health` answers **405** (FastAPI adds no HEAD to a `@app.get` route in this
configuration), so a monitoring probe has to fall back to `GET`; and with `TRUST_PROXY=1` the
limiters key on the first `X-Forwarded-For` entry, which is why the app must never be reachable
without the proxy in front.
