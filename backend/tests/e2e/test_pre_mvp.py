"""Сквозная проверка pre-MVP (docs/Capabilities_Poputchiki.md, «Проверка pre-MVP», шаги 1–6).

Всё идёт через HTTP API; телефон участники передают через сервис бота `save_phone`,
как бот после получения контакта. Каждый ответ проверяется по контракту.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Application, Transfer
from app.services.users import TelegramProfile, save_phone
from tests.api.conftest import auth_headers
from tests.helpers.audit_replay import assert_history_matches_transfers, audit_rows
from tests.helpers.contract import assert_contract

INSTRUCTION = "Встречаемся у стойки информации в терминале D, затем идём к парковке P1"
MEETING = {"address": "Терминал D, выход 3", "lat": 55.97, "lon": 37.41}
PICKUP = {"address": "Парковка P1", "lat": 55.971, "lon": 37.412}
DESTINATION = {"address": "Отель «Космос»", "lat": 55.82, "lon": 37.64}


@dataclass(frozen=True)
class Person:
    telegram_id: int
    name: str
    phone: str

    @property
    def headers(self) -> dict[str, str]:
        return auth_headers(self.telegram_id, first_name=self.name, username=f"u{self.telegram_id}")

    @property
    def profile(self) -> TelegramProfile:
        return TelegramProfile(
            telegram_id=self.telegram_id, first_name=self.name, username=f"u{self.telegram_id}"
        )


ADMIN = Person(900, "Админ", "+79000000900")
ALICE = Person(901, "Алиса", "+79000000901")  # А
BORIS = Person(902, "Борис", "+79000000902")  # Б
VERA = Person(903, "Вера", "+79000000903")  # В
GLEB = Person(904, "Глеб", "+79000000904")  # Г
DIMA = Person(905, "Дима", "+79000000905")  # Д
ERIK = Person(906, "Эрик", "+79000000906")  # новичок после отъезда
IGOR = Person(907, "Игорь", "+79000000907")
STRANGER = Person(990, "Чужой", "+79000000990")


# ---------------------------------------------------------------- хелперы


async def call(
    client: AsyncClient,
    method: str,
    template: str,
    who: Person,
    *,
    expect: int,
    params: dict[str, Any] | None = None,
    json: Any = None,
    **path: Any,
) -> Response:
    """Вызов API от имени `who`: проверка статуса и контракта; `path` подставляется в шаблон."""
    response = await client.request(
        method, template.format(**path), params=params, json=json, headers=who.headers
    )
    assert response.status_code == expect, response.text
    assert_contract(response, method, template)
    return response


async def share_phone(session: AsyncSession, who: Person) -> None:
    """Бот получил контакт и сохранил телефон."""
    user = await save_phone(session, who.profile, who.phone)
    assert user.phone == who.phone


async def airport_id(client: AsyncClient, code: str) -> str:
    response = await call(client, "GET", "/locations", ADMIN, expect=200, params={"q": code})
    (found,) = [item for item in response.json() if item["code"] == code]
    return str(found["id"])


async def create_event(client: AsyncClient, title: str, **extra: Any) -> tuple[str, str]:
    """Админ создаёт группу и берёт приглашение; возвращает (event_id, код)."""
    payload = {
        "title": title,
        "starts_on": "2026-11-12",
        "ends_on": "2026-11-14",
        "meeting_point": MEETING,
        "destination": DESTINATION,
        "meeting_instruction": INSTRUCTION,
        **extra,
    }
    created = await call(client, "POST", "/events", ADMIN, expect=201, json=payload)
    assert created.json()["is_admin"] is True
    event_id = str(created.json()["id"])
    invite = await call(
        client, "GET", "/events/{event_id}/invite", ADMIN, expect=200, event_id=event_id
    )
    code = str(invite.json()["code"])
    assert invite.json()["link"].endswith(f"?start=join_{code}")
    return event_id, code


async def add_trip(
    client: AsyncClient, event_id: str, arrival_code: str, arrival_local: str, number: str
) -> str:
    """Админ добавляет рейс вручную; время без смещения — по таймзоне аэропорта."""
    payload = {
        "mode": "flight",
        "number": number,
        "departure_location_id": await airport_id(client, "LED"),
        "arrival_location_id": await airport_id(client, arrival_code),
        "scheduled_departure": "2026-11-12T07:00:00",
        "scheduled_arrival": arrival_local,
    }
    response = await call(
        client,
        "POST",
        "/events/{event_id}/trips",
        ADMIN,
        expect=201,
        json=payload,
        event_id=event_id,
    )
    return str(response.json()["id"])


async def join(client: AsyncClient, session: AsyncSession, who: Person, code: str) -> None:
    """Телефон боту, превью приглашения (код в нижнем регистре), вступление."""
    await share_phone(session, who)
    preview = await call(client, "GET", "/invites/{code}", who, expect=200, code=code.lower())
    assert preview.json()["join_open"] is True
    assert preview.json()["already_member"] is False
    joined = await call(client, "POST", "/invites/{code}/join", who, expect=201, code=code.lower())
    assert joined.json()["is_participant"] is True
    assert joined.json()["id"] == preview.json()["event_id"]


async def apply(
    client: AsyncClient,
    session: AsyncSession,
    event_id: str,
    who: Person,
    trip_id: str,
    *,
    wait: int = 15,
    companion: bool = False,
    baggage: int = 0,
    expect: int = 201,
) -> Response:
    payload = {
        "trip_id": trip_id,
        "with_companion": companion,
        "baggage_count": baggage,
        "max_wait_minutes": wait,
    }
    response = await call(
        client,
        "PUT",
        "/events/{event_id}/my-application",
        who,
        expect=expect,
        json=payload,
        event_id=event_id,
    )
    await assert_invariants(session, uuid.UUID(event_id))
    return response


async def my_application(client: AsyncClient, event_id: str, who: Person) -> dict[str, Any]:
    response = await call(
        client, "GET", "/events/{event_id}/my-application", who, expect=200, event_id=event_id
    )
    data: dict[str, Any] = response.json()
    return data


async def act(
    client: AsyncClient, session: AsyncSession, event_id: str, who: Person, action: str
) -> Response:
    response = await call(
        client,
        "POST",
        f"/events/{{event_id}}/my-application/{action}",
        who,
        expect=200,
        event_id=event_id,
    )
    await assert_invariants(session, uuid.UUID(event_id))
    return response


async def mark(
    client: AsyncClient, session: AsyncSession, event_id: str, who: Person, **marks: bool
) -> Response:
    response = await call(
        client,
        "PUT",
        "/events/{event_id}/my-application/marks",
        who,
        expect=200,
        json=marks,
        event_id=event_id,
    )
    await assert_invariants(session, uuid.UUID(event_id))
    return response


async def my_trip(client: AsyncClient, event_id: str, who: Person) -> dict[str, Any]:
    response = await call(
        client, "GET", "/events/{event_id}/my-trip", who, expect=200, event_id=event_id
    )
    data: dict[str, Any] = response.json()
    return data


async def admin_participants(client: AsyncClient, event_id: str) -> dict[str, Any]:
    response = await call(
        client, "GET", "/events/{event_id}/admin/participants", ADMIN, expect=200, event_id=event_id
    )
    data: dict[str, Any] = response.json()
    return data


async def admin_transfers(client: AsyncClient, event_id: str) -> list[dict[str, Any]]:
    response = await call(
        client, "GET", "/events/{event_id}/admin/transfers", ADMIN, expect=200, event_id=event_id
    )
    data: list[dict[str, Any]] = response.json()
    return data


def member_names(transfer: dict[str, Any]) -> list[str]:
    return sorted(m["contact"]["first_name"] for m in transfer["members"])


def by_name(participants: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["contact"]["first_name"]: p for p in participants["participants"]}


async def assert_invariants(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Инварианты БД: assigned только с трансфером; в трансфере >= 2 заявок, <= 3 пассажиров,
    <= 2 мест багажа; заявка входит не более чем в один трансфер; история аудита восстанавливает
    те же составы трансферов."""
    session.expire_all()
    applications = (
        await session.scalars(select(Application).where(Application.event_id == event_id))
    ).all()
    members: dict[uuid.UUID, list[Application]] = {}
    seen: set[uuid.UUID] = set()
    for application in applications:
        assert (application.status == "assigned") == (application.transfer_id is not None)
        assert application.id not in seen
        seen.add(application.id)
        if application.transfer_id is not None:
            members.setdefault(application.transfer_id, []).append(application)
    transfer_ids = set(
        (await session.scalars(select(Transfer.id).where(Transfer.event_id == event_id))).all()
    )
    assert transfer_ids == set(members), "трансфер без заявок или заявка в чужом трансфере"
    for group in members.values():
        assert len(group) >= 2
        assert sum(2 if a.with_companion else 1 for a in group) <= 3
        assert sum(a.baggage_count for a in group) <= 2
    await assert_history_matches_transfers(session, event_id)


