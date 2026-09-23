import asyncio
import logging
import time
from aiogoogle import Aiogoogle
from aiogoogle.auth.creds import ServiceAccountCreds

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 2
_DRIVE_SEMAPHORE = asyncio.Semaphore(8)
DRIVE_SYNC_CHAPTER_BATCH_SIZE = 10
_CACHED_GOOGLE_API = None
_CACHED_GOOGLE_API_LOCK = asyncio.Lock()

async def check_clock_skew() -> tuple[bool, float]:
    """Compare local time with Google server time. Returns (is_ok, skew_seconds)."""
    import email.utils
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.head("https://www.googleapis.com", timeout=5) as resp:
                date_hdr = resp.headers.get("Date")
                if not date_hdr:
                    return True, 0
                google_time = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                local = time.time()
                skew = abs(local - google_time)
                return skew < 300, skew  # 5 min tolerance
    except Exception:
        return True, 0  # if check fails, assume ok



class DriveAuthMixin:
    def __init__(self, service_account_json_path: str):
        self._cache: dict = {}
        self._creds = None
        self._sa_path = service_account_json_path
        self._cache_ttl = 300
        self.last_sync_failures: list[str] = []

    async def _init_creds(self):
        """Initialize service account credentials (cached, non-blocking file read)."""
        if self._creds is not None:
            return
        import json
        import asyncio as _asyncio
        def _load():
            # Case 1: _sa_path is raw JSON content (if GOOGLE_SERVICE_ACCOUNT_JSON env var contains JSON)
            if isinstance(self._sa_path, str) and self._sa_path.strip().startswith("{"):
                content = self._sa_path.strip()
                # Remove BOM if present
                if content.startswith("\ufeff"):
                    content = content.lstrip("\ufeff")
                try:
                    return json.loads(content)
                except json.JSONDecodeError as e:
                    if "Extra data" in str(e):
                        try:
                            obj, _ = json.JSONDecoder().raw_decode(content)
                            logger.warning("Service Account JSON had extra data — using first JSON object only")
                            return obj
                        except Exception:
                            raise
                    raise
            # Case 2: _sa_path is a file path
            with open(self._sa_path, 'r', encoding='utf-8-sig') as f:
                content = f.read()
            # Strip BOM and whitespace
            if content.startswith("\ufeff"):
                content = content.lstrip("\ufeff")
            content = content.strip()
            if not content:
                raise ValueError(f"Service Account file is empty: {self._sa_path}")
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e):
                    # File contains extra data after first JSON (e.g. duplicated object) — recover first object
                    try:
                        obj, idx = json.JSONDecoder().raw_decode(content)
                        remaining = content[idx:].strip()
                        if remaining:
                            logger.warning(f"Service Account JSON had extra data after first object (char {idx}) — ignoring trailing {len(remaining)} chars. Please fix the file to contain a single JSON object.")
                        return obj
                    except Exception as e2:
                        logger.error(f"Failed to parse Service Account JSON even with raw_decode: {e2}")
                        raise
                # Try to handle single quotes or trailing commas? Re-raise with helpful message
                logger.error(f"Invalid Service Account JSON at {self._sa_path}: {e}. Ensure the file contains a single valid JSON object from Google Cloud Console.")
                raise
        sa_info = await _asyncio.to_thread(_load)
        self._creds = ServiceAccountCreds(
            scopes=["https://www.googleapis.com/auth/drive"],
            **sa_info
        )
    
    async def _get_drive(self, aiogoogle: Aiogoogle):
        """Get cached Google Drive API object - avoids discovery fetch per request."""
        global _CACHED_GOOGLE_API
        if _CACHED_GOOGLE_API is not None:
            return _CACHED_GOOGLE_API
        async with _CACHED_GOOGLE_API_LOCK:
            if _CACHED_GOOGLE_API is not None:
                return _CACHED_GOOGLE_API
            # Only one coroutine fetches discovery doc
            api = await aiogoogle.discover("drive", "v3")
            _CACHED_GOOGLE_API = api
            return api

    def _prune_cache(self):
        """Remove expired entries to prevent unbounded growth."""
        now = time.time()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts >= self._cache_ttl]
        for k in expired:
            self._cache.pop(k, None)
        # Hard cap: keep last 200 entries
        if len(self._cache) > 200:
            # Remove oldest
            sorted_items = sorted(self._cache.items(), key=lambda x: x[1][0])
            for k, _ in sorted_items[: len(self._cache) - 200]:
                self._cache.pop(k, None)

    async def _execute_with_retry(self, aiogoogle_instance, request):
        """Execute Drive API request with exponential backoff (quota-aware + clock-skew hint + rate-limit)."""
        # FIX: semaphore only during actual request, not during sleep
        for attempt in range(MAX_RETRIES):
            try:
                async with _DRIVE_SEMAPHORE:
                    return await aiogoogle_instance.as_service_account(request)
            except Exception as e:
                msg = str(e).lower()
                if "cannotdeletepermission" in msg:
                    raise
                is_invalid_grant = "invalid_grant" in msg
                is_quota = any(k in msg for k in ("quota", "ratelimitexceeded", "rate limit", "429", "403"))
                if is_invalid_grant and attempt == 0:
                    ok, skew = await check_clock_skew()
                    if not ok:
                        logger.error(
                            f"invalid_grant — system clock is off by {int(skew)}s! "
                            f"Fix: Settings → Time & Language → Set time automatically → Sync now, then restart bot. "
                            f"Google requires clock within 5 minutes."
                        )
                    else:
                        logger.error("invalid_grant — check Service Account JSON (private_key/client_email) and that Drive API is enabled & folder is shared with the service account email.")
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Request failed after {MAX_RETRIES} attempts: {e}")
                    raise
                base = BACKOFF_BASE ** (attempt + 1)
                wait_time = base * (3 if is_quota else 1)
                if is_quota:
                    logger.warning(f"Quota/rate limit hit, retrying in {wait_time}s: {e}")
                else:
                    logger.warning(f"Attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
                await asyncio.sleep(wait_time)
    

    def clear_cache(self):
        self._cache.clear()
