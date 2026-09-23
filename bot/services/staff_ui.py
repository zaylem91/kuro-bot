import hikari
import miru
import logging
import math
from bot.database.connection import get_pool
from bot.services.ui import create_error, create_success, create_info, create_warning
from bot.core import get_miru_client
from bot.config import EMOJI, GIPHY_STAFF_DASHBOARD
from bot.extensions.workflow_commands.workflow_state import STATUS_ROLE_MAP, STAGE_ROLES
from bot.services.cooldown import check_cooldown

logger = logging.getLogger(__name__)

def build_staff_dashboard_container(app=None):
    from hikari.impl.special_endpoints import (
        ContainerComponentBuilder, 
        SectionComponentBuilder, ThumbnailComponentBuilder, 
        MediaGalleryItemBuilder,
    )
    
    c = ContainerComponentBuilder(id=1)
    
    # 1. Header (Section)
    try:
        if app is None:
            from bot.core import get_bot
            try:
                app = get_bot()
            except Exception:
                app = None
        avatar = str(app.get_me().display_avatar_url) if app else "https://cdn.discordapp.com/embed/avatars/0.png"
    except Exception:
        avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
    sec = SectionComponentBuilder(id=2, accessory=ThumbnailComponentBuilder(media=avatar))
    sec.add_text_display(f"# {EMOJI['important']} Staff Workspace")
    c.add_component(sec)
    
    # 2. Text Content
    msg = (
        f"## {EMOJI.get('staff', '👥')} Staff Dashboard\n\n"
        "Welcome to your personal workspace! Use the buttons below to manage your tasks:\n\n"
        f"📝 **Claim Chapter:** Claim an unassigned chapter.\n"
        f"✅ **Submit Work:** Mark a chapter as done and request payment.\n"
        f"{EMOJI.get('moneybag', '💰')} **My Balance:** View your unpaid chapters and total balance.\n"
        f"⚙️ **Edit Profile:** Update your payment details."
    )
    c.add_text_display(msg)
    
    # 3. Big GIF (Media Gallery)
    c.add_media_gallery([MediaGalleryItemBuilder(media=GIPHY_STAFF_DASHBOARD)])
    
    # 4. Divider Line
    c.add_separator(id=3, spacing=hikari.SpacingType.SMALL, divider=True)
    
    return c

