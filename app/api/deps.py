import uuid
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import decode_token
from app.models import Business, BusinessMember, User
from app.models.enums import ROLE_PERMISSIONS, MemberRole, Permission

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    try:
        user_id = decode_token(creds.credentials, "access")
    except jwt.InvalidTokenError:
        raise unauthorized from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


@dataclass
class BusinessContext:
    """The tenant for this request. Every business-scoped query must filter by business.id."""

    business: Business
    user: User
    role: MemberRole

    @property
    def id(self) -> uuid.UUID:
        return self.business.id

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


def get_business(
    business_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BusinessContext:
    row = db.execute(
        select(Business, BusinessMember.role)
        .join(BusinessMember, BusinessMember.business_id == Business.id)
        .where(Business.id == business_id, BusinessMember.user_id == user.id)
    ).first()
    if row is None:
        # 404, not 403: don't reveal that a business the user can't access exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return BusinessContext(business=row[0], user=user, role=row[1])


def require(permission: Permission) -> Callable[..., BusinessContext]:
    """Dependency factory: the caller must be a member *and* have `permission`."""

    def dependency(ctx: BusinessContext = Depends(get_business)) -> BusinessContext:
        if not ctx.can(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your role ({ctx.role.value}) can't do this",
            )
        return ctx

    return dependency
