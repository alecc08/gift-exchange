"""Acceptance audit against a running instance: python tools/verify_acceptance.py [base-url]"""

import json
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8085"
DB = "data/exchange.db"
HEADERS = {"X-Admin-Password": "admin", "Content-Type": "application/json"}


def request(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"]
    bad = conn.execute(
        "SELECT COUNT(*) c FROM assignments a "
        "JOIN participants g ON g.id = a.giver_id "
        "JOIN participants r ON r.id = a.receiver_id "
        "WHERE g.id = r.id OR lower(trim(g.family_group)) = lower(trim(r.family_group))"
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
    print("   self / same-family pairings :", bad)
    for key, value in per_role.items():
        print(f"   {key:<22}:", value)

    status, state = request("/api/admin/state")
    payload = json.dumps(state).lower()
    leaks = [word for word in ("assignment", "giver", "receiver", "pairs") if word in payload]
    print("2) GET /api/admin/state        :", status, "keys:", sorted(state.keys()))
    print("   participant keys            :", sorted(state["participants"][0].keys()))
    print("   leaked assignment vocabulary:", leaks or "none")

    token = state["participants"][0]["token"]
    before = next(p for p in state["participants"] if p["token"] == token)["viewed_at"]
    status, draw = request(f"/api/draw/{token}")
    _, refreshed = request("/api/admin/state")
    after = next(p for p in refreshed["participants"] if p["token"] == token)["viewed_at"]
    print("3) GET /api/draw/<uuid>        :", status, draw["status"], "with", len(draw["assignments"]), "assignments")
    print("   viewed_at                   :", before, "->", after)

    locked_code = request("/api/admin/config", "PUT", {"adult_draws_adult": 2})[0]
    add_code = request("/api/admin/participants", "POST", {"name": "Late", "role": "ADULT", "family_group": "New"})[0]
    delete_code = request(f"/api/admin/participants/{state['participants'][0]['id']}", "DELETE")[0]
    print("4) while locked (PUT/POST/DELETE):", locked_code, add_code, delete_code)

    ok = (
        total == 30
        and bad == 0
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
