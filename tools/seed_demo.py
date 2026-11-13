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
    ("Mario", "ADULT", "Tremblay", "Une tuque en laine, un bon bourbon, des gants de vélo"),
    ("Julie", "ADULT", "Tremblay", "Des plantes vivaces, une couple de casseroles en fonte, un foulard"),
    ("Lise", "ADULT", "Gagnon", "Un cours de poterie, une bougie au sapin baumier, un bon vin rouge"),
    ("Marc", "ADULT", "Gagnon", "Des cannes à pêche, un sac de jour, des vinyles de jazz"),
    ("Steven", "ADULT", "Bouchard", "Un jeu de société, un abonnement au cinéma, un cours de menuiserie"),
    ("Carole", "ADULT", "Roy", "Une couple de chaussures de marche, un livre de recettes d’ici"),
    ("Florent", "CHILD", "Tremblay", "Un maillot du Canadien, des cartes Pokémon, des raquettes"),
    ("Magalie", "CHILD", "Gagnon", "Des patins, un livre de fantasy, le jeu de société Tabou"),
    ("Rosalie", "CHILD", "Bouchard", "Un appareil photo enfant, des feutres, des chaussettes à orignaux"),
    ("Charlie", "CHILD", "Roy", "Un football, un jeu Pokémon, des briques de construction"),
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
            "event_title": "Échange de cadeaux du temps des Fêtes — Tremblay & Gagnon",
            "budget_note": "25 $ par personne, taxes incluses — les cadeaux maison, c’est correct",
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
