"""Capture the README media with Playwright against a running instance.

    GE_BASE=http://127.0.0.1:8085 python tools/capture_docs_media.py

Writes PNG stills to docs/screenshots/, two .webm recordings to a temp directory,
and docs/media/manifest.json describing where the animations start inside each video
(tools/build_docs_media.py turns those into the README GIFs).

Requires Playwright (`uv pip install playwright && playwright install chromium`). This is a
docs-only tool: the app itself has no test/docs dependencies.
"""

import json
import os
import pathlib
import re
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
REPO = pathlib.Path(__file__).resolve().parents[1]
SHOTS = REPO / "docs" / "screenshots"
MEDIA = REPO / "docs" / "media"
VIDEOS = pathlib.Path(os.environ.get("GE_VIDEO_DIR", "/tmp/opencode/videos"))


def bearer_headers():
    """One login per run: the login endpoint is rate limited on purpose."""
    body = json.dumps({"password": PASSWORD}).encode()
    request = urllib.request.Request(
        BASE + "/api/admin/login", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(request).read())["token"]
    return {"Authorization": "Bearer " + token}


def admin_api(path, headers, method="GET"):
    request = urllib.request.Request(BASE + path, data=b"{}" if method == "POST" else None,
                                     method=method,
                                     headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(request).read())


def open_link(token):
    """Open one participant link so the roster shows a real 'viewed' marker."""
    with urllib.request.urlopen(f"{BASE}/api/draw/{token}") as response:
        return response.status


def shot(page, name, full=False):
    page.screenshot(path=str(SHOTS / name), full_page=full, animations="allow")
    print("  still:", name)


def capture_admin_page(page):
    """Organizer flow: login, rules, warnings, roster, filters, clipboard, draw, EN."""
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#pw")
    time.sleep(1.2)
    shot(page, "01-login.png")

    page.fill("#pw", PASSWORD)
    page.press("#pw", "Enter")
    page.wait_for_selector("text=Copier tous les liens")
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

    page.locator("text=Liste des participants").scroll_into_view_if_needed()
    time.sleep(0.6)
    shot(page, "04-roster.png")
    page.get_by_role("button", name=re.compile(r"^Martin")).first.click()
    time.sleep(0.6)
    shot(page, "05-family-filter.png")
    page.get_by_role("button", name="Afficher tout le monde").click()
    time.sleep(0.4)

    page.get_by_role("button", name="Copier tous les liens").click()
    page.wait_for_selector("#toasts > div")
    time.sleep(0.5)
    shot(page, "06-toast-clipboard.png")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    VIDEOS.mkdir(parents=True, exist_ok=True)

    auth = bearer_headers()
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
            pending.wait_for_selector("text=Vérifier à nouveau")
            time.sleep(1.0)
            shot(pending, "07-participant-pending.png")
            pending.close()

            page.locator("text=Générer et verrouiller").scroll_into_view_if_needed()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name=re.compile("Générer et verrouiller")).click()
            page.wait_for_selector("text=Tirage verrouillé")

            # a few participants have already opened their link: the panel tracks that
            others = [p["token"] for p in snapshot["participants"] if p["name"] != hero][:3]
            for link in others:
                open_link(link)
            page.get_by_role("button", name="Rafraîchir").click()
            page.wait_for_selector(r"text=/[1-9]\/\d+ lien\(s\) ouvert/")
            page.locator("text=Tirage au sort").scroll_into_view_if_needed()
            time.sleep(0.8)
            shot(page, "08-draw-locked.png")
        else:
            page.locator("text=Tirage au sort").scroll_into_view_if_needed()
            time.sleep(0.6)
            shot(page, "08-draw-locked.png")

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

        # ---- organizer flow, recorded (unlock -> panels -> toast) ----
        view = browser.new_context(viewport={"width": 1180, "height": 760},
                                   record_video_dir=str(VIDEOS),
                                   record_video_size={"width": 1180, "height": 760})
        view.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        flow = view.new_page()
        started = time.monotonic()
        flow.goto(BASE, wait_until="networkidle")
        flow.wait_for_selector("#pw")
        time.sleep(1.0)
        typed_at = time.monotonic() - started
        flow.type("#pw", PASSWORD, delay=90)
        flow.press("#pw", "Enter")
        flow.wait_for_selector("text=Copier tous les liens")
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
        mob2.goto(BASE, wait_until="networkidle")
        mob2.fill("#pw", PASSWORD)
        mob2.press("#pw", "Enter")
        mob2.wait_for_selector("text=Copier tous les liens")
        time.sleep(1.2)
        shot(mob2, "15-mobile-admin.png")
        mobile.close()
        browser.close()

    (MEDIA / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("manifest:", MEDIA / "manifest.json")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
