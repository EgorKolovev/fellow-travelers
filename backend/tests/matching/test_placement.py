from itertools import permutations
from uuid import UUID

import pytest

from app.matching import (
    JoinTransfer,
    MatchCandidate,
    NewGroup,
    OpenTransfer,
    find_placement,
    members_to_exclude,
)
from tests.matching.helpers import OTHER_AIRPORT, cand, open_transfer


def _uid(n: int) -> UUID:
    return UUID(int=n)


def _new_group_ids(result: JoinTransfer | NewGroup | None) -> set[UUID]:
    assert isinstance(result, NewGroup)
    return {c.id for c in result.members}


# ---------- find_placement: какой вариант сильнее ----------


def test_placement_nothing_to_offer() -> None:
    assert find_placement(cand("10:00"), [], []) is None


def test_placement_new_triple_beats_join() -> None:
    new = cand("10:00", wait=30)
    a, b = cand("10:00", wait=30), cand("10:00", wait=30)
    pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    # join: 3 пассажира, 1 заявка; новая тройка: 3 пассажира, 3 заявки
    result = find_placement(new, [pair], [a, b])
    assert _new_group_ids(result) == {new.id, a.id, b.id}


def test_placement_join_beats_new_pair_of_singles() -> None:
    new = cand("10:00", wait=30)
    pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    # join: 3 пассажира; новая пара 1+1: 2 пассажира, хотя разброс у неё нулевой
    result = find_placement(new, [pair], [cand("10:00", wait=30)])
    assert result == JoinTransfer(pair.id)


def test_placement_new_pair_with_companion_beats_join() -> None:
    new = cand("10:00", wait=30)
    companion = cand("10:00", wait=30, pax=2)
    pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    # join: 3 пассажира, 1 заявка; новая пара «спутник + один»: 3 пассажира, 2 заявки
    result = find_placement(new, [pair], [companion])
    assert _new_group_ids(result) == {new.id, companion.id}


def test_placement_new_with_companion_does_not_join_pair() -> None:
    new = cand("10:00", wait=30, pax=2)
    pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    assert find_placement(new, [pair], []) is None


def test_placement_new_pair_when_no_open_transfers() -> None:
    new = cand("10:00", wait=30)
    other = cand("10:10", wait=30)
    assert _new_group_ids(find_placement(new, [], [other])) == {new.id, other.id}


@pytest.mark.parametrize(
    ("new", "members"),
    [
        pytest.param(
            cand("10:00", bag=1),
            (cand("10:00", bag=1), cand("10:00", bag=1)),
            id="baggage-overload",
        ),
        pytest.param(
            cand("10:30", wait=0),
            (cand("10:00", wait=0), cand("10:00", wait=0)),
            id="members-wait-too-little",
        ),
        pytest.param(
            cand("10:00", wait=5),
            (cand("10:10", wait=30), cand("10:10", wait=30)),
            id="new-waits-too-little",
        ),
        pytest.param(
            cand("10:00", wait=30, airport=OTHER_AIRPORT),
            (cand("10:00", wait=30), cand("10:00", wait=30)),
            id="different-airport",
        ),
        pytest.param(
            cand("10:00", wait=30),
            (cand("10:00", wait=30), cand("10:00", wait=30), cand("10:00", wait=30)),
            id="car-is-full",
        ),
        pytest.param(
            cand("10:00", wait=30, pax=2),
            (cand("10:00", wait=30), cand("10:00", wait=30)),
            id="companion-capacity",
        ),
    ],
)
def test_placement_join_rejected(new: MatchCandidate, members: tuple[MatchCandidate, ...]) -> None:
    assert find_placement(new, [open_transfer(*members)], []) is None


def test_placement_join_skipped_when_new_is_member() -> None:
    new = cand("10:00", wait=30)
    mine = open_transfer(new, cand("10:00", wait=30))
    assert find_placement(new, [mine], []) is None


def test_placement_ignores_new_in_free() -> None:
    new = cand("10:00", wait=30)
    assert find_placement(new, [], [new]) is None


def test_placement_falls_back_to_group_when_join_rejected() -> None:
    new = cand("10:00", wait=30, bag=1)
    full_bags = open_transfer(cand("10:00", wait=30, bag=1), cand("10:00", wait=30, bag=1))
    other = cand("10:10", wait=30)
    result = find_placement(new, [full_bags], [other])
    assert _new_group_ids(result) == {new.id, other.id}


def test_placement_accepts_one_shot_iterables() -> None:
    new = cand("10:00", wait=30)
    pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    result = find_placement(new, iter([pair]), iter([]))
    assert result == JoinTransfer(pair.id)


def test_placement_prefers_smaller_spread_between_joins() -> None:
    new = cand("10:00", wait=30)
    tight = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    loose = open_transfer(cand("10:00", wait=30), cand("10:20", wait=30))
    assert find_placement(new, [loose, tight], []) == JoinTransfer(tight.id)
    assert find_placement(new, [tight, loose], []) == JoinTransfer(tight.id)


