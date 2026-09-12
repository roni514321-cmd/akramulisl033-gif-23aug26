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

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db").strip()

if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing! Create a .env file with BOT_TOKEN=<your token>.")
    sys.exit(1)

if not ADMIN_IDS:
    print("⚠️  Warning: ADMIN_IDS is empty in .env — nobody will be able to use /admin.")

MAX_XLSX_SIZE_MB = 8
PAGE_SIZE = 8
RATE_LIMIT_WINDOW = 1.0       # seconds
RATE_LIMIT_MAX_HITS = 5       # max messages/callbacks per window

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
    # seed default settings
    async with SessionLocal() as s:
        defaults = {
            "maintenance_mode": "0",
            "low_stock_threshold": "5",
            "deposit_methods": "bKash,Nagad,Rocket,Bank,Crypto",
            "default_seller_price": "0",
            "support_contact": "@support",
        }
        for k, v in defaults.items():
            existing = await s.get(Setting, k)
            if existing is None:
                s.add(Setting(key=k, value=v))
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


def data_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


# =====================================================================
# CALLBACK DATA FACTORIES
# =====================================================================
class NavCB(CallbackData, prefix="nav"):
    to: str


class CatCB(CallbackData, prefix="cat"):
    name: str
    page: int = 0


class ProdCB(CallbackData, prefix="prod"):
    id: int


class BuyCB(CallbackData, prefix="buy"):
    action: str   # inc/dec/confirm/cancel
    id: int
    qty: int = 1


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
    action: str   # view/edit_price/delete/add
    id: int = 0


class AXlsxCB(CallbackData, prefix="axl"):
    action: str   # pick_product/confirm/cancel/export
    id: int = 0


class AUserCB(CallbackData, prefix="auser"):
    action: str   # addbal/rembal/ban/unban
    id: int = 0


class AToggleCB(CallbackData, prefix="atog"):
    key: str


# =====================================================================
# FSM STATES
# =====================================================================
class DepositStates(StatesGroup):
    amount = State()
    note = State()


class SellStates(StatesGroup):
    waiting_data = State()


class BuyStates(StatesGroup):
    quantity = State()


class AdminProductStates(StatesGroup):
    add_category = State()
    add_name = State()
    add_price = State()
    add_seller_price = State()
    add_desc = State()
    edit_price = State()


class AdminXlsxStates(StatesGroup):
    waiting_file = State()


class AdminBalanceStates(StatesGroup):
    waiting_amount = State()


class AdminSearchStates(StatesGroup):
    waiting_query = State()


class AdminBroadcastStates(StatesGroup):
    waiting_text = State()


class AdminSettingsStates(StatesGroup):
    waiting_value = State()


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
    kb.button(text="🛒 Marketplace")
    kb.button(text="👤 Profile")
    kb.button(text="💰 Wallet")
    kb.button(text="📦 My Orders")
    kb.button(text="💼 Sell")
    kb.button(text="📞 Support")
    kb.adjust(2, 2, 2)
    if user_is_admin:
        kb.row(KeyboardButton(text="🛠 Admin Panel"))
    return kb.as_markup(resize_keyboard=True)


def kb_back(to: str = "home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=NavCB(to=to))
    b.button(text="🏠 Home", callback_data=NavCB(to="home"))
    b.adjust(2)
    return b.as_markup()


def kb_wallet() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Deposit", callback_data=NavCB(to="deposit"))
    b.button(text="🧾 Transaction History", callback_data=NavCB(to="txhistory"))
    b.button(text="⬅️ Back", callback_data=NavCB(to="home"))
    b.adjust(1, 1, 1)
    return b.as_markup()


def kb_deposit_methods(methods: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=m, callback_data=DepMethodCB(method=m))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Back", callback_data=NavCB(to="wallet").pack()))
    return b.as_markup()


def kb_categories(categories: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in categories:
        b.button(text=f"📁 {c}", callback_data=CatCB(name=c))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🏠 Home", callback_data=NavCB(to="home").pack()))
    return b.as_markup()


def kb_products(products: list[Product], category: str, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p.name} — {money(p.price)}৳", callback_data=ProdCB(id=p.id))
    b.adjust(1)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=CatCB(name=category, page=page - 1).pack()))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=CatCB(name=category, page=page + 1).pack()))
    if nav_row:
        b.row(*nav_row)
    b.row(InlineKeyboardButton(text="⬅️ Categories", callback_data=NavCB(to="market").pack()))
    return b.as_markup()


