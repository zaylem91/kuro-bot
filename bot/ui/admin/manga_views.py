from .core import *

class MangaSelect(miru.TextSelect):
    def __init__(self, options, *args, **kwargs):
        super().__init__(options=options, placeholder="Select a manga to manage...", *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        manga_id = int(self.values[0])
        view = SingleMangaView(manga_id)
        
        # Build embed for this manga
        pool = get_pool()
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT * FROM mangas WHERE id = $1", manga_id)
            if not m:
                await ctx.respond(components=[create_error("Manga not found (it may have been deleted).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            staff = await conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id = $1", manga_id)

        staff_list = []
        if staff:
            roles = {"TL": "Translators", "CO-TL": "Assistant TLs", "PR": "Proofreaders", "CO-PR": "Assistant PRs", "ED": "Editors", "CO-ED": "Assistant EDs"}
            grouped = {"TL": [], "CO-TL": [], "PR": [], "CO-PR": [], "ED": [], "CO-ED": []}
            for s in staff:
                r = s["role"]
                if r not in grouped: grouped[r] = []
                grouped[r].append(f"<@{s['discord_id']}>")
            
            for role, users in grouped.items():
                if users:
                    staff_list.append(f"**{roles.get(role, role.upper())}:** {', '.join(users)}")
        
        staff_text = "\n".join(staff_list) if staff_list else "*No staff assigned yet.*"
        
        manga_name_safe = sanitize_manga_name(m["manga_name"])
        drive_link = f"https://drive.google.com/drive/folders/{m['drive_folder_id']}" if m['drive_folder_id'] else "—"
        c = create_info(
            f"**Status:** `{m['status']}`\n**Start Ch:** `{m['start_chapter']}`\n**Drive:** [Link]({drive_link})\n\n### 👥 Current Staff\n{staff_text}", 
            title=manga_name_safe
        )
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.view.stop()


class MangaMenuView(miru.View):
    def __init__(self, options, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        if options:
            self.add_item(MangaSelect(options=options))
        
        btn_add = miru.Button(label="Add New Manga", emoji=EMOJI["CheckMark"], style=hikari.ButtonStyle.SUCCESS, row=1)
        btn_add.callback = self.btn_add
        self.add_item(btn_add)

        btn_search = miru.Button(label="Search", emoji="🔍", style=hikari.ButtonStyle.SECONDARY, row=1)
        btn_search.callback = self.btn_search
        self.add_item(btn_search)
        
        btn_back = miru.Button(label="Back to Dashboard", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
        btn_back.callback = self.btn_back
        self.add_item(btn_back)

    async def btn_search(self, ctx: miru.ViewContext):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from .manga_modals import SearchMangaModal
        modal = SearchMangaModal(title="Search Manga")
        await ctx.respond_with_modal(modal)

    async def btn_add(self, ctx: miru.ViewContext):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from .manga_modals import AddMangaModal
        modal = AddMangaModal("Add New Manga")
        await ctx.respond_with_modal(modal)

    async def btn_back(self, ctx: miru.ViewContext):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class SingleMangaView(miru.View):
    def __init__(self, manga_id: int, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.manga_id = manga_id
        self.selected_user_id = None
        self.selected_role = None

    @miru.button(label="Edit Details", emoji=EMOJI.get("purplebriefcase", "💼"), style=hikari.ButtonStyle.PRIMARY, row=0)
    async def btn_edit(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT * FROM mangas WHERE id = $1", self.manga_id)
        from .manga_modals import EditMangaModal
        modal = EditMangaModal("Edit Manga", manga_id=self.manga_id, m=m)
        await ctx.respond_with_modal(modal)

    @miru.button(label="Toggle Status", emoji="🔄", style=hikari.ButtonStyle.SECONDARY, row=0)
    async def btn_status(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        pool = get_pool()
        # 3-state cycle: ongoing -> paused -> dropped -> ongoing
        STATUS_CYCLE = {
            "ongoing": "paused",
            "paused": "dropped",
            "dropped": "ongoing",
            "finished": "ongoing",
            "completed": "ongoing",
        }
        STATUS_LABEL = {
            "ongoing": "🟢 Ongoing",
            "paused": "⏸️ Paused",
            "dropped": "❌ Dropped",
        }
        drive_revoke_list: list[tuple[str, str, str]] = []
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT status, manga_name, discord_channel_id, drive_folder_id FROM mangas WHERE id = $1", self.manga_id)
            if not m:
                await ctx.respond(components=[create_error("Manga not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            old_status = m["status"]
            new_status = STATUS_CYCLE.get(old_status, "ongoing")
            await conn.execute("UPDATE mangas SET status = $1 WHERE id = $2", new_status, self.manga_id)
            removed_staff = 0
            # When paused/dropped -> remove all staff and prepare Drive revokes
            if new_status in ("paused", "dropped"):
                # Collect staff + Gmails before delete for Drive revoke + audit
                staff_rows = []
                try:
                    staff_rows = await conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id=$1", self.manga_id)
                except Exception:
                    staff_rows = []
                try:
                    rows = await conn.fetch(
                        "SELECT ms.discord_id, u.drive_email FROM manga_staff ms JOIN users u ON u.discord_id = ms.discord_id WHERE ms.manga_id=$1 AND u.drive_email IS NOT NULL",
                        self.manga_id,
                    )
                    folder_id = m["drive_folder_id"]
                    for r in rows:
                        if r["drive_email"]:
                            drive_revoke_list.append((r["discord_id"], r["drive_email"], folder_id))
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                try:
                    res = await conn.execute("DELETE FROM manga_staff WHERE manga_id = $1", self.manga_id)
                    try:
                        removed_staff = int(str(res).split()[-1])
                    except Exception:
                        removed_staff = 0
                    # Audit each auto_removed staff
                    if staff_rows:
                        changed_by_a = str(ctx.user.id)
                        for sr in staff_rows:
                            try:
                                await conn.execute(
                                    "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1,$2,$3,'auto_removed',$4)",
                                    self.manga_id, sr["discord_id"], sr["role"], changed_by_a
                                )
                            except Exception as e:
                                import logging
                                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                except Exception:
                    removed_staff = 0
                try:
                    await conn.execute("UPDATE chapters SET assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE manga_id = $1 AND assigned_to IS NOT NULL", self.manga_id)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        try:
            manga_options_cache.clear()
            workload_cache.clear()
            drive_deep_sync_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        # Revoke Drive access for removed staff (outside DB transaction)
        if drive_revoke_list:
            try:
                from bot.core import get_drive_service
                for _, email, fid in drive_revoke_list:
                    try:
                        await get_drive_service().revoke_folder_access(fid, email)
                    except Exception as e:
                        logger.warning(f"Drive revoke failed for {email} on {fid}: {e}")
            except Exception as e:
                logger.warning(f"Drive revoke batch failed: {e}")
        # Revoke Discord room access for removed staff (this manga's room only).
        # Use staff_rows (all removed members, even without Gmail) — not just drive list.
        if new_status in ("paused", "dropped") and m["discord_channel_id"]:
            try:
                from bot.services.manga_access import revoke_channel_access
                try:
                    removed_ids = list({str(sr["discord_id"]) for sr in (staff_rows or [])})
                except Exception:
                    removed_ids = []
                if not removed_ids:
                    removed_ids = list({did for did, _, _ in drive_revoke_list})
                for did in removed_ids:
                    try:
                        await revoke_channel_access(ctx.client.app, m["discord_channel_id"], did)
                    except Exception as e:
                        logger.warning(f"Channel revoke failed for {did} on pause/drop: {e}")
            except Exception as e:
                logger.warning(f"Channel revoke batch failed on pause/drop: {e}")

        # --- Move Discord channel to status category ---
        moved_msg = ""
        if m["discord_channel_id"]:
            try:
                from bot.config import get_category_for_status
                guild_id = getattr(ctx, "guild_id", None)
                if not guild_id:
                    try:
                        from bot.config import GUILD_ID
                        guild_id = GUILD_ID
                    except Exception:
                        guild_id = None
                if guild_id:
                    target_name, target_id = get_category_for_status(new_status)
                    # Ensure category exists
                    if not target_id:
                        try:
                            channels = await ctx.client.app.rest.fetch_guild_channels(int(guild_id))
                            for ch in channels:
                                if ch.type == hikari.ChannelType.GUILD_CATEGORY and ch.name == target_name:
                                    target_id = ch.id
                                    break
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                        if not target_id:
                            try:
                                cat = await ctx.client.app.rest.create_guild_category(int(guild_id), target_name)
                                target_id = cat.id
                            except Exception as e:
                                logger.warning(f"Failed to create category {target_name}: {e}")
                                target_id = None
                    if target_id:
                        await ctx.client.app.rest.edit_channel(int(m["discord_channel_id"]), parent_category=target_id)
                        moved_msg = f" • Moved channel to **{target_name}**"
            except Exception as e:
                logger.warning(f"Failed to move channel for {m['manga_name']} to {new_status} category: {e}")

        try:
            from bot.services.webhook_sender import update_manga_webhook
            await update_manga_webhook(ctx.client.app, self.manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        label = STATUS_LABEL.get(new_status, new_status)
        old_label = STATUS_LABEL.get(old_status, old_status)
        extra = ""
        if new_status in ("paused", "dropped") and removed_staff > 0:
            extra = f"\n🧹 Removed **{removed_staff}** staff member(s) from this manga and cleared claims."
        await ctx.respond(components=[create_success(f"Status of **{m['manga_name']}** changed from `{old_label}` to `{label}`{moved_msg}.{extra}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Delete", emoji="🗑️", style=hikari.ButtonStyle.DANGER, row=0)
    async def btn_delete(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        pool = get_pool()
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT manga_name, discord_channel_id, drive_folder_id FROM mangas WHERE id = $1", self.manga_id)
            if not m:
                await ctx.edit_response(components=[create_error("Manga not found (already deleted).")], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Ledger guard: deleting cascades to chapters -> transactions (unpaid vanishes).
            unpaid = await conn.fetchval(
                "SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t "
                "JOIN chapters c ON c.id = t.chapter_id WHERE c.manga_id = $1 AND t.status = 'unpaid'",
                self.manga_id,
            )
            if unpaid and unpaid > 0:
                from bot.services.pricing import format_cents_as_dollars as _fmt
                await ctx.edit_response(components=[create_error(f"Cannot delete **{m['manga_name']}** — staff have `{_fmt(unpaid)}` unpaid for this manga. Settle payouts first (Finance Hub), then delete.")], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Collect Drive revoke info before delete (ex-staff must lose folder access)
            drive_revokes: list[tuple[str, str]] = []
            try:
                folder_id = m["drive_folder_id"]
                if folder_id:
                    rows = await conn.fetch(
                        "SELECT DISTINCT u.drive_email FROM manga_staff ms "
                        "JOIN users u ON u.discord_id = ms.discord_id "
                        "WHERE ms.manga_id = $1 AND u.drive_email IS NOT NULL",
                        self.manga_id,
                    )
                    drive_revokes = [(r["drive_email"], folder_id) for r in rows if r["drive_email"]]
            except Exception:
                drive_revokes = []

            # Delete discord channel if it exists
            if m and m["discord_channel_id"]:
                try:
                    await ctx.client.app.rest.delete_channel(int(m["discord_channel_id"]))
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

            await conn.execute("DELETE FROM mangas WHERE id = $1", self.manga_id)
        # Revoke Drive access for ex-staff (outside DB connection)
        if drive_revokes:
            try:
                from bot.core import get_drive_service
                ds = get_drive_service()
                for email, fid in drive_revokes:
                    try:
                        await ds.revoke_folder_access(fid, email)
                    except Exception as e:
                        logger.warning(f"Drive revoke failed for {email} on delete manga {self.manga_id}: {e}")
            except Exception as e:
                logger.warning(f"Drive revoke batch failed on delete manga: {e}")
        try:
            manga_options_cache.clear()
            workload_cache.clear()
            drive_deep_sync_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        view = AdminDashboard()
        c = create_success(f"Manga **{m['manga_name']}** has been permanently deleted.")
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Sync Drive", emoji="🔄", style=hikari.ButtonStyle.SECONDARY, row=0)
    async def btn_sync(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.respond(components=[create_info("Scanning Google Drive... This may take a minute.", title="Syncing...")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        pool = get_pool()
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT drive_folder_id, manga_name FROM mangas WHERE id = $1", self.manga_id)
            if not m:
                await ctx.respond(components=[create_error("Manga not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            drive_id = m["drive_folder_id"]
            manga_name = m["manga_name"]
        try:
            from bot.core import get_drive_service
            ds = get_drive_service()
            progress = await ds.deep_sync_manga(drive_id)
        except Exception as e:
            await ctx.respond(components=[create_error(f"Drive sync failed: {e}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if not progress:
            await ctx.respond(components=[create_error("No chapters found in Drive.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        new_count = 0
        updated = 0
        async with pool.acquire() as conn:
            for ch_num, data in progress.items():
                existing = await conn.fetchrow("SELECT id, status FROM chapters WHERE manga_id=$1 AND chapter_number=$2", self.manga_id, ch_num)
                if existing:
                    if existing["status"] != data["status"]:
                        await conn.execute("UPDATE chapters SET status=$1, drive_folder_id=$2 WHERE id=$3", data["status"], data["id"], existing["id"])
                        updated += 1
                else:
                    await conn.execute("INSERT INTO chapters (manga_id, chapter_number, drive_folder_id, status) VALUES ($1,$2,$3,$4)", self.manga_id, ch_num, data["id"], data["status"])
                    new_count += 1
        try:
            from bot.services.webhook_sender import update_manga_webhook
            await update_manga_webhook(ctx.client.app, self.manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        await ctx.respond(components=[create_success(f"Synced **{manga_name}**: {new_count} new, {updated} updated. Total: {len(progress)} chapters.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.user_select(placeholder="1. Select Staff Member", row=1)
    async def select_staff(self, ctx: miru.ViewContext, select: miru.UserSelect):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.selected_user_id = str(select.values[0].id)
        await ctx.defer() # Acknowledge interaction silently

    @miru.text_select(
        placeholder="2. Select Role", 
        options=[
            miru.SelectOption(label="Translator (TL)", value="tl", emoji=EMOJI.get("tl", "📝")),
            miru.SelectOption(label="Assistant TL (CO-TL)", value="co-tl", emoji=EMOJI.get("tl", "📝")),
            miru.SelectOption(label="Proofreader (PR)", value="pr", emoji=EMOJI.get("pr", "🔍")),
            miru.SelectOption(label="Assistant PR (CO-PR)", value="co-pr", emoji=EMOJI.get("pr", "🔍")),
            miru.SelectOption(label="Editor (ED)", value="ed", emoji=EMOJI.get("ed", "💻")),
            miru.SelectOption(label="Assistant ED (CO-ED)", value="co-ed", emoji=EMOJI.get("ed", "💻")),
        ],
        row=2
    )
    async def select_role(self, ctx: miru.ViewContext, select: miru.TextSelect):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        self.selected_role = select.values[0]
        await ctx.defer()

    @miru.button(label="Assign Staff", emoji="✅", style=hikari.ButtonStyle.SUCCESS, row=3)
    async def btn_assign(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if not self.selected_user_id or not self.selected_role:
            await ctx.respond(components=[create_error("Please select both a **Staff Member** and a **Role** first!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        role_upper = self.selected_role.upper()
        pool = get_pool()
        async with pool.acquire() as conn:
            is_reg = await conn.fetchval("SELECT is_registered FROM users WHERE discord_id = $1", self.selected_user_id)
            if not is_reg:
                await ctx.respond(components=[create_error(f"<@{self.selected_user_id}> is not registered. They must use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Fix #1: check global role exists in user_roles (CO-* accepts base role too)
            has_global = await conn.fetchval("SELECT 1 FROM user_roles WHERE discord_id=$1 AND role=$2", self.selected_user_id, role_upper)
            if not has_global and role_upper.startswith("CO-"):
                base = role_upper.replace("CO-", "")
                has_global = await conn.fetchval("SELECT 1 FROM user_roles WHERE discord_id=$1 AND role=$2", self.selected_user_id, base)
            if not has_global:
                await ctx.respond(components=[create_error(f"<@{self.selected_user_id}> does not have global role **{role_upper}** (or base **{role_upper.replace('CO-','')}**). Assign it first via `/edit_role`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Fix #2: only one primary TL/PR/ED per manga — second must be CO-*
            if role_upper in ("TL", "PR", "ED"):
                cnt = await conn.fetchval("SELECT COUNT(*) FROM manga_staff WHERE manga_id=$1 AND role=$2", self.manga_id, role_upper)
                if cnt and cnt >= 1:
                    existing = await conn.fetchrow("SELECT discord_id FROM manga_staff WHERE manga_id=$1 AND role=$2 LIMIT 1", self.manga_id, role_upper)
                    if existing and str(existing["discord_id"]) != str(self.selected_user_id):
                        await ctx.respond(components=[create_error(f"This manga already has a primary **{role_upper}**: <@{existing['discord_id']}>. Use **CO-{role_upper}** for additional staff.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
            # Prepare variables for post-DB Drive + Discord work (avoid holding pool during network I/O)
            drive_grant_ok = None
            drive_email = None
            folder_id = None
            channel_id = None
            m = None
            insert_ok = False
            try:
                await conn.execute(
                    "INSERT INTO manga_staff (manga_id, discord_id, role) VALUES ($1, $2, $3)",
                    self.manga_id, self.selected_user_id, role_upper
                )
                # Audit: log staff addition
                try:
                    await conn.execute(
                        "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1,$2,$3,'added',$4)",
                        self.manga_id, self.selected_user_id, role_upper, str(ctx.user.id)
                    )
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                # Collect Drive + Discord info while holding connection (no network yet)
                try:
                    row_g = await conn.fetchrow("SELECT drive_email FROM users WHERE discord_id=$1", self.selected_user_id)
                    drow = await conn.fetchrow("SELECT drive_folder_id, manga_name, discord_channel_id FROM mangas WHERE id=$1", self.manga_id)
                    drive_email = row_g["drive_email"] if row_g and row_g["drive_email"] else None
                    folder_id = drow["drive_folder_id"] if drow else None
                    channel_id = drow["discord_channel_id"] if drow and drow["discord_channel_id"] else None
                except Exception:
                    drive_email = None
                    folder_id = None
                    channel_id = None
                m = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", self.manga_id)
                insert_ok = True
            except Exception:
                # Likely unique constraint violation
                await ctx.respond(components=[create_error("User is already assigned to this role!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
        # --- Drive grant outside DB connection (pool released) ---
        if insert_ok and drive_email and folder_id:
            try:
                from bot.core import get_drive_service
                drive_grant_ok = await get_drive_service().grant_folder_access(folder_id, drive_email, role="writer")
            except Exception as ge:
                logger.warning(f"Drive grant failed for {drive_email} on {folder_id}: {ge}")
                drive_grant_ok = False
        # --- Discord channel grant (this manga's room only) ---
        channel_grant_ok = None
        if insert_ok and channel_id:
            try:
                from bot.services.manga_access import grant_channel_access
                channel_grant_ok = await grant_channel_access(ctx.client.app, channel_id, self.selected_user_id)
            except Exception as ge:
                logger.warning(f"Channel grant failed for {self.selected_user_id} on {channel_id}: {ge}")
                channel_grant_ok = False
        if insert_ok:
            try:
                workload_cache.clear()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            await update_manga_webhook(ctx.client.app, self.manga_id)
            # Live refresh Current Staff (fresh DB fetch, new connection)
            try:
                async with pool.acquire() as fresh_conn:
                    fresh_m = await fresh_conn.fetchrow("SELECT * FROM mangas WHERE id=$1", self.manga_id)
                    fresh_staff = await fresh_conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id=$1", self.manga_id)
                from bot.services.ui import sanitize_manga_name as _san
                roles_map = {"TL": "Translators", "CO-TL": "Assistant TLs", "PR": "Proofreaders", "CO-PR": "Assistant PRs", "ED": "Editors", "CO-ED": "Assistant EDs"}
                grouped = {"TL": [], "CO-TL": [], "PR": [], "CO-PR": [], "ED": [], "CO-ED": []}
                for s in fresh_staff:
                    r = s["role"]
                    if r not in grouped: grouped[r] = []
                    grouped[r].append(f"<@{s['discord_id']}>")
                staff_list = []
                for role, users in grouped.items():
                    if users:
                        staff_list.append(f"**{roles_map.get(role, role.upper())}:** {', '.join(users)}")
                staff_text = "\n".join(staff_list) if staff_list else "*No staff assigned yet.*"
                drive_link = f"https://drive.google.com/drive/folders/{fresh_m['drive_folder_id']}" if fresh_m['drive_folder_id'] else "—"
                new_c = create_info(
                    f"**Status:** `{fresh_m['status']}`\n**Start Ch:** `{fresh_m['start_chapter']}`\n**Drive:** [Link]({drive_link})\n\n### 👥 Current Staff\n{staff_text}",
                    title=_san(fresh_m["manga_name"])
                )
                await ctx.edit_response(components=embed_view_in_container(self, new_c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            msg = f"Assigned <@{self.selected_user_id}> as **{role_upper}** to **{m['manga_name']}**!"
            if drive_email:
                if drive_grant_ok:
                    msg += f"\n✅ Granted Drive access to `{drive_email}` (writer)."
                elif drive_grant_ok is False:
                    msg += f"\n⚠️ Could not grant Drive access to `{drive_email}` — check Gmail and that the folder is shared with the service account."
            else:
                msg += "\n⚠️ No Gmail set for this user — Drive access not granted. Ask them to set it via Staff Dashboard → Settings."
            if channel_grant_ok is True:
                msg += f"\n✅ Added to manga room <#{channel_id}>."
            elif channel_grant_ok is False:
                msg += f"\n⚠️ Could not add to manga room <#{channel_id}> — check bot permissions (Manage Channels)."
            await ctx.respond(components=[create_success(msg)], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Remove Staff", emoji="❌", style=hikari.ButtonStyle.DANGER, row=3)
    async def btn_remove(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if not self.selected_user_id or not self.selected_role:
            await ctx.respond(components=[create_error("Please select both a **Staff Member** and a **Role** first!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        pool = get_pool()
        _m_name = None
        _need_revoke = False
        _revoke_email = None
        _revoke_folder = None
        _revoke_channel = None
        _delete_ok = False
        async with pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM manga_staff WHERE manga_id = $1 AND discord_id = $2 AND role = $3",
                self.manga_id, self.selected_user_id, self.selected_role.upper()
            )
            mrow = await conn.fetchrow("SELECT manga_name, discord_channel_id FROM mangas WHERE id = $1", self.manga_id)
            _m_name = mrow["manga_name"] if mrow else "Manga"
            _revoke_channel = mrow["discord_channel_id"] if mrow and mrow["discord_channel_id"] else None
            if res == "DELETE 0":
                await ctx.respond(components=[create_error(f"<@{self.selected_user_id}> does not have the **{self.selected_role.upper()}** role on this manga.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Audit: log removal
            try:
                await conn.execute(
                    "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1,$2,$3,'removed',$4)",
                    self.manga_id, self.selected_user_id, self.selected_role.upper(), str(ctx.user.id)
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            # Collect revoke info without network call.
            # Only revoke Drive + channel when the user has NO remaining roles
            # on THIS manga — other mangas are never touched.
            _still_has_other_role = False
            try:
                still_has = await conn.fetchval("SELECT 1 FROM manga_staff WHERE manga_id=$1 AND discord_id=$2 LIMIT 1", self.manga_id, self.selected_user_id)
                if not still_has:
                    row_g = await conn.fetchrow("SELECT drive_email FROM users WHERE discord_id=$1", self.selected_user_id)
                    drow = await conn.fetchrow("SELECT drive_folder_id FROM mangas WHERE id=$1", self.manga_id)
                    _revoke_email = row_g["drive_email"] if row_g and row_g["drive_email"] else None
                    _revoke_folder = drow["drive_folder_id"] if drow else None
                    _need_revoke = bool(_revoke_email and _revoke_folder)
                    if not _revoke_email:
                        _need_revoke = False
                else:
                    # Still has another role on this manga — keep room + Drive
                    _revoke_channel = None
                    _still_has_other_role = True
            except Exception as e:
                logger.warning(f"Drive revoke collect failed on remove: {e}")
            _delete_ok = True
        # --- Drive revoke outside DB connection (this manga's folder only) ---
        drive_revoke_msg = ""
        if _delete_ok and _need_revoke:
            try:
                from bot.core import get_drive_service
                ok = await get_drive_service().revoke_folder_access(_revoke_folder, _revoke_email)
                drive_revoke_msg = f"\n✅ Revoked Drive access for `{_revoke_email}`." if ok else f"\n⚠️ Could not revoke Drive access for `{_revoke_email}`."
            except Exception as e:
                logger.warning(f"Drive revoke failed on remove: {e}")
                drive_revoke_msg = f"\n⚠️ Could not revoke Drive access for `{_revoke_email}`."
        elif _delete_ok and _revoke_email is None:
            # Check if user had no email at all
            try:
                async with pool.acquire() as _c:
                    _chk = await _c.fetchval("SELECT drive_email FROM users WHERE discord_id=$1", self.selected_user_id)
                    if not _chk:
                        drive_revoke_msg = "\n⚠️ User had no Gmail — no Drive access to revoke."
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        # --- Discord channel revoke (this manga's room only) ---
        channel_revoke_msg = ""
        if _delete_ok and _still_has_other_role:
            channel_revoke_msg = "\nℹ️ Still has another role on this manga — kept room + Drive access."
        elif _delete_ok and _revoke_channel:
            try:
                from bot.services.manga_access import revoke_channel_access
                ok = await revoke_channel_access(ctx.client.app, _revoke_channel, self.selected_user_id)
                if ok:
                    channel_revoke_msg = f"\n✅ Removed from manga room <#{_revoke_channel}>."
                elif ok is False:
                    channel_revoke_msg = f"\n⚠️ Could not remove from manga room <#{_revoke_channel}> — check bot permissions."
            except Exception as e:
                logger.warning(f"Channel revoke failed on remove: {e}")
                channel_revoke_msg = "\n⚠️ Could not remove from manga room."
        if _delete_ok:
            try:
                workload_cache.clear()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            await update_manga_webhook(ctx.client.app, self.manga_id)
            # Live refresh Current Staff (fresh connection)
            try:
                async with pool.acquire() as fresh_conn:
                    fresh_m = await fresh_conn.fetchrow("SELECT * FROM mangas WHERE id=$1", self.manga_id)
                    fresh_staff = await fresh_conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id=$1", self.manga_id)
                from bot.services.ui import sanitize_manga_name as _san2
                roles_map2 = {"TL": "Translators", "CO-TL": "Assistant TLs", "PR": "Proofreaders", "CO-PR": "Assistant PRs", "ED": "Editors", "CO-ED": "Assistant EDs"}
                grouped2 = {"TL": [], "CO-TL": [], "PR": [], "CO-PR": [], "ED": [], "CO-ED": []}
                for s in fresh_staff:
                    r = s["role"]
                    if r not in grouped2: grouped2[r] = []
                    grouped2[r].append(f"<@{s['discord_id']}>")
                staff_list2 = []
                for role, users in grouped2.items():
                    if users:
                        staff_list2.append(f"**{roles_map2.get(role, role.upper())}:** {', '.join(users)}")
                staff_text2 = "\n".join(staff_list2) if staff_list2 else "*No staff assigned yet.*"
                drive_link2 = f"https://drive.google.com/drive/folders/{fresh_m['drive_folder_id']}" if fresh_m['drive_folder_id'] else "—"
                new_c2 = create_info(
                    f"**Status:** `{fresh_m['status']}`\n**Start Ch:** `{fresh_m['start_chapter']}`\n**Drive:** [Link]({drive_link2})\n\n### 👥 Current Staff\n{staff_text2}",
                    title=_san2(fresh_m["manga_name"])
                )
                await ctx.edit_response(components=embed_view_in_container(self, new_c2), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            await ctx.respond(components=[create_success(f"Removed <@{self.selected_user_id}> from **{self.selected_role.upper()}** on **{_m_name}**!{drive_revoke_msg}{channel_revoke_msg}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Suggest Staff", emoji="💡", style=hikari.ButtonStyle.SECONDARY, row=3)
    async def btn_suggest(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if not self.selected_role:
            await ctx.respond(components=[create_error("Please select a **Role** first (step 2), then press Suggest.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        role_upper = self.selected_role.upper()
        # Match base or assistant variant (CO-TL holders can fill TL-family slots).
        base_role = role_upper.replace("CO-", "")
        stage_global_roles = [base_role, f"CO-{base_role}"]
        pool = get_pool()
        async with pool.acquire() as conn:
            # Find least-loaded members who have this role globally and are not already on this manga with that role
            candidates = await conn.fetch(
                """
                SELECT
                    u.discord_id,
                    COUNT(DISTINCT m2.id) as active_cnt
                FROM users u
                JOIN user_roles ur ON ur.discord_id = u.discord_id AND ur.role = ANY($1::text[])
                LEFT JOIN manga_staff ms ON ms.discord_id = u.discord_id
                LEFT JOIN mangas m2 ON m2.id = ms.manga_id AND m2.status = 'ongoing'
                WHERE u.is_registered = TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM manga_staff ms2
                    WHERE ms2.manga_id = $2 AND ms2.discord_id = u.discord_id AND ms2.role = $3
                  )
                GROUP BY u.discord_id
                ORDER BY active_cnt ASC, u.registered_at ASC
                LIMIT 5
                """,
                stage_global_roles, self.manga_id, role_upper,
            )
            if not candidates:
                # Fallback: try without excluding already-assigned (maybe all are assigned)
                candidates = await conn.fetch(
                    """
                    SELECT u.discord_id, COUNT(DISTINCT m2.id) as active_cnt
                    FROM users u
                    JOIN user_roles ur ON ur.discord_id = u.discord_id AND ur.role = ANY($1::text[])
                    LEFT JOIN manga_staff ms ON ms.discord_id = u.discord_id
                    LEFT JOIN mangas m2 ON m2.id = ms.manga_id AND m2.status = 'ongoing'
                    WHERE u.is_registered = TRUE
                    GROUP BY u.discord_id
                    ORDER BY active_cnt ASC
                    LIMIT 5
                    """,
                    stage_global_roles,
                )
                if not candidates:
                    await ctx.respond(components=[create_error(f"No registered members with role **{base_role}** (or `CO-{base_role}`) found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
            lines = []
            for r in candidates:
                cnt = r["active_cnt"]
                load = "⚪ Idle" if cnt == 0 else "🟢 Low" if cnt < 3 else "🟡 Medium" if cnt < 5 else "🔴 High"
                lines.append(f"<@{r['discord_id']}> — **{cnt}** active mangas — {load}")
            desc = f"**Best candidates for {role_upper} (least loaded first):**\n" + "\n".join(lines) + "\n\n*Pick one from the User dropdown above, then press Assign.*"
            await ctx.respond(components=[create_info(desc, title="💡 Suggested Staff")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Back", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
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


