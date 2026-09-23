import json
import sys

sys.path.insert(0, ".")

from scripts.audit_historical_work import (
    _get_uploader,
    _parse_timestamp,
    generate_reports,
)


def test_get_uploader_last_modifying():
    item = {
        "lastModifyingUser": {"emailAddress": "cleaner@gmail.com", "displayName": "Cleaner"},
        "owners": [{"emailAddress": "owner@gmail.com", "displayName": "Owner"}],
    }
    email, name = _get_uploader(item)
    assert email == "cleaner@gmail.com"
    assert name == "Cleaner"


def test_get_uploader_fallback_owner():
    item = {
        "owners": [{"emailAddress": "uploader@gmail.com", "displayName": "Uploader"}],
    }
    email, name = _get_uploader(item)
    assert email == "uploader@gmail.com"
    assert name == "Uploader"


def test_parse_timestamp():
    item = {"createdTime": "2026-07-15T14:30:00.000Z"}
    dt, month_str = _parse_timestamp(item)
    assert month_str == "2026-07"
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 15


def test_generate_reports(tmp_path):
    records = [
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "ED",
            "chapter_number": "10",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Chapter 10",
            "timestamp": "2026-08-10T12:00:00+00:00",
            "month": "2026-08",
        },
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "ED",
            "chapter_number": "11",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Chapter 11",
            "timestamp": "2026-08-15T12:00:00+00:00",
            "month": "2026-08",
        },
        {
            "manga_id": 2,
            "manga_name": "Tower of God",
            "stage": "TL",
            "chapter_number": "50",
            "uploader_email": "unregistered@gmail.com",
            "uploader_name": "Stranger",
            "item_name": "Ch 50",
            "timestamp": "2026-07-01T12:00:00+00:00",
            "month": "2026-07",
        },
    ]

    email_to_user = {
        "member1@gmail.com": {
            "discord_id": "111222333444",
            "drive_email": "member1@gmail.com",
            "default_role": "ED",
        }
    }
    discord_id_to_user = {
        "111222333444": {
            "discord_id": "111222333444",
            "drive_email": "member1@gmail.com",
            "default_role": "ED",
        }
    }

    md_file = tmp_path / "report.md"
    json_file = tmp_path / "report.json"

    generate_reports(
        records=records,
        email_to_user=email_to_user,
        discord_id_to_user=discord_id_to_user,
        output_md_path=str(md_file),
        output_json_path=str(json_file),
    )

    assert md_file.exists()
    assert json_file.exists()

    md_content = md_file.read_text(encoding="utf-8")
    assert "<@111222333444>" in md_content
    assert "Solo Leveling" in md_content
    assert "2026-08" in md_content
    assert "unregistered@gmail.com" in md_content
    assert "Tower of God" in md_content

    with open(json_file, "r", encoding="utf-8") as jf:
        data = json.load(jf)

    assert "111222333444" in data["registered_members"]
    assert "unregistered@gmail.com" in data["unregistered_uploaders"]
    user_months = data["registered_members"]["111222333444"]["months"]
    assert "2026-08" in user_months
    assert data["registered_members"]["111222333444"]["months"]["2026-08"]["Solo Leveling"]["ED"] == ["10", "11"]
    assert "raw_records" in data
    assert len(data["raw_records"]) == 3


class MockTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


class MockDbConnection:
    def __init__(self):
        self.chapters = {}  # (manga_id, chapter_number) -> {"id": int, "status": str}
        self.chapter_history = []  # list of dicts
        self.next_chapter_id = 1
        self.executed_queries = []

    def transaction(self):
        return MockTransaction()

    async def fetchrow(self, query, *args):
        self.executed_queries.append((query, args))
        if "FROM chapters WHERE manga_id = $1 AND chapter_number = $2" in query:
            manga_id, ch_num = args[0], args[1]
            key = (manga_id, ch_num)
            if key in self.chapters:
                return {"id": self.chapters[key]["id"], "status": self.chapters[key]["status"]}
            return None
        return None

    async def fetchval(self, query, *args):
        self.executed_queries.append((query, args))
        if "INSERT INTO chapters" in query:
            manga_id, ch_num, item_id, default_status, dt = args[0], args[1], args[2], args[3], args[4]
            cid = self.next_chapter_id
            self.next_chapter_id += 1
            self.chapters[(manga_id, ch_num)] = {
                "id": cid,
                "status": default_status,
                "drive_folder_id": item_id,
                "created_at": dt,
            }
            return cid
        elif "SELECT 1 FROM chapter_history" in query:
            cid, stage, user_id = args[0], args[1], args[2]
            for h in self.chapter_history:
                if h["chapter_id"] == cid and h["stage"] == stage and h["user_id"] == user_id and h["action"] == "done":
                    return 1
            return None
        return None

    async def execute(self, query, *args):
        self.executed_queries.append((query, args))
        if "INSERT INTO chapter_history" in query:
            cid, stage, user_id, dt = args[0], args[1], args[2], args[3]
            self.chapter_history.append({
                "chapter_id": cid,
                "stage": stage,
                "user_id": user_id,
                "action": "done",
                "amount_cents": 0,
                "reason": "historical_audit_import",
                "timestamp": dt,
            })
            return "INSERT 0 1"
        return "OK"


