"""Matching-engine invariants (spec section 4)."""

from collections import Counter

import pytest
from fastapi import HTTPException

import main
from main import generate_draw

UNSOLVABLE = (
    "Constraints are mathematically unsolvable with current family groups "
    "and draw counts."
)


def roster(adults=10, children=10, families=5):
    people, pid = [], 1
    for index in range(adults):
        people.append({"id": pid, "role": "ADULT", "family_group": f"Fam{index % families}"}); pid += 1
    for index in range(children):
        people.append({"id": pid, "role": "CHILD", "family_group": f"Fam{index % families}"}); pid += 1
    return people


def config(ada=0, adc=0, cda=0, cdc=0):
    return {
        "adult_draws_adult": ada,
        "adult_draws_child": adc,
        "child_draws_adult": cda,
        "child_draws_child": cdc,
    }


def by_id(people):
    return {p["id"]: p for p in people}


def assert_valid(pairs, people):
    people_by_id = by_id(people)
    for giver_id, receiver_id in pairs:
        giver, receiver = people_by_id[giver_id], people_by_id[receiver_id]
        assert giver_id != receiver_id, "self-draw"
        assert main.norm_group(giver["family_group"]) != main.norm_group(receiver["family_group"]), (
            "same family_group pairing"
        )
    assert len(set(pairs)) == len(pairs), "giver drew the same receiver twice"


def test_acceptance_scenario_big_family_roster():
    """Acceptance criterion 1: 30 rows, each child received exactly twice, no same-family pairs."""
    people = roster(adults=10, children=10, families=5)
    pairs = generate_draw(config(ada=1, adc=2, cda=0, cdc=0), people, seed=7)

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
    pairs = generate_draw(config(ada=1, adc=2, cda=0, cdc=0), people, seed=seed)
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
    pairs = generate_draw(quotas, people, seed=3)

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


def test_family_group_comparison_is_case_and_space_insensitive():
    people = [
        {"id": 1, "role": "ADULT", "family_group": " Martin "},
        {"id": 2, "role": "ADULT", "family_group": "martin"},
        {"id": 3, "role": "ADULT", "family_group": "Dubois"},
        {"id": 4, "role": "ADULT", "family_group": "Dubois"},
    ]
    pairs = generate_draw(config(ada=1), people, seed=1)
    assert len(pairs) == 4
    received = Counter(receiver for _, receiver in pairs)
    assert all(received[person["id"]] == 1 for person in people)
    assert_valid(pairs, people)


def test_deterministic_for_a_given_seed():
    people = roster(adults=6, children=4, families=3)
    quotas = config(ada=1, adc=1)
    assert generate_draw(quotas, people, seed=42) == generate_draw(quotas, people, seed=42)
    assert generate_draw(quotas, people, seed=1) != generate_draw(quotas, people, seed=2)


def test_zero_quotas_produce_no_assignments():
    assert generate_draw(config(), roster(adults=4, children=3), seed=1) == []


def test_empty_target_group_is_400():
    people = roster(adults=3, children=0, families=2)
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=1, adc=2), people, seed=1)
    assert caught.value.status_code == 400
    assert caught.value.detail == "Cannot assign gifts to an empty target group"


def test_unsolvable_family_grouping_is_422():
    """Everyone shares one household, so an adult can never draw an adult."""
    people = [
        {"id": 1, "role": "ADULT", "family_group": "Solo"},
        {"id": 2, "role": "ADULT", "family_group": "solo"},
        {"id": 3, "role": "CHILD", "family_group": "SOLO"},
    ]
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=1, adc=1), people, seed=1)
    assert caught.value.status_code == 422
    assert caught.value.detail == UNSOLVABLE


def test_unsolvable_quota_exceeds_eligible_receivers_is_422():
    people = roster(adults=3, children=2, families=2)
    with pytest.raises(HTTPException) as caught:
        generate_draw(config(ada=3), people, seed=1)
    assert caught.value.status_code == 422
    assert caught.value.detail == UNSOLVABLE


def test_tight_household_constraints_still_solve():
    """Two households of 3 adults each, each adult draws 2 adults: exactly one way out."""
    people = [
        *({"id": i, "role": "ADULT", "family_group": "A"} for i in (1, 2, 3)),
        *({"id": i, "role": "ADULT", "family_group": "B"} for i in (4, 5, 6)),
    ]
    for seed in range(6):
        pairs = generate_draw(config(ada=2), people, seed=seed)
        assert len(pairs) == 12
        assert_valid(pairs, people)
        received = Counter(receiver for _, receiver in pairs)
        assert all(received[person["id"]] == 2 for person in people)
