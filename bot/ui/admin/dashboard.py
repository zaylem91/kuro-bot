from .core import *

class AdminDashboard(miru.View):
    def __init__(self, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)

    @miru.button(label="Manage Projects", emoji=EMOJI["book"], style=hikari.ButtonStyle.PRIMARY, custom_id="admin_mangas", row=0)
    async def btn_mangas(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        options = await fetch_manga_options()
        view = MangaMenuView(options)
        c = build_manga_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Manage Staff", emoji=EMOJI["staff"], style=hikari.ButtonStyle.SUCCESS, custom_id="admin_staff", row=0)
    async def btn_staff(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = StaffMenuView()
        c = build_staff_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Finance & Payouts", emoji=EMOJI["moneybag"], style=hikari.ButtonStyle.SECONDARY, custom_id="admin_finance", row=0)
    async def btn_finance(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        failed_dispatches = await get_failed_invoice_dispatches(get_pool())
        view = FinanceMenuView(failed_dispatches)
        c = build_finance_dashboard_container(ctx.client.app, failed_dispatches)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Workload", emoji="📊", style=hikari.ButtonStyle.SECONDARY, custom_id="admin_workload", row=1)
    async def btn_workload(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        view, container = await build_workload_view()
        await ctx.edit_response(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="QC Review", emoji="🔎", style=hikari.ButtonStyle.DANGER, custom_id="admin_qc", row=1)
    async def btn_qc(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            chapters = await conn.fetch("SELECT c.id, c.chapter_number, m.manga_name FROM chapters c JOIN mangas m ON c.manga_id = m.id WHERE c.status = 'pending_review' ORDER BY c.updated_at ASC LIMIT 25")
            
        if not chapters:
            await ctx.respond(components=[create_success("All caught up! No chapters pending review.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        view = QCReviewView(chapters)
        c = create_info(f"You have `{len(chapters)}` chapter(s) pending final review.\nSelect a chapter below to Approve or Reject it.", title="QC Review (Quality Control)")
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Help", emoji="❓", style=hikari.ButtonStyle.SECONDARY, custom_id="admin_help", row=1)
    async def btn_help(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        help_text = (
            "## ❓ Admin Dashboard Help\n\n"
            f"{EMOJI['book']} **Manage Projects:** Add new manga, edit details, toggle status (ongoing→paused→dropped), delete, and assign or remove staff per manga. Paused/dropped mangas move to their own category and staff are removed automatically.\n\n"
            f"{EMOJI['staff']} **Manage Staff:** Browse registered members, view their audit history and manage roles.\n\n"
            f"📊 **Workload:** See every member's active mangas (TL/PR/ED breakdown) + claimed chapters, sorted by load (High/Medium/Low/Idle). Select a member for detailed list.\n\n"
            f"{EMOJI['moneybag']} **Finance & Payouts:** View unpaid balances, settle payouts and adjust global pricing.\n\n"
            "🔎 **QC Review:** Review chapters in `pending_review`. Select a chapter to Approve (publish) or Reject (send back with reason).\n\n"
            "💡 **Tip:** Use `/register` for new members, `/workload` for quick load check, and `/qc` for QC actions."
        )
        await ctx.respond(components=[create_info(help_text, title="Help")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


