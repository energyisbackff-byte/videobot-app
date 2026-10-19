"""AI Video Generator Telegram Bot — Bitcoin subscription edition.

Users subscribe monthly in BTC (auto-detected on-chain, sub tied to their
Telegram ID). Admin controls everything from Telegram: wallet address, price,
limits, manual approvals, plus full server control via /exec and /update.

Interface: HTML formatting, no emoji — professional presentation throughout.
Runs with plain polling.
"""
import asyncio
import logging
import os
import subprocess
import time
from collections import defaultdict
from html import escape
import re

VALID_BTC = re.compile(r"^(bc1[a-z0-9]{25,59}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$",
                       re.IGNORECASE)

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ConversationHandler, ContextTypes, MessageHandler,
                          filters)

import config
import subs
from fal_provider import FalError, generate_video, upload_image_to_fal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("videobot")

AWAITING_INPUT, AWAITING_PROMPT, AWAITING_TIER = range(3)
WALLET_WAIT, PRICE_WAIT = 3, 4

job_queue: asyncio.Queue = asyncio.Queue()
daily_usage: dict[int, list[float]] = defaultdict(list)

ADMIN_MENU = (
    "<b>Admin — Server Control</b>\n"
    "/status — service, uptime, RAM, disk\n"
    "/logs [n] — last n log lines (default 30)\n"
    "/restart — restart the bot service\n"
    "/reboot YES — reboot the whole server\n"
    "/set KEY=VALUE — edit configuration and restart\n"
    "/exec &lt;cmd&gt; — run any shell command as root\n"
    "/update — pull latest code from git and restart\n\n"
    "<b>Admin — Subscriptions</b>\n"
    "/approve &lt;id&gt; [days] — grant a subscription manually\n"
    "/revoke &lt;id&gt; — remove a subscription\n"
    "/subs — list active subscribers\n\n"
    "/wallet — set the receiving Bitcoin address\n"
    "/price — set the monthly price (USD)\n"
    "/set KEY=VALUE — advanced: edit any configuration value"
)

HELP_MENU = (
    "<b>AI Video Studio — Help</b>\n\n"
    "<blockquote><b>Commands</b>\n"
    "/generate — Create a new video\n"
    "/subscribe — Plan details and payment\n"
    "/subscription — Your plan status\n"
    "/cancel — Stop the current request\n"
    "/help — This menu</blockquote>\n\n"
    "<blockquote><b>Creating a video</b>\n"
    "1. Send /generate\n"
    "2. Describe the scene: subject, action, setting, lighting\n"
    "3. Optional: attach a reference photo first for character consistency\n"
    "4. Delivery typically takes 2–8 minutes</blockquote>\n\n"
    "<blockquote><b>Payment</b>\n"
    "Monthly subscription in Bitcoin. The exact amount shown identifies your "
    "payment — activation is automatic after one network confirmation.\n\n"
    f"Support: @{config.SUPPORT_USERNAME}</blockquote>"
)

# ----------------------------- helpers --------------------------------------

def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == config.ADMIN_ID


def _can_generate(user_id: int) -> bool:
    if user_id == config.ADMIN_ID or config.FREE_MODE:
        return True
    if not subs.is_subscribed(user_id):
        return False
    return subs.monthly_usage(user_id) < config.MONTHLY_VIDEO_LIMIT


def _blocked(prompt: str) -> str | None:
    low = prompt.lower()
    for term in config.BLOCKED_TERMS:
        if term in low:
            return term
    return None


def _tier_keyboard(update: Update) -> InlineKeyboardMarkup:
    rows = []
    privileged = _is_admin(update) or config.FREE_MODE
    for key, t in config.TIERS.items():
        if not privileged and key != "standard":
            continue  # subscribers ride Standard; Pro/Ultra are admin perks
        rows.append([InlineKeyboardButton(t["label"], callback_data=f"tier:{key}")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="tier:cancel")])
    return InlineKeyboardMarkup(rows)


def _clip(text: str, limit: int = 3800) -> str:
    return text if len(text) <= limit else "…(truncated)…\n" + text[-limit:]


