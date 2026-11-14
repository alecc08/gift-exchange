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

> **Superseded in part** by *Grouping: a household is not a surname* (addendum, 2026-11-13):
> `participants.group_key` and `config.avoid_same_group` were added, `family_group`'s comment above
> no longer holds (it is a display surname now), and an existing database is upgraded in place by
> `MIGRATIONS` in `main.py`.

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

> **Superseded in part** by *Grouping: a household is not a surname* (addendum, 2026-11-13): the
> second rule above works on `group_key` (not `family_group`) and is a **preference** rather than a
> rejection rule, the 422 message changed to talk about the draw counts, and `generate_draw` now
> returns `(pairs, crossed)`.

## 5. Backend API Specification (main.py)

### 5.1 Authentication

* Admin endpoints check HTTP header `X-Admin-Password` against
  `os.environ.get("ADMIN_PASSWORD", "admin")`. Return 401 Unauthorized on mismatch.
* Participant endpoints authenticate solely via the `{token}` path parameter.

### 5.2 Endpoints

> **Superseded in part** by the grouping addendum (2026-11-13): `participants` also carry
> `group_key`, `PUT /api/admin/config` also accepts `avoid_same_group`, and `POST /api/admin/draw`
> also returns `same_group_crossings`. The "never query the assignments table" rule now has exactly
> one exception — a single `COUNT` of same-group pairs, exposed as `config.same_group_crossings`.

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
key-symmetric (119 keys each) by `tests/test_i18n.py`.

**Register, revised 2026-11-13.** The first pass leaned on spoken Québécois to prove it was
Québécois, and it read as costume. The current rule is *ordinary Quebec French*: the words a
Quebecer actually uses in an interface, without the phonetic spelling or the caricature markers.
« pis » → « et », « toé/moé » → « toi/moi », « frette » → « verrouillé », « binômes » → « qui pige
qui », « liste de souhaits » → « liste d'idées », « astheure » → « maintenant », « capoté » →
« brisé / n'a pas fonctionné », « souhaite » → « idée », « pu » → « plus ». What stays Quebec and
plain: *piger*, *gâter*, *tuque*, *mitaines*, *raquette*, « correct », the `tu` address, and the
`fr-CA` date/currency formatting. The banned forms are a list in `tests/test_i18n.py`, because a
style rule that lives only in a human's head comes back the first time someone adds one string.

| Aspect | What changed |
|---|---|
| Register | Ordinary Quebec French, warm but not costumised (see the revision above): « Règles et quotas », « Les participants », « Les personnes que tu vas gâter », « Ouvre ton lien », « garde-le pour toi », « Ça s'en vient, repasse plus tard ! ». |
| Lock vocabulary | `state.locked` → « Tirage verrouillé », the draw button → « Lancer le tirage », reset → « Recommencer le tirage ». The top-bar control is `admin.signout` → « Fermer la session » / "Sign out": it calls `POST /api/admin/logout`, and it was briefly labelled « Lancer le tirage » during the rename, which put two different actions under one label — the capture tool clicked the wrong one, which is how it was caught. `tests/test_i18n.py` now refuses duplicate labels except an allowlist. |
| Softened errors | « Le tirage n'a pas fonctionné », « Ce n'est pas possible avec les familles et les quotas actuels », « Mot de passe pas correct. », « Réessaie dans {s} seconde(s). » |
| Removed Gallicisms | « Liste d'envies » → « liste d'idées »; « pull bleu marine » → « tuque en laine / patins / jeu de société »; « groupe familial » → « Famille »; household example « Martin » → « Tremblay ». |
| Locale | `locale()` returns `fr-CA` (and `en-CA`), so timestamps and the relative chip format as « il y a 5 minutes » with the 24-hour clock; `<html lang>` is `fr-CA`. |
| Currency | No amount field exists in the schema — money only lives in the free-text `budget_note`. Every default, placeholder and demo value now uses CAD: « Ex. 25 $ par personne, taxes incluses », demo note « 25 $ par personne, taxes incluses — les cadeaux maison, c'est correct », EN placeholder « e.g. $25 CAD each, taxes included ». `€` no longer appears anywhere in the repo. |
| Tooling | `tools/seed_demo.py` seeds a Quebec family (Tremblay / Gagnon / Bouchard / Roy, wishlists with tuques, mitaines, raquettes, chaussettes à orignaux); `tools/verify_acceptance.py` uses Quebec households (Tremblay, Gagnon, Bouchard, Roy, Ouellet) and a « 25 $ par personne » budget; `tests/test_api.py` fixtures use `25 $ maximum`. |
| Deliberate limit | Error copy stays explicit and unambiguous — « mais il y en a seulement {available} en dehors de sa famille » — with no phonetic spelling and no vulgarisms: an error message has to be readable by the person who is about to be locked out. |

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

## Link previews (addendum, 2026-11-13)

The organizer's deliverable is a URL, so the preview a shared URL produces is part of the product.
Added: `static/og-image.png` (1200×630), `static/favicon.svg`, `static/apple-touch-icon.png`,
`static/icon-512.png`, `static/site.webmanifest`, and an injected block of Open Graph + Twitter
card tags in the shell (`share_meta()` / `public_base_url()` in `main.py`). The old `/` handler was
a `FileResponse`; it is now an injected `HTMLResponse`, because only the request knows which host
the family was given.

Design decisions worth keeping:

| Decision | Why |
|---|---|
| One generic card for `/` and for `/?t=<uuid>` | Crawlers read this markup and a participant link is what gets shared. Nothing about a real family may ever be personalised into it — no name, no event title, no token. Asserted per test. |
| Absolute URLs built from the request | A relative `og:image` is the classic silently-blank preview, and the same image answers on a public domain, a LAN hostname, or an `ip:port`. |
| `X-Forwarded-Proto` honoured only with `TRUST_PROXY=1` | Otherwise the card advertises `http://` behind your own TLS proxy, and several crawlers drop the card or the HSTS upgrade breaks the family's links. Same trust gate as the rate limiters, on purpose. |
| A Host header that is not a host loses the URL tags | `public_base_url()` validates against `^[A-Za-z0-9.\-\[\]:]{1,253}$` and emits title/description only. There is no clean fallback: Starlette builds `request.url` from the same header, so the first draft "fell back" to the same hostile value and the test caught it. The values are also `html.escape`d, so the failure mode was a preview pointing at an attacker-chosen host — not attribute injection. |
| Static assets keep ETag/Last-Modified, the shell is `no-cache` | The shell differs per host; the PNG never changes. No explicit `Cache-Control` was pinned on `/static`: a year of freshness would need a fingerprinted filename to be safe, and revalidation against a byte-identical file is already cheap (verified: `If-None-Match` → `304`). |
| `mimetypes.add_type` for `.webmanifest` / `.svg` | A manifest served as `application/octet-stream` is a silently broken "add to home screen". |
| The card is generated, not hand-drawn | `tools/share-card.html` + `tools/app-icon.html` are the sources, rendered by Chromium through the same palette, snowflake geometry and web fonts as the app, so the preview cannot drift from the UI. `build_share_art.py --check` measures the rendered headline. |
| Square-safe layout is enforced | Facebook/WhatsApp/iMessage crop the wide card to the centre 630 px on phones. The build measures the ivory headline in the PNG (components grouped by row bands) and **fails** if a line leaves that band. |

