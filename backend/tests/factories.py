"""Простые фабрики для тестов с БД. Каждая делает flush и возвращает объект."""

import itertools
import random
import string
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActorKind,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    BackgroundJob,
    Event,
    JobKind,
    Location,
    LocationKind,
    Membership,
    Transfer,
    TransportMode,
    Trip,
    User,
)

_counter = itertools.count(1)
_INVITE_ALPHABET = string.ascii_uppercase + string.digits


async def get_location(session: AsyncSession, code: str) -> Location:
    return (
        await session.execute(
            select(Location).where(Location.kind == LocationKind.AIRPORT, Location.code == code)
        )
    ).scalar_one()


async def make_user(session: AsyncSession, **kw: Any) -> User:
    n = next(_counter)
    kw.setdefault("telegram_id", 1_000_000 + n)
    kw.setdefault("first_name", f"User{n}")
    user = User(**kw)
    session.add(user)
    await session.flush()
    return user


async def make_event(session: AsyncSession, **kw: Any) -> Event:
    if "admin_id" not in kw:
        kw["admin_id"] = (await make_user(session)).id
    kw.setdefault("title", "Конференция")
    kw.setdefault("starts_on", date(2026, 11, 12))
    kw.setdefault("ends_on", date(2026, 11, 14))
    kw.setdefault("meeting_point", {"address": "Терминал D", "lat": 55.97, "lon": 37.41})
    kw.setdefault("destination", {"address": "Отель", "lat": 55.75, "lon": 37.62})
    kw.setdefault("invite_code", "".join(random.choices(_INVITE_ALPHABET, k=6)))
    event = Event(**kw)
    session.add(event)
    await session.flush()
    return event


async def make_membership(session: AsyncSession, **kw: Any) -> Membership:
    membership = Membership(**kw)
    session.add(membership)
    await session.flush()
    return membership


async def make_trip(session: AsyncSession, **kw: Any) -> Trip:
    if "event_id" not in kw:
        kw["event_id"] = (await make_event(session)).id
    if "departure_location_id" not in kw:
        kw["departure_location_id"] = (await get_location(session, "SVX")).id
    if "arrival_location_id" not in kw:
        kw["arrival_location_id"] = (await get_location(session, "SVO")).id
    kw.setdefault("mode", TransportMode.FLIGHT)
    kw.setdefault("number", f"SU{next(_counter):04d}")
    kw.setdefault("scheduled_departure", datetime(2026, 11, 12, 2, 30, tzinfo=UTC))
    kw.setdefault("scheduled_arrival", datetime(2026, 11, 12, 5, 5, tzinfo=UTC))
    trip = Trip(**kw)
    session.add(trip)
    await session.flush()
    return trip


async def make_transfer(session: AsyncSession, **kw: Any) -> Transfer:
    if "event_id" not in kw:
        kw["event_id"] = (await make_event(session)).id
    transfer = Transfer(**kw)
    session.add(transfer)
    await session.flush()
    return transfer


async def make_application(session: AsyncSession, **kw: Any) -> Application:
    """Без явных event_id/user_id/trip_id создаёт группу, пользователя и рейс."""
    if "trip_id" not in kw:
        trip_kw: dict[str, Any] = {"event_id": kw["event_id"]} if "event_id" in kw else {}
        trip = await make_trip(session, **trip_kw)
        kw["trip_id"] = trip.id
        kw.setdefault("event_id", trip.event_id)
    if "user_id" not in kw:
        kw["user_id"] = (await make_user(session)).id
    kw.setdefault("with_companion", False)
    kw.setdefault("baggage_count", 1)
    kw.setdefault("max_wait_minutes", 15)
    if kw.get("transfer_id") is not None:
        kw.setdefault("status", ApplicationStatus.ASSIGNED)
    application = Application(**kw)
    session.add(application)
    await session.flush()
    return application


async def make_audit_log(session: AsyncSession, **kw: Any) -> AuditLog:
    """Запись истории; без явного event_id создаёт группу, по умолчанию — действие системы."""
    if "event_id" not in kw:
        kw["event_id"] = (await make_event(session)).id
    kw.setdefault("action", AuditAction.TRIP_UPDATED)
    kw.setdefault("actor_kind", ActorKind.SYSTEM)
    entry = AuditLog(**kw)
    session.add(entry)
    await session.flush()
    return entry


async def make_job(session: AsyncSession, **kw: Any) -> BackgroundJob:
    """Задание пересчёта; без явного event_id создаёт группу."""
    if "event_id" not in kw:
        kw["event_id"] = (await make_event(session)).id
    kw.setdefault("kind", JobKind.RECONCILE_TRIP)
    job = BackgroundJob(**kw)
    session.add(job)
    await session.flush()
    return job
