"""The lower bound in `_min_crossings()` must be a *bound*, and the solver must not overshoot it.

Brute force is the only honest referee here: for a roster small enough to enumerate, every perfect
deal of the receiver pool is a permutation, so the true minimum number of same-group gifts is just
the best one found. If the bound were ever larger than that, the budget ladder would start above the
answer and the console would promise a crossing that was never needed.
"""

from itertools import permutations

import pytest

import main
from main import generate_draw, group_key_of

from test_engine import config


def people(spec):
    """spec: list of (role, group) -> participant dicts with stable ids."""
    return [{"id": index + 1, "role": role, "family_group": family, "group_key": family}
            for index, (role, family) in enumerate(spec)]


def brute_force_min_crossings(participants, quota_by_role, respect_groups=True):
    """Smallest number of same-group gifts over every way of dealing one receiver per giver.

    Only valid for the one-gift-each case this is used with: each giver takes a distinct receiver,
    i.e. a derangement-like permutation of the roster.
    """
    group = {p["id"]: group_key_of(p, respect_groups) for p in participants}
    ids = [p["id"] for p in participants]
    best = None
    for perm in permutations(ids):
        crossings = 0
        valid = True
        for giver_id, receiver_id in zip(ids, perm):
            if giver_id == receiver_id:
                valid = False
                break
            if group[giver_id] == group[receiver_id]:
                crossings += 1
        if valid and (best is None or crossings < best):
            best = crossings
        if best == 0:
            break
    return best


CASES = [
    # (label, [(role, group), ...])
    ("two in one household, one alone", [("ADULT", "Roy"), ("ADULT", "Roy"), ("ADULT", "Nolin")]),
    ("three separate households", [("ADULT", "A"), ("ADULT", "B"), ("ADULT", "C")]),
    ("everyone together", [("ADULT", "A"), ("ADULT", "A")]),
    ("two households of two", [("ADULT", "A"), ("ADULT", "A"), ("ADULT", "B"), ("ADULT", "B")]),
    ("three against one", [("ADULT", "A"), ("ADULT", "A"), ("ADULT", "A"), ("ADULT", "B")]),
    ("nobody grouped", [("ADULT", ""), ("ADULT", ""), ("ADULT", "")]),
]


@pytest.mark.parametrize("label,spec", CASES, ids=[label for label, _ in CASES])
def test_bound_never_exceeds_the_true_minimum(label, spec):
    roster = people(spec)
    quota = {"ADULT": (1, 0), "CHILD": (0, 0)}
    group_of = {p["id"]: group_key_of(p, True) for p in roster}
    lower = main._min_crossings(roster, quota, roster, [], group_of, len(roster), 0)

    expected = brute_force_min_crossings(roster, quota)
    assert lower <= expected, f"{label}: bound {lower} exceeds the real minimum {expected}"

    for seed in range(6):
        _pairs, crossed = generate_draw(config(ada=1), roster, seed=seed)
        assert crossed >= lower, f"{label}: solver used {crossed} below its own bound {lower}"
        assert crossed <= expected, f"{label}: solver used {crossed}, worse than the best {expected}"


def test_the_bound_is_tight_where_it_matters():
    """Two in one household and one alone: the arithmetic forces exactly one crossing, and the
    engine spends exactly one — for every seed."""
    roster = people([("ADULT", "Roy"), ("ADULT", "Roy"), ("ADULT", "Nolin")])
    quota = {"ADULT": (1, 0), "CHILD": (0, 0)}
    group_of = {p["id"]: group_key_of(p, True) for p in roster}
    lower = main._min_crossings(roster, quota, roster, [], group_of, 3, 0)
    assert lower == 1 == brute_force_min_crossings(roster, quota)
    assert {generate_draw(config(ada=1), roster, seed=seed)[1] for seed in range(8)} == {1}
