"""Seed a running deployment with a demo family roster (for demos and screenshots).

    python tools/seed_demo.py                 # reset + seed, leaves the draw open
    python tools/seed_demo.py --draw          # ... and generate + lock the draw

Reads GE_BASE (default http://127.0.0.1:8085) and ADMIN_PASSWORD (default "admin").
Prints the private participant links. Stdlib only, like verify_acceptance.py.
"""

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("GE_BASE", "http://127.0.0.1:8085").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

ROSTER = [
    ("Nicolas", "ADULT", "Martin", "Un vin nature, des verres à pied, un bon couteau de cuisine"),
    ("Julie", "ADULT", "Martin", "Des plantes d'intérieur, un plaid en laine, de la céramique"),
    ("Sophie", "ADULT", "Dubois", "Un roman de Modiano, un coffret de thés, un vélo de ville"),
    ("Marc", "ADULT", "Dubois", "Des vinyles de jazz, une machine à expresso, un sac de randonnée"),
    ("Clara", "ADULT", "Nguyen", "Un abonnement à la médiathèque, des crayons aquarellables"),
    ("Théo", "ADULT", "Faure", "Un échiquier en bois, un cours de menuiserie"),
    ("Léa", "CHILD", "Martin", "Des feutres Stabilo, un livre sur les volcans, une peluche hérisson"),
    ("Camille", "CHILD", "Dubois", "Un vélo, des LEGO, le jeu de société Taboo"),
    ("Manon", "CHILD", "Nguyen", "Un appareil photo enfant, un carnet de dessins, des chaussettes à licornes"),
    ("Hugo", "CHILD", "Faure", "Un football, un jeu Pokémon, des briques de construction"),
]


def call(path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read() or b"null"
    return response.status, json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description="Seed a demo roster into a running instance.")
    parser.add_argument("--draw", action="store_true", help="generate and lock the draw too")
    args = parser.parse_args()

    _, login = call("/api/admin/login", "POST", {"password": PASSWORD})
    auth = {"Authorization": f"Bearer {login['token']}"}

    call("/api/admin/reset", "POST", {}, auth)
    _, state = call("/api/admin/state", headers=auth)
    for person in state["participants"]:
        call(f"/api/admin/participants/{person['id']}", "DELETE", None, auth)

    call(
        "/api/admin/config",
        "PUT",
        {
            "event_title": "Échange de cadeaux des Martin & Dubois",
            "budget_note": "25 € maximum — les cadeaux faits maison sont les bienvenus",
            "adult_draws_adult": 1,
            "adult_draws_child": 2,
            "child_draws_adult": 0,
            "child_draws_child": 0,
        },
        auth,
    )

    for name, role, group, wishlist in ROSTER:
        call(
            "/api/admin/participants",
            "POST",
            {"name": name, "role": role, "family_group": group, "wishlist": wishlist},
            auth,
        )

    _, state = call("/api/admin/state", headers=auth)
    print(f"seeded {len(state['participants'])} participants at {BASE}")
    for person in state["participants"]:
        print(f"  {person['name']:<9} {BASE}/?t={person['token']}")

    if args.draw:
        status, body = call("/api/admin/draw", "POST", {}, auth)
        print("draw:", status, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
