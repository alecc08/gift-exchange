"""Anonymous coordination notes between gift givers.

Why this file is bigger than the feature looks like it deserves: a note says "I am buying this for
that person", so the interesting failures are not crashes, they are leaks. The person the note is
about must not see it (it spoils the gift), the organizer must not see it (the console is blind to
pairings by design), and no reader may be able to tell *which* household wrote it. Those three are
the contract; the rest is that a note follows the draw it was written for and disappears with it.

The scenario is the one that made the feature necessary: ten adults each drawing one adult and two
children, ten children, so every child is being shopped for by three households who have no other way
to compare plans.

Unlike the rest of the suite, these tests read the pairings out of the database. There is no choice:
the whole subject is a pairing the API refuses to show anybody, so a test that wants "two givers of
the same person" has to ask the only place that knows.
"""

import json

import main
from conftest import ADMIN, add, set_config

FESTIVE = dict(event_title="Échange de Noël", budget_note="25 $ maximum",
               adult_draws_adult=1, adult_draws_child=2, child_draws_adult=0, child_draws_child=0)

# One read, three jobs: which person two givers share, each giver's token, and the handle that
# giver's card carries for that person.
PAIRS_SQL = (
    "SELECT a.id AS assignment_id, a.note_key, g.id AS giver_id, g.name AS giver_name, "
    "       g.token AS giver_token, r.id AS receiver_id, r.name AS receiver_name "
    "FROM assignments a JOIN participants g ON g.id = a.giver_id "
    "JOIN participants r ON r.id = a.receiver_id "
    "ORDER BY r.id, a.id"
)


def roster(client, adults=10, children=10, families=6):
    people = []
    for index in range(adults):
        people.append(add(client, f"A{index + 1}", "ADULT", f"Fam{index % families}"))
    for index in range(children):
        people.append(add(client, f"C{index + 1}", "CHILD", f"Fam{index % families}"))
    return people


def drawn_roster(client):
    """The festive rules, a roster, and a draw that has really run."""
    set_config(client, **FESTIVE)
    roster(client)
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200


def pairs(db):
    conn = db()
    try:
        return [dict(row) for row in conn.execute(PAIRS_SQL)]
    finally:
        conn.close()


def co_givers(db):
    """Every giver of one person who has at least two of them.

    With these rules each child has exactly three givers, so such a person exists by arithmetic — but
    *which* one is the engine's business, so it is read back rather than assumed.
    """
    grouped: dict[int, list] = {}
    for pair in pairs(db):
        grouped.setdefault(pair["receiver_id"], []).append(pair)
    for group in grouped.values():
        if len(group) >= 2:
            return group
    raise AssertionError("nobody ended up with two gift givers, so this scenario proves nothing")


def note_count(db):
    conn = db()
    try:
        return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()


def view(client, giver):
    """One giver's own payload, straight from their link."""
    return client.get(f"/api/draw/{giver['giver_token']}").json()


def card(client, giver):
    """The card this giver holds for the person they share with somebody else."""
    matches = [item for item in view(client, giver)["assignments"]
               if item["name"] == giver["receiver_name"]]
    assert matches, f"{giver['giver_name']} was never given {giver['receiver_name']} as a pick"
    return matches[0]


def notes_on(client, giver):
    return card(client, giver)["notes"]


def write(client, giver, text, handle=None):
    return client.put(f"/api/draw/{giver['giver_token']}/note",
                      json={"to": handle or giver["note_key"], "text": text})


def test_two_households_shopping_for_the_same_child_see_each_other(client, db):
    """The feature, in one test: write once, and the other household's card carries it."""
    drawn_roster(client)
    first, second = co_givers(db)[:2]

    assert write(client, first, "je prends le livre de cuisine").status_code == 200

    assert notes_on(client, first) == [{"text": "je prends le livre de cuisine", "mine": True}]
    assert notes_on(client, second) == [{"text": "je prends le livre de cuisine", "mine": False}], \
        "the second household cannot see what the first one is buying"


