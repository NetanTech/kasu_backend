import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import BusinessContext, get_business, get_current_user, require
from app.core.db import get_db
from app.models import AuditEvent, BusinessMember, Invitation, User
from app.models.enums import ROLE_PERMISSIONS, Permission
from app.schemas.business import BusinessOut
from app.schemas.team import (
    AcceptIn,
    AuditEventOut,
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    InvitationPreview,
    MemberOut,
    MyAccessOut,
    RoleUpdate,
    TransferIn,
)
from app.services import team
from app.services.team import TeamError

router = APIRouter(prefix="/businesses/{business_id}", tags=["team"])
public_router = APIRouter(prefix="/invitations", tags=["team"])


def _fail(db: Session, exc: TeamError):
    db.rollback()
    raise HTTPException(exc.status_code, str(exc)) from None


# --------------------------------------------------------------------------- access & members


@router.get("/me/access", response_model=MyAccessOut)
def my_access(ctx: BusinessContext = Depends(get_business)) -> MyAccessOut:
    """Your role here and what it allows; use it to show or hide parts of the app."""
    return MyAccessOut(role=ctx.role, permissions=sorted(ROLE_PERMISSIONS[ctx.role]))


@router.get("/members", response_model=list[MemberOut])
def list_members(
    ctx: BusinessContext = Depends(get_business), db: Session = Depends(get_db)
) -> list[MemberOut]:
    rows = db.execute(
        select(BusinessMember, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == ctx.id)
        .order_by(BusinessMember.created_at)
    ).all()
    return [
        MemberOut(
            user_id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=m.role,
            joined_at=m.created_at,
            is_you=u.id == ctx.user.id,
        )
        for m, u in rows
    ]


@router.patch("/members/{user_id}", response_model=MemberOut)
def change_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    ctx: BusinessContext = Depends(require(Permission.MANAGE_TEAM)),
    db: Session = Depends(get_db),
) -> MemberOut:
    try:
        member = team.change_role(db, ctx.id, ctx.user, ctx.role, user_id, body.role)
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    user = db.get(User, user_id)
    return MemberOut(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        role=member.role,
        joined_at=member.created_at,
        is_you=False,
    )


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    user_id: uuid.UUID,
    ctx: BusinessContext = Depends(get_business),
    db: Session = Depends(get_db),
) -> Response:
    """Remove someone (needs manage_team), or remove yourself to leave the business."""
    if user_id != ctx.user.id and not ctx.can(Permission.MANAGE_TEAM):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"Your role ({ctx.role.value}) can't do this"
        )
    try:
        team.remove_member(db, ctx.id, ctx.user, ctx.role, user_id)
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/transfer-ownership", response_model=list[MemberOut])
def transfer_ownership(
    body: TransferIn,
    ctx: BusinessContext = Depends(require(Permission.MANAGE_BUSINESS)),
    db: Session = Depends(get_db),
) -> list[MemberOut]:
    """Make another team member the owner. You become an admin."""
    try:
        team.transfer_ownership(db, ctx.id, ctx.user, body.user_id)
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    return list_members(ctx, db)


@router.get("/audit-log", response_model=list[AuditEventOut])
def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    ctx: BusinessContext = Depends(require(Permission.MANAGE_TEAM)),
    db: Session = Depends(get_db),
) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == ctx.id)
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------- invitations


@router.post("/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED)
def create_invitation(
    body: InvitationCreate,
    ctx: BusinessContext = Depends(require(Permission.MANAGE_TEAM)),
    db: Session = Depends(get_db),
) -> InvitationCreated:
    """Invite someone by email. Returns a link to share (e.g. on WhatsApp); it's shown once."""
    try:
        invitation, token = team.create_invitation(
            db, ctx.id, ctx.user, ctx.role, body.email, body.role
        )
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    return InvitationCreated(
        **InvitationOut.model_validate(invitation).model_dump(),
        token=token,
        invite_link=team.invite_link(token),
    )


@router.get("/invitations", response_model=list[InvitationOut])
def list_invitations(
    include_closed: bool = False,
    ctx: BusinessContext = Depends(require(Permission.MANAGE_TEAM)),
    db: Session = Depends(get_db),
) -> list[Invitation]:
    stmt = select(Invitation).where(Invitation.business_id == ctx.id)
    if not include_closed:
        stmt = stmt.where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
    return list(db.scalars(stmt.order_by(Invitation.created_at.desc())))


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationOut)
def revoke_invitation(
    invitation_id: uuid.UUID,
    ctx: BusinessContext = Depends(require(Permission.MANAGE_TEAM)),
    db: Session = Depends(get_db),
) -> Invitation:
    invitation = db.scalar(
        select(Invitation).where(Invitation.id == invitation_id, Invitation.business_id == ctx.id)
    )
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
    try:
        team.revoke_invitation(db, invitation, ctx.user, ctx.role)
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    return invitation


# --------------------------------------------------------------------------- invitee side


@public_router.post("/preview", response_model=InvitationPreview)
def preview_invitation(body: AcceptIn, db: Session = Depends(get_db)) -> InvitationPreview:
    """What an invite link is for, so the landing page can say "Ada invited you to join
    Mama Nkechi Provisions as staff". No login needed; the token is the credential."""
    try:
        invitation = team.find_by_token(db, body.token)
    except TeamError as exc:
        _fail(db, exc)
    return InvitationPreview(
        business_name=invitation.business.name,
        role=invitation.role,
        email=invitation.email,
        invited_by_name=invitation.inviter.full_name if invitation.inviter else None,
        expires_at=invitation.expires_at,
    )


@public_router.post("/accept", response_model=BusinessOut)
def accept_invitation(
    body: AcceptIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BusinessOut:
    """Join the business. You must be signed in with the email the invite was sent to."""
    try:
        member = team.accept_invitation(db, body.token, user)
        db.commit()
    except TeamError as exc:
        _fail(db, exc)
    business = member.business
    return BusinessOut.model_validate({**business.__dict__, "role": member.role})
