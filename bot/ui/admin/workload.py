from .core import *

class WorkloadDetailView(miru.View):
    def __init__(self, discord_id: str, display_name: str, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.discord_id = discord_id
        self.display_name = display_name

    @miru.button(label="Back to Workload", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view, container = await build_workload_view(page=0)
        await ctx.edit_response(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Back to Dashboard", emoji="🏠", style=hikari.ButtonStyle.PRIMARY)
    async def btn_home(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class WorkloadUserSelect(miru.UserSelect):
    def __init__(self, *args, **kwargs):
        super().__init__(placeholder="Select a member to see their mangas...", *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user = self.values[0]
        discord_id = str(user.id)
        display = getattr(user, "display_name", None) or getattr(user, "username", discord_id)
        pool = get_pool()
        async with pool.acquire() as conn:
            # Verify registered
            is_reg = await conn.fetchval("SELECT is_registered FROM users WHERE discord_id=$1", discord_id)
            if not is_reg:
                await ctx.respond(components=[create_error(f"<@{discord_id}> is not registered.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Mangas per role (only ongoing)
            mangas = await conn.fetch(
                """
                SELECT m.manga_name, m.status, ms.role
                FROM manga_staff ms
                JOIN mangas m ON m.id = ms.manga_id
                WHERE ms.discord_id = $1
                ORDER BY 
                    CASE m.status WHEN 'ongoing' THEN 1 WHEN 'paused' THEN 2 WHEN 'dropped' THEN 3 ELSE 4 END,
                    m.manga_name
                """,
                discord_id,
            )
            # Claimed chapters currently assigned
            claimed = await conn.fetch(
                """
                SELECT c.id, c.chapter_number, c.status, m.manga_name
                FROM chapters c
                JOIN mangas m ON m.id = c.manga_id
                WHERE c.assigned_to = $1 AND c.status NOT IN ('published','rejected')
                ORDER BY m.manga_name, c.chapter_number
                LIMIT 25
                """,
                discord_id,
            )
        # Build detail container
        if not mangas and not claimed:
            desc = f"<@{discord_id}> has no mangas assigned and no claimed chapters.\n\n*Idle — available for new assignments.*"
        else:
            parts = []
            if mangas:
                # Group by role
                grouped: dict[str, list[str]] = {}
                for r in mangas:
                    grouped.setdefault(r["role"], []).append(f"{r['manga_name']} (`{r['status']}`)")
                lines = [f"**Total mangas:** `{len(mangas)}`"]
                for role in ["TL","CO-TL","PR","CO-PR","ED","CO-ED"]:
                    if role in grouped:
                        lines.append(f"**{role}:** {', '.join(grouped[role])}")
                # Any other roles
                for role, lst in grouped.items():
                    if role not in ["TL","CO-TL","PR","CO-PR","ED","CO-ED"]:
                        lines.append(f"**{role}:** {', '.join(lst)}")
                parts.append("\n".join(lines))
            else:
                parts.append("*No mangas assigned.*")
            if claimed:
                clines = [f"**Claimed chapters ({len(claimed)}):**"]
                for ch in claimed:
                    clines.append(f"• #{ch['id']} — **{ch['manga_name']}** Ch.{ch['chapter_number']} (`{ch['status']}`)")
                parts.append("\n".join(clines))
            else:
                parts.append("*No chapters currently claimed.*")
            desc = "\n\n".join(parts)
            if len(desc) > 3500:
                desc = desc[:3500] + "\n*... truncated*"
        # Determine load badge
        total = len(mangas)
        if total >= 5:
            badge = "🔴 High load"
        elif total >= 3:
            badge = "🟡 Medium load"
        elif total >= 1:
            badge = "🟢 Low load"
        else:
            badge = "⚪ Idle"
        title = f"Workload — {display} — {badge}"
        c = create_info(desc, title=title)
        view = WorkloadDetailView(discord_id, display)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.view.stop()


class WorkloadView(miru.View):
    def __init__(self, rows, page=0, max_page=1, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.rows = rows
        self.page = page
        self.max_page = max_page
        self.add_item(WorkloadUserSelect(row=2))
        if max_page > 1:
            prev = miru.Button(label="◀ Prev", style=hikari.ButtonStyle.SECONDARY, disabled=(page==0), row=3)
            prev.callback = self.on_prev
            self.add_item(prev)
            nxt = miru.Button(label="Next ▶", style=hikari.ButtonStyle.SECONDARY, disabled=(page>=max_page-1), row=3)
            nxt.callback = self.on_next
            self.add_item(nxt)
        clean = miru.Button(label="Clean Left Members", emoji="🧹", style=hikari.ButtonStyle.DANGER, row=4)
        clean.callback = self.on_clean
        self.add_item(clean)
        back = miru.Button(label="Back to Dashboard", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
        back.callback = self.on_back
        self.add_item(back)

    async def on_prev(self, ctx: miru.ViewContext, *args):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.page = max(0, self.page-1)
        # Fresh rows + container together so content and buttons never disagree.
        view, container = await build_workload_view(page=self.page)
        await ctx.edit_response(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    async def on_next(self, ctx: miru.ViewContext, *args):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.page = min(self.max_page-1, self.page+1)
        view, container = await build_workload_view(page=self.page)
        await ctx.edit_response(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    async def on_clean(self, ctx: miru.ViewContext, *args):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        guild_id = getattr(ctx, "guild_id", None)
        if not guild_id:
            try:
                from bot.config import GUILD_ID as _gid
                guild_id = _gid
            except Exception:
                guild_id = None
        if not guild_id:
            await ctx.respond(components=[create_error("Guild ID not configured.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT discord_id FROM users WHERE is_registered = TRUE")
        ids = [r["discord_id"] for r in rows]
        left_ids: list[str] = []
        # Check membership via cache first (fast), then REST only for misses
        for did in ids:
            try:
                # Try cache
                member = ctx.client.app.cache.get_member(int(guild_id), int(did))  # type: ignore
                if member is not None:
                    continue
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            # Fallback to REST (rate-limited, do sequentially with small delay)
            try:
                await ctx.client.app.rest.fetch_member(int(guild_id), int(did))
            except hikari.NotFoundError:
                left_ids.append(did)
            except hikari.ForbiddenError:
                # No permission to check — skip
                continue
            except Exception:
                continue
            # Small delay to avoid 429
            await asyncio.sleep(0.2)
        if not left_ids:
            await ctx.respond(components=[create_success("No left members found — all registered users are still in the server.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        # Collect Drive + channel info for revoke before DB delete (like events.py)
        drive_revoke_map: dict[str, list[tuple[str, str]]] = {}  # discord_id -> list of (email, folder_id)
        channel_revoke_map: dict[str, list[str]] = {}  # discord_id -> list of channel_id
        audit_rows: list = []
        affected_mids: list[int] = []
        try:
            async with pool.acquire() as conn:
                rows_emails = await conn.fetch("SELECT discord_id, drive_email FROM users WHERE discord_id = ANY($1::text[]) AND drive_email IS NOT NULL", left_ids)
                email_map = {r["discord_id"]: r["drive_email"] for r in rows_emails}
                staff_rows = await conn.fetch("SELECT manga_id, discord_id, role FROM manga_staff WHERE discord_id = ANY($1::text[])", left_ids)
                audit_rows = list(staff_rows)
                if staff_rows:
                    mids = list({r["manga_id"] for r in staff_rows})
                    affected_mids = mids
                    drows = await conn.fetch("SELECT id, drive_folder_id, discord_channel_id FROM mangas WHERE id = ANY($1::int[])", mids)
                    fid_map = {r["id"]: r["drive_folder_id"] for r in drows}
                    chan_map = {r["id"]: r["discord_channel_id"] for r in drows}
                    for r in staff_rows:
                        email = email_map.get(r["discord_id"])
                        fid = fid_map.get(r["manga_id"])
                        if email and fid:
                            drive_revoke_map.setdefault(r["discord_id"], []).append((email, fid))
                        cid = chan_map.get(r["manga_id"])
                        if cid:
                            lst = channel_revoke_map.setdefault(r["discord_id"], [])
                            if cid not in lst:
                                lst.append(cid)
        except Exception as e:
            logger.warning(f"Failed to collect Drive revoke map for clean: {e}")
        # Mark as unregistered and remove from all mangas
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET is_registered = FALSE WHERE discord_id = ANY($1::text[])", left_ids)
                await conn.execute("DELETE FROM manga_staff WHERE discord_id = ANY($1::text[])", left_ids)
                await conn.execute("UPDATE chapters SET assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE assigned_to = ANY($1::text[])", left_ids)
                # Audit each auto-removal (same trail as member-leave / pause-drop)
                if audit_rows:
                    changed_by = str(ctx.user.id)
                    for sr in audit_rows:
                        try:
                            await conn.execute(
                                "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1,$2,$3,'auto_removed',$4)",
                                sr["manga_id"], sr["discord_id"], sr["role"], changed_by,
                            )
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        # Revoke Drive access outside transaction
        if drive_revoke_map:
            try:
                from bot.core import get_drive_service
                ds = get_drive_service()
                for did, pairs in drive_revoke_map.items():
                    for email, fid in pairs:
                        try:
                            await ds.revoke_folder_access(fid, email)
                        except Exception as e:
                            logger.warning(f"Drive revoke failed for {email} on {fid} (clean): {e}")
            except Exception as e:
                logger.warning(f"Drive revoke batch failed in clean: {e}")
        # Revoke Discord room access (only rooms of mangas they were on)
        if channel_revoke_map:
            try:
                from bot.services.manga_access import revoke_channel_access
                for did, cids in channel_revoke_map.items():
                    for cid in cids:
                        try:
                            await revoke_channel_access(ctx.client.app, cid, did)
                        except Exception as e:
                            logger.warning(f"Channel revoke failed for {did} on {cid} (clean): {e}")
            except Exception as e:
                logger.warning(f"Channel revoke batch failed in clean: {e}")
        # Refresh affected manga webhooks so removed staff disappear from rooms
        if affected_mids:
            try:
                from bot.services.webhook_sender import update_manga_webhook
                import asyncio as _asyncio
                await _asyncio.gather(
                    *(update_manga_webhook(ctx.client.app, mid) for mid in affected_mids),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.warning(f"Webhook refresh failed in clean: {e}")
        try:
            workload_cache.clear()
            manga_options_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        mentions = ", ".join(f"<@{did}>" for did in left_ids[:10])
        extra = f" ({', '.join(left_ids[:5])})" if len(left_ids) <= 5 else ""
        revoked_info = f"\n🔒 Revoked Drive access for {sum(len(v) for v in drive_revoke_map.values())} folder(s)." if drive_revoke_map else ""
        await ctx.respond(components=[create_success(f"Cleaned **{len(left_ids)}** left member(s): {mentions}{extra}\nThey are now hidden from Workload and unassigned from all mangas.{revoked_info}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        # Refresh view
        view, container = await build_workload_view(page=0)
        await ctx.edit_response(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    async def on_back(self, ctx: miru.ViewContext, *args):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


