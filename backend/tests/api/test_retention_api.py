"""После удаления группы (Д11) старые ссылки отвечают 404 по контракту."""

from datetime import UTC, date, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import User
from app.services.retention import purge_expired
from tests.api.conftest import auth_headers
from tests.factories import make_event, make_membership
from tests.helpers.contract import assert_contract

CODE = "ABC234"


async def test_invite_and_group_links_answer_404_after_purge(
    api_client: AsyncClient, db_session: AsyncSession, user: User
) -> None:
    event = await make_event(
        db_session, invite_code=CODE, starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 1)
    )
    await make_membership(db_session, event_id=event.id, user_id=user.id)
    before = await api_client.get(f"/invites/{CODE}", headers=auth_headers())
    assert before.status_code == 200
    assert_contract(before, "GET", "/invites/{code}")

    factory = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    assert await purge_expired(factory, now=datetime(2026, 12, 1, tzinfo=UTC)) == 1

    invite = await api_client.get(f"/invites/{CODE}", headers=auth_headers())
    assert invite.status_code == 404
    assert_contract(invite, "GET", "/invites/{code}")
    assert invite.json()["error"]["code"] == "INVITE_NOT_FOUND"

    trip = await api_client.get(f"/events/{event.id}/my-trip", headers=auth_headers())
    assert trip.status_code == 404
    assert_contract(trip, "GET", "/events/{event_id}/my-trip")
    assert trip.json()["error"]["code"] == "NOT_FOUND"
