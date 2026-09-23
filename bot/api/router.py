from __future__ import annotations
from aiohttp import web
from bot.api.core import logger, _guard

from bot.api.system import (
    dashboard_page, dashboard_redirect, dashboard_css, dashboard_js, dashboard_favicon, dashboard_locale,
    options_handler, api_login, api_logout, api_share_link, api_share_link_revoke,
    api_stats, api_jobs, api_pricing_get, api_pricing_put
)
from bot.api.manga import (
    api_mangas, api_manga_detail, api_manga_create, api_manga_status,
    api_manga_sync, api_manga_delete, api_manga_update, api_manga_create_channel,
    api_manga_add_staff, api_manga_remove_staff
)
from bot.api.chapters import api_chapters_overview, api_chapters, _chapter_action
from bot.api.finance import api_finance_overview, api_finance_export
from bot.api.staff import api_staff, api_staff_detail, api_staff_update, api_pay
from bot.api.database import (
    api_database_status, api_database_backups, api_database_backup_create,
    api_database_backup_download, api_database_backup_delete, api_database_backup_inspect,
    api_database_backup_upload
)

def register_dashboard_routes(app: web.Application) -> None:
    """Attach dashboard page + API to the existing health aiohttp app."""
    
    app.router.add_get("/dashboard", dashboard_redirect)
    app.router.add_get("/dashboard/", dashboard_page)
    app.router.add_get("/dashboard/style.css", dashboard_css)
    app.router.add_get("/dashboard/app.js", dashboard_js)
    app.router.add_get("/dashboard/favicon.ico", dashboard_favicon)
    app.router.add_get("/favicon.ico", dashboard_favicon)
    app.router.add_get(r"/dashboard/locales/{filename:[a-zA-Z0-9_\-]+\.js}", dashboard_locale)
    for path in ("/api/stats", "/api/mangas", "/api/chapters", "/api/staff",
                 "/api/finance/overview", "/api/pricing", "/api/jobs",
                 "/api/database/status", "/api/database/backups",
                 "/api/share-links", "/api/login", "/api/logout"):
        app.router.add_options(path, options_handler)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/share-links", api_share_link)
    app.router.add_delete("/api/share-links/{token}", api_share_link_revoke)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/mangas", api_mangas)
    app.router.add_post("/api/mangas", api_manga_create)
    app.router.add_get("/api/mangas/{manga_id}", api_manga_detail)
    app.router.add_put("/api/mangas/{manga_id}", api_manga_update)
    app.router.add_options("/api/mangas/{manga_id}", options_handler)
    app.router.add_put("/api/mangas/{manga_id}/status", api_manga_status)
    app.router.add_options("/api/mangas/{manga_id}/status", options_handler)
    app.router.add_post("/api/mangas/{manga_id}/create-channel", api_manga_create_channel)
    app.router.add_options("/api/mangas/{manga_id}/create-channel", options_handler)
    app.router.add_post("/api/mangas/{manga_id}/staff", api_manga_add_staff)
    app.router.add_options("/api/mangas/{manga_id}/staff", options_handler)
    app.router.add_delete("/api/mangas/{manga_id}/staff/{user_id}/{role}", api_manga_remove_staff)
    app.router.add_options("/api/mangas/{manga_id}/staff/{user_id}/{role}", options_handler)
    app.router.add_post("/api/mangas/{manga_id}/sync", api_manga_sync)
    app.router.add_options("/api/mangas/{manga_id}/sync", options_handler)
    app.router.add_delete("/api/mangas/{manga_id}", api_manga_delete)
    app.router.add_get("/api/chapters/overview", api_chapters_overview)
    app.router.add_get("/api/chapters", api_chapters)
    for _kind in ("approve", "reject", "reopen", "unclaim", "assign"):
        app.router.add_options(f"/api/chapters/{{chapter_id}}/{_kind}", options_handler)

    # NOTE: chapter action handlers are registered explicitly below
    # so each closure binds its own action kind (avoids late-binding bugs).
    async def _approve(req):
        denied = await _guard(req, write=True)
        if denied is not None:
            return denied
        return await _chapter_action(req, "approve")

    async def _reject(req):
        denied = await _guard(req, write=True)
        if denied is not None:
            return denied
        return await _chapter_action(req, "reject")

    async def _reopen(req):
        denied = await _guard(req, write=True)
        if denied is not None:
            return denied
        return await _chapter_action(req, "reopen")

    async def _unclaim(req):
        denied = await _guard(req, write=True)
        if denied is not None:
            return denied
        return await _chapter_action(req, "unclaim")

    async def _assign(req):
        denied = await _guard(req, write=True)
        if denied is not None:
            return denied
        return await _chapter_action(req, "assign")

    app.router.add_post("/api/chapters/{chapter_id}/approve", _approve)
    app.router.add_post("/api/chapters/{chapter_id}/reject", _reject)
    app.router.add_post("/api/chapters/{chapter_id}/reopen", _reopen)
    app.router.add_post("/api/chapters/{chapter_id}/unclaim", _unclaim)
    app.router.add_post("/api/chapters/{chapter_id}/assign", _assign)
    app.router.add_get("/api/staff", api_staff)
    app.router.add_options("/api/staff/{user_id}", options_handler)
    app.router.add_get("/api/staff/{user_id}", api_staff_detail)
    app.router.add_put("/api/staff/{user_id}", api_staff_update)
    app.router.add_options("/api/staff/{user_id}/pay", options_handler)
    app.router.add_post("/api/staff/{user_id}/pay", api_pay)
    app.router.add_get("/api/finance/overview", api_finance_overview)
    app.router.add_get("/api/finance/export", api_finance_export)
    app.router.add_options("/api/finance/export", options_handler)
    app.router.add_get("/api/pricing", api_pricing_get)
    app.router.add_put("/api/pricing", api_pricing_put)
    app.router.add_get("/api/jobs", api_jobs)
    
    # Database & Backups
    app.router.add_get("/api/database/status", api_database_status)
    app.router.add_get("/api/database/backups", api_database_backups)
    app.router.add_post("/api/database/backups", api_database_backup_create)
    app.router.add_options("/api/database/backups/upload", options_handler)
    app.router.add_post("/api/database/backups/upload", api_database_backup_upload)
    app.router.add_options("/api/database/backups/{filename}", options_handler)
    app.router.add_delete("/api/database/backups/{filename}", api_database_backup_delete)
    app.router.add_options("/api/database/backups/{filename}/download", options_handler)
    app.router.add_get("/api/database/backups/{filename}/download", api_database_backup_download)
    app.router.add_options("/api/database/backups/{filename}/inspect", options_handler)
    app.router.add_get("/api/database/backups/{filename}/inspect", api_database_backup_inspect)
    
    logger.info("Dashboard API registered (/dashboard + /api/*)")

