import asyncio
import asyncpg
import hikari
import miru
import logging
from datetime import date
from bot.database.connection import get_pool
from bot.services.ui import create_error, create_success, create_info
from bot.services.permissions import is_admin, is_team_leader
from bot.services.ui import sanitize_manga_name, sanitize_mention
from bot.core import get_miru_client
from bot.config import EMOJI, TEAM_NAME, GIPHY_ADMIN_MAIN, GIPHY_MANGA_HUB, GIPHY_STAFF_HUB, GIPHY_FINANCE_HUB
from bot.tasks.monthly_invoice import get_failed_invoice_dispatches, retry_invoice_dispatch
from bot.services.cache import workload_cache, manga_options_cache, drive_deep_sync_cache
logger = logging.getLogger(__name__)
def build_manga_dashboard_container(app):
    c = create_info(
        f"### {EMOJI['book']} Manga Projects Hub\n\n"
        "Select a manga project from the menu below to edit details, change covers, or modify its status.\n"
        "You can also add a completely new project using the button below.",
        title="Manga Management"
    )
    from hikari.impl.special_endpoints import MediaGalleryItemBuilder
    c.add_media_gallery([MediaGalleryItemBuilder(media=GIPHY_MANGA_HUB)])
    return c
def build_main_dashboard_container(app):
    from hikari.impl.special_endpoints import (
        ContainerComponentBuilder, 
        SectionComponentBuilder, ThumbnailComponentBuilder, 
        MediaGalleryItemBuilder,
    )
    
    c = ContainerComponentBuilder(id=1)
    
    # 1. Header (Section)
    try:
        avatar = str(app.get_me().display_avatar_url)
    except Exception:
        avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
    sec = SectionComponentBuilder(id=2, accessory=ThumbnailComponentBuilder(media=avatar))
    sec.add_text_display(f"# {EMOJI['important']} Admin Control Panel {EMOJI['purplegear']}")
    c.add_component(sec)
    
    _team = TEAM_NAME or "Scanlation"
    msg = (
        f"## {EMOJI['ownership']} Welcome to the {_team} Admin Dashboard!\n\n"
        "Here you can manage everything smoothly without typing complex commands.\n"
        "Use the buttons below to navigate between different management sections:\n\n"
        f"{EMOJI['book']} **Projects:** Add, edit, or delete manga projects.\n"
        f"{EMOJI['staff']} **Staff:** Manage team roles and assign work.\n"
        f"📊 **Workload:** See who has many/few mangas and claimed chapters.\n"
        f"{EMOJI['moneybag']} **Finance:** Settle unpaid balances and adjust global rates."
    )
    c.add_text_display(msg)
    
    # 3. Big GIF (Media Gallery)
    c.add_media_gallery([MediaGalleryItemBuilder(media=GIPHY_ADMIN_MAIN)])
    
    # 4. Divider Line
    c.add_separator(id=3, spacing=hikari.SpacingType.SMALL, divider=True)
    
    return c
def build_staff_dashboard_container(app):
    c = create_info(
        f"### {EMOJI['staff']} Staff Management Hub\n\n"
        "Use the menu below to select a registered staff member.\n"
        "Once selected, you can view their full profile (timezone, payment, Gmail, roles, mangas, balance), "
        "edit their profile info, manage their global roles, or view their audit history.",
        title="Staff Management"
    )
    from hikari.impl.special_endpoints import MediaGalleryItemBuilder
    c.add_media_gallery([MediaGalleryItemBuilder(media=GIPHY_STAFF_HUB)])
    return c
def build_finance_dashboard_container(app, failed_dispatches=()):
    failure_lines = []
    for dispatch in failed_dispatches:
        error = sanitize_mention(str(dispatch["last_error"] or "No error recorded"))
        error = error.replace("`", "'").replace("\n", " ")[:300]
        failure_lines.append(
            f"- **{dispatch['billing_period']:%Y-%m}** — <@{dispatch['discord_id']}> "
            f"(`{dispatch['attempt_count']} attempt(s)`)\n"
            f"  **Error:** {error}"
        )
    if failure_lines:
        failure_summary = (
            "\n\n### ⚠️ Failed Invoice Deliveries\n"
            "These records are persisted in the dispatch ledger. Select one below "
            "to inspect it and explicitly retry it.\n"
            + "\n".join(failure_lines)
        )
    else:
        failure_summary = "\n\n### ✅ Failed Invoice Deliveries\nNo failed invoice deliveries."

    c = create_info(
        f"### {EMOJI['moneybag']} Finance & Payroll Hub\n\n"
        "Select a staff member from the menu below to view and settle their unpaid balances.\n"
        "You can also inspect failed monthly invoices or adjust the global rate for chapters."
        + failure_summary,
        title="Finance Management"
    )
    from hikari.impl.special_endpoints import MediaGalleryItemBuilder
    c.add_media_gallery([MediaGalleryItemBuilder(media=GIPHY_FINANCE_HUB)])
    return c
def embed_view_in_container(view, container):
    for row in view.build():
        container.add_action_row(row.components)
    return [container]
