"""Сквозная проверка: в логах нет персональных данных, invite-кодов и initData.

Логи пишет настоящий `configure_logging("DEBUG")`, SQL включён на уровне INFO — самом подробном,
который библиотека отдаёт без значений строк результата.
"""

import logging
import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, quote, urlencode

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import auth_headers
from tests.factories import make_event, make_trip, make_user
from tests.helpers.contract import assert_contract
from tests.helpers.telegram import BOT_TOKEN, sign_init_data, user_json
from tests.logs import JsonLogs

TELEGRAM_ID = 5_550_123_456
FIRST_NAME = "Леонид"
LAST_NAME = "Секретов"
USERNAME = "leonid_secret"
PHONE = "+79993456789"
INVITE_CODE = "QZX742"
UNKNOWN_CODE = "QQQ999"


@pytest.fixture
def sql_logged(json_logs: JsonLogs, caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Включает лог SQL до создания соединения: оно запоминает уровень при создании."""
    with caplog.at_level(logging.INFO, logger="sqlalchemy.engine"):
        yield


def profile_headers() -> dict[str, str]:
    return auth_headers(TELEGRAM_ID, first_name=FIRST_NAME, last_name=LAST_NAME, username=USERNAME)


def secrets_of(headers: dict[str, str]) -> list[str]:
    """Всё, чего не должно быть в логах: значения профиля, коды и сама initData."""
    authorization = headers["Authorization"]
    init_data = authorization.removeprefix("tma ")
    hashes = [value for key, value in parse_qsl(init_data) if key == "hash"]
    return [
        PHONE,
        PHONE.removeprefix("+"),
        INVITE_CODE,
        UNKNOWN_CODE,
        str(TELEGRAM_ID),
        FIRST_NAME,
        LAST_NAME,
        USERNAME,
        authorization,
        init_data,
        *hashes,
    ]


def assert_no_secrets(logs: JsonLogs, secrets: list[str]) -> None:
    lowered = logs.text.lower()
    for secret in secrets:
        assert secret.lower() not in lowered, f"в логах нашлось {secret[:12]!r}"


# ---------------------------------------------------------------- сквозной сценарий


@pytest.mark.usefixtures("sql_logged")
async def test_join_and_application_leave_no_personal_data_in_logs(
    json_logs: JsonLogs, api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(
        db_session,
        telegram_id=TELEGRAM_ID,
        first_name=FIRST_NAME,
        last_name=LAST_NAME,
        username=USERNAME,
        phone=PHONE,
    )
    event = await make_event(db_session, invite_code=INVITE_CODE)
    trip = await make_trip(db_session, event_id=event.id)
    headers = profile_headers()

    preview = await api_client.get(f"/invites/{INVITE_CODE.lower()}", headers=headers)
    assert_contract(preview, "GET", "/invites/{code}")
    assert preview.status_code == 200
    unknown = await api_client.post(f"/invites/{UNKNOWN_CODE}/join", headers=headers)
    assert_contract(unknown, "POST", "/invites/{code}/join")
    assert unknown.status_code == 404
    joined = await api_client.post(f"/invites/{INVITE_CODE.lower()}/join", headers=headers)
    assert_contract(joined, "POST", "/invites/{code}/join")
    assert joined.status_code == 201
    me = await api_client.get("/me", headers=headers)
    assert_contract(me, "GET", "/me")
    assert me.json()["phone"] == PHONE
    applied = await api_client.put(
        f"/events/{event.id}/my-application",
        json={
            "trip_id": str(trip.id),
            "with_companion": False,
            "baggage_count": 1,
            "max_wait_minutes": 15,
        },
        headers=headers,
    )
    assert_contract(applied, "PUT", "/events/{event_id}/my-application")
    assert applied.status_code == 201

    access = json_logs.by_logger("app.access")
    assert [(r["route"], r["status"]) for r in access] == [
        ("/api/v1/invites/{code}", 200),
        ("/api/v1/invites/{code}/join", 404),
        ("/api/v1/invites/{code}/join", 201),
        ("/api/v1/me", 200),
        ("/api/v1/events/{event_id}/my-application", 201),
    ]
    assert len({r["request_id"] for r in access}) == len(access)
    # проверка не пустая: SQL попал в лог, а значения его параметров скрыты
    sql = [r for r in json_logs.records if r["logger"].startswith("sqlalchemy.engine")]
    assert any("INSERT INTO applications" in r["message"] for r in sql)
    assert any("parameters hidden" in r["message"] for r in sql)
    assert_no_secrets(json_logs, secrets_of(headers))


# ---------------------------------------------------------------- отказ по initData


def rejected_init_data() -> dict[str, str]:
    """Варианты битой initData, каждый несёт данные профиля или секретную строку."""
    now = str(int(time.time()))
    user = user_json(id=TELEGRAM_ID, first_name=FIRST_NAME, last_name=LAST_NAME, username=USERNAME)
    zero_hash = "0" * 64
    return {
        "expired": sign_init_data(
            {"auth_date": str(int(time.time()) - 10 * 86400), "user": user}, BOT_TOKEN
        ),
        "wrong_hash": urlencode({"auth_date": now, "user": user, "hash": zero_hash}),
        "no_hash": urlencode({"auth_date": now, "user": user}),
        "duplicate_key": (f"auth_date={now}&auth_date={now}&user={quote(user)}&hash={zero_hash}"),
        "not_a_query": f"SECRET-TOKEN {PHONE}",
        "bad_user_json": sign_init_data(
            {"auth_date": now, "user": f"{{broken {FIRST_NAME} {PHONE}"}, BOT_TOKEN
        ),
    }


@pytest.mark.parametrize("case", list(rejected_init_data()))
async def test_rejected_init_data_is_not_logged(
    json_logs: JsonLogs, api_client: AsyncClient, case: str
) -> None:
    raw = rejected_init_data()[case]

    response = await api_client.get("/me", headers={"Authorization": f"tma {raw}"})

    assert response.status_code == 401
    assert_contract(response, "GET", "/me")
    [rejected] = json_logs.by_logger("app.auth.telegram")
    assert rejected["message"].startswith("initData rejected: ")
    assert_no_secrets(
        json_logs,
        [
            raw,
            quote(raw),
            "SECRET-TOKEN",
            "0" * 64,
            str(TELEGRAM_ID),
            FIRST_NAME,
            LAST_NAME,
            USERNAME,
            PHONE,
        ],
    )
