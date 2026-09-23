"""Scanlation Bot — main entry point (lightbulb v3 + miru v4)."""
import asyncio
import os

# Performance: on UNIX-like systems use uvloop (libuv) instead of the default
# asyncio event loop. No-op on Windows (os.name == "nt") or if uvloop isn't installed.
if os.name != "nt":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

import logging
import hikari
import lightbulb
import miru

from bot.config import DISCORD_TOKEN, DATABASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON, GUILD_ID
from bot.database.connection import init_db, close_db, get_pool
from bot.services.drive import DriveService

# Do not configure logging manually — hikari formats it with colored levels (I/W/E with banner)
logger = logging.getLogger("manga_bot")

def _strip_pii(event, hint):
    try:
        event.pop("user", None)
        req = event.get("request")
        if isinstance(req, dict):
            req.pop("headers", None)
            req.pop("cookies", None)
            req.pop("env", None)
        contexts = event.get("contexts", {})
        if isinstance(contexts, dict):
            contexts.pop("runtime", None)
    except Exception as e:
        logger.debug("Failed to strip PII from Sentry event: %s", e)
    return event

bot = hikari.GatewayBot(
    token=DISCORD_TOKEN,
    intents=hikari.Intents.ALL_UNPRIVILEGED | hikari.Intents.GUILD_MEMBERS,
)

# Lightbulb client
if GUILD_ID:
    client = lightbulb.client_from_app(bot, default_enabled_guilds=[GUILD_ID])
else:
    client = lightbulb.client_from_app(bot)

miru_client = miru.Client(bot)

@miru_client.set_unhandled_component_interaction_hook
async def unhandled_components(interaction: hikari.ComponentInteraction) -> None:
    if interaction.custom_id == "ui_close_btn":
        try:
            from hikari.impl.special_endpoints import ContainerComponentBuilder
            c = ContainerComponentBuilder(id=1, accent_color=0x2ECC71)
            c.add_text_display("✅ Dashboard closed.")
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                components=[c]
            )
        except Exception as e:
            logger.error(f"Failed to close UI: {e}")


# Expose shared refs via core module
import bot.core as core
core.miru_client = miru_client
core.lb_client = client