def kb_product_detail(product_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Buy", callback_data=BuyCB(action="start", id=product_id, qty=1))
    b.button(text="⬅️ Back", callback_data=NavCB(to="market"))
    b.adjust(1)
    return b.as_markup()


def kb_qty(product_id: int, qty: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="➖", callback_data=BuyCB(action="dec", id=product_id, qty=qty).pack()),
        InlineKeyboardButton(text=f"{qty}", callback_data="noop"),
        InlineKeyboardButton(text="➕", callback_data=BuyCB(action="inc", id=product_id, qty=qty).pack()),
    )
    b.row(InlineKeyboardButton(text="✅ Confirm Purchase", callback_data=BuyCB(action="confirm", id=product_id, qty=qty).pack()))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data=BuyCB(action="cancel", id=product_id, qty=qty).pack()))
    return b.as_markup()


def kb_admin_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📦 Products", callback_data=AListCB(kind="aprod"))
    b.button(text="📊 Stock", callback_data=AListCB(kind="stock"))
    b.button(text="🧾 Orders", callback_data=AListCB(kind="orders"))
    b.button(text="💰 Deposits", callback_data=AListCB(kind="deps"))
    b.button(text="💼 Seller Requests", callback_data=AListCB(kind="sells"))
    b.button(text="👥 Users", callback_data=NavCB(to="admin_users"))
    b.button(text="📢 Broadcast", callback_data=NavCB(to="admin_broadcast"))
    b.button(text="⚙️ Settings", callback_data=NavCB(to="admin_settings"))
    b.button(text="🏠 Home", callback_data=NavCB(to="home"))
    b.adjust(2, 2, 2, 2, 1)
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
        f"💳 Balance: <b>{money(user.balance)}৳</b>\n\n"
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
@router.message(F.text == "🛒 Marketplace")
async def msg_market(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Product.category).where(Product.is_active == True).distinct()
        )).scalars().all()
    if not cats:
        await message.answer("🛒 <b>Marketplace</b>\n\nNo products available yet.")
        return
    await message.answer("🛒 <b>Marketplace</b>\n\nChoose a category:", reply_markup=kb_categories(list(cats)))


@router.message(F.text == "👤 Profile")
async def msg_profile(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        user = await s.get(User, message.from_user.id)
    text = (
        "👤 <b>Your Profile</b>\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔖 Username: @{user.username or '—'}\n"
        f"💳 Balance: <b>{money(user.balance)}৳</b>\n"
        f"⬆️ Total Deposit: {money(user.total_deposit)}৳\n"
        f"⬇️ Total Spent: {money(user.total_spent)}৳\n"
        f"🧾 Total Orders: {user.total_orders}\n"
        f"📅 Joined: {user.joined_at.strftime('%d/%m/%Y')}"
    )
    await message.answer(text)


@router.message(F.text == "💰 Wallet")
async def msg_wallet(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        user = await s.get(User, message.from_user.id)
    text = f"💰 <b>Wallet</b>\n\nBalance: <b>{money(user.balance)}৳</b>"
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
            lines.append(f"• {o.order_id} | {o.product_name} x{o.quantity} | {money(o.total_price)}৳ | {o.created_at.strftime('%d/%m/%Y')}")
        text = "\n".join(lines)
    await message.answer(text)


@router.message(F.text == "💼 Sell")
async def msg_sell(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        products = (await s.execute(select(Product).where(Product.is_active == True))).scalars().all()
    if not products:
        await message.answer("💼 <b>Sell</b>\n\nNo product categories are open for selling right now.")
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p.name} (pays {money(p.seller_price)}৳/item)", callback_data=SellProdCB(id=p.id))
    b.adjust(1)
    await message.answer("💼 <b>Sell</b>\n\nWhich product are you submitting stock for?", reply_markup=b.as_markup())


@router.message(F.text == "📞 Support")
async def msg_support(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        contact = await get_setting(s, "support_contact", "@support")
    await message.answer(f"📞 <b>Support</b>\n\nNeed help? Contact us: {contact}")


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
        f"💳 Balance: <b>{money(user.balance)}৳</b>\n"
        f"⬆️ Total Deposit: {money(user.total_deposit)}৳\n"
        f"⬇️ Total Spent: {money(user.total_spent)}৳\n"
        f"🧾 Total Orders: {user.total_orders}\n"
        f"📅 Joined: {user.joined_at.strftime('%d/%m/%Y')}"
    )
    await safe_edit(call, text, kb_back("home"))
    await call.answer()


# =====================================================================
# SUPPORT
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "support"))
async def cb_support(call: CallbackQuery):
    async with SessionLocal() as s:
        contact = await get_setting(s, "support_contact", "@support")
    text = f"📞 <b>Support</b>\n\nNeed help? Contact us: {contact}"
    await safe_edit(call, text, kb_back("home"))
    await call.answer()