### Verification — 2026-11-13

```
.venv/bin/pytest -q                      -> 66 passed   (17 new: 11 share metadata + 6 i18n)
python3 tools/build_share_art.py         -> og-image.png 1200x630 547 KB
                                            crop-safe: 2 headline line(s); widest 381 px at
                                            x=413..793 (mobile crops the middle 630 px: 285..915)
                                            icon-512.png 512x512, apple-touch-icon.png 180x180
GET /                                    -> '<meta property="og:image"
                                            content="http://testserver/static/og-image.png">'
GET / with Host: tirage-noel.alserv.org + X-Forwarded-Proto: https (TRUST_PROXY=1)
                                         -> content="https://tirage-noel.alserv.org/static/og-image.png"
GET / with Host: evil";x="1              -> og:image/og:url absent, og:title present, no "evil"
GET /?t=<uuid> share block == share block of /       (and the token is not in it)
GET /static/og-image.png                 -> 200 image/png, IHDR 1200x630, 547 KB < 1 MB
GET /static/site.webmanifest             -> application/manifest+json
GET /static/og-image.png If-None-Match   -> 304
i18n audit                               -> 119 fr / 119 en keys, symmetric, placeholders match,
                                            banned forms: none, duplicate labels: none
                                            (allowlist: admin.title == login.title)
docs gallery                             -> re-shot against the new copy (15 stills + 2 GIFs)
```

The rename that produced the duplicate-label test: `admin.lock` (top bar) calls
`POST /api/admin/logout`; during the plain-French pass it was labelled « Lancer le tirage », the
same text as the real draw button, and `tools/capture_docs_media.py` failed with a Playwright
strict-mode violation naming both buttons. Fixed by `admin.signout` → « Fermer la session » /
"Sign out", and `test_no_two_different_controls_share_a_label` now guards it.

## Grouping: a household is not a surname (addendum, 2026-11-13)

Reported from the real event: everyone was being classified by `family_group`, but the same surname
appears in several households, so the surname could not be the unit of the exclusion rule. Two
changes follow, and they are separable — one is data, one is policy.

**Data.** `participants.group_key` (label « Groupe ») is the constraint unit; `family_group` is
demoted to a display surname and is never read by the engine. `group_key` may be blank, meaning *in
no group*, and two blanks are not one group: `group_key_of()` hands anybody with no group a private
key (`#solo:<id>`), which makes the exclusion a no-op without a second code path — and is also what
prevents a self-pick when the rule is switched off.

**Policy.** §4.1's "no same-family draw" was an axiom; it is now a preference (`config.avoid_same_group`,
default on). The solver spends its budget on a draw with zero crossings and only relaxes when none
exists, so the failure mode that used to be a `422` becomes a draw plus an honest report.

