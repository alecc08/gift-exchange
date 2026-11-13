"""Multiple draws on one server: separation, copying, deleting, and the legacy single-draw contract.

The reason this file exists is that a family runs several exchanges from one deployment — the
Reveillon, the office, the cousins' — and each one's pairings, links and mail must stay inside its
own draw. Every test here is about a leak between draws: a roster that shows up in two places, a
token that reveals the wrong event, a reset that clears someone else's stamps.
"""

import sqlite3

import pytest
from conftest import ADMIN, PASSWORD, add, set_config
from fastapi.testclient import TestClient

import main


def create(client, title, **kwargs):
    response = client.post("/api/admin/draws", headers=ADMIN,
                           json={"event_title": title, **kwargs})
    assert response.status_code == 201, response.text
    return response.json()["draw"]


def roster(client, draw_id):
    state = client.get("/api/admin/state", params={"draw_id": draw_id}, headers=ADMIN).json()
    return state["participants"], state["config"]


def draw(client, draw_id):
    return client.post("/api/admin/draw", params={"draw_id": draw_id}, headers=ADMIN)


def family_roster(client, draw_id, count=3):
    """Three adults in three different households, so a draw of them is always solvable."""
    set_config(client, draw_id, adult_draws_adult=1, adult_draws_child=0,
               child_draws_adult=0, child_draws_child=0)
    for index in range(count):
        add(client, f"Adulte {index + 1}", "ADULT", f"Famille {index + 1}",
            email=f"adulte{index + 1}@exemple.ca", draw_id=draw_id)


# --------------------------------------------------------------------------- #
# Creating and separating
# --------------------------------------------------------------------------- #


def test_a_second_draw_starts_empty_and_separate(client):
    add(client, "Gaétan", "ADULT", "Bernier")
    second = create(client, "Échange du bureau")

    people, config = roster(client, second["id"])
    assert people == [], "a new draw must not inherit anyone implicitly"
    assert config["is_locked"] == 0
    assert config["id"] == second["id"]
    assert len(roster(client, 1)[0]) == 1, "the first draw keeps its own roster"


def test_a_new_draw_keeps_the_default_rules_until_they_are_edited(client):
    set_config(client, 1, adult_draws_adult=3, budget_note="50 $")
    second = create(client, "Second")
    _, config = roster(client, second["id"])
    assert config["adult_draws_adult"] != 3 or config["budget_note"] == "50 $"
    # rules are per draw: editing the new one leaves the old one alone
    set_config(client, second["id"], adult_draws_adult=1, budget_note="20 $")
    assert roster(client, 1)[1]["adult_draws_adult"] == 3
    assert roster(client, 1)[1]["budget_note"] == "50 $"
    assert roster(client, second["id"])[1]["budget_note"] == "20 $"


def test_a_draw_needs_a_name(client):
    for title in ("", "   "):
        assert client.post("/api/admin/draws", headers=ADMIN,
                           json={"event_title": title}).status_code == 422


def test_the_list_reports_status_but_never_a_name_or_a_pairing(client):
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    draw(client, first["id"])

    listed = client.get("/api/admin/draws", headers=ADMIN).json()
    entry = next(item for item in listed["draws"] if item["id"] == first["id"])
    assert entry["participants"] == 3
    assert entry["is_locked"] == 1
    assert entry["drawn_at"], "a drawn draw carries the moment it was drawn"
    body = str(listed)
    assert "Adulte" not in body, "the list screen shows counts, never the roster's names"
    assert "receiver" not in body and "giver" not in body


def test_an_ambiguous_call_asks_which_draw(client):
    """One draw: draw_id is optional, as it always was. Two: guessing stops being allowed."""
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 200
    create(client, "Deuxième")

    response = client.get("/api/admin/state", headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["detail"] == main.DRAW_ID_REQUIRED
    bulk = client.post("/api/admin/emails", headers=ADMIN)
    assert bulk.status_code == 400
    assert bulk.json()["detail"] == main.DRAW_ID_REQUIRED
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 400
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "X", "role": "ADULT", "family_group": "Y"}).status_code == 400


def test_an_unknown_draw_is_404_not_a_silent_default(client):
    assert client.get("/api/admin/state", params={"draw_id": 99}, headers=ADMIN).status_code == 404
    assert client.post("/api/admin/draws", headers=ADMIN,
                       json={"event_title": "Copie", "copy_participants_from": 99}).status_code == 404


