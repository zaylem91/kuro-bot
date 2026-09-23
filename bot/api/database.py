from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web

from bot.api.core import logger, _guard, _json, _err, _sec_headers
from bot.database.connection import get_pool
from bot.tasks.backup_loop import BACKUP_DIR, _run_pg_dump_sync

_SAFE_FILENAME_RE = re.compile(r"^(?:backup|db)_[a-zA-Z0-9_-]+\.sql$")
_LAST_MANUAL_BACKUP_TIME: float = 0.0
_MANUAL_BACKUP_COOLDOWN: float = 30.0  # seconds
MAX_BACKUP_UPLOAD_SIZE: int = 100 * 1024 * 1024  # 100 MB max upload



def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_safe_backup_path(filename: str) -> Path | None:
    """Validate filename strictly to prevent path traversal."""
    sanitized = Path(filename).name
    if not _SAFE_FILENAME_RE.match(sanitized):
        return None
    backup_dir = BACKUP_DIR.resolve()
    target = (backup_dir / sanitized).resolve()
    if not target.is_relative_to(backup_dir):
        return None
    if not target.is_file():
        return None
    return target


async def api_database_status(request: web.Request) -> web.Response:
    """Telemetry and stats about the database and backup storage."""
    denied = await _guard(request, write=False, allow_share=False)
    if denied is not None:
        return denied

    pool = get_pool()
    pool_stats = {
        "size": pool.get_size(),
        "free": pool.get_idle_size(),
        "min": pool.get_min_size(),
        "max": pool.get_max_size(),
    }

    schema_version = 0
    tables_stats = {
        "mangas": 0,
        "chapters": 0,
        "staff": 0,
        "transactions": 0,
        "background_jobs": 0,
    }
    async with pool.acquire() as conn:
        # Get highest applied migration
        try:
            row = await conn.fetchrow("SELECT COALESCE(MAX(version), 0) AS max_ver FROM schema_versions")
            if row:
                schema_version = row["max_ver"]
        except Exception:
            schema_version = 0

        # Query counts of main entities
        try:
            mangas = await conn.fetchval("SELECT COUNT(*) FROM mangas")
            chapters = await conn.fetchval("SELECT COUNT(*) FROM chapters")
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            transactions = await conn.fetchval("SELECT COUNT(*) FROM transactions")
            jobs = await conn.fetchval("SELECT COUNT(*) FROM background_jobs")
            tables_stats = {
                "mangas": mangas or 0,
                "chapters": chapters or 0,
                "staff": users_count or 0,
                "users": users_count or 0,
                "transactions": transactions or 0,
                "background_jobs": jobs or 0,
            }
        except Exception as e:
            logger.warning("Failed to query table counts for database status: %s", e)

    # Calculate backup storage metrics
    BACKUP_DIR.mkdir(exist_ok=True)
    backups = [b for b in BACKUP_DIR.glob("*.sql") if _SAFE_FILENAME_RE.match(b.name)]
    total_bytes = sum(b.stat().st_size for b in backups if b.is_file())
    total_records = (tables_stats["mangas"] + tables_stats["chapters"] +
                     tables_stats["staff"] + tables_stats["transactions"])

    return _json(request, {
        "schema_version": schema_version,
        "pool": pool_stats,
        "tables": tables_stats,
        "total_records": total_records,
        "backups_count": len(backups),
        "backups_total_size": total_bytes,
        "backups_total_size_human": _format_size(total_bytes),
    })