| Decision | Why |
|---|---|
| Two fields rather than one renamed one | The surname is real information the organizer wants on the roster; overloading it is what broke. Nothing in the engine reads it any more, which `test_shared_surname_does_not_group_people` pins down. |
| Blank `group_key` is legal, blank `family_group` is not | "In no group" is a real case (a friend of the family). The surname stays required because it labels the row. Deliberately *not* validated by the same `_not_blank` validator, and commented as such. |
| Relaxation is a budget ladder, not a fallback pass | `_solve_attempt(..., same_budget=k)` counts crossings as it deals, so a relaxed draw uses exactly k. Starting at `_min_crossings()` (a real lower bound: `base·size` of a role's slots sit inside any group of size, and the demand inside that group can only be partly served from outside) stops the search wasting its whole attempt budget on k−1 impossible configurations. |
| Strict pass keeps the full attempt budget, relaxations get `max_attempts // 10` | Without that split the ladder multiplies the worst case, and a roster that needs a crossing would take minutes. |
| `_feasible()` keeps only the quota arithmetic | Same-group tightness is no longer a reason to answer 422. Keeping the old household supply/demand checks there would have kept the old failure. `test_impossible_without_a_crossing_relaxes_once_instead_of_failing` is the regression test for exactly that. |
| `eligible(role, group, allow_same)` returns outsiders *then* insiders | A relaxed draw stays as relaxed as its budget allows instead of scattering crossings through it. |
| The organizer learns a count, not a pairing | `/api/admin/state` touches `assignments` exactly once, as a `COUNT` of same-group pairs (`config.same_group_crossings`). Enough to admit the draw relaxed, not enough to name a pair; `test_acceptance_no_assignments_leak_to_admin` still forbids the words *assignment/giver/receiver/pairs* in the payload. |
| The frontend predicts the same number | `groupNotes()` is `_min_crossings()` ported line by line, including `#solo:<id>`. A prediction that turns out exact is what makes the notice credible instead of mystical. |
| `reported` is counted from the deal, not from the budget | `generate_draw` sums the pairs it actually returned. Reporting the budget instead would let the number drift from the assignments on disk the moment a solver found a cheaper deal than it was allowed. |
| The lower bound is checked against brute force | `tests/test_bounds.py` enumerates every derangement-style deal for six small rosters and asserts `bound ≤ true minimum ≤ what the solver used`. A bound nobody checks can silently stop being a bound, and the ladder would then start above the answer — promising a crossing that was never needed. |
| Only quota arithmetic disables *Lancer le tirage* | `warnings` (hard) and `groupNotes` (informational) are separate boxes; a relaxable rule must not grey out the button the organizer needs to click. |
| Migration backfills `group_key` from `family_group` | An existing database keeps drawing exactly as it did before; `tests/test_groups.py` builds the pre-change schema literally (`OLD_SCHEMA`) and asserts the upgrade, plus that running it twice is a no-op. |

### Verification — 2026-11-13 (grouping pass)

```
.venv/bin/pytest -q                      -> 90 passed   (24 new: 11 grouping + 7 brute-force
                                             bounds + 6 engine/i18n guards)
tools/verify_acceptance.py               -> ACCEPTANCE: PASS
  seeded 10 adults + 10 children in 6 households (two of them both surnamed Tremblay)
  30 assignments, 0 self/same-group pairings, children receive [2], adults receive [1],
  API same_group_crossings == DB count == 0, no assignment vocabulary in /api/admin/state
tools/check_organizer_ui.py              -> organizer console OK: no console errors, no page
                                             errors, no raw i18n keys. What it walks:
                                             checkbox off -> save -> reload -> still off, panel not
                                             falsely dirty (asAdminConfig coerces the 0/1 on BOTH
                                             the live and the saved copy); filter by
                                             « Roy (chez Gaétan) » -> 2 of 3 rows; tight roster ->
                                             note predicts 1 crossing, button stays enabled, draw
                                             locks, panel says « Le tirage a pigé 1 fois … »; add a
                                             person -> prediction disappears and the notice flips to
                                             « Personne a pigé dans son propre groupe. »; EN switch
                                             resolves every new label
i18n audit                               -> 131 fr / 131 en keys, symmetric, placeholders match,
                                            banned forms none, duplicate labels none; new guards:
                                            every t('key') in the markup exists in both
                                            dictionaries, and every detail.includes('…') fragment
                                            still matches a real server message (the old one looked
                                            for "unsolvable", which no longer appears anywhere)
docs gallery                             -> re-shot: 17 stills + 2 GIFs (05-family-filter →
                                            05-group-filter; new 16-group-note, 17-draw-relaxed),
                                            built from seed_demo's roster, which the capture tool
                                            now re-seeds so the gallery is reproducible from a
                                            bare database
```

## Security review of the public deployment (addendum, 2026-11-13)

The question asked was "can someone attack us through this site", so everything below is a
measurement of the live deployment, not a restatement of the threat table.

### What the internet can actually reach

`74.57.226.127` probed with TCP connects from `check-host.net` nodes (US/DE/NL and, for 443, 55
countries):

| Port | Result | Why |
|---|---|---|
| 443 | **open**, answers as `tirage-noel.alserv.org` only | the single OPNsense port forward → traefik `external` (:4443) |
| 4443 | closed/filtered | the forward targets the container's :4443 mapping, not the host's |
| 80, 22, 445, 3000, 8081, 8443, 9898 … | closed/filtered | OPNsense default deny on WAN; nothing else is forwarded |

On the `external` entrypoint exactly **one** router exists (`grep entrypoints=external` over the
stack: gift-exchange alone), and traefik answers `404 page not found` to any other `Host` — so the
other ~25 services on this host are not merely unlinked, they are unroutable from outside.
TLS: Let's Encrypt `CN=*.alserv.org`, valid to 2026-12-31.

### The client IP survives the edge (this is what makes the limits real)

A request from off-network appears in the container log as `request ip=136.124.33.3 …` — the
visitor's real address, not the firewall's. So the login limiter (5/min), the lockout, the
participant-link budget and the shared API budget are all **per visitor**, and a credential spray
from the internet gets 5 guesses a minute, not 5 per attacker. Traefik overwrites `X-Forwarded-For`
(measured earlier: a forged XFF is logged as the real peer), so the bucket cannot be widened by
spoofing a header.

That is also the reason **not** to add a traefik `rateLimit` middleware here yet: traefik's own
view of the client is correct, but a second limiter in front of the app's would double-count and,
because a family event is a burst of the same handful of links, would lock out grandmothers before
it locked out an attacker.

### One real defect found: the private links were in the log

`access_log_middleware` logged `request.url.path`, and the participant route *is*
`GET /api/draw/<uuid>`. Every link open therefore wrote that person's entire credential into the
container log — which on a deployed host means journald, readable by more people (and possibly more
shippers) than the directory holding the SQLite file:

```
INFO gift-exchange request ip=192.168.1.10 method=GET path=/api/draw/e3a1cdc7-… status=200 ms=3.0
```

Fixed by `redacted_path()`: UUID-shaped path segments become `<token>`, everything else is logged
as before (`/api/admin/state` keeps its shape, so the log stays useful). Two tests in
`test_security.py` — one drives the real routes and asserts the token string never appears in the
log, one asserts the redactor only touches token-shaped segments. Removing the fix makes the first
test fail, which is the point: `audit()`'s own docstring already promised "never logs passwords or
tokens" and nothing was checking it.

Not fixed, and worth knowing: journal lines written **before** the fix still contain the links that
were opened then. `journalctl --vacuum-time=1s` (or rotating the participants) is the cleanup, and
the links are only a credential to whoever can already read the host's logs.

### What is still true about the risk, in plain terms

* Cloudflare is DNS-only on this record: no WAF, no bot filtering, no DDoS scrubbing. The house IP
  is the thing that answers. Accepted trade for one family event.
* Anyone holding a link *is* that participant — no second factor. 122-bit UUIDv4 tokens, unknown
  tokens are an indistinguishable `404`, and the link endpoint has its own per-IP budget.
* The database is not encrypted at rest, and the organizer console is one password deep — which is
  why the console shows a banner while the password is the default and refuses everything when it is
  blank.
* The limiter is in-process memory: a restart resets it. Fine for a season, not for a permanent
  public service.

## Link emails via Resend (addendum, 2026-11-13)

**The ask.** Stop hand-copying links: give each participant an email field and mail them their own
link, themed like the site. Token lives in `.env` only. Ship as an unmerged PR.

**Decisions (organizer's answers + measured fallout).**
- From: `Tirage de Noël <tirage-noel@alserv.org>`, whole string in `RESEND_FROM`.
- One language per event (`config.email_language`, fr/en), not per participant.
- Explicit send buttons **plus** an opt-in `auto_email_on_draw` checkbox, default off.
- Free-text emails: no format rejection at save time; the send-time `EMAIL_RE` is the authority.
  Rationale: a parent mid-typing must not hit a 422; a doomed address must not burn an API call.
- Editing an email is the only roster edit allowed while locked, and it clears `email_sent_at`.
- API over SMTP: structured per-send errors, a message id for the audit trail, `RESEND_BASE_URL`
  stub hook. SMTP (`smtp.resend.com`) documented as the fallback in README only.

**Transport facts measured, not assumed.**
- Cloudflare in front of `api.resend.com` answers `403 / error code: 1010` to Python's default
  `Python-urllib` agent; a real `User-Agent` is mandatory (comment in `send_email`).
- The production key is a restricted **send-only** key: `GET /domains` → `401 restricted_api_key`.
  Domain state can only be learned by sending. Stored in `/opt/.env` on LXC 101 (600), never in git.
- Inline `<svg>` is stripped by Gmail; the themed art is `<img>`-referenced from `/static` (flake,
  gift strip). CID attachments work too through Resend (`content_id`) — used for the first probe.

**Deliverability, end to end.** First probe (pre-DMARC, plain template) → Gmail **spam**. Root
cause measured: no DMARC on `alserv.org` at all. Added via the Cloudflare token:
`_dmarc.alserv.org → v=DMARC1; p=none; sp=none` (relaxed alignment — Resend's MAIL FROM is the
`send.alserv.org` subdomain, so strict `aspf` would have broken alignment). Second probe, same
themed template, fresh Gmail account, DMARC live → **inbox**, confirmed by the organizer. Gmail
does not cache verdicts, but per-account spam-click feedback persists: the first account needs a
"Report not spam" + contact add.

**Verification.** 128 tests (`tests/test_emails.py`: migration of an old DB, free-text save,
locked-email-edit rule, per-row bulk report, stamp/clear lifecycle, auto-send ordering, escaping,
art URLs, transport headers incl. the User-Agent, key-never-logged). Headless console pass:
13/13 steps, zero console errors — column, free-text flag, single send, preview modal
(srcdoc iframe renders the themed HTML), bulk report, language persist, email editable while locked.

### Throttle (added after the first live probe)

Families share mailboxes — kids' links go to the parent — so the sending pace is a deliverability
feature, not just provider politeness. One process-wide gate, two layers: a global
`EMAIL_SEND_INTERVAL_MS` (1200 ms; Resend free tier 429s above ~1 req/s) and a per-address
`EMAIL_SAME_ADDRESS_GAP_S` (4 s) so four links landing at one inbox never arrive as a burst.
Bulk order interleaves addresses so the lanes share the wait instead of stacking sleeps, and a
provider 429 gets exactly one retry after `EMAIL_RETRY_AFTER_S` (3 s). Measured on the live build:
4 sends, 4 distinct addresses, elapsed 3.63 s = three 1200 ms spacings, zero failures.

### Link shape and subject (hotfix after the first real send, 2026-11-13)

The first batch of link emails on prod carried `https://tirage-noel.alserv.org/draw/<uuid>` — the
API's path shape, not the SPA's. `/draw/<uuid>` is not a route (the token endpoints live under
`/api/draw/…`), so twelve people got FastAPI's `{"detail":"Not Found"}`. Nothing in the suite had
ever *followed* a link out of a mail: the test asserted the link's text, and the text was wrong.
Fixed in three parts: one builder (`participant_link()`) for the console, the mail and the preview;
`GET /draw/<uuid>` → 302 → `/?t=<uuid>` so the links already sitting in inboxes work without a
resend; and a test that extracts the href from the mail body and requires `200 text/html`. Measured
on prod after the deploy: the old URL bounces and lands, the new URL is served directly.

Subject order changed from `{event} — ton lien secret` to `{name} — ton lien secret · {event}`. The
event title on prod is 53 characters, which pushed the whole purpose of the mail past Gmail's list
view truncation and left six identical subjects at one shared mailbox. Name first is the only thing
a mailbox sorts and scans by; the event goes last so it is the part that gets cut.

---

## Multiple draws, a console that fits a phone, and a gift in the mail (addendum, 2026-11-13)

**The ask.** « L'admin peut créer MULTIPLE tirages, pas juste un seul… la vue admin permet
d'ouvrir un tirage précis, ou d'en créer un nouveau. Ça veut dire un écran de plus qui liste tous
les tirages avec leur statut. » And: « la UI est trop chargée et pas vraiment responsive — « pas
encore ouvert », « lien envoyé il y a … »… il faut que ça se voie sur mobile; là je pense que ça
cache juste le surplus quand la page est trop étroite. » Bundled into one branch, verified in a
browser, shipped as an unmerged PR.

**Data model.** `config` (one row, one event) becomes `draws` (one row per event) and
`participants.draw_id` (`NOT NULL DEFAULT 1`). Two columns come with it: `created_at`, and
`drawn_at` — NULL on a draw that was locked before the column existed, which the UI renders as
« Tiré » with no invented date.

*Rebuild, not rename.* The original table was `id INTEGER PRIMARY KEY CHECK (id = 1)`. An
`ALTER TABLE config RENAME TO draws` carries that CHECK along, and then creating a second draw —
the entire point of the change — fails with a constraint error on the live box. The migration
therefore creates `draws_new` without the check, copies column by column (so any vintage of legacy
table works: `avoid_same_group` may not exist yet), drops `config`, renames. Ids survive, so
`draw_id = 1` is true by construction for the running deployment. `created_at`/`drawn_at` are left
to `apply_migrations`, and the default draw of a fresh install is seeded *after* the migrations —
seeded inside `SCHEMA` it would run before those columns exist on an upgraded database. The index on
`participants(draw_id)` is created after the migrations for the same reason.

**The API contract.** Every `/api/admin/*` call except the draws list and the login pair takes
`?draw_id=`. Omitted, it means "the only draw there is" — the contract the older client and the
whole pre-multi-draw suite are written against, so nothing existing broke. With two draws and no id
the server refuses to guess (`400 No draw selected`); unknown id `404`; empty server `400 There is
no draw yet`. Participant ids are global, so `_participant_row()` scopes the *write* paths too —
without it a console pointed at one draw could edit, mail or delete a person from another by id.
The participant link never carries a draw id: the token resolves to its own row, hence its own draw.

`copy_participants_from` copies people (name, household, wishlist, address) with **fresh tokens** and
no `viewed_at`/`email_sent_at`: a link that already revealed last year's pairing must not silently
become this year's credential, and two draws sharing a token would break the one property the URL
relies on — whoever holds a token holds exactly one draw. Deleting a **locked** draw is refused
rather than forced; relatives are holding links that reveal it, so the sequence is reset-then-delete.

**Rehearsed on a copy of production before deploying.** 12 tokens byte-identical, 18 pairings intact,
the old participant links still answer `200 READY` with the right event title, draw #2 creatable
(the `CHECK (id = 1)` trap avoided), zero token overlap between a draw and its copy, a cross-draw
delete refused `404`, and the locked draw refusing its own deletion.

**The list screen.** « Choisis un tirage »: one card per draw — status, and the counts an organizer
actually scans (people / links sent / links opened). Counts only: `assignments` is touched as a
`COUNT`, never read, so the list cannot leak a pairing. Opening a draw writes `?draw=` into the URL
so a reload or a bookmark comes back to the same event; a bookmark to a draw that no longer exists
falls back to the list instead of showing a console pointed at nothing.

**Responsive.** There were no breakpoints at all: at phone width the roster's seven columns
overflowed their container and the chips (« Pas encore ouvert », « Lien envoyé il y a 2 h ») ran into
the neighbouring cell. Now: ≤900 px the roster is one card per person with each field labelled by its
own column name (`td[data-label]`, same strings as the headers, so they translate); ≤640 px 40 px
button targets and 16 px inputs (below that Mobile Safari zooms on focus and does not give the zoom
back); ≤480 px the chips keep the fact and drop the *when*, which stays in the `title`. Quota table,
preview modal and toasts reflow.

**Mail art.** A big gift between the header and the copy: box shut, lid jumped off, four mittened
paws holding it open, two eyes looking up at it, motion arcs and escaped sparkles — the mail's whole
promise (the pairing is already in the box, only you can open it) in one picture. Static on the
organizer's explicit call (« keep it non-animated, let's play it safe »): Outlook ignores
`@keyframes`, Gmail strips them, and art that freezes in half the family's inboxes is worse than a
still frame. Localized `alt`, because for a screen reader that image *is* the pitch. A guard test
asserts neither the mail nor the SVG carries `@keyframes`/`<animate>` — checked on the parsed SVG
tree, because the art's own comment contains the word "animation" to explain why there is none.

**Two bugs only a browser could catch**, both now static guards (`tests/test_frontend.py`):
- `drawStatus()` called `formatDate(...)` bare. Markup may do that (Alpine puts the component on its
  scope chain); JavaScript inside the component may not — Alpine swallowed the ReferenceError and the
  status chip of every locked draw rendered empty.
- The error strip lived inside the console block, so a refused action on the *list* — deleting a
  locked draw — came back as a 400 the organizer could only read in the devtools console. It now
  sits above both screens, and a guard asserts exactly one copy, before both.

The i18n label guard paid for itself too: with a second « Supprimer » (delete a draw) the English
pair `draws.delete` / `roster.delete` both read "Delete", so the roster's became **Remove** — two
unrelated controls sharing a label is what broke the docs capture tool last time.

**Verification.** 165 tests, incl. `tests/test_draws.py` (21: separation, copy semantics, per-draw
mail and reset, cross-draw write refusal, delete rules, and the upgrade of a locked live-shaped
database) and `tests/test_frontend.py` (Alpine `this.` scope, draw-scoped fetch coverage, the error
strip's placement). `tools/check_organizer_ui.py` now walks login → list → console → second draw →
refused locked delete → delete → **390 px and 360 px with no element outside the viewport and no
horizontal scroll**, and passes clean: no console errors, no page errors, no raw i18n keys. Overflow
probed by measurement (1440/1024/900/768/430/390/360 on the list, the console and the participant
reveal), email art checked at 620 px and 390 px.

## One email per mailbox (addendum, 2026-11-13)

**The ask.** "At most one email per email address is sent… in a family where 3 are linked to the
same email, let's simply have the 3 links clearly identified in the email, as well as the email
title along with the draw's name: *Pige Adultes, Alec, Gabrielle voici vos liens*. Maybe this should
be its own template since it might be hard to adapt the single-recipient wording." Both halves of
that sentence turned out to be the right call, and for the same reason: singular copy bent into a
plural shape reads like a bug, and a family that gets three near-identical mails in one minute reads
it as abuse — the receiver's spam filter and the tired parent both.

**Grouping lives in exactly one place.** `_group_by_address()` bundles participants by the address
they gave, in first-seen roster order, and *every* send path (bulk, per-row, auto-on-draw) goes
through it. That is the whole design: a rule remembered separately by three callers is a rule that
drifts. A blank or unsendable address stays a group of one, so the report can still name the uncle
who was skipped instead of hiding him inside a neighbour's row.

Two older pieces were deleted rather than kept as insurance:

- `_interleave_by_address()` — the previous mitigation (spread the repeats out). Grouping makes the
  repeat impossible, so a scheduler is now dead weight.
- `deliver_link_email()` — one person, one mail. `deliver_household_email()` replaces it and stamps
  `email_sent_at` on the whole group: two relatives "mailed" and a third showing « Jamais envoyé »
  is the failure mode this had to avoid. `ready` (drawn yet?) is passed in as an argument instead of
  being re-read from a settings row, because with several draws on one server there is no single row
  to read.

**A second template, on the same shell.** `build_email()` (one link) and `build_links_email()`
(household) both render `_email_shell()` + `_email_assets()`, so the palette, the art and the footer
cannot drift. The wording is a separate set of strings — `hello_many`, `title_many_*`, `body_many_*`,
`links_note`, `link_label`, `link_button`, `privacy_many`, `subject_many(_pending)`, `and`,
`and_more` — because « ton lien » in a mail holding three links is simply untrue, and conditional
`{% if n > 1 %}` sprinkles would have made the template unreadable. Each link is labelled
(« Voir le lien de Alec ») so nobody has to guess which button is theirs, and the mail still never
contains a pairing or an outsider's name.

**Subject.** The ingredients the user asked for — the people, the words « vos liens », the draw name
— in the order the singular subject already used in production: `Alec, Gabrielle et Hugo — vos liens
· Pige Adultes`. Kept consistent rather than reordered, because a family that already saw
« Ghislaine — ton lien secret · Réveillon chez Gaétan » should recognize the pattern, and the event
title last is the part Gmail truncates. Past `MAX_SUBJECT_NAMES` (3) the list becomes
« A, B, C et 2 autre(s) » so « vos liens » never scrolls off a phone's list view — which is the whole
reason the subject names people.

**The console has to say it before the click.** A chip next to « Envoyer les liens par courriel »
counts « 3 personne(s) · 1 boîte(s) · 1 courriel(s) »; each row's « Envoyer » tooltip explains that
the address already receives N links; and a row's send now sends the *household* (otherwise three
clicks on three relatives is three mails anyway). The report answers per person — so a skipped
address is still named — plus `messages`, the number of mails that really left, and `with: [names]`
on a single send. The rate limit counts sends, not people: twelve relatives at five mailboxes cost
five provider calls.

**Transport detail found on the way.** Cloudflare in front of Resend answers `403 / error code: 1010`
to Python's default `Python-urllib/3.12` agent — measured against the live API: `Python-urllib`
→ 403 (blocked at the edge, the key never inspected), a browser UA → `401 API key is invalid`
(Resend itself answering), the old app-string UA → also 401. The default `User-Agent` is now an
ordinary browser string, still overridable with `RESEND_USER_AGENT`.

**Verification.** 175 tests. `tests/test_emails.py` gained: one mail per mailbox carrying every link
and no outsider's link, the single-row send grouping the household, two sends to one mailbox staying
spaced, household names escaped, the same art in both templates with no animation, the subject
joining names as a sentence and cutting at 3, the household mail in **both** languages, the preview
`links` parameter (and its 422), and auto-send-on-draw obeying the grouping (3 people at one
address → `messages == 2`, all three stamped). `tests/test_frontend.py` guards that the rule is
visible in the UI: the row tooltip, the mailbox counter, the multi-link preview button, and a bulk
report that still says how many mails left. `tools/check_organizer_ui.py` passes clean on the new
build, and the live send from production is checked against a real inbox: one mail, three labelled
links, the names in the subject.

## The mail's art is a PNG now (addendum, 2026-11-13)

**The symptom.** The first household test mail arrived complete — right subject, three labelled links
— and every picture in it was blank. Two separate causes were in that one mail, and only one of them
is about the art format.

**What was actually measured:**

| Check | Result |
| --- | --- |
| The three URLs in the first mail | `http://127.0.0.1:8000/static/…` — **wrong host**, because that send was scripted from inside the container and the app built its links from the request's `Host` header |
| Whether Gmail could fetch those | no. A loopback address is unreachable from Google's proxies, so the art was blank *and* every link in the mail was dead for the family |
| Re-sent through `https://tirage-noel.alserv.org` | art renders, links resolve |
| And the format? | Gmail serves every image through its own proxy, and that proxy **does not serve SVG** — documented by Google (no plans to support it) and by cani.email ("partially supported, non-Google only"). The SVG art would have failed too, on any host |

The README had claimed the proxy renders SVG — an assumption that was never tested. Both fixes went
in, and the rule now lives in a test rather than in prose: **art that has to appear in an email is a
raster, and a URL that leaves by email must be one the receiver can open.**

**The art.** The SVGs are still the source of truth — they are the files that get edited, and the
site's own vectors match them. `tools/build_email_art.py` (Chromium, like the other tools in that
directory) rasterizes the three the mail loads at **2x the displayed size**, transparent background,
and the mail references the `.png`. The `<img width height>` attributes are kept, so the intrinsic
pixels are exactly 2x the box the mail declares — sharp on a phone, and no mail client resampling.
All three together are 49 KiB, well under the threshold where Gmail starts dropping images.

