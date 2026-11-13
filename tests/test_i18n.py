"""The two UI dictionaries: same keys, same placeholders, and plain Quebec French.

The copy rule is deliberately enforced rather than trusted. The app speaks ordinary Quebec
French — "qui pige qui", "jamais dans la même famille", "réessaie" — and not the performed joual
version of it. That is a style choice anyone can undo by adding one string, so the banned forms
are listed here with the reason, and the FR/EN switch is checked key by key because a key missing
from one language renders a raw key name in the UI.
"""

import re

import main

# Word-boundary on `pis` so it does not fire on "pige", "pisser" or "capopsis".
BANNED_FR = [
    (r"\bpis\b", "use « et » — « pis » reads as caricature on a button"),
    (r"\btoé\b|\bmoé\b|\bste\b", "spelled-out spoken contractions; « toi » / « moi » are fine"),
    (r"\bfrett", "« verrouillé » / « lancé » says what actually happened"),
    (r"\bbinôm", "say « qui pige qui »"),
    (r"astheur", "« maintenant »"),
    (r"\bcapot", "« brisé » / « n’a pas fonctionné »"),
    (r"\bsouhait", "the UI word is « liste d’idées »"),
    (r"\bpu\b(?!isque)", "« plus » — « marche pu » is dialogue, not interface copy"),
    (r"c’année", "« cette année »"),
    (r"\bben\b", "« bien » — no phonetic spelling in the interface"),
]

ENTRY_RE = re.compile(r"^\s*'([a-z0-9.]+)':\s*'((?:[^'\\]|\\.)*)',\s*$", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# `detail.includes('...')` is how the SPA decides which explanation to show, and `t('key')` is how
# the markup asks for a label. Both are stringly-typed, so both drift silently.
INCLUDE_RE = re.compile(r"detail\.includes\('([^']+)'\)")
LABEL_RE = re.compile(r"\bt\('([a-z0-9.]+)'")


def dictionary(language: str) -> dict[str, str]:
    """The fr/en block of the I18N object in static/index.html."""
    start = main.INDEX_HTML.index(f"  {language}: {{")
    brace = main.INDEX_HTML.index("{", start)
    depth, end = 0, brace
    for i in range(brace, len(main.INDEX_HTML)):
        if main.INDEX_HTML[i] == "{":
            depth += 1
        elif main.INDEX_HTML[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    entries = {}
    for line in main.INDEX_HTML[brace:end].splitlines():
        found = ENTRY_RE.match(line)
        if found:
            entries[found.group(1)] = found.group(2)
    return entries


def test_both_languages_have_the_same_keys():
    french, english = dictionary("fr"), dictionary("en")
    assert french and english
    missing_in_en = set(french) - set(english)
    missing_in_fr = set(english) - set(french)
    assert not missing_in_en and not missing_in_fr, (
        f"fr only: {sorted(missing_in_en)} · en only: {sorted(missing_in_fr)}"
    )


def test_no_value_is_empty():
    for language in ("fr", "en"):
        for key, value in dictionary(language).items():
            assert value.strip(), f"{language}.{key} is empty"


def test_placeholders_match_between_languages():
    """A French string that lost its {name} renders the literal braces instead of a name."""
    french, english = dictionary("fr"), dictionary("en")
    for key in sorted(set(french) & set(english)):
        assert set(PLACEHOLDER_RE.findall(french[key])) == set(PLACEHOLDER_RE.findall(english[key])), key


def test_french_is_plain_quebec_french_not_caricature():
    french = dictionary("fr")
    broken = []
    for key, value in sorted(french.items()):
        for pattern, why in BANNED_FR:
            if re.search(pattern, value, re.IGNORECASE):
                broken.append(f"{key}: {value!r} -> {why}")
    assert not broken, "\n".join(broken)


def test_no_two_different_controls_share_a_label():
    """Renaming is how two unrelated buttons end up reading the same thing — which is not only a
    click-anyone hazard, it breaks anything that selects a control by its text (the docs capture
    tool clicked "Lancer le tirage" and found the logout button first). Keys that name the same
    thing in two places are allowlisted."""
    SAME_THING_TWICE = {frozenset({"admin.title", "login.title"}),
                        # The preview modal is named after the button that opens it: two controls,
                        # one thing — exactly what the allowlist is for.
                        frozenset({"rules.preview", "mail.preview_title"})}
    for language in ("fr", "en"):
        by_value: dict[str, list[str]] = {}
        for key, value in dictionary(language).items():
            by_value.setdefault(value.strip().lower(), []).append(key)
        clash = {value: sorted(keys) for value, keys in by_value.items()
                 if len(keys) > 1 and frozenset(keys) not in SAME_THING_TWICE}
        assert not clash, f"{language}: {clash}"


def test_the_share_copy_and_the_ui_agree():
    """The card and the interface describe the same promise in the same words."""
    french = dictionary("fr")
    assert "qui pige qui" in main.SHARE_DESCRIPTION
    assert "qui pige qui" in french["draw.blindNote"]
    assert "liste d’idées" in french["p.myWishlist"].lower()


def test_every_label_the_markup_asks_for_exists():
    """A typo in `t('rostre.group')` renders the raw key name on screen. Prefixes built at runtime
    (`t('role.' + p.role)`) are checked the other way round: something must start with them."""
    french, english = dictionary("fr"), dictionary("en")
    used = set(LABEL_RE.findall(main.INDEX_HTML))
    assert used, "no labels found — the markup changed shape"
    broken = []
    for key in sorted(used):
        if key.endswith("."):
            children = [child for child in french if child.startswith(key)]
            if not children or any(not english.get(child) for child in children):
                broken.append(key + "*")
        elif key not in french or key not in english:
            broken.append(key)
    assert not broken, broken


def test_the_frontend_still_understands_the_servers_own_error_text():
    """`errorText()` recognises the API's sentences by substring. Reword one in main.py without
    touching the SPA and the organizer reads a bare English string instead of the explanation."""
    fragments = INCLUDE_RE.findall(main.INDEX_HTML)
    assert fragments
    server = (main.UNSOLVABLE, main.EMPTY_TARGET_GROUP, main.LOCKED_MSG, main.ALREADY_LOCKED, main.NO_PARTICIPANTS,
              main.EMAIL_NOT_CONFIGURED, main.EMAIL_NO_ADDRESS, main.EMAIL_SEND_FAILED,
              main.DRAW_NOT_FOUND, main.DRAW_ID_REQUIRED, main.DRAW_NONE)
    dead = [fragment for fragment in fragments if not any(fragment in text for text in server)]
    assert not dead, f"matchers that can never fire again: {dead}"
    untranslated = [text for text in server if not any(fragment in text for fragment in fragments)]
    assert not untranslated, f"errors the UI would show as-is: {untranslated}"
