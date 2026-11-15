"""GET /events/{id}/admin/history: чтение истории группы (docs/contracts/README.md, §11).

Записи появляются от настоящих действий через HTTP — заявки, подбор пары, go-solo, правка рейса.
Только записи, которые не получить действиями, добавляются в таблицу напрямую.
"""

import uuid
from datetime import datetime
from typing import Any, NamedTuple

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import delete
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import ActorKind, AuditAction, AuditLog, AuditReason, Event, User
from tests.api.conftest import USER_TELEGRAM_ID, auth_headers
from tests.api.test_applications import act, apply, join
from tests.api.test_trips import patch_trip
from tests.factories import make_event, make_membership, make_trip, make_user
from tests.helpers.audit_replay import audit_rows
from tests.helpers.contract import assert_contract

MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"
HISTORY_PATH = "/events/{event_id}/admin/history"
ADMIN_TELEGRAM_ID = 500
MATE_TELEGRAM_ID = 101

ENTRY_KEYS = {
    "seq",
    "occurred_at",
    "action",
    "reason",
    "actor_kind",
    "actor",
    "subject",
    "application_id",
    "transfer_id",
    "trip_id",
    "details",
}

# что происходит в группе (см. фикстуру `group`), от новых записей к старым
GROUP_ACTIONS_NEWEST_FIRST = [
    "trip_updated",
    "transfer_dissolved",
    "transfer_left",
    "transfer_left",
    "transfer_created",
    "application_submitted",
    "application_submitted",
]


async def get_history(
    client: AsyncClient,
    event_id: uuid.UUID | str,
    telegram_id: int = ADMIN_TELEGRAM_ID,
    params: Any = None,
) -> Response:
    response = await client.get(
        f"/events/{event_id}/admin/history", params=params, headers=auth_headers(telegram_id)
    )
    assert_contract(response, "GET", HISTORY_PATH)
    return response


def items_of(response: Response) -> list[dict[str, Any]]:
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def person(user: User) -> dict[str, Any]:
    return {"user_id": str(user.id), "first_name": user.first_name, "last_name": user.last_name}


def only(items: list[dict[str, Any]], action: str) -> dict[str, Any]:
    (found,) = (item for item in items if item["action"] == action)
    return found


def add_record(session: AsyncSession, event: Event, **fields: Any) -> AuditLog:
    """Запись истории напрямую: для случаев, которые действиями через API не получить."""
    fields.setdefault("action", AuditAction.TRANSFER_DEPARTED)
    fields.setdefault("actor_kind", ActorKind.SYSTEM)
    record = AuditLog(event_id=event.id, **fields)
    session.add(record)
    return record


class Group(NamedTuple):
    member: User
    mate: User
    trip_id: uuid.UUID


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await make_user(
        db_session, telegram_id=ADMIN_TELEGRAM_ID, first_name="Ivan", phone="+79990000500"
    )


@pytest.fixture
async def event(db_session: AsyncSession, admin: User) -> Event:
    return await make_event(db_session, admin_id=admin.id)


@pytest.fixture
async def member(db_session: AsyncSession, event: Event, user: User) -> User:
    """`user` (telegram 42) — участник группы без прав админа."""
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    return user


@pytest.fixture
async def mate(db_session: AsyncSession, event: Event) -> User:
    return await join(db_session, event, MATE_TELEGRAM_ID)


@pytest.fixture
async def group(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, member: User, mate: User
) -> Group:
    """Семь записей: две заявки, пара, выход одного через go-solo, расформирование, правка рейса."""
    await apply(api_client, db_session, event, MATE_TELEGRAM_ID)
    await apply(api_client, db_session, event, USER_TELEGRAM_ID, minutes=5)
    await act(api_client, db_session, event, "go-solo")
    trip = await make_trip(db_session, event_id=event.id)
    response = await patch_trip(
        api_client, trip.id, {"estimated_arrival": "2026-11-12T09:15:00"}, ADMIN_TELEGRAM_ID
    )
    assert response.status_code == 200
    # initData перезаписывает имя и username при каждом запросе: задаём свои после действий
    member.first_name, member.last_name = "Anna", "Smirnova"
    member.username = "anna_login"
    mate.first_name, mate.last_name = "Boris", None
    mate.username = "boris_login"
    await db_session.flush()
    return Group(member=member, mate=mate, trip_id=trip.id)


# ---------------------------------------------------------------- доступ


async def test_requires_auth(api_client: AsyncClient) -> None:
    response = await api_client.get(HISTORY_PATH.format(event_id=MISSING_ID))

    assert response.status_code == 401
    assert_contract(response, "GET", HISTORY_PATH)


async def test_stranger_gets_404(api_client: AsyncClient, event: Event, other_user: User) -> None:
    response = await get_history(api_client, event.id, 43)

    assert response.status_code == 404


async def test_unknown_event_gets_404(api_client: AsyncClient, admin: User) -> None:
    response = await get_history(api_client, MISSING_ID)

    assert response.status_code == 404


