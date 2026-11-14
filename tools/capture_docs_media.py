"""Capture the README media with Playwright against a running instance.

    GE_BASE=http://127.0.0.1:8085 python tools/capture_docs_media.py

Writes PNG stills to docs/screenshots/, two .webm recordings to a temp directory,
and docs/media/manifest.json describing where the animations start inside each video
(tools/build_docs_media.py turns those into the README GIFs).

The demo roster is rebuilt from seed_demo.py at both ends of the run, so the gallery is reproducible
from any state — the stills are described against those names, households and wishlists.

**Set `PUBLIC_BASE_URL` on the instance you capture** (e.g. `http://127.0.0.1:8085`). The mail
preview builds its art URLs from it; unset, the modal shows empty boxes where the gift should be and
the still documents a bug instead of the design.

Requires Playwright (`uv pip install playwright && playwright install chromium`). This is a
docs-only tool: the app itself has no test/docs dependencies.
"""

import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
# Where the console lives, and it is not the site root: "/" is the family's page, so every organizer
# shot has to open this path. Pass GE_ADMIN_PATH when the instance runs with ADMIN_PATH set, the same
# way GE_BASE is passed — the app never advertises the path, on purpose.
ADMIN_PATH = os.environ.get("GE_ADMIN_PATH", "/admin")
REPO = pathlib.Path(__file__).resolve().parents[1]
SHOTS = REPO / "docs" / "screenshots"
MEDIA = REPO / "docs" / "media"
VIDEOS = pathlib.Path(os.environ.get("GE_VIDEO_DIR", "/tmp/opencode/videos"))

sys.path.insert(0, str(REPO / "tools"))
import seed_demo  # noqa: E402  same roster the gallery is built from


def goto_console(page, **kwargs):
    """Open the organizer console — the only screen with the password field."""
    page.goto(BASE + ADMIN_PATH, wait_until="networkidle", **kwargs)
    page.wait_for_selector("#pw")


def goto_family_page(page):
    """Open the page a relative reaches by typing the hostname: no token, no login, no console.

    The check is the server's own route flag rather than a caption, so it cannot go stale the way a
    text match does when the copy is reworded.
    """
    page.goto(BASE + "/", wait_until="networkidle")
    if page.evaluate("window.__GE_ORGANIZER__ !== false"):
        raise SystemExit("the site root is not the family's page any more — nothing here is worth shooting")
    if page.query_selector("#pw"):
        raise SystemExit("the login form is on the family's page")
    page.wait_for_selector("h2")


def bearer_headers():
    """One login per run: the login endpoint is rate limited on purpose."""
    body = json.dumps({"password": PASSWORD}).encode()
    request = urllib.request.Request(
        BASE + "/api/admin/login", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(request).read())["token"]
    return {"Authorization": "Bearer " + token}


def admin_api(path, headers, method="GET", body=None):
    if body is not None:
        data = json.dumps(body).encode()
    elif method == "POST":
        data = b"{}"
    else:
        data = None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **headers})
    try:
        return json.loads(urllib.request.urlopen(request).read())
    except urllib.error.HTTPError as error:
        # the server's `detail` is the whole explanation; without it a failed call is a stack trace
        # and a status code.
        raise RuntimeError(f"{method} {path} -> {error.code}: "
                           f"{error.read().decode('utf-8', 'replace')}") from None


def open_link(token):
    """Open one participant link so the roster shows a real 'viewed' marker."""
    with urllib.request.urlopen(f"{BASE}/api/draw/{token}") as response:
        return response.status


def shot(page, name, full=False):
    page.screenshot(path=str(SHOTS / name), full_page=full, animations="allow")
    print("  still:", name)


def enter_console(page, shoot_list=True):
    """Login now lands on the draw list; open the first draw to reach the console.

    Also shoots the list itself, since it is the first thing an organizer sees.
    """
    page.wait_for_selector("#draw-cards article")
    time.sleep(0.8)
    if shoot_list:
        shot(page, "18-draw-list.png", full=True)
    page.locator("#draw-cards article").first.get_by_role("button", name="Ouvrir").click()
    page.wait_for_selector("text=Copier tous les liens")


