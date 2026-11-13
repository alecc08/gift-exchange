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
    SAME_THING_TWICE = {frozenset({"admin.title", "login.title"})}
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
