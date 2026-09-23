"""Discord channel access helpers for manga rooms.

Each manga has its own text channel (mangas.discord_channel_id) that is hidden
from @everyone. Staff assigned to a manga must get an explicit member overwrite
so they can see and work in that manga's room. When they are removed from that
manga (and have no remaining roles on it), the overwrite must be deleted — plus
their Google Drive access for that manga's folder must be revoked.

Only the affected manga is touched; other mangas are never affected.
"""
import logging
import hikari

logger = logging.getLogger(__name__)

# What a manga staff member needs inside the manga room.
STAFF_ALLOW = (
    hikari.Permissions.VIEW_CHANNEL
    | hikari.Permissions.SEND_MESSAGES
    | hikari.Permissions.READ_MESSAGE_HISTORY
    | hikari.Permissions.ATTACH_FILES
    | hikari.Permissions.EMBED_LINKS
    | hikari.Permissions.ADD_REACTIONS
)


async def grant_channel_access(app, channel_id, user_id) -> bool | None:
    """Grant a member access to a manga room. Returns True/False, None if skipped.

    Idempotent — safe to call when the overwrite already exists.
    """
    if not channel_id or not user_id:
        return None
    try:
        await app.rest.edit_permission_overwrite(
            int(channel_id),
            int(user_id),
            target_type=hikari.PermissionOverwriteType.MEMBER,
            allow=STAFF_ALLOW,
        )
        return True
    except hikari.NotFoundError:
        logger.warning(f"Channel {channel_id} not found for grant to {user_id}")
        return False
    except hikari.ForbiddenError:
        logger.warning(f"Missing permissions to grant {user_id} access to channel {channel_id}")
        return False
    except Exception as e:
        logger.warning(f"Failed to grant channel {channel_id} to {user_id}: {e}")
        return False


async def revoke_channel_access(app, channel_id, user_id) -> bool | None:
    """Remove a member's overwrite from a manga room. Returns True/False, None if skipped.

    Deleting the overwrite (not denying) restores the default hidden state.
    Idempotent — NotFound means already clean.
    """
    if not channel_id or not user_id:
        return None
    try:
        await app.rest.delete_permission_overwrite(int(channel_id), int(user_id))
        return True
    except hikari.NotFoundError:
        # Already clean (channel or overwrite gone)
        return True
    except hikari.ForbiddenError:
        logger.warning(f"Missing permissions to revoke {user_id} from channel {channel_id}")
        return False
    except Exception as e:
        logger.warning(f"Failed to revoke channel {channel_id} from {user_id}: {e}")
        return False
