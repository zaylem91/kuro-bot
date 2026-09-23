"""Monthly invoice — on 1st of each month, send summary to each member's finance channel."""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import hikari
import lightbulb
from bot.database.connection import get_pool
from bot.config import MONTHLY_TIMEZONE
from bot.services.pricing import format_cents_as_dollars

logger = logging.getLogger(__name__)
loader = lightbulb.Loader()

INVOICE_SEND_CONCURRENCY = 5
INVOICE_SEND_BATCH_SIZE = 100
INVOICE_MIN_SEND_INTERVAL = 0.1
INVOICE_SEND_LEASE = timedelta(minutes=15)

async def _claim_invoice_dispatch(
    pool,
    discord_id: str,
    billing_period,
    *,
    retry_sent: bool = False,
    recover_stale_sending: bool = True,
) -> bool:
    """Atomically claim a member's invoice for delivery.

    A successful dispatch is left untouched during normal reruns. Failed
    dispatches can be claimed again, while a stale sending claim is recovered
    after a process restart. ``retry_sent`` is reserved for an explicit
    administrator retry.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO invoice_dispatches (
                discord_id, billing_period, status, attempt_count,
                last_attempt_at, updated_at
            )
            VALUES ($1, $2, 'sending', 1, now(), now())
            ON CONFLICT (discord_id, billing_period) DO UPDATE
            SET status = 'sending',
                attempt_count = invoice_dispatches.attempt_count + 1,
                last_attempt_at = now(),
                last_error = NULL,
                updated_at = now()
            WHERE invoice_dispatches.status = 'failed'
               OR ($3 AND invoice_dispatches.status = 'sent')
               OR ($4 AND (
                   invoice_dispatches.status = 'sending'
                   AND invoice_dispatches.last_attempt_at
                       < now() - $5::interval
               ))
            RETURNING discord_id
            """,
            discord_id,
            billing_period,
            retry_sent,
            recover_stale_sending,
            INVOICE_SEND_LEASE,
        )
    return row is not None


async def _mark_invoice_sent(pool, discord_id: str, billing_period) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE invoice_dispatches
            SET status = 'sent',
                sent_at = now(),
                last_error = NULL,
                updated_at = now()
            WHERE discord_id = $1 AND billing_period = $2
              AND status = 'sending'
            """,
            discord_id,
            billing_period,
        )


async def _mark_invoice_failed(
    pool,
    discord_id: str,
    billing_period,
    error: Exception,
) -> None:
    error_text = str(error).strip() or error.__class__.__name__
    # Keep the persisted error useful without allowing an API response or
    # exception with an unusual size to grow the dispatch row indefinitely.
    error_text = error_text[:2000]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE invoice_dispatches
            SET status = 'failed',
                last_error = $3,
                updated_at = now()
            WHERE discord_id = $1 AND billing_period = $2
              AND status = 'sending'
            """,
            discord_id,
            billing_period,
            error_text,
        )


def _next_billing_period(billing_period: date) -> date:
    """Return the first day of the month after ``billing_period``."""
    return (billing_period.replace(day=28) + timedelta(days=4)).replace(day=1)


