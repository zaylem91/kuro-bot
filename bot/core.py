"""Central registry for shared singletons — populated from __main__.py on startup."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import miru
    import lightbulb
    from bot.services.drive import DriveService

# Assigned in __main__.py
miru_client: miru.Client | None = None
lb_client: lightbulb.Client | None = None
drive_service: DriveService | None = None
health_runner = None  # aiohttp runner for /health


def get_drive_service() -> DriveService:
    """Return DriveService — must be called after bot startup."""
    if drive_service is None:
        raise RuntimeError("Drive service not initialized yet.")
    return drive_service


def get_miru_client() -> miru.Client:
    """Return miru client — must be called after bot startup."""
    if miru_client is None:
        raise RuntimeError("Miru client not initialized yet.")
    return miru_client

def get_bot():
    """Return the hikari app."""
    if lb_client is None:
        raise RuntimeError("lb_client not initialized yet.")
    return lb_client.app
