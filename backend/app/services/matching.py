import time
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
from app.observability import metrics
from app.observability.deferred import defer_increment, defer_observe
from app.observability.metrics import MatchTrigger, PlacementKind
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
    """Итог `fill_event`: сколько заявок село в существующие трансферы и сколько в новые.

    Считаются заявки, а не трансферы: посадка в открытый трансфер даёт 1, новая группа — её размер.
    """

    joined: int
    created: int

    @property
    def placed(self) -> int:
        """Сколько заявок размещено всего."""
        return self.joined + self.created


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


@dataclass(slots=True)
class _Pool:
    """Состояние группы для размещения: открытые трансферы и свободные заявки с кандидатами.

    Читается из БД один раз (`_load_pool`) и после каждого решения обновляется в памяти
    (`_place`), поэтому серия размещений не перечитывает группу заново.
    """

    transfers: dict[uuid.UUID, Transfer]
    open: dict[uuid.UUID, OpenTransfer]
    free: dict[uuid.UUID, tuple[Application, MatchCandidate]]  # по порядку (created_at, id)


@dataclass(frozen=True, slots=True)
class _Placed:
    transfer: Transfer
    kind: PlacementKind
    applications: int  # сколько заявок разместило решение: join — 1, новая группа — её размер


async def _load_pool(
    session: AsyncSession, event_id: uuid.UUID, *, skip: uuid.UUID | None = None
) -> _Pool:
    """Открытые трансферы и свободные заявки группы (searching, без отъезда и трансфера).

    Всё свежее из БД, два запроса и ещё один при наличии открытых трансферов. `skip` — заявка,
    которую размещают отдельно: среди свободных её нет.
    """
    transfers, open_transfers = await _open_transfers(session, event_id)
    stmt = (
        select(Application, Trip)
        .join(Trip, Trip.id == Application.trip_id)
        .where(
            Application.event_id == event_id,
            Application.status == ApplicationStatus.SEARCHING,
            Application.departed_at.is_(None),
            Application.transfer_id.is_(None),
        )
        .order_by(Application.created_at, Application.id)
        .execution_options(populate_existing=True)
    )
    if skip is not None:
        stmt = stmt.where(Application.id != skip)
    rows = (await session.execute(stmt)).all()
    return _Pool(
        transfers=transfers,
        open={transfer.id: transfer for transfer in open_transfers},
        free={app.id: (app, _candidate(app, trip)) for app, trip in rows},
    )


async def _place(
    session: AsyncSession,
    application: Application,
    candidate: MatchCandidate,
    pool: _Pool,
    reason: AuditReason,
) -> _Placed | None:
    """Размещает одну заявку по решению `find_placement` и обновляет `pool`; None — ждёт.

    Общий путь `match_application` и `fill_event`: запись в БД (только flush), история и
    состояние `pool`. Посадка в открытый трансфер пишет `transfer_joined`, новая группа —
    `transfer_created`; оба от системы с причиной `reason`. Метрики считает вызывающий.
    """
    decision = find_placement(
        candidate, pool.open.values(), (member for _, member in pool.free.values())
    )
    if decision is None:
        return None
    if isinstance(decision, JoinTransfer):
        transfer = pool.transfers[decision.transfer_id]
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
        pool.free.pop(application.id, None)
        members = (*pool.open[transfer.id].members, candidate)
        pool.open[transfer.id] = OpenTransfer(transfer.id, members)
        return _Placed(transfer, PlacementKind.JOIN, 1)
    transfer = Transfer(event_id=application.event_id, origin=TransferOrigin.AUTO)
    session.add(transfer)
    await session.flush()
    for member in decision.members:
        target = application if member.id == application.id else pool.free[member.id][0]
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
    for member in decision.members:
        pool.free.pop(member.id, None)
    pool.transfers[transfer.id] = transfer
    pool.open[transfer.id] = OpenTransfer(transfer.id, decision.members)
    kind = PlacementKind.NEW_TRIPLE if len(decision.members) >= 3 else PlacementKind.NEW_PAIR
    return _Placed(transfer, kind, len(decision.members))


def _observe_match(
    session: AsyncSession, trigger: MatchTrigger, kind: PlacementKind, seconds: float
) -> None:
    """Учитывает размещение в метриках после commit сценария.

    Отказ (`none`) считается только для `application`: это явная попытка пользователя. Проход
    `fill_event` пробует каждую ждущую заявку, и такие отказы дали бы сотни рядов на одну отмену,
    поэтому для `leave` и `reconcile` считаются только настоящие посадки.
    """
    if kind == PlacementKind.NONE and trigger != MatchTrigger.APPLICATION:
        return
    defer_increment(session, metrics.match_placements_total.labels(trigger.value, kind.value))
    defer_observe(session, metrics.match_duration_seconds.labels(trigger.value), seconds)