async def _run_shell(cmd: str, timeout: int = 120) -> str:
    loop = asyncio.get_running_loop()

    def _go() -> str:
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=timeout)
            out = (p.stdout + p.stderr).strip()
            return out or "(done, no output)"
        except subprocess.TimeoutExpired:
            return f"(timed out after {timeout}s)"

    return await loop.run_in_executor(None, _go)


def _fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


async def _send_subscribe_pitch(update: Update):
    uid = update.effective_user.id
    pending = subs.get_or_create_pending(uid, config.MONTHLY_PRICE_USD)
    if not pending:
        await update.message.reply_text(
            "The Bitcoin rate is temporarily unavailable. Please try again in a minute.")
        return
    await update.message.reply_text(
        "<b>Monthly subscription</b>\n"
        f"Price: <b>{config.MONTHLY_PRICE_USD:.0f} USD in BTC</b> — "
        f"{config.SUB_DAYS} days, up to {config.MONTHLY_VIDEO_LIMIT} videos.\n\n"
        f"1. Send exactly <code>{pending['btc']}</code> BTC to:\n"
        f"<code>{config.WALLET_ADDRESS}</code>\n\n"
        "2. Nothing else. The exact amount identifies your payment. "
        f"Activation is automatic after {config.BTC_CONFIRMATIONS} network "
        "confirmation — about 10 minutes.\n\n"
        "<blockquote>Quote valid for 2 hours. If your wallet offers “subtract "
        "fee from amount”, turn it off — the exact amount shown must arrive. "
        f"Other payment methods: @{config.SUPPORT_USERNAME}</blockquote>",
        parse_mode="HTML")


# ----------------------------- user commands --------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>AI Video Studio</b>\n"
        "Cinematic AI video generation, delivered in this chat.\n\n"
        "● Describe any scene — receive a finished clip in minutes\n"
        "● Attach a reference photo to keep a character consistent\n"
        "● Monthly plan payable in Bitcoin — activation is automatic\n\n"
        "Commands: /generate · /subscribe · /subscription · /help"
    )
    if _is_admin(update):
        text += "\n\nAdmin: /admin for server control."
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MENU, parse_mode="HTML")


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _can_generate(update.effective_user.id) and subs.is_subscribed(update.effective_user.id):
        await cmd_subscription(update, ctx)
        return
    await _send_subscribe_pitch(update)


async def cmd_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _is_admin(update):
        await update.message.reply_text("Admin — unlimited access.")
        return
    info = subs.sub_info(uid)
    if info and info["until"] > time.time():
        used = subs.monthly_usage(uid)
        await update.message.reply_text(
            "<b>Active subscription</b>\n"
            f"Expires: <b>{_fmt_date(info['until'])}</b>\n"
            f"Videos this month: <b>{used}/{config.MONTHLY_VIDEO_LIMIT}</b>",
            parse_mode="HTML")
    else:
        await update.message.reply_text(
            "No active subscription. Send /subscribe to get one.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Send /generate to start over.")
    return ConversationHandler.END


