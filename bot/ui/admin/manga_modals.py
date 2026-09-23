from .core import *

class AddMangaModal(miru.Modal):
    name = miru.TextInput(label="Manga Name", placeholder="Solo Leveling", required=True)
    drive_id = miru.TextInput(label="Drive Folder Link or ID", placeholder="https://drive.google.com/drive/folders/... or folder ID", required=True)
    start_ch = miru.TextInput(label="Start Chapter", placeholder="0", value="0", required=True)
    cover_url = miru.TextInput(label="Cover URL (Optional)", placeholder="https://...", required=False)
    mangadex_url = miru.TextInput(label="MangaDex Link (Optional)", placeholder="https://mangadex.org/title/...", required=False)

    async def callback(self, ctx: miru.ModalContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.defer()
        try:
            ch_num = int(self.start_ch.value)
        except ValueError:
            await ctx.edit_response(components=[create_error("Start chapter must be a number.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
            
        from bot.config import MANGA_CATEGORY_ID, MANGA_CATEGORY_NAME, TEAM_LEADER_ROLE_ID, TEAM_NAME
        import logging
        logger = logging.getLogger(__name__)
        
        discord_channel_id = None
        webhook_id = None
        webhook_token = None
        
        raw_name = self.name.value.strip()
        name = sanitize_manga_name(raw_name)
        # Accept full Drive link or raw ID — extract ID robustly (handles all link forms)
        from bot.services.drive import extract_drive_id
        try:
            drive_id_raw = extract_drive_id(self.drive_id.value)
        except ValueError as e:
            await ctx.edit_response(components=[create_error(str(e))], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        # H3: also sanitize cover_url? ensure it is URL
        if self.cover_url.value and not self.cover_url.value.strip().startswith(("http://","https://")):
            await ctx.edit_response(components=[create_error("Cover URL must start with http:// or https://")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        mangadex_raw = self.mangadex_url.value.strip() if self.mangadex_url.value else ""
        if mangadex_raw and not mangadex_raw.startswith(("http://","https://")):
            mangadex_raw = "https://" + mangadex_raw
        if mangadex_raw and "mangadex.org" not in mangadex_raw.lower():
            await ctx.edit_response(components=[create_error("MangaDex link must be a valid https://mangadex.org/... URL")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        
        # Create Channel
        try:
            category_id = MANGA_CATEGORY_ID
            if not category_id:
                # Find category by name
                channels = await ctx.client.app.rest.fetch_guild_channels(ctx.guild_id)
                for c in channels:
                    if c.type == hikari.ChannelType.GUILD_CATEGORY and c.name == MANGA_CATEGORY_NAME:
                        category_id = c.id
                        break
                        
            overwrites = []
            if TEAM_LEADER_ROLE_ID:
                overwrites = [
                    hikari.PermissionOverwrite(
                        id=ctx.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=hikari.Permissions.VIEW_CHANNEL,
                    ),
                    hikari.PermissionOverwrite(
                        id=TEAM_LEADER_ROLE_ID,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=hikari.Permissions.VIEW_CHANNEL,
                    )
                ]
                
            text_channel = await ctx.client.app.rest.create_guild_text_channel(
                ctx.guild_id,
                name=name.replace(" ", "-").lower()[:90],
                category=category_id,
                permission_overwrites=overwrites
            )
            discord_channel_id = str(text_channel.id)
            
            # Create Webhook — Discord limit 80 chars (50035 Invalid Form Body if too long)
            _suffix = f" • {TEAM_NAME}" if TEAM_NAME else ""
            _max_name = 80 - len(_suffix)
            _wname = f"{name[:_max_name]}{_suffix}" if len(name) + len(_suffix) > 80 else f"{name}{_suffix}"
            webhook = await ctx.client.app.rest.create_webhook(text_channel.id, name=_wname)
            webhook_id = str(webhook.id)
            webhook_token = webhook.token
            if not webhook_token:
                try:
                    wh = await ctx.client.app.rest.fetch_webhook(webhook.id)
                    webhook_token = wh.token
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        except Exception as e:
            logger.error(f"Failed to create Discord channel/webhook for {name}: {e}")
            
        pool = get_pool()
        cover_raw = self.cover_url.value.strip() if self.cover_url.value else ""
        # mangadex already validated above
        async with pool.acquire() as conn:
            try:
                manga_id = await conn.fetchval(
                    "INSERT INTO mangas (manga_name, drive_folder_id, start_chapter, cover_url, mangadex_url, discord_channel_id, webhook_id, webhook_token) VALUES ($1, $2, $3, NULLIF($4, ''), NULLIF($5, ''), $6, $7, $8) RETURNING id",
                    name, drive_id_raw, ch_num, cover_raw, mangadex_raw, discord_channel_id, webhook_id, webhook_token
                )
                
                # Invalidate caches
                try:
                    manga_options_cache.clear()
                    workload_cache.clear()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                # Update webhook
                try:
                    from bot.services.webhook_sender import update_manga_webhook
                    await update_manga_webhook(ctx.client.app, manga_id)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
                    
                # Sync drive in background — via persistent queue (survives restart)
                try:
                    from bot.services.task_queue import enqueue_job
                    await enqueue_job(pool, "drive_sync", {"manga_id": manga_id, "drive_folder_id": drive_id_raw})
                except Exception as e:
                    # Fallback to fire-and-forget if queue not ready
                    logger.warning(f"Queue enqueue failed, falling back to task: {e}")
                    import asyncio
                    async def _bg_sync():
                        try:
                            from bot.core import get_drive_service
                            ds = get_drive_service()
                            progress = await ds.deep_sync_manga(drive_id_raw)
                            async with get_pool().acquire() as bg_conn:
                                for ch_num, data in progress.items():
                                    await bg_conn.execute(
                                        "INSERT INTO chapters (manga_id, chapter_number, drive_folder_id, status) VALUES ($1, $2, $3, $4) ON CONFLICT (manga_id, chapter_number) DO NOTHING",
                                        manga_id, ch_num, data['id'], data['status']
                                    )
                            from bot.services.webhook_sender import update_manga_webhook
                            await update_manga_webhook(ctx.client.app, manga_id)
                        except Exception as ex:
                            logger.warning(f"Background sync failed: {ex}")
                    asyncio.create_task(_bg_sync())
                
                await ctx.edit_response(components=[create_success(f"Added **{name}** with channel <#{discord_channel_id}>!\n\n*Queued Google Drive scan — will sync shortly...*")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            except Exception as e:
                # Rollback: Clean up orphaned Discord channel if it was created
                if discord_channel_id:
                    try:
                        await ctx.client.app.rest.delete_channel(int(discord_channel_id))
                        logger.info(f"Cleaned up orphaned channel {discord_channel_id} after DB failure for {name}")
                    except Exception as clean_err:
                        logger.warning(f"Failed to delete orphaned channel {discord_channel_id}: {clean_err}")
                await ctx.edit_response(components=[create_error(f"Failed: {e}")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class EditMangaModal(miru.Modal):
    def __init__(self, title: str, manga_id: int, m: dict, *args, **kwargs):
        super().__init__(title, *args, **kwargs)
        self.manga_id = manga_id
        self.ti_name = miru.TextInput(label="Manga Name", value=m["manga_name"], custom_id="name", required=True)
        self.ti_drive = miru.TextInput(label="Drive Folder Link or ID", value=m["drive_folder_id"], custom_id="drive", required=True, placeholder="https://drive.google.com/drive/folders/... or ID")
        self.ti_cover = miru.TextInput(label="Cover URL", value=m.get("cover_url", "") or "", custom_id="cover", required=False)
        self.ti_mangadex = miru.TextInput(label="MangaDex Link (Optional)", value=m.get("mangadex_url", "") or "", custom_id="mangadex", required=False, placeholder="https://mangadex.org/title/...")
        self.add_item(self.ti_name)
        self.add_item(self.ti_drive)
        self.add_item(self.ti_cover)
        self.add_item(self.ti_mangadex)

    async def callback(self, ctx: miru.ModalContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        raw_name = self.ti_name.value.strip()
        name = sanitize_manga_name(raw_name)
        from bot.services.drive import extract_drive_id as _extract
        try:
            drive = _extract(self.ti_drive.value)
        except ValueError as e:
            await ctx.respond(components=[create_error(str(e))], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        cover = self.ti_cover.value.strip()
        if cover and not cover.startswith(("http://","https://")):
            await ctx.respond(components=[create_error("Cover URL must start with http")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        mangadex_raw = self.ti_mangadex.value.strip() if self.ti_mangadex.value else ""
        if mangadex_raw and not mangadex_raw.startswith(("http://","https://")):
            mangadex_raw = "https://" + mangadex_raw
        if mangadex_raw and "mangadex.org" not in mangadex_raw.lower():
            await ctx.respond(components=[create_error("MangaDex link must be a valid https://mangadex.org/... URL")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        
        pool = get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    "UPDATE mangas SET manga_name=$1, drive_folder_id=$2, cover_url=NULLIF($3, ''), mangadex_url=NULLIF($4, '') WHERE id=$5",
                    name, drive, cover, mangadex_raw, self.manga_id
                )
            except asyncpg.exceptions.UniqueViolationError:
                await ctx.respond(components=[create_error(f"A manga named **{name}** already exists.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                return
        try:
            manga_options_cache.clear()
            workload_cache.clear()
            drive_deep_sync_cache.clear()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            
        try:
            from bot.services.webhook_sender import update_manga_webhook
            await update_manga_webhook(ctx.client.app, self.manga_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
            
        await ctx.respond(components=[create_success(f"Updated **{name}**.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)


class SearchMangaModal(miru.Modal, title="Search Manga"):
    query = miru.TextInput(label="Search term", placeholder="e.g. Solo Leveling", required=True)

    async def callback(self, ctx: miru.ModalContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        term = self.query.value.strip()
        if not term:
            await ctx.respond(components=[create_error("Please enter a search term.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        pool = get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT id, manga_name, status FROM mangas WHERE manga_name ILIKE $1 ORDER BY manga_name ASC LIMIT 24",
                f"%{term}%",
            )
        if not records:
            await ctx.respond(components=[create_error(f"No mangas found for `{term}`.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        options = []
        for r in records:
            st = r["status"]
            if st == "ongoing":
                emoji = EMOJI.get("status", "🟢")
            elif st == "paused":
                emoji = "⏸️"
            elif st == "dropped":
                emoji = "❌"
            else:
                emoji = "🏁"
            options.append(miru.SelectOption(label=r["manga_name"][:100], value=str(r["id"]), emoji=emoji))
        from .manga_views import MangaMenuView
        view = MangaMenuView(options)
        c = build_manga_dashboard_container(ctx.client.app)
        # Show filtered results as ephemeral followup
        await ctx.respond(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)


