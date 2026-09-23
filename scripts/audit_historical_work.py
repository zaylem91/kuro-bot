#!/usr/bin/env python3
"""
Historical Work Auditor — Scanlation Bot
-----------------------------------------
Connects to the database to fetch registered members' Gmails,
scans manga folders on Google Drive, extracts uploaders (owners/lastModifyingUser)
and timestamps (createdTime), groups historical work by member and month,
and generates comprehensive reports (Markdown & JSON).

IMPORTANT:
This script operates in STRICT READ-ONLY MODE.
It does NOT modify user balances, transactions, or payment tables.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import asyncpg
from aiogoogle import Aiogoogle
from aiogoogle.auth.creds import ServiceAccountCreds

from bot.config import DATABASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON
from bot.services.drive import extract_chapter_number, extract_drive_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("historical_auditor")


def _load_service_account_creds(sa_path_or_json: str) -> ServiceAccountCreds:
    """Load ServiceAccountCreds from file path or raw JSON string."""
    if isinstance(sa_path_or_json, str) and sa_path_or_json.strip().startswith("{"):
        raw = sa_path_or_json.strip().lstrip("\ufeff")
        data = json.loads(raw)
        return ServiceAccountCreds(scopes=["https://www.googleapis.com/auth/drive.readonly"], **data)

    with open(sa_path_or_json, "r", encoding="utf-8-sig") as f:
        content = f.read().strip().lstrip("\ufeff")
        data = json.loads(content)
    return ServiceAccountCreds(scopes=["https://www.googleapis.com/auth/drive.readonly"], **data)


def _get_uploader(item: dict) -> Tuple[Optional[str], Optional[str]]:
    """Extract (email, display_name) of the uploader/modifier from a Drive file resource."""
    # 1. Check lastModifyingUser
    mod = item.get("lastModifyingUser") or {}
    email = mod.get("emailAddress")
    name = mod.get("displayName")
    if email:
        return email.lower().strip(), name

    # 2. Check owners
    owners = item.get("owners") or []
    if owners and isinstance(owners, list):
        first_owner = owners[0]
        email = first_owner.get("emailAddress")
        name = first_owner.get("displayName")
        if email:
            return email.lower().strip(), name

    # 3. Check sharingUser
    sharing = item.get("sharingUser") or {}
    email = sharing.get("emailAddress")
    name = sharing.get("displayName")
    if email:
        return email.lower().strip(), name

    return None, None


def _parse_timestamp(item: dict) -> Tuple[datetime, str]:
    """Parse createdTime or modifiedTime from Drive file resource into (datetime, 'YYYY-MM')."""
    time_str = item.get("createdTime") or item.get("modifiedTime")
    if not time_str:
        dt = datetime.now()
        return dt, dt.strftime("%Y-%m")

    # Handle RFC 3339 / ISO 8601
    clean_ts = time_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean_ts)
    except Exception:
        dt = datetime.now()
    return dt, dt.strftime("%Y-%m")


async def fetch_db_data() -> Tuple[Dict[str, dict], Dict[str, dict], List[dict]]:
    """
    Fetch registered users and active mangas from the database.
    Returns:
      (email_to_user, discord_id_to_user, mangas)
    """
    logger.info("Connecting to database to fetch registered staff & manga records...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        users = await conn.fetch(
            """
            SELECT discord_id, drive_email, default_role, payment_method, is_registered
            FROM users
            WHERE is_registered = TRUE AND drive_email IS NOT NULL
            """
        )
        email_to_user: Dict[str, dict] = {}
        discord_id_to_user: Dict[str, dict] = {}
        for u in users:
            row_dict = dict(u)
            email = (u["drive_email"] or "").lower().strip()
            if email:
                email_to_user[email] = row_dict
            discord_id_to_user[str(u["discord_id"])] = row_dict

        mangas = await conn.fetch(
            """
            SELECT id, manga_name, drive_folder_id, final_subfolder_name, status
            FROM mangas
            WHERE status != 'dropped'
            ORDER BY manga_name ASC
            """
        )
        mangas_list = [dict(m) for m in mangas]
        logger.info(f"Loaded {len(email_to_user)} registered staff emails and {len(mangas_list)} mangas.")
        return email_to_user, discord_id_to_user, mangas_list
    finally:
        await conn.close()


async def scan_drive_manga(
    aiogoogle: Aiogoogle,
    drive_api: Any,
    manga: dict,
    email_to_user: Dict[str, dict],
    deep_inspect: bool = True,
) -> List[dict]:
    """
    Scan one manga folder on Google Drive and extract chapter upload events.
    """
    manga_name = manga["manga_name"]
    raw_folder_id = manga["drive_folder_id"]
    try:
        folder_id = extract_drive_id(raw_folder_id)
    except Exception:
        logger.warning(f"Skipping {manga_name}: invalid drive_folder_id '{raw_folder_id}'")
        return []

    logger.info(f"Scanning manga: '{manga_name}' (Drive ID: {folder_id})...")

    # List subfolders of the manga (RAW, TL, PR, ED, Final)
    fields_list = "nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, owners(emailAddress, displayName), lastModifyingUser(emailAddress, displayName))"
    try:
        req = drive_api.files.list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields=fields_list,
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        res = await aiogoogle.as_service_account(req)
        manga_children = res.get("files", [])
    except Exception as e:
        logger.warning(f"Could not list children of {manga_name}: {e}")
        return []

    # Map stage subfolders
    stage_subfolders = {}
    for item in manga_children:
        if item.get("mimeType") == "application/vnd.google-apps.folder":
            name_lower = item["name"].strip().lower()
            if name_lower in ("raw", "raws"):
                stage_subfolders["RAW"] = item
            elif name_lower in ("tl", "translate", "translation"):
                stage_subfolders["TL"] = item
            elif name_lower in ("pr", "proofread", "proofreading"):
                stage_subfolders["PR"] = item
            elif name_lower in ("ed", "edit", "editing", "final", "clean"):
                stage_subfolders["ED"] = item

    if not stage_subfolders:
        # Check if the manga folder directly contains chapter folders
        stage_subfolders["GENERAL"] = {"id": folder_id, "name": manga_name}

    records: List[dict] = []

    for stage_name, stage_folder in stage_subfolders.items():
        stage_fid = stage_folder["id"]
        page_token = None
        while True:
            try:
                req = drive_api.files.list(
                    q=f"'{stage_fid}' in parents and trashed=false",
                    fields=fields_list,
                    pageToken=page_token,
                    pageSize=100,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                res = await aiogoogle.as_service_account(req)
                items = res.get("files", [])
            except Exception as e:
                logger.warning(f"Error reading stage {stage_name} for {manga_name}: {e}")
                break

            for item in items:
                ch_num = extract_chapter_number(item["name"])
                if ch_num is None:
                    continue

                uploader_email, uploader_name = _get_uploader(item)
                dt, month_str = _parse_timestamp(item)

                # If it's a folder and uploader is unknown or a bot/service account,
                # check files inside to find the actual member who uploaded the pages
                is_folder = item.get("mimeType") == "application/vnd.google-apps.folder"
                if deep_inspect and is_folder and (not uploader_email or "gserviceaccount.com" in uploader_email or uploader_email not in email_to_user):
                    try:
                        inner_req = drive_api.files.list(
                            q=f"'{item['id']}' in parents and trashed=false",
                            fields="files(id, name, mimeType, createdTime, modifiedTime, owners(emailAddress, displayName), lastModifyingUser(emailAddress, displayName))",
                            pageSize=15,
                            supportsAllDrives=True,
                            includeItemsFromAllDrives=True,
                        )
                        inner_res = await aiogoogle.as_service_account(inner_req)
                        inner_files = inner_res.get("files", [])
                        for inner in inner_files:
                            inner_email, inner_name = _get_uploader(inner)
                            if inner_email and ("gserviceaccount.com" not in inner_email):
                                uploader_email = inner_email
                                uploader_name = inner_name or uploader_name
                                dt, month_str = _parse_timestamp(inner)
                                if inner_email in email_to_user:
                                    break
                    except Exception:
                        pass

                records.append({
                    "manga_id": manga["id"],
                    "manga_name": manga_name,
                    "stage": stage_name,
                    "chapter_number": str(ch_num),
                    "uploader_email": uploader_email or "unknown",
                    "uploader_name": uploader_name or "Unknown",
                    "item_name": item["name"],
                    "item_id": item.get("id") or stage_fid,
                    "timestamp": dt.isoformat(),
                    "month": month_str,
                })

            page_token = res.get("nextPageToken")
            if not page_token:
                break

    return records


def generate_reports(
    records: List[dict],
    email_to_user: Dict[str, dict],
    discord_id_to_user: Dict[str, dict],
    target_month: Optional[str] = None,
    output_md_path: str = "historical_work_report.md",
    output_json_path: str = "historical_work_report.json",
) -> None:
    """Generate markdown and JSON audit reports."""
    # Filter by month if requested
    if target_month:
        records = [r for r in records if r["month"] == target_month]

    # Grouping structures:
    member_work = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(set))))
    unregistered_work = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(set))))

    all_months = set()

    for r in records:
        email = r["uploader_email"].lower().strip()
        month = r["month"]
        all_months.add(month)
        manga = r["manga_name"]
        stage = r["stage"]
        ch = r["chapter_number"]

        if email in email_to_user:
            did = email_to_user[email]["discord_id"]
            member_work[did][month][manga][stage].add(ch)
        else:
            unregistered_work[email][month][manga][stage].add(ch)

    sorted_months = sorted(list(all_months), reverse=True)

    # Prepare JSON serializable structure
    json_data = {
        "generated_at": datetime.now().isoformat(),
        "total_records_scanned": len(records),
        "months_covered": sorted_months,
        "registered_members": {},
        "unregistered_uploaders": {},
        "raw_records": records,
    }

    for did, months_dict in member_work.items():
        u_info = discord_id_to_user.get(did, {})
        json_data["registered_members"][did] = {
            "discord_id": did,
            "drive_email": u_info.get("drive_email"),
            "default_role": u_info.get("default_role"),
            "months": {
                m: {
                    manga: {stg: sorted(list(chs), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else 0) for stg, chs in stgs.items()}
                    for manga, stgs in mangas.items()
                }
                for m, mangas in months_dict.items()
            }
        }

    for email, months_dict in unregistered_work.items():
        json_data["unregistered_uploaders"][email] = {
            "drive_email": email,
            "months": {
                m: {
                    manga: {stg: sorted(list(chs), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else 0) for stg, chs in stgs.items()}
                    for manga, stgs in mangas.items()
                }
                for m, mangas in months_dict.items()
            }
        }

    with open(output_json_path, "w", encoding="utf-8") as jf:
        json.dump(json_data, jf, indent=2, ensure_ascii=False)
    logger.info(f"Structured JSON report saved to: {output_json_path}")

    # Build Markdown Report
    lines = [
        "# 📊 Historical Work Audit Report",
        f"**Audit Date:** `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`  ",
        "**Audit Mode:** `Strict Read-Only` — Current balances and financial ledger records were not modified.  ",
        f"**Total Chapters Discovered:** `{len(records)}`  ",
        "",
        "---",
        "",
        "## 1. Monthly Summary for Registered Members",
        "",
        "| Member (Discord ID) | Registered Email | Month | Manga | Stage | Completed Chapters | Count |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for did in sorted(member_work.keys()):
        u_info = discord_id_to_user.get(did, {})
        email = u_info.get("drive_email", "N/A")
        for month in sorted(member_work[did].keys(), reverse=True):
            for manga in sorted(member_work[did][month].keys()):
                for stage, ch_set in member_work[did][month][manga].items():
                    sorted_chs = sorted(list(ch_set), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else 0)
                    chs_str = ", ".join(sorted_chs)
                    lines.append(f"| `<@{did}>` | `{email}` | **{month}** | {manga} | `{stage}` | {chs_str} | **{len(sorted_chs)}** |")

    lines.extend([
        "",
        "---",
        "",
        "## 2. Member Breakdown by Month",
        "",
    ])

    for did in sorted(member_work.keys()):
        u_info = discord_id_to_user.get(did, {})
        email = u_info.get("drive_email", "N/A")
        role = u_info.get("default_role", "Staff")

        total_member_chapters = sum(
            len(ch_set)
            for m in member_work[did].values()
            for mg in m.values()
            for ch_set in mg.values()
        )

        lines.append(f"### 👤 Member: <@{did}> (`{email}`) — Role: `{role}`")
        lines.append(f"**Total Discovered Chapters:** `{total_member_chapters}` chapters  ")
        lines.append("")

        for month in sorted(member_work[did].keys(), reverse=True):
            month_total = sum(len(ch_set) for mg in member_work[did][month].values() for ch_set in mg.values())
            lines.append(f"#### 📅 Month `{month}` (Total: **{month_total}** chapters):")
            for manga in sorted(member_work[did][month].keys()):
                stage_details = []
                for stage, ch_set in member_work[did][month][manga].items():
                    sorted_chs = sorted(list(ch_set), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else 0)
                    stage_details.append(f"Stage `{stage}`: chapters [{', '.join(sorted_chs)}]")
                lines.append(f"- **{manga}**: " + " | ".join(stage_details))
            lines.append("")

    if unregistered_work:
        lines.extend([
            "---",
            "",
            "## 3. ⚠️ Work Uploaded by Unregistered Accounts",
            "> Notice: These chapters were uploaded by emails not yet registered in the bot via `/register`:",
            "",
            "| Email | Month | Manga | Stage | Uploaded Chapters | Count |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for email in sorted(unregistered_work.keys()):
            for month in sorted(unregistered_work[email].keys(), reverse=True):
                for manga in sorted(unregistered_work[email][month].keys()):
                    for stage, ch_set in unregistered_work[email][month][manga].items():
                        sorted_chs = sorted(list(ch_set), key=lambda x: float(x) if x.replace('.', '', 1).isdigit() else 0)
                        lines.append(f"| `{email}` | **{month}** | {manga} | `{stage}` | {', '.join(sorted_chs)} | **{len(sorted_chs)}** |")

    with open(output_md_path, "w", encoding="utf-8") as mf:
        mf.write("\n".join(lines))
    logger.info(f"Markdown report saved to: {output_md_path}")


async def save_historical_work_to_db(
    records: List[dict],
    email_to_user: Dict[str, dict],
    db_conn_or_url: Any = DATABASE_URL,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Safely backfills historical work into 'chapters' and 'chapter_history'.

    GUARANTEES:
    1. NEVER creates or updates rows in 'transactions'.
    2. NEVER modifies user balances, payments, or ledger records.
    3. chapter_history.amount_cents is strictly 0 for every inserted row.
    4. chapter_history.reason is set to 'historical_audit_import'.
    5. Completely idempotent: duplicate chapters and duplicate history entries are skipped.
    """
    stats = {
        "total_records": len(records),
        "registered_actions": 0,
        "unregistered_skipped": 0,
        "chapters_created": 0,
        "chapters_existing": 0,
        "history_inserted": 0,
        "history_skipped": 0,
        "transactions_created": 0,
    }

    # Group actions by (manga_id, chapter_number, stage, user_id) to avoid intra-scan duplicates
    # and select the earliest timestamp for each action
    grouped_actions = {}
    for r in records:
        email = (r.get("uploader_email") or "").lower().strip()
        if not email or email not in email_to_user:
            stats["unregistered_skipped"] += 1
            continue

        stats["registered_actions"] += 1
        user_id = str(email_to_user[email]["discord_id"])
        manga_id = r["manga_id"]
        ch_num_str = str(r["chapter_number"])
        stage = r.get("stage", "ED")
        ts_str = r.get("timestamp")
        try:
            dt = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
        except Exception:
            dt = datetime.now()

        key = (manga_id, ch_num_str, stage, user_id)
        if key not in grouped_actions or dt < grouped_actions[key]["timestamp"]:
            grouped_actions[key] = {
                "manga_id": manga_id,
                "chapter_number": ch_num_str,
                "stage": stage,
                "user_id": user_id,
                "timestamp": dt,
                "item_id": r.get("item_id") or "historical_import",
            }

    if dry_run:
        logger.info(f"[DRY-RUN] Would process {len(grouped_actions)} distinct member actions into DB.")
        stats["history_inserted"] = len(grouped_actions)
        return stats

    close_conn = False
    if isinstance(db_conn_or_url, str):
        conn = await asyncpg.connect(db_conn_or_url)
        close_conn = True
    else:
        conn = db_conn_or_url

    try:
        async with conn.transaction():
            chapter_id_cache: Dict[Tuple[int, str], int] = {}

            for action_data in grouped_actions.values():
                manga_id = action_data["manga_id"]
                ch_str = action_data["chapter_number"]
                stage = action_data["stage"]
                user_id = action_data["user_id"]
                dt = action_data["timestamp"]
                item_id = action_data["item_id"]

                try:
                    ch_num = Decimal(ch_str)
                except Exception:
                    continue

                ch_key = (manga_id, ch_str)
                if ch_key not in chapter_id_cache:
                    row = await conn.fetchrow(
                        "SELECT id, status FROM chapters WHERE manga_id = $1 AND chapter_number = $2",
                        manga_id, ch_num,
                    )
                    if row:
                        chapter_id_cache[ch_key] = row["id"]
                        stats["chapters_existing"] += 1
                    else:
                        stage_upper = stage.upper()
                        if stage_upper in ("ED", "FINAL", "GENERAL"):
                            default_status = "published"
                        elif stage_upper == "PR":
                            default_status = "pending_ed"
                        elif stage_upper == "TL":
                            default_status = "pending_pr"
                        else:
                            default_status = "pending_tl"

                        new_id = await conn.fetchval(
                            """
                            INSERT INTO chapters (manga_id, chapter_number, drive_folder_id, status, created_at, updated_at)
                            VALUES ($1, $2, $3, $4, $5, $5)
                            ON CONFLICT (manga_id, chapter_number) DO UPDATE
                            SET updated_at = EXCLUDED.updated_at
                            RETURNING id
                            """,
                            manga_id, ch_num, item_id, default_status, dt,
                        )
                        chapter_id_cache[ch_key] = new_id
                        stats["chapters_created"] += 1

                cid = chapter_id_cache[ch_key]

                hist_exists = await conn.fetchval(
                    """
                    SELECT 1 FROM chapter_history
                    WHERE chapter_id = $1 AND stage = $2 AND user_id = $3 AND action = 'done'
                    LIMIT 1
                    """,
                    cid, stage, user_id,
                )

                if hist_exists:
                    stats["history_skipped"] += 1
                else:
                    await conn.execute(
                        """
                        INSERT INTO chapter_history (chapter_id, stage, user_id, action, amount_cents, reason, timestamp)
                        VALUES ($1, $2, $3, 'done', 0, 'historical_audit_import', $4)
                        """,
                        cid, stage, user_id, dt,
                    )
                    stats["history_inserted"] += 1

        logger.info(
            f"DB Sync completed: {stats['chapters_created']} chapters created, "
            f"{stats['chapters_existing']} existing chapters matched, "
            f"{stats['history_inserted']} history entries inserted, "
            f"{stats['history_skipped']} duplicate history entries skipped, "
            f"{stats['transactions_created']} transactions created (0 cents balance impact)."
        )
        return stats
    finally:
        if close_conn:
            await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Audit historical member work from Google Drive & Database.")
    parser.add_argument("--manga", type=str, default=None, help="Filter by manga name (substring match) or ID.")
    parser.add_argument("--month", type=str, default=None, help="Filter by specific month in YYYY-MM format.")
    parser.add_argument("--output", type=str, default="historical_work_report.md", help="Path for markdown report.")
    parser.add_argument("--json-output", type=str, default="historical_work_report.json", help="Path for JSON report.")
    parser.add_argument("--no-deep", action="store_true", help="Disable inspecting inside chapter folders for page uploaders.")
    parser.add_argument("--save-to-db", action="store_true", help="Save historical chapters and work records to database (strictly 0 cents / no balance change).")
    parser.add_argument("--dry-run", action="store_true", help="Simulate database import without modifying the database.")
    parser.add_argument("--from-json", type=str, default=None, help="Import records from previously generated JSON report instead of re-scanning Drive.")
    args = parser.parse_args()

    mode_label = "DB Sync Mode" if args.save_to_db else ("Dry Run Mode" if args.dry_run else "Read-Only Mode")
    logger.info(f"Starting Historical Work Audit ({mode_label})...")

    # 1. Fetch DB records
    email_to_user, discord_id_to_user, mangas = await fetch_db_data()

    # Filter mangas if requested
    if args.manga:
        q = args.manga.strip().lower()
        mangas = [m for m in mangas if q in m["manga_name"].lower() or str(m["id"]) == q]
        logger.info(f"Filtered mangas down to {len(mangas)} matching '{args.manga}'.")

    all_records: List[dict] = []

    if args.from_json:
        if not os.path.exists(args.from_json):
            logger.error(f"Specified JSON file does not exist: {args.from_json}")
            return
        logger.info(f"Loading audit data from existing JSON file: {args.from_json}")
        with open(args.from_json, "r", encoding="utf-8") as jf:
            loaded_json = json.load(jf)
        all_records = loaded_json.get("raw_records", [])
        if not all_records:
            if "registered_members" in loaded_json:
                logger.info("Found legacy JSON format — reconstructing records from 'registered_members'...")
                mangas_by_name = {m["manga_name"].lower().strip(): m["id"] for m in mangas}
                for did, u_data in loaded_json["registered_members"].items():
                    email = u_data.get("drive_email") or ""
                    for month, manga_dict in u_data.get("months", {}).items():
                        for m_name, stages_dict in manga_dict.items():
                            mid = mangas_by_name.get(m_name.lower().strip())
                            if not mid:
                                continue
                            for stage, ch_list in stages_dict.items():
                                for ch_str in ch_list:
                                    all_records.append({
                                        "manga_id": mid,
                                        "manga_name": m_name,
                                        "stage": stage,
                                        "chapter_number": ch_str,
                                        "uploader_email": email,
                                        "uploader_name": "Staff Member",
                                        "item_name": f"Chapter {ch_str}",
                                        "item_id": "historical_import",
                                        "timestamp": f"{month}-01T12:00:00+00:00",
                                        "month": month,
                                    })
                logger.info(f"Reconstructed {len(all_records)} actions from registered members.")
            else:
                logger.warning("No records found in JSON file.")
    else:
        if not mangas:
            logger.warning("No mangas to scan. Exiting.")
            return

        # 2. Init Google Drive Service
        creds = _load_service_account_creds(GOOGLE_SERVICE_ACCOUNT_JSON)

        async with Aiogoogle(service_account_creds=creds) as aiogoogle:
            drive_api = await aiogoogle.discover("drive", "v3")
            for manga in mangas:
                manga_records = await scan_drive_manga(
                    aiogoogle=aiogoogle,
                    drive_api=drive_api,
                    manga=manga,
                    email_to_user=email_to_user,
                    deep_inspect=not args.no_deep,
                )
                all_records.extend(manga_records)

        logger.info(f"Scanning completed. Total chapter actions discovered: {len(all_records)}")

        # 3. Generate Reports
        generate_reports(
            records=all_records,
            email_to_user=email_to_user,
            discord_id_to_user=discord_id_to_user,
            target_month=args.month,
            output_md_path=args.output,
            output_json_path=args.json_output,
        )

        print("\n" + "=" * 60)
        print("✅ Historical Work Audit Completed Successfully!")
        print(f"📄 Markdown Report: {os.path.abspath(args.output)}")
        print(f"📁 JSON Data:       {os.path.abspath(args.json_output)}")
        print("=" * 60 + "\n")

    # 4. Optional Database Sync (with 0 financial impact guarantee)
    if args.save_to_db or args.dry_run:
        logger.info("Processing historical work into database...")
        db_stats = await save_historical_work_to_db(
            records=all_records,
            email_to_user=email_to_user,
            db_conn_or_url=DATABASE_URL,
            dry_run=args.dry_run,
        )
        print("\n" + "=" * 60)
        mode_str = "🔍 Dry Run Result" if args.dry_run else "💾 Database Commit Result"
        print(f"{mode_str}:")
        print(f"  - New chapters created:        {db_stats['chapters_created']}")
        print(f"  - Existing chapters found:     {db_stats['chapters_existing']}")
        print(f"  - Completion records inserted: {db_stats['history_inserted']}")
        print(f"  - Records skipped (duplicate): {db_stats['history_skipped']}")
        print(f"  - Unregistered staff works:    {db_stats['unregistered_skipped']}")
        print(f"  - New ledger transactions:     {db_stats['transactions_created']} (Balances unaffected)")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