async def _fetch_invoice_user(pool, discord_id: str, billing_period):
    """Load the invoice totals used by both scheduled and manual delivery."""
    next_period = _next_billing_period(billing_period)
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                u.discord_id,
                u.finance_channel_id,
                COUNT(t.id) FILTER (
                    WHERE t.created_at >= $2 AND t.created_at < $3
                ) AS chapter_count,
                COALESCE(SUM(t.amount_cents) FILTER (
                    WHERE t.created_at >= $2 AND t.created_at < $3
                ), 0) AS period_total,
                COALESCE(SUM(t.amount_cents) FILTER (
                    WHERE t.status = 'unpaid'
                ), 0) AS unpaid_total
            FROM users u
            LEFT JOIN transactions t ON t.user_id = u.discord_id
            WHERE u.discord_id = $1
            GROUP BY u.discord_id, u.finance_channel_id
            """,
            discord_id,
            billing_period,
            next_period,
        )


async def _deliver_invoice(
    bot: hikari.GatewayBot,
    pool,
    user_row,
    billing_period: date,
    *,
    retry_sent: bool = False,
    recover_stale_sending: bool = True,
    requested_by: str | None = None,
    previous_status: str | None = None,
    rate_limiter=None,
) -> str:
    """Claim, send, and persist one invoice delivery.

    ``requested_by`` identifies an administrator-initiated retry. The audit
    row is written only after the dispatch is atomically claimed, so an
    interaction that lost a concurrent claim cannot create a false audit
    entry.
    """
    discord_id = str(user_row["discord_id"])
    period_str = billing_period.strftime("%Y-%m")
    claimed = await _claim_invoice_dispatch(
        pool,
        discord_id,
        billing_period,
        retry_sent=retry_sent,
        recover_stale_sending=recover_stale_sending,
    )
    if not claimed:
        return "skipped"

    if requested_by is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO invoice_dispatch_audit (
                        discord_id, billing_period, action, from_status,
                        to_status, changed_by
                    )
                    VALUES ($1, $2, 'retry', $3, 'sending', $4)
                    """,
                    discord_id,
                    billing_period,
                    previous_status or "failed",
                    requested_by,
                )
        except Exception as error:
            await _mark_invoice_failed(pool, discord_id, billing_period, error)
            raise

    try:
        from hikari.impl.special_endpoints import ContainerComponentBuilder

        cnt = user_row["chapter_count"] or 0
        total = user_row["period_total"] or 0
        unpaid = user_row["unpaid_total"] or 0
        container = ContainerComponentBuilder(id=1, accent_color=0xF1C40F)
        container.add_text_display(f"## 🧾 Monthly Invoice — {period_str}")
        container.add_separator(divider=True)
        container.add_text_display(
            f"**Chapters last month:** `{cnt}`\n"
            f"**Earned last month:** `{format_cents_as_dollars(total)}`\n"
            f"**Total unpaid:** `{format_cents_as_dollars(unpaid)}`"
        )
        container.add_separator(divider=True)
        container.add_text_display(
            "*Auto-generated on 1st of month. Contact admin for payout via "
            "Admin Dashboard → Finance & Payouts.*"
        )

        if rate_limiter is None:
            await bot.rest.create_message(
                int(user_row["finance_channel_id"]),
                content=f"<@{discord_id}>",
            )
            await bot.rest.create_message(
                int(user_row["finance_channel_id"]),
                components=[container],
                flags=hikari.MessageFlag.IS_COMPONENTS_V2,
            )
        else:
            send_semaphore, send_lock, send_state = rate_limiter
            async with send_semaphore:
                async with send_lock:
                    elapsed = time.monotonic() - send_state["last"]
                    if send_state["last"] and elapsed < INVOICE_MIN_SEND_INTERVAL:
                        await asyncio.sleep(INVOICE_MIN_SEND_INTERVAL - elapsed)
                    send_state["last"] = time.monotonic()
                await bot.rest.create_message(
                    int(user_row["finance_channel_id"]),
                    content=f"<@{discord_id}>",
                )
                await bot.rest.create_message(
                    int(user_row["finance_channel_id"]),
                    components=[container],
                    flags=hikari.MessageFlag.IS_COMPONENTS_V2,
                )
        await _mark_invoice_sent(pool, discord_id, billing_period)
        return "sent"
    except Exception as error:
        try:
            await _mark_invoice_failed(pool, discord_id, billing_period, error)
        except Exception:
            logger.exception(
                "Could not persist failed invoice dispatch for %s (%s)",
                discord_id,
                period_str,
            )
        logger.warning(
            "Invoice to %s failed for %s (attempt will be retried): %s",
            discord_id,
            period_str,
            error,
        )
        return "failed"


async def get_failed_invoice_dispatches(pool, limit: int = 25):
    """Return failed invoice deliveries for the finance dashboard."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                d.discord_id,
                d.billing_period,
                d.status,
                d.attempt_count,
                d.last_error,
                d.updated_at
            FROM invoice_dispatches d
            JOIN users u ON u.discord_id = d.discord_id
            WHERE d.status = 'failed'
            ORDER BY d.billing_period DESC, d.updated_at DESC
            LIMIT $1
            """,
            limit,
        )


async def retry_invoice_dispatch(
    bot: hikari.GatewayBot,
    discord_id: str,
    billing_period: date,
    requested_by: str,
) -> str:
    """Explicitly retry one failed (or intentionally selected sent) dispatch."""
    pool = get_pool()
    user_row = await _fetch_invoice_user(pool, discord_id, billing_period)
    if user_row is None:
        return "missing_user"
    if not user_row["finance_channel_id"]:
        return "missing_channel"

    async with pool.acquire() as conn:
        previous_status = await conn.fetchval(
            """
            SELECT status
            FROM invoice_dispatches
            WHERE discord_id = $1 AND billing_period = $2
            """,
            discord_id,
            billing_period,
        )
    if previous_status not in {"failed", "sent"}:
        return "not_retryable"

    return await _deliver_invoice(
        bot,
        pool,
        user_row,
        billing_period,
        retry_sent=True,
        recover_stale_sending=False,
        requested_by=requested_by,
        previous_status=previous_status,
    )