async def test_member_gets_403(api_client: AsyncClient, event: Event, member: User) -> None:
    response = await get_history(api_client, event.id, USER_TELEGRAM_ID)

    assert response.status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": "many"},
        {"before_seq": 0},
        {"before_seq": -5},
        {"before_seq": "last"},
    ],
)
async def test_invalid_params_are_422(
    api_client: AsyncClient, event: Event, params: dict[str, Any]
) -> None:
    response = await get_history(api_client, event.id, params=params)

    assert response.status_code == 422


# ---------------------------------------------------------------- содержимое


async def test_empty_history(api_client: AsyncClient, event: Event) -> None:
    response = await get_history(api_client, event.id)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_before_seq": None}


async def test_admin_sees_entries_newest_first(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    response = await get_history(api_client, event.id)

    items = items_of(response)
    assert [item["action"] for item in items] == GROUP_ACTIONS_NEWEST_FIRST
    seqs = [item["seq"] for item in items]
    assert seqs == sorted(seqs, reverse=True)
    assert len(set(seqs)) == len(seqs)
    assert response.json()["next_before_seq"] is None
    for item in items:
        assert set(item) == ENTRY_KEYS
        # момент времени со смещением, а не наивный
        assert datetime.fromisoformat(item["occurred_at"]).utcoffset() is not None


async def test_transfer_left_shows_actor_and_subject(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    items = items_of(await get_history(api_client, event.id))

    # новые записи первыми: сначала вывод оставшегося, потом выход самого участника
    remaining_exit, own_exit = (i for i in items if i["action"] == "transfer_left")
    # выходит сам участник: автор и субъект — один человек
    assert own_exit["reason"] == "go_solo"
    assert own_exit["actor_kind"] == "participant"
    assert own_exit["actor"] == person(group.member)
    assert own_exit["actor"]["first_name"] == "Anna"
    assert own_exit["actor"]["last_name"] == "Smirnova"
    assert own_exit["subject"] == person(group.member)
    assert own_exit["details"] == {"from_status": "assigned", "to_status": "solo"}
    assert own_exit["transfer_id"] is not None
    assert own_exit["application_id"] is not None
    # оставшегося выводит тот же участник: автор — он, субъект — оставшийся
    assert remaining_exit["reason"] == "member_left"
    assert remaining_exit["actor"] == person(group.member)
    assert remaining_exit["subject"] == person(group.mate)
    assert remaining_exit["subject"]["last_name"] is None
    assert remaining_exit["transfer_id"] == own_exit["transfer_id"]
    assert remaining_exit["application_id"] != own_exit["application_id"]


async def test_system_entry_has_no_actor(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    items = items_of(await get_history(api_client, event.id))

    created = only(items, "transfer_created")
    assert created["actor_kind"] == "system"
    assert created["actor"] is None
    assert created["subject"] is None
    assert created["reason"] == "auto_match"
    assert len(created["details"]["application_ids"]) == 2


async def test_trip_updated_has_no_subject(
    api_client: AsyncClient, event: Event, group: Group, admin: User
) -> None:
    items = items_of(await get_history(api_client, event.id))

    updated = only(items, "trip_updated")
    assert updated["subject"] is None
    assert updated["actor_kind"] == "admin"
    assert updated["actor"] == person(admin)
    assert updated["reason"] == "admin_edit"
    assert updated["trip_id"] == str(group.trip_id)
    assert (updated["application_id"], updated["transfer_id"]) == (None, None)
    assert updated["details"]["changed_fields"] == ["estimated_arrival"]


async def test_dissolved_transfer_has_actor_but_no_subject(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    items = items_of(await get_history(api_client, event.id))

    dissolved = only(items, "transfer_dissolved")
    assert dissolved["actor"] == person(group.member)
    assert dissolved["subject"] is None
    assert dissolved["reason"] == "member_left"


async def test_deleted_profiles_give_null_persons(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    gone = await make_user(db_session, first_name="Gone")
    add_record(
        db_session,
        event,
        action=AuditAction.STATUS_CHANGED,
        reason=AuditReason.GO_SOLO,
        actor_kind=ActorKind.PARTICIPANT,
        actor_user_id=gone.id,
        subject_user_id=gone.id,
    )
    await db_session.flush()
    # FK ON DELETE SET NULL: запись остаётся, пользователь в ней пропадает
    await db_session.execute(delete(User).where(User.id == gone.id))

    (entry,) = items_of(await get_history(api_client, event.id))

    assert entry["actor_kind"] == "participant"
    assert entry["actor"] is None
    assert entry["subject"] is None


async def test_no_phone_or_username_in_response(
    api_client: AsyncClient, event: Event, group: Group, admin: User
) -> None:
    response = await get_history(api_client, event.id)

    def walk(node: Any) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {key for value in node.values() for key in walk(value)}
        if isinstance(node, list):
            return {key for value in node for key in walk(value)}
        return set()

    assert not walk(response.json()) & {"phone", "username", "photo_url"}
    for entry in response.json()["items"]:
        for who in (entry["actor"], entry["subject"]):
            assert who is None or set(who) == {"user_id", "first_name", "last_name"}
    for someone in (group.member, group.mate, admin):
        for secret in (someone.phone, someone.username):
            assert secret
            assert secret not in response.text


async def test_other_group_entries_hidden(
    api_client: AsyncClient, db_session: AsyncSession, event: Event, group: Group
) -> None:
    before = (await get_history(api_client, event.id)).json()
    other = await make_event(db_session)
    await join(db_session, other, 103)

    await apply(api_client, db_session, other, 103)

    assert len(await audit_rows(db_session, other.id)) == 1
    assert (await get_history(api_client, event.id)).json() == before


# ---------------------------------------------------------------- страницы


async def test_pages_follow_each_other_without_overlap(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    full = items_of(await get_history(api_client, event.id))
    seen: list[dict[str, Any]] = []
    sizes: list[int] = []
    params: dict[str, Any] = {"limit": 2}

    for _ in range(len(full)):
        page = (await get_history(api_client, event.id, params=params)).json()
        seen += page["items"]
        sizes.append(len(page["items"]))
        if page["next_before_seq"] is None:
            break
        assert page["next_before_seq"] == page["items"][-1]["seq"]
        params = {"limit": 2, "before_seq": page["next_before_seq"]}

    # последняя страница неполная, а next_before_seq на ней null (цикл вышел по break)
    assert sizes == [2, 2, 2, 1]
    assert page["next_before_seq"] is None
    assert seen == full


async def test_next_before_seq_only_when_older_entries_exist(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    total = len(GROUP_ACTIONS_NEWEST_FIRST)

    exact = (await get_history(api_client, event.id, params={"limit": total})).json()
    short = (await get_history(api_client, event.id, params={"limit": total - 1})).json()

    assert len(exact["items"]) == total
    assert exact["next_before_seq"] is None
    assert len(short["items"]) == total - 1
    assert short["next_before_seq"] == short["items"][-1]["seq"]
    last = (
        await get_history(api_client, event.id, params={"before_seq": short["next_before_seq"]})
    ).json()
    assert len(last["items"]) == 1
    assert last["next_before_seq"] is None


async def test_before_seq_is_exclusive_and_oldest_has_nothing_older(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    full = items_of(await get_history(api_client, event.id))

    below_newest = await get_history(api_client, event.id, params={"before_seq": full[0]["seq"]})
    below_oldest = await get_history(api_client, event.id, params={"before_seq": full[-1]["seq"]})

    assert items_of(below_newest) == full[1:]
    assert below_oldest.json() == {"items": [], "next_before_seq": None}


async def test_before_seq_beyond_bigint_is_not_an_error(
    api_client: AsyncClient, event: Event, group: Group
) -> None:
    full = items_of(await get_history(api_client, event.id))

    response = await get_history(api_client, event.id, params={"before_seq": 2**64})

    assert items_of(response) == full


async def test_default_page_is_100_and_limit_200_takes_more(
    api_client: AsyncClient, db_session: AsyncSession, event: Event
) -> None:
    for _ in range(101):
        add_record(db_session, event)
    await db_session.flush()

    default = (await get_history(api_client, event.id)).json()
    widest = (await get_history(api_client, event.id, params={"limit": 200})).json()

    assert len(default["items"]) == 100
    assert default["next_before_seq"] == default["items"][-1]["seq"]
    assert len(widest["items"]) == 101
    assert widest["next_before_seq"] is None


# ---------------------------------------------------------------- без N+1


async def _count_queries(engine: AsyncEngine, client: AsyncClient, event: Event) -> int:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa_event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(
            f"/events/{event.id}/admin/history", headers=auth_headers(ADMIN_TELEGRAM_ID)
        )
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200
    return len(statements)


async def _group_with_records(session: AsyncSession, admin: User, size: int) -> Event:
    group = await make_event(session, admin_id=admin.id)
    for _ in range(size):
        actor = await make_user(session)
        subject = await make_user(session)
        add_record(
            session,
            group,
            action=AuditAction.TRANSFER_LEFT,
            actor_kind=ActorKind.PARTICIPANT,
            actor_user_id=actor.id,
            subject_user_id=subject.id,
        )
    await session.flush()
    return group


async def test_query_count_does_not_grow(
    api_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine, admin: User
) -> None:
    small = await _group_with_records(db_session, admin, 2)
    large = await _group_with_records(db_session, admin, 30)

    # прогрев: первый запрос админа ещё обновляет его запись при аутентификации
    await _count_queries(db_engine, api_client, small)
    few = await _count_queries(db_engine, api_client, small)
    many = await _count_queries(db_engine, api_client, large)

    assert few == many
