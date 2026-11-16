"""Помощники для тестов уведомлений."""

import uuid
from datetime import timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationStatus


async def deliver_pending(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Считает неотправленные строки группы отправленными (два часа назад, вне окна лимита).

    Новые события склеиваются в `pending` (M-04a), поэтому тест, которому нужна отдельная строка
    на следующее событие, сначала «отправляет» накопленное. Метрик и Telegram это не трогает.
    """
    await session.execute(
        update(Notification)
        .where(Notification.event_id == event_id, Notification.status == NotificationStatus.PENDING)
        .values(status=NotificationStatus.SENT, sent_at=func.now() - timedelta(hours=2))
    )
