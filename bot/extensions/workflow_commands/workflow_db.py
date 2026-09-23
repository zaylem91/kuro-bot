import asyncio
import logging
import asyncpg
import hikari
import lightbulb
from bot.database.connection import get_pool
from bot.services.pricing import get_price_for_role, refresh_pricing_cache, format_cents_as_dollars
from bot.services.ui import create_error, create_success, create_info
from bot.extensions.workflow_commands.workflow_state import STATUS_ROLE_MAP, STAGE_ROLES, NEXT_STATUS, ChapterAlreadyClaimed, LedgerInsertFailed

logger = logging.getLogger(__name__)

async def manga_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    pool = get_pool()
    q = ctx.focused.value or ""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT manga_name FROM mangas WHERE manga_name ILIKE $1 LIMIT 25", f"%{q}%")
    await ctx.respond([r["manga_name"] for r in rows])

async def _finalize_done(ctx: "miru.ViewContext", chapter_id: int, user_role: str, user_id: str, images_count: int, warnings: list[str] | None = None) -> None:
    """Execute the DB transaction and notifications for /done completion.
    Shared by ChapterSelectView._on_select (no warnings) and EDWarningConfirmView (confirmed despite warnings)."""
    warnings = warnings or []
    pool = get_pool()
    expected_status = STATUS_ROLE_MAP[user_role]
    # Refresh pricing cache before transaction (avoid extra DB hit inside tx)
    try:
        await refresh_pricing_cache(pool)
    except Exception as e:
        logger.error(f"Failed to refresh pricing cache: {e}")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                chapter = await conn.fetchrow(
                    "SELECT * FROM chapters WHERE id = $1 FOR UPDATE",
                    chapter_id,
                )
                if not chapter:
                    await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                if chapter["status"] != expected_status:
                    await ctx.respond(
                        components=[create_error("This chapter is not in your stage or was already updated.")],
                        flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
                    )
                    return
                if chapter["assigned_to"] and chapter["assigned_to"] != user_id:
                     await ctx.respond(components=[create_error("This chapter is assigned to another member.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                     return
                # Re-validate roles inside the transaction (closes removal/hijack window):
                # user must hold a global role AND a manga role for this stage.
                stage_roles = STAGE_ROLES.get(expected_status, (user_role,))
                has_global = await conn.fetchval(
                    "SELECT 1 FROM user_roles WHERE discord_id = $1 AND role = ANY($2::text[])",
                    user_id, list(stage_roles),
                )
                if not has_global:
                    await ctx.respond(components=[create_error("You no longer hold a role for this stage.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                is_staff_now = await conn.fetchval(
                    "SELECT 1 FROM manga_staff WHERE manga_id = $1 AND discord_id = $2 AND role = ANY($3::text[])",
                    chapter["manga_id"], user_id, list(stage_roles),
                )
                if not is_staff_now:
                    await ctx.respond(components=[create_error("You are no longer staff for this manga with that role.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return

                new_status = NEXT_STATUS[expected_status]
                if user_role in ("TL", "CO-TL"):
                    pr_exists = await conn.fetchval(
                        "SELECT 1 FROM manga_staff WHERE manga_id = $1 AND role IN ('PR', 'CO-PR')",
                        chapter["manga_id"],
                    )
                    if not pr_exists:
                        new_status = "pending_ed"

                if user_role in ("ED", "CO-ED"):
                    await conn.execute(
                        "UPDATE chapters SET images_count = $1 WHERE id = $2",
                        images_count, chapter_id,
                    )

                price = get_price_for_role(user_role, images_count)
                res = await conn.execute(
                    """UPDATE chapters
                       SET status = $1, assigned_to = NULL, assigned_at = NULL, updated_at = now()
                        WHERE id = $2 AND status = $3""",
                    new_status, chapter_id, expected_status,
                )
                if res == "UPDATE 0":
                    raise ChapterAlreadyClaimed()
                # Prevent double-payout if already paid previously (e.g. rework after payout)
                paid_already = await conn.fetchval(
                    "SELECT 1 FROM transactions WHERE user_id = $1 AND chapter_id = $2 AND role = $3 AND status = 'paid'",
                    user_id, chapter_id, user_role
                )
                if not paid_already:
                    try:
                        await conn.execute(
                            """INSERT INTO transactions (user_id, chapter_id, role, amount_cents)
                               VALUES ($1, $2, $3, $4)
                               ON CONFLICT (user_id, chapter_id, role) DO UPDATE 
                               SET amount_cents = EXCLUDED.amount_cents, status = 'unpaid'
                               WHERE transactions.status != 'paid'""",
                            user_id, chapter_id, user_role, price
                        )
                    except asyncpg.exceptions.UniqueViolationError:
                        raise LedgerInsertFailed()
                else:
                    logger.info(f"Chapter {chapter_id} was already paid to {user_id} ({user_role}). Preserving paid status.")
                await conn.execute(
                    """INSERT INTO chapter_history
                           (chapter_id, stage, user_id, action, amount_cents)
                       VALUES ($1, $2, $3, 'done', $4)""",
                    chapter_id, user_role, user_id, (0 if paid_already else price),
                )

        price_str = format_cents_as_dollars(price) + (" (already paid)" if paid_already else "")
        is_ed_stage = user_role in ("ED", "CO-ED")
        is_tl_stage = user_role in ("TL", "CO-TL")
        is_pr_stage = user_role in ("PR", "CO-PR")
        images_info = f" | {images_count} images" if is_ed_stage else ""
        if warnings:
            images_info += f"\n⚠️ Warnings acknowledged: {'; '.join(warnings)}"
        # Notify next stage — single DB fetch + parallel mentions
        next_msg = ""
        mention_content = None
        manga_name = "Manga"
        try:
            from bot.services.notifications import notify_next_stage, get_staff_mentions
            async with pool.acquire() as conn2:
                mrow = await conn2.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
                manga_name = mrow["manga_name"] if mrow else "Manga"
                if is_tl_stage:
                    has_pr = await conn2.fetchval("SELECT 1 FROM manga_staff WHERE manga_id = $1 AND role IN ('PR', 'CO-PR')", chapter["manga_id"])
                else:
                    has_pr = False
            base_role = user_role.replace("CO-", "")
            if is_tl_stage:
                if has_pr:
                    mention_task = asyncio.create_task(get_staff_mentions(chapter["manga_id"], "PR"))
                else:
                    mention_task = asyncio.create_task(get_staff_mentions(chapter["manga_id"], "ED"))
                notify_task = asyncio.create_task(notify_next_stage(ctx.client.app, dict(chapter), manga_name, base_role))
                next_msg, mention_content = await asyncio.gather(notify_task, mention_task)
                if not mention_content:
                    mention_content = None
            elif is_pr_stage:
                next_msg, mention_content = await asyncio.gather(
                    notify_next_stage(ctx.client.app, dict(chapter), manga_name, base_role),
                    get_staff_mentions(chapter["manga_id"], "ED"),
                )
                if not mention_content:
                    mention_content = None
            else:
                next_msg = await notify_next_stage(ctx.client.app, dict(chapter), manga_name, base_role)
        except Exception as e:
            logger.error(f"Notification failed: {e}")
            next_msg = ""

        try:
            from bot.services.webhook_sender import send_webhook_embed
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=manga_name,
                event="done",
                guild_id=getattr(ctx, "guild_id", None),
                actor_name=ctx.user.username,
                actor_id=user_id,
                chapter_number=chapter["chapter_number"],
                role=user_role,
                new_status=new_status,
                earned=price_str,
                images_count=images_count if user_role in ("ED", "CO-ED") else None,
                extra_text=next_msg if next_msg else None,
                mention_content=mention_content if mention_content else None,
            )
        except Exception as e:
            logger.warning(f"Webhook embed failed for /done: {e}")

        msg = (
            f"✅ Chapter completed!\n"
            f"Added: **{price_str}** to your ledger{images_info}\n"
            f"New status: `{new_status}`"
        )
        if next_msg:
            msg += f"\n{next_msg}"
        await ctx.respond(components=[create_success(msg)], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    except ChapterAlreadyClaimed:
        await ctx.respond(
            components=[create_error("Chapter status was already updated, try again.")],
            flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
        )
    except LedgerInsertFailed:
        await ctx.respond(
            components=[create_error("You already claimed payment for this stage on this chapter.")],
            flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
        )


async def _qc_approve(ctx: lightbulb.Context, chapter_id: int):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", chapter_id)
            if not chapter:
                await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if chapter["status"] != "pending_review":
                await ctx.respond(components=[create_error(f"Chapter is not in review (current: `{chapter['status']}`).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            await conn.execute(
                "UPDATE chapters SET status = 'published', assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE id = $1",
                chapter_id,
            )
            await conn.execute(
                """INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)
                   VALUES ($1, 'REVIEW', $2, 'approved', 0)""",
                chapter_id, str(ctx.user.id),
            )
            mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
            manga_name = mrow["manga_name"] if mrow else "Manga"
    try:
        from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
        await send_webhook_embed(
            ctx.client.app,
            manga_id=chapter["manga_id"],
            manga_name=manga_name,
            event="approve",
            guild_id=ctx.guild_id,
            actor_name=ctx.user.username,
            actor_id=str(ctx.user.id),
            chapter_number=chapter["chapter_number"],
        )
        await update_manga_webhook(ctx.client.app, chapter["manga_id"])
    except Exception as e:
        logger.warning(f"Webhook embed failed for /qc approve: {e}")
    await ctx.respond(components=[create_success(f"✅ Approved chapter **#{chapter_id}** and published.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

async def _qc_reopen(ctx: lightbulb.Context, chapter_id: int):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", chapter_id)
            if not chapter:
                await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if chapter["status"] not in ("published", "rejected"):
                await ctx.respond(components=[create_error(f"Can only reopen published/rejected chapters (current: `{chapter['status']}`).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            await conn.execute(
                "UPDATE chapters SET status = 'pending_tl', assigned_to = NULL, assigned_at = NULL, images_count = NULL, updated_at = now() WHERE id = $1",
                chapter_id,
            )
            # Reopen restarts from scratch: void all unpaid ledger for this chapter
            # (paid rows stay for history). Prevents double-pay on redo.
            await conn.execute(
                "DELETE FROM transactions WHERE chapter_id = $1 AND status = 'unpaid'",
                chapter_id,
            )
            await conn.execute(
                """INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)
                   VALUES ($1, $2, $3, 'reopened', 0)""",
                chapter_id, chapter["status"], str(ctx.user.id),
            )
            mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
            manga_name = mrow["manga_name"] if mrow else "Manga"
    try:
        from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
        await send_webhook_embed(
            ctx.client.app,
            manga_id=chapter["manga_id"],
            manga_name=manga_name,
            event="reopen",
            guild_id=ctx.guild_id,
            actor_name=ctx.user.username,
            actor_id=str(ctx.user.id),
            chapter_number=chapter["chapter_number"],
            new_status="pending_tl",
        )
        await update_manga_webhook(ctx.client.app, chapter["manga_id"])
    except Exception as e:
        logger.warning(f"Webhook embed failed for /qc reopen: {e}")
    await ctx.respond(components=[create_success(f"🔄 Reopened chapter **#{chapter_id}** to `pending_tl`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

async def _qc_sync(ctx: lightbulb.Context, manga_name: str):
    await ctx.respond(components=[create_info("Performing deep scan of Google Drive folders... This may take a minute.", title="Scanning...")], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
    pool = get_pool()
    async with pool.acquire() as conn:
        m = await conn.fetchrow("SELECT id, drive_folder_id, manga_name, discord_channel_id FROM mangas WHERE manga_name = $1", manga_name)
        if not m:
            await ctx.interaction.edit_initial_response(components=[create_error("Manga not found.")])
            return
        manga_id = m["id"]
        drive_folder_id = m["drive_folder_id"]
        manga_name = m["manga_name"]
    try:
        from bot.core import get_drive_service
        drive_service = get_drive_service()
        progress = await drive_service.deep_sync_manga(drive_folder_id)
    except Exception as e:
        logger.error(f"Deep sync failed for {manga_name}: {e}")
        await ctx.interaction.edit_initial_response(components=[create_error(f"Failed to scan Drive: {e}")])
        return
    if not progress:
        await ctx.interaction.edit_initial_response(components=[create_error("No chapter folders found in Drive for this manga.")])
        return
    updated_count = 0
    new_count = 0
    skipped_protected = 0
    counts = {'published': 0, 'pending_review': 0, 'pending_ed': 0, 'pending_pr': 0, 'pending_tl': 0}
    # Drive inference never returns pending_review/rejected — never regress those
    # (or published) from Drive presence alone. Assigned chapters keep their owner.
    PROTECTED_STATUSES = ('published', 'pending_review', 'rejected')
    async with pool.acquire() as conn:
        for ch_num, data in progress.items():
            status = data['status']
            ch_folder_id = data['id']
            counts[status] = counts.get(status, 0) + 1
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT id, status FROM chapters WHERE manga_id = $1 AND chapter_number = $2 FOR UPDATE", manga_id, ch_num)
                if existing:
                    if existing["status"] in PROTECTED_STATUSES:
                        skipped_protected += 1
                        continue
                    if existing["status"] != status:
                        await conn.execute("UPDATE chapters SET status = $1, drive_folder_id = $2, updated_at = now() WHERE id = $3", status, ch_folder_id, existing["id"])
                        updated_count += 1
                        await conn.execute(
                            "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, $2, $3, 'synced', 0)",
                            existing["id"], status, str(ctx.user.id),
                        )
                else:
                    await conn.execute("INSERT INTO chapters (manga_id, chapter_number, drive_folder_id, status) VALUES ($1, $2, $3, $4) ON CONFLICT (manga_id, chapter_number) DO NOTHING", manga_id, ch_num, ch_folder_id, status)
                    new_count += 1
    total_raw = sum(counts.values())
    total_tl = counts.get('published', 0) + counts.get('pending_review', 0) + counts.get('pending_ed', 0) + counts.get('pending_pr', 0)
    total_ed = counts.get('published', 0) + counts.get('pending_review', 0)
    pending_tl = counts.get('pending_tl', 0)
    pending_ed_pr = counts.get('pending_ed', 0) + counts.get('pending_pr', 0)
    from hikari.impl.special_endpoints import ContainerComponentBuilder
    container = ContainerComponentBuilder(id=1, accent_color=0x3498DB)
    container.add_text_display(
        f"## 📊 Progress Report: {manga_name}\n"
        f"Deep scan completed successfully.\n"
        f"✅ **{new_count}** new chapters registered.\n"
        f"🔄 **{updated_count}** chapter statuses updated."
        + (f"\n🛡️ **{skipped_protected}** protected (published/review/rejected) left untouched." if skipped_protected else "")
    )
    container.add_separator(divider=True)
    container.add_text_display(
        f"**📁 Total Chapters in RAW:** `{total_raw}`\n"
        f"**📝 Total Translated:** `{total_tl}`\n"
        f"**✅ Total Finalized (ED):** `{total_ed}`"
    )
    bottleneck = []
    if pending_ed_pr > 0:
        bottleneck.append(f"• **{pending_ed_pr}** chapters translated but pending Editing/PR.")
    if pending_tl > 0:
        bottleneck.append(f"• **{pending_tl}** RAW chapters pending Translation.")
    if not bottleneck:
        bottleneck.append("🎉 All caught up! No pending chapters.")
    container.add_separator(divider=True)
    container.add_text_display("### ⚠️ Pending Work\n" + "\n".join(bottleneck))
    try:
        from bot.services.webhook_sender import update_manga_webhook
        await update_manga_webhook(ctx.client.app, manga_id)
    except Exception as e:
        logger.error(f"Failed to update original webhook for {manga_name}: {e}")
    await ctx.interaction.edit_initial_response(components=[container])


