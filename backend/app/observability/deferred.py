"""Действия, которые выполняются только после commit сессии: счётчики метрик.

Счётчик «заявка села в трансфер» не должен расти, если сценарий потом откатился. Код регистрирует
действие через `defer` (или `defer_increment` / `defer_observe`), а слушатели на классе `Session`
применяют его, когда commit дошёл до корня, и отбрасывают при откате. Действие привязано к той
транзакции, внутри которой оно зарегистрировано:

- commit savepoint переносит действия в родительскую транзакцию, они ждут её commit;
- откат savepoint отбрасывает действия этого savepoint и вложенных в него;
- commit корневой транзакции применяет всё накопленное, откат или закрытие сессии — отбрасывает.

Слушатели включаются при импорте модуля, поэтому модуль, который вызывает `defer`, импортирует
его сам. Ошибка в действии пишется в лог и сценарий не прерывает: commit уже состоялся.
"""

import logging
from collections.abc import Callable

from prometheus_client import Counter, Histogram
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

logger = logging.getLogger(__name__)

Action = Callable[[], None]

_PENDING_KEY = "ft_deferred_actions"


def _current(session: Session) -> SessionTransaction | None:
    """Самая глубокая открытая транзакция: savepoint, если он есть, иначе корневая."""
    return session.get_nested_transaction() or session.get_transaction()


def defer(session: AsyncSession | Session, action: Action) -> None:
    """Выполнит `action` после commit корневой транзакции; при откате действие пропадёт.

    Вне транзакции (сессия ещё ничего не делала) откатывать нечего, и действие выполняется сразу.
    """
    sync = session.sync_session if isinstance(session, AsyncSession) else session
    transaction = _current(sync)
    if transaction is None:
        action()
        return
    pending: dict[SessionTransaction, list[Action]] = sync.info.setdefault(_PENDING_KEY, {})
    pending.setdefault(transaction, []).append(action)


def defer_increment(session: AsyncSession | Session, counter: Counter, amount: float = 1) -> None:
    """Увеличит счётчик (уже с лейблами, `counter.labels(...)`) после commit."""
    defer(session, lambda: counter.inc(amount))


def defer_observe(session: AsyncSession | Session, histogram: Histogram, value: float) -> None:
    """Добавит наблюдение в гистограмму (уже с лейблами) после commit."""
    defer(session, lambda: histogram.observe(value))


@event.listens_for(Session, "after_commit")
def _apply_or_hand_over(session: Session) -> None:
    # срабатывает и при commit savepoint, и при commit корня; текущая транзакция ещё не закрыта
    pending: dict[SessionTransaction, list[Action]] | None = session.info.get(_PENDING_KEY)
    transaction = _current(session)
    if not pending or transaction is None:
        return
    actions = pending.pop(transaction, [])
    if transaction.nested and transaction.parent is not None:
        pending.setdefault(transaction.parent, []).extend(actions)
        return
    for action in actions:
        try:
            action()
        except Exception:
            logger.warning("Отложенное действие не выполнено", exc_info=True)


@event.listens_for(Session, "after_transaction_end")
def _drop_unfinished(session: Session, transaction: SessionTransaction) -> None:
    # после commit запись уже забрана; остаток — транзакция откатилась или сессию закрыли
    pending: dict[SessionTransaction, list[Action]] | None = session.info.get(_PENDING_KEY)
    if not pending:
        return
    pending.pop(transaction, None)
    if transaction.parent is None:
        pending.clear()
