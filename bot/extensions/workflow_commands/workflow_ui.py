import math
import logging
import hikari
import miru
from bot.services.ui import create_error, create_warning, create_info, create_success
from bot.extensions.workflow_commands.workflow_state import STATUS_ROLE_MAP
from bot.extensions.workflow_commands.workflow_db import _finalize_done
from bot.database.connection import get_pool

logger = logging.getLogger(__name__)

class EDWarningConfirmView(miru.View):
    """Confirmation view shown when ED images have validation warnings."""

    def __init__(self, chapter_id: int, images_count: int, warnings: list[str], user_role: str, user_id: str):
        super().__init__(timeout=120.0)
        self.chapter_id = chapter_id
        self.images_count = images_count
        self.warnings = warnings
        self.user_role = user_role
        self.user_id = user_id

    @miru.button(label="✅ Confirm — Submit Anyway", style=hikari.ButtonStyle.DANGER)
    async def confirm(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error("This confirmation is not yours.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await _finalize_done(ctx, self.chapter_id, self.user_role, self.user_id, self.images_count, self.warnings)
        self.stop()

    @miru.button(label="❌ Cancel — Fix Images", style=hikari.ButtonStyle.SECONDARY)
    async def cancel(self, ctx: miru.ViewContext, button: miru.Button) -> None:
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error("This confirmation is not yours.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.respond(
            components=[create_info("Cancelled — please fix the images in Drive and try `/done` again.", title="Cancelled")],
            flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
        )
        self.stop()


# Select menu with pagination

# Select menu with pagination

class ChapterSelectView(miru.View):
    """Chapter selection with pagination (>25 chapters)."""

    def __init__(self, chapters: list, user_role: str, user_id: str, page: int = 0, mode: str = "done"):
        super().__init__(timeout=120.0)
        self.chapters = chapters
        self.user_role = user_role
        self.user_id = user_id
        self.page = page
        self.mode = mode
        self.per_page = 25
        self.max_pages = max(1, math.ceil(len(chapters) / self.per_page))
        self._build_components()

    def _build_components(self) -> None:
        self.clear_items()
        start_idx = self.page * self.per_page
        end_idx = start_idx + self.per_page
        current_chapters = self.chapters[start_idx:end_idx]
        options = []
        for chap in current_chapters:
            label = f"{chap['manga_name']} — Chapter {chap['chapter_number']}"
            label = label[:100]
            options.append(
                miru.SelectOption(
                    label=label,
                    value=str(chap["id"]),
                    description=f"Status: {chap['status']}",
                )
            )
        placeholder = "Select the chapter to claim..." if self.mode == "claim" else "Select the chapter you finished..."
        select = miru.TextSelect(options=options, placeholder=placeholder)
        select.callback = self._on_select
        self.add_item(select)
        if self.max_pages > 1:
            prev_btn = miru.Button(
                label="Previous", style=hikari.ButtonStyle.SECONDARY,
                disabled=(self.page == 0),
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)
            page_label = miru.Button(
                label=f"{self.page + 1}/{self.max_pages}",
                style=hikari.ButtonStyle.SECONDARY, disabled=True,
            )
            self.add_item(page_label)
            next_btn = miru.Button(
                label="Next", style=hikari.ButtonStyle.SECONDARY,
                disabled=(self.page >= self.max_pages - 1),
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

    async def _on_prev(self, ctx: miru.ViewContext) -> None:
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error("This menu is not yours.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.page = max(0, self.page - 1)
        self._build_components()
        await ctx.edit_response(components=self)

    async def _on_next(self, ctx: miru.ViewContext) -> None:
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error("This menu is not yours.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.page = min(self.max_pages - 1, self.page + 1)
        self._build_components()
        await ctx.edit_response(components=self)

    def get_container(self, text: str):
        """Helper to wrap view in a Components V2 container for /done responses."""
        from hikari.impl.special_endpoints import ContainerComponentBuilder
        c = ContainerComponentBuilder(id=1, accent_color=0x3498DB)
        c.add_text_display(text)
        for row in self.build():
            c.add_action_row(row.components)
        return c

    async def _on_select(self, ctx: miru.ViewContext) -> None:
        """Handle chapter claim or completion — atomic transaction (Drive I/O outside tx)."""
        cmd_name = "/claim" if self.mode == "claim" else "/done"
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error(f"This menu is not yours. Run `{cmd_name}` yourself.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        cd_key = "claim_submit" if self.mode == "claim" else "done_submit"
        on_cd, rem = check_cooldown(ctx.user.id, cd_key, 5)
        if on_cd:
            await ctx.respond(components=[create_error(f"Slow down! Try again in {rem}s.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        chapter_id = int(ctx.interaction.values[0])
        pool = get_pool()
        expected_status = STATUS_ROLE_MAP[self.user_role]

        if self.mode == "claim":
            async with pool.acquire() as conn:
                async with conn.transaction():
                    chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", chapter_id)
                    if not chapter:
                        await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    if chapter["status"] != expected_status:
                        await ctx.respond(components=[create_error(f"This chapter is in `{chapter['status']}`, not `{expected_status}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    if chapter["assigned_to"]:
                        if chapter["assigned_to"] == self.user_id:
                            await ctx.respond(components=[create_error("You already claimed this chapter.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        else:
                            await ctx.respond(components=[create_error(f"Already claimed by <@{chapter['assigned_to']}>")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    claimed = await conn.fetchrow(
                        """
                        UPDATE chapters
                        SET assigned_to = $1, assigned_at = now(), updated_at = now()
                        WHERE id = $2 AND assigned_to IS NULL AND status = $3
                        RETURNING status
                        """,
                        self.user_id, chapter_id, chapter["status"]
                    )
                    if not claimed:
                        await ctx.respond(components=[create_error("Someone claimed or changed it just now. Try another.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, $2, $3, 'claimed', 0)",
                        chapter_id, claimed["status"], self.user_id,
                    )
                mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
                manga_name = mrow["manga_name"] if mrow else "Manga"
            try:
                from bot.services.webhook_sender import send_webhook_embed
                await send_webhook_embed(
                    ctx.client.app,
                    manga_id=chapter["manga_id"],
                    manga_name=manga_name,
                    event="claim",
                    guild_id=getattr(ctx, "guild_id", None),
                    actor_name=ctx.user.username,
                    actor_id=self.user_id,
                    chapter_number=chapter["chapter_number"],
                    role=self.user_role,
                    new_status=chapter["status"],
                )
            except Exception as e:
                logger.warning(f"Webhook embed failed for claim: {e}")
            await ctx.respond(components=[create_success(f"🔒 Claimed chapter **#{chapter['chapter_number']}** (`{chapter['status']}`) for you! Use `/done` or Staff Dashboard when finished.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            self.stop()
            return

        # Pre-fetch + Drive count outside transaction to avoid holding connection during I/O
        images_count = 0
        warnings_for_confirm: list[str] = []
        raw_count = 0
        ed_count = 0
        if self.user_role in ("ED", "CO-ED"):
            # Single DB roundtrip: join chapters + mangas
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT c.manga_id, c.status, c.chapter_number, m.drive_folder_id, m.final_subfolder_name "
                    "FROM chapters c JOIN mangas m ON m.id = c.manga_id WHERE c.id = $1",
                    chapter_id,
                )
                if not row:
                    await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                if row["status"] != expected_status:
                    await ctx.respond(components=[create_error("This chapter is not in your stage or was already updated.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                ch_info = {"chapter_number": row["chapter_number"], "manga_id": row["manga_id"]}
                manga_info = {"drive_folder_id": row["drive_folder_id"]}
                if not manga_info["drive_folder_id"]:
                    await ctx.respond(components=[create_error("No drive folder configured for this manga.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
            try:
                drive_service = get_drive_service()
                raw_count, ed_count, ed_ch_id, ed_warnings = await drive_service.compare_chapter_images(manga_info["drive_folder_id"], ch_info["chapter_number"])
                
                if not ed_ch_id:
                    await ctx.respond(components=[create_error("No chapter folder found in ED. Please create a folder and upload the images.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                
                if ed_count == 0:
                    await ctx.respond(components=[create_error("No valid images found in the ED folder.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                    
                if raw_count > 0 and ed_count < raw_count:
                    await ctx.respond(components=[create_error(f"Missing images! RAW folder has {raw_count} images, but your ED folder only has {ed_count} images.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                
                images_count = ed_count
                warnings_for_confirm: list[str] = ed_warnings or []
                
            except Exception as e:
                logger.error(f"Drive count failed for chapter {chapter_id}: {e}")
                await ctx.respond(components=[create_error("Failed to read image count from Drive. Try again later.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

        # If ED has image validation warnings (duplicates, zero-byte, missing sequence), require confirmation
        if self.user_role in ("ED", "CO-ED") and warnings_for_confirm:
            warn_text = "; ".join(warnings_for_confirm[:5])
            if len(warnings_for_confirm) > 5:
                warn_text += f" (+{len(warnings_for_confirm)-5} more)"
            container = create_warning(
                f"Found **{len(warnings_for_confirm)}** warning(s) in ED folder:\n`{warn_text}`\n\n"
                f"RAW: `{raw_count}` images | ED: `{ed_count}` images\n\n"
                f"**Are you sure you want to submit despite these warnings?**\n"
                f"Click **Confirm** to submit at your own risk, or **Cancel** to fix the images first.",
                title="Image Validation Warnings",
            )
            view = EDWarningConfirmView(chapter_id, images_count, warnings_for_confirm, self.user_role, self.user_id)
            # Wrap warning container + view buttons into Components V2
            from bot.ui.admin import embed_view_in_container  # reuse helper
            try:
                await ctx.respond(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                logger.error(f"Failed to embed warning view: {e}")
                # Fallback: send warning text + view without embed helper
                await ctx.respond(components=[container], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
                from bot.core import get_miru_client
                get_miru_client().start_view(view)
                self.stop()
                return
            from bot.core import get_miru_client
            get_miru_client().start_view(view)
            self.stop()
            return

        # No warnings (or not ED) — proceed directly
        await _finalize_done(ctx, chapter_id, self.user_role, self.user_id, images_count, warnings_for_confirm if self.user_role in ("ED", "CO-ED") else [])
        self.stop()

class RoleSelectView(miru.View):
    """If user has multiple roles, let them pick which role to complete or claim."""
    def __init__(self, roles: list[str], user_id: str, chapters_by_role: dict, mode: str = "done"):
        super().__init__(timeout=60.0)
        self.roles = roles
        self.user_id = user_id
        self.chapters_by_role = chapters_by_role
        self.mode = mode

    @miru.text_select(
        placeholder="Choose role...",
        options=[
            miru.SelectOption(label="TL — Translator", value="TL"),
            miru.SelectOption(label="PR — Proofreader", value="PR"),
            miru.SelectOption(label="ED — Editor", value="ED"),
        ],
    )
    async def select_role(self, ctx: miru.ViewContext) -> None:
        cmd_name = "/claim" if self.mode == "claim" else "/done"
        if str(ctx.user.id) != str(self.user_id):
            await ctx.respond(components=[create_error(f"This menu is not yours. Run `{cmd_name}` yourself.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        role = ctx.interaction.values[0]
        if role not in self.roles:
            await ctx.respond(components=[create_error("You don't have that role.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        chapters = self.chapters_by_role.get(role, [])
        if not chapters:
            await ctx.respond(components=[create_error(f"No chapters for {role}.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = ChapterSelectView(chapters=chapters, user_role=role, user_id=self.user_id, mode=self.mode)
        action_verb = "want to claim" if self.mode == "claim" else "just finished"
        txt = f"### 📝 Chapter Selection\n**Role:** `{role}`\n**Pending Chapters:** `{len(chapters)}`\n\nPlease select the chapter you {action_verb}:"
        await ctx.edit_response(components=[view.get_container(txt)], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        from bot.core import get_miru_client as _gmc
        _gmc().start_view(view)
        self.stop()

class RejectReasonModal(miru.Modal, title="Rejection Reason"):
    reason = miru.TextInput(label="Reason", placeholder="e.g. Bad translation on page 5...", style=hikari.TextInputStyle.PARAGRAPH, required=True)

    def __init__(self, chapter_id: int):
        super().__init__()
        self.chapter_id = chapter_id

    async def callback(self, ctx: miru.ModalContext) -> None:
        from bot.services.permissions import is_admin
        if not is_admin(ctx.member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        
        chapter_id = self.chapter_id
        reason_text = self.reason.value
        pool = get_pool()
        revert_map = {
            "pending_review": "pending_ed",
            "pending_ed": "pending_pr",
            "pending_pr": "pending_tl",
        }
        async with pool.acquire() as conn:
            async with conn.transaction():
                chapter = await conn.fetchrow(
                    "SELECT * FROM chapters WHERE id = $1 FOR UPDATE", chapter_id
                )
                if not chapter:
                    await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                new_status = revert_map.get(chapter["status"])
                if not new_status:
                    await ctx.respond(
                        components=[create_error(f"Cannot reject chapter in current status (`{chapter['status']}`).")],
                        flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
                    )
                    return
                if new_status == "pending_pr":
                     pr_exists = await conn.fetchval(
                         "SELECT 1 FROM manga_staff WHERE manga_id = $1 AND role IN ('PR', 'CO-PR')",
                         chapter["manga_id"],
                     )
                     if not pr_exists:
                         new_status = "pending_tl"
                await conn.execute(
                    "UPDATE chapters SET status = $1, assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE id = $2",
                    new_status, chapter_id,
                )
                # Void unpaid ledger for the rejected stage only (paid rows stay).
                # Prevents double-pay on redo and lets the same worker re-earn.
                _rejected_stage_roles = {
                    "pending_review": ("ED", "CO-ED"),
                    "pending_ed": ("PR", "CO-PR"),
                    "pending_pr": ("TL", "CO-TL"),
                }.get(chapter["status"])
                if _rejected_stage_roles:
                    await conn.execute(
                        "DELETE FROM transactions WHERE chapter_id = $1 AND status = 'unpaid' AND role = ANY($2::text[])",
                        chapter_id, list(_rejected_stage_roles),
                    )
                await conn.execute(
                    """INSERT INTO chapter_history
                           (chapter_id, stage, user_id, action, amount_cents, reason)
                       VALUES ($1, $2, $3, 'rejected', 0, $4)""",
                    chapter_id, chapter["status"], str(ctx.user.id), reason_text
                )
                last_worker = await conn.fetchval(
                    "SELECT user_id FROM chapter_history WHERE chapter_id = $1 AND action = 'done' ORDER BY timestamp DESC LIMIT 1",
                    chapter_id
                )
                # Fetch manga name for DM
                mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"]) if chapter else None
                manga_name = mrow["manga_name"] if mrow else "Manga"
        msg = f"Rejected chapter #{chapter_id}, reverted to `{new_status}`.\n**Reason:** {reason_text}"
        if last_worker:
            msg += f"\n<@{last_worker}> please check the notes."

        # Send webhook embed to manga channel
        try:
            from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
            mention_text = f"<@{last_worker}> please check the notes." if last_worker else None
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=manga_name,
                event="reject",
                guild_id=getattr(ctx, "guild_id", None),
                actor_name=ctx.user.username,
                actor_id=str(ctx.user.id),
                chapter_number=chapter["chapter_number"],
                new_status=new_status,
                reason=reason_text,
                mention_content=mention_text,
            )
            await update_manga_webhook(ctx.client.app, chapter["manga_id"])
        except Exception as e:
            logger.warning(f"Webhook embed failed for /reject: {e}")

        await ctx.respond(components=[create_success(msg)], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        # DM notification via helper
        if last_worker:
            try:
                from bot.services.notifications import send_dm_if_possible
                from hikari.impl.special_endpoints import ContainerComponentBuilder
                container = ContainerComponentBuilder(id=1, accent_color=0xE74C3C)
                container.add_text_display("## Your chapter was rejected\nPlease fix the issues and run `/done` again.")
                container.add_separator(divider=True)
                container.add_text_display(f"**Manga:** {manga_name}\n**Chapter ID:** {chapter_id}\n\n**💬 Reason:** {reason_text[:1000]}")
                container.add_separator(divider=True)
                container.add_text_display(f"Rejected by {ctx.user.username}")
                await send_dm_if_possible(ctx.client.app, last_worker, components=[container], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)


