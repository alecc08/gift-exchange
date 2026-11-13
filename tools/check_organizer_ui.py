"""Drive the organizer console in a browser and fail loudly on anything pytest cannot see.

    GE_BASE=http://127.0.0.1:8085 ADMIN_PASSWORD=<pw> python tools/check_organizer_ui.py

The Python suite covers the API and the engine; `tests/test_i18n.py` covers the strings. What is
left is the glue in `static/index.html` — Alpine getters, the `0/1` ↔ boolean coercion behind the
group checkbox, and the two notices the console shows about groups. Those break silently (a getter
that throws renders an empty box), so this walks the exact path the feature adds:

  1. turn « Éviter les piges dans le même groupe » off, save, reload — it must still be off and the
     panel must not read "unsaved changes" (the bug that would happen if one copy kept 0 and the
     other kept false);
  2. add a roster with two households, one of them two people deep, and filter by group;
  3. check the console *predicts* the crossing the arithmetic forces, that the draw button stays
     enabled (a relaxable rule must not block the draw), and that it then *reports* it;
  4. reset, add a person who makes a clean draw possible, and check the notice flips;
  5. switch to EN and fail on any raw i18n key left on screen.

Exits non-zero on a failed assertion, a console error, or a page error. The Tailwind CDN warning is
ignored: it is the documented cost of a no-build frontend.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DATA = "Alpine.$data(document.querySelector('[x-data]'))"

# [full name, family name, group] — Roy (chez Gaétan) holds two people, so one gift has to cross.
ROSTER = [("Aline", "Roy", "Roy (chez Gaétan)"),
          ("Benoit", "Roy", "Roy (chez Gaétan)"),
          ("Carole", "Nolin", "Nolin")]


class Console:
    def __init__(self, page):
        self.problems = []
        page.on("console", lambda msg: self.record(f"console {msg.type}: {msg.text}")
                if msg.type in ("error", "warning") and "cdn.tailwindcss.com" not in msg.text else None)
        page.on("pageerror", lambda error: self.record(f"pageerror: {error}"))

    def record(self, text):
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

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#pw")
        page.fill("#pw", PASSWORD)
        page.press("#pw", "Enter")
        page.wait_for_selector("text=Copier tous les liens")

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
        rows = page.locator("#roster-table tbody tr:visible")
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

        page.get_by_role("button", name="EN", exact=True).click()
        page.wait_for_selector("text=Avoid picks within the same group")
        page.wait_for_selector("text=Nobody drew inside their own group.")
        assert not page.locator(r"text=/\b(draw|roster|rules|note|warn)\.[a-zA-Z]+/").count(), \
            "a raw i18n key is visible on screen"
        browser.close()

    for problem in dict.fromkeys(problems.problems):
        print(problem)
    if problems.problems:
        return 1
    print("organizer console OK: no console errors, no page errors, no raw i18n keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