def test_placement_prefers_smaller_total_wait_at_equal_spread() -> None:
    new = cand("10:10", wait=30)
    # разброс везде 10 минут; ожидания: 10+10+0 против 10+0+0
    early_pair = open_transfer(cand("10:00", wait=30), cand("10:00", wait=30))
    late_pair = open_transfer(cand("10:00", wait=30), cand("10:10", wait=30))
    assert find_placement(new, [early_pair, late_pair], []) == JoinTransfer(late_pair.id)
    assert find_placement(new, [late_pair, early_pair], []) == JoinTransfer(late_pair.id)


def test_placement_equal_joins_resolved_by_sorted_ids() -> None:
    new = cand("10:00", wait=30, id=_uid(1000))
    low = open_transfer(
        cand("10:00", wait=30, id=_uid(10)), cand("10:00", wait=30, id=_uid(11)), id=_uid(1)
    )
    high = open_transfer(
        cand("10:00", wait=30, id=_uid(20)), cand("10:00", wait=30, id=_uid(21)), id=_uid(2)
    )
    for order in permutations([high, low]):
        assert find_placement(new, order, []) == JoinTransfer(low.id)


def test_placement_deterministic_under_shuffle() -> None:
    new = cand("10:00", wait=30, id=_uid(1000))
    transfers = [
        OpenTransfer(
            _uid(i),
            (
                cand("10:00", wait=30, id=_uid(10 * i)),
                cand("10:00", wait=30, id=_uid(10 * i + 1)),
            ),
        )
        for i in range(1, 4)
    ]
    free = [cand("10:05", wait=30, id=_uid(100 + i)) for i in range(5)]
    results = {
        find_placement(new, list(t), list(f))
        for t in permutations(transfers)
        for f in [free, free[::-1]]
    }
    assert len(results) == 1
    assert None not in results


# ---------- members_to_exclude ----------


def test_exclude_nothing_for_empty_waiting() -> None:
    assert members_to_exclude([], frozenset()) == ()


def test_exclude_nothing_for_single() -> None:
    assert members_to_exclude([cand("10:00", pax=2, bag=2)], frozenset()) == ()


def test_exclude_nothing_when_compatible() -> None:
    a, b = cand("10:00", wait=15), cand("10:10", wait=0)
    assert members_to_exclude([a, b], frozenset({a.id, b.id})) == ()


@pytest.mark.parametrize("changed_ids", [(), (1, 2), (3,), (1, 2, 3)], ids=str)
def test_exclude_third_on_other_flight_even_if_pair_changed(changed_ids: tuple[int, ...]) -> None:
    # двое на одном рейсе, третий позже: остаётся пара, она совместима и больше
    pair = (cand("10:00", id=_uid(1)), cand("10:00", id=_uid(2)))
    third = cand("11:00", id=_uid(3))
    changed = frozenset(_uid(i) for i in changed_ids)
    assert members_to_exclude([*pair, third], changed) == (third.id,)


def test_exclude_prefers_changed_member_among_equal_options() -> None:
    # багаж 3 > 2: годится любая пара, вопрос только в том, кого убрать
    group = [cand("10:00", bag=1, id=_uid(i)) for i in (1, 2, 3)]
    assert members_to_exclude(group, frozenset({_uid(2)})) == (_uid(2),)
    assert members_to_exclude(group, frozenset({_uid(3)})) == (_uid(3),)


@pytest.mark.parametrize("changed_ids", [(), (1, 2, 3), (4,)], ids=str)
def test_exclude_tie_resolved_by_arrival_then_id(changed_ids: tuple[int, ...]) -> None:
    group = [cand("10:00", bag=1, id=_uid(i)) for i in (3, 1, 2)]
    changed = frozenset(_uid(i) for i in changed_ids)
    assert members_to_exclude(group, changed) == (_uid(1),)


def test_exclude_keeps_only_one_when_all_pairs_incompatible() -> None:
    a = cand("10:00", id=_uid(1))
    b = cand("10:10", id=_uid(2))
    c = cand("10:20", id=_uid(3))
    # исключено должно быть двое; чем меньше среди них неизменившихся, тем лучше
    assert members_to_exclude([a, b, c], frozenset({a.id, b.id})) == (a.id, b.id)
    assert members_to_exclude([a, b, c], frozenset({a.id, c.id})) == (a.id, c.id)
    assert members_to_exclude([a, b, c], frozenset({b.id, c.id})) == (b.id, c.id)
    # без различий решает порядок исключённых: остаётся прилетающий позже всех
    assert members_to_exclude([a, b, c], frozenset()) == (a.id, b.id)


def test_exclude_result_sorted_by_arrival_not_id() -> None:
    a = cand("10:00", id=_uid(3))
    b = cand("10:10", id=_uid(2))
    c = cand("10:20", id=_uid(1))
    assert members_to_exclude([c, a, b], frozenset({a.id, b.id})) == (a.id, b.id)


def test_exclude_companion_pair_over_capacity_drops_one() -> None:
    a = cand("10:00", pax=2, id=_uid(1))
    b = cand("10:00", pax=2, id=_uid(2))
    assert members_to_exclude([a, b], frozenset({b.id})) == (b.id,)
    assert members_to_exclude([a, b], frozenset({a.id})) == (a.id,)


def test_exclude_deterministic_under_shuffle() -> None:
    group = [cand("10:00", bag=1, id=_uid(i)) for i in (1, 2, 3)]
    results = {members_to_exclude(list(p), frozenset()) for p in permutations(group)}
    assert results == {(_uid(1),)}
