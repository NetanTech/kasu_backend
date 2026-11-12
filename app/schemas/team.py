import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import MemberRole, Permission


class InvitationCreate(BaseModel):
    email: EmailStr
    role: MemberRole = MemberRole.MEMBER


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: MemberRole
    invited_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class InvitationCreated(InvitationOut):
    token: str = Field(description="Shown only once. Share the invite_link instead of storing it.")
    invite_link: str


class InvitationPreview(BaseModel):
    business_name: str
    role: MemberRole
    email: str
    invited_by_name: str | None
    expires_at: datetime


class AcceptIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: MemberRole
    joined_at: datetime
    is_you: bool


class RoleUpdate(BaseModel):
    role: MemberRole


class TransferIn(BaseModel):
    user_id: uuid.UUID


class MyAccessOut(BaseModel):
    role: MemberRole
    permissions: list[Permission]


class AuditEventOut(BaseModel):
    id: uuid.UUID
    action: str
    actor_user_id: uuid.UUID | None
    target_user_id: uuid.UUID | None
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}
