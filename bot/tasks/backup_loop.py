"""Automatic backup — pg_dump daily at 3 AM, keep last 7."""
import asyncio
import logging
import subprocess
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import lightbulb

from bot.config import DATABASE_URL, MONTHLY_TIMEZONE

logger = logging.getLogger(__name__)
loader = lightbulb.Loader()

BACKUP_DIR = Path("backups")
BACKUP_KEEP = 7


def _run_pg_dump_sync() -> tuple[bool, str]:
    """Synchronous pg_dump — returns (success, message). Called via to_thread."""
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = BACKUP_DIR / f"backup_{ts}.sql"
        import os
        from urllib.parse import urlparse
        env = os.environ.copy()
        try:
            parsed = urlparse(DATABASE_URL)
            if parsed.password:
                env["PGPASSWORD"] = parsed.password
                user = parsed.username or ""
                host = parsed.hostname or "localhost"
                port = str(parsed.port or 5432)
                dbname = parsed.path.lstrip("/") or "manga_db"
                cmd = ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", dbname, "-f", str(out)]
            else:
                cmd = ["pg_dump", DATABASE_URL, "-f", str(out)]
        except Exception:
            cmd = ["pg_dump", DATABASE_URL, "-f", str(out)]

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            try:
                size = out.stat().st_size
            except Exception:
                size = 0
            msg = f"Backup saved to {out} ({size} bytes)"
            # Keep last 7 backups
            all_backups = sorted(
                BACKUP_DIR.glob("backup_*.sql"),
                key=lambda p: p.stat().st_mtime
            )
            for old in all_backups[:-BACKUP_KEEP]:
                try:
                    old.unlink()
                    logger.info(f"Removed old backup {old}")
                except Exception as e:
                    logger.warning(f"Failed to remove old backup {old}: {e}")
            return True, msg
        else:
            err = (result.stderr or "")[:800]
            msg = f"pg_dump failed: {err}"
            logger.warning(msg)
            # Cleanup empty file if created
            try:
                if out.exists() and out.stat().st_size == 0:
                    out.unlink()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            return False, msg
    except FileNotFoundError:
        msg = "pg_dump not found — install PostgreSQL client tools or use docker: docker compose exec db pg_dump -U postgres manga_db > backup.sql"
        logger.warning(msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = "pg_dump timed out after 120s"
        logger.warning(msg)
        return False, msg
    except Exception as e:
        msg = f"Backup error: {e}"
        logger.warning(msg, exc_info=True)
        return False, msg


async def run_backup() -> tuple[bool, str]:
    """Public async helper — runs pg_dump in thread and rotates backups."""
    return await asyncio.to_thread(_run_pg_dump_sync)


# Try crontrigger if available (requires croniter), else hourly check
try:
    import croniter
    del croniter
    _HAS_CRON = True
except ImportError:
    _HAS_CRON = False

if _HAS_CRON:
    @loader.task(lightbulb.crontrigger("0 3 * * *"))
    async def backup_task() -> None:
        try:
            ok, msg = await run_backup()
            if ok:
                logger.info(f"Daily backup completed: {msg}")
            else:
                logger.warning(f"Daily backup failed: {msg}")
        except Exception as e:
            logger.error(f"Backup task error: {e}", exc_info=True)
else:
    @loader.task(lightbulb.uniformtrigger(3600))
    async def backup_task() -> None:
        """Hourly check — runs backup only when hour == 3 in configured timezone."""
        try:
            try:
                tz = ZoneInfo(MONTHLY_TIMEZONE)
            except Exception:
                tz = ZoneInfo("UTC")
            now = datetime.datetime.now(tz)
            if now.hour != 3:
                return
            ok, msg = await run_backup()
            if ok:
                logger.info(f"Daily backup completed: {msg}")
            else:
                logger.warning(f"Daily backup failed: {msg}")
        except Exception as e:
            logger.error(f"Backup task error: {e}", exc_info=True)