async def _spawn_chapter_select(ctx, user_id: str, mode: str, requested_role: str = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        user_roles = await conn.fetch("SELECT role FROM user_roles WHERE discord_id = $1", user_id)
        if not user_roles:
            await ctx.respond(components=[create_error("You have no registered role. Use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        roles = [r["role"] for r in user_roles if r["role"] in STATUS_ROLE_MAP]
        if not roles:
            await ctx.respond(components=[create_error("You have no registered role. Use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        if requested_role:
            if requested_role not in roles:
                await ctx.respond(components=[create_error(f"You don't have role {requested_role}. Your roles: {', '.join(roles)}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            roles = [requested_role]

        stages: dict[str, set[str]] = {}
        for role in roles:
            stages.setdefault(STATUS_ROLE_MAP[role], set()).add(role)
        role_conditions = []
        params: list = [user_id]
        idx = 2
        for stage in stages:
            allowed = list(STAGE_ROLES.get(stage, ()))
            role_conditions.append(f"(c.status = ${idx} AND ms.role = ANY(${idx+1}::text[]))")
            params.extend([stage, allowed])
            idx += 2
        role_filter = " OR ".join(role_conditions)

        assignment_clause = f"c.assigned_to IS NULL AND ({role_filter})"
        if mode == "done":
            assignment_clause = f"c.assigned_to = $1 OR (c.assigned_to IS NULL AND ({role_filter}))"

        query = f"""
            SELECT DISTINCT c.id, c.chapter_number, c.status, c.manga_id, c.drive_folder_id,
                   c.assigned_to, m.manga_name
            FROM chapters c
            JOIN mangas m ON c.manga_id = m.id
            JOIN manga_staff ms ON ms.manga_id = m.id AND ms.discord_id = $1
            WHERE m.status = 'ongoing'
              AND ({assignment_clause})
            ORDER BY m.manga_name, c.chapter_number
            LIMIT 100
        """
        chapters = await conn.fetch(query, *params)
        user_stages = {STATUS_ROLE_MAP[r] for r in roles}
        filtered_by_staff = []
        for ch in chapters:
            if ch["assigned_to"] == user_id:
                if ch["status"] in user_stages:
                    filtered_by_staff.append(ch)
            else:
                filtered_by_staff.append(ch)
        chapters = filtered_by_staff

    if not chapters:
        await ctx.respond(components=[create_warning("No chapters available for you.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        return

    chapters_by_role: dict[str, list] = {r: [] for r in roles}
    for ch in chapters:
        for r in roles:
            if ch["status"] == STATUS_ROLE_MAP[r]:
                chapters_by_role[r].append(ch)
    chapters_by_role = {k: v for k, v in chapters_by_role.items() if v}

    from bot.extensions.workflow_commands.workflow_ui import ChapterSelectView, RoleSelectView
    from bot.ui.admin import embed_view_in_container

    if len(chapters_by_role) == 1:
        only_role = list(chapters_by_role.keys())[0]
        only_chapters = list(chapters_by_role.values())[0]
        view = ChapterSelectView(chapters=only_chapters, user_role=only_role, user_id=user_id, mode=mode)
        pages_info = ""
        if len(only_chapters) > 25:
            pages_info = f"\nPage 1/{math.ceil(len(only_chapters) / 25)}"
        txt = f"### 📝 Chapter Selection\n**Role:** `{only_role}`\n**Pending Chapters:** `{len(only_chapters)}` {pages_info}\n\nPlease select the chapter(s):"
        c = create_info(txt, title="Chapter Selection")
        await ctx.respond(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        return

    role_view = RoleSelectView(roles=list(chapters_by_role.keys()), user_id=user_id, chapters_by_role=chapters_by_role, mode=mode)
    role_view.clear_items()
    opts = []
    for r in chapters_by_role:
        opts.append(miru.SelectOption(label=f"{r} — {len(chapters_by_role[r])} chapters", value=r))
    sel = miru.TextSelect(options=opts, placeholder="You have multiple roles — choose one")
    sel.callback = role_view.select_role
    role_view.add_item(sel)
    
    action_text = "completing" if mode == "done" else "claiming"
    c = create_info(f"You have multiple roles. Choose which role you're {action_text}:", title="Multiple Roles Detected")
    await ctx.respond(components=embed_view_in_container(role_view, c), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
    get_miru_client().start_view(role_view)

class StaffDashboard(miru.View):
    def __init__(self, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)

    @miru.button(label="Claim Chapter", emoji="📝", style=hikari.ButtonStyle.PRIMARY, custom_id="staff_claim")
    async def btn_claim(self, ctx: miru.ViewContext, button: miru.Button):
        await _spawn_chapter_select(ctx, str(ctx.user.id), "claim")

    @miru.button(label="Submit Work", emoji="✅", style=hikari.ButtonStyle.SUCCESS, custom_id="staff_submit")
    async def btn_submit(self, ctx: miru.ViewContext, button: miru.Button):
        await _spawn_chapter_select(ctx, str(ctx.user.id), "done")

    @miru.button(label="My Balance", emoji=EMOJI.get('moneybag', '💰'), style=hikari.ButtonStyle.SECONDARY, custom_id="staff_balance")
    async def btn_balance(self, ctx: miru.ViewContext, button: miru.Button):
        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT t.*, c.chapter_number
                FROM transactions t
                LEFT JOIN chapters c ON t.chapter_id = c.id
                WHERE t.user_id = $1 AND t.status = 'unpaid' 
                ORDER BY t.created_at ASC
            """, user_id)
            is_reg = await conn.fetchval("SELECT is_registered FROM users WHERE discord_id = $1", user_id)
        if not is_reg:
            await ctx.respond(components=[create_error("You are not registered. Use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if not records:
            await ctx.respond(components=[create_success("You have no unpaid chapters. Good job!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return

        from bot.services.pricing import format_cents_as_dollars
        total_cents = sum(r["amount_cents"] for r in records)
        lines = []
        for r in records:
            dt_str = r["created_at"].strftime("%Y-%m-%d")
            c_label = f"Ch {r['chapter_number']}" if r.get("chapter_number") is not None else (f"Ch ID:{r['chapter_id']}" if r.get("chapter_id") else "Bonus/Penalty")
            lines.append(f"`{dt_str}` | {c_label} | `{r['role']}` | **{format_cents_as_dollars(r['amount_cents'])}**")
        desc = "\n".join(lines)
        if len(desc) > 3000:
            desc = desc[:3000] + "\n... (truncated)"
        desc += f"\n\n**Total Unpaid:** `{format_cents_as_dollars(total_cents)}`"
        await ctx.respond(components=[create_info(desc, title="Your Unpaid Balance 💰")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Settings", emoji="⚙️", style=hikari.ButtonStyle.SECONDARY, custom_id="staff_settings")
    async def btn_settings(self, ctx: miru.ViewContext, button: miru.Button):
        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT timezone, payment_method, payment_info, drive_email FROM users WHERE discord_id = $1 AND is_registered = TRUE", user_id)
        if not row:
            await ctx.respond(components=[create_error("You are not registered. Use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        modal = StaffSettingsModal(
            title="Edit Profile Settings",
            tz_val=row["timezone"] or "+0",
            method_val=row["payment_method"] or "binance",
            info_val=row["payment_info"] or "",
            gmail_val=row["drive_email"] or "",
        )
        await ctx.respond_with_modal(modal)

    @miru.button(label="Help", emoji="❓", style=hikari.ButtonStyle.SECONDARY, custom_id="staff_help")
    async def btn_help(self, ctx: miru.ViewContext, button: miru.Button):
        help_text = (
            "## ❓ Staff Dashboard Help\n\n"
            "📝 **Claim Chapter:** Enter a Chapter ID to reserve it for yourself. You must have the required role (TL/PR/ED) and be assigned to the manga.\n\n"
            "✅ **Submit Work:** Shows all chapters available for your roles. Select one to mark as done, earn payment and move it to the next stage.\n\n"
            "💰 **My Balance:** View your unpaid earnings and transaction history.\n\n"
            "⚙️ **Settings:** Update your timezone and payment method (Binance/PayPal).\n\n"
            "🔓 **Unclaim:** Release a chapter you claimed if you cannot finish it.\n\n"
            "📊 **Status:** View progress overview for any manga.\n\n"
            "📜 **History:** View your recent work and payouts.\n\n"
            "💡 **Tip:** You can also use `/done` and `/register` slash commands for faster access."
        )
        await ctx.respond(components=[create_info(help_text, title="Help")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="Unclaim", emoji="🔓", style=hikari.ButtonStyle.SECONDARY, row=1, custom_id="staff_unclaim")
    async def btn_unclaim(self, ctx: miru.ViewContext, button: miru.Button):
        modal = UnclaimChapterModal(title="Unclaim Chapter")
        await ctx.respond_with_modal(modal)

    @miru.button(label="Status", emoji="📊", style=hikari.ButtonStyle.SECONDARY, row=1, custom_id="staff_status")
    async def btn_status(self, ctx: miru.ViewContext, button: miru.Button):
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT manga_name, status, (SELECT COUNT(*) FROM chapters WHERE manga_id=mangas.id) as cnt FROM mangas ORDER BY manga_name LIMIT 25")
        if not rows:
            await ctx.respond(components=[create_error("No mangas yet.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        lines = [f"**{r['manga_name']}** — `{r['status']}` — {r['cnt']} chapters" for r in rows]
        desc = "\n".join(lines)
        if len(desc) > 4000:
            desc = desc[:4000] + "..."
        await ctx.respond(components=[create_info(desc, title="Manga Projects")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    @miru.button(label="History", emoji="📜", style=hikari.ButtonStyle.SECONDARY, row=1, custom_id="staff_history")
    async def btn_history(self, ctx: miru.ViewContext, button: miru.Button):
        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            tx = await conn.fetch(
                """SELECT c.manga_id, m.manga_name, c.chapter_number, t.role, t.amount_cents, t.status, t.created_at
                   FROM transactions t
                   JOIN chapters c ON c.id = t.chapter_id
                   JOIN mangas m ON m.id = c.manga_id
                   WHERE t.user_id=$1 ORDER BY t.created_at DESC LIMIT 10""",
                user_id
            )
            pays = await conn.fetch("SELECT amount_cents, created_at FROM payments WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user_id)
        from bot.services.pricing import format_cents_as_dollars
        if not tx and not pays:
            await ctx.respond(components=[create_info("No transactions yet. Use Submit Work to earn!", title="Your History")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        desc = ""
        if tx:
            lines = [f"{r['manga_name']} #{r['chapter_number']} `{r['role']}` {format_cents_as_dollars(r['amount_cents'])} — {r['status']}" for r in tx]
            desc += "**Recent Transactions:**\n" + "\n".join(lines) + "\n\n"
        if pays:
            desc += "**Recent Payouts:**\n" + "\n".join([f"{format_cents_as_dollars(r['amount_cents'])} on {r['created_at']:%Y-%m-%d}" for r in pays])
        await ctx.respond(components=[create_info(desc.strip(), title="Your History")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

class ClaimChapterModal(miru.Modal):
    chapter_id = miru.TextInput(label="Chapter ID", placeholder="Enter the Chapter ID (numbers only)", required=True)

    async def callback(self, ctx: miru.ModalContext) -> None:
        try:
            cid = int(self.chapter_id.value)
        except ValueError:
            await ctx.respond(components=[create_error("Chapter ID must be a number.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        if cid > 2147483647:
            await ctx.respond(components=[create_error("Chapter ID is too large.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return

        on_cd, rem = check_cooldown(ctx.user.id, "claim", 5)
        if on_cd:
            await ctx.respond(components=[create_error(f"Slow down! Try again in {rem}s.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return

        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            user_roles = await conn.fetch("SELECT role FROM user_roles WHERE discord_id = $1", user_id)
            roles = [r["role"] for r in user_roles]
            if not roles:
                await ctx.respond(components=[create_error("You have no role. Use `/register`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            async with conn.transaction():
                chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1 FOR UPDATE", cid)
                if not chapter:
                    await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                stage_roles = STAGE_ROLES.get(chapter["status"])
                if not stage_roles or not any(r in roles for r in stage_roles):
                    await ctx.respond(components=[create_error(f"This chapter is in `{chapter['status']}` — you need role {'/'.join(stage_roles) if stage_roles else '?'}. Your roles: {', '.join(roles)}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                is_staff = await conn.fetchval(
                    "SELECT 1 FROM manga_staff WHERE manga_id = $1 AND discord_id = $2 AND role = ANY($3::text[])",
                    chapter["manga_id"], user_id, list(stage_roles)
                )
                if not is_staff:
                    await ctx.respond(components=[create_error("You are not staff for this manga with that role.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                if chapter["assigned_to"]:
                    if chapter["assigned_to"] == user_id:
                        await ctx.respond(components=[create_error("You already claimed this chapter.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    else:
                        await ctx.respond(components=[create_error(f"Already claimed by <@{chapter['assigned_to']}>")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                claimed = await conn.fetchrow(
                    """
                    UPDATE chapters
                    SET assigned_to = $1, assigned_at = now(), updated_at = now()
                    WHERE id = $2
                      AND assigned_to IS NULL
                      AND status = $3
                    RETURNING status
                    """,
                    user_id, cid, chapter["status"],
                )
                if not claimed:
                    await ctx.respond(components=[create_error("Someone claimed or changed it just now. Try another.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                await conn.execute(
                    "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, $2, $3, 'claimed', 0)",
                    cid, claimed["status"], user_id
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
                actor_id=user_id,
                chapter_number=chapter["chapter_number"],
                role="/".join(stage_roles),
                new_status=chapter["status"],
            )
        except Exception as e:
            logger.warning(f"Webhook embed failed for /claim: {e}")

        await ctx.respond(components=[create_success(f"🔒 Claimed chapter **#{cid}** (`{chapter['status']}`) for you! Use `Submit Work` when finished.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

class UnclaimChapterModal(miru.Modal, title="Unclaim Chapter"):
    chapter_id = miru.TextInput(label="Chapter ID", placeholder="Enter Chapter ID to unclaim", required=True)
    async def callback(self, ctx: miru.ModalContext) -> None:
        try:
            cid = int(self.chapter_id.value)
        except ValueError:
            await ctx.respond(components=[create_error("Chapter ID must be a number.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        if cid > 2147483647:
            await ctx.respond(components=[create_error("Chapter ID is too large.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                chapter = await conn.fetchrow("SELECT id, assigned_to, manga_id, chapter_number FROM chapters WHERE id=$1 FOR UPDATE", cid)
                if not chapter:
                    await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                from bot.services.permissions import is_admin
                is_adm = is_admin(getattr(ctx, "member", None))
                if chapter["assigned_to"] != user_id and not is_adm:
                    await ctx.respond(components=[create_error("You can only unclaim your own chapters (or admin).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                if not chapter["assigned_to"]:
                    await ctx.respond(components=[create_error("Chapter is not claimed.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                ownership_clause = "assigned_to IS NOT NULL" if is_adm else "assigned_to = $2"
                ownership_args = (cid,) if is_adm else (cid, user_id)
                released = await conn.execute(
                    f"UPDATE chapters SET assigned_to=NULL, assigned_at=NULL, updated_at=now() WHERE id=$1 AND {ownership_clause}",
                    *ownership_args,
                )
                if released == "UPDATE 0":
                    await ctx.respond(components=[create_error("The chapter ownership changed before it could be unclaimed. Refresh and try again.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                await conn.execute("INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1,'UNCLAIM',$2,'unclaimed',0)", cid, user_id)
            mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id=$1", chapter["manga_id"])
            manga_name = mrow["manga_name"] if mrow else "Manga"
        try:
            from bot.services.webhook_sender import send_webhook_embed
            await send_webhook_embed(ctx.client.app, manga_id=chapter["manga_id"], manga_name=manga_name, event="unclaim", guild_id=getattr(ctx, "guild_id", None), actor_name=ctx.user.username, actor_id=user_id, chapter_number=chapter["chapter_number"])
        except Exception as e:
            logger.warning(f"Webhook failed: {e}")
        await ctx.respond(components=[create_success(f"🔓 Unclaimed chapter **#{cid}**.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

class StaffSettingsModal(miru.Modal):
    def __init__(self, tz_val: str, method_val: str, info_val: str, gmail_val: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tz = miru.TextInput(label="Timezone Offset (e.g. +2, -5:30)", value=tz_val.replace('UTC', ''), required=True)
        self.pay_method = miru.TextInput(label="Payment Method (binance or paypal)", value=method_val, required=True)
        self.pay_info = miru.TextInput(label="Payment Info (Email or ID)", value=info_val, required=True)
        self.gmail = miru.TextInput(label="Gmail for Drive (required)", value=gmail_val, required=True, placeholder="your@gmail.com")
        self.add_item(self.tz)
        self.add_item(self.pay_method)
        self.add_item(self.pay_info)
        self.add_item(self.gmail)

    async def callback(self, ctx: miru.ModalContext):
        raw_tz = self.tz.value.strip()
        import re
        if not re.fullmatch(r"[+-]?\d{1,2}(?::[0-5]\d)?", raw_tz):
            await ctx.respond(components=[create_error("Invalid timezone. Use like +2, -3, +5:30")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        tz_input = raw_tz
        if tz_input and not tz_input[0] in "+-":
            tz_input = f"+{tz_input}"
        tz_input = f"UTC{tz_input}"
            
        method = self.pay_method.value.strip().lower()
        if method not in ["binance", "paypal"]:
            await ctx.respond(components=[create_error("Payment method must be 'binance' or 'paypal'.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        info = self.pay_info.value.strip()
        gmail_raw = self.gmail.value.strip()
        gmail = gmail_raw.lower()
        if not gmail or "@" not in gmail or "." not in gmail.split("@")[-1]:
            await ctx.respond(components=[create_error("Gmail is required. Must be a valid Google account email.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if method == "paypal":
            if "@" not in info or "." not in info.split("@")[-1]:
                await ctx.respond(components=[create_error("Invalid PayPal email.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
        elif method == "binance":
            if len(info) < 5:
                await ctx.respond(components=[create_error("Binance ID too short.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

        user_id = str(ctx.user.id)
        pool = get_pool()
        old_row = None
        old_gmail = None
        try:
            async with pool.acquire() as _c:
                _old = await _c.fetchrow("SELECT timezone, payment_method, payment_info, drive_email FROM users WHERE discord_id=$1", user_id)
                if _old:
                    old_row = dict(_old)
                    old_gmail = _old["drive_email"] if _old["drive_email"] else None
        except Exception:
            old_gmail = None
        async with pool.acquire() as conn:
            existing_user = await conn.fetchval(
                "SELECT discord_id FROM users WHERE LOWER(payment_info) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE",
                info, user_id
            )
            if existing_user:
                await ctx.respond(components=[create_error("This payment info is already registered to another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            existing_gmail = await conn.fetchval(
                "SELECT discord_id FROM users WHERE LOWER(drive_email) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE AND drive_email IS NOT NULL",
                gmail, user_id,
            )
            if existing_gmail:
                await ctx.respond(components=[create_error("This Gmail is already registered to another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

            async with conn.transaction():
                try:
                    # Fetch old values for audit before update (if not already fetched via separate conn)
                    if old_row is None:
                        _chk = await conn.fetchrow("SELECT timezone, payment_method, payment_info, drive_email FROM users WHERE discord_id=$1", user_id)
                        old_row = dict(_chk) if _chk else {}
                        old_gmail = old_row.get("drive_email") if old_row else None
                    res = await conn.execute(
                        "UPDATE users SET timezone = $1, payment_method = $2, payment_info = $3, drive_email = $4 WHERE discord_id = $5",
                        tz_input, method, info, gmail, user_id
                    )
                except Exception as e:
                    import asyncpg
                    if isinstance(e, asyncpg.exceptions.UniqueViolationError) or "uq_users" in str(e) or "payment_info" in str(e).lower() or "drive_email" in str(e).lower():
                        msg = str(e).lower()
                        if "drive_email" in msg:
                            await ctx.respond(components=[create_error("This Gmail was just taken by another member.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        else:
                            await ctx.respond(components=[create_error("This payment info was just taken by another member.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    raise
                if res == "UPDATE 0":
                    await ctx.respond(components=[create_error("You are not registered. Use `/register` first.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                # Audit logging for all changed fields (including drive_email)
                try:
                    changed_by = str(ctx.user.id)
                    old = old_row or {}
                    if str(old.get("timezone")) != str(tz_input):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'timezone',$2,$3,$4)", user_id, str(old.get("timezone")), str(tz_input), changed_by)
                    if str(old.get("payment_method")) != str(method):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_method',$2,$3,$4)", user_id, str(old.get("payment_method")), str(method), changed_by)
                    if str(old.get("payment_info")) != str(info):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_info',$2,$3,$4)", user_id, str(old.get("payment_info")), str(info), changed_by)
                    if str(old.get("drive_email") or "") != str(gmail or ""):
                        if gmail is not None:
                            await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'drive_email',$2,$3,$4)", user_id, str(old.get("drive_email")), str(gmail), changed_by)
                except Exception as ae:
                    import logging
                    logging.getLogger(__name__).debug(f"Failed to insert user_audit for {user_id}: {ae}")

        # Gmail is required — update Drive permissions if changed (collect IDs outside, Drive outside pool)
        _drive_mids: list[int] = []
        _drive_fid_map: dict[int, str] = {}
        _drive_success = False
        if gmail.lower() != (old_gmail or "").lower():
            _drive_success = True
            try:
                async with pool.acquire() as _c2:
                    mrows = await _c2.fetch("SELECT manga_id FROM manga_staff WHERE discord_id=$1", user_id)
                    if mrows:
                        _drive_mids = [r["manga_id"] for r in mrows]
                        frows = await _c2.fetch("SELECT id, drive_folder_id FROM mangas WHERE id = ANY($1::int[])", _drive_mids)
                        _drive_fid_map = {r["id"]: r["drive_folder_id"] for r in frows}
            except Exception:
                _drive_mids = []
                _drive_fid_map = {}
                _drive_success = False
            if _drive_mids:
                try:
                    from bot.core import get_drive_service
                    ds = get_drive_service()
                    for mid in _drive_mids:
                        fid = _drive_fid_map.get(mid)
                        if not fid:
                            continue
                        try:
                            if old_gmail:
                                await ds.revoke_folder_access(fid, old_gmail)
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                        try:
                            await ds.grant_folder_access(fid, gmail, role="writer")
                        except Exception as ge:
                            import logging
                            logging.getLogger(__name__).warning(f"Drive Gmail update failed for {gmail} on {fid}: {ge}")
                            _drive_success = False
                except Exception:
                    _drive_success = False

        drive_msg = f"\n✅ Drive access updated for `{gmail}`." if _drive_success else ""
        await ctx.respond(components=[create_success("Profile updated successfully! ✅" + drive_msg)], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
