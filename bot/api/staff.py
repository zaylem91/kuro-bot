
from aiohttp import web
import json
import logging

from bot.api.core import (
    _guard, _get_pool, _err, _json, _num, _iso, _audit, logger
)
async def api_staff(request: web.Request) -> web.Response:
    denied = await _guard(request, allow_share=False)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    q = (request.query.get("search", "") or "").strip().lower()[:100]
    role_filter = (request.query.get("role", "") or "").strip().upper()[:20]
    workload_filter = (request.query.get("workload", "") or "").strip().lower()[:20]
    payment_filter = (request.query.get("payment", "") or "").strip().lower()[:20]

    try:
        async with pool.acquire() as conn:
            users = await conn.fetch(
                "SELECT discord_id, is_registered, can_manage_payments, payment_method, payment_info, drive_email "
                "FROM users WHERE is_registered=TRUE ORDER BY discord_id"
            )
            if not users:
                return _json(request, {
                    "items": [],
                    "stats": {
                        "total_staff": 0, "active_staff": 0, "idle_staff": 0,
                        "total_unpaid_cents": 0, "total_unpaid_count": 0, "role_counts": {}
                    }
                })
            ids = [u["discord_id"] for u in users]
            roles = await conn.fetch(
                "SELECT discord_id, role FROM user_roles WHERE discord_id = ANY($1::text[])",
                ids)
            mangas = await conn.fetch(
                "SELECT ms.discord_id, ms.role, COUNT(*) AS c FROM manga_staff ms "
                "JOIN mangas m ON m.id=ms.manga_id AND m.status='ongoing' "
                "WHERE ms.discord_id = ANY($1::text[]) GROUP BY 1,2", ids)
            claimed = await conn.fetch(
                "SELECT assigned_to AS discord_id, COUNT(*) AS c FROM chapters "
                "WHERE assigned_to = ANY($1::text[]) AND status != 'published' GROUP BY 1", ids)
            unpaid = await conn.fetch(
                "SELECT user_id AS discord_id, COUNT(*) AS n, "
                "COALESCE(SUM(amount_cents),0) AS s FROM transactions "
                "WHERE status='unpaid' AND user_id = ANY($1::text[]) GROUP BY 1", ids)

        rmap: dict[str, list] = {}
        all_roles_count: dict[str, int] = {}
        for r in roles:
            rmap.setdefault(r["discord_id"], []).append(r["role"])
            all_roles_count[r["role"]] = all_roles_count.get(r["role"], 0) + 1

        mmap: dict[str, int] = {}
        for r in mangas:
            mmap[r["discord_id"]] = mmap.get(r["discord_id"], 0) + r["c"]
        cmap = {r["discord_id"]: r["c"] for r in claimed}
        umap = {r["discord_id"]: (r["n"], r["s"]) for r in unpaid}

        try:
            from bot.core import get_bot
            bot = get_bot()
        except Exception:
            bot = None

        all_items = []
        total_unpaid_cents = 0
        total_unpaid_count = 0
        active_count = 0

        for u in users:
            uid_str = u["discord_id"]
            n, s = umap.get(uid_str, (0, 0))
            total_unpaid_cents += s
            total_unpaid_count += n

            username = uid_str
            avatar_url = ""
            if bot:
                try:
                    duser = bot.cache.get_user(int(uid_str))
                    if duser:
                        username = duser.username
                        avatar_url = str(duser.display_avatar_url)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

            u_mangas = mmap.get(uid_str, 0)
            u_claimed = cmap.get(uid_str, 0)
            if (u_mangas + u_claimed) > 0:
                active_count += 1

            all_items.append({
                "user_id": uid_str,
                "username": username,
                "avatar_url": avatar_url,
                "roles": sorted(rmap.get(uid_str, [])),
                "active_mangas": u_mangas,
                "claimed_chapters": u_claimed,
                "unpaid_count": n,
                "unpaid_cents": s,
                "can_manage_payments": bool(u["can_manage_payments"]),
                "payment_method": u["payment_method"] or "",
                "payment_info": u["payment_info"] or "",
                "drive_email": u["drive_email"] or "",
            })

        filtered = all_items
        if q:
            filtered = [
                x for x in filtered
                if q in x["user_id"].lower() or q in x["username"].lower()
            ]
        if role_filter:
            filtered = [
                x for x in filtered
                if any(role_filter in r for r in x["roles"])
            ]
        if workload_filter:
            if workload_filter == "active":
                filtered = [x for x in filtered if (x["active_mangas"] + x["claimed_chapters"]) > 0]
            elif workload_filter == "idle":
                filtered = [x for x in filtered if (x["active_mangas"] + x["claimed_chapters"]) == 0]
            elif workload_filter == "overloaded":
                filtered = [x for x in filtered if (x["active_mangas"] + x["claimed_chapters"]) >= 3]
        if payment_filter:
            if payment_filter == "unpaid":
                filtered = [x for x in filtered if x["unpaid_cents"] > 0]
            elif payment_filter == "zero":
                filtered = [x for x in filtered if x["unpaid_cents"] == 0]

        filtered.sort(key=lambda x: (x["active_mangas"] + x["claimed_chapters"]), reverse=True)

        return _json(request, {
            "items": filtered,
            "stats": {
                "total_staff": len(all_items),
                "active_staff": active_count,
                "idle_staff": len(all_items) - active_count,
                "total_unpaid_cents": total_unpaid_cents,
                "total_unpaid_count": total_unpaid_count,
                "role_counts": all_roles_count,
            }
        })
    except Exception as e:
        logger.exception("[DASHBOARD] staff failed: %s", e)
        return _err(request, "internal error", 500)


