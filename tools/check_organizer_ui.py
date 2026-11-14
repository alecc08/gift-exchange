"""Drive the organizer console in a browser and fail loudly on anything pytest cannot see.

    GE_BASE=http://127.0.0.1:8085 ADMIN_PASSWORD=<pw> python tools/check_organizer_ui.py

The Python suite covers the API and the engine; `tests/test_i18n.py` covers the strings and
`tests/test_frontend.py` the SPA's static shape. What is left is the glue in `static/index.html` —
Alpine getters, the `0/1` ↔ boolean coercion behind the group checkbox, the two notices the console
shows about groups, the draw list, and the phone-width layout. Those break silently (a getter that
throws renders an empty box), so this walks the exact paths:

  1. land on the **draw list**, open a draw, and come back to the list;
  2. turn "Éviter les piges dans le même groupe" off, save, reload — it must still be off and the
     panel must not read "unsaved changes" (the bug that would happen if one copy kept 0 and the
     other kept false);
  3. add a roster with two households, one of them two people deep, and filter by group;
  4. check the console *predicts* the crossing the arithmetic forces, that the draw button stays
     enabled (a relaxable rule must not block the draw), and that it then *reports* it;
  5. reset, add a person who makes a clean draw possible, and check the notice flips;
  6. open a **second draw**, check its roster is empty and the first one's is untouched, that a
     locked draw refuses to be deleted, and that deleting the second one works;
  7. at 390 px, check the roster renders as cards and nothing sticks out of the viewport;
  8. switch to EN and fail on any raw i18n key left on screen;
  9. leave the console and check the family's half of the same bundle at 390 px: the hostname, a
     mistyped address and a stale link from the first mailing all render the landing card, with no
     password field, no echoed token, no horizontal scroll and no raw key in either language.

Exits non-zero on a failed assertion, a console error, or a page error. The Tailwind CDN warning is
ignored: it is the documented cost of a no-build frontend.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# The console is not the site root any more — "/" is the family's page. Pass GE_ADMIN_PATH when the
# instance runs with ADMIN_PATH set; the app does not advertise the path, so the tool takes it as an
# argument like it takes the base URL and the password.
ADMIN_PATH = os.environ.get("GE_ADMIN_PATH", "/admin")
DATA = "Alpine.$data(document.querySelector('[x-data]'))"

# [full name, family name, group] — Roy (chez Gaétan) holds two people, so one gift has to cross.
ROSTER = [("Aline", "Roy", "Roy (chez Gaétan)"),
          ("Benoit", "Roy", "Roy (chez Gaétan)"),
          ("Carole", "Nolin", "Nolin")]


class Console:
    """Collects browser complaints, with one allowance the checks themselves provoke.

    A deliberate refusal (deleting a locked draw answers `400`) makes Chromium log a failed
    resource, exactly like an accidental one. `allow()` pre-authorises one occurrence of an exact
    status, so the check can prove the refusal happened without tripping its own alarm.
    """

    def __init__(self, page):
        self.problems = []
        self.allowed: dict[str, int] = {}
        page.on("console", lambda msg: self.record(f"console {msg.type}: {msg.text}")
                if msg.type in ("error", "warning") and "cdn.tailwindcss.com" not in msg.text else None)
        page.on("pageerror", lambda error: self.record(f"pageerror: {error}"))

    def allow(self, fragment: str) -> None:
        self.allowed[fragment] = self.allowed.get(fragment, 0) + 1

    def record(self, text: str) -> None:
        for fragment, count in list(self.allowed.items()):
            if count > 0 and fragment in text:
                self.allowed[fragment] = count - 1
                return
        self.problems.append(text)


def main() -> int:
    if not PASSWORD:
        print("set ADMIN_PASSWORD (and GE_BASE if not http://127.0.0.1:8085)")
        return 2

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 950}, locale="fr-CA").new_page()
        problems = Console(page)

        def idle():
            """Every action ends in a fetch: wait it out, or the `:disabled="busy"` guards make the
            next click time out for reasons that have nothing to do with the feature."""
            page.wait_for_function(f"{DATA}.busy === false")

        def add_person(name, family, group):
            page.fill("#p-name", name)
            page.fill("#p-family", family)
            page.fill("#p-group", group)
            page.wait_for_function(f"{DATA}.draftReady === true")
            page.get_by_role("button", name="Ajouter").click()
            idle()

        page.goto(BASE + ADMIN_PATH, wait_until="networkidle")
        page.wait_for_selector("#pw")
        page.fill("#pw", PASSWORD)
        page.press("#pw", "Enter")

        # The list screen is the entry point now: one server can hold several exchanges, and the
        # console only exists once one is open.
        page.wait_for_selector("text=Choisis un tirage")
        page.wait_for_selector("#draw-cards article")   # the heading is static; the cards are a fetch
        cards = page.locator("#draw-cards article")
        assert cards.count() >= 1, "the draw list opened empty on an instance that has draws"
        first_title = cards.first.locator("h3").inner_text()
        cards.first.get_by_role("button", name="Ouvrir").click()
        page.wait_for_selector("text=Copier tous les liens")
        assert "?draw=" in page.url, \
            "opening a draw did not put it in the URL, so a reload would lose it"

        # Start from a clean slate using the session the console already has, so the check is
        # repeatable against an instance someone else left mid-event.
        page.evaluate("""async () => {
          const headers = { 'Content-Type': 'application/json',
                            Authorization: 'Bearer ' + sessionStorage.getItem('ge_admin_token') };
          await fetch('/api/admin/reset', { method: 'POST', headers, body: '{}' });
          await fetch('/api/admin/config', { method: 'PUT', headers, body: JSON.stringify({
            adult_draws_adult: 1, adult_draws_child: 0,
            child_draws_adult: 0, child_draws_child: 0, avoid_same_group: true }) });
          const state = await (await fetch('/api/admin/state', { headers })).json();
          for (const person of state.participants) {
            await fetch(`/api/admin/participants/${person.id}`, { method: 'DELETE', headers });
          }
        }""")
        page.reload(wait_until="networkidle")
        page.wait_for_selector("text=Copier tous les liens")

        checkbox = page.locator("input[type=checkbox]").first
        assert checkbox.is_checked(), "avoid_same_group should start on"
        checkbox.click()
        page.wait_for_selector("text=Il y a des changements pas enregistrés")
        page.get_by_role("button", name=re.compile("Enregistrer les règles")).click()
        page.wait_for_selector("text=Règles enregistrées")
        idle()

        page.reload(wait_until="networkidle")
        page.wait_for_selector("text=Copier tous les liens")
        assert not page.locator("input[type=checkbox]").first.is_checked(), "the off state did not stick"
        assert page.locator("text=Il y a des changements pas enregistrés").first.is_hidden(), \
            "the panel reads dirty straight after a save — the 0/1 and boolean copies disagree"
        page.wait_for_selector("text=La règle des groupes est désactivée")
        checkbox.click()
        page.get_by_role("button", name=re.compile("Enregistrer les règles")).click()
        idle()

        for name, family, group in ROSTER:
            add_person(name, family, group)
        # The add is a fetch, and the row list is a second one after it: wait for the roster to
        # actually show three people before counting, or the count is whatever happened to be in.
        page.wait_for_function(
            "() => [...document.querySelectorAll('#roster-table tbody tr.part')]"
            ".filter(tr => tr.offsetParent !== null).length === %d" % len(ROSTER))
        # `.part` marks a person row: the table also carries a "nobody here yet" row, which would
        # otherwise be counted as a person.
        rows = page.locator("#roster-table tbody tr.part:visible")
        assert rows.count() == 3, f"expected 3 roster rows, saw {rows.count()}"
        page.get_by_role("button", name=re.compile(r"^Roy \(chez Gaétan\)")).first.click()
        page.wait_for_selector("text=Montre tout le monde")
        assert rows.count() == 2, "filtering by group did not narrow the table"
        page.get_by_role("button", name="Montre tout le monde").click()

        page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
        page.wait_for_selector("text=devra bien piger 1 fois")
        draw = page.get_by_role("button", name=re.compile("Lancer le tirage"))
        assert draw.is_enabled(), "a note about groups must not disable the draw"
        page.once("dialog", lambda dialog: dialog.accept())
        draw.click()
        page.wait_for_selector("text=Le tirage a pigé 1 fois")
        print("reported:", page.locator("text=Le tirage a pigé").first.inner_text())

        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Recommencer le tirage").click()
        page.wait_for_selector("text=Tirage recommencé")
        add_person("Gaétan", "Tremblay", "Tremblay (chez Réjean)")
        page.locator("h2", has_text="Le tirage").scroll_into_view_if_needed()
        assert page.locator("text=devra bien piger").first.is_hidden(), \
            "the console still predicts a crossing that is no longer needed"
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name=re.compile("Lancer le tirage")).click()
        page.wait_for_selector("text=Personne a pigé dans son propre groupe")

        # ------------------------------------------------------------------ #
        # Several draws on one server: the list keeps them apart
        # ------------------------------------------------------------------ #
        page.get_by_role("button", name="Tous les tirages").click()
        page.wait_for_selector("text=Choisis un tirage")
        assert cards.count() == 1, f"expected one draw on the demo instance, saw {cards.count()}"

        page.fill("#d-title", "Échange du bureau")
        page.get_by_role("button", name="Créer le tirage").click()
        idle()
        page.wait_for_selector("#roster-table")
        page.wait_for_selector("text=Il n’y a personne dans la liste")
        assert page.locator("#roster-table tbody tr.part:visible").count() == 0, \
            "a new draw came with people it should not have"

        page.get_by_role("button", name="Tous les tirages").click()
        page.wait_for_function(f"{DATA}.draws.length === 2")   # the list is a fetch, the heading is not
        assert cards.count() == 2, "the new draw is not on the list"
        page.locator("#draw-cards article", has_text=first_title).get_by_role("button", name="Ouvrir").click()
        page.wait_for_selector("#roster-table")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#roster-table tbody tr.part')]"
            ".filter(tr => tr.offsetParent !== null).length === 4")
        assert page.locator("#roster-table tbody tr.part:visible").count() == 4, \
            "opening a second draw changed the first one's roster"
        drawn_tokens = set(page.evaluate(f"{DATA}.participants.map(p => p.token)"))

        # ------------------------------------------------------------------ #
        # "Rejouer avec ces personnes": the per-card shortcut to `copy_participants_from`, which is
        # the only way an organizer is likely to reach it. Three things are worth pinning: the button
        # only fills the form (nothing is created until "Créer"), the people come across, and the
        # links do not — a last season's pairing must not become this season's credential.
        # ------------------------------------------------------------------ #
        page.get_by_role("button", name="Tous les tirages").click()
        page.wait_for_selector("text=Choisis un tirage")
        source_id = page.evaluate(f"{DATA}.draws.find(d => d.event_title === {first_title!r}).id")
        page.locator("#draw-cards article", has_text=first_title).get_by_role(
            "button", name="Rejouer avec ces personnes").click()
        page.wait_for_function(f"Number({DATA}.drawDraft.copy_from) === {source_id}", timeout=5000)
        assert page.locator("#d-copy").input_value() == str(source_id), \
            "the card button did not name the source draw in the form"
        assert page.input_value("#d-title") == first_title, \
            "the card button offered a different name than the card it was clicked on"
        assert cards.count() == 2, "the copy button created a draw by itself"

        page.fill("#d-title", "Copie du roster")     # renaming is the normal move, and it keeps the
        page.get_by_role("button", name="Créer le tirage").click()   # two cards tellable apart below
        page.wait_for_selector("#roster-table")
        page.wait_for_function(f"{DATA}.cfg.is_locked === 0")
        assert page.locator("#roster-table tbody tr.part:visible").count() == 4, \
            "the copy did not bring the four people across"
        copied_tokens = set(page.evaluate(f"{DATA}.participants.map(p => p.token)"))
        assert copied_tokens and not (copied_tokens & drawn_tokens), \
            "a copied draw reused links that are already sitting in somebody's inbox"

        # This draw has been drawn, so deleting it has to be refused *and said*.
        page.get_by_role("button", name="Tous les tirages").click()
        page.wait_for_selector("text=Choisis un tirage")
        page.wait_for_function(f"{DATA}.draws.length === 3")   # the list is a fetch, the heading is not
        assert cards.count() == 3, "the copied draw is not on the list"
        page.once("dialog", lambda dialog: dialog.accept())
        problems.allow("status of 400")   # the refusal this check is proving
        page.locator("#draw-cards article", has_text=first_title).get_by_role("button", name="Supprimer").click()
        page.wait_for_selector("text=Le tirage est verrouillé")
        assert cards.count() == 3, "a locked draw was deleted"

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#draw-cards article", has_text="Échange du bureau").get_by_role("button", name="Supprimer").click()
        page.wait_for_function(f"{DATA}.draws.length === 2")

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#draw-cards article", has_text="Copie du roster").get_by_role("button", name="Supprimer").click()
        page.wait_for_function(f"{DATA}.draws.length === 1")

        # ------------------------------------------------------------------ #
        # Phone width: the roster is cards, and nothing hangs off the screen
        # ------------------------------------------------------------------ #
        page.locator("#draw-cards article").first.get_by_role("button", name="Ouvrir").click()
        page.wait_for_selector("#roster-table")
        # A real token, for the stale-link case at the end. It is never printed and never sent
        # anywhere: the only question asked about it is "does this string appear in the response".
        participant_token = page.evaluate(f"{DATA}.participants[0].token") or \
            "00000000-0000-0000-0000-000000000000"
        for width in (390, 360):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(300)
            offscreen = page.evaluate("""() => {
              const vw = document.documentElement.clientWidth;
              return [...document.querySelectorAll('body *')].filter(el => {
                const r = el.getBoundingClientRect();
                if (!r.width || !r.height) return false;
                const cs = getComputedStyle(el);
                if (cs.position === 'fixed' || el.closest('#snow')) return false;
                return r.right > vw + 1 || r.left < -1;
              }).map(el => el.tagName.toLowerCase() + ' ' + (typeof el.className === 'string' ? el.className : ''));
            }""")
            assert not offscreen, f"{width}px: outside the viewport: {offscreen[:3]}"
            assert page.evaluate("document.documentElement.scrollWidth") <= \
                page.evaluate("document.documentElement.clientWidth") + 1, f"{width}px: horizontal scroll"
        assert page.evaluate("getComputedStyle(document.querySelector('#roster-table thead')).display") == "none", \
            "the roster is still a wide table at phone width"
        unlabelled = page.evaluate(
            "[...document.querySelectorAll('#roster-table tr.part td')].filter(td => !td.dataset.label).length")
        assert unlabelled == 0, "a card cell lost the column name it is labelled with"
        page.set_viewport_size({"width": 1440, "height": 950})
        page.wait_for_timeout(250)

        page.get_by_role("button", name="EN", exact=True).click()
        page.wait_for_selector("text=Avoid picks within the same group")
        page.wait_for_selector("text=Nobody drew inside their own group.")
        assert not page.locator(r"text=/\b(landing|draw|roster|rules|note|warn)\.[a-zA-Z0-9_.]+/").count(), \
            "a raw i18n key is visible on screen"

        # ------------------------------------------------------------------ #
        # The family's half of the same bundle: the root, a mistyped address and a stale link are
        # all the landing card. The console is guarded by the password; this half is guarded by
        # nothing, and its whole job is to not be a login form, so it gets checked here too.
        # ------------------------------------------------------------------ #
        page.set_viewport_size({"width": 390, "height": 844})
        for path, why in (("/", "the hostname the email carries"),
                          ("/Reveillon", "a fat-fingered address"),
                          (f"/draw/{participant_token}", "a link from the first mailing")):
            page.goto(BASE + path, wait_until="networkidle")
            assert page.evaluate("window.__GE_ORGANIZER__ === false"), f"{path}: not the family's page"
            if page.query_selector("#pw"):
                problems.record(f"{path}: a password field on the page that is only {why}")
            heading = page.locator("h2").first.inner_text().strip()
            assert heading, f"{path}: the landing card rendered an empty heading"
            # The root is an explanatory page now, not one card: about two dozen strings, several
            # headings, three steps. A page that long is a page where a key can go missing quietly,
            # so every landing string the markup asks for has to have become visible text here.
            strings = page.evaluate("""() => [...document.querySelectorAll(`[x-text^="t('landing."]`)]
              .map(el => [el.getAttribute('x-text'), (el.textContent || '').trim()])""")
            assert len(strings) >= 24, f"{path}: only {len(strings)} landing strings in the markup"
            blank = [expr for expr, text in strings if not text]
            assert not blank, f"{path}: rendered with no text: {blank[:3]}"
            assert page.locator("section").count() >= 5, \
                f"{path}: the landing page lost its sections ({page.locator('section').count()} sections)"
            if participant_token in page.content():
                problems.record(f"{path}: the token came back in the served document")
            width_check = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert width_check <= 1, f"{path}: horizontal scroll at 390 px ({width_check}px)"
            page.get_by_role("button", name="EN", exact=True).click()
            page.wait_for_timeout(200)
            assert not page.locator(r"text=/\b(landing|draw|roster|rules|note|warn)\.[a-zA-Z0-9_.]+/").count(), \
                f"{path}: a raw i18n key on the family's page in English"
            title_en = page.title()
            page.get_by_role("button", name="FR", exact=True).click()
            page.wait_for_timeout(200)
            assert title_en and page.title() != title_en, \
                f"{path}: the tab title ignores the language switch ({title_en!r} / {page.title()!r})"
        browser.close()

    for problem in dict.fromkeys(problems.problems):
        print(problem)
    if problems.problems:
        return 1
    print("organizer console OK: no console errors, no page errors, no raw i18n keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
