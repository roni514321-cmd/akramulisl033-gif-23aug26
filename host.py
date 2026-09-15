"""
=====================================================================
 Professional Telegram Digital Marketplace Bot
 Single-file, fully runnable implementation.

 Stack: Python 3.11+, aiogram 3.x, SQLAlchemy (async) + SQLite,
        openpyxl, python-dotenv

 Run:
    pip install -r requirements.txt
    (create a .env file next to this script — see bottom of file)
    python bot.py

 Works on Windows / Termux / Railway (long-polling, no webhook needed).
=====================================================================
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# --------------------------------------------------------------------
# aiohttp (SMS webhook server — receives forwarded payment SMS)
# --------------------------------------------------------------------
from aiohttp import web

# --------------------------------------------------------------------
# aiogram
# --------------------------------------------------------------------
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Document,
    BufferedInputFile,
    ErrorEvent,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram import BaseMiddleware

# --------------------------------------------------------------------
# SQLAlchemy (async)
# --------------------------------------------------------------------
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Text, DateTime,
    Boolean, ForeignKey, select, func, delete, update as sa_update
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# --------------------------------------------------------------------
# openpyxl
# --------------------------------------------------------------------
from openpyxl import load_workbook, Workbook

# =====================================================================
# CONFIG
# =====================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: set[int] = set()
for _part in _ADMIN_IDS_RAW.replace(" ", "").split(","):
    if _part.isdigit():
        ADMIN_IDS.add(int(_part))

def _normalize_database_url(url: str) -> str:
    """Railway (বা অন্য হোস্টিং) থেকে সরাসরি কপি করা DB URL সাধারণত
    'mysql://user:pass@host:port/db' বা 'postgres://...' ফরম্যাটে থাকে —
    এগুলো sync ড্রাইভারের জন্য বানানো। কিন্তু এই বট async SQLAlchemy engine
    ব্যবহার করে, যার জন্য async ড্রাইভার (mysql+asyncmy / postgresql+asyncpg)
    উল্লেখ করা লাগে — নাহলে এই এরর আসবে:
    "The loaded 'pymysql' driver is not async" ইত্যাদি।
    তাই এখানে স্বয়ংক্রিয়ভাবে সঠিক ড্রাইভার বসিয়ে দেওয়া হচ্ছে, যাতে হোস্টিং
    থেকে কপি করা URL হুবহু .env-এ বসালেই কাজ করে — আলাদা করে '+asyncmy'
    যোগ করার দরকার নেই।"""
    u = url.strip()
    if u.startswith("mysql://"):
        return "mysql+asyncmy://" + u[len("mysql://"):]
    if u.startswith("mysql+pymysql://"):
        return "mysql+asyncmy://" + u[len("mysql+pymysql://"):]
    if u.startswith("postgres://"):
        return "postgresql+asyncpg://" + u[len("postgres://"):]
    if u.startswith("postgresql://") and "+asyncpg" not in u and "+psycopg" not in u:
        return "postgresql+asyncpg://" + u[len("postgresql://"):]
    return u


DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db"))

# ✅ MySQL/Postgres ব্যবহার করলে async ড্রাইভার প্যাকেজ (asyncmy/asyncpg)
# ইনস্টল করা না থাকলে confusing traceback-এর বদলে এখানেই স্পষ্ট বার্তা দিয়ে
# বন্ধ করে দেওয়া হচ্ছে।
if DATABASE_URL.startswith("mysql+asyncmy://"):
    try:
        import asyncmy  # noqa: F401
    except ImportError:
        print(
            "❌ MySQL ব্যবহারের জন্য 'asyncmy' প্যাকেজ ইনস্টল করা নেই।\n"
            "   রান করুন: pip install asyncmy\n"
            "   (Railway/Requirements.txt এ 'asyncmy' লাইন যোগ করে রিডিপ্লয় করুন)"
        )
        sys.exit(1)
elif DATABASE_URL.startswith("postgresql+asyncpg://"):
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        print(
            "❌ PostgreSQL ব্যবহারের জন্য 'asyncpg' প্যাকেজ ইনস্টল করা নেই।\n"
            "   রান করুন: pip install asyncpg\n"
            "   (Railway/Requirements.txt এ 'asyncpg' লাইন যোগ করে রিডিপ্লয় করুন)"
        )
        sys.exit(1)

if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing! Create a .env file with BOT_TOKEN=<your token>.")
    sys.exit(1)

if not ADMIN_IDS:
    print("⚠️  Warning: ADMIN_IDS is empty in .env — nobody will be able to use /admin.")

MAX_XLSX_SIZE_MB = 8
MAX_BACKUP_SIZE_MB = 25  # ফুল DB ব্যাকআপে Orders/Transactions/SMS log ইত্যাদি সহ থাকায় সাইজ বড় হতে পারে
PAGE_SIZE = 8

# উইথড্র-এর জন্য সব সম্ভাব্য পেমেন্ট মেথডের মাস্টার লিস্ট — এডমিন Settings থেকে
# এখান থেকে যেকোনো একটাকে on/off করতে পারবে (withdraw_methods সেটিং-এ শুধু
# "on" থাকা মেথডগুলোর নাম কমা দিয়ে সেভ থাকে)
ALL_WITHDRAW_METHODS = ["bKash", "Nagad", "Rocket", "Binance"]
RATE_LIMIT_WINDOW = 1.0       # seconds
RATE_LIMIT_MAX_HITS = 5       # max messages/callbacks per window

# 1 USD = কতো BDT — এখানে পরিবর্তন করলে পুরো বট জুড়ে $ কনভার্শন আপডেট হয়ে যাবে
USD_RATE = float(os.getenv("USD_RATE", "121").strip() or "121")

# =====================================================================
# SMS AUTO-DEPOSIT WEBHOOK CONFIG
# =====================================================================
# ফোনে ইনস্টল করা SMS Forwarder App (যেমন: "SMS Forwarder", "Sms2Telegram" ইত্যাদি)
# থেকে bKash/Nagad/Rocket এর পেমেন্ট SMS এখানে পাঠানো হবে। এই এন্ডপয়েন্টে যেকেউ
# রিকোয়েস্ট পাঠিয়ে ভুয়া ব্যালেন্স যোগ করার চেষ্টা করতে পারে, তাই একটা গোপন
# TOKEN বাধ্যতামূলক — Railway এর Variables ট্যাবে SMS_WEBHOOK_TOKEN সেট করুন
# এবং সেটা কাউকে শেয়ার করবেন না।
#
# Railway এ বসালে PORT ভ্যারিয়েবলটা Railway নিজে থেকেই দিয়ে দেয় — সেটাই আগে
# ব্যবহার হবে (SMS_WEBHOOK_PORT শুধু লোকাল/Termux এ রান করার জন্য fallback)।
#
# RAILWAY_PUBLIC_URL এ নিজের Railway অ্যাপের পাবলিক URL বসিয়ে দিন, যেমন:
#   RAILWAY_PUBLIC_URL=https://your-app-name.up.railway.app
# (Railway → Settings → Networking → Generate Domain থেকে এই URL পাবেন)
# এটা দিয়ে বট নিজে থেকেই সম্পূর্ণ webhook URL কনসোলে দেখিয়ে দেবে।
SMS_WEBHOOK_TOKEN = os.getenv("SMS_WEBHOOK_TOKEN", "").strip()
SMS_WEBHOOK_PORT = int(
    (os.getenv("PORT") or os.getenv("SMS_WEBHOOK_PORT") or "8085").strip() or "8085"
)
SMS_WEBHOOK_PATH = os.getenv("SMS_WEBHOOK_PATH", "/sms-webhook").strip() or "/sms-webhook"

# Railway তে "Generate Domain" করলে RAILWAY_PUBLIC_DOMAIN নামে ভ্যারিয়েবল
# অটোমেটিক পাওয়া যায় (শুধু হোস্টনেম, স্কিমা ছাড়া) — RAILWAY_PUBLIC_URL ম্যানুয়ালি
# না দেওয়া থাকলে সেটা থেকেই বেইজ URL বানানো হবে।
RAILWAY_PUBLIC_URL = os.getenv("RAILWAY_PUBLIC_URL", "").strip().rstrip("/")
_RAILWAY_AUTO_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
if not RAILWAY_PUBLIC_URL and _RAILWAY_AUTO_DOMAIN:
    RAILWAY_PUBLIC_URL = "https://" + _RAILWAY_AUTO_DOMAIN.replace("https://", "").replace("http://", "")

if not SMS_WEBHOOK_TOKEN:
    SMS_WEBHOOK_TOKEN = uuid.uuid4().hex
    print(
        "⚠️  SMS_WEBHOOK_TOKEN সেট করা ছিল না — এখন একটা র‍্যান্ডম টোকেন "
        f"তৈরি করা হয়েছে (প্রতিবার বট রিস্টার্টে বদলে যাবে): {SMS_WEBHOOK_TOKEN}\n"
        "   এটা স্থায়ী রাখতে Railway Variables এ যোগ করুন: SMS_WEBHOOK_TOKEN=" + SMS_WEBHOOK_TOKEN
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("marketbot")

# =====================================================================
# DATABASE MODELS
# =====================================================================
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)  # telegram user id
    username = Column(String(64), nullable=True)
    full_name = Column(String(128), nullable=True)
    balance = Column(Float, default=0.0, nullable=False)
    total_deposit = Column(Float, default=0.0, nullable=False)
    total_spent = Column(Float, default=0.0, nullable=False)
    total_income = Column(Float, default=0.0, nullable=False)
    total_orders = Column(Integer, default=0, nullable=False)
    status = Column(String(16), default="active", nullable=False)  # active / banned
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(64), nullable=False, default="General")
    name = Column(String(128), nullable=False)
    price = Column(Float, nullable=False, default=0.0)
    seller_price = Column(Float, nullable=False, default=0.0)  # paid to sellers / item
    description = Column(Text, nullable=True, default="")
    is_active = Column(Boolean, default=True, nullable=False)
    is_buy_listing = Column(Boolean, default=False, nullable=False)  # ✅ এডমিন যা ইউজারদের থেকে কিনতে চায় (শুধু Sell Mail এ দেখাবে, মার্কেটপ্লেসে না)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class InventoryItem(Base):
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    data = Column(Text, nullable=False)          # delivered content (e.g. "mail: x | pass: y")
    data_hash = Column(String(64), nullable=False, index=True)  # for duplicate detection
    status = Column(String(16), default="available", nullable=False)  # available/sold/removed
    order_id = Column(String(32), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sold_at = Column(DateTime, nullable=True)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(32), unique=True, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String(128), nullable=False)
    quantity = Column(Integer, nullable=False)
    total_price = Column(Float, nullable=False)
    delivered_data = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    type = Column(String(24), nullable=False)  # deposit/purchase/sell_credit/admin_add/admin_remove
    amount = Column(Float, nullable=False)     # +credit / -debit
    balance_after = Column(Float, nullable=False)
    note = Column(String(256), nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Deposit(Base):
    __tablename__ = "deposits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String(32), nullable=False)
    trx_id = Column(String(64), nullable=True, index=True)  # ইউজারের দেওয়া TrxID — SMS এর সাথে ম্যাচ করাতে ব্যবহার হয়
    note = Column(String(256), nullable=True, default="")
    status = Column(String(16), default="pending", nullable=False)  # pending/approved/rejected
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(BigInteger, nullable=True)  # 0 = auto-approved by SMS matching (system)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String(32), nullable=False)
    account_number = Column(String(64), nullable=True, default="")  # যেই নম্বরে এডমিন টাকা পাঠাবে
    note = Column(String(256), nullable=True, default="")
    status = Column(String(16), default="pending", nullable=False)  # pending/approved/rejected
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(BigInteger, nullable=True)


class SellerSubmission(Base):
    __tablename__ = "seller_submissions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_name = Column(String(128), nullable=True)
    raw_data = Column(Text, nullable=False)
    item_count = Column(Integer, nullable=False, default=0)
    credited_amount = Column(Float, nullable=True)
    status = Column(String(16), default="pending", nullable=False)  # pending/approved/rejected
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(BigInteger, nullable=True)
    file_id = Column(String(256), nullable=True)
    file_name = Column(String(256), nullable=True)


class SmsMessage(Base):
    """ফোনের SMS/Notification Forwarder অ্যাপ থেকে ওয়েবহুকের মাধ্যমে আসা
    bKash/Nagad/Rocket পেমেন্ট SMS এবং Binance পেমেন্ট নোটিফিকেশন এখানে সেইভ
    থাকে। ইউজারের জমা দেওয়া amount + trx_id (Binance-এ trx_id = username)
    এর সাথে ম্যাচ করে অটো-অ্যাপ্রুভের জন্য ব্যবহার হয়।"""
    __tablename__ = "sms_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    method = Column(String(32), nullable=True, default="")      # bKash/Nagad/Rocket (detected)
    amount = Column(Float, nullable=True)
    trx_id = Column(String(64), nullable=True, index=True)      # সবসময় uppercase করে সেইভ হয়
    raw_text = Column(Text, nullable=False)
    sender = Column(String(64), nullable=True, default="")
    is_used = Column(Boolean, default=False, nullable=False)
    used_by_deposit_id = Column(Integer, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True, default="")


class AdminLog(Base):
    __tablename__ = "admin_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(BigInteger, nullable=False)
    action = Column(String(64), nullable=False)
    details = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ✅ পুরনো (আগে থেকে বানানো) products টেবিলে নতুন কলাম না থাকলে যোগ করে দেয়
        # — create_all() নতুন টেবিল বানায় কিন্তু পুরনো টেবিলে কলাম অ্যাড করে না
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN is_buy_listing BOOLEAN NOT NULL DEFAULT FALSE"
            )
        except Exception:
            pass  # কলাম আগে থেকেই থাকলে এখানে এরর আসবে, সেটা ignore করা হচ্ছে
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE withdrawals ADD COLUMN account_number VARCHAR(64) DEFAULT ''"
            )
        except Exception:
            pass  # কলাম আগে থেকেই থাকলে এখানে এরর আসবে, সেটা ignore করা হচ্ছে
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE seller_submissions ADD COLUMN file_id VARCHAR(256)"
            )
        except Exception:
            pass
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE seller_submissions ADD COLUMN file_name VARCHAR(256)"
            )
        except Exception:
            pass
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN total_income FLOAT NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE deposits ADD COLUMN trx_id VARCHAR(64)"
            )
        except Exception:
            pass  # কলাম আগে থেকেই থাকলে এখানে এরর আসবে, সেটা ignore করা হচ্ছে
    # seed default settings
    async with SessionLocal() as s:
        defaults = {
            "maintenance_mode": "0",
            "low_stock_threshold": "5",
            "deposit_methods": "bKash,Nagad,Rocket,Binance",
            "withdraw_methods": "bKash,Nagad,Rocket,Binance",
            "default_seller_price": "0",
            "support_contact": "@support",
            "min_deposit_amount": "0",
            "min_withdraw_amount": "0",
            "binance_usdt_rate": "127",
        }
        for k, v in defaults.items():
            existing = await s.get(Setting, k)
            if existing is None:
                s.add(Setting(key=k, value=v))
        await s.commit()

    # migrate existing installs: drop "Bank" as a method, rename "Crypto" -> "Binance"
    async with SessionLocal() as s:
        for key in ("deposit_methods", "withdraw_methods"):
            row = await s.get(Setting, key)
            if row and row.value:
                parts = [p.strip() for p in row.value.split(",") if p.strip()]
                new_parts, changed = [], False
                for p in parts:
                    if p.lower() == "bank":
                        changed = True
                        continue
                    if p.lower() == "crypto":
                        new_parts.append("Binance")
                        changed = True
                    else:
                        new_parts.append(p)
                if changed:
                    row.value = ",".join(new_parts)
        # carry over any previously-set Crypto payment number to Binance
        crypto_num = await s.get(Setting, "payment_number_Crypto")
        binance_num = await s.get(Setting, "payment_number_Binance")
        if crypto_num and crypto_num.value and not (binance_num and binance_num.value):
            if binance_num:
                binance_num.value = crypto_num.value
            else:
                s.add(Setting(key="payment_number_Binance", value=crypto_num.value))
        await s.commit()


# =====================================================================
# SIMPLE RESOURCE LOCKS  (race-condition protection)
# =====================================================================
_locks: dict[str, asyncio.Lock] = {}


def get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


# =====================================================================
# SETTINGS HELPERS
# =====================================================================
async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(Setting, key)
    return row.value if row and row.value is not None else default


async def set_setting(session: AsyncSession, key: str, value: str):
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()


async def is_maintenance_on(session: AsyncSession) -> bool:
    return (await get_setting(session, "maintenance_mode", "0")) == "1"


# =====================================================================
# USER HELPERS
# =====================================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    user = await session.get(User, tg_user.id)
    if user is None:
        user = User(
            id=tg_user.id,
            username=tg_user.username or "",
            full_name=(tg_user.full_name or "")[:128],
            balance=0.0,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        changed = False
        if user.username != (tg_user.username or ""):
            user.username = tg_user.username or ""
            changed = True
        if user.full_name != (tg_user.full_name or ""):
            user.full_name = (tg_user.full_name or "")[:128]
            changed = True
        if changed:
            await session.commit()
    return user


async def add_balance(session: AsyncSession, user: User, amount: float, ttype: str, note: str = ""):
    """Atomically add (or subtract, if amount negative) balance + log a transaction."""
    async with get_lock(f"user:{user.id}"):
        fresh = await session.get(User, user.id)
        fresh.balance = round(fresh.balance + amount, 2)
        if amount > 0 and ttype == "deposit":
            fresh.total_deposit = round(fresh.total_deposit + amount, 2)
        if ttype == "purchase":
            fresh.total_spent = round(fresh.total_spent + abs(amount), 2)
        if amount > 0 and ttype == "sell_credit":
            fresh.total_income = round(fresh.total_income + amount, 2)
        session.add(Transaction(
            user_id=user.id, type=ttype, amount=amount,
            balance_after=fresh.balance, note=note[:250],
        ))
        await session.commit()
        return fresh.balance


def gen_order_id() -> str:
    return "ORD-" + uuid.uuid4().hex[:8].upper()


def money(v: float) -> str:
    v = round(float(v), 2)
    return f"{int(v)}" if v == int(v) else f"{v}"


def dual_money(v: float) -> str:
    """টাকা দুইটা কারেন্সিতে দেখায়: BDT (৳) ও তার সমমূল্যের USD ($)."""
    v = float(v)
    usd = round(v / USD_RATE, 2)
    return f"{money(v)}৳ (${usd:,.2f})"


def data_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


# =====================================================================
# SMS PARSING — bKash / Nagad / Rocket পেমেন্ট SMS থেকে amount + TrxID বের করা
# =====================================================================
_SMS_INCOMING_HINTS = (
    "you have received", "received tk", "received taka", "cash in",
    "money received", "credited", "পেমেন্ট", "রিসিভ",
)
_SMS_OUTGOING_HINTS = (
    "you have sent", "payment sent", "cash out", "withdrawn", "debited",
    "you sent", "send money",
)

_AMOUNT_RE = re.compile(r"(?:tk|taka|bdt)\.?\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_TRXID_RE = re.compile(
    r"(?:trx\s*id|txn\s*id|transaction\s*id|trxid|txnid|trx\s*no\.?|txn\s*no\.?|ref(?:erence)?\s*id)"
    r"[\s:\-]*([a-z0-9]{4,})",
    re.IGNORECASE,
)

# 🪙 Binance ওয়ালেট নোটিফিকেশন — এতে কোনো TrxID থাকে না, বরং যে ইউজারনেম থেকে
# টাকা এসেছে সেটাই থাকে। তাই এখানে trx_id ফিল্ডে (uppercase করে) সেই
# Binance username সেইভ করা হয় — matching logic এর বাকি সবকিছু অপরিবর্তিত থাকে।
# উদাহরণ: "You have received a payment of 0.1 USDT from olihasan2010 on 2026-09-15 03:19:31(UTC)"
_BINANCE_RECEIVED_RE = re.compile(
    r"received\s+(?:a\s+)?payment\s+of\s*([\d]+(?:\.\d+)?)\s*([a-z]{2,10})\s+from\s+([a-z0-9_.]{3,32})",
    re.IGNORECASE,
)


def _id_label(method: str) -> str:
    return "Binance Username" if method == "Binance" else "TrxID"


def _amount_unit(method: str) -> str:
    return "USDT" if method == "Binance" else "BDT"

# 📱 প্রতিটা মোবাইল ফাইন্যান্সিয়াল সার্ভিসের sender ফিল্ডে হয় নিউমেরিক
# শর্ট-কোড আসে (Rocket → "16216"), অথবা সরাসরি নাম আসে (Nagad → "NAGAD",
# bKash → "bKash")। ✅ ভেরিফায়েড থেকে যা কনফার্ম হয়েছে:
#   - Rocket sender  = "16216"  (numeric short code)
#   - Nagad  sender  = "NAGAD" (নামেই আসে, আর SMS বডিতেও "NAGAD" শব্দ থাকে
#     বলে টেক্সট-ম্যাচিং রুলেই এটা এমনিতে ধরা পড়ে যায়)
# bKash-এর sender এখনো ভেরিফাই করা হয়নি — placeholder নাম্বার/কোড অনুমান করে
# বসানো হচ্ছে না, ভুল কোড ম্যাচিং-এ ভুল করে দিতে পারে। bKash-এর একটা SMS
# Forwarder স্ক্রিনশট পেলে এখানে সঠিক কোড/নাম বসিয়ে দেওয়া যাবে।
_SENDER_METHOD_CODES = {
    "16216": "Rocket",
}
_SENDER_METHOD_NAMES = {
    "nagad": "Nagad",
}


def _method_from_sender(sender: str) -> str:
    if not sender:
        return ""
    sender_l = sender.strip().lower()
    for name, method in _SENDER_METHOD_NAMES.items():
        if name in sender_l:
            return method
    s = re.sub(r"[^0-9]", "", sender)  # +880, স্পেস, ড্যাশ ইত্যাদি বাদ দিয়ে শুধু ডিজিট রাখা
    for code, method in _SENDER_METHOD_CODES.items():
        if s == code or s.endswith(code):
            return method
    return ""


def parse_payment_sms(text: str, hint_method: str = "", sender: str = "") -> Optional[dict]:
    """একটা raw SMS টেক্সট থেকে method/amount/trx_id বের করার চেষ্টা করে।
    ইনকামিং পেমেন্ট SMS না মনে হলে, বা amount/trx_id না পেলে None রিটার্ন করে।"""
    if not text:
        return None
    t = text.strip()
    tl = t.lower()

    if any(h in tl for h in _SMS_OUTGOING_HINTS) and not any(h in tl for h in _SMS_INCOMING_HINTS):
        return None  # এটা টাকা পাঠানো/ক্যাশ-আউটের SMS, ডিপোজিটের জন্য না

    # 🪙 আগে Binance-স্টাইল ক্রিপ্টো পেমেন্ট নোটিফিকেশন চেক করা হচ্ছে — এই
    # ফরম্যাটে TrxID থাকে না, বরং sender username থাকে, তাই নিচের bKash-স্টাইল
    # extraction চালানোর আগেই এটা আলাদাভাবে হ্যান্ডেল করা হয়।
    bm = _BINANCE_RECEIVED_RE.search(t)
    if bm:
        try:
            b_amount = float(bm.group(1))
        except ValueError:
            b_amount = None
        b_username = bm.group(3).strip().upper()
        if b_amount is not None and b_username:
            return {"method": "Binance", "amount": b_amount, "trx_id": b_username}
        return None

    # ১) sender short-code (16216/16247/16167) দিয়ে চেক — সবচেয়ে নির্ভরযোগ্য,
    #    কারণ এটা টেক্সট কনটেন্টের উপর নির্ভর করে না।
    method = _method_from_sender(sender)
    # ২) না পেলে, SMS টেক্সটে "bkash"/"nagad"/"rocket"/"dbbl" শব্দ খোঁজা হয়
    if not method:
        if "bkash" in tl:
            method = "bKash"
        elif "nagad" in tl:
            method = "Nagad"
        elif "rocket" in tl or "dbbl" in tl:
            method = "Rocket"
        # ✅ FIX: SMS Forwarder app আলাদা "sender"/"from" field পাঠায় না (শুধু
        # JSON body-তে message content আসে), তাই sender-code চেক (16216) কাজ
        # করে না। কিন্তু ফরওয়ার্ড হওয়া টেক্সটে "Incoming Number" হিসেবে
        # 16216 (Rocket-এর অফিসিয়াল নাম্বার) নিজেই থেকে যায় — সেটা এখানে
        # স্বতন্ত্র সংখ্যা (word-boundary) হিসেবে খুঁজে Rocket ধরা হচ্ছে।
        if not method and re.search(r"(?<!\d)16216(?!\d)", t):
            method = "Rocket"
    # ৩) তাও না পেলে, ফরোয়ার্ডার অ্যাপ থেকে আসা hint_method ব্যবহার হয়
    if not method and hint_method:
        method = hint_method.strip()

    amount = None
    m = _AMOUNT_RE.search(t)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None

    trx_id = None
    m2 = _TRXID_RE.search(t)
    if m2:
        trx_id = m2.group(1).strip().upper()

    if amount is None or not trx_id:
        return None
    return {"method": method, "amount": amount, "trx_id": trx_id}


def _methods_match(a: str, b: str) -> bool:
    """Payment method দুটো (bKash/Nagad/Rocket) case-insensitively মিলছে কিনা।
    কোনোটা খালি থাকলে মিল ধরা হয় না — method যাচাই না করে auto-approve করা যাবে না।"""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    return bool(a) and bool(b) and a == b


async def _auto_reject_mismatched_deposit(
    session: AsyncSession, dep: "Deposit", sms: "SmsMessage", mismatch_lines: list[str]
) -> None:
    """TrxID মিলেছে কিন্তু Payment Method বা Amount মিলেনি — মানে ইউজার ভুল তথ্য
    দিয়েছে। এই ক্ষেত্রে deposit-টা আর pending রাখা হবে না, সাথে সাথে reject করে
    ইউজারকে জানিয়ে দেওয়া হচ্ছে যাতে সে সঠিক তথ্য দিয়ে আবার চেষ্টা করতে পারে।
    এডমিনকেও অডিট-এর জন্য নোটিফাই করা হয়।"""
    async with get_lock(f"deposit:{dep.id}"):
        fresh_dep = await session.get(Deposit, dep.id)
        if not fresh_dep or fresh_dep.status != "pending":
            return
        fresh_dep.status = "rejected"
        fresh_dep.reviewed_at = datetime.utcnow()
        fresh_dep.reviewed_by = 0  # 0 = system auto-reject (mismatch)
        fresh_dep.note = "auto_rejected_mismatch"
        await session.commit()
        await log_admin(
            session, 0, "deposit_auto_reject_mismatch",
            f"dep_id={fresh_dep.id} " + " | ".join(mismatch_lines),
        )

    async with SessionLocal() as _s:
        support = await get_setting(_s, "support_contact", "@support")
    try:
        await bot.send_message(
            fresh_dep.user_id,
            f"❌ <b>আপনার দেওয়া তথ্য সঠিক নয়!</b>\n\n"
            f"🆔 Request: <code>DEP-{fresh_dep.id}</code>\n\n"
            f"আপনার দেওয়া ডিপোজিট এর তথ্য সঠিক নয়।\n\n"
            f"👉 সঠিক তথ্য দিয়ে আবার চেষ্টা করুন, অথবা {support}-এ যোগাযোগ করুন।",
            reply_markup=kb_back("home"),
        )
    except Exception:
        pass

    try:
        await broadcast_to_admins(
            "🚫 <b>Deposit Auto-Rejected — Mismatch</b>\n\n"
            f"🆔 Deposit: DEP-{fresh_dep.id} (user {fresh_dep.user_id})\n"
            f"🔑 {_id_label(sms.method)}: <code>{sms.trx_id}</code> (মিলেছে)\n\n"
            + "\n".join(mismatch_lines) +
            "\n\nইউজারকে ভুল তথ্যের নোটিফিকেশন পাঠিয়ে রিকোয়েস্টটা auto-reject করা হয়েছে।"
        )
    except Exception:
        pass


async def store_sms_and_try_match(text: str, sender: str = "", hint_method: str = "") -> dict:
    """ওয়েবহুক থেকে আসা SMS পার্স + সেইভ করে, এবং কোনো pending ডিপোজিটের সাথে
    ম্যাচ করে থাকলে সাথে সাথে অটো-অ্যাপ্রুভ করে দেয়। রেজাল্ট dict রিটার্ন করে।
    ফরোয়ার্ডার অ্যাপ রিট্রাই করলে (একই SMS দুইবার আসলে) ডুপ্লিকেট রো তৈরি করে না।
    ✅ FIX: শুধু TrxID মিললেই আর অটো-অ্যাপ্রুভ হবে না — payment method, amount ও
    TrxID — এই ৩টাই মিললে তবেই approve হবে। TrxID মিললেও method/amount না
    মিললে admin কে error/mismatch alert যাবে, deposit pending-ই থাকবে।"""
    parsed = parse_payment_sms(text, hint_method, sender=sender)
    if not parsed:
        return {"stored": False, "reason": "not_a_payment_sms"}

    async with SessionLocal() as s:
        # ✅ ডুপ্লিকেট রিট্রাই চেক — bKash/Nagad/Rocket এ TrxID ইউনিক বলে
        # trx_id+amount দিয়ে চেক করাই যথেষ্ট। কিন্তু Binance-এ trx_id আসলে
        # username (ইউনিক transaction identifier না — একই ইউজার বারবার একই
        # amount পাঠাতে পারে), তাই সেখানে পুরো raw SMS টেক্সট হুবহু মিললে
        # তবেই সেটাকে "app এর retry" হিসেবে ধরা হয়, নাহলে নতুন transaction।
        if parsed["method"] == "Binance":
            existing = (await s.execute(
                select(SmsMessage).where(SmsMessage.raw_text == text[:1000])
            )).scalars().first()
        else:
            existing = (await s.execute(
                select(SmsMessage).where(SmsMessage.trx_id == parsed["trx_id"])
            )).scalars().first()
        if existing and existing.amount is not None and abs(existing.amount - parsed["amount"]) < 0.01:
            return {
                "stored": True, "sms_id": existing.id, "parsed": parsed,
                "duplicate": True, "matched_deposit_id": existing.used_by_deposit_id,
            }

        sms = SmsMessage(
            method=parsed["method"], amount=parsed["amount"], trx_id=parsed["trx_id"],
            raw_text=text[:1000], sender=(sender or "")[:64],
        )
        s.add(sms)
        await s.commit()
        await s.refresh(sms)

        # 📥 নতুন SMS সেইভ হওয়া মাত্র এডমিনদের নোটিফাই করা হচ্ছে (ম্যাচ পাওয়া যাক বা না যাক)
        try:
            await broadcast_to_admins(
                "📥 <b>New SMS Received</b>\n\n"
                f"💳 Method: {sms.method or '—'}\n"
                f"🔑 {_id_label(sms.method)}: <code>{sms.trx_id}</code>\n"
                f"💰 Amount: {sms.amount} {_amount_unit(sms.method)}\n"
                f"📅 Time: {sms.received_at.strftime('%d/%m/%Y %I:%M %p')}\n\n"
                f"⏳ User {_id_label(sms.method)} + Amount submit করলে auto-approve হবে।"
            )
        except Exception:
            pass

        dep = (await s.execute(
            select(Deposit).where(
                Deposit.status == "pending",
                Deposit.trx_id.isnot(None),
                func.upper(Deposit.trx_id) == sms.trx_id,
            ).order_by(Deposit.id.asc())
        )).scalars().first()

        matched_dep_id = None
        if dep:
            amount_ok = abs(dep.amount - sms.amount) < 0.01
            method_ok = _methods_match(dep.method, sms.method)
            if amount_ok and method_ok:
                ok = await auto_approve_deposit(s, dep, sms)
                if ok:
                    matched_dep_id = dep.id
            else:
                # ❌ TrxID মিলেছে কিন্তু amount/method মিলেনি — মানে ইউজার ভুল তথ্য
                # দিয়েছিলো। তাই এটা pending/manual-review এ না রেখে সাথে সাথে
                # auto-reject করে ইউজারকে সঠিক তথ্য দিতে বলা হচ্ছে।
                mismatch_lines = []
                if not method_ok:
                    mismatch_lines.append(f"💳 Method মিলছে না — Deposit: <b>{dep.method or '—'}</b> vs SMS: <b>{sms.method or '—'}</b>")
                if not amount_ok:
                    mismatch_lines.append(f"💰 Amount মিলছে না — Deposit: <b>{dep.amount}</b> vs SMS: <b>{sms.amount}</b>")
                await _auto_reject_mismatched_deposit(s, dep, sms, mismatch_lines)

    return {"stored": True, "sms_id": sms.id, "parsed": parsed, "matched_deposit_id": matched_dep_id}


async def try_auto_approve_from_stored_sms(session: AsyncSession, dep: Deposit) -> str:
    """একটা নতুন pending ডিপোজিট বানানোর সাথে সাথে, আগে থেকে সেইভ থাকা কোনো
    অব্যবহৃত SMS এর সাথে method+amount+trx_id — এই ৩টাই ১০০% ম্যাচ করলে তবেই
    সাথে সাথে অটো-অ্যাপ্রুভ করে। TrxID মিললেও method/amount না মিললে — মানে
    ইউজার ভুল তথ্য দিয়েছে — deposit-টা pending না রেখে সাথে সাথে reject করে
    ইউজারকে ভুল তথ্যের কথা জানানো হয়।

    রিটার্ন করে: "approved" | "rejected_mismatch" | "no_match"
    """
    if not dep.trx_id:
        return "no_match"
    sms = (await session.execute(
        select(SmsMessage).where(
            SmsMessage.is_used == False,  # noqa: E712
            SmsMessage.trx_id == dep.trx_id.strip().upper(),
        ).order_by(SmsMessage.id.asc())
    )).scalars().first()
    if not sms:
        return "no_match"

    amount_ok = sms.amount is not None and abs(sms.amount - dep.amount) < 0.01
    method_ok = _methods_match(dep.method, sms.method)
    if amount_ok and method_ok:
        ok = await auto_approve_deposit(session, dep, sms)
        return "approved" if ok else "no_match"

    # ❌ TrxID মিলেছে কিন্তু amount/method মিলেনি — ইউজারের দেওয়া তথ্য ভুল,
    # তাই pending না রেখে সাথে সাথে reject করে দেওয়া হচ্ছে।
    mismatch_lines = []
    if not method_ok:
        mismatch_lines.append(f"💳 Method মিলছে না — Deposit: <b>{dep.method or '—'}</b> vs SMS: <b>{sms.method or '—'}</b>")
    if not amount_ok:
        mismatch_lines.append(f"💰 Amount মিলছে না — Deposit: <b>{dep.amount}</b> vs SMS: <b>{sms.amount}</b>")
    await _auto_reject_mismatched_deposit(session, dep, sms, mismatch_lines)
    return "rejected_mismatch"


async def auto_approve_deposit(session: AsyncSession, dep: Deposit, sms: "SmsMessage") -> bool:
    """dep + sms দুটোকেই used/approved হিসেবে মার্ক করে এবং ইউজারের ব্যালেন্সে টাকা যোগ করে।
    কল করার আগে নিশ্চিত হতে হবে dep.status == 'pending' এবং sms.is_used == False।"""
    async with get_lock(f"deposit:{dep.id}"):
        fresh_dep = await session.get(Deposit, dep.id)
        if not fresh_dep or fresh_dep.status != "pending":
            return False
        fresh_sms = await session.get(SmsMessage, sms.id)
        if not fresh_sms or fresh_sms.is_used:
            return False

        fresh_dep.status = "approved"
        fresh_dep.reviewed_at = datetime.utcnow()
        fresh_dep.reviewed_by = 0  # 0 = system auto-approve
        fresh_sms.is_used = True
        fresh_sms.used_by_deposit_id = fresh_dep.id
        await session.commit()

        user = await session.get(User, fresh_dep.user_id)
        new_bal = await add_balance(
            session, user, fresh_dep.amount, "deposit",
            f"Auto-approved Deposit #{fresh_dep.id} ({fresh_dep.method}) TrxID {fresh_dep.trx_id}",
        )
        await log_admin(session, 0, "deposit_auto_approve", f"dep_id={fresh_dep.id} sms_id={fresh_sms.id}")

    try:
        await bot.send_message(
            fresh_dep.user_id,
            f"✅ <b>Deposit Auto-Approved!</b>\n\n"
            f"🆔 Request: <code>DEP-{fresh_dep.id}</code>\n"
            f"💳 Method: {fresh_dep.method}\n"
            f"💰 +{dual_money(fresh_dep.amount)} added\n"
            f"💼 New Balance: {dual_money(new_bal)}",
        )
    except Exception:
        pass
    try:
        await broadcast_to_admins(
            f"🤖 <b>Auto-Approved Deposit</b>\n\n"
            f"🆔 DEP-{fresh_dep.id} | 👤 {fresh_dep.user_id}\n"
            f"💳 {fresh_dep.method} | 💰 {dual_money(fresh_dep.amount)}\n"
            f"🔑 {_id_label(fresh_dep.method)}: <code>{fresh_dep.trx_id}</code>\n"
            f"📩 Matched SMS #{fresh_sms.id}",
        )
    except Exception:
        pass
    return True


# =====================================================================
# CALLBACK DATA FACTORIES
# =====================================================================
class NavCB(CallbackData, prefix="nav"):
    to: str


class ProdListCB(CallbackData, prefix="plist"):
    page: int = 0


class ProdCB(CallbackData, prefix="prod"):
    id: int


class BuyCB(CallbackData, prefix="buy"):
    action: str   # inc/dec/confirm/cancel
    id: int
    qty: int = 1


class WithdrawMethodCB(CallbackData, prefix="wdm"):
    method: str


class WithdrawReviewCB(CallbackData, prefix="wdr"):
    action: str
    id: int


class DepMethodCB(CallbackData, prefix="depm"):
    method: str


class DepReviewCB(CallbackData, prefix="depr"):
    action: str   # approve/reject
    id: int


class SellReviewCB(CallbackData, prefix="sellr"):
    action: str
    id: int


class SellProdCB(CallbackData, prefix="sellp"):
    id: int


class AListCB(CallbackData, prefix="alst"):
    kind: str
    page: int = 0


class AProdCB(CallbackData, prefix="aprod"):
    action: str   # view/edit_price/delete/add/toggle/addbuy
    id: int = 0


class AXlsxCB(CallbackData, prefix="axl"):
    action: str   # pick_product/confirm/cancel/export
    id: int = 0


class AUserCB(CallbackData, prefix="auser"):
    action: str   # addbal/rembal/ban/unban
    id: int = 0


class AToggleCB(CallbackData, prefix="atog"):
    key: str


class WMToggleCB(CallbackData, prefix="wmtog"):
    method: str


class DepNumSetCB(CallbackData, prefix="depnum"):
    method: str


class BackupCB(CallbackData, prefix="bkp"):
    action: str   # export/import/confirm/cancel


# =====================================================================
# FSM STATES
# =====================================================================
class DepositStates(StatesGroup):
    amount = State()
    trx_id = State()


class WithdrawStates(StatesGroup):
    amount = State()
    waiting_method = State()
    account_number = State()


class SellStates(StatesGroup):
    waiting_data = State()


class BuyStates(StatesGroup):
    quantity = State()


class AdminProductStates(StatesGroup):
    add_name = State()
    add_price = State()
    add_desc = State()
    edit_price = State()
    add_buy_name = State()
    add_buy_price = State()


class AdminXlsxStates(StatesGroup):
    waiting_file = State()


class AdminBalanceStates(StatesGroup):
    waiting_amount = State()


class AdminSearchStates(StatesGroup):
    waiting_query = State()
    waiting_info_id = State()


class AdminBroadcastStates(StatesGroup):
    waiting_text = State()


class AdminSettingsStates(StatesGroup):
    waiting_value = State()


class AdminMsgUserStates(StatesGroup):
    waiting_id = State()
    waiting_text = State()


class AdminBalanceEditStates(StatesGroup):
    waiting_id = State()


class AdminSellApproveStates(StatesGroup):
    waiting_pcs = State()


class AdminDepositNumberStates(StatesGroup):
    waiting_value = State()


class AdminBackupStates(StatesGroup):
    waiting_file = State()


# per-user tiny memory for a couple of "pending" admin choices that don't
# need full FSM (kept simple & explicit on purpose)
_ADMIN_TMP: dict[int, dict] = {}


# =====================================================================
# KEYBOARDS
# =====================================================================
def kb_main_reply(user_is_admin: bool = False) -> ReplyKeyboardMarkup:
    """✅ /start এর পর যেই মেনু বাটনগুলো দেখা যায়, সেগুলো এখন inline বাটনের
    বদলে চ্যাটের নিচে সবসময় দেখানো (persistent) keyboard বাটন — এডমিনের
    জন্য আলাদা '🛠 Admin Panel' বাটনসহ।"""
    kb = ReplyKeyboardBuilder()
    kb.button(text="🛒 Buy Mail")
    kb.button(text="💼 Sell Mail")
    kb.button(text="👤 Profile")
    kb.button(text="💰 Deposit")
    kb.button(text="📦 My Orders")
    kb.button(text="🧾 My Sell Requests")
    kb.button(text="📞 Support")
    kb.adjust(2, 2, 2, 1)
    if user_is_admin:
        kb.row(KeyboardButton(text="🛠 Admin Panel"))
    return kb.as_markup(resize_keyboard=True)


def kb_back(to: str = "home") -> Optional[InlineKeyboardMarkup]:
    # ✅ ব্যবহারকারীর অনুরোধে "⬅️ Back" ও "🏠 Home" ইনলাইন বাটন সরিয়ে দেওয়া
    # হয়েছে — নিচের persistent কীবোর্ড দিয়েই নেভিগেশন হবে।
    return None


def _support_url(contact: str) -> str:
    """support_contact সেটিং (যেমন '@username' বা পুরো t.me লিংক) থেকে
    সরাসরি ক্লিকযোগ্য https://t.me/username URL বানায়।"""
    c = (contact or "").strip()
    if not c:
        return "https://t.me/support"
    if c.startswith("http://") or c.startswith("https://"):
        return c
    if c.startswith("t.me/"):
        return "https://" + c
    return "https://t.me/" + c.lstrip("@")


