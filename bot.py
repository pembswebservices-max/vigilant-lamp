"""
bot.py — Main entry point.

USER COMMANDS:
/start [ref_code]   - register, capture referral code if present
/help               - show usage
/alert              - create a new alert (guided flow: platform, category, keyword, price, condition)
/myalerts           - list your active alerts
/stop <id>          - deactivate an alert
/cancel             - cancel whatever you're in the middle of (e.g. mid /alert flow)
/refer              - show your referral link + earnings
/upgrade            - see pricing, how to pay right now (manual)
/checknow           - manually trigger a check (needed on free-tier hosting that sleeps)

ADMIN COMMANDS (only work for your own Telegram ID — see ADMIN_TELEGRAM_ID in .env):
/addpaid <telegram_id> <lifetime|monthly> <amount_gbp>
    Marks a user as paid after you've taken payment manually (bank transfer/PayPal etc).
    Also auto-credits referral commission to whoever referred them, if anyone.
/paidcommission <referral_id>
    Marks a referral commission as paid out, once you've sent the referrer their cut.
/stats
    Quick dashboard: total users, paid users, active alerts by platform, unpaid commission.

Run locally:
    1. Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN + ADMIN_TELEGRAM_ID
    2. pip install -r requirements.txt
    3. python bot.py
"""

import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
from sources import ebay, crypto, vinted

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")  # your own Telegram ID, as a string
PORT = int(os.getenv("PORT", "8080"))  # Render sets this automatically for web services

LIFETIME_PRICE = 100
MONTHLY_PRICE = 20

# Conversation states for the guided /alert flow
PLATFORM, CATEGORY, KEYWORD, MAXPRICE, CONDITION, DIRECTION = range(6)

MAX_RESULTS_PER_ALERT_PER_CHECK = 5  # cap so one alert can't flood a user with messages


# ---------------- HEALTH-CHECK SERVER ----------------
# Render's free tier sleeps a web service after ~15 min with no HTTP traffic.
# This tiny server gives it something to respond to. Point a free uptime
# pinger (e.g. UptimeRobot or cron-job.org) at your Render URL every 5-10 min
# and the service will now genuinely stay awake, instead of needing /checknow
# run by hand. On a paid always-on plan this endpoint is harmless but unused.

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"MarketAlert bot is running.")

    def log_message(self, format, *args):
        pass  # suppress noisy request logs, we have our own logging


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"Health-check server listening on port {PORT}")
    server.serve_forever()


# ---------------- BASIC COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    ref_code = context.args[0] if context.args else None
    db.get_or_create_user(telegram_id, username, referred_by_code=ref_code)

    await update.message.reply_text(
        "Welcome to MarketAlert 🔔\n\n"
        "I'll ping you the second a matching listing or price hits your target.\n\n"
        "Commands:\n"
        "/alert - create a new alert\n"
        "/myalerts - view your alerts\n"
        "/stop <id> - remove an alert\n"
        "/refer - get your referral link + earnings\n"
        "/upgrade - see pricing\n"
        "/checknow - manually check for matches\n"
        "/cancel - cancel whatever you're doing\n\n"
        f"Free tier: up to {db.FREE_TIER_ALERT_LIMIT} active alerts."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Nothing in progress.")
    return ConversationHandler.END


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💳 Pricing:\n\n"
        f"Monthly: £{MONTHLY_PRICE}/month — unlimited alerts, all platforms\n"
        f"Lifetime: £{LIFETIME_PRICE} one-off — unlimited alerts, forever\n\n"
        "Payments are handled manually right now — DM me directly to pay and "
        "I'll activate your account within a few hours."
    )


async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Run /start first.")
        return

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user['referral_code']}"

    summary = db.get_referral_summary(update.effective_user.id)
    lines = [f"Your referral link:\n{link}\n"]
    lines.append(f"You earn {int(db.COMMISSION_RATE * 100)}% commission when people you refer subscribe.\n")

    if summary["referrals"]:
        lines.append(f"People referred: {len(summary['referrals'])}")
        lines.append(f"Total commission earned: £{summary['total_earned']}")
        lines.append(f"Unpaid (owed to you): £{summary['total_owed']}")
    else:
        lines.append("No referrals yet — share your link to start earning.")

    await update.message.reply_text("\n".join(lines))


# ---------------- /alert GUIDED FLOW ----------------

PLATFORM_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("eBay", callback_data="platform:ebay"),
     InlineKeyboardButton("Vinted", callback_data="platform:vinted")],
    [InlineKeyboardButton("Crypto price alert", callback_data="platform:crypto")],
])


def category_keyboard():
    buttons = [[InlineKeyboardButton(cat, callback_data=f"category:{cat}")] for cat in db.CATEGORIES]
    return InlineKeyboardMarkup(buttons)


