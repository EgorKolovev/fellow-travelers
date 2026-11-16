"""Отметка отъезда админом и отмена «Уехал» участником в течение 30 минут (Д7, M-03)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    ApplicationStatus,
    BackgroundJob,
    Event,
    JobKind,
    Transfer,
    TransferStatus,
    User,
)
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.api.test_admin_transfers import (
    ADMIN_TELEGRAM_ID,
    BASE,
    MISSING_ID,
    app_at,
    auto_transfer,
    seed_history,
    state,
)
from tests.factories import make_event, make_membership, make_trip, make_user
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.contract import assert_contract

S = ApplicationStatus
APPLICATION_DEPARTED = "/events/{event_id}/admin/applications/{application_id}/departed"
TRANSFER_DEPARTED = "/events/{event_id}/admin/transfers/{transfer_id}/departed"
MARKS = "/events/{event_id}/my-application/marks"


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await make_user(db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan")


@pytest.fixture
async def event(db_session: AsyncSession, admin: User) -> Event:
    return await make_event(db_session, admin_id=admin.id)


async def admin_app_departed(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    telegram_id: int | None = ADMIN_TELEGRAM_ID,
) -> Response:
    response = await client.post(
        f"/events/{event_id}/admin/applications/{application_id}/departed",
        headers=auth_headers(telegram_id) if telegram_id is not None else {},
    )
    assert_contract(response, "POST", APPLICATION_DEPARTED)
    return response


async def admin_transfer_departed(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    transfer_id: uuid.UUID | str,
    telegram_id: int | None = ADMIN_TELEGRAM_ID,
) -> Response:
    response = await client.post(
        f"/events/{event_id}/admin/transfers/{transfer_id}/departed",
        headers=auth_headers(telegram_id) if telegram_id is not None else {},
    )
    assert_contract(response, "POST", TRANSFER_DEPARTED)
    return response


async def put_marks(
    client: AsyncClient,
    event_id: uuid.UUID,
    application: Application,
    session: AsyncSession,
    payload: dict[str, Any],
) -> Response:
    user = await session.get(User, application.user_id)
    assert user is not None
    response = await client.put(
        f"/events/{event_id}/my-application/marks",
        json=payload,
        headers=auth_headers(user.telegram_id),
    )
    assert_contract(response, "PUT", MARKS)
    return response


async def cancel(
    client: AsyncClient, event_id: uuid.UUID, application: Application, session: AsyncSession
) -> None:
    user = await session.get(User, application.user_id)
    assert user is not None
    response = await client.post(
        f"/events/{event_id}/my-application/cancel", headers=auth_headers(user.telegram_id)
    )
    assert response.status_code == 200


async def backdate(session: AsyncSession, application: Application, minutes: int) -> None:
    """Отметка «Уехал» была `minutes` минут назад."""
    await session.execute(
        update(Application)
        .where(Application.id == application.id)
        .values(departed_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await session.flush()


async def row(session: AsyncSession, application: Application) -> Application:
    return (
        await session.execute(
            select(Application)
            .where(Application.id == application.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def transfer_status(session: AsyncSession, transfer_id: uuid.UUID) -> TransferStatus:
    return (
        await session.execute(
            select(Transfer.status)
            .where(Transfer.id == transfer_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def pair(
    session: AsyncSession, event: Event, **kw: Any
) -> tuple[Transfer, Application, Application]:
    transfer = await auto_transfer(session, event)
    first = await app_at(session, event, transfer=transfer, **kw)
    second = await app_at(session, event, transfer=transfer)
    await seed_history(session, event, transfer)
    return transfer, first, second


# ------------------------------------------------------------------ админ: заявка


async def test_admin_marks_application_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, second = await pair(db_session, event, with_companion=True)

    response = await admin_app_departed(api_client, event.id, first.id)

    assert response.status_code == 200
    body = response.json()
    assert body["departed_at"] is not None
    assert (body["status"], body["transfer_id"], body["passengers"]) == (
        "assigned",
        str(transfer.id),
        2,
    )
    stored = await row(db_session, first)
    assert stored.departed_from_transfer_id == transfer.id
    assert (await row(db_session, second)).departed_at is None  # попутчики не отмечаются
    assert await transfer_status(db_session, transfer.id) == TransferStatus.ACTIVE
    rows = [r for r in await audit_rows(db_session, event.id) if r.action == "marks_changed"]
    assert [(r.actor_kind, r.reason, r.details) for r in rows] == [
        ("admin", "admin_edit", {"at_meeting_point": False, "departed": True})
    ]
    assert rows[0].application_id == first.id
    assert rows[0].actor_user_id == event.admin_id


async def test_admin_application_departed_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    _, first, _ = await pair(db_session, event)
    moment = (await admin_app_departed(api_client, event.id, first.id)).json()["departed_at"]

    again = await admin_app_departed(api_client, event.id, first.id)

    assert again.status_code == 200
    assert again.json()["departed_at"] == moment
    rows = [r for r in await audit_rows(db_session, event.id) if r.action == "marks_changed"]
    assert len(rows) == 1


async def test_admin_marking_last_member_departs_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, second = await pair(db_session, event)

    await admin_app_departed(api_client, event.id, first.id)
    await admin_app_departed(api_client, event.id, second.id)

    assert await transfer_status(db_session, transfer.id) == TransferStatus.DEPARTED
    actions = [(r.action, r.actor_kind) for r in await audit_rows(db_session, event.id)]
    assert ("transfer_departed", "admin") in actions
    await assert_history_matches_transfers(db_session, event.id)


async def test_admin_application_departed_cancelled_is_locked(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    cancelled = await app_at(db_session, event, status=S.CANCELLED)

    response = await admin_app_departed(api_client, event.id, cancelled.id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPLICATION_LOCKED"


async def test_admin_application_departed_access(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    target = await app_at(db_session, event)
    participant = await make_user(db_session, telegram_id=USER_TELEGRAM_ID, phone="+79990000001")
    await make_membership(db_session, event_id=event.id, user_id=participant.id)
    other_event = await make_event(db_session)
    foreign = await app_at(db_session, other_event)

    assert (await admin_app_departed(api_client, event.id, target.id, None)).status_code == 401
    assert (
        await admin_app_departed(api_client, event.id, target.id, USER_TELEGRAM_ID)
    ).status_code == 403
    assert (await admin_app_departed(api_client, event.id, foreign.id)).status_code == 404
    assert (await admin_app_departed(api_client, event.id, MISSING_ID)).status_code == 404
    await make_user(db_session, telegram_id=777)
    assert (await admin_app_departed(api_client, event.id, target.id, 777)).status_code == 404
    assert (await row(db_session, target)).departed_at is None
    assert (await row(db_session, foreign)).departed_at is None


async def test_departed_application_is_not_matched(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    departed = await app_at(db_session, event)
    await admin_app_departed(api_client, event.id, departed.id)
    newcomer = await make_user(db_session, telegram_id=USER_TELEGRAM_ID, phone="+79990000002")
    await make_membership(db_session, event_id=event.id, user_id=newcomer.id)
    trip = await make_trip(db_session, event_id=event.id, scheduled_arrival=BASE)

    response = await api_client.put(
        f"/events/{event.id}/my-application",
        json={
            "trip_id": str(trip.id),
            "with_companion": False,
            "baggage_count": 0,
            "max_wait_minutes": 15,
        },
        headers=auth_headers(USER_TELEGRAM_ID),
    )

    assert response.status_code == 201
    assert (response.json()["status"], response.json()["transfer_id"]) == ("searching", None)
    assert (await state(db_session, departed))[0] == S.SEARCHING


# ------------------------------------------------------------------ админ: трансфер


async def test_admin_marks_transfer_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, second = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    moment = (await row(db_session, first)).departed_at

    response = await admin_transfer_departed(api_client, event.id, transfer.id)

    assert response.status_code == 200
    assert response.json()["status"] == "departed"
    assert {m["application_id"] for m in response.json()["members"]} == {
        str(first.id),
        str(second.id),
    }
    assert (await row(db_session, first)).departed_at == moment  # прежняя отметка не тронута
    second_row = await row(db_session, second)
    assert second_row.departed_at is not None
    assert second_row.departed_from_transfer_id == transfer.id
    rows = await audit_rows(db_session, event.id)
    marks = [r for r in rows if r.action == "marks_changed"]
    assert sorted(str(r.application_id) for r in marks) == sorted([str(first.id), str(second.id)])
    assert {(r.actor_kind, r.reason) for r in marks} == {("admin", "admin_edit")}
    departed = [r for r in rows if r.action == "transfer_departed"]
    assert [(r.actor_kind, r.transfer_id) for r in departed] == [("admin", transfer.id)]


async def test_admin_transfer_departed_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, _ = await pair(db_session, event)
    await admin_transfer_departed(api_client, event.id, transfer.id)
    moment = (await row(db_session, first)).departed_at
    count = len(await audit_rows(db_session, event.id))

    again = await admin_transfer_departed(api_client, event.id, transfer.id)

    assert again.status_code == 200
    assert again.json()["status"] == "departed"
    assert (await row(db_session, first)).departed_at == moment
    assert len(await audit_rows(db_session, event.id)) == count


async def test_admin_transfer_departed_access(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, _, _ = await pair(db_session, event)
    participant = await make_user(db_session, telegram_id=USER_TELEGRAM_ID)
    await make_membership(db_session, event_id=event.id, user_id=participant.id)
    other_event = await make_event(db_session)
    foreign = await auto_transfer(db_session, other_event)
    await make_user(db_session, telegram_id=777)

    assert (
        await admin_transfer_departed(api_client, event.id, transfer.id, None)
    ).status_code == 401
    assert (
        await admin_transfer_departed(api_client, event.id, transfer.id, USER_TELEGRAM_ID)
    ).status_code == 403
    assert (
        await admin_transfer_departed(api_client, event.id, transfer.id, 777)
    ).status_code == 404
    assert (await admin_transfer_departed(api_client, event.id, foreign.id)).status_code == 404
    assert (await admin_transfer_departed(api_client, event.id, MISSING_ID)).status_code == 404
    assert await transfer_status(db_session, transfer.id) == TransferStatus.ACTIVE


# ------------------------------------------------------------------ отмена «Уехал»


async def test_undo_stays_in_active_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, _ = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    await backdate(db_session, first, 29)

    response = await put_marks(api_client, event.id, first, db_session, {"departed": False})

    assert response.status_code == 200
    assert response.json()["departed_at"] is None
    assert (response.json()["status"], response.json()["transfer_id"]) == (
        "assigned",
        str(transfer.id),
    )
    stored = await row(db_session, first)
    assert stored.departed_from_transfer_id is None
    assert await transfer_status(db_session, transfer.id) == TransferStatus.ACTIVE
    rows = [r for r in await audit_rows(db_session, event.id) if r.action == "marks_changed"]
    last = rows[-1]
    assert (last.actor_kind, last.reason, last.details) == (
        "participant",
        "departure_undone",
        {"at_meeting_point": False, "departed": False},
    )
    await assert_history_matches_transfers(db_session, event.id)


async def test_undo_reactivates_departed_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, second = await pair(db_session, event)
    await admin_transfer_departed(api_client, event.id, transfer.id)
    assert await transfer_status(db_session, transfer.id) == TransferStatus.DEPARTED
    await backdate(db_session, second, 5)

    response = await put_marks(api_client, event.id, second, db_session, {"departed": False})

    assert response.status_code == 200
    assert response.json()["transfer_id"] == str(transfer.id)
    assert await transfer_status(db_session, transfer.id) == TransferStatus.ACTIVE
    assert (await row(db_session, first)).departed_at is not None


async def test_undo_after_transfer_dissolved_returns_to_search_and_matches(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, second = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    await cancel(api_client, event.id, second, db_session)  # пара распалась, first стал solo
    assert (await state(db_session, first)) == (S.SOLO, None)
    partner = await app_at(db_session, event)  # ждёт попутчика
    await backdate(db_session, first, 10)

    response = await put_marks(api_client, event.id, first, db_session, {"departed": False})

    assert response.status_code == 200
    assert response.json()["departed_at"] is None
    assert response.json()["status"] == "assigned"
    new_transfer_id = response.json()["transfer_id"]
    assert new_transfer_id is not None
    assert new_transfer_id != str(transfer.id)
    assert (await state(db_session, partner))[1] == uuid.UUID(new_transfer_id)
    rows = await audit_rows(db_session, event.id)
    assert [r.action for r in rows if r.reason == "departure_undone"][:2] == [
        "marks_changed",
        "status_changed",
    ]
    assert (await row(db_session, first)).departed_from_transfer_id is None
    await assert_history_matches_transfers(db_session, event.id)


async def test_undo_after_transfer_dissolved_without_pair_stays_searching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    _, first, second = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    await cancel(api_client, event.id, second, db_session)

    response = await put_marks(api_client, event.id, first, db_session, {"departed": False})

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["transfer_id"]) == ("searching", None)


async def test_undo_solo_stays_solo(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    solo = await app_at(db_session, event, status=S.SOLO)
    await admin_app_departed(api_client, event.id, solo.id)
    assert (await row(db_session, solo)).departed_from_transfer_id is None

    response = await put_marks(api_client, event.id, solo, db_session, {"departed": False})

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["departed_at"]) == ("solo", None)
    actions = [(r.action, r.reason) for r in await audit_rows(db_session, event.id)]
    assert ("status_changed", "departure_undone") not in actions


async def test_undo_searching_runs_matching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    searching = await app_at(db_session, event)
    await admin_app_departed(api_client, event.id, searching.id)
    partner = await app_at(db_session, event)
    assert (await state(db_session, partner))[1] is None

    response = await put_marks(api_client, event.id, searching, db_session, {"departed": False})

    assert response.status_code == 200
    assert response.json()["status"] == "assigned"
    assert (await state(db_session, partner))[1] == uuid.UUID(response.json()["transfer_id"])


async def test_undo_after_window_conflicts(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, _ = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    await backdate(db_session, first, 31)

    response = await put_marks(api_client, event.id, first, db_session, {"departed": False})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_DEPARTED"
    stored = await row(db_session, first)
    assert (stored.departed_at is not None, stored.departed_from_transfer_id) == (True, transfer.id)


@pytest.mark.parametrize("flag", [True, False])
async def test_meeting_point_change_after_departure_conflicts(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, flag: bool
) -> None:
    _, first, _ = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)

    response = await put_marks(
        api_client, event.id, first, db_session, {"at_meeting_point": flag, "departed": False}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_DEPARTED"
    assert (await row(db_session, first)).departed_at is not None


async def test_participant_departed_remembers_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, _ = await pair(db_session, event)

    response = await put_marks(api_client, event.id, first, db_session, {"departed": True})

    assert response.status_code == 200
    assert (await row(db_session, first)).departed_from_transfer_id == transfer.id


async def test_undo_for_row_before_migration_returns_to_departed_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    """Строка до миграции 0007: `departed_from_transfer_id` пуст, но заявка в трансфере."""
    transfer, first, second = await pair(db_session, event)
    await admin_transfer_departed(api_client, event.id, transfer.id)
    assert await transfer_status(db_session, transfer.id) == TransferStatus.DEPARTED
    await db_session.execute(
        update(Application)
        .where(Application.id == second.id)
        .values(departed_from_transfer_id=None, departed_at=datetime.now(UTC))
    )
    await db_session.flush()

    response = await put_marks(api_client, event.id, second, db_session, {"departed": False})

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["transfer_id"]) == (
        "assigned",
        str(transfer.id),
    )
    assert await transfer_status(db_session, transfer.id) == TransferStatus.ACTIVE
    assert (await row(db_session, first)).departed_at is not None


async def test_marks_changed_records_carry_transfer_id(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    transfer, first, _ = await pair(db_session, event)
    await put_marks(api_client, event.id, first, db_session, {"departed": True})
    await put_marks(api_client, event.id, first, db_session, {"departed": False})

    rows = [r for r in await audit_rows(db_session, event.id) if r.action == "marks_changed"]

    assert [(r.details["departed"], r.transfer_id) for r in rows] == [
        (True, transfer.id),
        (False, transfer.id),
    ]


async def test_undo_back_into_transfer_enqueues_reconcile(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    """Пока заявка «уехала», рейс могли сдвинуть: после возврата в трансфер ставится пересчёт."""
    _, first, _ = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    assert await pending_jobs(db_session, event) == []

    await put_marks(api_client, event.id, first, db_session, {"departed": False})

    jobs = await pending_jobs(db_session, event)
    assert [(job.kind, job.trip_id) for job in jobs] == [(JobKind.RECONCILE_TRIP, first.trip_id)]


async def test_undo_into_new_search_does_not_enqueue_reconcile(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    _, first, second = await pair(db_session, event)
    await admin_app_departed(api_client, event.id, first.id)
    await cancel(api_client, event.id, second, db_session)

    await put_marks(api_client, event.id, first, db_session, {"departed": False})

    assert await pending_jobs(db_session, event) == []


async def pending_jobs(session: AsyncSession, event: Event) -> list[BackgroundJob]:
    return list(
        (
            await session.scalars(
                select(BackgroundJob)
                .where(BackgroundJob.event_id == event.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
