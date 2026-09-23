"""Centralized webhook sender for manga room notifications (Discohook-style).

Every manga channel has its own webhook (created in /add_manga).
This service sends professional embeds through that webhook for all
workflow events: done, claim, reject, approve, reopen, unclaim, notify.

Falls back to a normal bot message if the webhook is missing.
"""
import asyncio
import logging
from typing import Optional

import hikari

from bot.database.connection import get_pool
from bot.config import EMOJI, TEAM_NAME
from bot.services.ui import sanitize_manga_name, sanitize_mention

logger = logging.getLogger(__name__)

# ── colour palette per event type ──────────────────────────────────
EVENT_COLORS: dict[str, int] = {
    "done":    0x57F287,   # green  — completion
    "claim":   0x5865F2,   # blurple — claimed
    "reject":  0xED4245,   # red    — rejected
    "approve": 0xFEE75C,   # gold   — published
    "reopen":  0xEB459E,   # pink   — reopened
    "unclaim": 0x99AAB5,   # grey   — released
    "notify":  0x3498DB,
    "new_raw": 0x9B59B6,   # purple — new raw
}

EVENT_TITLES: dict[str, str] = {
    "done":    "✅ Chapter Completed",
    "claim":   "🔒 Chapter Claimed",
    "reject":  "❌ Chapter Rejected",
    "approve": "🎉 Chapter Published",
    "reopen":  "🔄 Chapter Reopened",
    "unclaim": "🔓 Chapter Released",
    "notify":  "📢 Next Stage Ready",
    "new_raw":  "📁 New Chapter RAW Available",
}

ROLE_EMOJI: dict[str, str] = {
    "TL": EMOJI.get("tl", "🔤"),
    "PR": EMOJI.get("pr", "📝"),
    "ED": EMOJI.get("ed", "🎨"),
}


# ── helpers ────────────────────────────────────────────────────────

async def _get_webhook_credentials(manga_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (webhook_id, webhook_token, discord_channel_id) for a manga."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT webhook_id, webhook_token, discord_channel_id FROM mangas WHERE id = $1",
            manga_id,
        )
    if not row:
        return None, None, None
    return row["webhook_id"], row["webhook_token"], row["discord_channel_id"]


_GUILD_ICON_CACHE: dict[int, tuple[float, Optional[str]]] = {}
_GUILD_ICON_TTL = 3600
import time as _time

async def _get_guild_icon(bot: hikari.GatewayBot, guild_id: int) -> Optional[str]:
    """Best-effort fetch of the guild icon URL — cached 1h."""
    now = _time.time()
    if guild_id in _GUILD_ICON_CACHE:
        ts, val = _GUILD_ICON_CACHE[guild_id]
        if now - ts < _GUILD_ICON_TTL:
            return val
    # Try cache first
    try:
        cached_guild = bot.cache.get_guild(guild_id)  # type: ignore[attr-defined]
        if cached_guild and cached_guild.icon_hash:
            icon = str(cached_guild.make_icon_url())
            _GUILD_ICON_CACHE[guild_id] = (now, icon)
            return icon
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    try:
        guild = await bot.rest.fetch_guild(guild_id)
        icon = str(guild.make_icon_url()) if guild.icon_hash else None
        _GUILD_ICON_CACHE[guild_id] = (now, icon)
        return icon
    except Exception:
        _GUILD_ICON_CACHE[guild_id] = (now, None)
        return None


async def _get_user_avatar(bot: hikari.GatewayBot, user_id: int) -> Optional[str]:
    """Best-effort fetch of a user's avatar URL — uses cache first to avoid REST."""
    try:
        # Use cache if available (no REST)
        try:
            cached = bot.cache.get_user(user_id)  # type: ignore[attr-defined]
            if cached:
                return str(cached.display_avatar_url)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        user = await bot.rest.fetch_user(user_id)
        return str(user.display_avatar_url)
    except Exception:
        return None


# ── main sender ────────────────────────────────────────────────────

