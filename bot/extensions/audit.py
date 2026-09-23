"""Audit log viewer — /audit command for reviewing user & staff changes."""
import re
import hikari
import lightbulb
import lightbulb.prefab
from bot.database.connection import get_pool
from bot.services.ui import create_error, create_info
from bot.services.permissions import is_admin
from bot.services.autocomplete import registered_user_autocomplete

loader = lightbulb.Loader()


async def manga_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    pool = get_pool()
    q = ctx.focused.value or ""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT manga_name FROM mangas WHERE manga_name ILIKE $1 LIMIT 25", f"%{q}%")
    await ctx.respond([r["manga_name"] for r in rows])


def _extract_id(text: str) -> str | None:
    m = re.findall(r"\d{17,20}", text)
    return m[0] if m else None


@loader.command
class Audit(
    lightbulb.SlashCommand,
    name="audit",
    description="View audit logs — user changes & staff add/remove (admin only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    audit_type = lightbulb.string(
        "type",
        "Which logs?",
        choices=[
            lightbulb.Choice(name="all", value="all"),
            lightbulb.Choice(name="user (drive_email, payment...)", value="user"),
            lightbulb.Choice(name="manga_staff (add/remove)", value="manga_staff"),
        ],
        default="all",
    )
    limit = lightbulb.integer("limit", "How many entries? (1-25)", default=10, min_value=1, max_value=25)
    user = lightbulb.string("user", "Filter by member (optional)", autocomplete=registered_user_autocomplete, default="")
    manga = lightbulb.string("manga", "Filter by manga name (optional, for staff logs)", autocomplete=manga_autocomplete, default="")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL
        )
        if not is_admin(ctx.member):
            await ctx.interaction.edit_initial_response(components=[create_error("Admin only.")])
            return

        audit_type = self.audit_type or "all"
        limit = int(self.limit or 10)
        filter_user_id = None
        if self.user:
            filter_user_id = _extract_id(self.user)
            if not filter_user_id:
                await ctx.interaction.edit_initial_response(components=[create_info("Invalid user format. Please select from autocomplete or provide a valid ID.")])
                return
        filter_manga_name = self.manga.strip() if self.manga else None

        pool = get_pool()
        lines: list[str] = []

        # Resolve manga_id if filter provided
        filter_manga_id = None
        if filter_manga_name:
            async with pool.acquire() as conn:
                mrow = await conn.fetchrow("SELECT id FROM mangas WHERE manga_name = $1", filter_manga_name)
                if mrow:
                    filter_manga_id = mrow["id"]
                else:
                    # try ILIKE fallback
                    mrow = await conn.fetchrow("SELECT id FROM mangas WHERE manga_name ILIKE $1 LIMIT 1", f"%{filter_manga_name}%")
                    if mrow:
                        filter_manga_id = mrow["id"]
            if not filter_manga_id:
                await ctx.interaction.edit_initial_response(components=[create_info("Invalid manga format. Please select from autocomplete or provide a valid name.")])
                return

        if audit_type in ("all", "user"):
            # Build query for user_audit
            q = "SELECT discord_id, field, old_value, new_value, changed_by, changed_at FROM user_audit WHERE 1=1"
            params: list = []
            idx = 1
            if filter_user_id:
                q += f" AND discord_id = ${idx}"
                params.append(filter_user_id)
                idx += 1
            # manga filter doesn't apply to user_audit, ignore
            q += f" ORDER BY changed_at DESC LIMIT ${idx}"
            params.append(limit if audit_type == "user" else max(5, limit // 2))
            async with pool.acquire() as conn:
                rows = await conn.fetch(q, *params)
            if rows:
                lines.append(f"### 👤 User Audit — last {len(rows)}")
                for r in rows:
                    ts = r["changed_at"].strftime("%Y-%m-%d %H:%M") if r["changed_at"] else "?"
                    old = (r["old_value"] or "—")[:40]
                    new = (r["new_value"] or "—")[:40]
                    # sanitize to avoid mention spam already via user id
                    lines.append(f"`{ts}` <@{r['discord_id']}> `{r['field']}` `{old}` → `{new}` by <@{r['changed_by']}>")
            else:
                lines.append("### 👤 User Audit\n*No entries found.*")

        if audit_type in ("all", "manga_staff"):
            q2 = """
                SELECT a.manga_id, m.manga_name, a.discord_id, a.role, a.action, a.changed_by, a.changed_at
                FROM manga_staff_audit a
                JOIN mangas m ON m.id = a.manga_id
                WHERE 1=1
            """
            params2: list = []
            idx = 1
            if filter_user_id:
                q2 += f" AND a.discord_id = ${idx}"
                params2.append(filter_user_id)
                idx += 1
            if filter_manga_id:
                q2 += f" AND a.manga_id = ${idx}"
                params2.append(filter_manga_id)
                idx += 1
            q2 += f" ORDER BY a.changed_at DESC LIMIT ${idx}"
            params2.append(limit if audit_type == "manga_staff" else max(5, limit // 2))
            async with pool.acquire() as conn:
                rows2 = await conn.fetch(q2, *params2)
            if rows2:
                lines.append(f"### 👥 Manga Staff Audit — last {len(rows2)}")
                for r in rows2:
                    ts = r["changed_at"].strftime("%Y-%m-%d %H:%M") if r["changed_at"] else "?"
                    lines.append(f"`{ts}` **{r['manga_name']}** — <@{r['discord_id']}> `{r['role']}` **{r['action']}** by <@{r['changed_by']}>")
            else:
                lines.append("### 👥 Manga Staff Audit\n*No entries found.*")

        if not lines:
            lines.append("*No audit entries.*")

        # Truncate if too long (>3500 chars)
        desc = "\n".join(lines)
        if len(desc) > 3800:
            desc = desc[:3800] + "\n*...truncated*"

        # Show filters in header
        filter_info = []
        if filter_user_id:
            filter_info.append(f"User: <@{filter_user_id}>")
        if filter_manga_name and filter_manga_id:
            filter_info.append(f"Manga: {filter_manga_name}")
        filt_str = f"\n*Filters:* {' | '.join(filter_info)}" if filter_info else ""

        container = create_info(desc + filt_str, title=f"📋 Audit Log — {audit_type} (limit {limit})")
        await ctx.interaction.edit_initial_response(components=[container])


@loader.error_handler
async def audit_error_handler(exc: lightbulb.exceptions.ExecutionPipelineFailedException) -> bool:
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