**The link origin (`PUBLIC_BASE_URL`), which is the part that actually hurt.** Any URL in a mail —
the participant's link and the art — is built from one base, and it used to be read off the request.
Correct when the organizer clicks in a browser; wrong in exactly the way above when the call is
scripted inside the container, a health check, or a port-forward. A mail whose every link is dead on
arrival is worse than an error on the organizer's screen, so:

- `PUBLIC_BASE_URL` set → it wins outright for everything that leaves by mail, and it is the
  documented answer for any deployment behind a proxy. (Share tags deliberately keep following the
  request's own `Host`: a crawler wants the host it visited, and those are validated against
  injection separately.)
- otherwise the request's origin, **refused when it is loopback**: `503` naming the variable, the
  per-row send answering the same, and the auto-send-on-draw listing every person as skipped with
  `EMAIL_BAD_BASE_URL` while the draw itself still commits;
- the console translates that 503 into its own message instead of the admin-password one. A latent
  ordering bug in `errorText()` was fixed on the way: it answered *every* 503 with « admin password
  not configured » before reading the detail, which had been masking the email feature's own 503 all
  along — a guard now pins the order.

**One header that was quietly wrong too.** Every response carried
`Cross-Origin-Resource-Policy: same-origin`. That is *not* what Gmail broke — it re-serves images
from `googleusercontent.com`, so the origin's CORP never reaches the browser — but it does break the
clients that fetch an email's art directly (Apple Mail and friends), and the browser answers
`ERR_BLOCKED_BY_RESPONSE.NotSameOrigin`, which looks like a broken image and is a header. `/static/*`
now sends `cross-origin`; everything else keeps `same-origin`, because a participant's data only ever
leaves through the API and there is nothing personal in the art directory.