def capture_email_console(page):
    """The mail panel and the mail itself — one email per mailbox, art included.

    Taken while the draw is locked, which is the state where the panel offers the links mail. The
    second still is the multi-link preview: one household's message, with every person's link
    labelled, and the gift art that only appears because the PNGs are served as rasters from an
    absolute origin (see README.md, the email section).
    """
    page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
    time.sleep(0.6)
    # 08-draw-locked.png is taken at this same scroll position, so this one moves down to the mail
    # block: the two stills have to say different things.
    page.evaluate("window.scrollBy({top: 330, behavior: 'instant'})")
    time.sleep(0.6)
    shot(page, "20-email-panel.png")

    # Taller than the rest of the gallery on purpose: the modal only grows as far as its iframe, whose
    # height comes from a min-height, and the point of this still is the labelled links — which sit
    # under the art. So the iframe is stretched and the modal follows (it is capped at 90% of the
    # viewport, hence the tall viewport).
    page.set_viewport_size({"width": 1100, "height": 1500})
    time.sleep(0.6)
    page.get_by_role("button", name="Aperçu à plusieurs liens").click()
    page.wait_for_selector("#preview-frame")
    time.sleep(1.4)                       # the srcdoc iframe paints and pulls its art over HTTP
    page.eval_on_selector("#preview-frame", "el => { el.style.minHeight = '1180px'; }")
    time.sleep(0.8)
    shot(page, "21-email-preview.png")
    page.get_by_role("button", name="Fermer", exact=True).click()
    page.wait_for_selector("#preview-frame", state="hidden")
    page.set_viewport_size({"width": 1440, "height": 900})
    time.sleep(0.4)


def capture_family_page(page):
    """The page behind the hostname itself: what a relative sees instead of the login form.

    No auth, no token, no roster — which is the point of the still. It is also the response a
    mistyped address gets, since every unmatched path renders this screen.
    """
    goto_family_page(page)
    time.sleep(1.0)
    shot(page, "22-family-landing.png")


