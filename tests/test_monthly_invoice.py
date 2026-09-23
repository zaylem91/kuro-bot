import asyncio
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GUILD_ID", "123456789012345678")

from bot.tasks.monthly_invoice import (  # noqa: E402
    _claim_invoice_dispatch,
    _mark_invoice_failed,
    _mark_invoice_sent,
    INVOICE_SEND_LEASE,
)
import bot.tasks.monthly_invoice as monthly_invoice  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fake pool that simulates the invoice_dispatches SQL contract.
# This removes the requirement for a running PostgreSQL server.
# ---------------------------------------------------------------------------

class _FakeRow(dict):
    """Dict subclass that supports both mapping and index access like asyncpg Record."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _FakeConnection:
    """Minimal asyncpg-compatible connection backed by a shared dict store."""

    def __init__(self, store, lock):
        self._store = store
        self._lock = lock

    async def fetchrow(self, query, *args):
        async with self._lock:
            return self._fetchrow_locked(query, args)

    def _fetchrow_locked(self, query, args):
        if "INSERT INTO invoice_dispatches" in query:
            return self._claim_dispatch(args)
        if "SELECT status, attempt_count, last_error" in query:
            discord_id, billing_period = args[0], args[1]
            key = (discord_id, str(billing_period))
            row = self._store.get(key)
            if row is None:
                return None
            return _FakeRow(
                status=row["status"],
                attempt_count=row["attempt_count"],
                last_error=row["last_error"],
            )
        return None

    async def fetchval(self, query, *args):
        async with self._lock:
            if "SELECT status" in query:
                discord_id, billing_period = args[0], args[1]
                key = (discord_id, str(billing_period))
                row = self._store.get(key)
                if row is None:
                    return None
                return row["status"]
            return None

    def _claim_dispatch(self, args):
        discord_id = args[0]
        billing_period = args[1]
        retry_sent = args[2]
        recover_stale = args[3]
        # args[4] = INVOICE_SEND_LEASE (unused in fake)
        key = (discord_id, str(billing_period))

        existing = self._store.get(key)
        if existing is None:
            # Fresh INSERT
            self._store[key] = {
                "status": "sending",
                "attempt_count": 1,
                "last_error": None,
                "last_attempt_at": datetime.now(),
                "sent_at": None,
            }
            return _FakeRow(discord_id=discord_id)

        # ON CONFLICT — check WHERE conditions
        can_update = False
        if existing["status"] == "failed":
            can_update = True
        elif retry_sent and existing["status"] == "sent":
            can_update = True
        elif recover_stale and existing["status"] == "sending":
            last = existing.get("last_attempt_at")
            if last and (datetime.now() - last) > INVOICE_SEND_LEASE:
                can_update = True

        if not can_update:
            return None  # No RETURNING row → claim denied

        existing["status"] = "sending"
        existing["attempt_count"] += 1
        existing["last_attempt_at"] = datetime.now()
        existing["last_error"] = None
        return _FakeRow(discord_id=discord_id)

    async def execute(self, query, *args):
        async with self._lock:
            self._execute_locked(query, args)

    def _execute_locked(self, query, args):
        if "INSERT INTO invoice_dispatch_audit" in query:
            return
        if "UPDATE invoice_dispatches" not in query:
            return
        discord_id = args[0]
        billing_period = args[1]
        key = (discord_id, str(billing_period))
        existing = self._store.get(key)
        if existing is None:
            return
        if existing["status"] != "sending":
            return

        if "SET status = 'sent'" in query:
            existing["status"] = "sent"
            existing["sent_at"] = datetime.now()
            existing["last_error"] = None
        elif "SET status = 'failed'" in query:
            error_text = args[2]
            existing["status"] = "failed"
            existing["last_error"] = error_text

    async def fetch(self, query, *args):
        async with self._lock:
            if "u.is_registered = TRUE" in query:
                return self._store.get("__eligible_users__", [])
            return []


class _FakeAcquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return _FakeConnection(self._pool._store, self._pool._lock)

    async def __aexit__(self, *args):
        pass


class _FakePool:
    """In-memory pool that mimics asyncpg pool.acquire() context manager."""

    def __init__(self):
        self._store = {}
        self._lock = asyncio.Lock()

    def acquire(self):
        return _FakeAcquire(self)


# ---------------------------------------------------------------------------
# Scheduled-invoice helpers (adapted to work with the fake pool)
# ---------------------------------------------------------------------------

class _ScheduledInvoiceConnection:
    """Return a deterministic eligible user while using the real dispatch SQL."""

    def __init__(self, connection, user_row):
        self._connection = connection
        self._user_row = user_row

    async def fetch(self, query, *args):
        if "u.is_registered = TRUE" in query:
            return [self._user_row]
        return await self._connection.fetch(query, *args)

    async def fetchrow(self, query, *args):
        return await self._connection.fetchrow(query, *args)

    async def execute(self, query, *args):
        return await self._connection.execute(query, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _ScheduledInvoicePool:
    def __init__(self, pool, user_row):
        self._pool = pool
        self._user_row = user_row

    class _Acquire:
        def __init__(self, parent):
            self._parent = parent
            self._ctx = None

        async def __aenter__(self):
            self._ctx = self._parent._pool.acquire()
            connection = await self._ctx.__aenter__()
            return _ScheduledInvoiceConnection(connection, self._parent._user_row)

        async def __aexit__(self, *args):
            return await self._ctx.__aexit__(*args)

    def acquire(self):
        return self._Acquire(self)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class MonthlyInvoiceDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Verify the database-backed idempotency contract for monthly invoices."""

    async def asyncSetUp(self):
        self.pool = _FakePool()

    @property
    def user_id(self):
        return f"monthly-invoice-test-{self._testMethodName}"

    @property
    def billing_period(self):
        return date(2026, 8, 1)

    async def fetch_dispatch(self):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT status, attempt_count, last_error
                FROM invoice_dispatches
                WHERE discord_id = $1 AND billing_period = $2
                """,
                self.user_id,
                self.billing_period,
            )

    async def test_sent_dispatch_is_skipped_on_normal_rerun(self):
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )
        await _mark_invoice_sent(self.pool, self.user_id, self.billing_period)

        self.assertFalse(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )

        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sent")
        self.assertEqual(dispatch["attempt_count"], 1)
        self.assertIsNone(dispatch["last_error"])

    async def test_failed_dispatch_is_reclaimed_and_error_is_persisted(self):
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )
        error = RuntimeError("Discord rejected the invoice message")
        await _mark_invoice_failed(
            self.pool,
            self.user_id,
            self.billing_period,
            error,
        )

        failed_dispatch = await self.fetch_dispatch()
        self.assertEqual(failed_dispatch["status"], "failed")
        self.assertEqual(
            failed_dispatch["last_error"],
            "Discord rejected the invoice message",
        )

        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )
        reclaimed_dispatch = await self.fetch_dispatch()
        self.assertEqual(reclaimed_dispatch["status"], "sending")
        self.assertEqual(reclaimed_dispatch["attempt_count"], 2)
        self.assertIsNone(reclaimed_dispatch["last_error"])

    async def test_concurrent_claims_allow_one_sender(self):
        claims = await asyncio.gather(
            *(
                _claim_invoice_dispatch(
                    self.pool,
                    self.user_id,
                    self.billing_period,
                )
                for _ in range(8)
            )
        )

        self.assertEqual(sum(claims), 1)
        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sending")
        self.assertEqual(dispatch["attempt_count"], 1)

    async def test_retry_sent_requires_explicit_override(self):
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )
        await _mark_invoice_sent(self.pool, self.user_id, self.billing_period)

        self.assertFalse(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
                retry_sent=False,
            )
        )
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
                retry_sent=True,
            )
        )

        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sending")
        self.assertEqual(dispatch["attempt_count"], 2)

    async def test_scheduled_delivery_marks_sent_after_rest_and_skips_rerun(self):
        user_row = {
            "discord_id": self.user_id,
            "finance_channel_id": "987654321",
            "chapter_count": 1,
            "period_total": 1250,
            "unpaid_total": 1250,
        }
        scheduled_pool = _ScheduledInvoicePool(self.pool, user_row)
        bot = MagicMock()
        bot.rest.create_message = AsyncMock()

        async def successful_rest_call(*args, **kwargs):
            dispatch = await self.fetch_dispatch()
            self.assertEqual(dispatch["status"], "sending")

        bot.rest.create_message.side_effect = successful_rest_call

        class SeptemberFirst(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 1, 0, 5, tzinfo=tz)

        with (
            patch.object(monthly_invoice, "get_pool", return_value=scheduled_pool),
            patch.object(monthly_invoice, "datetime", SeptemberFirst),
        ):
            await monthly_invoice.send_monthly_invoices(bot)

        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sent")
        self.assertEqual(dispatch["attempt_count"], 1)
        self.assertEqual(bot.rest.create_message.await_count, 2)

        with (
            patch.object(monthly_invoice, "get_pool", return_value=scheduled_pool),
            patch.object(monthly_invoice, "datetime", SeptemberFirst),
        ):
            await monthly_invoice.send_monthly_invoices(bot)

        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sent")
        self.assertEqual(dispatch["attempt_count"], 1)
        self.assertEqual(bot.rest.create_message.await_count, 2)

    async def test_stale_lease_recovery(self):
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
            )
        )
        
        # Make the lease stale
        key = (self.user_id, str(self.billing_period))
        self.pool._store[key]["last_attempt_at"] = datetime.now() - timedelta(minutes=16)
        
        # Claim again with recover_stale_sending=True
        self.assertTrue(
            await _claim_invoice_dispatch(
                self.pool,
                self.user_id,
                self.billing_period,
                recover_stale_sending=True,
            )
        )
        
        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sending")
        self.assertEqual(dispatch["attempt_count"], 2)

    async def test_retry_invoice_dispatch_success(self):
        user_row = {
            "discord_id": self.user_id,
            "finance_channel_id": "987654321",
            "chapter_count": 1,
            "period_total": 1250,
            "unpaid_total": 1250,
        }
        
        bot = MagicMock()
        bot.rest.create_message = AsyncMock()

        # Insert a failed invoice dispatch
        key = (self.user_id, str(self.billing_period))
        self.pool._store[key] = {
            "status": "failed",
            "attempt_count": 1,
            "last_error": "Some error",
            "last_attempt_at": datetime.now(),
            "sent_at": None,
        }
        
        with (
            patch.object(monthly_invoice, "get_pool", return_value=self.pool),
            patch.object(monthly_invoice, "_fetch_invoice_user", return_value=user_row),
        ):
            result = await monthly_invoice.retry_invoice_dispatch(
                bot, self.user_id, self.billing_period, requested_by="admin-user"
            )

        self.assertEqual(result, "sent")
        self.assertEqual(bot.rest.create_message.await_count, 2)
        
        dispatch = await self.fetch_dispatch()
        self.assertEqual(dispatch["status"], "sent")
        self.assertEqual(dispatch["attempt_count"], 2)


if __name__ == "__main__":
    unittest.main()
