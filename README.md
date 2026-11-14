# Family Gift Exchange

Configurable Secret Santa for a big family: role-based draw quotas (adult / child), a group rule
that keeps a household out of its own draw, one private link per person, and an organizer who
**never sees the pairings**.

FastAPI + SQLite · single-page frontend (Tailwind CDN + Alpine.js, no build step) · **two locales —
Québec French (`fr-CA`) and English — every string in both**, one click to switch · amounts in
Canadian dollars. This README is English, on purpose: [Languages](#languages-french-and-english)
covers what each locale does.

| The gift-box reveal | Unlock → snowfall → copy every link |
|---|---|
| ![Gift-box reveal](docs/media/reveal.gif) | ![Organizer flow](docs/media/organizer-flow.gif) |

---

## Deploy

You need Docker. That's it.

```bash
git clone https://github.com/alecc08/gift-exchange.git
cd gift-exchange
cp .env.example .env
nano .env                  # set ADMIN_PASSWORD and SESSION_SECRET (see below)
docker compose up -d       # builds the image, starts on http://127.0.0.1:8085
```

Two values in `.env` actually matter. Generate them and paste the output in:

```bash
openssl rand -base64 18    # -> ADMIN_PASSWORD   (long passphrase = the organizer key)
openssl rand -hex 32       # -> SESSION_SECRET   (signs the admin session tokens)
```

Then open **http://localhost:8085/admin**, sign in, and:

1. **Panel 1** — event title, budget note (e.g. "$25 CAD each, taxes included"), who draws what,
   and whether picks inside the same group should be avoided.
2. **Panel 2** — add the family: name, adult/child, family name, and the **group** (the household the
   rule actually works on — see [Grouping](#grouping-a-family-name-is-not-a-household)). Hit
   **Copy all links** to put every private link on the clipboard.
3. **Panel 3** — **Generate & lock draw**. Everyone gets their own link; nobody, not even
   you, sees the pairings.
4. Each person opens their link, unwraps the gift, and can edit their wishlist at any time —
   it saves itself.

The site root is the **family's** page, not the console: it tells a relative that their link arrives
by email and nothing more. The console lives at `/admin` (`ADMIN_PATH` moves it), it is not linked
from the family's page, and it asks search engines not to index it. One bundle, one document — the
server stamps each response with the single flag that says which screen to open.

Want to look around before adding real people?

```bash
ADMIN_PASSWORD=<your password> python3 tools/seed_demo.py --draw   # fictional Quebec family
```

**To reach the family on your Wi-Fi**, use the default file (publishes on all interfaces):
`docker compose up -d`, then give everyone `http://<this-machine's-LAN-IP>:8085`.
**To put it behind a domain with HTTPS**, use the hardened file and a proxy — next section.

**Already have Docker and no interest in building?** CI publishes the same image on every push to
`main`, so one line of the compose file is enough — replace `build: .` with a pinned tag
(`.github/workflows/build.yml` is the whole recipe, and the package is public):

```yaml
    image: ghcr.io/alecc08/gift-exchange:<full git sha>     # :latest also exists; pin the sha
```

### Which compose file?

| | `docker-compose.yml` | `docker-compose.hardened.yml` |
|---|---|---|
| Port | `8085:8000` on every interface | `127.0.0.1:8085:8000` only (proxy must front it) |
| Container user | root (image default) | uid `1000`, unprivileged |
| Root filesystem | writable | **read-only**, `/tmp` on tmpfs |
| Linux capabilities | default | **all dropped**, `no-new-privileges` |
| `SESSION_SECRET` | optional (derived) | **required** — refuses to start without it |
| `ADMIN_PASSWORD` | defaults to `admin` | **required** — refuses to start unset |
| Extras | — | memory/CPU/pid caps, log rotation |

```bash
mkdir -p data && chown -R 1000:1000 data      # one-time, only for the hardened file
docker compose -f docker-compose.hardened.yml up -d
```

### HTTPS behind a reverse proxy (what you want for a real event)

The app deliberately does not terminate TLS. Put a proxy in front and set `TRUST_PROXY=1`.

Caddy (automatic Let's Encrypt):

```caddy
santa.example.com {
    reverse_proxy 127.0.0.1:8085
}
```

Traefik (add to the service in `docker-compose.hardened.yml`):

```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.santa.rule=Host(`santa.example.com`)
      - traefik.http.routers.santa.entrypoints=websecure
      - traefik.http.routers.santa.tls.certresolver=letsencrypt
      - traefik.http.services.santa.loadbalancer.server.port=8000
```

### No Docker?

```bash
bash run_tests.sh                      # creates .venv, installs deps, runs the suite
ADMIN_PASSWORD=<pw> SESSION_SECRET=<hex> \
  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8085 --no-access-log
```

`--no-access-log` because the app logs its own API lines, already `X-Forwarded-For`-aware; without it
a request is recorded twice, once with the proxy's address and once with the container's.

### Backups (do one — and keep it private)

`data/` holds every wishlist **and** every participant's private link token, so whoever can read a
backup can open anybody's gifts. Encrypt it, `chmod 600` it.

```bash
docker compose stop
cp -a data "/backups/santa-$(date +%F)" && chmod -R 600 "/backups/santa-$(date +%F)"
docker compose start          # hardened file: docker compose -f docker-compose.hardened.yml start
```

One row of damage control: `POST /api/admin/reset` wipes the pairings and the opened-markers and
reopens editing; participant links stay valid. If you need the links themselves to stop working,
delete and re-add the participant.

---

## Security hardening guidelines

### Do these before you send a single link

1. **Set a real `ADMIN_PASSWORD`.** The spec default `admin` is refused by the hardened compose
   file, and while it is in use the container logs a warning and the organizer console shows a
   yellow banner saying the admin password is still the default one. If you see
   that banner, you are not ready to send links. A **blank** value (an unset `${VAR}` in a
   compose file) shuts the console entirely: every login and admin call answers `503`.
2. **Set `SESSION_SECRET`.** Without it the signing key is derived from the password, so every
   password change invalidates live sessions (and any token you might have leaked across deploys
   keeps a predictable derivation). `openssl rand -hex 32`.
3. **Terminate TLS at a proxy** (Caddy/Traefik/nginx) and never expose `8085` straight to the
   internet — participant links carry a bearer-equivalent UUID in the query string.
4. **Set `TRUST_PROXY=1` only behind your own proxy.** It is what makes `X-Forwarded-For` count
   for rate limiting and logs; with the app directly exposed, a client could spoof that header and
   buy itself a fresh budget.
5. **Publish the port on localhost** when a proxy fronts it (`docker-compose.hardened.yml` does
   this) so nothing bypasses TLS.
6. **Protect `data/`**: it is the whole state (names, wishlists, tokens). Keep it out of git
   (already git-ignored), `chmod 600` backups, full-disk encryption on the host.
7. **Back up once before the event** (command above). Restoring is `cp` it back and restart.
8. **Update the image periodically** (`docker compose build --pull && docker compose up -d`) so
   the base Python image stays patched; the app itself pins `fastapi==0.115.0` / `uvicorn==0.30.6`
   on purpose.
9. **Distribute the links like keys**: privately (one message per person), never in a public group
   post or printed on the invitation; a link is enough to see that person's gifts.
10. **Watch the log** while links are live — `docker compose logs -f`. Every security-relevant
    event is one line, with no secrets in it:

    ```
    INFO gift-exchange login_failed ip=24.114.27.9
    INFO gift-exchange login_lockout ip=24.114.27.9 seconds=300
    INFO gift-exchange request ip=24.114.27.9 method=GET path=/api/limits status=200 ms=1.3
    INFO gift-exchange draw_locked ip=… assignments=18
    ```

### Already enforced by the code (nothing to configure)

| Control | Behaviour |
|---|---|
| Password guessing | Per-IP fixed-window limit on `POST /api/admin/login` → `429` + `Retry-After`, and a temporary lockout after repeated failures; constant-time comparison. |
| Misconfigured secret | A **blank** `ADMIN_PASSWORD` (the usual result of an unset `${SECRET}` expanding in a compose file) is not treated as a password: the organizer console answers `503` to every login and every admin request, `/api/limits` reports `default_password: true`, the console shows the banner and the log says so. It can never authenticate `""`. |
| Console discovery | The organizer console is served at `/admin` (configurable with `ADMIN_PATH`), not at the site root, and nothing on the family's page names it. The root and every unmatched path render the family's landing card, so typing the hostname from an email cannot land on a password form by accident. The console document also carries `X-Robots-Tag: noindex, nofollow` and no share tags. This is discovery control, not authentication: what guards the console is the password, the login budget and the lockout. |
| Token guessing | `GET /api/draw/{token}` has its own per-IP budget; tokens are UUIDv4; unknown tokens are an indistinguishable `404`. |
| Volume / payload abuse | Shared per-IP budget for the rest, and a request-body cap → `413`. |
| Password on every request | Login also returns a short-lived **HMAC-SHA256 session token** (12 h) usable as `Authorization: Bearer`; `POST /api/admin/logout` revokes it server-side; the SPA stores the token, never the password. |
| Injection / XSS | Zero-build frontend, no `innerHTML` of user data; CSP pins the CDNs (`script-src`, `base-uri 'self'`, `form-action 'none'`, `frame-ancestors 'none'`, `object-src 'none'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`. One deliberate exception: `/static/*`
  sends `Cross-Origin-Resource-Policy: cross-origin` instead of `same-origin`, because an email client and
  the social-image crawler have to be able to load the art from another origin — and nothing personal is in
  `/static`. |
| Private-link leakage | `Referrer-Policy: no-referrer` + `<meta name="referrer" content="no-referrer">`, so `/?t=<uuid>` never rides out in a Referer. |
| Unbounded input | Pydantic length caps on every text field (name 80, family name 60, group 60, wishlist 1000, title 120, budget 500); the SPA mirrors them via `GET /api/limits`. |
| Torn writes | Draws and resets are serialised by a process lock inside one SQLite transaction; a rejected draw writes nothing. |
| Accidental re-draw | `POST /api/admin/draw` on a locked event → `409` ("already locked, reset first") instead of silently invalidating links people already opened. |
| Organizer blindness | No admin route ever returns a pairing. The one thing read out of the `assignments` table is a single `COUNT` of same-group pairs for `/api/admin/state` — enough to admit a draw had to relax a rule, never enough to name one. |
| Debug noise vs leaks | One line per **API** request (`request ip=… method=… path=… status=… ms=…`) plus auth/draw/reset/link events, with IP, status and latency. Only paths under `/api` are logged at all: page routes — `/`, `/admin`, `/static/…` — never reach the log, so a participant link cannot be recovered from it by reading the lines. For the requests that *are* logged, the path is **redacted** anyway: any UUID-shaped segment becomes `<token>`, so `/api/draw/<uuid>` logs as `/api/draw/<token>` — that path *is* a family member's credential, and the log outlives the event. A first link opening is audited as `link_opened participant_id=…`, never as the token. Error bodies are the fixed `detail` strings, never stack traces. `/docs`, `/redoc` and `/openapi.json` are switched off, and an unmatched path that asks for one of them (or for a file, like `/favicon.ico`) answers the API's JSON `404` rather than the family's page. The image starts uvicorn with `--no-access-log` so the app's own X-Forwarded-For-aware line is the single record. |

### Tuning knobs

| Env var | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | `admin` (hardened file: required) | Organizer password. |
| `SESSION_SECRET` | derived (hardened file: required) | HMAC key for session tokens. |
| `ADMIN_PATH` | `/admin` | Path the organizer console is served at. One or more path segments; a value that collides with `/api`, `/static` or the schema route, or that would need escaping in a document, is refused and the default is used. Read once at startup — routes are registered then, so changing it means a restart. Nothing on the family's page links to it. |
| `TRUST_PROXY` | `0` | Trust `X-Forwarded-For` only behind your own proxy. |
| `PUBLIC_BASE_URL` | unset | The absolute origin used for **mailed** links and mailed art. Unset, the request's own `Host` is used — and a loopback `Host` then *refuses* to send (503) rather than mail links nobody can open. Set it to the address the family types. (Share tags keep following the request's `Host`, which is what a crawler needs, and they are validated against injection separately.) |
| `LOGIN_RATE_LIMIT` | `10` | Login attempts per minute per client. |
| `LOGIN_LOCKOUT_THRESHOLD` / `LOGIN_LOCKOUT_SECONDS` | `5` / `300` | Failed logins before a temporary lockout, and its length. |
| `DRAW_RATE_LIMIT` / `API_RATE_LIMIT` | `120` / `300` | Per-minute budgets for participant links / everything else. |
| `MAX_BODY_BYTES` | `16384` | Request body cap → `413`. |
| `SESSION_TTL_SECONDS` | `43200` | Admin session lifetime. |
| `DB_PATH` | `data/exchange.db` | SQLite file. Both compose files set it to `/app/data/exchange.db` and mount that directory. |
| `RESEND_API_KEY` | unset | Mail is **off** without it: the send endpoints answer `503` and the console says so. Read per call, so a restart is not needed to change it. |
| `RESEND_FROM` | unset | Verified sender to send from; a domain you have verified in Resend. |
| `RESEND_TIMEOUT` | `15` | Seconds per outbound request to Resend. |
| `RESEND_USER_AGENT` | a browser UA | Cloudflare answers `403 / 1010` to `Python-urllib/3.x`, so the app sends an ordinary browser agent. The override exists for the same reason the header exists. |
| `RESEND_BASE_URL` | `https://api.resend.com` | Test hook: points sends at a local stub. Nothing in a real deployment should change it. |
| `EMAIL_RATE_LIMIT` | `60` | Sends per minute per organizer client (not per person — one mailbox is one send). |
| `EMAIL_SEND_INTERVAL_MS` / `EMAIL_SAME_ADDRESS_GAP_S` | `1200` / `4` | Spacing between any two sends, and the longer spacing before a second mail to the same mailbox. |
| `EMAIL_RETRY_AFTER_S` | `3` | Sleep after a provider `429`, then retry once — never a retry loop. |

### What this app does *not* protect against

* **Anyone who can read a link** can see that person's gifts — the token *is* the credential, and
  there is no second factor, no email verification, no account system.
* Browser history, a shared phone, a printed link, a forwarded message.
* **Data at rest is not encrypted.** Whoever can read `data/exchange.db` reads everything.
* Long-lived brute-force blocking: the limiter lives in process memory and **resets on restart**.
  For a public deployment, add proxy-level protections (Caddy/Traefik rate limiting, fail2ban,
  IP allowlisting on `/api/admin`).
* It is built for one family event on infrastructure you control, not as a multi-tenant service.

Full threat/control write-up: [`PLAN.md`](PLAN.md#security-hardening-addendum).

### Pre-flight check (before sending the links)

```bash
curl -s localhost:8085/api/health                       # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8085/         # 200 — the family's page
curl -s -o /dev/null -w '%{http_code}\n' localhost:8085/admin    # 200 — the console, at its own path
curl -s localhost:8085/openapi.json                             # {"detail":"Not Found"} — no map
curl -s localhost:8085/api/limits | grep default_password
#   "default_password": false   <-- must be false
ADMIN_PASSWORD=<pw> python3 tools/verify_acceptance.py  # ends with: ACCEPTANCE: PASS
```

---

## The organizer console

The gallery is shot in the app's default locale, Québec French; the captions below quote the
English labels, because both dictionaries are complete and every string in one has a twin in the
other ([Languages](#languages-french-and-english)).

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/01-login.jpg" alt="Organizer sign-in card on the festive background"><br><b>The door</b> — one password, rate limited and locked out after five failures; the whole organizer surface sits behind it. It lives at `/admin`, not at the hostname the family was given.</td>
    <td width="50%"><img src="docs/screenshots/18-draw-list.jpg" alt="Draw list: two draws, one not yet drawn and one locked, plus the form that opens another"><br><b>Sign in, pick a draw</b> — the first screen is the list: one card per draw with its status, its people, how many links went out and how many were opened. Everything below is one draw at a time.</td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/02-admin-rules.jpg" alt="Rules and quotas panel with live totals and character counters"><br><b>Rules &amp; quotas</b> — live gift totals, character counters, and a warning while the admin password is still the default.</td>
    <td width="50%"><img src="docs/screenshots/03-warnings.jpg" alt="Warning that the draw cannot succeed with these quotas"><br><b>Feasibility warnings</b> — the quota arithmetic is checked while you type, so an impossible draw is explained instead of failing after a click.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/04-roster.jpg" alt="Roster table with surname, group, address, status and action columns"><br><b>Roster</b> — per-person gift count, a column for the surname, one for the household, one for the address the mail will go to, and its own <b>Send</b> button.</td>
    <td><img src="docs/screenshots/05-group-filter.jpg" alt="Roster filtered to one household by its group chip"><br><b>Filter by household</b> — the group chips carry counts; one click narrows the roster to that table, which is how you check one household's rows without scanning the whole family.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/06-toast-clipboard.jpg" alt="Toast confirming all links were copied"><br><b>Copy all links</b> — one click, one toast; each row keeps its own copy state, for the families who would rather get their link in person.</td>
    <td><img src="docs/screenshots/08-draw-locked.jpg" alt="Locked draw panel with opened link counter"><br><b>Locked draw</b> — counts how many links were opened; re-drawing a locked event is refused until you reset.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/16-group-note.jpg" alt="Console predicting that the draw will have to pick inside a group"><br><b>The console thinks ahead</b> — this roster cannot respect the group rule; the note says how many picks it will cost, and the draw button stays enabled.</td>
    <td><img src="docs/screenshots/17-draw-relaxed.jpg" alt="Locked draw reporting the same-group pick it had to make"><br><b>…and admits what it did</b> — the draw ran, with exactly the one crossing the arithmetic forced. A count, never a pairing.</td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/screenshots/09-admin-english.jpg" alt="Same console in English"><br><b>One-click FR ⇄ EN</b> — every string exists in both languages, remembered per browser, and the parity of the two dictionaries is a test, not a hope.</td>
  </tr>
</table>

### On a phone

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/19-roster-phone.jpg" alt="Roster at 390 px: each person is a card with every field labelled"><br><b>The roster, re-built</b> — below 900 px a person is a card, and each cell repeats its column name (Email, Status, Actions), so nothing is silently dropped on the way to the small screen.</td>
    <td width="50%"><img src="docs/screenshots/15-mobile-admin.jpg" alt="Organizer console on a phone with the draw summary and the first panel"><br><b>The console, not a shrunken console</b> — the draw summary, the panel headings and the buttons stay readable one-handed; 40 px targets below 640 px, and inputs at 16 px so Mobile Safari never zooms in and leaves you there.</td>
  </tr>
</table>

There are real breakpoints now, instead of a desktop layout that simply hung off the edge of a
narrow screen. Below **900 px** the roster stops being a seven-column table and becomes one card per
person, each field labelled with its column name (`td[data-label]`, so the labels are the same
strings as the table headers and translate with the UI). Below **640 px** buttons get a 40 px target
and inputs go to 16 px — under that, Mobile Safari zooms the page on focus and never gives the zoom
back. Below **480 px** the status chips drop the *when* ("2 hours ago") and keep the *what*
("Link emailed"); the full timestamp stays in the chip's `title` and in the tooltip, so the fact
survives and the chip stops running into the cell next to it. The quota table, the mail preview and
the toasts reflow with the screen; the toasts go full-width. `tools/check_organizer_ui.py` asserts
this at 390 px and 360 px: no element outside the viewport, no horizontal scroll, and the roster
rendered as cards.

### Several draws on one server

One deployment can hold more than one exchange — the holiday party, the office, the cousins' — and each
one has its **own** roster, rules, links, pairings and mail history. Signing in lands on
**Pick a draw**: one card per draw with its status ("Not drawn yet" / "Drawn Nov 13"), how many
people are in it, how many links went out and how many were opened. Opening a
draw puts it in the URL (`/?draw=7`), so a reload or a bookmarked console comes back to the same
event instead of dropping you on the list.

| Endpoint | Behaviour |
| --- | --- |
| `GET /api/admin/draws` | Every draw with its counts. Counts only — never a name, never a pairing. |
| `POST /api/admin/draws` | `{event_title, budget_note?, copy_participants_from?}`. Copies **people** (names, households, wishlists, addresses) with **fresh tokens** and no `viewed_at` / `email_sent_at`: a link that already went out with last year's pairing must not become this year's credential. |
| `DELETE /api/admin/draws/{id}` | Removes the draw, its roster and its pairings. A **locked** draw is refused (`400`) rather than forced: relatives are holding links that reveal it, so the sequence is *reset*, then delete. |
| every other `/api/admin/*` | Takes `?draw_id=`. Omitted, it means "the only draw there is" — the contract every older client and the whole pre-multi-draw test suite was written against. With two draws and no `draw_id` the server refuses to guess: `400 No draw selected`. Unknown id: `404 This draw does not exist`. Empty server: `400 There is no draw yet`. |

Participant ids are global, so the console cannot reach across either: editing, mailing or deleting
a person who belongs to another draw answers `404`. A participant's link never carries a draw id —
the token resolves to its own row, and therefore to its own draw.

**Upgrading an existing database** (a deployment that already ran one draw) needs nothing from you:
on start the app rebuilds the old single-row `config` table as `draws`, keeps the ids, adds
`draw_id` to every participant with `DEFAULT 1`, and adds `created_at` / `drawn_at`. Tokens,
pairings and delivery stamps are untouched, and the mail a family already received keeps working.
It is a rebuild rather than an `ALTER TABLE … RENAME` because the original table was
`id INTEGER PRIMARY KEY CHECK (id = 1)` — renamed, that check comes along and a *second* draw can
never be created. `tests/test_draws.py` pins the upgrade against a live-shaped database, and the
migration was rehearsed on a copy of the production file before anything was deployed.

### Grouping: a family name is not a household

A big family has two different things people call "family": the **surname**, and the **household
the rule is about**. Cousins can carry the same surname as you and live in a different house, and two
surnames can share one roof. So the participant record carries both:

| Field | Label | What it does |
|---|---|---|
| `family_group` | Family name | A label for the roster. Never used by the engine. |
| `group_key` | Group | The unit of the rule. Two people with the same group_key are one household. |

An empty `group_key` means *in no group* — and two empties are **not** one group: each one becomes a
private key (`#solo:<id>` internally), so someone with nothing filled in can be drawn by anyone
except themselves.

The rule is a **preference, not an axiom** (Panel 1: "Avoid picks within the same group"):

* The solver first tries a draw with **zero** same-group picks. Most rosters finish there.
* If none exists, it does **not** answer `422`. It computes a lower bound on how many gifts *must*
  cross (`_min_crossings()`), then tries that budget, then one more, until a draw exists — so the
  relaxation is as small as the arithmetic allows. A 2-person household in a 3-person roster draws,
  with exactly one crossing.
* The console says so, in both directions: before the draw the note predicts it ("Groups this big
  force the draw to pick inside one of them at least 1 time(s)."), after the draw the panel reports
  the number ("The draw picked inside the same group 1 time(s): with these groups, it had to.").
  A prediction this exact is not a coincidence — the frontend runs the same bound, ported from
  `main.py`.
* Only the **quota arithmetic** can block the button. Group tightness never disables *Generate &
  lock draw*, because the engine can still deliver a draw.
* Unchecking the box switches groups off entirely (`#solo:<id>` for everyone), which is also what
  keeps a 2-person household from drawing itself.

The organizer still learns nothing about who drew whom. The one thing read out of the `assignments`
table for `/api/admin/state` is a single `COUNT` of same-group pairs, exposed as
`config.same_group_crossings` — a number, never a name.

## Link emails (Resend)

The console can mail each participant their own link instead of the organizer copy-pasting twelve
URLs into WhatsApp. Off by default at the *server* level: no `RESEND_API_KEY`, no emails — the
buttons stay visible but disabled with a clear "not configured" note, and every send endpoint
answers 503 (fail closed, same posture as a blank `ADMIN_PASSWORD`).

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/20-email-panel.jpg" alt="Draw panel with the mail button, the household copy and the mailbox counter"><br><b>The send is counted in mailboxes</b> — the chip says "8 people · 4 mailbox(es) · 4 email(s)": what you are about to pay for is addresses, not people, and the copy under the button says so in one sentence.</td>
    <td width="50%"><img src="docs/screenshots/21-email-preview.jpg" alt="The household mail: art, intro, and three labelled buttons, one per person"><br><b>One mail, one household</b> — the subject carries their names, the gift art is a PNG served from <code>/static</code>, and there is one labelled button each ("Open Aline's link", …). Nothing in the mail belongs to anybody outside that mailbox.</td>
  </tr>
</table>

* **The field is free text.** `participants.email` is normalized (trim, lowercase) and never
  format-rejected at save time — a typo is flagged softly in the cell, and only the send-time
  check (`EMAIL_RE`) refuses to spend an API call on it. Fixing an address after the draw is the
  one roster edit allowed *while locked*: `PUT /api/admin/participants/{id}` accepts `email` only,
  and editing it clears `email_sent_at` because the old mail went somewhere else.
* **One email per mailbox.** `_group_by_address()` bundles participants by the address they gave,
  and every send path (bulk, per-row, auto-on-draw) goes through it, so the rule cannot be forgotten
  in one place while obeyed in another. Twelve cousins at five addresses is five provider calls, and
  a mail to a family of three carries **three labelled links** — "Open Alec's link", and so on
  — never anyone else's. `deliver_household_email()` stamps `email_sent_at` on the whole group, so
  the roster cannot show two people "mailed" and a third forgotten. Blank and unsendable addresses
  stay a group of one: the report still names the uncle who was skipped.
* **Two templates, one shell.** `build_email()` (one link) and `build_links_email()` (a household)
  share `_email_shell()`/`_email_assets()`, so the art and the palette cannot drift apart. The
  household copy is its own set of strings (`hello_many`, `body_many_*`, `privacy_many`,
  `link_label`, …) rather than the singular wording stretched: "your link" in a mail that holds
  three links is a lie, and a template full of `s` conditionals reads worse than a second template.
* **What a mail contains:** the event name, the participants' names, and their own links — never a
  pairing, never an outsider's name. Names are escaped before they touch the HTML.
  `X-Entity-Ref-ID` is the participant id(s) joined, so repeats thread into one conversation.
* **The link is `/?t=<uuid>`, built by `participant_link()` and nobody else.** That is the URL the
  SPA reads and the one the console's "Copy link" hands out; the token is a query parameter, so the
  link works whatever path the page is opened at. The first real send built it from the API's path
  shape instead (`/draw/<uuid>`), and twelve inboxes got `{"detail":"Not Found"}` — the API lives
  under `/api`, so the pretty path was never a route. The links were re-sent from the one builder,
  and that path is still not a route: it renders the family's landing card, which says where the real
  link is, and it never reads a token out of a path segment. A redirect kept alive only by an address
  nobody mails any more is a route nobody maintains. The test that would have caught the original
  failure follows the href out of the mail body and demands `200 text/html`.
* **The subject leads with the people:** `Ghislaine — your secret link · New Year at Gaétan's` for
  one person, `Alec, Gabrielle and Hugo — your links · Adult draw` for a household. A family shares
  one inbox, so mails from one sender with one subject are impossible to tell apart in a phone's
  list view — and the event title, being last, is the part Gmail truncates. Past
  `MAX_SUBJECT_NAMES` (3) the list becomes "A, B, C and 2 other(s)" rather than pushing the
  words "your links" off the screen. Which language a mail is written in is the draw's
  `email_language` setting, not a guess: Québec French unless the organizer switches it.
* **Theme:** the site's palette (pine/gold/ivory) in inline styles, with the snowflake, the big gift
  and the gift strip served from `/static` as `<img>`. **They are PNG, not SVG.** Gmail serves every
  image through its own proxy and that proxy does not serve SVG — Google has said so publicly, and
  cani.email lists SVG as "partially supported, non-Google only". (What a real inbox actually proved
  first was a different bug, below: the first test mails pointed at `127.0.0.1`. Both are worth
  fixing, and only one of them was visible.) Inline `<svg>` markup is stripped by Gmail too, so the
  art is `<img>`-referenced, never inline. The SVGs stay the source of truth (they are what gets
  edited and what the site's own vectors match); `tools/build_email_art.py` rasterizes the three the
  mail loads, at 2x the displayed size so a phone shows them sharp, and a test asserts both the
  format and that the intrinsic pixels are 2x the `width`/`height` the mail declares. Without an
  absolute base URL the art is simply omitted — a relative src in an email is a broken image with
  extra steps.
  The big gift is the mail's whole idea in one picture: the box is shut, the lid has jumped off, and
  somebody inside is holding it open and looking up — the pairing is already in there, and only the
  reader can open it. It is drawn static on purpose: email clients disagree about CSS animation
  (Outlook ignores `@keyframes`, Gmail strips them), and art that silently freezes in half the
  family's inboxes is worse than no animation. The `<img>` carries a localized `alt`, because for a
  screen reader that picture *is* the pitch.
* **The link's absolute origin: `PUBLIC_BASE_URL`, or the Host header, never a loopback host.** Every
  URL in a mail — the participant's link and the art — is built from one base. Reading it off the
  request is usually right and sometimes a disaster: an admin call scripted inside the container, a
  health check or a port-forward carries `Host: 127.0.0.1:8000`, and the mail then invites the family
  to open the server's own machine. That is exactly what happened to the first test sends on the real
  deployment: the copy, the subject and the links were perfect and **every URL in the mail was
  unreachable from outside**, which is why the images were blank and a click would have died. So:
  set `PUBLIC_BASE_URL` to the address the family types (it wins outright for everything that leaves
  by mail), and without it a loopback origin is **refused** — `503` naming the variable, and in the
  auto-send report each person is listed as skipped with that reason. The draw still commits: a mail
  that cannot be written correctly never rolls back a draw. (Share tags deliberately keep following the
  request's own `Host`: a crawler wants the host it visited, and those URLs are validated against
  injection separately.)
* **Triggers:** **Send** per row (which sends that row's whole *household*, and answers with
  `with: [names]` so the console can say what really left), **Email the links** for
  everyone with an address (per-person report, skipped addresses listed not hidden, plus
  `messages` = how many mails actually left), and an opt-in `auto_email_on_draw` config switch that
  mails *after* the draw commits — a dead Resend can never leave a half-drawn event.
* **Throttling:** sends are paced by one process-wide gate — a global spacing
  (`EMAIL_SEND_INTERVAL_MS`, default 1200 ms, since Resend's free tier 429s above ~1 req/s) and
  a longer per-address spacing (`EMAIL_SAME_ADDRESS_GAP_S`, default 4 s). Since one mailbox gets one
  mail, the per-address gap now covers the case that is left: the organizer clicking **Send** on
  a relative in the middle of a bulk run, or re-sending after fixing an address. The rate limit
  counts *sends*, not people — billing twelve relatives at five mailboxes as twelve would throttle
  the wrong thing. A 429 from the provider gets exactly one retry after `EMAIL_RETRY_AFTER_S`
  (default 3 s) — never a retry loop.
* **Preview:** `POST /api/admin/emails/preview` renders the template with sample names (`Aline`,
  `Gabrielle`, `Hugo`) and a sample all-zeros link, so the modal is screenshot-able and cannot leak
  the roster. `links: 1..3` chooses which template to show; the console has two buttons, **Email
  preview** and **Multi-link preview**, because a family mail is only believable once you
  have seen it. The preview resolves its origin through `mail_base_url()`, the same function the send
  uses: it used to read the request's `Host`, so an organizer on the server itself saw `127.0.0.1`
  links in the modal while the real mail would have carried the configured hostname — a preview that
  disagrees with the send is worse than no preview, and that is how the loopback bug got debugged.
* **Transport:** stdlib `urllib`, one POST to `api.resend.com/emails`. The `User-Agent` header is
  load-bearing: Cloudflare in front of Resend answers `403 / error code 1010` to Python's default
  `Python-urllib/3.x` agent (measured — a browser UA gets a normal `401 API key is invalid` from
  Resend itself). The app sends an ordinary browser UA; `RESEND_USER_AGENT` overrides it. The key is
  never logged — participant ids + Resend message id is the audit trail.
* **Deliverability:** verified for `alserv.org` — Resend's DKIM (`resend._domainkey`), aligned SPF
  (`send.alserv.org`), and now a DMARC record (`v=DMARC1; p=none`). If your domain has no DMARC,
  expect Gmail to spam a first send; add the TXT record before blaming the template.
  Resend also offers SMTP (`smtp.resend.com`, user `resend`, password = your API key); this app
  uses the API instead: structured errors per send, a message id per participant, and a
  `RESEND_BASE_URL` test hook that SMTP has no equivalent of.

**The send contract**, because the console has to explain what left:

| Endpoint | Behaviour |
| --- | --- |
| `POST /api/admin/emails` | Mails everyone with an address in this draw. Answers per person (`results`, so a skipped address is named, not hidden) plus `messages` — twelve people at five addresses answers `sent: 12, messages: 5`. |
| `POST /api/admin/participants/{id}/email` | Sends that person's **mailbox**: one mail holding every link at that address in this draw, and `with: [names]` says who else was in it. Clicking it three times on three relatives is three mails (each already complete), not nine links' worth of spam. |
| `POST /api/admin/emails/preview` | `{language?, state?, links?}`. Sample names only, so it can be screenshotted; `links: 3` shows the household template. |

## The participant view

The address the family is given is not the console: it is the card below, which says where their link
is. Nobody has to find the site to take part — they start from the email — but the page behind the
hostname still has to explain that to an aunt who typed it, and to a cousin whose address was one
character off. Every address that is not the console, the API or a file renders it, and it carries
no login form and no hint that one exists.

<table>
  <tr>
    <td colspan="2"><img src="docs/screenshots/22-family-landing.jpg" alt="The page at the site root: a card saying the draw does not open here and the link arrives by email"><br><b>The page behind the hostname</b> — one card, in the family's language, with the privacy line reused from the participant page. The console is not mentioned and not linked.</td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/07-participant-pending.jpg" alt="Participant page before the draw"><br><b>Before the draw</b> — "the draw hasn't been finalized yet"; it polls, and offers a manual check.</td>
    <td width="50%"><img src="docs/screenshots/10-reveal-tease.jpg" alt="The closed gift and the button that opens it"><br><b>The box is shut</b> — the pairing is already there, and the page says so without saying what it is: "Your draw is ready. Open the gift."</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/11-reveal-mid.jpg" alt="Gift mid-reveal, lid off, snow falling"><br><b>…and something inside is trying to come out</b> — one click, the lid jumps, the snow falls, then the names. The reveal is a moment, not a page refresh.</td>
    <td><img src="docs/screenshots/12-revealed.jpg" alt="Revealed gifts with wishlists"><br><b>After the reveal</b> — who you buy for, with their wishlist, plus “copy my list”.</td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/13-wishlist-autosave.jpg" alt="Wishlist autosaving"><br><b>Autosave</b> — saves 1.2 s after you stop typing; <kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>Enter</kbd> saves now.</td>
    <td><img src="docs/screenshots/14-mobile-participant.jpg" alt="Participant view on a phone"><br><b>Phone-first</b> — the whole flow works one-handed, no horizontal scroll.</td>
  </tr>
</table>

UX details worth knowing:

* **Both locales everywhere.** Every label, notice and error the organizer or the participant sees
  exists in Québec French and English — [Languages](#languages-french-and-english) below.
  Money is Canadian dollars in both, formatted per locale (`25 $` in French, `$25 CAD` in English).
* **Nothing destructive without a confirm**, and failures keep their message on screen with a
  translated explanation ("Incorrect password.", "Too many requests. Try again in 300
  second(s).") instead of a raw `detail` string.
* **Field limits come from the server** (`GET /api/limits`) and are applied as `maxlength` with
  counters, so the frontend can never drift from the API's Pydantic caps.
* **Reveal is sticky per link** (`sessionStorage`): a refresh does not replay the surprise.

### Languages: French and English

Two complete locales ship in the box, and neither one is an after-the-fact translation: **Québec
French (`fr-CA`) is the default** — the family this was built for reads French — and **English** is
one click in the top bar. There are two language switches on purpose, because they serve different
people:

| What | Which switch | Default |
|---|---|---|
| Console and participant page | the FR / EN buttons in the top bar, remembered per browser (`localStorage: ge_lang`) | `fr` |
| The mail — subject, greeting, per-person button labels, privacy line | Panel 1: **Email language** (`config.email_language`) | `fr` |

So an organizer who works in English can still send the family a French mail, which is usually what a
Québec family wants.

What makes "two locales" a fact rather than a claim:

* One dictionary per language in `static/index.html` (`I18N.fr`, `I18N.en`) — **196 keys each** — and
  `tests/test_i18n.py` fails on a key that exists in one and not the other. A missing key would render
  its raw name on screen, which is exactly how a half-translated UI gets born. The same test fails when
  the README quotes a different number, because that count is the sentence a translator checks.
* The same `{placeholder}` names on both sides of every key, asserted key by key, so a sentence built
  from quotas and gift counts cannot break in one language only.
* The mail has its own parallel dictionary (`EMAIL_STRINGS` in `main.py`), so the subject, the greeting,
  the labelled buttons and the privacy note are each written twice instead of machine-stitched from the
  singular copy.
* Dates, the 24-hour clock and the relative "2 hours ago" chips go through `locale()`, which answers
  `fr-CA` or `en-CA`, and `<html lang>` is set to the same value.
* The copy rule is tested, not trusted. `tests/test_i18n.py` carries the list of French forms that are
  **banned** — the phonetic spellings and spoken contractions that turn Québécois copy into a
  caricature — each with the reason it is banned, so a contributor who does not read French can still
  see why a string was refused. `PLAN.md` records the mirror image: what stays deliberately Quebec.
* Labels are collision-checked: two different controls may not answer to one label. That guard paid for
  itself during a rename, when the sign-out button briefly carried the draw button's label and a capture
  script clicked the wrong control.
* One honest gap: the card's title is fixed French in both locales — `SHARE_TITLE` is
  "Échange de cadeaux · Père Noël secret", and *Père Noël secret* is what the family calls the game.
  The same name opens the browser tab before the SPA switches to the localized brand, and it is the
  kicker above the greeting in the mail. The card itself is generic — no name, no event title, no
  token — so this is cosmetic, not a leak.

And how the copy sounds, since it is the part a family actually reads: the French is ordinary Québec
French rather than the stage version — the words people really use for the game (*piger*, *gâter*) and
the informal `tu` address, with no performed joual. The English is plain rather than literal: the reveal
heading is "The gifts you give", not a word-for-word transfer of the French one.

### Sharing the link: what the preview shows

The organizer hands out links, so the link preview is the front door. Every **family** page carries
Open Graph + Twitter card tags — the gift-card title, the description, and
`static/og-image.png` (1200×630 PNG), plus `favicon.svg`, `apple-touch-icon.png`, `icon-512.png`
and a `site.webmanifest` so "add to home screen" looks intentional on a phone. The console document
is the same file with the block left empty: nobody shares a login page, and `og:url` pointing at the
family's page from a console would be a lie.

Three decisions in it are deliberate:

* **The URL-dependent tags are injected by the server** (`share_meta()` in `main.py`). The same
  image answers on a public domain, a LAN hostname, or `http://192.168.1.10:8085`, and a preview
  has to name the host the browser was actually given. `X-Forwarded-Proto` is trusted only with
  `TRUST_PROXY=1`, otherwise the card would advertise `http://` behind your own TLS proxy — and a
  Host header that isn't a host loses the URL tags rather than echoing garbage.
* **The card is identical for `/` and for a private `/?t=<uuid>`.** Crawlers read this markup, so
  it carries the generic gift card and never a name, an event title, or a token. Asserted in
  `tests/test_share_metadata.py`.
* **The card is square-safe.** Facebook, WhatsApp and iMessage crop the wide image to the centre
  square on phones, so the headline is laid out to live inside the middle 630 px — and
  `tools/build_share_art.py` measures the rendered headline and **fails the build** when it
  doesn't fit.

Regenerate the art (Chromium renders the same HTML/SVG/fonts as the app, so the card matches the
UI instead of drifting from it):

```bash
python3 tools/build_share_art.py   # static/og-image.png, icon-512.png, apple-touch-icon.png
```

## Engine

`generate_draw(config, participants)` builds a balanced receiver pool (`divmod` over each target
group, so nobody receives an unfair number of gifts), then deals slots to givers — most-constrained
first — with depth-first backtracking.

Two layers of rule, and they are deliberately not the same kind of thing:

* **Hard**: no self-draw, no two gifts from one giver to the same receiver, and a giver cannot owe
  more adult/child gifts than the roster contains receivers of that kind. Only these are checked
  up front (`_feasible`), and only these can produce a `422`.
* **Soft**: the same-group exclusion. The search runs with a crossing budget, starting at the
  `_min_crossings()` lower bound and stepping up by one, so it degrades by the smallest amount the
  roster allows instead of failing. The strict pass gets the full attempt budget (`20000` by
  default); a relaxation gets `max_attempts // 10`, so the ladder cannot multiply the worst case.

Returns `(pairs, crossed)` — `crossed` is how many gifts landed inside a group, which is what the
console reports.

## Tests

```bash
bash run_tests.sh          # 233 tests: engine invariants (incl. per-person fairness), grouping
                           # bounds, API contract, page routing, security, previews, email
                           # delivery, and the guards over the copy and the gallery itself
```

`run_tests.sh` creates its own virtualenv (`uv` if present) and adds `httpx` + `pytest` there only,
keeping `requirements.txt` pinned to `fastapi==0.115.0` and `uvicorn==0.30.6`. Audit a live
instance (it seeds the spec scenario, draws, then checks the HTTP API **and** the SQLite file):

```bash
ADMIN_PASSWORD=<pw> python3 tools/verify_acceptance.py     # ACCEPTANCE: PASS
```

## Layout

```
gift-exchange/
├── docker-compose.yml            # simple: all interfaces, port 8085
├── docker-compose.hardened.yml   # localhost-only, read-only FS, no capabilities, uid 1000
├── .env.example                  # copy to .env (git-ignored)
├── Dockerfile                    # python:3.12-slim + pinned fastapi/uvicorn
├── main.py                       # API + matching engine + SQLite schema + the two page routes
├── static/index.html             # SPA: landing card + participant view + organizer console
│                                 # (one bundle, three screens, FR-CA/EN)
│                                 # + og-image.png, favicon.svg, icons, site.webmanifest
├── docs/                         # screenshots + GIFs used by this README
├── tests/                        # 233 tests
├── tools/                        # seed_demo, verify_acceptance, browser + docs media, share-card build
├── run_tests.sh
├── LICENSE · NOTICE              # MIT + third-party artwork attribution
└── PLAN.md                       # specification, deviations, threat table, verification log
```

Regenerate the gallery (needs Playwright, ffmpeg, Pillow — none of them are app dependencies):

```bash
uv pip install playwright && playwright install chromium
ADMIN_PASSWORD=<pw> python3 tools/seed_demo.py      # optional: the capture re-seeds it itself
# PUBLIC_BASE_URL matters here: it is what the mail preview builds its links and its art from, and
# unset the preview still shows empty boxes where the gift should be.
# GE_ADMIN_PATH is where the capture tool finds the console — it is not at the root any more, and the
# app does not advertise the path, so the tool is told the same way it is told the base URL.
PUBLIC_BASE_URL=http://127.0.0.1:8085 GE_BASE=http://127.0.0.1:8085 GE_ADMIN_PATH=/admin \
  python3 tools/capture_docs_media.py
python3 tools/build_docs_media.py
python3 tools/build_share_art.py            # link-preview card + icons (also Chromium)
python3 tools/build_email_art.py            # the mail's PNG art, from the SVGs in static/
```

The console's client-side glue (Alpine getters, the checkbox's `0/1` ↔ boolean coercion, the two
group notices) is invisible to the Python suite, so there is a browser check for it too:

```bash
GE_BASE=http://127.0.0.1:8085 GE_ADMIN_PATH=/admin ADMIN_PASSWORD=<pw> \
  python3 tools/check_organizer_ui.py
```

## Notes

* Names, wishlists and events in the screenshots are **fictional demo data** from
  `tools/seed_demo.py`; `data/` is git-ignored and nothing about a real family is in this repo.
  Two of the demo households are deliberately both surnamed Tremblay — that is what the Group
  column is for.
* Snowfall, the reveal and the burst are CSS/SVG only, disabled under `prefers-reduced-motion`.
* Icons: one inline SVG sprite — a bespoke crystalline snowflake plus Tabler Icons (MIT), see
  [NOTICE](NOTICE).
* Clipboard writes fall back to a hidden textarea on plain-HTTP origins where
  `navigator.clipboard` is blocked.

## Licence

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions welcome under the same licence.
