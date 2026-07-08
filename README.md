# MarketAlert Bot — v2

Telegram bot for marketplace + crypto price alerts, with a manual payment system
and automatic referral commissions.

**Status:**
- ✅ Crypto price alerts — real, working (CoinGecko free API, no key needed)
- ✅ Payment system — manual (you confirm payment, bot activates access + notifies everyone)
- ✅ Referral commissions — automatic, tracked, payout marking
- ✅ Category filtering — predefined buttons (Bikes, Shoes, Clothes, Gold, Cars, Electronics, Other)
- ✅ Admin stats dashboard
- ✅ Health-check server (keeps free-tier Render hosting awake via uptime pinger)
- 🟡 eBay alerts — mock data until your developer account is approved. Swap one flag once it's ready.
- 🟡 Vinted alerts — mock data (Vinted has no official API — a real unofficial endpoint is stubbed in, expect to need to debug it against Vinted's actual response format when you get there)
- ⬜ Facebook Marketplace — not built yet (hardest, no official API, most likely to need ongoing maintenance)

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`:
   ```
   cp .env.example .env
   ```

3. Fill in `.env`:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `ADMIN_TELEGRAM_ID` — your own numeric Telegram ID (message @userinfobot to get it). This unlocks `/addpaid`, `/paidcommission`, and `/stats` for you only.
   - Everything else has sensible defaults, safe to leave blank for now.

4. Run the bot:
   ```
   python bot.py
   ```

5. Open your bot in Telegram, send `/start`.

## All commands

**Everyone:**
- `/start` — register (or register via a referral link)
- `/help` — show command list
- `/alert` — guided flow: pick platform (eBay/Vinted/Crypto) → category → keyword → price → condition
- `/myalerts` — list your active alerts
- `/stop <id>` — deactivate an alert
- `/cancel` — bail out of whatever flow you're mid-way through
- `/refer` — get your referral link + see commission earned/owed
- `/upgrade` — see pricing
- `/checknow` — manually trigger a check right now (real for crypto, mock for eBay/Vinted until APIs are wired up)

**You only (admin, gated by `ADMIN_TELEGRAM_ID`):**
- `/addpaid <telegram_id> <lifetime|monthly> <amount_gbp>` — mark someone as paid after you've taken payment manually. Auto-DMs them, auto-credits + DMs their referrer if they have one.
- `/paidcommission <referral_id>` — mark a referral commission as actually paid out (after you've sent the money)
- `/stats` — total users, paid users, active alerts per platform, unpaid commission total

## Testing the alert loop right now (before eBay/Vinted APIs are ready)

1. `/alert` → tap `eBay` → tap a category → type a keyword → set a max price → set condition
2. `/checknow` → mock mode always returns one fake match, so you can confirm the full pipeline works (creation → check → message)
3. `/alert` → tap `Crypto price alert` → type `BTC` → set a target price close to the real current price → choose `above` or `below`
4. `/checknow` → this one is REAL — hits CoinGecko live

## Testing payments + referral commissions on yourself

1. Get your own Telegram ID from @userinfobot, confirm it matches `ADMIN_TELEGRAM_ID` in your `.env`
2. Get a second Telegram account (or ask a friend) to `/start` your bot using your referral link from `/refer`
3. As admin, run `/addpaid <their_telegram_id> lifetime 100`
4. You should see: a confirmation to you, a DM to them, a commission credit + DM to you as the referrer
5. Run `/refer` again — you should see the commission reflected
6. Run `/stats` — should show 2 users, 1 paid, 1 referral, £20 unpaid commission

## Once your eBay API is approved

1. Get App ID (Client ID) **and** Cert ID (Client Secret) from developer.ebay.com
2. Add both to `.env` as `EBAY_APP_ID` and `EBAY_CERT_ID`
3. In `sources/ebay.py`, change `USE_MOCK_DATA = True` to `USE_MOCK_DATA = False`
4. The OAuth token exchange, category-in-query search, condition filtering, and price filtering are all already wired up and tested — should work as-is, but real-world eBay responses can surprise you, so test with `/checknow` immediately after switching and watch the logs

## Keeping the bot alive on Render's free tier

The bot now runs a tiny built-in health-check web server (on the `PORT` Render provides automatically) that responds `200 OK` to any request. This means:

1. Render's free tier still sleeps after ~15 min with no HTTP traffic — but now you can point a **free uptime pinger** at your Render URL to keep it awake automatically, instead of manually running `/checknow`:
   - [UptimeRobot](https://uptimerobot.com) (free tier: ping every 5 min)
   - [cron-job.org](https://cron-job.org) (free, configurable interval)
2. Set the pinger to hit your Render service's public URL every 5-10 minutes
3. With that running, the background scheduler (`CHECK_INTERVAL_MINUTES`) will now actually fire automatically, and users will get real unprompted alerts — no manual `/checknow` needed
4. This is still not as reliable as a paid always-on plan (there can be a short gap right after a cold start), but it's a solid free-tier stopgap while you're pre-revenue

Once you have paying users, move to Render's paid tier (~$7/month) or a background worker service type — the health server is harmless either way, just unused on a plan that never sleeps.

## File structure

```
marketalert-bot/
├── bot.py               # main entry point, all Telegram commands, health server, scheduler
├── database.py          # SQLite: users, alerts, referrals, sent listings, stats
├── sources/
│   ├── ebay.py            # eBay search (mock for now, real OAuth + search ready)
│   ├── vinted.py           # Vinted search (mock for now, real endpoint stubbed — unofficial API)
│   └── crypto.py           # crypto price checks (real, CoinGecko)
├── requirements.txt
├── .env.example
├── .python-version      # pins Python to 3.11.9 — newer versions (3.13+) break the Telegram library
└── bot.db               # created automatically on first run
```

## Known limitations to be aware of

- **SQLite** is fine for early-stage use but is a single file with no concurrent-write scaling — if you get to hundreds of simultaneous users, plan to migrate to PostgreSQL eventually (Render offers this).
- **Vinted's real endpoint is unofficial** — it can break or get rate-limited without warning. Don't advertise Vinted alerts publicly until you've stress-tested it.
- **Payments are manual** — there's no fraud protection beyond you personally confirming money arrived before running `/addpaid`. Fine at low volume, worth automating (Stripe) once you're doing this at scale.
- **Referral commission is a running balance, not auto-paid out** — you still need to actually send referrers their money and mark it with `/paidcommission`.