async def fetch_workload_rows(use_cache: bool = True):
    """Fetch per-member workload: manga counts + claimed chapters. Sorted by load descending."""
    cache_key = "workload:all"
    if use_cache:
        cached = workload_cache.get(cache_key)
        if cached is not None:
            return cached
    pool = get_pool()
    async with pool.acquire() as conn:
        # Active mangas only (ongoing) — paused/dropped are cleared automatically so they won't count
        rows = await conn.fetch(
            """
            SELECT
                u.discord_id,
                COALESCE(u.default_role, '?') as default_role,
                COUNT(DISTINCT m.id) as active_mangas,
                COUNT(DISTINCT CASE WHEN ms.role IN ('TL','CO-TL') THEN m.id END) as tl_mangas,
                COUNT(DISTINCT CASE WHEN ms.role IN ('PR','CO-PR') THEN m.id END) as pr_mangas,
                COUNT(DISTINCT CASE WHEN ms.role IN ('ED','CO-ED') THEN m.id END) as ed_mangas
            FROM users u
            LEFT JOIN manga_staff ms ON ms.discord_id = u.discord_id
            LEFT JOIN mangas m ON m.id = ms.manga_id AND m.status = 'ongoing'
            WHERE u.is_registered = TRUE
            GROUP BY u.discord_id
            ORDER BY active_mangas DESC, u.registered_at ASC
            LIMIT 200
            """
        )
        # Claimed chapters per user (separate to avoid join multiplication)
        if rows:
            ids = [r["discord_id"] for r in rows]
            claimed = await conn.fetch(
                """
                SELECT assigned_to as discord_id, COUNT(*) as cnt
                FROM chapters
                WHERE assigned_to = ANY($1::text[]) AND status NOT IN ('published','rejected')
                GROUP BY assigned_to
                """,
                ids,
            )
            claim_map = {r["discord_id"]: r["cnt"] for r in claimed}
        else:
            claim_map = {}
    # Merge claim counts
    result = []
    for r in rows:
        d = dict(r)
        d["claimed"] = claim_map.get(r["discord_id"], 0)
        result.append(d)
    # Re-sort by active + claimed to surface overloaded members
    result.sort(key=lambda x: (x["active_mangas"], x["claimed"]), reverse=True)
    workload_cache.set(cache_key, result)
    return result
def build_workload_container(rows, page=0, per_page=12):
    """Build Container for workload overview with pagination."""
    total = len(rows)
    max_page = max(1, (total + per_page - 1) // per_page)
    start = page * per_page
    chunk = rows[start:start+per_page]
    if not rows:
        desc = "*No registered members yet.*"
    else:
        lines = []
        for idx, r in enumerate(chunk, start=start+1):
            # Rank indicator
            if r["active_mangas"] >= 5:
                load = "🔴 High"
            elif r["active_mangas"] >= 3:
                load = "🟡 Medium"
            elif r["active_mangas"] >= 1:
                load = "🟢 Low"
            else:
                load = "⚪ Idle"
            lines.append(
                f"`{idx:>2}.` <@{r['discord_id']}> — **{r['active_mangas']}** mangas "
                f"(TL:{r['tl_mangas']} PR:{r['pr_mangas']} ED:{r['ed_mangas']}) "
                f"| Claimed: **{r['claimed']}** | {load} | `{r['default_role']}`"
            )
        desc = "\n".join(lines)
        if total > per_page:
            desc += f"\n\n*Page {page+1}/{max_page} — {total} members total. Use ◀/▶ to navigate, or select a member below for details.*"
        else:
            desc += f"\n\n*Total: {total} members. Select a member below for detailed manga list.*"
    c = create_info(desc, title="📊 Workload Overview — Mangas per member")
    # Add legend
    c.add_text_display(
        "**Legend:** 🔴 High (5+ mangas) • 🟡 Medium (3-4) • 🟢 Low (1-2) • ⚪ Idle (0) • `Claimed` = chapters currently assigned"
    )
    return c, max_page
async def build_workload_view(page=0):
    rows = await fetch_workload_rows()
    container, max_page = build_workload_container(rows, page=page)
    view = WorkloadView(rows, page=page, max_page=max_page)
    return view, container
async def fetch_manga_options(limit=24, use_cache: bool = True):
    cache_key = f"manga_options:{limit}"
    if use_cache:
        cached = manga_options_cache.get(cache_key)
        if cached is not None:
            return cached
    pool = get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT id, manga_name, status FROM mangas ORDER BY manga_name ASC LIMIT $1", limit)
    options = []
    for r in records:
        st = r["status"]
        if st == "ongoing":
            emoji = EMOJI.get("status", "🟢")
        elif st == "paused":
            emoji = "⏸️"
        elif st == "dropped":
            emoji = "❌"
        elif st in ("finished", "completed"):
            emoji = "🏁"
        else:
            emoji = "🏁"
        options.append(miru.SelectOption(
            label=r["manga_name"][:100],
            value=str(r["id"]),
            emoji=emoji
        ))
    manga_options_cache.set(cache_key, options)
    return options
from bot.services.webhook_sender import update_manga_webhook
async def fetch_staff_profile(user_id: str):
    """Fetch full staff profile: user row, global roles, manga assignments, balances."""
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT discord_id, timezone, default_role, payment_method, payment_info, "
            "drive_email, is_registered, can_manage_payments, registered_at, finance_channel_id "
            "FROM users WHERE discord_id = $1",
            user_id,
        )
        roles_rows = await conn.fetch(
            "SELECT role FROM user_roles WHERE discord_id = $1 ORDER BY role", user_id
        )
        roles = [r["role"] for r in roles_rows]
        assignments = await conn.fetch(
            "SELECT ms.role, m.manga_name, m.status FROM manga_staff ms "
            "JOIN mangas m ON m.id = ms.manga_id WHERE ms.discord_id = $1 "
            "ORDER BY m.manga_name ASC",
            user_id,
        )
        unpaid_cents = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
            "WHERE user_id = $1 AND status = 'unpaid'",
            user_id,
        )
        claimed_count = await conn.fetchval(
            "SELECT COUNT(*) FROM chapters WHERE assigned_to = $1 "
            "AND status NOT IN ('published','rejected')",
            user_id,
        )
    return user, roles, assignments, unpaid_cents or 0, claimed_count or 0