async def alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()  # guard against stale state from a previous cancelled flow
    user = db.get_or_create_user(update.effective_user.id, update.effective_user.username or "unknown")

    if not db.is_currently_paid(user) and db.count_active_alerts(user["telegram_id"]) >= db.FREE_TIER_ALERT_LIMIT:
        await update.message.reply_text(
            f"Free tier is limited to {db.FREE_TIER_ALERT_LIMIT} active alerts.\n"
            "Use /upgrade to unlock unlimited alerts across all platforms."
        )
        return ConversationHandler.END

    await update.message.reply_text("What platform is this alert for?", reply_markup=PLATFORM_KEYBOARD)
    return PLATFORM


async def alert_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    platform = query.data.split(":")[1]  # 'ebay', 'vinted', or 'crypto'
    context.user_data["platform"] = platform

    if platform == "crypto":
        await query.edit_message_text("Which coin? (BTC, ETH, SOL, XRP, DOGE, ADA, USDT)")
        return KEYWORD
    else:
        await query.edit_message_text("Pick a category:", reply_markup=category_keyboard())
        return CATEGORY


async def alert_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split(":")[1]
    context.user_data["category"] = category

    await query.edit_message_text(
        f"Category: {category}\n\nWhat are you searching for? (e.g. 'trek mountain bike')"
    )
    return KEYWORD


async def alert_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    if not keyword or len(keyword) > 200:
        await update.message.reply_text("Please enter a search term between 1 and 200 characters.")
        return KEYWORD

    if context.user_data.get("platform") == "crypto" and keyword.upper() not in crypto.SYMBOL_MAP:
        await update.message.reply_text(
            f"Unknown coin '{keyword}'. Supported: {', '.join(crypto.SYMBOL_MAP)}"
        )
        return KEYWORD

    context.user_data["keyword"] = keyword

    if context.user_data["platform"] == "crypto":
        await update.message.reply_text("Target price in GBP? (e.g. 50000)")
    else:
        await update.message.reply_text("Max price in GBP? (e.g. 150, or 'any')")
    return MAXPRICE


async def alert_maxprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    try:
        price = None if text == "any" else float(text)
        if price is not None and price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please reply with a positive number, or 'any'.")
        return MAXPRICE

    if context.user_data.get("platform") == "crypto" and price is None:
        await update.message.reply_text("Crypto alerts need a specific target price — please enter a number.")
        return MAXPRICE

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
        category=ud.get("category"),
    )
    context.user_data.clear()
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
    context.user_data.clear()
    await update.message.reply_text(f"✅ Alert #{alert_id} created. I'll notify you when triggered.")
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
            cat = f" ({a['category']})" if a["category"] else ""
            lines.append(f"#{a['id']} [{a['platform']}]{cat} '{a['keyword']}' {price}, {a['condition']}")

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
    await update.message.reply_text("Stopped." if success else "Couldn't find that alert, or it's not yours.")


# ---------------- CHECK LOGIC ----------------

async def run_checks(bot):
    """Checks all active alerts across platforms, sends matches, dedupes via sent_listings."""
    alerts = db.get_active_alerts()
    logger.info(f"Running checks on {len(alerts)} active alert(s)...")

    for a in alerts:
        try:
            if a["platform"] == "ebay":
                results = ebay.search_ebay(a["keyword"], a["max_price"], a["condition"], a["category"])
            elif a["platform"] == "vinted":
                results = vinted.search_vinted(a["keyword"], a["max_price"], a["condition"], a["category"])
            elif a["platform"] == "crypto":
                result = crypto.check_crypto_alert(a["keyword"], a["max_price"], a["direction"])
                results = [result] if result else []
            else:
                continue

            sent_this_check = 0
            for r in results:
                if sent_this_check >= MAX_RESULTS_PER_ALERT_PER_CHECK:
                    break
                if db.has_been_sent(a["id"], r["id"]):
                    continue
                db.mark_as_sent(a["id"], r["id"])
                sent_this_check += 1

                if a["platform"] == "crypto":
                    text = f"🔔 Price alert #{a['id']} triggered:\n{r['title']}\n{r['url']}"
                else:
                    text = f"🔔 Match for alert #{a['id']}:\n{r['title']}\n£{r['price']}\n{r['url']}"

                try:
                    await bot.send_message(chat_id=a["user_id"], text=text)
                except Forbidden:
                    # User blocked the bot — deactivate their alerts so we stop wasting API calls on them
                    logger.info(f"User {a['user_id']} has blocked the bot, deactivating alert #{a['id']}")
                    db.deactivate_alert(a["id"], a["user_id"])
                except TelegramError as e:
                    logger.warning(f"Telegram error sending to {a['user_id']}: {e}")

        except Exception as e:
            logger.error(f"Error checking alert #{a['id']} ({a['platform']}): {e}")


