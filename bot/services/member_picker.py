"""Hybrid member picker — filtered list with avatar in confirmation."""
import hikari
import miru
from bot.database.connection import get_pool

async def get_registered_members(role: str | None = None) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        if role in ("TL","PR","ED"):
            rows = await conn.fetch(
                "SELECT u.discord_id, u.default_role FROM users u JOIN user_roles ur ON ur.discord_id=u.discord_id WHERE u.is_registered=TRUE AND ur.role=$1 LIMIT 25",
                role
            )
        else:
            rows = await conn.fetch("SELECT discord_id, default_role FROM users WHERE is_registered=TRUE LIMIT 25")
    return [{"did": r["discord_id"], "role": r["default_role"]} for r in rows]

async def enrich_with_display(ctx, members: list[dict]):
    from bot.services.autocomplete import _resolve_display
    guild_id = getattr(ctx, "guild_id", None) or getattr(getattr(ctx, "interaction", None), "guild_id", None)
    for m in members:
        m["display"] = await _resolve_display(ctx, guild_id, m["did"])
    return members

class MemberPickerView(miru.View):
    """View shown when user wants to browse registered members (filtered, with avatar in embed after pick)."""
    def __init__(self, members: list[dict], on_pick):
        super().__init__(timeout=60)
        self.on_pick = on_pick  # async callback(did)
        options = []
        for m in members[:25]:
            label = f"{m['display']} — {m['role']}"[:100]
            # value must be ID, description shows last 4
            options.append(miru.SelectOption(label=label, value=m["did"], description=f"ID …{m['did'][-4:]}"))
        if not options:
            options = [miru.SelectOption(label="No registered members found", value="none", description="—")]
        select = miru.TextSelect(options=options, placeholder="Pick a registered member")
        select.callback = self._on_select
        self.add_item(select)

    def get_container(self, text: str):
        from hikari.impl.special_endpoints import ContainerComponentBuilder
        container = ContainerComponentBuilder(id=1, accent_color=0x3498DB)
        container.add_text_display(text)
        for row in self.build():
            container.add_action_row(row.components)
        return container

    async def _on_select(self, ctx: miru.ViewContext):
        did = ctx.interaction.values[0]
        if did == "none":
            await ctx.respond("No members.", flags=hikari.MessageFlag.EPHEMERAL)
            return
        await self.on_pick(ctx, did)
        self.stop()
