from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.notifications import forget_pending, plan_notifications

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # hide_parameters: значения параметров SQL (телефон, имя) не попадают ни в echo,
        # ни в тексты исключений
        _engine = create_async_engine(
            settings.database_url, echo=settings.debug, hide_parameters=True
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def _plan_notifications_before_commit(session: Session) -> None:
    # коммит вложенной транзакции (savepoint) не конец сценария: записи копятся до внешнего commit
    if session.in_nested_transaction():
        return
    plan_notifications(session)


# Один раз на класс сессии: API, воркер и бот получают планировщик без правок в сервисах.
# `before_commit` вызывается внутри greenlet асинхронной сессии, поэтому синхронный обработчик
# может пользоваться `Session.execute` и `Session.flush`:
# https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-events-with-the-asyncio-extension
event.listen(Session, "before_commit", _plan_notifications_before_commit)
event.listen(Session, "after_rollback", forget_pending)
