import asyncio
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Optional
from aiogoogle import Aiogoogle

try:
    from bot.services.cache import drive_deep_sync_cache
except Exception:
    drive_deep_sync_cache = None

logger = logging.getLogger(__name__)

VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".psd"}
DRIVE_SYNC_CHAPTER_BATCH_SIZE = 10

def extract_chapter_number(name: str) -> Optional[Decimal]:
    """Robustly extract chapter number from folder or file name."""
    if not name:
        return None
    # 1. Clean resolution tags (e.g., 1080p, [720p], 4k, 2160p)
    cleaned = re.sub(r"(?i)\[?\b\d{3,4}p\b\]?", "", name)
    # 2. Clean version tags: e.g. v2, _v2, -v2, .v2, (v2), [v2], or attached directly like 10v2, 4.5v2
    cleaned = re.sub(r"(?i)(?:[\s._\-\(\[]+|\b|(?<=\d))(?:v|ver|version)\.?\s*\d+[\)\]]?(?=$|[\s._\-\)\]])", "", cleaned)
    # Strip empty brackets/parentheses left after cleaning: () or []
    cleaned = re.sub(r"[\(\[]\s*[\)\]]", "", cleaned).strip()

    # 3. Prefer explicit chapter prefix (e.g., Chapter 10, Ch. 10, Chap 10, Cap 10, c10, Ep 10)
    # Use (?![0-9.]) so word boundary is not blocked by attached characters
    ch_prefix = re.search(r"(?i)\b(?:chapter|chap|ch|cap|c|episode|ep)[\s._#-]*(\d+(?:\.\d+)?)(?![0-9.])", cleaned)
    if ch_prefix:
        try:
            return Decimal(ch_prefix.group(1))
        except (InvalidOperation, ValueError):
            pass

    # 4. If Volume or Season exists without a chapter prefix, ignore the volume/season number
    cleaned_no_vol = re.sub(r"(?i)\b(?:vol|volume|season|s)\.?\s*\d+\b", "", cleaned)

    # 5. Fallback: extract remaining number candidates
    matches = re.findall(r"\d+(?:\.\d+)?", cleaned_no_vol)
    if not matches:
        matches = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not matches:
        return None
    try:
        return Decimal(matches[-1])
    except (InvalidOperation, ValueError):
        return None