# =====================================================================
# WALLET / DEPOSIT
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "wallet"))
async def cb_wallet(call: CallbackQuery):
    async with SessionLocal() as s:
        user = await s.get(User, call.from_user.id)
    text = f"💰 <b>Wallet</b>\n\nBalance: <b>{money(user.balance)}৳</b>"
    await safe_edit(call, text, kb_wallet())
    await call.answer()


@router.callback_query(NavCB.filter(F.to == "deposit"))
async def cb_deposit(call: CallbackQuery):
    async with SessionLocal() as s:
        methods_raw = await get_setting(s, "deposit_methods", "bKash,Nagad,Rocket,Bank,Crypto")
    methods = [m.strip() for m in methods_raw.split(",") if m.strip()]
    await safe_edit(call, "💳 <b>Select a deposit method:</b>", kb_deposit_methods(methods))
    await call.answer()


@router.callback_query(DepMethodCB.filter())
async def cb_deposit_method(call: CallbackQuery, callback_data: DepMethodCB, state: FSMContext):
    await state.update_data(dep_method=callback_data.method)
    await state.set_state(DepositStates.amount)
    await safe_edit(
        call,
        f"💳 Method: <b>{callback_data.method}</b>\n\nEnter the deposit amount (number only):",
        kb_back("wallet"),
    )
    await call.answer()


@router.message(StateFilter(DepositStates.amount))
async def msg_deposit_amount(message: Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(txt)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid amount. Please send a valid positive number.")
        return
    await state.update_data(dep_amount=amount)
    await state.set_state(DepositStates.note)
    await message.answer("🔑 Send your transaction ID / reference note (or send <code>-</code> to skip):")


@router.message(StateFilter(DepositStates.note))
async def msg_deposit_note(message: Message, state: FSMContext, db_user: User):
    data = await state.get_data()
    method = data.get("dep_method", "Unknown")
    amount = float(data.get("dep_amount", 0))
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    await state.clear()

    async with SessionLocal() as s:
        dep = Deposit(user_id=db_user.id, amount=amount, method=method, note=note[:250], status="pending")
        s.add(dep)
        await s.commit()
        await s.refresh(dep)

    await message.answer(
        f"⏳ <b>Deposit request submitted!</b>\n\n"
        f"🆔 Request: <code>DEP-{dep.id}</code>\n"
        f"💳 Method: {method}\n"
        f"💰 Amount: {money(amount)}৳\n\n"
        f"An admin will review it shortly.",
        reply_markup=kb_back("home"),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=DepReviewCB(action="approve", id=dep.id))
    kb.button(text="❌ Reject", callback_data=DepReviewCB(action="reject", id=dep.id))
    kb.adjust(2)
    await broadcast_to_admins(
        f"💰 <b>New Deposit Request</b>\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"💳 Method: {method}\n"
        f"💰 Amount: {money(amount)}৳\n"
        f"📝 Note: {note or '—'}\n"
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
            user = await s.get(User, dep.user_id)
            if callback_data.action == "approve":
                dep.status = "approved"
                dep.reviewed_at = datetime.utcnow()
                dep.reviewed_by = call.from_user.id
                await s.commit()
                new_bal = await add_balance(s, user, dep.amount, "deposit", f"Deposit #{dep.id} ({dep.method})")
                try:
                    await bot.send_message(
                        user.id,
                        f"✅ <b>Deposit Approved!</b>\n\n💰 +{money(dep.amount)}৳ added\n"
                        f"💳 New Balance: {money(new_bal)}৳",
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
            lines.append(f"• {t.created_at.strftime('%d/%m %H:%M')} | {t.type} | {sign}{money(t.amount)}৳ | bal: {money(t.balance_after)}৳")
        text = "\n".join(lines)
    await safe_edit(call, text, kb_back("wallet"))
    await call.answer()


# =====================================================================
# MARKETPLACE
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "market"))
async def cb_market(call: CallbackQuery):
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Product.category).where(Product.is_active == True).distinct()
        )).scalars().all()
    if not cats:
        await safe_edit(call, "🛒 <b>Marketplace</b>\n\nNo products available yet.", kb_back("home"))
        await call.answer()
        return
    await safe_edit(call, "🛒 <b>Marketplace</b>\n\nChoose a category:", kb_categories(list(cats)))
    await call.answer()


