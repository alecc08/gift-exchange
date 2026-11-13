"""Matching-engine invariants (spec section 4) + the soft same-group rule."""

from collections import Counter

import pytest
from fastapi import HTTPException

import main
from main import generate_draw

UNSOLVABLE = (
    "The draw counts cannot be satisfied with this roster: somebody has more "
    "gifts to give than there are people allowed to receive them."
)


def roster(adults=10, children=10, families=5):
    """Households live in `group_key`; `family_group` is now only a display surname."""
    people, pid = [], 1
    for index in range(adults):
        people.append({"id": pid, "role": "ADULT", "family_group": f"Fam{index % families}",
                       "group_key": f"H{index % families}"}); pid += 1
    for index in range(children):
        people.append({"id": pid, "role": "CHILD", "family_group": f"Fam{index % families}",
                       "group_key": f"H{index % families}"}); pid += 1
    return people


def config(ada=0, adc=0, cda=0, cdc=0, avoid=1):
    return {
        "adult_draws_adult": ada,
        "adult_draws_child": adc,
        "child_draws_adult": cda,
        "child_draws_child": cdc,
        "avoid_same_group": avoid,
    }


def by_id(people):
    return {p["id"]: p for p in people}


def draw(people, quotas, seed=1):
    """Pairs only — the crossing count is asserted separately where it matters."""
    pairs, _crossed = generate_draw(quotas, people, seed=seed)
    return pairs


def crossings(pairs, people):
    group = {p["id"]: main.norm_group(p.get("group_key", "")) for p in people}
    return [pair for pair in pairs if group[pair[0]] and group[pair[0]] == group[pair[1]]]


def assert_valid(pairs, people, respect_groups=True):
    people_by_id = by_id(people)
    for giver_id, receiver_id in pairs:
        giver, receiver = people_by_id[giver_id], people_by_id[receiver_id]
        assert giver_id != receiver_id, "self-draw"
        giver_group = main.norm_group(giver["group_key"])
        # A blank group_key is "in no group": two blanks are not one household, so there is nothing
        # to compare. That is the whole point of the private key in group_key_of().
        if respect_groups and giver_group:
            assert giver_group != main.norm_group(receiver["group_key"]), (
                "same group pairing with the rule on"
            )
    assert len(set(pairs)) == len(pairs), "giver drew the same receiver twice"


def test_acceptance_scenario_big_family_roster():
    """Acceptance criterion 1: 30 rows, each child received exactly twice, no same-group pairs."""
    people = roster(adults=10, children=10, families=5)
    pairs, crossed = generate_draw(config(ada=1, adc=2, cda=0, cdc=0), people, seed=7)

    assert crossed == 0, "a roster this wide should not need any relaxation"
    assert len(pairs) == 30
    per_giver = Counter(giver for giver, _ in pairs)
    adults = [p for p in people if p["role"] == "ADULT"]
    children = [p for p in people if p["role"] == "CHILD"]
    assert all(per_giver[person["id"]] == 3 for person in adults)
    assert all(per_giver.get(person["id"], 0) == 0 for person in children)

    received = Counter(receiver for _, receiver in pairs)
    assert all(received[person["id"]] == 2 for person in children)
    assert all(received[person["id"]] == 1 for person in adults)
    assert_valid(pairs, people)


@pytest.mark.parametrize("seed", range(12))
def test_acceptance_scenario_holds_for_every_seed(seed):
    people = roster(adults=10, children=10, families=5)
    pairs = draw(people, config(ada=1, adc=2, cda=0, cdc=0), seed=seed)
    assert len(pairs) == 30
    received = Counter(receiver for _, receiver in pairs)
    assert set(received.values()) == {2, 1}  # children twice, adults once
    assert_valid(pairs, people)