def kb_support(contact: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅24/7 live chat", url=_support_url(contact))
    b.adjust(1)
    return b.as_markup()


def kb_profile() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💸 Withdraw", callback_data=NavCB(to="withdraw"))
    b.adjust(1)
    return b.as_markup()


def kb_wallet() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Deposit", callback_data=NavCB(to="deposit"))
    b.button(text="🧾 Transaction History", callback_data=NavCB(to="txhistory"))
    b.adjust(1, 1)
    return b.as_markup()


def kb_deposit_methods(methods: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=m, callback_data=DepMethodCB(method=m))
    b.adjust(2)
    return b.as_markup()


def kb_withdraw_methods(enabled: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in ALL_WITHDRAW_METHODS:
        text = m if m in enabled else f"🔒 {m}"
        b.button(text=text, callback_data=WithdrawMethodCB(method=m))
    b.adjust(2)
    return b.as_markup()


def kb_products(products: list[Product], page: int, has_next: bool, stock_counts: Optional[dict] = None) -> InlineKeyboardMarkup:
    stock_counts = stock_counts or {}
    b = InlineKeyboardBuilder()
    for p in products:
        # ✅ price/stock এখন মেসেজ টেক্সটে না দেখানোয়, বাটনের লেবেলেই দেখানো হচ্ছে
        stock = stock_counts.get(p.id, 0)
        b.button(text=f"🛒 {p.name} • {dual_money(p.price)} • Stock: {stock}", callback_data=ProdCB(id=p.id))
    b.adjust(1)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=ProdListCB(page=page - 1).pack()))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=ProdListCB(page=page + 1).pack()))
    if nav_row:
        b.row(*nav_row)
    return b.as_markup()


