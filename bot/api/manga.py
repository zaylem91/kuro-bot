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

ROLE_ALIASES = {
    "TRANSLATION": "TL",
    "TRANSLATOR": "TL",
    "TL": "TL",
    "CO-TL": "CO-TL",
    "COTL": "CO-TL",
    "PROOFREADING": "PR",
    "PROOFREADER": "PR",
    "PR": "PR",
    "CO-PR": "CO-PR",
    "COPR": "CO-PR",
    "EDITING": "ED",
    "EDITOR": "ED",
    "ED": "ED",
    "CO-ED": "CO-ED",
    "COED": "CO-ED",
}


async def api_mangas(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    q = (request.query.get("search", "") or "").strip()[:100]
    status = (request.query.get("status", "") or "").strip()
    try:
        limit = min(max(int(request.query.get("limit", "50")), 1), 100)
        offset = max(int(request.query.get("offset", "0")), 0)
    except ValueError:
        return _err(request, "bad pagination", 400)
        
    sort_key = request.query.get("sort", "name").lower()
    order = request.query.get("order", "asc").upper()
    order_sql = "DESC" if order == "DESC" else "ASC"
    
    if sort_key == "chapters":
        order_clause = f"chapters {order_sql}, m.manga_name ASC"
    elif sort_key == "in_review":
        order_clause = f"in_review {order_sql}, m.manga_name ASC"
    else:
        order_clause = f"m.manga_name {order_sql}"

    try:
        async with pool.acquire() as conn:
            conds, args = [], []
            if q:
                args.append(f"%{q}%")
                conds.append(f"m.manga_name ILIKE ${len(args)}")
            if status in MANGA_STATUSES:
                args.append(status)
                conds.append(f"m.status = ${len(args)}")
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            args += [limit, offset]
            rows = await conn.fetch(
                f"""SELECT m.id, m.manga_name, m.status, m.drive_folder_id,
                           m.cover_url, m.mangadex_url, m.last_synced_at,
                           (SELECT COUNT(*) FROM chapters c WHERE c.manga_id=m.id) AS chapters,
                           (SELECT COUNT(*) FROM chapters c WHERE c.manga_id=m.id AND c.status='pending_review') AS in_review,
                           (SELECT COUNT(*) FROM manga_staff s WHERE s.manga_id=m.id) AS staff
                    FROM mangas m {where}
                    ORDER BY {order_clause} LIMIT ${len(args)-1} OFFSET ${len(args)}""",
                *args)
            total_args = args[:-2]
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM mangas m {where}", *total_args) if total_args else await conn.fetchval(
                "SELECT COUNT(*) FROM mangas")
        return _json(request, {
            "total": total, "limit": limit, "offset": offset,
            "items": [{
                "id": r["id"], "name": r["manga_name"], "status": r["status"],
                "drive_folder_id": r["drive_folder_id"],
                "cover_url": r["cover_url"], "mangadex_url": r["mangadex_url"],
                "last_synced_at": _iso(r["last_synced_at"]),
                "chapters": r["chapters"], "in_review": r["in_review"],
                "staff": r["staff"],
            } for r in rows],
        })
    except Exception as e:
        logger.exception("[DASHBOARD] mangas failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_detail(request: web.Request) -> web.Response:
    denied = await _guard(request)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT * FROM mangas WHERE id=$1", manga_id)
            if not m:
                return _err(request, "manga not found", 404)
            chapters = await conn.fetch(
                "SELECT * FROM chapters WHERE manga_id=$1 ORDER BY chapter_number DESC LIMIT 200",
                manga_id)
            staff = await conn.fetch(
                "SELECT ms.role, ms.discord_id FROM manga_staff ms "
                "WHERE ms.manga_id=$1 ORDER BY ms.role", manga_id)
        try:
            from bot.core import get_bot
            bot = get_bot()
        except Exception:
            bot = None
            
        def _resolve(uid):
            u_name = uid
            u_av = ""
            if bot and uid:
                try:
                    duser = bot.cache.get_user(int(uid))
                    if duser:
                        u_name = duser.username
                        u_av = str(duser.display_avatar_url)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            return u_name, u_av
            
        staff_out = []
        for r in staff:
            nm, av = _resolve(r["discord_id"])
            staff_out.append({"role": r["role"], "user_id": r["discord_id"], "username": nm, "avatar_url": av})
            
        chapters_out = []
        for r in chapters:
            ch_dict = _row_chapter(dict(r) | {})
            if ch_dict.get("assigned_to"):
                nm, av = _resolve(ch_dict["assigned_to"])
                ch_dict["assignee_name"] = nm
                ch_dict["assignee_avatar"] = av
            chapters_out.append(ch_dict)

        return _json(request, {
            "id": m["id"], "name": m["manga_name"], "status": m["status"],
            "drive_folder_id": m["drive_folder_id"],
            "cover_url": m["cover_url"], "mangadex_url": m["mangadex_url"],
            "discord_channel_id": m["discord_channel_id"],
            "last_synced_at": _iso(m["last_synced_at"]),
            "chapters": chapters_out,
            "staff": staff_out,
        })
    except Exception as e:
        logger.exception("[DASHBOARD] manga detail failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_create(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)
    name = _sanitize_name(body.get("name", ""), 200)
    drive_raw = str(body.get("drive", "") or "").strip()[:500]
    try:
        start_ch = int(body.get("start_chapter", 0))
    except (ValueError, TypeError):
        return _err(request, "start_chapter must be a number", 400)
    cover = str(body.get("cover_url", "") or "").strip()[:500]
    mdex = str(body.get("mangadex_url", "") or "").strip()[:500]
    if not name:
        return _err(request, "name is required", 400)
    if cover and not cover.startswith(("http://", "https://")):
        return _err(request, "cover_url must start with http(s)://", 400)
    if mdex:
        if not mdex.startswith(("http://", "https://")):
            mdex = "https://" + mdex
        if "mangadex.org" not in mdex.lower():
            return _err(request, "mangadex_url must be a mangadex.org link", 400)
    try:
        from bot.services.drive import extract_drive_id
        drive_id = extract_drive_id(drive_raw)
    except ValueError as e:
        return _err(request, f"bad drive link: {e}", 400)
    except Exception as e:
        logger.exception("[DASHBOARD] drive extract failed: %s", e)
        return _err(request, "drive validation failed", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            try:
                manga_id = await conn.fetchval(
                    "INSERT INTO mangas (manga_name, drive_folder_id, start_chapter, "
                    "cover_url, mangadex_url) VALUES ($1,$2,$3,NULLIF($4,''),NULLIF($5,'')) "
                    "RETURNING id",
                    name, drive_id, start_ch, cover, mdex)
            except Exception as e:
                msg = str(e).lower()
                if "unique" in msg or "duplicate" in msg:
                    return _err(request, "manga name already exists", 409)
                raise
        try:
            from bot.services.task_queue import enqueue_job
            await enqueue_job(pool, "drive_sync",
                              {"manga_id": manga_id, "drive_folder_id": drive_id})
        except Exception as e:
            logger.warning("[DASHBOARD] enqueue sync failed: %s", e)
        try:
            from bot.services.cache import manga_options_cache, workload_cache
            manga_options_cache.clear()
            workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        _audit(request, "manga.create", f"id={manga_id} name={name!r}")
        return _json(request, {"id": manga_id, "name": name,
                               "detail": "created — Drive scan queued"}, status=201)
    except Exception as e:
        logger.exception("[DASHBOARD] manga create failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_status(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)
    new_status = str(body.get("status", "") or "").strip().lower()
    if new_status not in MANGA_STATUSES:
        return _err(request, "status must be ongoing|paused|dropped", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                m = await conn.fetchrow(
                    "SELECT status, manga_name FROM mangas WHERE id=$1 FOR UPDATE",
                    manga_id)
                if not m:
                    return _err(request, "manga not found", 404)
                old = m["status"]
                if old == new_status:
                    return _json(request, {"id": manga_id, "status": old,
                                           "detail": "already in that status"})
                await conn.execute("UPDATE mangas SET status=$1 WHERE id=$2",
                                   new_status, manga_id)
                removed = 0
                if new_status in ("paused", "dropped"):
                    staff_rows = await conn.fetch(
                        "SELECT discord_id, role FROM manga_staff WHERE manga_id=$1",
                        manga_id)
                    await conn.execute("DELETE FROM manga_staff WHERE manga_id=$1",
                                       manga_id)
                    removed = len(staff_rows)
                    for sr in staff_rows:
                        try:
                            await conn.execute(
                                "INSERT INTO manga_staff_audit "
                                "(manga_id, discord_id, role, action, changed_by) "
                                "VALUES ($1,$2,$3,'auto_removed',$4)",
                                manga_id, sr["discord_id"], sr["role"], "dashboard")
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                    await conn.execute(
                        "UPDATE chapters SET assigned_to=NULL, assigned_at=NULL, "
                        "updated_at=now() WHERE manga_id=$1 AND assigned_to IS NOT NULL",
                        manga_id)
        try:
            from bot.services.cache import (drive_deep_sync_cache, manga_options_cache,
                                            workload_cache)
            for c in (drive_deep_sync_cache, manga_options_cache, workload_cache):
                try:
                    c.clear()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        try:
            from bot.services.webhook_sender import update_manga_webhook
            from bot.core import get_bot
            await update_manga_webhook(get_bot(), manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)  # webhook refresh is best-effort from dashboard
        _audit(request, "manga.status",
               f"id={manga_id} {old}->{new_status} removed_staff={removed}")
        return _json(request, {"id": manga_id, "old_status": old,
                               "status": new_status, "removed_staff": removed})
    except Exception as e:
        logger.exception("[DASHBOARD] manga status failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_sync(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT drive_folder_id, manga_name FROM mangas WHERE id=$1", manga_id)
            if not row:
                return _err(request, "manga not found", 404)
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM background_jobs WHERE status IN ('pending','running') "
                "AND job_type='drive_sync' AND payload->>'manga_id' = $1", str(manga_id))
            if pending:
                return _json(request, {"detail": "sync already queued/running",
                                       "manga_id": manga_id})
        from bot.services.task_queue import enqueue_job
        job_id = await enqueue_job(pool, "drive_sync",
                                   {"manga_id": manga_id,
                                    "drive_folder_id": row["drive_folder_id"]})
        _audit(request, "manga.sync", f"id={manga_id} job={job_id}")
        return _json(request, {"job_id": job_id, "manga_id": manga_id,
                               "detail": "Drive sync queued"})
    except RuntimeError as e:
        logger.warning("[DASHBOARD] queue not ready: %s", e)
        return _err(request, "task queue not ready — try again after bot startup", 503)
    except Exception as e:
        logger.exception("[DASHBOARD] manga sync failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_delete(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    force = (request.query.get("force", "") or "").lower() in ("1", "true", "yes")
    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                m = await conn.fetchrow(
                    "SELECT manga_name FROM mangas WHERE id=$1", manga_id)
                if not m:
                    return _err(request, "manga not found", 404)
                unpaid = await conn.fetchval(
                    "SELECT COALESCE(SUM(t.amount_cents),0) FROM transactions t "
                    "JOIN chapters c ON c.id=t.chapter_id "
                    "WHERE c.manga_id=$1 AND t.status='unpaid'", manga_id)
                if unpaid and unpaid > 0 and not force:
                    return _json(request, {
                        "error": "manga has unpaid ledger",
                        "unpaid_cents": unpaid,
                        "hint": "pay staff first or retry with ?force=1",
                    }, status=409)
                await conn.execute("DELETE FROM mangas WHERE id=$1", manga_id)
        _audit(request, "manga.delete", f"id={manga_id} force={force}")
        return _json(request, {"deleted": manga_id})
    except Exception as e:
        logger.exception("[DASHBOARD] manga delete failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_update(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)

    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT * FROM mangas WHERE id=$1", manga_id)
            if not m:
                return _err(request, "manga not found", 404)

            def _get(field, default=None):
                try:
                    v = m[field]
                    return v if v is not None else default
                except Exception:
                    return default

            name = _sanitize_name(body.get("name", _get("manga_name", "")))
            drive_id_input = str(body.get("drive_folder_id", _get("drive_folder_id", "")) or "").strip()
            if drive_id_input:
                from bot.services.drive import extract_drive_id
                try:
                    drive_id = extract_drive_id(drive_id_input)
                except ValueError as e:
                    return _err(request, str(e), 400)
            else:
                drive_id = _get("drive_folder_id", "")

            cover = str(body.get("cover_url", _get("cover_url", "")) or "").strip()
            if cover and not cover.startswith(("http://", "https://")):
                return _err(request, "cover_url must start with http:// or https://", 400)

            mdex = str(body.get("mangadex_url", _get("mangadex_url", "")) or "").strip()
            if mdex and not mdex.startswith(("http://", "https://")):
                mdex = "https://" + mdex

            start_ch = body.get("start_chapter", _get("start_chapter", 0))
            try:
                start_ch = max(0, int(start_ch))
            except (ValueError, TypeError):
                start_ch = _get("start_chapter", 0)

            await conn.execute(
                """
                UPDATE mangas
                SET manga_name = $1, drive_folder_id = $2, cover_url = NULLIF($3, ''),
                    mangadex_url = NULLIF($4, ''), start_chapter = $5
                WHERE id = $6
                """,
                name, drive_id, cover, mdex, start_ch, manga_id,
            )

        try:
            from bot.services.cache import manga_options_cache, workload_cache
            manga_options_cache.clear()
            workload_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        try:
            from bot.services.webhook_sender import update_manga_webhook
            from bot.core import get_bot
            await update_manga_webhook(get_bot(), manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        _audit(request, "manga.update", f"id={manga_id} name={name!r}")
        return _json(request, {"status": "ok", "id": manga_id, "name": name})
    except Exception as e:
        logger.exception("[DASHBOARD] manga update failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_create_channel(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)

    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        from bot.core import get_bot
        from bot.config import GUILD_ID, TEAM_LEADER_ROLE_ID, MANGA_CATEGORY_ID
        import hikari
        bot = get_bot()
        if not bot:
            return _err(request, "discord bot not ready", 503)

        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT id, manga_name, discord_channel_id, status FROM mangas WHERE id=$1", manga_id)
            if not m:
                return _err(request, "manga not found", 404)
            if m["discord_channel_id"]:
                return _err(request, "channel already exists", 400)

            guild_id = GUILD_ID
            if not guild_id:
                return _err(request, "GUILD_ID not configured", 503)

            from bot.config import get_category_for_status
            _, category_id = get_category_for_status(m["status"])
            if not category_id:
                category_id = MANGA_CATEGORY_ID

            overwrites = []
            if TEAM_LEADER_ROLE_ID:
                overwrites = [
                    hikari.PermissionOverwrite(
                        id=guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=hikari.Permissions.VIEW_CHANNEL,
                    ),
                    hikari.PermissionOverwrite(
                        id=TEAM_LEADER_ROLE_ID,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=hikari.Permissions.VIEW_CHANNEL,
                    )
                ]

            name = m["manga_name"]
            chan_name = name.replace(" ", "-").lower()[:90]
            text_channel = await bot.rest.create_guild_text_channel(
                guild_id,
                name=chan_name,
                category=category_id,
                permission_overwrites=overwrites
            )
            cid = str(text_channel.id)

            from bot.config import TEAM_NAME
            _suffix = f" • {TEAM_NAME}" if TEAM_NAME else ""
            _max_len = 80 - len(_suffix)
            _wname = f"{name[:_max_len]}{_suffix}" if len(name) + len(_suffix) > 80 else f"{name}{_suffix}"
            webhook = await bot.rest.create_webhook(text_channel.id, name=_wname)
            wid = str(webhook.id)
            wtoken = webhook.token
            if not wtoken:
                wh = await bot.rest.fetch_webhook(webhook.id)
                wtoken = wh.token

            await conn.execute(
                """UPDATE mangas
                   SET discord_channel_id = $1, webhook_id = $2, webhook_token = $3
                   WHERE id = $4""",
                cid, wid, wtoken, manga_id
            )

            # Grant existing staff access to the new channel
            staff = await conn.fetch("SELECT DISTINCT discord_id FROM manga_staff WHERE manga_id = $1", manga_id)
            from bot.services.manga_access import grant_channel_access
            for s in staff:
                await grant_channel_access(bot, cid, s["discord_id"])

        from bot.services.webhook_sender import update_manga_webhook
        await update_manga_webhook(bot, manga_id)

        _audit(request, "manga.create_channel", f"id={manga_id} channel={cid}")
        return _json(request, {"status": "ok", "discord_channel_id": cid, "webhook_id": wid})
    except Exception as e:
        logger.exception("[DASHBOARD] create channel failed: %s", e)
        return _err(request, f"failed to create channel: {e}", 500)

async def api_manga_add_staff(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
    except (ValueError, KeyError):
        return _err(request, "bad manga id", 400)
    try:
        body = await request.json()
    except Exception:
        return _err(request, "invalid JSON body", 400)

    user_id = str(body.get("user_id", "") or "").strip()
    raw_role = str(body.get("role", "") or "").strip().upper()
    role = ROLE_ALIASES.get(raw_role, raw_role)
    valid_roles = ('TL', 'CO-TL', 'PR', 'CO-PR', 'ED', 'CO-ED')
    if role not in valid_roles:
        return _err(request, f"role must be one of {valid_roles}", 400)
    if not _valid_discord_id(user_id):
        return _err(request, "invalid discord user_id", 400)

    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT id, manga_name, discord_channel_id, drive_folder_id FROM mangas WHERE id=$1", manga_id)
            if not m:
                return _err(request, "manga not found", 404)

            u = await conn.fetchrow("SELECT is_registered, drive_email FROM users WHERE discord_id=$1", user_id)
            if not u or not u["is_registered"]:
                return _err(request, "user is not registered", 400)

            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO manga_staff (manga_id, role, discord_id) VALUES ($1, $2, $3) ON CONFLICT (manga_id, role, discord_id) DO NOTHING",
                    manga_id, role, user_id
                )
                await conn.execute(
                    "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1, $2, $3, 'added', 'dashboard')",
                    manga_id, user_id, role
                )

            cid = m["discord_channel_id"]
            fid = m["drive_folder_id"]
            gmail = u["drive_email"]

        if cid:
            try:
                from bot.core import get_bot
                from bot.services.manga_access import grant_channel_access
                await grant_channel_access(get_bot(), cid, user_id)
            except Exception as ex:
                logger.warning(f"Failed to grant channel access: {ex}")

        if fid and gmail:
            try:
                from bot.core import get_drive_service
                await get_drive_service().grant_folder_access(fid, gmail, role="writer")
            except Exception as ex:
                logger.warning(f"Failed to grant drive access: {ex}")

        try:
            from bot.services.webhook_sender import update_manga_webhook
            from bot.core import get_bot
            await update_manga_webhook(get_bot(), manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        try:
            from bot.services.cache import workload_cache, manga_options_cache
            workload_cache.clear()
            manga_options_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        _audit(request, "manga.add_staff", f"manga={manga_id} user={user_id} role={role}")
        return _json(request, {"status": "ok", "manga_id": manga_id, "user_id": user_id, "role": role})
    except Exception as e:
        logger.exception("[DASHBOARD] add staff failed: %s", e)
        return _err(request, "internal error", 500)

async def api_manga_remove_staff(request: web.Request) -> web.Response:
    denied = await _guard(request, write=True)
    if denied is not None:
        return denied
    try:
        manga_id = int(request.match_info["manga_id"])
        user_id = str(request.match_info["user_id"]).strip()
        raw_role = str(request.match_info["role"]).strip().upper()
        role = ROLE_ALIASES.get(raw_role, raw_role)
    except (ValueError, KeyError):
        return _err(request, "bad parameters", 400)

    pool = _get_pool()
    if pool is None:
        return _err(request, "database not ready", 503)

    try:
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT id, manga_name, discord_channel_id, drive_folder_id FROM mangas WHERE id=$1", manga_id)
            if not m:
                return _err(request, "manga not found", 404)

            u = await conn.fetchrow("SELECT drive_email FROM users WHERE discord_id=$1", user_id)
            gmail = u["drive_email"] if u else None

            async with conn.transaction():
                deleted = await conn.execute(
                    "DELETE FROM manga_staff WHERE manga_id=$1 AND discord_id=$2 AND role=$3",
                    manga_id, user_id, role
                )
                if deleted != "DELETE 0":
                    await conn.execute(
                        "INSERT INTO manga_staff_audit (manga_id, discord_id, role, action, changed_by) VALUES ($1, $2, $3, 'removed', 'dashboard')",
                        manga_id, user_id, role
                    )

            has_other_roles = await conn.fetchval(
                "SELECT 1 FROM manga_staff WHERE manga_id=$1 AND discord_id=$2",
                manga_id, user_id
            )

        cid = m["discord_channel_id"]
        fid = m["drive_folder_id"]

        if not has_other_roles:
            if cid:
                try:
                    from bot.core import get_bot
                    from bot.services.manga_access import revoke_channel_access
                    await revoke_channel_access(get_bot(), cid, user_id)
                except Exception as ex:
                    logger.warning(f"Failed to revoke channel access: {ex}")

            if fid and gmail:
                try:
                    from bot.core import get_drive_service
                    await get_drive_service().revoke_folder_access(fid, gmail)
                except Exception as ex:
                    logger.warning(f"Failed to revoke drive access: {ex}")

        try:
            from bot.services.webhook_sender import update_manga_webhook
            from bot.core import get_bot
            await update_manga_webhook(get_bot(), manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        try:
            from bot.services.cache import workload_cache, manga_options_cache
            workload_cache.clear()
            manga_options_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

        _audit(request, "manga.remove_staff", f"manga={manga_id} user={user_id} role={role}")
        return _json(request, {"status": "ok", "removed": True, "manga_id": manga_id, "user_id": user_id, "role": role})
    except Exception as e:
        logger.exception("[DASHBOARD] remove staff failed: %s", e)
        return _err(request, "internal error", 500)

