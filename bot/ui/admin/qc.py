from .core import *

class QCReviewView(miru.View):
    def __init__(self, chapters, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.chapters = chapters
        
        opts = []
        for ch in chapters:
            opts.append(miru.SelectOption(label=f"{ch['manga_name']} - Ch {ch['chapter_number']}", value=str(ch['id'])))
            
        sel = miru.TextSelect(options=opts, placeholder="Select a chapter to review...", row=0)
        sel.callback = self.select_chapter
        self.add_item(sel)
        
        btn_back = miru.Button(label="Back to Dashboard", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=1)
        btn_back.callback = self.btn_back
        self.add_item(btn_back)

    async def select_chapter(self, ctx: miru.ViewContext, select: miru.TextSelect):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        ch_id = int(select.values[0])
        ch = next((c for c in self.chapters if c['id'] == ch_id), None)
        if not ch:
            return
            
        view = QCActionView(ch_id, ch['manga_name'], ch['chapter_number'])
        c = create_info(f"**Manga:** {ch['manga_name']}\n**Chapter:** {ch['chapter_number']}\n\nWhat would you like to do?", title="QC Action")
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class QCActionView(miru.View):
    def __init__(self, chapter_id: int, manga_name: str, chapter_number: int, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.chapter_id = chapter_id
        self.manga_name = manga_name
        self.chapter_number = chapter_number

    @miru.button(label="Approve & Publish", emoji="✅", style=hikari.ButtonStyle.SUCCESS)
    async def btn_approve(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", self.chapter_id)
                if not chapter or chapter["status"] != "pending_review":
                    await ctx.edit_response(components=[create_error("Chapter is no longer pending review.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                    
                await conn.execute(
                    "UPDATE chapters SET status = 'published', assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE id = $1",
                    self.chapter_id,
                )
                await conn.execute(
                    "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, 'REVIEW', $2, 'approved', 0)",
                    self.chapter_id, str(ctx.user.id),
                )
                
        try:
            from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=self.manga_name,
                event="approve",
                guild_id=ctx.guild_id,
                actor_name=ctx.user.username,
                actor_id=str(ctx.user.id),
                chapter_number=self.chapter_number,
            )
            await update_manga_webhook(ctx.client.app, chapter["manga_id"])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        # Back to Admin Dashboard
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()
        
        await ctx.respond(components=[create_success(f"Approved and published Chapter {self.chapter_number}!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Reject (Send Back)", emoji="❌", style=hikari.ButtonStyle.DANGER)
    async def btn_reject(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from bot.ui.admin import RejectReasonModal
        modal = RejectReasonModal(self.chapter_id, self.manga_name)
        await ctx.respond_with_modal(modal)
        
    @miru.button(label="Back", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            chapters = await conn.fetch("SELECT c.id, c.chapter_number, m.manga_name FROM chapters c JOIN mangas m ON c.manga_id = m.id WHERE c.status = 'pending_review' ORDER BY c.updated_at ASC LIMIT 25")
            
        view = QCReviewView(chapters)
        c = create_info(f"You have `{len(chapters)}` chapter(s) pending final review.\nSelect a chapter below to Approve or Reject it.", title="QC Review (Quality Control)")
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class RejectReasonModal(miru.Modal):
    def __init__(self, chapter_id: int, manga_name: str, *args, **kwargs):
        super().__init__(title="Rejection Reason", *args, **kwargs)
        self.chapter_id = chapter_id
        self.manga_name = manga_name
        self.reason = miru.TextInput(label="Reason", placeholder="e.g. Bad translation on page 5...", style=hikari.TextInputStyle.PARAGRAPH, required=True)
        self.add_item(self.reason)

    async def callback(self, ctx: miru.ModalContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
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
                chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", chapter_id)
                if not chapter:
                    await ctx.edit_response(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                
                new_status = revert_map.get(chapter["status"])
                if not new_status:
                    await ctx.edit_response(components=[create_error(f"Cannot reject chapter in current status (`{chapter['status']}`).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                    
                if new_status == "pending_pr":
                     pr_exists = await conn.fetchval("SELECT 1 FROM manga_staff WHERE manga_id = $1 AND role IN ('PR', 'CO-PR')", chapter["manga_id"])
                     if not pr_exists:
                         new_status = "pending_tl"

                await conn.execute("UPDATE chapters SET status = $1, assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE id = $2", new_status, chapter_id)
                # Void unpaid ledger for the rejected stage only (paid rows stay).
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
                await conn.execute("INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents, reason) VALUES ($1, $2, $3, 'rejected', 0, $4)", chapter_id, chapter["status"], str(ctx.user.id), reason_text)
                
                last_worker = await conn.fetchval("SELECT user_id FROM chapter_history WHERE chapter_id = $1 AND action = 'done' ORDER BY timestamp DESC LIMIT 1", chapter_id)

        try:
            from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
            mention_text = f"<@{last_worker}> please check the notes." if last_worker else None
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=self.manga_name,
                event="reject",
                guild_id=ctx.guild_id,
                actor_name=ctx.user.username,
                actor_id=str(ctx.user.id),
                chapter_number=chapter["chapter_number"],
                new_status=new_status,
                reason=reason_text,
                mention_content=mention_text,
            )
            await update_manga_webhook(ctx.client.app, chapter["manga_id"])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        if last_worker:
            try:
                from bot.services.notifications import send_dm_if_possible
                from hikari.impl.special_endpoints import ContainerComponentBuilder
                container = ContainerComponentBuilder(id=1, accent_color=0xE74C3C)
                container.add_text_display("## Your chapter was rejected\nPlease fix the issues and submit again.")
                container.add_separator(divider=True)
                container.add_text_display(f"**Manga:** {self.manga_name}\n**Chapter:** {chapter['chapter_number']}\n\n**💬 Reason:** {reason_text[:1000]}")
                container.add_separator(divider=True)
                container.add_text_display(f"Rejected by {ctx.user.username}")
                await send_dm_if_possible(ctx.client.app, last_worker, components=[container], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        await ctx.respond(components=[create_success(f"Rejected chapter and reverted to `{new_status}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