@pytest.mark.parametrize(
    "quotas",
    [
        config(ada=1, adc=1),
        config(ada=2, adc=1, cda=1),
        config(ada=1, adc=2, cda=1, cdc=1),
        config(ada=3, adc=2, cda=2, cdc=1),
    ],
)
def test_balanced_receiver_quotas_and_constraints(quotas):
    people = roster(adults=8, children=6, families=4)
    pairs = draw(people, quotas, seed=3)

    adults = [p for p in people if p["role"] == "ADULT"]
    children = [p for p in people if p["role"] == "CHILD"]
    t_adult = len(adults) * quotas["adult_draws_adult"] + len(children) * quotas["child_draws_adult"]
    t_child = len(adults) * quotas["adult_draws_child"] + len(children) * quotas["child_draws_child"]
    assert len(pairs) == t_adult + t_child

    received = Counter(receiver for _, receiver in pairs)
    for group, total, members in (("ADULT", t_adult, adults), ("CHILD", t_child, children)):
        if not members or total == 0:
            for person in members:
                assert received.get(person["id"], 0) == 0
            continue
        base, remainder = divmod(total, len(members))
        counts = sorted(received.get(person["id"], 0) for person in members)
        assert counts[0] >= base and counts[-1] <= base + (1 if remainder else 0)
        assert sum(counts) == total

    quota_by_role = {
        "ADULT": (quotas["adult_draws_adult"], quotas["adult_draws_child"]),
        "CHILD": (quotas["child_draws_adult"], quotas["child_draws_child"]),
    }
    per_giver = Counter(giver for giver, _ in pairs)
    for person in people:
        expected = sum(quota_by_role[person["role"]])
        assert per_giver.get(person["id"], 0) == expected
    assert_valid(pairs, people)


def test_group_comparison_is_case_and_space_insensitive():
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Martin", "group_key": " Roux "},
        {"id": 2, "role": "ADULT", "family_group": "Roux", "group_key": "roux"},
        {"id": 3, "role": "ADULT", "family_group": "Dubois", "group_key": "Dubois"},
        {"id": 4, "role": "ADULT", "family_group": "Dubois", "group_key": "dubois"},
    ]
    pairs = draw(people, config(ada=1), seed=1)
    assert len(pairs) == 4
    received = Counter(receiver for _, receiver in pairs)
    assert all(received[person["id"]] == 1 for person in people)
    assert_valid(pairs, people)


def test_shared_surname_does_not_group_people():
    """The reason `group_key` exists: two households can carry the same family name.

    Both surnames are Tremblay, the households are not. Under the old rule these four could never
    have crossed at all; now the two households each hold two people, which is satisfiable.
    """
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Tremblay", "group_key": "Tremblay-A"},
        {"id": 2, "role": "ADULT", "family_group": "Tremblay", "group_key": "Tremblay-A"},
        {"id": 3, "role": "ADULT", "family_group": "Tremblay", "group_key": "Tremblay-B"},
        {"id": 4, "role": "ADULT", "family_group": "Tremblay", "group_key": "Tremblay-B"},
    ]
    pairs, crossed = generate_draw(config(ada=1), people, seed=5)
    assert crossed == 0
    assert len(pairs) == 4
    assert_valid(pairs, people)


def test_no_group_key_means_no_group():
    """Blank group_key is "in no group" — and two blanks are not one group."""
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Tremblay", "group_key": ""},
        {"id": 2, "role": "ADULT", "family_group": "Tremblay", "group_key": ""},
    ]
    pairs, crossed = generate_draw(config(ada=1), people, seed=2)
    assert len(pairs) == 2
    assert crossed == 0
    assert sorted(pairs) == [(1, 2), (2, 1)]
    assert_valid(pairs, people)


def test_impossible_without_a_crossing_relaxes_once_instead_of_failing():
    """Two in one household, one alone, everyone draws one adult: one crossing is unavoidable.

    The old engine called this unsolvable. Now it draws, and the relaxation is exactly the one the
    arithmetic forces — no more.
    """
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Roy", "group_key": "Roy"},
        {"id": 2, "role": "ADULT", "family_group": "Roy", "group_key": "Roy"},
        {"id": 3, "role": "ADULT", "family_group": "Nolin", "group_key": "Nolin"},
    ]
    for seed in range(6):
        pairs, crossed = generate_draw(config(ada=1), people, seed=seed)
        assert len(pairs) == 3
        assert crossed == 1, "exactly one gift may cross"
        assert len(crossings(pairs, people)) == 1
        assert all(giver != receiver for giver, receiver in pairs)
        received = Counter(receiver for _, receiver in pairs)
        assert all(received[person["id"]] == 1 for person in people)


