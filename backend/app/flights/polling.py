"""Расписание опроса поставщика рейсов (Д2): чистая функция без БД."""

from datetime import datetime, timedelta

from app.models.enums import TripStatus

FAR_BEFORE_DEPARTURE = timedelta(hours=24)
NEAR_BEFORE_DEPARTURE = timedelta(hours=6)
FAR_INTERVAL = timedelta(minutes=60)
MIDDLE_INTERVAL = timedelta(minutes=30)
NEAR_INTERVAL = timedelta(minutes=10)
BEFORE_ARRIVAL = timedelta(minutes=30)
GIVE_UP_AFTER_ARRIVAL = timedelta(hours=2)


def _require_aware(**values: datetime) -> None:
    for name, value in values.items():
        if value.utcoffset() is None:
            raise ValueError(f"{name} должно быть с часовым поясом")


def next_poll_at(
    *,
    scheduled_departure: datetime,
    effective_arrival: datetime,
    status: TripStatus,
    now: datetime,
) -> datetime | None:
    """Когда опрашивать рейс в следующий раз; `None` — опрос прекращён.

    Прекращается при `landed` / `cancelled` и через 2 ч после расчётного прибытия. До вылета
    интервал зависит от времени до него: от суток — 60 мин, от 6 ч — 30 мин, иначе 10 мин.
    После вылета (или при статусе `departed`) пауза до «прибытие минус 30 мин», затем 10 мин.
    Все времена с часовым поясом, иначе `ValueError`.
    """
    _require_aware(
        scheduled_departure=scheduled_departure, effective_arrival=effective_arrival, now=now
    )
    if status in (TripStatus.LANDED, TripStatus.CANCELLED):
        return None
    if now > effective_arrival + GIVE_UP_AFTER_ARRIVAL:
        return None
    if now < scheduled_departure and status != TripStatus.DEPARTED:
        until = scheduled_departure - now
        if until >= FAR_BEFORE_DEPARTURE:
            return now + FAR_INTERVAL
        if until >= NEAR_BEFORE_DEPARTURE:
            return now + MIDDLE_INTERVAL
        return now + NEAR_INTERVAL
    wake = effective_arrival - BEFORE_ARRIVAL
    return wake if now < wake else now + NEAR_INTERVAL
