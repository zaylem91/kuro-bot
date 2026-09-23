"""Claim / Assign system â€” explicit chapter ownership with timeout."""
import re
import hikari
from bot.services.ui import create_error, create_success
import lightbulb
import lightbulb.prefab
from bot.database.connection import get_pool
from bot.services.permissions import is_admin
from bot.services.autocomplete import registered_user_autocomplete
from bot.services.cooldown import check_cooldown

def _extract_id(text: str) -> str | None:
    m = re.findall(r"\d{17,20}", text)
    return m[0] if m else None

loader = lightbulb.Loader()

STATUS_ROLE_MAP = {
    "TL": "pending_tl",
    "CO-TL": "pending_tl",
    "PR": "pending_pr",
    "CO-PR": "pending_pr",
    "ED": "pending_ed",
    "CO-ED": "pending_ed",
}

STAGE_ROLES: dict[str, tuple[str, str]] = {
    "pending_tl": ("TL", "CO-TL"),
    "pending_pr": ("PR", "CO-PR"),
    "pending_ed": ("ED", "CO-ED"),
}

@loader.command
class Assign(
    lightbulb.SlashCommand,
    name="assign",
    description="Assign a chapter to a member (admin only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    chapter_id = lightbulb.integer("chapter_id", "Chapter ID")
    user = lightbulb.string("user", "Member (registered only)", autocomplete=registered_user_autocomplete)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL
        )
        on_cd, rem = check_cooldown(ctx.user.id, "assign", 5)
        if on_cd:
            await ctx.interaction.edit_initial_response(components=[create_error(f"Slow down! Try again in {rem}s.")])
            return
        if not is_admin(ctx.member):
            await ctx.interaction.edit_initial_response(components=[create_error("Admin only.")])
            return
        target_id = _extract_id(self.user)
        if not target_id:
            await ctx.interaction.edit_initial_response(components=[create_error("Pick a registered member from autocomplete.")])
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                chapter = await conn.fetchrow("SELECT * FROM chapters WHERE id = $1", self.chapter_id)
                if not chapter:
                    await ctx.interaction.edit_initial_response(components=[create_error("Chapter not found.")])
                    return
                if chapter["status"] in ("published", "rejected"):
                    await ctx.interaction.edit_initial_response(components=[create_error(f"Cannot assign in status `{chapter['status']}`. Use /reopen first.")])
                    return
                stage_roles = STAGE_ROLES.get(chapter["status"])
                if stage_roles:
                    has_role = await conn.fetchval("SELECT 1 FROM user_roles WHERE discord_id=$1 AND role = ANY($2::text[])", target_id, list(stage_roles))
                    if not has_role:
                        await ctx.interaction.edit_initial_response(components=[create_error(f"Target doesn't have required role for this stage ({'/'.join(stage_roles)}).")])
                        return
                    is_staff = await conn.fetchval("SELECT 1 FROM manga_staff WHERE manga_id=$1 AND discord_id=$2 AND role = ANY($3::text[])", chapter["manga_id"], target_id, list(stage_roles))
                    if not is_staff:
                        await ctx.interaction.edit_initial_response(components=[create_error(f"Target is not staff for this manga as {'/'.join(stage_roles)}. Use Admin Dashboard → Manage Projects to add staff first.")])
                        return

                # The ownership check belongs in the write itself. The chapter
                # read above is only for validation and the response text.
                try:
                    assigned = await conn.fetchrow(
                        """
                        UPDATE chapters
                        SET assigned_to = $1, assigned_at = now(), updated_at = now()
                        WHERE id = $2
                          AND assigned_to IS NULL
                          AND status = $3
                          AND status NOT IN ('published', 'rejected')
                        RETURNING status
                        """,
                        target_id, self.chapter_id, chapter["status"],
                    )
                except Exception as e:
                    import asyncpg
                    if isinstance(e, asyncpg.exceptions.ForeignKeyViolationError):
                        await ctx.interaction.edit_initial_response(
                            components=[create_error("Target user is not registered or not found.")]
                        )
                        return
                    raise
                if not assigned:
                    await ctx.interaction.edit_initial_response(
                        components=[create_error("Chapter was assigned or changed just now. Refresh and try again.")]
                    )
                    return
                await conn.execute(
                    "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, $2, $3, 'assigned', 0)",
                    self.chapter_id, assigned["status"], target_id
                )
                mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
                manga_name = mrow["manga_name"] if mrow else "Manga"

        assigned_role = stage_roles[0] if stage_roles else chapter["status"]
        try:
            from bot.services.webhook_sender import send_webhook_embed, update_manga_webhook
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=manga_name,
                event="claim",
                guild_id=getattr(ctx, "guild_id", None),
                actor_name=ctx.user.username,
                actor_id=target_id,
                chapter_number=chapter["chapter_number"],
                role=assigned_role,
                new_status=assigned["status"],
            )
            await update_manga_webhook(ctx.client.app, chapter["manga_id"])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Webhook update failed for /assign: {e}")

        await ctx.interaction.edit_initial_response(
            components=[create_success(f"Assigned chapter **#{chapter['chapter_number']}** (`{assigned['status']}`) to <@{target_id}>.")]
        )