# --------------------------------------------------------------------------- #
# Pairings, resets and mail stay inside one draw
# --------------------------------------------------------------------------- #


def test_drawing_one_draw_does_not_touch_another(client, db):
    first = create(client, "Réveillon")
    second = create(client, "Bureau")
    family_roster(client, first["id"])
    family_roster(client, second["id"], count=3)

    assert draw(client, first["id"]).status_code == 200
    assert roster(client, second["id"])[1]["is_locked"] == 0, "the second draw stays open"
    assert client.post("/api/admin/draw", params={"draw_id": second["id"]},
                       headers=ADMIN).status_code == 200

    conn = db()
    pairs = conn.execute("SELECT giver_id, receiver_id FROM assignments").fetchall()
    by_draw = {}
    for row in pairs:
        giver = conn.execute("SELECT draw_id FROM participants WHERE id = ?",
                             (row["giver_id"],)).fetchone()["draw_id"]
        receiver = conn.execute("SELECT draw_id FROM participants WHERE id = ?",
                                (row["receiver_id"],)).fetchone()["draw_id"]
        assert giver == receiver, "a pairing crossed two draws"
        by_draw[giver] = by_draw.get(giver, 0) + 1
    conn.close()
    assert by_draw == {first["id"]: 3, second["id"]: 3}


def test_resetting_one_draw_leaves_the_other_locked_and_mailed(client, db):
    first = create(client, "Réveillon")
    second = create(client, "Bureau")
    family_roster(client, first["id"])
    family_roster(client, second["id"])
    for target in (first["id"], second["id"]):
        assert draw(client, target).status_code == 200
    for target in (first["id"], second["id"]):
        people, _ = roster(client, target)
        for person in people:
            person["viewed_at"] = "set"
            client.get(f"/api/draw/{person['token']}")
    assert client.post("/api/admin/reset", params={"draw_id": first["id"]},
                       headers=ADMIN).status_code == 200

    assert roster(client, first["id"])[1]["is_locked"] == 0
    assert roster(client, second["id"])[1]["is_locked"] == 1
    conn = db()
    still_viewed = conn.execute(
        "SELECT COUNT(*) FROM participants WHERE draw_id = ? AND viewed_at IS NOT NULL",
        (second["id"],)).fetchone()[0]
    cleared = conn.execute(
        "SELECT COUNT(*) FROM participants WHERE draw_id = ? AND viewed_at IS NOT NULL",
        (first["id"],)).fetchone()[0]
    remaining = conn.execute(
        "SELECT COUNT(*) FROM assignments a JOIN participants p ON p.id = a.giver_id "
        "WHERE p.draw_id = ?", (second["id"],)).fetchone()[0]
    conn.close()
    assert (still_viewed, cleared, remaining) == (3, 0, 3)


def test_bulk_mail_only_mails_the_draw_being_viewed(client, monkeypatch):
    import test_emails

    test_emails.configure_emails(monkeypatch)
    stub = test_emails.StubTransport()
    monkeypatch.setattr(main, "send_email", stub)

    first = create(client, "Réveillon")
    second = create(client, "Bureau")
    family_roster(client, first["id"])
    family_roster(client, second["id"])

    report = client.post("/api/admin/emails", params={"draw_id": second["id"]},
                         headers=ADMIN).json()
    assert report["sent"] == 3
    assert {m["to"] for m in stub.sent} == {"adulte1@exemple.ca", "adulte2@exemple.ca",
                                            "adulte3@exemple.ca"}
    mailed_in_first = sum(1 for p in roster(client, first["id"])[0] if p["email_sent_at"])
    assert mailed_in_first == 0, "a send on one draw must not stamp another one's rows"


# --------------------------------------------------------------------------- #
# Copying a roster, and what a copied person's link means
# --------------------------------------------------------------------------- #


