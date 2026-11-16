"""Отправитель уведомлений в воркере (M-04): аренда строк, отправка вне транзакции, результат.

Проход `send_due`:

1. Первая транзакция берёт до `batch` строк `pending` с `run_after <= now` (порядок
   `(run_after, id)`, `FOR UPDATE SKIP LOCKED`) и отодвигает им `run_after` на `LEASE`.
   Параллельные воркеры делят строки, упавший воркер возвращает их по истечении аренды.
2. Для каждой строки вне транзакции собирается текст и идёт запрос в Telegram.
3. Отдельная короткая транзакция пишет результат. Метрика растёт после commit.

Если проход затянулся и аренда подходит к концу, новые отправки не начинаются: остаток вернётся
в очередь сам, а сообщение не уйдёт дважды.

В логах только id уведомления, вид, исход и имя класса ошибки: ни telegram_id, ни имён, ни текста,
ни токена, ни адреса relay.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import mini_app_link
from app.models import (
    Event,
    Location,
    Notification,
    NotificationKind,
    NotificationStatus,
    Trip,
    User,
)
from app.observability import metrics
from app.observability.deferred import defer_increment
from app.observability.metrics import NotificationOutcome
from app.services.notification_text import BUTTON_TEXT, render
from app.timezones import to_local

logger = logging.getLogger(__name__)

# аренда: пока строку отправляют, другой воркер её не берёт; если воркер упал, строка вернётся сама
LEASE = timedelta(minutes=2)
# сколько ждём ответ Telegram; константа модуля, чтобы тест мог её уменьшить
SEND_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600


class NotificationSender(Protocol):
    """Доставка одного сообщения с кнопкой-ссылкой; ошибки Telegram поднимаются как исключения."""

    async def send(self, chat_id: int, text: str, button_url: str) -> None: ...


class TelegramSender:
    """`NotificationSender` через aiogram `Bot` (в проде он ходит через relay)."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str, button_url: str) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=BUTTON_TEXT, url=button_url)]]
        )
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Итог прохода: отправлено, пропущено (бот заблокирован), отложено на повтор, провалено."""

    sent: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class _Leased:
    id: int
    event_id: uuid.UUID
    user_id: uuid.UUID
    kind: NotificationKind
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class _Message:
    chat_id: int
    text: str
    button_url: str


async def _lease_due(
    session_factory: async_sessionmaker[AsyncSession], now: datetime, batch: int
) -> list[_Leased]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Notification.id,
                    Notification.event_id,
                    Notification.user_id,
                    Notification.kind,
                    Notification.payload,
                    Notification.attempts,
                )
                .where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.run_after <= now,
                )
                .order_by(Notification.run_after, Notification.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            return []
        await session.execute(
            update(Notification)
            .where(Notification.id.in_([row.id for row in rows]))
            .values(run_after=now + LEASE)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return [
        _Leased(row.id, row.event_id, row.user_id, row.kind, dict(row.payload), row.attempts)
        for row in rows
    ]


def _arrival_local(payload: dict[str, Any], timezone: str | None) -> datetime | None:
    raw = payload.get("effective_arrival")
    if not isinstance(raw, str) or timezone is None:
        return None
    return to_local(datetime.fromisoformat(raw), timezone)


async def _build_message(
    session_factory: async_sessionmaker[AsyncSession], item: _Leased
) -> _Message | None:
    """Адресат, текст и ссылка кнопки; `None`, если пользователь или группа уже удалены."""
    async with session_factory() as session:
        chat_id = await session.scalar(select(User.telegram_id).where(User.id == item.user_id))
        title = await session.scalar(select(Event.title).where(Event.id == item.event_id))
        if chat_id is None or title is None:
            return None
        trip_number: str | None = None
        timezone: str | None = None
        trip_id = item.payload.get("trip_id")
        if isinstance(trip_id, str):
            trip = (
                await session.execute(
                    select(Trip.number, Location.timezone)
                    .join(Location, Location.id == Trip.arrival_location_id)
                    .where(Trip.id == uuid.UUID(trip_id), Trip.event_id == item.event_id)
                )
            ).first()
            if trip is not None:
                trip_number, timezone = trip.number, trip.timezone
    text = render(
        item.kind,
        item.payload,
        title=title,
        trip_number=trip_number,
        arrival_local=_arrival_local(item.payload, timezone),
    )
    return _Message(
        chat_id=chat_id,
        text=text,
        button_url=mini_app_link(f"event_{item.event_id}"),
    )


def _is_unreachable(exc: BaseException) -> bool:
    """Бот заблокирован или пользователь ещё не писал боту: повтор не поможет."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and "chat not found" in exc.message.lower()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2**attempts, BACKOFF_MAX_SECONDS))


