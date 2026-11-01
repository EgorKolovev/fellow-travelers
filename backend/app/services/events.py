import secrets
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.models import Application, ApplicationStatus, Event, Membership, User
from app.schemas.event import DATES_ORDER_MESSAGE, EventCreate, EventUpdate

INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 6
INVITE_CODE_ATTEMPTS = 10
_UNIQUE_VIOLATION = "23505"


@dataclass(frozen=True, slots=True)
class EventListItem:
    event: Event
    is_admin: bool
    is_participant: bool
    my_application_status: ApplicationStatus | None


def generate_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))


def build_invite_link(code: str) -> str:
    return f"https://t.me/{get_settings().bot_username}?start=join_{code}"


async def list_my_events(session: AsyncSession, user: User) -> list[EventListItem]:
    stmt = (
        select(Event, Membership.user_id, Application.status)
        .outerjoin(Membership, (Membership.event_id == Event.id) & (Membership.user_id == user.id))
        .outerjoin(
            Application, (Application.event_id == Event.id) & (Application.user_id == user.id)
        )
        .where(or_(Event.admin_id == user.id, Membership.user_id.is_not(None)))
        .order_by(Event.starts_on, Event.title, Event.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        EventListItem(
            event=event,
            is_admin=event.admin_id == user.id,
            is_participant=member_id is not None,
            my_application_status=status,
        )
        for event, member_id, status in rows
    ]


async def create_event(session: AsyncSession, user: User, data: EventCreate) -> Event:
    for _ in range(INVITE_CODE_ATTEMPTS):
        event = Event(
            title=data.title,
            starts_on=data.starts_on,
            ends_on=data.ends_on,
            admin_id=user.id,
            meeting_point=data.meeting_point.model_dump(),
            pickup_point=data.pickup_point.model_dump() if data.pickup_point else None,
            destination=data.destination.model_dump(),
            meeting_instruction=data.meeting_instruction,
            chat_url=data.chat_url,
            join_open=True,
            invite_code=generate_invite_code(),
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
                raise
            continue
        await session.commit()
        return event
    raise RuntimeError("Не удалось сгенерировать уникальный код приглашения")


def _dates_error() -> AppError:
    return AppError(
        ErrorCode.VALIDATION_ERROR,
        "Проверьте введённые данные",
        422,
        {"fields": {"ends_on": DATES_ORDER_MESSAGE}},
    )


async def update_event(session: AsyncSession, event: Event, data: EventUpdate) -> Event:
    changes = {name: getattr(data, name) for name in data.model_fields_set}
    starts_on: date = changes.get("starts_on", event.starts_on)
    ends_on: date = changes.get("ends_on", event.ends_on)
    if ends_on < starts_on:
        raise _dates_error()
    for name, value in changes.items():
        if name in ("meeting_point", "pickup_point", "destination") and value is not None:
            value = value.model_dump()
        setattr(event, name, value)
    await session.commit()
    return event