def kb_product_detail(product_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Buy", callback_data=BuyCB(action="start", id=product_id, qty=1))
    b.adjust(1)
    return b.as_markup()


def kb_qty(product_id: int, qty: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="➖", callback_data=BuyCB(action="dec", id=product_id, qty=qty).pack()),
        InlineKeyboardButton(text=f"{qty}", callback_data="noop"),
        InlineKeyboardButton(text="➕", callback_data=BuyCB(action="inc", id=product_id, qty=qty).pack()),
    )
    b.row(InlineKeyboardButton(text="✏️ Type Quantity (pcs)", callback_data=BuyCB(action="typeqty", id=product_id, qty=qty).pack()))
    b.row(InlineKeyboardButton(text="✅ Confirm Purchase", callback_data=BuyCB(action="confirm", id=product_id, qty=qty).pack()))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data=BuyCB(action="cancel", id=product_id, qty=qty).pack()))
    return b.as_markup()


def kb_admin_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Selling Products", callback_data=AProdCB(action="add"))
    b.button(text="🛍 Buy Product Add", callback_data=AProdCB(action="addbuy"))
    b.button(text="📦 Products", callback_data=AListCB(kind="aprod"))
    b.button(text="📊 Stock", callback_data=AListCB(kind="stock"))
    b.button(text="🧾 Orders", callback_data=AListCB(kind="orders"))
    b.button(text="💰 Deposits", callback_data=AListCB(kind="deps"))
    b.button(text="📩 SMS Log", callback_data=AListCB(kind="sms"))
    b.button(text="💼 Seller Requests", callback_data=AListCB(kind="sells"))
    b.button(text="💳 Balance Edit", callback_data=NavCB(to="admin_balance_edit"))
    b.button(text="👥 Users", callback_data=NavCB(to="admin_users"))
    b.button(text="🧑‍💻 User Info", callback_data=NavCB(to="user_info"))
    b.button(text="✉️ Message User", callback_data=NavCB(to="admin_msg_user"))
    b.button(text="📢 Broadcast", callback_data=NavCB(to="admin_broadcast"))
    b.button(text="⚙️ Settings", callback_data=NavCB(to="admin_settings"))
    b.button(text="🗄 Export/Import DB", callback_data=NavCB(to="backup_panel"))
    b.button(text="🏠 Home", callback_data=NavCB(to="home"))
    b.adjust(2, 2, 2, 2, 2, 2, 2, 1, 1)
    return b.as_markup()


def kb_admin_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Admin Panel", callback_data=NavCB(to="admin"))
    return b.as_markup()


# =====================================================================
# BOT / DISPATCHER
# =====================================================================
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# =====================================================================
# MIDDLEWARE — rate limit + ban check + maintenance mode
# =====================================================================
class GuardMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self._hits: dict[int, list[float]] = {}

    def _rate_limited(self, uid: int) -> bool:
        now = time.time()
        arr = self._hits.setdefault(uid, [])
        arr[:] = [t for t in arr if now - t < RATE_LIMIT_WINDOW]
        arr.append(now)
        return len(arr) > RATE_LIMIT_MAX_HITS

    async def __call__(self, handler, event, data):
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:
            return await handler(event, data)
        uid = tg_user.id

        if self._rate_limited(uid) and not is_admin(uid):
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Too many requests, slow down.", show_alert=False)
            return

        async with SessionLocal() as session:
            user = await get_or_create_user(session, tg_user)
            if user.status == "banned":
                text = "🚫 <b>You have been banned from using this bot.</b>"
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer("You are banned.", show_alert=True)
                return

            if await is_maintenance_on(session) and not is_admin(uid):
                text = "🛠 <b>Bot is under maintenance.</b>\nPlease check back soon."
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer("Under maintenance.", show_alert=True)
                return

            data["db_user"] = user
        return await handler(event, data)


router.message.middleware(GuardMiddleware())
router.callback_query.middleware(GuardMiddleware())


# =====================================================================
# /start & HOME
# =====================================================================
def home_text(user: User) -> str:
    return (
        "✨ <b>Welcome to the Marketplace!</b>\n\n"
        f"👤 {user.full_name or user.username or user.id}\n"
        f"💳 Balance: <b>{dual_money(user.balance)}</b>\n\n"
        "Choose an option below:"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db_user: User, state: FSMContext):
    await state.clear()
    await message.answer(home_text(db_user), reply_markup=kb_main_reply(is_admin(message.from_user.id)))


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ You are not authorized to use the admin panel.")
        return
    await message.answer(await admin_dashboard_text(), reply_markup=kb_admin_home())


