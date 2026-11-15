from enum import StrEnum


class MemberRole(StrEnum):
    OWNER = "owner"  # everything, including business settings and the whole team
    ADMIN = "admin"  # manager: all data and analytics; manages members and viewers
    MEMBER = "member"  # staff: records sales, purchases, imports, stock counts
    VIEWER = "viewer"  # accountant/advisor: read-only, including analytics


class Permission(StrEnum):
    RECORD = "record"  # create/correct transactions, imports, stock counts, products
    VIEW_FINANCIALS = "view_financials"  # analytics, insights, health score, brief, dashboard
    VIEW_PERSONAL = "view_personal"  # owner's personal expenses, withdrawals, contributions
    MANAGE_TEAM = "manage_team"  # invite, change roles, remove people
    MANAGE_BUSINESS = "manage_business"  # business settings, ownership transfer


ROLE_PERMISSIONS: dict[MemberRole, frozenset[Permission]] = {
    MemberRole.OWNER: frozenset(Permission),
    MemberRole.ADMIN: frozenset(
        {
            Permission.RECORD,
            Permission.VIEW_FINANCIALS,
            Permission.VIEW_PERSONAL,
            Permission.MANAGE_TEAM,
        }
    ),
    MemberRole.MEMBER: frozenset({Permission.RECORD}),
    MemberRole.VIEWER: frozenset({Permission.VIEW_FINANCIALS, Permission.VIEW_PERSONAL}),
}

# Roles each role may grant, change or remove. Ownership only moves via transfer.
MANAGEABLE_ROLES: dict[MemberRole, frozenset[MemberRole]] = {
    MemberRole.OWNER: frozenset({MemberRole.ADMIN, MemberRole.MEMBER, MemberRole.VIEWER}),
    MemberRole.ADMIN: frozenset({MemberRole.MEMBER, MemberRole.VIEWER}),
    MemberRole.MEMBER: frozenset(),
    MemberRole.VIEWER: frozenset(),
}


class TransactionKind(StrEnum):
    """What a transaction *is*. Drives how it affects profit vs. cash (PRD §6, §10, §11)."""

    SALE = "sale"  # revenue
    PURCHASE = "purchase"  # inventory/stock bought for resale -> COGS via WAC
    EXPENSE = "expense"  # business operating expense
    PERSONAL_EXPENSE = "personal_expense"  # personal spending paid with business cash
    OWNER_WITHDRAWAL = "owner_withdrawal"  # cash taken out by the owner
    OWNER_CONTRIBUTION = "owner_contribution"  # owner puts cash in (not revenue)
    OTHER_INCOME = "other_income"  # non-sales business income
    CUSTOMER_PAYMENT = "customer_payment"  # customer settles a credit sale (receivable)
    SUPPLIER_PAYMENT = "supplier_payment"  # business settles a credit purchase (payable)


# Kinds that move money *into* the business; everything else moves money out.
INFLOW_KINDS = frozenset(
    {
        TransactionKind.SALE,
        TransactionKind.OWNER_CONTRIBUTION,
        TransactionKind.OTHER_INCOME,
        TransactionKind.CUSTOMER_PAYMENT,
    }
)

# Kinds that never touch operating profit (PRD §6: withdrawals are cash, not opex).
NON_OPERATING_KINDS = frozenset(
    {
        TransactionKind.PERSONAL_EXPENSE,
        TransactionKind.OWNER_WITHDRAWAL,
        TransactionKind.OWNER_CONTRIBUTION,
        TransactionKind.CUSTOMER_PAYMENT,
        TransactionKind.SUPPLIER_PAYMENT,
    }
)


class Settlement(StrEnum):
    PAID = "paid"  # cash moved at the time of the transaction
    CREDIT = "credit"  # on credit -> receivable (sale) or payable (purchase/expense)


class TransactionStatus(StrEnum):
    CONFIRMED = "confirmed"  # trusted; drives analytics
    PROVISIONAL = "provisional"  # stored, but low confidence / unconfirmed (PRD §19)
    VOIDED = "voided"  # retracted; kept for audit, excluded from analytics


class TransactionSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    API = "api"


class TransactionEventType(StrEnum):
    CREATED = "created"
    CORRECTED = "corrected"
    CONFIRMED = "confirmed"
    VOIDED = "voided"


class CounterpartyKind(StrEnum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"


class ImportStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    COMMITTED = "committed"
    FAILED = "failed"


class ImportRowStatus(StrEnum):
    READY = "ready"  # parsed cleanly; will be committed as confirmed
    NEEDS_REVIEW = "needs_review"  # has issues the user should resolve
    DUPLICATE = "duplicate"  # likely already in the ledger / repeated in file
    SKIPPED = "skipped"  # user chose not to import
    COMMITTED = "committed"
    UNRESOLVED = "unresolved"  # could not be turned into a transaction (PRD §7.1)


class BriefStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT_DATA = "insufficient_data"  # the brief is a prompt to add more records


class NotificationKind(StrEnum):
    WEEKLY_BRIEF = "weekly_brief"