def test_everyone_in_one_household_still_draws():
    """The extreme case the old engine refused outright: nobody is outside the group."""
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Solo", "group_key": "Solo"},
        {"id": 2, "role": "ADULT", "family_group": "solo", "group_key": "SOLO"},
        {"id": 3, "role": "ADULT", "family_group": "SOLO", "group_key": " solo "},
    ]
    pairs, crossed = generate_draw(config(ada=1), people, seed=4)
    assert len(pairs) == 3
    assert crossed == 3 == len(crossings(pairs, people))


def test_preference_off_ignores_groups_but_never_deals_somebody_to_themselves():
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Solo", "group_key": "Solo"},
        {"id": 2, "role": "ADULT", "family_group": "Solo", "group_key": "SOLO"},
    ]
    for seed in range(8):
        pairs, crossed = generate_draw(config(ada=1, avoid=0), people, seed=seed)
        assert sorted(pairs) == [(1, 2), (2, 1)]
        assert crossed == 0


def test_mixed_households_stay_separate_from_names():
    """A CHILD in one household and an ADULT in another, both named Tremblay: no constraint between
    them, and the two real households are still respected."""
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Tremblay", "group_key": "A"},
        {"id": 2, "role": "CHILD", "family_group": "Tremblay", "group_key": "B"},
        {"id": 3, "role": "ADULT", "family_group": "Roy", "group_key": "B"},
        {"id": 4, "role": "CHILD", "family_group": "Roy", "group_key": "A"},
    ]
    pairs, crossed = generate_draw(config(ada=1, adc=1, cda=0, cdc=0), people, seed=11)
    assert crossed == 0
    assert_valid(pairs, people)
    group = {p["id"]: p["group_key"] for p in people}
    for giver, receiver in pairs:
        assert group[giver] != group[receiver]


def test_deterministic_for_a_given_seed():
    people = roster(adults=6, children=4, families=3)
    quotas = config(ada=1, adc=1)
    assert generate_draw(quotas, people, seed=42) == generate_draw(quotas, people, seed=42)
    assert generate_draw(quotas, people, seed=1) != generate_draw(quotas, people, seed=2)


def test_zero_quotas_produce_no_assignments():
    assert generate_draw(config(), roster(adults=4, children=3), seed=1) == ([], 0)


def test_empty_target_group_is_400():
    people = roster(adults=3, children=0, families=2)
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=1, adc=2), people, seed=1)
    assert caught.value.status_code == 400
    assert caught.value.detail == "Cannot assign gifts to an empty target group"


def test_unsolvable_quota_exceeds_available_receivers_is_422():
    """The only hard failure left: 3 adults, each owing 3 adult gifts, 2 possible receivers."""
    people = roster(adults=3, children=2, families=2)
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=3), people, seed=1)
    assert caught.value.status_code == 422
    assert caught.value.detail == UNSOLVABLE


def test_one_person_cannot_be_their_own_receiver_even_with_groups_off():
    people = [{"id": 1, "role": "ADULT", "family_group": "Solo", "group_key": "Solo"}]
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=1, avoid=0), people, seed=1)
    assert caught.value.status_code == 422


def test_tight_household_constraints_still_solve():
    """Two households of 3 adults each, each adult draws 2 adults: exactly one way out."""
    people = [
        *({"id": i, "role": "ADULT", "family_group": "X", "group_key": "A"} for i in (1, 2, 3)),
        *({"id": i, "role": "ADULT", "family_group": "X", "group_key": "B"} for i in (4, 5, 6)),
    ]
    for seed in range(6):
        pairs, crossed = generate_draw(config(ada=2), people, seed=seed)
        assert crossed == 0
        assert len(pairs) == 12
        assert_valid(pairs, people)
        received = Counter(receiver for _, receiver in pairs)
        assert all(received[person["id"]] == 2 for person in people)


def test_min_crossings_bound_is_never_overstated():
    """The lower bound must be a real bound: a draw that starts at k always uses exactly k."""
    rosters = [
        # 2 in one household, 1 alone
        [{"id": 1, "role": "ADULT", "group_key": "A"}, {"id": 2, "role": "ADULT", "group_key": "A"},
         {"id": 3, "role": "ADULT", "group_key": "B"}],
        # 1 in each of three households
        [{"id": 1, "role": "ADULT", "group_key": "A"}, {"id": 2, "role": "ADULT", "group_key": "B"},
         {"id": 3, "role": "ADULT", "group_key": "C"}],
        # everyone together
        [{"id": 1, "role": "ADULT", "group_key": "A"}, {"id": 2, "role": "ADULT", "group_key": "A"}],
    ]
    for people in rosters:
        for person in people:
            person.setdefault("family_group", "X")
        quota = {"ADULT": (1, 0), "CHILD": (0, 0)}
        group_of = {p["id"]: main.group_key_of(p, True) for p in people}
        lower = main._min_crossings(people, quota, people, [], group_of, len(people), 0)
        _pairs, crossed = generate_draw(config(ada=1), people, seed=3)
        assert crossed >= lower, (people, lower, crossed)