# =====================================================================
# 🆕 MAIN MENU — persistent keyboard button handlers
# (রেজিস্ট্রেশন অর্ডার ইচ্ছাকৃতভাবে উপরে রাখা হয়েছে, যাতে কোনো FSM অবস্থায়
#  থাকলেও এই মেনু বাটনগুলো চাপলে সাথে সাথেই সেই স্টেট ক্লিয়ার হয়ে সঠিক
#  মেনুতে চলে যায় — টেক্সট ইনপুট হিসেবে ভুলভাবে প্রসেস না হয়ে)
# =====================================================================
async def render_product_list(page: int = 0) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Buyer-facing product list — flat, no categories. Products show up as direct buttons."""
    async with SessionLocal() as s:
        all_products = (await s.execute(
            select(Product)
            .where(Product.is_active == True, Product.is_buy_listing == False)
            .order_by(Product.id)
        )).scalars().all()

    if not all_products:
        return "🛒 <b>Marketplace</b>\n\nNo products available yet.", None

    start = page * PAGE_SIZE
    chunk = all_products[start:start + PAGE_SIZE]
    has_next = len(all_products) > start + PAGE_SIZE

    async with SessionLocal() as s:
        stock_counts = {}
        for p in chunk:
            cnt = (await s.execute(
                select(func.count()).select_from(InventoryItem)
                .where(InventoryItem.product_id == p.id, InventoryItem.status == "available")
            )).scalar_one()
            stock_counts[p.id] = cnt

    text = "🛒 <b>Marketplace</b>\n\nChoose a product 👇"
    markup = kb_products(chunk, page, has_next, stock_counts)
    return text, markup


@router.message(F.text == "🛒 Buy Mail")
async def msg_market(message: Message, state: FSMContext):
    await state.clear()
    text, markup = await render_product_list(0)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == "👤 Profile")
async def msg_profile(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        user = await s.get(User, message.from_user.id)
    text = (
        "👤 <b>Your Profile</b>\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔖 Username: @{user.username or '—'}\n"
        f"💳 Balance: <b>{dual_money(user.balance)}</b>\n"
        f"⬆️ Total Deposit: {dual_money(user.total_deposit)}\n"
        f"💼 Total Income: {dual_money(user.total_income)}\n"
        f"🧾 Total Orders: {user.total_orders}\n"
        f"📅 Joined: {user.joined_at.strftime('%d/%m/%Y')}"
    )
    await message.answer(text, reply_markup=kb_profile())


@router.message(F.text == "💰 Deposit")
async def msg_wallet(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        user = await s.get(User, message.from_user.id)
    text = f"💰 <b>Deposit</b>\n\nBalance: <b>{dual_money(user.balance)}</b>"
    await message.answer(text, reply_markup=kb_wallet())


@router.message(F.text == "📦 My Orders")
async def msg_orders(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order).where(Order.user_id == message.from_user.id).order_by(Order.id.desc()).limit(10)
        )).scalars().all()
    if not rows:
        text = "📦 <b>My Orders</b>\n\nYou have no orders yet."
    else:
        lines = ["📦 <b>My Orders (last 10)</b>\n"]
        for o in rows:
            lines.append(f"• {o.order_id} | {o.product_name} x{o.quantity} | {dual_money(o.total_price)} | {o.created_at.strftime('%d/%m/%Y')}")
        text = "\n".join(lines)
    await message.answer(text)


@router.message(F.text == "🧾 My Sell Requests")
async def msg_my_sell_requests(message: Message, state: FSMContext):
    """✅ ইউজার বিক্রি করেছে কিন্তু এখনো এডমিন এপ্রুভ/টাকা দেয়নি — এমন
    সেল রিকোয়েস্টগুলো (pending/approved/rejected সবগুলোই) ইউজার নিজে দেখতে পারবে।"""
    await state.clear()
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(SellerSubmission)
            .where(SellerSubmission.user_id == message.from_user.id)
            .order_by(SellerSubmission.id.desc())
            .limit(10)
        )).scalars().all()
    if not rows:
        await message.answer("🧾 <b>My Sell Requests</b>\n\nYou haven't submitted anything to sell yet.")
        return

    status_label = {
        "pending": "⏳ Pending (টাকা এখনো দেয়া হয়নি)",
        "approved": "✅ Approved & Paid",
        "rejected": "❌ Rejected",
    }
    lines = ["🧾 <b>My Sell Requests (last 10)</b>\n"]
    for sub in rows:
        lines.append(
            f"📦 <b>{sub.product_name or '—'}</b> | {sub.item_count} item(s)\n"
            f"   {status_label.get(sub.status, sub.status)}"
            + (f" — {dual_money(sub.credited_amount)}" if sub.status == "approved" and sub.credited_amount else "")
            + f"\n   📅 {sub.created_at.strftime('%d/%m/%Y')} • 🆔 SUB-{sub.id}"
        )
    pending_count = sum(1 for sub in rows if sub.status == "pending")
    text = "\n\n".join(lines)
    if pending_count:
        text += f"\n\n⏳ আপনার <b>{pending_count}টি</b> রিকোয়েস্ট এখনো পেন্ডিং এ আছে — এডমিন রিভিউ করলেই টাকা যোগ হয়ে যাবে।"
    await message.answer(text)


@router.message(F.text == "💼 Sell Mail")
async def msg_sell(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        products = (await s.execute(
            select(Product).where(Product.is_active == True, Product.seller_price > 0)
        )).scalars().all()
    if not products:
        await message.answer("💼 <b>Sell Mail</b>\n\nNo products are open for selling right now.")
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p.name} — pays {dual_money(p.seller_price)}/item", callback_data=SellProdCB(id=p.id))
    b.adjust(1)
    await message.answer("💼 <b>Sell Mail</b>", reply_markup=b.as_markup())


@router.message(F.text == "📞 Support")
async def msg_support(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        contact = await get_setting(s, "support_contact", "@support")
    await message.answer(
        f"☎️ <b>Support</b>\n\n👨‍💻 Developer: @relax1472\n\n📞 Need help? Contact us: {contact}",
        reply_markup=kb_support(contact),
    )


@router.message(F.text == "🛠 Admin Panel")
async def msg_admin_panel_btn(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ You are not authorized to use the admin panel.")
        return
    await message.answer(await admin_dashboard_text(), reply_markup=kb_admin_home())


@router.callback_query(NavCB.filter(F.to == "home"))
async def cb_home(call: CallbackQuery, db_user: User, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        fresh = await s.get(User, db_user.id)
    # ✅ হোম মেনু এখন নিচের persistent keyboard বাটন দিয়ে দেখানো হয়,
    # তাই এখানে আর কোনো inline keyboard লাগবে না।
    await safe_edit(call, home_text(fresh) + "\n\n👇 <i>Use the menu below to navigate.</i>", None)
    await call.answer()


# =====================================================================
# small util: edit-or-send
# =====================================================================
async def safe_edit(call: CallbackQuery, text: str, markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        try:
            await call.message.answer(text, reply_markup=markup)
        except Exception:
            pass


# =====================================================================
# PROFILE
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "profile"))
async def cb_profile(call: CallbackQuery):
    async with SessionLocal() as s:
        user = await s.get(User, call.from_user.id)
    text = (
        "👤 <b>Your Profile</b>\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔖 Username: @{user.username or '—'}\n"
        f"💳 Balance: <b>{dual_money(user.balance)}</b>\n"
        f"⬆️ Total Deposit: {dual_money(user.total_deposit)}\n"
        f"💼 Total Income: {dual_money(user.total_income)}\n"
        f"🧾 Total Orders: {user.total_orders}\n"
        f"📅 Joined: {user.joined_at.strftime('%d/%m/%Y')}"
    )
    await safe_edit(call, text, kb_profile())
    await call.answer()


# =====================================================================
# WITHDRAW
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "withdraw"))
async def cb_withdraw(call: CallbackQuery, state: FSMContext):
    async with SessionLocal() as s:
        user = await s.get(User, call.from_user.id)
        min_wd = float(await get_setting(s, "min_withdraw_amount", "0"))
    if user.balance <= 0:
        await call.answer("⛔ Your balance is 0.", show_alert=True)
        return
    await state.set_state(WithdrawStates.amount)
    hint = f"\n💵 Minimum Withdrawal: {dual_money(min_wd)}" if min_wd > 0 else ""
    text = (
        f"💸 <b>Withdraw</b>\n\n"
        f"💳 Available Balance: <b>{dual_money(user.balance)}</b>{hint}\n\n"
        f"কত টাকা তুলতে চান? এমাউন্ট লিখুন (শুধু নাম্বার):"
    )
    await safe_edit(call, text, None)
    await call.answer()


@router.message(StateFilter(WithdrawStates.amount))
async def msg_withdraw_amount(message: Message, state: FSMContext, db_user: User):
    txt = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(txt)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid amount. Please send a valid positive number.")
        return

    async with SessionLocal() as s:
        user = await s.get(User, db_user.id)
        min_wd = float(await get_setting(s, "min_withdraw_amount", "0"))
    if min_wd > 0 and amount < min_wd:
        await message.answer(f"❌ Minimum withdrawal amount is {dual_money(min_wd)}. Please enter a higher amount.")
        return
    if amount > user.balance:
        await message.answer(
            f"❌ Insufficient balance. Your current balance is {dual_money(user.balance)}."
        )
        return

    async with SessionLocal() as s:
        methods_raw = await get_setting(s, "withdraw_methods", ",".join(ALL_WITHDRAW_METHODS))
    enabled = {m.strip() for m in methods_raw.split(",") if m.strip()}
    if not enabled:
        await message.answer("⛔ No withdrawal methods configured. Contact support.")
        await state.clear()
        return

    await state.update_data(wd_amount=amount)
    await state.set_state(WithdrawStates.waiting_method)
    await message.answer(
        f"💰 Amount: <b>{dual_money(amount)}</b>\n\n"
        f"একটি পেমেন্ট মেথড সিলেক্ট করুন:\n"
        f"🔒 = সাময়িক সময়ের জন্য বন্ধ রাখা হয়েছে",
        reply_markup=kb_withdraw_methods(enabled),
    )


@router.callback_query(WithdrawMethodCB.filter())
async def cb_withdraw_method(call: CallbackQuery, callback_data: WithdrawMethodCB, state: FSMContext):
    data = await state.get_data()
    amount = data.get("wd_amount")
    if not amount:
        await call.answer("⛔ Session expired, please start again.", show_alert=True)
        await state.clear()
        return
    async with SessionLocal() as s:
        methods_raw = await get_setting(s, "withdraw_methods", ",".join(ALL_WITHDRAW_METHODS))
    enabled = {m.strip() for m in methods_raw.split(",") if m.strip()}
    if callback_data.method not in enabled:
        await call.answer(
            f"🔒 {callback_data.method} সাময়িক সময়ের জন্য বন্ধ রাখা হয়েছে। অন্য একটি মেথড বেছে নিন।",
            show_alert=True,
        )
        return
    await state.update_data(wd_method=callback_data.method)
    await state.set_state(WithdrawStates.account_number)
    await safe_edit(
        call,
        f"💸 Method: <b>{callback_data.method}</b>\n"
        f"💰 Amount: <b>{dual_money(amount)}</b>\n\n"
        f"আপনার <b>{callback_data.method}</b> নাম্বারটি লিখুন (এই নম্বরেই টাকা পাঠানো হবে):",
        None,
    )
    await call.answer()


@router.message(StateFilter(WithdrawStates.account_number))
async def msg_withdraw_account_number(message: Message, state: FSMContext, db_user: User):
    number = (message.text or "").strip()
    if not number or len(number) < 5 or len(number) > 64:
        await message.answer("❌ একটি সঠিক নাম্বার লিখুন (কমপক্ষে ৫ ডিজিট/ক্যারেক্টার):")
        return

    data = await state.get_data()
    amount = data.get("wd_amount")
    method = data.get("wd_method", "Unknown")
    if not amount:
        await message.answer("⛔ Session expired, please start again.")
        await state.clear()
        return

    async with get_lock(f"withdraw_submit:{db_user.id}"):
        async with SessionLocal() as s:
            user = await s.get(User, db_user.id)
            if amount > user.balance:
                await message.answer(
                    f"❌ Insufficient balance. Your current balance is {dual_money(user.balance)}."
                )
                await state.clear()
                return
            await state.clear()
            # টাকা সাথে সাথে হোল্ড/ডিডাক্ট করা হচ্ছে যাতে ইউজার একই ব্যালেন্স
            # থেকে একাধিকবার উইথড্র রিকোয়েস্ট দিতে না পারে
            # ⚠️ NOTE: add_balance() নিজেই ভিতরে "user:{id}" লক নেয়, তাই এখানে
            # ডাবল-সাবমিট আটকাতে ইচ্ছাকৃতভাবে আলাদা নামের ("withdraw_submit:{id}")
            # লক ব্যবহার করা হয়েছে — একই লক দুইবার নিলে asyncio.Lock reentrant
            # না হওয়ায় ডেডলক হয়ে যেতো এবং রিকোয়েস্টটা কখনোই সাবমিট/এডমিনের
            # কাছে যেতো না (এই কারণেই আগে withdraw আটকে যাচ্ছিল)
            new_bal = await add_balance(s, user, -amount, "withdraw_pending", f"Withdraw request ({method})")
            wd = Withdrawal(user_id=db_user.id, amount=amount, method=method, account_number=number[:64], status="pending")
            s.add(wd)
            await s.commit()
            await s.refresh(wd)

    await message.answer(
        f"⏳ <b>Withdraw request submitted!</b>\n\n"
        f"🆔 Request: <code>WD-{wd.id}</code>\n"
        f"💳 Method: {method}\n"
        f"🔢 Number: <code>{number}</code>\n"
        f"💰 Amount: {dual_money(amount)}\n"
        f"💳 Remaining Balance: {dual_money(new_bal)}\n\n"
        f"An admin will review it shortly."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=WithdrawReviewCB(action="approve", id=wd.id))
    kb.button(text="❌ Reject", callback_data=WithdrawReviewCB(action="reject", id=wd.id))
    kb.adjust(2)
    await broadcast_to_admins(
        f"💸 <b>New Withdraw Request</b>\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"💳 Method: {method}\n"
        f"🔢 Number: <code>{number}</code>\n"
        f"💰 Amount: {dual_money(amount)}\n"
        f"🆔 Request: <code>WD-{wd.id}</code>",
        kb.as_markup(),
    )


@router.callback_query(WithdrawReviewCB.filter())
async def cb_withdraw_review(call: CallbackQuery, callback_data: WithdrawReviewCB):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    wd_id = callback_data.id
    async with get_lock(f"withdraw:{wd_id}"):
        async with SessionLocal() as s:
            wd = await s.get(Withdrawal, wd_id)
            if not wd:
                await call.answer("Not found.", show_alert=True)
                return
            if wd.status != "pending":
                await call.answer("Already processed!", show_alert=True)
                return
            user = await s.get(User, wd.user_id)
            if callback_data.action == "approve":
                wd.status = "approved"
                wd.reviewed_at = datetime.utcnow()
                wd.reviewed_by = call.from_user.id
                await s.commit()
                try:
                    await bot.send_message(
                        user.id,
                        f"✅ <b>Withdraw Completed!</b>\n\n💸 {dual_money(wd.amount)} sent via {wd.method}\n"
                        f"💳 Balance: {dual_money(user.balance)}",
                    )
                except Exception:
                    pass
            else:
                wd.status = "rejected"
                wd.reviewed_at = datetime.utcnow()
                wd.reviewed_by = call.from_user.id
                await s.commit()
                new_bal = await add_balance(s, user, wd.amount, "withdraw_refund", f"Withdraw #{wd.id} rejected/refunded")
                try:
                    await bot.send_message(
                        user.id,
                        f"❌ <b>Withdraw Rejected.</b>\n\n🆔 Request: WD-{wd.id}\n"
                        f"💰 {dual_money(wd.amount)} refunded to your balance.\n"
                        f"💳 New Balance: {dual_money(new_bal)}",
                    )
                except Exception:
                    pass
            await log_admin(s, call.from_user.id, f"withdraw_{callback_data.action}", f"wd_id={wd.id}")

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer(f"{'Approved' if callback_data.action == 'approve' else 'Rejected'}!")


# =====================================================================
# SUPPORT
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "support"))
async def cb_support(call: CallbackQuery):
    async with SessionLocal() as s:
        contact = await get_setting(s, "support_contact", "@support")
    text = f"☎️ <b>Support</b>\n\n👨‍💻 Developer: @relax1472\n\n📞 Need help? Contact us: {contact}"
    await safe_edit(call, text, kb_support(contact))
    await call.answer()


# =====================================================================
# WALLET / DEPOSIT
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "wallet"))
async def cb_wallet(call: CallbackQuery):
    async with SessionLocal() as s:
        user = await s.get(User, call.from_user.id)
    text = f"💰 <b>Deposit</b>\n\nBalance: <b>{dual_money(user.balance)}</b>"
    await safe_edit(call, text, kb_wallet())
    await call.answer()


@router.callback_query(NavCB.filter(F.to == "deposit"))
async def cb_deposit(call: CallbackQuery):
    async with SessionLocal() as s:
        methods_raw = await get_setting(s, "deposit_methods", "bKash,Nagad,Rocket,Binance")
    methods = [m.strip() for m in methods_raw.split(",") if m.strip()]
    await safe_edit(call, "💳 <b>Select a deposit method:</b>", kb_deposit_methods(methods))
    await call.answer()


@router.callback_query(DepMethodCB.filter())
async def cb_deposit_method(call: CallbackQuery, callback_data: DepMethodCB, state: FSMContext):
    await state.update_data(dep_method=callback_data.method)
    await state.set_state(DepositStates.amount)
    async with SessionLocal() as s:
        min_dep = float(await get_setting(s, "min_deposit_amount", "0"))
        pay_number = await get_setting(s, f"payment_number_{callback_data.method}", "")
        binance_rate = float(await get_setting(s, "binance_usdt_rate", "127"))
    hint = f"\n(Minimum deposit: {dual_money(min_dep)})" if min_dep > 0 else ""
    send_money_note = (
        "⚠️ অবশ্যই <b>Send Money</b> অপশন দিয়ে টাকা পাঠাবেন\n"
        if callback_data.method in ("bKash", "Nagad", "Rocket") else ""
    )
    number_line = (
        f"📮 Send payment to: <code>{pay_number}</code>\n"
        if pay_number else
        "⚠️ এই মেথডের জন্য এখনো কোনো নাম্বার সেট করা নেই, সাপোর্টে যোগাযোগ করুন।\n"
    )
    if callback_data.method == "Binance":
        amount_prompt = (
            f"💱 Rate: <b>1 USDT = ৳{money(binance_rate)}</b>\n"
            f"{hint}\nEnter the amount in <b>USDT</b> (number only):"
        )
    else:
        amount_prompt = f"{hint}\nEnter the deposit amount (number only):"
    await safe_edit(
        call,
        f"💳 Method: <b>{callback_data.method}</b>\n\n{number_line}{send_money_note}{amount_prompt}",
        kb_back("wallet"),
    )
    await call.answer()


@router.message(StateFilter(DepositStates.amount))
async def msg_deposit_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    method = data.get("dep_method", "Unknown")
    txt = (message.text or "").strip().replace(",", ".")
    try:
        entered = float(txt)
        if entered <= 0:
            raise ValueError
    except ValueError:
        bad_label = "USDT amount" if method == "Binance" else "amount"
        await message.answer(f"❌ Invalid {bad_label}. Please send a valid positive number.")
        return

    usdt_amount = None
    if method == "Binance":
        async with SessionLocal() as s:
            binance_rate = float(await get_setting(s, "binance_usdt_rate", "127"))
        usdt_amount = entered
        amount = round(entered * binance_rate, 2)
        await state.update_data(dep_usdt_amount=usdt_amount, dep_rate=binance_rate)
    else:
        amount = entered

    async with SessionLocal() as s:
        min_dep = float(await get_setting(s, "min_deposit_amount", "0"))
    if min_dep > 0 and amount < min_dep:
        await message.answer(f"❌ Minimum deposit amount is {dual_money(min_dep)}. Please enter a higher amount.")
        return
    await state.update_data(dep_amount=amount)
    await state.set_state(DepositStates.trx_id)
    if method == "Binance":
        await message.answer(
            f"💱 {money(usdt_amount)} USDT = {dual_money(amount)}\n\n"
            "যে Binance username থেকে টাকা পাঠিয়েছেন সেটা লিখে পাঠান:👇"
        )
    else:
        await message.answer("Send your transection Id(TrxID):👇")


@router.message(StateFilter(DepositStates.trx_id))
async def msg_deposit_trxid(message: Message, state: FSMContext, db_user: User):
    data = await state.get_data()
    method = data.get("dep_method", "Unknown")
    amount = float(data.get("dep_amount", 0))
    usdt_amount = data.get("dep_usdt_amount")
    id_label = _id_label(method)
    trx_id = (message.text or "").strip().upper()
    trx_id = re.sub(r"\s+", "", trx_id)

    min_len = 3 if method == "Binance" else 4
    if len(trx_id) < min_len or not re.match(r"^[A-Z0-9_.]+$", trx_id):
        source_hint = "যে Binance ইউজারনেম থেকে টাকা পাঠিয়েছেন সেটা" if method == "Binance" else "SMS থেকে"
        await message.answer(f"❌ এটা সঠিক {id_label} মনে হচ্ছে না। {source_hint} হুবহু কপি করে আবার পাঠান।")
        return

    await state.clear()

    async with SessionLocal() as s:
        # ⚠️ Binance-এ trx_id আসলে username — একই username দিয়ে বারবার
        # (ভিন্ন ভিন্ন সময়ে) বৈধভাবে ডিপোজিট করা যেতে পারে, তাই এই "আগে একবার
        # approved হয়ে গেছে" ডুপ্লিকেট-চেকটা শুধু bKash/Nagad/Rocket এর জন্য
        # প্রযোজ্য (যেখানে TrxID সত্যিকারের ইউনিক ট্রানজেকশন আইডি)।
        if method != "Binance":
            dup = (await s.execute(
                select(Deposit).where(
                    func.upper(Deposit.trx_id) == trx_id,
                    Deposit.status == "approved",
                )
            )).scalars().first()
            if dup:
                await message.answer(
                    f"❌ এই {id_label} দিয়ে আগেই একটা ডিপোজিট অ্যাপ্রুভ হয়ে গেছে। ভুল {id_label} পাঠালে সাপোর্টে যোগাযোগ করুন।",
                    reply_markup=kb_back("home"),
                )
                return

        dep = Deposit(user_id=db_user.id, amount=amount, method=method, trx_id=trx_id, status="pending")
        s.add(dep)
        await s.commit()
        await s.refresh(dep)

        auto_result = await try_auto_approve_from_stored_sms(s, dep)

    if auto_result == "approved":
        # ইউজার ও এডমিনকে ইতিমধ্যে auto_approve_deposit() থেকে নোটিফাই করা হয়ে গেছে
        return
    if auto_result == "rejected_mismatch":
        # ভুল Method/Amount/TrxID — deposit ইতিমধ্যে auto-reject হয়ে গেছে এবং
        # ইউজারকে সঠিক তথ্য দিতে বলে error message পাঠানো হয়ে গেছে, তাই এখানে
        # আর "pending, wait for approval" মেসেজ পাঠানো হবে না।
        return

    wait_note = "নোটিফিকেশন/SMS ম্যাচ পাওয়া গেলেই automatic approved হয়ে যাবে।"
    usdt_line = f"💱 USDT: {money(usdt_amount)}\n" if method == "Binance" and usdt_amount else ""
    await message.answer(
        f"⏳ <b>Deposit request submitted!</b>\n\n"
        f"🆔 Request: <code>DEP-{dep.id}</code>\n"
        f"💳 Method: {method}\n"
        f"{usdt_line}"
        f"💰 Amount: {dual_money(amount)}\n"
        f"🔑 {id_label}: <code>{trx_id}</code>\n\n"
        f"{wait_note}",
        reply_markup=kb_back("home"),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=DepReviewCB(action="approve", id=dep.id))
    kb.button(text="❌ Reject", callback_data=DepReviewCB(action="reject", id=dep.id))
    kb.adjust(2)
    admin_note = "(নোটিফিকেশন/SMS ম্যাচ পাওয়া যায়নি)"
    await broadcast_to_admins(
        f"💰 <b>New Deposit Request</b> {admin_note}\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"💳 Method: {method}\n"
        f"{usdt_line}"
        f"💰 Amount: {dual_money(amount)}\n"
        f"🔑 {id_label}: <code>{trx_id}</code>\n"
        f"🆔 Request: <code>DEP-{dep.id}</code>",
        kb.as_markup(),
    )


@router.callback_query(DepReviewCB.filter())
async def cb_deposit_review(call: CallbackQuery, callback_data: DepReviewCB):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    dep_id = callback_data.id
    async with get_lock(f"deposit:{dep_id}"):
        async with SessionLocal() as s:
            dep = await s.get(Deposit, dep_id)
            if not dep:
                await call.answer("Not found.", show_alert=True)
                return
            if dep.status != "pending":
                await call.answer("Already processed!", show_alert=True)
                return
            trx_lock_key = f"trx:{dep.trx_id.strip().upper()}" if dep.trx_id else f"deposit:{dep_id}"
            async with get_lock(trx_lock_key):
                user = await s.get(User, dep.user_id)
                if callback_data.action == "approve":
                    # ✅ একই TrxID দিয়ে (অন্য কোনো Deposit রো-তে) আগেই কোনোটা approved
                    # হয়ে থাকলে — এই deposit-টা আর approve হতে দেওয়া হচ্ছে না, যাতে
                    # একই ট্রানজ্যাকশনের জন্য দুইবার ব্যালেন্স যোগ না হয়ে যায়।
                    if dep.trx_id:
                        dup = (await s.execute(
                            select(Deposit).where(
                                func.upper(Deposit.trx_id) == dep.trx_id.strip().upper(),
                                Deposit.status == "approved",
                                Deposit.id != dep.id,
                            )
                        )).scalars().first()
                        if dup:
                            dep.status = "rejected"
                            dep.reviewed_at = datetime.utcnow()
                            dep.reviewed_by = call.from_user.id
                            await s.commit()
                            await log_admin(
                                s, call.from_user.id, "deposit_auto_reject_duplicate_trx",
                                f"dep_id={dep.id} duplicate_of=DEP-{dup.id} trx_id={dep.trx_id}",
                            )
                            try:
                                await call.message.edit_reply_markup(reply_markup=None)
                            except Exception:
                                pass
                            await call.answer(
                                f"⛔ এই TrxID দিয়ে আগেই DEP-{dup.id} approved হয়ে গেছে — ডুপ্লিকেট, তাই এটা reject করা হলো।",
                                show_alert=True,
                            )
                            return
                    dep.status = "approved"
                    dep.reviewed_at = datetime.utcnow()
                    dep.reviewed_by = call.from_user.id
                    await s.commit()
                    new_bal = await add_balance(s, user, dep.amount, "deposit", f"Deposit #{dep.id} ({dep.method})")
                    try:
                        await bot.send_message(
                            user.id,
                            f"✅ <b>Deposit Approved!</b>\n\n💰 +{dual_money(dep.amount)} added\n"
                            f"💳 New Balance: {dual_money(new_bal)}",
                        )
                    except Exception:
                        pass
                else:
                    dep.status = "rejected"
                    dep.reviewed_at = datetime.utcnow()
                    dep.reviewed_by = call.from_user.id
                    await s.commit()
                    try:
                        await bot.send_message(
                            user.id,
                            f"❌ <b>Deposit Rejected.</b>\n\n🆔 Request: DEP-{dep.id}\nPlease contact support.",
                        )
                    except Exception:
                        pass
                await log_admin(s, call.from_user.id, f"deposit_{callback_data.action}", f"dep_id={dep.id}")

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer(f"{'Approved' if callback_data.action == 'approve' else 'Rejected'}!")


@router.callback_query(NavCB.filter(F.to == "txhistory"))
async def cb_txhistory(call: CallbackQuery):
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Transaction).where(Transaction.user_id == call.from_user.id)
            .order_by(Transaction.id.desc()).limit(15)
        )).scalars().all()
    if not rows:
        text = "🧾 <b>Transaction History</b>\n\nNo transactions yet."
    else:
        lines = ["🧾 <b>Transaction History (last 15)</b>\n"]
        for t in rows:
            sign = "+" if t.amount >= 0 else ""
            lines.append(f"• {t.created_at.strftime('%d/%m %H:%M')} | {t.type} | {sign}{dual_money(t.amount)} | bal: {dual_money(t.balance_after)}")
        text = "\n".join(lines)
    await safe_edit(call, text, kb_back("wallet"))
    await call.answer()


# =====================================================================
# MARKETPLACE
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "market"))
async def cb_market(call: CallbackQuery):
    text, markup = await render_product_list(0)
    await safe_edit(call, text, markup)
    await call.answer()


@router.callback_query(ProdListCB.filter())
async def cb_product_list_page(call: CallbackQuery, callback_data: ProdListCB):
    text, markup = await render_product_list(callback_data.page)
    await safe_edit(call, text, markup)
    await call.answer()


@router.callback_query(ProdCB.filter())
async def cb_product_detail(call: CallbackQuery, callback_data: ProdCB):
    async with SessionLocal() as s:
        p = await s.get(Product, callback_data.id)
        if not p:
            await call.answer("Product not found.", show_alert=True)
            return
        stock = (await s.execute(
            select(func.count()).select_from(InventoryItem)
            .where(InventoryItem.product_id == p.id, InventoryItem.status == "available")
        )).scalar_one()
    text = (
        f"🏷 <b>{p.name}</b>\n\n"
        f"💰 Price: {dual_money(p.price)}\n"
        f"📦 Stock: {stock}\n\n"
        f"{p.description or ''}"
    )
    await safe_edit(call, text, kb_product_detail(p.id))
    await call.answer()


def parse_item_line(line: str) -> dict:
    """InventoryItem.data স্ট্রিং যেমন 'mail: x | pass: y | fulldata: z' — সেটাকে
    হেডিং -> ভ্যালুর dict এ ভাঙে (ইনসার্শন অর্ডার ঠিক রেখে)।"""
    result: dict = {}
    for part in line.split(" | "):
        if ":" in part:
            k, v = part.split(":", 1)
            result[k.strip()] = v.strip()
        elif part.strip():
            result[part.strip()] = ""
    return result


def _html_escape(text: str) -> str:
    """<code>/<b> ট্যাগের ভেতরে বসানোর আগে ইউজার/এডমিনের ডেটায় থাকা &, <, > এস্কেপ করা।"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_item_line_html(line: str, inline: bool = False) -> str:
    """একটা raw item line (যেমন 'Email: x | Pass: y') কে সুন্দর HTML ফরম্যাটে
    রূপান্তর করে — হেডিং (Email:, Pass: ইত্যাদি) <b>bold</b> থাকবে আর আসল
    ডেটা <code>mono</code>-স্পেসে থাকবে (এক ট্যাপে কপি করা যায়)। ইউজার বা
    এডমিন — কারো কাছে product data টেক্সট হিসেবে গেলে সব জায়গায় এই একই
    ফাংশন ব্যবহার হয়, যাতে ফরম্যাট সবখানে একরকম থাকে।
    inline=True দিলে ' | ' দিয়ে এক লাইনে জোড়া লাগায় (লিস্ট/প্রিভিউ-র জন্য),
    নাহলে প্রতিটা ফিল্ড আলাদা লাইনে দেখায় (একক আইটেম ডেলিভারির জন্য)।"""
    parsed = parse_item_line(line)
    if not parsed:
        return f"<code>{_html_escape(line)}</code>"
    parts = []
    for k, v in parsed.items():
        k_esc, v_esc = _html_escape(k), _html_escape(v)
        parts.append(f"<b>{k_esc}:</b> <code>{v_esc}</code>" if v else f"<code>{k_esc}</code>")
    return " | ".join(parts) if inline else "\n".join(parts)


def build_item_preview_html(lines: list, numbered: bool = False, max_chars: int = 2500) -> tuple:
    """একগুচ্ছ raw item line থেকে HTML-ফরম্যাটেড প্রিভিউ বানায় (হেডিং bold,
    ডেটা mono)। max_chars ছাড়িয়ে গেলে — কোনো লাইনের মাঝখানে কেটে HTML ট্যাগ
    ভেঙে ফেলে না, পুরো একটা লাইন বাদ দিয়ে দেয় (তাই আউটপুট সবসময় ভ্যালিড HTML)।
    রিটার্ন করে (preview_text, shown_count, total_count)।"""
    out, total_len, shown = [], 0, 0
    for i, l in enumerate(lines, start=1):
        formatted = format_item_line_html(l, inline=True)
        entry = f"{i}. {formatted}" if numbered else formatted
        if out and total_len + len(entry) > max_chars:
            break
        out.append(entry)
        total_len += len(entry)
        shown = i
    return ("\n".join(out) if out else "—"), shown, len(lines)


def build_delivery_xlsx(items: list[str], product_name: str) -> BufferedInputFile:
    """একাধিক পিস কেনা হলে — এডমিনের আপলোড করা হেডিং অনুযায়ী কলামসহ xlsx বানায়।"""
    parsed = [parse_item_line(line) for line in items]
    headers: list[str] = []
    for d in parsed:
        for k in d:
            if k not in headers:
                headers.append(k)
    wb = Workbook()
    ws = wb.active
    ws.title = "delivery"
    ws.append(headers)
    for d in parsed:
        ws.append([d.get(h, "") for h in headers])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", product_name.strip()) or "delivery"
    return BufferedInputFile(bio.read(), filename=f"{safe_name}_delivery.xlsx")


class PurchaseError(Exception):
    pass


async def process_purchase(user_id: int, product_id: int, qty: int) -> dict:
    """মূল পারচেজ লজিক — ব্যালেন্স কেটে নেয়, ইনভেন্টরি sold করে, অর্ডার বানায়,
    এবং ডেলিভারির জন্য দরকারি সব ডেটা রিটার্ন করে। সমস্যা হলে PurchaseError রেইজ করে।"""
    async with get_lock(f"product:{product_id}"), get_lock(f"user:{user_id}"):
        async with SessionLocal() as s:
            p = await s.get(Product, product_id)
            user = await s.get(User, user_id)
            if not p or not p.is_active:
                raise PurchaseError("❌ Product unavailable.")

            items = (await s.execute(
                select(InventoryItem)
                .where(InventoryItem.product_id == product_id, InventoryItem.status == "available")
                .order_by(InventoryItem.id).limit(qty)
            )).scalars().all()

            if len(items) < qty:
                raise PurchaseError(f"❌ Only {len(items)} in stock now.")

            total = round(p.price * qty, 2)
            if user.balance < total:
                raise PurchaseError("❌ Insufficient balance. Please deposit first.")

            order_id = gen_order_id()
            delivered_lines = []
            for it in items:
                it.status = "sold"
                it.sold_at = datetime.utcnow()
                it.order_id = order_id
                delivered_lines.append(it.data)
            delivered_text = "\n".join(delivered_lines)

            user.balance = round(user.balance - total, 2)
            user.total_spent = round(user.total_spent + total, 2)
            user.total_orders += 1

            order = Order(
                order_id=order_id, user_id=user.id, product_id=p.id, product_name=p.name,
                quantity=qty, total_price=total, delivered_data=delivered_text,
            )
            s.add(order)
            s.add(Transaction(
                user_id=user.id, type="purchase", amount=-total,
                balance_after=user.balance, note=f"Order {order_id} — {p.name} x{qty}",
            ))
            await s.commit()

            remaining = (await s.execute(
                select(func.count()).select_from(InventoryItem)
                .where(InventoryItem.product_id == product_id, InventoryItem.status == "available")
            )).scalar_one()
            threshold = int(await get_setting(s, "low_stock_threshold", "5"))

            return {
                "order_id": order_id,
                "product_name": p.name,
                "qty": qty,
                "total": total,
                "new_balance": user.balance,
                "items": delivered_lines,
                "remaining": remaining,
                "threshold": threshold,
            }


def format_purchase_summary(result: dict) -> str:
    return (
        f"✅ <b>Purchase Successful!</b>\n\n"
        f"🆔 Order: <code>{result['order_id']}</code>\n"
        f"📦 {result['product_name']} x{result['qty']}\n"
        f"💰 Total: {dual_money(result['total'])}\n"
        f"💳 New Balance: {dual_money(result['new_balance'])}\n"
    )


