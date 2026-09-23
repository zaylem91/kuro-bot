from __future__ import annotations

import logging
import re
import time
import secrets
import hmac
from aiohttp import web

from bot.api.core import (
    logger,
    _SHARE_LINKS, _SESSIONS, SESSION_TTL_SECONDS,
    SHARE_MINUTES, SHARE_MAX_SECONDS,
    _cleanup_share_links, _valid_share, _serve_static,
    _err, _json, _guard, _audit, _num, _iso, _row_chapter,
    _get_pool, _sanitize_name, _valid_discord_id, _client_ip,
    _check_ip, _check_rate_limit, _dashboard_token, _is_banned,
    _record_auth_fail, _create_session, _sec_headers, _cors_headers,
    MANGA_STATUSES, CHAPTER_STATUSES, PRICE_KEYS
)

async def api_chapters_overview(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    
    q = (request.query.get("search", "") or "").strip()[:100]
    where_clause = ""
    args = []
    if q:
        where_clause = "WHERE m.manga_name ILIKE $1"
        args.append(f"%{q}%")
        
    query = f"""
        SELECT m.id, m.manga_name, m.cover_url,
            COUNT(c.id) FILTER (WHERE c.status = 'pending_tl' AND c.assigned_to IS NULL) as tl_avail,
            COUNT(c.id) FILTER (WHERE c.status = 'pending_pr' AND c.assigned_to IS NULL) as pr_avail,
            COUNT(c.id) FILTER (WHERE c.status = 'pending_ed' AND c.assigned_to IS NULL) as ed_avail,
            COUNT(c.id) FILTER (WHERE c.assigned_to IS NOT NULL AND c.status != 'published') as claimed,
            COUNT(c.id) FILTER (WHERE c.status = 'pending_review') as in_review,
            COUNT(c.id) FILTER (WHERE c.status = 'published') as published,
            MAX(c.updated_at) as last_updated
        FROM mangas m
        LEFT JOIN chapters c ON c.manga_id = m.id
        {where_clause}
        GROUP BY m.id
        ORDER BY last_updated DESC NULLS LAST, m.manga_name ASC
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        
        items = []
        for r in rows:
            d = dict(r)
            if d["last_updated"]:
                d["last_updated"] = _iso(d["last_updated"])
            items.append(d)
            
        return _json(request, {"items": items})
    except Exception as e:
        logger.exception("[DASHBOARD] chapters overview failed: %s", e)
        return _err(request, "internal error", 500)

async def api_chapters(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    status = (request.query.get("status", "") or "").strip()
    try:
        manga_id = int(request.query["manga_id"]) if request.query.get("manga_id") else None
    except ValueError:
        return _err(request, "bad manga_id", 400)
    try:
        limit = min(max(int(request.query.get("limit", "50")), 1), 100)
        offset = max(int(request.query.get("offset", "0")), 0)
    except ValueError:
        return _err(request, "bad pagination", 400)
        
    sort_key = request.query.get("sort", "updated_at").lower()
    order = request.query.get("order", "desc").upper()
    order_sql = "ASC" if order == "ASC" else "DESC"
    
    if sort_key == "chapter_number":
        order_clause = f"c.chapter_number {order_sql}, c.updated_at DESC"
    elif sort_key == "manga_name":
        order_clause = f"m.manga_name {order_sql}, c.chapter_number DESC"
    else:
        order_clause = f"c.updated_at {order_sql}"

    search_q = (request.query.get("search", "") or "").strip()

    try:
        async with pool.acquire() as conn:
            conds, args = [], []
            if manga_id:
                args.append(manga_id)
                conds.append(f"c.manga_id = ${len(args)}")
            if status in CHAPTER_STATUSES:
                args.append(status)
                conds.append(f"c.status = ${len(args)}")
            if search_q:
                args.append(f"%{search_q}%")
                conds.append(f"m.manga_name ILIKE ${len(args)}")
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            
            count_sql = f"SELECT COUNT(*) FROM chapters c JOIN mangas m ON m.id=c.manga_id {where}"
            total = await conn.fetchval(count_sql, *args)
            
            args += [limit, offset]
            rows = await conn.fetch(
                f"""SELECT c.*, m.manga_name FROM chapters c
                    JOIN mangas m ON m.id=c.manga_id {where}
                    ORDER BY {order_clause}
                    LIMIT ${len(args)-1} OFFSET ${len(args)}""", *args)
        try:
            from bot.core import get_bot
            bot = get_bot()
        except Exception:
            bot = None

        def _resolve_name(uid):
            if not uid: return None
            if bot:
                try:
                    duser = bot.cache.get_user(int(uid))
                    if duser: return duser.username
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            return uid
            
        items = []
        for r in rows:
            d = _row_chapter(dict(r))
            # Resolve discord ID to username for the dashboard
            if d["assigned_to"]:
                d["assigned_to_name"] = _resolve_name(d["assigned_to"])
            else:
                d["assigned_to_name"] = None
            items.append(d)

        return _json(request, {
            "total": total, "limit": limit, "offset": offset,
            "items": items,
        })
    except Exception as e:
        logger.exception("[DASHBOARD] chapters failed: %s", e)
        return _err(request, "internal error", 500)

async def _chapter_action(request: web.Request, kind: str) -> web.Response:
    try:
        chapter_id = int(request.match_info["chapter_id"])
    except (ValueError, KeyError):
        return _err(request, "bad chapter id", 400)
    body: dict = {}
    if kind in ("reject", "assign"):
        try:
            body = await request.json()
        except Exception:
            return _err(request, "invalid JSON body", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    REVERT = {"pending_review": "pending_ed", "pending_ed": "pending_pr",
              "pending_pr": "pending_tl"}
    REJECT_ROLES = {"pending_review": ("ED", "CO-ED"), "pending_ed": ("PR", "CO-PR"),
                    "pending_pr": ("TL", "CO-TL")}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                ch = await conn.fetchrow(
                    "SELECT * FROM chapters WHERE id=$1 FOR UPDATE", chapter_id)
                if not ch:
                    return _err(request, "chapter not found", 404)

                if kind == "approve":
                    if ch["status"] != "pending_review":
                        return _err(request,
                                    f"only pending_review can be approved (now {ch['status']})",
                                    409)
                    await conn.execute(
                        "UPDATE chapters SET status='published', assigned_to=NULL, "
                        "assigned_at=NULL, updated_at=now() WHERE id=$1", chapter_id)
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)"
                        " VALUES ($1,'REVIEW',$2,'approved',0)", chapter_id, "dashboard")

                elif kind == "reject":
                    reason = str(body.get("reason", "") or "").strip()[:1000]
                    if not reason:
                        return _err(request, "reason is required", 400)
                    new_status = REVERT.get(ch["status"])
                    if not new_status:
                        return _err(request,
                                    f"cannot reject from {ch['status']}", 409)
                    if new_status == "pending_pr":
                        has_pr = await conn.fetchval(
                            "SELECT 1 FROM manga_staff WHERE manga_id=$1 "
                            "AND role IN ('PR','CO-PR')", ch["manga_id"])
                        if not has_pr:
                            new_status = "pending_tl"
                    await conn.execute(
                        "UPDATE chapters SET status=$1, assigned_to=NULL, assigned_at=NULL,"
                        " updated_at=now() WHERE id=$2", new_status, chapter_id)
                    roles = list(REJECT_ROLES[ch["status"]])
                    await conn.execute(
                        "DELETE FROM transactions WHERE chapter_id=$1 AND status='unpaid'"
                        " AND role = ANY($2::text[])", chapter_id, roles)
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents, reason)"
                        " VALUES ($1,$2,$3,'rejected',0,$4)",
                        chapter_id, ch["status"], "dashboard", reason)

                elif kind == "reopen":
                    if ch["status"] not in ("published", "rejected"):
                        return _err(request,
                                    f"only published/rejected can reopen (now {ch['status']})",
                                    409)
                    await conn.execute(
                        "UPDATE chapters SET status='pending_tl', assigned_to=NULL, "
                        "assigned_at=NULL, images_count=NULL, updated_at=now() WHERE id=$1",
                        chapter_id)
                    await conn.execute(
                        "DELETE FROM transactions WHERE chapter_id=$1 AND status='unpaid'",
                        chapter_id)
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)"
                        " VALUES ($1,$2,'dashboard','reopened',0)",
                        chapter_id, ch["status"])

                elif kind == "unclaim":
                    if not ch["assigned_to"]:
                        return _json(request, {"detail": "chapter is not claimed"})
                    await conn.execute(
                        "UPDATE chapters SET assigned_to=NULL, assigned_at=NULL, "
                        "updated_at=now() WHERE id=$1", chapter_id)
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)"
                        " VALUES ($1,$2,'dashboard','unclaimed',0)",
                        chapter_id, ch["status"])

                elif kind == "assign":
                    user_id = str(body.get("user_id", "") or "").strip()
                    m = re.findall(r"\d{17,20}", user_id)
                    user_id = m[0] if m else ""
                    if not _valid_discord_id(user_id):
                        return _err(request, "bad user_id (need Discord ID or mention)", 400)
                    if ch["assigned_to"]:
                        return _err(request, "chapter already claimed", 409)
                    reg = await conn.fetchval(
                        "SELECT is_registered FROM users WHERE discord_id=$1", user_id)
                    if not reg:
                        return _err(request, "user is not registered", 404)
                    await conn.execute(
                        "UPDATE chapters SET assigned_to=$1, assigned_at=now(), "
                        "updated_at=now() WHERE id=$2", user_id, chapter_id)
                    await conn.execute(
                        "INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents)"
                        " VALUES ($1,$2,$3,'claimed',0)",
                        chapter_id, ch["status"], user_id)
                else:
                    return _err(request, "unknown action", 400)
                manga_id = ch["manga_id"]
        try:
            from bot.services.webhook_sender import update_manga_webhook
            from bot.core import get_bot
            await update_manga_webhook(get_bot(), manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        _audit(request, f"chapter.{kind}", f"id={chapter_id}")
        mrow = {"approve": "published"}.get(kind)
        out = {"id": chapter_id, "action": kind}
        if mrow:
            out["status"] = mrow
        return _json(request, out)
    except Exception as e:
        logger.exception("[DASHBOARD] chapter %s failed: %s", kind, e)
        return _err(request, "internal error", 500)

