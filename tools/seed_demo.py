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

# name, family name, group (household), role, wishlist, email. Two households carry the same family
# name — that is the situation the group field exists for, and the screenshots should show it.
# One mailbox per household is the whole email design (one mail per boîte aux lettres), so the demo
# has to carry addresses or the console's mail panel and the gallery say nothing. Children are listed
# without one: their link travels in their household's mail. `example.com` is the RFC 2606 reserved
# domain, so a demo left configured can never mail a real person by accident.
ROSTER = [
    ("Mario", "Tremblay", "Tremblay (chez Gaétan)", "ADULT",
     "Une tuque en laine, un bon bourbon, des gants de vélo", "chez.gaetan@example.com"),
    ("Julie", "Tremblay", "Tremblay (chez Gaétan)", "ADULT",
     "Des plantes vivaces, une couple de casseroles en fonte, un foulard", "chez.gaetan@example.com"),
    ("Florent", "Tremblay", "Tremblay (chez Gaétan)", "CHILD",
     "Un maillot du Canadien, des cartes Pokémon, des raquettes", ""),
    ("Lise", "Tremblay", "Tremblay (chez Réjean)", "ADULT",
     "Un cours de poterie, une bougie au sapin baumier, un bon vin rouge", "chez.rejean@example.com"),
    ("Marc", "Tremblay", "Tremblay (chez Réjean)", "ADULT",
     "Des cannes à pêche, un sac de jour, des vinyles de jazz", "chez.rejean@example.com"),
    ("Steven", "Gagnon", "Gagnon", "ADULT",
     "Un jeu de société, un abonnement au cinéma, un cours de menuiserie", "gagnon@example.com"),
    ("Carole", "Gagnon", "Gagnon", "ADULT",
     "Une couple de chaussures de marche, un livre de recettes d’ici", "gagnon@example.com"),
    ("Magalie", "Gagnon", "Gagnon", "CHILD",
     "Des patins, un livre de fantasy, le jeu de société Tabou", ""),
    ("Rosalie", "Roy", "Roy", "CHILD",
     "Un appareil photo enfant, des feutres, des chaussettes à orignaux", "famille.roy@example.com"),
    ("Charlie", "Roy", "Roy", "CHILD",
     "Un football, un jeu Pokémon, des briques de construction", "famille.roy@example.com"),
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


def login():
    _, response = call("/api/admin/login", "POST", {"password": PASSWORD})
    return {"Authorization": f"Bearer {response['token']}"}


def seed(auth):
    """Wipe and rebuild the demo roster (draw left open). Used by capture_docs_media.py too, so the
    gallery and the demo can never drift apart."""
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
            "avoid_same_group": True,
        },
        auth,
    )

    for name, family, group, role, wishlist, email in ROSTER:
        call(
            "/api/admin/participants",
            "POST",
            {
                "name": name,
                "role": role,
                "family_group": family,
                "group_key": group,
                "wishlist": wishlist,
                "email": email,
            },
            auth,
        )
    return call("/api/admin/state", headers=auth)[1]


def main():
    parser = argparse.ArgumentParser(description="Seed a demo roster into a running instance.")
    parser.add_argument("--draw", action="store_true", help="generate and lock the draw too")
    args = parser.parse_args()

    auth = login()
    state = seed(auth)
    print(f"seeded {len(state['participants'])} participants at {BASE}")
    for person in state["participants"]:
        print(f"  {person['name']:<9} {person['group_key']:<24} {BASE}/?t={person['token']}")

    if args.draw:
        status, body = call("/api/admin/draw", "POST", {}, auth)
        print("draw:", status, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