**Guards.** No `.svg` may appear in the mail HTML; the three PNGs must exist, start with the PNG
signature, and match 2x the `width`/`height` the template declares; `/static/*` must answer
`cross-origin` while `/`, the API and an error page keep `same-origin`; a send whose base would be
loopback is refused and mails nothing, `PUBLIC_BASE_URL` overrides the `Host` header for both the
link and the art, and the auto-send still commits the draw while reporting the skip per person. 181
tests.

## Mailed URLs cannot be loopback (addendum, 2026-11-13)

Follow-up to the blank-images report, and the part that actually bit: the test sends were scripted
**from inside the container**, so their `Host` was `127.0.0.1:8000`, and `public_base_url()` copied
that into every URL the app writes — the participant's link and the mail's art. The mail looked
perfect and was unusable: no external proxy can fetch a loopback address, and the family clicking a
link would have landed on nothing. (This is also why the PNG change could not be judged from that
first inbox: that mail was already unreachable. The SVG limitation stands on its own evidence —
Google's own statements and cani.email — and the PNG mail sent through the real hostname did render.)

`mail_base_url()` is now the one function the send paths ask, and it is deliberately narrower than
`public_base_url()`:

1. `PUBLIC_BASE_URL` wins outright when set. That is the honest answer for anything behind a proxy,
   and it also makes the share tags immune to a chosen `Host` header.
2. Otherwise the request's own origin — refused when the host is loopback (`localhost`, `127.x`,
   `::1`, `0.0.0.0`), logged as `mail_base_url_refused`, because a mail whose links are dead on
   arrival is worse than an error on the organizer's screen.
