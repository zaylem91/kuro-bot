"""State machine constants and exceptions."""

STATUS_ROLE_MAP = {
    "TL": "pending_tl",
    "CO-TL": "pending_tl",
    "PR": "pending_pr",
    "CO-PR": "pending_pr",
    "ED": "pending_ed",
    "CO-ED": "pending_ed",
}

STAGE_ROLES: dict[str, tuple[str, str]] = {
    "pending_tl": ("TL", "CO-TL"),
    "pending_pr": ("PR", "CO-PR"),
    "pending_ed": ("ED", "CO-ED"),
}

NEXT_STATUS = {
    "pending_tl": "pending_pr",
    "pending_pr": "pending_ed",
    "pending_ed": "pending_review",
    "pending_review": "published",
}

class ChapterAlreadyClaimed(Exception):
    pass

class LedgerInsertFailed(Exception):
    pass
