"""Email delivery: roster field, Resend transport, templates, endpoints.

The transport is stubbed in most of these — the point is the app's contract (who gets mailed,
what the mail contains, what gets stamped), not Resend's API. The tests near the bottom inspect
the actual outbound request instead, without leaving the process.
"""

import io
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from conftest import ADMIN, add, set_config
from test_i18n import BANNED_FR

import main

# A "current release minus email" database — exactly what a live deployment carries.
OLD_SCHEMA = """
CREATE TABLE config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    avoid_same_group INTEGER NOT NULL DEFAULT 1,
    is_locked INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO config (id) VALUES (1);
CREATE TABLE participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL,
    group_key TEXT NOT NULL DEFAULT '',
    wishlist TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    viewed_at TEXT DEFAULT NULL
);
CREATE TABLE assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    FOREIGN KEY (giver_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES participants(id) ON DELETE CASCADE,
    UNIQUE(giver_id, receiver_id)
);
"""

FESTIVE = dict(adult_draws_adult=1, adult_draws_child=1, child_draws_adult=0, child_draws_child=0)


class StubTransport:
    """Swap for main.send_email: records every send (with a timestamp), always accepts."""

    def __init__(self):
        self.sent = []

    def __call__(self, *, to, subject, html_body, text_body, ref_id):
        self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body,
                          "ref_id": ref_id, "at": time.monotonic()})
        return "stub-message-id"


