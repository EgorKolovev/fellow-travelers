import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, User
from app.services import events as events_service
from tests.api.conftest import auth_headers
from tests.factories import make_event, make_membership
from tests.helpers.contract import assert_contract

INVITE_CODE_RE = re.compile(r"^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$")
MISSING_ID = "6f1b7a52-3c9e-4f5a-9d44-1a2b3c4d5e6f"


def event_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "  Конференция  ",
        "starts_on": "2026-11-12",
        "ends_on": "2026-11-14",
        "meeting_point": {"address": "Терминал D", "lat": 55.97, "lon": 37.41},
        "destination": {"address": "Отель"},
        "meeting_instruction": "У стойки информации",
    }
    payload.update(overrides)
    return payload


def error_fields(response: Any) -> dict[str, str]:
    fields: dict[str, str] = response.json()["error"]["details"]["fields"]
    return fields


# ---------------------------------------------------------------- POST /events


async def test_create_event(api_client: AsyncClient, db_session: AsyncSession, user: User) -> None:
    response = await api_client.post("/events", json=event_payload(), headers=auth_headers())

    assert response.status_code == 201
    assert_contract(response, "POST", "/events")
    body = response.json()
    assert body["title"] == "Конференция"
    assert body["starts_on"] == "2026-11-12"
    assert body["ends_on"] == "2026-11-14"
    assert body["meeting_point"] == {"address": "Терминал D", "lat": 55.97, "lon": 37.41}
    assert body["destination"] == {"address": "Отель", "lat": None, "lon": None}
    assert body["pickup_point"] == body["meeting_point"]
    assert body["meeting_instruction"] == "У стойки информации"
    assert body["chat_url"] is None
    assert body["join_open"] is True
    assert body["is_admin"] is True
    assert body["is_participant"] is False
    stored = (
        await db_session.execute(select(Event).where(Event.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert stored.admin_id == user.id
    assert stored.pickup_point is None
    assert INVITE_CODE_RE.match(stored.invite_code)


async def test_create_event_with_pickup_point_and_chat(api_client: AsyncClient, user: User) -> None:
    payload = event_payload(
        pickup_point={"address": "Парковка П2", "lat": 55.9, "lon": 37.4},
        chat_url="https://t.me/+abc",
    )

    response = await api_client.post("/events", json=payload, headers=auth_headers())

    assert_contract(response, "POST", "/events")
    body = response.json()
    assert body["pickup_point"]["address"] == "Парковка П2"
    assert body["chat_url"] == "https://t.me/+abc"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"ends_on": "2026-11-11"}, "ends_on"),
        ({"meeting_point": {"address": "A", "lat": 55.0}}, "meeting_point"),
        ({"meeting_point": {"address": "A", "lat": 91, "lon": 0}}, "meeting_point.lat"),
        ({"surprise": 1}, "surprise"),
        ({"chat_url": "ftp://example.com/x"}, "chat_url"),
        ({"chat_url": "https://t.me/" + "a" * 200}, "chat_url"),
        ({"meeting_instruction": "x" * 2001}, "meeting_instruction"),
    ],
)
async def test_create_event_validation(
    api_client: AsyncClient, user: User, overrides: dict[str, Any], field: str
) -> None:
    response = await api_client.post(
        "/events", json=event_payload(**overrides), headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "POST", "/events")
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert field in error_fields(response)


VALID_CHAT_URLS = [
    "https://t.me/my_group_chat",
    "https://t.me/+AbC-12_x",
    "https://t.me/joinchat/AbC123",
]
INVALID_CHAT_URLS = [
    "http://t.me/chat_name",
    "https://evil.example/x",
    "https://t.me.evil.com/chat_name",
    "https://t.me/proxy?server=1",
    "https://t.me/proxy",
    "https://t.me/Share",
    "https://t.me/chat_name?start=1",
    "https://t.me/abc",
    "https://t.me/",
    "javascript:alert(1)",
]
CHAT_URL_MESSAGE = "Укажите ссылку на чат в Telegram (https://t.me/…)"


@pytest.mark.parametrize("chat_url", VALID_CHAT_URLS)
async def test_create_event_accepts_telegram_chat_url(
    api_client: AsyncClient, user: User, chat_url: str
) -> None:
    response = await api_client.post(
        "/events", json=event_payload(chat_url=chat_url), headers=auth_headers()
    )

    assert response.status_code == 201
    assert_contract(response, "POST", "/events")
    assert response.json()["chat_url"] == chat_url


@pytest.mark.parametrize("chat_url", INVALID_CHAT_URLS)
async def test_create_event_rejects_non_telegram_chat_url(
    api_client: AsyncClient, user: User, chat_url: str
) -> None:
    response = await api_client.post(
        "/events", json=event_payload(chat_url=chat_url), headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "POST", "/events")
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert error_fields(response)["chat_url"] == CHAT_URL_MESSAGE


