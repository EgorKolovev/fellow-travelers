import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Application, Base
from tests.db import alembic_config, migrate_from_scratch
from tests.factories import make_application, make_audit_log


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
