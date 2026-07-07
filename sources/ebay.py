"""
sources/ebay.py

Handles eBay listing search.

Right now this returns MOCK data so you can test the full bot loop
before your eBay developer account is approved.

Once approved:
1. Get your App ID (Client ID) from developer.ebay.com
2. Put it in .env as EBAY_APP_ID
3. Set USE_MOCK_DATA = False below
That's it — the rest of the bot doesn't need to change.
"""

import os
import random
import requests

USE_MOCK_DATA = True  # flip this to False once your eBay API key is active

EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_BROWSE_API_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def search_ebay(keyword: str, max_price: float = None, condition: str = "any", category: str = None):
    """
    Returns a list of dicts: [{id, title, price, url, condition}, ...]
    """
    if USE_MOCK_DATA:
        return _mock_search(keyword, max_price)

    return _real_search(keyword, max_price, condition, category)


def _mock_search(keyword: str, max_price: float = None):
    """Fake listings so you can test the bot loop end-to-end right now."""
    fake_price = round(random.uniform(10, (max_price or 100)), 2)
    listing_id = f"mock-{keyword.replace(' ', '-')}-{random.randint(1000, 9999)}"
    return [{
        "id": listing_id,
        "title": f"{keyword.title()} (mock listing)",
        "price": fake_price,
        "url": "https://www.ebay.co.uk/",
        "condition": "used"
    }]


def _real_search(keyword: str, max_price: float, condition: str, category: str):
    """Real eBay Browse API call — used once USE_MOCK_DATA = False."""
    if not EBAY_APP_ID:
        raise RuntimeError("EBAY_APP_ID not set in .env")

    headers = {
        "Authorization": f"Bearer {_get_oauth_token()}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
    }
    params = {"q": keyword, "limit": 10}
    if max_price:
        params["filter"] = f"price:[..{max_price}],priceCurrency:GBP"

    resp = requests.get(EBAY_BROWSE_API_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("itemSummaries", []):
        results.append({
            "id": item["itemId"],
            "title": item["title"],
            "price": float(item["price"]["value"]),
            "url": item["itemWebUrl"],
            "condition": item.get("condition", "unknown"),
        })
    return results


def _get_oauth_token():
    """
    eBay's Browse API needs an OAuth token (client credentials flow).
    Fill this in once you have your App ID + Cert ID from eBay dev account.
    """
    raise NotImplementedError("Add OAuth token exchange here once eBay API is approved")