# ---------------------------------------------------------------- сценарий


async def test_pre_mvp_full_scenario(api_client: AsyncClient, db_session: AsyncSession) -> None:
    c, s = api_client, db_session

    # Шаг 1. Админ создаёт группу: адреса, текстовая инструкция; берёт приглашение.
    event_id, code = await create_event(c, "Конференция", pickup_point=PICKUP)

    # Шаг 1. Рейсы вручную: два в SVO с разницей 10 минут, один в DME, один в SVO на 3 часа позже.
    trip_a = await add_trip(c, event_id, "SVO", "2026-11-12T10:00:00", "SU 100")
    trip_b = await add_trip(c, event_id, "SVO", "2026-11-12T10:10:00", "SU 101")
    trip_v = await add_trip(c, event_id, "DME", "2026-11-12T10:00:00", "S7 200")
    trip_g = await add_trip(c, event_id, "SVO", "2026-11-12T13:00:00", "SU 300")
    trips = await call(c, "GET", "/events/{event_id}/trips", ADMIN, expect=200, event_id=event_id)
    assert len(trips.json()) == 4

    # Шаг 2. Участники передают телефон боту, открывают приглашение, вступают, видят группу.
    # Дима телефон пока не передаёт: превью доступно, вступить нельзя.
    for who in (ALICE, BORIS, VERA, GLEB):
        await join(c, s, who, code)
    await call(c, "GET", "/invites/{code}", DIMA, expect=200, code=code.lower())
    denied = await call(c, "POST", "/invites/{code}/join", DIMA, expect=403, code=code.lower())
    assert denied.json()["error"]["code"] == "PHONE_REQUIRED"
    preview = await call(c, "GET", "/invites/{code}", DIMA, expect=200, code=code)
    assert preview.json()["already_member"] is False
    assert (await call(c, "GET", "/me", DIMA, expect=200)).json()["has_phone"] is False

    # Шаг 2. После передачи телефона боту Дима вступает.
    await share_phone(s, DIMA)
    me = (await call(c, "GET", "/me", DIMA, expect=200)).json()
    assert (me["has_phone"], me["phone"]) == (True, DIMA.phone)
    joined = await call(c, "POST", "/invites/{code}/join", DIMA, expect=201, code=code.lower())
    assert joined.json()["id"] == event_id
    for who in (ALICE, BORIS, VERA, GLEB, DIMA):
        events = (await call(c, "GET", "/me/events", who, expect=200)).json()
        assert [(e["id"], e["is_admin"], e["is_participant"]) for e in events] == [
            (event_id, False, True)
        ]
        assert events[0]["my_application_status"] is None
    await call(c, "GET", "/events/{event_id}/my-application", DIMA, expect=404, event_id=event_id)

    # Шаг 3. А (SVO 10:00, ждёт 15) и Б (SVO 10:10, ждёт 0) собираются в одну пару.
    first = await apply(c, s, event_id, ALICE, trip_a, wait=15)
    assert first.json()["status"] == "searching"
    second = await apply(c, s, event_id, BORIS, trip_b, wait=0)
    assert second.json()["status"] == "assigned"
    pair_id = second.json()["transfer_id"]
    assert pair_id is not None

    # Шаг 3. В (DME) остаётся в поиске: другой аэропорт.
    vera = await apply(c, s, event_id, VERA, trip_v, wait=15)
    assert vera.json()["status"] == "searching"

    # Шаг 3. Г (SVO, +3 ч) остаётся в поиске.
    gleb = await apply(c, s, event_id, GLEB, trip_g, wait=15, baggage=1)
    assert gleb.json()["status"] == "searching"

    # Шаг 3. Д со спутником и 2 местами багажа на рейс Г: по ожиданию с Г совместим, пассажиров
    # 1 + 2 = 3 (влезают), но багаж 1 + 2 = 3 > 2 — вместе не собираются.
    dima = await apply(c, s, event_id, DIMA, trip_g, wait=15, companion=True, baggage=2)
    assert dima.json()["status"] == "searching"
    assert dima.json()["passengers"] == 2
    assert dima.json()["transfer_id"] is None
    assert (await my_trip(c, event_id, GLEB))["transfer"] is None

    # Шаг 2. «Повторный вход»: списки и заявка сохранились.
    events = (await call(c, "GET", "/me/events", ALICE, expect=200)).json()
    assert events[0]["my_application_status"] == "assigned"
    again = await my_application(c, event_id, ALICE)
    assert (again["status"], again["transfer_id"]) == ("assigned", pair_id)
    assert again["trip"]["id"] == trip_a
    assert again["max_wait_minutes"] == 15

    # Шаг 4. А и Б видят друг друга (имена, телефоны), инструкцию и taxi-точки.
    for viewer, other in ((ALICE, BORIS), (BORIS, ALICE)):
        trip = await my_trip(c, event_id, viewer)
        assert trip["event"]["meeting_instruction"] == INSTRUCTION
        assert trip["taxi"]["from"] == PICKUP
        assert trip["taxi"]["to"] == DESTINATION
        transfer = trip["transfer"]
        assert transfer["id"] == pair_id
        assert transfer["status"] == "active"
        assert transfer["passengers_total"] == 2
        assert member_names(transfer) == ["Алиса", "Борис"]
        (mine,) = [m for m in transfer["members"] if m["is_me"]]
        (theirs,) = [m for m in transfer["members"] if not m["is_me"]]
        assert mine["contact"]["first_name"] == viewer.name
        assert theirs["contact"]["first_name"] == other.name
        assert theirs["contact"]["phone"] == other.phone

    # Шаг 4. В не видит контактов А и Б.
    vera_trip = await call(
        c, "GET", "/events/{event_id}/my-trip", VERA, expect=200, event_id=event_id
    )
    assert vera_trip.json()["transfer"] is None
    for hidden in (ALICE, BORIS):
        assert hidden.phone not in vera_trip.text
        assert hidden.name not in vera_trip.text

    # Шаг 4. Админ видит всех; спутник считается за двоих; видит пару.
    participants = await admin_participants(c, event_id)
    assert participants["counters"] == {
        "expected": 6,
        "searching": 4,
        "assigned": 2,
        "solo": 0,
        "at_meeting_point": 0,
        "departed": 0,
    }
    people = by_name(participants)
    assert set(people) == {"Алиса", "Борис", "Вера", "Глеб", "Дима"}
    assert people["Дима"]["application"]["passengers"] == 2
    assert people["Алиса"]["application"]["transfer_id"] == pair_id
    assert people["Вера"]["contact"]["phone"] == VERA.phone
    transfers = await admin_transfers(c, event_id)
    assert [t["id"] for t in transfers] == [pair_id]
    assert member_names(transfers[0]) == ["Алиса", "Борис"]

    # Шаг 5. А: «Я на месте», затем «Уехал».
    on_place = await mark(c, s, event_id, ALICE, at_meeting_point=True)
    assert on_place.json()["at_meeting_point"] is True
    left = await mark(c, s, event_id, ALICE, departed=True)
    assert left.json()["departed_at"] is not None

    # Шаг 5. Б видит отметки А после обновления экрана.
    (alice_for_boris,) = [
        m for m in (await my_trip(c, event_id, BORIS))["transfer"]["members"] if not m["is_me"]
    ]
    assert alice_for_boris["at_meeting_point"] is True
    assert alice_for_boris["departed_at"] == left.json()["departed_at"]

    # Шаг 5. Админ тоже.
    participants = await admin_participants(c, event_id)
    assert participants["counters"]["at_meeting_point"] == 1
    assert participants["counters"]["departed"] == 1
    assert by_name(participants)["Алиса"]["application"]["departed_at"] is not None
    (alice_for_admin,) = [
        m
        for m in (await admin_transfers(c, event_id))[0]["members"]
        if m["contact"]["first_name"] == "Алиса"
    ]
    assert alice_for_admin["at_meeting_point"] is True
    assert alice_for_admin["departed_at"] is not None

    # Шаг 5. Уехавший исключён из подбора: Вера (DME, в поиске) уезжает, и новая совместимая
    # заявка Эрика на тот же рейс с ней не собирается.
    await mark(c, s, event_id, VERA, departed=True)
    await join(c, s, ERIK, code)
    erik = await apply(c, s, event_id, ERIK, trip_v, wait=15)
    assert (erik.json()["status"], erik.json()["transfer_id"]) == ("searching", None)
    vera_app = await my_application(c, event_id, VERA)
    assert (vera_app["status"], vera_app["transfer_id"]) == ("searching", None)

    # Шаг 6. Б выходит из трансфера («Еду самостоятельно»): пара расформировывается.
    # Алиса уже уехала, поэтому остаётся solo и в поиск не возвращается.
    solo = await act(c, s, event_id, BORIS, "go-solo")
    assert (solo.json()["status"], solo.json()["transfer_id"]) == ("solo", None)
    alice_app = await my_application(c, event_id, ALICE)
    assert (alice_app["status"], alice_app["transfer_id"]) == ("solo", None)
    assert alice_app["departed_at"] is not None
    assert (await my_trip(c, event_id, ALICE))["transfer"] is None
    assert (await my_trip(c, event_id, BORIS))["transfer"] is None
    assert await admin_transfers(c, event_id) == []
    people = by_name(await admin_participants(c, event_id))
    assert people["Борис"]["application"]["status"] == "solo"
    assert people["Алиса"]["application"]["status"] == "solo"
    # После выхода Б заполнение ничего не нашло: Глеб и Дима несовместимы по багажу, Эрик на рейсе
    # в DME один (Вера там уже уехала). Все трое по-прежнему ищут и не собраны.
    assert people["Глеб"]["application"]["status"] == "searching"
    assert people["Дима"]["application"]["status"] == "searching"
    assert people["Эрик"]["application"]["status"] == "searching"

    # Шаг 7. Посторонний не видит группу.
    await share_phone(s, STRANGER)
    await call(c, "GET", "/events/{event_id}", STRANGER, expect=404, event_id=event_id)
    await call(c, "GET", "/events/{event_id}/my-trip", STRANGER, expect=404, event_id=event_id)
    for admin_path in ("participants", "transfers"):
        await call(
            c,
            "GET",
            f"/events/{{event_id}}/admin/{admin_path}",
            STRANGER,
            expect=404,
            event_id=event_id,
        )
    assert (await call(c, "GET", "/me/events", STRANGER, expect=200)).json() == []

    await assert_invariants(s, uuid.UUID(event_id))

    # История группы: что произошло, в каком порядке и по какой причине.
    history = await audit_rows(s, uuid.UUID(event_id))
    assert [(row.action, row.reason, row.actor_kind) for row in history] == [
        ("application_submitted", None, "participant"),  # А
        ("application_submitted", None, "participant"),  # Б
        ("transfer_created", "auto_match", "system"),  # пара А + Б
        ("application_submitted", None, "participant"),  # В
        ("application_submitted", None, "participant"),  # Г
        ("application_submitted", None, "participant"),  # Д
        ("marks_changed", None, "participant"),  # А: на месте
        ("marks_changed", None, "participant"),  # А: уехал
        ("marks_changed", None, "participant"),  # В: уехал
        ("application_submitted", None, "participant"),  # Эрик
        ("transfer_left", "go_solo", "participant"),  # Б вышел
        ("transfer_left", "member_left", "participant"),  # А уехал, остаётся solo
        ("transfer_dissolved", "member_left", "participant"),
    ]
    assert history[-3].subject_user_id != history[-2].subject_user_id
    assert history[-3].actor_user_id == history[-2].actor_user_id == history[-1].actor_user_id
    assert history[-2].details == {"from_status": "assigned", "to_status": "solo"}


