"""Health endpoint + metrics — runs alongside bot for monitoring."""
import hmac
import logging
import os
import time
from aiohttp import web

logger = logging.getLogger(__name__)

_start_time = time.time()
_request_count = 0

def _check_auth(request) -> bool:
    """Optional token auth for health/metrics — if HEALTH_TOKEN is set, require it."""
    token = os.getenv("HEALTH_TOKEN", "").strip()
    if not token:
        try:
            from bot.config import HEALTH_TOKEN as cfg_token
            token = cfg_token
        except Exception:
            token = ""
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        auth = auth[7:].strip()
    if not auth:
        auth = request.headers.get("X-Health-Token", "")
    return bool(auth and hmac.compare_digest(auth.encode("utf-8"), token.encode("utf-8")))

async def health_handler(request):
    has_token = bool(os.getenv("HEALTH_TOKEN", "").strip())
    if not has_token:
        try:
            from bot.config import HEALTH_TOKEN as cfg_token
            has_token = bool(cfg_token)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    if not has_token:
        # No token configured: return minimal status without internal details
        return web.json_response({"status": "ok"})
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    global _request_count
    _request_count += 1
    uptime = int(time.time() - _start_time)
    # Try to get pool stats
    pool_stats = {}
    try:
        from bot.database.connection import get_pool
        pool = get_pool()
        pool_stats = {
            "pool_size": pool.get_size() if hasattr(pool, "get_size") else "unknown",
            "pool_free": pool.get_idle_size() if hasattr(pool, "get_idle_size") else "unknown",
        }
    except Exception:
        pool_stats = {"pool": "not initialized"}
    # Drive cache stats
    try:
        from bot.services.cache import drive_deep_sync_cache, workload_cache
        cache_stats = {
            "drive_cache_size": len(drive_deep_sync_cache._store) if drive_deep_sync_cache else 0,
            "workload_cache_size": len(workload_cache._store) if workload_cache else 0,
        }
    except Exception:
        cache_stats = {}
    return web.json_response({
        "status": "ok",
        "uptime_seconds": uptime,
        "requests_served": _request_count,
        "pool": pool_stats,
        "cache": cache_stats,
        "timestamp": int(time.time()),
    })

async def metrics_handler(request):
    if not _check_auth(request):
        return web.Response(text="unauthorized", status=401)
    uptime = int(time.time() - _start_time)
    text = f"""# HELP bot_uptime_seconds Bot uptime
# TYPE bot_uptime_seconds counter
bot_uptime_seconds {uptime}
# HELP bot_health_requests_total Health requests
# TYPE bot_health_requests_total counter
bot_health_requests_total {_request_count}
"""
    return web.Response(text=text, content_type="text/plain; version=0.0.4")

def create_app():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/metrics", metrics_handler)
    # The root preview URL should open the dashboard. Keep health monitoring
    # available explicitly at /health and /metrics.
    async def root_handler(request):
        raise web.HTTPFound("/dashboard/")

    app.router.add_get("/", root_handler)
    # Remote Dashboard (HTML panel + JSON API) — token-protected, see dashboard_api.py
    try:
        from bot.api.router import register_dashboard_routes
        register_dashboard_routes(app)
    except Exception as e:
        logger.warning(f"Dashboard API not registered: {e}")
    return app

async def start_health_server(port: int = 8080, host: str | None = None):
    try:
        port = int(os.getenv("HEALTH_PORT", str(port)))
    except Exception:
        port = 8080
    if host is None:
        host = os.getenv("HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if not host:
            try:
                from bot.config import HEALTH_HOST as cfg_host
                host = cfg_host
            except Exception:
                host = "127.0.0.1"
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        logger.info(f"Health server started on {host}:{port} (/health, /metrics)")
    except OSError as e:
        logger.warning(f"Health server failed to start on :{port}: {e}")
    return runner