def configure_emails(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_not_real")
    monkeypatch.setenv("RESEND_FROM", "Tirage de Noël <tirage-noel@alserv.org>")


def roster(client):
    add(client, "Ghislaine", "ADULT", "Bernier", email="ghislaine@exemple.ca")
    add(client, "Réjean", "ADULT", "Bernier", email="rejean@exemple.ca")
    add(client, "Florent", "CHILD", "Bernier", email="")


def by_name(state_payload, name):
    return next(p for p in state_payload["participants"] if p["name"] == name)


# --------------------------------------------------------------------------- #
# Schema and migration
# --------------------------------------------------------------------------- #


def test_old_database_gains_the_email_columns_with_safe_defaults(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO participants (name, role, family_group, group_key, token) "
                 "VALUES ('Aline','ADULT','Roy','Roy','t1')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(path))
    main.init_db()
    main.init_db()  # idempotent

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    person = conn.execute("SELECT email, email_sent_at FROM participants WHERE id = 1").fetchone()
    config = conn.execute(
        "SELECT email_language, auto_email_on_draw FROM draws WHERE id = 1").fetchone()
    conn.close()
    assert person["email"] == "" and person["email_sent_at"] is None
    assert config["email_language"] == "fr" and config["auto_email_on_draw"] == 0


def test_limits_advertise_the_email_field(client):
    assert client.get("/api/limits").json()["email"] == main.MAX_EMAIL


# --------------------------------------------------------------------------- #
# Roster field: free-text in, editable, locked-draw rules
# --------------------------------------------------------------------------- #


def test_email_is_free_text_and_normalized(client):
    created = add(client, "Lise", "ADULT", "Roy", email="  Lise.Roy@Exemple.CA  ")
    assert created["email"] == "lise.roy@exemple.ca"
    weird = add(client, "Oncle Gaétan", "ADULT", "Roy", email="pas un email")
    assert weird["email"] == "pas un email"  # stored, never refused — flagging is the UI's job


def test_admin_can_edit_a_participant(client):
    created = add(client, "Réjean", "ADULT", "Roy", email="vieux@exemple.ca")
    response = client.put(f"/api/admin/participants/{created['id']}", headers=ADMIN,
                          json={"name": "Réjean", "email": "neuf@exemple.ca"})
    assert response.status_code == 200, response.text
    updated = response.json()["participant"]
    assert updated["email"] == "neuf@exemple.ca"
    assert updated["token"] == created["token"], "editing must not burn the participant's link"


def test_editing_the_email_clears_the_sent_stamp(client, monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setattr(main, "send_email", StubTransport())
    created = add(client, "Ghislaine", "ADULT", "Bernier", email="vieux@exemple.ca")
    client.post(f"/api/admin/participants/{created['id']}/email", headers=ADMIN)
    assert by_name(client.get("/api/admin/state", headers=ADMIN).json(),
                   "Ghislaine")["email_sent_at"] is not None

    client.put(f"/api/admin/participants/{created['id']}", headers=ADMIN,
               json={"email": "neuf@exemple.ca"})
    row = by_name(client.get("/api/admin/state", headers=ADMIN).json(), "Ghislaine")
    assert row["email"] == "neuf@exemple.ca"
    assert row["email_sent_at"] is None, "the old send went somewhere else"


def test_while_locked_only_the_email_is_editable(client):
    created = add(client, "Lise", "ADULT", "Roy", email="lise@exemple.ca")
    add(client, "Paul", "ADULT", "Tremblay")
    add(client, "Florent", "CHILD", "Roy")
    set_config(client, **FESTIVE)
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200

    fixed = client.put(f"/api/admin/participants/{created['id']}", headers=ADMIN,
                       json={"email": "lise.corrige@exemple.ca"})
    assert fixed.status_code == 200, fixed.text

    rename = client.put(f"/api/admin/participants/{created['id']}", headers=ADMIN,
                        json={"name": "Lise Marie"})
    assert rename.status_code == 400
    assert "locked" in rename.json()["detail"]


def test_edit_unknown_or_empty_is_refused(client):
    assert client.put("/api/admin/participants/999", headers=ADMIN,
                      json={"email": "x@y.ca"}).status_code == 404
    created = add(client, "Paul", "ADULT", "Roy")
    assert client.put(f"/api/admin/participants/{created['id']}", headers=ADMIN,
                      json={}).status_code == 422


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def test_without_a_key_the_feature_is_off_everywhere(client, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    roster(client)
    state = client.get("/api/admin/state", headers=ADMIN).json()
    assert state["email_enabled"] is False
    assert client.post("/api/admin/emails", headers=ADMIN).status_code == 503
    person = state["participants"][0]
    assert client.post(f"/api/admin/participants/{person['id']}/email",
                       headers=ADMIN).status_code == 503
    # the preview still works: seeing the template never needs credentials
    assert client.post("/api/admin/emails/preview", headers=ADMIN, json={}).status_code == 200


def test_send_one_stamps_and_carries_only_that_participants_link(client, monkeypatch):
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    roster(client)
    ghislaine = by_name(client.get("/api/admin/state", headers=ADMIN).json(), "Ghislaine")

    response = client.post(f"/api/admin/participants/{ghislaine['id']}/email", headers=ADMIN)
    assert response.status_code == 200, response.text

    assert len(stub.sent) == 1
    mail = stub.sent[0]
    assert mail["to"] == "ghislaine@exemple.ca"
    assert f"/?t={ghislaine['token']}" in mail["html"]
    # A link in an email is only worth what it resolves to: follow it and demand the interface,
    # not a JSON 404. This is the check that the first real send failed — the mail carried the
    # API's path shape (`/draw/<uuid>`), a route the server never served.
    href = re.search(r'href="(https?://[^"]*/\?t=[0-9a-f-]{36})"', mail["html"]).group(1)
    where = urllib.parse.urlparse(href)
    landing = client.get(f"{where.path}?{where.query}")
    assert landing.status_code == 200, href
    assert "landing" in landing.text or "giftExchange" in landing.text
    assert "text/html" in landing.headers["content-type"]
    assert ghislaine["token"] in landing.text or "?t=" in landing.text
    assert ghislaine["name"] in mail["subject"], "the mailbox sorts by subject; the name must be in it"
    # the mail names its own recipient and nobody else — no roster leak, no pairing
    assert "Réjean" not in mail["html"] and "Florent" not in mail["html"]
    assert mail["ref_id"] == f"gift-exchange-{ghislaine['id']}"

    row = by_name(client.get("/api/admin/state", headers=ADMIN).json(), "Ghislaine")
    assert row["email_sent_at"] is not None


def test_free_text_nonsense_is_skipped_at_send_time_not_at_save_time(client, monkeypatch):
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    created = add(client, "Oncle Gaétan", "ADULT", "Roy", email="gaetan (téléphone)")
    response = client.post(f"/api/admin/participants/{created['id']}/email", headers=ADMIN)
    assert response.status_code == 422
    assert response.json()["detail"] == main.EMAIL_NO_ADDRESS
    assert stub.sent == [], "a doomed address must not spend an API call"


def test_bulk_send_reports_row_by_row(client, monkeypatch):
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    add(client, "Ghislaine", "ADULT", "Bernier", email="g@exemple.ca")
    add(client, "Réjean", "ADULT", "Bernier", email="r@exemple.ca")
    add(client, "Sans-Adresse", "ADULT", "Roy")

    response = client.post("/api/admin/emails", headers=ADMIN)
    assert response.status_code == 200
    report = response.json()
    assert (report["sent"], report["failed"]) == (2, 1)
    results = {r["name"]: r for r in report["results"]}
    assert results["Sans-Adresse"]["reason"] == main.EMAIL_NO_ADDRESS
    assert {m["to"] for m in stub.sent} == {"g@exemple.ca", "r@exemple.ca"}


# --------------------------------------------------------------------------- #
# Throttling: providers have request ceilings, inboxes have patience
# --------------------------------------------------------------------------- #


def test_bulk_never_sends_to_the_same_mailbox_twice_in_a_row(client, monkeypatch):
    """The kids' links share the parent's address: interleaving spreads them out for free."""
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    add(client, "Enfant Un", "CHILD", "Bernier", email="parent@exemple.ca")
    add(client, "Enfant Deux", "CHILD", "Bernier", email="parent@exemple.ca")
    add(client, "Enfant Trois", "CHILD", "Bernier", email="parent@exemple.ca")
    add(client, "Oncle", "ADULT", "Roy", email="oncle@exemple.ca")

    client.post("/api/admin/emails", headers=ADMIN)
    order = [m["to"] for m in stub.sent]
    # One address holds three links and the other one link: after the interleaving runs out of
    # lanes, repeats are unavoidable — the per-address time gap (tested next) is what makes the
    # repeat arrive politely. What must hold here is that the other lane is inserted as early
    # as possible.
    assert order == ["parent@exemple.ca", "oncle@exemple.ca",
                     "parent@exemple.ca", "parent@exemple.ca"]


def test_same_address_sends_keep_their_distance(client, monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setenv("EMAIL_SAME_ADDRESS_GAP_S", "1")
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    for name in ("Enfant Un", "Enfant Deux", "Enfant Trois"):
        add(client, name, "CHILD", "Bernier", email="parent@exemple.ca")

    client.post("/api/admin/emails", headers=ADMIN)
    gaps = [b["at"] - a["at"] for a, b in zip(stub.sent, stub.sent[1:])]
    assert len(gaps) == 2
    assert all(g >= 0.95 for g in gaps), gaps


def test_global_spacing_holds_across_different_addresses(client, monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setenv("EMAIL_SEND_INTERVAL_MS", "60")
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    for name, mail in (("A", "a@x.ca"), ("B", "b@x.ca"), ("C", "c@x.ca"), ("D", "d@x.ca")):
        add(client, name, "ADULT", "Roy", email=mail)

    client.post("/api/admin/emails", headers=ADMIN)
    gaps = [b["at"] - a["at"] for a, b in zip(stub.sent, stub.sent[1:])]
    assert len(gaps) == 3
    assert all(g >= 0.05 for g in gaps), gaps


def test_provider_429_gets_exactly_one_polite_retry(monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setenv("EMAIL_RETRY_AFTER_S", "0")
    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://api.resend.com/emails", 429,
                                         "Too Many Requests", {},
                                         io.BytesIO(b'{"message":"rate limited"}'))
        return fake_response({"id": "second-time-lucky"})

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert main.send_email(to="g@exemple.ca", subject="s", html_body="h", text_body="t",
                           ref_id="r") == "second-time-lucky"
    assert len(calls) == 2


def test_a_second_429_is_reported_not_retried_forever(monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setenv("EMAIL_RETRY_AFTER_S", "0")
    calls = []

    def always_429(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError("https://api.resend.com/emails", 429, "Too Many Requests",
                                     {}, io.BytesIO(b'{"message":"rate limited"}'))

    monkeypatch.setattr(urllib.request, "urlopen", always_429)
    with pytest.raises(main.EmailSendError) as caught:
        main.send_email(to="g@exemple.ca", subject="s", html_body="h", text_body="t", ref_id="r")
    assert caught.value.status == 429
    assert len(calls) == 2


def test_throttle_state_is_process_wide(client, monkeypatch):
    """A manual single send fired right after a bulk run still respects the gate."""
    configure_emails(monkeypatch)
    monkeypatch.setenv("EMAIL_SEND_INTERVAL_MS", "80")
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    a = add(client, "A", "ADULT", "Roy", email="a@x.ca")
    b = add(client, "B", "ADULT", "Roy", email="b@x.ca")

    client.post("/api/admin/emails", headers=ADMIN)          # bulk: A and B
    client.post(f"/api/admin/participants/{b['id']}/email", headers=ADMIN)  # manual follow-up
    gaps = [y["at"] - x["at"] for x, y in zip(stub.sent, stub.sent[1:])]
    assert len(gaps) == 2 and all(g >= 0.07 for g in gaps), gaps


def test_provider_refusal_is_reported_and_leaves_no_stamp(client, monkeypatch):
    configure_emails(monkeypatch)

    def refusal(*, to, subject, html_body, text_body, ref_id):
        raise main.EmailSendError(422, "rejected the address")

    monkeypatch.setattr(main, "send_email", refusal)
    created = add(client, "Ghislaine", "ADULT", "Bernier", email="g@exemple.ca")
    response = client.post(f"/api/admin/participants/{created['id']}/email", headers=ADMIN)
    assert response.status_code == 422
    assert response.json()["detail"] == main.EMAIL_SEND_FAILED
    row = by_name(client.get("/api/admin/state", headers=ADMIN).json(), "Ghislaine")
    assert row["email_sent_at"] is None


def test_reset_clears_the_stamps_but_keeps_the_addresses(client, monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setattr(main, "send_email", StubTransport())
    created = add(client, "Ghislaine", "ADULT", "Bernier", email="g@exemple.ca")
    client.post(f"/api/admin/participants/{created['id']}/email", headers=ADMIN)
    assert by_name(client.get("/api/admin/state", headers=ADMIN).json(),
                   "Ghislaine")["email_sent_at"] is not None

    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 200
    row = by_name(client.get("/api/admin/state", headers=ADMIN).json(), "Ghislaine")
    assert row["email_sent_at"] is None
    assert row["email"] == "g@exemple.ca"


def test_auto_email_on_draw_is_off_by_default_and_sends_when_on(client, monkeypatch):
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    roster(client)

    set_config(client, **FESTIVE)
    quiet = client.post("/api/admin/draw", headers=ADMIN)
    assert quiet.status_code == 200 and "emails" not in quiet.json()
    assert stub.sent == []
    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 200

    set_config(client, **FESTIVE, auto_email_on_draw=True)
    drawn = client.post("/api/admin/draw", headers=ADMIN)
    assert drawn.status_code == 200
    assert drawn.json()["emails"]["sent"] == 2  # Florent has no address and is simply not mailed
    assert all(m["to"] in {"ghislaine@exemple.ca", "rejean@exemple.ca"} for m in stub.sent)


def test_email_language_is_one_event_wide_choice(client, monkeypatch):
    configure_emails(monkeypatch)
    stub = StubTransport()
    monkeypatch.setattr(main, "send_email", stub)
    created = add(client, "Alec", "ADULT", "Chamberland", email="alec@exemple.ca")

    assert client.put("/api/admin/config", headers=ADMIN,
                      json={"email_language": "de"}).status_code == 422
    set_config(client, email_language="en")
    client.post(f"/api/admin/participants/{created['id']}/email", headers=ADMIN)
    assert "Open my link" in stub.sent[0]["html"]
    assert "Ouvrir mon lien" not in stub.sent[0]["html"]


# --------------------------------------------------------------------------- #
# Templates and preview
# --------------------------------------------------------------------------- #


def test_preview_never_contains_the_real_roster(client):
    add(client, "Ghislaine", "ADULT", "Bernier", email="g@exemple.ca")
    add(client, "Réjean", "ADULT", "Bernier", email="r@exemple.ca")
    response = client.post("/api/admin/emails/preview", headers=ADMIN, json={"state": "ready"})
    assert response.status_code == 200
    payload = response.json()
    text = json.dumps(payload)
    for forbidden in ("Ghislaine", "Réjean", "g@exemple.ca", "r@exemple.ca"):
        assert forbidden not in text
    tokens = {p["token"] for p in client.get("/api/admin/state", headers=ADMIN).json()["participants"]}
    assert not any(token in text for token in tokens), "a preview link must be a sample"
    assert "00000000-0000-0000-0000-000000000000" in payload["html"]


def test_preview_follows_the_event_language_and_the_state(client):
    set_config(client, email_language="en")
    pending = client.post("/api/admin/emails/preview", headers=ADMIN,
                          json={"state": "pending"}).json()
    assert pending["language"] == "en" and "draw has not happened" in pending["text"]
    fr = client.post("/api/admin/emails/preview", headers=ADMIN,
                     json={"language": "fr", "state": "ready"}).json()
    assert "tirage est fait" in fr["text"]


def test_names_are_escaped_before_they_touch_the_html():
    content = main.build_email('Al<script>x</script>', "Noël <2026>", "https://x/draw/t", "fr", True)
    assert "<script>" not in content["html"]
    assert "&lt;script&gt;" in content["html"]
    assert "Noël &lt;2026&gt;" in content["html"]


def test_template_carries_the_site_art_over_absolute_urls():
    content = main.build_email("Aline", "Noël", "https://x.test/draw/t", "fr", True,
                               assets_base="https://tirage-noel.alserv.org")
    assert 'src="https://tirage-noel.alserv.org/static/email-flake.svg"' in content["html"]
    assert 'src="https://tirage-noel.alserv.org/static/email-gift-strip.svg"' in content["html"]
    assert 'src="https://tirage-noel.alserv.org/static/email-gift.svg"' in content["html"]
    # inline <svg> is stripped by Gmail; the art is always <img>-referenced, never inline
    assert "<svg" not in content["html"]
    bare = main.build_email("Aline", "Noël", "https://x.test/draw/t", "fr", True)
    assert "<img" not in bare["html"], "no absolute base means no art, not a broken relative src"


def test_the_email_has_no_animation_to_lose():
    """The gift is drawn mid-joke (lid off, somebody pushing), which is a still-frame joke.

    Keyframes would be the cute thing to add and the wrong one: Outlook ignores them, Gmail strips
    them, and half the family reading a mail whose art silently froze is worse than a mail that was
    never animated.
    """
    content = main.build_email("Aline", "Noël", "https://x.test/draw/t", "fr", True,
                               assets_base="https://tirage-noel.alserv.org")
    for forbidden in ("<style", "@keyframes", "animation"):
        assert forbidden not in content["html"], forbidden
    # `text-transform: uppercase` is a real style in this mail; `transform:` on its own is the
    # animation's lever, and that is the one that must not appear.
    assert not re.search(r"(?<!text-)\btransform\s*:", content["html"]), "a transform was added"
    art_path = main.STATIC_DIR / "email-gift.svg"
    art = art_path.read_text(encoding="utf-8")
    # Checked on the parsed tree, not the raw text: the art's own comment says the word "animation"
    # out loud to explain why there is none, and a text match would flag the warning as the bug.
    root = ET.fromstring(art)
    tags = {element.tag.rpartition("}")[2] for element in root.iter()}
    attributes = {name for element in root.iter() for name in element.attrib}
    assert not (tags & {"style", "animate", "animateTransform", "animateMotion", "script"}), tags
    assert not any(name.startswith("animation") or name == "transform-origin" for name in attributes)


def test_the_hero_gift_is_described_in_the_language_of_the_mail():
    """A screen reader gets the joke or gets nothing; either way it must not get a raw alt='<'."""
    french = main.build_email("Aline", "Noël", "https://x.test/draw/t", "fr", True,
                              assets_base="https://x.test")["html"]
    english = main.build_email("Aline", "Noël", "https://x.test/draw/t", "en", True,
                               assets_base="https://x.test")["html"]
    assert 'alt="Un cadeau bien fermé' in french
    assert 'alt="A tightly closed gift' in english
    assert 'alt=""' not in french.split("email-gift.svg")[1][:120], "the hero has no description"


def test_email_art_assets_exist_and_are_valid_svg():
    for name in ("email-flake.svg", "email-gift-strip.svg", "email-gift.svg"):
        path = main.STATIC_DIR / name
        assert path.exists(), name
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        assert root.tag == "{http://www.w3.org/2000/svg}svg", name


def test_pending_mail_never_promises_a_result():
    pending = main.build_email("Aline", "Noël", "https://x/draw/t", "fr", ready=False)
    assert "tirage n’a pas encore été fait" in pending["text"]
    assert "liste d’idées" in pending["text"]  # the link is useful before the draw too


def test_french_email_copy_follows_the_same_rule_as_the_interface():
    """The i18n suite polices the SPA dictionary; the email copy is copy too."""
    for key, value in main.EMAIL_STRINGS["fr"].items():
        for pattern, why in BANNED_FR:
            assert not re.search(pattern, value, re.IGNORECASE), f"{key}: {value!r} -> {why}"


def test_both_email_languages_offer_the_same_subject_placeholders():
    """A subject that lost its {name} in one language mails the braces to a grandmother."""
    french, english = main.EMAIL_STRINGS["fr"], main.EMAIL_STRINGS["en"]
    assert set(french) == set(english)
    for key in set(french) & set(english):
        assert set(re.findall(r"\{(\w+)\}", french[key])) == set(
            re.findall(r"\{(\w+)\}", english[key])), key


def test_the_subject_names_the_person_because_a_family_shares_one_inbox():
    """Six mails from one sender with one subject are indistinguishable in a phone's list view."""
    ready = main.build_email("Ghislaine", "Réveillon chez Gaétan", "https://x.test/?t=t", "fr", True)
    assert ready["subject"].startswith("Ghislaine")
    assert "ton lien" in ready["subject"]
    assert "Réveillon chez Gaétan" in ready["subject"]
    pending = main.build_email("Enfant", "Réveillon", "https://x.test/?t=t", "en", False)
    assert pending["subject"].startswith("Enfant") and "your link" in pending["subject"]


# --------------------------------------------------------------------------- #
# The link in the mail has to be a route the server serves
# --------------------------------------------------------------------------- #


def test_the_link_shape_the_mail_carries_is_the_shape_the_spa_reads():
    """The console's copy button and the email must agree — one builder, not two guesses."""
    assert main.participant_link("https://tirage-noel.alserv.org", "abc") == \
        "https://tirage-noel.alserv.org/?t=abc"
    # the SPA reads its token from the query string, so the two cannot drift apart silently
    assert "params.get('t')" in main.INDEX_HTML
    assert "${window.location.origin}/?t=${person.token}" in main.INDEX_HTML


def test_the_api_path_shape_still_works_for_links_already_in_inboxes(client):
    """The first real send mailed `/draw/<uuid>`, which answered 404. Emails cannot be unsent."""
    created = add(client, "Ghislaine", "ADULT", "Bernier")
    token = created["token"]

    bounce = client.get(f"/draw/{token}", follow_redirects=False)
    assert bounce.status_code == 302
    assert bounce.headers["location"] == f"/?t={token}"

    landing = client.get(f"/draw/{token}")          # follow it the way a mail client does
    assert landing.status_code == 200
    assert "text/html" in landing.headers["content-type"]
    assert "giftExchange" in landing.text


def test_a_non_token_under_the_old_path_stays_a_404(client):
    """The bounce is for UUIDs, not a catch-all that hints other shapes are meaningful."""
    for path in ("/draw/pas-un-jeton", "/draw/1234", "/draw/"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------------- #
# The transport itself (no network: urlopen is the stub)
# --------------------------------------------------------------------------- #


def fake_response(payload):
    class Response:
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response()


def test_send_email_posts_to_resend_with_the_headers_that_matter(monkeypatch):
    configure_emails(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return fake_response({"id": "real-id"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    message_id = main.send_email(to="g@exemple.ca", subject="s", html_body="<b>x</b>",
                                 text_body="x", ref_id="gift-exchange-1")
    assert message_id == "real-id"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["authorization"] == "Bearer re_test_key_not_real"
    # Cloudflare answers 403/1010 to the default Python-urllib agent — this header is load-bearing.
    assert captured["headers"]["user-agent"] == main.EMAIL_USER_AGENT
    assert "Python-urllib" not in str(captured["headers"])
    assert captured["body"]["from"] == "Tirage de Noël <tirage-noel@alserv.org>"
    assert captured["body"]["headers"]["X-Entity-Ref-ID"] == "gift-exchange-1"
    assert captured["timeout"] == main.EMAIL_TIMEOUT


def test_send_email_never_leaks_the_key_into_logs_or_errors(monkeypatch, caplog):
    configure_emails(monkeypatch)

    def boom(request, timeout):
        raise urllib.error.URLError("no route to api.resend.com")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("INFO"):
        with pytest.raises(main.EmailSendError) as caught:
            main.send_email(to="g@exemple.ca", subject="s", html_body="h", text_body="t",
                            ref_id="gift-exchange-1")
    assert "no route" in caught.value.message
    assert "re_test_key_not_real" not in caplog.text
    assert "re_test_key_not_real" not in str(caught.value)


def test_http_error_becomes_the_providers_own_message(monkeypatch):
    configure_emails(monkeypatch)

    def http_422(request, timeout):
        body = io.BytesIO(json.dumps(
            {"statusCode": 422, "message": "Invalid `to` field"}).encode())
        raise urllib.error.HTTPError("https://api.resend.com/emails", 422,
                                     "Unprocessable Entity", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", http_422)
    with pytest.raises(main.EmailSendError) as caught:
        main.send_email(to="x", subject="s", html_body="h", text_body="t", ref_id="r")
    assert caught.value.status == 422
    assert "Invalid `to` field" in caught.value.message


def test_base_url_hook_redirects_the_send(monkeypatch):
    configure_emails(monkeypatch)
    monkeypatch.setenv("RESEND_BASE_URL", "http://127.0.0.1:9/stub")
    seen = {}

    def refused(request, timeout):
        seen["url"] = request.full_url
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(main.EmailSendError):
        main.send_email(to="g@exemple.ca", subject="s", html_body="h", text_body="t", ref_id="r")
    assert seen["url"] == "http://127.0.0.1:9/stub/emails"
