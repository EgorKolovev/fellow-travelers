"""Запись уведомлений с защитой от потока сообщений (M-04a).

Общая точка для планировщика (`before_commit`, синхронная `Session`) и прямого `notify`
(`AsyncSession`, через `run_sync`). Для каждой пары `(user_id, event_id)`:

1. Есть неотправленная строка `pending` — новое событие склеивается в неё: тот же вид обновляет
   `payload`, другой вид даёт `several_changes` с `payload` из одного `event_id`. `run_after`,
   `dedupe_key` и `attempts` не меняются.
2. Строки нет — вставка с `run_after = now() + DEBOUNCE`, а если человеку по группе за последний час
   уже ушло или уходит `HOURLY_LIMIT` сообщений, то не раньше выхода из окна лишних сообщений.
3. Вставку заняла другая транзакция (частичный уникальный индекс) — снова склейка. Строку могли
   успеть арендовать (`sending`): тогда вставка повторяется. Попыток `MAX_ATTEMPTS`; если не вышло,
   в лог идёт WARNING с группой и пользователем (без исключения: изменение уже сделано).

Каждый шаг — один запрос на всю пачку получателей, число запросов не растёт с числом людей.

Окно часа считается по `sent_at` отправленных строк: именно в этот момент сообщение дошло до
человека (`created_at` занижал бы счёт, если строка долго ждала отложенного срока). У строк в
`sending` `sent_at` ещё нет, они «уходят сейчас» и считаются моментом `now()`.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, Uuid, case, column, func, literal, select, tuple_, update, values
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Notification, NotificationKind, NotificationStatus
from app.observability import metrics
from app.observability.deferred import defer_increment
from app.observability.metrics import PlannedOutcome

logger = logging.getLogger(__name__)

# новая строка ждёт, чтобы частые изменения успели склеиться
DEBOUNCE = timedelta(seconds=30)
# не больше стольких сообщений человеку по одной группе за WINDOW
HOURLY_LIMIT = 6
WINDOW = timedelta(hours=1)
MAX_ATTEMPTS = 3

_Pair = tuple[uuid.UUID, uuid.UUID]  # (user_id, event_id)


@dataclass(frozen=True, slots=True)
class NewNotification:
    user_id: uuid.UUID
    event_id: uuid.UUID
    kind: NotificationKind
    payload: Mapping[str, Any]
    dedupe_key: str

    @property
    def pair(self) -> _Pair:
        return (self.user_id, self.event_id)


def _merge(session: Session, items: Sequence[NewNotification]) -> set[_Pair]:
    """Склеивает пачку с существующими `pending`; возвращает пары, у которых строка нашлась."""
    data = values(
        column("user_id", Uuid),
        column("event_id", Uuid),
        column("kind", String),
        column("payload", JSONB),
        column("only_event", JSONB),
        name="v",
    ).data(
        [
            (i.user_id, i.event_id, i.kind.value, dict(i.payload), {"event_id": str(i.event_id)})
            for i in items
        ]
    )
    same_kind = Notification.kind == data.c.kind
    stmt = (
        update(Notification)
        .where(
            Notification.user_id == data.c.user_id,
            Notification.event_id == data.c.event_id,
            Notification.status == NotificationStatus.PENDING,
        )
        .values(
            kind=case(
                (same_kind, Notification.kind),
                else_=literal(NotificationKind.SEVERAL_CHANGES, Notification.kind.type),
            ),
            payload=case((same_kind, data.c.payload), else_=data.c.only_event),
        )
        .returning(Notification.user_id, Notification.event_id)
        .execution_options(synchronize_session=False)
    )
    return {(row.user_id, row.event_id) for row in session.execute(stmt)}


def _limits(session: Session, pairs: Sequence[_Pair]) -> dict[_Pair, tuple[datetime, datetime]]:
    """Для пар, где за час набралось `HOURLY_LIMIT` сообщений: `(когда можно слать, now())`.

    Берётся момент `HOURLY_LIMIT`-го с конца сообщения окна: когда он выйдет из часа, останется
    `HOURLY_LIMIT - 1` и ещё одно поместится. При ровно лимите это самое раннее сообщение.
    """
    moment = func.coalesce(Notification.sent_at, func.now())
    ranked = (
        select(
            Notification.user_id,
            Notification.event_id,
            moment.label("moment"),
            func.row_number()
            .over(
                partition_by=(Notification.user_id, Notification.event_id),
                order_by=moment.desc(),
            )
            .label("rank"),
        )
        .where(
            tuple_(Notification.user_id, Notification.event_id).in_(list(pairs)),
            Notification.status.in_((NotificationStatus.SENT, NotificationStatus.SENDING)),
            moment > func.now() - WINDOW,
        )
        .subquery()
    )
    stmt = select(ranked.c.user_id, ranked.c.event_id, ranked.c.moment + WINDOW, func.now()).where(
        ranked.c.rank == HOURLY_LIMIT
    )
    return {(row[0], row[1]): (row[2], row[3]) for row in session.execute(stmt)}


def _insert_new(session: Session, rows: list[dict[str, Any]]) -> set[_Pair]:
    """Вставляет строки пачкой; конфликт индекса (чужая `pending`, тот же ключ) пропускает."""
    stmt = (
        pg_insert(Notification)
        .values(rows)
        .on_conflict_do_nothing()
        .returning(Notification.user_id, Notification.event_id)
    )
    return {(row.user_id, row.event_id) for row in session.execute(stmt)}


def _known_keys(session: Session, items: Sequence[NewNotification]) -> set[tuple[uuid.UUID, str]]:
    """Пары `(user_id, dedupe_key)`, которые уже записаны: повтор уведомления, а не гонка."""
    stmt = select(Notification.user_id, Notification.dedupe_key).where(
        tuple_(Notification.user_id, Notification.dedupe_key).in_(
            [(i.user_id, i.dedupe_key) for i in items]
        )
    )
    return {(row[0], row[1]) for row in session.execute(stmt)}


def _waves(items: Sequence[NewNotification]) -> list[list[NewNotification]]:
    """Пачки без повторов пары: второе событие того же человека склеится с первым."""
    seen: dict[_Pair, int] = defaultdict(int)
    waves: list[list[NewNotification]] = []
    for item in items:
        index = seen[item.pair]
        seen[item.pair] += 1
        if index == len(waves):
            waves.append([])
        waves[index].append(item)
    return waves


def _store_wave(session: Session, items: list[NewNotification]) -> dict[PlannedOutcome, int]:
    counts = dict.fromkeys(PlannedOutcome, 0)
    remaining = sorted(items, key=lambda i: i.pair)  # один порядок блокировок во всех транзакциях
    for _ in range(MAX_ATTEMPTS):
        merged = _merge(session, remaining)
        counts[PlannedOutcome.MERGED] += len(merged)
        remaining = [i for i in remaining if i.pair not in merged]
        if not remaining:
            return counts

        limits = _limits(session, [i.pair for i in remaining])
        rows: list[dict[str, Any]] = []
        deferred: set[_Pair] = set()
        for item in remaining:
            run_after: Any = func.now() + DEBOUNCE
            if item.pair in limits:
                free_at, now = limits[item.pair]
                run_after = func.greatest(run_after, free_at)
                if free_at > now + DEBOUNCE:
                    deferred.add(item.pair)
            rows.append(
                {
                    "user_id": item.user_id,
                    "event_id": item.event_id,
                    "kind": item.kind,
                    "payload": dict(item.payload),
                    "dedupe_key": item.dedupe_key,
                    "run_after": run_after,
                }
            )
        inserted = _insert_new(session, rows)
        counts[PlannedOutcome.DEFERRED] += len(inserted & deferred)
        counts[PlannedOutcome.CREATED] += len(inserted - deferred)
        remaining = [i for i in remaining if i.pair not in inserted]
        if not remaining:
            return counts

        # вставка не прошла: либо другая транзакция вставила `pending` (склеим на следующем
        # круге), либо это повтор уже записанного уведомления (ничего не делаем)
        known = _known_keys(session, remaining)
        remaining = [i for i in remaining if (i.user_id, i.dedupe_key) not in known]
        if not remaining:
            return counts

    for item in remaining:
        logger.warning(
            "Уведомление не записано после %d попыток: группа %s, пользователь %s",
            MAX_ATTEMPTS,
            item.event_id,
            item.user_id,
        )
    return counts


def store(session: Session, items: Sequence[NewNotification]) -> None:
    """Записывает уведомления в текущую транзакцию (без commit); см. описание модуля."""
    totals = dict.fromkeys(PlannedOutcome, 0)
    for wave in _waves(items):
        for outcome, count in _store_wave(session, wave).items():
            totals[outcome] += count
    for outcome, count in totals.items():
        if count:
            defer_increment(session, metrics.notifications_planned_total.labels(outcome), count)


async def store_async(session: AsyncSession, items: Sequence[NewNotification]) -> None:
    if items:
        await session.run_sync(lambda sync_session: store(sync_session, items))
