import hikari
from bot.config import TEAM_LEADER_ROLE_ID, ADMIN_ROLE_ID

def is_team_leader(member: hikari.Member | None) -> bool:
    """Check if member is team leader — required for financial operations."""
    if member is None:
        return False
    return TEAM_LEADER_ROLE_ID in member.role_ids

def is_admin(member: hikari.Member | None) -> bool:
    """Check if member is admin or team leader — team leader inherits admin perms."""
    if member is None:
        return False
    perms = getattr(member, "permissions", None)
    if perms is not None and (perms & hikari.Permissions.ADMINISTRATOR):
        return True
    return ADMIN_ROLE_ID in member.role_ids or TEAM_LEADER_ROLE_ID in member.role_ids
