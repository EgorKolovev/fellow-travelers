"""Сам хелпер восстановления составов: правило из docs/contracts/README.md, §11."""

import itertools
import uuid
from typing import Any

import pytest

from app.models import ActorKind, AuditAction, AuditLog
from tests.helpers.audit_replay import replay_transfers

_seq = itertools.count(1)


def entry(action: AuditAction, **kw: Any) -> AuditLog:
    return AuditLog(id=next(_seq), action=action, actor_kind=ActorKind.SYSTEM, **kw)


def created(transfer_id: uuid.UUID, *members: uuid.UUID) -> AuditLog:
    return entry(
        AuditAction.TRANSFER_CREATED,
        transfer_id=transfer_id,
        details={"application_ids": [str(m) for m in members]},
    )


def joined(transfer_id: uuid.UUID, member: uuid.UUID) -> AuditLog:
    return entry(AuditAction.TRANSFER_JOINED, transfer_id=transfer_id, application_id=member)


def left(transfer_id: uuid.UUID, member: uuid.UUID) -> AuditLog:
    return entry(AuditAction.TRANSFER_LEFT, transfer_id=transfer_id, application_id=member)


def dissolved(transfer_id: uuid.UUID, *remaining: uuid.UUID) -> AuditLog:
    return entry(
        AuditAction.TRANSFER_DISSOLVED,
        transfer_id=transfer_id,
        details={"application_ids": [str(m) for m in remaining]},
    )


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def test_created_plus_joined_minus_left() -> None:
    transfer = uuid.uuid4()
    a, b, c, d = ids(4)

    result = replay_transfers(
        [created(transfer, a, b), joined(transfer, c), left(transfer, a), joined(transfer, d)]
    )

    assert result == {transfer: {b, c, d}}


def test_dissolved_transfer_is_empty() -> None:
    transfer = uuid.uuid4()
    a, b = ids(2)

    result = replay_transfers([created(transfer, a, b), left(transfer, a), dissolved(transfer, b)])

    assert result == {transfer: set()}


def test_dissolved_clears_members_even_without_left_records() -> None:
    transfer = uuid.uuid4()
    a, b = ids(2)

    assert replay_transfers([created(transfer, a, b), dissolved(transfer, a, b)]) == {
        transfer: set()
    }


def test_transfers_are_independent_and_unrelated_records_are_ignored() -> None:
    first, second = ids(2)
    a, b, c, d = ids(4)
    other = entry(AuditAction.STATUS_CHANGED, application_id=a)
    departed = entry(AuditAction.TRANSFER_DEPARTED, transfer_id=first)

    result = replay_transfers(
        [created(first, a, b), other, created(second, c, d), departed, left(second, c)]
    )

    assert result == {first: {a, b}, second: {d}}


def test_records_are_applied_in_id_order_not_input_order() -> None:
    transfer = uuid.uuid4()
    a, b, c = ids(3)
    records = [created(transfer, a, b), joined(transfer, c), left(transfer, c)]

    assert replay_transfers(reversed(records)) == {transfer: {a, b}}


def test_second_creation_of_same_transfer_is_an_error() -> None:
    transfer = uuid.uuid4()
    a, b = ids(2)

    with pytest.raises(AssertionError, match="создан дважды"):
        replay_transfers([created(transfer, a, b), created(transfer, a, b)])


def test_left_of_unknown_transfer_is_an_error() -> None:
    """Выход из трансфера, которого нет в истории, означает пропущенную запись о его создании."""
    with pytest.raises(KeyError):
        replay_transfers([left(uuid.uuid4(), uuid.uuid4())])