async def test_create_event_dates_message(api_client: AsyncClient, user: User) -> None:
    response = await api_client.post(
        "/events", json=event_payload(ends_on="2026-11-11"), headers=auth_headers()
    )

    assert error_fields(response)["ends_on"] == "Дата окончания раньше даты начала"


async def test_create_event_retries_invite_code_on_collision(
    api_client: AsyncClient,
    db_session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_event(db_session, admin_id=user.id, invite_code="AAAAAA")
    codes: Iterator[str] = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(events_service, "generate_invite_code", lambda: next(codes))

    response = await api_client.post("/events", json=event_payload(), headers=auth_headers())

    assert response.status_code == 201
    assert_contract(response, "POST", "/events")
    stored = (
        await db_session.execute(select(Event).where(Event.id == uuid.UUID(response.json()["id"])))
    ).scalar_one()
    assert stored.invite_code == "BBBBBB"


async def test_create_event_gives_up_after_ten_collisions(
    api_client: AsyncClient,
    db_session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_event(db_session, admin_id=user.id, invite_code="AAAAAA")
    monkeypatch.setattr(events_service, "generate_invite_code", lambda: "AAAAAA")

    response = await api_client.post("/events", json=event_payload(), headers=auth_headers())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_generate_invite_code_format() -> None:
    for _ in range(200):
        assert INVITE_CODE_RE.match(events_service.generate_invite_code())


# ---------------------------------------------------------------- GET /events/{id}


async def test_get_event_as_admin(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.get(f"/events/{event.id}", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "GET", "/events/{event_id}")
    body = response.json()
    assert body["id"] == str(event.id)
    assert body["is_admin"] is True
    assert body["is_participant"] is False
    assert body["pickup_point"] == body["meeting_point"]


async def test_get_event_as_participant(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)
    await make_membership(db_session, event_id=event.id, user_id=user.id)

    response = await api_client.get(f"/events/{event.id}", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "GET", "/events/{event_id}")
    assert response.json()["is_admin"] is False
    assert response.json()["is_participant"] is True


async def test_get_event_returns_explicit_pickup_point(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(
        db_session, admin_id=user.id, pickup_point={"address": "П2", "lat": None, "lon": None}
    )

    response = await api_client.get(f"/events/{event.id}", headers=auth_headers())

    assert_contract(response, "GET", "/events/{event_id}")
    assert response.json()["pickup_point"]["address"] == "П2"


async def test_get_event_by_stranger_is_404(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)

    response = await api_client.get(f"/events/{event.id}", headers=auth_headers())

    assert response.status_code == 404
    assert_contract(response, "GET", "/events/{event_id}")
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_get_missing_event_is_404(api_client: AsyncClient, user: User) -> None:
    response = await api_client.get(f"/events/{MISSING_ID}", headers=auth_headers())

    assert response.status_code == 404
    assert_contract(response, "GET", "/events/{event_id}")
    assert response.json()["error"]["message"] == "Группа не найдена"


async def test_get_event_invalid_uuid_is_422(api_client: AsyncClient, user: User) -> None:
    response = await api_client.get("/events/not-a-uuid", headers=auth_headers())

    assert response.status_code == 422
    assert_contract(response, "GET", "/events/{event_id}")
    assert "event_id" in error_fields(response)


# ---------------------------------------------------------------- PATCH /events/{id}


async def test_patch_updates_only_given_field(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id, chat_url="https://t.me/my_group_chat")

    response = await api_client.patch(
        f"/events/{event.id}", json={"title": "Новое имя"}, headers=auth_headers()
    )

    assert response.status_code == 200
    assert_contract(response, "PATCH", "/events/{event_id}")
    body = response.json()
    assert body["title"] == "Новое имя"
    assert body["starts_on"] == "2026-11-12"
    assert body["ends_on"] == "2026-11-14"
    assert body["meeting_point"]["address"] == "Терминал D"
    assert body["destination"]["address"] == "Отель"
    assert body["chat_url"] == "https://t.me/my_group_chat"
    assert body["join_open"] is True


async def test_patch_updates_points_and_dates(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}",
        json={
            "starts_on": "2026-11-20",
            "ends_on": "2026-11-22",
            "meeting_point": {"address": "Терминал B"},
            "pickup_point": {"address": "Выход 5", "lat": 1, "lon": 2},
            "meeting_instruction": "Новая инструкция",
        },
        headers=auth_headers(),
    )

    assert_contract(response, "PATCH", "/events/{event_id}")
    body = response.json()
    assert body["starts_on"] == "2026-11-20"
    assert body["meeting_point"]["address"] == "Терминал B"
    assert body["pickup_point"] == {"address": "Выход 5", "lat": 1.0, "lon": 2.0}
    assert body["meeting_instruction"] == "Новая инструкция"


async def test_patch_null_pickup_point_resets_it(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(
        db_session, admin_id=user.id, pickup_point={"address": "П2", "lat": None, "lon": None}
    )

    response = await api_client.patch(
        f"/events/{event.id}", json={"pickup_point": None}, headers=auth_headers()
    )

    assert_contract(response, "PATCH", "/events/{event_id}")
    body = response.json()
    assert body["pickup_point"] == body["meeting_point"]
    await db_session.refresh(event)
    assert event.pickup_point is None


async def test_patch_null_chat_url_removes_it(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id, chat_url="https://t.me/my_group_chat")

    response = await api_client.patch(
        f"/events/{event.id}", json={"chat_url": None}, headers=auth_headers()
    )

    assert_contract(response, "PATCH", "/events/{event_id}")
    assert response.json()["chat_url"] is None


@pytest.mark.parametrize("chat_url", VALID_CHAT_URLS)
async def test_patch_accepts_telegram_chat_url(
    api_client: AsyncClient, db_session: AsyncSession, user: User, chat_url: str
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"chat_url": chat_url}, headers=auth_headers()
    )

    assert response.status_code == 200
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert response.json()["chat_url"] == chat_url


@pytest.mark.parametrize("chat_url", INVALID_CHAT_URLS)
async def test_patch_rejects_non_telegram_chat_url(
    api_client: AsyncClient, db_session: AsyncSession, user: User, chat_url: str
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"chat_url": chat_url}, headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert error_fields(response)["chat_url"] == CHAT_URL_MESSAGE


async def test_patch_join_open_false(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"join_open": False}, headers=auth_headers()
    )

    assert_contract(response, "PATCH", "/events/{event_id}")
    assert response.json()["join_open"] is False
    await db_session.refresh(event)
    assert event.join_open is False


async def test_patch_empty_body_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(f"/events/{event.id}", json={}, headers=auth_headers())

    assert_contract(response, "PATCH", "/events/{event_id}")
    assert response.json()["title"] == "Конференция"


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "starts_on",
        "ends_on",
        "meeting_point",
        "destination",
        "meeting_instruction",
        "join_open",
    ],
)
async def test_patch_null_for_required_field_is_422(
    api_client: AsyncClient, db_session: AsyncSession, user: User, field: str
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={field: None}, headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert field in error_fields(response)


async def test_patch_unknown_field_is_422(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"invite_code": "ZZZZZZ"}, headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "PATCH", "/events/{event_id}")