async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking now...")
    await run_checks(context.bot)
    await update.message.reply_text("Done. You'll get a message above if anything matched.")


# ---------------- ADMIN COMMANDS ----------------

def _is_admin(update: Update) -> bool:
    return bool(ADMIN_TELEGRAM_ID) and str(update.effective_user.id) == str(ADMIN_TELEGRAM_ID)


async def addpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return  # silently ignore for non-admins — don't reveal this command exists

    if len(context.args) != 3:
        await update.message.reply_text("Usage: /addpaid <telegram_id> <lifetime|monthly> <amount_gbp>")
        return

    try:
        target_id = int(context.args[0])
        plan = context.args[1].lower()
        amount = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Bad arguments. Usage: /addpaid <telegram_id> <lifetime|monthly> <amount_gbp>")
        return

    if plan not in ("lifetime", "monthly"):
        await update.message.reply_text("Plan must be 'lifetime' or 'monthly'.")
        return
    if amount <= 0:
        await update.message.reply_text("Amount must be positive.")
        return

    result = db.mark_user_paid(target_id, plan, amount)
    if result is None:
        await update.message.reply_text(
            f"⚠️ No user found with Telegram ID {target_id}. They need to /start the bot at least once first."
        )
        return

    referrer_id, commission = result
    await update.message.reply_text(f"✅ User {target_id} marked as paid ({plan}, £{amount}).")

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🎉 Payment confirmed! You now have {plan} access. Use /alert to set up unlimited alerts."
        )
    except TelegramError as e:
        logger.warning(f"Couldn't DM user {target_id}: {e}")

    if referrer_id:
        await update.message.reply_text(f"💰 Referral commission of £{commission} credited to user {referrer_id}.")
        try:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=f"💰 You just earned £{commission} commission from a referral! Check /refer for your total."
            )
        except TelegramError as e:
            logger.warning(f"Couldn't DM referrer {referrer_id}: {e}")


async def paidcommission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /paidcommission <referral_id>")
        return

    try:
        referral_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Referral id must be a number.")
        return

    success = db.mark_commission_paid(referral_id)
    await update.message.reply_text("✅ Marked as paid." if success else "Couldn't find that referral.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    s = db.get_stats()
    platform_lines = "\n".join(f"  {p}: {c}" for p, c in s["alerts_by_platform"].items()) or "  none yet"

    await update.message.reply_text(
        "📊 Bot stats\n\n"
        f"Total users: {s['total_users']}\n"
        f"Paid users (flagged): {s['paid_users_flagged']}\n"
        f"Active alerts: {s['active_alerts']}\n"
        f"{platform_lines}\n\n"
        f"Total referrals made: {s['total_referrals']}\n"
        f"Unpaid commission owed (total): £{s['unpaid_commission_total']}"
    )


# ---------------- GLOBAL ERROR HANDLER ----------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches anything not already handled so the bot never silently dies on one
    bad update. Logs full details and, if configured, pings the admin so you
    find out immediately rather than discovering it days later from a user complaint.
    """
    logger.error("Unhandled exception while processing an update:", exc_info=context.error)

    if ADMIN_TELEGRAM_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"⚠️ Bot error: {type(context.error).__name__}: {context.error}"
            )
        except TelegramError:
            pass  # don't let a failed error-notification cause its own crash


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env file")

    db.init_db()

    # Health-check server runs in its own thread so it doesn't block the bot's event loop
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    alert_conv = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            PLATFORM: [CallbackQueryHandler(alert_platform, pattern=r"^platform:")],
            CATEGORY: [CallbackQueryHandler(alert_category, pattern=r"^category:")],
            KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_keyword)],
            MAXPRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_maxprice)],
            CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_condition)],
            DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_direction)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("refer", refer))
    app.add_handler(CommandHandler("myalerts", myalerts))
    app.add_handler(CommandHandler("stop", stop_alert))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("addpaid", addpaid))
    app.add_handler(CommandHandler("paidcommission", paidcommission))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(alert_conv)
    app.add_error_handler(error_handler)

    # Background scheduler — checks all alerts automatically every
    # CHECK_INTERVAL_MINUTES. Started inside post_init so it only runs once
    # Telegram's own event loop is already live (starting it earlier caused
    # a crash — see README). Combined with the health-check server above,
    # this now runs reliably even on Render's free tier as long as an
    # uptime pinger hits the service periodically.
    async def on_startup(application: Application):
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            lambda: application.create_task(run_checks(application.bot)),
            "interval",
            minutes=CHECK_INTERVAL_MINUTES,
        )
        scheduler.start()
        logger.info(f"Scheduler started — checking every {CHECK_INTERVAL_MINUTES} minute(s).")

    app.post_init = on_startup

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
