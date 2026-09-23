import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, ".")

os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ["DASHBOARD_TOKEN"] = "secret_test_token_123"

import bot.config as config
config.DASHBOARD_TOKEN = "secret_test_token_123"

from bot.api.router import register_dashboard_routes
import bot.api.database as db_api
from bot.tasks.backup_loop import BACKUP_DIR


class TestDatabaseBackupAPI(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_dashboard_routes(app)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import bot.api.core as core
        core._AUTH_FAILS.clear()
        core._REQ_LOG.clear()
        db_api._LAST_MANUAL_BACKUP_TIME = 0.0
        BACKUP_DIR.mkdir(exist_ok=True)

    async def asyncTearDown(self):
        import bot.api.core as core
        core._AUTH_FAILS.clear()
        core._REQ_LOG.clear()
        # Clean up any test backup files
        for f in BACKUP_DIR.glob("backup_*"):
            if "test" in f.name or "202699" in f.name:
                try:
                    f.unlink()
                except Exception:
                    pass
        await super().asyncTearDown()

    @property
    def auth_headers(self):
        return {"Authorization": "Bearer secret_test_token_123"}

    async def test_database_status_success(self):
        """Status endpoint should return pool stats, schema version, and entity counts."""
        fake_conn = AsyncMock()
        fake_conn.fetchrow.return_value = {"max_ver": 42}
        fake_conn.fetchval.side_effect = [
            10,  # mangas
            150, # chapters
            25,  # staff
            80,  # transactions
            3,   # background_jobs
        ]

        class FakePool:
            def get_size(self): return 5
            def get_idle_size(self): return 4
            def get_min_size(self): return 2
            def get_max_size(self): return 20

            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        with patch("bot.api.database.get_pool", return_value=FakePool()):
            resp = await self.client.get("/api/database/status", headers=self.auth_headers)
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["schema_version"], 42)
            self.assertEqual(data["pool"]["max"], 20)
            self.assertEqual(data["tables"]["mangas"], 10)
            self.assertEqual(data["tables"]["chapters"], 150)
            self.assertEqual(data["tables"]["staff"], 25)
            self.assertEqual(data["tables"]["users"], 25)
            self.assertIn("backups_count", data)

    async def test_database_backups_list(self):
        """Backups endpoint should list valid backup_*.sql files."""
        test_file = BACKUP_DIR / "backup_20269999_120000.sql"
        test_file.write_text("DUMMY SQL DATA")

        resp = await self.client.get("/api/database/backups", headers=self.auth_headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("backups", data)
        filenames = [b["filename"] for b in data["backups"]]
        self.assertIn("backup_20269999_120000.sql", filenames)

    async def test_database_backup_create_and_cooldown(self):
        """Triggering backup should run pg_dump and enforce 30s cooldown."""
        with patch("bot.api.database._run_pg_dump_sync", return_value=(True, "Backup saved")):
            resp = await self.client.post("/api/database/backups", headers=self.auth_headers)
            self.assertEqual(resp.status, 201)
            data = await resp.json()
            self.assertTrue(data["success"])

            # Calling again immediately should trigger 429 cooldown
            resp_cooldown = await self.client.post("/api/database/backups", headers=self.auth_headers)
            self.assertEqual(resp_cooldown.status, 429)

    async def test_database_backup_download_and_path_traversal_defense(self):
        """Downloading valid backup works; path traversal or invalid names are rejected."""
        test_file = BACKUP_DIR / "backup_20269999_130000.sql"
        test_file.write_text("-- HEADER TEST SQL CONTENT")

        # 1. Valid download
        resp = await self.client.get(
            "/api/database/backups/backup_20269999_130000.sql/download",
            headers=self.auth_headers
        )
        self.assertEqual(resp.status, 200)
        content = await resp.text()
        self.assertEqual(content, "-- HEADER TEST SQL CONTENT")
        self.assertIn("attachment; filename=", resp.headers.get("Content-Disposition", ""))

        # 2. Invalid filename pattern rejected
        resp_invalid = await self.client.get(
            "/api/database/backups/malicious.txt/download",
            headers=self.auth_headers
        )
        self.assertEqual(resp_invalid.status, 404)

        # 3. Path traversal attack rejected
        resp_traversal = await self.client.get(
            "/api/database/backups/..%2F..%2Fetc%2Fpasswd/download",
            headers=self.auth_headers
        )
        self.assertEqual(resp_traversal.status, 404)

    async def test_database_backup_delete_safe(self):
        """Deleting existing backup unlinks it; invalid or nonexistent returns 404."""
        test_file = BACKUP_DIR / "backup_20269999_140000.sql"
        test_file.write_text("TEMP CONTENT")
        self.assertTrue(test_file.exists())

        # 1. Delete existing
        resp = await self.client.delete(
            "/api/database/backups/backup_20269999_140000.sql",
            headers=self.auth_headers
        )
        self.assertEqual(resp.status, 200)
        self.assertFalse(test_file.exists())

        # 2. Delete non-existent
        resp_notfound = await self.client.delete(
            "/api/database/backups/backup_20269999_140000.sql",
            headers=self.auth_headers
        )
        self.assertEqual(resp_notfound.status, 404)

    async def test_database_guest_rejected(self):
        """Guest tokens with dash_share cookie must be blocked on database endpoints."""
        import time
        import bot.api.core as core
        token = "test_guest_token_123456789"
        core._SHARE_LINKS[token] = time.monotonic() + 3600

        self.client.session.cookie_jar.update_cookies({"dash_share": token})

        # Test status blocked
        resp_status = await self.client.get("/api/database/status")
        self.assertEqual(resp_status.status, 401)

        # Test backups list blocked
        resp_backups = await self.client.get("/api/database/backups")
        self.assertEqual(resp_backups.status, 401)

    async def test_database_backup_inspect_success_and_traversal(self):
        """Inspection parses SQL copy dumps properly and rejects path traversal."""
        test_file = BACKUP_DIR / "backup_20269999_150000.sql"
        content = (
            "-- PostgreSQL database dump\n"
            "SET statement_timeout = 0;\n\n"
            "COPY public.mangas (id, name, status) FROM stdin;\n"
            "1\tSolo Leveling\tongoing\n"
            "2\tTower of God\thiatus\n"
            "\\.\n\n"
            "COPY public.chapters (id, manga_id, chapter_number) FROM stdin;\n"
            "101\t1\t150\n"
            "\\.\n"
        )
        test_file.write_text(content, encoding="utf-8")

        # 1. Valid inspection
        resp = await self.client.get(
            "/api/database/backups/backup_20269999_150000.sql/inspect",
            headers=self.auth_headers
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["filename"], "backup_20269999_150000.sql")
        self.assertEqual(data["total_records"], 3)
        self.assertEqual(data["tables_count"], 2)
        tbl_names = [t["name"] for t in data["tables"]]
        self.assertIn("mangas", tbl_names)
        self.assertIn("chapters", tbl_names)

        mangas_tbl = next(t for t in data["tables"] if t["name"] == "mangas")
        self.assertEqual(mangas_tbl["count"], 2)
        self.assertEqual(len(mangas_tbl["sample"]), 2)
        self.assertEqual(mangas_tbl["sample"][0]["name"], "Solo Leveling")

        # 2. Traversal attempt rejected
        resp_traversal = await self.client.get(
            "/api/database/backups/..%2F..%2Fetc%2Fpasswd/inspect",
            headers=self.auth_headers
        )
        self.assertEqual(resp_traversal.status, 404)

        # 3. Non-existent file rejected
        resp_missing = await self.client.get(
            "/api/database/backups/backup_20269999_999999.sql/inspect",
            headers=self.auth_headers
        )
        self.assertEqual(resp_missing.status, 404)

    async def test_database_backup_upload_success(self):
        """Uploading valid SQL dump stores file safely with backup_ prefix."""
        import io
        from aiohttp import FormData

        sql_content = "-- Uploaded SQL Dump\nCREATE TABLE test (id int);\n"
        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(sql_content.encode("utf-8")),
            filename="my_test_backup.sql",
            content_type="application/sql",
        )

        resp = await self.client.post(
            "/api/database/backups/upload",
            data=data,
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status, 201)
        res_data = await resp.json()
        self.assertTrue(res_data["success"])
        saved_filename = res_data["backup"]["filename"]
        self.assertTrue(saved_filename.startswith("backup_"))
        self.assertTrue(saved_filename.endswith("_my_test_backup.sql"))

        # Verify the file actually exists on disk
        saved_path = BACKUP_DIR / saved_filename
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.read_text(encoding="utf-8"), sql_content)

        # Verify the uploaded backup can be inspected and deleted via existing endpoints
        resp_inspect = await self.client.get(
            f"/api/database/backups/{saved_filename}/inspect",
            headers=self.auth_headers,
        )
        self.assertEqual(resp_inspect.status, 200)

        # Cleanup
        saved_path.unlink(missing_ok=True)

    async def test_database_backup_upload_invalid_extension(self):
        """Uploading non-sql file should be rejected with 400."""
        import io
        from aiohttp import FormData

        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"Not a sql file"),
            filename="malicious_script.sh",
            content_type="text/plain",
        )

        resp = await self.client.post(
            "/api/database/backups/upload",
            data=data,
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status, 400)
        res_data = await resp.json()
        self.assertIn("Only .sql", res_data.get("error", ""))

    async def test_database_backup_upload_binary_rejected(self):
        """Uploading binary executable disguised as .sql should be rejected with 400."""
        import io
        from aiohttp import FormData

        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"MZ\x90\x00\x03\x00\x00\x00fake_binary"),
            filename="virus_disguised_test.sql",
            content_type="application/sql",
        )

        resp = await self.client.post(
            "/api/database/backups/upload",
            data=data,
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status, 400)
        res_data = await resp.json()
        self.assertIn("binary executable", res_data.get("error", ""))

    async def test_database_backup_upload_guest_rejected(self):
        """Guest cannot upload backups."""
        import io
        import time
        import bot.api.core as core
        from aiohttp import FormData

        token = "test_guest_upload_token"
        core._SHARE_LINKS[token] = time.monotonic() + 3600
        self.client.session.cookie_jar.update_cookies({"dash_share": token})

        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"-- sql"),
            filename="test_guest.sql",
            content_type="application/sql",
        )

        resp = await self.client.post(
            "/api/database/backups/upload",
            data=data,
        )
        self.assertEqual(resp.status, 401)


