"""AI Video Generator Telegram Bot — crypto subscription edition.

Users subscribe monthly (USDT-TRC20, auto-detected on-chain, sub tied to
their Telegram ID). Admin controls everything from Telegram: wallet address,
price, limits, manual approvals, plus full server control via /exec.

Runs with plain polling.
"""
import asyncio
import logging
import os
import subprocess
import time
from collections import defaultdict

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

job_queue: asyncio.Queue = asyncio.Queue()
daily_usage: dict[int, list[float]] = defaultdict(list)

ADMIN_MENU = (
    "👑 *Admin panel — server control*\n\n"
    "/status — service, uptime, RAM, disk\n"
    "/logs \\[n] — last n bot log lines (default 30)\n"
    "/restart — restart the bot service\n"
    "/reboot YES — reboot the whole server\n"
    "/set KEY\\=VALUE — edit .env and restart\n"
    "/exec <cmd> — ⚠️ run ANY shell command as root\n"
    "/update — pull latest code from git & restart\n\n"
    "💰 *Subscriptions*\n"
    "/approve <tg\\_id> \\[days] — manually grant subscription\n"
    "/revoke <tg\\_id> — remove a subscription\n"
    "/subs — list active subscribers\n"
    "Wallet: /set WALLET\\_ADDRESS\\=T… · Price: /set MONTHLY\\_PRICE\\_USD\\=25"
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
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tier:cancel")])
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
    amount = subs.get_or_create_pending(uid, config.MONTHLY_PRICE_USD)
    await update.message.reply_text(
        "🔒 *Monthly subscription required*\n\n"
        f"Price: *{config.MONTHLY_PRICE_USD:.0f} USDT* (TRC20) — "
        f"{config.SUB_DAYS} days, up to {config.MONTHLY_VIDEO_LIMIT} videos.\n\n"
        f"1️⃣ Send EXACTLY `{amount}` USDT on the *TRC20* network to:\n"
        f"`{config.WALLET_ADDRESS}`\n\n"
        f"2️⃣ That's all. The cents in `{amount}` identify *your* payment — "
        "send the exact figure from any wallet or exchange. Approval is "
        "automatic, usually 1–2 minutes after it lands. No screenshots, no "
        "TXID needed.\n\n"
        f"Other payment methods: message support @{config.SUPPORT_USERNAME}",
        parse_mode="Markdown")


# ----------------------------- user commands --------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎬 *AI Video Studio Bot*\n\n"
        "Describe any video — I generate it in high quality and deliver it "
        "here. Attach a reference photo to keep a character consistent.\n\n"
        "• /generate — create a video\n"
        "• /subscribe — monthly plan & how to pay\n"
        "• /subscription — your plan status\n"
        "• /cancel — stop the current request"
    )
    if _is_admin(update):
        text += "\n\n👑 You're admin — /admin for server control."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _can_generate(update.effective_user.id) and subs.is_subscribed(update.effective_user.id):
        await cmd_subscription(update, ctx)
        return
    await _send_subscribe_pitch(update)


async def cmd_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _is_admin(update):
        await update.message.reply_text("👑 Admin — unlimited, forever.")
        return
    info = subs.sub_info(uid)
    if info and info["until"] > time.time():
        used = subs.monthly_usage(uid)
        await update.message.reply_text(
            f"✅ *Active subscription*\n"
            f"Expires: *{_fmt_date(info['until'])}*\n"
            f"Videos this month: *{used}/{config.MONTHLY_VIDEO_LIMIT}*",
            parse_mode="Markdown")
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
    await update.message.reply_text(ADMIN_MENU, parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    out = await _run_shell(
        "systemctl status videobot --no-pager | head -14; echo '---'; "
        "uptime; echo '---'; free -h | head -2; echo '---'; df -h / | tail -1")
    await update.message.reply_text(f"```\n{_clip(out)}\n```", parse_mode="Markdown")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    n = 30
    if ctx.args and ctx.args[0].isdigit():
        n = min(200, int(ctx.args[0]))
    out = await _run_shell(f"journalctl -u videobot -n {n} --no-pager")
    await update.message.reply_text(f"```\n{_clip(out)}\n```", parse_mode="Markdown")


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text("Restarting bot service…")
    await _run_shell("systemctl restart videobot")
    await update.message.reply_text("✅ Restarted. /status to check.")


async def cmd_reboot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not (ctx.args and ctx.args[0] == "YES"):
        await update.message.reply_text(
            "This reboots the *whole server*. Send `/reboot YES` to confirm.",
            parse_mode="Markdown")
        return
    await update.message.reply_text("Rebooting in 5 seconds… back in ~1 minute.")
    await _run_shell("nohup bash -c 'sleep 5; reboot' >/dev/null 2>&1 &")


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or "=" not in ctx.args[0]:
        await update.message.reply_text(
            "Usage: `/set KEY=VALUE`", parse_mode="Markdown")
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
        f"✅ `{key}` updated in .env and bot restarted.", parse_mode="Markdown")


