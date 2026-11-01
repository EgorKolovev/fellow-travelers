from datetime import UTC, datetime, timedelta, timezone
from itertools import permutations
from uuid import UUID, uuid4

import pytest

from app.matching import MatchCandidate, find_group, is_compatible
from tests.matching.helpers import OTHER_AIRPORT, cand

MSK = timezone(timedelta(hours=3))
NSK = timezone(timedelta(hours=7))


def _same_id_pair() -> list[MatchCandidate]:
    a = cand("10:00", wait=15)
    return [a, cand("10:00", wait=15, id=a.id)]


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        pytest.param([cand("10:00", wait=15), cand("10:10", wait=0)], True, id="example-a-b"),
        pytest.param([cand("10:00", wait=0), cand("10:10", wait=15)], False, id="example-reversed"),
        pytest.param(
            [cand("10:00", wait=20), cand("10:05", wait=15), cand("10:20", wait=0)],
            True,
            id="triple-all-within-wait",
        ),
        pytest.param(
            [cand("10:00", wait=19), cand("10:05", wait=15), cand("10:20", wait=0)],
            False,
            id="triple-one-short",
        ),
        pytest.param([cand("10:00"), cand("10:00"), cand("10:00")], True, id="three-pax-ok"),
        pytest.param(
            [cand("10:00"), cand("10:00"), cand("10:00"), cand("10:00")], False, id="four-pax"
        ),
        pytest.param([cand("10:00", pax=2), cand("10:00")], True, id="companion-plus-one"),
        pytest.param(
            [cand("10:00", pax=2), cand("10:00", pax=2)], False, id="companion-plus-companion"
        ),
        pytest.param([cand("10:00", bag=1), cand("10:00", bag=1)], True, id="baggage-2"),
        pytest.param([cand("10:00", bag=2), cand("10:00", bag=1)], False, id="baggage-3"),
        pytest.param(
            [cand("10:00"), cand("10:00", airport=OTHER_AIRPORT)], False, id="different-airports"
        ),
        pytest.param(
            [cand("23:50", wait=30, day=1), cand("00:10", wait=0, day=2)],
            True,
            id="across-midnight",
        ),
        pytest.param(
            [cand("23:50", wait=10, day=1), cand("00:10", wait=0, day=2)],
            False,
            id="across-midnight-too-long",
        ),
        pytest.param(
            [cand("13:00", wait=0, tz=MSK), cand("16:00", wait=0, tz=NSK)],
            False,
            id="timezones-different-instants",
        ),
        pytest.param(
            [cand("13:00", wait=0, tz=MSK), cand("17:00", wait=0, tz=NSK)],
            True,
            id="timezones-same-instant",
        ),
        pytest.param(
            [cand("10:00", wait=1440, day=1), cand("09:00", wait=0, day=2)],
            True,
            id="day-wait-23h-later",
        ),
        pytest.param(
            [cand("10:00", wait=1440, day=1), cand("10:00", wait=0, day=2)],
            True,
            id="day-wait-exactly-1440",
        ),
        pytest.param(
            [cand("10:00", wait=1440, day=1), cand("10:01", wait=0, day=2)],
            False,
            id="day-wait-exceeded",
        ),
        pytest.param(
            [cand("10:00", wait=45), cand("10:45", wait=0)],
            True,
            id="non-legacy-wait-45",
        ),
        pytest.param(
            [cand("10:00", wait=45), cand("10:46", wait=0)],
            False,
            id="non-legacy-wait-45-exceeded",
        ),
        pytest.param(_same_id_pair(), False, id="duplicate-ids"),
        pytest.param([], False, id="empty"),
        pytest.param([cand("10:00", pax=2, bag=2)], True, id="single-ok"),
    ],
)
def test_is_compatible(group: list[MatchCandidate], expected: bool) -> None:
    assert is_compatible(group) is expected


def test_find_group_no_candidates() -> None:
    assert find_group(cand("10:00"), []) is None


def test_find_group_single_with_companion_is_not_transfer() -> None:
    assert find_group(cand("10:00", pax=2), []) is None


def test_find_group_prefers_companion_over_smaller_spread() -> None:
    new = cand("10:00", wait=30)
    single = cand("10:00", wait=30)
    companion = cand("10:20", wait=30, pax=2)
    result = find_group(new, [single, companion])
    assert result is not None
    assert {c.id for c in result} == {new.id, companion.id}


def test_find_group_prefers_three_over_two_passengers() -> None:
    new = cand("10:00", wait=30)
    a = cand("10:05", wait=30)
    far = cand("10:00", wait=30)
    # (new, far) — 2 пассажира с нулевым разбросом, (new, a, far) — 3 пассажира
    result = find_group(new, [a, far])
    assert result is not None
    assert len(result) == 3


def test_find_group_smaller_spread_between_equal_passenger_pairs() -> None:
    new = cand("10:00", wait=30, pax=2)
    far = cand("10:25", wait=30)
    near = cand("10:05", wait=30)
    result = find_group(new, [far, near])
    assert result is not None
    assert {c.id for c in result} == {new.id, near.id}


def test_find_group_sorted_by_arrival_then_id() -> None:
    new = cand("10:10", wait=0)
    early = cand("10:00", wait=15)
    assert find_group(new, [early]) == (early, new)


def test_find_group_deterministic_under_shuffle() -> None:
    new = cand("10:00", wait=30)
    others = [
        cand("10:10", wait=30, id=UUID(int=10)),
        cand("10:10", wait=30, id=UUID(int=11)),
        cand("10:10", wait=30, id=UUID(int=12)),
        cand("10:10", wait=30, pax=2, id=UUID(int=13)),
    ]
    results = {find_group(new, list(p)) for p in permutations(others)}
    assert len(results) == 1
    assert None not in results


def test_find_group_ignores_new_in_candidates() -> None:
    new = cand("10:00", wait=30)
    assert find_group(new, [new]) is None
    other = cand("10:05", wait=0)
    result = find_group(new, [new, other])
    assert result is not None
    assert {c.id for c in result} == {new.id, other.id}


def test_find_group_skips_incompatible() -> None:
    new = cand("10:00", wait=30, bag=2)
    wrong_airport = cand("10:00", wait=30, airport=OTHER_AIRPORT)
    too_much_baggage = cand("10:00", wait=30, bag=1)
    too_late = cand("11:00", wait=30)
    assert find_group(new, [wrong_airport, too_much_baggage, too_late]) is None


def test_find_group_works_with_mixed_timezones() -> None:
    new = cand("13:00", wait=0, tz=MSK)
    other = cand("17:00", wait=0, tz=NSK)
    result = find_group(new, [other])
    assert result is not None
    assert len(result) == 2


def test_candidate_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MatchCandidate(
            id=uuid4(),
            arrival_location_id=uuid4(),
            arrival_at=datetime(2026, 7, 1, 10, 0),
            passengers=1,
            baggage=0,
            max_wait=timedelta(0),
        )


@pytest.mark.parametrize(
    ("passengers", "baggage", "wait"),
    [(0, 0, 0), (3, 0, 0), (1, -1, 0), (1, 0, -1)],
)
def test_candidate_rejects_invalid_values(passengers: int, baggage: int, wait: int) -> None:
    with pytest.raises(ValueError, match="must"):
        MatchCandidate(
            id=uuid4(),
            arrival_location_id=uuid4(),
            arrival_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            passengers=passengers,
            baggage=baggage,
            max_wait=timedelta(minutes=wait),
        )
