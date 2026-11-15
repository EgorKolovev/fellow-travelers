import random
from datetime import UTC, datetime, timedelta
from itertools import combinations
from uuid import UUID

from hypothesis import event, given, settings
from hypothesis import strategies as st

from app.matching import (
    JoinTransfer,
    MatchCandidate,
    NewGroup,
    OpenTransfer,
    find_group,
    find_placement,
    is_compatible,
    members_to_exclude,
)

_BASE = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

Scenario = tuple[MatchCandidate, list[MatchCandidate]]


@st.composite
def _candidate(draw: st.DrawFn, id_int: int) -> MatchCandidate:
    return MatchCandidate(
        id=UUID(int=id_int),
        arrival_location_id=UUID(int=draw(st.integers(1, 2))),
        arrival_at=_BASE + timedelta(minutes=draw(st.integers(0, 60))),
        passengers=draw(st.integers(1, 2)),
        baggage=draw(st.integers(0, 2)),
        max_wait=timedelta(minutes=draw(st.integers(0, 40))),
    )


@st.composite
def _scenario(draw: st.DrawFn) -> Scenario:
    size = draw(st.integers(0, 8))
    new = draw(_candidate(1000))
    others = [draw(_candidate(i)) for i in range(size)]
    if draw(st.booleans()):
        # кандидат с id как у new должен игнорироваться
        others.append(draw(_candidate(1000)))
    return new, others


def _brute(new: MatchCandidate, others: list[MatchCandidate]) -> tuple[MatchCandidate, ...] | None:
    pool = [c for c in others if c.id != new.id]
    groups = [
        (new, *subset)
        for size in (1, 2)
        for subset in combinations(pool, size)
        if is_compatible((new, *subset))
    ]
    if not groups:
        return None

    def key(g: tuple[MatchCandidate, ...]) -> tuple[int, timedelta, timedelta, tuple[UUID, ...]]:
        latest = max(c.arrival_at for c in g)
        spread = latest - min(c.arrival_at for c in g)
        wait = sum((latest - c.arrival_at for c in g), timedelta(0))
        return -sum(c.passengers for c in g), spread, wait, tuple(sorted(c.id for c in g))

    best = min(groups, key=key)
    return tuple(sorted(best, key=lambda c: (c.arrival_at, c.id)))


@settings(max_examples=200, deadline=None)
@given(_scenario())
def test_result_is_valid_group(scenario: Scenario) -> None:
    new, others = scenario
    result = find_group(new, others)
    if result is not None:
        assert new in result
        assert is_compatible(result)
        assert 2 <= len(result) <= 3


@settings(max_examples=200, deadline=None)
@given(_scenario(), st.randoms(use_true_random=False))
def test_result_independent_of_order(scenario: Scenario, rnd: random.Random) -> None:
    new, others = scenario
    shuffled = list(others)
    rnd.shuffle(shuffled)
    assert find_group(new, others) == find_group(new, shuffled)


@settings(max_examples=200, deadline=None)
@given(_scenario())
def test_matches_brute_force(scenario: Scenario) -> None:
    new, others = scenario
    assert find_group(new, others) == _brute(new, others)


# ---------- find_placement ----------

Placement = JoinTransfer | NewGroup | None
PlacementScenario = tuple[MatchCandidate, list[OpenTransfer], list[MatchCandidate]]


@st.composite
def _close_candidate(draw: st.DrawFn, id_int: int) -> MatchCandidate:
    """Кандидат, который часто совместим с другими: иначе join и группы почти не встречаются."""
    if draw(st.sampled_from([True, True, False])):
        return MatchCandidate(
            id=UUID(int=id_int),
            arrival_location_id=UUID(int=1),
            arrival_at=_BASE + timedelta(minutes=draw(st.integers(0, 15))),
            passengers=draw(st.sampled_from([1, 1, 1, 2])),
            baggage=draw(st.sampled_from([0, 0, 1])),
            max_wait=timedelta(minutes=draw(st.integers(15, 40))),
        )
    return MatchCandidate(
        id=UUID(int=id_int),
        arrival_location_id=UUID(int=draw(st.integers(1, 2))),
        arrival_at=_BASE + timedelta(minutes=draw(st.integers(0, 60))),
        passengers=draw(st.integers(1, 2)),
        baggage=draw(st.integers(0, 2)),
        max_wait=timedelta(minutes=draw(st.integers(0, 40))),
    )


@st.composite
def _placement_scenario(draw: st.DrawFn) -> PlacementScenario:
    new = draw(_close_candidate(1000))
    transfers: list[OpenTransfer] = []
    for t in range(draw(st.integers(0, 3))):
        members = [
            draw(_close_candidate(100 + 10 * t + k))
            for k in range(draw(st.sampled_from([2, 2, 2, 3])))
        ]
        if draw(st.booleans()) and draw(st.booleans()):
            # заявка уже в трансфере: join для неё невозможен
            members[0] = draw(_close_candidate(1000))
        transfers.append(OpenTransfer(UUID(int=5000 + t), tuple(members)))
    free = [draw(_close_candidate(i)) for i in range(draw(st.sampled_from([0, 0, 1, 1, 2, 4])))]
    if draw(st.booleans()):
        # кандидат с id как у new должен игнорироваться
        free.append(draw(_close_candidate(1000)))
    return new, transfers, free


def _placement_rank(
    car: tuple[MatchCandidate, ...], placed: int
) -> tuple[int, int, timedelta, timedelta, tuple[UUID, ...]]:
    latest = max(c.arrival_at for c in car)
    return (
        -sum(c.passengers for c in car),
        -placed,
        latest - min(c.arrival_at for c in car),
        sum((latest - c.arrival_at for c in car), timedelta(0)),
        tuple(sorted(c.id for c in car)),
    )


