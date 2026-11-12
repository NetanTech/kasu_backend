import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import MemberRole
from app.schemas.common import ORM


class BusinessCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sector: str | None = Field(default=None, max_length=100)
    country: str = Field(default="NG", min_length=2, max_length=2)
    currency: str = Field(default="NGN", min_length=3, max_length=3)


class BusinessUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sector: str | None = Field(default=None, max_length=100)


class BusinessOut(ORM):
    id: uuid.UUID
    name: str
    sector: str | None
    country: str
    currency: str
    created_at: datetime
    role: MemberRole


class ProductOut(ORM):
    id: uuid.UUID
    name: str
    unit: str | None
    category: str | None


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    unit: str | None = Field(default=None, max_length=50)
    category: str | None = Field(default=None, max_length=100)


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=200)