@router.callback_query(CatCB.filter())
async def cb_category(call: CallbackQuery, callback_data: CatCB):
    category = callback_data.name
    page = callback_data.page
    async with SessionLocal() as s:
        all_products = (await s.execute(
            select(Product).where(Product.category == category, Product.is_active == True)
            .order_by(Product.id)
        )).scalars().all()

    start = page * PAGE_SIZE
    chunk = all_products[start:start + PAGE_SIZE]
    has_next = len(all_products) > start + PAGE_SIZE

    if not chunk:
        await safe_edit(call, f"📁 <b>{category}</b>\n\nNo products in this category.", kb_back("market"))
        await call.answer()
        return

    async with SessionLocal() as s:
        stock_counts = {}
        for p in chunk:
            cnt = (await s.execute(
                select(func.count()).select_from(InventoryItem)
                .where(InventoryItem.product_id == p.id, InventoryItem.status == "available")
            )).scalar_one()
            stock_counts[p.id] = cnt

    lines = [f"📁 <b>{category}</b>\n"]
    for p in chunk:
        lines.append(f"• {p.name} — {money(p.price)}৳ (stock: {stock_counts.get(p.id, 0)})")
    await safe_edit(call, "\n".join(lines), kb_products(chunk, category, page, has_next))
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
        f"📁 Category: {p.category}\n"
        f"💰 Price: {money(p.price)}৳\n"
        f"📦 Stock: {stock}\n\n"
        f"{p.description or ''}"
    )
    await safe_edit(call, text, kb_product_detail(p.id))
    await call.answer()


@router.callback_query(BuyCB.filter())
async def cb_buy(call: CallbackQuery, callback_data: BuyCB, db_user: User):
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
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>{qty}</b>\nTotal: {money(p.price * qty)}৳", kb_qty(pid, qty))
        await call.answer()
        return

    if action == "dec":
        qty = max(qty - 1, 1)
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>{qty}</b>\nTotal: {money(p.price * qty)}৳", kb_qty(pid, qty))
        await call.answer()
        return

    if action == "start":
        if stock <= 0:
            await call.answer("Out of stock!", show_alert=True)
            return
        await safe_edit(call, f"🛒 <b>{p.name}</b>\n\nQuantity: <b>1</b>\nTotal: {money(p.price)}৳", kb_qty(pid, 1))
        await call.answer()
        return

    if action == "confirm":
        await do_purchase(call, db_user, pid, qty)
        return


