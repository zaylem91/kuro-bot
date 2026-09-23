"""Centralized notification helpers."""
import logging
import hikari
from bot.database.connection import get_pool

logger = logging.getLogger(__name__)

ROLE_MENTION = {
    "TL": "Translator",
    "PR": "Proofreader",
    "ED": "Editor",
}

NEXT_ROLE = {
    "pending_tl": "PR",
    "pending_pr": "ED",
    "pending_ed": "REVIEW",
}

async def get_staff_mentions(manga_id: int, role: str) -> str:
    """Return mentions string for all staff with given role on a manga.

    Base roles also mention their assistants (PR -> PR + CO-PR).
    """
    roles = [role]
    co_map = {"TL": "CO-TL", "PR": "CO-PR", "ED": "CO-ED"}
    if role in co_map:
        roles.append(co_map[role])
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT discord_id FROM manga_staff WHERE manga_id = $1 AND role = ANY($2::text[])", manga_id, roles)
    if not rows:
        return ""
    return " ".join([f"<@{r['discord_id']}>" for r in rows])

async def notify_next_stage(bot: hikari.GatewayBot, chapter: dict, manga_name: str, completed_role: str, batch_chapters: list[str] = None):
    """
    After a /done, notify the next stage staff in the chapter's context.
    We try to send to the channel where /done was used; fallback to no-op if not available.
    Instead this helper returns a mention string for the caller to include in its response.
    """
    # Map current status -> next role to ping
    # TL done -> PR, PR done -> ED, ED done -> Review (admin)
    base_completed = completed_role.replace("CO-", "")
    if base_completed == "TL":
        # Check if PR exists (base or assistant)
        pool = get_pool()
        async with pool.acquire() as conn:
            has_pr = await conn.fetchval("SELECT 1 FROM manga_staff WHERE manga_id = $1 AND role IN ('PR', 'CO-PR')", chapter["manga_id"])
        if has_pr:
            mentions = await get_staff_mentions(chapter["manga_id"], "PR")
            return f"Next: PR {mentions}" if mentions else "Next: PR (no staff assigned)"
        else:
            mentions = await get_staff_mentions(chapter["manga_id"], "ED")
            return f"Skipped PR → Next: ED {mentions}"
    elif base_completed == "PR":
        mentions = await get_staff_mentions(chapter["manga_id"], "ED")
        return f"Next: ED {mentions}" if mentions else "Next: ED"
    elif base_completed == "ED":
        ch_str = ", ".join(batch_chapters) if batch_chapters else str(chapter['chapter_number'])
        return f"Chapter(s) **{manga_name} #{ch_str}** is now in `pending_review` — admins please `/approve` or `/reject`."
    return ""

async def send_dm_if_possible(bot: hikari.GatewayBot, user_id: str, embed=None, components=None, flags=hikari.UNDEFINED):
    try:
        # Use REST to create DM channel
        dm = await bot.rest.create_dm_channel(int(user_id))
        if components:
            await bot.rest.create_message(dm.id, components=components, flags=flags)
        else:
            await bot.rest.create_message(dm.id, embed=embed)
    except Exception as e:
        logger.debug(f"DM to {user_id} failed: {e}")

async def notify_rejection(bot: hikari.GatewayBot, chapter_id: int, manga_name: str, reason: str, rejected_by: str):
    """DM last worker about rejection."""
    pool = get_pool()
    async with pool.acquire() as conn:
        last = await conn.fetchval(
            "SELECT user_id FROM chapter_history WHERE chapter_id = $1 AND action='done' ORDER BY timestamp DESC LIMIT 1",
            chapter_id,
        )
    if not last:
        return
    from hikari.impl.special_endpoints import ContainerComponentBuilder
    container = ContainerComponentBuilder(id=1, accent_color=0xE74C3C)
    container.add_text_display("## Your chapter was rejected\nPlease fix the issues and run `/done` again.")
    container.add_separator(divider=True)
    container.add_text_display(f"**Manga:** {manga_name}\n**Chapter ID:** {chapter_id}\n\n**💬 Reason:** {reason[:1000]}")
    container.add_separator(divider=True)
    container.add_text_display(f"Rejected by <@{rejected_by}>")
    await send_dm_if_possible(bot, last, components=[container], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