@bot.listen(hikari.StartingEvent)
async def on_starting(event: hikari.StartingEvent) -> None:
    """Initialize database and Drive service on startup."""
    # Sentry — init here so hikari's log handler already exists (visible like in image)
    try:
        from bot.config import SENTRY_DSN
        _dsn_dbg = (SENTRY_DSN or "")[:12]
        logger.info(f"Sentry DSN loaded: {bool(SENTRY_DSN)} len={len(SENTRY_DSN or '')} prefix={_dsn_dbg!r}")
        if SENTRY_DSN:
            import sentry_sdk  # type: ignore[import-not-found]
            sentry_sdk.init(
                dsn=SENTRY_DSN,
                traces_sample_rate=0.1,
                send_default_pii=False,
                before_send=_strip_pii,
            )
            logger.info("Sentry enabled (PII stripped)")
        else:
            logger.info("Sentry disabled (SENTRY_DSN empty)")
    except Exception as _e:
        logger.warning(f"Sentry init failed: {_e}", exc_info=True)

    logger.info("Initializing database...")
    await init_db(DATABASE_URL)
    logger.info("Database connected")

    # Health server for monitoring (no extra deps beyond aiohttp already used)
    try:
        from bot.services.health import start_health_server
        core.health_runner = await start_health_server()
    except Exception as e:
        logger.warning(f"Health server not started: {e}")

    # Persistent background job queue for Drive syncs
    try:
        from bot.services.task_queue import init_queue, start_worker
        await init_queue(get_pool())
        await start_worker(get_pool())
        logger.info("Task queue initialized")
    except Exception as e:
        logger.warning(f"Task queue not started: {e}")

    # Warn if Drive root is not configured — sync will fail silently otherwise
    try:
        from bot.config import DRIVE_ROOT_FOLDER_ID
        if not DRIVE_ROOT_FOLDER_ID:
            logger.warning("DRIVE_ROOT_FOLDER_ID is empty — Drive sync will fail until you set it in .env")
    except Exception as e:
        logger.debug("Drive root folder check skipped: %s", e)
    logger.info("Initializing Google Drive service...")
    core.drive_service = DriveService(GOOGLE_SERVICE_ACCOUNT_JSON)
    logger.info("Drive service ready")
    # Clock skew check — Google requires system time within 5 min for service account JWT
    try:
        from bot.services.drive import check_clock_skew
        ok, skew = await check_clock_skew()
        if not ok:
            logger.warning(f"⚠️  System clock is off by {int(skew)}s — Drive will fail with invalid_grant until you sync time! (Settings → Time → Sync now)")
        else:
            logger.info(f"Clock skew OK ({int(skew)}s)")
    except Exception as e:
        logger.debug("Clock skew check skipped: %s", e)

    EXTENSIONS = [
        "bot.extensions.profile",
        "bot.extensions.workflow_commands.done",
        "bot.extensions.workflow_commands.qc",
        
        "bot.extensions.admin",
        "bot.extensions.events",
        "bot.extensions.claim",
        "bot.extensions.audit",
        "bot.extensions.finance",
        "bot.tasks.monthly_reset_loop",
        "bot.tasks.assignment_timeout",
        "bot.tasks.monthly_invoice",
        "bot.tasks.backup_loop",
    ]
    for ext in EXTENSIONS:
        try:
            await client.load_extensions(ext)
            logger.info(f"Loaded: {ext}")
        except Exception as e:
            logger.error(f"Failed to load {ext}: {e}", exc_info=True)

    try:
        await client.start()
    except hikari.errors.HTTPResponseError as e:
        if getattr(e, "status", None) == 429:
            retry_after = 60
            logger.warning(
                "Discord returned Global Rate Limit 429 on startup. "
                "Sleeping %ss before retrying to prevent crash-restart ban loops...",
                retry_after
            )
            await asyncio.sleep(retry_after)
            await client.start()
        else:
            raise

    # One-time cleanup: remove stale GLOBAL commands left from old guild/no-guild deploys.
    # Only run when explicitly enabled to avoid extra REST call on every startup.
    # Set CLEAR_GLOBAL_COMMANDS=1 in .env to enable one-time cleanup.
    import os
    if os.getenv("CLEAR_GLOBAL_COMMANDS", "").lower() in ("1", "true", "yes"):
        try:
            app = await bot.rest.fetch_application()
            await bot.rest.set_application_commands(app.id, [])
            logger.info("Cleared stale global commands")
        except Exception as e:
            logger.debug(f"Clear global commands skipped: {e}")

    # Guild permissions via bot API are deprecated by Discord (April 2022) — bots get 403 "Bots cannot use this endpoint".
    # Visibility is now handled by default_member_permissions=ADMINISTRATOR on admin commands (admin.py:46 etc.)
    # plus manual config in Server Settings → Integrations → Lingo → Command Permissions for TEAM_LEADER.
    # No programmatic set_guild_permissions here to avoid 403 + rate-limit spam.

@bot.listen(hikari.StoppingEvent)
async def on_stopping(event: hikari.StoppingEvent) -> None:
    """Graceful shutdown — stop miru/tasks before closing DB to avoid 'terminating tasks forcefully'."""
    logger.info("Stopping miru client...")
    try:
        miru_client.stop()
    except Exception as e:
        logger.debug("Miru client stop skipped: %s", e)
    # Stop health server & queue worker
    try:
        import bot.core as _core
        runner = getattr(_core, "health_runner", None)
        if runner:
            await runner.cleanup()
    except Exception as e:
        logger.debug("Health runner cleanup skipped: %s", e)
    try:
        from bot.services.task_queue import stop_worker
        await stop_worker()
    except Exception as e:
        logger.debug("Task queue worker stop skipped: %s", e)
    # Give lightbulb's scheduled tasks (drive_sync, monthly, etc.) a moment to cancel.
    # They are `asyncio.sleep` based — force-cancellation is harmless but noisy.
    import asyncio
    await asyncio.sleep(0.2)
    logger.info("Closing database pool...")
    await close_db()
    logger.info("Database closed")




if __name__ == "__main__":
    bot.run()
