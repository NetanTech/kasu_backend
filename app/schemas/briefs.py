import uuid
from datetime import date, datetime

from app.models.enums import BriefStatus, NotificationKind
from app.schemas.common import ORM


class BriefSummaryOut(ORM):
    id: uuid.UUID
    week_start: date
    week_end: date
    status: BriefStatus
    created_at: datetime
    opened_by_me: bool


class NotificationOut(ORM):
    id: uuid.UUID
    business_id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    brief_id: uuid.UUID | None
    created_at: datetime
    opened_at: datetime | None
