from enum import StrEnum


class LocationKind(StrEnum):
    AIRPORT = "airport"
    STATION = "station"


class TransportMode(StrEnum):
    FLIGHT = "flight"


class TripStatus(StrEnum):
    SCHEDULED = "scheduled"
    DELAYED = "delayed"
    DEPARTED = "departed"
    LANDED = "landed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class TripSource(StrEnum):
    MANUAL = "manual"
    PROVIDER = "provider"


class ApplicationStatus(StrEnum):
    SEARCHING = "searching"
    ASSIGNED = "assigned"
    SOLO = "solo"
    CANCELLED = "cancelled"


class TransferStatus(StrEnum):
    ACTIVE = "active"
    DEPARTED = "departed"


class TransferOrigin(StrEnum):
    AUTO = "auto"
    ADMIN = "admin"


class ActorKind(StrEnum):
    PARTICIPANT = "participant"
    ADMIN = "admin"
    SYSTEM = "system"


class AuditAction(StrEnum):
    APPLICATION_SUBMITTED = "application_submitted"
    APPLICATION_UPDATED = "application_updated"
    STATUS_CHANGED = "status_changed"
    MARKS_CHANGED = "marks_changed"
    TRANSFER_CREATED = "transfer_created"
    TRANSFER_JOINED = "transfer_joined"
    TRANSFER_LEFT = "transfer_left"
    TRANSFER_DISSOLVED = "transfer_dissolved"
    TRANSFER_DEPARTED = "transfer_departed"
    TRIP_UPDATED = "trip_updated"


class AuditReason(StrEnum):
    GO_SOLO = "go_solo"
    CANCEL = "cancel"
    RESUME_SEARCH = "resume_search"
    AUTO_MATCH = "auto_match"
    AUTO_FILL = "auto_fill"
    INCOMPATIBLE_AFTER_CHANGE = "incompatible_after_change"
    MEMBER_LEFT = "member_left"
    ALL_DEPARTED = "all_departed"
    ADMIN_EDIT = "admin_edit"
    APPLICATION_CHANGED = "application_changed"
    DEPARTURE_UNDONE = "departure_undone"
    FLIGHT_CANCELLED = "flight_cancelled"
    PROVIDER_UPDATE = "provider_update"


class JobKind(StrEnum):
    RECONCILE_TRIP = "reconcile_trip"


class JobStatus(StrEnum):
    PENDING = "pending"
    FAILED = "failed"


class NotificationKind(StrEnum):
    TRANSFER_ASSIGNED = "transfer_assigned"
    TRANSFER_CHANGED = "transfer_changed"
    TRANSFER_LEFT = "transfer_left"
    POINTS_CHANGED = "points_changed"
    FLIGHT_CHANGED = "flight_changed"
    FLIGHT_CANCELLED = "flight_cancelled"
    # итог склейки разных видов в одной ещё не отправленной строке (M-04a)
    SEVERAL_CHANGES = "several_changes"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"
