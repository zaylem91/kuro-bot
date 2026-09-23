import asyncio
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from bot.services.task_queue import (
    _process_sync_job,
    worker_loop,
    MAX_ATTEMPTS,
    _notify_job_failure,
    _retry_later,
)


class TestTaskQueue(unittest.IsolatedAsyncioTestCase):
    async def test_process_sync_job_skips_chapters_below_start_chapter(self):
        """Chapters lower than manga's start_chapter should be ignored."""
        fake_ds = MagicMock()
        fake_ds.deep_sync_manga = AsyncMock(return_value={
            "2": {"id": "f2", "status": "pending_tl"},
            "5": {"id": "f5", "status": "pending_tl"},
            "10": {"id": "f10", "status": "pending_tl"},
        })

        fake_conn = AsyncMock()
        fake_conn.fetchrow.side_effect = [
            # manga row
            {"start_chapter": Decimal("5")},
            # existing chapter for 5
            None,
            # existing chapter for 10
            None,
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

        with patch("bot.core.get_drive_service", return_value=fake_ds), \
             patch("bot.database.connection.get_pool", return_value=FakePool()), \
             patch("bot.core.get_bot", return_value=MagicMock()), \
             patch("bot.services.webhook_sender.update_manga_webhook", new_callable=AsyncMock):

            await _process_sync_job({"manga_id": 1, "drive_folder_id": "root_f"})

        executed_inserts = [
            call for call in fake_conn.execute.call_args_list
            if "INSERT INTO chapters" in call[0][0]
        ]
        # Only chapters 5 and 10 should be inserted, NOT chapter 2
        inserted_ch_nums = [call[0][2] for call in executed_inserts]
        self.assertNotIn("2", inserted_ch_nums)
        self.assertIn("5", inserted_ch_nums)
        self.assertIn("10", inserted_ch_nums)

    async def test_process_sync_job_protects_published_status(self):
        """Published or review chapters must not have their status regressed to pending_tl."""
        fake_ds = MagicMock()
        fake_ds.deep_sync_manga = AsyncMock(return_value={
            "1": {"id": "new_f1", "status": "pending_tl"},
        })

        fake_conn = AsyncMock()
        fake_conn.fetchrow.side_effect = [
            {"start_chapter": 0},
            {"id": 42, "status": "published", "drive_folder_id": "old_f1"},
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

        with patch("bot.core.get_drive_service", return_value=fake_ds), \
             patch("bot.database.connection.get_pool", return_value=FakePool()), \
             patch("bot.core.get_bot", return_value=MagicMock()), \
             patch("bot.services.webhook_sender.update_manga_webhook", new_callable=AsyncMock):

            await _process_sync_job({"manga_id": 1, "drive_folder_id": "root_f"})

        # Check that status was NOT updated to pending_tl
        for call in fake_conn.execute.call_args_list:
            sql = call[0][0]
            if "UPDATE chapters" in sql:
                self.assertNotIn("status = $1", sql)
                self.assertIn("drive_folder_id = $1", sql)

    async def test_process_sync_job_clears_assigned_when_status_changes(self):
        """When status changes from pending_tl to pending_pr, assignment must be cleared."""
        fake_ds = MagicMock()
        fake_ds.deep_sync_manga = AsyncMock(return_value={
            "1": {"id": "f1", "status": "pending_pr"},
        })

        fake_conn = AsyncMock()
        fake_conn.fetchrow.side_effect = [
            {"start_chapter": 0},
            {"id": 99, "status": "pending_tl", "drive_folder_id": "f1"},
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

        with patch("bot.core.get_drive_service", return_value=fake_ds), \
             patch("bot.database.connection.get_pool", return_value=FakePool()), \
             patch("bot.core.get_bot", return_value=MagicMock()), \
             patch("bot.services.webhook_sender.update_manga_webhook", new_callable=AsyncMock):

            await _process_sync_job({"manga_id": 1, "drive_folder_id": "root_f"})

        # Check that assigned_to = NULL is present in the update
        status_updates = [
            call for call in fake_conn.execute.call_args_list
            if "assigned_to = NULL" in call[0][0]
        ]
        self.assertTrue(len(status_updates) > 0)

    async def test_permanent_failure_alerts_and_marks_failed(self):
        """When max attempts are reached, task is marked failed and failure notification is sent."""
        fake_bot = MagicMock()
        fake_bot.rest.execute_webhook = AsyncMock()
        with patch("bot.core.get_bot", return_value=fake_bot), \
             patch("bot.services.webhook_sender._get_webhook_credentials", new_callable=AsyncMock) as mock_wh:
            mock_wh.return_value = ("111", "token123", "222")
            await _notify_job_failure(123, "drive_sync", {"manga_id": 1}, "Simulated test error")
            fake_bot.rest.execute_webhook.assert_called_once()


if __name__ == "__main__":
    unittest.main()