@router.callback_query(BuyCB.filter())
async def cb_buy(call: CallbackQuery, callback_data: BuyCB, db_user: User, state: FSMContext):
    action, pid, qty = callback_data.action, callback_data.id, callback_data.qty

    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        if not p or not p.is_active:
            await call.answer("Product unavailable.", show_alert=True)
            return
        stock = (await s.execute(
            select(func.count()).select_from(InventoryItem)
            .where(InventoryItem.product_id == pid, InventoryItem.status == "available")
        )).scalar_one()

    if action == "cancel":
        await safe_edit(call, "❌ Purchase cancelled.", kb_product_detail(pid))
        await call.answer()
        return

    if action == "inc":
        qty = min(qty + 1, max(stock, 1))
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>{qty}</b>\nTotal: {dual_money(p.price * qty)}", kb_qty(pid, qty))
        await call.answer()
        return

    if action == "dec":
        qty = max(qty - 1, 1)
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>{qty}</b>\nTotal: {dual_money(p.price * qty)}", kb_qty(pid, qty))
        await call.answer()
        return

    if action == "start":
        if stock <= 0:
            await call.answer("Out of stock!", show_alert=True)
            return
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>1</b>\nTotal: {dual_money(p.price)}", kb_qty(pid, 1))
        await call.answer()
        return

    if action == "typeqty":
        if stock <= 0:
            await call.answer("Out of stock!", show_alert=True)
            return
        await state.set_state(BuyStates.quantity)
        await state.update_data(buy_product_id=pid)
        await safe_edit(
            call,
            f"🛒 <b>{p.name}</b>\n\n📦 In stock: {stock}\n\n"
            f"কতগুলো (pcs) কিনতে চান, সংখ্যা লিখে পাঠান (যেমন: 13):",
            None,
        )
        await call.answer()
        return

    if action == "confirm":
        await do_purchase(call, db_user, pid, qty)
        return


@router.message(StateFilter(BuyStates.quantity))
async def msg_buy_quantity(message: Message, state: FSMContext, db_user: User):
    txt = (message.text or "").strip()
    try:
        qty = int(txt)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ সঠিক একটা সংখ্যা লিখুন (যেমন: 13)।")
        return

    data = await state.get_data()
    pid = data.get("buy_product_id")
    await state.clear()

    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        if not p or not p.is_active:
            await message.answer("❌ Product unavailable.")
            return
        stock = (await s.execute(
            select(func.count()).select_from(InventoryItem)
            .where(InventoryItem.product_id == pid, InventoryItem.status == "available")
        )).scalar_one()

    if qty > stock:
        await message.answer(f"❌ মাত্র {stock}টি স্টকে আছে। কম সংখ্যা লিখে আবার চেষ্টা করুন।")
        return

    try:
        result = await process_purchase(db_user.id, pid, qty)
    except PurchaseError as e:
        await message.answer(str(e))
        return

    summary = format_purchase_summary(result)
    if result["qty"] == 1:
        item_text = format_item_line_html(result["items"][0])
        await message.answer(f"{summary}\n📄 <b>Your item:</b>\n\n{item_text[:3500]}")
    else:
        await message.answer(f"{summary}\n📄 আপনার ফাইল নিচে পাঠানো হলো:")
        doc = build_delivery_xlsx(result["items"], result["product_name"])
        await message.answer_document(doc)

    if result["remaining"] <= result["threshold"]:
        await broadcast_to_admins(f"⚠️ <b>Low stock!</b>\n\n{result['product_name']}: only {result['remaining']} left.")


async def do_purchase(call: CallbackQuery, db_user: User, product_id: int, qty: int):
    try:
        result = await process_purchase(db_user.id, product_id, qty)
    except PurchaseError as e:
        await call.answer(str(e), show_alert=True)
        return

    summary = format_purchase_summary(result)
    if result["qty"] == 1:
        item_text = format_item_line_html(result["items"][0])
        await safe_edit(call, f"{summary}\n📄 <b>Your item:</b>\n\n{item_text[:3500]}", kb_back("home"))
        await call.answer("✅ Purchased!")
    else:
        await safe_edit(call, f"{summary}\n📄 আপনার ফাইল নিচে পাঠানো হলো:", kb_back("home"))
        await call.answer("✅ Purchased!")
        doc = build_delivery_xlsx(result["items"], result["product_name"])
        await call.message.answer_document(doc)

    if result["remaining"] <= result["threshold"]:
        await broadcast_to_admins(f"⚠️ <b>Low stock!</b>\n\n{result['product_name']}: only {result['remaining']} left.")


# =====================================================================
# MY ORDERS
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "orders"))
async def cb_orders(call: CallbackQuery):
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Order).where(Order.user_id == call.from_user.id).order_by(Order.id.desc()).limit(10)
        )).scalars().all()
    if not rows:
        text = "📦 <b>My Orders</b>\n\nYou have no orders yet."
    else:
        lines = ["📦 <b>My Orders (last 10)</b>\n"]
        for o in rows:
            lines.append(f"• {o.order_id} | {o.product_name} x{o.quantity} | {dual_money(o.total_price)} | {o.created_at.strftime('%d/%m/%Y')}")
        text = "\n".join(lines)
    await safe_edit(call, text, kb_back("home"))
    await call.answer()


# =====================================================================
# SELL FLOW
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "sell"))
async def cb_sell(call: CallbackQuery):
    async with SessionLocal() as s:
        products = (await s.execute(
            select(Product).where(Product.is_active == True, Product.seller_price > 0)
        )).scalars().all()
    if not products:
        await safe_edit(call, "💼 <b>Sell Mail</b>\n\nNo products are open for selling right now.", kb_back("home"))
        await call.answer()
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p.name} — pays {dual_money(p.seller_price)}/item", callback_data=SellProdCB(id=p.id))
    b.adjust(1)
    await safe_edit(call, "💼 <b>Sell Mail</b>", b.as_markup())
    await call.answer()


@router.callback_query(SellProdCB.filter())
async def cb_sell_pick_product(call: CallbackQuery, callback_data: SellProdCB, state: FSMContext):
    await state.update_data(sell_product_id=callback_data.id)
    await state.set_state(SellStates.waiting_data)
    await safe_edit(
        call,
        "📝 আপনার ডেটা পাঠান —\n\n"
        "• <b>টেক্সট</b> হিসেবে (এক লাইনে একটা আইটেম):\n"
        "<code>mail1@example.com:pass1\nmail2@example.com:pass2</code>\n\n"
        "• অথবা সরাসরি একটি <b>.xlsx</b> ফাইল পাঠান (প্রথম সারি = হেডিং, যেমন mail, pass, fulldata)।",
        None,
    )
    await call.answer()


@router.message(StateFilter(SellStates.waiting_data), F.document)
async def msg_sell_data_xlsx(message: Message, state: FSMContext, db_user: User):
    doc: Document = message.document
    if not doc.file_name.lower().endswith((".xlsx", ".xlsm")):
        await message.answer("❌ শুধু .xlsx ফাইল সাপোর্ট করে। আবার পাঠান, অথবা টেক্সট আকারে ডেটা দিন।")
        return
    if doc.file_size and doc.file_size > MAX_XLSX_SIZE_MB * 1024 * 1024:
        await message.answer(f"❌ ফাইলটা অনেক বড়। সর্বোচ্চ {MAX_XLSX_SIZE_MB}MB।")
        return

    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    raw_bytes = buf.read()

    try:
        headers, rows, blanks = parse_xlsx_rows(raw_bytes)
    except Exception as e:
        await message.answer(f"❌ ফাইলটা পড়া গেলো না: {e}")
        return

    if not headers or not rows:
        await message.answer("❌ ফাইলে ব্যবহারযোগ্য কোনো ডেটা পাওয়া যায়নি।")
        return

    lines = [row_to_line(headers, row) for row in rows]

    data = await state.get_data()
    pid = data.get("sell_product_id")
    await state.clear()

    async with SessionLocal() as s:
        p = await s.get(Product, pid) if pid else None
        sub = SellerSubmission(
            user_id=db_user.id, product_id=pid, product_name=(p.name if p else None),
            raw_data="\n".join(lines)[:8000], item_count=len(lines), status="pending",
            file_id=doc.file_id, file_name=doc.file_name,
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)

    await message.answer(
        f"⏳ <b>Submission received!</b>\n\n📦 Product: {sub.product_name or '—'}\n🔢 Items: {len(lines)}\n\n"
        "An admin will review it soon."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="👁 View Submission", callback_data=SellReviewCB(action="view", id=sub.id))
    kb.adjust(1)
    await broadcast_to_admins(
        f"💼 <b>New Seller Submission — {sub.product_name or '—'} (XLSX)</b>\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"🔢 Items: {len(lines)}\n"
        f"🆔 Ref: SUB-{sub.id}",
        kb.as_markup(),
    )
    # আসল ফাইলটাও এডমিনদের কাছে সরাসরি পাঠিয়ে দেওয়া হচ্ছে
    await broadcast_document_to_admins(doc.file_id, caption=f"📎 Raw file — {sub.product_name or '—'} (SUB-{sub.id}, from {db_user.id})")


@router.message(StateFilter(SellStates.waiting_data))
async def msg_sell_data(message: Message, state: FSMContext, db_user: User):
    data = await state.get_data()
    pid = data.get("sell_product_id")
    raw = (message.text or "").strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        await message.answer("❌ কোনো বৈধ লাইন পাওয়া যায়নি। আবার ডেটা পাঠান, অথবা .xlsx ফাইল পাঠান।")
        return
    await state.clear()

    async with SessionLocal() as s:
        p = await s.get(Product, pid) if pid else None
        sub = SellerSubmission(
            user_id=db_user.id, product_id=pid, product_name=(p.name if p else None),
            raw_data=raw[:8000], item_count=len(lines), status="pending",
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)

    await message.answer(
        f"⏳ <b>Submission received!</b>\n\n📦 Product: {sub.product_name or '—'}\n🔢 Items: {len(lines)}\n\n"
        "An admin will review it soon."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 View Submission", callback_data=SellReviewCB(action="view", id=sub.id))
    kb.adjust(1)
    await broadcast_to_admins(
        f"💼 <b>New Seller Submission — {sub.product_name or '—'}</b>\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"🔢 Items: {len(lines)}\n"
        f"🆔 Ref: SUB-{sub.id}",
        kb.as_markup(),
    )


@router.callback_query(SellReviewCB.filter(F.action == "view"))
async def cb_sell_review_view(call: CallbackQuery, callback_data: SellReviewCB):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    async with SessionLocal() as s:
        sub = await s.get(SellerSubmission, callback_data.id)
    if not sub:
        await call.answer("Not found.", show_alert=True)
        return
    if sub.status != "pending":
        await call.answer("Already processed!", show_alert=True)
        return

    lines = [l.strip() for l in sub.raw_data.splitlines() if l.strip()]
    preview, shown, total = build_item_preview_html(lines, numbered=True, max_chars=3000)
    more_note = f"\n\n… +{total - shown} more line(s) not shown" if shown < total else ""

    text = (
        f"💼 <b>{sub.product_name or '—'}</b>\n\n"
        f"👤 User: <code>{sub.user_id}</code>\n"
        f"🔢 Total items submitted: {sub.item_count}\n"
        f"🆔 Ref: SUB-{sub.id}\n\n"
        f"{preview}{more_note}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approve", callback_data=SellReviewCB(action="approve", id=sub.id))
    b.button(text="❌ Reject", callback_data=SellReviewCB(action="reject", id=sub.id))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Back", callback_data=AListCB(kind="sells").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(SellReviewCB.filter(F.action.in_({"approve", "reject"})))
async def cb_sell_review(call: CallbackQuery, callback_data: SellReviewCB, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    sub_id = callback_data.id
    async with SessionLocal() as s:
        sub = await s.get(SellerSubmission, sub_id)
    if not sub:
        await call.answer("Not found.", show_alert=True)
        return
    if sub.status != "pending":
        await call.answer("Already processed!", show_alert=True)
        return

    if callback_data.action == "reject":
        async with get_lock(f"sub:{sub_id}"):
            async with SessionLocal() as s:
                sub = await s.get(SellerSubmission, sub_id)
                if not sub or sub.status != "pending":
                    await call.answer("Already processed!", show_alert=True)
                    return
                user = await s.get(User, sub.user_id)
                sub.status = "rejected"
                sub.reviewed_at = datetime.utcnow()
                sub.reviewed_by = call.from_user.id
                await s.commit()
                await log_admin(s, call.from_user.id, "sell_reject", f"sub_id={sub.id}")
            try:
                await bot.send_message(user.id, f"❌ <b>Your submission for {sub.product_name or 'your product'} (SUB-{sub.id}) was rejected.</b>")
            except Exception:
                pass
        await call.answer("Rejected.")
        await safe_edit(call, f"❌ {sub.product_name or ('SUB-' + str(sub_id))} rejected.", kb_admin_back())
        return

    # approve → এডমিনকে জিজ্ঞেস করা হচ্ছে কত pcs approve/confirm করতে চান
    await state.set_state(AdminSellApproveStates.waiting_pcs)
    await state.update_data(approve_sub_id=sub_id)
    await safe_edit(
        call,
        f"✅ <b>{sub.product_name or '—'}</b> (SUB-{sub_id}) — মোট {sub.item_count} pcs জমা দিয়েছে।\n\n"
        f"কত <b>pcs Approve / Confirm</b> করতে চান তা লিখুন (সংখ্যা, সর্বোচ্চ {sub.item_count}):",
        kb_admin_back(),
    )
    await call.answer()


@router.message(StateFilter(AdminSellApproveStates.waiting_pcs))
async def msg_sell_approve_pcs(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    sub_id = data.get("approve_sub_id")
    try:
        pcs = int(message.text.strip())
        if pcs <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ সঠিক একটা সংখ্যা লিখুন (যেমন: 5)।")
        return

    async with get_lock(f"sub:{sub_id}"):
        async with SessionLocal() as s:
            sub = await s.get(SellerSubmission, sub_id)
            if not sub:
                await state.clear()
                await message.answer("❌ Submission not found.", reply_markup=kb_admin_back())
                return
            if sub.status != "pending":
                await state.clear()
                await message.answer("⚠️ এই সাবমিশনটা আগেই প্রসেস হয়ে গেছে।", reply_markup=kb_admin_back())
                return
            if pcs > sub.item_count:
                await message.answer(f"❌ {sub.item_count}-এর বেশি হতে পারবে না। আবার একটা সংখ্যা লিখুন:")
                return

            await state.clear()
            user = await s.get(User, sub.user_id)
            lines = [l.strip() for l in sub.raw_data.splitlines() if l.strip()][:pcs]
            product = await s.get(Product, sub.product_id) if sub.product_id else None
            seller_price = product.seller_price if product else 0.0
            added, dup = 0, 0
            for line in lines:
                h = data_hash(line)
                exists = (await s.execute(
                    select(InventoryItem.id).where(InventoryItem.data_hash == h)
                )).first()
                if exists:
                    dup += 1
                    continue
                s.add(InventoryItem(
                    product_id=sub.product_id or 0, data=line, data_hash=h, status="available",
                ))
                added += 1
            credit = round(seller_price * added, 2)
            sub.status = "approved"
            sub.credited_amount = credit
            sub.reviewed_at = datetime.utcnow()
            sub.reviewed_by = message.from_user.id
            await s.commit()
            if credit > 0:
                await add_balance(s, user, credit, "sell_credit", f"Submission SUB-{sub.id} ({pcs} pcs confirmed)")
            await log_admin(s, message.from_user.id, "sell_approve", f"sub_id={sub.id} pcs={pcs} added={added}")

    try:
        await bot.send_message(
            user.id,
            f"✅ <b>Your submission for {sub.product_name or 'your product'} was approved!</b>\n\n"
            f"📦 Confirmed: {pcs} pcs (of {sub.item_count} submitted)\n"
            f"➕ Added to stock: {added} ({dup} duplicate(s) skipped)\n"
            f"💰 Credited: {dual_money(credit)}",
        )
    except Exception:
        pass

    await message.answer(
        f"✅ Done. {sub.product_name or ('SUB-' + str(sub_id))}: {pcs} pcs confirmed → {added} added to stock ({dup} duplicate skipped).\n"
        f"💰 Credited: {dual_money(credit)}",
        reply_markup=kb_admin_back(),
    )

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer("Done!")


# =====================================================================
# ADMIN — DASHBOARD
# =====================================================================
async def admin_dashboard_text() -> str:
    async with SessionLocal() as s:
        users_n = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        products_n = (await s.execute(select(func.count()).select_from(Product))).scalar_one()
        avail_n = (await s.execute(select(func.count()).select_from(InventoryItem).where(InventoryItem.status == "available"))).scalar_one()
        sold_n = (await s.execute(select(func.count()).select_from(InventoryItem).where(InventoryItem.status == "sold"))).scalar_one()
        orders_n = (await s.execute(select(func.count()).select_from(Order))).scalar_one()
        sales_sum = (await s.execute(select(func.coalesce(func.sum(Order.total_price), 0.0)))).scalar_one()
        deposits_sum = (await s.execute(select(func.coalesce(func.sum(Deposit.amount), 0.0)).where(Deposit.status == "approved"))).scalar_one()
        pending_sells = (await s.execute(select(func.count()).select_from(SellerSubmission).where(SellerSubmission.status == "pending"))).scalar_one()
        pending_deps = (await s.execute(select(func.count()).select_from(Deposit).where(Deposit.status == "pending"))).scalar_one()
        maint = await is_maintenance_on(s)
    return (
        "🛠 <b>Admin Dashboard</b>\n\n"
        f"👥 Users: {users_n}\n"
        f"📦 Products: {products_n}\n"
        f"✅ Available Stock: {avail_n}\n"
        f"🔴 Sold Stock: {sold_n}\n"
        f"🧾 Orders: {orders_n}\n"
        f"💵 Total Sales: {dual_money(sales_sum)}\n"
        f"💰 Total Deposits (approved): {dual_money(deposits_sum)}\n"
        f"⏳ Pending Deposits: {pending_deps}\n"
        f"⏳ Pending Seller Requests: {pending_sells}\n"
        f"🔧 Maintenance Mode: {'ON 🔴' if maint else 'OFF 🟢'}"
    )


@router.callback_query(NavCB.filter(F.to == "admin"))
async def cb_admin_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    await safe_edit(call, await admin_dashboard_text(), kb_admin_home())
    await call.answer()


async def log_admin(session: AsyncSession, admin_id: int, action: str, details: str = ""):
    session.add(AdminLog(admin_id=admin_id, action=action, details=details[:250]))
    await session.commit()


async def broadcast_to_admins(text: str, markup: Optional[InlineKeyboardMarkup] = None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=markup)
        except Exception:
            pass


async def broadcast_document_to_admins(file_id: str, caption: str = ""):
    """ইউজারের পাঠানো xlsx ফাইলটা হুবহু সব এডমিনের কাছে ফরওয়ার্ড করে।"""
    for aid in ADMIN_IDS:
        try:
            await bot.send_document(aid, file_id, caption=caption)
        except Exception:
            pass


def admin_only(call: CallbackQuery) -> bool:
    return is_admin(call.from_user.id)


# =====================================================================
# ADMIN — PRODUCTS
# =====================================================================
@router.callback_query(AListCB.filter(F.kind == "aprod"))
async def cb_admin_products(call: CallbackQuery, callback_data: AListCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    page = callback_data.page
    async with SessionLocal() as s:
        rows = (await s.execute(select(Product).order_by(Product.id.desc()))).scalars().all()
    chunk = rows[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    has_next = len(rows) > (page + 1) * PAGE_SIZE

    b = InlineKeyboardBuilder()
    for p in chunk:
        flag = "🟢" if p.is_active else "🔴"
        if p.is_buy_listing:
            b.button(text=f"{flag} 🛍 {p.name} — {dual_money(p.seller_price)}", callback_data=AProdCB(action="view", id=p.id))
        else:
            b.button(text=f"{flag} {p.name} — {dual_money(p.price)}", callback_data=AProdCB(action="view", id=p.id))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="aprod", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="aprod", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="➕ Add Product", callback_data=AProdCB(action="add").pack()))
    b.row(InlineKeyboardButton(text="🛍 Add Buy Listing", callback_data=AProdCB(action="addbuy").pack()))
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))

    text = "📦 <b>Products</b>\n\n" + ("No products yet." if not chunk else "Tap a product to manage it.")
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AProdCB.filter(F.action == "add"))
async def cb_admin_add_product_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminProductStates.add_name)
    await safe_edit(call, "🏷 Enter the <b>product name</b> (this becomes its button):", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminProductStates.add_name))
async def msg_add_name(message: Message, state: FSMContext):
    await state.update_data(new_name=message.text.strip()[:128])
    await state.set_state(AdminProductStates.add_price)
    await message.answer("💰 Enter the <b>sell price</b> (number, ৳):")


@router.message(StateFilter(AdminProductStates.add_price))
async def msg_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid price. Enter a number.")
        return
    await state.update_data(new_price=price)
    await state.set_state(AdminProductStates.add_desc)
    await message.answer("📝 Enter a short <b>description</b> (or send <code>skip</code> to leave it empty):")


@router.message(StateFilter(AdminProductStates.add_desc))
async def msg_add_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    if desc.lower() in ("skip", "-"):
        desc = ""
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as s:
        p = Product(
            category="General", name=data["new_name"], price=data["new_price"],
            seller_price=0.0, description=desc[:500], is_active=True,
        )
        s.add(p)
        await s.commit()
        await log_admin(s, message.from_user.id, "add_product", p.name)
    await message.answer(f"✅ Product <b>{data['new_name']}</b> added!", reply_markup=kb_admin_back())


# =====================================================================
# ADMIN — ADD BUY LISTING (এডমিন যেই প্রোডাক্ট ইউজারদের থেকে কিনতে চায়,
# শুধু নাম + দাম দিয়ে অ্যাড করবে — এটা মার্কেটপ্লেসে দেখাবে না, শুধু
# "💼 Sell Mail" মেনুতে দেখাবে যাতে ইউজাররা এটা বিক্রি করতে পারে)
# =====================================================================
@router.callback_query(AProdCB.filter(F.action == "addbuy"))
async def cb_admin_add_buy_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminProductStates.add_buy_name)
    await safe_edit(call, "🛍 Enter the <b>product name</b> you want to buy from users (this becomes its button):", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminProductStates.add_buy_name))