async def api_staff_detail(request: web.Request) -> web.Response:
    denied = await _guard(request, allow_share=False)
    if denied is not None:
        return denied
    user_id = (request.match_info.get("user_id") or "").strip()
    m = re.findall(r"\d{17,20}", user_id)
    user_id = m[0] if m else ""
    if not _valid_discord_id(user_id):
        return _err(request, "bad user id", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT discord_id, is_registered, can_manage_payments, payment_method, payment_info, drive_email, registered_at "
                "FROM users WHERE discord_id=$1", user_id
            )
            if not user:
                return _err(request, "staff member not found", 404)

            roles_rows = await conn.fetch(
                "SELECT role FROM user_roles WHERE discord_id=$1 ORDER BY role", user_id
            )
            mangas_rows = await conn.fetch(
                "SELECT m.id, m.manga_name, m.cover_url, ms.role "
                "FROM manga_staff ms "
                "JOIN mangas m ON m.id=ms.manga_id "
                "WHERE ms.discord_id=$1 AND m.status='ongoing' "
                "ORDER BY m.manga_name ASC", user_id
            )
            claimed_rows = await conn.fetch(
                "SELECT c.id, c.chapter_number, c.status, c.assigned_at, m.id as manga_id, m.manga_name "
                "FROM chapters c "
                "JOIN mangas m ON m.id=c.manga_id "
                "WHERE c.assigned_to=$1 AND c.status != 'published' "
                "ORDER BY c.assigned_at DESC NULLS LAST", user_id
            )
            unpaid_rows = await conn.fetch(
                "SELECT t.id, t.chapter_id, t.role, t.amount_cents, t.created_at, c.chapter_number, m.manga_name "
                "FROM transactions t "
                "JOIN chapters c ON c.id=t.chapter_id "
                "JOIN mangas m ON m.id=c.manga_id "
                "WHERE t.user_id=$1 AND t.status='unpaid' "
                "ORDER BY t.created_at DESC", user_id
            )
            paid_summary = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount_cents),0) as paid_cents, COUNT(*) as paid_count "
                "FROM transactions WHERE user_id=$1 AND status='paid'", user_id
            )
            completed_count = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions WHERE user_id=$1", user_id
            ) or 0
            payments_rows = await conn.fetch(
                "SELECT id, amount_cents, paid_by, payment_method, payment_info, created_at "
                "FROM payments WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user_id
            )

        username = user_id
        avatar_url = ""
        try:
            from bot.core import get_bot
            bot = get_bot()
            if bot:
                duser = bot.cache.get_user(int(user_id))
                if duser:
                    username = duser.username
                    avatar_url = str(duser.display_avatar_url)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        unpaid_cents = sum(r["amount_cents"] for r in unpaid_rows)

        return _json(request, {
            "user_id": user_id,
            "username": username,
            "avatar_url": avatar_url,
            "roles": [r["role"] for r in roles_rows],
            "drive_email": user["drive_email"] or "",
            "payment_method": user["payment_method"] or "",
            "payment_info": user["payment_info"] or "",
            "registered_at": _iso(user["registered_at"]) if user["registered_at"] else "",
            "can_manage_payments": bool(user["can_manage_payments"]),
            "stats": {
                "active_mangas": len(mangas_rows),
                "claimed_chapters": len(claimed_rows),
                "completed_chapters": completed_count,
                "unpaid_cents": unpaid_cents,
                "unpaid_count": len(unpaid_rows),
                "paid_all_time_cents": paid_summary["paid_cents"] if paid_summary else 0,
            },
            "assigned_mangas": [
                {"id": r["id"], "name": r["manga_name"], "cover_url": r["cover_url"] or "", "role": r["role"]}
                for r in mangas_rows
            ],
            "claimed_chapters": [
                {
                    "id": r["id"],
                    "chapter_number": str(r["chapter_number"]),
                    "status": r["status"],
                    "assigned_at": _iso(r["assigned_at"]) if r["assigned_at"] else "",
                    "manga_id": r["manga_id"],
                    "manga_name": r["manga_name"],
                }
                for r in claimed_rows
            ],
            "unpaid_transactions": [
                {
                    "id": r["id"],
                    "chapter_id": r["chapter_id"],
                    "chapter_number": str(r["chapter_number"]),
                    "manga_name": r["manga_name"],
                    "role": r["role"],
                    "amount_cents": r["amount_cents"],
                    "created_at": _iso(r["created_at"]) if r["created_at"] else "",
                }
                for r in unpaid_rows
            ],
            "recent_payments": [
                {
                    "id": r["id"],
                    "amount_cents": r["amount_cents"],
                    "paid_by": r["paid_by"],
                    "payment_method": r["payment_method"],
                    "payment_info": r["payment_info"],
                    "created_at": _iso(r["created_at"]) if r["created_at"] else "",
                }
                for r in payments_rows
            ]
        })
    except Exception as e:
        logger.exception("[DASHBOARD] staff detail failed: %s", e)
        return _err(request, "internal error", 500)


