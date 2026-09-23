import os
import logging
from pathlib import Path
import asyncpg

logger = logging.getLogger(__name__)

async def run_migrations(pool: asyncpg.Pool):
    """
    Run all pending database migrations sequentially.
    Ensures that schema updates are applied exactly once.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        logger.warning(f"Migrations directory not found at {migrations_dir}")
        return

    # Create schema_versions table if it doesn't exist
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_versions (
                version INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        # Determine current version
        current_version = await conn.fetchval("SELECT MAX(version) FROM schema_versions")
        if current_version is None:
            current_version = 0

        # Discover and sort migration files
        migration_files = []
        for file_path in migrations_dir.glob("*.sql"):
            try:
                # Expecting format: 001_description.sql
                version = int(file_path.stem.split("_")[0])
                migration_files.append((version, file_path))
            except ValueError:
                logger.warning(f"Ignoring improperly named migration file: {file_path.name}")
        
        migration_files.sort(key=lambda x: x[0])

        # Apply pending migrations
        for version, file_path in migration_files:
            if version > current_version:
                logger.info(f"Applying migration: {file_path.name} (Version {version})")
                with open(file_path, "r", encoding="utf-8") as f:
                    sql_content = f.read()

                # Execute migration inside a transaction
                async with conn.transaction():
                    await conn.execute(sql_content)
                    await conn.execute(
                        "INSERT INTO schema_versions (version, filename) VALUES ($1, $2)",
                        version, file_path.name
                    )
                logger.info(f"Successfully applied migration {version}")

        latest_version = await conn.fetchval("SELECT MAX(version) FROM schema_versions")
        logger.info(f"Database schema is up to date (Version {latest_version or 0})")