async def _store_result(
    session_factory: async_sessionmaker[AsyncSession],
    item: _Leased,
    outcome: NotificationOutcome,
    *,
    error: str | None = None,
    retry_at: datetime | None = None,
    attempts: int | None = None,
) -> None:
    values: dict[str, Any] = {"last_error": error}
    if outcome == NotificationOutcome.SENT:
        values.update(status=NotificationStatus.SENT, sent_at=func.now())
    elif outcome == NotificationOutcome.SKIPPED:
        values.update(status=NotificationStatus.SKIPPED)
    elif outcome == NotificationOutcome.FAILED:
        values.update(status=NotificationStatus.FAILED, attempts=attempts)
    else:
        values.update(run_after=retry_at)
        if attempts is not None:
            values.update(attempts=attempts)
    async with session_factory() as session:
        result = await session.execute(
            update(Notification)
            .where(Notification.id == item.id, Notification.status == NotificationStatus.PENDING)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:  # type: ignore[attr-defined]  # строки уже нет (группу удалили): не считаем
            defer_increment(session, metrics.notifications_total.labels(item.kind.value, outcome))
        await session.commit()


async def _deliver(
    session_factory: async_sessionmaker[AsyncSession],
    sender: NotificationSender,
    item: _Leased,
    now: datetime,
) -> NotificationOutcome | None:
    """Собирает и отправляет одно сообщение, пишет результат; `None` — строка уже не нужна."""
    error: BaseException | None = None
    try:
        message = await _build_message(session_factory, item)
        if message is None:
            return None  # пользователь или группа удалены: строка уйдёт каскадом
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await sender.send(message.chat_id, message.text, message.button_url)
    except Exception as exc:
        error = exc
    if error is None:
        await _store_result(session_factory, item, NotificationOutcome.SENT)
        return NotificationOutcome.SENT

    name = type(error).__name__
    log_extra = {"notification_id": item.id, "notification_kind": item.kind.value}
    if _is_unreachable(error):
        outcome = NotificationOutcome.SKIPPED
        await _store_result(session_factory, item, outcome, error=name)
        logger.info("Уведомление пропущено (%s)", name, extra=log_extra)
    elif isinstance(error, TelegramRetryAfter):
        outcome = NotificationOutcome.RETRIED
        retry_at = now + timedelta(seconds=error.retry_after)
        await _store_result(session_factory, item, outcome, error=name, retry_at=retry_at)
        logger.warning("Уведомление отложено (%s)", name, extra=log_extra)
    else:
        attempts = item.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            outcome = NotificationOutcome.FAILED
            await _store_result(session_factory, item, outcome, error=name, attempts=attempts)
            logger.error("Уведомление не доставлено (%s)", name, extra=log_extra)
        else:
            outcome = NotificationOutcome.RETRIED
            await _store_result(
                session_factory,
                item,
                outcome,
                error=name,
                retry_at=now + _backoff(attempts),
                attempts=attempts,
            )
            logger.warning("Уведомление отложено (%s)", name, extra=log_extra)
    return outcome


async def send_due(
    session_factory: async_sessionmaker[AsyncSession],
    sender: NotificationSender,
    *,
    now: datetime | None = None,
    batch: int = 20,
    stop: asyncio.Event | None = None,
) -> SendResult:
    """Один проход: отправляет сообщения, которым пора, и записывает исход каждого.

    Исход: успех — `sent`; бот заблокирован или чата нет — `skipped`; `TelegramRetryAfter` —
    повтор через `retry_after` секунд без роста `attempts`; сеть, 5xx, таймаут и любая другая
    ошибка — `attempts += 1` и пауза `min(30 с · 2^attempts, 1 ч)`, на 8-й неудаче `failed`.
    Ошибка одной строки не останавливает остальные. После `stop` новые отправки не начинаются.
    """
    if stop is not None and stop.is_set():
        return SendResult()
    now = now or datetime.now(UTC)
    leased = await _lease_due(session_factory, now, batch)
    started = time.monotonic()
    budget = LEASE.total_seconds() - SEND_TIMEOUT_SECONDS
    counts = dict.fromkeys(NotificationOutcome, 0)
    for item in leased:
        if stop is not None and stop.is_set():
            break
        if time.monotonic() - started > budget:
            break  # аренда почти истекла: остаток вернётся в очередь сам
        outcome = await _deliver(session_factory, sender, item, now)
        if outcome is not None:
            counts[outcome] += 1
    return SendResult(
        sent=counts[NotificationOutcome.SENT],
        skipped=counts[NotificationOutcome.SKIPPED],
        retried=counts[NotificationOutcome.RETRIED],
        failed=counts[NotificationOutcome.FAILED],
    )