def _brute_placement(
    new: MatchCandidate, transfers: list[OpenTransfer], free: list[MatchCandidate]
) -> Placement:
    options: list[tuple[tuple[int, int, timedelta, timedelta, tuple[UUID, ...]], Placement]] = []
    for transfer in transfers:
        car = (*transfer.members, new)
        if new.id not in {m.id for m in transfer.members} and is_compatible(car):
            options.append((_placement_rank(car, 1), JoinTransfer(transfer.id)))
    group = _brute(new, free)
    if group is not None:
        options.append((_placement_rank(group, len(group)), NewGroup(group)))
    if not options:
        return None
    return min(options, key=lambda option: option[0])[1]


@settings(max_examples=300, deadline=None)
@given(_placement_scenario())
def test_placement_result_is_valid(scenario: PlacementScenario) -> None:
    new, transfers, free = scenario
    result = find_placement(new, transfers, free)
    event(type(result).__name__)
    if isinstance(result, JoinTransfer):
        by_id = {t.id: t for t in transfers}
        assert result.transfer_id in by_id
        members = by_id[result.transfer_id].members
        assert new.id not in {m.id for m in members}
        assert is_compatible((*members, new))
    elif isinstance(result, NewGroup):
        assert new in result.members
        assert is_compatible(result.members)
        assert 2 <= len(result.members) <= 3
        assert {m.id for m in result.members} - {new.id} <= {c.id for c in free}


@settings(max_examples=300, deadline=None)
@given(_placement_scenario(), st.randoms(use_true_random=False))
def test_placement_independent_of_order(scenario: PlacementScenario, rnd: random.Random) -> None:
    new, transfers, free = scenario
    shuffled_transfers = [
        OpenTransfer(t.id, tuple(rnd.sample(t.members, len(t.members)))) for t in transfers
    ]
    rnd.shuffle(shuffled_transfers)
    shuffled_free = list(free)
    rnd.shuffle(shuffled_free)
    assert find_placement(new, transfers, free) == find_placement(
        new, shuffled_transfers, shuffled_free
    )


@settings(max_examples=300, deadline=None)
@given(_placement_scenario())
def test_placement_matches_brute_force(scenario: PlacementScenario) -> None:
    new, transfers, free = scenario
    assert find_placement(new, transfers, free) == _brute_placement(new, transfers, free)


# ---------- members_to_exclude ----------

ExclusionScenario = tuple[list[MatchCandidate], frozenset[UUID]]


@st.composite
def _exclusion_scenario(draw: st.DrawFn) -> ExclusionScenario:
    waiting = [draw(_close_candidate(i)) for i in range(draw(st.integers(1, 3)))]
    # id вне waiting тоже допустимы: changed приходит со всей группы
    changed = frozenset(UUID(int=i) for i in draw(st.sets(st.integers(0, 4))))
    return waiting, changed


def _brute_exclusion(waiting: list[MatchCandidate], changed: frozenset[UUID]) -> tuple[UUID, ...]:
    best: tuple[tuple[int, int, list[tuple[datetime, UUID]]], list[MatchCandidate]] | None = None
    for mask in range(1, 1 << len(waiting)):
        kept = [c for i, c in enumerate(waiting) if mask >> i & 1]
        if not is_compatible(kept):
            continue
        out = [c for i, c in enumerate(waiting) if not mask >> i & 1]
        key = (
            -len(kept),
            sum(1 for c in out if c.id not in changed),
            sorted((c.arrival_at, c.id) for c in out),
        )
        if best is None or key < best[0]:
            best = (key, out)
    assert best is not None
    return tuple(c.id for c in sorted(best[1], key=lambda c: (c.arrival_at, c.id)))


@settings(max_examples=300, deadline=None)
@given(_exclusion_scenario())
def test_exclusion_remainder_is_compatible(scenario: ExclusionScenario) -> None:
    waiting, changed = scenario
    excluded = members_to_exclude(waiting, changed)
    event("empty" if not excluded else f"excluded {len(excluded)}")
    assert len(set(excluded)) == len(excluded)
    assert set(excluded) <= {c.id for c in waiting}
    kept = [c for c in waiting if c.id not in excluded]
    assert kept, "нельзя исключать всех"
    assert is_compatible(kept)
    by_id = {c.id: c for c in waiting}
    keys = [(by_id[i].arrival_at, i) for i in excluded]
    assert keys == sorted(keys)


@settings(max_examples=300, deadline=None)
@given(_exclusion_scenario())
def test_exclusion_empty_for_compatible_group(scenario: ExclusionScenario) -> None:
    waiting, changed = scenario
    if is_compatible(waiting):
        assert members_to_exclude(waiting, changed) == ()


@settings(max_examples=300, deadline=None)
@given(_exclusion_scenario(), st.randoms(use_true_random=False))
def test_exclusion_independent_of_order(scenario: ExclusionScenario, rnd: random.Random) -> None:
    waiting, changed = scenario
    shuffled = list(waiting)
    rnd.shuffle(shuffled)
    assert members_to_exclude(waiting, changed) == members_to_exclude(shuffled, changed)


@settings(max_examples=300, deadline=None)
@given(_exclusion_scenario())
def test_exclusion_matches_brute_force(scenario: ExclusionScenario) -> None:
    waiting, changed = scenario
    assert members_to_exclude(waiting, changed) == _brute_exclusion(waiting, changed)