async def api_pay(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    user_id = (request.match_info.get("user_id") or "").strip()
    m = re.findall(r"\d{17,20}", user_id)
    user_id = m[0] if m else ""
    if not _valid_discord_id(user_id):
        return _err(request, "bad user id", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                udata = await conn.fetchrow(
                    "SELECT payment_method, payment_info FROM users WHERE discord_id=$1",
                    user_id)
                if not udata:
                    return _err(request, "user not found", 404)
                rows = await conn.fetch(
                    "UPDATE transactions SET status='paid' WHERE user_id=$1 AND status='unpaid'"
                    " RETURNING id, amount_cents", user_id)
                if not rows:
                    return _err(request, "balance is already zero", 409)
                total = sum(r["amount_cents"] for r in rows)
                payment_id = await conn.fetchval(
                    "INSERT INTO payments (user_id, amount_cents, paid_by, payment_method, payment_info)"
                    " VALUES ($1,$2,$3,$4,$5) RETURNING id", user_id, total, "dashboard",
                    udata["payment_method"] or "unknown", udata["payment_info"] or "unknown")
                tx_ids = [r["id"] for r in rows]
                await conn.execute("UPDATE transactions SET payment_id = $1 WHERE id = ANY($2::int[])", payment_id, tx_ids)
        _audit(request, "staff.pay", f"user={user_id} cents={total} payment_id={payment_id}")
        return _json(request, {"user_id": user_id, "paid_cents": total,
                               "payment_id": payment_id, "chapters": len(rows)})
    except Exception as e:
        logger.exception("[DASHBOARD] pay failed: %s", e)
        return _err(request, "internal error", 500)


async def api_staff_update(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    user_id = request.match_info.get("user_id", "").strip()
    if not _valid_discord_id(user_id):
        return _err(request, "invalid user id", 400)
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)

    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        async with pool.acquire() as conn:
            old = await conn.fetchrow("SELECT * FROM users WHERE discord_id=$1", user_id)
            if not old:
                return _err(request, "user not found", 404)

            tz = body.get("timezone", old["timezone"])
            method = body.get("payment_method", old["payment_method"])
            info = body.get("payment_info", old["payment_info"])
            gmail = body.get("drive_email", old["drive_email"])
            can_manage = bool(body.get("can_manage_payments", old["can_manage_payments"]))
            roles = body.get("roles")

            if method and method not in ("binance", "paypal"):
                return _err(request, "payment_method must be binance or paypal", 400)

            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE users
                    SET timezone=$1, payment_method=$2, payment_info=$3, drive_email=$4, can_manage_payments=$5
                    WHERE discord_id=$6
                    """,
                    tz, method, info, gmail, can_manage, user_id
                )

                if roles is not None and isinstance(roles, list):
                    valid_roles = {'TL', 'CO-TL', 'PR', 'CO-PR', 'ED', 'CO-ED'}
                    sanitized_roles = [r.upper() for r in roles if r.upper() in valid_roles]
                    await conn.execute("DELETE FROM user_roles WHERE discord_id=$1", user_id)
                    for r in sanitized_roles:
                        await conn.execute("INSERT INTO user_roles (discord_id, role) VALUES ($1, $2)", user_id, r)

                # Audit
                if str(old["timezone"]) != str(tz):
                    await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'timezone',$2,$3,'dashboard')", user_id, str(old["timezone"]), str(tz))
                if str(old["payment_method"]) != str(method):
                    await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_method',$2,$3,'dashboard')", user_id, str(old["payment_method"]), str(method))
                if str(old["payment_info"]) != str(info):
                    await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_info',$2,$3,'dashboard')", user_id, str(old["payment_info"]), str(info))
                if str(old["drive_email"] or "") != str(gmail or ""):
                    await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'drive_email',$2,$3,'dashboard')", user_id, str(old["drive_email"] or ""), str(gmail or ""))

            # Handle drive permissions update if gmail changed
            old_gmail = old["drive_email"]
            if gmail and gmail.lower() != (old_gmail or "").lower():
                mids = await conn.fetch("SELECT DISTINCT m.drive_folder_id FROM manga_staff ms JOIN mangas m ON m.id=ms.manga_id WHERE ms.discord_id=$1", user_id)
                fids = [r["drive_folder_id"] for r in mids if r["drive_folder_id"]]
                if fids:
                    from bot.core import get_drive_service
                    ds = get_drive_service()
                    for fid in fids:
                        if old_gmail:
                            try:
                                await ds.revoke_folder_access(fid, old_gmail)
                            except Exception as e:
                                import logging
                                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                        try:
                            await ds.grant_folder_access(fid, gmail, role="writer")
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        try:
            from bot.services.cache import workload_cache, user_workload_cache
            workload_cache.clear()
            user_workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        _audit(request, "staff.update", f"user={user_id}")
        return _json(request, {"status": "ok", "user_id": user_id})
    except Exception as e:
        logger.exception("[DASHBOARD] staff update failed: %s", e)
        return _err(request, "internal error", 500)