import pytest
from scripts.audit_historical_work import save_historical_work_to_db


@pytest.mark.asyncio
async def test_save_historical_work_to_db_fresh():
    records = [
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "ED",
            "chapter_number": "10",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Chapter 10",
            "timestamp": "2026-08-10T12:00:00+00:00",
            "month": "2026-08",
        },
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "ED",
            "chapter_number": "11",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Chapter 11",
            "timestamp": "2026-08-15T12:00:00+00:00",
            "month": "2026-08",
        },
    ]
    email_to_user = {
        "member1@gmail.com": {
            "discord_id": "111222333444",
            "drive_email": "member1@gmail.com",
            "default_role": "ED",
        }
    }

    mock_conn = MockDbConnection()
    stats = await save_historical_work_to_db(records, email_to_user, db_conn_or_url=mock_conn)

    assert stats["chapters_created"] == 2
    assert stats["chapters_existing"] == 0
    assert stats["history_inserted"] == 2
    assert stats["history_skipped"] == 0
    assert stats["unregistered_skipped"] == 0
    assert stats["transactions_created"] == 0

    assert len(mock_conn.chapter_history) == 2
    for h in mock_conn.chapter_history:
        assert h["amount_cents"] == 0
        assert h["action"] == "done"
        assert h["reason"] == "historical_audit_import"
        assert h["user_id"] == "111222333444"

    # Confirm no transactions query ever touched
    for q, _ in mock_conn.executed_queries:
        assert "transactions" not in q.lower()


@pytest.mark.asyncio
async def test_save_historical_work_to_db_idempotent():
    records = [
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "ED",
            "chapter_number": "10",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Chapter 10",
            "timestamp": "2026-08-10T12:00:00+00:00",
            "month": "2026-08",
        }
    ]
    email_to_user = {
        "member1@gmail.com": {
            "discord_id": "111222333444",
            "drive_email": "member1@gmail.com",
            "default_role": "ED",
        }
    }

    mock_conn = MockDbConnection()
    # First run
    stats1 = await save_historical_work_to_db(records, email_to_user, db_conn_or_url=mock_conn)
    assert stats1["chapters_created"] == 1
    assert stats1["history_inserted"] == 1

    # Second run (exact same data)
    stats2 = await save_historical_work_to_db(records, email_to_user, db_conn_or_url=mock_conn)
    assert stats2["chapters_created"] == 0
    assert stats2["chapters_existing"] == 1
    assert stats2["history_inserted"] == 0
    assert stats2["history_skipped"] == 1

    # Chapter history count remains 1, no duplicate rows
    assert len(mock_conn.chapter_history) == 1


@pytest.mark.asyncio
async def test_save_historical_work_to_db_unregistered_and_dry_run():
    records = [
        {
            "manga_id": 2,
            "manga_name": "Tower of God",
            "stage": "TL",
            "chapter_number": "50",
            "uploader_email": "stranger@gmail.com",
            "uploader_name": "Stranger",
            "item_name": "Ch 50",
            "timestamp": "2026-07-01T12:00:00+00:00",
            "month": "2026-07",
        }
    ]
    email_to_user = {}

    mock_conn = MockDbConnection()
    stats = await save_historical_work_to_db(records, email_to_user, db_conn_or_url=mock_conn)
    assert stats["unregistered_skipped"] == 1
    assert stats["history_inserted"] == 0
    assert len(mock_conn.chapter_history) == 0

    # Dry run test
    dry_records = [
        {
            "manga_id": 1,
            "manga_name": "Solo Leveling",
            "stage": "TL",
            "chapter_number": "1",
            "uploader_email": "member1@gmail.com",
            "uploader_name": "Member One",
            "item_name": "Ch 1",
            "timestamp": "2026-07-01T12:00:00+00:00",
            "month": "2026-07",
        }
    ]
    registered = {
        "member1@gmail.com": {
            "discord_id": "111",
            "drive_email": "member1@gmail.com",
        }
    }
    dry_stats = await save_historical_work_to_db(dry_records, registered, db_conn_or_url=mock_conn, dry_run=True)
    assert dry_stats["history_inserted"] == 1
    # Ensure dry-run did not alter mock_conn
    assert len(mock_conn.chapter_history) == 0
