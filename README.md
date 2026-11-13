# Family Gift Exchange · Échange de cadeaux

Configurable Secret Santa for a big family: role-based draw quotas (adult / child), household
exclusions, one private UUID link per person, and an organizer who **never sees the pairings**.

FastAPI + SQLite backend, single-page frontend (Tailwind CDN + Alpine.js CDN, no build step).
UI is **Québec French by default** (`fr-CA`) with a one-click FR/EN switch, and every amount is
Canadian dollars.

Spec and the list of deliberate deviations: [`PLAN.md`](PLAN.md).

## See it in motion

| The gift-box reveal (lid, snowflake burst, cards fading in) | Organizer: unlock → panels → copy every link |
|---|---|
| ![Gift-box reveal animation](docs/media/reveal.gif) | ![Organizer flow: unlock, snowfall, clipboard toast](docs/media/organizer-flow.gif) |

Snowfall, the reveal and the burst are pure CSS/SVG — no animation library, and everything is
disabled under `prefers-reduced-motion`.

## Quick start

```bash
ADMIN_PASSWORD=change-me docker compose up -d      # http://localhost:8085
python tools/seed_demo.py                          # optional: a 10-person demo family
```

* `docker-compose.yml` publishes `8085:8000`, mounts `./data` for the SQLite file, and sets
  `ADMIN_PASSWORD` and `DB_PATH=/app/data/exchange.db`.
* The database schema is created automatically on startup; nothing to migrate.
* `tools/seed_demo.py --draw` also runs and locks the draw, which is the fastest way to look at
  the participant view.

Local development instead:

```bash
bash run_tests.sh                                   # venv + deps + full suite
ADMIN_PASSWORD=admin DB_PATH=$PWD/data/exchange.db \
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8085 --reload
```

## How an exchange runs

1. **Panel 1 — Rules & quotas.** Event title, budget note, and the 2×2 grid: each adult draws
   _X_ adults / _Y_ children, each child draws _X_ adults / _Y_ children. A live totals row shows
   how many gifts that implies.
2. **Panel 2 — Roster.** Add people with a role and a family group. Two people in the same group
   (compared case/whitespace-insensitively) can never draw each other. Family chips let you filter
   the table; **Copy all links** puts every private link on the clipboard in one click.
3. **Panel 3 — Generate & lock.** The matching engine deals the gifts and locks the event.
   Statuses show only whether a link was opened (« Consulté · il y a 5 minutes » /
   « Pas encore ouvert »), never who buys what.
4. Each person opens their own link, unwraps the gift card, and can edit their wishlist at any
   time — it autosaves, even after the draw is locked.
5. **Reset draw** wipes the pairings and the opened-markers and reopens editing.

## The organizer console

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/02-admin-rules.jpg" alt="Rules and quotas panel with live totals and character counters"><br><b>Rules &amp; quotas</b> — live gift totals, character counters, and a warning while the admin password is still the default.</td>
    <td width="50%"><img src="docs/screenshots/03-warnings.jpg" alt="Warning that the draw cannot succeed with these quotas"><br><b>Feasibility warnings</b> — the same rules the engine uses are checked while you type, so an impossible quota is explained instead of failing after a click.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/04-roster.jpg" alt="Roster with family chips and per-person gift counts"><br><b>Roster</b> — per-person gift count, household chips with counts, wishlist preview.</td>
    <td><img src="docs/screenshots/06-toast-clipboard.jpg" alt="Toast confirming all links were copied"><br><b>Copy all links</b> — one click, one toast; individual links keep their own copy state.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/08-draw-locked.jpg" alt="Locked draw panel with opened link counter"><br><b>Locked draw</b> — the panel counts how many links were opened; re-drawing a locked event is refused until you reset.</td>
    <td><img src="docs/screenshots/09-admin-english.jpg" alt="Same console in English"><br><b>One-click FR ⇄ EN</b> — every string is in both languages, choice remembered per browser.</td>
  </tr>
</table>

## The participant view

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/07-participant-pending.jpg" alt="Participant page before the draw"><br><b>Before the draw</b> — a private page that says "not yet"; it polls and offers a manual check.</td>
    <td width="50%"><img src="docs/screenshots/12-revealed.jpg" alt="Revealed gifts with wishlists"><br><b>After the reveal</b> — who you buy for, with their wishlist, plus a "copy my list" button.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/13-wishlist-autosave.jpg" alt="Wishlist autosaving"><br><b>Wishlist autosave</b> — saves itself 1.2 s after you stop typing; <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>Enter</kbd> saves now.</td>
    <td><img src="docs/screenshots/14-mobile-participant.jpg" alt="Participant view on a phone"><br><b>Phone-first</b> — the whole flow works one-handed; the reveal is the same everywhere.</td>
  </tr>
</table>

UX details worth knowing:

* **The French is Québécois** (`fr-CA`): « Règles pis quotas », « Le monde que tu vas gâter »,
  « Fretter le tirage », « Ouvre donc ça », and dates like « il y a 5 minutes ». Money is Canadian
  dollars (« 25 $ par personne, taxes incluses »); the English switch says `$25 CAD`.
* **Nothing is destructive without a confirm**, and every failure keeps its message on screen
  (wrong password, rate limit, unsolvable roster) with a translated explanation, not a raw
  `detail` string.