async def msg_add_buy_name(message: Message, state: FSMContext):
    await state.update_data(new_buy_name=message.text.strip()[:128])
    await state.set_state(AdminProductStates.add_buy_price)
    await message.answer("💰 Enter the <b>price you'll pay per item</b> (number, ৳):")


@router.message(StateFilter(AdminProductStates.add_buy_price))
async def msg_add_buy_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid price. Enter a number.")
        return
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as s:
        p = Product(
            category="General", name=data["new_buy_name"], price=0.0,
            seller_price=price, description="", is_active=True, is_buy_listing=True,
        )
        s.add(p)
        await s.commit()
        await log_admin(s, message.from_user.id, "add_buy_listing", p.name)
    await message.answer(
        f"✅ Buy listing <b>{data['new_buy_name']}</b> added! It'll now show up under 💼 Sell Mail for users.",
        reply_markup=kb_admin_back(),
    )


@router.callback_query(AProdCB.filter(F.action == "view"))
async def cb_admin_product_view(call: CallbackQuery, callback_data: AProdCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        p = await s.get(Product, callback_data.id)
        if not p:
            await call.answer("Not found.", show_alert=True); return
        stock = (await s.execute(
            select(func.count()).select_from(InventoryItem)
            .where(InventoryItem.product_id == p.id, InventoryItem.status == "available")
        )).scalar_one()
    if p.is_buy_listing:
        text = (
            f"🛍 <b>{p.name}</b> (Buy Listing)\n\n"
            f"💼 Buy price (paid to seller): {dual_money(p.seller_price)}/item\n"
            f"📦 Stock: {stock}\n"
            f"🔘 Status: {'Active' if p.is_active else 'Disabled'}\n"
        )
    else:
        text = (
            f"🏷 <b>{p.name}</b>\n\n"
            f"💰 Price: {dual_money(p.price)}\n"
            f"💼 Seller payout: {dual_money(p.seller_price)}/item\n"
            f"📦 Stock: {stock}\n"
            f"🔘 Status: {'Active' if p.is_active else 'Disabled'}\n"
            f"📝 {p.description or '—'}"
        )
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Edit Price", callback_data=AProdCB(action="edit_price", id=p.id))
    b.button(text="🔁 Toggle Active", callback_data=AProdCB(action="toggle", id=p.id))
    b.button(text="📥 Import Stock (XLSX)", callback_data=AXlsxCB(action="pick_product", id=p.id))
    b.button(text="📤 Export Stock (XLSX)", callback_data=AXlsxCB(action="export", id=p.id))
    b.button(text="🗑 Delete Product", callback_data=AProdCB(action="delete", id=p.id))
    b.adjust(2, 2, 1)
    b.row(InlineKeyboardButton(text="⬅️ Products", callback_data=AListCB(kind="aprod").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AProdCB.filter(F.action == "toggle"))
async def cb_admin_product_toggle(call: CallbackQuery, callback_data: AProdCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        p = await s.get(Product, callback_data.id)
        if not p:
            await call.answer("Not found.", show_alert=True); return
        p.is_active = not p.is_active
        await s.commit()
    await call.answer("Updated!")
    await cb_admin_product_view(call, callback_data)


@router.callback_query(AProdCB.filter(F.action == "delete"))
async def cb_admin_product_delete(call: CallbackQuery, callback_data: AProdCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        p = await s.get(Product, callback_data.id)
        if p:
            await s.execute(delete(InventoryItem).where(InventoryItem.product_id == p.id))
            await s.delete(p)
            await s.commit()
            await log_admin(s, call.from_user.id, "delete_product", p.name)
    await safe_edit(call, "🗑 Product deleted.", kb_admin_home())
    await call.answer()


@router.callback_query(AProdCB.filter(F.action == "edit_price"))
async def cb_admin_edit_price_start(call: CallbackQuery, callback_data: AProdCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminProductStates.edit_price)
    await state.update_data(edit_product_id=callback_data.id)
    await safe_edit(call, "💰 Enter the new price (number):", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminProductStates.edit_price))
async def msg_edit_price(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("edit_product_id")
    try:
        price = float(message.text.strip().replace(",", "."))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid price.")
        return
    await state.clear()
    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        if p:
            p.price = price
            await s.commit()
    await message.answer("✅ Price updated!", reply_markup=kb_admin_back())


# =====================================================================
# ADMIN — XLSX IMPORT / EXPORT
# =====================================================================
def parse_xlsx_rows(raw_bytes: bytes) -> tuple[list[str], list[dict], int]:
    """Returns (headers, rows_as_dicts, blank_row_count)."""
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], [], 0
    headers = [str(h).strip() if h is not None else f"col{i+1}" for i, h in enumerate(header_row)]
    out = []
    blanks = 0
    for row in rows_iter:
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            blanks += 1
            continue
        d = {}
        for i, h in enumerate(headers):
            val = row[i] if i < len(row) else None
            d[h] = "" if val is None else str(val).strip()
        out.append(d)
    return headers, out, blanks


def row_to_line(headers: list[str], row: dict) -> str:
    return " | ".join(f"{h}: {row.get(h, '')}" for h in headers)


@router.callback_query(AXlsxCB.filter(F.action == "pick_product"))
async def cb_xlsx_pick_product(call: CallbackQuery, callback_data: AXlsxCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminXlsxStates.waiting_file)
    await state.update_data(xlsx_product_id=callback_data.id)
    await safe_edit(call, "📥 Send the <b>.xlsx</b> file now (first row = headers).", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminXlsxStates.waiting_file), F.document)
async def msg_xlsx_file(message: Message, state: FSMContext):
    doc: Document = message.document
    if not doc.file_name.lower().endswith((".xlsx", ".xlsm")):
        await message.answer("❌ Please send a valid .xlsx file.")
        return
    if doc.file_size and doc.file_size > MAX_XLSX_SIZE_MB * 1024 * 1024:
        await message.answer(f"❌ File too large. Max {MAX_XLSX_SIZE_MB}MB.")
        return

    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    raw_bytes = buf.read()

    try:
        headers, rows, blanks = parse_xlsx_rows(raw_bytes)
    except Exception as e:
        await message.answer(f"❌ Could not read the file: {e}")
        return

    if not headers or not rows:
        await message.answer("❌ No usable data found in the file.")
        return

    data = await state.get_data()
    pid = data.get("xlsx_product_id")

    async with SessionLocal() as s:
        existing_hashes = set((await s.execute(select(InventoryItem.data_hash))).scalars().all())

    valid, dup = [], 0
    seen_in_file = set()
    for row in rows:
        line = row_to_line(headers, row)
        h = data_hash(line)
        if h in existing_hashes or h in seen_in_file:
            dup += 1
            continue
        seen_in_file.add(h)
        valid.append(line)

    await state.update_data(xlsx_valid_lines=valid, xlsx_headers=headers)
    await state.set_state(None)

    preview, _shown, _total = build_item_preview_html(valid[:5], numbered=False, max_chars=1600)
    text = (
        f"📊 <b>Import Preview</b>\n\n"
        f"🗂 Headers: {', '.join(headers)}\n"
        f"✅ Valid rows: {len(valid)}\n"
        f"♻️ Duplicates skipped: {dup}\n"
        f"🚫 Blank rows skipped: {blanks}\n\n"
        f"<b>Sample:</b>\n{preview}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm Import", callback_data=AXlsxCB(action="confirm", id=pid))
    b.button(text="❌ Cancel", callback_data=AXlsxCB(action="cancel", id=pid))
    b.adjust(2)
    await message.answer(text, reply_markup=b.as_markup())


@router.callback_query(AXlsxCB.filter(F.action == "confirm"))
async def cb_xlsx_confirm(call: CallbackQuery, callback_data: AXlsxCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    data = await state.get_data()
    valid = data.get("xlsx_valid_lines") or []
    pid = callback_data.id
    if not valid:
        await call.answer("Nothing to import.", show_alert=True)
        return
    async with SessionLocal() as s:
        for line in valid:
            s.add(InventoryItem(product_id=pid, data=line, data_hash=data_hash(line), status="available"))
        await s.commit()
        await log_admin(s, call.from_user.id, "xlsx_import", f"product_id={pid} count={len(valid)}")
    await state.clear()
    await safe_edit(call, f"✅ Imported <b>{len(valid)}</b> items into stock!", kb_admin_back())
    await call.answer()


@router.callback_query(AXlsxCB.filter(F.action == "cancel"))
async def cb_xlsx_cancel(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.clear()
    await safe_edit(call, "❌ Import cancelled.", kb_admin_back())
    await call.answer()


@router.callback_query(AXlsxCB.filter(F.action == "export"))
async def cb_xlsx_export(call: CallbackQuery, callback_data: AXlsxCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    pid = callback_data.id
    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        items = (await s.execute(select(InventoryItem).where(InventoryItem.product_id == pid))).scalars().all()

    wb = Workbook()
    ws = wb.active
    ws.title = "stock"
    ws.append(["id", "data", "status", "added_at", "sold_at", "order_id"])
    for it in items:
        ws.append([
            it.id, it.data, it.status,
            it.added_at.strftime("%d/%m/%Y %H:%M") if it.added_at else "",
            it.sold_at.strftime("%d/%m/%Y %H:%M") if it.sold_at else "",
            it.order_id or "",
        ])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"{(p.name if p else 'stock').replace(' ', '_')}_export.xlsx"
    await call.message.answer_document(BufferedInputFile(bio.read(), filename=fname))
    await call.answer("Exported!")


# =====================================================================
# ADMIN — STOCK / ORDERS LISTS
# =====================================================================
@router.callback_query(AListCB.filter(F.kind == "stock"))
async def cb_admin_stock(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Product.name, func.count(InventoryItem.id))
            .join(InventoryItem, InventoryItem.product_id == Product.id, isouter=True)
            .where((InventoryItem.status == "available") | (InventoryItem.status.is_(None)))
            .group_by(Product.name)
        )).all()
    if not rows:
        text = "📊 <b>Stock</b>\n\nNo stock data yet."
    else:
        lines = ["📊 <b>Available Stock by Product</b>\n"]
        for name, cnt in rows:
            lines.append(f"• {name}: {cnt}")
        text = "\n".join(lines)
    await safe_edit(call, text, kb_admin_back())
    await call.answer()


@router.callback_query(AListCB.filter(F.kind == "orders"))
async def cb_admin_orders(call: CallbackQuery, callback_data: AListCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    page = callback_data.page
    async with SessionLocal() as s:
        rows = (await s.execute(select(Order).order_by(Order.id.desc()))).scalars().all()
    chunk = rows[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    has_next = len(rows) > (page + 1) * PAGE_SIZE
    if not chunk:
        text = "🧾 <b>Orders</b>\n\nNo orders yet."
    else:
        lines = ["🧾 <b>Orders</b>\n"]
        for o in chunk:
            lines.append(f"• {o.order_id} | user {o.user_id} | {o.product_name} x{o.quantity} | {dual_money(o.total_price)}")
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="orders", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="orders", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AListCB.filter(F.kind == "deps"))
async def cb_admin_deposits(call: CallbackQuery, callback_data: AListCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    page = callback_data.page
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Deposit).where(Deposit.status == "pending").order_by(Deposit.id.desc())
        )).scalars().all()
    chunk = rows[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    has_next = len(rows) > (page + 1) * PAGE_SIZE
    if not chunk:
        text = "💰 <b>Pending Deposits</b>\n\nNone right now."
    else:
        lines = ["💰 <b>Pending Deposits</b>\n"]
        for d in chunk:
            lines.append(f"• DEP-{d.id} | user {d.user_id} | {d.method} | {dual_money(d.amount)} | Trx: {d.trx_id or '—'}")
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    for d in chunk:
        b.button(text=f"✅ DEP-{d.id}", callback_data=DepReviewCB(action="approve", id=d.id))
        b.button(text=f"❌ DEP-{d.id}", callback_data=DepReviewCB(action="reject", id=d.id))
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="deps", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="deps", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AListCB.filter(F.kind == "sells"))
async def cb_admin_sells(call: CallbackQuery, callback_data: AListCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    page = callback_data.page
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(SellerSubmission).where(SellerSubmission.status == "pending").order_by(SellerSubmission.id.desc())
        )).scalars().all()
    chunk = rows[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    has_next = len(rows) > (page + 1) * PAGE_SIZE

    header = f"💼 <b>Pending Seller Requests:</b> {len(rows)}"
    if not chunk:
        header += "\n\nNone right now."
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="sells", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="sells", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, header, b.as_markup())
    await call.answer()

    # প্রতিটা পেন্ডিং সাবমিশন — user, product, time, items, ডাটা/ফাইল একসাথে + Review/Reject বাটন
    async with SessionLocal() as s:
        for sub in chunk:
            seller = await s.get(User, sub.user_id)
            uname = f"@{seller.username}" if seller and seller.username else "—"
            time_str = sub.created_at.strftime("%Y-%m-%d %H:%M UTC") if sub.created_at else "—"
            caption = (
                f"💼 <b>{sub.product_name or '—'}</b>\n\n"
                f"👤 user: <code>{sub.user_id}</code>({uname})\n"
                f"🕒 time: {time_str}\n"
                f"🔢 Items: {sub.item_count} pcs\n"
                f"🆔 Ref: SUB-{sub.id}"
            )
            rb = InlineKeyboardBuilder()
            rb.button(text="✅ Review", callback_data=SellReviewCB(action="approve", id=sub.id))
            rb.button(text="❌ Reject", callback_data=SellReviewCB(action="reject", id=sub.id))
            rb.adjust(2)

            if sub.file_id:
                try:
                    await bot.send_document(
                        call.from_user.id, sub.file_id,
                        caption=f"{caption}\n📎 {sub.file_name or 'data.xlsx'}",
                        reply_markup=rb.as_markup(),
                    )
                    continue
                except Exception:
                    pass  # ফাইল পাঠানো না গেলে নিচের টেক্সট-প্রিভিউ ফলব্যাক হিসেবে যাবে

            lines = [l for l in sub.raw_data.splitlines() if l.strip()]
            preview, shown, total = build_item_preview_html(lines, numbered=True, max_chars=2500)
            more_note = f"\n\n… +{total - shown} more line(s) not shown" if shown < total else ""
            text = f"{caption}\n\nData:\n{preview}{more_note}"
            await bot.send_message(call.from_user.id, text, reply_markup=rb.as_markup())


# =====================================================================
# ADMIN — USERS (search / balance / ban)
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "admin_users"))
async def cb_admin_users(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminSearchStates.waiting_query)
    await safe_edit(call, "🔎 Send a user ID or @username to search:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminSearchStates.waiting_query))
async def msg_admin_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip().lstrip("@")
    async with SessionLocal() as s:
        if q.isdigit():
            user = await s.get(User, int(q))
        else:
            user = (await s.execute(select(User).where(User.username == q))).scalars().first()
    await state.clear()
    if not user:
        await message.answer("❌ User not found.", reply_markup=kb_admin_back())
        return
    text = (
        f"👤 <b>User {user.id}</b>\n\n"
        f"🔖 @{user.username or '—'} | {user.full_name or '—'}\n"
        f"💳 Balance: {dual_money(user.balance)}\n"
        f"⬆️ Deposited: {dual_money(user.total_deposit)}\n"
        f"⬇️ Spent: {dual_money(user.total_spent)}\n"
        f"🧾 Orders: {user.total_orders}\n"
        f"🔘 Status: {user.status}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Balance", callback_data=AUserCB(action="addbal", id=user.id))
    b.button(text="➖ Remove Balance", callback_data=AUserCB(action="rembal", id=user.id))
    if user.status == "banned":
        b.button(text="✅ Unban", callback_data=AUserCB(action="unban", id=user.id))
    else:
        b.button(text="🚫 Ban", callback_data=AUserCB(action="ban", id=user.id))
    b.adjust(2, 1)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await message.answer(text, reply_markup=b.as_markup())


# =====================================================================
# ADMIN — MESSAGE SPECIFIC USER (নির্দিষ্ট একজন ইউজারকে সরাসরি মেসেজ পাঠানো)
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "admin_msg_user"))
async def cb_admin_msg_user_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminMsgUserStates.waiting_id)
    await safe_edit(call, "🆔 যেই ইউজারকে মেসেজ পাঠাতে চান তার <b>User ID</b> লিখুন:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminMsgUserStates.waiting_id))
async def msg_admin_msg_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip().lstrip("@")
    if not q.isdigit():
        await message.answer("❌ সঠিক নাম্বার আকারে User ID দিন।", reply_markup=kb_admin_back())
        return
    uid = int(q)
    async with SessionLocal() as s:
        user = await s.get(User, uid)
    if not user:
        await state.clear()
        await message.answer("❌ User not found.", reply_markup=kb_admin_back())
        return
    await state.update_data(msg_target_id=uid)
    await state.set_state(AdminMsgUserStates.waiting_text)
    await message.answer(
        f"✍️ User <code>{uid}</code> (@{user.username or '—'} | {user.full_name or '—'}) কে যেই মেসেজ পাঠাতে চান, সেটা লিখুন:"
    )


@router.message(StateFilter(AdminMsgUserStates.waiting_text))
async def msg_admin_msg_user_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("msg_target_id")
    text = message.html_text or message.text or ""
    await state.clear()
    if not uid or not text.strip():
        await message.answer("❌ কিছু ভুল হয়েছে। আবার চেষ্টা করুন।", reply_markup=kb_admin_back())
        return
    ok = True
    try:
        await bot.send_message(uid, text)
    except Exception:
        ok = False
    async with SessionLocal() as s:
        await log_admin(s, message.from_user.id, "msg_user", f"user={uid}")
    if ok:
        await message.answer(f"✅ মেসেজ পাঠানো হয়েছে User <code>{uid}</code> কে।", reply_markup=kb_admin_back())
    else:
        await message.answer(
            f"❌ মেসেজ পাঠানো যায়নি (User <code>{uid}</code>) — সম্ভবত ইউজার বটকে ব্লক করে রেখেছেন।",
            reply_markup=kb_admin_back(),
        )


# =====================================================================
# ADMIN — BALANCE EDIT (সরাসরি User ID দিয়ে ব্যালান্স +/- করা)
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "admin_balance_edit"))
async def cb_admin_balance_edit_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminBalanceEditStates.waiting_id)
    await safe_edit(call, "🆔 যেই ইউজারের ব্যালান্স Edit করতে চান তার <b>User ID</b> লিখুন:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminBalanceEditStates.waiting_id))
async def msg_admin_balance_edit_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip().lstrip("@")
    if not q.isdigit():
        await message.answer("❌ সঠিক নাম্বার আকারে User ID দিন।", reply_markup=kb_admin_back())
        return
    uid = int(q)
    await state.clear()
    async with SessionLocal() as s:
        user = await s.get(User, uid)
    if not user:
        await message.answer("❌ User not found.", reply_markup=kb_admin_back())
        return
    text = (
        f"💳 <b>Balance Edit — User {uid}</b>\n\n"
        f"🔖 @{user.username or '—'} | {user.full_name or '—'}\n"
        f"💰 বর্তমান ব্যালান্স: {dual_money(user.balance)}\n\n"
        f"নিচে থেকে + অথবা − বাটনে ক্লিক করুন:"
    )
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Balance", callback_data=AUserCB(action="addbal", id=user.id))
    b.button(text="➖ Remove Balance", callback_data=AUserCB(action="rembal", id=user.id))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await message.answer(text, reply_markup=b.as_markup())


# =====================================================================
# ADMIN — USER INFO (user id দিয়ে একজন ইউজারের সব তথ্য এক জায়গায় দেখা)
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "user_info"))
async def cb_admin_user_info_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminSearchStates.waiting_info_id)
    await safe_edit(call, "🆔 ইউজারের <b>User ID</b> লিখুন:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminSearchStates.waiting_info_id))
async def msg_admin_user_info(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    q = message.text.strip().lstrip("@")
    if not q.isdigit():
        await message.answer("❌ সঠিক নাম্বার আকারে User ID দিন।", reply_markup=kb_admin_back())
        return
    uid = int(q)

    async with SessionLocal() as s:
        user = await s.get(User, uid)
        if not user:
            await message.answer("❌ User not found.", reply_markup=kb_admin_back())
            return

        dep_sum = (await s.execute(
            select(func.coalesce(func.sum(Deposit.amount), 0.0))
            .where(Deposit.user_id == uid, Deposit.status == "approved")
        )).scalar_one()
        dep_pending = (await s.execute(
            select(func.count()).select_from(Deposit)
            .where(Deposit.user_id == uid, Deposit.status == "pending")
        )).scalar_one()

        wd_sum = (await s.execute(
            select(func.coalesce(func.sum(Withdrawal.amount), 0.0))
            .where(Withdrawal.user_id == uid, Withdrawal.status == "approved")
        )).scalar_one()
        wd_pending = (await s.execute(
            select(func.count()).select_from(Withdrawal)
            .where(Withdrawal.user_id == uid, Withdrawal.status == "pending")
        )).scalar_one()

        sell_sum = (await s.execute(
            select(func.coalesce(func.sum(SellerSubmission.credited_amount), 0.0))
            .where(SellerSubmission.user_id == uid, SellerSubmission.status == "approved")
        )).scalar_one()
        sell_pending = (await s.execute(
            select(func.count()).select_from(SellerSubmission)
            .where(SellerSubmission.user_id == uid, SellerSubmission.status == "pending")
        )).scalar_one()

        recent_orders = (await s.execute(
            select(Order).where(Order.user_id == uid).order_by(Order.id.desc()).limit(5)
        )).scalars().all()

    lines = [
        f"🧑‍💻 <b>User Info — {uid}</b>\n",
        f"🔖 @{user.username or '—'} | {user.full_name or '—'}",
        f"🔘 Status: {user.status}",
        f"📅 Joined: {user.joined_at.strftime('%d/%m/%Y')}\n",
        f"💳 Balance: {dual_money(user.balance)}",
        f"⬆️ Total Deposited: {dual_money(user.total_deposit)}",
        f"⬇️ Total Spent: {dual_money(user.total_spent)}",
        f"🧾 Total Orders: {user.total_orders}\n",
        f"💰 Approved Deposits: {dual_money(dep_sum)} ({dep_pending} pending)",
        f"💸 Approved Withdrawals: {dual_money(wd_sum)} ({wd_pending} pending)",
        f"💼 Seller Credits: {dual_money(sell_sum)} ({sell_pending} pending)",
    ]
    if recent_orders:
        lines.append("\n🧾 <b>Recent Orders:</b>")
        for o in recent_orders:
            lines.append(f"• {o.order_id} | {o.product_name} x{o.quantity} | {dual_money(o.total_price)} | {o.created_at.strftime('%d/%m/%Y')}")
    else:
        lines.append("\n🧾 No orders yet.")
    text = "\n".join(lines)

    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Balance", callback_data=AUserCB(action="addbal", id=user.id))
    b.button(text="➖ Remove Balance", callback_data=AUserCB(action="rembal", id=user.id))
    if user.status == "banned":
        b.button(text="✅ Unban", callback_data=AUserCB(action="unban", id=user.id))
    else:
        b.button(text="🚫 Ban", callback_data=AUserCB(action="ban", id=user.id))
    b.adjust(2, 1)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await message.answer(text, reply_markup=b.as_markup())


@router.callback_query(AUserCB.filter(F.action.in_({"ban", "unban"})))
async def cb_admin_user_ban(call: CallbackQuery, callback_data: AUserCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        user = await s.get(User, callback_data.id)
        if not user:
            await call.answer("Not found.", show_alert=True); return
        user.status = "banned" if callback_data.action == "ban" else "active"
        await s.commit()
        await log_admin(s, call.from_user.id, callback_data.action, f"user={user.id}")
    await call.answer("Updated!")
    await safe_edit(call, f"✅ User {user.id} is now <b>{user.status}</b>.", kb_admin_back())


@router.callback_query(AUserCB.filter(F.action.in_({"addbal", "rembal"})))
async def cb_admin_user_balance_start(call: CallbackQuery, callback_data: AUserCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminBalanceStates.waiting_amount)
    await state.update_data(bal_user_id=callback_data.id, bal_action=callback_data.action)
    label = "add" if callback_data.action == "addbal" else "remove"
    await safe_edit(call, f"💰 Enter amount to {label} (number):", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminBalanceStates.waiting_amount))
async def msg_admin_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("bal_user_id")
    action = data.get("bal_action")
    try:
        amt = float(message.text.strip().replace(",", "."))
        if amt <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid number.")
        return
    await state.clear()
    signed = amt if action == "addbal" else -amt
    async with SessionLocal() as s:
        user = await s.get(User, uid)
        if not user:
            await message.answer("❌ User not found.")
            return
        new_bal = await add_balance(s, user, signed, "admin_add" if action == "addbal" else "admin_remove",
                                     f"By admin {message.from_user.id}")
        await log_admin(s, message.from_user.id, action, f"user={uid} amount={amt}")
    try:
        await bot.send_message(uid, f"💳 Your balance was adjusted by admin: {'+' if signed>0 else ''}{dual_money(signed)}. New balance: {dual_money(new_bal)}")
    except Exception:
        pass
    await message.answer(f"✅ Done. New balance: {dual_money(new_bal)}", reply_markup=kb_admin_back())


# =====================================================================
# ADMIN — BROADCAST
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "admin_broadcast"))
async def cb_admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminBroadcastStates.waiting_text)
    await safe_edit(call, "📢 Send the message you want to broadcast to all users:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminBroadcastStates.waiting_text))
async def msg_admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.html_text or message.text or ""
    await state.clear()
    async with SessionLocal() as s:
        ids = (await s.execute(select(User.id).where(User.status == "active"))).scalars().all()
    await message.answer(f"📢 Broadcasting to {len(ids)} users...")
    sent, failed = 0, 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except (TelegramForbiddenError, Exception):
            failed += 1
        await asyncio.sleep(0.05)  # gentle flood control
    await message.answer(f"✅ Broadcast done. Sent: {sent} | Failed: {failed}", reply_markup=kb_admin_back())


# =====================================================================
# ADMIN — SETTINGS
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "admin_settings"))
async def cb_admin_settings(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        maint = await is_maintenance_on(s)
        threshold = await get_setting(s, "low_stock_threshold", "5")
        methods = await get_setting(s, "deposit_methods", "")
        support = await get_setting(s, "support_contact", "")
        min_dep = await get_setting(s, "min_deposit_amount", "0")
        min_wd = await get_setting(s, "min_withdraw_amount", "0")
        binance_rate = await get_setting(s, "binance_usdt_rate", "127")
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🔧 Maintenance Mode: {'ON 🔴' if maint else 'OFF 🟢'}\n"
        f"📉 Low Stock Threshold: {threshold}\n"
        f"💳 Deposit Methods: {methods}\n"
        f"📞 Support Contact: {support}\n"
        f"⬆️ Min Deposit Amount: {dual_money(float(min_dep))}\n"
        f"⬇️ Min Withdrawal Amount: {dual_money(float(min_wd))}\n"
        f"🪙 Binance USDT Rate: 1 USDT = ৳{binance_rate}"
    )
    b = InlineKeyboardBuilder()
    b.button(text=("🔴 Turn Maintenance OFF" if maint else "🟢 Turn Maintenance ON"), callback_data=AToggleCB(key="maintenance_mode"))
    b.button(text="✏️ Set Low Stock Threshold", callback_data=NavCB(to="set_threshold"))
    b.button(text="✏️ Set Deposit Methods", callback_data=NavCB(to="set_methods"))
    b.button(text="✏️ Set Support Contact", callback_data=NavCB(to="set_support"))
    b.button(text="✏️ Set Min Deposit Amount", callback_data=NavCB(to="set_min_deposit"))
    b.button(text="✏️ Set Min Withdrawal Amount", callback_data=NavCB(to="set_min_withdraw"))
    b.button(text="✏️ Set Binance USDT Rate", callback_data=NavCB(to="set_binance_rate"))
    b.button(text="🔀 Withdraw Payment Methods", callback_data=NavCB(to="withdraw_methods_panel"))
    b.button(text="📮 Deposit Payment Numbers", callback_data=NavCB(to="deposit_numbers_panel"))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AToggleCB.filter(F.key == "maintenance_mode"))
