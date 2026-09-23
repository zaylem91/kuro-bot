"""Reset database — drops public schema and reapplies schema.sql.

Safety: requires interactive confirmation + refuses to run in production.
"""
import asyncio
import asyncpg
import os
import sys
from pathlib import Path
from dotenv import load_dotenv


async def reset_db():
    # Load env from project root and bot/.env
    root_env = Path(__file__).parent / ".env"
    bot_env = Path(__file__).parent / "bot" / ".env"
    load_dotenv(dotenv_path=root_env, override=False)
    load_dotenv(dotenv_path=bot_env, override=False)
    if not root_env.exists() and not bot_env.exists():
        load_dotenv()

    # Safety: refuse to run in production
    env = os.getenv("ENVIRONMENT", "").lower().strip()
    if env in ("production", "prod"):
        print("\n❌ REFUSED: Cannot reset database in production environment.")
        print("   ENVIRONMENT is set to '%s'." % env)
        return

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("No DATABASE_URL found.")
        return

    # Extract DB name for display
    db_name = url.rsplit("/", 1)[-1] if "/" in url else "unknown"
    db_host = url.split("@")[-1] if "@" in url else url

    print("\n⚠️  WARNING: This will DROP the entire public schema and DELETE ALL DATA!")
    print(f"   Target: {db_host}")
    print(f"   Database: {db_name}")
    print()

    # Interactive confirmation: user must type the database name
    try:
        confirm = input(f"Type the database name '{db_name}' to confirm: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return

    if confirm != db_name:
        print(f"\n❌ Confirmation failed. You typed '{confirm}', expected '{db_name}'.")
        return

    print("\nConnecting to DB...")
    conn = await asyncpg.connect(url)
    try:
        print("Dropping public schema...")
        await conn.execute("DROP SCHEMA public CASCADE;")
        await conn.execute("CREATE SCHEMA public;")
        print("Applying migrations...")
        # Since we just dropped the schema, we can close this single connection
        # and use the connection.py init_db pool to run migrations properly
    finally:
        await conn.close()
    
    from bot.database.connection import init_db, close_db
    pool = await init_db(url)
    await close_db()
    
    print("\n✅ Database reset and migrations complete.")


if __name__ == "__main__":
    asyncio.run(reset_db())
