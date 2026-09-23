"""Persistent background task queue for Drive syncs — avoids lost jobs on restart."""
import asyncio
import logging
import json

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None

async def init_queue(pool):
    """Ensure background_jobs table exists and restore pending jobs."""
    global _queue
    _queue = asyncio.Queue()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE SKIP LOCKED prevents duplicate pickup across multiple instances
            rows = await conn.fetch(
                """SELECT id, job_type, payload FROM background_jobs
                   WHERE status IN ('pending','running')
                   ORDER BY created_at ASC LIMIT 100
                   FOR UPDATE SKIP LOCKED"""
            )
            for r in rows:
                await _queue.put((r["id"], r["job_type"], dict(r["payload"])))
                # Mark running as pending again
                await conn.execute(
                    "UPDATE background_jobs SET status='pending', updated_at=now() WHERE id=$1",
                    r["id"],
                )
            if rows:
                logger.info(f"Restored {len(rows)} background jobs from DB")

def get_queue() -> asyncio.Queue:
    if _queue is None:
        raise RuntimeError("Task queue not initialized")
    return _queue

async def enqueue_job(pool, job_type: str, payload: dict):
    """Persist job to DB and enqueue to memory."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO background_jobs (job_type, payload) VALUES ($1, $2::jsonb) RETURNING id",
            job_type, json.dumps(payload),
        )
        job_id = row["id"]
    await get_queue().put((job_id, job_type, payload))
    logger.info(f"Enqueued {job_type} job {job_id}: {payload}")
    return job_id

async def _process_sync_job(payload: dict):
    manga_id = payload.get("manga_id")
    drive_id = payload.get("drive_folder_id")
    if not manga_id or not drive_id:
        logger.warning(f"Invalid sync payload: {payload}")
        return
    try:
        from bot.core import get_drive_service
        from bot.database.connection import get_pool
        from bot.services.webhook_sender import update_manga_webhook
        from bot.core import get_bot
        ds = get_drive_service()
        progress = await ds.deep_sync_manga(drive_id)
        pool = get_pool()
        from decimal import Decimal
        PROTECTED_STATUSES = ('published', 'pending_review', 'rejected')
        async with pool.acquire() as conn:
            manga_row = await conn.fetchrow("SELECT start_chapter FROM mangas WHERE id = $1", manga_id)
            start_chapter = manga_row["start_chapter"] if manga_row and manga_row["start_chapter"] is not None else 0
            start_dec = Decimal(str(start_chapter))

            async with conn.transaction():
                for ch_num, data in progress.items():
                    try:
                        if Decimal(str(ch_num)) < start_dec:
                            continue
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                    
                    new_status = data["status"]
                    new_fid = data["id"]
                    existing = await conn.fetchrow(
                        "SELECT id, status, drive_folder_id FROM chapters WHERE manga_id = $1 AND chapter_number = $2 FOR UPDATE",
                        manga_id, ch_num,
                    )
                    if existing:
                        if existing["status"] in PROTECTED_STATUSES:
                            if existing["drive_folder_id"] != new_fid:
                                await conn.execute(
                                    "UPDATE chapters SET drive_folder_id = $1, updated_at = now() WHERE id = $2",
                                    new_fid, existing["id"],
                                )
                        else:
                            if existing["status"] != new_status:
                                await conn.execute(
                                    """UPDATE chapters
                                       SET status = $1, drive_folder_id = $2, assigned_to = NULL, assigned_at = NULL, updated_at = now()
                                       WHERE id = $3""",
                                    new_status, new_fid, existing["id"],
                                )
                            elif existing["drive_folder_id"] != new_fid:
                                await conn.execute(
                                    "UPDATE chapters SET drive_folder_id = $1, updated_at = now() WHERE id = $2",
                                    new_fid, existing["id"],
                                )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO chapters (manga_id, chapter_number, drive_folder_id, status)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (manga_id, chapter_number) DO NOTHING
                            """,
                            manga_id, ch_num, new_fid, new_status,
                        )
        try:
            bot = get_bot()
            await update_manga_webhook(bot, manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        logger.info(f"Background sync done for manga {manga_id}: {len(progress)} chapters")
    except Exception as e:
        logger.warning(f"Background sync failed for {payload}: {e}")
        raise

MAX_ATTEMPTS = 3

async def _notify_job_failure(job_id: int, job_type: str, payload: dict, error_msg: str):
    """Notify via Discord when a background job has permanently failed."""
    logger.error(f"[TaskQueue] Job {job_id} ({job_type}) PERMANENTLY FAILED after max attempts: {error_msg}")
    manga_id = payload.get("manga_id")
    if manga_id:
        try:
            import hikari
            from bot.core import get_bot
            from bot.services.webhook_sender import _get_webhook_credentials
            bot = get_bot()
            if not bot:
                return
            wh_id, wh_token, ch_id = await _get_webhook_credentials(manga_id)
            embed = hikari.Embed(
                title="⚠️ Background Task Failed Permanently",
                description=f"**Job ID:** {job_id}\n**Type:** `{job_type}`\n**Error:** {error_msg[:300]}",
                color=0xED4245,
            )
            if wh_id and wh_token:
                await bot.rest.execute_webhook(
                    hikari.Snowflake(wh_id),
                    wh_token,
                    embed=embed,
                )
            elif ch_id:
                await bot.rest.create_message(hikari.Snowflake(ch_id), embed=embed)
        except Exception as e:
            logger.debug(f"Could not deliver job failure alert to Discord: {e}")

async def _retry_later(job_id: int, job_type: str, payload: dict, delay_seconds: float):
    """Wait delay_seconds and push job back to queue."""
    await asyncio.sleep(delay_seconds)
    try:
        await get_queue().put((job_id, job_type, payload))
    except Exception as e:
        logger.warning(f"Failed to re-enqueue job {job_id}: {e}")

async def worker_loop(pool):
    q = get_queue()
    while True:
        job_id, job_type, payload = await q.get()
        attempts = 0
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE background_jobs SET status='running', attempts=attempts+1, updated_at=now() WHERE id=$1 RETURNING attempts",
                    job_id,
                )
                if row:
                    attempts = row["attempts"]
            if job_type == "drive_sync":
                await _process_sync_job(payload)
            else:
                logger.warning(f"Unknown job_type {job_type}")
            async with pool.acquire() as conn:
                await conn.execute("UPDATE background_jobs SET status='done', updated_at=now() WHERE id=$1", job_id)
        except Exception as e:
            logger.exception(f"Job {job_id} failed on attempt {attempts}: {e}")
            try:
                async with pool.acquire() as conn:
                    if attempts < MAX_ATTEMPTS:
                        await conn.execute("UPDATE background_jobs SET status='pending', updated_at=now() WHERE id=$1", job_id)
                        delay = min(60, 2 ** attempts)
                        asyncio.create_task(_retry_later(job_id, job_type, payload, delay))
                    else:
                        await conn.execute("UPDATE background_jobs SET status='failed', updated_at=now() WHERE id=$1", job_id)
                        asyncio.create_task(_notify_job_failure(job_id, job_type, payload, str(e)))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        finally:
            q.task_done()

async def start_worker(pool):
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(worker_loop(pool))
    logger.info("Background task worker started")

async def stop_worker():
    global _worker_task
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
