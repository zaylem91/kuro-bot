import asyncpg
from pathlib import Path

_pool: asyncpg.Pool | None = None

async def init_db(database_url: str) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    # Tuned pool: avoid default 10/10 saturation during invoice batch (100 concurrent) + Drive semaphore (5) + autocomplete bursts
    # max_size 20 still safe for small Postgres; command_timeout prevents hanging queries from blocking bot
    _pool = await asyncpg.create_pool(
        database_url,
        min_size=5,
        max_size=20,
        command_timeout=30,
        statement_cache_size=200,
        max_queries=50000,
        max_inactive_connection_lifetime=300,
    )
    try:
        from bot.database.migrate import run_migrations
        await run_migrations(_pool)
    except Exception:
        await _pool.close()
        _pool = None
        raise
    return _pool

def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized.")
    return _pool


async def acquire_with_timeout(timeout: float = 5.0):
    """Acquire a connection with timeout to prevent indefinite waits.
    
    Use in latency-sensitive paths (slash commands, autocomplete) where
    waiting forever for a free connection would cause Discord's 3s
    interaction timeout to expire.
    """
    import asyncio
    pool = get_pool()
    try:
        return await asyncio.wait_for(pool.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Could not acquire DB connection within {timeout}s — "
            f"pool exhausted (size={pool.get_size()}, free={pool.get_idle_size()})"
        )


async def close_db() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            await pool.close()
        except RuntimeError as e:
            if "Event loop is closed" not in str(e):
                raise

