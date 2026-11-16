"""Тексты уведомлений (M-04): собираются при отправке из вида, названия группы и данных рейса.

В текст попадают только название группы, номер рейса и время прибытия. Телефонов, имён и
username попутчиков здесь нет: контакты открываются в мини-приложении.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.models import NotificationKind

BUTTON_TEXT = "Открыть поездку"


def format_arrival(arrival_local: datetime) -> str:
    """«ЧЧ:ММ, ДД.ММ»: `arrival_local` уже переведено в часовой пояс аэропорта прилёта."""
    return arrival_local.strftime("%H:%M, %d.%m")


def render(
    kind: NotificationKind,
    payload: Mapping[str, Any],
    *,
    title: str,
    trip_number: str | None,
    arrival_local: datetime | None,
) -> str:
    """Текст сообщения по-русски. `payload` в текст не попадает: всё нужное пришло аргументами."""
    del payload
    flight = f"Рейс {trip_number}" if trip_number else "Рейс"
    match kind:
        case NotificationKind.TRANSFER_ASSIGNED:
            return f"Попутчики найдены — «{title}». Откройте поездку, чтобы увидеть контакты."
        case NotificationKind.TRANSFER_CHANGED:
            return f"Состав вашего трансфера изменился — «{title}»."
        case NotificationKind.TRANSFER_LEFT:
            return (
                f"Вы больше не в трансфере — «{title}». "
                "Мы продолжаем искать попутчиков, если поиск включён."
            )
        case NotificationKind.POINTS_CHANGED:
            return (
                f"Организатор изменил место сбора или маршрут — «{title}». "
                "Проверьте детали поездки."
            )
        case NotificationKind.FLIGHT_CHANGED:
            if arrival_local is None:
                return f"{flight}: расчётное прибытие изменилось — «{title}»."
            return (
                f"{flight}: новое расчётное прибытие {format_arrival(arrival_local)} — «{title}»."
            )
        case NotificationKind.FLIGHT_CANCELLED:
            return f"{flight} отменён — «{title}». Выберите другой рейс в заявке."
