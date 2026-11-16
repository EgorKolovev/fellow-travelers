"""Сценарий атаки из предрелизной проверки (M1): участник по кругу отменяет и подаёт заявку.

Раньше каждый круг слал попутчику по два сообщения бота. Теперь события склеиваются, а за час
по группе человеку уходит не больше шести сообщений. Всё идёт через HTTP на сессиях движка с
commit: часовой лимит считается по времени БД, воркер гоняется с «часами» теста.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import Event, Notification, NotificationKind, NotificationStatus, User
from app.services.matching import lock_event
from app.services.notification_sender import SendResult, send_due
from tests.api import test_applications_concurrency as concurrency
from tests.api.conftest import auth_headers
from tests.api.test_applications_concurrency import Crowd, put_application
from tests.factories import make_application, make_event, make_membership, make_user
from tests.helpers.concurrency import client_on_engine, wait, wait_for_lock_waiter
from tests.helpers.contract import assert_contract
from tests.services.test_notification_sender import FakeSender

# фикстуры группы, сессий и клиента на движке — те же, что у тестов гонок заявок
client = concurrency.client
crowd = concurrency.crowd
sessions = concurrency.sessions

APP_PATH = "/events/{event_id}/my-application"
HOURLY_LIMIT = 6


async def cancel(client: AsyncClient, crowd: Crowd, index: int) -> None:
    response = await client.post(
        f"/events/{crowd.event_id}/my-application/cancel",
        headers=auth_headers(crowd.telegram_ids[index]),
    )
    assert_contract(response, "POST", f"{APP_PATH}/cancel")
    assert response.status_code == 200


async def rows(sessions: async_sessionmaker[AsyncSession], crowd: Crowd) -> list[Notification]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(Notification)
                    .where(Notification.event_id == crowd.event_id)
                    .order_by(Notification.id)
                )
            ).all()
        )


async def one_round(client: AsyncClient, crowd: Crowd) -> None:
    """Участник 1 отменяет заявку и подаёт снова; попутчик 0 каждый раз получает сообщения."""
    assert (await put_application(client, crowd, 1)).status_code in (200, 201)
    await cancel(client, crowd, 1)


async def test_ten_cancel_and_resubmit_cycles_leave_one_pending_row_for_the_mate(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    assert (await put_application(client, crowd, 0)).status_code in (200, 201)

    for _ in range(10):
        await one_round(client, crowd)

    found = [r for r in await rows(sessions, crowd) if r.user_id == crowd.user_ids[0]]
    assert [(r.status, r.kind) for r in found] == [
        (NotificationStatus.PENDING, NotificationKind.SEVERAL_CHANGES)
    ]
    assert found[0].payload == {"event_id": str(crowd.event_id)}


async def test_no_more_than_six_messages_per_hour_then_one_more_after_the_window(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], crowd: Crowd
) -> None:
    assert (await put_application(client, crowd, 0)).status_code in (200, 201)
    start = datetime.now(UTC)
    sender = FakeSender()
    sent = 0

    for step in range(1, 13):
        await one_round(client, crowd)
        # проход воркера каждые ~31 с «времени теста»: дольше 30 с задержки, короче часа
        result = await send_due(sessions, sender, now=start + timedelta(seconds=31 * step))
        sent += result.sent

    assert sent == HOURLY_LIMIT
    assert {call.chat_id for call in sender.calls} == {crowd.telegram_ids[0]}
    mate_rows = [r for r in await rows(sessions, crowd) if r.user_id == crowd.user_ids[0]]
    delivered = [r for r in mate_rows if r.status == NotificationStatus.SENT]
    waiting = [r for r in mate_rows if r.status == NotificationStatus.PENDING]
    assert len(delivered) == HOURLY_LIMIT
    # все события после шестого склеены в одну отложенную строку до выхода самой ранней из окна
    (held,) = waiting
    earliest = min(r.sent_at for r in delivered if r.sent_at is not None)
    assert held.run_after == earliest + timedelta(hours=1)

    # внутри часа строка не уходит, сразу после окна уходит одним сообщением
    assert await send_due(sessions, sender, now=start + timedelta(minutes=59)) == SendResult()
    after = await send_due(sessions, sender, now=held.run_after + timedelta(seconds=1))
    assert after == SendResult(sent=1)
    async with sessions() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.event_id == crowd.event_id,
                Notification.user_id == crowd.user_ids[0],
                Notification.status == NotificationStatus.SENT,
            )
        )
    assert total == HOURLY_LIMIT + 1


async def test_points_edit_waits_for_the_group_lock_and_then_notifies(
    app: FastAPI, db_engine: AsyncEngine
) -> None:
    """Правка точек берёт блокировку группы: с подбором одних и тех же строк не пересекается."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session:
        admin = await make_user(session, telegram_id=7_300_001, first_name="Admin")
        group = await make_event(session, admin_id=admin.id)
        member = await make_user(session)
        await make_membership(session, event_id=group.id, user_id=member.id)
        await make_application(session, event_id=group.id, user_id=member.id)
        await session.commit()
        event_id, admin_id, member_id = group.id, admin.id, member.id
    try:
        async with client_on_engine(app, sessions) as http, sessions() as holder:
            await lock_event(holder, event_id)  # как сценарий подбора, ещё не закоммиченный
            request = asyncio.ensure_future(
                http.patch(
                    f"/events/{event_id}",
                    json={"meeting_instruction": "Выходите к колонне 3"},
                    headers=auth_headers(7_300_001),
                )
            )
            try:
                await wait_for_lock_waiter(sessions, request)
                assert not request.done()
                await holder.commit()
                response = await wait(request)
            finally:
                if not request.done():
                    request.cancel()

        assert_contract(response, "PATCH", "/events/{event_id}")
        assert response.status_code == 200
        async with sessions() as session:
            found = (
                await session.execute(
                    select(Notification.user_id, Notification.kind).where(
                        Notification.event_id == event_id
                    )
                )
            ).all()
        assert [tuple(row) for row in found] == [(member_id, NotificationKind.POINTS_CHANGED)]
    finally:
        async with sessions() as session:
            await session.execute(delete(Event).where(Event.id == event_id))
            await session.execute(delete(User).where(User.id.in_((admin_id, member_id))))
            await session.commit()
