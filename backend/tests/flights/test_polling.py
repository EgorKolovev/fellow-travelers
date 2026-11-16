"""`polling.next_poll_at`: интервалы опроса по Д2."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.flights.polling import next_poll_at
from app.models.enums import TripStatus

DEP = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
ARR = DEP + timedelta(hours=3)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def poll(now: datetime, status: TripStatus = TripStatus.SCHEDULED) -> datetime | None:
    return next_poll_at(scheduled_departure=DEP, effective_arrival=ARR, status=status, now=now)


@pytest.mark.parametrize(
    ("before_departure", "interval"),
    [
        (48 * H, 60 * M),
        (24 * H, 60 * M),
        (24 * H - M, 30 * M),
        (6 * H, 30 * M),
        (6 * H - M, 10 * M),
        (M, 10 * M),
    ],
)
def test_before_departure_intervals(before_departure: timedelta, interval: timedelta) -> None:
    now = DEP - before_departure
    assert poll(now) == now + interval


def test_after_departure_waits_until_half_hour_before_arrival() -> None:
    now = DEP + H
    assert poll(now) == ARR - 30 * M


def test_departure_moment_counts_as_after_departure() -> None:
    assert poll(DEP) == ARR - 30 * M


def test_departed_status_before_scheduled_departure_counts_as_airborne() -> None:
    now = DEP - H
    assert poll(now, TripStatus.DEPARTED) == ARR - 30 * M


def test_half_hour_before_arrival_and_later_polls_every_ten_minutes() -> None:
    wake = ARR - 30 * M
    assert poll(wake) == wake + 10 * M
    assert poll(ARR) == ARR + 10 * M


def test_stops_two_hours_after_arrival() -> None:
    assert poll(ARR + 2 * H) == ARR + 2 * H + 10 * M
    assert poll(ARR + 2 * H + timedelta(seconds=1)) is None


@pytest.mark.parametrize("status", [TripStatus.LANDED, TripStatus.CANCELLED])
def test_final_status_stops_polling(status: TripStatus) -> None:
    assert poll(DEP - 48 * H, status) is None


@pytest.mark.parametrize("status", [TripStatus.DELAYED, TripStatus.UNKNOWN])
def test_other_statuses_keep_polling(status: TripStatus) -> None:
    assert poll(DEP - 48 * H, status) == DEP - 47 * H


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2026, 11, 12, 10, 0)
    with pytest.raises(ValueError, match="часовым поясом"):
        next_poll_at(
            scheduled_departure=naive, effective_arrival=ARR, status=TripStatus.SCHEDULED, now=DEP
        )
    with pytest.raises(ValueError, match="часовым поясом"):
        next_poll_at(
            scheduled_departure=DEP, effective_arrival=naive, status=TripStatus.SCHEDULED, now=DEP
        )
    with pytest.raises(ValueError, match="часовым поясом"):
        next_poll_at(
            scheduled_departure=DEP, effective_arrival=ARR, status=TripStatus.SCHEDULED, now=naive
        )


@given(
    offset=st.integers(min_value=-10 * 24 * 60, max_value=10 * 24 * 60),
    duration=st.integers(min_value=1, max_value=20 * 60),
    status=st.sampled_from(list(TripStatus)),
)
def test_result_is_none_or_strictly_later(offset: int, duration: int, status: TripStatus) -> None:
    now = DEP + offset * M
    result = next_poll_at(
        scheduled_departure=DEP,
        effective_arrival=DEP + duration * M,
        status=status,
        now=now,
    )
    assert result is None or result > now