async def test_patch_ends_on_before_current_starts_on_is_422(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"ends_on": "2026-11-01"}, headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert error_fields(response) == {"ends_on": "Дата окончания раньше даты начала"}
    await db_session.refresh(event)
    assert event.ends_on.isoformat() == "2026-11-14"


async def test_patch_starts_on_after_current_ends_on_is_422(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"starts_on": "2026-12-01"}, headers=auth_headers()
    )

    assert response.status_code == 422
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert "ends_on" in error_fields(response)


async def test_patch_by_participant_is_403(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)
    await make_membership(db_session, event_id=event.id, user_id=user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"title": "Взлом"}, headers=auth_headers()
    )

    assert response.status_code == 403
    assert_contract(response, "PATCH", "/events/{event_id}")
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_patch_by_stranger_is_404(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)

    response = await api_client.patch(
        f"/events/{event.id}", json={"title": "Взлом"}, headers=auth_headers()
    )

    assert response.status_code == 404
    assert_contract(response, "PATCH", "/events/{event_id}")


async def test_patch_missing_event_is_404(api_client: AsyncClient, user: User) -> None:
    response = await api_client.patch(
        f"/events/{MISSING_ID}", json={"title": "X"}, headers=auth_headers()
    )

    assert response.status_code == 404
    assert_contract(response, "PATCH", "/events/{event_id}")


# ---------------------------------------------------------------- GET /events/{id}/invite


async def test_invite_for_admin(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(db_session, admin_id=user.id, invite_code="K7Q2ZP")

    response = await api_client.get(f"/events/{event.id}/invite", headers=auth_headers())

    assert response.status_code == 200
    assert_contract(response, "GET", "/events/{event_id}/invite")
    assert response.json() == {
        "code": "K7Q2ZP",
        "link": "https://t.me/test_bot?start=join_K7Q2ZP",
    }


async def test_invite_for_participant_is_403(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)
    await make_membership(db_session, event_id=event.id, user_id=user.id)

    response = await api_client.get(f"/events/{event.id}/invite", headers=auth_headers())

    assert response.status_code == 403
    assert_contract(response, "GET", "/events/{event_id}/invite")
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_invite_for_stranger_is_404(
    api_client: AsyncClient, db_session: AsyncSession, user: User, other_user: User
) -> None:
    event = await make_event(db_session, admin_id=other_user.id)

    response = await api_client.get(f"/events/{event.id}/invite", headers=auth_headers())

    assert response.status_code == 404
    assert_contract(response, "GET", "/events/{event_id}/invite")