async def do_purchase(call: CallbackQuery, db_user: User, product_id: int, qty: int):
    async with get_lock(f"product:{product_id}"), get_lock(f"user:{db_user.id}"):
        async with SessionLocal() as s:
            p = await s.get(Product, product_id)
            user = await s.get(User, db_user.id)
            items = (await s.execute(
                select(InventoryItem)
                .where(InventoryItem.product_id == product_id, InventoryItem.status == "available")
                .order_by(InventoryItem.id).limit(qty)
            )).scalars().all()

            if not p or not p.is_active:
                await call.answer("Product unavailable.", show_alert=True)
                return
            if len(items) < qty:
                await call.answer(f"❌ Only {len(items)} in stock now.", show_alert=True)
                return
            total = round(p.price * qty, 2)
            if user.balance < total:
                await call.answer("❌ Insufficient balance. Please deposit first.", show_alert=True)
                return

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

    await safe_edit(
        call,
        f"✅ <b>Purchase Successful!</b>\n\n"
        f"🆔 Order: <code>{order_id}</code>\n"
        f"📦 {p.name} x{qty}\n"
        f"💰 Total: {money(total)}৳\n"
        f"💳 New Balance: {money(user.balance)}৳\n\n"
        f"📄 <b>Your items:</b>\n<code>{delivered_text[:3500]}</code>",
        kb_back("home"),
    )
    await call.answer("✅ Purchased!")

    if remaining <= threshold:
        await broadcast_to_admins(f"⚠️ <b>Low stock!</b>\n\n{p.name}: only {remaining} left.")


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
            lines.append(f"• {o.order_id} | {o.product_name} x{o.quantity} | {money(o.total_price)}৳ | {o.created_at.strftime('%d/%m/%Y')}")
        text = "\n".join(lines)
    await safe_edit(call, text, kb_back("home"))
    await call.answer()


# =====================================================================
# SELL FLOW
# =====================================================================
@router.callback_query(NavCB.filter(F.to == "sell"))
async def cb_sell(call: CallbackQuery):
    async with SessionLocal() as s:
        products = (await s.execute(select(Product).where(Product.is_active == True))).scalars().all()
    if not products:
        await safe_edit(call, "💼 <b>Sell</b>\n\nNo product categories are open for selling right now.", kb_back("home"))
        await call.answer()
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p.name} (pays {money(p.seller_price)}৳/item)", callback_data=SellProdCB(id=p.id))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Back", callback_data=NavCB(to="home").pack()))
    await safe_edit(call, "💼 <b>Sell</b>\n\nWhich product are you submitting stock for?", b.as_markup())
    await call.answer()


@router.callback_query(SellProdCB.filter())
async def cb_sell_pick_product(call: CallbackQuery, callback_data: SellProdCB, state: FSMContext):
    await state.update_data(sell_product_id=callback_data.id)
    await state.set_state(SellStates.waiting_data)
    await safe_edit(
        call,
        "📝 Send your data now — <b>one item per line</b>.\n"
        "Example:\n<code>mail1@example.com:pass1\nmail2@example.com:pass2</code>",
        kb_back("home"),
    )
    await call.answer()


@router.message(StateFilter(SellStates.waiting_data))
async def msg_sell_data(message: Message, state: FSMContext, db_user: User):
    data = await state.get_data()
    pid = data.get("sell_product_id")
    raw = (message.text or "").strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        await message.answer("❌ No valid lines found. Please send your data again.")
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
        f"⏳ <b>Submission received!</b>\n\n🆔 ID: SUB-{sub.id}\n📦 Items: {len(lines)}\n\n"
        "An admin will review it soon.",
        reply_markup=kb_back("home"),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=SellReviewCB(action="approve", id=sub.id))
    kb.button(text="❌ Reject", callback_data=SellReviewCB(action="reject", id=sub.id))
    kb.adjust(2)
    await broadcast_to_admins(
        f"💼 <b>New Seller Submission</b>\n\n"
        f"👤 User: <code>{db_user.id}</code> (@{db_user.username or '—'})\n"
        f"🆔 ID: SUB-{sub.id}\n"
        f"📦 Product: {sub.product_name or '—'}\n"
        f"🔢 Items: {len(lines)}\n\n"
        f"<code>{raw[:500]}</code>",
        kb.as_markup(),
    )