# ------------------------- admin commands -----------------------------------

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text(ADMIN_MENU, parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    out = await _run_shell(
        "systemctl status videobot --no-pager | head -14; echo '---'; "
        "uptime; echo '---'; free -h | head -2; echo '---'; df -h / | tail -1")
    await update.message.reply_text(
        f"<pre>{escape(_clip(out))}</pre>", parse_mode="HTML")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    n = 30
    if ctx.args and ctx.args[0].isdigit():
        n = min(200, int(ctx.args[0]))
    out = await _run_shell(f"journalctl -u videobot -n {n} --no-pager")
    await update.message.reply_text(
        f"<pre>{escape(_clip(out))}</pre>", parse_mode="HTML")


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text("Restarting the bot service…")
    await _run_shell("systemctl restart videobot")
    await update.message.reply_text("Done. /status to verify.")


async def cmd_reboot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not (ctx.args and ctx.args[0] == "YES"):
        await update.message.reply_text(
            "This reboots the entire server. Send /reboot YES to confirm.")
        return
    await update.message.reply_text("Rebooting in 5 seconds. Back in about a minute.")
    await _run_shell("nohup bash -c 'sleep 5; reboot' >/dev/null 2>&1 &")


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or "=" not in ctx.args[0]:
        await update.message.reply_text(
            "Usage: /set KEY=VALUE\n"
            "Common settings have their own commands: /wallet and /price.")
        return
    kv = ctx.args[0]
    key = kv.split("=", 1)[0].strip()
    if not key or not all(c.isalnum() or c == "_" for c in key):
        await update.message.reply_text("Invalid key name.")
        return
    shell_cmd = (
        f"cd /root/videobot && "
        f"(grep -q '^{key}=' .env && sed -i 's|^{key}=.*|{kv}|' .env "
        f"|| echo '{kv}' >> .env) && systemctl restart videobot")
    await _run_shell(shell_cmd)
    await update.message.reply_text(
        f"Done. <code>{escape(key)}</code> updated and the bot restarted.",
        parse_mode="HTML")


async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pull the latest code from the git repo and restart. Admin only."""
    if not _is_admin(update):
        return
    await update.message.reply_text("Pulling the latest code from git…")
    out = await _run_shell(
        "cd /root/videobot && git pull --ff-only 2>&1; echo EXIT=$?")
    if "EXIT=0" not in out:
        await update.message.reply_text(
            f"<pre>{escape(_clip(out))}</pre>\nGit repo not set up yet (or pull failed) — see GIT_SETUP.md",
            parse_mode="HTML")
        return
    await update.message.reply_text(
        f"<pre>{escape(_clip(out))}</pre>\nPulled. Restarting with the new code…",
        parse_mode="HTML")
    await _run_shell("systemctl restart videobot")


async def cmd_exec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    cmdline = update.message.text.partition(" ")[2].strip()
    if not cmdline:
        await update.message.reply_text("Usage: /exec <shell command>")
        return
    log.warning("ADMIN /exec: %s", cmdline)
    await update.message.reply_text(f"Running: <code>{escape(cmdline)}</code>",
                                    parse_mode="HTML")
    out = await _run_shell(f"cd /root && {cmdline}", timeout=180)
    await update.message.reply_text(
        f"<pre>{escape(_clip(out))}</pre>", parse_mode="HTML")


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /approve <telegram_id> [days]")
        return
    uid = int(ctx.args[0])
    days = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else config.SUB_DAYS
    until = subs.activate(uid, days, granted_by=f"admin:{update.effective_user.id}")
    await update.message.reply_text(
        f"Done. <code>{uid}</code> subscribed until <b>{_fmt_date(until)}</b> ({days} days).",
        parse_mode="HTML")
    try:
        await ctx.bot.send_message(
            uid,
            f"<b>Subscription active</b>\nYour plan runs until "
            f"<b>{_fmt_date(until)}</b>.\nSend /generate to create a video.",
            parse_mode="HTML")
    except Exception:
        pass


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke <telegram_id>")
        return
    uid = int(ctx.args[0])
    if subs.revoke(uid):
        await update.message.reply_text(
            f"Done. Subscription removed for <code>{uid}</code>.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"<code>{uid}</code> has no subscription.", parse_mode="HTML")


async def cmd_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    active = subs.list_subs()
    if not active:
        await update.message.reply_text("No active subscribers.")
        return
    lines = [f"<b>Active subscribers: {len(active)}</b>\n"]
    for uid, s in sorted(active.items(), key=lambda x: x[1]["until"]):
        lines.append(
            f"● <code>{uid}</code> — until {_fmt_date(s['until'])} ({escape(s.get('granted_by',''))})")
    await update.message.reply_text(_clip("\n".join(lines)), parse_mode="HTML")


# --------------------- admin: wallet & price (conversational) ---------------

async def wallet_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Send the <b>Bitcoin address</b> that payments should go to.\n"
        "Formats: bc1q…, bc1p…, 1…, or 3…\n"
        "Send /cancel to abort.",
        parse_mode="HTML")
    return WALLET_WAIT


async def wallet_got(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = (update.message.text or "").strip()
    if not VALID_BTC.match(addr):
        await update.message.reply_text(
            "That doesn't look like a valid Bitcoin address. "
            "Check it and send again, or /cancel.")
        return WALLET_WAIT
    await _run_shell(
        f"cd /root/videobot && (grep -q '^WALLET_ADDRESS=' .env && "
        f"sed -i 's|^WALLET_ADDRESS=.*|WALLET_ADDRESS={addr}|' .env "
        f"|| echo 'WALLET_ADDRESS={addr}' >> .env)")
    await update.message.reply_text(
        f"Done. Payments will now go to:\n<code>{escape(addr)}</code>\n\n"
        "Restarting the watcher on this address…",
        parse_mode="HTML")
    await _run_shell(
        "nohup bash -c 'sleep 2; systemctl restart videobot' >/dev/null 2>&1 &")
    return ConversationHandler.END


async def price_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        f"Current monthly price: {config.MONTHLY_PRICE_USD:.0f} USD.\n"
        "Send the new price in USD (numbers only), or /cancel to abort.")
    return PRICE_WAIT


async def price_got(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip().lstrip("$").replace(",", ".")
    try:
        price = float(raw)
        if not (1 <= price <= 10000):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Send a plain number between 1 and 10000 — e.g. 25. Or /cancel.")
        return PRICE_WAIT
    kv = f"MONTHLY_PRICE_USD={price:g}"
    await _run_shell(
        f"cd /root/videobot && (grep -q '^MONTHLY_PRICE_USD=' .env && "
        f"sed -i 's|^MONTHLY_PRICE_USD=.*|{kv}|' .env "
        f"|| echo '{kv}' >> .env)")
    await update.message.reply_text(
        f"Done. Monthly price set to <b>{price:g} USD</b>. Restarting…",
        parse_mode="HTML")
    await _run_shell(
        "nohup bash -c 'sleep 2; systemctl restart videobot' >/dev/null 2>&1 &")
    return ConversationHandler.END


# ------------------------- generate conversation ----------------------------

async def gen_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _can_generate(uid):
        if subs.is_subscribed(uid):
            await update.message.reply_text(
                f"You have used your {config.MONTHLY_VIDEO_LIMIT} videos for this "
                "month. The limit resets on the 1st.")
        else:
            await _send_subscribe_pitch(update)
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        "Describe the video you want.\n\n"
        "Optional: send a <b>photo first</b> if you want a specific character "
        "or product in it — then send the description.",
        parse_mode="HTML")
    return AWAITING_INPUT


async def got_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        photo = update.message.photo[-1]
        f = await photo.get_file()
        path = f"ref_{update.effective_user.id}_{int(time.time())}.jpg"
        await f.download_to_drive(path)
        ctx.user_data["photo_path"] = path
        await update.message.reply_text(
            "Reference photo received. Now describe the video.")
        return AWAITING_PROMPT
    return await _got_prompt(update, ctx)


async def _got_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = (update.message.text or "").strip()
    if len(prompt) < 8:
        await update.message.reply_text(
            "Give me more detail — subject, action, setting, lighting.")
        return AWAITING_PROMPT if ctx.user_data.get("photo_path") else AWAITING_INPUT
    if _blocked(prompt):
        await update.message.reply_text(
            "That request can't be generated. Try a different idea.")
        return ConversationHandler.END
    ctx.user_data["prompt"] = prompt
    await update.message.reply_text("Choose quality:",
                                    reply_markup=_tier_keyboard(update))
    return AWAITING_TIER


async def got_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _got_prompt(update, ctx)


async def got_tier(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tier_key = q.data.split(":", 1)[1]
    if tier_key == "cancel":
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END
    if not _can_generate(q.from_user.id):
        await q.edit_message_text("Subscription required — send /subscribe.")
        return ConversationHandler.END
    if tier_key != "standard" and not (_is_admin(update) or config.FREE_MODE):
        tier_key = "standard"  # subscribers are Standard-only
    ctx.user_data["tier"] = tier_key
    tier = config.TIERS[tier_key]
    await q.edit_message_text(f"Queued — {tier['label']}.")
    await _enqueue(q.message.chat_id, q.from_user.id, ctx)
    return ConversationHandler.END


async def _enqueue(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    daily_usage[user_id].append(time.time())
    await job_queue.put({
        "chat_id": chat_id,
        "user_id": user_id,
        "prompt": ctx.user_data["prompt"],
        "tier": ctx.user_data["tier"],
        "photo_path": ctx.user_data.get("photo_path"),
    })


# ----------------------------- worker ---------------------------------------

async def worker(app: Application):
    while True:
        job = await job_queue.get()
        chat_id = job["chat_id"]
        tier = config.TIERS[job["tier"]]
        status = await app.bot.send_message(
            chat_id, f"Processing — {tier['label']}. Typical time: 2–8 minutes.")
        out = f"out_{chat_id}_{int(time.time())}.mp4"
        try:
            image_url = None
            model = tier["model"]
            if job["photo_path"]:
                await status.edit_text("Uploading reference photo…")
                image_url = upload_image_to_fal(config.FAL_KEY, job["photo_path"])
                model = tier["model_i2v"]

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: generate_video(
                    config.FAL_KEY, model, job["prompt"], out,
                    duration=config.CLIP_SECONDS,
                    aspect_ratio=config.ASPECT_RATIO,
                    image_url=image_url))
            await status.delete()
            with open(out, "rb") as v:
                await app.bot.send_video(
                    chat_id, v,
                    caption=f"AI Video Studio — {escape(job['prompt'][:90])}",
                    parse_mode="HTML", supports_streaming=True)
            if job["user_id"] != config.ADMIN_ID and not config.FREE_MODE:
                subs.increment_usage(job["user_id"])
        except FalError as e:
            await status.edit_text(
                f"Generation failed: {escape(str(e))}\nPlease try again later.",
                parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            log.exception("worker error")
            await status.edit_text(f"Unexpected error: {escape(str(e))}",
                                   parse_mode="HTML")
        finally:
            for p in (out, job.get("photo_path")):
                if p and os.path.exists(p):
                    os.remove(p)
        job_queue.task_done()


# ------------------------- payment watcher ----------------------------------

async def payment_watcher(app: Application):
    while True:
        try:
            hits = subs.check_btc_payments(config.WALLET_ADDRESS,
                                           config.BTC_CONFIRMATIONS)
            for hit in hits:
                until = subs.activate(hit["user_id"], config.SUB_DAYS,
                                      txid=hit["txid"])
                log.info("payment confirmed: user=%s sats=%s txid=%s",
                         hit["user_id"], hit["sats"], hit["txid"])
                try:
                    await app.bot.send_message(
                        hit["user_id"],
                        "<b>Payment confirmed</b>\n"
                        f"{hit['btc']} BTC received.\n"
                        f"Your subscription is active until <b>{_fmt_date(until)}</b>.\n\n"
                        "Send /generate to create a video.",
                        parse_mode="HTML")
                except Exception:
                    pass
                if config.ADMIN_ID:
                    try:
                        await app.bot.send_message(
                            config.ADMIN_ID,
                            f"New subscriber <code>{hit['user_id']}</code> — "
                            f"{hit['btc']} BTC\ntx <code>{hit['txid'][:24]}…</code>",
                            parse_mode="HTML")
                    except Exception:
                        pass
        except Exception:
            log.exception("watcher error")
        await asyncio.sleep(60)


async def post_init(app: Application):
    app.create_task(worker(app))
    app.create_task(payment_watcher(app))


# ----------------------------- main -----------------------------------------

def main():
    if not config.BOT_TOKEN or not config.FAL_KEY:
        raise SystemExit("Set BOT_TOKEN and FAL_KEY in .env first (see README).")

    app = (Application.builder()
           .token(config.BOT_TOKEN)
           .post_init(post_init)
           .build())

    conv = ConversationHandler(
        entry_points=[CommandHandler("generate", gen_start)],
        states={
            AWAITING_INPUT:  [MessageHandler(filters.PHOTO, got_input),
                              MessageHandler(filters.TEXT & ~filters.COMMAND, got_input)],
            AWAITING_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_prompt)],
            AWAITING_TIER:   [CallbackQueryHandler(got_tier, pattern=r"^tier:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("subscription", cmd_subscription))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("reboot", cmd_reboot))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("subs", cmd_subs))

    admin_settings_conv = ConversationHandler(
        entry_points=[CommandHandler("wallet", wallet_start),
                      CommandHandler("price", price_start)],
        states={
            WALLET_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_got)],
            PRICE_WAIT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, price_got)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(admin_settings_conv)
    app.add_handler(conv)

    log.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