@pytest.mark.parametrize("seed", range(8))
def test_every_child_is_picked_exactly_twice_in_a_three_household_event(seed):
    """The shape people actually run: 3 households of 2 adults + 2 children, each adult drawing
    1 adult + 2 children.

    The even distribution is arithmetic, not luck: the receiver pool is built with
    `divmod(12, 6)` before anybody is paired, so 12 child-gifts land as exactly 2 per child with
    nothing left over. The group rule cannot tilt it, because the pool is fixed before the deal —
    bending the rule (see the lopsided-roster cases below) costs crossings, never fairness.
    """
    people = roster(adults=6, children=6, families=3)
    pairs, crossed = generate_draw(config(ada=1, adc=2), people, seed=seed)

    assert len(pairs) == 18
    received = Counter(receiver for _giver, receiver in pairs)
    assert sorted(received[p["id"]] for p in people if p["role"] == "CHILD") == [2] * 6
    assert sorted(received[p["id"]] for p in people if p["role"] == "ADULT") == [1] * 6

    # four eligible children per adult, two needed: this roster never needs the rule to bend
    assert crossed == 0
    assert not crossings(pairs, people)

    # and a child's two gifts never come from the same person
    for giver in {g for g, _r in pairs}:
        given = [r for g, r in pairs if g == giver]
        assert len(given) == len(set(given)), (giver, given)
    assert_valid(pairs, people)


def test_when_the_totals_do_not_divide_the_spread_is_one_gift_not_three():
    """Fairness has a floor, and it is worth stating exactly what it is.

    6 adults each giving 2 child-gifts is 12 gifts over 7 children: `divmod(12, 7)` = 1 each with
    5 left over, so five children receive 2 and two receive 1. That is the worst case the engine
    can produce — it never hands one child 3 and another 1.
    """
    people = roster(adults=6, children=7, families=3)
    counts = set()
    for seed in range(6):
        pairs, _crossed = generate_draw(config(ada=1, adc=2), people, seed=seed)
        received = Counter(receiver for _giver, receiver in pairs)
        child_counts = sorted(received[p["id"]] for p in people if p["role"] == "CHILD")
        assert child_counts == [1, 1, 2, 2, 2, 2, 2], child_counts
        counts.add(tuple(child_counts))
        assert_valid(pairs, people)
    assert max(c[-1] - c[0] for c in counts) == 1


def test_a_household_big_enough_to_force_crossings_still_distributes_evenly():
    """4 adults + 4 children in one household, 1 adult + 1 child in two others.

    There the group rule cannot be honoured (6 gifts have to cross), which is exactly the case the
    soft rule exists for — and the thing to check is that bending the rule does not bend the
    distribution: every child still receives exactly 2, every adult exactly 1.
    """
    people, pid = [], 1
    for group, adults, children in (("H1", 4, 4), ("H2", 1, 1), ("H3", 1, 1)):
        for _ in range(adults):
            people.append({"id": pid, "role": "ADULT", "family_group": group, "group_key": group}); pid += 1
        for _ in range(children):
            people.append({"id": pid, "role": "CHILD", "family_group": group, "group_key": group}); pid += 1

    for seed in range(6):
        pairs, crossed = generate_draw(config(ada=1, adc=2), people, seed=seed)
        received = Counter(receiver for _giver, receiver in pairs)
        assert sorted(received[p["id"]] for p in people if p["role"] == "CHILD") == [2] * 6
        assert sorted(received[p["id"]] for p in people if p["role"] == "ADULT") == [1] * 6
        assert crossed == len(crossings(pairs, people)) > 0
        # the rule deliberately bent here, so assert_valid must not demand it held
        assert_valid(pairs, people, respect_groups=False)
