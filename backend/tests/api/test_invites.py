import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, User
from tests.api.conftest import auth_headers
from tests.factories import make_event, make_membership, make_user
from tests.helpers.contract import assert_contract

CODE = "ABC234"


async def count_memberships(session: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.event_id == event_id, Membership.user_id == user_id)
    )
    return int(count or 0)


# ---------------------------------------------------------------- GET /invites/{code}


async def test_preview_requires_auth(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/invites/{CODE}")

    assert response.status_code == 401
    assert_contract(response, "GET", "/invites/{code}")


async def test_preview_lowercase_code(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE)

    response = await api_client.get(f"/invites/{CODE.lower()}", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "GET", "/invites/{code}")
    assert response.json() == {
        "event_id": str(event.id),
        "title": "Конференция",
        "starts_on": "2026-11-12",
        "ends_on": "2026-11-14",
        "join_open": True,
        "already_member": False,
    }


async def test_preview_unknown_code(api_client: AsyncClient, user: User) -> None:
    response = await api_client.get("/invites/ZZZZZZ", headers=auth_headers())

    assert response.status_code == 404
    assert_contract(response, "GET", "/invites/{code}")
    assert response.json()["error"]["code"] == "INVITE_NOT_FOUND"


@pytest.mark.parametrize("code", ["ABC", "ABCDE!", "ABCDEFG"])
async def test_preview_bad_format(api_client: AsyncClient, user: User, code: str) -> None:
    response = await api_client.get(f"/invites/{code}", headers=auth_headers())

    assert response.status_code == 422
    assert_contract(response, "GET", "/invites/{code}")


async def test_preview_already_member(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE)
    await make_membership(db_session, event_id=event.id, user_id=user.id)

    response = await api_client.get(f"/invites/{CODE}", headers=auth_headers())

    assert_contract(response, "GET", "/invites/{code}")
    assert response.json()["already_member"] is True


async def test_preview_admin_without_membership_is_not_member(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    await make_event(db_session, invite_code=CODE, admin_id=user.id)

    response = await api_client.get(f"/invites/{CODE}", headers=auth_headers())

    assert_contract(response, "GET", "/invites/{code}")
    assert response.json()["already_member"] is False


async def test_preview_join_closed(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    await make_event(db_session, invite_code=CODE, join_open=False)

    response = await api_client.get(f"/invites/{CODE}", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "GET", "/invites/{code}")
    assert response.json()["join_open"] is False


# ---------------------------------------------------------------- POST /invites/{code}/join


async def test_join_requires_auth(api_client: AsyncClient) -> None:
    response = await api_client.post(f"/invites/{CODE}/join")

    assert response.status_code == 401
    assert_contract(response, "POST", "/invites/{code}/join")


async def test_join_unknown_code(api_client: AsyncClient, user: User) -> None:
    response = await api_client.post("/invites/ZZZZZZ/join", headers=auth_headers())

    assert response.status_code == 404
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["error"]["code"] == "INVITE_NOT_FOUND"


async def test_join_bad_format(api_client: AsyncClient, user: User) -> None:
    response = await api_client.post("/invites/ABC/join", headers=auth_headers())

    assert response.status_code == 422
    assert_contract(response, "POST", "/invites/{code}/join")


async def test_join_first_time_then_repeat(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE)

    first = await api_client.post(f"/invites/{CODE.lower()}/join", headers=auth_headers())

    assert first.status_code == 201
    assert_contract(first, "POST", "/invites/{code}/join")
    assert first.json()["id"] == str(event.id)
    assert first.json()["is_participant"] is True
    assert first.json()["is_admin"] is False
    assert await count_memberships(db_session, event.id, user.id) == 1

    second = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers())

    assert second.status_code == 200
    assert_contract(second, "POST", "/invites/{code}/join")
    assert second.json() == first.json()
    assert await count_memberships(db_session, event.id, user.id) == 1


async def test_join_closed(api_client: AsyncClient, db_session: AsyncSession, user: User) -> None:
    event = await make_event(db_session, invite_code=CODE, join_open=False)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers())

    assert response.status_code == 409
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["error"]["code"] == "JOIN_CLOSED"
    assert await count_memberships(db_session, event.id, user.id) == 0


async def test_join_closed_but_already_member(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE, join_open=False)
    await make_membership(db_session, event_id=event.id, user_id=user.id)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["is_participant"] is True
    assert await count_memberships(db_session, event.id, user.id) == 1


async def test_admin_joins_own_group(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE, admin_id=user.id)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers())

    assert response.status_code == 201
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["is_admin"] is True
    assert response.json()["is_participant"] is True
    assert await count_memberships(db_session, event.id, user.id) == 1


async def test_joined_group_visible_in_my_events_and_event(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, invite_code=CODE)
    await api_client.post(f"/invites/{CODE}/join", headers=auth_headers())

    listing = await api_client.get("/me/events", headers=auth_headers())
    detail = await api_client.get(f"/events/{event.id}", headers=auth_headers())

    assert_contract(listing, "GET", "/me/events")
    assert [item["id"] for item in listing.json()] == [str(event.id)]
    assert detail.status_code == 200
    assert_contract(detail, "GET", "/events/{event_id}")
    assert detail.json()["is_participant"] is True


async def test_join_without_phone_is_forbidden(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    no_phone = await make_user(db_session, telegram_id=555, first_name="NoPhone", phone=None)
    event = await make_event(db_session, invite_code=CODE)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers(555))

    assert response.status_code == 403
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["error"]["code"] == "PHONE_REQUIRED"
    assert await count_memberships(db_session, event.id, no_phone.id) == 0
    preview = await api_client.get(f"/invites/{CODE}", headers=auth_headers(555))
    assert preview.status_code == 200
    assert_contract(preview, "GET", "/invites/{code}")
    assert preview.json()["already_member"] is False


async def test_join_without_phone_but_already_member(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    no_phone = await make_user(db_session, telegram_id=555, first_name="NoPhone", phone=None)
    event = await make_event(db_session, invite_code=CODE)
    await make_membership(db_session, event_id=event.id, user_id=no_phone.id)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers(555))

    assert response.status_code == 200
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["is_participant"] is True
    assert await count_memberships(db_session, event.id, no_phone.id) == 1


async def test_join_closed_without_phone_reports_join_closed(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    no_phone = await make_user(db_session, telegram_id=555, first_name="NoPhone", phone=None)
    event = await make_event(db_session, invite_code=CODE, join_open=False)

    response = await api_client.post(f"/invites/{CODE}/join", headers=auth_headers(555))

    assert response.status_code == 409
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["error"]["code"] == "JOIN_CLOSED"
    assert await count_memberships(db_session, event.id, no_phone.id) == 0


async def test_join_unknown_code_without_phone_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, telegram_id=555, first_name="NoPhone", phone=None)

    response = await api_client.post("/invites/ZZZZZZ/join", headers=auth_headers(555))

    assert response.status_code == 404
    assert_contract(response, "POST", "/invites/{code}/join")
    assert response.json()["error"]["code"] == "INVITE_NOT_FOUND"
