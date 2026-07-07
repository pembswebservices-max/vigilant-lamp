"""
database.py
Handles all SQLite storage: users, alerts, referrals, sent listings.
Using SQLite for now — fine for dev and even early production (single file, zero setup).
"""

import sqlite3
import secrets
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "bot.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
                sent_at TEXT
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

        conn.commit()


# ---------- USERS ----------

def generate_referral_code():
    return secrets.token_hex(4)  # e.g. 'a1b2c3d4'


def get_or_create_user(telegram_id: int, username: str, referred_by_code: str = None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if user:
            return dict(user)

        ref_code = generate_referral_code()
        c.execute("""
            INSERT INTO users (telegram_id, username, referral_code, referred_by, is_paid, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (telegram_id, username, ref_code, referred_by_code, datetime.utcnow().isoformat()))

        # if referred by someone valid, log it
        if referred_by_code:
            c.execute("SELECT telegram_id FROM users WHERE referral_code = ?", (referred_by_code,))
            referrer = c.fetchone()
            if referrer:
                c.execute("""
                    INSERT INTO referrals (referrer_id, referred_id, created_at)
                    VALUES (?, ?, ?)
                """, (referrer["telegram_id"], telegram_id, datetime.utcnow().isoformat()))

        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return dict(c.fetchone())


def set_paid_status(telegram_id: int, is_paid: bool, paid_until: str = None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_paid = ?, paid_until = ? WHERE telegram_id = ?",
                   (1 if is_paid else 0, paid_until, telegram_id))


def get_user(telegram_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = c.fetchone()
        return dict(row) if row else None


# ---------- ALERTS ----------

FREE_TIER_ALERT_LIMIT = 2


def count_active_alerts(user_id: int) -> int:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM alerts WHERE user_id = ? AND active = 1", (user_id,))
        return c.fetchone()["cnt"]


def create_alert(user_id: int, platform: str, keyword: str, max_price: float = None,
                  condition: str = "any", category: str = None, direction: str = None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO alerts (user_id, platform, keyword, max_price, condition, category, direction, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (user_id, platform, keyword, max_price, condition, category, direction, datetime.utcnow().isoformat()))
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
        c.execute("INSERT INTO sent_listings (alert_id, listing_id, sent_at) VALUES (?, ?, ?)",
                   (alert_id, listing_id, datetime.utcnow().isoformat()))