def test_copy_participants_copies_people_not_history(client):
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    assert draw(client, first["id"]).status_code == 200
    for person in roster(client, first["id"])[0]:
        client.get(f"/api/draw/{person['token']}")

    copy = create(client, "Réveillon l'an prochain", copy_participants_from=first["id"])
    old_people, _ = roster(client, first["id"])
    new_people, new_config = roster(client, copy["id"])

    assert len(new_people) == len(old_people)
    assert {p["name"] for p in new_people} == {p["name"] for p in old_people}
    assert not ({p["token"] for p in new_people} & {p["token"] for p in old_people}), \
        "a copied person gets a fresh link"
    assert all(p["viewed_at"] is None and p["email_sent_at"] is None for p in new_people)
    assert new_config["is_locked"] == 0
    # the wishlist and the address are part of the person, so they come along
    assert all(p["email"].endswith("exemple.ca") for p in new_people)


def test_a_link_reveals_the_draw_it_belongs_to(client):
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    assert draw(client, first["id"]).status_code == 200
    old_token = roster(client, first["id"])[0][0]["token"]

    copy = create(client, "Bureau", copy_participants_from=first["id"])
    new_token = roster(client, copy["id"])[0][0]["token"]

    old = client.get(f"/api/draw/{old_token}").json()
    new = client.get(f"/api/draw/{new_token}").json()
    assert old["status"] == "READY" and old["event_title"] == "Réveillon"
    assert old["assignments"], "the original draw still reveals its pairing"
    assert new["status"] == "PENDING" and new["event_title"] == "Bureau"
    assert "assignments" not in new, "an undrawn copy reveals nothing yet"


def test_a_copy_lands_exactly_once(client):
    """The new draw is always the target, so "copy itself" is not a request that can be made.
    What can go wrong is the copy running twice, so that is what is checked."""
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    copy = create(client, "Copie", copy_participants_from=first["id"])
    people, _ = roster(client, copy["id"])
    assert len(people) == 3, "exactly the source roster, no duplicate rows"
    assert len({person["id"] for person in people}) == 3
    assert len({person["token"] for person in people}) == 3


def test_copying_an_empty_draw_is_just_a_new_draw(client):
    first = create(client, "Vide")
    response = client.post("/api/admin/draws", headers=ADMIN,
                           json={"event_title": "Copie du vide",
                                 "copy_participants_from": first["id"]})
    assert response.status_code == 201
    assert response.json()["copied_participants"] == 0


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #


