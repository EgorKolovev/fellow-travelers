from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from uuid import UUID

MAX_PASSENGERS = 3
MAX_BAGGAGE = 2


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    id: UUID
    arrival_location_id: UUID
    arrival_at: datetime
    passengers: int
    baggage: int
    max_wait: timedelta

    def __post_init__(self) -> None:
        if self.arrival_at.tzinfo is None or self.arrival_at.utcoffset() is None:
            raise ValueError("arrival_at must be timezone-aware")
        if self.passengers not in (1, 2):
            raise ValueError("passengers must be 1 or 2")
        if self.baggage < 0:
            raise ValueError("baggage must not be negative")
        if self.max_wait < timedelta(0):
            raise ValueError("max_wait must not be negative")


def is_compatible(group: Sequence[MatchCandidate]) -> bool:
    """Можно ли посадить всю группу в одну машину (минимум заявок не проверяется)."""
    if not group:
        return False
    if len({c.id for c in group}) != len(group):
        return False
    if len({c.arrival_location_id for c in group}) != 1:
        return False
    if sum(c.passengers for c in group) > MAX_PASSENGERS:
        return False
    if sum(c.baggage for c in group) > MAX_BAGGAGE:
        return False
    # все ждут самого позднего прилетающего, каждый — в пределах своего ожидания
    latest = max(c.arrival_at for c in group)
    return all(latest - c.arrival_at <= c.max_wait for c in group)


def _sort_key(c: MatchCandidate) -> tuple[datetime, UUID]:
    return c.arrival_at, c.id


def _group_rank(
    group: Sequence[MatchCandidate],
) -> tuple[int, timedelta, timedelta, tuple[UUID, ...]]:
    arrivals = [c.arrival_at for c in group]
    latest = max(arrivals)
    return (
        -sum(c.passengers for c in group),
        latest - min(arrivals),
        sum((latest - t for t in arrivals), timedelta(0)),
        tuple(sorted(c.id for c in group)),
    )


def find_group(
    new: MatchCandidate, candidates: Iterable[MatchCandidate]
) -> tuple[MatchCandidate, ...] | None:
    """Лучшая группа из `new` и 1–2 кандидатов либо None, если трансфер не собрать.

    Группа всегда из 2–3 заявок.
    """
    suitable = [
        c
        for c in candidates
        if c.id != new.id
        and c.arrival_location_id == new.arrival_location_id
        and new.passengers + c.passengers <= MAX_PASSENGERS
        and is_compatible((new, c))
    ]
    # в заявке минимум 1 пассажир, а мест 3, поэтому с new влезает не более 2 кандидатов
    groups: list[tuple[MatchCandidate, ...]] = [(new, c) for c in suitable]
    groups.extend((new, a, b) for a, b in combinations(suitable, 2) if is_compatible((new, a, b)))
    if not groups:
        return None
    best = min(groups, key=_group_rank)
    return tuple(sorted(best, key=_sort_key))


@dataclass(frozen=True, slots=True)
class OpenTransfer:
    """Трансфер, который можно дополнить: active, origin=auto, без уехавших (отбирает сервис)."""

    id: UUID
    members: tuple[MatchCandidate, ...]  # минимум 2 заявки


@dataclass(frozen=True, slots=True)
class JoinTransfer:
    transfer_id: UUID


@dataclass(frozen=True, slots=True)
class NewGroup:
    members: tuple[MatchCandidate, ...]


_PlacementRank = tuple[int, int, timedelta, timedelta, tuple[UUID, ...]]


def _placement_rank(car: Sequence[MatchCandidate], placed: int) -> _PlacementRank:
    """Ранг варианта по итоговой машине `car`; `placed` — сколько заявок размещает решение."""
    arrivals = [c.arrival_at for c in car]
    latest = max(arrivals)
    return (
        -sum(c.passengers for c in car),
        -placed,
        latest - min(arrivals),
        sum((latest - t for t in arrivals), timedelta(0)),
        tuple(sorted(c.id for c in car)),
    )


def find_placement(
    new: MatchCandidate,
    open_transfers: Iterable[OpenTransfer],
    free: Iterable[MatchCandidate],
) -> JoinTransfer | NewGroup | None:
    """Куда посадить `new`: в открытый трансфер, в новую группу из `free` либо никуда (None).

    Вариантов не больше одного на трансфер плюс одна группа от `find_group`. Лучший — по рангу:
    больше пассажиров в машине, больше размещённых заявок (join — одна, группа — все её члены),
    меньше разброс прилётов, меньше сумма ожиданий, затем отсортированные id машины.
    """
    options: list[tuple[_PlacementRank, JoinTransfer | NewGroup]] = []
    for transfer in open_transfers:
        if any(m.id == new.id for m in transfer.members):
            continue
        car = (*transfer.members, new)
        if is_compatible(car):
            options.append((_placement_rank(car, 1), JoinTransfer(transfer.id)))
    group = find_group(new, free)
    if group is not None:
        options.append((_placement_rank(group, len(group)), NewGroup(group)))
    if not options:
        return None
    return min(options, key=lambda option: option[0])[1]


def _exclusion_rank(
    excluded: Sequence[MatchCandidate], total: int, changed: AbstractSet[UUID]
) -> tuple[int, int, tuple[tuple[datetime, UUID], ...]]:
    return (
        -(total - len(excluded)),
        sum(1 for c in excluded if c.id not in changed),
        tuple(sorted(_sort_key(c) for c in excluded)),
    )


def members_to_exclude(
    waiting: Sequence[MatchCandidate], changed: AbstractSet[UUID]
) -> tuple[UUID, ...]:
    """Кого исключить из трансфера, чтобы остальные снова поместились в одну машину.

    `waiting` — неуехавшие члены активного трансфера (не больше 3), `changed` — id заявок на
    изменившихся рейсах. Остаётся наибольшая совместимая подгруппа; при равенстве исключаем
    меньше неизменившихся заявок, затем решает порядок (arrival_at, id) исключённых.
    Возвращает id исключённых по (arrival_at, id); пусто, если `waiting` уже совместима.
    """
    if not waiting or is_compatible(waiting):
        return ()
    options: list[tuple[MatchCandidate, ...]] = []
    # в совместимой подгруппе не больше MAX_PASSENGERS заявок, поэтому полный перебор мал
    for size in range(1, min(len(waiting), MAX_PASSENGERS) + 1):
        for kept in combinations(waiting, size):
            if is_compatible(kept):
                kept_ids = {c.id for c in kept}
                options.append(tuple(c for c in waiting if c.id not in kept_ids))
    # без вариантов остаться может только заявка, несовместимая сама с собой: не исключаем никого
    best = min(
        options,
        key=lambda excluded: _exclusion_rank(excluded, len(waiting), changed),
        default=(),
    )
    return tuple(c.id for c in sorted(best, key=_sort_key))
