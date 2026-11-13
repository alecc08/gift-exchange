"""Acceptance audit against a running instance.

    python tools/verify_acceptance.py [base-url]        # seeds the spec scenario, then verifies
    python tools/verify_acceptance.py --no-seed         # verify whatever is in the DB now

Seeds PLAN.md §7's scenario (10 adults + 10 children in 6 households, each adult draws 1 adult +
2 children — two of the households share the surname Tremblay, which is the point), runs the draw,
then checks all four acceptance criteria. Exit code 0 only when every criterion passes.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
DB = os.environ.get("GE_DB", "data/exchange.db")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
HEADERS = {"X-Admin-Password": PASSWORD, "Content-Type": "application/json"}

# surname, household. Two households deliberately share the surname Tremblay: that is the case the
# old engine could not tell apart, and the acceptance run is only meaningful if it is present.
HOUSEHOLDS = [
    ("Tremblay", "Tremblay (maison de Gaétan)"),
    ("Tremblay", "Tremblay (maison de Réjean)"),
    ("Gagnon", "Gagnon"),
    ("Bouchard", "Bouchard"),
    ("Roy", "Roy"),
    ("Ouellet", "Ouellet"),
]
ADULT_NAMES = [
    "Mario", "Lise", "Steven", "Josée", "Yvan", "Carole", "Éric", "Manon", "Réjean", "Nicole",
]
CHILD_NAMES = ["Florent", "Magalie", "Émile", "Rosalie", "Zack", "Béatrice", "Xavier", "Charlie",
               "Océane", "Tom"]


def request(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def seed_acceptance_scenario():
    """Rebuild the roster + config that PLAN.md §7 criterion 1 describes."""
    request("/api/admin/reset", "POST", {})
    _, state = request("/api/admin/state")
    for person in state["participants"]:
        request(f"/api/admin/participants/{person['id']}", "DELETE")
    status, body = request(
        "/api/admin/config",
        "PUT",
        {
            "event_title": "Acceptance scenario — 10 adults, 10 children, 6 households",
            "budget_note": "25 $ par personne",
            "adult_draws_adult": 1,
            "adult_draws_child": 2,
            "child_draws_adult": 0,
            "child_draws_child": 0,
            "avoid_same_group": True,
        },
    )
    assert status == 200, body
    for index, name in enumerate(ADULT_NAMES):
        surname, household = HOUSEHOLDS[index % len(HOUSEHOLDS)]
        status, body = request("/api/admin/participants", "POST", {
            "name": name, "role": "ADULT", "family_group": surname, "group_key": household})
        assert status == 201, body
    for index, name in enumerate(CHILD_NAMES):
        surname, household = HOUSEHOLDS[index % len(HOUSEHOLDS)]
        status, body = request("/api/admin/participants", "POST", {
            "name": name, "role": "CHILD", "family_group": surname, "group_key": household})
        assert status == 201, body
    status, body = request("/api/admin/draw", "POST", {})
    assert status == 200, body
    print(f"seeded acceptance scenario: {len(ADULT_NAMES)} adults + {len(CHILD_NAMES)} children "
          f"in {len(HOUSEHOLDS)} households -> {body['assignments_created']} assignments, "
          f"{body['same_group_crossings']} same-group crossing(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PLAN.md acceptance criteria against a live instance.")
    parser.add_argument("base", nargs="?", help="base URL (deprecated: use GE_BASE)")
    parser.add_argument("--no-seed", action="store_true", help="verify the current DB as-is")
    args = parser.parse_args()
    if args.base:
        print("note: pass GE_BASE=<url> to change the target; ignoring positional URL")

    if not args.no_seed:
        seed_acceptance_scenario()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"]
    bad = conn.execute(
        "SELECT COUNT(*) c FROM assignments a "
        "JOIN participants g ON g.id = a.giver_id "
        "JOIN participants r ON r.id = a.receiver_id "
        "WHERE g.id = r.id OR (g.group_key <> '' AND lower(trim(g.group_key)) = lower(trim(r.group_key)))"
    ).fetchone()["c"]
    per_role = {}
    for role in ("ADULT", "CHILD"):
        rows = conn.execute(
            "SELECT r.id, COUNT(*) c FROM assignments a JOIN participants r ON r.id = a.receiver_id "
            "WHERE r.role = ? GROUP BY r.id",
            (role,),
        ).fetchall()
        per_role[role + " receives"] = sorted({row["c"] for row in rows}) or [0]
        givers = conn.execute(
            "SELECT p.id, COUNT(*) c FROM assignments a JOIN participants p ON p.id = a.giver_id "
            "WHERE p.role = ? GROUP BY p.id",
            (role,),
        ).fetchall()
        roster = conn.execute("SELECT COUNT(*) c FROM participants WHERE role = ?", (role,)).fetchone()["c"]
        counts = {row["c"] for row in givers} | {0}
        per_role[role + " gives"] = sorted(counts) if roster else []
    conn.close()

    print("1) assignments rows            :", total)
    print("   self / same-group pairings  :", bad)
    for key, value in per_role.items():
        print(f"   {key:<22}:", value)

    status, state = request("/api/admin/state")
    payload = json.dumps(state).lower()
    leaks = [word for word in ("assignment", "giver", "receiver", "pairs") if word in payload]
    print("2) GET /api/admin/state        :", status, "keys:", sorted(state.keys()))
    print("   participant keys            :", sorted(state["participants"][0].keys()))
    print("   leaked assignment vocabulary:", leaks or "none")
    reported = state["config"]["same_group_crossings"]
    print("   same-group crossings (API)  :", reported, "(DB says", bad, ")")

    adult = next(p for p in state["participants"] if p["role"] == "ADULT" and p["viewed_at"] is None)
    before = adult["viewed_at"]
    status, draw = request(f"/api/draw/{adult['token']}")
    _, refreshed = request("/api/admin/state")
    after = next(p for p in refreshed["participants"] if p["token"] == adult["token"])["viewed_at"]
    print(f"3) GET /api/draw/<uuid> ({adult['name']}):", status, draw["status"],
          "with", len(draw["assignments"]), "assignments")
    print("   viewed_at                   :", before, "->", after)

    locked_code = request("/api/admin/config", "PUT", {"adult_draws_adult": 2})[0]
    add_code = request("/api/admin/participants", "POST",
                       {"name": "Late", "role": "ADULT", "family_group": "New"})[0]
    delete_code = request(f"/api/admin/participants/{state['participants'][0]['id']}", "DELETE")[0]
    print("4) while locked (PUT/POST/DELETE):", locked_code, add_code, delete_code)

    ok = (
        total == 30
        and bad == 0
        and reported == bad
        and per_role["CHILD receives"] == [2]
        and per_role["ADULT receives"] == [1]
        and per_role["ADULT gives"] == [0, 3]
        and per_role["CHILD gives"] == [0]
        and not leaks
        and before is None
        and after is not None
        and (locked_code, add_code, delete_code) == (400, 400, 400)
    )
    print("\nACCEPTANCE:", "PASS" if ok else "CHECK THE NUMBERS ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
