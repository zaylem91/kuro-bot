"""Sync DB roles (user_roles) with Discord roles (TL/PR/ED)."""
import logging
import hikari
from bot.config import ROLE_ID_MAP

logger = logging.getLogger(__name__)

async def assign_discord_role(bot: hikari.GatewayBot | hikari.RESTBot, guild_id: int, user_id: int, role: str):
    """Assign Discord role for a team role (TL/PR/ED). No-op if not configured."""
    role_id = ROLE_ID_MAP.get(role)
    if not role_id:
        logger.debug(f"No Discord role configured for {role}, skipping assign")
        return False
    try:
        await bot.rest.add_role_to_member(guild_id, user_id, role_id)
        logger.info(f"Assigned Discord role {role} ({role_id}) to {user_id}")
        return True
    except hikari.ForbiddenError:
        logger.warning(f"Missing permissions to assign {role} to {user_id}")
        return False
    except Exception as e:
        logger.warning(f"Failed to assign {role} to {user_id}: {e}")
        return False

async def remove_discord_role(bot: hikari.GatewayBot | hikari.RESTBot, guild_id: int, user_id: int, role: str):
    role_id = ROLE_ID_MAP.get(role)
    if not role_id:
        return False
    try:
        await bot.rest.remove_role_from_member(guild_id, user_id, role_id)
        logger.info(f"Removed Discord role {role} ({role_id}) from {user_id}")
        return True
    except hikari.ForbiddenError:
        logger.warning(f"Missing permissions to remove {role} from {user_id}")
        return False
    except Exception as e:
        logger.warning(f"Failed to remove {role} from {user_id}: {e}")
        return False

async def sync_user_roles(bot, guild_id: int, user_id: int, db_roles: list[str]):
    """Ensure user has Discord roles matching DB roles (for repair)."""
    for role in ["TL", "PR", "ED"]:
        has_db = role in db_roles
        # Check current Discord roles via API? We trust DB as source of truth for now
        # Just assign if has_db
        if has_db:
            await assign_discord_role(bot, guild_id, user_id, role)