* **Field limits come from the server** (`GET /api/limits`) and are applied as `maxlength` with
  counters, so the frontend cannot drift from the API's Pydantic caps.
* **Session, not password:** the browser stores a short-lived bearer token
  (`sessionStorage.ge_admin_token`) — the admin password never sits in the browser after login.
* **Reveal is sticky per link** (`sessionStorage`), so a refresh does not spoil the surprise twice.

## Engine

`generate_draw(config, participants)` (in `main.py`) builds a balanced receiver pool
(`divmod` over each target group, so nobody receives an unfair number of gifts), then deals slots
to givers — most-constrained first — with depth-first backtracking, retried up to
`max_attempts = 20000` times. Hard rules: no self-draw, no same-household draw, never two gifts
for the same person. Cheap necessary conditions short-circuit the hopeless cases, so impossible
rosters answer `422` immediately instead of burning attempts.

## Tests

```bash
bash run_tests.sh          # 49 tests (engine invariants, API contract, security)
```

`run_tests.sh` creates its own virtualenv (`uv` if present) and adds `httpx` + `pytest` there
only, keeping `requirements.txt` pinned to `fastapi==0.115.0` and `uvicorn==0.30.6`.

To audit a running instance against the four acceptance criteria — it seeds the spec scenario,
runs the draw and checks the SQLite file directly:

```bash
python tools/verify_acceptance.py          # ends with "ACCEPTANCE: PASS"
```

## Security

| Setting (env) | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | `admin` | Organizer password. A warning is logged (and shown in the UI) while this default is in use. |
| `SESSION_SECRET` | derived | HMAC key for session tokens; derived with PBKDF2 from the admin password when unset. |
| `TRUST_PROXY` | `0` | Honour `X-Forwarded-For` only behind your own reverse proxy. |
| `LOGIN_RATE_LIMIT` / `LOGIN_LOCKOUT_THRESHOLD` / `LOGIN_LOCKOUT_SECONDS` | `10` / `5` / `300` | Login attempts per minute per client, then a temporary lockout. |
| `DRAW_RATE_LIMIT` / `API_RATE_LIMIT` | `120` / `300` | Per-minute budgets for participant links and everything else. |
| `MAX_BODY_BYTES` | `16384` | Request body cap → 413. |
| `SESSION_TTL_SECONDS` | `43200` | Session token lifetime. |

Hardening in force: per-IP fixed-window rate limits with `429` + `Retry-After`, login lockout,
constant-time comparisons for password and token signatures, short-lived revocable HMAC session
tokens (`POST /api/admin/login` → `Authorization: Bearer`, `POST /api/admin/logout`; the spec's
`X-Admin-Password` header still works), `413` body cap, Pydantic length caps on every text field,
security headers with a CDN-pinning CSP (`frame-ancestors 'none'`, `Referrer-Policy: no-referrer`
so a `/?t=<uuid>` link never leaks through the referrer), serialised draw/reset transactions,
`409` instead of silently re-drawing a locked event, and an audit log of auth, draw, reset and
link-opening events without secrets. TLS is expected to terminate at your reverse proxy.

Full threat/control table: [`PLAN.md`](PLAN.md#security-hardening-addendum).

## Layout

```
gift-exchange/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py                     # API + matching engine + SQLite schema
├── static/index.html           # SPA: admin console + participant view (FR/EN)
├── docs/
│   ├── screenshots/*.jpg       # README gallery
│   └── media/*.gif             # reveal + organizer flow
├── tests/                      # engine invariants + API behaviour + security
├── tools/
│   ├── verify_acceptance.py    # black-box acceptance audit (seeds the spec scenario)
│   ├── seed_demo.py            # demo family for demos and screenshots
│   ├── capture_docs_media.py   # Playwright pass that produces docs media
│   └── build_docs_media.py     # ffmpeg GIF assembly + still downscaling
├── run_tests.sh
└── PLAN.md                     # specification, addenda and verification log
```

Regenerate the gallery (needs Playwright, ffmpeg, Pillow — none of them are app dependencies):

```bash
uv pip install playwright && playwright install chromium
python tools/seed_demo.py
GE_BASE=http://127.0.0.1:8085 python tools/capture_docs_media.py
python tools/build_docs_media.py
```

## Notes

* All names, wishlists and events shown in the screenshots are **fictional demo data** produced
  by `tools/seed_demo.py`; nothing about a real family is in this repository, and the SQLite file
  (`data/`) is git-ignored.
* Snowfall, the gift-box reveal and the snowflake burst are CSS/SVG only and disabled under
  `prefers-reduced-motion`.
* Icons: inline SVG sprite — one bespoke crystalline snowflake plus Tabler Icons (MIT).
* Clipboard writes fall back to a hidden textarea on plain-HTTP origins, where
  `navigator.clipboard` is unavailable.
* `X-Admin-Password` guards every admin route; participant routes are guarded only by their
  UUID token. Password comparison is constant-time.

## Licence

MIT — see [LICENSE](LICENSE) and the artwork attribution in [NOTICE](NOTICE). The inline SVG
sprite mixes original artwork with Tabler Icons (MIT); fonts (Fraunces, Inter) are loaded from
Google Fonts under the SIL OFL and are not vendored. Contributions are welcome under the same
licence.
