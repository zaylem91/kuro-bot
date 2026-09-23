import asyncio
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

sys.path.insert(0, ".")

os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ["DASHBOARD_TOKEN"] = "secret_test_token_123"

import bot.config as config
config.DASHBOARD_TOKEN = "secret_test_token_123"

from bot.api.router import register_dashboard_routes


class TestDashboardAPI(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_dashboard_routes(app)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import bot.api.core as core
        core._AUTH_FAILS.clear()
        core._REQ_LOG.clear()

    async def asyncTearDown(self):
        import bot.api.core as core
        core._AUTH_FAILS.clear()
        core._REQ_LOG.clear()
        await super().asyncTearDown()

    async def test_auth_rejection_on_missing_token(self):
        """Requests without token or cookie should return 401."""
        resp = await self.client.get("/api/stats")
        self.assertEqual(resp.status, 401)

    async def test_auth_success_with_header(self):
        """Requests with correct Authorization header should pass auth."""
        fake_conn = AsyncMock()
        fake_conn.fetch.side_effect = [
            [{"status": "ongoing", "c": 2}],  # manga_rows
            [{"status": "pending_tl", "c": 3}],  # ch_rows
            [],  # jobs
            [],  # recent
        ]
        fake_conn.fetchval.return_value = 5  # staff_count
        fake_conn.fetchrow.return_value = {"n": 2, "s": 2000}  # unpaid

        class FakePool:
            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        with patch("bot.database.connection.get_pool", return_value=FakePool()):
            headers = {"Authorization": "Bearer secret_test_token_123"}
            resp = await self.client.get("/api/stats", headers=headers)
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data.get("staff_registered"), 5)

    async def test_manga_update_route(self):
        """PUT /api/mangas/{id} updates details."""
        fake_conn = AsyncMock()
        fake_conn.fetchrow.return_value = {
            "id": 1,
            "manga_name": "Old Manga Title",
            "drive_folder_id": "old_fid",
            "cover_url": "",
            "mangadex_url": "",
            "start_chapter": 0
        }

        class FakePool:
            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        with patch("bot.database.connection.get_pool", return_value=FakePool()):
            headers = {"Authorization": "Bearer secret_test_token_123"}
            payload = {
                "name": "Updated Manga Title",
                "start_chapter": 12.5,
                "drive_folder_id": "new_drive_123",
                "mangadex_id": "md_abc_123",
            }
            resp = await self.client.put("/api/mangas/1", headers=headers, json=payload)
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data.get("status"), "ok")

    async def test_manga_staff_add_and_remove(self):
        """POST and DELETE /api/mangas/{id}/staff."""
        fake_conn = AsyncMock()
        fake_conn.fetchrow.side_effect = [
            {"id": 1, "manga_name": "Test Manga", "discord_channel_id": None, "drive_folder_id": None},
            {"is_registered": True, "drive_email": "staff@example.com"},
            {"id": 1, "manga_name": "Test Manga", "discord_channel_id": None, "drive_folder_id": None},
            {"drive_email": "staff@example.com"},
        ]

        class FakeTx:
            async def __aenter__(self):
                return fake_conn
            async def __aexit__(self, *args):
                pass

        fake_conn.transaction = MagicMock(return_value=FakeTx())

        class FakePool:
            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        headers = {"Authorization": "Bearer secret_test_token_123"}

        with patch("bot.database.connection.get_pool", return_value=FakePool()), \
             patch("bot.core.get_bot", return_value=MagicMock()), \
             patch("bot.services.webhook_sender.update_manga_webhook", new_callable=AsyncMock):

            # Test add staff with valid snowflake and role
            add_resp = await self.client.post(
                "/api/mangas/1/staff",
                headers=headers,
                json={"user_id": "123456789012345678", "role": "translation"},
            )
            self.assertEqual(add_resp.status, 200)
            data = await add_resp.json()
            self.assertEqual(data.get("role"), "TL")

            # Test remove staff
            del_resp = await self.client.delete(
                "/api/mangas/1/staff/123456789012345678/translation",
                headers=headers,
            )
            self.assertEqual(del_resp.status, 200)
            data = await del_resp.json()
            self.assertEqual(data.get("role"), "TL")

    async def test_finance_export_csv(self):
        """GET /api/finance/export generates a valid CSV ledger."""
        fake_conn = AsyncMock()
        from datetime import datetime, timezone
        fake_conn.fetch.return_value = [
            {
                "id": 101,
                "manga_name": "Solo Leveling",
                "chapter_number": 150,
                "user_id": "111222333",
                "username": "TranslatorGuy",
                "role": "TL",
                "amount_cents": 85,
                "status": "paid",
                "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                "payment_id": 5,
            }
        ]

        class FakePool:
            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        with patch("bot.database.connection.get_pool", return_value=FakePool()):
            headers = {"Authorization": "Bearer secret_test_token_123"}
            resp = await self.client.get("/api/finance/export", headers=headers)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/csv; charset=utf-8")
            content = await resp.text()
            self.assertIn("Transaction ID,User ID,Manga,Chapter", content)
            self.assertIn("Solo Leveling", content)
            self.assertIn("111222333", content)
            self.assertIn("0.85", content)

    async def test_session_login_and_logout(self):
        """POST /api/login sets session cookie; /api/logout clears it."""
        resp = await self.client.post("/api/login", json={"token": "secret_test_token_123"})
        self.assertEqual(resp.status, 200)
        cookies = resp.cookies
        self.assertIn("dash_auth", cookies)
        session_id = cookies["dash_auth"].value
        # Raw token must NOT be in the cookie!
        self.assertNotEqual(session_id, "secret_test_token_123")

        fake_conn = AsyncMock()
        fake_conn.fetch.return_value = []
        fake_conn.fetchval.return_value = 0
        fake_conn.fetchrow.return_value = {"n": 0, "s": 0}

        class FakePool:
            def acquire(self):
                class FakeCtx:
                    async def __aenter__(self):
                        return fake_conn
                    async def __aexit__(self, *args):
                        pass
                return FakeCtx()

        with patch("bot.database.connection.get_pool", return_value=FakePool()):
            resp2 = await self.client.get("/api/stats", cookies={"dash_auth": session_id})
            self.assertEqual(resp2.status, 200)

        resp3 = await self.client.post("/api/logout", cookies={"dash_auth": session_id})
        self.assertEqual(resp3.status, 200)

        resp4 = await self.client.get("/api/stats", cookies={"dash_auth": session_id})
        self.assertEqual(resp4.status, 401)

    async def test_ip_spoofing_ignored_from_untrusted_remote(self):
        """X-Forwarded-For should be ignored when connection is not from trusted proxy."""
        from bot.api.core import _client_ip
        req = MagicMock()
        req.remote = "198.51.100.5"
        req.headers = {"X-Forwarded-For": "203.0.113.99"}
        self.assertEqual(_client_ip(req), "198.51.100.5")

        req.remote = "127.0.0.1"
        self.assertEqual(_client_ip(req), "203.0.113.99")

    async def test_dashboard_locales_served_properly(self):
        """Locales endpoint should serve valid js for all 5 languages and reject traversal."""
        for code in ("ar", "en", "fr", "es", "ru"):
            resp = await self.client.get(f"/dashboard/locales/{code}.js")
            self.assertEqual(resp.status, 200, f"Failed for locale {code}")
            self.assertIn("application/javascript", resp.headers.get("Content-Type", ""))
            text = await resp.text()
            self.assertIn("DASH_LOCALES", text)
            self.assertIn(f'"{code}"', text)

        # Non-existent locale should return 404
        resp_404 = await self.client.get("/dashboard/locales/non_existent.js")
        self.assertEqual(resp_404.status, 404)

        # Path traversal or invalid extensions should return 400 or 404
        resp_bad = await self.client.get("/dashboard/locales/../../secret")
        self.assertIn(resp_bad.status, (400, 404))

    async def test_dashboard_favicon_served_properly(self):
        """Favicon endpoints should serve binary icon file properly with correct mime."""
        for path in ("/dashboard/favicon.ico", "/favicon.ico"):
            resp = await self.client.get(path)
            self.assertEqual(resp.status, 200, f"Failed for {path}")
            self.assertEqual(resp.headers.get("Content-Type"), "image/x-icon")
            data = await resp.read()
            self.assertTrue(len(data) > 0)


if __name__ == "__main__":
    unittest.main()

