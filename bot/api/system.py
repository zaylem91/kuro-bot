from __future__ import annotations

import logging
import re
import time
import secrets
import hmac
from aiohttp import web

from bot.api.core import (
    logger,
    _SHARE_LINKS, _SESSIONS, SESSION_TTL_SECONDS, BAN_SECONDS,
    SHARE_MINUTES, SHARE_MAX_SECONDS,
    _cleanup_share_links, _valid_share, _serve_static,
    _err, _json, _guard, _audit, _num, _iso, _row_chapter,
    _get_pool, _sanitize_name, _valid_discord_id, _client_ip,
    _check_ip, _check_rate_limit, _dashboard_token, _is_banned,
    _record_auth_fail, _create_session, _sec_headers, _cors_headers,
    MANGA_STATUSES, CHAPTER_STATUSES, PRICE_KEYS
)

async def dashboard_page(request: web.Request) -> web.Response:
    """Serve the control panel UI. Public (no secrets inside);
    all data calls still require the token header."""
    _cleanup_share_links()
    link_token = (request.query.get("share", "") or "").strip()[:256]
    if link_token and link_token in _SHARE_LINKS:
        expires_at = _SHARE_LINKS[link_token]
        if expires_at > time.monotonic():
            # Remove the token from the address bar after setting an HttpOnly
            # cookie. This limits accidental sharing via browser history/referrer.
            response = web.HTTPFound("/dashboard/")
            response.set_cookie(
                "dash_share", link_token, httponly=True, samesite="Lax",
                max_age=max(1, int(expires_at - time.monotonic())),
                path="/",
            )
            return response
        _SHARE_LINKS.pop(link_token, None)

    response = _serve_static(request, "index.html", "text/html", html=True)
    if _valid_share(request):
        marker = "<script>window.DASH_SHARE_MODE=true;</script>"
        response.text = response.text.replace("</head>", marker + "</head>")
    return response

async def dashboard_redirect(request: web.Request) -> web.Response:
    """/dashboard -> /dashboard/ so the relative asset paths
    (style.css / app.js) resolve correctly both when hosted
    and when index.html is opened directly from disk (file://)."""
    resp = web.Response(status=301, headers={"Location": "/dashboard/"})
    _sec_headers(resp)
    return _cors_headers(request, resp)

async def dashboard_css(request: web.Request) -> web.Response:
    return _serve_static(request, "style.css", "text/css")

async def dashboard_js(request: web.Request) -> web.Response:
    return _serve_static(request, "app.js", "application/javascript")

async def dashboard_favicon(request: web.Request) -> web.Response:
    return _serve_static(request, "favicon.ico", "image/x-icon", is_binary=True)