async def api_database_backups(request: web.Request) -> web.Response:
    """List all available .sql database backups."""
    denied = await _guard(request, write=False, allow_share=False)
    if denied is not None:
        return denied

    BACKUP_DIR.mkdir(exist_ok=True)
    backups = []
    matching_files = [f for f in BACKUP_DIR.glob("*.sql") if _SAFE_FILENAME_RE.match(f.name)]
    for f in sorted(matching_files, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            st = f.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            backups.append({
                "filename": f.name,
                "size_bytes": st.st_size,
                "size_human": _format_size(st.st_size),
                "created_at": mtime.isoformat(),
            })
        except Exception as e:
            logger.debug("Failed reading backup stat for %s: %s", f, e)

    return _json(request, {
        "backups": backups,
        "total": len(backups),
    })


async def api_database_backup_create(request: web.Request) -> web.Response:
    """Trigger an immediate pg_dump backup."""
    global _LAST_MANUAL_BACKUP_TIME
    denied = await _guard(request, write=True, allow_share=False)
    if denied is not None:
        return denied

    now = time.monotonic()
    elapsed = now - _LAST_MANUAL_BACKUP_TIME
    if elapsed < _MANUAL_BACKUP_COOLDOWN:
        remaining = int(_MANUAL_BACKUP_COOLDOWN - elapsed) + 1
        return _err(request, f"Backup creation cooldown active. Please wait {remaining}s.", 429)

    _LAST_MANUAL_BACKUP_TIME = now
    ok, msg = await asyncio.to_thread(_run_pg_dump_sync)
    if not ok:
        return _err(request, f"Backup failed: {msg}", 500)

    # Return the newly created backup info
    newest = None
    matching_files = [f for f in BACKUP_DIR.glob("*.sql") if _SAFE_FILENAME_RE.match(f.name)]
    backups = sorted(matching_files, key=lambda p: p.stat().st_mtime, reverse=True)
    if backups:
        newest_file = backups[0]
        st = newest_file.stat()
        newest = {
            "filename": newest_file.name,
            "size_bytes": st.st_size,
            "size_human": _format_size(st.st_size),
            "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        }

    return _json(request, {
        "success": True,
        "message": msg,
        "backup": newest,
    }, status=201)


async def api_database_backup_download(request: web.Request) -> web.StreamResponse:
    """Stream a .sql backup file securely for download."""
    denied = await _guard(request, write=False, allow_share=False)
    if denied is not None:
        return denied

    filename = request.match_info.get("filename", "")
    target = _get_safe_backup_path(filename)
    if target is None:
        return _err(request, "Backup file not found or invalid filename", 404)

    resp = web.FileResponse(
        target,
        headers={
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "Content-Type": "application/sql",
        }
    )
    _sec_headers(resp)
    return resp


async def api_database_backup_delete(request: web.Request) -> web.Response:
    """Delete an existing .sql backup."""
    denied = await _guard(request, write=True, allow_share=False)
    if denied is not None:
        return denied

    filename = request.match_info.get("filename", "")
    target = _get_safe_backup_path(filename)
    if target is None:
        return _err(request, "Backup file not found or invalid filename", 404)

    try:
        target.unlink()
        logger.info("[DASHBOARD] Backup %s deleted by admin", filename)
        return _json(request, {
            "success": True,
            "message": f"Backup {filename} deleted successfully",
        })
    except Exception as e:
        logger.error("[DASHBOARD] Failed deleting backup %s: %s", filename, e)
        return _err(request, f"Failed deleting backup: {e}", 500)


async def api_database_backup_upload(request: web.Request) -> web.Response:
    """Upload and store a .sql database backup file."""
    denied = await _guard(request, write=True, allow_share=False)
    if denied is not None:
        return denied

    if not request.content_type.startswith("multipart/"):
        return _err(request, "Expected multipart/form-data request", 400)

    try:
        reader = await request.multipart()
    except Exception as e:
        return _err(request, f"Failed parsing multipart request: {e}", 400)

    field = await reader.next()
    if field is None or not getattr(field, "filename", None):
        return _err(request, "No file provided in upload", 400)

    raw_filename = str(field.filename)
    if not raw_filename.lower().endswith(".sql"):
        return _err(request, "Only .sql database backup files are accepted", 400)

    stem = Path(raw_filename).stem
    clean_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:40].strip("_") or "backup"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_filename = f"backup_{ts}_{clean_stem}.sql"

    # Read the first chunk to inspect header before touching the filesystem
    try:
        first_chunk = await field.read_chunk(size=65536)
    except Exception as e:
        return _err(request, f"Failed reading upload stream: {e}", 400)

    if not first_chunk:
        return _err(request, "Uploaded file is empty", 400)

    # Reject binary executables (ELF, Windows PE / MZ, Mach-O, Java class)
    if (first_chunk.startswith(b"\x7fELF") or first_chunk.startswith(b"MZ") or
            first_chunk.startswith(b"\xca\xfe\xba\xbe") or first_chunk.startswith(b"\xfe\xed\xfa")):
        return _err(request, "File content is invalid or binary executable, expected SQL dump", 400)

    total_read = len(first_chunk)
    if total_read > MAX_BACKUP_UPLOAD_SIZE:
        return _err(request, f"File exceeds maximum allowed size of {_format_size(MAX_BACKUP_UPLOAD_SIZE)}", 413)

    BACKUP_DIR.mkdir(exist_ok=True)
    target_path = BACKUP_DIR / safe_filename
    size_exceeded = False

    try:
        with open(target_path, "wb") as f:
            f.write(first_chunk)
            while True:
                chunk = await field.read_chunk(size=65536)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > MAX_BACKUP_UPLOAD_SIZE:
                    size_exceeded = True
                    break
                f.write(chunk)
    except Exception as e:
        target_path.unlink(missing_ok=True)
        logger.error("[DASHBOARD] Failed saving uploaded backup: %s", e)
        return _err(request, f"Failed saving backup: {e}", 500)

    if size_exceeded:
        target_path.unlink(missing_ok=True)
        return _err(request, f"File exceeds maximum allowed size of {_format_size(MAX_BACKUP_UPLOAD_SIZE)}", 413)

    st = target_path.stat()
    logger.info("[DASHBOARD] Backup uploaded by admin: %s (%s)", safe_filename, _format_size(st.st_size))

    return _json(request, {
        "success": True,
        "message": "Backup uploaded successfully",
        "backup": {
            "filename": safe_filename,
            "size_bytes": st.st_size,
            "size_human": _format_size(st.st_size),
            "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        }
    }, status=201)


_COPY_REGEX = re.compile(r"^COPY\s+(?:public\.)?([a-zA-Z0-9_]+)\s*\((.*?)\)\s+FROM\s+stdin;", re.IGNORECASE)

TABLE_METADATA = {
    # Core Application Data
    "mangas": {
        "icon": "📚",
        "category": "core",
        "title_key": "tblMangas",
        "desc_key": "tblMangasDesc",
        "row_key": "tblMangasRow",
        "name": "mangas",
        "order": 1,
    },
    "chapters": {
        "icon": "📄",
        "category": "core",
        "title_key": "tblChapters",
        "desc_key": "tblChaptersDesc",
        "row_key": "tblChaptersRow",
        "name": "chapters",
        "order": 2,
    },
    "users": {
        "icon": "👥",
        "category": "core",
        "title_key": "tblUsers",
        "desc_key": "tblUsersDesc",
        "row_key": "tblUsersRow",
        "name": "users",
        "order": 3,
    },
    "user_roles": {
        "icon": "🎖️",
        "category": "core",
        "title_key": "tblUserRoles",
        "desc_key": "tblUserRolesDesc",
        "row_key": "tblUserRolesRow",
        "name": "user_roles",
        "order": 4,
    },
    "manga_staff": {
        "icon": "🤝",
        "category": "core",
        "title_key": "tblMangaStaff",
        "desc_key": "tblMangaStaffDesc",
        "row_key": "tblMangaStaffRow",
        "name": "manga_staff",
        "order": 5,
    },
    # Finance & Ledger
    "transactions": {
        "icon": "💳",
        "category": "finance",
        "title_key": "tblTransactions",
        "desc_key": "tblTransactionsDesc",
        "row_key": "tblTransactionsRow",
        "name": "transactions",
        "order": 6,
    },
    "payments": {
        "icon": "🧾",
        "category": "finance",
        "title_key": "tblPayments",
        "desc_key": "tblPaymentsDesc",
        "row_key": "tblPaymentsRow",
        "name": "payments",
        "order": 7,
    },
    "pricing_config": {
        "icon": "⚙️",
        "category": "finance",
        "title_key": "tblPricing",
        "desc_key": "tblPricingDesc",
        "row_key": "tblPricingRow",
        "name": "pricing_config",
        "order": 8,
    },
    "invoice_dispatches": {
        "icon": "📨",
        "category": "finance",
        "title_key": "tblInvoiceDispatches",
        "desc_key": "tblInvoiceDispatchesDesc",
        "row_key": "tblInvoiceDispatchesRow",
        "name": "invoice_dispatches",
        "order": 9,
    },
    # Audit & History Logs
    "chapter_history": {
        "icon": "📜",
        "category": "audit",
        "title_key": "tblChapterHistory",
        "desc_key": "tblChapterHistoryDesc",
        "row_key": "tblChapterHistoryRow",
        "name": "chapter_history",
        "order": 10,
    },
    "user_audit": {
        "icon": "📝",
        "category": "audit",
        "title_key": "tblUserAudit",
        "desc_key": "tblUserAuditDesc",
        "row_key": "tblUserAuditRow",
        "name": "user_audit",
        "order": 11,
    },
    "price_audit": {
        "icon": "🏷️",
        "category": "audit",
        "title_key": "tblPriceAudit",
        "desc_key": "tblPriceAuditDesc",
        "row_key": "tblPriceAuditRow",
        "name": "price_audit",
        "order": 12,
    },
    "manga_staff_audit": {
        "icon": "📋",
        "category": "audit",
        "title_key": "tblMangaStaffAudit",
        "desc_key": "tblMangaStaffAuditDesc",
        "row_key": "tblMangaStaffAuditRow",
        "name": "manga_staff_audit",
        "order": 13,
    },
    "invoice_dispatch_audit": {
        "icon": "🔍",
        "category": "audit",
        "title_key": "tblInvoiceAudit",
        "desc_key": "tblInvoiceAuditDesc",
        "row_key": "tblInvoiceAuditRow",
        "name": "invoice_dispatch_audit",
        "order": 14,
    },
    # System & Queue
    "background_jobs": {
        "icon": "⏳",
        "category": "system",
        "title_key": "tblJobs",
        "desc_key": "tblJobsDesc",
        "row_key": "tblJobsRow",
        "name": "background_jobs",
        "order": 15,
    },
    "chapter_cache": {
        "icon": "⚡",
        "category": "system",
        "title_key": "tblChapterCache",
        "desc_key": "tblChapterCacheDesc",
        "row_key": "tblChapterCacheRow",
        "name": "chapter_cache",
        "order": 16,
    },
    "schema_versions": {
        "icon": "🗂️",
        "category": "system",
        "title_key": "tblSchema",
        "desc_key": "tblSchemaDesc",
        "row_key": "tblSchemaRow",
        "name": "schema_versions",
        "order": 17,
    },
}


def _parse_sql_backup(path: Path) -> dict:
    """Stream and parse a pg_dump SQL file to extract table row counts and samples."""
    tables = {}
    total_records = 0
    current_table = None
    current_cols = []

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue

            copy_match = _COPY_REGEX.match(stripped)
            if copy_match:
                t_name = copy_match.group(1).lower()
                raw_cols = copy_match.group(2)
                current_cols = [c.strip().strip('"') for c in raw_cols.split(",") if c.strip()]
                current_table = t_name
                if current_table not in tables:
                    meta = TABLE_METADATA.get(current_table, {
                        "name": current_table,
                        "title_key": f"tbl_{current_table}",
                        "desc_key": f"tbl_{current_table}_desc",
                        "row_key": f"tbl_{current_table}_row",
                        "icon": "📊",
                        "category": "system",
                        "order": 99,
                    })
                    tables[current_table] = {
                        "name": current_table,
                        "title_key": meta.get("title_key", f"tbl_{current_table}"),
                        "desc_key": meta.get("desc_key", f"tbl_{current_table}_desc"),
                        "row_key": meta.get("row_key", f"tbl_{current_table}_row"),
                        "icon": meta.get("icon", "📊"),
                        "category": meta.get("category", "system"),
                        "order": meta.get("order", 99),
                        "columns": current_cols,
                        "count": 0,
                        "sample": [],
                    }
                continue

            if stripped == r"\.":
                current_table = None
                current_cols = []
                continue

            if current_table and current_table in tables:
                t_info = tables[current_table]
                t_info["count"] += 1
                total_records += 1
                if len(t_info["sample"]) < 10:
                    raw_vals = line.rstrip("\r\n").split("\t")
                    row_dict = {}
                    for i, col in enumerate(current_cols):
                        val = raw_vals[i] if i < len(raw_vals) else ""
                        if val == r"\N":
                            val = None
                        row_dict[col] = val
                    t_info["sample"].append(row_dict)

    summary_counts = {
        "mangas": tables.get("mangas", {}).get("count", 0),
        "chapters": tables.get("chapters", {}).get("count", 0),
        "staff": tables.get("users", {}).get("count", tables.get("staff", {}).get("count", 0)),
        "transactions": tables.get("transactions", {}).get("count", 0),
    }

    sorted_tables = sorted(tables.values(), key=lambda t: t.get("order", 99))

    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "size_human": _format_size(path.stat().st_size),
        "total_records": total_records,
        "tables_count": len(tables),
        "summary": summary_counts,
        "tables": sorted_tables,
    }


async def api_database_backup_inspect(request: web.Request) -> web.Response:
    """Inspect and preview tables and records inside a .sql backup."""
    denied = await _guard(request, write=False, allow_share=False)
    if denied is not None:
        return denied

    filename = request.match_info.get("filename", "")
    target = _get_safe_backup_path(filename)
    if target is None:
        return _err(request, "Backup file not found or invalid filename", 404)

    try:
        parsed = await asyncio.to_thread(_parse_sql_backup, target)
        return _json(request, parsed)
    except Exception as e:
        logger.error("[DASHBOARD] Failed inspecting backup %s: %s", filename, e)
        return _err(request, f"Failed inspecting backup: {e}", 500)

