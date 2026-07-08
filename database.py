"""
database.py
Handles all SQLite storage: users, alerts, referrals, sent listings.
Using SQLite for now — fine for dev and even early production (single file, zero setup).

All timestamps are stored as timezone-aware UTC ISO strings. Using
datetime.now(timezone.utc) rather than the older datetime.utcnow() —
the latter is deprecated and produces "naive" datetimes that silently
fail to compare correctly against timezone-aware ones, which is exactly
the kind of bug that stays invisible until a subscription expiry check
quietly does the wrong thing.
"""

import os
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

DB_PATH = "bot.db"

# Allow overriding commission rate via env var without touching code,
# e.g. COMMISSION_RATE=0.25 in Render's Environment tab.
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.20"))

FREE_TIER_ALERT_LIMIT = int(os.getenv("FREE_TIER_ALERT_LIMIT", "2"))

CATEGORIES = ["Bikes", "Shoes", "Clothes", "Gold & Jewellery", "Cars", "Electronics", "Other"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                referral_code TEXT UNIQUE,
                referred_by TEXT,
                is_paid INTEGER DEFAULT 0,
                paid_until TEXT,
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                platform TEXT,          -- 'ebay', 'crypto', 'vinted', 'facebook'
                keyword TEXT,           -- search term OR coin symbol e.g. 'BTC'
                max_price REAL,         -- max price (marketplace) OR target price (crypto)
                condition TEXT,         -- 'new', 'used', 'any' — ignored for crypto
                category TEXT,          -- predefined category, optional
                direction TEXT,         -- for crypto: 'above' or 'below'. null for marketplace
                active INTEGER DEFAULT 1,
                created_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS sent_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER,
                listing_id TEXT,
                sent_at TEXT,
                UNIQUE(alert_id, listing_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                commission_owed REAL DEFAULT 0,
                paid INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)

        # Helpful indexes — matters once you have more than a handful of users/alerts,
        # since run_checks() and dedupe lookups run on every scheduled check.
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sent_alert ON sent_listings(alert_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id)")

        conn.commit()


# ---------- USERS ----------

def generate_referral_code() -> str:
    return secrets.token_hex(4)  # e.g. 'a1b2c3d4'


def get_or_create_user(telegram_id: int, username: str, referred_by_code: str = None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if user:
            return dict(user)

        # Guard against someone using their own code, or a code that doesn't exist
        referrer_row = None
        if referred_by_code:
            c.execute("SELECT telegram_id FROM users WHERE referral_code = ?", (referred_by_code,))
            referrer_row = c.fetchone()
            if referrer_row and referrer_row["telegram_id"] == telegram_id:
                referrer_row = None  # can't refer yourself
            if not referrer_row:
                referred_by_code = None  # invalid code, don't store garbage

        ref_code = generate_referral_code()
        c.execute("""
            INSERT INTO users (telegram_id, username, referral_code, referred_by, is_paid, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (telegram_id, username, ref_code, referred_by_code, now_iso()))

        if referrer_row:
            c.execute("""
                INSERT INTO referrals (referrer_id, referred_id, created_at)
                VALUES (?, ?, ?)
            """, (referrer_row["telegram_id"], telegram_id, now_iso()))

        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return dict(c.fetchone())


def get_user(telegram_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = c.fetchone()
        return dict(row) if row else None


def set_paid_status(telegram_id: int, is_paid: bool, paid_until: str = None):
    """paid_until = None means lifetime access. A date string means subscription expiring then."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_paid = ?, paid_until = ? WHERE telegram_id = ?",
                   (1 if is_paid else 0, paid_until, telegram_id))
        return c.rowcount > 0


def is_currently_paid(user: dict) -> bool:
    """True if user has lifetime access OR an active (non-expired) subscription."""
    if not user or not user["is_paid"]:
        return False
    if user["paid_until"] is None:
        return True  # lifetime
    return parse_iso(user["paid_until"]) > datetime.now(timezone.utc)


def mark_user_paid(telegram_id: int, plan: str, amount_paid: float):
    """
    plan: 'lifetime' or 'monthly'
    amount_paid: what they actually paid, in GBP — used to calculate referral commission
    Returns (referrer_id, commission_amount) if a commission was credited, else (None, None).
    Returns None entirely if telegram_id doesn't correspond to a known user.
    """
    user = get_user(telegram_id)
    if not user:
        return None

    paid_until = None
    if plan == "monthly":
        paid_until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    set_paid_status(telegram_id, True, paid_until)

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT referrer_id, id FROM referrals WHERE referred_id = ?", (telegram_id,))
        ref = c.fetchone()
        if not ref:
            return (None, None)

        commission = round(amount_paid * COMMISSION_RATE, 2)
        c.execute("UPDATE referrals SET commission_owed = commission_owed + ? WHERE id = ?",
                   (commission, ref["id"]))
        return (ref["referrer_id"], commission)


# ---------- REFERRALS ----------

def get_referral_summary(referrer_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT r.*, u.username as referred_username
            FROM referrals r
            LEFT JOIN users u ON u.telegram_id = r.referred_id
            WHERE r.referrer_id = ?
            ORDER BY r.created_at DESC
        """, (referrer_id,))
        rows = [dict(row) for row in c.fetchall()]
        total_owed = sum(r["commission_owed"] for r in rows if not r["paid"])
        total_earned = sum(r["commission_owed"] for r in rows)
        return {"referrals": rows, "total_owed": round(total_owed, 2), "total_earned": round(total_earned, 2)}


def mark_commission_paid(referral_id: int) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE referrals SET paid = 1 WHERE id = ?", (referral_id,))
        return c.rowcount > 0


# ---------- ALERTS ----------

def count_active_alerts(user_id: int) -> int:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM alerts WHERE user_id = ? AND active = 1", (user_id,))
        return c.fetchone()["cnt"]


def create_alert(user_id: int, platform: str, keyword: str, max_price: float = None,
                  condition: str = "any", category: str = None, direction: str = None) -> int:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO alerts (user_id, platform, keyword, max_price, condition, category, direction, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (user_id, platform, keyword.strip()[:200], max_price, condition, category, direction, now_iso()))
        return c.lastrowid


def get_active_alerts(user_id: int = None):
    with get_db() as conn:
        c = conn.cursor()
        if user_id:
            c.execute("SELECT * FROM alerts WHERE user_id = ? AND active = 1", (user_id,))
        else:
            c.execute("SELECT * FROM alerts WHERE active = 1")
        return [dict(row) for row in c.fetchall()]


def deactivate_alert(alert_id: int, user_id: int) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE alerts SET active = 0 WHERE id = ? AND user_id = ?", (alert_id, user_id))
        return c.rowcount > 0


# ---------- SENT LISTINGS (dedupe) ----------

def has_been_sent(alert_id: int, listing_id: str) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM sent_listings WHERE alert_id = ? AND listing_id = ?", (alert_id, listing_id))
        return c.fetchone() is not None


def mark_as_sent(alert_id: int, listing_id: str):
    with get_db() as conn:
        c = conn.cursor()
        # INSERT OR IGNORE: if two scheduler runs somehow overlap, the UNIQUE
        # constraint means we silently skip rather than crash on a duplicate.
        c.execute("INSERT OR IGNORE INTO sent_listings (alert_id, listing_id, sent_at) VALUES (?, ?, ?)",
                   (alert_id, listing_id, now_iso()))


# ---------- ADMIN STATS ----------

def get_stats() -> dict:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM users")
        total_users = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM users WHERE is_paid = 1")
        paid_users_flagged = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM alerts WHERE active = 1")
        active_alerts = c.fetchone()["cnt"]

        c.execute("SELECT platform, COUNT(*) as cnt FROM alerts WHERE active = 1 GROUP BY platform")
        by_platform = {row["platform"]: row["cnt"] for row in c.fetchall()}

        c.execute("SELECT COALESCE(SUM(commission_owed), 0) as total FROM referrals WHERE paid = 0")
        unpaid_commission = c.fetchone()["total"]

        c.execute("SELECT COUNT(*) as cnt FROM referrals")
        total_referrals = c.fetchone()["cnt"]

        return {
            "total_users": total_users,
            "paid_users_flagged": paid_users_flagged,  # includes expired subscriptions still flagged is_paid=1
            "active_alerts": active_alerts,
            "alerts_by_platform": by_platform,
            "unpaid_commission_total": round(unpaid_commission, 2),
            "total_referrals": total_referrals,
        }