async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pull the latest code from the private git repo and restart. Admin only."""
    if not _is_admin(update):
        return
    await update.message.reply_text("⏳ Pulling latest code from git…")
    out = await _run_shell(
        "cd /root/videobot && git pull --ff-only 2>&1; echo EXIT=$?")
    if "EXIT=0" not in out:
        await update.message.reply_text(
            f"```\n{_clip(out)}\n```\n⚠️ Git repo not set up yet (or pull failed) — see GIT_SETUP.md",
            parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"```\n{_clip(out)}\n```\n✅ Pulled. Restarting with new code…",
        parse_mode="Markdown")
    await _run_shell("systemctl restart videobot")


async def cmd_exec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    cmdline = update.message.text.partition(" ")[2].strip()
    if not cmdline:
        await update.message.reply_text("Usage: `/exec <shell command>`",
                                        parse_mode="Markdown")
        return
    log.warning("ADMIN /exec: %s", cmdline)
    await update.message.reply_text(f"⏳ `$ {cmdline}`", parse_mode="Markdown")
    out = await _run_shell(f"cd /root && {cmdline}", timeout=180)
    await update.message.reply_text(f"```\n{_clip(out)}\n```", parse_mode="Markdown")


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/approve <telegram_id> [days]`", parse_mode="Markdown")
        return
    uid = int(ctx.args[0])
    days = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else config.SUB_DAYS
    until = subs.activate(uid, days, granted_by=f"admin:{update.effective_user.id}")
    await update.message.reply_text(
        f"✅ `{uid}` subscribed until *{_fmt_date(until)}* ({days} days).",
        parse_mode="Markdown")
    try:
        await ctx.bot.send_message(
            uid, f"✅ Your subscription is active until {_fmt_date(until)}! "
                 "Send /generate to create a video.")
    except Exception:
        pass


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: `/revoke <telegram_id>`",
                                        parse_mode="Markdown")
        return
    uid = int(ctx.args[0])
    if subs.revoke(uid):
        await update.message.reply_text(f"🚫 Subscription removed for `{uid}`.",
                                        parse_mode="Markdown")
    else:
        await update.message.reply_text(f"`{uid}` has no subscription.",
                                        parse_mode="Markdown")


async def cmd_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    active = subs.list_subs()
    if not active:
        await update.message.reply_text("No active subscribers.")
        return
    lines = [f"💰 *Active subscribers: {len(active)}*\n"]
    for uid, s in sorted(active.items(), key=lambda x: x[1]["until"]):
        lines.append(f"• `{uid}` — until {_fmt_date(s['until'])} ({s.get('granted_by','')})")
    await update.message.reply_text(_clip("\n".join(lines)), parse_mode="Markdown")


# ------------------------- generate conversation ----------------------------

async def gen_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _can_generate(uid):
        if subs.is_subscribed(uid):
            await update.message.reply_text(
                f"You've used your {config.MONTHLY_VIDEO_LIMIT} videos for this "
                "month. Your limit resets on the 1st.")
        else:
            await _send_subscribe_pitch(update)
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        "Describe the video you want.\n\n"
        "Optional: send a *photo first* if you want a specific "
        "character/product in it — then send the description.",
        parse_mode="Markdown")
    return AWAITING_INPUT


async def got_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        photo = update.message.photo[-1]
        f = await photo.get_file()
        path = f"ref_{update.effective_user.id}_{int(time.time())}.jpg"
        await f.download_to_drive(path)
        ctx.user_data["photo_path"] = path
        await update.message.reply_text(
            "Got the reference photo ✅ Now describe the video.")
        return AWAITING_PROMPT
    return await _got_prompt(update, ctx)


async def _got_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = (update.message.text or "").strip()
    if len(prompt) < 8:
        await update.message.reply_text(
            "Give me a bit more detail — subject, action, setting, lighting.")
        return AWAITING_PROMPT if ctx.user_data.get("photo_path") else AWAITING_INPUT
    if _blocked(prompt):
        await update.message.reply_text(
            "Sorry, I can't generate that kind of content. Try a different idea.")
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
    await q.edit_message_text(f"🎬 {tier['label']} — queued!")
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
        status = await app.bot.send_message(chat_id, f"⏳ {tier['label']}…")
        out = f"out_{chat_id}_{int(time.time())}.mp4"
        try:
            image_url = None
            model = tier["model"]
            if job["photo_path"]:
                await status.edit_text("⏳ Uploading reference photo…")
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
                    chat_id, v, caption=f"🎬 {job['prompt'][:100]}",
                    supports_streaming=True)
            if job["user_id"] != config.ADMIN_ID and not config.FREE_MODE:
                subs.increment_usage(job["user_id"])
        except FalError as e:
            await status.edit_text(f"⚠️ Generation failed: {e}\nTry again later.")
        except Exception as e:  # noqa: BLE001
            log.exception("worker error")
            await status.edit_text(f"⚠️ Unexpected error: {e}")
        finally:
            for p in (out, job.get("photo_path")):
                if p and os.path.exists(p):
                    os.remove(p)
        job_queue.task_done()


# ------------------------- payment watcher ----------------------------------

async def payment_watcher(app: Application):
    while True:
        try:
            hits = subs.check_tron_payments(config.WALLET_ADDRESS)
            for hit in hits:
                until = subs.activate(hit["user_id"], config.SUB_DAYS,
                                      txid=hit["txid"])
                log.info("payment confirmed: user=%s amount=%s txid=%s",
                         hit["user_id"], hit["amount"], hit["txid"])
                try:
                    await app.bot.send_message(
                        hit["user_id"],
                        f"✅ *Payment confirmed* — {hit['amount']} USDT received.\n"
                        f"Your subscription is active until "
                        f"*{_fmt_date(until)}*.\nSend /generate to create a video!",
                        parse_mode="Markdown")
                except Exception:
                    pass
                if config.ADMIN_ID:
                    try:
                        await app.bot.send_message(
                            config.ADMIN_ID,
                            f"💰 New subscriber `{hit['user_id']}` paid "
                            f"{hit['amount']} USDT\n(tx `{hit['txid'][:20]}…`)",
                            parse_mode="Markdown")
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
    app.add_handler(conv)

    log.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
