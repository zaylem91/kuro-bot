from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Config helpers (read lazily so tests / import without env still work)
# ---------------------------------------------------------------------------

def _dashboard_token() -> str:
    """DASHBOARD_TOKEN only — no fallback to HEALTH_TOKEN (security fix)."""
    try:
        import bot.config
        t = getattr(bot.config, "DASHBOARD_TOKEN", "") or os.getenv("DASHBOARD_TOKEN", "")
        return t.strip() if t else ""
    except Exception:
        return ""


def _allowed_ips() -> set[str]:
    try:
        from bot.config import DASHBOARD_ALLOWED_IPS as _ips
        return {p.strip() for p in (_ips or "").split(",") if p.strip()}
    except Exception:
        return set()


def _cors_origin(request: web.Request) -> str:
    try:
        from bot.config import DASHBOARD_CORS_ORIGINS as _o
        configured = (_o or "").strip()
    except Exception:
        configured = ""
    if not configured or configured == "*":
        return "*"
    origin = request.headers.get("Origin", "")
    allowed = {p.strip() for p in configured.split(",") if p.strip()}
    if origin in allowed:
        return origin
    return ""  # no CORS header -> browser blocks cross-origin


# ---------------------------------------------------------------------------
# Rate limiting + brute-force protection (in-memory, per worker process)
# ---------------------------------------------------------------------------

# ip -> deque[monotonic timestamps] for general rate limiting
_REQ_LOG: dict[str, deque] = {}
# ip -> {"fails": int, "ban_until": float}
_AUTH_FAILS: dict[str, dict] = {}

GET_LIMIT = 120          # GET requests per 60s per IP
WRITE_LIMIT = 30         # POST/PUT/DELETE per 60s per IP
MAX_AUTH_FAILS = 5
BAN_SECONDS = 600        # 10 minutes

# Temporary read-only dashboard links. These are intentionally in-memory:
# they expire automatically and are invalidated when the bot restarts.
_SHARE_LINKS: dict[str, float] = {}
SHARE_MINUTES = (15, 60, 1440)
SHARE_MAX_SECONDS = 24 * 60 * 60


# Active login sessions (session_token -> expiry monotonic)
_SESSIONS: dict[str, float] = {}
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _trusted_proxies() -> set[str]:
    try:
        from bot.config import DASHBOARD_TRUSTED_PROXIES as _tp
        configured = _tp
    except Exception:
        import os
        configured = os.getenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1,::1")
    return {p.strip() for p in (configured or "").split(",") if p.strip()} or {"127.0.0.1", "::1"}


def _client_ip(request: web.Request) -> str:
    remote = (request.remote or "unknown")[:64]
    # Only respect X-Forwarded-For if connection originates from a trusted reverse proxy
    if remote in _trusted_proxies():
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return remote


def _cleanup_sessions() -> None:
    now = time.monotonic()
    for sid, exp in list(_SESSIONS.items()):
        if exp <= now:
            _SESSIONS.pop(sid, None)


def _create_session() -> str:
    _cleanup_sessions()
    sid = secrets.token_hex(32)
    _SESSIONS[sid] = time.monotonic() + SESSION_TTL_SECONDS
    return sid


def _valid_session(sid: str) -> bool:
    if not sid:
        return False
    exp = _SESSIONS.get(sid)
    if exp is None:
        return False
    if exp <= time.monotonic():
        _SESSIONS.pop(sid, None)
        return False
    return True


def _is_banned(ip: str) -> bool:
    rec = _AUTH_FAILS.get(ip)
    if not rec:
        return False
    ban_until = rec.get("ban_until", 0.0)
    if ban_until <= 0.0:
        return False
    if ban_until > time.monotonic():
        return True
    # Ban was set and has now expired -> clean up
    _AUTH_FAILS.pop(ip, None)
    return False