@loader.command
class Unclaim(
    lightbulb.SlashCommand,
    name="unclaim",
    description="Release your claim on a chapter",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    chapter_id = lightbulb.integer("chapter_id", "Chapter ID")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        on_cd, rem = check_cooldown(ctx.user.id, "unclaim", 5)
        if on_cd:
            await ctx.respond(components=[create_error(f"Slow down! Try again in {rem}s.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_id = str(ctx.user.id)
        pool = get_pool()
        async with pool.acquire() as conn:
            chapter = await conn.fetchrow("SELECT id, assigned_to, manga_id, chapter_number FROM chapters WHERE id = $1", self.chapter_id)
            if not chapter:
                await ctx.respond(components=[create_error("Chapter not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if chapter["assigned_to"] != user_id and not is_admin(ctx.member):
                await ctx.respond(components=[create_error("You can only unclaim your own chapters (or admin).")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if not chapter["assigned_to"]:
                await ctx.respond(components=[create_error("Chapter is not claimed.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            async with conn.transaction():
                ownership_clause = "assigned_to IS NOT NULL" if is_admin(ctx.member) else "assigned_to = $2"
                ownership_args = (self.chapter_id,) if is_admin(ctx.member) else (self.chapter_id, user_id)
                released = await conn.execute(
                    f"UPDATE chapters SET assigned_to = NULL, assigned_at = NULL, updated_at = now() WHERE id = $1 AND {ownership_clause}",
                    *ownership_args,
                )
                if released == "UPDATE 0":
                    await ctx.respond(components=[create_error("The chapter ownership changed before it could be unclaimed. Refresh and try again.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                await conn.execute(
                    "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents) VALUES ($1, 'UNCLAIM', $2, 'unclaimed', 0)",
                    self.chapter_id, user_id
                )
            # Fetch manga name for webhook
            mrow = await conn.fetchrow("SELECT manga_name FROM mangas WHERE id = $1", chapter["manga_id"])
            manga_name = mrow["manga_name"] if mrow else "Manga"

        # Send webhook embed to manga channel
        try:
            from bot.services.webhook_sender import send_webhook_embed
            await send_webhook_embed(
                ctx.client.app,
                manga_id=chapter["manga_id"],
                manga_name=manga_name,
                event="unclaim",
                guild_id=getattr(ctx, "guild_id", None),
                actor_name=ctx.user.username,
                actor_id=user_id,
                chapter_number=chapter["chapter_number"],
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Webhook embed failed for /unclaim: {e}")

        await ctx.respond(components=[create_success(f"🔓 Unclaimed chapter **#{self.chapter_id}** — now available for anyone.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


@loader.command
class Claim(
    lightbulb.SlashCommand,
    name="claim",
    description="Claim chapter(s) for yourself",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        on_cd, rem = check_cooldown(ctx.user.id, "claim", 5)
        if on_cd:
            await ctx.respond(components=[create_error(f"Slow down! Try again in {rem}s.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        from bot.services.staff_ui import _spawn_chapter_select
        await _spawn_chapter_select(ctx, str(ctx.user.id), "claim")


@loader.error_handler
async def claim_error_handler(exc: lightbulb.exceptions.ExecutionPipelineFailedException) -> bool:
    """Gracefully handle cooldowns for claim-related commands."""
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

