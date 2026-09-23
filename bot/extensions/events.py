import asyncio
import hikari
import lightbulb
import logging
from bot.config import TEAM_NAME
from bot.database.connection import get_pool
from bot.services.webhook_sender import update_manga_webhook
from bot.services.ui import create_warning

loader = lightbulb.Loader()
logger = logging.getLogger(__name__)

@loader.listener(hikari.MemberDeleteEvent)
async def on_member_leave(event: hikari.MemberDeleteEvent):
    user_id = str(event.user_id)
    pool = get_pool()
    drive_email = None
    manga_drive_map: dict[int, str] = {}
    manga_channel_map: dict[int, str] = {}
    async with pool.acquire() as conn:
        # Fetch Gmail for Drive revoke (before any delete)
        try:
            row = await conn.fetchrow("SELECT drive_email FROM users WHERE discord_id=$1", user_id)
            drive_email = row["drive_email"] if row and row["drive_email"] else None
        except Exception as e:
            logger.warning(f"Failed to fetch drive email: {e}")
            drive_email = None
            
        async with conn.transaction():
            # Always mark user as left — hide from Workload (which filters is_registered=TRUE)
            # Keep row for audit/history but mark inactive
            try:
                await conn.execute(
                    "UPDATE users SET is_registered = FALSE WHERE discord_id = $1 AND is_registered = TRUE",
                    user_id,
                )
            except Exception as e:
                logger.warning(f"Failed to mark user as unregistered: {e}")
            try:
                # Also clear cached workload/manga options
                from bot.services.cache import workload_cache, manga_options_cache
                workload_cache.clear()
                manga_options_cache.clear()
            except Exception as e:
                logger.warning(f"Failed to clear caches: {e}")
            # Check if they are in any mangas (fetch full rows for audit)
            staff_rows = await conn.fetch("SELECT manga_id, role FROM manga_staff WHERE discord_id = $1", user_id)
            mangas = staff_rows  # keep same variable name for later but now has role
            # For distinct manga_id list
            distinct_manga_ids = list({r["manga_id"] for r in staff_rows}) if staff_rows else []
            # Fetch drive_folder_ids + channel ids for revoke (before delete)
            if staff_rows:
                try:
                    m_ids = distinct_manga_ids
                    drows = await conn.fetch("SELECT id, drive_folder_id, discord_channel_id FROM mangas WHERE id = ANY($1::int[])", m_ids)
                    if drive_email:
                        manga_drive_map = {r["id"]: r["drive_folder_id"] for r in drows}
                    manga_channel_map = {r["id"]: r["discord_channel_id"] for r in drows if r["discord_channel_id"]}
                except Exception as e:
                    logger.warning(f"Failed to fetch manga drive and channel maps: {e}")
            # Release any chapters claimed by the leaving member (avoid stale claims up to 48h)
            try:
                released_rows = await conn.fetch(
                    "UPDATE chapters SET assigned_to = NULL, assigned_at = NULL, updated_at = now() "
                    "WHERE assigned_to = $1 "
                    "RETURNING id, status",
                    user_id,
                )
                if released_rows:
                    for ch in released_rows:
                        try:
                            await conn.execute(
                                "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents, reason) "
                                "VALUES ($1, $2, $3, 'auto_unclaimed', 0, 'member_left')",
                                ch["id"], ch["status"] or "UNKNOWN", user_id
                            )
                        except Exception as e:
                            logger.warning(f"Failed to insert chapter_history for auto_unclaimed chapter {ch['id']}: {e}")
            except Exception as e:
                logger.warning(f"Failed to release chapters: {e}")

            if not staff_rows:
                # Still revoke drive access? No mangas, so nothing to revoke
                logger.info(f"Member {user_id} left — no mangas assigned, marked as unregistered")
                # Still notify owner that someone left even if 0 mangas? keep quiet to avoid spam
                return
                
            # Delete from manga_staff so they are fully removed
            await conn.execute("DELETE FROM manga_staff WHERE discord_id = $1", user_id)
            # Audit each auto_removed entry
            for r in staff_rows:
                try:
                    await conn.execute(
                        "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1,$2,$3,'auto_removed',$4)",
                        r["manga_id"], user_id, r["role"], user_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to insert audit log for auto_removed: {e}")
    # Revoke Drive access for each manga (outside DB transaction)
    if drive_email and manga_drive_map:
        try:
            from bot.core import get_drive_service
            for mid, fid in manga_drive_map.items():
                if fid:
                    try:
                        await get_drive_service().revoke_folder_access(fid, drive_email)
                    except Exception as e:
                        logger.warning(f"Drive revoke failed for {drive_email} on manga {mid}: {e}")
        except Exception as e:
            logger.warning(f"Drive revoke on leave failed for {user_id}: {e}")
    # Clean up Discord room overwrites for each manga they were on (best-effort)
    if manga_channel_map:
        try:
            from bot.services.manga_access import revoke_channel_access
            for mid, cid in manga_channel_map.items():
                if cid:
                    try:
                        await revoke_channel_access(event.app, cid, user_id)
                    except Exception as e:
                        logger.warning(f"Channel revoke failed for {user_id} on manga {mid}: {e}")
        except Exception as e:
            logger.warning(f"Channel revoke on leave failed for {user_id}: {e}")
    async with pool.acquire() as conn:
        
        # Update webhooks for all affected mangas — parallelized (was sequential)
        manga_names = []
        # Dedup manga_ids (user may have multiple roles per manga)
        manga_ids = list({m["manga_id"] for m in mangas})
        rows = await conn.fetch("SELECT id, manga_name FROM mangas WHERE id = ANY($1::int[])", manga_ids)
        id_to_name = {r["id"]: r["manga_name"] for r in rows}
        for mid in manga_ids:
            name = id_to_name.get(mid)
            if name:
                manga_names.append(name)
        # Parallel webhook updates
        results = await asyncio.gather(
            *(update_manga_webhook(event.app, mid) for mid in manga_ids),
            return_exceptions=True,
        )
        for mid, res in zip(manga_ids, results):
            if isinstance(res, Exception):
                logger.error(f"Failed to update webhook for manga {mid} after {event.user_id} left: {res}")
                
    # Send notification to guild owner
    try:
        guild = await event.app.rest.fetch_guild(event.guild_id)
        owner_id = guild.owner_id
        owner = await event.app.rest.fetch_user(owner_id)
        
        msg = (
            f"**⚠️ Staff Member Left!**\n"
            f"User <@{user_id}> (`{user_id}`) has left the server.\n\n"
            f"They were automatically **removed from {len(manga_ids)} mangas**:\n"
            + "\n".join(f"- {name}" for name in manga_names) + "\n\n"
            "Even if they rejoin the server, they will not regain access to these mangas until you manually reassign them."
        )
        
        c = create_warning(msg, title="Security Alert: Staff Left")
        
        dm_channel = await owner.fetch_dm_channel()
        await dm_channel.send(components=[c], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        logger.info(f"Notified owner {owner_id} about {user_id} leaving.")
    except Exception as e:
        logger.error(f"Could not notify owner about user leaving: {e}")


@loader.listener(hikari.MemberCreateEvent)
async def on_member_join(event: hikari.MemberCreateEvent):
    """Detect if a returning member was previously in the team and notify them."""
    if event.member.is_bot:
        return
    user_id = str(event.user.id)
    pool = get_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT is_registered, payment_method, payment_info, drive_email FROM users WHERE discord_id = $1",
            user_id,
        )
        if not user_row:
            return  # Brand new user, never registered with the bot

        # Check past roles
        role_rows = await conn.fetch("SELECT role FROM user_roles WHERE discord_id = $1", user_id)
        roles = [r["role"] for r in role_rows]

        # Check unpaid transactions
        unpaid_cents = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE user_id = $1 AND status = 'unpaid'",
            user_id,
        )

        logger.info(f"Returning team member rejoined: {user_id} (registered={user_row['is_registered']}, roles={roles})")

        # Send welcoming direct message to member
        try:
            from bot.services.ui import create_info
            _team = TEAM_NAME or "the team"
            msg = (
                f"👋 **Welcome back to {_team}!**\n"
                f"We detected that you rejoined the server.\n\n"
                f"• **User ID:** `{user_id}`\n"
                f"• **Previous Roles:** {', '.join(roles) if roles else 'None'}\n"
                f"• **Pending Balance:** ${unpaid_cents / 100:.2f}\n\n"
                "If you wish to resume work with the team, please contact administration or use the `/staff` command to update your information."
            )
            c = create_info(msg, title="Welcome Back")
            await dm_channel.send(components=[c], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        except Exception as e:
            logger.debug(f"Could not send rejoin DM to {user_id}: {e}")

        # Notify guild owner
        try:
            from bot.services.ui import create_info
            guild = await event.app.rest.fetch_guild(event.guild_id)
            owner = await event.app.rest.fetch_user(guild.owner_id)
            admin_msg = (
                f"ℹ️ **Former Staff Member Rejoined**\n"
                f"User <@{user_id}> (`{user_id}`) has rejoined the server.\n\n"
                f"• **Previous Roles:** {', '.join(roles) if roles else 'None'}\n"
                f"• **Drive Email:** `{user_row['drive_email'] or 'Not set'}`\n"
                f"• **Unpaid Balance:** ${unpaid_cents / 100:.2f}\n\n"
                "Their previous manga assignments were cleared when they left. Use `/manage_manga` or the dashboard to reassign if needed."
            )
            c_admin = create_info(admin_msg, title="Staff Rejoined Server")
            owner_dm = await owner.fetch_dm_channel()
            await owner_dm.send(components=[c_admin], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        except Exception as e:
            logger.debug(f"Could not notify owner about rejoin of {user_id}: {e}")
