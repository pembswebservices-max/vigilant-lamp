# MarketAlert Bot — v1

Telegram bot for marketplace + crypto price alerts.

**Status right now:**
- ✅ Crypto price alerts — real, working (CoinGecko free API, no key needed)
- 🟡 eBay alerts — using mock/fake data until your eBay developer account is approved (usually up to 24hrs). Swap one flag once it's ready.
- ⬜ Vinted — not built yet
- ⬜ Facebook Marketplace — not built yet (hardest, no official API)

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`:
   ```
   cp .env.example .env
   ```

3. Fill in `TELEGRAM_BOT_TOKEN` in `.env` (get this from @BotFather on Telegram).

4. Run the bot:
   ```
   python bot.py
   ```

5. Open your bot in Telegram, send `/start`.

## Testing the alert loop right now (before eBay API is ready)

1. `/alert` → choose `ebay` → type any keyword → set a max price → set condition
2. `/checknow` → this manually triggers a check. Since eBay is in mock mode, it'll always return one fake match so you can see the full flow works — message formatting, dedupe logic, everything.
3. `/alert` → choose `crypto` → type `BTC` → set a target price (try something close to the real current price so it triggers) → choose `above` or `below`
4. `/checknow` → this one is REAL — it hits CoinGecko live. If BTC's actual price matches your condition, you'll get a real alert.

## Once your eBay API is approved

1. Get your App ID from developer.ebay.com
2. Add it to `.env` as `EBAY_APP_ID`
3. In `sources/ebay.py`, change `USE_MOCK_DATA = True` to `USE_MOCK_DATA = False`
4. Fill in the `_get_oauth_token()` function (eBay's client credentials flow — I can help write this when you're ready)

## Notes on hosting

This bot has a background scheduler (`APScheduler`) that checks alerts automatically every
`CHECK_INTERVAL_MINUTES`. This ONLY works on always-on hosting. On Render's free tier
(which sleeps after inactivity), the scheduler won't run reliably — use `/checknow` manually
while developing, and move to a paid always-on plan (or a background worker, not a web service)
before going live with paying customers.

## File structure

```
marketalert-bot/
├── bot.py              # main entry point, all Telegram commands
├── database.py         # SQLite: users, alerts, referrals, sent listings
├── sources/
│   ├── ebay.py          # eBay search (mock for now, real function ready)
│   └── crypto.py        # crypto price checks (real, CoinGecko)
├── requirements.txt
├── .env.example
└── bot.db               # created automatically on first run
```
