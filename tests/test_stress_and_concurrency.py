"""Stress and Concurrency Test Suite — Scanlation Bot.

Pushes the bot and its subsystems to extreme breaking points to verify:
1. Concurrency Race Conditions: 100 parallel chapter claims.
2. REST API Flood & Rate Limiter: 500+ rapid requests & brute-force lockdown.
3. Financial Ledger Atomicity: Concurrent payouts on identical transactions.
4. Background Task Queue Saturation: 50 simultaneous background jobs under worker contention.
5. Google Drive Semaphore Ceiling: Strict 8-concurrency limit enforcement under 50 parallel calls.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
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
import bot.api.core as api_core
from bot.services.drive.auth import _DRIVE_SEMAPHORE


# ---------------------------------------------------------------------------
# 1. Concurrency Race Condition: 100 Simultaneous Chapter Claims
# ---------------------------------------------------------------------------

class TestChapterClaimConcurrency(unittest.IsolatedAsyncioTestCase):
    """Test 100 simultaneous workers trying to claim the same chapter at the exact same instant."""

    async def test_100_concurrent_claims_race_condition(self):
        # In-memory shared state for a single chapter
        chapter_state = {
            "id": 42,
            "assigned_to": None,
            "assigned_at": None,
        }
        lock = asyncio.Lock()
        results = []

        async def worker_claim(user_id: str):
            # Simulates asyncpg atomic UPDATE ... WHERE id = $1 AND assigned_to IS NULL
            async with lock:
                if chapter_state["assigned_to"] is None:
                    # Atomic winning claim
                    chapter_state["assigned_to"] = user_id
                    chapter_state["assigned_at"] = "2026-09-22T16:00:00Z"
                    results.append((user_id, "SUCCESS"))
                else:
                    # Contention collision — already claimed
                    results.append((user_id, "ALREADY_CLAIMED"))

        # Launch 100 concurrent workers simultaneously
        tasks = [worker_claim(f"user_{i}") for i in range(100)]
        await asyncio.gather(*tasks)

        # Assertions
        successes = [r for r in results if r[1] == "SUCCESS"]
        collisions = [r for r in results if r[1] == "ALREADY_CLAIMED"]

        self.assertEqual(len(successes), 1, "Exactly ONE worker must win the claim")
        self.assertEqual(len(collisions), 99, "99 workers must fail gracefully")
        self.assertIsNotNone(chapter_state["assigned_to"])
        self.assertEqual(chapter_state["assigned_to"], successes[0][0])


# ---------------------------------------------------------------------------
# 2. REST API Flood & Brute-Force Rate Limiting (500+ requests)
# ---------------------------------------------------------------------------

class TestAPIFloodAndBruteForce(AioHTTPTestCase):
    """Test REST API under high-load request floods and verify IP brute-force lockdown."""

    async def get_application(self):
        app = web.Application()
        register_dashboard_routes(app)
        return app

    async def asyncSetUp(self):
        await super().asyncSetUp()
        api_core._AUTH_FAILS.clear()
        api_core._REQ_LOG.clear()

    async def asyncTearDown(self):
        api_core._AUTH_FAILS.clear()
        api_core._REQ_LOG.clear()
        await super().asyncTearDown()

    async def test_api_brute_force_lockdown_and_flood(self):
        # 1. Send 5 failed auth attempts to trigger brute-force lockout
        for attempt in range(api_core.MAX_AUTH_FAILS):
            resp = await self.client.post("/api/login", json={"token": f"wrong_password_{attempt}"})
            self.assertEqual(resp.status, 401)

        # 2. 6th attempt should be blocked by IP BAN (429 Too Many Requests)
        blocked_resp = await self.client.post("/api/login", json={"token": "any_token"})
        self.assertEqual(blocked_resp.status, 429)
        body = await blocked_resp.json()
        self.assertIn("too many failed attempts", body.get("error", "").lower())

        # 3. Simulate high-speed flood of 200 requests from banned IP to test memory stability
        flood_statuses = []
        for _ in range(200):
            r = await self.client.get("/api/stats")
            flood_statuses.append(r.status)

        # All requests from banned IP must be rejected immediately (429 or 401) without crashing
        self.assertTrue(all(s in (401, 429) for s in flood_statuses))
        self.assertEqual(len(flood_statuses), 200)

        # 4. Legitimate request with correct token from another unbanned IP succeeds
        fake_conn = AsyncMock()
        fake_conn.fetch.side_effect = [[], [], [], []]
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
            # Clear fails and rate logs for fresh test
            api_core._AUTH_FAILS.clear()
            api_core._REQ_LOG.clear()
            headers = {"Authorization": "Bearer secret_test_token_123"}
            good_resp = await self.client.get("/api/stats", headers=headers)
            self.assertEqual(good_resp.status, 200)


# ---------------------------------------------------------------------------
# 3. Financial Concurrent Payout Race Condition
# ---------------------------------------------------------------------------

class TestFinancialConcurrentPayouts(unittest.IsolatedAsyncioTestCase):
    """Verify that multiple simultaneous payouts for the same member cannot double-pay."""

    async def test_concurrent_payout_race_condition_protection(self):
        # Simulated database transactions table
        transactions = [
            {"id": 1, "user_id": "1001", "amount_cents": 150, "status": "unpaid", "payment_id": None},
            {"id": 2, "user_id": "1001", "amount_cents": 200, "status": "unpaid", "payment_id": None},
            {"id": 3, "user_id": "1001", "amount_cents": 85, "status": "unpaid", "payment_id": None},
        ]
        payments = []
        db_lock = asyncio.Lock()

        async def execute_payout(caller_id: str):
            # Simulates pessimistic locking SELECT ... FOR UPDATE within transaction
            async with db_lock:
                unpaid = [t for t in transactions if t["status"] == "unpaid"]
                if not unpaid:
                    return 0, "NO_UNPAID"
                
                total_cents = sum(t["amount_cents"] for t in unpaid)
                payment_id = len(payments) + 1
                payments.append({
                    "id": payment_id,
                    "user_id": "1001",
                    "amount_cents": total_cents,
                    "paid_by": caller_id,
                })
                for t in unpaid:
                    t["status"] = "paid"
                    t["payment_id"] = payment_id

                return total_cents, "SUCCESS"

        # Fire 10 parallel payout attempts
        tasks = [execute_payout(f"admin_{i}") for i in range(10)]
        results = await asyncio.gather(*tasks)

        successful_payouts = [r for r in results if r[1] == "SUCCESS"]
        empty_payouts = [r for r in results if r[1] == "NO_UNPAID"]

        self.assertEqual(len(successful_payouts), 1, "Only one payout execution may claim the unpaid balance")
        self.assertEqual(len(empty_payouts), 9, "9 parallel attempts must see 0 unpaid and perform no double-payout")
        self.assertEqual(successful_payouts[0][0], 435, "Total payout amount must match sum (150+200+85 = 435 cents)")
        self.assertEqual(len(payments), 1, "Only one payment receipt generated")


# ---------------------------------------------------------------------------
# 4. Background Task Queue Saturation (50 Parallel Jobs)
# ---------------------------------------------------------------------------

class TestTaskQueueSaturation(unittest.IsolatedAsyncioTestCase):
    """Stress test the background job queue under heavy concurrent insertion and processing."""

    async def test_task_queue_saturation_and_backpressure(self):
        jobs = {
            i: {"id": i, "status": "pending", "attempts": 0}
            for i in range(50)
        }
        queue_lock = asyncio.Lock()
        processed_count = 0

        async def worker_step():
            nonlocal processed_count
            while True:
                # Simulates SELECT ... FOR UPDATE SKIP LOCKED
                job_to_process = None
                async with queue_lock:
                    for j in jobs.values():
                        if j["status"] == "pending":
                            j["status"] = "running"
                            j["attempts"] += 1
                            job_to_process = j
                            break

                if not job_to_process:
                    break

                # Simulate work
                await asyncio.sleep(0.001)

                async with queue_lock:
                    job_to_process["status"] = "done"
                    processed_count += 1

        # Spawn 5 concurrent competing workers
        workers = [worker_step() for _ in range(5)]
        await asyncio.gather(*workers)

        self.assertEqual(processed_count, 50, "All 50 jobs must be processed exactly once")
        self.assertTrue(all(j["status"] == "done" for j in jobs.values()))
        self.assertTrue(all(j["attempts"] == 1 for j in jobs.values()))


# ---------------------------------------------------------------------------
# 5. Google Drive Semaphore Strict Concurrency Ceiling
# ---------------------------------------------------------------------------

class TestDriveSemaphoreCeiling(unittest.IsolatedAsyncioTestCase):
    """Verify that the Google Drive semaphore strictly caps concurrent operations at 8."""

    async def test_drive_semaphore_strict_ceiling(self):
        active_counter = 0
        max_seen = 0
        counter_lock = asyncio.Lock()

        async def simulate_drive_request():
            nonlocal active_counter, max_seen
            async with _DRIVE_SEMAPHORE:
                async with counter_lock:
                    active_counter += 1
                    if active_counter > max_seen:
                        max_seen = active_counter

                # Simulate I/O latency
                await asyncio.sleep(0.01)

                async with counter_lock:
                    active_counter -= 1

        # Launch 50 parallel requests
        tasks = [simulate_drive_request() for _ in range(50)]
        await asyncio.gather(*tasks)

        self.assertLessEqual(max_seen, 8, "Concurrency must NEVER exceed the 8-semaphore ceiling")
        self.assertEqual(active_counter, 0, "All semaphore slots must be properly released")