def test_a_shared_pick_says_how_many_givers_it_has(client, db):
    """The page needs one number: is this pick worth a note, or am I the only one giving to them?

    A count and not a list. `co_givers` is the only piece of draw structure a participant is told, and
    it cannot name anybody — so the same rule as everywhere else still applies.
    """
    drawn_roster(client)
    group = co_givers(db)
    shared = card(client, group[0])
    assert shared["co_givers"] == len(group), "the card does not know how many households share it"

    # An adult under these rules has exactly one giver, so their card must say 1: that is what keeps
    # the note box off a pick nobody shares.
    every = pairs(db)
    solo = next(pair for pair in every
                if len([other for other in every if other["receiver_id"] == pair["receiver_id"]]) == 1)
    assert card(client, solo)["co_givers"] == 1, "a one-giver pick was counted as shared"
    payload = json.dumps(view(client, group[0]))
    assert '"co_giver_names"' not in payload and '"giver_id"' not in payload


def test_a_note_never_names_its_author(client, db):
    """Anonymous means no name, no author id, no timestamp: nothing to trace a sentence to a house.

    A timestamp is the subtlest of the three — two co-givers could work out who wrote what from the
    order the notes arrived in, and *that* is the pairing this app keeps from everybody, the organizer
    included.
    """
    drawn_roster(client)
    givers = co_givers(db)
    for index, giver in enumerate(givers):
        assert write(client, giver, f"idée {index + 1}").status_code == 200

    body = view(client, givers[0])
    payload = json.dumps(body)
    assert '"author"' not in payload and '"author_id"' not in payload and '"updated_at"' not in payload

    # What the page may name: this person, and the picks under their card. Anything else — including
    # the co-giver whose note is sitting right there on the card — stays unnamed.
    named = {body["participant"]["name"]} | {item["name"] for item in body["assignments"]}
    strangers = [pair["giver_name"] for pair in pairs(db) if pair["giver_name"] not in named]
    assert strangers, "nobody is outside this page, so the check would prove nothing"
    assert all(f'"{name}"' not in payload for name in strangers), \
        "a person who is not one of my picks was named on my page"
    assert len(notes_on(client, givers[0])) == len(givers), "the anonymous list lost one of its lines"


def test_the_person_the_note_is_about_never_sees_it(client, db):
    """Spoiler rule: three households planning a child's gift do not plan it in front of the child.

    Checked against the whole payload rather than one field, because the leak that would matter is a
    helpful future field — "your notes" — bolted onto the person's own view.
    """
    drawn_roster(client)
    givers = co_givers(db)
    assert write(client, givers[0], "le jeu de société est pris").status_code == 200

    conn = db()
    token = conn.execute("SELECT token FROM participants WHERE id = ?",
                         (givers[0]["receiver_id"],)).fetchone()["token"]
    conn.close()
    body = json.dumps(client.get(f"/api/draw/{token}").json())
    assert "le jeu de société est pris" not in body, \
        "a relative was shown what other people are buying for them"


def test_the_organizer_cannot_read_notes(client, db):
    """The console stays blind. A note is family conversation, not a roster column.

    `admin_state` is the console's one big read and it has never carried a pairing. Notes name their
    receiver to whoever can list them, so exposing them is how who-picks-who finally leaks.
    """
    drawn_roster(client)
    givers = co_givers(db)
    assert write(client, givers[0], "j’apporte le jeu").status_code == 200

    for path in ("/api/admin/state", "/api/admin/draws"):
        body = json.dumps(client.get(path, headers=ADMIN).json())
        assert "j’apporte le jeu" not in body, f"{path} carried a note's text"
        assert "notes" not in body and "note_key" not in body, f"{path} grew a notes field"