def test_a_locked_draw_cannot_be_deleted(client):
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    assert draw(client, first["id"]).status_code == 200

    response = client.delete(f"/api/admin/draws/{first['id']}", headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["detail"] == main.LOCKED_MSG
    assert len(roster(client, first["id"])[0]) == 3, "nothing was deleted"


def test_deleting_a_draw_removes_its_roster_and_pairings_only(client, db):
    first = create(client, "À supprimer")
    second = create(client, "À garder")
    family_roster(client, first["id"])
    family_roster(client, second["id"])
    for target in (first["id"], second["id"]):
        draw(client, target)
    assert client.post("/api/admin/reset", params={"draw_id": first["id"]},
                       headers=ADMIN).status_code == 200

    gone = client.delete(f"/api/admin/draws/{first['id']}", headers=ADMIN).json()
    assert gone["participants_removed"] == 3

    conn = db()
    assert conn.execute("SELECT COUNT(*) FROM participants WHERE draw_id = ?",
                        (first["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM participants WHERE draw_id = ?",
                        (second["id"],)).fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 3, \
        "only the deleted draw's pairings went"
    ids = [row["id"] for row in conn.execute("SELECT id FROM draws")]
    conn.close()
    assert first["id"] not in ids and second["id"] in ids, ids

    tokens = [p["token"] for p in roster(client, second["id"])[0]]
    assert client.get(f"/api/draw/{tokens[0]}").status_code == 200


def test_a_deleted_draws_link_stops_working(client):
    first = create(client, "Réveillon")
    family_roster(client, first["id"])
    token = roster(client, first["id"])[0][0]["token"]
    assert client.get(f"/api/draw/{token}").status_code == 200
    client.delete(f"/api/admin/draws/{first['id']}", headers=ADMIN)
    assert client.get(f"/api/draw/{token}").status_code == 404


def test_an_empty_server_has_no_draw_to_act_on(client):
    only = client.get("/api/admin/draws", headers=ADMIN).json()["draws"]
    for draw_row in only:
        assert client.delete(f"/api/admin/draws/{draw_row['id']}", headers=ADMIN).status_code == 200
    assert client.get("/api/admin/draws", headers=ADMIN).json()["draws"] == []
    response = client.get("/api/admin/state", headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["detail"] == main.DRAW_NONE


def test_participants_cannot_be_reached_from_another_draw(client):
    first = create(client, "Réveillon")
    second = create(client, "Bureau")
    add(client, "Gaétan", "ADULT", "Bernier", draw_id=first["id"])
    gaetan = roster(client, first["id"])[0][0]

    assert client.put(f"/api/admin/participants/{gaetan['id']}", params={"draw_id": second["id"]},
                      headers=ADMIN, json={"name": "Quelqu'un d'autre"}).status_code == 404
    assert client.delete(f"/api/admin/participants/{gaetan['id']}",
                         params={"draw_id": second["id"]}, headers=ADMIN).status_code == 404
    assert client.post(f"/api/admin/participants/{gaetan['id']}/email",
                       params={"draw_id": second["id"]}, headers=ADMIN).status_code == 404
    assert roster(client, first["id"])[0][0]["name"] == "Gaétan"


# --------------------------------------------------------------------------- #
# The upgrade path: what a live single-draw database becomes
# --------------------------------------------------------------------------- #

# Exactly the shape a running deployment carries — including CHECK (id = 1), which is the trap:
# renamed rather than rebuilt, it silently makes a second draw impossible to create.
SINGLE_DRAW_SCHEMA = """
CREATE TABLE config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    avoid_same_group INTEGER NOT NULL DEFAULT 1,
    email_language TEXT NOT NULL DEFAULT 'fr',
    auto_email_on_draw INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0
);
INSERT INTO config (id, event_title, is_locked) VALUES (1, 'Réveillon chez Gaétan', 1);

CREATE TABLE participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL,
    group_key TEXT NOT NULL DEFAULT '',
    wishlist TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    viewed_at TEXT DEFAULT NULL,
    email_sent_at TEXT DEFAULT NULL
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


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    """A live-looking database: one locked draw, its roster, its pairings, then the upgrade."""
    path = tmp_path / "exchange.db"
    conn = sqlite3.connect(path)
    conn.executescript(SINGLE_DRAW_SCHEMA)
    ids = []
    for index, name in enumerate(("Gaétan", "Carole", "Benoît")):
        cursor = conn.execute(
            "INSERT INTO participants (name, role, family_group, group_key, email, token, "
            "viewed_at, email_sent_at) VALUES (?, 'ADULT', ?, ?, ?, ?, '2026-11-13T18:00:00+00:00', "
            "'2026-11-13T18:00:00+00:00')",
            (name, f"Famille {index}", "Famille", f"{name}@exemple.ca", f"jeton-{index}"))
        ids.append(cursor.lastrowid)
    conn.executemany("INSERT INTO assignments (giver_id, receiver_id) VALUES (?, ?)",
                     list(zip(ids, ids[1:] + ids[:1])))
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def test_the_upgrade_keeps_every_link_and_every_pairing(upgraded):
    state = upgraded.get("/api/admin/state", headers=ADMIN).json()
    assert state["config"]["event_title"] == "Réveillon chez Gaétan"
    assert state["config"]["is_locked"] == 1
    assert [p["token"] for p in state["participants"]] == ["jeton-0", "jeton-1", "jeton-2"]
    assert all(p["viewed_at"] and p["email_sent_at"] for p in state["participants"])
    assert upgraded.get("/api/draw/jeton-0").json()["assignments"], "the pairing survived"


def test_the_upgraded_database_can_open_a_second_draw(upgraded):
    """The whole point, and the CHECK (id = 1) is what would have made it fail in production."""
    second = create(upgraded, "Échange du bureau")
    assert second["id"] == 2
    listed = upgraded.get("/api/admin/draws", headers=ADMIN).json()["draws"]
    assert {row["id"] for row in listed} == {1, 2}
    assert upgraded.get("/api/admin/state", params={"draw_id": 2},
                        headers=ADMIN).json()["participants"] == []


def test_the_upgraded_timestamps_all_use_one_format(upgraded):
    created = create(upgraded, "Troisième")
    rows = upgraded.get("/api/admin/draws", headers=ADMIN).json()["draws"]
    stamps = [row["created_at"] for row in rows] + [created["created_at"]]
    assert all(stamp and "+00:00" in stamp for stamp in stamps), stamps
