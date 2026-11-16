"""`render`: тексты уведомлений по видам, местное время прибытия, отсутствие номера рейса."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import NotificationKind
from app.services.notification_text import format_arrival, render
from app.timezones import to_local

TITLE = "Конференция"
PAYLOAD: dict[str, Any] = {"event_id": "e", "trip_id": "t", "status": "delayed"}
# 05:05 UTC = 08:05 в Москве
ARRIVAL = to_local(datetime(2026, 11, 12, 5, 5, tzinfo=UTC), "Europe/Moscow")


def _render(kind: NotificationKind, **kw: Any) -> str:
    args: dict[str, Any] = {"title": TITLE, "trip_number": "SU1234", "arrival_local": ARRIVAL}
    args.update(kw)
    return render(kind, PAYLOAD, **args)


def test_format_arrival_uses_local_time_of_the_airport() -> None:
    assert format_arrival(ARRIVAL) == "08:05, 12.11"
    ekb = to_local(datetime(2026, 11, 12, 5, 5, tzinfo=UTC), "Asia/Yekaterinburg")
    assert format_arrival(ekb) == "10:05, 12.11"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (
            NotificationKind.TRANSFER_ASSIGNED,
            "Попутчики найдены — «Конференция». Откройте поездку, чтобы увидеть контакты.",
        ),
        (NotificationKind.TRANSFER_CHANGED, "Состав вашего трансфера изменился — «Конференция»."),
        (
            NotificationKind.TRANSFER_LEFT,
            "Вы больше не в трансфере — «Конференция». "
            "Мы продолжаем искать попутчиков, если поиск включён.",
        ),
        (
            NotificationKind.POINTS_CHANGED,
            "Организатор изменил место сбора или маршрут — «Конференция». "
            "Проверьте детали поездки.",
        ),
        (
            NotificationKind.FLIGHT_CHANGED,
            "Рейс SU1234: новое расчётное прибытие 08:05, 12.11 — «Конференция».",
        ),
        (
            NotificationKind.FLIGHT_CANCELLED,
            "Рейс SU1234 отменён — «Конференция». Выберите другой рейс в заявке.",
        ),
        (
            NotificationKind.SEVERAL_CHANGES,
            "В поездке «Конференция» несколько изменений. Откройте поездку, чтобы посмотреть.",
        ),
    ],
)
def test_every_kind_has_its_text(kind: NotificationKind, expected: str) -> None:
    assert _render(kind) == expected


def test_all_kinds_are_covered() -> None:
    for kind in NotificationKind:
        assert _render(kind)


def test_flight_texts_without_number() -> None:
    assert (
        _render(NotificationKind.FLIGHT_CHANGED, trip_number=None)
        == "Рейс: новое расчётное прибытие 08:05, 12.11 — «Конференция»."
    )
    assert (
        _render(NotificationKind.FLIGHT_CANCELLED, trip_number=None)
        == "Рейс отменён — «Конференция». Выберите другой рейс в заявке."
    )


def test_flight_changed_without_known_arrival_has_no_time() -> None:
    text = _render(NotificationKind.FLIGHT_CHANGED, arrival_local=None)

    assert text == "Рейс SU1234: расчётное прибытие изменилось — «Конференция»."


def test_text_has_nothing_but_title_number_and_time() -> None:
    payload = {"event_id": "e", "trip_id": "t", "phone": "+79990001122", "first_name": "Иван"}

    for kind in NotificationKind:
        text = render(kind, payload, title=TITLE, trip_number=None, arrival_local=ARRIVAL)
        assert "+7999" not in text
        assert "Иван" not in text
        assert "t.me" not in text


def test_flight_changed_for_mate_names_the_mate_flight() -> None:
    mate = {**PAYLOAD, "role": "mate"}

    assert render(
        NotificationKind.FLIGHT_CHANGED,
        mate,
        title=TITLE,
        trip_number="SU1234",
        arrival_local=ARRIVAL,
    ) == ("Рейс попутчика SU1234: новое расчётное прибытие 08:05, 12.11 — «Конференция».")
    assert render(
        NotificationKind.FLIGHT_CHANGED, mate, title=TITLE, trip_number=None, arrival_local=None
    ) == ("Рейс попутчика: расчётное прибытие изменилось — «Конференция».")
    passenger = {**PAYLOAD, "role": "passenger"}
    assert render(
        NotificationKind.FLIGHT_CHANGED,
        passenger,
        title=TITLE,
        trip_number="SU1234",
        arrival_local=ARRIVAL,
    ) == _render(NotificationKind.FLIGHT_CHANGED)