def test_a_note_is_only_for_people_you_actually_picked(client, db):
    """A handle resolves through `giver_id = me`, so it cannot be aimed at somebody else.

    Three ways to try it, all refused the same way: a real handle copied off a co-giver's card for the
    same person (the giver check is what refuses it — the handle is valid and in the same draw), a
    handle for a person this giver never drew, and nonsense.
    """
    drawn_roster(client)
    givers = co_givers(db)
    assert write(client, givers[1], "intrusion", handle=givers[0]["note_key"]).status_code == 404

    elsewhere = next(pair for pair in pairs(db)
                     if pair["giver_id"] != givers[0]["giver_id"]
                     and pair["receiver_id"] != givers[0]["receiver_id"])
    assert write(client, givers[0], "pas mon monde", handle=elsewhere["note_key"]).status_code == 404
    assert write(client, givers[0], "x", handle="not-a-handle").status_code == 404

    texts = [note["text"] for note in notes_on(client, givers[0])]
    assert "intrusion" not in texts and "pas mon monde" not in texts


def test_notes_open_when_the_draw_runs(client):
    """Before the draw there is no "my person" to write about, and the answer says so."""
    set_config(client, **FESTIVE)
    people = roster(client)
    response = client.put(f"/api/draw/{people[0]['token']}/note",
                          json={"to": "00000000-0000-0000-0000-000000000000", "text": "trop tôt"})
    assert response.status_code == 409
    assert "draw has been run" in response.json()["detail"]
    assert client.get(f"/api/draw/{people[0]['token']}").json()["status"] == "PENDING"


def test_one_note_per_giver_per_person(client, db):
    """Overwrite, not a thread — and an empty note means "Effacer", not a blank line on a card."""
    drawn_roster(client)
    first, second = co_givers(db)[:2]

    assert write(client, first, "première idée").status_code == 200
    assert write(client, first, "deuxième idée").status_code == 200
    assert notes_on(client, second) == [{"text": "deuxième idée", "mine": False}]
    assert note_count(db) == 1, "a giver got a thread where there should be one line"

    assert write(client, first, "").status_code == 200
    assert notes_on(client, second) == []
    assert write(client, first, "   ").status_code == 200          # whitespace clears too
    assert note_count(db) == 0


def test_the_note_limit_is_the_one_the_page_advertises(client, db):
    drawn_roster(client)
    first = co_givers(db)[0]

    assert client.get("/api/limits").json()["note"] == main.MAX_NOTE == 300
    assert write(client, first, "x" * main.MAX_NOTE).status_code == 200
    assert write(client, first, "x" * (main.MAX_NOTE + 1)).status_code == 422
    assert client.put(f"/api/draw/{first['giver_token']}/note",
                      json={"to": "", "text": "du texte"}).status_code == 422      # a pick is needed
    assert client.put(f"/api/draw/{first['giver_token']}/note",
                      json={"to": first["note_key"]}).status_code == 422      # `text` is required


def test_a_reset_takes_the_notes_and_the_handles_with_them(client, db):
    """A note belongs to one draw's result. Re-draw, and both the words and the keys are gone.

    The handle half matters as much as the privacy half: new pairings mint new keys, so a tab left
    open across a reset cannot write into the new draw with the old handle.
    """
    drawn_roster(client)
    first, second = co_givers(db)[:2]
    assert write(client, first, "cadeau prévu").status_code == 200

    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 200
    assert note_count(db) == 0, "a reset left notes behind"

    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200
    stale = first["note_key"]
    assert all(pair["note_key"] != stale for pair in pairs(db)), \
        "a re-draw kept handles a relative may have copied somewhere"
    assert write(client, first, "je rejoue avec l’ancienne clé", handle=stale).status_code == 404

    fresh_first, fresh_second = co_givers(db)[:2]
    assert write(client, fresh_first, "nouvelle idée").status_code == 200
    assert notes_on(client, fresh_second) == [{"text": "nouvelle idée", "mine": False}]


