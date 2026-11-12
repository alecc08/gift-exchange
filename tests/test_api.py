"""API behaviour: auth, blind organizer, locking, token flow (spec sections 5 & 7)."""

import json
import re

from conftest import ADMIN, add, set_config

FESTIVE = dict(event_title="Échange de Noël", budget_note="25 € maximum",
               adult_draws_adult=1, adult_draws_child=2, child_draws_adult=0, child_draws_child=0)


def build(client, adults=10, children=10, families=5):
    people = []
    for index in range(adults):
        people.append(add(client, f"A{index + 1}", "ADULT", f"Fam{index % families}"))
    for index in range(children):
        people.append(add(client, f"C{index + 1}", "CHILD", f"Fam{index % families}"))
    return people


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert 'id="reveal-btn"' in page.text          # participant reveal control
    assert "Révéler mes cadeaux" in page.text      # French is the default language


def test_login_and_admin_auth(client):
    assert client.post("/api/admin/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "s3cret"}).status_code == 200
    assert client.get("/api/admin/state").status_code == 401
    assert client.get("/api/admin/state", headers={"X-Admin-Password": "nope"}).status_code == 401
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 200


def test_config_and_roster_defaults(client):
    state = client.get("/api/admin/state", headers=ADMIN).json()
    assert state["config"]["is_locked"] == 0
    assert state["config"]["event_title"] == "Christmas Gift Exchange"
    assert (state["config"]["adult_draws_adult"], state["config"]["adult_draws_child"]) == (1, 2)

    updated = set_config(client, event_title="Secret Santa 2026", adult_draws_adult=2,
                         adult_draws_child=1, child_draws_adult=1, child_draws_child=0,
                         budget_note="$30 max")
    assert updated["adult_draws_adult"] == 2 and updated["budget_note"] == "$30 max"

    person = add(client, "Alec", "ADULT", "C08", "lego")
    assert re.fullmatch(r"[0-9a-f-]{36}", person["token"])
    assert person["viewed_at"] is None
    assert len(client.get("/api/admin/state", headers=ADMIN).json()["participants"]) == 1


def test_quota_validation_rejects_out_of_range(client):
    assert client.put("/api/admin/config", headers=ADMIN, json={"adult_draws_adult": 11}).status_code == 422
    assert client.put("/api/admin/config", headers=ADMIN, json={"child_draws_child": -1}).status_code == 422


def test_blank_name_or_group_rejected(client):
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "  ", "role": "ADULT", "family_group": "x"}).status_code == 422
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "x", "role": "TEEN", "family_group": "y"}).status_code == 422


def test_acceptance_no_assignments_leak_to_admin(client):
    """Acceptance criterion 2: no assignment mapping anywhere in the admin payload."""
    set_config(client, **FESTIVE)
    build(client)
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200

    payload = client.get("/api/admin/state", headers=ADMIN)
    assert payload.status_code == 200
    text = json.dumps(payload.json())
    for forbidden in ("assignment", "giver", "receiver", "pairs"):
        assert forbidden not in text.lower()

    # re-drawing a locked event is refused instead of silently invalidating links
    again = client.post("/api/admin/draw", headers=ADMIN)
    assert again.status_code == 409
    assert "already locked" in again.json()["detail"]


def test_draw_matches_roster_in_db(client, db):
    set_config(client, **FESTIVE)
    build(client)
    response = client.post("/api/admin/draw", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["assignments_created"] == 30

    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    same_family = conn.execute(
        "SELECT COUNT(*) FROM assignments a JOIN participants g ON g.id = a.giver_id "
        "JOIN participants r ON r.id = a.receiver_id "
        "WHERE lower(trim(g.family_group)) = lower(trim(r.family_group)) OR g.id = r.id"
    ).fetchone()[0]
    child_receivers = conn.execute(
        "SELECT COUNT(*) FROM (SELECT r.id, COUNT(*) c FROM assignments a "
        "JOIN participants r ON r.id = a.receiver_id WHERE r.role = 'CHILD' GROUP BY r.id HAVING c <> 2)"
    ).fetchone()[0]
    adult_givers = conn.execute(
        "SELECT COUNT(*) FROM (SELECT a.giver_id, COUNT(*) c FROM assignments a "
        "JOIN participants p ON p.id = a.giver_id WHERE p.role = 'ADULT' GROUP BY a.giver_id HAVING c <> 3)"
    ).fetchone()[0]
    child_givers = conn.execute(
        "SELECT COUNT(*) FROM assignments a JOIN participants p ON p.id = a.giver_id WHERE p.role = 'CHILD'"
    ).fetchone()[0]
    conn.close()

    assert total == 30
    assert same_family == 0
    assert child_receivers == 0     # every child received exactly twice
    assert adult_givers == 0        # every adult draws exactly three
    assert child_givers == 0        # children draw none


def test_locked_roster_and_config_are_read_only(client):
    """Acceptance criterion 4: mutations while is_locked == 1 return 400."""
    set_config(client, **FESTIVE)
    people = build(client, adults=4, children=4, families=2)
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200

    assert client.put("/api/admin/config", headers=ADMIN,
                      json={"adult_draws_adult": 2}).status_code == 400
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "Late", "role": "ADULT", "family_group": "New"}).status_code == 400
    assert client.delete(f"/api/admin/participants/{people[0]['id']}", headers=ADMIN).status_code == 400

    state = client.get("/api/admin/state", headers=ADMIN).json()
    assert state["config"]["adult_draws_adult"] == 1
    assert len(state["participants"]) == 8


