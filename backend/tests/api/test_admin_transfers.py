"""POST/PUT ручного состава трансфера (CAP-06, CAP-11): docs/spec-mvp.md, M-03 и Д6."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    Event,
    Transfer,
    TransferOrigin,
    TransferStatus,
    Trip,
    TripStatus,
    User,
)
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.factories import (
    get_location,
    make_application,
    make_audit_log,
    make_event,
    make_membership,
    make_transfer,
    make_trip,
    make_user,
)
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.contract import assert_contract

ADMIN_TELEGRAM_ID = 500
BASE = datetime(2026, 11, 12, 5, 5, tzinfo=UTC)
COLLECTION = "/events/{event_id}/admin/transfers"
MEMBERS = "/events/{event_id}/admin/transfers/{transfer_id}/members"
MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"

S = ApplicationStatus


async def post_transfer(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    ids: list[Any],
    telegram_id: int | None = ADMIN_TELEGRAM_ID,
) -> Response:
    response = await client.post(
        f"/events/{event_id}/admin/transfers",
        json={"application_ids": [str(i) for i in ids]},
        headers=auth_headers(telegram_id) if telegram_id is not None else {},
    )
    assert_contract(response, "POST", COLLECTION)
    return response


async def put_members(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    transfer_id: uuid.UUID | str,
    ids: list[Any],
    telegram_id: int | None = ADMIN_TELEGRAM_ID,
) -> Response:
    response = await client.put(
        f"/events/{event_id}/admin/transfers/{transfer_id}/members",
        json={"application_ids": [str(i) for i in ids]},
        headers=auth_headers(telegram_id) if telegram_id is not None else {},
    )
    assert_contract(response, "PUT", MEMBERS)
    return response


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await make_user(db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan")


@pytest.fixture
async def event(db_session: AsyncSession, admin: User) -> Event:
    return await make_event(db_session, admin_id=admin.id)


async def app_at(
    session: AsyncSession,
    event: Event,
    minutes: int = 0,
    *,
    transfer: Transfer | None = None,
    airport: str | None = None,
    **kw: Any,
) -> Application:
    """Заявка участника группы с рейсом, прилетающим в BASE + minutes; baggage_count=0."""
    user = await make_user(session)
    await make_membership(session, event_id=event.id, user_id=user.id)
    trip_kw: dict[str, Any] = {}
    if airport is not None:
        trip_kw["arrival_location_id"] = (await get_location(session, airport)).id
    trip = await make_trip(
        session,
        event_id=event.id,
        scheduled_arrival=BASE + timedelta(minutes=minutes),
        **trip_kw,
    )
    kw.setdefault("baggage_count", 0)
    if transfer is None:
        kw.setdefault("status", S.SEARCHING)
    return await make_application(
        session,
        event_id=event.id,
        user_id=user.id,
        trip_id=trip.id,
        transfer_id=transfer.id if transfer is not None else None,
        **kw,
    )


async def auto_transfer(session: AsyncSession, event: Event) -> Transfer:
    return await make_transfer(session, event_id=event.id)


async def seed_history(session: AsyncSession, event: Event, *transfers: Transfer) -> None:
    """`transfer_created` для трансферов, собранных фабрикой: без него состав не восстановить."""
    for transfer in transfers:
        ids = (
            await session.scalars(
                select(Application.id).where(Application.transfer_id == transfer.id)
            )
        ).all()
        await make_audit_log(
            session,
            event_id=event.id,
            action=AuditAction.TRANSFER_CREATED,
            transfer_id=transfer.id,
            details={"application_ids": [str(i) for i in ids]},
        )


async def state(session: AsyncSession, application: Application) -> tuple[ApplicationStatus, Any]:
    row = (
        await session.execute(
            select(Application.status, Application.transfer_id)
            .where(Application.id == application.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return row[0], row[1]


async def transfer_origin(session: AsyncSession, transfer_id: uuid.UUID | str) -> Any:
    return (
        await session.execute(
            select(Transfer.origin)
            .where(Transfer.id == uuid.UUID(str(transfer_id)))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def member_ids(response: Response) -> set[str]:
    return {m["application_id"] for m in response.json()["members"]}


def admin_rows(rows: list[AuditLog], action: str) -> list[AuditLog]:
    return [r for r in rows if r.action == action]


# ------------------------------------------------------------------ POST: успех


async def test_post_builds_manual_transfer_from_two_searching(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, admin: User
) -> None:
    a = await app_at(db_session, event, 0, baggage_count=1)
    b = await app_at(db_session, event, 10, baggage_count=1, with_companion=True)

    response = await post_transfer(api_client, event.id, [a.id, b.id])

    assert response.status_code == 201
    body = response.json()
    assert (body["status"], body["passengers_total"], body["baggage_total"]) == ("active", 3, 2)
    assert member_ids(response) == {str(a.id), str(b.id)}
    assert await transfer_origin(db_session, body["id"]) == TransferOrigin.ADMIN
    assert (await state(db_session, a))[0] == S.ASSIGNED
    rows = await audit_rows(db_session, event.id)
    (created,) = admin_rows(rows, "transfer_created")
    assert (created.actor_kind, created.actor_user_id, created.reason) == (
        "admin",
        admin.id,
        "admin_edit",
    )
    assert created.details == {"application_ids": [str(a.id), str(b.id)]}
    assert str(created.transfer_id) == body["id"]
    await assert_history_matches_transfers(db_session, event.id)


async def test_post_takes_application_out_of_triple_and_fills_old_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    old = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=old)
    b = await app_at(db_session, event, 5, transfer=old)
    c = await app_at(db_session, event, 10, transfer=old)
    d = await app_at(db_session, event, 3)
    free = await app_at(db_session, event, 8)

    await seed_history(db_session, event, old)
    response = await post_transfer(api_client, event.id, [a.id, d.id])

    assert response.status_code == 201
    new_id = response.json()["id"]
    assert (await state(db_session, a))[1] == uuid.UUID(new_id)
    assert (await state(db_session, b))[1] == old.id
    assert (await state(db_session, c))[1] == old.id
    # место в прежнем трансфере заняла подходящая заявка из поиска; ручной трансфер не дополнен
    assert await state(db_session, free) == (S.ASSIGNED, old.id)
    assert member_ids(response) == {str(a.id), str(d.id)}
    await assert_history_matches_transfers(db_session, event.id)


async def test_post_from_pair_dissolves_old_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, admin: User
) -> None:
    old = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=old)
    b = await app_at(db_session, event, 5, transfer=old)
    d = await app_at(db_session, event, 3)

    await seed_history(db_session, event, old)
    response = await post_transfer(api_client, event.id, [a.id, d.id])

    assert response.status_code == 201
    assert await state(db_session, b) == (S.SEARCHING, None)
    assert await transfer_origin(db_session, old.id) is None
    rows = await audit_rows(db_session, event.id)
    (left,) = [r for r in admin_rows(rows, "transfer_left") if r.application_id == a.id]
    assert (left.actor_kind, left.actor_user_id, left.reason) == ("admin", admin.id, "admin_edit")
    assert len(admin_rows(rows, "transfer_dissolved")) == 1
    await assert_history_matches_transfers(db_session, event.id)


async def test_post_from_same_pair_keeps_pair_together(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    old = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=old)
    b = await app_at(db_session, event, 5, transfer=old)

    await seed_history(db_session, event, old)
    response = await post_transfer(api_client, event.id, [a.id, b.id])

    assert response.status_code == 201
    assert member_ids(response) == {str(a.id), str(b.id)}
    await assert_history_matches_transfers(db_session, event.id)


async def test_manual_transfer_is_not_filled_by_new_application(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5)
    newcomer = await app_at(db_session, event, 7)

    response = await post_transfer(api_client, event.id, [a.id, b.id])

    assert response.status_code == 201
    assert await state(db_session, newcomer) == (S.SEARCHING, None)


# ------------------------------------------------------------------ POST: ошибки


async def error_of(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def snapshot_db(session: AsyncSession, event: Event) -> tuple[Any, Any]:
    apps = (
        await session.execute(
            select(Application.id, Application.status, Application.transfer_id)
            .where(Application.event_id == event.id)
            .order_by(Application.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    transfers = (
        await session.execute(
            select(Transfer.id, Transfer.origin, Transfer.status)
            .where(Transfer.event_id == event.id)
            .order_by(Transfer.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [tuple(r) for r in apps], [tuple(r) for r in transfers]


async def assert_nothing_changed(
    session: AsyncSession, event: Event, before: tuple[Any, Any], history_len: int
) -> None:
    assert await snapshot_db(session, event) == before
    assert len(await audit_rows(session, event.id)) == history_len


async def test_post_fewer_than_two_is_422(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    for ids in ([], [a.id]):
        response = await post_transfer(api_client, event.id, ids)
        assert await error_of(response) == (422, "VALIDATION_ERROR")
        assert "application_ids" in response.json()["error"]["details"]["fields"]


async def test_post_duplicate_id_is_422(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    response = await post_transfer(api_client, event.id, [a.id, a.id])
    assert await error_of(response) == (422, "VALIDATION_ERROR")
    assert "application_ids" in response.json()["error"]["details"]["fields"]


async def test_post_unknown_or_foreign_application_is_422(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    other_event = await make_event(db_session)
    foreign = await app_at(db_session, other_event, 0)
    for other in (uuid.UUID(MISSING_ID), foreign.id):
        response = await post_transfer(api_client, event.id, [a.id, other])
        assert await error_of(response) == (422, "VALIDATION_ERROR")
        assert "application_ids" in response.json()["error"]["details"]["fields"]


@pytest.mark.parametrize("status", [S.SOLO, S.CANCELLED])
async def test_post_solo_or_cancelled_is_422(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, status: ApplicationStatus
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5, status=status)
    response = await post_transfer(api_client, event.id, [a.id, b.id])
    assert await error_of(response) == (422, "VALIDATION_ERROR")
    assert "application_ids" in response.json()["error"]["details"]["fields"]


async def test_post_departed_application_is_409_already_departed(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5, departed_at=BASE)
    response = await post_transfer(api_client, event.id, [a.id, b.id])
    assert await error_of(response) == (409, "ALREADY_DEPARTED")


async def test_post_too_many_passengers_is_409_capacity(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0, with_companion=True)
    b = await app_at(db_session, event, 5, with_companion=True)
    response = await post_transfer(api_client, event.id, [a.id, b.id])
    assert await error_of(response) == (409, "CAPACITY_EXCEEDED")


async def test_post_too_much_baggage_is_409_capacity(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    apps = [await app_at(db_session, event, i, baggage_count=1) for i in (0, 2, 4)]
    response = await post_transfer(api_client, event.id, [a.id for a in apps])
    assert await error_of(response) == (409, "CAPACITY_EXCEEDED")


async def test_post_different_airports_is_409_incompatible(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5, airport="DME")
    response = await post_transfer(api_client, event.id, [a.id, b.id])
    assert await error_of(response) == (409, "INCOMPATIBLE_TRANSFER")


async def test_post_wait_too_long_is_409_incompatible(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)  # ждёт 15 минут
    b = await app_at(db_session, event, 60)
    response = await post_transfer(api_client, event.id, [a.id, b.id])
    assert await error_of(response) == (409, "INCOMPATIBLE_TRANSFER")


async def test_post_error_priority(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    # 422 важнее 409; ALREADY_DEPARTED важнее CAPACITY; CAPACITY важнее INCOMPATIBLE
    solo = await app_at(db_session, event, 0, status=S.SOLO)
    departed = await app_at(db_session, event, 0, departed_at=BASE, with_companion=True)
    big_far = await app_at(db_session, event, 90, with_companion=True, airport="DME")
    big = await app_at(db_session, event, 0, with_companion=True)

    r = await post_transfer(api_client, event.id, [solo.id, departed.id])
    assert await error_of(r) == (422, "VALIDATION_ERROR")
    r = await post_transfer(api_client, event.id, [departed.id, big_far.id])
    assert await error_of(r) == (409, "ALREADY_DEPARTED")
    r = await post_transfer(api_client, event.id, [big.id, big_far.id])
    assert await error_of(r) == (409, "CAPACITY_EXCEEDED")


async def test_post_error_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    old = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=old)
    await app_at(db_session, event, 5, transfer=old)
    far = await app_at(db_session, event, 90)
    before = await snapshot_db(db_session, event)
    history_len = len(await audit_rows(db_session, event.id))

    response = await post_transfer(api_client, event.id, [a.id, far.id])

    assert await error_of(response) == (409, "INCOMPATIBLE_TRANSFER")
    await assert_nothing_changed(db_session, event, before, history_len)


async def test_post_access(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5)
    ids = [a.id, b.id]
    assert (await post_transfer(api_client, event.id, ids, None)).status_code == 401
    # посторонний — 404
    assert (await post_transfer(api_client, event.id, ids, USER_TELEGRAM_ID)).status_code == 404
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    # участник — 403
    assert (await post_transfer(api_client, event.id, ids, USER_TELEGRAM_ID)).status_code == 403
    assert await snapshot_db(db_session, event) == (
        [(x.id, S.SEARCHING, None) for x in sorted((a, b), key=lambda x: x.id)],
        [],
    )


# ------------------------------------------------------------------ PUT: успех


async def test_put_replaces_one_member(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, admin: User
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 5, transfer=t)
    c = await app_at(db_session, event, 3)

    await seed_history(db_session, event, t)
    response = await put_members(api_client, event.id, t.id, [a.id, c.id])

    assert response.status_code == 200
    assert response.json()["id"] == str(t.id)
    assert member_ids(response) == {str(a.id), str(c.id)}
    assert await state(db_session, b) == (S.SEARCHING, None)
    assert await transfer_origin(db_session, t.id) == TransferOrigin.ADMIN
    rows = await audit_rows(db_session, event.id)
    (left,) = admin_rows(rows, "transfer_left")
    (joined,) = admin_rows(rows, "transfer_joined")
    for row, app in ((left, b), (joined, c)):
        assert (row.actor_kind, row.actor_user_id, row.reason) == ("admin", admin.id, "admin_edit")
        assert row.application_id == app.id
        assert row.transfer_id == t.id
    await assert_history_matches_transfers(db_session, event.id)


async def test_put_adds_third_member(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 5, transfer=t)
    c = await app_at(db_session, event, 8)

    await seed_history(db_session, event, t)
    response = await put_members(api_client, event.id, t.id, [a.id, b.id, c.id])

    assert response.status_code == 200
    assert member_ids(response) == {str(a.id), str(b.id), str(c.id)}
    assert response.json()["passengers_total"] == 3
    await assert_history_matches_transfers(db_session, event.id)


async def test_put_takes_member_from_other_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    other = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 5, transfer=t)
    x = await app_at(db_session, event, 2, transfer=other)
    y = await app_at(db_session, event, 4, transfer=other)

    await seed_history(db_session, event, t, other)
    response = await put_members(api_client, event.id, t.id, [a.id, b.id, x.id])

    assert response.status_code == 200
    assert member_ids(response) == {str(a.id), str(b.id), str(x.id)}
    # в прежнем трансфере остался один: он распущен, остаток снова в поиске
    assert await state(db_session, y) == (S.SEARCHING, None)
    assert await transfer_origin(db_session, other.id) is None
    await assert_history_matches_transfers(db_session, event.id)


async def test_put_removed_application_is_matched_again(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 5, transfer=t)
    c = await app_at(db_session, event, 3)
    e = await app_at(db_session, event, 6)  # в поиске, подходит убранной b

    await seed_history(db_session, event, t)
    response = await put_members(api_client, event.id, t.id, [a.id, c.id])

    assert response.status_code == 200
    new_b = await state(db_session, b)
    new_e = await state(db_session, e)
    assert new_b[0] == S.ASSIGNED
    assert new_b == new_e
    assert new_b[1] != t.id
    # новая пара автоматическая, а ручной трансфер не дополнен
    assert await transfer_origin(db_session, new_b[1]) == TransferOrigin.AUTO
    assert member_ids(response) == {str(a.id), str(c.id)}
    await assert_history_matches_transfers(db_session, event.id)


async def test_put_empty_list_dissolves_transfer(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 40, transfer=t, max_wait_minutes=60)

    await seed_history(db_session, event, t)
    response = await put_members(api_client, event.id, t.id, [])

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["members"], body["passengers_total"]) == (str(t.id), [], 0)
    assert (await state(db_session, a))[1] != t.id
    assert (await state(db_session, b))[1] != t.id
    assert await transfer_origin(db_session, t.id) is None
    rows = await audit_rows(db_session, event.id)
    assert len(admin_rows(rows, "transfer_dissolved")) == 1
    await assert_history_matches_transfers(db_session, event.id)

    again = await put_members(api_client, event.id, t.id, [])
    assert again.status_code == 404


async def test_put_keeps_departed_member_but_cannot_remove_them(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t, departed_at=BASE)
    b = await app_at(db_session, event, 5, transfer=t)
    c = await app_at(db_session, event, 8, transfer=t)

    removing = await put_members(api_client, event.id, t.id, [b.id, c.id])
    assert await error_of(removing) == (409, "ALREADY_DEPARTED")

    keeping = await put_members(api_client, event.id, t.id, [a.id, b.id])
    assert keeping.status_code == 200
    assert member_ids(keeping) == {str(a.id), str(b.id)}
    assert await state(db_session, c) == (S.SEARCHING, None)


# ------------------------------------------------------------------ PUT: ошибки


async def test_put_departed_transfer_is_409(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await make_transfer(db_session, event_id=event.id, status=TransferStatus.DEPARTED)
    a = await app_at(db_session, event, 0, transfer=t, departed_at=BASE)
    b = await app_at(db_session, event, 5, transfer=t, departed_at=BASE)
    response = await put_members(api_client, event.id, t.id, [a.id, b.id])
    assert await error_of(response) == (409, "ALREADY_DEPARTED")


async def test_put_single_application_is_422(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    await app_at(db_session, event, 5, transfer=t)
    response = await put_members(api_client, event.id, t.id, [a.id])
    assert await error_of(response) == (422, "VALIDATION_ERROR")
    assert "application_ids" in response.json()["error"]["details"]["fields"]


async def test_put_unknown_transfer_and_other_group_transfer_are_404(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5)
    other_event = await make_event(db_session)
    foreign = await make_transfer(db_session, event_id=other_event.id)
    for transfer_id in (MISSING_ID, foreign.id):
        response = await put_members(api_client, event.id, transfer_id, [a.id, b.id])
        assert await error_of(response) == (404, "NOT_FOUND")


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("foreign", (422, "VALIDATION_ERROR")),
        ("solo", (422, "VALIDATION_ERROR")),
        ("dup", (422, "VALIDATION_ERROR")),
        ("departed", (409, "ALREADY_DEPARTED")),
        ("capacity", (409, "CAPACITY_EXCEEDED")),
        ("incompatible", (409, "INCOMPATIBLE_TRANSFER")),
    ],
)
async def test_put_errors_change_nothing(
    api_client: AsyncClient,
    db_session: AsyncSession,
    event: Event,
    case: str,
    expected: tuple[int, str],
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    await app_at(db_session, event, 5, transfer=t)
    ids: list[uuid.UUID]
    match case:
        case "foreign":
            other_event = await make_event(db_session)
            ids = [a.id, (await app_at(db_session, other_event, 0)).id]
        case "solo":
            ids = [a.id, (await app_at(db_session, event, 0, status=S.SOLO)).id]
        case "dup":
            ids = [a.id, a.id]
        case "departed":
            ids = [a.id, (await app_at(db_session, event, 0, departed_at=BASE)).id]
        case "capacity":
            ids = [
                (await app_at(db_session, event, 0, with_companion=True)).id,
                (await app_at(db_session, event, 0, with_companion=True)).id,
            ]
        case _:
            ids = [a.id, (await app_at(db_session, event, 90)).id]
    before = await snapshot_db(db_session, event)
    history_len = len(await audit_rows(db_session, event.id))

    response = await put_members(api_client, event.id, t.id, ids)

    assert await error_of(response) == expected
    await assert_nothing_changed(db_session, event, before, history_len)


async def test_put_access(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, user: User
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    b = await app_at(db_session, event, 5, transfer=t)
    ids = [a.id, b.id]
    assert (await put_members(api_client, event.id, t.id, ids, None)).status_code == 401
    assert (await put_members(api_client, event.id, t.id, ids, USER_TELEGRAM_ID)).status_code == 404
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    assert (await put_members(api_client, event.id, t.id, ids, USER_TELEGRAM_ID)).status_code == 403


async def cancel_trip_of(session: AsyncSession, application: Application) -> None:
    trip = await session.get(Trip, application.trip_id)
    assert trip is not None
    trip.status = TripStatus.CANCELLED
    await session.flush()


async def test_post_cancelled_flight_is_409_incompatible(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    a = await app_at(db_session, event, 0)
    b = await app_at(db_session, event, 5)
    await cancel_trip_of(db_session, b)
    before = await snapshot_db(db_session, event)
    history_len = len(await audit_rows(db_session, event.id))

    response = await post_transfer(api_client, event.id, [a.id, b.id])

    assert await error_of(response) == (409, "INCOMPATIBLE_TRANSFER")
    assert "отменён" in response.json()["error"]["message"]
    await assert_nothing_changed(db_session, event, before, history_len)


async def test_put_cancelled_flight_is_409_incompatible(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    t = await auto_transfer(db_session, event)
    a = await app_at(db_session, event, 0, transfer=t)
    await app_at(db_session, event, 5, transfer=t)
    c = await app_at(db_session, event, 3)
    await cancel_trip_of(db_session, c)
    before = await snapshot_db(db_session, event)
    history_len = len(await audit_rows(db_session, event.id))

    response = await put_members(api_client, event.id, t.id, [a.id, c.id])

    assert await error_of(response) == (409, "INCOMPATIBLE_TRANSFER")
    assert "отменён" in response.json()["error"]["message"]
    await assert_nothing_changed(db_session, event, before, history_len)
