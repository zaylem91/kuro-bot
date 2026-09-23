import math
import hikari
import lightbulb
import lightbulb.prefab
import miru
from bot.services.ui import create_error, create_info, create_success
from bot.services.cooldown import check_cooldown
from bot.database.connection import get_pool
from bot.extensions.workflow_commands.workflow_state import (
    STATUS_ROLE_MAP,
    STAGE_ROLES,
)
from bot.extensions.workflow_commands.workflow_ui import (
    ChapterSelectView,
    RoleSelectView
)

loader = lightbulb.Loader()

@loader.command
class Done(
    lightbulb.SlashCommand,
    name="done",
    description="Mark a chapter as done — shows chapters available for your role",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    role = lightbulb.string("role", "Filter by role (optional)", choices=[lightbulb.Choice(name="TL", value="TL"), lightbulb.Choice(name="CO-TL", value="CO-TL"), lightbulb.Choice(name="PR", value="PR"), lightbulb.Choice(name="CO-PR", value="CO-PR"), lightbulb.Choice(name="ED", value="ED"), lightbulb.Choice(name="CO-ED", value="CO-ED")], default="")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        on_cd, rem = check_cooldown(ctx.user.id, "done", 5)
        if on_cd:
            await ctx.respond(components=[create_error(f"Slow down! Try again in {rem}s.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_id = str(ctx.user.id)
        pool = get_pool()
        requested_role = self.role or None

        async with pool.acquire() as conn:
            user_roles = await conn.fetch(
                "SELECT role FROM user_roles WHERE discord_id = $1", user_id
            )
            if not user_roles:
                await ctx.respond(
                    components=[create_error("You have no registered role. Use /register first.")],
                    flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
                )
                return

            roles = [r["role"] for r in user_roles if r["role"] in STATUS_ROLE_MAP]
            if not roles:
                await ctx.respond(components=[create_error("Invalid roles.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

            if requested_role and requested_role not in roles:
                await ctx.respond(components=[create_error(f"You don't have role {requested_role}. Your roles: {', '.join(roles)}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

            # If a specific role was requested, narrow to it
            query_roles = [requested_role] if requested_role else roles

            # Group by stage: each stage accepts base or CO- variant in manga_staff.
            stages: dict[str, set[str]] = {}
            for role in query_roles:
                expected = STATUS_ROLE_MAP[role]
                stages.setdefault(expected, set()).add(role)
            # Also include the sibling variant for manga_staff matching
            # (e.g. global TL can work CO-TL slots and vice versa).
            role_conditions = []
            params: list = [user_id]  # $1 = user_id
            idx = 2
            for stage in stages:
                allowed = list(STAGE_ROLES.get(stage, ()))
                role_conditions.append(f"(c.status = ${idx} AND ms.role = ANY(${idx+1}::text[]))")
                params.extend([stage, allowed])
                idx += 2

            role_filter = " OR ".join(role_conditions)

            query = f"""
                SELECT DISTINCT c.id, c.chapter_number, c.status, c.manga_id, c.drive_folder_id,
                       c.assigned_to, m.manga_name
                FROM chapters c
                JOIN mangas m ON c.manga_id = m.id
                LEFT JOIN manga_staff ms ON ms.manga_id = m.id AND ms.discord_id = $1
                WHERE m.status = 'ongoing'
                  AND (
                      c.assigned_to = $1
                      OR (c.assigned_to IS NULL AND ({role_filter}))
                  )
                ORDER BY m.manga_name, c.chapter_number
                LIMIT 150
            """
            chapters = await conn.fetch(query, *params)
            # Fix assigned_to bypass: filter out assigned chapters where user doesn't have matching stage
            user_stages = {STATUS_ROLE_MAP[r] for r in roles}
            filtered_by_staff = []
            for ch in chapters:
                if ch["assigned_to"] == user_id:
                    if ch["status"] in user_stages:
                        filtered_by_staff.append(ch)
                    # else: assigned but role mismatch — skip (orphaned assignment)
                else:
                    filtered_by_staff.append(ch)
            chapters = filtered_by_staff

        if not chapters:
            await ctx.respond(
                components=[create_info("No chapters waiting for you.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            return

        # Build per-role buckets for selector (stage match only — no fan-out).
        chapters_by_role: dict[str, list] = {r: [] for r in query_roles}
        for ch in chapters:
            for r in query_roles:
                if ch["status"] == STATUS_ROLE_MAP[r]:
                    chapters_by_role[r].append(ch)

        # Remove empty buckets
        chapters_by_role = {k: v for k, v in chapters_by_role.items() if v}

        # If requested role or single role, show directly
        if requested_role:
            view = ChapterSelectView(chapters=chapters_by_role[requested_role], user_role=requested_role, user_id=user_id)
            txt = f"### 📝 Chapter Selection\n**Role:** `{requested_role}`\n**Pending Chapters:** `{len(chapters_by_role[requested_role])}`\n\nPlease select the chapter you just finished from the menu below:"
            await ctx.respond(
                components=[view.get_container(txt)],
                flags=hikari.MessageFlag.IS_COMPONENTS_V2
            )
            from bot.core import get_miru_client
            get_miru_client().start_view(view)
            return

        if len(chapters_by_role) == 1:
            only_role = list(chapters_by_role.keys())[0]
            only_chapters = list(chapters_by_role.values())[0]
            view = ChapterSelectView(chapters=only_chapters, user_role=only_role, user_id=user_id)
            pages_info = ""
            if len(only_chapters) > 25:
                pages_info = f"\nPage 1/{math.ceil(len(only_chapters) / 25)}"
            txt = f"### 📝 Chapter Selection\n**Role:** `{only_role}`\n**Pending Chapters:** `{len(only_chapters)}` {pages_info}\n\nPlease select the chapter you just finished from the menu below:"
            await ctx.respond(
                components=[view.get_container(txt)],
                flags=hikari.MessageFlag.IS_COMPONENTS_V2
            )
            from bot.core import get_miru_client
            get_miru_client().start_view(view)
            return

        # Multiple roles — let user pick
        role_view = RoleSelectView(roles=list(chapters_by_role.keys()), user_id=user_id, chapters_by_role=chapters_by_role)
        # Filter options dynamically
        role_view.clear_items()
        opts = []
        for r in chapters_by_role:
            opts.append(miru.SelectOption(label=f"{r} — {len(chapters_by_role[r])} chapters", value=r))
        sel = miru.TextSelect(options=opts, placeholder="You have multiple roles — choose one")
        sel.callback = role_view.select_role
        role_view.add_item(sel)
        # Wrap role_view in container for consistency
        from hikari.impl.special_endpoints import ContainerComponentBuilder
        c = ContainerComponentBuilder(id=1, accent_color=0x3498DB)
        c.add_text_display("You have multiple roles. Choose which role you're completing:")
        for row in role_view.build():
            c.add_action_row(row.components)
        await ctx.respond(components=[c], flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        from bot.core import get_miru_client as _gmc2
        _gmc2().start_view(role_view)

@loader.error_handler
async def workflow_error_handler(exc: lightbulb.exceptions.ExecutionPipelineFailedException) -> bool:
    cause = exc.__cause__ or (exc.causes[0] if exc.causes else None)
    if isinstance(cause, lightbulb.prefab.OnCooldown):
        try:
            await exc.context.respond(
                components=[create_error(f"Slow down! Try again in {cause.remaining:.1f}s.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        return True
    return False

