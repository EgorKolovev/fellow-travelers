import asyncio
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import (
    Application,
    AuditReason,
    Base,
    Notification,
    NotificationKind,
)
from tests.db import alembic_config, migrate_from_scratch
from tests.factories import make_application, make_audit_log, make_event, make_trip, make_user


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    return list(compare_metadata(context, Base.metadata))


async def test_migration_matches_models(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        assert await connection.run_sync(_diff) == []


async def test_downgrade_base_then_upgrade_head(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    # Alembic (env.py) сам вызывает asyncio.run, поэтому из работающего цикла — только в потоке
    await asyncio.to_thread(command.downgrade, alembic_config(), "base")
    await db_engine.dispose()  # соединения пула знают про старую схему
    try:
        async with db_engine.connect() as connection:
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
        assert tables == ["alembic_version"]
    finally:
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


async def _constraint_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'applications'::regclass AND contype = 'c'"
            )
        )
        return set(rows.scalars())


async def test_wait_minutes_downgrade_coerces_to_legacy_values_then_upgrade(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    # Given: заявки с ожиданием вне старого набора 0/15/30 закоммичены в БД
    waits = [0, 1, 15, 16, 30, 45, 1440]
    async with AsyncSession(db_engine) as session:
        ids = [(await make_application(session, max_wait_minutes=w)).id for w in waits]
        await session.commit()
    try:
        # When: откат 0002 -> 0001
        await asyncio.to_thread(command.downgrade, alembic_config(), "0001")
        await db_engine.dispose()

        # Then: значения приведены к старому набору, действует старый CHECK
        async with db_engine.connect() as connection:
            rows = await connection.execute(
                select(Application.id, Application.max_wait_minutes).where(Application.id.in_(ids))
            )
            stored = {app_id: wait for app_id, wait in rows}
        assert [stored[i] for i in ids] == [0, 15, 15, 30, 30, 30, 30]
        constraints = await _constraint_names(db_engine)
        assert "ck_applications_max_wait_minutes_allowed" in constraints
        assert "ck_applications_max_wait_minutes_range" not in constraints

        # When: снова upgrade head — Then: действует новый CHECK
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()
        constraints = await _constraint_names(db_engine)
        assert "ck_applications_max_wait_minutes_range" in constraints
        assert "ck_applications_max_wait_minutes_allowed" not in constraints
    finally:
        # закоммиченные данные и схему возвращаем в исходное состояние
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


async def _audit_log_objects(engine: AsyncEngine) -> tuple[bool, set[str], set[str]]:
    """Есть ли таблица audit_log, какие у неё триггеры и есть ли функция триггера.

    Autogenerate триггеры и функции не сравнивает, поэтому их наличие проверяем напрямую.
    """
    async with engine.connect() as connection:
        table = await connection.scalar(text("SELECT to_regclass('audit_log') IS NOT NULL"))
        triggers = await connection.execute(
            text(
                "SELECT tgname FROM pg_trigger"
                " WHERE tgrelid = to_regclass('audit_log') AND NOT tgisinternal"
            )
        )
        functions = await connection.execute(
            text("SELECT proname FROM pg_proc WHERE proname = 'audit_log_forbid_change'")
        )
        return bool(table), set(triggers.scalars()), set(functions.scalars())


async def test_audit_log_trigger_and_function_exist_at_head(db_engine: AsyncEngine) -> None:
    table, triggers, functions = await _audit_log_objects(db_engine)

    assert table
    assert triggers == {"audit_log_append_only", "audit_log_no_truncate"}
    assert functions == {"audit_log_forbid_change"}


async def test_audit_log_downgrade_drops_table_trigger_and_function_then_upgrade(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    # Given: в audit_log есть закоммиченная запись, а триггер запрещает её удалять
    async with AsyncSession(db_engine) as session:
        await make_audit_log(session)
        await session.commit()
    try:
        # When: откат 0003 -> 0002 (DROP TABLE срабатывает и при непустой таблице)
        await asyncio.to_thread(command.downgrade, alembic_config(), "0002")
        await db_engine.dispose()

        # Then: нет ни таблицы, ни триггера, ни функции
        assert await _audit_log_objects(db_engine) == (False, set(), set())

        # When: снова upgrade head — Then: всё на месте, таблица пустая
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()
        table, triggers, functions = await _audit_log_objects(db_engine)
        assert (table, triggers, functions) == (
            True,
            {"audit_log_append_only", "audit_log_no_truncate"},
            {"audit_log_forbid_change"},
        )
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM audit_log")) == 0
    finally:
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


async def _departed_from_transfer_column(engine: AsyncEngine) -> dict[str, object] | None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(lambda c: inspect(c).get_columns("applications"))
        foreign_keys = await connection.run_sync(
            lambda c: inspect(c).get_foreign_keys("applications")
        )
    found = [c for c in columns if c["name"] == "departed_from_transfer_id"]
    if not found:
        return None
    assert not any("departed_from_transfer_id" in fk["constrained_columns"] for fk in foreign_keys)
    return {"nullable": found[0]["nullable"]}


async def test_0007_adds_and_drops_departed_from_transfer_without_fk(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    assert await _departed_from_transfer_column(db_engine) == {"nullable": True}
    try:
        await asyncio.to_thread(command.downgrade, alembic_config(), "0006")
        await db_engine.dispose()
        assert await _departed_from_transfer_column(db_engine) is None

        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()
        assert await _departed_from_transfer_column(db_engine) == {"nullable": True}
    finally:
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


async def _insert_notification(
    engine: AsyncEngine, user_id: uuid.UUID, event_id: uuid.UUID, key: str, **columns: str
) -> int:
    values = {"kind": "transfer_changed", "status": "pending", **columns}
    async with engine.begin() as connection:
        return int(
            await connection.scalar(
                text(
                    "INSERT INTO notifications"
                    " (user_id, event_id, kind, payload, dedupe_key, status)"
                    " VALUES (:u, :e, :kind, '{}', :key, :status) RETURNING id"
                ),
                {"u": user_id, "e": event_id, "key": key, **values},
            )
            or 0
        )


async def _notification_states(engine: AsyncEngine) -> list[tuple[int, str, str, str | None]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT id, status, kind, last_error FROM notifications ORDER BY id")
        )
        return [tuple(row) for row in rows]


async def test_0009_coalesces_existing_duplicates_and_downgrade_restores_old_values(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    async with AsyncSession(db_engine) as session:
        event = await make_event(session)
        other_event = await make_event(session)
        user = await make_user(session)
        other_user = await make_user(session)
        ids = (event.id, other_event.id, user.id, other_user.id)
        await session.commit()
    event_id, other_event_id, user_id, other_user_id = ids
    try:
        # Given: схема 0008, двое `pending` у одного человека в группе, остальное не дубли
        await asyncio.to_thread(command.downgrade, alembic_config(), "0008")
        await db_engine.dispose()
        first = await _insert_notification(db_engine, user_id, event_id, "a")
        second = await _insert_notification(db_engine, user_id, event_id, "b")
        third = await _insert_notification(db_engine, user_id, event_id, "c")
        sent = await _insert_notification(db_engine, user_id, event_id, "d", status="sent")
        elsewhere = await _insert_notification(db_engine, user_id, other_event_id, "e")
        someone = await _insert_notification(db_engine, other_user_id, event_id, "f")

        # When: upgrade до 0009
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()

        # Then: у человека в группе одна `pending` (самая ранняя), лишние `skipped`
        states = {row[0]: row[1:] for row in await _notification_states(db_engine)}
        assert states[first][0] == "pending"
        assert states[second] == ("skipped", "transfer_changed", "Coalesced")
        assert states[third][0] == "skipped"
        assert states[sent][0] == "sent"
        assert states[elsewhere][0] == states[someone][0] == "pending"
        with pytest.raises(Exception, match="uq_notifications_user_event_pending"):
            await _insert_notification(db_engine, user_id, event_id, "g")
        # новые значения допустимы, `sending` не мешает новой `pending`
        sending = await _insert_notification(
            db_engine, other_user_id, other_event_id, "h", status="sending", kind="several_changes"
        )
        await _insert_notification(db_engine, other_user_id, other_event_id, "i")

        # When: откат до 0008
        await asyncio.to_thread(command.downgrade, alembic_config(), "0008")
        await db_engine.dispose()

        # Then: `sending` -> `pending`, `several_changes` -> `transfer_changed`
        states = {row[0]: row[1:] for row in await _notification_states(db_engine)}
        assert states[sending][:2] == ("pending", "transfer_changed")
        with pytest.raises(Exception, match="ck_notifications_notification_status"):
            await _insert_notification(db_engine, user_id, event_id, "j", status="sending")
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM notifications WHERE event_id IN (:a, :b)"),
                {"a": event_id, "b": other_event_id},
            )
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()
        async with AsyncSession(db_engine) as session:
            await session.execute(
                text("DELETE FROM events WHERE id IN (:a, :b)"),
                {"a": event_id, "b": other_event_id},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": user_id, "b": other_user_id}
            )
            await session.commit()
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()


README = Path(__file__).resolve().parents[2] / "README.md"


def _documented_cleanup_sql() -> str:
    """SQL подготовки отката релиза MVP ровно как в README (между `<<'SQL'` и `SQL`)."""
    match = re.search(
        r"psql -U fellow -d fellow_travelers <<'SQL'\n(.*?)\n\s*SQL\n", README.read_text(), re.S
    )
    assert match is not None, "в README нет SQL отката релиза MVP"
    sql = "\n".join(line.strip() for line in match.group(1).splitlines())
    assert "audit_log_append_only" in sql
    assert "UPDATE audit_log SET reason = NULL" in sql
    return sql


async def _run_documented_cleanup(engine: AsyncEngine) -> None:
    """Выполняет SQL из README по операторам в одной транзакции (BEGIN/COMMIT даёт `begin`)."""
    statements = [part.strip() for part in _documented_cleanup_sql().split(";")]
    async with engine.begin() as connection:
        for statement in statements:
            if statement and statement not in {"BEGIN", "COMMIT"}:
                await connection.execute(text(statement))


async def test_downgrade_to_0004_with_mvp_data_after_documented_cleanup(
    db_engine: AsyncEngine, migrated_database: str
) -> None:
    # Given: на head есть данные MVP: история с причиной MVP, уведомление, рейс с опросом,
    # заявка с departed_from_transfer_id
    async with AsyncSession(db_engine) as session:
        event = await make_event(session)
        user = await make_user(session)
        trip = await make_trip(
            session,
            event_id=event.id,
            provider_ref="ref-1",
            next_poll_at=datetime(2026, 11, 11, 8, 0, tzinfo=UTC),
        )
        application = await make_application(
            session,
            event_id=event.id,
            user_id=user.id,
            trip_id=trip.id,
            departed_from_transfer_id=uuid.uuid4(),
        )
        entry = await make_audit_log(
            session, event_id=event.id, reason=AuditReason.PROVIDER_UPDATE, trip_id=trip.id
        )
        session.add(
            Notification(
                user_id=user.id,
                event_id=event.id,
                kind=NotificationKind.TRANSFER_CHANGED,
                payload={"event_id": str(event.id)},
                dedupe_key="k1",
            )
        )
        entry_id, application_id = entry.id, application.id
        await session.commit()
    try:
        # When: откат без подготовки — Then: падает на CHECK, схема остаётся прежней
        with pytest.raises(Exception, match="ck_audit_log_audit_reason"):
            await asyncio.to_thread(command.downgrade, alembic_config(), "0004")
        await db_engine.dispose()
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('notifications') IS NOT NULL"))

        # When: ровно SQL из README, затем откат
        await _run_documented_cleanup(db_engine)
        await asyncio.to_thread(command.downgrade, alembic_config(), "0004")
        await db_engine.dispose()

        # Then: строка истории осталась с reason NULL, триггер на месте, таблицы MVP нет
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('notifications') IS NULL"))
            row = (
                await connection.execute(
                    text("SELECT reason, event_id FROM audit_log WHERE id = :id"), {"id": entry_id}
                )
            ).one()
            assert row.reason is None
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM applications WHERE id = :id"), {"id": application_id}
                )
                == 1
            )
        _, triggers, _ = await _audit_log_objects(db_engine)
        assert triggers == {"audit_log_append_only", "audit_log_no_truncate"}
        assert await _departed_from_transfer_column(db_engine) is None

        # When: снова upgrade head — Then: проходит, строка истории на месте
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        await db_engine.dispose()
        async with db_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM audit_log WHERE id = :id AND reason IS NULL"),
                    {"id": entry_id},
                )
                == 1
            )
    finally:
        # упавший тест мог оставить причины MVP в истории: без очистки `downgrade base` упадёт
        await _run_documented_cleanup(db_engine)
        await asyncio.to_thread(migrate_from_scratch, migrated_database)
        await db_engine.dispose()