async def dashboard_locale(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    if not re.fullmatch(r"^[a-zA-Z0-9_\-]+\.js$", filename):
        return _err(request, "invalid locale file", 400)
    return _serve_static(request, f"locales/{filename}", "application/javascript")

async def options_handler(request: web.Request) -> web.Response:
    resp = web.Response(status=204)
    _sec_headers(resp)
    return _cors_headers(request, resp)

async def api_login(request: web.Request) -> web.Response:
    try:
        ip = _client_ip(request)
        if not _check_ip(request):
            return _err(request, "forbidden", 403)
        ok, retry = _check_rate_limit(ip, is_write=False)
        if not ok:
            resp = _err(request, "rate limited - slow down", 429)
            resp.headers["Retry-After"] = str(retry)
            return resp
            
        try:
            data = await request.json()
            provided = data.get("token", "")
        except Exception:
            provided = ""
            
        real_token = _dashboard_token()
        if not real_token or len(real_token) < 16:
            return _err(request, "dashboard disabled on server", 503)
            
        if _is_banned(ip):
            resp = _err(request, "too many failed attempts - try again later", 429)
            resp.headers["Retry-After"] = str(BAN_SECONDS)
            return resp
            
        if not provided or not hmac.compare_digest(provided.encode("utf-8"), real_token.encode("utf-8")):
            _record_auth_fail(ip)
            return _err(request, "invalid credentials", 401)
            
        session_id = _create_session()
        resp = _json(request, {"status": "ok"})
        proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        is_https = request.secure or proto == "https"
        resp.set_cookie(
            "dash_auth",
            session_id,
            httponly=True,
            samesite="Lax",
            secure=is_https,
            max_age=int(SESSION_TTL_SECONDS),
            path="/",
        )
        return resp
    except Exception as e:
        logger.exception("api_login error: %s", e)
        return _err(request, "Internal server error", 500)

async def api_logout(request: web.Request) -> web.Response:
    sid = (request.cookies.get("dash_auth") or "").strip()
    if sid:
        _SESSIONS.pop(sid, None)
    resp = _json(request, {"status": "ok"})
    resp.del_cookie("dash_auth", path="/")
    resp.del_cookie("dash_share", path="/")
    return resp

async def api_share_link(request: web.Request) -> web.Response:
    """Create a temporary read-only dashboard link for an authenticated admin."""
    denied = await _guard(request, allow_share=False)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        minutes = int(body.get("minutes", 60))
    except (TypeError, ValueError):
        return _err(request, "minutes must be 15, 60, or 1440", 400)
    if minutes not in SHARE_MINUTES:
        return _err(request, "minutes must be 15, 60, or 1440", 400)

    _cleanup_share_links()
    token = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + min(minutes * 60, SHARE_MAX_SECONDS)
    _SHARE_LINKS[token] = expires_at

    proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    if proto not in ("http", "https"):
        proto = "https" if request.secure else "http"
    host = request.headers.get("X-Forwarded-Host", request.host).split(",", 1)[0].strip()
    url = f"{proto}://{host}/dashboard/?share={token}"
    _audit(request, "share_link.create", f"minutes={minutes}")
    return _json(request, {
        "url": url,
        "expires_in": minutes * 60,
        "expires_at": int(time.time() + minutes * 60),
        "read_only": True,
    })

async def api_share_link_revoke(request: web.Request) -> web.Response:
    """Revoke one temporary share link before its natural expiry."""
    denied = await _guard(request, write=True, allow_share=False)
    if denied is not None:
        return denied
    token = request.match_info.get("token", "").strip()[:256]
    existed = _SHARE_LINKS.pop(token, None) is not None
    _audit(request, "share_link.revoke", f"existed={existed}")
    return _json(request, {"revoked": existed})

async def api_stats(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            manga_rows = await conn.fetch(
                "SELECT status, COUNT(*) AS c FROM mangas GROUP BY status")
            ch_rows = await conn.fetch(
                "SELECT status, COUNT(*) AS c FROM chapters GROUP BY status")
            staff_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE is_registered = TRUE")
            unpaid = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS s "
                "FROM transactions WHERE status='unpaid'")
            jobs = await conn.fetch(
                "SELECT status, COUNT(*) AS c FROM background_jobs GROUP BY status")
            recent = await conn.fetch(
                "SELECT h.action, h.stage, h.timestamp, c.chapter_number, m.manga_name "
                "FROM chapter_history h JOIN chapters c ON c.id=h.chapter_id "
                "JOIN mangas m ON m.id=c.manga_id "
                "ORDER BY h.timestamp DESC LIMIT 8")
        try:
            from bot.services import health as _h
            uptime = int(time.time() - _h._start_time)
        except Exception:
            uptime = -1
        return _json(request, {
            "uptime_seconds": uptime,
            "mangas_by_status": {r["status"]: r["c"] for r in manga_rows},
            "chapters_by_status": {r["status"]: r["c"] for r in ch_rows},
            "staff_registered": staff_count,
            "unpaid": {"count": unpaid["n"], "cents": unpaid["s"]},
            "jobs_by_status": {r["status"]: r["c"] for r in jobs},
            "recent_activity": [
                {"action": r["action"], "stage": r["stage"],
                 "manga": r["manga_name"],
                 "chapter": _num(r["chapter_number"]),
                 "at": _iso(r["timestamp"])} for r in recent
            ],
        })
    except Exception as e:
        logger.exception("[DASHBOARD] stats failed: %s", e)
        return _err(request, "internal error", 500)

async def api_jobs(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        limit = min(max(int(request.query.get("limit", "30")), 1), 100)
    except ValueError:
        return _err(request, "bad limit", 400)
    status = (request.query.get("status", "") or "").strip()
    try:
        async with pool.acquire() as conn:
            if status in ("pending", "running", "done", "failed"):
                rows = await conn.fetch(
                    "SELECT id, job_type, payload, status, attempts, created_at, updated_at "
                    "FROM background_jobs WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
                    status, limit)
            else:
                rows = await conn.fetch(
                    "SELECT id, job_type, payload, status, attempts, created_at, updated_at "
                    "FROM background_jobs ORDER BY created_at DESC LIMIT $1", limit)
            counts = await conn.fetch(
                "SELECT status, COUNT(*) AS c FROM background_jobs GROUP BY status")
        import json as _json_mod
        items = []
        for r in rows:
            try:
                payload = dict(r["payload"]) if r["payload"] else {}
            except Exception:
                try:
                    payload = _json_mod.loads(r["payload"])
                except Exception:
                    payload = {}
            # payload may contain only IDs — safe to return
            items.append({"id": r["id"], "job_type": r["job_type"],
                          "payload": payload, "status": r["status"],
                          "attempts": r["attempts"],
                          "created_at": _iso(r["created_at"]),
                          "updated_at": _iso(r["updated_at"])})
        return _json(request, {
            "counts": {c["status"]: c["c"] for c in counts},
            "items": items,
        })
    except Exception as e:
        logger.exception("[DASHBOARD] jobs failed: %s", e)
        return _err(request, "internal error", 500)

async def api_pricing_get(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM pricing_config")
        return _json(request, {r["key"]: r["value"] for r in rows})
    except Exception as e:
        logger.exception("[DASHBOARD] pricing get failed: %s", e)
        return _err(request, "internal error", 500)

async def api_pricing_put(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)
    key = str(body.get("key", "") or "").strip()
    try:
        value = int(body.get("value"))
    except (TypeError, ValueError):
        return _err(request, "value must be an integer (cents)", 400)
    if key not in PRICE_KEYS:
        return _err(request, f"key must be one of {sorted(PRICE_KEYS)}", 400)
    if not 0 <= value <= 10000:
        return _err(request, "value must be 0..10000 cents", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchval(
                    "SELECT value FROM pricing_config WHERE key=$1 FOR UPDATE", key)
                await conn.execute(
                    "INSERT INTO pricing_config (key, value) VALUES ($1,$2) "
                    "ON CONFLICT (key) DO UPDATE SET value=$2", key, value)
                await conn.execute(
                    "INSERT INTO price_audit (key, old_value, new_value, changed_by)"
                    " VALUES ($1,$2,$3,$4)", key, old, value, "dashboard")
        try:
            from bot.services.pricing import invalidate_cache
            invalidate_cache()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        _audit(request, "pricing.update", f"{key} {old}->{value}")
        return _json(request, {"key": key, "old_value": old, "value": value})
    except Exception as e:
        logger.exception("[DASHBOARD] pricing put failed: %s", e)
        return _err(request, "internal error", 500)

