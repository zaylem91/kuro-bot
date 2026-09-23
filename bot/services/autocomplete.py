"""Autocomplete helpers — registered members only."""
import hikari
import lightbulb
from bot.database.connection import get_pool

# Cache display names for 5 minutes to avoid REST spam
_display_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300

async def _resolve_display(ctx, guild_id: int | None, did: str) -> str:
    import time
    now = time.time()
    if did in _display_cache:
        ts, name = _display_cache[did]
        if now - ts < _CACHE_TTL:
            return name
    display = did
    # 1) Try cache (fast, no API call)
    if guild_id:
        try:
            member = ctx.client.app.cache.get_member(guild_id, int(did))  # type: ignore
            if member:
                display = member.display_name or member.username
                _display_cache[did] = (now, display)
                return display
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    # 2) Try REST fetch_member (shows guild nickname + avatar, needs GuildMembers intent)
    if guild_id:
        try:
            member = await ctx.client.app.rest.fetch_member(guild_id, int(did))
            display = member.display_name or member.username
            _display_cache[did] = (now, display)
            return display
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    # 3) Fallback: global user fetch
    try:
        user = await ctx.client.app.rest.fetch_user(int(did))
        display = user.global_name or user.username
        _display_cache[did] = (now, display)
        return display
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    _display_cache[did] = (now, display)
    return display

async def _fast_display(ctx, guild_id, did: str, fallback_role: str) -> str:
    """Fast cache-only display for autocomplete (no REST)."""
    if guild_id:
        try:
            m = ctx.client.app.cache.get_member(guild_id, int(did))  # type: ignore
            if m:
                return m.display_name or m.username
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    try:
        u = ctx.client.app.cache.get_user(int(did))  # type: ignore
        if u:
            return u.username
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    import time
    if did in _display_cache and time.time() - _display_cache[did][0] < _CACHE_TTL:
        return _display_cache[did][1]
    
    # Try fetching asynchronously in background so next time it is in cache?
    try:
        import asyncio
        async def _bg_fetch():
            try:
                user = await ctx.client.app.rest.fetch_user(int(did))
                _display_cache[did] = (time.time(), user.global_name or user.username)
            except: pass
        asyncio.create_task(_bg_fetch())
    except:
        pass
        
    return f"User {did}"  # fallback

async def registered_user_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    """Show only registered members. Fast — no REST, must respond <3s or Discord returns Unknown interaction."""
    try:
        raw = (ctx.focused.value or "").strip()
        query = raw
        if "," in raw:
            query = raw.split(",")[-1].strip()
        elif " " in raw and "|" in raw:
            query = raw.split()[-1]
        query = query.lower()

        pool = get_pool()
        async with pool.acquire() as conn:
            if query:
                # Single query with trgm index support (pg_trgm GIN). Avoid second fallback query.
                # Fetch 50 candidates - filtering on display name done in Python (fast, no extra DB hit)
                rows = await conn.fetch(
                    "SELECT discord_id, default_role FROM users WHERE is_registered = TRUE AND (discord_id ILIKE $1 OR default_role ILIKE $1) LIMIT 50",
                    f"%{query}%"
                )
                if not rows:
                    # Only fallback to display-name search if needed - reuses already-fetched candidates via Python
                    # Without second DB roundtrip: fetch broader set once
                    rows = await conn.fetch(
                        "SELECT discord_id, default_role FROM users WHERE is_registered = TRUE ORDER BY registered_at DESC LIMIT 50"
                    )
            else:
                rows = await conn.fetch(
                    "SELECT discord_id, default_role FROM users WHERE is_registered = TRUE ORDER BY registered_at DESC LIMIT 50"
                )
        if not rows:
            await ctx.respond([])
            return

        guild_id = getattr(ctx, "guild_id", None) or getattr(getattr(ctx, "interaction", None), "guild_id", None)

        suggestions: list[str] = []
        for r in rows:
            did = r["discord_id"]
            role = r["default_role"] or "?"
            display = await _fast_display(ctx, guild_id, did, role)
            if query and query not in display.lower() and query not in did.lower() and query not in role.lower():
                continue
            suggestions.append(f"{display} — {role} | {did}")

        if not suggestions and not query:
            for r in rows[:25]:
                suggestions.append(f"{r['discord_id']} — {r['default_role']} | {r['discord_id']}")
        await ctx.respond(suggestions[:25])
    except hikari.errors.NotFoundError:
        pass  # interaction expired — ignore
    except Exception:
        try:
            await ctx.respond([])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