async def cb_toggle_maintenance(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        cur = await is_maintenance_on(s)
        await set_setting(s, "maintenance_mode", "0" if cur else "1")
        await log_admin(s, call.from_user.id, "toggle_maintenance", str(not cur))
    await call.answer("Updated!")
    await cb_admin_settings(call)


@router.callback_query(NavCB.filter(F.to == "withdraw_methods_panel"))
async def cb_withdraw_methods_panel(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        raw = await get_setting(s, "withdraw_methods", ",".join(ALL_WITHDRAW_METHODS))
    enabled = {m.strip() for m in raw.split(",") if m.strip()}
    text = (
        "🔀 <b>Withdraw Payment Methods</b>\n\n"
        "যেকোনো মেথডে ক্লিক করে on/off করতে পারবেন। off করা মেথড দিয়ে ইউজাররা "
        "উইথড্র করতে পারবে না, বাকিগুলো দিয়ে ঠিকঠাক পারবে।\n\n"
    )
    for m in ALL_WITHDRAW_METHODS:
        text += f"{'🟢 ON ' if m in enabled else '🔴 OFF'} — {m}\n"
    b = InlineKeyboardBuilder()
    for m in ALL_WITHDRAW_METHODS:
        status = "🟢" if m in enabled else "🔴"
        b.button(text=f"{status} {m}", callback_data=WMToggleCB(method=m))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Settings", callback_data=NavCB(to="admin_settings").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(WMToggleCB.filter())
async def cb_toggle_withdraw_method(call: CallbackQuery, callback_data: WMToggleCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        raw = await get_setting(s, "withdraw_methods", ",".join(ALL_WITHDRAW_METHODS))
        enabled = [m.strip() for m in raw.split(",") if m.strip()]
        m = callback_data.method
        if m in enabled:
            enabled.remove(m)
            turned = "OFF"
        else:
            # ALL_WITHDRAW_METHODS-এর অর্ডার বজায় রেখে যোগ করা হচ্ছে
            enabled = [x for x in ALL_WITHDRAW_METHODS if x in enabled or x == m]
            turned = "ON"
        await set_setting(s, "withdraw_methods", ",".join(enabled))
        await log_admin(s, call.from_user.id, "toggle_withdraw_method", f"{m}={turned}")
    await call.answer(f"{m} turned {turned}!")
    await cb_withdraw_methods_panel(call)


@router.callback_query(NavCB.filter(F.to == "deposit_numbers_panel"))
async def cb_deposit_numbers_panel(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    async with SessionLocal() as s:
        methods_raw = await get_setting(s, "deposit_methods", "bKash,Nagad,Rocket,Binance")
        methods = [m.strip() for m in methods_raw.split(",") if m.strip()]
        numbers = {}
        for m in methods:
            numbers[m] = await get_setting(s, f"payment_number_{m}", "")
    text = (
        "📮 <b>Deposit Payment Numbers</b>\n\n"
        "প্রতিটা পেমেন্ট মেথডের জন্য আলাদা নাম্বার সেট করুন — ইউজার ডিপোজিট করার সময় "
        "এই নাম্বারটাই দেখতে পাবে।\n\n"
    )
    for m in methods:
        num = numbers[m]
        text += f"• <b>{m}</b>: {num if num else '⚠️ Not set'}\n"
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=f"✏️ {m}", callback_data=DepNumSetCB(method=m))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Settings", callback_data=NavCB(to="admin_settings").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(DepNumSetCB.filter())
async def cb_deposit_number_set_start(call: CallbackQuery, callback_data: DepNumSetCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminDepositNumberStates.waiting_value)
    await state.update_data(dep_number_method=callback_data.method)
    await safe_edit(
        call,
        f"📮 <b>{callback_data.method}</b> — এই মেথডে টাকা পাঠানোর জন্য যেই নাম্বার/অ্যাকাউন্ট ইউজারকে দেখাতে চান, তা লিখুন:\n\n"
        "(পরিষ্কার করতে <code>-</code> পাঠান)",
        kb_admin_back(),
    )
    await call.answer()


@router.message(StateFilter(AdminDepositNumberStates.waiting_value))
async def msg_deposit_number_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    method = data.get("dep_number_method")
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    await state.clear()
    async with SessionLocal() as s:
        await set_setting(s, f"payment_number_{method}", value)
        await log_admin(s, message.from_user.id, "set_deposit_number", f"{method}={value}")
    await message.answer(
        f"✅ <b>{method}</b> এর পেমেন্ট নাম্বার {'ক্লিয়ার করা হলো' if not value else 'সেভ হয়েছে'}!",
        reply_markup=kb_admin_back(),
    )


@router.callback_query(NavCB.filter(F.to.in_({"set_threshold", "set_methods", "set_support", "set_min_deposit", "set_min_withdraw", "set_binance_rate"})))
async def cb_settings_edit_start(call: CallbackQuery, callback_data: NavCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    key_map = {
        "set_threshold": ("low_stock_threshold", "📉 Enter the new low-stock threshold (integer):"),
        "set_methods": ("deposit_methods", "💳 Enter comma-separated deposit methods (e.g. bKash,Nagad,Binance):"),
        "set_support": ("support_contact", "📞 Enter the new support contact (e.g. @yourhandle):"),
        "set_min_deposit": ("min_deposit_amount", "⬆️ Enter the new <b>minimum deposit amount</b> (number, ৳, 0 = no minimum):"),
        "set_min_withdraw": ("min_withdraw_amount", "⬇️ Enter the new <b>minimum withdrawal amount</b> (number, ৳, 0 = no minimum):"),
        "set_binance_rate": ("binance_usdt_rate", "🪙 Enter the new <b>1 USDT = কত টাকা</b> রেট (শুধু সংখ্যা, যেমন 127):"),
    }
    key, prompt = key_map[callback_data.to]
    await state.set_state(AdminSettingsStates.waiting_value)
    await state.update_data(setting_key=key)
    await safe_edit(call, prompt, kb_admin_back())
    await call.answer()


# সংখ্যা (number) হতে হবে এমন সেটিংসের কী — এগুলোর ভ্যালু সেভ করার আগে ভ্যালিডেট করা হয়
_NUMERIC_SETTING_KEYS = {"low_stock_threshold", "min_deposit_amount", "min_withdraw_amount", "binance_usdt_rate"}


# শূন্য/ঋণাত্মক হতে পারবে না এমন সেটিংস (division-এ ব্যবহার হয় বলে) — 0 হলে ক্র্যাশ করবে
_STRICTLY_POSITIVE_SETTING_KEYS = {"binance_usdt_rate"}


@router.message(StateFilter(AdminSettingsStates.waiting_value))
async def msg_settings_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = (message.text or "").strip()
    if key in _NUMERIC_SETTING_KEYS:
        try:
            num = float(value.replace(",", "."))
            min_allowed = 0.000001 if key in _STRICTLY_POSITIVE_SETTING_KEYS else 0
            if num < min_allowed:
                raise ValueError
        except ValueError:
            msg = "❌ Invalid number. Enter a valid positive number." if key in _STRICTLY_POSITIVE_SETTING_KEYS else "❌ Invalid number. Enter a valid non-negative number."
            await message.answer(msg)
            return
        value = str(int(num)) if num == int(num) else str(num)
    await state.clear()
    async with SessionLocal() as s:
        await set_setting(s, key, value)
        await log_admin(s, message.from_user.id, "set_setting", f"{key}={value}")
    await message.answer("✅ Setting updated!", reply_markup=kb_admin_back())


# =====================================================================
# ADMIN — EXPORT / IMPORT DB (settings + products/buttons + stock)
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "backup_panel"))
async def cb_backup_panel(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    text = (
        "🗄 <b>Export / Import DB</b>\n\n"
        "এখান থেকে বটের <b>সম্পূর্ণ ডাটাবেস</b> একটা ফাইলে এক্সপোর্ট করতে পারবেন — "
        "Settings, Products, Stock, <b>Users (ব্যালেন্স সহ)</b>, Orders, Transactions, "
        "Deposits, Withdrawals, Seller Submissions, SMS log, Admin log — সব কিছু।\n\n"
        "বট আপডেট/মাইগ্রেশনের আগে <b>📤 Export DB</b> চাপুন, ফাইলটা নিরাপদ জায়গায় সেভ রাখুন। "
        "পরে <b>📥 Import DB</b> চেপে সেই ফাইলটা পাঠালেই বট আগের মত হয়ে যাবে — ইউজারদের ব্যালেন্স ও হিস্ট্রি সহ।\n\n"
        "⚠️ Import করলে বটের বর্তমান <b>সব ডাটা</b> (ব্যালেন্স/Orders/Transactions সহ) <b>মুছে গিয়ে</b> ফাইলের ডেটা দিয়ে রিপ্লেস হয়ে যাবে। "
        "Import এর পর ব্যাকআপে না-থাকা কোনো নতুন ডিপোজিট/অর্ডার থাকলে সেটা হারিয়ে যাবে — তাই সবসময় সবচেয়ে সাম্প্রতিক Export ফাইলটাই Import করুন।"
    )
    b = InlineKeyboardBuilder()
    b.button(text="📤 Export DB", callback_data=BackupCB(action="export"))
    b.button(text="📥 Import DB", callback_data=BackupCB(action="import"))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(BackupCB.filter(F.action == "export"))
async def cb_backup_export(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await call.answer("Preparing export...")
    async with SessionLocal() as s:
        settings_rows = (await s.execute(select(Setting))).scalars().all()
        products = (await s.execute(select(Product))).scalars().all()
        inventory = (await s.execute(select(InventoryItem))).scalars().all()
        users = (await s.execute(select(User))).scalars().all()
        orders = (await s.execute(select(Order))).scalars().all()
        transactions = (await s.execute(select(Transaction))).scalars().all()
        deposits = (await s.execute(select(Deposit))).scalars().all()
        withdrawals = (await s.execute(select(Withdrawal))).scalars().all()
        seller_subs = (await s.execute(select(SellerSubmission))).scalars().all()
        sms_msgs = (await s.execute(select(SmsMessage))).scalars().all()
        admin_logs = (await s.execute(select(AdminLog))).scalars().all()

    payload = {
        "version": 2,  # v2 = ফুল ডাটাবেস ব্যাকআপ (v1 শুধু settings/products/inventory কভার করতো)
        "exported_at": datetime.utcnow().isoformat(),
        "settings": {row.key: row.value for row in settings_rows},
        "products": [
            {
                "id": p.id,
                "category": p.category,
                "name": p.name,
                "price": p.price,
                "seller_price": p.seller_price,
                "description": p.description,
                "is_active": p.is_active,
                "is_buy_listing": p.is_buy_listing,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in products
        ],
        "inventory": [
            {
                "id": i.id,
                "product_id": i.product_id,
                "data": i.data,
                "data_hash": i.data_hash,
                "status": i.status,
                "order_id": i.order_id,
                "added_at": i.added_at.isoformat() if i.added_at else None,
                "sold_at": i.sold_at.isoformat() if i.sold_at else None,
            }
            for i in inventory
        ],
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "balance": u.balance,
                "total_deposit": u.total_deposit,
                "total_spent": u.total_spent,
                "total_income": u.total_income,
                "total_orders": u.total_orders,
                "status": u.status,
                "joined_at": u.joined_at.isoformat() if u.joined_at else None,
            }
            for u in users
        ],
        "orders": [
            {
                "id": o.id,
                "order_id": o.order_id,
                "user_id": o.user_id,
                "product_id": o.product_id,
                "product_name": o.product_name,
                "quantity": o.quantity,
                "total_price": o.total_price,
                "delivered_data": o.delivered_data,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in orders
        ],
        "transactions": [
            {
                "id": t.id,
                "user_id": t.user_id,
                "type": t.type,
                "amount": t.amount,
                "balance_after": t.balance_after,
                "note": t.note,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transactions
        ],
        "deposits": [
            {
                "id": d.id,
                "user_id": d.user_id,
                "amount": d.amount,
                "method": d.method,
                "trx_id": d.trx_id,
                "note": d.note,
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
                "reviewed_by": d.reviewed_by,
            }
            for d in deposits
        ],
        "withdrawals": [
            {
                "id": w.id,
                "user_id": w.user_id,
                "amount": w.amount,
                "method": w.method,
                "account_number": w.account_number,
                "note": w.note,
                "status": w.status,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "reviewed_at": w.reviewed_at.isoformat() if w.reviewed_at else None,
                "reviewed_by": w.reviewed_by,
            }
            for w in withdrawals
        ],
        "seller_submissions": [
            {
                "id": ss.id,
                "user_id": ss.user_id,
                "product_id": ss.product_id,
                "product_name": ss.product_name,
                "raw_data": ss.raw_data,
                "item_count": ss.item_count,
                "credited_amount": ss.credited_amount,
                "status": ss.status,
                "created_at": ss.created_at.isoformat() if ss.created_at else None,
                "reviewed_at": ss.reviewed_at.isoformat() if ss.reviewed_at else None,
                "reviewed_by": ss.reviewed_by,
                "file_id": ss.file_id,
                "file_name": ss.file_name,
            }
            for ss in seller_subs
        ],
        "sms_messages": [
            {
                "id": sm.id,
                "method": sm.method,
                "amount": sm.amount,
                "trx_id": sm.trx_id,
                "raw_text": sm.raw_text,
                "sender": sm.sender,
                "is_used": sm.is_used,
                "used_by_deposit_id": sm.used_by_deposit_id,
                "received_at": sm.received_at.isoformat() if sm.received_at else None,
            }
            for sm in sms_msgs
        ],
        "admin_logs": [
            {
                "id": al.id,
                "admin_id": al.admin_id,
                "action": al.action,
                "details": al.details,
                "created_at": al.created_at.isoformat() if al.created_at else None,
            }
            for al in admin_logs
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(raw, filename=fname),
        caption=(
            f"✅ <b>Full Export complete!</b>\n\n"
            f"⚙️ Settings: {len(payload['settings'])}\n"
            f"📦 Products: {len(payload['products'])}\n"
            f"📊 Stock items: {len(payload['inventory'])}\n"
            f"👤 Users: {len(payload['users'])}\n"
            f"🧾 Orders: {len(payload['orders'])}\n"
            f"💳 Transactions: {len(payload['transactions'])}\n"
            f"💰 Deposits: {len(payload['deposits'])}\n"
            f"🏧 Withdrawals: {len(payload['withdrawals'])}\n"
            f"🛒 Seller submissions: {len(payload['seller_submissions'])}\n"
            f"📩 SMS logs: {len(payload['sms_messages'])}\n"
            f"📝 Admin logs: {len(payload['admin_logs'])}\n\n"
            f"⚠️ এই ফাইলে ইউজারদের ব্যালেন্স ও আর্থিক তথ্য আছে — নিরাপদ জায়গায় সেভ রাখুন, কাউকে শেয়ার করবেন না।"
        ),
        reply_markup=kb_admin_back(),
    )
    async with SessionLocal() as s:
        await log_admin(
            s, call.from_user.id, "db_export",
            f"products={len(payload['products'])} stock={len(payload['inventory'])} "
            f"users={len(payload['users'])} orders={len(payload['orders'])}",
        )


@router.callback_query(BackupCB.filter(F.action == "import"))
async def cb_backup_import_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminBackupStates.waiting_file)
    await safe_edit(
        call,
        "📥 <b>Import DB</b>\n\n"
        "আগে এক্সপোর্ট করা <code>.json</code> ব্যাকআপ ফাইলটা এখানে পাঠান।\n\n"
        "⚠️ এটা আপলোড করলে বর্তমান <b>সব ডাটা</b> — Users/ব্যালেন্স/Orders/Transactions/Deposits/"
        "Withdrawals/Seller submissions সহ — মুছে গিয়ে ফাইলের ডেটা দিয়ে রিপ্লেস হবে। এটা Undo করা যায় না।",
        kb_admin_back(),
    )
    await call.answer()


@router.message(StateFilter(AdminBackupStates.waiting_file), F.document)
async def msg_backup_import_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    doc: Document = message.document
    if not doc.file_name.lower().endswith(".json"):
        await message.answer("❌ শুধু .json ব্যাকআপ ফাইল সাপোর্ট করে। আবার পাঠান।")
        return
    if doc.file_size and doc.file_size > MAX_BACKUP_SIZE_MB * 1024 * 1024:
        await message.answer(f"❌ ফাইলটা অনেক বড়। সর্বোচ্চ {MAX_BACKUP_SIZE_MB}MB।")
        return

    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    raw_bytes = buf.read()

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
        assert isinstance(payload, dict)
        settings_d = payload.get("settings", {})
        products_l = payload.get("products", [])
        inventory_l = payload.get("inventory", [])
        users_l = payload.get("users", [])
        orders_l = payload.get("orders", [])
        transactions_l = payload.get("transactions", [])
        deposits_l = payload.get("deposits", [])
        withdrawals_l = payload.get("withdrawals", [])
        seller_l = payload.get("seller_submissions", [])
        sms_l = payload.get("sms_messages", [])
        adminlog_l = payload.get("admin_logs", [])
        assert isinstance(settings_d, dict)
        for _lst in (products_l, inventory_l, users_l, orders_l, transactions_l,
                     deposits_l, withdrawals_l, seller_l, sms_l, adminlog_l):
            assert isinstance(_lst, list)
    except Exception:
        await message.answer("❌ ফাইলটা পড়া গেলো না বা এটা বৈধ ব্যাকআপ ফাইল না।")
        return

    # v1 (পুরনো) ব্যাকআপে "users" key থাকে না — সেটা দিয়ে ফুল-ব্যাকআপ কিনা বোঝা যায়
    is_full = "users" in payload

    await state.update_data(backup_raw=raw_bytes.decode("utf-8"))
    await state.set_state(None)

    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm Import", callback_data=BackupCB(action="confirm"))
    b.button(text="❌ Cancel", callback_data=BackupCB(action="cancel"))
    b.adjust(2)

    if is_full:
        preview = (
            f"📊 <b>Import Preview</b> (Full backup)\n\n"
            f"⚙️ Settings: {len(settings_d)}\n"
            f"📦 Products: {len(products_l)}\n"
            f"📊 Stock items: {len(inventory_l)}\n"
            f"👤 Users: {len(users_l)}\n"
            f"🧾 Orders: {len(orders_l)}\n"
            f"💳 Transactions: {len(transactions_l)}\n"
            f"💰 Deposits: {len(deposits_l)}\n"
            f"🏧 Withdrawals: {len(withdrawals_l)}\n"
            f"🛒 Seller submissions: {len(seller_l)}\n"
            f"📩 SMS logs: {len(sms_l)}\n"
            f"📝 Admin logs: {len(adminlog_l)}\n\n"
            f"⚠️ Confirm করলে বর্তমান <b>সব ডাটা</b> (ব্যালেন্স/Orders/Transactions সহ) মুছে গিয়ে "
            f"এই ফাইলের ডেটা দিয়ে রিপ্লেস হবে। এটা Undo করা যাবে না।"
        )
    else:
        preview = (
            f"📊 <b>Import Preview</b> (পুরনো ফরম্যাট — শুধু Catalog)\n\n"
            f"⚙️ Settings: {len(settings_d)}\n"
            f"📦 Products: {len(products_l)}\n"
            f"📊 Stock items: {len(inventory_l)}\n\n"
            f"ℹ️ এই ফাইলে Users/Orders/Transactions ডাটা নেই, তাই Confirm করলেও ওগুলো স্পর্শ করা হবে না — "
            f"শুধু Settings/Products/Stock মুছে ফাইলের ডেটা দিয়ে রিপ্লেস হবে।"
        )

    await message.answer(preview, reply_markup=b.as_markup())


@router.message(StateFilter(AdminBackupStates.waiting_file))
async def msg_backup_import_not_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("📎 অনুগ্রহ করে ব্যাকআপ <code>.json</code> ফাইলটা ডকুমেন্ট আকারে পাঠান।")


@router.callback_query(BackupCB.filter(F.action == "cancel"))
async def cb_backup_cancel(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.clear()
    await safe_edit(call, "❌ Import cancelled.", kb_admin_back())
    await call.answer()


@router.callback_query(BackupCB.filter(F.action == "confirm"))
async def cb_backup_confirm(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    data = await state.get_data()
    raw = data.get("backup_raw")
    if not raw:
        await call.answer("Backup data expired, please upload the file again.", show_alert=True)
        return
    await state.clear()

    try:
        payload = json.loads(raw)
    except Exception:
        await call.answer("❌ Invalid backup data.", show_alert=True)
        return

    def parse_dt(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except Exception:
            return None

    # v1 (পুরনো) ব্যাকআপে "users" key থাকে না — সেক্ষেত্রে Users/Orders/Transactions/Deposits/
    # Withdrawals/Seller submissions/SMS/Admin log একদম স্পর্শ করা হবে না, যাতে ভুলে পুরনো
    # ফরম্যাটের ফাইল আপলোড করলে লাইভ ব্যালেন্স/হিস্ট্রি মুছে না যায়।
    is_full = "users" in payload

    async with get_lock("db_import"):
        async with SessionLocal() as s:
            # ডিলিট অর্ডার: Product-এর উপর নির্ভরশীল চাইল্ড টেবিল আগে, তারপর Product
            await s.execute(delete(InventoryItem))
            await s.execute(delete(Order))
            await s.execute(delete(SellerSubmission))
            await s.execute(delete(Product))
            await s.execute(delete(Setting))
            if is_full:
                await s.execute(delete(Transaction))
                await s.execute(delete(Deposit))
                await s.execute(delete(Withdrawal))
                await s.execute(delete(SmsMessage))
                await s.execute(delete(AdminLog))
                await s.execute(delete(User))
            await s.commit()

            for k, v in payload.get("settings", {}).items():
                s.add(Setting(key=k, value=v))
            await s.commit()

            if is_full:
                for u in payload.get("users", []):
                    s.add(User(
                        id=u["id"],
                        username=u.get("username"),
                        full_name=u.get("full_name"),
                        balance=u.get("balance", 0.0),
                        total_deposit=u.get("total_deposit", 0.0),
                        total_spent=u.get("total_spent", 0.0),
                        total_income=u.get("total_income", 0.0),
                        total_orders=u.get("total_orders", 0),
                        status=u.get("status", "active"),
                        joined_at=parse_dt(u.get("joined_at")) or datetime.utcnow(),
                    ))
                await s.commit()

            for p in payload.get("products", []):
                s.add(Product(
                    id=p["id"],
                    category=p.get("category", "General"),
                    name=p.get("name", "Unnamed"),
                    price=p.get("price", 0.0),
                    seller_price=p.get("seller_price", 0.0),
                    description=p.get("description", ""),
                    is_active=p.get("is_active", True),
                    is_buy_listing=p.get("is_buy_listing", False),
                    created_at=parse_dt(p.get("created_at")) or datetime.utcnow(),
                ))
            await s.commit()

            for i in payload.get("inventory", []):
                s.add(InventoryItem(
                    id=i["id"],
                    product_id=i["product_id"],
                    data=i.get("data", ""),
                    data_hash=i.get("data_hash", ""),
                    status=i.get("status", "available"),
                    order_id=i.get("order_id"),
                    added_at=parse_dt(i.get("added_at")) or datetime.utcnow(),
                    sold_at=parse_dt(i.get("sold_at")),
                ))
            await s.commit()

            if is_full:
                for o in payload.get("orders", []):
                    s.add(Order(
                        id=o["id"],
                        order_id=o["order_id"],
                        user_id=o["user_id"],
                        product_id=o["product_id"],
                        product_name=o.get("product_name", ""),
                        quantity=o.get("quantity", 1),
                        total_price=o.get("total_price", 0.0),
                        delivered_data=o.get("delivered_data", ""),
                        created_at=parse_dt(o.get("created_at")) or datetime.utcnow(),
                    ))
                await s.commit()

                for t in payload.get("transactions", []):
                    s.add(Transaction(
                        id=t["id"],
                        user_id=t["user_id"],
                        type=t.get("type", ""),
                        amount=t.get("amount", 0.0),
                        balance_after=t.get("balance_after", 0.0),
                        note=t.get("note", ""),
                        created_at=parse_dt(t.get("created_at")) or datetime.utcnow(),
                    ))
                await s.commit()

                for d in payload.get("deposits", []):
                    s.add(Deposit(
                        id=d["id"],
                        user_id=d["user_id"],
                        amount=d.get("amount", 0.0),
                        method=d.get("method", ""),
                        trx_id=d.get("trx_id"),
                        note=d.get("note", ""),
                        status=d.get("status", "pending"),
                        created_at=parse_dt(d.get("created_at")) or datetime.utcnow(),
                        reviewed_at=parse_dt(d.get("reviewed_at")),
                        reviewed_by=d.get("reviewed_by"),
                    ))
                await s.commit()

                for w in payload.get("withdrawals", []):
                    s.add(Withdrawal(
                        id=w["id"],
                        user_id=w["user_id"],
                        amount=w.get("amount", 0.0),
                        method=w.get("method", ""),
                        account_number=w.get("account_number", ""),
                        note=w.get("note", ""),
                        status=w.get("status", "pending"),
                        created_at=parse_dt(w.get("created_at")) or datetime.utcnow(),
                        reviewed_at=parse_dt(w.get("reviewed_at")),
                        reviewed_by=w.get("reviewed_by"),
                    ))
                await s.commit()

                for ss in payload.get("seller_submissions", []):
                    s.add(SellerSubmission(
                        id=ss["id"],
                        user_id=ss["user_id"],
                        product_id=ss.get("product_id"),
                        product_name=ss.get("product_name"),
                        raw_data=ss.get("raw_data", ""),
                        item_count=ss.get("item_count", 0),
                        credited_amount=ss.get("credited_amount"),
                        status=ss.get("status", "pending"),
                        created_at=parse_dt(ss.get("created_at")) or datetime.utcnow(),
                        reviewed_at=parse_dt(ss.get("reviewed_at")),
                        reviewed_by=ss.get("reviewed_by"),
                        file_id=ss.get("file_id"),
                        file_name=ss.get("file_name"),
                    ))
                await s.commit()

                for sm in payload.get("sms_messages", []):
                    s.add(SmsMessage(
                        id=sm["id"],
                        method=sm.get("method", ""),
                        amount=sm.get("amount"),
                        trx_id=sm.get("trx_id"),
                        raw_text=sm.get("raw_text", ""),
                        sender=sm.get("sender", ""),
                        is_used=sm.get("is_used", False),
                        used_by_deposit_id=sm.get("used_by_deposit_id"),
                        received_at=parse_dt(sm.get("received_at")) or datetime.utcnow(),
                    ))
                await s.commit()

                for al in payload.get("admin_logs", []):
                    s.add(AdminLog(
                        id=al["id"],
                        admin_id=al["admin_id"],
                        action=al.get("action", ""),
                        details=al.get("details", ""),
                        created_at=parse_dt(al.get("created_at")) or datetime.utcnow(),
                    ))
                await s.commit()

            await log_admin(
                s, call.from_user.id, "db_import",
                f"full={is_full} products={len(payload.get('products', []))} stock={len(payload.get('inventory', []))} "
                f"users={len(payload.get('users', []))} orders={len(payload.get('orders', []))}",
            )

    if is_full:
        done_text = (
            f"✅ <b>Full Import complete!</b>\n\n"
            f"⚙️ Settings: {len(payload.get('settings', {}))}\n"
            f"📦 Products: {len(payload.get('products', []))}\n"
            f"📊 Stock items: {len(payload.get('inventory', []))}\n"
            f"👤 Users: {len(payload.get('users', []))}\n"
            f"🧾 Orders: {len(payload.get('orders', []))}\n"
            f"💳 Transactions: {len(payload.get('transactions', []))}\n"
            f"💰 Deposits: {len(payload.get('deposits', []))}\n"
            f"🏧 Withdrawals: {len(payload.get('withdrawals', []))}\n"
            f"🛒 Seller submissions: {len(payload.get('seller_submissions', []))}\n\n"
            f"বট এখন আগের মত হয়ে গেছে — ইউজার, ব্যালেন্স ও হিস্ট্রি সহ।"
        )
    else:
        done_text = (
            f"✅ <b>Import complete!</b> (পুরনো ফরম্যাট)\n\n"
            f"⚙️ Settings: {len(payload.get('settings', {}))}\n"
            f"📦 Products: {len(payload.get('products', []))}\n"
            f"📊 Stock items: {len(payload.get('inventory', []))}\n\n"
            f"ℹ️ এই ব্যাকআপে Users/Orders/Transactions ছিল না, তাই ওগুলো অপরিবর্তিত আছে।"
        )

    await safe_edit(call, done_text, kb_admin_back())
    await call.answer("Imported!")


# =====================================================================
# noop callback (for the quantity display "button")
# =====================================================================
@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# =====================================================================
# SMS WEBHOOK SERVER (aiohttp) — ফোনের SMS Forwarder App থেকে এখানে POST/GET
# রিকোয়েস্ট আসবে। বট polling এর পাশাপাশি এই ছোট ওয়েব সার্ভারটাও চলবে।
# =====================================================================
def _check_sms_token(request: web.Request) -> bool:
    token = (
        request.query.get("token")
        or request.headers.get("X-Auth-Token")
        or ""
    )
    return token == SMS_WEBHOOK_TOKEN


_SMS_TEXT_FIELD_NAMES = ("text", "message", "body", "content", "sms", "msg", "text_message", "sms_body", "smsBody", "key")
_SMS_SENDER_FIELD_NAMES = ("from", "sender", "number", "phone", "sms_from", "smsFrom", "originator")


def _dig_field(d: dict, names: tuple) -> str:
    """dict এর মধ্যে (এমনকি nested dict/list হলেও একটু খুঁজে) common field name গুলো চেক করে।"""
    if not isinstance(d, dict):
        return ""
    for k in names:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # কিছু app একটা "data"/"sms" নেস্টেড অবজেক্টের ভেতরে পাঠায়
    for v in d.values():
        if isinstance(v, dict):
            found = _dig_field(v, names)
            if found:
                return found
    return ""


async def sms_webhook_handler(request: web.Request) -> web.Response:
    if not _check_sms_token(request):
        return web.json_response({"ok": False, "error": "invalid_token"}, status=401)

    text = request.query.get("text") or request.query.get("message") or ""
    sender = request.query.get("from") or request.query.get("sender") or ""
    hint_method = request.query.get("method") or ""

    raw_body_str = ""
    if request.method == "POST":
        try:
            raw_bytes = await request.read()
            raw_body_str = raw_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            raw_body_str = ""

        body = {}
        if raw_body_str:
            # ১) JSON হিসেবে পার্স করার চেষ্টা
            try:
                parsed_json = json.loads(raw_body_str)
                if isinstance(parsed_json, dict):
                    body = parsed_json
            except Exception:
                pass
            # ২) form-urlencoded হিসেবে পার্স করার চেষ্টা (key=value&key2=value2)
            if not body and "=" in raw_body_str and "&" in raw_body_str:
                try:
                    from urllib.parse import parse_qs
                    parsed_form = parse_qs(raw_body_str)
                    body = {k: v[0] for k, v in parsed_form.items() if v}
                except Exception:
                    pass

        # debugging এর জন্য Railway লগে raw body প্রিন্ট হবে (প্রথম ৫০০ ক্যারেক্টার)
        log.info(f"📩 SMS webhook POST body (raw): {raw_body_str[:500]!r}")

        if body:
            text = text or _dig_field(body, _SMS_TEXT_FIELD_NAMES)
            sender = sender or _dig_field(body, _SMS_SENDER_FIELD_NAMES)
            hint_method = hint_method or _dig_field(body, ("method", "app", "provider"))

        # ৩) কোনো known field-এ কিছু না পেলে, পুরো raw body-টাকেই SMS টেক্সট
        # হিসেবে ধরে নেওয়া হচ্ছে (অনেক সিম্পল forwarder app শুধু plain SMS
        # content-ই raw body হিসেবে পাঠায়, কোনো wrapper key ছাড়া)
        if not text and raw_body_str and not raw_body_str.lstrip().startswith(("{", "[")):
            text = raw_body_str

    if not text:
        log.warning(f"SMS webhook: no_text_field | raw={raw_body_str[:500]!r}")
        # ✅ SMS Forwarder app যেন সবসময় HTTP 200 পায় (Success দেখাবে)।
        # app non-2xx কে Fail/Retry ধরে; আসল কারণ Railway লগে থেকে যাচ্ছে।
        return web.json_response(
            {
                "ok": True,
                "note": "no_text_field",
                "raw_body_preview": raw_body_str[:300],
            },
            status=200,
        )

    try:
        result = await store_sms_and_try_match(text, sender=sender, hint_method=hint_method)
    except Exception as e:
        log.exception("sms_webhook_handler error: %s", e)
        # ✅ App-কে সবসময় 200/ok দেওয়া হচ্ছে (unauthorized বাদে) যাতে
        # forwarder app Success দেখায়; আসল error Railway লগে থেকে যাচ্ছে।
        return web.json_response({"ok": True, "note": "internal_error_logged"}, status=200)

    return web.json_response({"ok": True, **result})


async def sms_health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "sms-webhook", "path": SMS_WEBHOOK_PATH})


def build_sms_webapp() -> web.Application:
    app = web.Application()
    app.router.add_post(SMS_WEBHOOK_PATH, sms_webhook_handler)
    app.router.add_get(SMS_WEBHOOK_PATH, sms_webhook_handler)
    app.router.add_get("/", sms_health_handler)
    return app


async def start_sms_webhook_server():
    app = build_sms_webapp()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SMS_WEBHOOK_PORT)
    await site.start()
    log.info(f"📩 SMS webhook server listening on 0.0.0.0:{SMS_WEBHOOK_PORT}{SMS_WEBHOOK_PATH}")

    if RAILWAY_PUBLIC_URL:
        full_url = f"{RAILWAY_PUBLIC_URL}{SMS_WEBHOOK_PATH}?token={SMS_WEBHOOK_TOKEN}"
    else:
        full_url = f"http://<your-server-ip>:{SMS_WEBHOOK_PORT}{SMS_WEBHOOK_PATH}?token={SMS_WEBHOOK_TOKEN}"

    print(
        f"📩 SMS webhook ready → {full_url}\n"
        f"   ফরোয়ার্ডার অ্যাপে JSON/GET params হিসেবে পাঠান: text (SMS বডি), from (sender, ঐচ্ছিক)"
    )
    if not RAILWAY_PUBLIC_URL:
        print(
            "   ⚠️ RAILWAY_PUBLIC_URL (বা Railway এর Generate Domain করা "
            "RAILWAY_PUBLIC_DOMAIN) সেট নেই, তাই সঠিক পাবলিক URL দেখানো যায়নি। "
            "Railway → Settings → Networking → Generate Domain করে সেই URL টা "
            "RAILWAY_PUBLIC_URL ভ্যারিয়েবলে বসিয়ে দিন।"
        )


# =====================================================================
# ADMIN — SMS LOG (ডিবাগিং/ভেরিফিকেশনের জন্য)
# =====================================================================
@router.callback_query(AListCB.filter(F.kind == "sms"))
async def cb_admin_sms_log(call: CallbackQuery, callback_data: AListCB):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    page = callback_data.page
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(SmsMessage).order_by(SmsMessage.id.desc())
        )).scalars().all()
    chunk = rows[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]
    has_next = len(rows) > (page + 1) * PAGE_SIZE
    if not chunk:
        text = "📩 <b>SMS Log</b>\n\nকোনো SMS এখনো আসেনি।"
    else:
        lines = ["📩 <b>SMS Log</b>\n"]
        for m in chunk:
            status = "✅ used" if m.is_used else "🕓 unused"
            lines.append(
                f"• #{m.id} | {m.method or '—'} | {dual_money(m.amount or 0)} | "
                f"Trx: {m.trx_id or '—'} | {status}"
            )
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="sms", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="sms", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


# =====================================================================
# GLOBAL ERROR HANDLER
# =====================================================================
@dp.errors()
async def on_error(event: ErrorEvent):
    log.exception("Unhandled error: %s", event.exception)
    return True


# =====================================================================
# ENTRYPOINT
# =====================================================================
async def main():
    await init_db()
    log.info("Database ready.")
    await start_sms_webhook_server()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Starting long polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