@router.callback_query(SellReviewCB.filter())
async def cb_sell_review(call: CallbackQuery, callback_data: SellReviewCB):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    sub_id = callback_data.id
    async with get_lock(f"sub:{sub_id}"):
        async with SessionLocal() as s:
            sub = await s.get(SellerSubmission, sub_id)
            if not sub:
                await call.answer("Not found.", show_alert=True)
                return
            if sub.status != "pending":
                await call.answer("Already processed!", show_alert=True)
                return
            user = await s.get(User, sub.user_id)

            if callback_data.action == "approve":
                lines = [l.strip() for l in sub.raw_data.splitlines() if l.strip()]
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
                sub.reviewed_by = call.from_user.id
                await s.commit()
                if credit > 0:
                    await add_balance(s, user, credit, "sell_credit", f"Submission SUB-{sub.id}")
                try:
                    await bot.send_message(
                        user.id,
                        f"✅ <b>Your submission was approved!</b>\n\n"
                        f"📦 Added: {added} item(s) ({dup} duplicate(s) skipped)\n"
                        f"💰 Credited: {money(credit)}৳",
                    )
                except Exception:
                    pass
            else:
                sub.status = "rejected"
                sub.reviewed_at = datetime.utcnow()
                sub.reviewed_by = call.from_user.id
                await s.commit()
                try:
                    await bot.send_message(user.id, f"❌ <b>Your submission SUB-{sub.id} was rejected.</b>")
                except Exception:
                    pass
            await log_admin(s, call.from_user.id, f"sell_{callback_data.action}", f"sub_id={sub.id}")

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
        f"💵 Total Sales: {money(sales_sum)}৳\n"
        f"💰 Total Deposits (approved): {money(deposits_sum)}৳\n"
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
        b.button(text=f"{flag} {p.name} — {money(p.price)}৳", callback_data=AProdCB(action="view", id=p.id))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="aprod", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="aprod", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="➕ Add Product", callback_data=AProdCB(action="add").pack()))
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))

    text = "📦 <b>Products</b>\n\n" + ("No products yet." if not chunk else "Tap a product to manage it.")
    await safe_edit(call, text, b.as_markup())
    await call.answer()


@router.callback_query(AProdCB.filter(F.action == "add"))
async def cb_admin_add_product_start(call: CallbackQuery, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    await state.set_state(AdminProductStates.add_category)
    await safe_edit(call, "📁 Enter the <b>category</b> name for the new product:", kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminProductStates.add_category))
async def msg_add_cat(message: Message, state: FSMContext):
    await state.update_data(new_category=message.text.strip()[:64])
    await state.set_state(AdminProductStates.add_name)
    await message.answer("🏷 Enter the <b>product name</b>:")


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
    await state.set_state(AdminProductStates.add_seller_price)
    await message.answer("💼 Enter the <b>seller payout per item</b> (0 if not sellable, number, ৳):")


@router.message(StateFilter(AdminProductStates.add_seller_price))
async def msg_add_seller_price(message: Message, state: FSMContext):
    try:
        sp = float(message.text.strip().replace(",", "."))
        if sp < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Invalid value. Enter a number (0 allowed).")
        return
    await state.update_data(new_seller_price=sp)
    await state.set_state(AdminProductStates.add_desc)
    await message.answer("📝 Enter a short <b>description</b> (or send <code>-</code> to skip):")


