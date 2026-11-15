"""Team management: invitations, roles, removal, ownership transfer. Every change is audited.

Rules
* Who can be invited/changed/removed is governed by MANAGEABLE_ROLES: owners manage admins,
  members and viewers; admins manage members and viewers only.
* Nobody becomes owner through an invite or role change; ownership moves only by transfer.
* There is always exactly one owner: the owner can't leave or be removed without transferring.
* Accepting needs the invite token (from the shared link) *and* a login whose email matches
  the invite. Emails aren't verified yet, so the token is the real proof; the email match
  stops a forwarded link from being used by someone else.
"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AuditEvent, BusinessMember, Invitation, User
from app.models.base import utcnow
from app.models.enums import MANAGEABLE_ROLES, MemberRole


class TeamError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_link(token: str) -> str:
    return f"{get_settings().app_url.rstrip('/')}/invite/{token}"


def audit(
    db: Session,
    business_id: uuid.UUID,
    actor: uuid.UUID | None,
    action: str,
    target: uuid.UUID | None = None,
    **details,
) -> None:
    db.add(
        AuditEvent(
            business_id=business_id,
            actor_user_id=actor,
            action=action,
            target_user_id=target,
            details={k: str(v) if v is not None else None for k, v in details.items()},
        )
    )


def _check_can_manage(actor_role: MemberRole, role: MemberRole, verb: str) -> None:
    if role not in MANAGEABLE_ROLES[actor_role]:
        raise TeamError(403, f"As {actor_role.value} you can't {verb} {role.value}s")


def _membership(db: Session, business_id: uuid.UUID, user_id: uuid.UUID) -> BusinessMember:
    m = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.user_id == user_id
        )
    )
    if m is None:
        raise TeamError(404, "That person isn't on this team")
    return m


def _pending(db: Session, business_id: uuid.UUID, email: str) -> list[Invitation]:
    return list(
        db.scalars(
            select(Invitation).where(
                Invitation.business_id == business_id,
                func.lower(Invitation.email) == email.lower(),
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
        )
    )


# --------------------------------------------------------------------------- invitations


def create_invitation(
    db: Session,
    business_id: uuid.UUID,
    actor: User,
    actor_role: MemberRole,
    email: str,
    role: MemberRole,
) -> tuple[Invitation, str]:
    """Returns the invitation and its plaintext token (shown once, never stored)."""
    _check_can_manage(actor_role, role, "invite")
    email = email.strip().lower()
    already = db.scalar(
        select(BusinessMember)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == business_id, func.lower(User.email) == email)
    )
    if already:
        raise TeamError(409, f"{email} is already on this team")

    # Re-inviting the same email replaces any earlier pending invite (acts as "resend").
    now = utcnow()
    for old in _pending(db, business_id, email):
        old.revoked_at = now

    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        business_id=business_id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=actor.id,
        expires_at=now + timedelta(days=get_settings().invite_days),
    )
    db.add(invitation)
    db.flush()
    audit(db, business_id, actor.id, "invitation.created", email=email, role=role.value)
    return invitation, token


def revoke_invitation(
    db: Session, invitation: Invitation, actor: User, actor_role: MemberRole
) -> Invitation:
    if invitation.accepted_at or invitation.revoked_at:
        raise TeamError(409, "This invitation is no longer pending")
    _check_can_manage(actor_role, invitation.role, "revoke invites for")
    invitation.revoked_at = utcnow()
    audit(
        db,
        invitation.business_id,
        actor.id,
        "invitation.revoked",
        email=invitation.email,
        role=invitation.role.value,
    )
    return invitation


def find_by_token(db: Session, token: str) -> Invitation:
    """A usable invitation for this token, or a TeamError saying why not."""
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if invitation is None:
        raise TeamError(404, "Invitation not found")
    if invitation.revoked_at:
        raise TeamError(410, "This invitation was cancelled. Ask for a new one.")
    if invitation.accepted_at:
        raise TeamError(410, "This invitation has already been used")
    if invitation.expires_at < utcnow():
        raise TeamError(410, "This invitation has expired. Ask for a new one.")
    return invitation


def accept_invitation(db: Session, token: str, user: User) -> BusinessMember:
    invitation = find_by_token(db, token)
    if user.email.lower() != invitation.email.lower():
        raise TeamError(
            403,
            f"This invitation is for a different email address. Sign in as "
            f"{_mask(invitation.email)} to accept it.",
        )
    existing = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == invitation.business_id,
            BusinessMember.user_id == user.id,
        )
    )
    if existing:
        raise TeamError(409, "You're already on this team")
    member = BusinessMember(
        business_id=invitation.business_id, user_id=user.id, role=invitation.role
    )
    db.add(member)
    invitation.accepted_at = utcnow()
    invitation.accepted_by = user.id
    db.flush()
    audit(
        db,
        invitation.business_id,
        user.id,
        "member.joined",
        target=user.id,
        role=invitation.role.value,
        invitation_id=invitation.id,
    )
    return member


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'*' * max(len(name) - 2, 1)}@{domain}"


# --------------------------------------------------------------------------- members


def change_role(
    db: Session,
    business_id: uuid.UUID,
    actor: User,
    actor_role: MemberRole,
    target_user_id: uuid.UUID,
    new_role: MemberRole,
) -> BusinessMember:
    if target_user_id == actor.id:
        raise TeamError(403, "You can't change your own role")
    member = _membership(db, business_id, target_user_id)
    if member.role == MemberRole.OWNER or new_role == MemberRole.OWNER:
        raise TeamError(403, "Use ownership transfer to change who owns the business")
    _check_can_manage(actor_role, member.role, "change the role of")
    _check_can_manage(actor_role, new_role, "assign the role of")
    old = member.role
    member.role = new_role
    audit(
        db,
        business_id,
        actor.id,
        "member.role_changed",
        target=target_user_id,
        old_role=old.value,
        new_role=new_role.value,
    )
    return member


def remove_member(
    db: Session,
    business_id: uuid.UUID,
    actor: User,
    actor_role: MemberRole,
    target_user_id: uuid.UUID,
) -> None:
    member = _membership(db, business_id, target_user_id)
    if member.role == MemberRole.OWNER:
        raise TeamError(403, "The owner can't leave or be removed. Transfer ownership first.")
    leaving = target_user_id == actor.id
    if not leaving:
        _check_can_manage(actor_role, member.role, "remove")
    db.delete(member)
    audit(
        db,
        business_id,
        actor.id,
        "member.left" if leaving else "member.removed",
        target=target_user_id,
        role=member.role.value,
    )


def transfer_ownership(
    db: Session, business_id: uuid.UUID, actor: User, target_user_id: uuid.UUID
) -> BusinessMember:
    if target_user_id == actor.id:
        raise TeamError(409, "You already own this business")
    target = _membership(db, business_id, target_user_id)
    me = _membership(db, business_id, actor.id)
    target.role, me.role = MemberRole.OWNER, MemberRole.ADMIN
    audit(db, business_id, actor.id, "ownership.transferred", target=target_user_id)
    return target