async def test_pre_mvp_triple_is_assembled_and_refilled_through_api(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Шаг 6 для трансфера из трёх: третий дополняет пару, после выхода одного двое остаются
    вместе, а освободившееся место занимает новая заявка (при выходе — из тех, кто уже ждёт)."""
    c, s = api_client, db_session
    event_id, code = await create_event(c, "Тройка")
    trio = (ALICE, BORIS, VERA)
    trips = [
        await add_trip(c, event_id, "SVO", f"2026-11-12T10:0{i}:00", f"SU {i}") for i in range(3)
    ]
    for who in (*trio, IGOR, ERIK):
        await join(c, s, who, code)

    # А и Б собираются в пару, В дополняет её до тройки: трансфер тот же, а не новый.
    first = await apply(c, s, event_id, ALICE, trips[0])
    assert first.json()["status"] == "searching"
    pair = await apply(c, s, event_id, BORIS, trips[1])
    assert pair.json()["status"] == "assigned"
    transfer_id = pair.json()["transfer_id"]
    third = await apply(c, s, event_id, VERA, trips[2])
    assert (third.json()["status"], third.json()["transfer_id"]) == ("assigned", transfer_id)
    for who in trio:
        view = (await my_trip(c, event_id, who))["transfer"]
        assert view["id"] == transfer_id
        assert view["passengers_total"] == 3
        assert member_names(view) == ["Алиса", "Борис", "Вера"]

    # Борис выходит.
    solo = await act(c, s, event_id, BORIS, "go-solo")
    assert solo.json()["status"] == "solo"

    # Оставшиеся двое по-прежнему вместе и видят изменившийся состав.
    for who in (ALICE, VERA):
        view = await my_trip(c, event_id, who)
        assert view["application"]["status"] == "assigned"
        assert view["transfer"]["id"] == transfer_id
        assert member_names(view["transfer"]) == ["Алиса", "Вера"]
        assert view["transfer"]["passengers_total"] == 2
    assert (await my_trip(c, event_id, BORIS))["transfer"] is None
    transfers = await admin_transfers(c, event_id)
    assert [t["id"] for t in transfers] == [transfer_id]
    assert member_names(transfers[0]) == ["Алиса", "Вера"]

    # Место свободно: новая совместимая заявка Игоря дополняет пару, трансфер снова полный.
    late = await apply(c, s, event_id, IGOR, trips[0])
    assert (late.json()["status"], late.json()["transfer_id"]) == ("assigned", transfer_id)
    transfers = await admin_transfers(c, event_id)
    assert member_names(transfers[0]) == ["Алиса", "Вера", "Игорь"]

    # Эрик приходит в полную машину и ждёт; когда Алиса выходит, место достаётся ему.
    waiting = await apply(c, s, event_id, ERIK, trips[1])
    assert (waiting.json()["status"], waiting.json()["transfer_id"]) == ("searching", None)
    left = await act(c, s, event_id, ALICE, "cancel")
    assert (left.json()["status"], left.json()["transfer_id"]) == ("cancelled", None)
    erik = await my_application(c, event_id, ERIK)
    assert (erik["status"], erik["transfer_id"]) == ("assigned", transfer_id)
    transfers = await admin_transfers(c, event_id)
    assert [t["id"] for t in transfers] == [transfer_id]
    assert member_names(transfers[0]) == ["Вера", "Игорь", "Эрик"]
    await assert_invariants(s, uuid.UUID(event_id))

    # В истории: сборка, дополнения, выходы и причины посадок; составы восстанавливаются.
    history = await audit_rows(s, uuid.UUID(event_id))
    assert [(row.action, row.reason, row.actor_kind) for row in history] == [
        ("application_submitted", None, "participant"),  # А
        ("application_submitted", None, "participant"),  # Б
        ("transfer_created", "auto_match", "system"),  # пара А + Б
        ("application_submitted", None, "participant"),  # В
        ("transfer_joined", "auto_match", "system"),  # В дополняет пару
        ("transfer_left", "go_solo", "participant"),  # Б вышел, двое остались
        ("application_submitted", None, "participant"),  # Игорь
        ("transfer_joined", "auto_match", "system"),  # Игорь дополняет пару
        ("application_submitted", None, "participant"),  # Эрик
        ("transfer_left", "cancel", "participant"),  # А отменила
        ("transfer_joined", "auto_fill", "system"),  # Эрик занимает её место
    ]