def test_viewed_at_transitions_and_token_flow(client):
    """Acceptance criterion 3: opening a link flips viewed_at, visible to the organizer."""
    set_config(client, **FESTIVE)
    people = build(client, adults=4, children=4, families=2)
    adult, child = people[0], people[4]

    pending = client.get(f"/api/draw/{adult['token']}")
    assert pending.status_code == 200
    assert pending.json()["status"] == "PENDING"
    assert pending.json()["event_title"] == "Échange de Noël"
    assert "budget_note" not in pending.json()
    assert client.get("/api/admin/state", headers=ADMIN).json()["participants"][0]["viewed_at"] is None

    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200

    ready = client.get(f"/api/draw/{adult['token']}")
    body = ready.json()
    assert body["status"] == "READY"
    assert body["budget_note"] == "25 € maximum"
    assert len(body["assignments"]) == 3
    assert {a["role"] for a in body["assignments"]} == {"ADULT", "CHILD"}

    second = client.get(f"/api/draw/{adult['token']}").json()
    assert second["assignments"] == body["assignments"]   # stable across reloads

    state = client.get("/api/admin/state", headers=ADMIN).json()
    viewed = {p["token"]: p["viewed_at"] for p in state["participants"]}
    assert viewed[adult["token"]] is not None
    assert viewed[child["token"]] is None

    child_view = client.get(f"/api/draw/{child['token']}").json()
    assert child_view["status"] == "READY" and child_view["assignments"] == []

    assert client.get("/api/draw/not-a-uuid").status_code == 404


def test_wishlist_editable_through_token_even_when_locked(client):
    set_config(client, adult_draws_adult=1, adult_draws_child=0, child_draws_adult=0, child_draws_child=0)
    people = build(client, adults=3, children=0, families=3)
    token = people[0]["token"]

    assert client.put(f"/api/draw/{token}/wishlist", json={"wishlist": "avant"}).status_code == 200
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200
    assert client.put(f"/api/draw/{token}/wishlist", json={"wishlist": "un train miniature"}).status_code == 200

    giver = next(p for p in people if p["id"] != people[0]["id"])
    seen = [
        wishlist
        for person in people
        for wishlist in [a["wishlist"] for a in client.get(f"/api/draw/{person['token']}").json()["assignments"]]
    ]
    assert "un train miniature" in seen
    assert client.put("/api/draw/bogus/wishlist", json={"wishlist": "x"}).status_code == 404


def test_reset_clears_assignments_and_viewed_markers(client, db):
    set_config(client, **FESTIVE)
    people = build(client, adults=4, children=4, families=2)
    client.post("/api/admin/draw", headers=ADMIN)
    client.get(f"/api/draw/{people[0]['token']}")

    assert client.post("/api/admin/reset", headers=ADMIN).status_code == 200
    conn = db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM participants WHERE viewed_at IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT is_locked FROM config WHERE id = 1").fetchone()[0] == 0
    conn.close()

    # roster survives the reset and can be edited again
    assert len(client.get("/api/admin/state", headers=ADMIN).json()["participants"]) == 8
    assert client.put("/api/admin/config", headers=ADMIN, json={"adult_draws_adult": 2}).status_code == 200


def test_draw_guard_rails(client, db):
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 400

    set_config(client, adult_draws_adult=1, adult_draws_child=0, child_draws_adult=0, child_draws_child=0)
    add(client, "Solo", "ADULT", "Martin")
    add(client, "Spouse", "ADULT", "martin")
    failed = client.post("/api/admin/draw", headers=ADMIN)
    assert failed.status_code == 422
    assert failed.json()["detail"] == (
        "Constraints are mathematically unsolvable with current family groups and draw counts."
    )
    conn = db()
    assert conn.execute("SELECT is_locked FROM config WHERE id = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    conn.close()

    assert client.post("/api/admin/draw").status_code == 401          # auth before anything else
    assert client.post("/api/admin/reset").status_code == 401
