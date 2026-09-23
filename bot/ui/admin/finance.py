from .core import *

class FinanceUserSelect(miru.UserSelect):
    def __init__(self, *args, **kwargs):
        super().__init__(placeholder="Select a User to Pay...", row=0, *args, **kwargs)

    async def callback(self, ctx: miru.ViewContext) -> None:
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        user_id = str(self.values[0].id)
        pool = get_pool()
        async with pool.acquire() as conn:
            total_cents = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE user_id = $1 AND status = 'unpaid'",
                user_id
            )
        
        from bot.services.pricing import format_cents_as_dollars
        view = PayoutConfirmView(user_id, total_cents)
        c = create_info(f"User <@{user_id}> has an unpaid balance of `{format_cents_as_dollars(total_cents or 0)}`.", title="Payout")
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.view.stop()


class InvoiceFailureSelect(miru.TextSelect):
    def __init__(self, failed_dispatches, *args, **kwargs):
        options = []
        for dispatch in failed_dispatches:
            period = dispatch["billing_period"]
            error = sanitize_mention(str(dispatch["last_error"] or "No error recorded"))
            description = (
                f"{dispatch['attempt_count']} attempt(s): "
                f"{error.replace(chr(96), chr(39)).replace(chr(10), ' ')[:70]}"
            )
            options.append(
                miru.SelectOption(
                    label=f"{period:%Y-%m} — {dispatch['discord_id']}"[:100],
                    value=f"{dispatch['discord_id']}:{period.isoformat()}",
                    description=description[:100],
                )
            )
        super().__init__(
            options=options,
            placeholder="Select a failed invoice to inspect or retry...",
            row=1,
            *args,
            **kwargs,
        )

    async def callback(self, ctx: miru.ViewContext) -> None:
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(
                components=[create_error("Admin only.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            return

        discord_id, period_text = self.values[0].split(":", 1)
        billing_period = date.fromisoformat(period_text)
        pool = get_pool()
        async with pool.acquire() as conn:
            dispatch = await conn.fetchrow(
                """
                SELECT discord_id, billing_period, status, attempt_count, last_error
                FROM invoice_dispatches
                WHERE discord_id = $1 AND billing_period = $2
                """,
                discord_id,
                billing_period,
            )
        if not dispatch or dispatch["status"] != "failed":
            await ctx.respond(
                components=[create_error("That invoice is no longer marked as failed.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            return

        error = sanitize_mention(str(dispatch["last_error"] or "No error recorded"))
        view = InvoiceRetryView(discord_id, billing_period)
        c = create_info(
            f"**Member:** <@{discord_id}>\n"
            f"**Billing period:** `{billing_period:%Y-%m}`\n"
            f"**Attempts:** `{dispatch['attempt_count']}`\n"
            f"**Persisted error:**\n```{error[:1800]}```\n\n"
            "Retry is explicit and will be recorded with your Discord ID.",
            title="Failed Invoice Delivery",
        )
        await ctx.edit_response(
            components=embed_view_in_container(view, c),
            flags=hikari.MessageFlag.IS_COMPONENTS_V2,
        )
        get_miru_client().start_view(view)
        self.view.stop()


class InvoiceRetryView(miru.View):
    def __init__(self, discord_id: str, billing_period: date, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.discord_id = discord_id
        self.billing_period = billing_period

    @miru.button(label="Retry Invoice Delivery", emoji="🔁", style=hikari.ButtonStyle.SUCCESS)
    async def btn_retry(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(
                components=[create_error("Admin only.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            return

        await ctx.defer()
        result = await retry_invoice_dispatch(
            ctx.client.app,
            self.discord_id,
            self.billing_period,
            str(ctx.user.id),
        )
        if result == "sent":
            failed_dispatches = await get_failed_invoice_dispatches(get_pool())
            view = FinanceMenuView(failed_dispatches)
            c = build_finance_dashboard_container(ctx.client.app, failed_dispatches)
            c.add_text_display(
                f"✅ Retried the `{self.billing_period:%Y-%m}` invoice for "
                f"<@{self.discord_id}>. The retry was recorded."
            )
            await ctx.edit_response(
                components=embed_view_in_container(view, c),
                flags=hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            get_miru_client().start_view(view)
            self.stop()
            return

        messages = {
            "failed": "The retry was attempted but delivery failed again; the new error is now persisted.",
            "missing_user": "That member no longer exists in the database.",
            "missing_channel": "That member no longer has a finance channel configured.",
            "not_retryable": "That dispatch is already in progress or is not eligible for retry.",
        }
        await ctx.edit_response(
            components=[create_error(messages.get(result, "The invoice retry could not be completed."))],
            flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
        )

    @miru.button(label="Back to Finance", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY)
    async def btn_back(self, ctx: miru.ViewContext, button: miru.Button):
        if not is_admin(getattr(ctx, "member", None)):
            await ctx.respond(
                components=[create_error("Admin only.")],
                flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2,
            )
            return
        failed_dispatches = await get_failed_invoice_dispatches(get_pool())
        view = FinanceMenuView(failed_dispatches)
        c = build_finance_dashboard_container(ctx.client.app, failed_dispatches)
        await ctx.edit_response(
            components=embed_view_in_container(view, c),
            flags=hikari.MessageFlag.IS_COMPONENTS_V2,
        )
        get_miru_client().start_view(view)
        self.stop()


class FinanceMenuView(miru.View):
    def __init__(self, failed_dispatches=(), *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.add_item(FinanceUserSelect())
        if failed_dispatches:
            self.add_item(InvoiceFailureSelect(failed_dispatches))
        
        btn = miru.Button(label="Back to Dashboard", emoji="⬅️", style=hikari.ButtonStyle.SECONDARY, row=4)
        btn.callback = self.btn_back
        self.add_item(btn)
        
        btn_prices = miru.Button(label="Set Prices", emoji=EMOJI["purplegear"], style=hikari.ButtonStyle.PRIMARY, row=2)
        btn_prices.callback = self.btn_prices
        self.add_item(btn_prices)

    async def btn_prices(self, ctx: miru.ViewContext):
        _member = getattr(ctx, "member", None)
        _is_leader = is_team_leader(_member)
        _has_db = False
        try:
            _pool = get_pool()
            async with _pool.acquire() as _conn:
                _row = await _conn.fetchval("SELECT can_manage_payments FROM users WHERE discord_id=$1", str(ctx.user.id))
                _has_db = bool(_row)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        if not (_is_leader or _has_db):
            await ctx.respond(components=[create_error("Finance manager only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        await ctx.respond(components=[create_info("Prices modal coming soon.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)

    async def btn_back(self, ctx: miru.ViewContext):
        _member = getattr(ctx, "member", None)
        if not is_admin(_member):
            await ctx.respond(components=[create_error("Admin only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        view = AdminDashboard()
        c = build_main_dashboard_container(ctx.client.app)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


class PayoutConfirmView(miru.View):
    def __init__(self, user_id: str, amount: int, *args, **kwargs):
        super().__init__(timeout=600, *args, **kwargs)
        self.user_id = user_id
        self.amount = amount

    @miru.button(label="Mark as Paid", emoji="✅", style=hikari.ButtonStyle.SUCCESS)
    async def btn_pay(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        _is_leader = is_team_leader(_member)
        _has_db = False
        try:
            _pool = get_pool()
            async with _pool.acquire() as _conn:
                _row = await _conn.fetchval("SELECT can_manage_payments FROM users WHERE discord_id=$1", str(ctx.user.id))
                _has_db = bool(_row)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        if not (_is_leader or _has_db):
            await ctx.respond(components=[create_error("Finance manager only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        from bot.services.pricing import format_cents_as_dollars
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                user_data = await conn.fetchrow("SELECT payment_method, payment_info FROM users WHERE discord_id=$1", self.user_id)
                if not user_data:
                    await ctx.respond(components=[create_error("User not found.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                # Atomic payout: UPDATE ... RETURNING so concurrent /done earnings
                # are either fully included or left for next payout — never cleared for free.
                rows = await conn.fetch("UPDATE transactions SET status = 'paid' WHERE user_id=$1 AND status='unpaid' RETURNING id, amount_cents", self.user_id)
                if not rows:
                    await ctx.respond(components=[create_error("Balance is already zero.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
                    return
                actual_cents = sum(r["amount_cents"] for r in rows)
                method = user_data["payment_method"] or "unknown"
                info = user_data["payment_info"] or "unknown"
                payment_id = await conn.fetchval("INSERT INTO payments (user_id, amount_cents, paid_by, payment_method, payment_info) VALUES ($1,$2,$3,$4,$5) RETURNING id", self.user_id, actual_cents, str(ctx.user.id), method, info)
                tx_ids = [r["id"] for r in rows]
                await conn.execute("UPDATE transactions SET payment_id = $1 WHERE id = ANY($2::int[])", payment_id, tx_ids)
                paid_cents = actual_cents
        price_str = format_cents_as_dollars(paid_cents)
        await ctx.respond(components=[create_success(f"Paid **{price_str}** to <@{self.user_id}> and recorded.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
        # Log to finance channel if linked
        try:
            async with pool.acquire() as _conn2:
                fid = await _conn2.fetchval("SELECT finance_channel_id FROM users WHERE discord_id=$1", self.user_id)
            if fid:
                log_embed = hikari.Embed(title="Payout Recorded", color=0x00FF00, description=f"Paid **{price_str}** to <@{self.user_id}>")
                log_embed.add_field("Method", f"{method} — {info}", inline=False)
                log_embed.add_field("Paid by", f"<@{ctx.user.id}>", inline=True)
                await ctx.client.app.rest.create_message(int(fid), embed=log_embed)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        failed_dispatches = await get_failed_invoice_dispatches(get_pool())
        view = FinanceMenuView(failed_dispatches)
        c = build_finance_dashboard_container(ctx.client.app, failed_dispatches)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()

    @miru.button(label="Cancel", emoji="❌", style=hikari.ButtonStyle.SECONDARY)
    async def btn_cancel(self, ctx: miru.ViewContext, button: miru.Button):
        _member = getattr(ctx, "member", None)
        _is_leader = is_team_leader(_member)
        _has_db = False
        try:
            _pool = get_pool()
            async with _pool.acquire() as _conn:
                _row = await _conn.fetchval("SELECT can_manage_payments FROM users WHERE discord_id=$1", str(ctx.user.id))
                _has_db = bool(_row)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Ignored exception', exc_info=e)
        if not (_is_leader or _has_db):
            await ctx.respond(components=[create_error("Finance manager only.")], flags=hikari.MessageFlag.EPHEMERAL | hikari.MessageFlag.IS_COMPONENTS_V2)
            return
        failed_dispatches = await get_failed_invoice_dispatches(get_pool())
        view = FinanceMenuView(failed_dispatches)
        c = build_finance_dashboard_container(ctx.client.app, failed_dispatches)
        await ctx.edit_response(components=embed_view_in_container(view, c), flags=hikari.MessageFlag.IS_COMPONENTS_V2)
        get_miru_client().start_view(view)
        self.stop()


