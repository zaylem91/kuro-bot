"""Simple pg_dump backup — run via cron or manually: python scripts/backup.py"""
import subprocess
from pathlib import Path
from datetime import datetime

from bot.config import DATABASE_URL

def main():
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = backup_dir / f"backup_{ts}.sql"
    # Try pg_dump if available
    try:
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

        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            print(f"Backup saved to {out} ({out.stat().st_size} bytes)")
            # Keep last 7 backups
            all_backups = sorted(
                backup_dir.glob("backup_*.sql"),
                key=lambda p: p.stat().st_mtime
            )
            for old in all_backups[:-7]:
                old.unlink()
                print(f"Removed old backup {old}")
        else:
            print(f"pg_dump failed: {result.stderr[:500]}")
            print("Ensure pg_dump is in PATH and DATABASE_URL is correct")
    except FileNotFoundError:
        print("pg_dump not found — install PostgreSQL client tools or use docker: docker compose exec db pg_dump -U postgres manga_db > backup.sql")
    except Exception as e:
        print(f"Backup error: {e}")

if __name__ == "__main__":
    # Load env
    try:
        main()
    except Exception as e:
        print(f"Backup failed: {e}")
        raise
