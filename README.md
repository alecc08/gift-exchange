# Family Gift Exchange · Échange de cadeaux

Configurable Secret Santa for a big family: role-based draw quotas (adult / child), household
exclusions, one private UUID link per person, and an organizer who **never sees the pairings**.

FastAPI + SQLite backend, single-page frontend (Tailwind CDN + Alpine.js CDN, no build step).
UI is **French by default** with a one-click FR/EN switch.

Spec and the list of deliberate deviations: [`PLAN.md`](PLAN.md).

## Quick start

```bash
ADMIN_PASSWORD=change-me docker compose up -d      # http://localhost:8085
```

* `docker-compose.yml` publishes `8085:8000`, mounts `./data` for the SQLite file, and sets
  `ADMIN_PASSWORD` and `DB_PATH=/app/data/exchange.db`.
* The database schema is created automatically on startup; nothing to migrate.

Local development instead:

```bash
bash run_tests.sh                                   # venv + deps + full suite
ADMIN_PASSWORD=admin DB_PATH=$PWD/data/exchange.db \
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8085 --reload
```

## How an exchange runs

1. **Panel 1 — Rules & quotas.** Event title, budget note, and the 2×2 grid: each adult draws
   _X_ adults / _Y_ children, each child draws _X_ adults / _Y_ children.
2. **Panel 2 — Roster.** Add people with a role and a family group. Two people in the same group
   (compared case/whitespace-insensitively) can never draw each other. Copy each person's link
   (`/?t=<uuid>`).
3. **Panel 3 — Generate & lock.** The matching engine deals the gifts and locks the event.
   Statuses show only whether a link was opened ("Consulté" / "Non ouvert"), never who buys what.
4. Each person opens their own link, unwraps the gift card, and can edit their wishlist at any
   time — even after the draw is locked.
5. **Reset draw** wipes the pairings and the opened-markers and reopens editing.

## Engine

`generate_draw(config, participants)` (in `main.py`) builds a balanced receiver pool
(`divmod` over each target group, so nobody receives an unfair number of gifts), then deals slots
to givers — most-constrained first — with depth-first backtracking, retried up to
`max_attempts = 20000` times. Hard rules: no self-draw, no same-household draw, never two gifts
for the same person. Cheap necessary conditions short-circuit the hopeless cases, so impossible
rosters answer `422` immediately instead of burning attempts.

## Tests

```bash
bash run_tests.sh          # 36 tests
```

`run_tests.sh` creates its own virtualenv (`uv` if present) and adds `httpx` + `pytest` there
only, keeping `requirements.txt` pinned to `fastapi==0.115.0` and `uvicorn==0.30.6`.

To audit a running instance against the acceptance criteria:

```bash
python tools/verify_acceptance.py http://localhost:8085
```

## Layout

```
gift-exchange/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py                     # API + matching engine + SQLite schema
├── static/index.html           # SPA: admin console + participant view (FR/EN)
├── tests/                      # engine invariants + API behaviour
├── tools/verify_acceptance.py   # black-box acceptance audit
├── run_tests.sh
└── PLAN.md                     # specification, addenda and verification log
```

## Notes

* Snowfall, the gift-box reveal and the snowflake burst are CSS/SVG only and disabled under
  `prefers-reduced-motion`.
* Icons: inline SVG sprite — one bespoke crystalline snowflake plus Tabler Icons (MIT).
* `X-Admin-Password` guards every admin route; participant routes are guarded only by their
  UUID token. Password comparison is constant-time.