def build_staff_profile_container(user_id: str, user, roles, assignments, unpaid_cents: int = 0, claimed_count: int = 0):
    """Build a rich profile container for a staff member (admin view)."""
    from bot.services.pricing import format_cents_as_dollars
    from bot.services.ui import sanitize_mention as _san

    if not user:
        return create_error(f"<@{user_id}> has no database record.")
    if not user["is_registered"]:
        return create_error(f"<@{user_id}> is not registered in the system.")

    tz = user["timezone"] or "—"
    default_role = user["default_role"] or "—"
    pay_method = (user["payment_method"] or "—").capitalize() if user["payment_method"] else "—"
    pay_info = _san(str(user["payment_info"] or "—"))
    gmail = _san(str(user["drive_email"] or "—"))
    finance_flag = "✅ Yes" if user["can_manage_payments"] else "❌ No"
    reg_at = user["registered_at"].strftime("%Y-%m-%d") if user["registered_at"] else "—"
    roles_txt = ", ".join(f"`{r}`" for r in roles) if roles else "*No global roles*"
    balance_txt = format_cents_as_dollars(unpaid_cents or 0)

    if assignments:
        grouped: dict[str, list[str]] = {}
        for a in assignments:
            grouped.setdefault(a["role"], []).append(f"{a['manga_name']} (`{a['status']}`)")
        manga_lines = [f"**Mangas ({len(assignments)}):**"]
        for role in ["TL", "CO-TL", "PR", "CO-PR", "ED", "CO-ED"]:
            if role in grouped:
                manga_lines.append(f"• **{role}:** {', '.join(grouped[role])}")
        for role, lst in grouped.items():
            if role not in ["TL", "CO-TL", "PR", "CO-PR", "ED", "CO-ED"]:
                manga_lines.append(f"• **{role}:** {', '.join(lst)}")
        mangas_txt = "\n".join(manga_lines)
    else:
        mangas_txt = "*No mangas assigned — Idle.*"

    desc = (
        f"**Member:** <@{user_id}> (`{user_id}`)\n"
        f"**Registered:** ✅ Yes since `{reg_at}`\n"
        f"**Default Role:** `{default_role}`\n"
        f"**Global Roles:** {roles_txt}\n\n"
        f"🕒 **Timezone:** `{tz}`\n"
        f"💳 **Payment:** `{pay_method}` — `{pay_info}`\n"
        f"📧 **Gmail (Drive):** `{gmail}`\n"
        f"💰 **Finance Manager:** {finance_flag}\n"
        f"💵 **Unpaid Balance:** `{balance_txt}`\n"
        f"📌 **Claimed Chapters:** `{claimed_count}`\n\n"
        f"{mangas_txt}\n\n"
        f"*Use the buttons below to **Edit Profile**, **Edit Roles**, or view **Audit History**.*"
    )
    if len(desc) > 3900:
        desc = desc[:3900] + "\n*... truncated*"
    return create_info(desc, title=f"👤 Staff Profile — <@{user_id}>")
def build_staff_roles_container(user_id: str, user, roles):
    """Container showing current global roles + default role for the roles editor."""
    default_role = (user["default_role"] if user and user["default_role"] else "—")
    roles_txt = ", ".join(f"`{r}`" for r in roles) if roles else "*No global roles*"
    desc = (
        f"**Member:** <@{user_id}>\n"
        f"**Current Global Roles:** {roles_txt}\n"
        f"**Default Role:** `{default_role}`\n\n"
        f"• Use **Add Role** to grant a new global role (also assigns Discord role for TL/PR/ED base).\n"
        f"• Use **Remove Role** to revoke one (blocked while still assigned to an ongoing manga with that role).\n"
        f"• Use **Set Default** to change the primary role (`TL` / `PR` / `ED`)."
    )
    return create_info(desc, title=f"🎭 Edit Roles — <@{user_id}>")
