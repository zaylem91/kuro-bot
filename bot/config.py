import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root first, then bot/.env as fallback (supports running from any cwd)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BOT_DIR = Path(__file__).resolve().parent

load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)
# Always load bot/.env as well for SENTRY_DSN etc. (project .env may not contain it)
load_dotenv(dotenv_path=_BOT_DIR / ".env", override=False)

def _get_env(key: str, required: bool = True) -> str:
    val = os.getenv(key)
    if required and not val:
        raise ValueError(f"Missing required environment variable: {key}")
    return val or ""

DISCORD_TOKEN = _get_env('DISCORD_TOKEN')
TEAM_LEADER_ROLE_ID = int(_get_env('TEAM_LEADER_ROLE_ID'))
ADMIN_ROLE_ID = int(_get_env('ADMIN_ROLE_ID'))
DATABASE_URL = _get_env('DATABASE_URL')

# Service account JSON: prefer env var, fallback to file next to this module
# Supports 3 cases: env var is a file path, env var is raw JSON content, or not set -> fallback to file
_env_sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if _env_sa_path:
    _stripped = _env_sa_path.strip()
    if _stripped.startswith("{"):
        # Env var contains raw JSON (common on Replit / PaaS where file not available)
        GOOGLE_SERVICE_ACCOUNT_JSON = _stripped
    elif Path(_env_sa_path).exists():
        GOOGLE_SERVICE_ACCOUNT_JSON = _env_sa_path
    else:
        GOOGLE_SERVICE_ACCOUNT_JSON = os.path.join(os.path.dirname(__file__), "Service Account.json")
else:
    GOOGLE_SERVICE_ACCOUNT_JSON = os.path.join(os.path.dirname(__file__), "Service Account.json")

# Guild ID for faster slash-command sync (optional)
GUILD_ID_STR = _get_env('GUILD_ID', required=False)
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR else None

# Root Drive folder ID - moved to env (no hard-coded fallback)
DRIVE_ROOT_FOLDER_ID = os.getenv('DRIVE_ROOT_FOLDER_ID', '')

# Default timezone for monthly stats
MONTHLY_TIMEZONE = os.getenv('MONTHLY_TIMEZONE', 'UTC')

# Discord role IDs for team positions (TL/PR/ED) — optional but recommended for auto-assign
TL_ROLE_ID_STR = os.getenv('TL_ROLE_ID', '')
PR_ROLE_ID_STR = os.getenv('PR_ROLE_ID', '')
ED_ROLE_ID_STR = os.getenv('ED_ROLE_ID', '')
TL_ROLE_ID = int(TL_ROLE_ID_STR) if TL_ROLE_ID_STR and TL_ROLE_ID_STR.isdigit() else None
PR_ROLE_ID = int(PR_ROLE_ID_STR) if PR_ROLE_ID_STR and PR_ROLE_ID_STR.isdigit() else None
ED_ROLE_ID = int(ED_ROLE_ID_STR) if ED_ROLE_ID_STR and ED_ROLE_ID_STR.isdigit() else None

ROLE_ID_MAP = {
    "TL": TL_ROLE_ID,
    "PR": PR_ROLE_ID,
    "ED": ED_ROLE_ID,
}

# Team / scanlation group branding
TEAM_NAME = os.getenv('TEAM_NAME', 'Kuro').strip()

# Manga categories per status (ongoing / paused / dropped)
MANGA_CATEGORY_NAME = os.getenv('MANGA_CATEGORY_NAME', '📚 Mangas')
MANGA_CATEGORY_ID_STR = os.getenv('MANGA_CATEGORY_ID', '')
MANGA_CATEGORY_ID = int(MANGA_CATEGORY_ID_STR) if MANGA_CATEGORY_ID_STR and MANGA_CATEGORY_ID_STR.isdigit() else None

MANGA_CATEGORY_PAUSED_NAME = os.getenv('MANGA_CATEGORY_PAUSED_NAME', '⏸️ Paused Mangas')
MANGA_CATEGORY_PAUSED_ID_STR = os.getenv('MANGA_CATEGORY_PAUSED_ID', '')
MANGA_CATEGORY_PAUSED_ID = int(MANGA_CATEGORY_PAUSED_ID_STR) if MANGA_CATEGORY_PAUSED_ID_STR and MANGA_CATEGORY_PAUSED_ID_STR.isdigit() else None

