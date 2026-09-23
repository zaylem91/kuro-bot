from .core import *

class StaffUserSelect(miru.UserSelect):
    def __init__(self, *args, **kwargs):
        super().__init__(placeholder="Select a Staff Member...", *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user = self.values[0]
        user_id = str(user.id)

        user_row, roles, assignments, unpaid_cents, claimed_count = await fetch_staff_profile(user_id)
        if not user_row or not user_row["is_registered"]:
            await ctx.respond(components=[create_error("This user is not registered in the system.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return

        view = SingleUserView(user_id)
        c = build_staff_profile_container(user_id, user_row, roles, assignments, unpaid_cents, claimed_count)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.view.stop()


class StaffMenuView(miru.View):
    def __init__(self, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.add_item(StaffUserSelect())
        
        btn = miru.Button(label="Back to Dashboard", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
        btn.callback = self.btn_back
        self.add_item(btn)

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


class StaffAddRoleSelect(miru.TextSelect):
    def __init__(self, user_id: str, *args, **kwargs):
        self.target_id = user_id
        options = [
            miru.SelectOption(label="Translator (TL)", value="TL"),
            miru.SelectOption(label="Assistant TL (CO-TL)", value="CO-TL"),
            miru.SelectOption(label="Proofreader (PR)", value="PR"),
            miru.SelectOption(label="Assistant PR (CO-PR)", value="CO-PR"),
            miru.SelectOption(label="Editor (ED)", value="ED"),
            miru.SelectOption(label="Assistant ED (CO-ED)", value="CO-ED"),
        ]
        super().__init__(options=options, placeholder="➕ Add a global role...", row=0, *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        role = self.values[0]
        base = role.replace("CO-", "")
        pool = get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM user_roles WHERE discord_id = $1 AND role = $2",
                self.target_id, role,
            )
            if exists:
                await ctx.respond(components=[create_error(f"<@{self.target_id}> already has role `{role}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            await conn.execute(
                "INSERT INTO user_roles (discord_id, role) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                self.target_id, role,
            )
            # If no default and this is a base role, set it as default
            try:
                cur_default = await conn.fetchval(
                    "SELECT default_role FROM users WHERE discord_id = $1", self.target_id
                )
                if not cur_default and base in ("TL", "PR", "ED"):
                    await conn.execute(
                        "UPDATE users SET default_role = $1 WHERE discord_id = $2", base, self.target_id
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        # Assign Discord role for the base (best-effort, outside DB conn)
        try:
            from bot.services.role_sync import assign_discord_role
            guild_id = getattr(ctx, "guild_id", None)
            if not guild_id:
                try:
                    from bot.config import GUILD_ID as _gid
                    guild_id = _gid
                except Exception:
                    guild_id = None
            if guild_id and base in ("TL", "PR", "ED"):
                await assign_discord_role(ctx.client.app, int(guild_id), int(self.target_id), base)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        try:
            workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        # Refresh roles view
        user_row, roles, _, _, _ = await fetch_staff_profile(self.target_id)
        c = build_staff_roles_container(self.target_id, user_row, roles)
        # self.view is StaffRolesView — rebuild to keep selects fresh
        await ctx.edit_response(components=embed_view_in_container(self.view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        await ctx.respond(components=[create_success(f"Added role `{role}` to <@{self.target_id}>.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class StaffRemoveRoleSelect(miru.TextSelect):
    def __init__(self, user_id: str, *args, **kwargs):
        self.target_id = user_id
        options = [
            miru.SelectOption(label="Translator (TL)", value="TL"),
            miru.SelectOption(label="Assistant TL (CO-TL)", value="CO-TL"),
            miru.SelectOption(label="Proofreader (PR)", value="PR"),
            miru.SelectOption(label="Assistant PR (CO-PR)", value="CO-PR"),
            miru.SelectOption(label="Editor (ED)", value="ED"),
            miru.SelectOption(label="Assistant ED (CO-ED)", value="CO-ED"),
        ]
        super().__init__(options=options, placeholder="➖ Remove a global role...", row=1, *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        role = self.values[0]
        base = role.replace("CO-", "")
        pool = get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM user_roles WHERE discord_id = $1 AND role = $2",
                self.target_id, role,
            )
            if not exists:
                await ctx.respond(components=[create_error(f"<@{self.target_id}> does not have role `{role}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            # Block removal while still assigned to an ongoing manga in the same
            # stage (base and CO- share a stage: removing TL while CO-TL assigned
            # would orphan the manga row).
            stage_roles = (base, f"CO-{base}") if not role.startswith("CO-") else (role.replace("CO-", ""), role)
            still_assigned = await conn.fetchval(
                "SELECT 1 FROM manga_staff ms JOIN mangas m ON m.id = ms.manga_id "
                "WHERE ms.discord_id = $1 AND ms.role = ANY($2::text[]) AND m.status = 'ongoing' LIMIT 1",
                self.target_id, list(stage_roles),
            )
            if still_assigned:
                await ctx.respond(components=[create_error(f"Cannot remove `{role}` — <@{self.target_id}> is still assigned to an ongoing manga in the {'/'.join(stage_roles)} stage. Remove them from the manga first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM user_roles WHERE discord_id = $1 AND role = $2",
                    self.target_id, role,
                )
                # Fix default_role if it pointed to the removed base role
                try:
                    cur_default = await conn.fetchval(
                        "SELECT default_role FROM users WHERE discord_id = $1", self.target_id
                    )
                    if cur_default and cur_default == role:
                        remaining = await conn.fetch("SELECT role FROM user_roles WHERE discord_id = $1", self.target_id)
                        rem_roles = [r["role"] for r in remaining]
                        new_default = None
                        for cand in ["TL", "PR", "ED"]:
                            if cand in rem_roles:
                                new_default = cand
                                break
                        if new_default is None:
                            # Fall back to base of any remaining CO-* role
                            for cand in ["TL", "PR", "ED"]:
                                if f"CO-{cand}" in rem_roles:
                                    new_default = cand
                                    break
                        await conn.execute("UPDATE users SET default_role = $1 WHERE discord_id = $2", new_default, self.target_id)
                    remaining_after = await conn.fetch("SELECT role FROM user_roles WHERE discord_id = $1", self.target_id)
                    rem_after = [r["role"] for r in remaining_after]
                except Exception:
                    rem_after = []
        # Remove Discord base role only if no remaining role maps to it
        try:
            still_maps_base = any(r == base or r == f"CO-{base}" for r in rem_after)
            if not still_maps_base and base in ("TL", "PR", "ED"):
                from bot.services.role_sync import remove_discord_role
                guild_id = getattr(ctx, "guild_id", None)
                if not guild_id:
                    try:
                        from bot.config import GUILD_ID as _gid
                        guild_id = _gid
                    except Exception:
                        guild_id = None
                if guild_id:
                    await remove_discord_role(ctx.client.app, int(guild_id), int(self.target_id), base)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        try:
            workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        user_row, roles, _, _, _ = await fetch_staff_profile(self.target_id)
        c = build_staff_roles_container(self.target_id, user_row, roles)
        await ctx.edit_response(components=embed_view_in_container(self.view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        await ctx.respond(components=[create_success(f"Removed role `{role}` from <@{self.target_id}>.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class StaffDefaultRoleSelect(miru.TextSelect):
    def __init__(self, user_id: str, *args, **kwargs):
        self.target_id = user_id
        options = [
            miru.SelectOption(label="TL", value="TL"),
            miru.SelectOption(label="PR", value="PR"),
            miru.SelectOption(label="ED", value="ED"),
        ]
        super().__init__(options=options, placeholder="⭐ Set default (primary) role...", row=2, *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        new_default = self.values[0]
        pool = get_pool()
        async with pool.acquire() as conn:
            has_base = await conn.fetchval(
                "SELECT 1 FROM user_roles WHERE discord_id = $1 AND role IN ($2, $3)",
                self.target_id, new_default, f"CO-{new_default}",
            )
            if not has_base:
                await ctx.respond(components=[create_error(f"<@{self.target_id}> does not have `{new_default}` (or `CO-{new_default}`). Add it first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            await conn.execute("UPDATE users SET default_role = $1 WHERE discord_id = $2", new_default, self.target_id)
        try:
            workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        user_row, roles, _, _, _ = await fetch_staff_profile(self.target_id)
        c = build_staff_roles_container(self.target_id, user_row, roles)
        await ctx.edit_response(components=embed_view_in_container(self.view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        await ctx.respond(components=[create_success(f"Default role for <@{self.target_id}> set to `{new_default}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class StaffRolesView(miru.View):
    def __init__(self, user_id: str, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.user_id = user_id
        self.add_item(StaffAddRoleSelect(user_id))
        self.add_item(StaffRemoveRoleSelect(user_id))
        self.add_item(StaffDefaultRoleSelect(user_id))

    @miru.button(label="Back to Profile", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_row, roles, assignments, unpaid_cents, claimed_count = await fetch_staff_profile(self.user_id)
        view = SingleUserView(self.user_id)
        c = build_staff_profile_container(self.user_id, user_row, roles, assignments, unpaid_cents, claimed_count)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class SingleUserView(miru.View):
    def __init__(self, user_id: str, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.user_id = user_id

    @miru.button(label="View Profile", emoji="👤", style=hikari.ButtonStyle.PRIMARY, row=0)
    async def btn_profile(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_row, roles, assignments, unpaid_cents, claimed_count = await fetch_staff_profile(self.user_id)
        if not user_row or not user_row["is_registered"]:
            await ctx.respond(components=[create_error("This user is not registered.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        c = build_staff_profile_container(self.user_id, user_row, roles, assignments, unpaid_cents, claimed_count)
        await ctx.edit_response(components=embed_view_in_container(self, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Edit Profile", emoji="✏️", style=hikari.ButtonStyle.SUCCESS, row=0)
    async def btn_edit_profile(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT timezone, payment_method, payment_info, drive_email, can_manage_payments "
                "FROM users WHERE discord_id = $1",
                self.user_id,
            )
        if not row:
            await ctx.respond(components=[create_error("User not found in database.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        # Lazy import to avoid circular dependency (admin.py imports admin_ui lazily too)
        from bot.extensions.admin import EditProfileModal
        raw_tz = str(row["timezone"] or "")
        # DB stores "UTC+2" — modal expects "+2"
        utc_time = raw_tz[3:] if raw_tz.startswith("UTC") else raw_tz
        modal = EditProfileModal(
            self.user_id,
            utc_time or "+0",
            str(row["payment_method"] or "binance"),
            str(row["payment_info"] or ""),
            "true" if row["can_manage_payments"] else "false",
            str(row["drive_email"] or ""),
        )
        await ctx.respond_with_modal(modal)

    @miru.button(label="Edit Roles", emoji="🎭", style=hikari.ButtonStyle.PRIMARY, row=0)
    async def btn_edit_roles(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_row, roles, _, _, _ = await fetch_staff_profile(self.user_id)
        if not user_row or not user_row["is_registered"]:
            await ctx.respond(components=[create_error("This user is not registered.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = StaffRolesView(self.user_id)
        c = build_staff_roles_container(self.user_id, user_row, roles)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Audit History", emoji="📜", style=hikari.ButtonStyle.SECONDARY, row=1, custom_id="admin_audit")
    async def btn_audit(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_id = self.user_id
        from bot.database.connection import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            work_hist = await conn.fetch('''
                SELECT ch.timestamp as dt, ch.action, ch.stage, m.manga_name, c.chapter_number
                FROM chapter_history ch
                JOIN chapters c ON ch.chapter_id = c.id
                JOIN mangas m ON c.manga_id = m.id
                WHERE ch.user_id = $1
                ORDER BY ch.timestamp DESC
                LIMIT 15
            ''', user_id)
            
            pay_hist = await conn.fetch('''
                SELECT created_at as dt, amount_cents, payment_method, paid_by
                FROM payments
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT 5
            ''', user_id)

        from bot.services.pricing import format_cents_as_dollars
        
        lines = []
        if work_hist:
            lines.append("### 📚 Recent Work Activity")
            for w in work_hist:
                time_str = w['dt'].strftime("%Y-%m-%d %H:%M")
                action = w['action'].capitalize()
                lines.append(f"`{time_str}` | **{w['manga_name']}** #{w['chapter_number']} | `{w['stage']}`: {action}")
        else:
            lines.append("### 📚 Recent Work Activity\nNo work history found.")
            
        lines.append("\n")
        
        if pay_hist:
            lines.append("### 💰 Recent Payouts")
            for p in pay_hist:
                time_str = p['dt'].strftime("%Y-%m-%d")
                amt = format_cents_as_dollars(p['amount_cents'])
                method = p['payment_method'].capitalize()
                lines.append(f"`{time_str}` | **{amt}** via {method} (Admin: <@{p['paid_by']}>)")
        else:
            lines.append("### 💰 Recent Payouts\nNo payout history found.")
            
        desc = "\n".join(lines)
        if len(desc) > 4000:
            desc = desc[:4000] + "\n... (truncated)"
            
        c = create_info(desc, title=f"Audit Log: <@{user_id}>")
        await ctx.respond(components=[c], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Back", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = StaffMenuView()
        c = build_staff_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