async def send_webhook_embed(
    bot: hikari.GatewayBot,
    *,
    manga_id: int,
    manga_name: str,
    event: str,
    guild_id: Optional[int] = None,
    actor_name: str = "",
    actor_id: Optional[str] = None,
    chapter_number: Optional[int] = None,
    role: Optional[str] = None,
    new_status: Optional[str] = None,
    earned: Optional[str] = None,
    images_count: Optional[int] = None,
    reason: Optional[str] = None,
    extra_text: Optional[str] = None,
    mention_content: Optional[str] = None,
) -> None:
    """Build and send a Discohook-style embed via the manga's webhook.

    Parameters
    ----------
    bot : hikari.GatewayBot
    manga_id : int
    manga_name : str
    event : str          One of EVENT_COLORS keys.
    guild_id : int|None  For fetching guild icon.
    actor_name : str     Display name of the user who triggered the event.
    actor_id : str|None  Discord ID of the actor (for avatar / thumbnail).
    chapter_number : int Chapter number.
    role : str|None      TL / PR / ED (if applicable).
    new_status : str     The chapter's new status after this event.
    earned : str|None    Formatted dollar amount (for /done).
    images_count : int   Image count (for ED /done).
    reason : str|None    Rejection reason (for /reject).
    extra_text : str     Any additional text inside the embed description.
    mention_content : str  Raw text placed *outside* the embed (for @mentions
                           which don't ping inside embeds).
    """
    # Fetch webhook creds and guild/avatar in parallel where possible
    webhook_task = asyncio.create_task(_get_webhook_credentials(manga_id))
    guild_icon_task = asyncio.create_task(_get_guild_icon(bot, guild_id)) if guild_id else None
    avatar_task = asyncio.create_task(_get_user_avatar(bot, int(actor_id))) if actor_id and event in ("done", "claim") else None

    webhook_id, webhook_token, channel_id = await webhook_task
    guild_icon = await guild_icon_task if guild_icon_task else None
    # actor_avatar will be awaited later after container is built, but start early
    # keep avatar_task for later
    color = EVENT_COLORS.get(event, 0x2F3136)
    title = EVENT_TITLES.get(event, event.title())

    from hikari.impl.special_endpoints import ContainerComponentBuilder, ThumbnailComponentBuilder, SectionComponentBuilder
    container = ContainerComponentBuilder(id=1, accent_color=color)

    manga_name_safe = sanitize_manga_name(manga_name)
    _team_suffix = f" • {TEAM_NAME}" if TEAM_NAME else ""
    header_text = f"## {title}\n**{manga_name_safe}{_team_suffix}**\n"
    if extra_text:
        header_text += sanitize_mention(extra_text)
    
    if guild_icon:
        header = SectionComponentBuilder(id=2, accessory=ThumbnailComponentBuilder(id=3, media=guild_icon))
        header.add_text_display(header_text)
        container.add_component(header)
    else:
        container.add_text_display(header_text)

    container.add_separator(divider=True)

    details_lines = []
    role_icon = ROLE_EMOJI.get(role, "👤") if role else "👤"

    ch_text = f"#{chapter_number}" if chapter_number is not None else "—"
    
    if event == "done":
        details_lines.append(f"**{role_icon} {role or 'Worker'}:** <@{actor_id}>" if actor_id else f"**{role_icon} {role or 'Worker'}:** {actor_name}")
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        if earned:
            details_lines.append(f"**💰 Earned:** {earned}")
        if images_count is not None and role == "ED":
            details_lines.append(f"**🖼️ Images:** {images_count}")
        if new_status:
            details_lines.append(f"**{EMOJI.get('status', '📊')} New Status:** `{new_status}`")
            
    elif event == "claim":
        details_lines.append(f"**👤 Claimed by:** <@{actor_id}>" if actor_id else f"**👤 Claimed by:** {actor_name}")
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        if role:
            details_lines.append(f"**{role_icon} Role:** {role}")
        if new_status:
            details_lines.append(f"**{EMOJI.get('status', '📊')} Stage:** `{new_status}`")
            
    elif event == "unclaim":
        details_lines.append(f"**👤 Released by:** <@{actor_id}>" if actor_id else f"**👤 Released by:** {actor_name}")
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        details_lines.append("This chapter is now available for anyone.")
        
    elif event == "reject":
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        if new_status:
            details_lines.append(f"**{EMOJI.get('status', '📊')} Reverted to:** `{new_status}`")
        if reason:
            details_lines.append(f"\n**💬 Reason:** {sanitize_mention(reason[:1024])}")
            
    elif event == "approve":
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        details_lines.append(f"**{EMOJI.get('status', '📊')} Status:** `published` ✅")
        
    elif event == "reopen":
        details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        if new_status:
            details_lines.append(f"**{EMOJI.get('status', '📊')} Reset to:** `{new_status}`")
            
    elif event == "notify":
        if chapter_number is not None:
            details_lines.append(f"**{EMOJI.get('manga', '📝')} Chapter:** {ch_text}")
        if new_status:
            details_lines.append(f"**{EMOJI.get('status', '📊')} Stage:** `{new_status}`")

    if mention_content:
        details_lines.append(f"\n🔔 {mention_content.strip()}")

    container.add_text_display("\n".join(details_lines))
    
    actor_avatar = await avatar_task if avatar_task else None

    container.add_separator(divider=True)
    footer_text = f"{actor_name}{_team_suffix}" if actor_name else (TEAM_NAME or "Scanlation")
    if actor_avatar and event in ("done", "claim"):
        footer = SectionComponentBuilder(id=4, accessory=ThumbnailComponentBuilder(id=5, media=actor_avatar))
        footer.add_text_display(footer_text)
        container.add_component(footer)
    else:
        container.add_text_display(footer_text)

    # Discord webhook username limit 80 chars (same as creation)
    _suffix = _team_suffix
    _mname_safe = sanitize_manga_name(manga_name)
    _max = 80 - len(_suffix)
    _uname = f"{_mname_safe[:_max]}{_suffix}" if len(_mname_safe) + len(_suffix) > 80 else f"{_mname_safe}{_suffix}"
    if webhook_id and webhook_token:
        try:
            await bot.rest.execute_webhook(
                int(webhook_id),
                webhook_token,
                components=[container],
                flags=hikari.MessageFlag.IS_COMPONENTS_V2,
                username=_uname,
                avatar_url=guild_icon or hikari.UNDEFINED,
                user_mentions=True,
                role_mentions=True,
            )
            return
        except Exception as e:
            logger.warning(f"Webhook send failed for manga {manga_name} (event={event}): {e}")

    if channel_id:
        try:
            await bot.rest.create_message(
                int(channel_id), 
                components=[container],
                flags=hikari.MessageFlag.IS_COMPONENTS_V2,
                user_mentions=True,
                role_mentions=True,
            )
        except hikari.ForbiddenError as e:
            logger.warning(f"Fallback failed for {manga_name}: missing permissions in channel {channel_id}: {e}")
        except hikari.NotFoundError as e:
            logger.warning(f"Fallback failed for {manga_name}: channel {channel_id} not found: {e}")
        except Exception as e:
            logger.warning(f"Fallback message also failed for manga {manga_name}: {e}")


