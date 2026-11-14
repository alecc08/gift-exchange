"""Static guards over the SPA's JavaScript.

The frontend is one file of Alpine with no build step and no bundler, so nothing checks its shape
before a human clicks it. Two mistakes here are invisible until the page is open: an Alpine scope
assumption (markup can call a component method by bare name, JavaScript cannot) and an admin call
that forgot which draw it belongs to. Both are regex-detectable, so they are tests.
"""

import re

import main

APP_SCRIPT_RE = re.compile(r"<script[^>]*>(?:(?!</script>).)*function giftExchange(?:(?!</script>).)*</script>",
                           re.DOTALL)
# Methods of the component object. Markup may call them bare (`x-text="formatDate(...)"` — Alpine
# puts the component on its scope chain); code inside the component cannot.
COMPONENT_METHODS = ("formatDate", "relative", "locale", "t", "toast", "drawsOf", "looksLikeEmail",
                     "drawPath", "showAdminError", "clearAdminError", "loadState", "loadDraws",
                     "parseDate", "tallyBy", "addressKey", "participantUrl", "consoleUrl",
                     "flashFlag", "enterAdmin",
                     # The participant page's note composer: `mine(a)` in markup is fine, `mine(a)` in
                     # JavaScript is a ReferenceError, and Alpine's absence of an error message made
                     # that expensive once.
                     "mine", "planOpen", "startPlan", "closePlan", "savePlan")
CALL_RE = re.compile(r"([\w$]*)\s*\(")
API_CALL_RE = re.compile(r"this\.api\((`[^`]*`|'[^']*')")
# Not draw-scoped by design: the list of draws, and the two calls that happen before a draw exists.
NOT_DRAW_SCOPED = ("/api/admin/draws", "/api/admin/login", "/api/admin/logout")


def app_script() -> tuple[str, int]:
    """The inline Alpine script, and the line of index.html where it starts."""
    found = APP_SCRIPT_RE.search(main.INDEX_HTML)
    assert found, "the Alpine component was not found in static/index.html — the markup changed shape"
    return found.group(0), main.INDEX_HTML[:found.start()].count("\n")


def test_no_component_method_is_called_without_this_in_javascript():
    """`formatDate(x)` inside a method body is a ReferenceError, not a date.

    This bit for real: the draw list's status line called `formatDate` bare through a helper, and
    Alpine swallowed the error, so the chip rendered empty on a locked draw.
    """
    js, offset = app_script()
    bare = []
    for number, line in enumerate(js.splitlines(), start=offset + 1):
        for name in COMPONENT_METHODS:
            # `(?<![\w$.])` skips longer identifiers and every `this.name(` call.
            if not re.search(rf"(?<![\w$.])\b{name}\s*\(", line):
                continue
            if re.match(rf"^\s*(async\s+)?{name}\s*\(", line):
                continue  # the method's own definition
            bare.append(f"line {number}: {line.strip()[:90]}")
    assert not bare, "call component methods through `this.`:\n" + "\n".join(bare)


def test_every_draw_scoped_admin_call_carries_the_draw():
    """A new admin endpoint that forgets `drawPath()` silently acts on the wrong event.

    With one draw the server still resolves it; with two it answers 400 "Choisis un tirage", which
    only shows up once somebody opens the office exchange. So the rule is checked in code.
    """
    js, _ = app_script()
    calls = [match.group(1).strip("`'") for match in API_CALL_RE.finditer(js)]
    assert calls, "no API calls found — the SPA changed shape"
    unscoped = []
    for path in calls:
        if not path.startswith("/api/admin/"):
            continue
        if any(path.startswith(endpoint) for endpoint in NOT_DRAW_SCOPED):
            continue
        unscoped.append(path)
    assert not unscoped, f"wrap these in this.drawPath(...): {sorted(set(unscoped))}"
    scoped = js.count("this.drawPath(")
    assert scoped >= 10, f"only {scoped} scoped calls — the draw plumbing looks half-finished"


def test_the_one_mail_per_mailbox_rule_is_visible_before_the_click():
    """The console has to say "un courriel par boîte" in three places, or the rule looks broken.

    A twelve-person roster with five addresses produces five sends. If the only place that says so is
    the README, an organizer who clicks "Envoyer" on each relative thinks the app forgot people. So
    the row hint, the bulk counter and the report all have to be wired, and a multi-link preview has
    to exist to show what a shared mail looks like.
    """
    markup = main.INDEX_HTML
    assert "sendHelpFor(p)" in markup, "the row's send button explains the static help again"
    assert 'x-text="t(\'roster.mailboxes\'' in markup, "the people/mailboxes counter is gone"
    assert "previewEmail(3)" in markup, "no way to preview the household template"
    assert "response.body.messages" in markup, "the bulk report stopped saying how many mails left"


def test_a_refused_action_has_a_place_to_say_so():
    """The draw list has no panel of its own, so the error strip has to sit above both screens.

    It used to live inside the console, which meant a refused action on the list (deleting a locked
    draw) came back as a 400 the organizer could only see in the browser console.
    """
    markup = main.INDEX_HTML
    panel = markup.index('x-show="adminError"')
    list_screen = markup.index('x-show="activeDraw === null"')
    console_screen = markup.index('x-show="activeDraw !== null"')
    assert markup.count('x-show="adminError"') == 1, "the error strip exists twice"
    assert panel < list_screen < console_screen, "the error strip is trapped inside one screen"
