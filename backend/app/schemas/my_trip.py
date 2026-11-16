from pydantic import BaseModel, ConfigDict, Field

from app import links
from app.models import Application as ApplicationModel
from app.models import Event as EventModel
from app.models import Transfer as TransferModel
from app.models import User
from app.schemas.application import Application
from app.schemas.common import Point
from app.schemas.event import Event
from app.schemas.transfer import Transfer


class Taxi(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: Point = Field(alias="from")
    to: Point
    yandex_go_url: str | None


class MyTrip(BaseModel):
    event: Event
    meeting_map_url: str | None
    application: Application | None
    transfer: Transfer | None
    taxi: Taxi

    @classmethod
    def build(
        cls,
        event: EventModel,
        user: User,
        *,
        is_participant: bool,
        application: ApplicationModel | None,
        transfer: TransferModel | None,
    ) -> "MyTrip":
        event_out = Event.build(event, user_id=user.id, is_participant=is_participant)
        return cls(
            event=event_out,
            meeting_map_url=links.map_url(event_out.meeting_point),
            application=None if application is None else Application.build(application),
            transfer=None if transfer is None else Transfer.build(transfer, viewer_id=user.id),
            # pickup_point в Event.build уже подменён местом сбора, если не задан
            taxi=Taxi(
                from_=event_out.pickup_point,
                to=event_out.destination,
                yandex_go_url=links.yandex_go_url(event_out.pickup_point, event_out.destination),
            ),
        )
