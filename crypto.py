"""
sources/crypto.py

Crypto PRICE alerts only (not trading signals) — e.g. "notify me when BTC
crosses £50,000". This uses CoinGecko's free public API, no API key needed.

This one is real from day one, no mock data required.
"""

import requests

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# Map common tickers to CoinGecko IDs. Extend as needed.
SYMBOL_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "USDT": "tether",
}


def get_price_gbp(symbol: str) -> float:
    coin_id = SYMBOL_MAP.get(symbol.upper())
    if not coin_id:
        raise ValueError(f"Unknown symbol '{symbol}'. Supported: {', '.join(SYMBOL_MAP)}")

    resp = requests.get(COINGECKO_URL, params={"ids": coin_id, "vs_currencies": "gbp"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data[coin_id]["gbp"]


def check_crypto_alert(keyword: str, target_price: float, direction: str) -> dict | None:
    """
    keyword: coin symbol e.g. 'BTC'
    target_price: the threshold
    direction: 'above' or 'below'
    Returns a match dict if triggered, else None.
    """
    current_price = get_price_gbp(keyword)

    triggered = (
        (direction == "above" and current_price >= target_price)
        or (direction == "below" and current_price <= target_price)
    )

    if not triggered:
        return None

    return {
        "id": f"{keyword.upper()}-{direction}-{target_price}",  # dedupe key
        "title": f"{keyword.upper()} is now £{current_price:,.2f}",
        "price": current_price,
        "url": f"https://www.coingecko.com/en/coins/{SYMBOL_MAP[keyword.upper()]}",
    }
