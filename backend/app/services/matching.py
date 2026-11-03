import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import BigInteger, bindparam, delete, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.matching import (
    JoinTransfer,
    MatchCandidate,
    OpenTransfer,
    find_placement,
    members_to_exclude,
)
from app.models import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditReason,
    Transfer,
    TransferOrigin,
    TransferStatus,
    Trip,
)
from app.services.audit import SYSTEM, Actor, record

_UINT64_RANGE = 1 << 64
_INT64_MAX = (1 << 63) - 1


def event_lock_key(event_id: uuid.UUID) -> int:
    """Детерминированный signed int64 из UUID группы: старшие 64 бита `event_id.int`."""
    key = event_id.int >> 64
    return key - _UINT64_RANGE if key > _INT64_MAX else key


async def lock_event(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Транзакционная advisory-блокировка группы: подбор и выход из трансфера идут по очереди.

    Каждый сценарий, меняющий заявки или трансферы группы, берёт её первой и только потом читает
    состояние (с `populate_existing`), иначе заявка могла бы попасть в два трансфера.
    Блокировка снимается вместе с транзакцией, то есть на commit/rollback сценария.
    """
    stmt = text("SELECT pg_advisory_xact_lock(:key)").bindparams(
        bindparam("key", event_lock_key(event_id), type_=BigInteger)
    )
    await session.execute(stmt)


def _candidate(application: Application, trip: Trip) -> MatchCandidate:
    return MatchCandidate(
        id=application.id,
        arrival_location_id=trip.arrival_location_id,
        arrival_at=trip.effective_arrival,
        passengers=2 if application.with_companion else 1,
        baggage=application.baggage_count,
        max_wait=timedelta(minutes=application.max_wait_minutes),
    )


@dataclass(frozen=True, slots=True)
class FillResult:
    """Итог `fill_event`: сколько заявок сели в существующие трансферы и сколько собрали новые."""

    joined: int
    created: int


async def _open_transfers(
    session: AsyncSession, event_id: uuid.UUID
) -> tuple[dict[uuid.UUID, Transfer], list[OpenTransfer]]:
    """Трансферы группы, которые можно дополнить: active, origin=auto, никто не уехал.

    Состав берётся свежим из БД вместе с рейсами. Трансфер меньше чем из двух заявок
    (по инвариантам такого нет) не дополняем: `OpenTransfer` обещает минимум две.
    """
    departed_member = aliased(Application)
    transfers = list(
        (
            await session.scalars(
                select(Transfer)
                .where(
                    Transfer.event_id == event_id,
                    Transfer.status == TransferStatus.ACTIVE,
                    Transfer.origin == TransferOrigin.AUTO,
                    ~exists().where(
                        departed_member.transfer_id == Transfer.id,
                        departed_member.departed_at.is_not(None),
                    ),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not transfers:
        return {}, []
    rows = (
        await session.execute(
            select(Application, Trip)
            .join(Trip, Trip.id == Application.trip_id)
            .where(Application.transfer_id.in_([transfer.id for transfer in transfers]))
            .execution_options(populate_existing=True)
        )
    ).all()
    members: defaultdict[uuid.UUID | None, list[MatchCandidate]] = defaultdict(list)
    for app, trip in rows:
        members[app.transfer_id].append(_candidate(app, trip))
    open_transfers = [
        OpenTransfer(transfer.id, tuple(members[transfer.id]))
        for transfer in transfers
        if len(members[transfer.id]) >= 2
    ]
    return {transfer.id: transfer for transfer in transfers}, open_transfers


async def match_application(
    session: AsyncSession,
    application: Application,
    *,
    reason: AuditReason = AuditReason.AUTO_MATCH,
) -> Transfer | None:
    """Размещает заявку в `searching`: в открытый трансфер или в новую группу; только flush.

    Вызывать под `lock_event`. Решение принимает `matching.find_placement`: открытые трансферы
    группы (active, origin=auto, никто не уехал) и свободные заявки (searching, без отъезда и
    трансфера) читаются свежими из БД. Возвращает трансфер, в который села заявка, либо None, если
    она осталась в `searching` (или не подходит для подбора).

    Посадка в открытый трансфер пишет `transfer_joined`, новая группа — `transfer_created`;
    оба от системы с причиной `reason`.
    """
    if (
        application.status != ApplicationStatus.SEARCHING
        or application.departed_at is not None
        or application.transfer_id is not None
    ):
        return None
    own_trip = (
        await session.execute(
            select(Trip).where(Trip.id == application.trip_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    transfers, open_transfers = await _open_transfers(session, application.event_id)
    rows = (
        await session.execute(
            select(Application, Trip)
            .join(Trip, Trip.id == Application.trip_id)
            .where(
                Application.event_id == application.event_id,
                Application.id != application.id,
                Application.status == ApplicationStatus.SEARCHING,
                Application.departed_at.is_(None),
                Application.transfer_id.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    by_id = {app.id: app for app, _ in rows}
    decision = find_placement(
        _candidate(application, own_trip),
        open_transfers,
        (_candidate(app, trip) for app, trip in rows),
    )
    if decision is None:
        return None
    if isinstance(decision, JoinTransfer):
        transfer = transfers[decision.transfer_id]
        # status и transfer_id меняются вместе: один UPDATE, CHECK assigned_has_transfer цел
        application.status = ApplicationStatus.ASSIGNED
        application.transfer_id = transfer.id
        await session.flush()
        record(
            session,
            event_id=application.event_id,
            action=AuditAction.TRANSFER_JOINED,
            actor=SYSTEM,
            reason=reason,
            application=application,
            transfer_id=transfer.id,
        )
        return transfer
    transfer = Transfer(event_id=application.event_id, origin=TransferOrigin.AUTO)
    session.add(transfer)
    await session.flush()
    for member in decision.members:
        target = application if member.id == application.id else by_id[member.id]
        # status и transfer_id меняются вместе: один UPDATE, CHECK assigned_has_transfer цел
        target.status = ApplicationStatus.ASSIGNED
        target.transfer_id = transfer.id
    await session.flush()
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.TRANSFER_CREATED,
        actor=SYSTEM,
        reason=reason,
        transfer_id=transfer.id,
        details={"application_ids": [str(member.id) for member in decision.members]},
    )
    return transfer


async def fill_event(session: AsyncSession, event_id: uuid.UUID) -> FillResult:
    """Размещает ждущие заявки группы; только flush, без commit. Вызывать под `lock_event`.

    Обходит заявки в `searching` без отъезда и трансфера по порядку подачи `(created_at, id)` и
    ведёт каждую через `match_application` с причиной `auto_fill`. Заявка перед проходом читается
    заново: её могла уже посадить предыдущая (в новую группу или в трансфер). Совместимость
    замкнута по подмножествам, поэтому одного прохода хватает: повторный вызов ничего не меняет
    и ничего не пишет в историю.
    """
    waiting = (
        await session.scalars(
            select(Application.id)
            .where(
                Application.event_id == event_id,
                Application.status == ApplicationStatus.SEARCHING,
                Application.departed_at.is_(None),
                Application.transfer_id.is_(None),
            )
            .order_by(Application.created_at, Application.id)
        )
    ).all()
    # трансфер, которого не было до прохода, считается созданным, остальные — дополненными
    known = set(
        (await session.scalars(select(Transfer.id).where(Transfer.event_id == event_id))).all()
    )
    joined = created = 0
    for application_id in waiting:
        application = await session.get(Application, application_id, populate_existing=True)
        if application is None:
            continue
        transfer = await match_application(session, application, reason=AuditReason.AUTO_FILL)
        if transfer is None:
            continue
        if transfer.id in known:
            joined += 1
        else:
            created += 1
            known.add(transfer.id)
    return FillResult(joined=joined, created=created)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Итог `reconcile_event`: исключено заявок, расформировано трансферов, размещено заявок."""

    excluded: int
    dissolved: int
    placed: int


async def reconcile_event(
    session: AsyncSession, event_id: uuid.UUID, *, changed_trip_ids: frozenset[uuid.UUID]
) -> ReconcileResult:
    """Приводит активные трансферы группы в соответствие с рейсами; только flush, без commit.

    Вызывать под `lock_event` после правки времени прибытия или аэропорта рейсов
    `changed_trip_ids`. Активные трансферы (любой origin) берутся по `(created_at, id)` вместе с
    участниками и рейсами, свежими из БД. Уехавшие не исключаются и в совместимости не учитываются:
    `members_to_exclude` получает только ждущих, а заявки на изменённых рейсах как `changed`.
    Исключённые уходят через `leave_transfer` в `searching` от системы с причиной
    `incompatible_after_change`; трансфер с остатком меньше двух заявок расформировывается.

    Сначала исключения по всем трансферам, и только потом один `fill_event`: иначе заявка из
    поиска села бы в трансфер, который ещё не проверен. Заполнение идёт и без исключений, потому
    что правка могла сделать совместимой заявку в поиске. Если исключать некого и подходящих
    заявок в поиске нет, ничего не меняется и в историю ничего не пишется, повторный вызов тоже.
    """
    transfer_ids = (
        await session.scalars(
            select(Transfer.id)
            .where(Transfer.event_id == event_id, Transfer.status == TransferStatus.ACTIVE)
            .order_by(Transfer.created_at, Transfer.id)
        )
    ).all()
    rows = (
        await session.execute(
            select(Application, Trip)
            .join(Trip, Trip.id == Application.trip_id)
            .where(Application.transfer_id.in_(transfer_ids))
            .order_by(Application.created_at, Application.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    members: defaultdict[uuid.UUID | None, list[tuple[Application, Trip]]] = defaultdict(list)
    for application, trip in rows:
        members[application.transfer_id].append((application, trip))

    excluded = 0
    for transfer_id in transfer_ids:
        waiting = [(app, trip) for app, trip in members[transfer_id] if app.departed_at is None]
        changed = frozenset(app.id for app, _ in waiting if app.trip_id in changed_trip_ids)
        by_id = {app.id: app for app, _ in waiting}
        for application_id in members_to_exclude(
            [_candidate(app, trip) for app, trip in waiting], changed
        ):
            await leave_transfer(
                session,
                by_id[application_id],
                new_status=ApplicationStatus.SEARCHING,
                actor=SYSTEM,
                reason=AuditReason.INCOMPATIBLE_AFTER_CHANGE,
            )
            excluded += 1
    survived = set(
        (await session.scalars(select(Transfer.id).where(Transfer.id.in_(transfer_ids)))).all()
    )
    filled = await fill_event(session, event_id)
    return ReconcileResult(
        excluded=excluded,
        dissolved=len(transfer_ids) - len(survived),
        placed=filled.joined + filled.created,
    )


async def leave_transfer(
    session: AsyncSession,
    application: Application,
    *,
    new_status: ApplicationStatus,
    actor: Actor,
    reason: AuditReason,
) -> None:
    """Снимает заявку с трансфера и выставляет ей `new_status` (solo/cancelled/searching); flush.

    Если в трансфере осталось меньше двух заявок, он расформировывается: оставшаяся заявка
    возвращается в `searching` (если она уже уехала, становится `solo` и не подбирается).
    Иначе состав остаётся как есть. Замену и новое место для оставшихся ищет не эта функция,
    а вызывающий сценарий: `fill_event` после выхода.

    В историю пишутся `transfer_left` уходящей заявки с переданными `actor` и `reason`; при
    расформировании — `transfer_left` каждой оставшейся (причина `member_left`, тот же `actor`)
    и одна `transfer_dissolved`; если оставшиеся все уехали — `transfer_departed`.

    Порядок важен из-за CHECK `assigned_has_transfer` и FK без отложенной проверки: уходящая заявка
    меняет transfer_id и status одним UPDATE и флашится первой; трансфер удаляется только после
    того, как на него никто не ссылается.
    """
    transfer_id = application.transfer_id
    if transfer_id is None:
        return
    remaining = list(
        (
            await session.scalars(
                select(Application)
                .where(Application.transfer_id == transfer_id, Application.id != application.id)
                .order_by(Application.created_at, Application.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    application.status = new_status
    application.transfer_id = None
    await session.flush()
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.TRANSFER_LEFT,
        actor=actor,
        reason=reason,
        application=application,
        transfer_id=transfer_id,
        details={
            "from_status": ApplicationStatus.ASSIGNED.value,
            "to_status": new_status.value,
        },
    )

    if len(remaining) >= 2:
        if all(app.departed_at is not None for app in remaining):
            await mark_transfer_departed(session, application.event_id, transfer_id, actor)
        return

    for app in remaining:
        # уехавший остаток едет сам: «Ищем попутчиков» ему показывать нельзя
        app.status = (
            ApplicationStatus.SOLO if app.departed_at is not None else ApplicationStatus.SEARCHING
        )
        app.transfer_id = None
        record(
            session,
            event_id=app.event_id,
            action=AuditAction.TRANSFER_LEFT,
            actor=actor,
            reason=AuditReason.MEMBER_LEFT,
            application=app,
            transfer_id=transfer_id,
            details={
                "from_status": ApplicationStatus.ASSIGNED.value,
                "to_status": app.status.value,
            },
        )
    await session.flush()
    await session.execute(delete(Transfer).where(Transfer.id == transfer_id))
    record(
        session,
        event_id=application.event_id,
        action=AuditAction.TRANSFER_DISSOLVED,
        actor=actor,
        reason=AuditReason.MEMBER_LEFT,
        transfer_id=transfer_id,
        details={"application_ids": [str(app.id) for app in remaining]},
    )


async def mark_transfer_departed(
    session: AsyncSession, event_id: uuid.UUID, transfer_id: uuid.UUID, actor: Actor
) -> None:
    """Переводит трансфер в `departed` и пишет `transfer_departed`; без commit.

    Вызывать, когда все его заявки уехали; `actor` — тот, чьё действие к этому привело.
    """
    await session.execute(
        update(Transfer).where(Transfer.id == transfer_id).values(status=TransferStatus.DEPARTED)
    )
    record(
        session,
        event_id=event_id,
        action=AuditAction.TRANSFER_DEPARTED,
        actor=actor,
        reason=AuditReason.ALL_DEPARTED,
        transfer_id=transfer_id,
    )


async def all_departed(session: AsyncSession, transfer_id: uuid.UUID) -> bool:
    waiting = await session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.transfer_id == transfer_id, Application.departed_at.is_(None))
    )
    return waiting == 0
