"""Member registration."""
import re
import hikari
from bot.services.ui import create_error
import lightbulb
from bot.database.connection import get_pool

import miru

loader = lightbulb.Loader()


async def _process_registration(
    ctx,
    *,
    user_id: str,
    guild_id: int | None,
    app: hikari.GatewayBot,
    role: str,
    raw_tz: str,
    payment_method: str,
    payment_info: str,
    gmail_raw: str,
) -> None:
    raw_tz = (raw_tz or "").strip()
    if raw_tz.isdigit():
        raw_tz = f"+{raw_tz}"
    if raw_tz.startswith(('+', '-')):
        raw_tz = f"UTC{raw_tz}"
    if not re.fullmatch(r"UTC[+-](0[0-9]|1[0-4]|[0-9])(:[0-5][0-9])?", raw_tz):
        await ctx.respond(components=[create_error("Invalid timezone. Use like +2, -3, +5:30")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        return
    tz_input = raw_tz

    method = (payment_method or "").strip().lower()
    if method not in ("binance", "paypal"):
        await ctx.respond(components=[create_error("Payment method must be binance or paypal.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        return

    info = (payment_info or "").strip()
    gmail = (gmail_raw or "").strip().lower()
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

    pool = get_pool()
    async with pool.acquire() as conn:
        existing_user = await conn.fetchval(
            "SELECT discord_id FROM users WHERE LOWER(payment_info) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE",
            info, user_id
        )
        if existing_user:
            await ctx.respond(components=[create_error("This payment info is already registered to another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if gmail:
            existing_gmail = await conn.fetchval(
                "SELECT discord_id FROM users WHERE LOWER(drive_email) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE AND drive_email IS NOT NULL",
                gmail, user_id,
            )
            if existing_gmail:
                await ctx.respond(components=[create_error("This Gmail is already registered to another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return

        existing = await conn.fetchrow("SELECT discord_id FROM users WHERE discord_id = $1 AND is_registered = TRUE", user_id)
        if existing:
            await ctx.respond(components=[create_error("You are already registered! Contact admin to edit your info. To add a second role, contact an admin.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return

        try:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO users (discord_id, timezone, default_role, payment_method, payment_info, drive_email, is_registered, registered_at)
                       VALUES ($1, $2, $3, $4, $5, $6, TRUE, now())
                       ON CONFLICT (discord_id) DO UPDATE SET
                            timezone = $2, default_role = $3, payment_method = $4,
                            payment_info = $5, drive_email = $6, is_registered = TRUE, registered_at = now()""",
                    user_id, tz_input, role, method, info, gmail
                )
                await conn.execute(
                    "INSERT INTO user_roles (discord_id, role) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    user_id, role
                )
        except Exception as e:
            # Handle unique constraint race on payment_info / drive_email
            import asyncpg
            _msg = str(e).lower()
            _is_unique = isinstance(e, asyncpg.exceptions.UniqueViolationError)
            if "drive_email" in _msg or "uq_users_drive_email" in _msg:
                await ctx.respond(components=[create_error("This Gmail was just registered by another member. Please use a different one.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            if _is_unique or "payment_info" in _msg or "uq_users_payment_info" in _msg:
                await ctx.respond(components=[create_error("This payment info was just registered by another member. Please use a different one.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            raise

    try:
        from bot.services.cache import workload_cache, user_workload_cache
        workload_cache.clear()
        user_workload_cache.clear()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

    # Auto-assign Discord role (persistent)
    role_assigned = False
    if guild_id:
        try:
            from bot.services.role_sync import assign_discord_role
            from bot.config import ROLE_ID_MAP
            if ROLE_ID_MAP.get(role):
                await assign_discord_role(app, guild_id, int(user_id), role)
                role_assigned = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to assign Discord role: {e}")

    from hikari.impl.special_endpoints import ContainerComponentBuilder
    container = ContainerComponentBuilder(id=1, accent_color=0x00FF00)

    container.add_text_display("## Registration successful!")
    container.add_separator(divider=True)

    role_text = role + (" (Discord role assigned)" if role_assigned else " — ask admin to assign Discord role if not configured")
    container.add_text_display(
        f"**Role:** {role_text}\n"
        f"**Timezone:** {tz_input}\n"
        f"**Payment method:** {method.capitalize()}\n"
        f"**Payment info:** {info}\n"
        f"**Drive Gmail:** {gmail}"
    )

    container.add_separator(divider=True)
    container.add_text_display("To edit info, contact an admin.")

    await ctx.respond(components=[container], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class RegistrationModal(miru.Modal, title="Team Member Registration"):
    utc_time = miru.TextInput(label="UTC Offset (e.g. +2 or -3)", placeholder="e.g. +2", required=True)
    payment_method = miru.TextInput(label="Payment Method (binance or paypal)", placeholder="binance / paypal", required=True)
    payment_info = miru.TextInput(label="Payment Info (Binance ID or PayPal email)", placeholder="...", required=True)
    gmail = miru.TextInput(label="Drive Gmail (Google account email)", placeholder="name@gmail.com", required=True)

    def __init__(self, role: str):
        super().__init__()
        self.role = role

    async def callback(self, ctx: miru.ModalContext) -> None:
        await _process_registration(
            ctx,
            user_id=str(ctx.user.id),
            guild_id=getattr(ctx, "guild_id", None),
            app=ctx.client.app,
            role=self.role,
            raw_tz=self.utc_time.value,
            payment_method=self.payment_method.value,
            payment_info=self.payment_info.value,
            gmail_raw=self.gmail.value,
        )


@loader.command
class Register(
    lightbulb.SlashCommand,
    name="register",
    description="Register as a new team member",
):
    role = lightbulb.string("role", "Primary role", choices=[lightbulb.Choice(name="TL", value="TL"), lightbulb.Choice(name="PR", value="PR"), lightbulb.Choice(name="ED", value="ED")])
    utc_time = lightbulb.string("utc_time", "Your UTC offset (optional - modal will open if omitted)", default=None)
    payment_method = lightbulb.string("payment_method", "Payment method (optional)", choices=[lightbulb.Choice(name="binance", value="binance"), lightbulb.Choice(name="paypal", value="paypal")], default=None)
    payment_info = lightbulb.string("payment_info", "Binance ID or PayPal email (optional)", default=None)
    gmail = lightbulb.string("gmail", "Your Gmail for Drive folder access (optional)", default=None)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        # If private info is omitted, open secure private modal
        if not (self.utc_time and self.payment_method and self.payment_info and self.gmail):
            from bot.core import get_miru_client
            modal = RegistrationModal(role=self.role)
            miru_client = get_miru_client()
            builder = modal.build_response(miru_client)
            await builder.create_modal_response(ctx.interaction)
            miru_client.start_modal(modal)
            return

        await _process_registration(
            ctx,
            user_id=str(ctx.user.id),
            guild_id=getattr(ctx, "guild_id", None),
            app=ctx.client.app,
            role=self.role,
            raw_tz=self.utc_time,
            payment_method=self.payment_method,
            payment_info=self.payment_info,
            gmail_raw=self.gmail,
        )


@loader.command
class StaffPanel(
    lightbulb.SlashCommand,
    name="staff",
    description="Open the Staff Dashboard",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        from bot.services.staff_ui import StaffDashboard, build_staff_dashboard_container
        from bot.core import get_miru_client
        view = StaffDashboard()
        c = build_staff_dashboard_container()
        from bot.ui.admin import embed_view_in_container
        await ctx.respond(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)

@loader.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    from bot.services.staff_ui import StaffDashboard
    from bot.core import get_miru_client
    get_miru_client().start_view(StaffDashboard(), bind_to=None)
