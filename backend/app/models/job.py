import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import JobKind, JobStatus


class BackgroundJob(Base):
    """Задание фоновой очереди: создаётся в одной транзакции с правкой, выполняется воркером.

    `trip_id` без FK: задание не должно мешать удалению рейса и переживает его. `event_id`
    с CASCADE уводит задания вместе с группой. Воркер забирает `pending` с `run_after <= now()`;
    при ошибке растёт `attempts`, `run_after` сдвигается, а после лимита попыток задание
    получает `failed` и ждёт ручного решения. В `last_error` только имя класса исключения.
    Ожидающее задание вида на группу может быть только одно (уникальный частичный индекс).
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        Index(
            "ix_background_jobs_pending_run_after",
            "run_after",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_background_jobs_event_id", "event_id"),
        # одно ожидающее задание каждого вида на группу; failed и выполняемые правок не блокируют
        Index(
            "uq_background_jobs_event_kind_pending",
            "event_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind, name="job_kind", length=32))
    trip_id: Mapped[uuid.UUID | None]
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, name="job_status"), server_default=JobStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