async def send_monthly_invoices(
    bot: hikari.GatewayBot,
    *,
    retry_sent: bool = False,
):
    """Send invoices for the previous billing month.

    The scheduled call leaves already-sent invoices alone. Callers performing
    an explicit administrator retry may pass ``retry_sent=True`` to reclaim
    successful dispatches intentionally.
    """
    try:
        tz = ZoneInfo(MONTHLY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    # Run on days 1-3: day 1 sends fresh invoices, days 2-3 retry failures only
    if now.day > 3:
        return
    # On day 2-3, only retry previously failed invoices (skip already-sent)
    is_retry_window = now.day > 1
    # Previous month period
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first_previous_month = (first_this_month - timedelta(days=1)).replace(day=1)
    period_str = first_previous_month.strftime("%Y-%m")
    billing_period = first_previous_month.date()

    pool = get_pool()
    async with pool.acquire() as conn:
        if is_retry_window:
            # Days 2-3: only retry previously failed invoices
            users = await conn.fetch(
                """
                SELECT
                    u.discord_id,
                    u.finance_channel_id,
                    COUNT(t.id) FILTER (
                        WHERE t.created_at >= $1 AND t.created_at < $2
                    ) AS chapter_count,
                    COALESCE(SUM(t.amount_cents) FILTER (
                        WHERE t.created_at >= $1 AND t.created_at < $2
                    ), 0) AS period_total,
                    COALESCE(SUM(t.amount_cents) FILTER (
                        WHERE t.status = 'unpaid'
                    ), 0) AS unpaid_total
                FROM users u
                LEFT JOIN transactions t ON t.user_id = u.discord_id
                INNER JOIN invoice_dispatches id
                    ON id.discord_id = u.discord_id
                    AND id.billing_period = $3
                    AND id.status = 'failed'
                WHERE u.finance_channel_id IS NOT NULL
                GROUP BY u.discord_id, u.finance_channel_id
                """,
                first_previous_month, first_this_month, billing_period,
            )
            logger.info("Invoice retry window (day %d): %d failed invoices to retry", now.day, len(users))
        else:
            users = await conn.fetch(
                """
                SELECT
                    u.discord_id,
                    u.finance_channel_id,
                    COUNT(t.id) FILTER (
                        WHERE t.created_at >= $1 AND t.created_at < $2
                    ) AS chapter_count,
                    COALESCE(SUM(t.amount_cents) FILTER (
                        WHERE t.created_at >= $1 AND t.created_at < $2
                    ), 0) AS period_total,
                    COALESCE(SUM(t.amount_cents) FILTER (
                        WHERE t.status = 'unpaid'
                    ), 0) AS unpaid_total
                FROM users u
                LEFT JOIN transactions t ON t.user_id = u.discord_id
                WHERE (u.is_registered = TRUE OR EXISTS(SELECT 1 FROM transactions t2 WHERE t2.user_id = u.discord_id AND t2.status = 'unpaid'))
                  AND u.finance_channel_id IS NOT NULL
                GROUP BY u.discord_id, u.finance_channel_id
                """,
                first_previous_month, first_this_month,
            )

    send_semaphore = asyncio.Semaphore(INVOICE_SEND_CONCURRENCY)
    send_lock = asyncio.Lock()
    send_state = {"last": 0.0}
    # DB claim concurrency limit to avoid pool exhaustion (pool max 20)
    db_semaphore = asyncio.Semaphore(10)

    async def send_invoice(user_row) -> str | None:
        cnt = user_row["chapter_count"] or 0
        unpaid = user_row["unpaid_total"] or 0
        if cnt == 0 and unpaid == 0:
            return None
        discord_id = str(user_row["discord_id"])
        try:
            async with db_semaphore:
                result = await _deliver_invoice(
                    bot,
                    pool,
                    user_row,
                    billing_period,
                    retry_sent=retry_sent,
                    rate_limiter=(send_semaphore, send_lock, send_state),
                )
        except Exception:
            logger.exception(
                "Could not claim invoice dispatch for %s (%s)",
                discord_id,
                period_str,
            )
            return discord_id
        return discord_id if result == "failed" else None

    results = []
    # Smaller batch to keep peak DB connections under pool max (20)
    effective_batch = min(INVOICE_SEND_BATCH_SIZE, 20)
    for start in range(0, len(users), effective_batch):
        results.extend(
            await asyncio.gather(
                *(send_invoice(user) for user in users[start:start + effective_batch])
            )
        )
    failures = [user_id for user_id in results if user_id is not None]
    if failures:
        logger.warning(
            "Monthly invoice run completed with partial failures: %s/%s sends failed (members: %s)",
            len(failures), len(users), ", ".join(failures[:10]),
        )

# Run daily at 00:05 — check if day==1 inside
@loader.task(lightbulb.uniformtrigger(86400))
async def monthly_invoice_task() -> None:
    try:
        # Need bot instance — get via hikari.GatewayBot? loader doesn't give bot easily.
        # We try to get from lightbulb client
        from bot.core import lb_client
        bot = None
        if lb_client:
            # lightbulb.Client has .app
            try:
                bot = lb_client.app  # type: ignore
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        if bot:
            await send_monthly_invoices(bot)  # type: ignore
    except Exception as e:
        logger.error(f"Monthly invoice error: {e}")