def _to_decimal(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None

def extract_drive_id(input_str: str) -> str:
    """
    Extract Drive folder/file ID from a full URL or raw ID.
    Supports all common Drive link formats:
      - https://drive.google.com/drive/folders/<ID>
      - https://drive.google.com/drive/folders/<ID>?usp=sharing
      - https://drive.google.com/drive/u/0/folders/<ID>
      - https://drive.google.com/open?id=<ID>
      - https://drive.google.com/file/d/<ID>/view
      - https://drive.google.com/uc?id=<ID>
      - https://docs.google.com/.../d/<ID>/...
      - raw ID itself
    Handles copy-paste with extra spaces, angle brackets, trailing slashes, query strings.
    Raises ValueError if no valid ID found.
    """
    import re
    if not input_str or not input_str.strip():
        raise ValueError("Drive link is empty")
    text = input_str.strip().strip("<>").strip()
    # Remove zero-width / invisible chars that sometimes get copied
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    # Direct ID (raw)
    if re.fullmatch(r"[A-Za-z0-9_-]{15,}", text):
        return text
    # Try common URL patterns in order of specificity
    patterns = [
        r"folders/([A-Za-z0-9_-]{10,})",
        r"[?&]id=([A-Za-z0-9_-]{10,})",
        r"/d/([A-Za-z0-9_-]{10,})",
        r"drive\.google\.com[^A-Za-z0-9_-]*([A-Za-z0-9_-]{15,})",  # fallback for unusual forms
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            candidate = m.group(1)
            # Strip any trailing query/fragment that might have been captured due to greedy
            candidate = candidate.split("?")[0].split("&")[0].split("#")[0].rstrip("/")
            # Validate candidate length
            if re.fullmatch(r"[A-Za-z0-9_-]{15,}", candidate):
                return candidate
            # If pattern captured 10+ but we want 15+, try to continue searching for longer match
            # Fallthrough to generic finder
    # Generic fallback: find all ID-like substrings (15+ chars) and pick the longest / most plausible
    # Filter out common English words that can match [A-Za-z_-] but are not IDs
    candidates = re.findall(r"[A-Za-z0-9_-]{15,}", text)
    # Ignore known URL fragments
    ignore = {"drive", "google", "folders", "open", "sharing", "usp", "view", "file", "document", "spreadsheets", "presentation", "drive/folders"}
    filtered = [c for c in candidates if c.lower() not in ignore and not c.lower().startswith("http")]
    if filtered:
        # Drive IDs are typically 28-44 chars and contain mixed case + digits + -_
        # Prefer longest candidate (most likely the real ID) that looks like an ID (has at least one digit or mix)
        # Sort by length desc, then by having digits/upper/lower mix
        def score(s: str) -> tuple:
            has_digit = any(ch.isdigit() for ch in s)
            has_upper = any(ch.isupper() for ch in s)
            has_mix = has_digit and has_upper
            return (len(s), 1 if has_mix else 0, 1 if has_digit else 0)
        filtered.sort(key=score, reverse=True)
        best = filtered[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{15,}", best):
            return best
    # Also try 10+ for very short legacy IDs (sanitize allows 10)
    candidates10 = re.findall(r"[A-Za-z0-9_-]{10,}", text)
    filtered10 = [c for c in candidates10 if c.lower() not in ignore]
    if filtered10:
        filtered10.sort(key=len, reverse=True)
        best10 = filtered10[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{10,}", best10):
            return best10
    raise ValueError("Could not extract Drive ID from link. Please copy the full Drive link or the folder ID (e.g. https://drive.google.com/drive/folders/1a2B...).")

def _escape_query_value(value: str) -> str:
    """Escape single quotes for Drive query strings."""
    return value.replace("'", "\\'")



class DriveFilesMixin:
    async def _list_files_inner(self, drive, aiogoogle, folder_id: str) -> list[dict]:
        files = []
        page_token = None
        while True:
            request = drive.files.list(
                q=f"'{folder_id}' in parents and mimeType!='application/vnd.google-apps.folder' and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageToken=page_token,
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            )
            response = await self._execute_with_retry(aiogoogle, request)
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    async def _has_files_inner(self, drive, aiogoogle, folder_id: str) -> bool:
        request = drive.files.list(
            q=f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed=false",
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        )
        response = await self._execute_with_retry(aiogoogle, request)
        return len(response.get("files", [])) > 0

    def validate_images(self, files: list[dict]) -> tuple[int, list[str]]:
        """Validate image files: returns (count, warnings)."""
        images = [f for f in files if PurePosixPath(f["name"]).suffix.lower() in VALID_IMAGE_EXTENSIONS]
        warnings: list[str] = []
        seen: dict[str, int] = {}
        for f in images:
            low = f["name"].lower()
            seen[low] = seen.get(low, 0) + 1
        dups = [n for n, c in seen.items() if c > 1]
        if dups:
            warnings.append(f"Duplicate names: {', '.join(dups[:3])}")
        zero = [f["name"] for f in images if f.get("size") == "0"]
        if zero:
            warnings.append(f"Zero-byte files: {', '.join(zero[:3])}")
        try:
            import re
            nums = []
            for f in images:
                m = re.search(r"(\d+)", f["name"])
                if m:
                    nums.append(int(m.group(1)))
            if nums:
                nums.sort()
                expected = list(range(min(nums), max(nums)+1))
                if nums != expected:
                    missing = sorted(set(expected) - set(nums))
                    if missing:
                        warnings.append(f"Missing sequence numbers: {missing[:5]}")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        return len(images), warnings
    
    async def count_images_in_folder(self, folder_id: str) -> int | None:
        """Count images in a folder (strict extension filter)."""
        try:
            files = await self.list_files_in_folder(folder_id)
        except Exception:
            return None
        images = [
            f for f in files
            if PurePosixPath(f["name"]).suffix.lower() in VALID_IMAGE_EXTENSIONS
        ]
        return len(images)

    async def count_images_in_subfolder(self, chapter_folder_id: str, subfolder_name: str = "Final") -> int | None:
        """Try counting inside a named subfolder; fallback to chapter folder itself."""
        try:
            subfolders = await self.list_subfolders(chapter_folder_id)
            for sf in subfolders:
                if sf["name"].lower() == subfolder_name.lower():
                    return await self.count_images_in_folder(sf["id"])
        except Exception as e:
            logger.warning(f"Subfolder search failed for {subfolder_name}: {e}")
        return await self.count_images_in_folder(chapter_folder_id)
    
    async def compare_chapter_images(self, manga_folder_id: str, chapter_number: float | int | str) -> tuple[int, int, str|None, list[str]]:
        """Find RAW and ED folders for a chapter, count images in both.
        Supports fractional chapters like 9.1, 9.2.
        Returns (raw_count, ed_count, ed_folder_id, warnings) where warnings come from ED validation
        (duplicates, zero-byte, missing sequence)."""
        target_dec = _to_decimal(chapter_number)
        if not self._creds:
            await self._init_creds()
        # Single session reuse: was 5 separate sessions (5 discoveries) -> now 1
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            manga_subfolders = await self._list_subfolders_inner(drive, aiogoogle, manga_folder_id)
            
            raw_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() == 'raw'), None)
            ed_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() in ['ed', 'final']), None)
            
            # Fetch RAW/ED chapter lists in parallel within same session
            raw_items = []
            ed_items = []
            if raw_id:
                raw_items = await self._list_all_items_inner(drive, aiogoogle, raw_id)
            if ed_id:
                # Only fetch ed_items if needed; can be done concurrently but sequential is okay within same session
                if raw_id and ed_id:
                    # Parallelize the second fetch while first already done; for true parallel use gather, but we are sequential to avoid extra complexity
                    pass
                ed_items = await self._list_all_items_inner(drive, aiogoogle, ed_id)
            
            raw_ch_id = None
            for f in raw_items:
                if f.get('mimeType') != 'application/vnd.google-apps.folder':
                    continue
                ch_num = extract_chapter_number(f['name'])
                if ch_num is not None and (target_dec is not None and ch_num == target_dec):
                    raw_ch_id = f['id']
                    break
                        
            ed_ch_id = None
            for f in ed_items:
                if f.get('mimeType') != 'application/vnd.google-apps.folder':
                    continue
                ch_num = extract_chapter_number(f['name'])
                if ch_num is not None and (target_dec is not None and ch_num == target_dec):
                    ed_ch_id = f['id']
                    break
                        
            raw_count = 0
            ed_count = 0
            ed_warnings: list[str] = []
            if raw_ch_id:
                files = await self._list_files_inner(drive, aiogoogle, raw_ch_id)
                raw_count, _ = self.validate_images(files)
            if ed_ch_id:
                files = await self._list_files_inner(drive, aiogoogle, ed_ch_id)
                ed_count, ed_warnings = self.validate_images(files)
                
            return raw_count, ed_count, ed_ch_id, ed_warnings

    async def deep_sync_manga(self, manga_folder_id: str) -> dict:
        """
        Scan RAW, TL, PR, ED folders to deeply infer progress.
        Returns a dictionary mapping chapter_number to its inferred status and folder ID.
        Optimized: single Aiogoogle session + cached discovery + single has_files batched + incremental cache.
        """
        # Incremental cache: avoid full scan if called again within 60s (handles double-click on Sync)
        cache_key = f"deep_sync:{manga_folder_id}"
        if drive_deep_sync_cache is not None:
            cached = drive_deep_sync_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Deep sync cache hit for {manga_folder_id[:12]}")
                return cached
        self.last_sync_failures = []
        if not self._creds:
            await self._init_creds()

        # Single session for entire deep sync - avoids N discoveries
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            manga_subfolders = await self._list_subfolders_inner(drive, aiogoogle, manga_folder_id)
            
            raw_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() == 'raw'), None)
            tl_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() == 'tl'), None)
            pr_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() == 'pr'), None)
            ed_id = next((sf['id'] for sf in manga_subfolders if sf['name'].lower() in ['ed', 'final']), None)

            async def get_chapters_in(folder_id):
                if not folder_id: return {}
                items = await self._list_all_items_inner(drive, aiogoogle, folder_id)
                res = {}
                for f in items:
                    ch_num = extract_chapter_number(f['name'])
                    if ch_num is None:
                        continue
                    # Normalize: Decimal('9.0') == Decimal('9') but keep original for key; using Decimal will dedup 9 and 9.0 correctly
                    is_folder = f.get('mimeType') == 'application/vnd.google-apps.folder'
                    if ch_num not in res:
                        res[ch_num] = {'id': f['id'], 'is_folder': is_folder, 'has_files': not is_folder}
                    else:
                        if not is_folder:
                            res[ch_num]['has_files'] = True
                return res

            folder_names = ("RAW", "TL", "PR", "ED")
            folder_results = await asyncio.gather(
                get_chapters_in(raw_id),
                get_chapters_in(tl_id),
                get_chapters_in(pr_id),
                get_chapters_in(ed_id),
                return_exceptions=True,
            )
            chapter_maps = []
            for folder_name, result in zip(folder_names, folder_results):
                if isinstance(result, Exception):
                    failure = f"{folder_name} folder listing failed: {result}"
                    self.last_sync_failures.append(failure)
                    logger.warning("Drive deep sync partial failure: %s", failure)
                    chapter_maps.append({})
                else:
                    chapter_maps.append(result)
            raw_chs, tl_chs, pr_chs, ed_chs = chapter_maps
            
            all_ch_nums = set(raw_chs.keys()) | set(tl_chs.keys()) | set(pr_chs.keys()) | set(ed_chs.keys())
            progress = {}
            
            # Pre-batch has_files checks: collect all folder IDs that need has_files probe
            # Previously each chapter did sequential has_files; now batch them
            need_probe: dict[int, list[tuple[str, dict]]] = {}  # ch -> list of (type, entry)
            for ch in all_ch_nums:
                candidates = []
                if ch in ed_chs and ed_chs[ch]['is_folder'] and not ed_chs[ch]['has_files']:
                    candidates.append(('ed', ed_chs[ch]))
                if ch in pr_chs and pr_chs[ch]['is_folder'] and not pr_chs[ch]['has_files']:
                    candidates.append(('pr', pr_chs[ch]))
                if ch in tl_chs and tl_chs[ch]['is_folder'] and not tl_chs[ch]['has_files']:
                    candidates.append(('tl', tl_chs[ch]))
                if ch in raw_chs and raw_chs[ch]['is_folder'] and not raw_chs[ch]['has_files']:
                    candidates.append(('raw', raw_chs[ch]))
                if candidates:
                    need_probe[ch] = candidates

            # Batch probe has_files with concurrency limit (uses _DRIVE_SEMAPHORE internally)
            probe_results: dict[str, bool] = {}  # folder_id -> has_files
            if need_probe:
                all_probe_ids = []
                for lst in need_probe.values():
                    for _, entry in lst:
                        all_probe_ids.append(entry['id'])
                # Chunked to avoid overwhelming semaphore
                for i in range(0, len(all_probe_ids), DRIVE_SYNC_CHAPTER_BATCH_SIZE):
                    chunk = all_probe_ids[i:i + DRIVE_SYNC_CHAPTER_BATCH_SIZE]
                    results = await asyncio.gather(
                        *(self._has_files_inner(drive, aiogoogle, fid) for fid in chunk),
                        return_exceptions=True,
                    )
                    for fid, res in zip(chunk, results):
                        if isinstance(res, Exception):
                            logger.warning("has_files probe failed for %s: %s", fid, res)
                            probe_results[fid] = False
                        else:
                            probe_results[fid] = bool(res)

            def has_files_cached(entry: dict) -> bool:
                if entry['has_files']:
                    return True
                if entry['is_folder']:
                    return probe_results.get(entry['id'], False)
                return False

            for ch in sorted(all_ch_nums):
                # In reverse workflow order: ED -> PR -> TL -> RAW
                if ch in ed_chs and has_files_cached(ed_chs[ch]):
                    progress[ch] = {'status': 'published', 'id': ed_chs[ch]['id']}
                    continue
                if ch in pr_chs and has_files_cached(pr_chs[ch]):
                    progress[ch] = {'status': 'pending_ed', 'id': ed_chs[ch]['id'] if ch in ed_chs else pr_chs[ch]['id']}
                    continue
                if ch in tl_chs and has_files_cached(tl_chs[ch]):
                    progress[ch] = {'status': 'pending_pr' if pr_id else 'pending_ed', 'id': tl_chs[ch]['id']}
                    continue
                if ch in raw_chs and has_files_cached(raw_chs[ch]):
                    progress[ch] = {'status': 'pending_tl', 'id': raw_chs[ch]['id']}
                    continue
                
                if ch in raw_chs:
                    progress[ch] = {'status': 'pending_tl', 'id': raw_chs[ch]['id']}
                elif ch in tl_chs:
                    progress[ch] = {'status': 'pending_tl', 'id': tl_chs[ch]['id']}
                elif ch in ed_chs:
                    progress[ch] = {'status': 'pending_tl', 'id': ed_chs[ch]['id']}
            
            if self.last_sync_failures:
                logger.warning(
                    "Drive deep sync completed with %s partial failure(s); "
                    "%s chapter result(s) are available.",
                    len(self.last_sync_failures), len(progress),
                )
            # Cache result for incremental sync
            if drive_deep_sync_cache is not None:
                drive_deep_sync_cache.set(cache_key, progress)
            return progress

    async def sync_manga_chapters(self, drive_folder_id: str, final_subfolder_name: str, start_chapter: float | int | str = 0) -> list[dict]:
        """Sync chapters from the final subfolder (cached 5 min). Supports fractional chapters like 9.1."""
        try:
            start_dec = Decimal(str(start_chapter))
        except (InvalidOperation, ValueError):
            start_dec = Decimal(0)
        cache_key = f"sync:{drive_folder_id}:{final_subfolder_name}:{start_chapter}"
        now = time.time()
        self._prune_cache()
        if cache_key in self._cache:
            ts, val = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                logger.debug(f"Cache hit for {cache_key}")
                return val

        manga_subfolders = await self.list_subfolders(drive_folder_id)
        final_sf_id = None
        for sf in manga_subfolders:
            if sf["name"].lower() == final_subfolder_name.lower():
                final_sf_id = sf["id"]
                break
        if not final_sf_id:
            logger.warning(f"Folder '{final_subfolder_name}' not found in {drive_folder_id}")
            return []
        chapter_folders = await self.list_subfolders(final_sf_id)
        chapters = []
        for folder in chapter_folders:
            ch_num = extract_chapter_number(folder["name"])
            if ch_num is None:
                continue
            if ch_num >= start_dec:
                chapters.append({
                    "name": folder["name"],
                    "id": folder["id"],
                    "chapter_number": ch_num
                })
        chapters.sort(key=lambda c: c["chapter_number"])
        self._cache[cache_key] = (now, chapters)
        return chapters

    async def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str | None = None) -> bool:
        """Move a file/folder to a new parent (used for Published auto-move)."""
        if not self._creds:
            await self._init_creds()
        async with Aiogoogle(service_account_creds=self._creds) as aiogoogle:
            drive = await self._get_drive(aiogoogle)
            kwargs = {"fileId": file_id, "addParents": new_parent_id, "fields": "id, parents", "supportsAllDrives": True}
            if old_parent_id:
                kwargs["removeParents"] = old_parent_id
            request = drive.files.update(**kwargs)
            try:
                await self._execute_with_retry(aiogoogle, request)
                self._cache.clear()
                return True
            except Exception as e:
                logger.warning(f"Move failed for {file_id} -> {new_parent_id}: {e}")
                return False

