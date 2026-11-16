"""Тексты уведомлений (M-04): собираются при отправке из вида, названия группы и данных рейса.

В текст попадают только название группы, номер рейса и время прибытия. Телефонов, имён и
username попутчиков здесь нет: контакты открываются в мини-приложении.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.models import NotificationKind

BUTTON_TEXT = "Открыть поездку"

TITLE_MAX_LENGTH = 60
NO_TITLE = "без названия"

# Управляющие символы и разделители строк; невидимые знаки нулевой ширины убираются совсем
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LETTER = "A-Za-zЀ-ӿ"
# Порядок важен: сначала целые «ссылочные» токены, потом отдельные сущности
_ENTITIES = [
    re.compile(r"\w+://\S+"),  # любая схема: https://, tg://, ftp://
    re.compile(r"(?<!\w)www\.\S+"),
    re.compile(r"\S+@\S+"),  # e-mail и «имя@бот»
    # «слово.домен»: хотя бы один знак перед точкой, точка и сразу за ней 2+ буквы. «ул. Ленина»
    # (после точки пробел), «8.30» и «т.е.» (цифры или одна буква) не совпадают; «pay.example»,
    # «t.me/x», «пример.рф» совпадают. Пара «г.Москва» без пробела тоже уйдёт: это цена, которую
    # платим за то, что Telegram сам решает, какие домены считать ссылками.
    re.compile(rf"\S+\.[{_LETTER}]{{2,}}\S*"),
    re.compile(r"@\w+"),  # упоминание
    re.compile(r"#\w+"),  # хэштег
    re.compile(r"(?<!\w)/\w+"),  # команда бота; «и/или» и «12/11» не трогаем
    re.compile(r"(?<!\w)\$[A-Za-z]{2,}"),  # кэштег
]
# Телефон и номер карты: цифры, пробелы, дефисы, скобки, ведущий плюс; считаем только цифры
_PHONE = re.compile(r"(?<!\w)\+?[\d(][\d\s\-()]*\d")
_MIN_PHONE_DIGITS = 7


def _drop_phone(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group())
    return " " if digits >= _MIN_PHONE_DIGITS else match.group()


def safe_title(title: str) -> str:
    """Название группы, безопасное для вставки в сообщение от бота.

    Название задаёт админ группы, а клиенты Telegram сами делают кликабельными ссылки, домены,
    упоминания, хэштеги, команды, e-mail и телефоны, даже без `parse_mode`. Название вида
    «Оплата: pay-transfer.example» стало бы ссылкой от официального бота. Поэтому такие токены
    вырезаются целиком (уместнее потерять часть названия, чем дать клик). Затем пробелы
    схлопываются, лишняя пунктуация по краям убирается, длинное название обрезается до 60 знаков
    по границе слова с «…». Если ничего не осталось, возвращается «без названия»: «вашей
    группы» не подходит грамматически во всех шаблонах. Регулярка «слово.домен»
    описана у `_ENTITIES`.
    Телефоном считается любая цепочка из 7+ цифр с пробелами, дефисами и скобками, поэтому
    «2026-2027» тоже уйдёт.
    """
    text = _ZERO_WIDTH.sub("", title)
    text = _CONTROL.sub(" ", text)
    for pattern in _ENTITIES:
        text = pattern.sub(" ", text)
    text = _PHONE.sub(_drop_phone, text)
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,;:])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = text.strip(" ,;:-–—|/\\")
    if len(text) > TITLE_MAX_LENGTH:
        cut = text[: TITLE_MAX_LENGTH - 1]
        if text[TITLE_MAX_LENGTH - 1] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:-–—|/\\") + "…"
    return text or NO_TITLE


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
    """Текст сообщения по-русски. Из `payload` берётся только `role` (свой рейс или попутчика)."""
    title = safe_title(title)
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
            if payload.get("role") == "mate":
                flight = f"Рейс попутчика {trip_number}" if trip_number else "Рейс попутчика"
            if arrival_local is None:
                return f"{flight}: расчётное прибытие изменилось — «{title}»."
            return (
                f"{flight}: новое расчётное прибытие {format_arrival(arrival_local)} — «{title}»."
            )
        case NotificationKind.SEVERAL_CHANGES:
            return f"В поездке «{title}» несколько изменений. Откройте поездку, чтобы посмотреть."
        case NotificationKind.FLIGHT_CANCELLED:
            return f"{flight} отменён — «{title}». Выберите другой рейс в заявке."
