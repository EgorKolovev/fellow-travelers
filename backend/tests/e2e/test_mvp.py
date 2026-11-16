"""Сквозная проверка MVP (docs/capabilities.md, «Проверка MVP», шаги 1–9, часть бэкенда).

Всё идёт через HTTP API. Поставщик рейсов — `FakeFlightProvider` через `dependency_overrides`,
опрос — прямой вызов `poll_due_trips`, пересчёт — `process_next`, отправка уведомлений —
`send_due` с тестовым отправителем, удаление — `purge_expired`. Время проходов задаётся явно.
Каждый ответ проверяется по контракту (`call`), после каждого действия — инварианты БД и истории.

Чего здесь нет: экраны (шаг 10), стартовое сообщение и кнопки бота, обращение в настоящий
Telegram, настоящий поставщик рейсов, цикл воркера и флаг `RETENTION_ENABLED` (это тесты воркера).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_flight_provider
from app.flights import FakeFlightProvider, ProviderFlight
from app.models import (
    Application,
    AuditLog,
    BackgroundJob,
    Event,
    Membership,
    Notification,
    NotificationStatus,
    Transfer,
    Trip,
    TripStatus,
    User,
)
from app.services.flight_polling import PollResult, poll_due_trips
from app.services.jobs import JobOutcome, process_next
from app.services.notification_sender import SendResult, send_due
from app.services.retention import purge_expired
from app.services.users import TelegramProfile
from tests.api.conftest import auth_headers
from tests.e2e.test_pre_mvp import (
    ADMIN,
    DESTINATION,
    MEETING,
    PICKUP,
    STRANGER,
    Person,
    act,
    add_trip,
    admin_participants,
    admin_transfers,
    apply,
    assert_invariants,
    by_name,
    call,
    create_event,
    join,
    mark,
    member_names,
    my_application,
    my_trip,
    share_phone,
)
from tests.helpers.audit_replay import audit_rows
from tests.helpers.jobs import shared_session_factory
from tests.helpers.notifications import deliver_pending

AVATAR = "https://t.me/i/userpic/320/alice.jpg"
# рейс поставщика: LED -> SVO, вылет 07:00 и прилёт 10:00 по Москве
DEP = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
ARR = datetime(2026, 11, 12, 7, 0, tzinfo=UTC)
EVENT_PATH = "/events/{event_id}"
ADMIN_TRANSFERS = "/events/{event_id}/admin/transfers"
MEMBERS = "/events/{event_id}/admin/transfers/{transfer_id}/members"


@dataclass(frozen=True)
class Traveler(Person):
    """Участник с необязательными username и аватаром (как их передаёт initData)."""

    has_username: bool = True
    photo_url: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        fields: dict[str, Any] = {
            "first_name": self.name,
            "username": f"u{self.telegram_id}" if self.has_username else None,
        }
        if self.photo_url is not None:
            fields["photo_url"] = self.photo_url
        return auth_headers(self.telegram_id, **fields)

    @property
    def profile(self) -> TelegramProfile:
        return TelegramProfile(
            telegram_id=self.telegram_id,
            first_name=self.name,
            username=f"u{self.telegram_id}" if self.has_username else None,
            photo_url=self.photo_url,
        )


def crew(count: int, start: int = 3000) -> list[Traveler]:
    return [Traveler(start + i, f"Гость{start + i}", f"+7900000{start + i}") for i in range(count)]


@pytest.fixture
def provider(app: FastAPI) -> FakeFlightProvider:
    fake = FakeFlightProvider([flight()])
    app.dependency_overrides[get_flight_provider] = lambda: fake
    return fake


def flight(ref: str = "ref-500", number: str = "SU500", **overrides: Any) -> ProviderFlight:
    fields: dict[str, Any] = {
        "provider_ref": ref,
        "number": number,
        "departure_iata": "LED",
        "arrival_iata": "SVO",
        "scheduled_departure": DEP,
        "scheduled_arrival": ARR,
        "estimated_arrival": None,
        "status": TripStatus.SCHEDULED,
    }
    fields.update(overrides)
    return ProviderFlight(**fields)


@dataclass
class Outbox:
    """Тестовый отправитель: записывает вызовы; `errors` — что бросать для чата."""

    calls: list[tuple[int, str, str]] = field(default_factory=list)
    errors: dict[int, BaseException] = field(default_factory=dict)

    async def send(self, chat_id: int, text: str, button_url: str) -> None:
        self.calls.append((chat_id, text, button_url))
        error = self.errors.get(chat_id)
        if error is not None:
            raise error


# ---------------------------------------------------------------- хелперы


async def join_all(c: AsyncClient, s: AsyncSession, people: list[Traveler], code: str) -> None:
    for who in people:
        await join(c, s, who, code)


async def search(
    c: AsyncClient, who: Person, event_id: str, *, number: str = "SU500", expect: int = 200
) -> Response:
    return await call(
        c,
        "GET",
        "/flights/search",
        who,
        expect=expect,
        params={"event_id": event_id, "number": number, "date": "2026-11-12"},
    )


async def poll(
    s: AsyncSession, fake: FakeFlightProvider, now: datetime, *trip_ids: str
) -> PollResult:
    """Рейсам даётся срок «пора» и идёт один проход опроса в момент `now`."""
    await s.execute(
        update(Trip)
        .where(Trip.id.in_([uuid.UUID(t) for t in trip_ids]))
        .values(next_poll_at=now - timedelta(minutes=1))
    )
    result = await poll_due_trips(shared_session_factory(s), fake, now=now)
    s.expire_all()
    return result


async def run_jobs(s: AsyncSession) -> int:
    """Обрабатывает все готовые задания пересчёта; каждое должно закончиться успехом."""
    factory = shared_session_factory(s)
    done = 0
    while (outcome := await process_next(factory)) is not None:
        assert outcome == JobOutcome.DONE
        done += 1
        assert done < 20
    s.expire_all()
    return done


async def set_departed_at(
    s: AsyncSession,
    application_id: str,
    moment: datetime | None = None,
    *,
    ago: timedelta | None = None,
) -> None:
    """Время «Уехал»: `ago` назад по часам БД (как считает окно код) или заданный `moment`."""
    value = func.now() - ago if ago is not None else moment
    await s.execute(
        update(Application)
        .where(Application.id == uuid.UUID(application_id))
        .values(departed_at=value)
    )
    s.expire_all()


async def rows(s: AsyncSession, event_id: str, *, after: int = 0) -> list[tuple[int, Notification]]:
    """Строки `notifications` группы с номером больше `after`: (telegram_id, строка)."""
    stmt = (
        select(User.telegram_id, Notification)
        .join(User, User.id == Notification.user_id)
        .where(Notification.event_id == uuid.UUID(event_id), Notification.id > after)
        .order_by(Notification.id)
        .execution_options(populate_existing=True)
    )
    return [(row[0], row[1]) for row in (await s.execute(stmt)).all()]


async def last_id(s: AsyncSession, event_id: str) -> int:
    found = await rows(s, event_id)
    return found[-1][1].id if found else 0


async def kinds(s: AsyncSession, event_id: str, *, after: int = 0) -> dict[int, str]:
    """Вид сообщения по telegram_id; два сообщения одному человеку — ошибка."""
    result: dict[int, str] = {}
    for telegram_id, row in await rows(s, event_id, after=after):
        assert telegram_id not in result, "одному человеку два сообщения"
        result[telegram_id] = row.kind
    return result


def ids(*people: Person) -> set[int]:
    return {p.telegram_id for p in people}


async def transfers_by_id(c: AsyncClient, event_id: str) -> dict[str, dict[str, Any]]:
    return {str(t["id"]): t for t in await admin_transfers(c, event_id)}


# ---------------------------------------------------------------- шаг 1


async def test_step1_full_path_with_provider_flight(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    """Телефон, группа с точками и координатами, приглашение, поиск рейса, заявки, трансфер,
    карта и Яндекс Go, «Я на месте», «Уехал», аватар попутчика (CAP-01)."""
    c, s = api_client, db_session
    alice = Traveler(951, "Алиса", "+79000000951", photo_url=AVATAR)
    boris = Traveler(952, "Борис", "+79000000952")
    await share_phone(s, ADMIN)
    event_id, code = await create_event(c, "Путь целиком", pickup_point=PICKUP)
    await join(c, s, alice, code)
    await join(c, s, boris, code)
    me = (await call(c, "GET", "/me", alice, expect=200)).json()
    assert me["photo_url"] == AVATAR

    # поиск рейса: рейс сохранён группе как рейс поставщика; повтор дублей не создаёт
    found = await search(c, alice, event_id)
    (trip,) = found.json()
    assert (trip["source"], trip["number"], trip["status"]) == ("provider", "SU500", "scheduled")
    assert trip["event_id"] == event_id
    again = await search(c, boris, event_id, number="su 500")
    assert [t["id"] for t in again.json()] == [trip["id"]]
    listed = await call(c, "GET", "/events/{event_id}/trips", ADMIN, expect=200, event_id=event_id)
    assert [t["id"] for t in listed.json()] == [trip["id"]]

    # заявки двух участников на этот рейс: второй сразу получает трансфер
    first = await apply(c, s, event_id, alice, trip["id"])
    assert first.json()["status"] == "searching"
    second = await apply(c, s, event_id, boris, trip["id"])
    assert second.json()["status"] == "assigned"
    transfer_id = second.json()["transfer_id"]
    assert second.json()["trip"]["source"] == "provider"

    # моя поездка: карта места сбора и заказ такси заполнены (координаты есть у всех точек)
    for who in (alice, boris):
        view = await my_trip(c, event_id, who)
        assert view["application"]["trip"]["id"] == trip["id"]
        assert view["transfer"]["id"] == transfer_id
        assert view["meeting_map_url"].startswith("https://")
        assert view["taxi"]["yandex_go_url"].startswith("https://")
        assert view["taxi"]["from"] == PICKUP
        assert view["taxi"]["to"] == DESTINATION
    # аватар Алисы виден Борису в контакте попутчика
    boris_view = await my_trip(c, event_id, boris)
    (mate,) = [m for m in boris_view["transfer"]["members"] if not m["is_me"]]
    assert mate["contact"]["first_name"] == "Алиса"
    assert mate["contact"]["photo_url"] == AVATAR
    (own,) = [m for m in boris_view["transfer"]["members"] if m["is_me"]]
    assert own["contact"]["photo_url"] is None

    # «Я на месте» и «Уехал» видны попутчику и админу
    await mark(c, s, event_id, alice, at_meeting_point=True)
    left = await mark(c, s, event_id, alice, departed=True)
    (alice_for_boris,) = [
        m for m in (await my_trip(c, event_id, boris))["transfer"]["members"] if not m["is_me"]
    ]
    assert alice_for_boris["at_meeting_point"] is True
    assert alice_for_boris["departed_at"] == left.json()["departed_at"]
    counters = (await admin_participants(c, event_id))["counters"]
    assert (counters["at_meeting_point"], counters["departed"]) == (1, 1)


# ---------------------------------------------------------------- шаг 2


async def test_step2_two_events_of_one_creator_and_access(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Две группы одного создателя, его участие в своей группе, права админа, закрытие входа."""
    c, s = api_client, db_session
    alice, boris = crew(2)
    await share_phone(s, ADMIN)
    event_a, code_a = await create_event(c, "Группа А")
    event_b, _ = await create_event(c, "Группа Б")
    trip_a = await add_trip(c, event_a, "SVO", "2026-11-12T10:00:00", "SU 210")
    mine = (await call(c, "GET", "/me/events", ADMIN, expect=200)).json()
    assert {(e["id"], e["is_admin"], e["is_participant"]) for e in mine} == {
        (event_a, True, False),
        (event_b, True, False),
    }

    # создатель не участник: заявку подать нельзя, пока не вступит по своему приглашению
    denied = await apply(c, s, event_a, ADMIN, trip_a, expect=403)
    assert denied.json()["error"]["code"] == "FORBIDDEN"
    await join(c, s, ADMIN, code_a)
    mine = {e["id"]: e for e in (await call(c, "GET", "/me/events", ADMIN, expect=200)).json()}
    assert (mine[event_a]["is_admin"], mine[event_a]["is_participant"]) == (True, True)
    assert (mine[event_b]["is_admin"], mine[event_b]["is_participant"]) == (True, False)
    submitted = await apply(c, s, event_a, ADMIN, trip_a)
    assert submitted.json()["status"] == "searching"
    # участие не отнимает прав админа
    await call(c, "GET", "/events/{event_id}/admin/board", ADMIN, expect=200, event_id=event_a)

    # участник: 403 на админских ручках своей группы, 404 в чужой группе
    await join(c, s, alice, code_a)
    two = [str(uuid.uuid4()), str(uuid.uuid4())]
    for who, expected in ((alice, 403), (STRANGER, 404)):
        if who is STRANGER:
            await share_phone(s, who)
        for template in (
            "/events/{event_id}/admin/board",
            "/events/{event_id}/admin/transfers",
        ):
            await call(c, "GET", template, who, expect=expected, event_id=event_a)
        await call(
            c,
            "POST",
            ADMIN_TRANSFERS,
            who,
            expect=expected,
            json={"application_ids": two},
            event_id=event_a,
        )
    await call(c, "GET", "/events/{event_id}/admin/board", alice, expect=404, event_id=event_b)
    assert (await call(c, "GET", "/me/events", alice, expect=200)).json()[0]["id"] == event_a

    # закрытое вступление: превью открывается, вступить нельзя (CAP-02)
    await share_phone(s, boris)
    patched = await call(
        c, "PATCH", EVENT_PATH, ADMIN, expect=200, json={"join_open": False}, event_id=event_a
    )
    assert patched.json()["join_open"] is False
    preview = await call(c, "GET", "/invites/{code}", boris, expect=200, code=code_a)
    assert (preview.json()["join_open"], preview.json()["already_member"]) == (False, False)
    closed = await call(c, "POST", "/invites/{code}/join", boris, expect=409, code=code_a)
    assert closed.json()["error"]["code"] == "JOIN_CLOSED"
    assert (await call(c, "GET", "/me/events", boris, expect=200)).json() == []
    # уже вступившие группу видят; после открытия вступить можно
    await call(c, "GET", EVENT_PATH, alice, expect=200, event_id=event_a)
    await call(
        c, "PATCH", EVENT_PATH, ADMIN, expect=200, json={"join_open": True}, event_id=event_a
    )
    await call(c, "POST", "/invites/{code}/join", boris, expect=201, code=code_a)


