"""Monthly period check — ledger stats are computed live, this only updates monthly_period."""
import logging
import lightbulb
from datetime import datetime
from zoneinfo import ZoneInfo
from bot.database.connection import get_pool
from bot.config import MONTHLY_TIMEZONE

logger = logging.getLogger(__name__)
loader = lightbulb.Loader()

async def check_monthly_period():
    """Update monthly_period for all registered users if month changed."""
    try:
        tz = ZoneInfo(MONTHLY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    current_period = datetime.now(tz).strftime("%Y-%m")
    pool = get_pool()
    async with pool.acquire() as conn:
        updated = await conn.execute(
            "UPDATE users SET monthly_period = $1 WHERE monthly_period IS DISTINCT FROM $1 AND is_registered = TRUE",
            current_period,
        )
        if updated != "UPDATE 0":
            logger.info(f"Updated monthly_period to {current_period}: {updated}")

@loader.task(lightbulb.uniformtrigger(86400))  # every 24h
async def monthly_reset_task() -> None:
    try:
        await check_monthly_period()
    except Exception as e:
        logger.error(f"Monthly check error: {e}")
