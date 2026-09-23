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

async def api_finance_overview(request: web.Request) -> web.Response:
    denied = await _guard(request, allow_share=False)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            unpaid = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS s "
                "FROM transactions WHERE status='unpaid'")
            paid = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS s FROM payments")
            top = await conn.fetch(
                "SELECT user_id, COUNT(*) AS n, SUM(amount_cents) AS s FROM transactions "
                "WHERE status='unpaid' GROUP BY user_id ORDER BY s DESC LIMIT 20")
            recent = await conn.fetch(
                "SELECT user_id, amount_cents, paid_by, created_at FROM payments "
                "ORDER BY created_at DESC LIMIT 15")
        try:
            from bot.core import get_bot
            bot = get_bot()
        except Exception:
            bot = None
            
        def _resolve(uid):
            u_name = uid
            u_av = ""
            if bot:
                try:
                    duser = bot.cache.get_user(int(uid))
                    if duser:
                        u_name = duser.username
                        u_av = str(duser.display_avatar_url)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            return u_name, u_av
            
        top_list = []
        for r in top:
            nm, av = _resolve(r["user_id"])
            top_list.append({"user_id": r["user_id"], "count": r["n"], "cents": r["s"], "username": nm, "avatar_url": av})
            
        recent_list = []
        for r in recent:
            nm, av = _resolve(r["user_id"])
            recent_list.append({
                "user_id": r["user_id"], "cents": r["amount_cents"],
                "paid_by": r["paid_by"], "at": _iso(r["created_at"]),
                "username": nm, "avatar_url": av
            })

        return _json(request, {
            "unpaid": {"count": unpaid["n"], "cents": unpaid["s"]},
            "paid_all_time": {"count": paid["n"], "cents": paid["s"]},
            "top_balances": top_list,
            "recent_payments": recent_list,
        })
    except Exception as e:
        logger.exception("[DASHBOARD] finance failed: %s", e)
        return _err(request, "internal error", 500)

async def api_finance_export(request: web.Request) -> web.Response:
    denied = await _guard(request, allow_share=False)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        import csv
        import io
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.id, t.user_id, t.role, t.amount_cents, t.status, t.payment_id, t.created_at,
                       c.chapter_number, m.manga_name
                FROM transactions t
                LEFT JOIN chapters c ON c.id = t.chapter_id
                LEFT JOIN mangas m ON m.id = c.manga_id
                ORDER BY t.created_at DESC
                LIMIT 5000
                """
            )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Transaction ID", "User ID", "Manga", "Chapter", "Role", "Amount ($)", "Status", "Payment ID", "Date"])
        for r in rows:
            amount = f"{r['amount_cents'] / 100:.2f}"
            dt = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
            writer.writerow([
                r["id"], r["user_id"], r["manga_name"] or "",
                _num(r["chapter_number"]) if r["chapter_number"] is not None else "",
                r["role"], amount, r["status"], r["payment_id"] or "", dt
            ])
        csv_data = output.getvalue()
        resp = web.Response(
            body=csv_data,
            content_type="text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="finance_ledger.csv"',
            }
        )
        _sec_headers(resp)
        return _cors_headers(request, resp)
    except Exception as e:
        logger.exception("[DASHBOARD] finance export failed: %s", e)
        return _err(request, "export failed", 500)

