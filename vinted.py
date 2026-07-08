"""
sources/vinted.py

Vinted has NO official public API, so this works differently to eBay:
- There's an unofficial internal API their own website/app uses
  (e.g. https://www.vinted.co.uk/api/v2/catalog/items) that people commonly
  reverse-engineer for tools like this — it can change or get rate-limited
  without warning, so treat it as less reliable than eBay's official API.
- Using it means sending normal browser-like requests (session cookies,
  realistic headers) — no official credentials to request.

USE_MOCK_DATA = True for now so the bot loop is fully testable.
Flip to False once you're ready to test the real endpoint — expect to need
to adjust headers/params through trial and error since it's unofficial.
"""

import random
import requests

USE_MOCK_DATA = True

VINTED_SEARCH_URL = "https://www.vinted.co.uk/api/v2/catalog/items"


def search_vinted(keyword: str, max_price: float = None, condition: str = "any", category: str = None):
    if USE_MOCK_DATA:
        return _mock_search(keyword, max_price)
    return _real_search(keyword, max_price, condition, category)


def _mock_search(keyword: str, max_price: float = None):
    fake_price = round(random.uniform(5, (max_price or 80)), 2)
    listing_id = f"mock-vinted-{keyword.replace(' ', '-')}-{random.randint(1000, 9999)}"
    return [{
        "id": listing_id,
        "title": f"{keyword.title()} (mock Vinted listing)",
        "price": fake_price,
        "url": "https://www.vinted.co.uk/",
        "condition": "used"
    }]


def _real_search(keyword: str, max_price: float, condition: str, category: str):
    """
    Unofficial endpoint — no auth token, but Vinted may block requests without
    realistic browser headers, and may rate-limit or change response shape
    without notice. Treat this as a starting point to debug against, not a
    finished integration.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    params = {
        "search_text": keyword,
        "per_page": 10,
        "order": "newest_first",
    }
    if max_price:
        params["price_to"] = max_price

    resp = requests.get(VINTED_SEARCH_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("items", []):
        results.append({
            "id": str(item.get("id")),
            "title": item.get("title", "Untitled"),
            "price": float(item.get("price", {}).get("amount", 0)),
            "url": item.get("url", "https://www.vinted.co.uk/"),
            "condition": item.get("status", "unknown"),
        })
    return results