async def _role_filtered_autocomplete(ctx: lightbulb.AutocompleteContext, required_role: str) -> None:
    """Show only registered members who have `required_role` in user_roles. Fast."""
    try:
        raw = (ctx.focused.value or "").strip()
        query = raw
        if "," in raw:
            query = raw.split(",")[-1].strip()
        elif " " in raw and "|" in raw:
            query = raw.split()[-1]
        query = query.lower()
        pool = get_pool()
        async with pool.acquire() as conn:
            if query:
                rows = await conn.fetch(
                    "SELECT u.discord_id, u.default_role FROM users u "
                    "JOIN user_roles ur ON ur.discord_id = u.discord_id "
                    "WHERE u.is_registered = TRUE AND ur.role = $1 AND u.discord_id ILIKE $2 LIMIT 50",
                    required_role, f"%{query}%"
                )
                # Avoid second DB hit if filter empty - fall back to broader set already in Python
                if not rows:
                    rows = await conn.fetch(
                        "SELECT u.discord_id, u.default_role FROM users u "
                        "JOIN user_roles ur ON ur.discord_id = u.discord_id "
                        "WHERE u.is_registered = TRUE AND ur.role = $1 ORDER BY u.registered_at DESC LIMIT 50",
                        required_role
                    )
            else:
                rows = await conn.fetch(
                    "SELECT u.discord_id, u.default_role FROM users u "
                    "JOIN user_roles ur ON ur.discord_id = u.discord_id "
                    "WHERE u.is_registered = TRUE AND ur.role = $1 ORDER BY u.registered_at DESC LIMIT 50",
                    required_role
                )
        if not rows:
            await ctx.respond([])
            return
        guild_id = getattr(ctx, "guild_id", None) or getattr(getattr(ctx, "interaction", None), "guild_id", None)
        suggestions: list[str] = []
        for r in rows:
            did = r["discord_id"]
            display = await _fast_display(ctx, guild_id, did, required_role)
            if query and query not in display.lower() and query not in did.lower():
                continue
            suggestions.append(f"{display} — {required_role} | {did}")
        await ctx.respond(suggestions[:25])
    except hikari.errors.NotFoundError:
        pass
    except Exception:
        try:
            await ctx.respond([])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)

async def tl_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    await _role_filtered_autocomplete(ctx, "TL")

async def pr_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    await _role_filtered_autocomplete(ctx, "PR")

async def ed_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    await _role_filtered_autocomplete(ctx, "ED")

async def dynamic_role_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    """For add_staff/remove_staff/edit_role: filter by the 'role' option already chosen."""
    try:
        # lightbulb stores options dict; try both ways
        opts = getattr(ctx, "options", {}) or {}
        # ctx.options may be a dict-like
        role = None
        if isinstance(opts, dict):
            role = opts.get("role")
        else:
            role = getattr(opts, "get", lambda *a, **k: None)("role")
        # Also try interaction options
        if not role:
            try:
                raw_opts = getattr(ctx.interaction, "options", []) or []
                for o in raw_opts:
                    if getattr(o, "name", "") == "role":
                        role = getattr(o, "value", None)
                        break
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
    except Exception:
        role = None
    if role in ("TL", "PR", "ED"):
        await _role_filtered_autocomplete(ctx, role)
    else:
        await registered_user_autocomplete(ctx)

async def manga_staff_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    """For remove_staff: show only staff of that manga."""
    try:
        try:
            manga_name = ctx.options.get("manga") or ""  # type: ignore
        except Exception:
            manga_name = ""
        pool = get_pool()
        if not manga_name:
            await registered_user_autocomplete(ctx)
            return
        async with pool.acquire() as conn:
            m_id = await conn.fetchval("SELECT id FROM mangas WHERE manga_name = $1", manga_name)
            if not m_id:
                await registered_user_autocomplete(ctx)
                return
            rows = await conn.fetch("SELECT discord_id, role FROM manga_staff WHERE manga_id = $1 LIMIT 25", m_id)
        guild_id = getattr(ctx, "guild_id", None) or getattr(getattr(ctx, "interaction", None), "guild_id", None)
        try:
            role = ctx.options.get("role")  # type: ignore
        except Exception:
            role = None
        if role in ("TL","PR","ED"):
            rows = [r for r in rows if r["role"] == role]
        suggestions = []
        for r in rows:
            did = r["discord_id"]
            display = await _fast_display(ctx, guild_id, did, r["role"])
            suggestions.append(f"{display} — {r['role']} | {did}")
        await ctx.respond(suggestions[:25])
    except hikari.errors.NotFoundError:
        pass
    except Exception:
        try:
            await ctx.respond([])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