def _record_auth_fail(ip: str) -> None:
    rec = _AUTH_FAILS.setdefault(ip, {"fails": 0, "ban_until": 0.0})
    rec["fails"] += 1
    if rec["fails"] >= MAX_AUTH_FAILS:
        rec["ban_until"] = time.monotonic() + BAN_SECONDS
        logger.warning("[DASHBOARD] IP %s banned for %ss after %d bad auth attempts",
                       ip, BAN_SECONDS, rec["fails"])


def _clear_auth_fails(ip: str) -> None:
    _AUTH_FAILS.pop(ip, None)


def _check_rate_limit(ip: str, is_write: bool) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds)."""
    now = time.monotonic()
    window = 60.0
    limit = WRITE_LIMIT if is_write else GET_LIMIT
    dq = _REQ_LOG.setdefault(ip, deque())
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= limit:
        retry = int(dq[0] + window - now) + 1
        return False, max(retry, 1)
    dq.append(now)

    # Periodic cleanup to prevent unbounded memory growth if hit by many unique IPs
    if len(_REQ_LOG) > 500:
        for k in list(_REQ_LOG.keys()):
            if not _REQ_LOG[k] or _REQ_LOG[k][-1] < now - window:
                _REQ_LOG.pop(k, None)

    return True, 0


# ---------------------------------------------------------------------------
# Auth + response helpers
# ---------------------------------------------------------------------------

def _extract_token(request: web.Request) -> str:
    """Check Authorization header, X-Dashboard-Token, and HttpOnly cookies."""
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()[:256]
    header_tok = request.headers.get("X-Dashboard-Token", "").strip()[:256]
    if header_tok: return header_tok
    return (request.cookies.get("dash_auth") or "").strip()[:256]


def _share_token(request: web.Request) -> str:
    """Read only the HttpOnly share cookie; never accept share tokens on API URLs."""
    return (request.cookies.get("dash_share") or "").strip()[:256]


def _valid_share(request: web.Request) -> bool:
    token = _share_token(request)
    if not token:
        return False
    expires_at = _SHARE_LINKS.get(token)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        _SHARE_LINKS.pop(token, None)
        return False
    return True


def _cleanup_share_links() -> None:
    now = time.monotonic()
    for token, expires_at in list(_SHARE_LINKS.items()):
        if expires_at <= now:
            _SHARE_LINKS.pop(token, None)


def _check_auth(request: web.Request) -> tuple[bool, str]:
    """Return (ok, reason)."""
    token = _dashboard_token()
    if not token or len(token) < 16:
        return False, "dashboard disabled on server (token not configured)"
    if _is_banned(_client_ip(request)):
        return False, "too many failed attempts — try again later"

    # 1. Check server-side session cookie
    cookie_auth = (request.cookies.get("dash_auth") or "").strip()
    if cookie_auth and _valid_session(cookie_auth):
        return True, ""

    # 2. Check token passed via Bearer, header, or direct cookie
    provided = _extract_token(request)
    if not provided:
        return False, "missing credentials"
    if hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")):
        return True, ""
    return False, "invalid credentials"


def _check_ip(request: web.Request) -> bool:
    allowed = _allowed_ips()
    if not allowed:
        return True
    return _client_ip(request) in allowed


def _sec_headers(response: web.Response, is_html: bool = False) -> web.Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    if is_html:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline' https://replit-cdn.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' http: https: wss:; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
    return response


def _cors_headers(request: web.Request, response: web.Response) -> web.Response:
    origin = _cors_origin(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Dashboard-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


def _json(request: web.Request, data, status: int = 200) -> web.Response:
    resp = web.json_response(data, status=status)
    _sec_headers(resp)
    return _cors_headers(request, resp)


def _err(request: web.Request, msg: str, status: int) -> web.Response:
    return _json(request, {"error": msg}, status=status)


async def _guard(request: web.Request, write: bool = False,
                 *, allow_share: bool = True):
    """Shared pre-handler: IP allowlist -> rate limit -> auth. Returns None if OK."""
    ip = _client_ip(request)
    if not _check_ip(request):
        logger.warning("[DASHBOARD] blocked IP not in allowlist: %s %s", ip, request.path)
        return _err(request, "forbidden", 403)
    ok, retry = _check_rate_limit(ip, is_write=write)
    if not ok:
        resp = _err(request, "rate limited — slow down", 429)
        resp.headers["Retry-After"] = str(retry)
        return resp
    if _is_banned(ip):
        resp = _err(request, "too many failed attempts — try again later", 429)
        resp.headers["Retry-After"] = str(BAN_SECONDS)
        return resp
    if allow_share and not write and _valid_share(request):
        return None
    authed, reason = _check_auth(request)
    if not authed:
        # "disabled" is a server misconfiguration, not an auth attack
        if reason.startswith("dashboard disabled"):
            return _err(request, reason, 503)
        _record_auth_fail(ip)
        # Generic message on purpose (don't reveal which part failed)
        resp = _err(request, "unauthorized", 401)
        resp.headers["WWW-Authenticate"] = 'Bearer realm="dashboard"'
        return resp
    _clear_auth_fails(ip)
    return None


def _audit(request: web.Request, action: str, detail: str = "") -> None:
    logger.warning("[DASHBOARD] ip=%s action=%s %s", _client_ip(request), action, detail[:200])


# ---------------------------------------------------------------------------
# Serialization / validation helpers
# ---------------------------------------------------------------------------

def _num(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _iso(v):
    try:
        return v.isoformat() if v is not None else None
    except Exception:
        return str(v) if v is not None else None


def _row_chapter(r) -> dict:
    return {
        "id": r["id"],
        "manga_id": r["manga_id"],
        "manga_name": r.get("manga_name"),
        "chapter_number": _num(r["chapter_number"]),
        "status": r["status"],
        "assigned_to": r["assigned_to"],
        "assigned_at": _iso(r.get("assigned_at")),
        "images_count": r.get("images_count"),
        "updated_at": _iso(r.get("updated_at")),
    }


_DISCORD_ID_RE = re.compile(r"^\d{17,20}$")
MANGA_STATUSES = {"ongoing", "paused", "dropped"}
CHAPTER_STATUSES = {"pending_tl", "pending_pr", "pending_ed",
                    "pending_review", "published", "rejected"}
PRICE_KEYS = {"tl_cents", "pr_cents", "ed_under_30", "ed_30_44", "ed_45_plus"}


def _valid_discord_id(v) -> bool:
    return isinstance(v, str) and bool(_DISCORD_ID_RE.match(v.strip()))


def _sanitize_name(name: str, limit: int = 200) -> str:
    name = (name or "").strip()[:limit]
    # neutralize mass mentions if a name is ever rendered in Discord
    return name.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def _get_pool():
    try:
        from bot.database.connection import get_pool
        return get_pool()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# Dashboard UI lives in a separate folder at the project root.
# bot/services/dashboard_api.py -> bot/ -> project-root -> dashboard/
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"


def _serve_static(request: web.Request, filename: str, content_type: str,
                  *, html: bool = False, is_binary: bool = False) -> web.Response:
    # Filenames are fixed constants from the router (no user input -> no traversal).
    path = _DASHBOARD_DIR / filename
    if not path.is_file():
        return _err(request, f"dashboard/{filename} not found on server", 404)
    if is_binary:
        data = path.read_bytes()
        resp = web.Response(body=data, content_type=content_type)
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(request, f"dashboard/{filename} is binary", 400)
        resp = web.Response(text=text, content_type=content_type, charset="utf-8")
    _sec_headers(resp, is_html=html)
    if not html:
        # Cache static CSS, JS, images, and locales for 24h with stale-while-revalidate
        resp.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=3600"
    return _cors_headers(request, resp)

