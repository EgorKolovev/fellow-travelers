"""`render`: тексты уведомлений по видам, местное время прибытия, отсутствие номера рейса."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import NotificationKind
from app.services.notification_text import format_arrival, render, safe_title
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


# ---------------------------------------------------------------- safe_title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # без изменений
        ("Конференция", "Конференция"),
        ("Слёт 2026, ул. Ленина 8.30", "Слёт 2026, ул. Ленина 8.30"),
        ("Встреча в 8.30, т.е. рано!", "Встреча в 8.30, т.е. рано!"),
        ("Вылет 12/11 и/или 13/11", "Вылет 12/11 и/или 13/11"),
        # ссылки и сущности Telegram убираются
        ("Оплата трансфера: pay-transfer.example", "Оплата трансфера"),
        ("Оплата https://evil.example/pay сегодня", "Оплата сегодня"),
        ("Оплата tg://resolve?domain=x сегодня", "Оплата сегодня"),
        ("Карта www.evil и всё", "Карта и всё"),
        ("Сбор t.me/x", "Сбор"),
        ("Сбор пример.рф сегодня", "Сбор сегодня"),
        ("Пиши @admin_bot срочно", "Пиши срочно"),
        ("Слёт #срочно2026", "Слёт"),
        ("Нажми /start сейчас", "Нажми сейчас"),
        ("Пиши boss@example.com сегодня", "Пиши сегодня"),
        ("Оплата $USDT сегодня", "Оплата сегодня"),
        ("Звони +7 (999) 123-45-67 сегодня", "Звони сегодня"),
        ("Звони 89991234567", "Звони"),
        ("Карта 4276 1234 5678 9012", "Карта"),
        # запятые вокруг убранного не копятся
        ("Слёт, pay.example, 2026", "Слёт, 2026"),
        # переводы строк и управляющие символы
        ("Слёт\nв\r\nМоскве\t\x00завтра", "Слёт в Москве завтра"),
        ("  много   пробелов  ", "много пробелов"),
        ("Слёт\u200b2026", "Слёт2026"),
    ],
)
def test_safe_title(title: str, expected: str) -> None:
    assert safe_title(title) == expected


def test_safe_title_truncates_on_word_boundary_with_ellipsis() -> None:
    title = "слово " * 30

    result = safe_title(title)

    assert result == ("слово " * 10).strip() + "…"
    assert len(result) <= 60


def test_safe_title_truncates_one_long_word_hard() -> None:
    result = safe_title("я" * 100)

    assert result == "я" * 59 + "…"


def test_safe_title_keeps_title_of_exactly_the_limit() -> None:
    assert safe_title("я" * 60) == "я" * 60


@pytest.mark.parametrize(
    "title", ["", "   ", "\n\t", "pay.example", "@admin", "https://x.example/y"]
)
def test_safe_title_without_anything_left_is_placeholder(title: str) -> None:
    assert safe_title(title) == "без названия"


def test_safe_title_is_idempotent() -> None:
    once = safe_title("Оплата: pay.example, " + "длинное " * 20)

    assert safe_title(once) == once


@pytest.mark.parametrize(
    "token",
    [
        "pay-transfer.example",
        "evil.example",
        "https://",
        "t.me",
        "пример.рф",
        "@admin",
        "#hash",
        "/start",
        "boss@example.com",
        "999",
    ],
)
@pytest.mark.parametrize("kind", list(NotificationKind))
def test_render_with_hostile_title_has_no_link_like_tokens(
    kind: NotificationKind, token: str
) -> None:
    hostile = {
        "pay-transfer.example": "Оплата: pay-transfer.example",
        "evil.example": "Платите на evil.example/pay",
        "https://": "Платите https://evil.example",
        "t.me": "Чат t.me/evil",
        "пример.рф": "Оплата пример.рф",
        "@admin": "Пишите @admin",
        "#hash": "Слёт #hash",
        "/start": "Нажмите /start",
        "boss@example.com": "Пишите boss@example.com",
        "999": "Звоните +7 (999) 123-45-67",
    }[token]

    text = _render(kind, title=hostile)

    assert token not in text
    # безвредная часть названия остаётся
    assert f"«{hostile.split()[0].rstrip(':')}" in text


@pytest.mark.parametrize("kind", list(NotificationKind))
def test_render_with_empty_title_reads_as_without_name(kind: NotificationKind) -> None:
    text = _render(kind, title="pay.example")

    assert "«без названия»" in text
