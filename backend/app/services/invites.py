from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models import Event, Membership, User
from app.services.access import is_member


async def get_event_by_code(session: AsyncSession, code: str) -> Event:
    event = await session.scalar(select(Event).where(Event.invite_code == code.upper()))
    if event is None:
        raise AppError(ErrorCode.INVITE_NOT_FOUND, "Приглашение не найдено", 404)
    return event


async def join_event(session: AsyncSession, event: Event, user: User) -> bool:
    """Вступает в группу; True — членство создано, False — пользователь уже участник.

    Действующие участники сохраняют доступ, даже если вступление закрыто или нет телефона.
    """
    if await is_member(session, event.id, user.id):
        return False
    if not event.join_open:
        raise AppError(ErrorCode.JOIN_CLOSED, "Вступление в группу закрыто", 409)
    if user.phone is None:
        raise AppError(
            ErrorCode.PHONE_REQUIRED, "Чтобы вступить в группу, поделитесь телефоном в боте", 403
        )
    stmt = (
        insert(Membership)
        .values(event_id=event.id, user_id=user.id)
        .on_conflict_do_nothing()
        .returning(Membership.user_id)
    )
    created = await session.scalar(stmt)
    await session.commit()
    return created is not None