def test_notes_are_scoped_to_one_draw(client, db):
    """Two events on one server: a note from the family draw is not a note in next year's.

    The read path is keyed by the reader's own picks, so this is mostly about that join never crossing
    draws — `notes` has no draw_id column, so the join is the whole guard. A copied roster must not
    carry last season's conversation into this season's either.
    """
    drawn_roster(client)
    first, second = co_givers(db)[:2]
    assert write(client, first, "carte-cadeau prise").status_code == 200

    created = client.post("/api/admin/draws", headers=ADMIN,
                          json={"event_title": "Réveillon", "copy_participants_from": 1}).json()
    new_draw = created["draw"]
    assert created["copied_participants"] == 20
    conn = db()
    strays = conn.execute(
        "SELECT COUNT(DISTINCT n.id) FROM notes n JOIN participants p "
        "ON p.id IN (n.author_id, n.receiver_id) WHERE p.draw_id = ?", (new_draw["id"],)).fetchone()[0]
    conn.close()
    assert strays == 0, "a copied roster brought last year's notes along"

    rules = {key: value for key, value in FESTIVE.items() if key != "event_title"}
    assert client.put("/api/admin/config", headers=ADMIN, params={"draw_id": new_draw["id"]},
                      json=rules).status_code == 200
    assert client.post("/api/admin/draw", headers=ADMIN,
                       params={"draw_id": new_draw["id"]}).status_code == 200

    assert "carte-cadeau prise" in json.dumps(view(client, first)), \
        "the second draw erased the first one's note"
    conn = db()
    copy_token = conn.execute(
        "SELECT token FROM participants WHERE draw_id = ? AND name = ?",
        (new_draw["id"], first["giver_name"])).fetchone()["token"]
    conn.close()
    assert "carte-cadeau prise" not in json.dumps(client.get(f"/api/draw/{copy_token}").json()), \
        "the copied person arrived with last season's note attached"


def test_a_note_cannot_outlive_a_participant(client, db):
    """Defense in depth, not a path the API can reach.

    Deleting a person means unlocking the draw, and unlocking wipes the notes — so the FK cascade is
    what protects the data if a row is ever removed by hand or by a future flow. Pinned because a
    cascade only exists if somebody remembers to declare it.
    """
    drawn_roster(client)
    first = co_givers(db)[0]
    assert write(client, first, "au cas où").status_code == 200

    conn = db()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM participants WHERE id = ?", (first["giver_id"],))
    conn.execute("DELETE FROM participants WHERE id = ?", (first["receiver_id"],))
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    conn.close()


def test_pairings_from_before_the_feature_get_a_handle(client, db):
    """The upgrade case: a family mid-draw gets the feature without redoing their draw.

    `note_key` arrives on an existing database by ALTER, which cannot carry a default, so the
    migration backfills one. Without that, every pairing drawn before the upgrade would have no handle
    to write against, and the feature would look broken to exactly the household that is mid-event
    right now. The index is dropped first because SQLite refuses to drop a column an index covers —
    and the upgrade recreating it is part of what is being proved.
    """
    drawn_roster(client)
    conn = db()
    conn.execute("DROP INDEX assignments_note_key")
    conn.execute("ALTER TABLE assignments DROP COLUMN note_key")
    conn.close()

    main.init_db()                          # the same upgrade path the container runs at startup

    conn = db()
    missing = conn.execute("SELECT COUNT(*) FROM assignments WHERE note_key IS NULL").fetchone()[0]
    handles = [row["note_key"] for row in conn.execute("SELECT note_key FROM assignments")]
    indexed = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'assignments_note_key'"
    ).fetchone()[0]
    conn.close()
    assert missing == 0, "a pairing drawn before this feature has no note handle"
    assert indexed == 1, "the handle index was not recreated by the upgrade"
    assert len(set(handles)) == len(handles) and None not in handles, "two pairings share a handle"

    first, second = co_givers(db)[:2]
    assert write(client, first, "après la mise à jour").status_code == 200
    assert notes_on(client, second) == [{"text": "après la mise à jour", "mine": False}]