@router.message(StateFilter(AdminProductStates.add_desc))
async def msg_add_desc(message: Message, state: FSMContext):
    desc = message.text.strip()
    if desc == "-":
        desc = ""
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as s:
        p = Product(
            category=data["new_category"], name=data["new_name"], price=data["new_price"],
            seller_price=data["new_seller_price"], description=desc[:500], is_active=True,
        )
        s.add(p)
        await s.commit()
        await log_admin(s, message.from_user.id, "add_product", p.name)
    await message.answer(f"✅ Product <b>{data['new_name']}</b> added!", reply_markup=kb_admin_back())


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
    text = (
        f"🏷 <b>{p.name}</b>\n\n"
        f"📁 Category: {p.category}\n"
        f"💰 Price: {money(p.price)}৳\n"
        f"💼 Seller payout: {money(p.seller_price)}৳/item\n"
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

    preview = "\n".join(valid[:5])
    text = (
        f"📊 <b>Import Preview</b>\n\n"
        f"🗂 Headers: {', '.join(headers)}\n"
        f"✅ Valid rows: {len(valid)}\n"
        f"♻️ Duplicates skipped: {dup}\n"
        f"🚫 Blank rows skipped: {blanks}\n\n"
        f"<b>Sample:</b>\n<code>{preview[:1200] or '—'}</code>"
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
            lines.append(f"• {o.order_id} | user {o.user_id} | {o.product_name} x{o.quantity} | {money(o.total_price)}৳")
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
            lines.append(f"• DEP-{d.id} | user {d.user_id} | {d.method} | {money(d.amount)}৳")
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
    if not chunk:
        text = "💼 <b>Pending Seller Requests</b>\n\nNone right now."
    else:
        lines = ["💼 <b>Pending Seller Requests</b>\n"]
        for sub in chunk:
            lines.append(f"• SUB-{sub.id} | user {sub.user_id} | {sub.product_name or '—'} | {sub.item_count} items")
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    for sub in chunk:
        b.button(text=f"✅ SUB-{sub.id}", callback_data=SellReviewCB(action="approve", id=sub.id))
        b.button(text=f"❌ SUB-{sub.id}", callback_data=SellReviewCB(action="reject", id=sub.id))
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=AListCB(kind="sells", page=page - 1).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=AListCB(kind="sells", page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin Panel", callback_data=NavCB(to="admin").pack()))
    await safe_edit(call, text, b.as_markup())
    await call.answer()


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
        f"💳 Balance: {money(user.balance)}৳\n"
        f"⬆️ Deposited: {money(user.total_deposit)}৳\n"
        f"⬇️ Spent: {money(user.total_spent)}৳\n"
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
        await bot.send_message(uid, f"💳 Your balance was adjusted by admin: {'+' if signed>0 else ''}{money(signed)}৳. New balance: {money(new_bal)}৳")
    except Exception:
        pass
    await message.answer(f"✅ Done. New balance: {money(new_bal)}৳", reply_markup=kb_admin_back())


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
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🔧 Maintenance Mode: {'ON 🔴' if maint else 'OFF 🟢'}\n"
        f"📉 Low Stock Threshold: {threshold}\n"
        f"💳 Deposit Methods: {methods}\n"
        f"📞 Support Contact: {support}"
    )
    b = InlineKeyboardBuilder()
    b.button(text=("🔴 Turn Maintenance OFF" if maint else "🟢 Turn Maintenance ON"), callback_data=AToggleCB(key="maintenance_mode"))
    b.button(text="✏️ Set Low Stock Threshold", callback_data=NavCB(to="set_threshold"))
    b.button(text="✏️ Set Deposit Methods", callback_data=NavCB(to="set_methods"))
    b.button(text="✏️ Set Support Contact", callback_data=NavCB(to="set_support"))
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


@router.callback_query(NavCB.filter(F.to.in_({"set_threshold", "set_methods", "set_support"})))
async def cb_settings_edit_start(call: CallbackQuery, callback_data: NavCB, state: FSMContext):
    if not admin_only(call):
        await call.answer("⛔ Admins only.", show_alert=True); return
    key_map = {
        "set_threshold": ("low_stock_threshold", "📉 Enter the new low-stock threshold (integer):"),
        "set_methods": ("deposit_methods", "💳 Enter comma-separated deposit methods (e.g. bKash,Nagad,Bank):"),
        "set_support": ("support_contact", "📞 Enter the new support contact (e.g. @yourhandle):"),
    }
    key, prompt = key_map[callback_data.to]
    await state.set_state(AdminSettingsStates.waiting_value)
    await state.update_data(setting_key=key)
    await safe_edit(call, prompt, kb_admin_back())
    await call.answer()


@router.message(StateFilter(AdminSettingsStates.waiting_value))
async def msg_settings_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = (message.text or "").strip()
    await state.clear()
    async with SessionLocal() as s:
        await set_setting(s, key, value)
        await log_admin(s, message.from_user.id, "set_setting", f"{key}={value}")
    await message.answer("✅ Setting updated!", reply_markup=kb_admin_back())


# =====================================================================
# noop callback (for the quantity display "button")
# =====================================================================
@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
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
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Starting long polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
