"""Finance system â€” balance display and payout via ledger."""
import re
import hikari
from bot.services.ui import create_error, create_success, create_info
import lightbulb
from bot.database.connection import get_pool
from bot.services.pricing import format_cents_as_dollars
from bot.services.autocomplete import registered_user_autocomplete

def _extract_id(text: str) -> str | None:
    m = re.findall(r"\d{17,20}", text)
    return m[0] if m else None

loader = lightbulb.Loader()


@loader.command
class SetupFinance(
    lightbulb.SlashCommand,
    name="setup_finance",
    description="Link this channel as finance channel for a member (admin)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    user = lightbulb.string("user", "Member (registered only)", autocomplete=registered_user_autocomplete)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        uid = _extract_id(self.user)
        if not uid:
            await ctx.respond(components=[create_error("Pick a registered member from the autocomplete.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            from bot.services.permissions import is_admin
            if not is_admin(ctx.member):
                await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            res = await conn.execute(
                "UPDATE users SET finance_channel_id = $1 WHERE discord_id = $2",
                str(ctx.channel_id), uid
            )
            if res == "UPDATE 0":
                await ctx.respond(components=[create_error(f"User <@{uid}> not found or not registered.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
        await ctx.respond(components=[create_success(f"This channel is now linked as finance channel for <@{uid}>")], flags=hikari.MessageFlag.IS_COMPONENTS_V2)

@loader.command
class SetPrice(
    lightbulb.SlashCommand,
    name="set_price",
    description="Update pricing (team leader only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    key = lightbulb.string("key", "Price key", choices=[
        lightbulb.Choice(name="tl_cents (translator)", value="tl_cents"),
        lightbulb.Choice(name="pr_cents (proofreader)", value="pr_cents"),
        lightbulb.Choice(name="ed_under_30", value="ed_under_30"),
        lightbulb.Choice(name="ed_30_44", value="ed_30_44"),
        lightbulb.Choice(name="ed_45_plus", value="ed_45_plus"),
    ])
    value = lightbulb.integer("value", "New price in cents (e.g. 85 = $0.85)", min_value=0, max_value=10000)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL
        )
        from bot.services.permissions import is_team_leader
        if not is_team_leader(ctx.member):
            await ctx.interaction.edit_initial_response(components=[create_error("Team leader only.")])
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchval("SELECT value FROM pricing_config WHERE key=$1 FOR UPDATE", self.key)
                await conn.execute(
                    "INSERT INTO pricing_config (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
                    self.key, self.value
                )
                await conn.execute(
                    "INSERT INTO price_audit (key, old_value, new_value, changed_by) VALUES ($1,$2,$3,$4)",
                    self.key, old, self.value, str(ctx.user.id)
                )
        from bot.services.pricing import invalidate_cache
        invalidate_cache()
        await ctx.interaction.edit_initial_response(components=[create_success(f"Updated pricing `{self.key}` → **{format_cents_as_dollars(self.value)}** ({self.value}¢).")])

@loader.command
class PriceHistory(
    lightbulb.SlashCommand,
    name="price_history",
    description="Show price change history (team leader only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL
        )
        from bot.services.permissions import is_team_leader
        if not is_team_leader(ctx.member):
            await ctx.interaction.edit_initial_response(components=[create_error("Team leader only.")])
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, old_value, new_value, changed_by, changed_at FROM price_audit ORDER BY changed_at DESC LIMIT 15")
        if not rows:
            await ctx.interaction.edit_initial_response(components=[create_info("No price changes yet.")])
            return
        embed = hikari.Embed(title="Price History (last 15)", color=0x95A5A6)
        for r in rows:
            old_s = format_cents_as_dollars(r["old_value"]) if r["old_value"] is not None else "—"
            new_s = format_cents_as_dollars(r["new_value"])
            embed.add_field(f"{r['key']} {old_s} → {new_s}", f"By <@{r['changed_by']}> — {r['changed_at']:%Y-%m-%d %H:%M}", inline=False)
        await ctx.interaction.edit_initial_response(embed=embed)

