from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.matching import MatchCandidate, OpenTransfer

AIRPORT = UUID(int=1)
OTHER_AIRPORT = UUID(int=2)


def cand(
    time: str,
    *,
    wait: int = 0,
    pax: int = 1,
    bag: int = 0,
    day: int = 1,
    tz: timezone = UTC,
    airport: UUID = AIRPORT,
    id: UUID | None = None,
) -> MatchCandidate:
    hour, minute = (int(p) for p in time.split(":"))
    return MatchCandidate(
        id=id or uuid4(),
        arrival_location_id=airport,
        arrival_at=datetime(2026, 7, day, hour, minute, tzinfo=tz),
        passengers=pax,
        baggage=bag,
        max_wait=timedelta(minutes=wait),
    )


def open_transfer(*members: MatchCandidate, id: UUID | None = None) -> OpenTransfer:
    return OpenTransfer(id=id or uuid4(), members=members)
