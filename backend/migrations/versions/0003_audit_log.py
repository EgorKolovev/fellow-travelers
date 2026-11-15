"""audit log

История изменений группы: таблица только для добавления. UPDATE, DELETE и TRUNCATE отклоняет
триггер. Каскад от events и SET NULL от users выполняются внутри системных триггеров ссылочной
целостности, то есть на глубине `pg_trigger_depth()` не меньше 2, и триггер их пропускает.
Для трансферов, которые уже есть в БД, вставляются записи transfer_created (базовая история).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORBID_CHANGE_FUNCTION = """
CREATE FUNCTION audit_log_forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- прямой UPDATE/DELETE: триггер на глубине 1; каскадные действия FK идут глубже.
    -- TRUNCATE запрещён всегда: он стирает всю историю разом и каскадов не имеет
    IF TG_OP = 'TRUNCATE' OR pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'audit_log доступна только для добавления (%)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""

_FORBID_CHANGE_TRIGGER = """
CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_change()
"""

# Трансферы, собранные до появления истории, получают запись transfer_created: без неё состав,
# восстановленный по истории, не включал бы их участников, и посадка третьего или выход одного
# дали бы неверный результат. Состав — заявки, которые сейчас ссылаются на трансфер.
_BASELINE_HISTORY = """
INSERT INTO audit_log (event_id, occurred_at, action, reason, actor_kind, transfer_id, details)
SELECT t.event_id,
       t.created_at,
       'transfer_created',
       NULL,
       'system',
       t.id,
       jsonb_build_object(
           'application_ids',
           COALESCE(
               (SELECT jsonb_agg(a.id::text ORDER BY a.created_at, a.id)
                  FROM applications a
                 WHERE a.transfer_id = t.id),
               '[]'::jsonb
           )
       )
  FROM transfers t
 ORDER BY t.created_at, t.id
"""

_FORBID_TRUNCATE_TRIGGER = """
CREATE TRIGGER audit_log_no_truncate
BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "application_submitted",
                "application_updated",
                "status_changed",
                "marks_changed",
                "transfer_created",
                "transfer_joined",
                "transfer_left",
                "transfer_dissolved",
                "transfer_departed",
                "trip_updated",
                name="audit_action",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(
                "go_solo",
                "cancel",
                "resume_search",
                "auto_match",
                "auto_fill",
                "incompatible_after_change",
                "member_left",
                "all_departed",
                "admin_edit",
                name="audit_reason",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "actor_kind",
            sa.Enum(
                "participant",
                "admin",
                "system",
                name="actor_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("subject_user_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("transfer_id", sa.Uuid(), nullable=True),
        sa.Column("trip_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_audit_log_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_subject_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_event_id_id", "audit_log", ["event_id", "id"], unique=False)
    op.execute(_FORBID_CHANGE_FUNCTION)
    op.execute(_FORBID_CHANGE_TRIGGER)
    op.execute(_FORBID_TRUNCATE_TRIGGER)
    op.execute(_BASELINE_HISTORY)


def downgrade() -> None:
    # IF EXISTS: базы, где 0003 накатили до появления этого триггера, откатываются без ошибки
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION audit_log_forbid_change()")
    op.drop_index("ix_audit_log_event_id_id", table_name="audit_log")
    op.drop_table("audit_log")