def capture_admin_page(page):
    """Organizer flow: login, rules, warnings, roster, filters, clipboard, draw, EN."""
    goto_console(page)
    time.sleep(1.2)
    shot(page, "01-login.png")

    page.fill("#pw", PASSWORD)
    page.press("#pw", "Enter")
    enter_console(page)
    time.sleep(1.0)
    shot(page, "02-admin-rules.png")

    # an infeasible quota is caught before the draw is ever attempted
    quota = page.locator("input[type=number]").first
    quota.fill("9")
    page.wait_for_selector(".chip.bg-holly-100")
    time.sleep(0.6)
    shot(page, "03-warnings.png")
    quota.fill("1")
    page.wait_for_selector(".chip.bg-holly-100", state="hidden")
    time.sleep(0.4)

    page.locator("h2", has_text="Les participants").scroll_into_view_if_needed()
    time.sleep(0.4)
    # and one screen further: this still is about the roster, and the add-a-person form is not it —
    # the rows are, including the address column the mail panel groups by
    page.evaluate("window.scrollBy({top: 430, behavior: 'instant'})")
    time.sleep(0.6)
    shot(page, "04-roster.png")
    page.get_by_role("button", name=re.compile(r"^Tremblay")).first.click()
    time.sleep(0.6)
    shot(page, "05-group-filter.png")
    page.get_by_role("button", name="Montre tout le monde").click()
    time.sleep(0.4)

    # the same roster at phone width: below 900 px each person becomes a card, which is the whole
    # answer to "the console hides the excess on a phone". Viewport-sized, not full page: the console
    # is long, and a 14 000 px strip is not something a reader can look at.
    page.set_viewport_size({"width": 390, "height": 844})
    time.sleep(0.8)
    # scroll AFTER the resize: the breakpoint re-lays-out the page, so the position from the wide
    # viewport is meaningless. And aim at the first labelled cell, not the heading — `td[data-label]`
    # is what makes the cards cards, and the heading sits above a tall add-a-person form.
    page.locator("td[data-label]").first.evaluate("el => el.scrollIntoView({block: 'start'})")
    time.sleep(0.6)
    shot(page, "19-roster-phone.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    time.sleep(0.6)

    page.get_by_role("button", name="Copier tous les liens").click()
    page.wait_for_selector("#toasts > div")
    time.sleep(0.5)
    shot(page, "06-toast-clipboard.png")


# The one roster where the group rule cannot be honoured: two people in one household, one person
# on their own, everyone drawing exactly one adult. This is the case the old engine answered 422.
RELAXED = [
    ("Aline", "Roy", "Roy (chez Gaétan)", "ADULT"),
    ("Benoit", "Roy", "Roy (chez Gaétan)", "ADULT"),
    ("Carole", "Nolin", "Nolin", "ADULT"),
]


def capture_relaxed_draw(browser, auth):
    """Predict the crossing, run the draw anyway, then report it — the whole soft-rule story."""
    admin_api("/api/admin/reset", auth, "POST")
    for person in admin_api("/api/admin/state", auth)["participants"]:
        admin_api(f"/api/admin/participants/{person['id']}", auth, "DELETE")
    admin_api("/api/admin/config", auth, "PUT", {
        "event_title": "Tirage de Noël — trois personnes",
        "budget_note": "25 $ par personne",
        "adult_draws_adult": 1, "adult_draws_child": 0,
        "child_draws_adult": 0, "child_draws_child": 0,
        "avoid_same_group": True,
    })
    for name, family, group, role in RELAXED:
        admin_api("/api/admin/participants", auth, "POST", {
            "name": name, "role": role, "family_group": family, "group_key": group})

    context = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2, locale="fr-CA")
    page = context.new_page()
    goto_console(page)
    page.fill("#pw", PASSWORD)
    page.press("#pw", "Enter")
    enter_console(page, shoot_list=False)     # 18-draw-list.png belongs to the demo roster, not this one
    page.wait_for_function("Alpine.$data(document.querySelector('[x-data]')).busy === false")
    time.sleep(1.0)
    shot(page, "16-group-note.png", full=True)  # the console predicts the crossing
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name=re.compile("Lancer le tirage")).click()
    page.wait_for_selector("text=Le tirage a pigé")
    time.sleep(0.8)
    shot(page, "17-draw-relaxed.png")          # ... and says what it actually did
    context.close()


# The draw list is the first thing an organizer sees, so the gallery needs more than one card on it.
# Copied from the demo roster (people only, fresh tokens) and locked, so the two cards really show
# different statuses. Removed again at the end of the run: everything else in this tool talks to
# "the only draw there is".
SECOND_DRAW = {"event_title": "Tirage du bureau — décembre", "budget_note": "15 $, n'importe quoi de drôle"}


def ensure_second_draw(auth, first_id: int) -> int:
    created = admin_api("/api/admin/draws", auth, "POST",
                        {**SECOND_DRAW, "copy_participants_from": first_id})
    draw_id = created["draw"]["id"]
    # `copy_participants_from` copies the *people*; the rules are the new draw's own business. Set the
    # quotas before drawing, or the copy sits on the server defaults and the draw is refused. From
    # here on there are two draws, so every call has to say which one it means.
    rules = admin_api(f"/api/admin/state?draw_id={first_id}", auth)["config"]
    admin_api(f"/api/admin/config?draw_id={draw_id}", auth, "PUT", {
        "event_title": SECOND_DRAW["event_title"], "budget_note": SECOND_DRAW["budget_note"],
        "adult_draws_adult": rules["adult_draws_adult"], "adult_draws_child": rules["adult_draws_child"],
        "child_draws_adult": rules["child_draws_adult"], "child_draws_child": rules["child_draws_child"],
        "avoid_same_group": rules["avoid_same_group"],
    })
    admin_api(f"/api/admin/draw?draw_id={draw_id}", auth, "POST")   # locked: the list shows both states
    return draw_id


