# Pricing — single source of truth, DB-configurable with in-memory fallback
FLAT_RATE_CENTS = 85  # default for TL/PR
_DEFAULTS = {
    "tl_cents": 85,
    "pr_cents": 85,
    "ed_under_30": 150,
    "ed_30_44": 200,
    "ed_45_plus": 300,
}

# Simple in-process cache to avoid hitting DB on every /done
_cache: dict[str, int] = {}
_cache_ts: float = 0
CACHE_TTL = 60  # seconds

def calculate_editor_price(images_count: int) -> int:
    """Calculate editor (ED) price based on image count — in cents (fallback)."""
    if images_count < 30:
        return _cache.get("ed_under_30", _DEFAULTS["ed_under_30"])
    elif images_count < 45:
        return _cache.get("ed_30_44", _DEFAULTS["ed_30_44"])
    else:
        return _cache.get("ed_45_plus", _DEFAULTS["ed_45_plus"])

def get_price_for_role(role: str, images_count: int = 0) -> int:
    """Sync version — uses cache/defaults, no DB I/O. CO-* assistants earn base rates."""
    base = role.replace("CO-", "") if role.startswith("CO-") else role
    if base == "TL":
        return _cache.get("tl_cents", _DEFAULTS["tl_cents"])
    elif base == "PR":
        return _cache.get("pr_cents", _DEFAULTS["pr_cents"])
    elif base == "ED":
        return calculate_editor_price(images_count)
    else:
        raise ValueError(f"Unknown role: {role}")

async def refresh_pricing_cache(pool=None) -> None:
    """Refresh cache from pricing_config table if available."""
    global _cache_ts
    import time
    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return
    try:
        from bot.database.connection import get_pool
        if pool is None:
            pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM pricing_config")
            for r in rows:
                _cache[r["key"]] = r["value"]
            _cache_ts = time.time()
    except Exception:
        # DB not ready or table missing — use defaults
        pass

async def get_price_for_role_async(role: str, images_count: int = 0, pool=None) -> int:
    """Async version — refreshes cache then returns price."""
    await refresh_pricing_cache(pool)
    return get_price_for_role(role, images_count)

def format_cents_as_dollars(cents: int) -> str:
    """Convert cents to dollar string."""
    dollars, remainder = divmod(abs(cents), 100)
    formatted = f"${dollars}.{remainder:02d}"
    return f"-{formatted}" if cents < 0 else formatted

def invalidate_cache():
    global _cache_ts
    _cache.clear()
    _cache_ts = 0
