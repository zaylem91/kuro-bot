"""Data contracts, type definitions, and schema models for Scanlation Bot.

Provides type safety, IDE autocompletion, and structured contracts across
API endpoints, database queries, and Discord UI views.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional, TypedDict

# ---------------------------------------------------------------------------
# Literals / Enums
# ---------------------------------------------------------------------------

MangaStatus = Literal["ongoing", "finished", "paused", "completed", "dropped"]
ChapterStatus = Literal["pending_tl", "pending_pr", "pending_ed", "pending_review", "published", "rejected"]
StaffRole = Literal["TL", "CO-TL", "PR", "CO-PR", "ED", "CO-ED"]
PaymentMethod = Literal["binance", "paypal"]
JobStatus = Literal["pending", "running", "done", "failed"]
InvoiceStatus = Literal["sending", "sent", "failed"]


# ---------------------------------------------------------------------------
# Database & Domain Models (TypedDict)
# ---------------------------------------------------------------------------

class MangaRecord(TypedDict, total=False):
    """Represents a row in the mangas table."""
    id: int
    manga_name: str
    drive_folder_id: str
    status: MangaStatus
    start_chapter: int
    final_subfolder_name: Optional[str]
    last_synced_at: Optional[datetime]
    discord_channel_id: Optional[str]
    webhook_id: Optional[str]
    webhook_token: Optional[str]
    webhook_message_id: Optional[str]
    cover_url: Optional[str]
    mangadex_url: Optional[str]


class ChapterRecord(TypedDict, total=False):
    """Represents a row in the chapters table."""
    id: int
    manga_id: int
    chapter_number: Decimal | float | int
    drive_folder_id: str
    status: ChapterStatus
    assigned_to: Optional[str]
    assigned_at: Optional[datetime]
    images_count: Optional[int]
    created_at: datetime
    updated_at: datetime


class StaffMemberRecord(TypedDict, total=False):
    """Represents a registered user in the users table."""
    discord_id: str
    finance_channel_id: Optional[str]
    timezone: Optional[str]
    default_role: Optional[Literal["TL", "PR", "ED"]]
    payment_method: Optional[PaymentMethod]
    payment_info: Optional[str]
    drive_email: Optional[str]
    monthly_period: Optional[str]
    is_registered: bool
    can_manage_payments: bool
    registered_at: Optional[datetime]


class TransactionRecord(TypedDict, total=False):
    """Represents a payable chapter transaction in the transactions table."""
    id: int
    user_id: str
    chapter_id: int
    role: StaffRole
    amount_cents: int
    status: Literal["unpaid", "paid"]
    payment_id: Optional[int]
    created_at: datetime


class PaymentRecord(TypedDict, total=False):
    """Represents a settled payment receipt in the payments table."""
    id: int
    user_id: str
    amount_cents: int
    paid_by: str
    payment_method: str
    payment_info: str
    created_at: datetime


class PricingConfigDict(TypedDict):
    """Current page rates in cents."""
    tl_cents: int
    pr_cents: int
    ed_under_30: int
    ed_30_44: int
    ed_45_plus: int


class JobRecord(TypedDict, total=False):
    """Represents a persistent background job in the background_jobs table."""
    id: int
    job_type: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    created_at: datetime
    updated_at: datetime


class StaffWorkload(TypedDict):
    """Workload intelligence profile for a team member."""
    discord_id: str
    roles: list[StaffRole]
    active_chapters_count: int
    load_level: Literal["high", "medium", "low", "idle"]
    manga_count: int
