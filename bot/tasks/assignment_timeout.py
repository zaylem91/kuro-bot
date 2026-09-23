"""Auto-unassign stale claims after 48h."""
import logging
import lightbulb
from bot.database.connection import get_pool

logger = logging.getLogger(__name__)
loader = lightbulb.Loader()

TIMEOUT_HOURS = 48
TIMEOUT_PAGE_SIZE = 100

async def release_stale_claims(bot=None):
    """Release stale claims and send aggregated DM per user (one DM lists all released chapters)."""
    pool = get_pool()
    released_count = 0
    # Collect per-user released chapters for aggregated DM
    per_user: dict[str, list[dict]] = {}
    affected_mangas: set[int] = set()
    released_info: list[dict] = []
    async with pool.acquire() as conn:
        while True:
            # Keep each transaction small. SKIP LOCKED also lets a second
            # timeout worker make progress without waiting on this page.
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, chapter_number, assigned_to, manga_id
                    FROM chapters
                    WHERE assigned_to IS NOT NULL
                      AND assigned_at IS NOT NULL
                      AND assigned_at < now() - ($1 || ' hours')::interval
                      AND status NOT IN ('published','rejected')
                    ORDER BY assigned_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                    """,
                    str(TIMEOUT_HOURS), TIMEOUT_PAGE_SIZE,
                )
                if not rows:
                    break

                ids = [row["id"] for row in rows]
                # Re-check assigned_at to avoid releasing a chapter that was just re-claimed between SELECT and UPDATE
                released = await conn.fetch(
                    """
                    UPDATE chapters
                    SET assigned_to = NULL, assigned_at = NULL, updated_at = now()
                    WHERE id = ANY($1::int[])
                      AND assigned_to IS NOT NULL
                      AND assigned_at IS NOT NULL
                      AND assigned_at < now() - ($2 || ' hours')::interval
                      AND status NOT IN ('published','rejected')
                    RETURNING id, chapter_number, manga_id
                    """,
                    ids,
                    str(TIMEOUT_HOURS),
                )
                if released:
                    released_ids = [row["id"] for row in released]
                    users_by_id = {row["id"]: row["assigned_to"] for row in rows}
                    released_users = [users_by_id[row["id"]] for row in released if row["id"] in users_by_id]
                    # One INSERT handles the whole page and remains in the
                    # same transaction as the ownership release.
                    await conn.execute(
                        """
                        INSERT INTO chapter_history
                            (chapter_id, stage, user_id, action, amount_cents)
                        SELECT chapter_id, 'TIMEOUT', user_id, 'auto_unclaimed', 0
                        FROM unnest($1::int[], $2::text[]) AS page(chapter_id, user_id)
                        """,
                        released_ids, released_users,
                    )
                    released_count += len(released)
                    for row, user_id in zip(released, released_users):
                        logger.info(
                            "Auto-unclaimed chapter %s (#%s) from %s",
                            row["id"], row["chapter_number"], user_id,
                        )
                        affected_mangas.add(row["manga_id"])
                        released_info.append({
                            "id": row["id"],
                            "chapter_number": row["chapter_number"],
                            "manga_id": row["manga_id"],
                            "user_id": user_id,
                        })
                        # Aggregate for DM
                        per_user.setdefault(user_id, []).append({"id": row["id"], "chapter_number": row["chapter_number"]})

    # Update manga webhooks and notify room
    if bot and affected_mangas:
        try:
            from bot.services.webhook_sender import update_manga_webhook, send_webhook_embed
            manga_names: dict[int, str] = {}
            async with pool.acquire() as conn:
                m_rows = await conn.fetch("SELECT id, manga_name FROM mangas WHERE id = ANY($1::int[])", list(affected_mangas))
                manga_names = {r["id"]: r["manga_name"] for r in m_rows}

            for r in released_info:
                mname = manga_names.get(r["manga_id"], "Manga")
                try:
                    await send_webhook_embed(
                        bot,
                        manga_id=r["manga_id"],
                        manga_name=mname,
                        event="unclaim",
                        actor_name="Auto Timeout (48h)",
                        actor_id=r["user_id"],
                        chapter_number=r["chapter_number"],
                    )
                except Exception as ex:
                    logger.debug(f"Failed to send unclaim webhook embed for ch {r['id']}: {ex}")

            for mid in affected_mangas:
                try:
                    await update_manga_webhook(bot, mid)
                except Exception as ex:
                    logger.debug(f"Failed to update manga webhook for manga {mid}: {ex}")
        except Exception as e:
            logger.warning(f"Failed to update webhooks on assignment timeout: {e}")

    # Send aggregated DM per user (one message instead of N)
    if bot and per_user:
        try:
            # Fetch manga names for released chapters in one query
            all_ids = [ch["id"] for lst in per_user.values() for ch in lst]
            manga_map = {}
            if all_ids:
                async with pool.acquire() as _conn:
                    rows = await _conn.fetch(
                        "SELECT c.id, m.manga_name FROM chapters c JOIN mangas m ON m.id=c.manga_id WHERE c.id = ANY($1::int[])",
                        all_ids,
                    )
                    manga_map = {r["id"]: r["manga_name"] for r in rows}
            for user_id, chapters in per_user.items():
                try:
                    from hikari.impl.special_endpoints import ContainerComponentBuilder
                    c = ContainerComponentBuilder(id=1, accent_color=0xE67E22)
                    c.add_text_display(f"## ⏰ Your claimed chapters were auto-released\nYou had **{len(chapters)}** chapter(s) claimed for >{TIMEOUT_HOURS}h without submitting. They are now available for others.")
                    c.add_separator(divider=True)
                    lines = []
                    for ch in chapters[:10]:
                        mname = manga_map.get(ch["id"], "Manga")
                        lines.append(f"• **{mname}** — Ch. {ch['chapter_number']} (#{ch['id']})")
                    if len(chapters) > 10:
                        lines.append(f"*...and {len(chapters)-10} more*")
                    c.add_text_display("\n".join(lines))
                    c.add_separator(divider=True)
                    c.add_text_display("If you still want to work on them, please claim again via Staff Dashboard → Claim Chapter.")
                    from bot.services.notifications import send_dm_if_possible
                    await send_dm_if_possible(bot, user_id, components=[c], flags=__import__("hikari").MessageFlag.IS_COMPONENTS_V2)
                except Exception as e:
                    logger.debug(f"Aggregated DM to {user_id} failed: {e}")
        except Exception as e:
            logger.debug(f"Failed to send aggregated timeout DMs: {e}")
    return released_count

@loader.task(lightbulb.uniformtrigger(3600))  # every hour
async def assignment_timeout_task() -> None:
    try:
        # Try to get bot instance for DM aggregation
        bot = None
        try:
            from bot.core import lb_client
            if lb_client:
                bot = lb_client.app  # type: ignore
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        n = await release_stale_claims(bot)
        if n:
            logger.info(f"Released {n} stale claims (>{TIMEOUT_HOURS}h)")
    except Exception as e:
        logger.error(f"Assignment timeout error: {e}")