MANGA_CATEGORY_DROPPED_NAME = os.getenv('MANGA_CATEGORY_DROPPED_NAME', '❌ Dropped Mangas')
MANGA_CATEGORY_DROPPED_ID_STR = os.getenv('MANGA_CATEGORY_DROPPED_ID', '')
MANGA_CATEGORY_DROPPED_ID = int(MANGA_CATEGORY_DROPPED_ID_STR) if MANGA_CATEGORY_DROPPED_ID_STR and MANGA_CATEGORY_DROPPED_ID_STR.isdigit() else None

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip().strip('"').strip("'").strip()
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080") or 8080)
HEALTH_HOST = os.getenv("HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"
HEALTH_TOKEN = os.getenv("HEALTH_TOKEN", "").strip()

# Remote Dashboard (HTML control panel on the same HEALTH_PORT)
# DASHBOARD_TOKEN is REQUIRED to enable /api/* — generate with:
#   python scripts/generate_dashboard_token.py
# Falls back to HEALTH_TOKEN if DASHBOARD_TOKEN is empty (backwards compat).
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
# Optional IP allowlist, comma-separated (e.g. "1.2.3.4,5.6.7.8").
# Empty = allow any IP (token still required).
DASHBOARD_ALLOWED_IPS = os.getenv("DASHBOARD_ALLOWED_IPS", "").strip()
# Optional CORS origins for the API, comma-separated.
# Default "*" lets a standalone dashboard.html opened from file:// work.
# Lock down in production, e.g. "https://dashboard.example.com".
DASHBOARD_CORS_ORIGINS = os.getenv("DASHBOARD_CORS_ORIGINS", "").strip()

def get_category_for_status(status: str):
    """Return (name, id) for a manga status."""
    if status == "paused":
        return MANGA_CATEGORY_PAUSED_NAME, MANGA_CATEGORY_PAUSED_ID
    elif status == "dropped":
        return MANGA_CATEGORY_DROPPED_NAME, MANGA_CATEGORY_DROPPED_ID
    else:  # ongoing, finished, completed, etc. -> main category
        return MANGA_CATEGORY_NAME, MANGA_CATEGORY_ID

# Media GIFs for dashboards - moved to env (no hard-coded URLs in code)
GIPHY_ADMIN_MAIN = os.getenv('GIPHY_ADMIN_MAIN', 'https://media.giphy.com/media/1nVSfqbKaL2OYkZq8V/giphy.gif')
GIPHY_MANGA_HUB = os.getenv('GIPHY_MANGA_HUB', 'https://media.giphy.com/media/DAAwYcL6rhvArpF3FL/giphy.gif')
GIPHY_STAFF_HUB = os.getenv('GIPHY_STAFF_HUB', 'https://media.giphy.com/media/IzR9o1CxrNYTU1g71p/giphy.gif')
GIPHY_FINANCE_HUB = os.getenv('GIPHY_FINANCE_HUB', 'https://media.giphy.com/media/12Eo7WogCAoj84/giphy.gif')
GIPHY_STAFF_DASHBOARD = os.getenv('GIPHY_STAFF_DASHBOARD', 'https://media.giphy.com/media/iXufGF6kBkWWJjnWiV/giphy.gif')

# Application emojis (uploaded in Developer Portal) — manga & status are GIF (animated)
EMOJI = {
    "gdrive": "<:gdrive:1545051179548020837>",
    "mangadex": "<:mangadex:1546524781375922256>",
    "manga": "<a:manga:1545051221205717063>",
    "tl": "<:tl:1545051534016913408>",
    "ed": "<:ed:1545051667202965504>",
    "pr": "<:pr:1545052442515734629>",
    "link": "<:link:1545052667045486622>",
    "status": "<a:status:1545103772395634820>",
    "raw": "<:file:1545109336563388426>",
    "file": "<:file:1545109336563388426>",
    "tl_done": "<:tl:1545051534016913408>",
    "ed_done": "<:ed:1545051667202965504>",
    "pr_done": "<:pr:1545052442515734629>",
    "CheckMark": "<a:CheckMark:1412079470205997056>",
    "book": "<a:book:1545434395609923674>",
    "purplegear": "<a:purplegear:1545433749263622254>",
    "purplebriefcase": "<a:purplebriefcase:1545433745979482185>",
    "bitcash": "<a:bitcash:1545433630589849790>",
    "staff": "<a:staff:1545433601200496690>",
    "moneybag": "<a:moneybag:1545433562331746355>",
    "ownership": "<:ownership:1545433290691973190>",
    "info": "<:important:1545674807411089418>",
    "important": "<:important:1545674807411089418>"
}
# Allow override via .env if IDs change (auto-detect animated)
for _k in list(EMOJI.keys()):
    _env = os.getenv(f"EMOJI_{_k.upper()}_ID")
    if _env and _env.isdigit():
        # manga & status are animated
        prefix = "a" if _k in ("manga", "status") else ""
        EMOJI[_k] = f"<{prefix}:{_k}:{_env}>" if prefix else f"<:{_k}:{_env}>"
    _anim = os.getenv(f"EMOJI_{_k.upper()}_ANIMATED")
    if _anim and _anim.lower() in ("1","true","yes"):
        # Force animated if needed
        if not EMOJI[_k].startswith("<a:"):
            EMOJI[_k] = EMOJI[_k].replace("<:", "<a:")
