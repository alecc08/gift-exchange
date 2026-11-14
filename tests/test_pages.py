"""Which path serves which screen, and what each screen is allowed to show.

The organizer console used to be the site root, so a relative who typed the hostname from the email
met a password form they had no business seeing — and every bookmark, dashboard card and screenshot
pointed at the same page the family was meant to read. That is a product bug rather than a security
one (the console is guarded by the login limit, the lockout and the password, never by the path), so
what these tests pin is the shape of the site: the two shells, the one flag that tells them apart,
and the catch-all that replaced the old mailed-link shim — including the parts it must *not* swallow.
"""

import re

import pytest

import main
from conftest import add

ORGANIZER_FLAG = "window.__GE_ORGANIZER__"


def meta_block(body: str) -> str:
    return "\n".join(line for line in body.splitlines()
                     if '<meta property="og:' in line or '<meta name="twitter:' in line)


# --------------------------------------------------------------------------- #
# The two shells
# --------------------------------------------------------------------------- #


def test_the_console_is_served_at_its_own_path(client):
    page = client.get(main.ORGANIZER_PATH)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert f"{ORGANIZER_FLAG}=true" in page.text
    # the marker is replaced, never shipped as a comment
    assert main.ROUTE_MARKER not in page.text
    # A search result for the organizer login is exactly the accidental discovery this path is for.
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    # A console is not something anybody shares; og:url pointing at the family's page from it would
    # be a lie, and the tags would hand a crawler a reason to come looking here.
    assert not meta_block(page.text)


def test_the_root_is_the_family_page(client):
    page = client.get("/")
    assert page.status_code == 200
    assert f"{ORGANIZER_FLAG}=false" in page.text
    assert "x-robots-tag" not in page.headers
    assert meta_block(page.text), "the family's page is the one that gets shared, so it keeps the card"


def test_both_shells_are_the_same_document_except_for_the_route_flag(client):
    """One SPA, one file: the only server-side difference is which screen it opens on."""
    family = client.get("/").text.replace(f"{ORGANIZER_FLAG}=false", "")
    console = client.get(main.ORGANIZER_PATH).text.replace(f"{ORGANIZER_FLAG}=true", "")
    assert family.replace(meta_block(client.get("/").text), "") == \
        console.replace(meta_block(client.get("/").text), "")


def test_the_shell_is_still_no_cache(client):
    """Both documents are stamped per route, so neither may be cached by a browser or a proxy."""
    for path in ("/", main.ORGANIZER_PATH):
        assert client.get(path).headers["cache-control"] == "no-cache"


def test_a_private_link_reaches_the_participant_screen_not_the_console(client):
    """`?t=<uuid>` is a query parameter, so it wins over the route — including at odd paths."""
    token = add(client, "Ghislaine", "ADULT", "Bernier")["token"]
    for path in ("/", f"/{token}"):
        page = client.get(path, params={"t": token})
        assert f"{ORGANIZER_FLAG}=false" in page.text
        assert token not in page.text, "the token stays in the URL; the served document never echoes it"


# --------------------------------------------------------------------------- #
# Nothing on the family's page may point at the console
# --------------------------------------------------------------------------- #


def test_the_spa_never_names_the_console_path(client):
    """The flag decides the screen, so nothing in the page has to name the console — and nothing may.

    `/api/admin/…` is the API and stays; a bare `/admin` in a link, a fetch or a `replaceState` would
    hand back the one thing the move was meant to stop: the console being one click, or one guessed
    path, away from the page the family reads.
    """
    for document in (main.INDEX_HTML, client.get("/").text):
        # `(?<!/api)` — the API lives under /api/admin/* and that part stays.
        for match in re.finditer(r"(?<!/api)/admin", document):
            context = document[max(0, match.start() - 40):match.start() + 30].replace("\n", " ")
            raise AssertionError(f"the SPA names the console path: …{context}…")
    assert "window.__GE_ORGANIZER__" in main.INDEX_HTML


def test_a_page_opened_without_the_flag_is_never_the_console():
    """Fail closed: the SPA asks `window.__GE_ORGANIZER__ === true`, so a document with no flag — a
    stale cache, a page saved from disk, a shell served by something else — opens the family's page."""
    assert "window.__GE_ORGANIZER__ === true" in main.INDEX_HTML
    assert "mode: 'landing'" in main.INDEX_HTML, "the safe default is what the component starts on"


def test_the_password_form_only_lives_inside_the_console_template():
    """Alpine renders one of the three templates; the login card must sit inside the console's."""
    html = main.INDEX_HTML
    console_open = html.index("""<template x-if="mode === 'admin'">""")
    landing_open = html.index("""<template x-if="mode === 'landing'">""")
    participant_open = html.index("""<template x-if="mode === 'participant'">""")
    assert console_open < landing_open < participant_open
    assert console_open < html.index("t('login.title')") < landing_open

    landing = html[landing_open:participant_open]
    for forbidden in ("<form", "password", "mot de passe", "login.", "admin."):
        assert forbidden not in landing.lower(), f"the landing card mentions {forbidden!r}"