async def match_application(
    session: AsyncSession,
    application: Application,
    *,
    reason: AuditReason = AuditReason.AUTO_MATCH,
    trigger: MatchTrigger = MatchTrigger.APPLICATION,
) -> Transfer | None:
    """Размещает заявку в `searching`: в открытый трансфер или в новую группу; только flush.

    Вызывать под `lock_event`. Решение принимает `matching.find_placement`: открытые трансферы
    группы (active, origin=auto, никто не уехал) и свободные заявки (searching, без отъезда и
    трансфера) читаются свежими из БД. Возвращает трансфер, в который села заявка, либо None, если
    она осталась в `searching` (или не подходит для подбора).

    Посадка в открытый трансфер пишет `transfer_joined`, новая группа — `transfer_created`;
    оба от системы с причиной `reason`. `trigger` попадает только в метрики: результат и
    длительность учитываются после commit сценария (`ft_match_placements_total`,
    `ft_match_duration_seconds`).
    """
    if (
        application.status != ApplicationStatus.SEARCHING
        or application.departed_at is not None
        or application.transfer_id is not None
    ):
        return None
    started = time.perf_counter()
    own_trip = (
        await session.execute(
            select(Trip).where(Trip.id == application.trip_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    pool = await _load_pool(session, application.event_id, skip=application.id)
    placed = await _place(session, application, _candidate(application, own_trip), pool, reason)
    _observe_match(
        session,
        trigger,
        placed.kind if placed is not None else PlacementKind.NONE,
        time.perf_counter() - started,
    )
    return placed.transfer if placed is not None else None


async def fill_event(
    session: AsyncSession, event_id: uuid.UUID, *, trigger: MatchTrigger
) -> FillResult:
    """Размещает ждущие заявки группы; только flush, без commit. Вызывать под `lock_event`.

    Открытые трансферы и заявки в `searching` без отъезда и трансфера читаются один раз, затем
    заявки обходятся по порядку подачи `(created_at, id)`: каждая проходит то же размещение, что и
    в `match_application`, с причиной `auto_fill`, а решение сразу отражается в состоянии в
    памяти (новый член трансфера, группа выходит из свободных и сама становится открытым
    трансфером). Число запросов поэтому не зависит от числа ждущих заявок. Заявка, которую уже
    посадило предыдущее решение, пропускается. Совместимость замкнута по подмножествам, поэтому
    одного прохода хватает: повторный вызов ничего не меняет и ничего не пишет в историю.

    `trigger` идёт в метрики: `leave` после выхода из трансфера, `reconcile` в конце пересчёта.
    Они считают только настоящие посадки, без отказов (`_observe_match`).
    """
    pool = await _load_pool(session, event_id)
    joined = created = 0
    for application_id in list(pool.free):
        entry = pool.free.get(application_id)
        if entry is None:
            continue  # уже посажена одним из прошлых решений
        started = time.perf_counter()
        application, candidate = entry
        placed = await _place(session, application, candidate, pool, AuditReason.AUTO_FILL)
        _observe_match(
            session,
            trigger,
            placed.kind if placed is not None else PlacementKind.NONE,
            time.perf_counter() - started,
        )
        if placed is None:
            continue
        if placed.kind == PlacementKind.JOIN:
            joined += placed.applications
        else:
            created += placed.applications
    return FillResult(joined=joined, created=created)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Итог `reconcile_event`: исключено заявок, расформировано трансферов, размещено заявок.

    `placed` — заявки, севшие в трансферы при заполнении (`FillResult.placed`), а не трансферы.
    """

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
    Если `changed_trip_ids` из одного рейса, его id попадает в записи истории этих выходов
    (`trip_id`): по ним видно, какая правка рейса вызвала исключение.

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

    # одна правка рейса — одна причина: её id попадает в историю; несколько рейсов сразу — без id
    cause_trip_id = next(iter(changed_trip_ids)) if len(changed_trip_ids) == 1 else None
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
                trip_id=cause_trip_id,
            )
            excluded += 1
    survived = set(
        (await session.scalars(select(Transfer.id).where(Transfer.id.in_(transfer_ids)))).all()
    )
    filled = await fill_event(session, event_id, trigger=MatchTrigger.RECONCILE)
    return ReconcileResult(
        excluded=excluded,
        dissolved=len(transfer_ids) - len(survived),
        placed=filled.placed,
    )


async def leave_transfer(
    session: AsyncSession,
    application: Application,
    *,
    new_status: ApplicationStatus,
    actor: Actor,
    reason: AuditReason,
    trip_id: uuid.UUID | None = None,
) -> None:
    """Снимает заявку с трансфера и выставляет ей `new_status` (solo/cancelled/searching); flush.

    Если в трансфере осталось меньше двух заявок, он расформировывается: оставшаяся заявка
    возвращается в `searching` (если она уже уехала, становится `solo` и не подбирается).
    Иначе состав остаётся как есть. Замену и новое место для оставшихся ищет не эта функция,
    а вызывающий сценарий: `fill_event` после выхода.

    В историю пишутся `transfer_left` уходящей заявки с переданными `actor` и `reason`; при
    расформировании — `transfer_left` каждой оставшейся (причина `member_left`, тот же `actor`)
    и одна `transfer_dissolved`; если оставшиеся все уехали — `transfer_departed`. `trip_id`, если
    он передан, ставится во все эти записи, кроме `transfer_departed`: рейс, правка которого
    привела к выходу.

    Метрики повторяют историю и растут после commit: `ft_transfer_exclusions_total` на каждую
    запись `transfer_left` с её причиной, `ft_transfers_dissolved_total` на каждое расформирование
    с причиной выхода, с которого оно началось (`reason`, а не `member_left` из истории).

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
        trip_id=trip_id,
        details={
            "from_status": ApplicationStatus.ASSIGNED.value,
            "to_status": new_status.value,
        },
    )
    defer_increment(session, metrics.transfer_exclusions_total.labels(reason.value))

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
            trip_id=trip_id,
            details={
                "from_status": ApplicationStatus.ASSIGNED.value,
                "to_status": app.status.value,
            },
        )
        defer_increment(
            session, metrics.transfer_exclusions_total.labels(AuditReason.MEMBER_LEFT.value)
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
        trip_id=trip_id,
        details={"application_ids": [str(app.id) for app in remaining]},
    )
    defer_increment(session, metrics.transfers_dissolved_total.labels(reason.value))


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
