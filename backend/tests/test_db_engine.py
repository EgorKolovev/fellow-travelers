import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import dispose_engine, get_engine
from tests.logs import JsonLogs

CANARY = "canary-7f3a9c"

INSERT_USER = text(
    "INSERT INTO users (id, telegram_id, first_name, phone, created_at, updated_at) "
    "VALUES (gen_random_uuid(), :telegram_id, :first_name, :phone, now(), now())"
)


async def test_engine_hides_sql_parameters(
    json_logs: JsonLogs, caplog: pytest.LogCaptureFixture, migrated_database: str
) -> None:
    """Значения параметров не попадают ни в лог SQL, ни в текст исключения."""
    get_settings.cache_clear()
    params = {"telegram_id": 910_000_001, "first_name": CANARY, "phone": f"+7{CANARY}"}
    try:
        assert get_engine().sync_engine.hide_parameters is True
        with caplog.at_level(logging.INFO, logger="sqlalchemy.engine"):
            async with get_engine().connect() as conn:
                transaction = await conn.begin()
                try:
                    await conn.execute(INSERT_USER, params)
                    with pytest.raises(IntegrityError) as raised:
                        await conn.execute(INSERT_USER, params)
                finally:
                    await transaction.rollback()
    finally:
        await dispose_engine()

    # SQL в логе есть (проверка не пустая), а значений параметров нет
    assert "INSERT INTO users" in json_logs.text
    assert "parameters hidden" in json_logs.text
    assert CANARY not in json_logs.text
    assert CANARY not in str(raised.value)
    assert "parameters hidden" in str(raised.value)
