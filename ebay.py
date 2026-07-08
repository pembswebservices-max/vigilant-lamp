"""
sources/ebay.py

Handles eBay listing search.

Right now this returns MOCK data so you can test the full bot loop
before your eBay developer account is approved.

Once approved:
1. Get your App ID (Client ID) + Cert ID (Client Secret) from developer.ebay.com
2. Put App ID in .env as EBAY_APP_ID, Cert ID as EBAY_CERT_ID
3. Set USE_MOCK_DATA = False below
That's it — the rest of the bot doesn't need to change. I'll help fill in
the OAuth token exchange in _get_oauth_token() when you're ready — it needs
your real credentials to test against, which is why it's stubbed for now.
"""

import os
import time
import random
import requests

USE_MOCK_DATA = True  # flip this to False once your eBay API key is active

EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID")
EBAY_BROWSE_API_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"

_cached_token = None
_cached_token_expiry = 0


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
    if not EBAY_APP_ID or not EBAY_CERT_ID:
        raise RuntimeError("EBAY_APP_ID / EBAY_CERT_ID not set in .env")

    headers = {
        "Authorization": f"Bearer {_get_oauth_token()}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
    }

    # eBay's Browse API doesn't take a free-text "category" param directly —
    # folding it into the search query is the simplest reliable approach
    # (proper category-ID filtering is possible later via the Taxonomy API
    # if search relevance becomes an issue).
    query = f"{keyword} {category}" if category and category.lower() != "other" else keyword
    params = {"q": query, "limit": 10}

    filters = []
    if max_price:
        filters.append(f"price:[..{max_price}],priceCurrency:GBP")
    if condition and condition.lower() in ("new", "used"):
        filters.append(f"conditionIds:{{{'1000' if condition.lower() == 'new' else '3000'}}}")
    if filters:
        params["filter"] = ",".join(filters)

    resp = requests.get(EBAY_BROWSE_API_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("itemSummaries", []):
        try:
            results.append({
                "id": item["itemId"],
                "title": item["title"],
                "price": float(item["price"]["value"]),
                "url": item["itemWebUrl"],
                "condition": item.get("condition", "unknown"),
            })
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed entries rather than crashing the whole check
    return results


def _get_oauth_token() -> str:
    """
    eBay's Browse API needs an OAuth token (client credentials flow).
    Cached in-memory and refreshed automatically before it expires, so we're
    not making a fresh auth call on every single search.
    """
    global _cached_token, _cached_token_expiry

    if _cached_token and time.time() < _cached_token_expiry:
        return _cached_token

    import base64
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    resp = requests.post(EBAY_OAUTH_URL, headers=headers, data=data, timeout=10)
    resp.raise_for_status()
    token_data = resp.json()

    _cached_token = token_data["access_token"]
    _cached_token_expiry = time.time() + token_data.get("expires_in", 7200) - 60  # refresh 60s early
    return _cached_token