# ── debounce for webhook updates ───────────────────────────────────
# When drive_sync updates 50 chapters, each triggers update_manga_webhook.
# Without debouncing this causes 50x DB queries + 50x Discord API edits.
# The debounce coalesces rapid calls for the same manga into one execution.
_debounce_tasks: dict[int, asyncio.Task] = {}
_DEBOUNCE_DELAY = 2.0  # seconds


async def update_manga_webhook(bot: hikari.GatewayBot, manga_id: int):
    """Debounced webhook update — coalesces rapid calls for the same manga."""
    existing = _debounce_tasks.get(manga_id)
    if existing and not existing.done():
        existing.cancel()

    async def _debounced():
        try:
            await asyncio.sleep(_DEBOUNCE_DELAY)
            await _update_manga_webhook_impl(bot, manga_id)
        except asyncio.CancelledError:
            pass  # superseded by a newer call
        except Exception as e:
            logger.warning(f"Debounced webhook update failed for manga {manga_id}: {e}")
        finally:
            _debounce_tasks.pop(manga_id, None)

    _debounce_tasks[manga_id] = asyncio.create_task(_debounced())


async def _update_manga_webhook_impl(bot: hikari.GatewayBot, manga_id: int):
    """Update the original manga webhook message with the latest progress."""
    from bot.database.connection import get_pool
    from bot.config import EMOJI
    pool = get_pool()
    async with pool.acquire() as conn:
        manga = await conn.fetchrow("SELECT * FROM mangas WHERE id = $1", manga_id)
        if not manga:
            return
        # Recover missing webhook (e.g., failed at creation due to name length before fix)
        if not manga["webhook_id"] or not manga["webhook_token"]:
            if not manga["discord_channel_id"]:
                logger.warning(f"Cannot create webhook for {manga['manga_name']}: no channel")
                return
            try:
                _suffix = f" • {TEAM_NAME}" if TEAM_NAME else ""
                _mname = manga["manga_name"] or "Manga"
                _max = 80 - len(_suffix)
                _wname = f"{_mname[:_max]}{_suffix}" if len(_mname) + len(_suffix) > 80 else f"{_mname}{_suffix}"
                wh = await bot.rest.create_webhook(int(manga["discord_channel_id"]), name=_wname)
                _wid = str(wh.id)
                _wtok = wh.token
                if not _wtok:
                    try:
                        wh2 = await bot.rest.fetch_webhook(wh.id)
                        _wtok = wh2.token
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                if _wid and _wtok:
                    await conn.execute("UPDATE mangas SET webhook_id=$1, webhook_token=$2 WHERE id=$3", _wid, _wtok, manga_id)
                    # refresh local copy
                    manga = dict(manga)
                    manga["webhook_id"] = _wid
                    manga["webhook_token"] = _wtok
                else:
                    logger.warning(f"Webhook created but token missing for {manga['manga_name']}")
                    return
            except Exception as e:
                logger.warning(f"Failed to create missing webhook for {manga['manga_name']}: {e}")
                return
            
        staff_rows = await conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id = $1", manga_id)
        
        # Aggregate in SQL instead of fetching all rows into Python
        rows = await conn.fetch("SELECT status, COUNT(*) as cnt FROM chapters WHERE manga_id = $1 GROUP BY status", manga_id)
        counts = {r["status"]: r["cnt"] for r in rows}
        total_raw = sum(counts.values())
        total_tl = counts.get('published', 0) + counts.get('pending_review', 0) + counts.get('pending_ed', 0) + counts.get('pending_pr', 0)
        total_ed = counts.get('published', 0) + counts.get('pending_review', 0)

    drive_url = f"https://drive.google.com/drive/folders/{manga['drive_folder_id']}"
    
    status_emoji = "🟢 Ongoing"
    if manga["status"] == "paused":
        status_emoji = "⏸️ Paused"
    elif manga["status"] in ("completed", "finished"):
        status_emoji = "✅ Completed" if manga["status"] == "completed" else "🏁 Finished"
    elif manga["status"] == "dropped":
        status_emoji = "❌ Dropped"
        
    desc_lines = [f"**{status_emoji} Project • Updated Live**", ""]
    if manga['discord_channel_id']:
        desc_lines.append(f"💬 **<#{manga['discord_channel_id']}>**")

    from hikari.impl.special_endpoints import ContainerComponentBuilder, LinkButtonBuilder
    container = ContainerComponentBuilder(id=1)

    manga_name_safe = sanitize_manga_name(manga["manga_name"])
    header_text = f"## {EMOJI.get('manga', '📚')} {manga_name_safe}\n" + "\n".join(desc_lines)
    
    container.add_text_display(header_text)
    if manga.get('cover_url'):
        from hikari.impl.special_endpoints import MediaGalleryItemBuilder
        container.add_media_gallery([MediaGalleryItemBuilder(media=manga['cover_url'])])
    
    container.add_separator(divider=True)
    
    staff_roles = {"TL": [], "CO-TL": [], "PR": [], "CO-PR": [], "ED": [], "CO-ED": []}
    for row in staff_rows:
        staff_roles[row["role"]].append(f"<@{row['discord_id']}>")
    
    staff_text = ""
    if staff_roles["TL"] or staff_roles["CO-TL"]:
        staff_text += f"**{EMOJI.get('tl', '📝')} Translators:** {', '.join(staff_roles['TL']) or 'None'}\n"
        if staff_roles["CO-TL"]:
            staff_text += f"↳ *Assistants:* {', '.join(staff_roles['CO-TL'])}\n"
    
    if staff_roles["ED"] or staff_roles["CO-ED"]:
        staff_text += f"**{EMOJI.get('ed', '💻')} Editors:** {', '.join(staff_roles['ED']) or 'None'}\n"
        if staff_roles["CO-ED"]:
            staff_text += f"↳ *Assistants:* {', '.join(staff_roles['CO-ED'])}\n"
            
    if staff_roles["PR"] or staff_roles["CO-PR"]:
        staff_text += f"**{EMOJI.get('pr', '🔍')} Proofreaders:** {', '.join(staff_roles['PR']) or 'None'}\n"
        if staff_roles["CO-PR"]:
            staff_text += f"↳ *Assistants:* {', '.join(staff_roles['CO-PR'])}\n"
            
    if not staff_text:
        staff_text = "*No staff assigned yet.*\n"
        
    container.add_text_display(staff_text.strip())
    
    container.add_separator(divider=True)
    
    container.add_text_display(
        f"### 📊 Progress Stats\n"
        f"**{EMOJI.get('raw', '📁')} Total RAWs:** `{total_raw}`\n"
        f"**{EMOJI.get('tl_done', '📝')} Translated:** `{total_tl}`\n"
        f"**{EMOJI.get('ed_done', '✅')} Finalized:** `{total_ed}`"
    )

    import re
    drive_emoji_str = EMOJI.get("gdrive", "📁")
    match = re.search(r'<a?:.*?:(\d+)>', drive_emoji_str)
    drive_emoji = int(match.group(1)) if match else drive_emoji_str
    # Build buttons row: Drive + optional MangaDex
    buttons = [LinkButtonBuilder(url=drive_url, label="Google Drive Folder", emoji=drive_emoji)]
    mangadex_url = (manga.get("mangadex_url") or "").strip()
    if mangadex_url:
        if not mangadex_url.startswith(("http://", "https://")):
            mangadex_url = "https://" + mangadex_url
        mdx_emoji_str = EMOJI.get("mangadex", "📖")
        mdx_match = re.search(r'<a?:.*?:(\d+)>', mdx_emoji_str)
        mdx_emoji = int(mdx_match.group(1)) if mdx_match else mdx_emoji_str
        buttons.append(LinkButtonBuilder(url=mangadex_url, label="MangaDex", emoji=mdx_emoji))
    container.add_action_row(buttons)
    
    def _get_bot_avatar(bot):
        try:
            me = bot.get_me()
            if me:
                return str(me.display_avatar_url)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        return None

    bot_avatar = _get_bot_avatar(bot)
    # Fallback to REST only if cache miss
    if not bot_avatar:
        try:
            me = await bot.rest.fetch_my_user()
            bot_avatar = str(me.display_avatar_url)
        except Exception:
            bot_avatar = None

    try:
        if manga["webhook_message_id"]:
            try:
                await bot.rest.edit_webhook_message(
                    int(manga["webhook_id"]),
                    manga["webhook_token"],
                    int(manga["webhook_message_id"]),
                    components=[container],
                )
            except hikari.NotFoundError:
                logger.info(f"Webhook message {manga['webhook_message_id']} not found for {manga['manga_name']}, recreating")
                _suf = f" • {TEAM_NAME}" if TEAM_NAME else ""
                _safe = sanitize_manga_name(manga['manga_name'])
                _max2 = 80 - len(_suf)
                _uname2 = f"{_safe[:_max2]}{_suf}" if len(_safe) + len(_suf) > 80 else f"{_safe}{_suf}"
                msg = await bot.rest.execute_webhook(
                    int(manga["webhook_id"]),
                    manga["webhook_token"],
                    components=[container],
                    username=_uname2,
                    avatar_url=bot_avatar or None,
                )
                if msg:
                    async with pool.acquire() as conn2:
                        await conn2.execute("UPDATE mangas SET webhook_message_id=$1 WHERE id=$2", str(msg.id), manga_id)
            except hikari.ForbiddenError as e:
                logger.warning(f"Webhook edit forbidden for {manga['manga_name']}: {e}")
        else:
            _suf3 = f" • {TEAM_NAME}" if TEAM_NAME else ""
            _safe3 = sanitize_manga_name(manga['manga_name'])
            _max3 = 80 - len(_suf3)
            _uname3 = f"{_safe3[:_max3]}{_suf3}" if len(_safe3) + len(_suf3) > 80 else f"{_safe3}{_suf3}"
            msg = await bot.rest.execute_webhook(
                int(manga["webhook_id"]),
                manga["webhook_token"],
                components=[container],
                username=_uname3,
                avatar_url=bot_avatar or None,
            )
            if msg:
                async with pool.acquire() as conn2:
                    await conn2.execute("UPDATE mangas SET webhook_message_id=$1 WHERE id=$2", str(msg.id), manga_id)
    except Exception as e:
        logger.warning(f"Failed to send/update webhook msg for {manga['manga_name']}: {e}")