# ---------------------------------------------------------------- шаг 3


async def test_step3_capacity_and_companion(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Вместимость")
    t10 = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 310")
    t15 = await add_trip(c, event_id, "SVO", "2026-11-12T15:00:00", "SU 315")
    t20 = await add_trip(c, event_id, "SVO", "2026-11-12T20:00:00", "SU 320")
    people = crew(11)
    await join_all(c, s, people, code)
    a, b, cc, d, e, f, g, h, i, j, k = people

    # 3 пассажира и 2 места багажа — один трансфер; четвёртый пассажир не садится
    await apply(c, s, event_id, a, t10, baggage=1)
    await apply(c, s, event_id, b, t10, baggage=1)
    third = await apply(c, s, event_id, cc, t10)
    fourth = await apply(c, s, event_id, d, t10)
    assert third.json()["status"] == "assigned"
    assert (fourth.json()["status"], fourth.json()["transfer_id"]) == ("searching", None)
    transfer = (await transfers_by_id(c, event_id))[third.json()["transfer_id"]]
    assert (transfer["passengers_total"], transfer["baggage_total"]) == (3, 2)
    assert len(transfer["members"]) == 3

    # багаж 3 > 2: по пассажирам место есть, по багажу нет
    await apply(c, s, event_id, e, t15, baggage=2)
    pair = await apply(c, s, event_id, f, t15)
    luggage = await apply(c, s, event_id, g, t15, baggage=1)
    assert pair.json()["status"] == "assigned"
    assert (luggage.json()["status"], luggage.json()["transfer_id"]) == ("searching", None)
    assert (await transfers_by_id(c, event_id))[pair.json()["transfer_id"]]["baggage_total"] == 2

    # спутник — это два пассажира в одной заявке: двое со спутниками вместе (4 > 3) не едут
    await apply(c, s, event_id, h, t20, companion=True)
    other = await apply(c, s, event_id, i, t20, companion=True)
    assert (other.json()["status"], other.json()["passengers"]) == ("searching", 2)
    await apply(c, s, event_id, j, t20)
    await apply(c, s, event_id, k, t20)
    everything = list((await transfers_by_id(c, event_id)).values())
    assert len(everything) == 4
    for item in everything:
        assert item["passengers_total"] <= 3
        assert item["baggage_total"] <= 2
    cars = [sorted(m["passengers"] for m in t["members"]) for t in everything]
    assert sorted(cars) == [[1, 1], [1, 1, 1], [1, 2], [1, 2]]
    for who in (h, i):
        assert (await my_application(c, event_id, who))["status"] == "assigned"


# ---------------------------------------------------------------- шаг 4


async def test_step4_participant_edits_assigned_application(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Правка заявки")
    trip = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 410")
    a, b, v, d = crew(4, 3100)
    await join_all(c, s, [a, b, v, d], code)
    await apply(c, s, event_id, a, trip)
    pair = await apply(c, s, event_id, b, trip)
    transfer_id = pair.json()["transfer_id"]
    triple = await apply(c, s, event_id, v, trip)
    assert triple.json()["transfer_id"] == transfer_id
    waiting = await apply(c, s, event_id, d, trip)
    assert (waiting.json()["status"], waiting.json()["transfer_id"]) == ("searching", None)

    # совместимая правка (багаж 1 помещается): остаётся в том же трансфере
    edited = await apply(c, s, event_id, a, trip, baggage=1, expect=200)
    assert (edited.json()["status"], edited.json()["transfer_id"]) == ("assigned", transfer_id)
    assert edited.json()["baggage_count"] == 1

    # несовместимая правка (другой рейс, на три часа позже): заявка выходит, двое остальных
    # остаются вместе в том же трансфере, на освободившееся место садится ждущий Д
    late_trip = await add_trip(c, event_id, "SVO", "2026-11-12T13:00:00", "SU 413")
    left = await apply(c, s, event_id, a, late_trip, expect=200)
    assert (left.json()["status"], left.json()["transfer_id"]) == ("searching", None)
    transfers = await transfers_by_id(c, event_id)
    assert set(transfers) == {transfer_id}
    assert member_names(transfers[transfer_id]) == sorted([b.name, v.name, d.name])
    assert (await my_application(c, event_id, d))["transfer_id"] == transfer_id
    history = await audit_rows(s, uuid.UUID(event_id))
    assert ("transfer_left", "application_changed", "participant") in [
        (r.action, r.reason, r.actor_kind) for r in history
    ]
    assert ("transfer_joined", "auto_fill", "system") in [
        (r.action, r.reason, r.actor_kind) for r in history
    ]


async def test_step4_edited_application_does_not_take_freed_seat_from_waiting(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Правка со спутником")
    trip = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 411")
    a, b, v, d = crew(4, 3150)
    await join_all(c, s, [a, b, v, d], code)
    await apply(c, s, event_id, a, trip)
    transfer_id = (await apply(c, s, event_id, b, trip)).json()["transfer_id"]
    await apply(c, s, event_id, v, trip)
    await apply(c, s, event_id, d, trip)

    # со спутником заявка А даёт 4 пассажира: она выходит, ждущий Д должен занять её место
    left = await apply(c, s, event_id, a, trip, companion=True, expect=200)
    assert (left.json()["status"], left.json()["transfer_id"]) == ("searching", None)
    assert (await my_application(c, event_id, d))["transfer_id"] == transfer_id


async def test_step4_admin_builds_and_edits_manual_transfer(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    await share_phone(s, ADMIN)
    event_id, code = await create_event(c, "Ручной состав")
    t16 = await add_trip(c, event_id, "SVO", "2026-11-12T16:00:00", "SU 416")
    t19 = await add_trip(c, event_id, "SVO", "2026-11-12T19:00:00", "SU 419")
    t21 = await add_trip(c, event_id, "SVO", "2026-11-12T21:30:00", "SU 421")
    p, q, r, w, x, y, z = crew(7, 3200)
    await join_all(c, s, [p, q, r, w, x, y, z], code)
    await apply(c, s, event_id, p, t16)
    pair = await apply(c, s, event_id, q, t16)
    assert pair.json()["status"] == "assigned"

    def post(who: Person, members: list[str], expect: int) -> Any:
        return call(
            c,
            "POST",
            ADMIN_TRANSFERS,
            who,
            expect=expect,
            json={"application_ids": members},
            event_id=event_id,
        )

    async def app_id(who: Person) -> str:
        return str((await my_application(c, event_id, who))["id"])

    # админ собирает ручной трансфер из тех же двоих; автоматический распускается
    manual = await post(ADMIN, [await app_id(p), await app_id(q)], 201)
    manual_id = manual.json()["id"]
    assert member_names(manual.json()) == sorted([p.name, q.name])
    assert list(await transfers_by_id(c, event_id)) == [manual_id]

    # ручной трансфер автоматически не дополняется: новая заявка остаётся в поиске
    waiting = await apply(c, s, event_id, r, t16)
    assert (waiting.json()["status"], waiting.json()["transfer_id"]) == ("searching", None)
    auto = await apply(c, s, event_id, w, t16, companion=True)
    assert auto.json()["status"] == "assigned"  # R + W (3 пассажира) собрались сами
    auto_id = auto.json()["transfer_id"]
    assert auto_id != manual_id

    # вместимость: ручной состав из 5 пассажиров (Р и В со спутником из автоматического трансфера)
    over = await call(
        c,
        "PUT",
        MEMBERS,
        ADMIN,
        expect=409,
        json={
            "application_ids": [await app_id(p), await app_id(q), await app_id(r), await app_id(w)]
        },
        event_id=event_id,
        transfer_id=manual_id,
    )
    assert over.json()["error"]["code"] == "CAPACITY_EXCEEDED"
    # отказ ничего не меняет
    assert member_names((await transfers_by_id(c, event_id))[manual_id]) == sorted([p.name, q.name])
    assert (await my_application(c, event_id, w))["transfer_id"] == auto_id

    # ожидание: Х прилетает на три часа позже, ждать не готов
    await apply(c, s, event_id, x, t19, wait=0)
    incompatible = await call(
        c,
        "PUT",
        MEMBERS,
        ADMIN,
        expect=409,
        json={"application_ids": [await app_id(p), await app_id(q), await app_id(x)]},
        event_id=event_id,
        transfer_id=manual_id,
    )
    assert incompatible.json()["error"]["code"] == "INCOMPATIBLE_TRANSFER"
    two_far = await post(ADMIN, [await app_id(x), await app_id(r)], 409)
    assert two_far.json()["error"]["code"] == "INCOMPATIBLE_TRANSFER"

    # ошибки ввода: одна заявка; заявка «еду сам»
    await post(ADMIN, [await app_id(x)], 422)
    await apply(c, s, event_id, y, t21)
    await act(c, s, event_id, y, "go-solo")
    await post(ADMIN, [await app_id(x), await app_id(y)], 422)
    await post(p, [await app_id(x), await app_id(r)], 403)
    await post(STRANGER, [await app_id(x), await app_id(r)], 404)

    # состав меняется: Р в ручном трансфере, автоматический остаётся без неё
    changed = await call(
        c,
        "PUT",
        MEMBERS,
        ADMIN,
        expect=200,
        json={"application_ids": [await app_id(p), await app_id(q), await app_id(r)]},
        event_id=event_id,
        transfer_id=manual_id,
    )
    assert member_names(changed.json()) == sorted([p.name, q.name, r.name])
    leftover = await my_application(c, event_id, w)
    assert (leftover["status"], leftover["transfer_id"]) == ("searching", None)
    assert list(await transfers_by_id(c, event_id)) == [manual_id]

    # пустой список распускает трансфер: заявки снова идут в подбор и собираются заново
    await call(
        c,
        "PUT",
        MEMBERS,
        ADMIN,
        expect=200,
        json={"application_ids": []},
        event_id=event_id,
        transfer_id=manual_id,
    )
    transfers = await transfers_by_id(c, event_id)
    assert manual_id not in transfers
    # состав новых машин зависит от идентификаторов, но минимум трое из четырёх снова собраны
    states = [await my_application(c, event_id, who) for who in (p, q, r, w)]
    assert all(state["transfer_id"] != manual_id for state in states)
    assert sum(state["status"] == "assigned" for state in states) >= 3
    await assert_invariants(s, uuid.UUID(event_id))
    history = await audit_rows(s, uuid.UUID(event_id))
    assert "admin" in {row.actor_kind for row in history}


# ---------------------------------------------------------------- шаг 5


async def test_step5_undo_departure_window_and_departed_not_matched(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Отъезд")
    t10 = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 510")
    t14 = await add_trip(c, event_id, "SVO", "2026-11-12T14:00:00", "SU 514")
    a, b, cc, d = crew(4, 3300)
    await join_all(c, s, [a, b, cc, d], code)
    await apply(c, s, event_id, a, t10)
    transfer_id = (await apply(c, s, event_id, b, t10)).json()["transfer_id"]

    # отмена в пределах 30 минут: заявка возвращается в трансфер
    gone = await mark(c, s, event_id, a, departed=True)
    app_id = gone.json()["id"]
    await set_departed_at(s, app_id, ago=timedelta(minutes=29))
    back = await mark(c, s, event_id, a, departed=False)
    assert back.json()["departed_at"] is None
    assert (back.json()["status"], back.json()["transfer_id"]) == ("assigned", transfer_id)

    # через 31 минуту: отмена и любая правка заявки отклоняются
    await mark(c, s, event_id, a, departed=True)
    await set_departed_at(s, app_id, ago=timedelta(minutes=31))
    late = await call(
        c,
        "PUT",
        "/events/{event_id}/my-application/marks",
        a,
        expect=409,
        json={"departed": False},
        event_id=event_id,
    )
    assert late.json()["error"]["code"] == "ALREADY_DEPARTED"
    await apply(c, s, event_id, a, t10, expect=409)
    assert (await my_application(c, event_id, a))["departed_at"] is not None

    # уехавший не попадает в подбор: В уехал, Г с тем же рейсом остаётся в поиске
    await apply(c, s, event_id, cc, t14)
    await mark(c, s, event_id, cc, departed=True)
    waiting = await apply(c, s, event_id, d, t14)
    assert (waiting.json()["status"], waiting.json()["transfer_id"]) == ("searching", None)


async def test_step5_admin_marks_departure(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Отъезд админом")
    t10 = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 520")
    t14 = await add_trip(c, event_id, "SVO", "2026-11-12T14:00:00", "SU 524")
    a, b, cc, d = crew(4, 3400)
    await join_all(c, s, [a, b, cc, d], code)
    await apply(c, s, event_id, a, t10)
    transfer_id = (await apply(c, s, event_id, b, t10)).json()["transfer_id"]
    await apply(c, s, event_id, cc, t14, companion=True)
    cc_id = str((await my_application(c, event_id, cc))["id"])

    # заявка отъезжает по отметке админа (со спутником одной заявкой); повтор идемпотентен
    path = "/events/{event_id}/admin/applications/{application_id}/departed"
    first = await call(c, "POST", path, ADMIN, expect=200, event_id=event_id, application_id=cc_id)
    assert first.json()["departed_at"] is not None
    assert first.json()["passengers"] == 2
    repeat = await call(c, "POST", path, ADMIN, expect=200, event_id=event_id, application_id=cc_id)
    assert repeat.json()["departed_at"] == first.json()["departed_at"]
    await call(c, "POST", path, a, expect=403, event_id=event_id, application_id=cc_id)

    # целый трансфер: все отмечены, повтор идемпотентен
    tpath = "/events/{event_id}/admin/transfers/{transfer_id}/departed"
    done = await call(
        c, "POST", tpath, ADMIN, expect=200, event_id=event_id, transfer_id=transfer_id
    )
    assert done.json()["status"] == "departed"
    assert all(m["departed_at"] is not None for m in done.json()["members"])
    again = await call(
        c, "POST", tpath, ADMIN, expect=200, event_id=event_id, transfer_id=transfer_id
    )
    assert again.json()["status"] == "departed"
    assert [m["departed_at"] for m in again.json()["members"]] == [
        m["departed_at"] for m in done.json()["members"]
    ]
    for who in (a, b):
        assert (await my_application(c, event_id, who))["departed_at"] is not None
    # уехавших подбор не трогает: новая заявка на рейс остаётся одна
    late = await apply(c, s, event_id, d, t10)
    assert (late.json()["status"], late.json()["transfer_id"]) == ("searching", None)
    await call(c, "POST", tpath, STRANGER, expect=404, event_id=event_id, transfer_id=transfer_id)


async def test_step5_board_counts_people_and_filters_by_trip_status(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Табло")
    t_a = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 530")
    t_b = await add_trip(c, event_id, "SVO", "2026-11-12T15:00:00", "SU 535")
    t_c = await add_trip(c, event_id, "SVO", "2026-11-12T20:00:00", "SU 540")
    a, b, v = crew(3, 3500)
    await join_all(c, s, [a, b, v], code)
    await apply(c, s, event_id, a, t_a, companion=True)
    await apply(c, s, event_id, b, t_b)
    await apply(c, s, event_id, v, t_c)
    await act(c, s, event_id, v, "cancel")
    await s.execute(update(Trip).where(Trip.id == uuid.UUID(t_b)).values(status=TripStatus.DELAYED))
    s.expire_all()

    path = "/events/{event_id}/admin/board"
    board = (await call(c, "GET", path, ADMIN, expect=200, event_id=event_id)).json()
    # люди со спутниками; отменённая заявка в «ожидаются» не входит
    assert board["counters"]["expected"] == 3
    assert board["counters"]["searching"] == 3
    by_trip = {item["trip"]["id"]: item for item in board["trips"]}
    assert by_trip[t_a]["people"] == 2
    assert by_trip[t_b]["people"] == 1
    assert t_c not in by_trip or by_trip[t_c]["people"] == 0

    delayed = await call(
        c, "GET", path, ADMIN, expect=200, params={"trip_status": "delayed"}, event_id=event_id
    )
    assert [item["trip"]["id"] for item in delayed.json()["trips"]] == [t_b]
    scheduled = await call(
        c, "GET", path, ADMIN, expect=200, params={"trip_status": "scheduled"}, event_id=event_id
    )
    assert t_b not in [item["trip"]["id"] for item in scheduled.json()["trips"]]
    assert t_a in [item["trip"]["id"] for item in scheduled.json()["trips"]]
    both = await call(
        c,
        "GET",
        path,
        ADMIN,
        expect=200,
        params={"trip_status": ["delayed", "scheduled"]},
        event_id=event_id,
    )
    assert {t_a, t_b} <= {item["trip"]["id"] for item in both.json()["trips"]}
    await call(
        c, "GET", path, ADMIN, expect=422, params={"trip_status": "bogus"}, event_id=event_id
    )
    await call(c, "GET", path, a, expect=403, event_id=event_id)


# ---------------------------------------------------------------- шаг 6


async def test_step6_provider_unavailable_manual_trip_still_works(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Поставщик недоступен")
    alice = crew(1, 3600)[0]
    await join(c, s, alice, code)

    provider.unavailable = True
    down = await search(c, alice, event_id, expect=503)
    assert down.json()["error"]["code"] == "FLIGHT_PROVIDER_UNAVAILABLE"
    manual = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 610")
    submitted = await apply(c, s, event_id, alice, manual)
    assert submitted.json()["trip"]["source"] == "manual"

    provider.unavailable = False
    (found,) = (await search(c, alice, event_id)).json()
    assert found["source"] == "provider"


async def test_step6_provider_answer_overwrites_manual_edit(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Свежий ответ")
    alice = crew(1, 3610)[0]
    await join(c, s, alice, code)
    (trip,) = (await search(c, alice, event_id)).json()
    await apply(c, s, event_id, alice, trip["id"])

    # админ правит рейс вручную
    patched = await call(
        c,
        "PATCH",
        "/trips/{trip_id}",
        ADMIN,
        expect=200,
        json={"estimated_arrival": "2026-11-12T11:00:00"},
        trip_id=trip["id"],
    )
    assert patched.json()["estimated_arrival"] is not None

    # свежий ответ поставщика (расчётного времени нет) возвращает его данные
    result = await poll(s, provider, datetime(2026, 11, 11, 12, 0, tzinfo=UTC), trip["id"])
    assert (result.polled, result.changed) == (1, 1)
    listed = await call(c, "GET", "/events/{event_id}/trips", ADMIN, expect=200, event_id=event_id)
    (fresh,) = listed.json()
    assert fresh["estimated_arrival"] is None
    assert fresh["source"] == "provider"
    updates = [
        (row.actor_kind, row.reason, row.details["changed_fields"])
        for row in await audit_rows(s, uuid.UUID(event_id))
        if row.action == "trip_updated" and row.reason == "provider_update"
    ]
    assert updates == [("system", "provider_update", ["estimated_arrival"])]
    await run_jobs(s)


async def test_step6_poll_schedule_follows_d2(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Интервалы")
    alice = crew(1, 3620)[0]
    await join(c, s, alice, code)
    (trip,) = (await search(c, alice, event_id)).json()
    await apply(c, s, event_id, alice, trip["id"])

    cases = [
        # (момент опроса, следующий опрос)
        (datetime(2026, 11, 10, 12, 0, tzinfo=UTC), timedelta(minutes=60)),  # до вылета 40 ч
        (datetime(2026, 11, 11, 12, 0, tzinfo=UTC), timedelta(minutes=30)),  # до вылета 16 ч
        (datetime(2026, 11, 12, 0, 0, tzinfo=UTC), timedelta(minutes=10)),  # до вылета 4 ч
    ]
    for now, interval in cases:
        result = await poll(s, provider, now, trip["id"])
        assert (result.polled, result.changed) == (1, 0)
        stored = await s.get(Trip, uuid.UUID(trip["id"]), populate_existing=True)
        assert stored is not None
        assert stored.next_poll_at == now + interval
    # после вылета пауза до «прибытие минус 30 минут» (06:30 UTC)
    await poll(s, provider, datetime(2026, 11, 12, 5, 0, tzinfo=UTC), trip["id"])
    stored = await s.get(Trip, uuid.UUID(trip["id"]), populate_existing=True)
    assert stored is not None
    assert stored.next_poll_at == ARR - timedelta(minutes=30)
    # посадка прекращает опрос
    provider.add(flight(status=TripStatus.LANDED))
    await poll(s, provider, datetime(2026, 11, 12, 6, 40, tzinfo=UTC), trip["id"])
    stored = await s.get(Trip, uuid.UUID(trip["id"]), populate_existing=True)
    assert stored is not None
    assert stored.next_poll_at is None
    await run_jobs(s)


async def test_step6_cancelled_flight_removes_passengers_and_they_are_not_matched(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Отмена рейса")
    alice, boris, carol = crew(3, 3630)
    await join_all(c, s, [alice, boris, carol], code)
    (trip,) = (await search(c, alice, event_id)).json()
    manual = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 630")
    await apply(c, s, event_id, alice, trip["id"], wait=15)
    paired = await apply(c, s, event_id, boris, manual, wait=15)
    assert paired.json()["status"] == "assigned"

    provider.add(flight(status=TripStatus.CANCELLED))
    result = await poll(s, provider, datetime(2026, 11, 11, 12, 0, tzinfo=UTC), trip["id"])
    assert result.changed == 1
    assert await run_jobs(s) >= 1
    for who in (alice, boris):
        application = await my_application(c, event_id, who)
        assert (application["status"], application["transfer_id"]) == ("searching", None)
    assert await admin_transfers(c, event_id) == []
    history = [(r.action, r.reason) for r in await audit_rows(s, uuid.UUID(event_id))]
    assert ("transfer_left", "flight_cancelled") in history

    # заявка на отменённый рейс не подбирается: Борис и Карол собираются без Алисы
    mate = await apply(c, s, event_id, carol, manual, wait=15)
    assert mate.json()["status"] == "assigned"
    (transfer,) = await admin_transfers(c, event_id)
    assert member_names(transfer) == sorted([boris.name, carol.name])
    assert (await my_application(c, event_id, alice))["transfer_id"] is None


@pytest.mark.parametrize(
    ("with_carol", "stay_kind"), [(True, "transfer_changed"), (False, "transfer_left")]
)
async def test_step8_cancellation_messages_across_poll_and_reconcile(
    api_client: AsyncClient,
    db_session: AsyncSession,
    provider: FakeFlightProvider,
    with_carol: bool,
    stay_kind: str,
) -> None:
    """Опрос видит отмену, задание пересчёта выводит пассажира рейса из трансфера (Д8).

    Ему `flight_cancelled`, оставшимся — `transfer_changed` (трое → двое) или `transfer_left`
    (пара).
    """
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Сообщения об отмене", pickup_point=PICKUP)
    alice, boris, carol = crew(3, 3850)
    await join_all(c, s, [alice, boris, carol], code)
    (trip,) = (await search(c, alice, event_id)).json()
    manual = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 851")
    await apply(c, s, event_id, alice, trip["id"], wait=15)
    await apply(c, s, event_id, boris, manual, wait=15)
    if with_carol:
        await apply(c, s, event_id, carol, manual, wait=15)
    (transfer,) = await admin_transfers(c, event_id)
    assert len(transfer["members"]) == (3 if with_carol else 2)
    await deliver_pending(
        s, uuid.UUID(event_id)
    )  # накопленное «ушло»: новые события — новые строки
    mark_id = await last_id(s, event_id)

    provider.add(flight(status=TripStatus.CANCELLED))
    result = await poll(s, provider, datetime(2026, 11, 11, 12, 0, tzinfo=UTC), trip["id"])
    assert result.changed == 1
    assert await run_jobs(s) >= 1

    expected = {alice.telegram_id: "flight_cancelled", boris.telegram_id: stay_kind}
    if with_carol:
        expected[carol.telegram_id] = stay_kind
    assert await kinds(s, event_id, after=mark_id) == expected
    await assert_invariants(s, uuid.UUID(event_id))


# ---------------------------------------------------------------- шаг 7


async def test_step7_phone_username_and_contact_visibility(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Контакты")
    t10 = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 710")
    t15 = await add_trip(c, event_id, "SVO", "2026-11-12T15:00:00", "SU 715")
    t21 = await add_trip(c, event_id, "SVO", "2026-11-12T21:30:00", "SU 721")
    dasha = Traveler(3701, "Даша", "+79000003701", has_username=False)
    eva = Traveler(3702, "Эва", "+79000003702")
    vera = Traveler(3703, "Вера", "+79000003703")
    gleb = Traveler(3704, "Глеб", "+79000003704")
    await join_all(c, s, [dasha, eva, vera, gleb], code)

    # без телефона заявку подать нельзя; после передачи телефона можно
    await s.execute(update(User).where(User.telegram_id == gleb.telegram_id).values(phone=None))
    s.expire_all()
    assert (await call(c, "GET", "/me", gleb, expect=200)).json()["has_phone"] is False
    denied = await apply(c, s, event_id, gleb, t15, expect=403)
    assert denied.json()["error"]["code"] == "PHONE_REQUIRED"
    await share_phone(s, gleb)
    ok = await apply(c, s, event_id, gleb, t15)
    assert ok.json()["status"] == "searching"

    # участник без username подаёт заявку, попутчик видит его телефон и пустой username
    await apply(c, s, event_id, dasha, t10)
    paired = await apply(c, s, event_id, eva, t10)
    assert paired.json()["status"] == "assigned"
    view = await my_trip(c, event_id, eva)
    (mate,) = [m for m in view["transfer"]["members"] if not m["is_me"]]
    assert (mate["contact"]["first_name"], mate["contact"]["username"]) == ("Даша", None)
    assert mate["contact"]["phone"] == dasha.phone
    # общего чата нет, остальное работает
    assert view["event"]["chat_url"] is None

    # телефоны видны только своим попутчикам и админу
    await apply(c, s, event_id, vera, t21, wait=0)
    stranger_view = await call(
        c, "GET", "/events/{event_id}/my-trip", gleb, expect=200, event_id=event_id
    )
    for hidden in (dasha, eva, vera):
        assert hidden.phone not in stranger_view.text
    vera_view = await call(
        c, "GET", "/events/{event_id}/my-trip", vera, expect=200, event_id=event_id
    )
    assert dasha.phone not in vera_view.text
    assert eva.phone not in vera_view.text
    people = by_name(await admin_participants(c, event_id))
    assert people["Даша"]["contact"]["phone"] == dasha.phone
    assert people["Даша"]["contact"]["username"] is None
    (transfer,) = await admin_transfers(c, event_id)
    assert {m["contact"]["phone"] for m in transfer["members"]} == {dasha.phone, eva.phone}


# ---------------------------------------------------------------- шаг 8


async def test_step8_notifications_recipients_content_and_delivery(
    api_client: AsyncClient, db_session: AsyncSession, provider: FakeFlightProvider
) -> None:
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Уведомления", pickup_point=PICKUP)
    alice, boris, vera = crew(3, 3800)
    await join_all(c, s, [alice, boris, vera], code)
    (trip,) = (await search(c, alice, event_id)).json()
    manual = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 810")

    # подбор пары: сообщение получает тот, кто ждал, автор действия — нет
    await apply(c, s, event_id, alice, trip["id"], wait=30)
    assert await kinds(s, event_id) == {}
    paired = await apply(c, s, event_id, boris, manual, wait=30)
    assert paired.json()["status"] == "assigned"
    assert await kinds(s, event_id) == {alice.telegram_id: "transfer_assigned"}

    # сдвиг прибытия на 20 минут: пассажиру рейса и его попутчику по трансферу
    await deliver_pending(s, uuid.UUID(event_id))  # сообщение о подборе ушло: дальше новые строки
    mark_id = await last_id(s, event_id)
    provider.add(flight(estimated_arrival=ARR + timedelta(minutes=20)))
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    result = await poll(s, provider, now, trip["id"])
    assert result.changed == 1
    assert await kinds(s, event_id, after=mark_id) == {
        alice.telegram_id: "flight_changed",
        boris.telegram_id: "flight_changed",
    }
    # повторный опрос без изменений новых сообщений не создаёт
    total = len(await rows(s, event_id))
    again = await poll(s, provider, now + timedelta(hours=1), trip["id"])
    assert (again.polled, again.changed) == (1, 0)
    assert len(await rows(s, event_id)) == total

    # правка точек: участникам с активной заявкой; отменившая и без заявки сообщений не получают
    await apply(c, s, event_id, vera, manual, wait=0)
    await act(c, s, event_id, vera, "cancel")
    await deliver_pending(s, uuid.UUID(event_id))
    mark_id = await last_id(s, event_id)
    await call(
        c,
        "PATCH",
        EVENT_PATH,
        ADMIN,
        expect=200,
        json={"meeting_point": {**MEETING, "address": "Терминал D, выход 5"}},
        event_id=event_id,
    )
    assert await kinds(s, event_id, after=mark_id) == {
        alice.telegram_id: "points_changed",
        boris.telegram_id: "points_changed",
    }

    # отправка: бот Бориса заблокирован, Алисе всё уходит; текст без телефонов и имён,
    # кнопка ведёт в «Мою поездку»
    all_rows = await rows(s, event_id, after=mark_id)  # строки до `mark_id` уже «ушли»
    outbox = Outbox(
        errors={
            boris.telegram_id: TelegramForbiddenError(
                method=SendMessage(chat_id=boris.telegram_id, text="x"),
                message="Forbidden: bot was blocked by the user",
            )
        }
    )
    later = datetime.now(UTC) + timedelta(hours=1)
    sent: SendResult = await send_due(shared_session_factory(s), outbox, now=later)
    to_alice = [r for tg, r in all_rows if tg == alice.telegram_id]
    to_boris = [r for tg, r in all_rows if tg == boris.telegram_id]
    assert (sent.sent, sent.skipped, sent.failed) == (len(to_alice), len(to_boris), 0)
    secrets = [p.phone for p in (alice, boris, vera)] + [p.name for p in (alice, boris, vera)]
    secrets += [f"u{p.telegram_id}" for p in (alice, boris, vera)] + [ADMIN.phone]
    assert len(outbox.calls) == len(all_rows)
    for chat_id, text, button_url in outbox.calls:
        assert chat_id in ids(alice, boris)
        assert button_url == f"https://t.me/test_bot?startapp=event_{event_id}"
        assert "Уведомления" in text
        assert not any(secret in text for secret in secrets)
    statuses = {(tg, row.status) for tg, row in await rows(s, event_id, after=mark_id)}
    assert statuses == {
        (alice.telegram_id, NotificationStatus.SENT),
        (boris.telegram_id, NotificationStatus.SKIPPED),
    }


# ---------------------------------------------------------------- шаг 9


async def test_step9_retention_deletes_expired_group_only(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    c, s = api_client, db_session
    alice, boris = crew(2, 3900)
    old_id, old_code = await create_event(c, "Старая группа")  # ends_on 2026-11-14
    new_id, _ = await create_event(c, "Новая группа", starts_on="2026-12-20", ends_on="2026-12-30")
    new_code = str(
        (
            await call(c, "GET", "/events/{event_id}/invite", ADMIN, expect=200, event_id=new_id)
        ).json()["code"]
    )
    old_trip = await add_trip(c, old_id, "SVO", "2026-11-12T10:00:00", "SU 910")
    new_trip = await add_trip(c, new_id, "SVO", "2026-11-12T10:00:00", "SU 911")
    await join_all(c, s, [alice, boris], old_code)
    await join(c, s, alice, new_code)
    await apply(c, s, old_id, alice, old_trip)
    await apply(c, s, old_id, boris, old_trip)
    await apply(c, s, new_id, alice, new_trip)
    gone = await mark(c, s, old_id, alice, departed=True)
    # последняя отметка «Уехал» — 20 ноября: срок — 27 ноября 10:00 UTC, позже срока по ends_on
    await set_departed_at(s, gone.json()["id"], datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    factory = shared_session_factory(s)

    # до порога ничего не удаляется (22 ноября 12:00 UTC — срок только по ends_on)
    assert await purge_expired(factory, now=datetime(2026, 11, 22, 13, 0, tzinfo=UTC)) == 0
    assert await purge_expired(factory, now=datetime(2026, 11, 27, 9, 59, tzinfo=UTC)) == 0
    await call(c, "GET", "/invites/{code}", alice, expect=200, code=old_code)

    assert await purge_expired(factory, now=datetime(2026, 11, 27, 10, 1, tzinfo=UTC)) == 1
    s.expire_all()

    # группа удалена целиком; профиль и телефон остались
    old = uuid.UUID(old_id)
    for model in (Application, Transfer, Trip, AuditLog, Notification, BackgroundJob, Membership):
        count = await s.scalar(select(func.count()).select_from(model).where(model.event_id == old))
        assert count == 0, model.__name__
    assert await s.get(Event, old) is None
    me = (await call(c, "GET", "/me", alice, expect=200)).json()
    assert me["phone"] == alice.phone
    # старые ссылки
    invite = await call(c, "GET", "/invites/{code}", alice, expect=404, code=old_code)
    assert invite.json()["error"]["code"] == "INVITE_NOT_FOUND"
    gone_trip = await call(
        c, "GET", "/events/{event_id}/my-trip", alice, expect=404, event_id=old_id
    )
    assert gone_trip.json()["error"]["code"] == "NOT_FOUND"
    # участие в другой группе цело
    listed = (await call(c, "GET", "/me/events", alice, expect=200)).json()
    assert [e["id"] for e in listed] == [new_id]
    assert (await my_application(c, new_id, alice))["status"] == "searching"
    assert (await call(c, "GET", "/me/events", boris, expect=200)).json() == []
    await assert_invariants(s, uuid.UUID(new_id))