3. The refusal is uniform: bulk and per-row answer `503` naming the variable, and auto-send-on-draw
   lists each person as skipped with `EMAIL_BAD_BASE_URL` — the draw itself still commits, because a
   mail problem must never roll a draw back.

Two smaller things found on the way, both now guarded: the SPA answered **every** 503 with « admin
password not configured » before reading the detail, so the email feature's own 503s (`not
configured on this server`, and now `public address`) were unreachable — a guard pins the order. And
the i18n parity check reads one entry per line, so a wrapped entry was invisible to every test; a new
guard counts the raw `'<key>':` starts against the parsed keys. 182 tests.

## The gallery was two releases behind (addendum, 2026-11-13)

`docs/screenshots/` stopped at 17 while the console had grown a draw list, a phone roster and a whole
mail panel. `tools/capture_docs_media.py` was already shooting `18-draw-list.png` and
`19-roster-phone.png` — those two JPEGs had simply never been committed. Rebuilding the gallery from
a bare database surfaced six more things, all of them about the tool rather than the design:

* **The run started from whatever the instance held**, so the stills described whatever roster
  happened to be in the demo DB. `seed_demo.seed()` now runs at the start *and* at the end.
* **The demo roster had no email addresses at all**, so the mail panel, the mailbox counter and the
  roster's address column could not be shown. The seed carries one address per household now, on the
  RFC 2606 reserved `example.com` — a demo left configured cannot mail a real person by accident.
* **Signing in lands on the draw list** and two stages of the tool still waited for the console
  right after Enter (`tools/check_organizer_ui.py` had been fixed for this, the capture had not).
* **A second draw on screen turns every unqualified `/api/admin/*` call into `400 No draw
  selected`** — the list still needs two draws to be worth looking at, so the tool creates one
  *after* the snapshot stage, sets its quotas (the copy carries people, not rules — drawing a fresh
  copy is refused), and removes it before the stages that mean "the only draw". `cleanup_extra_draws`
  makes the run restartable after an interrupted one.
* **`19-roster-phone.png` was a 14 000 px full-page strip** shipping at 660 KB, and its scroll was
  taken before the resize, so it showed the rules panel instead of the cards. Viewport-sized, phone
  width, scrolled to the first `td[data-label]` — the element that makes the cards cards.
* **The mail preview modal only grows as far as its iframe** (`min-h`, not `h`), so the still cut off
  under the gift art and the labelled links — the whole point of the household template — were not in
  the frame.

One real app bug surfaced on the way, and it was the same one as the addendum above, hiding in a
different place: **the preview endpoint resolved its origin with `public_base_url(request)` while the
send uses `mail_base_url(request)`**, so an organizer looking at the preview from inside the container
saw `127.0.0.1` links while the mail itself would have carried the real host. A preview that shows
one URL and sends another is not evidence — and that is exactly how the loopback bug was debugged in
the first place. It now goes through `mail_base_url()` and a test pins the two together.

**Guard:** `tests/test_docs.py` keeps the gallery honest in both directions — every image the README
shows exists, every file in `docs/` is shown by a README row (that is how `18`/`19` went unnoticed:
the README cannot reference what it does not know about), and no raw PNG capture is committed. The
gallery is 21 stills + 2 GIFs. 186 tests.

## The README had drifted into two languages (addendum, 2026-11-13)

Somewhere along the mail work the README stopped being one language. Captions and bullets quoted the
French UI in the middle of English sentences — 46 guillemets, a bilingual H1 — and a reader who does
not read French could not follow the feature description, while a reader who does could not tell
whether the app is French-only. None of those quotations were evidence of anything: the sentence
around them already said what the feature does, and the French was decoration inside it.

**The rule now: the README is English.** Where it names a control it names the **English** label
(`Generate & lock draw`, `Send`, `Email the links`, `Multi-link preview`, `Pick a draw`, `Not drawn
yet`, `Open Alec's link`), which is verifiable against `I18N.en` rather than being a paraphrase. The
**screenshots stay French**, because French is the locale the app boots in — so the README says that
out loud above the gallery instead of leaving the reader to reconcile a French screen with English
captions.

**What replaced the deleted French** is a `Languages: French and English` section, i.e. the language
support moved from being implied by stray quotations to being a claim with its receipts: `fr-CA` is the
default and English is one click away (`localStorage: ge_lang`); the mail has its **own** language
switch (`config.email_language`, Panel 1), so an organizer who works in English still sends a Québec
family a French mail; 193 keys per dictionary with identical `{placeholders}`; `EMAIL_STRINGS` as the
mail's parallel dictionary; `locale()` answering `fr-CA`/`en-CA` for dates, the 24-hour clock and the
relative chips, and setting `<html lang>`; the banned-French-forms list with the reason for each; the
label-collision guard. Plus one gap stated instead of hidden: `SHARE_TITLE` is fixed French in both
locales, which is cosmetic because the card carries no name, no event title and no token.

**Deliberately not touched:** the French quotations in `PLAN.md`. This file is the record of copy
decisions — what a string used to say, what it says now, why a form is banned — so quoting the French
*is* the content here, and paraphrasing it would cost the log its evidence. Code, tests and strings
unchanged: 186 tests, all the same keys.

## The console moves off the site root (addendum, 2026-11-14)

The bug was ordinary: the app had one URL, `/`, and that URL was the **organizer console**. A relative
who typed the hostname from the email — or who was sent it by a family group — landed on a password
form they had no business seeing, and every bookmark, dashboard card and screenshot pointed at the
same page the family was meant to read. Nothing was leaked; the console was always behind the
password, the login budget and the lockout. What was wrong was the front door.

**The shape now.** One bundle, three screens, one server-stamped flag:

| Path | Screen | Notes |
|---|---|---|
| `/` | the family's page | landing card, or one person's own page with `?t=<uuid>` |
| `/admin` | the organizer console | `ADMIN_PATH` moves it; `X-Robots-Tag: noindex, nofollow`; no share tags |
| anything else, GET | the family's page | a mistyped address gets an explanation instead of `{"detail":"Not Found"}` |

`spa_shell()` replaces two markers in the same `INDEX_HTML`: the share block (family pages only) and
`route_tag()`, a boolean — `window.__GE_ORGANIZER__=true|false`. The SPA reads it with `=== true`, so a
document with no flag opens the family's page, never the console. Nothing on the family's page names
the console path, and `tests/test_pages.py` fails if it ever does: every `/admin` in the bundle must be
preceded by `/api`, and `'/?draw=` / `replaceState({}, '', '/')` are banned outright, because those two
were how a signed-in organizer would have been ejected onto the landing card mid-session.

**`ADMIN_PATH` is validated, not trusted.** `normalize_organizer_path()` corrects the obvious
(`admin` → `/admin`, `/Admin/` → `/Admin`, `/a/./b` → `/a/b`) and refuses what it cannot make safe: a
non-ASCII segment, a space, a percent escape, a 200-character segment, `/api`, `/static`,
`/openapi.json`. A refusal falls back to `/admin` with one audit line rather than refusing to boot —
a gift exchange that will not start is worse than a console at the expected path. It is read **at
import**, because that is when routes are registered; the change needs a restart, and
`test_the_console_route_is_registered_and_the_catch_all_is_last` pins both that the route table
follows the value and that the catch-all is registered last (registered earlier it would answer
`/api/*`).

**The `/draw/<uuid>` shim is gone.** That route existed for one mistake: the first real mailing built
links from the API's path shape, twelve people got `{"detail":"Not Found"}`, and a 302 to `/?t=<uuid>`
was the repair. The links were re-sent from the one builder (`participant_link()`), the console's copy
button uses the same shape, and a redirect whose only customer is an address nobody mails any more is
a route nobody maintains — it also needed its own rate limit, its own UUID-shape check and its own log
redaction to stay honest. What replaces it is the honest behaviour: the path is not an endpoint, so it
renders the landing card, which tells the family where their real link is. It never reads a token out
of a path segment, and a test asserts the token appears nowhere in the response. Two tests that pinned
the 302 were deleted with it; `test_the_link_shape_the_mail_carries_is_the_shape_the_spa_reads` stays,
because that is the one holding the console and the mail template together.

**Tidying done on the way** (behaviour unchanged unless noted):

- `_draw_lock` + `BEGIN IMMEDIATE` + `try/COMMIT/except: ROLLBACK` was copy-pasted three times
  (`run_draw`, `reset_draw`, `delete_draw`), and the 6-line "delete this draw's assignments" statement
  with it. Now one `draw_transaction()` context manager and one `_clear_assignments()`.
- The admin config whitelist was a second copy of the rule columns. `EDITABLE_CONFIG_FIELDS` is now
  derived from `DRAW_RULE_COLUMNS` (minus `is_locked`), so a new column cannot quietly become writable
  or stay unwritable because somebody forgot a list. The two `UPDATE … SET col = ?` writers share
  `_update_columns()`, and the participant writer keeps its `AND draw_id = ?` scope.
- `update_participant` computed the same `model_dump` twice in two branches that produced identical
  dicts; one computation, and clearing the mail stamp is now a column in the same UPDATE
  (`email_sent_at = None`) instead of string surgery on the clause list.
- `admin_state` re-implemented `_all_participants`; now it calls it.
- Dead code removed: `LimitsOut` (a model never attached to the route it described),
  `DRAW_TITLE_REQUIRED` (the validator raises `ValueError` instead), `with_email` and `shared_with` in
  the API payloads (no consumer in the SPA or the suite — and `shared_with`'s docstring claimed the
  console showed it, which it never did), the SPA's `wishlistSaved` state and its write into
  `data.participant` (nothing rendered either), and one no-op CSS rule.
- Unbounded process memory: `RateLimiter.prune()` existed and was never called (now called from
  `hit()` past 4096 windows), and `_email_gate_at["by_address"]` grew one entry per address forever
  (now capped at 512, cleared when it reaches the cap — an hour-old gap is no gap).
- The family's hostname was hard-coded twice — CSP `img-src` and the mail preview's fallback origin —
  where a host change would silence the preview's art while the mail kept working. One
  `KNOWN_PUBLIC_ORIGIN`, and a test that ties the CSP entry to the URL the preview actually emits.
- `sessionStorage.getItem('admin_pw')` was read in two places and never written in any: the browser
  now sends only the bearer token. The bare `X-Admin-Password` header still works for curl and the
  tools — it is the API's, not the SPA's.
- SPA plumbing that had drifted into three copies: one `participantUrl()` (was inlining
  `${window.location.origin}/?t=` twice), one `flashFlag()` for the copied-flags' timer pairs, one
  `tallyBy()` for the household and surname chips, one `addressKey()` for the mailbox grouping
  (mirroring `_group_by_address`), one `parseDate()` behind both formatters, and `enterAdmin()` reading
  its own query string instead of taking a parsed one from each caller.
- Comments that had gone wrong rather than merely stale: `list_draws` claimed it touched `assignments`
  as a COUNT (it never queries that table), the email section header claimed every knob is read per
  call (the throttle sizes are import-time), `DRAW_RULE_COLUMNS` said three places had to agree (it is
  four), and `participant_link()` described `/draw/<uuid>` as a route that answers something.

**Verification.** 223 tests, up from 186: `tests/test_pages.py` (35 cases — the two shells, the
fail-closed flag, the catch-all leaving `/api` and `/static` alone, and `ADMIN_PATH` normalization),
the CSP/preview-origin test, and the rewritten stale-link test. `tools/check_organizer_ui.py` passes
clean against a live instance at `/admin` (login → draw list → rules → roster → group filter →
predicted crossing → second draw → delete → 390 px → EN, no console errors, no raw keys), and a
Playwright pass over the family's page confirms: root, `/Reveillon` and a stale `/draw/<uuid>` all
render the landing card with no `#pw` anywhere and the token never echoed; the card switches to
English; `/?t=<uuid>` still opens the participant page; and the card does not overflow a 390 px
viewport. New gallery still `22-family-landing.jpg`; the rest of the gallery is unchanged because no
console screen moved visually.

**Deployment consequence, stated before it surprises anyone:** organizer bookmarks and dashboard cards
pointing at the bare hostname now land on the family's page. Point them at `/admin`. Participant links
are untouched — the token was always a query parameter — so nobody in an inbox is affected, which is
the one thing that had to stay true.
