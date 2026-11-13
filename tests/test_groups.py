"""Grouping: a household is not a surname, and the same-group rule is a preference.

The API half of the change (the matching invariants live in test_engine.py): what the console can
set, what a participant record carries, what a database written before the field looks like after an
upgrade, and what the console is told when a draw had to relax.
"""

import sqlite3

from conftest import ADMIN, add, set_config

# The schema exactly as it was before `group_key` and `avoid_same_group` existed. Written out rather
# than derived from main.SCHEMA on purpose: the point is that an old file upgrades itself.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_title TEXT NOT NULL DEFAULT 'Christmas Gift Exchange',
    adult_draws_adult INTEGER NOT NULL DEFAULT 1,
    adult_draws_child INTEGER NOT NULL DEFAULT 2,
    child_draws_adult INTEGER NOT NULL DEFAULT 0,
    child_draws_child INTEGER NOT NULL DEFAULT 0,
    budget_note TEXT NOT NULL DEFAULT '',
    is_locked INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO config (id) VALUES (1);
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADULT', 'CHILD')),
    family_group TEXT NOT NULL,
    wishlist TEXT NOT NULL DEFAULT '',
    token TEXT UNIQUE NOT NULL,
    viewed_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    FOREIGN KEY (giver_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (receiver_id) REFERENCES participants(id) ON DELETE CASCADE,
    UNIQUE(giver_id, receiver_id)
);
"""


def test_limits_advertise_the_group_field(client):
    limits = client.get("/api/limits").json()
    assert limits["group_key"] == 60
    assert limits["family_group"] == 60


def test_participant_carries_group_independently_of_the_name(client):
    created = add(client, "Julie", "ADULT", "Tremblay", group="Tremblay-Deux")
    assert created["family_group"] == "Tremblay"
    assert created["group_key"] == "Tremblay-Deux"

    state = client.get("/api/admin/state", headers=ADMIN).json()
    assert state["participants"][0]["group_key"] == "Tremblay-Deux"


def test_group_may_be_blank_but_the_name_may_not(client):
    lonely = add(client, "Marc", "ADULT", "Roy", group="")
    assert lonely["group_key"] == ""

    unnamed = client.post("/api/admin/participants", headers=ADMIN,
                          json={"name": "Sans nom", "role": "ADULT", "family_group": "   "})
    assert unnamed.status_code == 422


def test_avoid_same_group_is_a_setting_and_round_trips(client):
    assert client.get("/api/admin/state", headers=ADMIN).json()["config"]["avoid_same_group"] == 1

    off = set_config(client, avoid_same_group=False)
    assert off["avoid_same_group"] == 0
    assert set_config(client, avoid_same_group=True)["avoid_same_group"] == 1


def test_two_households_sharing_a_surname_draw_freely(client):
    """The actual complaint: everyone is Tremblay, but two of them are one household and two are another."""
    set_config(client, adult_draws_adult=1, adult_draws_child=0, child_draws_adult=0, child_draws_child=0)
    for name, group in (("Aline", "Tremblay-Un"), ("Benoit", "Tremblay-Un"),
                        ("Carole", "Tremblay-Deux"), ("Denis", "Tremblay-Deux")):
        add(client, name, "ADULT", "Tremblay", group=group)

    drawn = client.post("/api/admin/draw", headers=ADMIN)
    assert drawn.status_code == 200
    assert drawn.json()["same_group_crossings"] == 0, "the surname must not have grouped them"


def test_a_forced_crossing_is_reported_instead_of_refused(client):
    set_config(client, adult_draws_adult=1, adult_draws_child=0, child_draws_adult=0, child_draws_child=0)
    add(client, "Aline", "ADULT", "Roy", group="Roy")
    add(client, "Benoit", "ADULT", "Roy", group="Roy")
    add(client, "Carole", "ADULT", "Nolin", group="Nolin")

    drawn = client.post("/api/admin/draw", headers=ADMIN)
    assert drawn.status_code == 200, drawn.text
    assert drawn.json()["same_group_crossings"] == 1
    assert client.get("/api/admin/state", headers=ADMIN).json()["config"]["same_group_crossings"] == 1


def test_turning_the_preference_off_lets_a_single_household_draw(client):
    set_config(client, adult_draws_adult=1, adult_draws_child=0, child_draws_adult=0, child_draws_child=0,
               avoid_same_group=False)
    add(client, "Aline", "ADULT", "Roy", group="Roy")
    add(client, "Benoit", "ADULT", "Roy", group="roy")

    drawn = client.post("/api/admin/draw", headers=ADMIN)
    assert drawn.status_code == 200, drawn.text
    assert drawn.json()["assignments_created"] == 2
    # groups were not consulted, so nothing counts as a crossing — on the draw *and* in the state,
    # where counting the raw same-group pairs would report a relaxation that was never forced
    assert drawn.json()["same_group_crossings"] == 0
    assert client.get("/api/admin/state", headers=ADMIN).json()["config"]["same_group_crossings"] == 0


def test_old_database_upgrades_and_keeps_drawing_the_way_it_did(tmp_path, monkeypatch):
    """A roster built before `group_key` existed must keep its old household boundaries."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO participants (name, role, family_group, token) VALUES ('Aline','ADULT','Roy','t1')")
    conn.execute("INSERT INTO participants (name, role, family_group, token) VALUES ('Benoit','ADULT','Nolin','t2')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(path))
    import main
    main.init_db()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name, family_group, group_key FROM participants ORDER BY id").fetchall()
    assert [row["group_key"] for row in rows] == ["Roy", "Nolin"]
    assert conn.execute("SELECT avoid_same_group FROM config WHERE id = 1").fetchone()[0] == 1
    conn.close()

    # idempotent: running the migration again changes nothing
    main.init_db()
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT group_key FROM participants ORDER BY id")] == ["Roy", "Nolin"]
    conn.close()