def test_no_navigation_sends_an_organizer_back_to_the_site_root():
    """`/?draw=<id>` and `replaceState(…, '/')` were how a signed-in organizer got ejected from the
    console onto the family's page. The console names its own path instead."""
    assert "'/?draw=" not in main.INDEX_HTML
    assert "replaceState({}, '', '/')" not in main.INDEX_HTML
    assert "this.consoleUrl(" in main.INDEX_HTML


def test_the_spa_builds_a_participant_link_in_exactly_one_place():
    """Two builders is how the console and the mail disagreed once already."""
    assert main.INDEX_HTML.count("/?t=${") == 1


# --------------------------------------------------------------------------- #
# The catch-all, and what it must not swallow
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/nope", "/Reveillon", "/nope/deep", "/Réveillon-2026"])
def test_a_mistyped_address_gets_the_landing_page_not_a_json_error(client, path):
    page = client.get(path)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert f"{ORGANIZER_FLAG}=false" in page.text
    assert "giftExchange" in page.text


def test_the_catch_all_leaves_the_api_alone(client):
    """A machine asking for an endpoint must get JSON, or a broken client fails silently."""
    wrong = client.get("/api/admin/stat")
    assert wrong.status_code == 404
    assert wrong.json() == {"detail": "Not Found"}

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/draw/nope").status_code == 404
    # the mount and the parameterised routes are matched before the catch-all
    art = client.get("/static/og-image.png")
    assert art.status_code == 200 and art.headers["content-type"] == "image/png"
    # and a POST to a nonsense path is still the API's problem, never a rendered page
    refused = client.post("/api/admin/nope", json={})
    assert refused.status_code in (404, 405), refused.status_code
    assert "text/html" not in refused.headers["content-type"]


def test_the_console_path_with_anything_after_it_is_not_a_welcome_page(client):
    """`/admin/settings` is a probe, not a relative: it gets the API's answer, not a warm card."""
    for path in (f"{main.ORGANIZER_PATH}/settings", f"{main.ORGANIZER_PATH}/",
                 f"{main.ORGANIZER_PATH}/login"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "text/html" not in response.headers["content-type"], path


@pytest.mark.parametrize("path", ["/favicon.ico", "/apple-touch-icon.png", "/openapi.json",
                                  "/docs", "/redoc", "/api", "/.well-known/security.txt"])
def test_an_asset_or_a_schema_path_is_never_answered_with_the_landing_card(client, path):
    """The catch-all is for relatives who fat-fingered a hostname, not for every unmatched GET.

    A browser asking for `/favicon.ico` and getting 200 `text/html` renders nothing visible and hides
    a missing icon for months; a scanner asking for `/openapi.json` and getting a festive card is
    being told "this page exists" by a server that means the opposite. Both get the API's 404.
    """
    response = client.get(path)
    assert response.status_code == 404, f"{path} answered {response.status_code}"
    assert "text/html" not in response.headers["content-type"], path
    assert response.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- #
# ADMIN_PATH
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("configured", "expected"), [
    (None, "/admin"),
    ("", "/admin"),
    ("   ", "/admin"),
    ("/", "/admin"),
    ("//", "/admin"),
    ("admin", "/admin"),                       # the intent is obvious, so it is corrected
    ("/Admin/", "/Admin"),
    ("/console/priv", "/console/priv"),
    ("/a/./b", "/a/b"),                        # a `.` segment adds nothing
    ("/api", "/admin"),                        # the server owns these
    ("/api/v1", "/admin"),
    ("/static", "/admin"),
    ("/openapi.json", "/admin"),
    ("/docs", "/admin"),                       # switched off on purpose; don't hide behind them
    ("/redoc", "/admin"),
    ("/../etc", "/admin"),                     # nothing that would have to be escaped again
    ("/a b", "/admin"),
    ("/a%20b", "/admin"),
    ("/café", "/admin"),
    ("/" + "x" * 200, "/admin"),
])
def test_the_organizer_path_is_corrected_or_refused(configured, expected):
    assert main.normalize_organizer_path(configured) == expected


def test_the_console_route_is_registered_and_the_catch_all_is_last():
    """`ADMIN_PATH` is read at import, so the route table is the proof that it was honoured — and the
    catch-all being registered last is the proof that it cannot shadow the API or the static mount."""
    paths = [getattr(route, "path", "") for route in main.app.routes]
    assert paths.count(main.ORGANIZER_PATH) == 1
    assert paths.count("/") == 1
    assert paths[-1] == "/{unmatched:path}", "registered earlier, the catch-all would answer /api/*"
