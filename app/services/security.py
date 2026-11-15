"""Rejected access attempts are refused with a clear error *and* logged (KASU-002).

Raise `AccessDenied` instead of a plain HTTPException wherever a request is refused for
authentication or authorization reasons. The app-level handler (see app.main) records it in
`access_denials` and in the `kasu.security` logger, then returns the normal error response.

The record is written in its own session so it is kept even though the request's own
transaction is rolled back.
"""

import logging
import uuid
from typing import Any

from fastapi import HTTPException, Request

from app.core.db import SessionLocal
from app.models import AccessDenial

log = logging.getLogger("kasu.security")


class AccessDenied(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        reason: str,
        user_id: uuid.UUID | None = None,
        business_id: uuid.UUID | None = None,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code, detail, headers=headers)
        self.reason = reason
        self.user_id = user_id
        self.business_id = business_id
        self.details = details or {}


def _business_from_path(request: Request) -> uuid.UUID | None:
    raw = request.path_params.get("business_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def record_denial(request: Request, exc: AccessDenied) -> None:
    business_id = exc.business_id or _business_from_path(request)
    ip = request.client.host if request.client else None
    log.warning(
        "access denied: %s %s -> %s reason=%s user=%s business=%s ip=%s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.reason,
        exc.user_id,
        business_id,
        ip,
    )
    try:
        with SessionLocal() as session:
            session.add(
                AccessDenial(
                    user_id=exc.user_id,
                    business_id=business_id,
                    method=request.method,
                    path=request.url.path[:500],
                    status_code=exc.status_code,
                    reason=exc.reason,
                    ip=ip,
                    details=exc.details,
                )
            )
            session.commit()
    except Exception:
        # Never turn a 401/403/404 into a 500 because the log write failed.
        log.exception("could not store access denial")
