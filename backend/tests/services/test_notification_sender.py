"""`send_due`: аренда, исходы отправки, повторы с паузой, параллельные проходы, остановка.

Сессии открываются на `db_engine` с commit: аренда и параллельные проходы на одной транзакции
`db_session` не проверить. Отправитель — `FakeSender`: в настоящий Telegram тесты не ходят.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    Event,
    Notification,
    NotificationKind,
    NotificationStatus,
    Trip,
    User,
)
from app.services import notification_sender
from app.services.notification_sender import LEASE, SendResult, TelegramSender, send_due
from tests.factories import make_event, make_trip, make_user
from tests.helpers.concurrency import wait
from tests.helpers.metrics import sample
from tests.helpers.telegram import BOT_TOKEN

NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
SECRET_NAME = "Секретнаяимя"
TITLE = "Конференция-12"
# 05:05 UTC: в Москве (аэропорт SVO) 08:05
ARRIVAL = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
METHOD = SendMessage(chat_id=1, text="x")


@dataclass
class Call:
    chat_id: int
    text: str
    button_url: str


@dataclass
class FakeSender:
    """Записывает вызовы; `errors` — что бросать на очередных вызовах (`None` — успех)."""

    errors: list[BaseException | None] = field(default_factory=list)
    delay: float = 0.0
    calls: list[Call] = field(default_factory=list)
    stop: asyncio.Event | None = None

    async def send(self, chat_id: int, text: str, button_url: str) -> None:
        self.calls.append(Call(chat_id, text, button_url))
        if self.stop is not None:
            self.stop.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error


@dataclass
class Group:
    event_id: uuid.UUID
    trip_id: uuid.UUID
    user_ids: list[uuid.UUID] = field(default_factory=list)
    telegram_ids: list[int] = field(default_factory=list)


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def group(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Group]:
    async with sessions() as session:
        admin = await make_user(session)
        event = await make_event(session, admin_id=admin.id, title=TITLE)
        trip = await make_trip(
            session,
            event_id=event.id,
            number="SU1234",
            scheduled_departure=ARRIVAL - timedelta(hours=3),
            scheduled_arrival=ARRIVAL,
        )
        await session.commit()
        created = Group(event_id=event.id, trip_id=trip.id, user_ids=[admin.id])
    try:
        yield created
    finally:
        async with sessions() as session:
            await session.execute(delete(Trip).where(Trip.event_id == created.event_id))
            await session.execute(delete(Event).where(Event.id == created.event_id))
            await session.execute(delete(User).where(User.id.in_(created.user_ids)))
            await session.commit()


async def _add(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    *,
    kind: NotificationKind = NotificationKind.TRANSFER_CHANGED,
    payload: dict[str, str] | None = None,
    run_after: datetime | None = None,
    attempts: int = 0,
    status: NotificationStatus = NotificationStatus.PENDING,
) -> int:
    async with sessions() as session:
        user = await make_user(session, first_name=SECRET_NAME)
        group.user_ids.append(user.id)
        group.telegram_ids.append(user.telegram_id)
        row = Notification(
            user_id=user.id,
            event_id=group.event_id,
            kind=kind,
            payload=payload or {"event_id": str(group.event_id)},
            dedupe_key=f"{kind.value}:{uuid.uuid4()}",
            run_after=run_after or NOW - timedelta(seconds=1),
            attempts=attempts,
            status=status,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _load(sessions: async_sessionmaker[AsyncSession], notification_id: int) -> Notification:
    async with sessions() as session:
        return await session.scalar(  # type: ignore[return-value]
            select(Notification).where(Notification.id == notification_id)
        )


def _count(kind: NotificationKind, outcome: str) -> float:
    return sample("ft_notifications_total", kind=kind.value, outcome=outcome)


async def test_pending_is_sent_with_button_and_marked_sent(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    nid = await _add(sessions, group, kind=NotificationKind.TRANSFER_ASSIGNED)
    sender = FakeSender()
    before = _count(NotificationKind.TRANSFER_ASSIGNED, "sent")

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(sent=1)
    (call,) = sender.calls
    assert call.chat_id == group.telegram_ids[0]
    assert call.text == f"Попутчики найдены — «{TITLE}». Откройте поездку, чтобы увидеть контакты."
    assert call.button_url == f"https://t.me/test_bot?startapp=event_{group.event_id}"
    row = await _load(sessions, nid)
    assert row.status == NotificationStatus.SENT
    assert row.sent_at is not None
    assert row.last_error is None
    assert _count(NotificationKind.TRANSFER_ASSIGNED, "sent") == before + 1


async def test_flight_changed_shows_number_and_local_arrival(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _add(
        sessions,
        group,
        kind=NotificationKind.FLIGHT_CHANGED,
        payload={
            "event_id": str(group.event_id),
            "trip_id": str(group.trip_id),
            "effective_arrival": ARRIVAL.isoformat(),
        },
    )
    sender = FakeSender()

    await send_due(sessions, sender, now=NOW)

    assert (
        sender.calls[0].text == f"Рейс SU1234: новое расчётное прибытие 08:05, 12.11 — «{TITLE}»."
    )


async def test_flight_text_without_trip_has_no_number_and_time(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    await _add(
        sessions,
        group,
        kind=NotificationKind.FLIGHT_CANCELLED,
        payload={"event_id": str(group.event_id), "trip_id": str(uuid.uuid4())},
    )
    sender = FakeSender()

    await send_due(sessions, sender, now=NOW)

    assert sender.calls[0].text == f"Рейс отменён — «{TITLE}». Выберите другой рейс в заявке."


async def test_forbidden_bot_is_skipped(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    nid = await _add(sessions, group)
    sender = FakeSender(errors=[TelegramForbiddenError(METHOD, "Forbidden: bot was blocked")])
    before = _count(NotificationKind.TRANSFER_CHANGED, "skipped")

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(skipped=1)
    row = await _load(sessions, nid)
    assert (row.status, row.last_error) == (NotificationStatus.SKIPPED, "TelegramForbiddenError")
    assert row.attempts == 0
    assert _count(NotificationKind.TRANSFER_CHANGED, "skipped") == before + 1


async def test_chat_not_found_is_skipped_but_other_bad_request_is_retried(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    not_found = await _add(sessions, group)
    other = await _add(sessions, group)
    sender = FakeSender(
        errors=[
            TelegramBadRequest(METHOD, "Bad Request: chat not found"),
            TelegramBadRequest(METHOD, "Bad Request: message is too long"),
        ]
    )

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(skipped=1, retried=1)
    assert (await _load(sessions, not_found)).status == NotificationStatus.SKIPPED
    retried = await _load(sessions, other)
    assert (retried.status, retried.attempts) == (NotificationStatus.PENDING, 1)


async def test_retry_after_postpones_without_attempt(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    nid = await _add(sessions, group, attempts=2)
    sender = FakeSender(errors=[TelegramRetryAfter(METHOD, "Too Many Requests", 17)])

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(retried=1)
    row = await _load(sessions, nid)
    assert row.status == NotificationStatus.PENDING
    assert row.attempts == 2
    assert row.run_after == NOW + timedelta(seconds=17)
    assert row.last_error == "TelegramRetryAfter"


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (TelegramNetworkError(METHOD, "boom"), "TelegramNetworkError"),
        (TelegramServerError(METHOD, "502"), "TelegramServerError"),
        (TimeoutError(), "TimeoutError"),
        (RuntimeError("что угодно"), "RuntimeError"),
    ],
)
async def test_network_errors_back_off_exponentially(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    error: BaseException,
    name: str,
) -> None:
    nid = await _add(sessions, group)
    sender = FakeSender(errors=[error])

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(retried=1)
    row = await _load(sessions, nid)
    assert (row.status, row.attempts, row.last_error) == (NotificationStatus.PENDING, 1, name)
    assert row.run_after == NOW + timedelta(seconds=60)  # 30 с · 2^1


@pytest.mark.parametrize(("attempts", "pause"), [(1, 120), (3, 480), (6, 3600)])
async def test_backoff_grows_and_is_capped_at_one_hour(
    sessions: async_sessionmaker[AsyncSession], group: Group, attempts: int, pause: int
) -> None:
    nid = await _add(sessions, group, attempts=attempts)
    sender = FakeSender(errors=[TelegramNetworkError(METHOD, "boom")])

    await send_due(sessions, sender, now=NOW)

    row = await _load(sessions, nid)
    assert row.attempts == attempts + 1
    assert row.run_after == NOW + timedelta(seconds=pause)


async def test_slow_telegram_times_out_and_is_retried(
    sessions: async_sessionmaker[AsyncSession], group: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notification_sender, "SEND_TIMEOUT_SECONDS", 0.05)
    nid = await _add(sessions, group)

    result = await send_due(sessions, FakeSender(delay=5), now=NOW)

    assert result == SendResult(retried=1)
    row = await _load(sessions, nid)
    assert (row.attempts, row.last_error) == (1, "TimeoutError")


async def test_eighth_failure_marks_failed_and_logs_without_personal_data(
    sessions: async_sessionmaker[AsyncSession],
    group: Group,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nid = await _add(sessions, group, attempts=7)
    sender = FakeSender(errors=[TelegramNetworkError(METHOD, f"{BOT_TOKEN} boom")])
    caplog.set_level(logging.DEBUG)
    before = _count(NotificationKind.TRANSFER_CHANGED, "failed")

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(failed=1)
    row = await _load(sessions, nid)
    assert (row.status, row.attempts, row.last_error) == (
        NotificationStatus.FAILED,
        8,
        "TelegramNetworkError",
    )
    assert _count(NotificationKind.TRANSFER_CHANGED, "failed") == before + 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].notification_id == nid  # type: ignore[attr-defined]
    dump = " ".join(f"{r.getMessage()} {r.__dict__} {r.exc_text}" for r in caplog.records)
    for secret in (
        str(group.telegram_ids[0]),
        SECRET_NAME,
        sender.calls[0].text,
        TITLE,
        BOT_TOKEN,
        "t.me",
    ):
        assert secret not in dump


async def test_only_due_pending_rows_are_taken(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    due = await _add(sessions, group)
    later = await _add(sessions, group, run_after=NOW + timedelta(seconds=1))
    done = await _add(sessions, group, status=NotificationStatus.SENT)
    skipped = await _add(sessions, group, status=NotificationStatus.SKIPPED)
    sender = FakeSender()

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(sent=1)
    assert [c.chat_id for c in sender.calls] == [group.telegram_ids[0]]
    assert (await _load(sessions, due)).status == NotificationStatus.SENT
    assert (await _load(sessions, later)).status == NotificationStatus.PENDING
    assert (await _load(sessions, done)).sent_at is None
    assert (await _load(sessions, skipped)).status == NotificationStatus.SKIPPED


async def test_oldest_run_after_goes_first_and_batch_limits(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    newer = await _add(sessions, group, run_after=NOW - timedelta(minutes=1))
    older = await _add(sessions, group, run_after=NOW - timedelta(minutes=5))
    third = await _add(sessions, group, run_after=NOW - timedelta(minutes=2))
    sender = FakeSender()

    result = await send_due(sessions, sender, now=NOW, batch=2)

    assert result == SendResult(sent=2)
    assert [c.chat_id for c in sender.calls] == [group.telegram_ids[1], group.telegram_ids[2]]
    assert (await _load(sessions, older)).status == NotificationStatus.SENT
    assert (await _load(sessions, third)).status == NotificationStatus.SENT
    assert (await _load(sessions, newer)).status == NotificationStatus.PENDING


async def test_leased_rows_are_hidden_until_lease_expires(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    nid = await _add(sessions, group)
    # воркер взял строку и «упал» до записи результата: строка арендована
    leased = await notification_sender._lease_due(sessions, NOW, 20)
    assert [item.id for item in leased] == [nid]

    sender = FakeSender()
    assert await send_due(sessions, sender, now=NOW + LEASE - timedelta(seconds=1)) == SendResult()
    assert await send_due(sessions, sender, now=NOW + LEASE) == SendResult(sent=1)


async def test_parallel_passes_never_send_one_message_twice(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    for _ in range(4):
        await _add(sessions, group)
    sender = FakeSender(delay=0.2)

    results = await wait(
        asyncio.gather(
            send_due(sessions, sender, now=NOW, batch=2),
            send_due(sessions, sender, now=NOW, batch=2),
        )
    )

    assert sorted(c.chat_id for c in sender.calls) == sorted(group.telegram_ids)
    assert [r.sent for r in results] == [2, 2]


async def test_stop_prevents_new_sends(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    ids = [await _add(sessions, group) for _ in range(3)]
    stop = asyncio.Event()
    sender = FakeSender(stop=stop)  # SIGTERM приходит во время первой отправки

    result = await send_due(sessions, sender, now=NOW, stop=stop)

    assert result == SendResult(sent=1)  # текущая отправка дошла до конца
    assert len(sender.calls) == 1
    assert (await _load(sessions, ids[0])).status == NotificationStatus.SENT
    assert (await _load(sessions, ids[1])).status == NotificationStatus.PENDING


async def test_already_stopped_takes_nothing(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    nid = await _add(sessions, group)
    stop = asyncio.Event()
    stop.set()
    sender = FakeSender()

    assert await send_due(sessions, sender, now=NOW, stop=stop) == SendResult()

    row = await _load(sessions, nid)
    assert row.status == NotificationStatus.PENDING
    assert row.run_after == NOW - timedelta(seconds=1)  # аренда не выдавалась


async def test_one_failure_does_not_stop_the_rest(
    sessions: async_sessionmaker[AsyncSession], group: Group
) -> None:
    for _ in range(3):
        await _add(sessions, group)
    sender = FakeSender(errors=[RuntimeError("x"), None, None])

    result = await send_due(sessions, sender, now=NOW)

    assert result == SendResult(sent=2, retried=1)


async def test_telegram_sender_sends_text_with_url_button() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def send_message(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    bot = FakeBot()

    await TelegramSender(bot).send(42, "текст", "https://t.me/test_bot?startapp=event_x")  # type: ignore[arg-type]

    assert bot.kwargs["chat_id"] == 42
    assert bot.kwargs["text"] == "текст"
    markup = bot.kwargs["reply_markup"]
    (button,) = markup.inline_keyboard[0]  # type: ignore[attr-defined]
    assert (button.text, button.url) == (
        "Открыть поездку",
        "https://t.me/test_bot?startapp=event_x",
    )
