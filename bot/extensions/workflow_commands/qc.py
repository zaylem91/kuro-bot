import hikari
import lightbulb
from bot.services.ui import create_error
from bot.services.permissions import is_admin
from bot.extensions.workflow_commands.workflow_db import (
    manga_autocomplete,
    _qc_approve,
    _qc_reopen,
    _qc_sync
)
from bot.extensions.workflow_commands.workflow_ui import (
    RejectReasonModal,
)

loader = lightbulb.Loader()

@loader.command
class QC(
    lightbulb.SlashCommand,
    name="qc",
    description="Quality Control: approve, reject, reopen or sync a manga (admin only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    action = lightbulb.string("action", "Action", choices=[
        lightbulb.Choice(name="approve - publish chapter", value="approve"),
        lightbulb.Choice(name="reject - send back", value="reject"),
        lightbulb.Choice(name="reopen - back to pending_tl", value="reopen"),
        lightbulb.Choice(name="sync - deep scan Drive", value="sync"),
    ])
    chapter_id = lightbulb.integer("chapter_id", "Chapter ID (for approve/reject/reopen)", default=None)
    manga = lightbulb.string("manga", "Manga name (for sync)", autocomplete=manga_autocomplete, default="")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        if not is_admin(ctx.member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        action = self.action
        if action in ("approve", "reject", "reopen"):
            if not self.chapter_id:
                await ctx.respond(components=[create_error("chapter_id is required for approve/reject/reopen.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if action == "approve":
                await _qc_approve(ctx, int(self.chapter_id))
            elif action == "reopen":
                await _qc_reopen(ctx, int(self.chapter_id))
            elif action == "reject":
                modal = RejectReasonModal(chapter_id=int(self.chapter_id))
                from bot.core import get_miru_client
                miru_client = get_miru_client()
                builder = modal.build_response(miru_client)
                await builder.create_modal_response(ctx.interaction)
                miru_client.start_modal(modal)
        elif action == "sync":
            if not self.manga:
                await ctx.respond(components=[create_error("manga is required for sync. Select a manga from autocomplete.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            await _qc_sync(ctx, self.manga)
        else:
            await ctx.respond(components=[create_error("Unknown action.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


