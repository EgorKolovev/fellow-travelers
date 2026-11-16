"""Уведомления участникам (M-04): строки `notifications` пишутся в транзакции изменения.

Почти каждое изменение уже пишет запись истории через `audit.record`; `record` кладёт её в
`session.info[PENDING_KEY]`. Перед commit (обработчик `before_commit`, см. `app/db.py`) планировщик
`plan_notifications` берёт накопленные записи, строит по ним сообщения и вставляет их в ту же
транзакцию. Так ни одна точка изменения не пропущена, а откат изменения уносит и сообщения.
Прямой вызов `notify` нужен только там, где записи истории нет (правка точек группы).

Планировщик синхронный: `before_commit` вызывается внутри greenlet асинхронной сессии, поэтому
обработчик может пользоваться обычным `Session.execute` и `Session.flush`.

В `payload` только белый список: `event_id`, `transfer_id`, `trip_id`, `effective_arrival`,
`status`, `role` (у `flight_changed`: `passenger` или `mate`).
Телефонов, имён, username и номеров заявок попутчиков там нет: текст собирает отправитель.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.models import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    AuditReason,
    Notification,
    NotificationKind,
    Trip,
    TripStatus,
)
from app.services.audit import PENDING_KEY, iso

# Д10: порог сдвига расчётного прибытия
ARRIVAL_THRESHOLD = timedelta(minutes=15)

# от самого важного к менее важному: один пользователь получает одно сообщение за транзакцию
_PRIORITY = (
    NotificationKind.TRANSFER_ASSIGNED,
    NotificationKind.TRANSFER_LEFT,
    NotificationKind.FLIGHT_CANCELLED,
    NotificationKind.TRANSFER_CHANGED,
    NotificationKind.FLIGHT_CHANGED,
)
_RANK = {kind: rank for rank, kind in enumerate(_PRIORITY)}

_CONFLICT_COLUMNS = ["user_id", "dedupe_key"]

# `payload.role` у `flight_changed`: чей это рейс для адресата
_ROLE_PASSENGER = "passenger"
_ROLE_MATE = "mate"


@dataclass(frozen=True, slots=True)
class _Candidate:
    kind: NotificationKind
    audit_id: int
    payload: dict[str, Any]


_Drafts = dict[tuple[uuid.UUID, uuid.UUID], list[_Candidate]]


def _insert_ignoring_duplicates(rows: list[dict[str, Any]]) -> Any:
    return (
        pg_insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(index_elements=_CONFLICT_COLUMNS)
    )


def _row(
    *,
    user_id: uuid.UUID,
    event_id: uuid.UUID,
    kind: NotificationKind,
    payload: Mapping[str, Any],
    dedupe_key: str,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "event_id": event_id,
        "kind": kind,
        "payload": dict(payload),
        "dedupe_key": dedupe_key,
    }


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    event_id: uuid.UUID,
    kind: NotificationKind,
    payload: Mapping[str, Any],
    dedupe_key: str,
) -> None:
    """Добавляет сообщение в текущую транзакцию: без commit, повтор с тем же ключом игнорируется.

    Для изменений без записи истории (`points_changed`); остальное строит `plan_notifications`.
    """
    await session.execute(
        _insert_ignoring_duplicates(
            [
                _row(
                    user_id=user_id,
                    event_id=event_id,
                    kind=kind,
                    payload=payload,
                    dedupe_key=dedupe_key,
                )
            ]
        )
    )


async def notify_many(
    session: AsyncSession,
    *,
    user_ids: Iterable[uuid.UUID],
    event_id: uuid.UUID,
    kind: NotificationKind,
    payload: Mapping[str, Any],
    dedupe_key: str,
) -> None:
    """Одно сообщение каждому из `user_ids` одним INSERT; семантика та же, что у `notify`."""
    rows = [
        _row(user_id=u, event_id=event_id, kind=kind, payload=payload, dedupe_key=dedupe_key)
        for u in user_ids
    ]
    if rows:
        await session.execute(_insert_ignoring_duplicates(rows))


class _Planner:
    def __init__(self, session: Session, entries: list[AuditLog]) -> None:
        self.session = session
        self.entries = entries
        # кто действовал в этой транзакции: своё действие участнику не сообщается (Д9)
        self.actors = {e.actor_user_id for e in entries if e.actor_user_id is not None}
        self.drafts: _Drafts = defaultdict(list)
        self._members: dict[uuid.UUID, list[uuid.UUID]] = {}
        self._departed: set[uuid.UUID] | None = None

    def run(self) -> None:
        trip_entries: dict[uuid.UUID, list[AuditLog]] = defaultdict(list)
        for entry in self.entries:
            if entry.action == AuditAction.TRANSFER_CREATED:
                self._transfer_created(entry)
            elif entry.action == AuditAction.TRANSFER_JOINED:
                self._transfer_joined(entry)
            elif entry.action == AuditAction.TRANSFER_LEFT:
                self._transfer_left(entry)
            elif entry.action == AuditAction.TRIP_UPDATED and entry.trip_id is not None:
                trip_entries[entry.trip_id].append(entry)
        for trip_id, group in trip_entries.items():
            self._trip_updated(trip_id, group)
        self._store()

    # ----------------------------------------------------------------- трансферы

    def _add(
        self,
        entry: AuditLog,
        user_ids: Iterable[uuid.UUID | None],
        kind: NotificationKind,
        payload: Mapping[str, Any],
    ) -> None:
        for user_id in user_ids:
            if user_id is None or user_id in self.actors:
                continue
            self.drafts[(entry.event_id, user_id)].append(_Candidate(kind, entry.id, dict(payload)))

    def _departed_applications(self) -> set[uuid.UUID]:
        """Заявки с отметкой «Уехал» среди записей `transfer_left` транзакции: один запрос."""
        if self._departed is None:
            ids = {
                e.application_id
                for e in self.entries
                if e.action == AuditAction.TRANSFER_LEFT and e.application_id is not None
            }
            self._departed = (
                set(
                    self.session.scalars(
                        select(Application.id).where(
                            Application.id.in_(ids), Application.departed_at.is_not(None)
                        )
                    )
                )
                if ids
                else set()
            )
        return self._departed

    def _members_of(self, transfer_id: uuid.UUID) -> list[uuid.UUID]:
        """Пользователи заявок, которые сейчас стоят в трансфере и ещё не уехали."""
        if transfer_id not in self._members:
            self._members[transfer_id] = list(
                self.session.scalars(
                    select(Application.user_id).where(
                        Application.transfer_id == transfer_id,
                        Application.departed_at.is_(None),
                    )
                )
            )
        return self._members[transfer_id]

    @staticmethod
    def _transfer_payload(entry: AuditLog) -> dict[str, Any]:
        return {"event_id": str(entry.event_id), "transfer_id": str(entry.transfer_id)}

    def _transfer_created(self, entry: AuditLog) -> None:
        if entry.transfer_id is None:
            return
        raw_ids = entry.details.get("application_ids", [])
        ids = [uuid.UUID(item) for item in raw_ids]
        owners = self.session.scalars(
            select(Application.user_id).where(
                Application.id.in_(ids), Application.transfer_id == entry.transfer_id
            )
        )
        self._add(entry, owners, NotificationKind.TRANSFER_ASSIGNED, self._transfer_payload(entry))

    def _others(self, entry: AuditLog) -> list[uuid.UUID]:
        assert entry.transfer_id is not None
        return [
            user_id
            for user_id in self._members_of(entry.transfer_id)
            if user_id != entry.subject_user_id
        ]

    def _transfer_joined(self, entry: AuditLog) -> None:
        if entry.transfer_id is None:
            return
        payload = self._transfer_payload(entry)
        self._add(entry, [entry.subject_user_id], NotificationKind.TRANSFER_ASSIGNED, payload)
        self._add(entry, self._others(entry), NotificationKind.TRANSFER_CHANGED, payload)

    def _transfer_left(self, entry: AuditLog) -> None:
        if entry.transfer_id is None:
            return
        payload = self._transfer_payload(entry)
        # вышедшему из-за отмены рейса уже пришло `flight_cancelled`: второго сообщения нет;
        # уехавшим сообщения о составе не отправляются (контракт §12)
        if (
            entry.reason != AuditReason.FLIGHT_CANCELLED
            and entry.application_id not in self._departed_applications()
        ):
            self._add(entry, [entry.subject_user_id], NotificationKind.TRANSFER_LEFT, payload)
        # расформированный трансфер пуст: оставшимся придёт их собственный `transfer_left`
        self._add(entry, self._others(entry), NotificationKind.TRANSFER_CHANGED, payload)

    # ----------------------------------------------------------------- рейсы

    def _trip_updated(self, trip_id: uuid.UUID, group: list[AuditLog]) -> None:
        trip = self.session.get(Trip, trip_id)
        if trip is None:
            return
        first, last = group[0], group[-1]
        became_cancelled = trip.status == TripStatus.CANCELLED and any(
            "status" in entry.details.get("changed_fields", []) for entry in group
        )
        payload = {
            "event_id": str(trip.event_id),
            "trip_id": str(trip.id),
            "effective_arrival": iso(trip.effective_arrival),
            "status": trip.status.value,
        }
        passengers, mates = self._trip_audience(trip_id)
        if became_cancelled:
            # попутчикам с других рейсов состав объяснят `transfer_changed` / `transfer_left`
            self._add(first, passengers, NotificationKind.FLIGHT_CANCELLED, payload)
            return
        if trip.notified_arrival is None:
            # то, что люди видели до первой правки, и есть «последнее сообщённое» значение
            trip.notified_arrival = datetime.fromisoformat(
                first.details["effective_arrival_before"]
            )
        base = trip.notified_arrival
        after = datetime.fromisoformat(last.details["effective_arrival_after"])
        if abs(after - base) < ARRIVAL_THRESHOLD:
            return
        trip.notified_arrival = after
        self._add(
            first,
            passengers,
            NotificationKind.FLIGHT_CHANGED,
            {**payload, "role": _ROLE_PASSENGER},
        )
        self._add(first, mates, NotificationKind.FLIGHT_CHANGED, {**payload, "role": _ROLE_MATE})

    def _trip_audience(self, trip_id: uuid.UUID) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Пассажиры рейса и отдельно их попутчики по трансферу; отменившие и уехавшие не в счёт."""
        waiting = (
            Application.status != ApplicationStatus.CANCELLED,
            Application.departed_at.is_(None),
        )
        rows = self.session.execute(
            select(Application.user_id, Application.transfer_id).where(
                Application.trip_id == trip_id, *waiting
            )
        ).all()
        passengers = {user_id for user_id, _ in rows}
        transfer_ids = {transfer_id for _, transfer_id in rows if transfer_id is not None}
        mates: set[uuid.UUID] = set()
        if transfer_ids:
            mates.update(
                self.session.scalars(
                    select(Application.user_id).where(
                        Application.transfer_id.in_(transfer_ids), *waiting
                    )
                )
            )
        return list(passengers), list(mates - passengers)

    # ----------------------------------------------------------------- запись

    def _store(self) -> None:
        rows = []
        for (event_id, user_id), candidates in self.drafts.items():
            chosen = min(candidates, key=lambda c: (_RANK[c.kind], c.audit_id))
            first_id = min(c.audit_id for c in candidates)
            rows.append(
                _row(
                    user_id=user_id,
                    event_id=event_id,
                    kind=chosen.kind,
                    payload=chosen.payload,
                    dedupe_key=f"{chosen.kind.value}:{first_id}",
                )
            )
        if rows:
            self.session.execute(_insert_ignoring_duplicates(rows))


def plan_notifications(session: Session) -> None:
    """Строит сообщения по записям истории этой транзакции; вызывать перед commit.

    Берёт накопленное в `session.info[PENDING_KEY]`, сбрасывает его (у записей появляются id) и
    вычисляет адресатов по текущему состоянию БД. Пустой список или откатанные записи — ничего.
    """
    pending: list[AuditLog] | None = session.info.pop(PENDING_KEY, None)
    if not pending:
        return
    session.flush()
    entries = sorted((e for e in pending if e in session and e.id is not None), key=lambda e: e.id)
    if entries:
        _Planner(session, entries).run()
        session.flush()


def forget_pending(session: Session, previous_transaction: SessionTransaction) -> None:
    """Откат корневой транзакции: накопленные записи истории исчезли вместе с ней.

    Подключён к `after_soft_rollback`: `after_rollback` срабатывает и на откат SAVEPOINT и тогда
    стёр бы историю внешней транзакции. Откат savepoint записи оставляет: откатанные из них
    планировщик отсеивает сам (`e in session`).
    """
    if previous_transaction.parent is None:
        session.info.pop(PENDING_KEY, None)


__all__ = ["PENDING_KEY", "forget_pending", "notify", "notify_many", "plan_notifications"]
