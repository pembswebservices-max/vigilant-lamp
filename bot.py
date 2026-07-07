"""
bot.py — Main entry point.

Commands:
/start [ref_code]   - register, capture referral code if present
/help               - show usage
/alert              - create a new alert (marketplace or crypto)
/myalerts           - list your active alerts
/stop <id>          - deactivate an alert
/refer              - show your referral link + stats
/checknow           - manually trigger a check (useful on free-tier hosting
                      that sleeps, since the scheduler won't run while asleep)

Run locally:
    1. Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN
    2. pip install -r requirements.txt
    3. python bot.py
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
from sources import ebay, crypto

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# Conversation states for the guided /alert flow
PLATFORM, KEYWORD, MAXPRICE, CONDITION, DIRECTION = range(5)


# ---------------- BASIC COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    ref_code = context.args[0] if context.args else None
    user = db.get_or_create_user(telegram_id, username, referred_by_code=ref_code)

    await update.message.reply_text(
        "Welcome to MarketAlert 🔔\n\n"
        "I'll ping you the second a matching listing or price hits your target.\n\n"
        "Commands:\n"
        "/alert - create a new alert\n"
        "/myalerts - view your alerts\n"
        "/stop <id> - remove an alert\n"
        "/refer - get your referral link\n"
        "/checknow - manually check for matches\n\n"
        f"Free tier: up to {db.FREE_TIER_ALERT_LIMIT} active alerts."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Run /start first.")
        return

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user['referral_code']}"
    await update.message.reply_text(
        f"Your referral link:\n{link}\n\n"
        "Share it — you'll earn commission when people you refer subscribe."
    )


# ---------------- /alert GUIDED FLOW ----------------

async def alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_or_create_user(update.effective_user.id, update.effective_user.username or "unknown")

    if not user["is_paid"] and db.count_active_alerts(user["telegram_id"]) >= db.FREE_TIER_ALERT_LIMIT:
        await update.message.reply_text(
            f"Free tier is limited to {db.FREE_TIER_ALERT_LIMIT} active alerts.\n"
            "Upgrade to unlimited alerts + faster checks. DM to subscribe."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "What platform? Reply with one of:\nebay / crypto"
        "\n(vinted & facebook coming soon)"
    )
    return PLATFORM


async def alert_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platform = update.message.text.strip().lower()
    if platform not in ("ebay", "crypto"):
        await update.message.reply_text("Please reply 'ebay' or 'crypto'.")
        return PLATFORM

    context.user_data["platform"] = platform
    if platform == "crypto":
        await update.message.reply_text("Which coin? (BTC, ETH, SOL, XRP, DOGE, ADA, USDT)")
    else:
        await update.message.reply_text("What are you searching for? (e.g. 'trek mountain bike')")
    return KEYWORD


async def alert_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["keyword"] = update.message.text.strip()

    if context.user_data["platform"] == "crypto":
        await update.message.reply_text("Target price in GBP? (e.g. 50000)")
    else:
        await update.message.reply_text("Max price in GBP? (e.g. 150, or 'any')")
    return MAXPRICE


async def alert_maxprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    price = None if text == "any" else float(text)
    context.user_data["max_price"] = price

    if context.user_data["platform"] == "crypto":
        await update.message.reply_text("Alert when price goes 'above' or 'below' this target?")
        return DIRECTION
    else:
        await update.message.reply_text("Condition? new / used / any")
        return CONDITION


async def alert_condition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    condition = update.message.text.strip().lower()
    if condition not in ("new", "used", "any"):
        await update.message.reply_text("Please reply new, used, or any.")
        return CONDITION

    ud = context.user_data
    alert_id = db.create_alert(
        user_id=update.effective_user.id,
        platform=ud["platform"],
        keyword=ud["keyword"],
        max_price=ud["max_price"],
        condition=condition,
    )
    await update.message.reply_text(f"✅ Alert #{alert_id} created. I'll notify you when a match appears.")
    return ConversationHandler.END


async def alert_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    direction = update.message.text.strip().lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("Please reply 'above' or 'below'.")
        return DIRECTION

    ud = context.user_data
    alert_id = db.create_alert(
        user_id=update.effective_user.id,
        platform="crypto",
        keyword=ud["keyword"],
        max_price=ud["max_price"],
        direction=direction,
    )
    await update.message.reply_text(f"✅ Alert #{alert_id} created. I'll notify you when triggered.")
    return ConversationHandler.END


async def alert_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------- MANAGE ALERTS ----------------

async def myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = db.get_active_alerts(update.effective_user.id)
    if not alerts:
        await update.message.reply_text("No active alerts. Create one with /alert")
        return

    lines = []
    for a in alerts:
        if a["platform"] == "crypto":
            lines.append(f"#{a['id']} [crypto] {a['keyword']} {a['direction']} £{a['max_price']}")
        else:
            price = f"up to £{a['max_price']}" if a["max_price"] else "any price"
            lines.append(f"#{a['id']} [{a['platform']}] '{a['keyword']}' {price}, {a['condition']}")

    await update.message.reply_text("Your active alerts:\n" + "\n".join(lines))


async def stop_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /stop <alert_id>")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Alert id must be a number.")
        return

    success = db.deactivate_alert(alert_id, update.effective_user.id)
    await update.message.reply_text("Stopped." if success else "Couldn't find that alert.")


# ---------------- CHECK LOGIC ----------------

async def run_checks(bot):
    """Checks all active alerts across platforms, sends matches, dedupes via sent_listings."""
    alerts = db.get_active_alerts()
    for a in alerts:
        try:
            if a["platform"] == "ebay":
                results = ebay.search_ebay(a["keyword"], a["max_price"], a["condition"], a["category"])
                for r in results:
                    if db.has_been_sent(a["id"], r["id"]):
                        continue
                    db.mark_as_sent(a["id"], r["id"])
                    await bot.send_message(
                        chat_id=a["user_id"],
                        text=f"🔔 Match for alert #{a['id']}:\n{r['title']}\n£{r['price']}\n{r['url']}"
                    )

            elif a["platform"] == "crypto":
                result = crypto.check_crypto_alert(a["keyword"], a["max_price"], a["direction"])
                if result and not db.has_been_sent(a["id"], result["id"]):
                    db.mark_as_sent(a["id"], result["id"])
                    await bot.send_message(
                        chat_id=a["user_id"],
                        text=f"🔔 Price alert #{a['id']} triggered:\n{result['title']}\n{result['url']}"
                    )

        except Exception as e:
            logger.error(f"Error checking alert #{a['id']}: {e}")


async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking now...")
    await run_checks(context.bot)
    await update.message.reply_text("Done. You'll get a message above if anything matched.")


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env file")

    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    alert_conv = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            PLATFORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_platform)],
            KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_keyword)],
            MAXPRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_maxprice)],
            CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_condition)],
            DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_direction)],
        },
        fallbacks=[CommandHandler("cancel", alert_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("refer", refer))
    app.add_handler(CommandHandler("myalerts", myalerts))
    app.add_handler(CommandHandler("stop", stop_alert))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(alert_conv)

    # Background scheduler — only actually useful on always-on hosting.
    # On free-tier Render, use /checknow manually instead while developing.
    # IMPORTANT: the scheduler must be started AFTER Telegram's own event loop
    # is already running, not before — so we hook it into post_init rather
    # than starting it directly in main(). Starting it too early is what
    # caused the "no current event loop" crash.
    async def on_startup(application: Application):
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            lambda: application.create_task(run_checks(application.bot)),
            "interval",
            minutes=CHECK_INTERVAL_MINUTES,
        )
        scheduler.start()
        logger.info("Scheduler started.")

    app.post_init = on_startup

    logger.info("Bot starting...")
    app.run_polling()



if __name__ == "__main__":
    main()
