"""Admin commands â€” manga projects, staff and profile corrections."""
import re
import hikari
from bot.services.ui import create_error, create_success
import lightbulb
import miru
from bot.database.connection import get_pool
from bot.services.permissions import is_admin
import logging

logger = logging.getLogger(__name__)

loader = lightbulb.Loader()

def extract_user_ids(text: str) -> list[str]:
    """Extract Discord IDs from mentions (<@id>) or long numeric strings (17-20 digits)."""
    if not text:
        return []
    ids = []
    mention_ids = re.findall(r"<@!?(\d{17,20})>", text)
    if mention_ids:
        ids = mention_ids
    else:
        long_ids = re.findall(r"\b\d{17,20}\b", text)
        if long_ids:
            ids = long_ids
        else:
            ids = re.findall(r"\d+", text)
    # Remove duplicates while preserving order
    return list(dict.fromkeys(ids))

async def manga_autocomplete(ctx: lightbulb.AutocompleteContext) -> None:
    pool = get_pool()
    query = ctx.focused.value or ""
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT manga_name FROM mangas WHERE manga_name ILIKE $1 LIMIT 25",
            f"%{query}%"
        )
    names = [r["manga_name"] for r in records]
    await ctx.respond(names)


# Edit Profile Modal

class EditProfileModal(miru.Modal, title="Edit Member Info"):
    utc_time = miru.TextInput(label="UTC offset (e.g. +2 or -3)", placeholder="e.g. +2", required=True)
    payment_method = miru.TextInput(label="Payment method (binance or paypal)", placeholder="binance/paypal", required=True)
    payment_info = miru.TextInput(label="Payment info (ID or email)", placeholder="...", required=True)
    drive_email = miru.TextInput(label="Gmail for Drive (required)", placeholder="your@gmail.com", required=True)
    can_manage_payments = miru.TextInput(label="Finance manager (true/false)", placeholder="true or false", required=True, value="false")

    def __init__(self, user_id: str, utc_time: str, payment_method: str, payment_info: str, can_manage: str, drive_email: str = ""):
        super().__init__()
        self.user_id = user_id
        self.utc_time.value = utc_time
        self.payment_method.value = payment_method
        self.payment_info.value = payment_info
        self.drive_email.value = drive_email or ""
        self.can_manage_payments.value = can_manage

    async def callback(self, ctx: miru.ModalContext) -> None:
        if not is_admin(ctx.member):
            await ctx.respond("❌ Admin only.", flags=hikari.MessageFlag.EPHEMERAL)
            return
        # Only team leader / admin can grant finance manager flag (no transitive
        # self-propagation: finance managers cannot mint new finance managers).
        can_manage_str = self.can_manage_payments.value.strip().lower()
        wants_finance = can_manage_str == "true"
        if wants_finance:
            if not is_admin(ctx.member):
                await ctx.respond(components=[create_error("Only team leader or admin can grant finance manager permission.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
        import re
        tz = self.utc_time.value.strip()
        if tz.isdigit():
            tz = f"+{tz}"
        if tz.startswith(('+', '-')):
            tz = f"UTC{tz}"
        if not re.fullmatch(r"UTC[+-](0[0-9]|1[0-4]|[0-9])(:[0-5][0-9])?", tz):
            await ctx.respond(components=[create_error("Invalid Timezone format. Must be like UTC+3 or UTC-05:00.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        method = self.payment_method.value.strip().lower()
        info = self.payment_info.value.strip()
        if method not in ("binance", "paypal"):
            await ctx.respond(components=[create_error("Payment method must be `binance` or `paypal`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        if method == "paypal" and "@" not in info:
              await ctx.respond(components=[create_error("Invalid PayPal email.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
              return
        can_manage = wants_finance
        gmail_raw = self.drive_email.value.strip()
        gmail = gmail_raw.lower()
        if not gmail or "@" not in gmail or "." not in gmail.split("@")[-1]:
            await ctx.respond(components=[create_error("Gmail is required. Must be a valid Google account email.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            existing_user = await conn.fetchval(
                "SELECT discord_id FROM users WHERE LOWER(payment_info) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE",
                info, self.user_id
            )
            if existing_user:
                await ctx.respond(components=[create_error("Payment info already used by another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            existing_gmail = await conn.fetchval(
                "SELECT discord_id FROM users WHERE LOWER(drive_email) = LOWER($1) AND discord_id != $2 AND is_registered = TRUE AND drive_email IS NOT NULL",
                gmail, self.user_id,
            )
            if existing_gmail:
                await ctx.respond(components=[create_error("Gmail already used by another member!")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
            old = await conn.fetchrow("SELECT timezone, payment_method, payment_info, drive_email, can_manage_payments FROM users WHERE discord_id=$1", self.user_id)
            async with conn.transaction():
                try:
                    await conn.execute(
                        """UPDATE users 
                           SET timezone = $1, payment_method = $2, payment_info = $3, drive_email = $4, can_manage_payments = $5
                           WHERE discord_id = $6""",
                        tz, method, info, gmail, can_manage, self.user_id
                    )
                except Exception as e:
                    import asyncpg
                    _msg = str(e).lower()
                    _is_unique = isinstance(e, asyncpg.exceptions.UniqueViolationError)
                    if "drive_email" in _msg or "uq_users_drive_email" in _msg:
                        await ctx.respond(components=[create_error("Gmail was just taken by another member.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    if _is_unique or "uq_users_payment_info" in _msg or "payment_info" in _msg:
                        await ctx.respond(components=[create_error("Payment info was just taken by another member.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                        return
                    raise
                changed_by = str(ctx.user.id)
                if old:
                    if str(old["timezone"]) != str(tz):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'timezone',$2,$3,$4)", self.user_id, str(old["timezone"]), str(tz), changed_by)
                    if str(old["payment_method"]) != str(method):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_method',$2,$3,$4)", self.user_id, str(old["payment_method"]), str(method), changed_by)
                    if str(old["payment_info"]) != str(info):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'payment_info',$2,$3,$4)", self.user_id, str(old["payment_info"]), str(info), changed_by)
                    if (old.get("drive_email") or "") != (gmail if gmail is not None else (old.get("drive_email") or "")):
                        # Only log if gmail was actually provided
                        if gmail is not None:
                            await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'drive_email',$2,$3,$4)", self.user_id, old.get("drive_email") or "", str(gmail), changed_by)
                    if str(old["can_manage_payments"]) != str(can_manage):
                        await conn.execute("INSERT INTO user_audit (discord_id, field, old_value, new_value, changed_by) VALUES ($1,'can_manage_payments',$2,$3,$4)", self.user_id, str(old["can_manage_payments"]), str(can_manage), changed_by)
            # Collect Drive revoke/grant info while still holding connection, but execute outside to avoid blocking pool
            gmail_changed = gmail is not None and gmail.lower() != (str(old.get("drive_email") or "").lower() if old else "")
            drive_mids: list[int] = []
            drive_fid_map: dict[int, str] = {}
            drive_old_gmail: str = str(old.get("drive_email") or "") if old else ""
            if gmail_changed:
                try:
                    mrows = await conn.fetch("SELECT manga_id FROM manga_staff WHERE discord_id=$1", self.user_id)
                    if mrows:
                        drive_mids = [r["manga_id"] for r in mrows]
                        frows = await conn.fetch("SELECT id, drive_folder_id FROM mangas WHERE id = ANY($1::int[])", drive_mids)
                        drive_fid_map = {r["id"]: r["drive_folder_id"] for r in frows}
                except Exception as e:
                    logger.warning(f"Failed to fetch manga staff or folders: {e}")
                    drive_mids = []
                    drive_fid_map = {}
        # Execute Drive permission updates outside DB connection (pool not blocked)
        if gmail_changed and drive_mids:
            try:
                from bot.core import get_drive_service
                ds = get_drive_service()
                for mid in drive_mids:
                    fid = drive_fid_map.get(mid)
                    if not fid:
                        continue
                    try:
                        if drive_old_gmail:
                            await ds.revoke_folder_access(fid, drive_old_gmail)
                    except Exception as e:
                        logger.warning(f"Failed to revoke old gmail {drive_old_gmail} from {fid}: {e}")
                    try:
                        await ds.grant_folder_access(fid, gmail, role="writer")
                    except Exception as ge:
                        import logging
                        logging.getLogger(__name__).warning(f"Drive Gmail update failed for {gmail} on {fid}: {ge}")
            except Exception as e:
                logger.warning(f"Drive permission update process failed: {e}")
        gmail_changed_for_msg = gmail is not None and old and gmail.lower() != str(old.get("drive_email") or "").lower()
        await ctx.respond(components=[create_success(f"Updated info for <@{self.user_id}>." + (f"\n✅ Drive access updated for `{gmail}`." if gmail_changed_for_msg else ""))], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

# NOTE: /edit_profile, /edit_role and /sync_manga have been moved to dashboards
# - Edit profile/roles: Admin Dashboard -> Manage Staff -> select user -> Edit (uses EditProfileModal)
# - Sync: Admin Dashboard -> Manage Projects -> select manga -> Sync Drive button, or /qc sync
# See bot/services/admin_ui.py:SingleMangaView and bot/extensions/workflow.py:QC

@loader.command
class AdminPanel(
    lightbulb.SlashCommand,
    name="admin",
    description="Open the Admin Dashboard (Control Panel)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        from bot.services.permissions import is_admin as _is_admin
        if not _is_admin(ctx.member):
            from bot.services.ui import create_error as _err
            await ctx.respond(components=[_err("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from bot.ui.admin import AdminDashboard
        from bot.core import get_miru_client
        view = AdminDashboard()
        from bot.ui.admin import build_main_dashboard_container
        c = build_main_dashboard_container(ctx.client.app)
        from bot.ui.admin import embed_view_in_container
        await ctx.respond(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)

@loader.command
class Workload(
    lightbulb.SlashCommand,
    name="workload",
    description="Show workload per member — mangas & claimed chapters (admin only)",
    default_member_permissions=hikari.Permissions.ADMINISTRATOR,
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        if not is_admin(ctx.member):
            from bot.services.ui import create_error
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from bot.ui.admin import build_workload_view, embed_view_in_container
        from bot.core import get_miru_client
        view, container = await build_workload_view(page=0)
        await ctx.respond(components=embed_view_in_container(view, container), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)


@loader.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    from bot.ui.admin import AdminDashboard
    from bot.core import get_miru_client
    get_miru_client().start_view(AdminDashboard(), bind_to=None)