def remove_draw(auth, draw_id: int) -> None:
    admin_api(f"/api/admin/reset?draw_id={draw_id}", auth, "POST")  # a locked draw is refused, not forced
    admin_api(f"/api/admin/draws/{draw_id}", auth, "DELETE")


def cleanup_extra_draws(auth) -> None:
    """Delete every draw but the first. Idempotent, and needed at both ends of the run: everything
    else in this tool talks to "the only draw there is", and an interrupted previous run can leave
    one behind."""
    for extra in admin_api("/api/admin/draws", auth)["draws"][1:]:
        remove_draw(auth, extra["id"])


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    VIDEOS.mkdir(parents=True, exist_ok=True)

    auth = bearer_headers()
    cleanup_extra_draws(auth)         # an interrupted earlier run must not leave a second draw behind
    # Rebuild the demo roster first: every still in the gallery is described against those names,
    # groups and wishlists, so the run has to start from them and not from whatever the instance
    # happens to hold. (The same seed runs again at the end, to leave the instance demo-ready.)
    seed_demo.seed(auth)
    snapshot = admin_api("/api/admin/state", auth)
    if snapshot["config"]["is_locked"]:
        admin_api("/api/admin/reset", auth, "POST")      # the open-state shots need an open draw
        snapshot = admin_api("/api/admin/state", auth)
    tokens = {p["name"]: p["token"] for p in snapshot["participants"]}
    locked = bool(snapshot["config"]["is_locked"])
    if locked:                                           # keep some real "viewed" markers around
        for person in snapshot["participants"][:3]:
            open_link(person["token"])
    config = snapshot["config"]
    adult_draws = config["adult_draws_adult"] + config["adult_draws_child"]
    givers = [p for p in snapshot["participants"]
              if p["viewed_at"] is None and (p["role"] == "ADULT" and adult_draws > 0
                                             or p["role"] == "CHILD"
                                             and config["child_draws_adult"] + config["child_draws_child"] > 0)]
    hero = (givers or snapshot["participants"])[0]["name"]
    hero_url = f"{BASE}/?t={tokens[hero]}"
    print(f"target {BASE}: {len(tokens)} participants, draw locked={locked}, hero={hero}")

    first_draw = admin_api("/api/admin/draws", auth)["draws"][0]["id"]
    second_draw = ensure_second_draw(auth, first_draw)   # so 18-draw-list.png shows the real thing

    manifest = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # ---- organizer, desktop ----
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      device_scale_factor=2, locale="fr-FR")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        page = context.new_page()
        capture_admin_page(page)

        if not locked:
            pending = context.new_page()
            pending.goto(hero_url, wait_until="networkidle")
            pending.wait_for_selector("text=Vérifier encore")
            time.sleep(1.0)
            shot(pending, "07-participant-pending.png")
            pending.close()

            page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name=re.compile("Lancer le tirage")).click()
            page.wait_for_selector("text=Tirage verrouillé")

            # a few participants have already opened their link: the panel tracks that
            others = [p["token"] for p in snapshot["participants"] if p["name"] != hero][:3]
            for link in others:
                open_link(link)
            page.get_by_role("button", name="Rafraîchir").click()
            page.wait_for_selector(r"text=/[1-9]\/\d+ lien\(s\) ouvert/")
            page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
            time.sleep(0.8)
            shot(page, "08-draw-locked.png")
        else:
            page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
            time.sleep(0.6)
            shot(page, "08-draw-locked.png")

        capture_email_console(page)

        page.get_by_role("button", name="EN", exact=True).click()
        page.wait_for_selector("text=Rules & draw quotas")
        page.evaluate("window.scrollTo(0, 340)")
        time.sleep(0.8)
        shot(page, "09-admin-english.png")
        context.close()

        # ---- participant reveal, recorded ----
        view = browser.new_context(viewport={"width": 820, "height": 900},
                                   device_scale_factor=2, locale="fr-FR",
                                   record_video_dir=str(VIDEOS),
                                   record_video_size={"width": 820, "height": 900})
        reveal = view.new_page()
        started = time.monotonic()
        reveal.goto(hero_url, wait_until="networkidle")
        reveal.wait_for_selector("#reveal-btn")
        time.sleep(1.2)
        shot(reveal, "10-reveal-tease.png")
        reveal.eval_on_selector("#reveal-btn", "el => el.scrollIntoView({block:'center'})")
        time.sleep(0.4)
        click_at = time.monotonic() - started
        reveal.click("#reveal-btn")
        time.sleep(0.7)
        shot(reveal, "11-reveal-mid.png")
        time.sleep(1.6)
        shot(reveal, "12-revealed.png", full=True)

        reveal.locator("textarea.field").scroll_into_view_if_needed()
        reveal.fill("textarea.field", "Un jeu de société Stratogo, un livre sur les requins")
        reveal.wait_for_selector("text=Enregistré")
        time.sleep(0.5)
        shot(reveal, "13-wishlist-autosave.png")
        manifest["reveal"] = {"video": str(reveal.video.path()), "click_at": round(click_at, 2)}
        reveal.close()
        view.close()

        # ---- the page behind the hostname (before any login touches the state) ----
        landing = browser.new_page()
        capture_family_page(landing)
        landing.close()

        # ---- organizer flow, recorded (unlock -> panels -> toast) ----
        view = browser.new_context(viewport={"width": 1180, "height": 760},
                                   record_video_dir=str(VIDEOS),
                                   record_video_size={"width": 1180, "height": 760})
        view.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        flow = view.new_page()
        started = time.monotonic()
        goto_console(flow)
        time.sleep(1.0)
        typed_at = time.monotonic() - started
        flow.type("#pw", PASSWORD, delay=90)
        flow.press("#pw", "Enter")
        enter_console(flow, shoot_list=False)      # login lands on the draw list now
        time.sleep(1.6)
        flow.evaluate("window.scrollTo({top: 260, behavior: 'smooth'})")
        time.sleep(1.2)
        flow.get_by_role("button", name="Copier tous les liens").click()
        time.sleep(2.2)
        manifest["flow"] = {"video": str(flow.video.path()),
                            "start_at": round(typed_at - 0.5, 2),
                            "duration": round(time.monotonic() - started - typed_at + 0.5, 2)}
        flow.close()
        view.close()

        # ---- mobile ----
        mobile = browser.new_context(viewport={"width": 390, "height": 844},
                                     device_scale_factor=3, locale="fr-FR",
                                     is_mobile=True, has_touch=True)
        mob = mobile.new_page()
        mob.goto(hero_url, wait_until="networkidle")
        mob.wait_for_selector("#reveal-btn")
        time.sleep(1.0)
        mob.click("#reveal-btn")
        time.sleep(2.2)
        shot(mob, "14-mobile-participant.png", full=True)
        mob2 = mobile.new_page()
        goto_console(mob2)
        mob2.fill("#pw", PASSWORD)
        mob2.press("#pw", "Enter")
        enter_console(mob2, shoot_list=False)
        time.sleep(1.2)
        shot(mob2, "15-mobile-admin.png")
        mobile.close()

        # back to a single draw: the stages below talk to "the only draw there is"
        remove_draw(auth, second_draw)

        # ---- the group rule bent on purpose (last, because it rewrites the roster) ----
        capture_relaxed_draw(browser, auth)
        browser.close()

    seed_demo.seed(auth)
    print("re-seeded the demo roster — the last two stills used a 3-person test roster")

    (MEDIA / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("manifest:", MEDIA / "manifest.json")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
