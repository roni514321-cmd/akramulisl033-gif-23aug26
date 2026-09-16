# -*- coding: utf-8 -*-
"""
Virtual Number Shop - Telegram Bot
====================================================================
বাটন, মেনু নেভিগেশন ও স্ট্যাকচারের পাশাপাশি এখন Purchase (Buy one pcs / Bulk Buy)
এবং Admin File Upload (stock যুক্ত করা) এর real logic যুক্ত করা হয়েছে।
⚠️ ডেটা এখনও in-memory (RAM) তে থাকে -> bot restart হলে সব হারিয়ে যাবে।
যেখানে আসল Payment gateway / persistent Database বসবে সেখানে এখনও
# TODO: ... কমেন্ট দিয়ে চিহ্নিত করা আছে।

Library: pyTelegramBotAPI (telebot)
    pip install pyTelegramBotAPI flask openpyxl

Environment variables (Railway এ / .env এ সেট করবেন):
    BOT_TOKEN     -> BotFather থেকে পাওয়া টোকেন
    ADMIN_ID      -> আপনার টেলিগ্রাম User ID (একাধিক হলে কমা দিয়ে আলাদা করুন)
    RAILWAY_URL   -> Railway তে ডিপ্লয় করা অ্যাপের পাবলিক URL (webhook এর জন্য)
    PORT          -> Railway যে পোর্ট দেয় (ডিফল্ট 8080)

Run mode:
    - RAILWAY_URL সেট থাকলে -> Webhook mode (Flask + Railway)
    - RAILWAY_URL না থাকলে -> Polling mode (লোকাল টেস্টিং এর জন্য)
"""

import os
import io
import uuid
import datetime

import telebot
from telebot import types

# ---------------------------------------------------------------------------
# ENV / CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_ID", "6053411200").split(",")
    if x.strip().isdigit()
]
RAILWAY_URL = os.environ.get("RAILWAY_URL", "")   # e.g. https://your-app.up.railway.app
PORT = int(os.environ.get("PORT", 8080))
REFERRAL_BONUS = 10   # TODO: real bonus amount (এডমিন Bot Settings থেকে সেট করার আগে placeholder)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# বটের আসল username নিজে থেকে (dynamically) নিয়ে নেওয়া হচ্ছে, যাতে রেফারেল লিংকে
# ভুল/হার্ডকোড করা username (যেমন "vertual_shop_bot" / "virtualshop") না বসে।
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username")  # fallback

# ---------------------------------------------------------------------------
# TEMP IN-MEMORY "DATABASE" (placeholder only)
# TODO: এইগুলোকে আসল Database (SQLite/PostgreSQL/MongoDB) দিয়ে replace করবেন
# ---------------------------------------------------------------------------
users = {}      # user_id -> {full_name, username, balance, total_purchased, today_spent, today_deposit, referrals, earned}
products = {
    "whatsapp": {
        "name": "WhatsApp Number",
        "price": 0,          # TODO: real price (এডমিন সেট করার আগে purchase ব্লক থাকবে)
        "stock": 0,          # Upload File থেকে auto আপডেট হয়
        "description": "WhatsApp verification number.",  # TODO
        "stock_list": [],    # [{"number": "...", "otp_link": "..."}, ...]
    },
    "telegram": {
        "name": "Telegram Number",
        "price": 0,          # TODO: real price (এডমিন সেট করার আগে purchase ব্লক থাকবে)
        "stock": 0,          # Upload File থেকে auto আপডেট হয়
        "description": "Telegram verification number.",  # TODO
        "stock_list": [],    # [{"number": "...", "otp_link": "..."}, ...]
    },
}
orders = {}     # order_id -> order data

# ---------------------------------------------------------------------------
# BOT SETTINGS (runtime-editable, Admin Panel -> ⚙️ Bot Settings থেকে বদলানো যায়)
# TODO: এইগুলোও persistent Database তে সংরক্ষণ করবেন (এখনো in-memory)
# ---------------------------------------------------------------------------
bot_settings = {
    "referral_bonus": REFERRAL_BONUS,   # প্রতি রেফারেলে বোনাস (BDT)
    "min_deposit": 0,                   # 0 মানে কোনো সীমা সেট করা নেই
    "max_deposit": 0,                   # 0 মানে কোনো সীমা সেট করা নেই
    "maintenance_mode": False,          # True হলে সাধারণ ইউজাররা বট ব্যবহার করতে পারবে না
}

# navigation state per user, e.g. {"menu": "buy_number", "product": "whatsapp"}
user_state = {}


def get_user(message_or_call):
    """Ensure user exists in temp store and return the user dict."""
    u = message_or_call.from_user
    uid = u.id
    if uid not in users:
        users[uid] = {
            "full_name": (u.first_name or "") + ((" " + u.last_name) if u.last_name else ""),
            "username": f"@{u.username}" if u.username else "N/A",
            "balance": 0,
            "total_purchased": 0,
            "today_spent": 0,
            "today_deposit": 0,
            "referrals": 0,
            "earned": 0,
            "referred_by": None,   # কে রেফার করেছে (user_id), একবারই সেট হবে
        }
    return users[uid]


def is_admin(user_id):
    return user_id in ADMIN_IDS


def maintenance_block(ctx):
    """Maintenance mode চালু থাকলে non-admin ইউজারদের ব্লক করে True রিটার্ন করে।
    ctx একটি message অথবা callback হতে পারে।"""
    uid = ctx.from_user.id
    if bot_settings["maintenance_mode"] and not is_admin(uid):
        chat_id = ctx.chat.id if hasattr(ctx, "chat") else ctx.message.chat.id
        bot.send_message(
            chat_id,
            "🛠️ <b>Bot is under maintenance</b>\n\n"
            "দুঃখিত, বটটি বর্তমানে মেইনটেন্যান্সে আছে। একটু পরে আবার চেষ্টা করুন।",
        )
        return True
    return False


def fmt_amount(value):
    """সব জায়গায় একই ফরম্যাটে টাকা দেখানোর জন্য: BDT 100 (≈$100)"""
    return f"BDT {value} (≈${value})"


# ---------------------------------------------------------------------------
# KEYBOARDS (Reply / Main menu)
# ---------------------------------------------------------------------------
def main_menu_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🛒 Buy Number"),
        types.KeyboardButton("👤 Profile"),
    )
    kb.add(
        types.KeyboardButton("💳 Deposit"),
        types.KeyboardButton("🎁 Referral"),
    )
    kb.add(
        types.KeyboardButton("🆘 Support"),
        types.KeyboardButton("⚙️ Method"),
    )
    if is_admin(user_id):
        kb.add(types.KeyboardButton("👮 Admin Panel"))
    return kb


def back_to_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("⬅️ Back to Menu"))
    return kb


# ---------------------------------------------------------------------------
# KEYBOARDS (Inline)
# ---------------------------------------------------------------------------
def buy_number_type_inline():
    """দুইটা ইনলাইন বাটন একটার নিচে আরেকটা (vertically stacked) থাকবে।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="prod_whatsapp"))
    kb.add(types.InlineKeyboardButton("✈️ Telegram Number", callback_data="prod_telegram"))
    return kb


def product_detail_keyboard():
    """3 keyboard buttons shown after selecting product, stacked vertically:
    Buy one pcs / Bulk Buy / Back"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(types.KeyboardButton("🛍️ Buy one pcs"))
    kb.add(types.KeyboardButton("📦 Bulk Buy"))
    kb.add(types.KeyboardButton("⬅️ Back"))
    return kb


def referral_inline():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔗 Share your link and earn!", switch_inline_query="join_now"))
    return kb


def upload_file_product_inline():
    """Admin ফাইল আপলোডের আগে কোন প্রোডাক্টের স্টক আপডেট হবে সেটা বেছে নেয়।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="uploadprod_whatsapp"),
        types.InlineKeyboardButton("✈️ Telegram Number", callback_data="uploadprod_telegram"),
    )
    return kb


def upload_confirm_inline():
    """Stock ফাইল parse হওয়ার পর আসলে stock এ যুক্ত করার আগে কনফার্মেশন।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Confirm & Add", callback_data="stockup_confirm"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="stockup_cancel"),
    )
    return kb


def set_price_product_inline():
    """Admin price পরিবর্তনের আগে কোন প্রোডাক্টের price বদলাবে সেটা বেছে নেয়।"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="priceprod_whatsapp"),
        types.InlineKeyboardButton("✈️ Telegram Number", callback_data="priceprod_telegram"),
    )
    return kb


def admin_panel_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Upload File", callback_data="admin_upload_file"),
        types.InlineKeyboardButton("💰 Set Price", callback_data="admin_set_price_stock"),
    )
    kb.add(
        types.InlineKeyboardButton("👥 Users List", callback_data="admin_users_list"),
        types.InlineKeyboardButton("📊 Statistics", callback_data="admin_statistics"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("💵 Add/Remove Balance", callback_data="admin_balance_edit"),
    )
    kb.add(
        types.InlineKeyboardButton("🧾 Orders", callback_data="admin_orders"),
        types.InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_bot_settings"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_back_to_menu"))
    return kb


def bot_settings_inline():
    """⚙️ Bot Settings সাব-মেনু: বর্তমান ভ্যালুসহ বাটন দেখায়, চাপলে বদলানো যায়।"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            f"🎁 Referral Bonus: {bot_settings['referral_bonus']} BDT",
            callback_data="settings_referral_bonus",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            f"💳 Deposit Limit: {bot_settings['min_deposit']} - {bot_settings['max_deposit']} BDT",
            callback_data="settings_deposit_limits",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🛠️ Maintenance Mode: " + ("✅ ON" if bot_settings["maintenance_mode"] else "❌ OFF"),
            callback_data="settings_toggle_maintenance",
        )
    )
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="settings_back"))
    return kb


# ---------------------------------------------------------------------------
# TEXT TEMPLATES
# ---------------------------------------------------------------------------
def profile_text(u):
    """প্রোফাইলের ডিটেইলস Bold লেবেল + <code> (monospace) ভ্যালু ফরম্যাটে।"""
    return (
        f"👤 <b>Profile</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{u['id']}</code>\n"
        f"👤 <b>Full Name:</b> <code>{u['full_name']}</code>\n"
        f"📝 <b>Username:</b> <code>{u['username']}</code>\n"
        f"💰 <b>Balance:</b> <code>{fmt_amount(u['balance'])}</code>\n"
        f"📊 <b>Total Purchased:</b> <code>{u['total_purchased']}</code>\n"
        f"💸 <b>Today Spent:</b> <code>{fmt_amount(u['today_spent'])}</code>\n"
        f"💳 <b>Today Deposit:</b> <code>{fmt_amount(u['today_deposit'])}</code>"
    )


def referral_text(user_id, data):
    return (
        "🎁 <b>Referral Program</b>\n\n\n"
        f"🎁 Your Referral Link:\nhttps://t.me/{BOT_USERNAME}?start={user_id}\n\n"
        f"💰 Bonus per referral: {fmt_amount(bot_settings['referral_bonus'])}\n"
        f"👥 Total referrals: {data['referrals']}\n"
        f"💵 Total earned: {fmt_amount(data['earned'])}"
    )


def product_detail_text(p):
    return (
        f"<b>{p['name']}</b>\n\n"
        f"💵 Price: {fmt_amount(p['price'])}\n"
        f"📦 Total Stock: {p['stock']}\n"
        f"📝 Description: {p['description']}"
    )


def purchase_success_text(order):
    return (
        "🎉 <b>Purchase Successful!</b>\n\n"
        f"🆔 Order ID: {order['order_id']}\n"
        f"📦 Product: {order['product_name']}\n"
        f"🔢 Quantity: {order['qty']} pcs\n"
        f"💵 Per piece: {fmt_amount(order['price'])}\n"
        f"💰 Total: {fmt_amount(order['total'])}\n"
        f"💳 Remaining Balance: {fmt_amount(order['remaining_balance'])}\n"
        f"📅 Date: {order['date']}\n\n"
        f"👨‍💻 Number: {order.get('number', 'N/A')}\n"
        f"📥 OTP Link: {order.get('otp_link', 'N/A')}"
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if maintenance_block(message):
        return

    uid = message.from_user.id
    is_new_user = uid not in users

    u = get_user(message)
    u["id"] = uid
    user_state[uid] = {"menu": "main"}

    # --- Referral: /start <referrer_id> ---
    parts = (message.text or "").split(maxsplit=1)
    if is_new_user and len(parts) > 1 and parts[1].strip().isdigit():
        referrer_id = int(parts[1].strip())
        if referrer_id != uid and referrer_id in users and not u.get("referred_by"):
            u["referred_by"] = referrer_id
            referrer = users[referrer_id]
            bonus = bot_settings["referral_bonus"]
            referrer["referrals"] += 1
            referrer["earned"] += bonus
            referrer["balance"] += bonus
            try:
                bot.send_message(
                    referrer_id,
                    "🎉 <b>New Referral!</b>\n\n"
                    f"আপনার লিংক দিয়ে একজন নতুন ইউজার জয়েন করেছে।\n"
                    f"💰 বোনাস যুক্ত হয়েছে: {fmt_amount(bonus)}",
                )
            except Exception:
                pass  # referrer হয়তো বটকে ব্লক করেছে

    bot.send_message(message.chat.id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))


# ---------------------------------------------------------------------------
# MAIN MENU (Reply keyboard) HANDLERS
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🛒 Buy Number")
def menu_buy_number(message):
    if maintenance_block(message):
        return
    user_state[message.from_user.id] = {"menu": "buy_number"}
    bot.send_message(message.chat.id, "Select number type:", reply_markup=buy_number_type_inline())


@bot.message_handler(func=lambda m: m.text == "👤 Profile")
def menu_profile(message):
    if maintenance_block(message):
        return
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "profile"}
    bot.send_message(message.chat.id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))


@bot.message_handler(func=lambda m: m.text == "💳 Deposit")
def menu_deposit(message):
    if maintenance_block(message):
        return
    user_state[message.from_user.id] = {"menu": "deposit"}
    limit_line = ""
    if bot_settings["min_deposit"] or bot_settings["max_deposit"]:
        limit_line = (
            f"📉 Min Deposit: {fmt_amount(bot_settings['min_deposit'])}\n"
            f"📈 Max Deposit: {fmt_amount(bot_settings['max_deposit'])}\n\n"
        )
    # TODO: Deposit method list / payment gateway integration বসবে এখানে
    bot.send_message(
        message.chat.id,
        "💳 <b>Deposit</b>\n\n" + limit_line + "Deposit করার পদ্ধতি এখানে যুক্ত হবে।",
        reply_markup=back_to_main_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "🎁 Referral")
def menu_referral(message):
    if maintenance_block(message):
        return
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "referral"}
    bot.send_message(
        message.chat.id,
        referral_text(u["id"], u),
        reply_markup=referral_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "🆘 Support")
def menu_support(message):
    if maintenance_block(message):
        return
    user_state[message.from_user.id] = {"menu": "support"}
    # TODO: real support username/link
    bot.send_message(
        message.chat.id,
        "🆘 <b>Support</b>\n\nযেকোনো সমস্যায় যোগাযোগ করুন: @your_support_username",
        reply_markup=back_to_main_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Method")
def menu_method(message):
    if maintenance_block(message):
        return
    user_state[message.from_user.id] = {"menu": "method"}
    # TODO: payment method list (bKash/Nagad/Binance ইত্যাদি)
    bot.send_message(
        message.chat.id,
        "⚙️ <b>Payment Method</b>\n\nMethod সমূহ এখানে দেখানো হবে।",
        reply_markup=back_to_main_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text in ["⬅️ Back to Menu", "⬅️ Back"])
def go_back(message):
    """সব জায়গা থেকে ধাপে ধাপে Back করার লজিক।"""
    uid = message.from_user.id

    # Buy Number মেনুর যেকোনো ধাপ (product select / product detail / bulk buy ইনপুট) থেকে
    # Back চাপলে সরাসরি Main Menu তে ফিরে যেতে হবে।
    # সব জায়গা থেকে -> main menu
    u = get_user(message)
    u["id"] = uid
    user_state[uid] = {"menu": "main"}
    bot.send_message(message.chat.id, "🏠 Main Menu", reply_markup=main_menu_keyboard(uid))


# ---------------------------------------------------------------------------
# INLINE CALLBACKS: product selection (WhatsApp / Telegram)
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("prod_"))
def cb_product_select(call):
    if maintenance_block(call):
        bot.answer_callback_query(call.id)
        return
    product_key = call.data.replace("prod_", "")   # "whatsapp" / "telegram"
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    user_state[call.from_user.id] = {"menu": "product_detail", "product": product_key}

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, product_detail_text(p))
    bot.send_message(
        call.message.chat.id,
        "Please choose an option below:",
        reply_markup=product_detail_keyboard(),
    )


# ---------------------------------------------------------------------------
# PRODUCT DETAIL MENU: Buy one pcs / Bulk Buy
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🛍️ Buy one pcs")
def buy_one_pcs(message):
    if maintenance_block(message):
        return
    uid = message.from_user.id
    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)

    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    u = get_user(message)
    u["id"] = uid

    # --- validation ---
    if p["price"] <= 0:
        bot.send_message(message.chat.id, "⚠️ এই প্রোডাক্টের দাম এখনও সেট করা হয়নি। এডমিনের সাথে যোগাযোগ করুন।")
        return

    stock_list = p.setdefault("stock_list", [])
    if not stock_list:
        bot.send_message(
            message.chat.id,
            "❌ <b>Stock Out!</b>\n\nদুঃখিত, এই প্রোডাক্টের স্টক এখন খালি। কিছুক্ষণ পরে চেষ্টা করুন।",
            reply_markup=product_detail_keyboard(),
        )
        return

    if u["balance"] < p["price"]:
        bot.send_message(
            message.chat.id,
            "❌ <b>Insufficient Balance!</b>\n\n"
            f"💰 আপনার ব্যালেন্স: {fmt_amount(u['balance'])}\n"
            f"💵 প্রয়োজন: {fmt_amount(p['price'])}\n\n"
            "অনুগ্রহ করে আগে Deposit করুন।",
            reply_markup=product_detail_keyboard(),
        )
        return

    # --- fulfill purchase (real balance + stock deduction) ---
    item = stock_list.pop(0)
    p["stock"] = len(stock_list)   # TODO: database তে persist করুন (এখনো in-memory)

    u["balance"] -= p["price"]
    u["total_purchased"] += 1
    u["today_spent"] += p["price"]

    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    order = {
        "order_id": order_id,
        "user_id": uid,
        "product_name": p["name"],
        "qty": 1,
        "price": p["price"],
        "total": p["price"],
        "remaining_balance": u["balance"],
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "number": item["number"],
        "otp_link": item["otp_link"],
    }
    orders[order_id] = order

    bot.send_message(message.chat.id, purchase_success_text(order))
    # Purchase এর পরেও buy মেনুতেই থাকবে (point 9)
    bot.send_message(message.chat.id, "আরও কিছু কিনতে চান?", reply_markup=product_detail_keyboard())


@bot.message_handler(func=lambda m: m.text == "📦 Bulk Buy")
def bulk_buy(message):
    if maintenance_block(message):
        return
    uid = message.from_user.id
    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)

    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    if p["price"] <= 0:
        bot.send_message(message.chat.id, "⚠️ এই প্রোডাক্টের দাম এখনও সেট করা হয়নি। এডমিনের সাথে যোগাযোগ করুন।")
        return

    stock_list = p.setdefault("stock_list", [])
    if not stock_list:
        bot.send_message(
            message.chat.id,
            "❌ <b>Stock Out!</b>\n\nদুঃখিত, এই প্রোডাক্টের স্টক এখন খালি।",
            reply_markup=product_detail_keyboard(),
        )
        return

    bot.send_message(
        message.chat.id,
        "📦 <b>Bulk Buy</b>\n\n"
        f"📊 বর্তমান স্টক: {len(stock_list)} পিস\n"
        f"💵 প্রতি পিস: {fmt_amount(p['price'])}\n\n"
        "কত পিস কিনতে চান, সংখ্যায় লিখে পাঠান (যেমন: 5):",
    )
    bot.register_next_step_handler(message, process_bulk_quantity)


def process_bulk_quantity(message):
    """Bulk Buy এর quantity ইনপুট প্রসেস করে, balance/stock check করে অর্ডার সম্পন্ন করে।"""
    uid = message.from_user.id
    text = (message.text or "").strip()

    # ইউজার মাঝপথে Back/Menu এ চলে যেতে চাইলে next-step এর সাথে conflict এড়ানো হচ্ছে
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ আগে একটা প্রোডাক্ট সিলেক্ট করুন।")
        return

    if not text.isdigit() or int(text) <= 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি সংখ্যা লিখুন (যেমন: 5)।")
        bot.register_next_step_handler(message, process_bulk_quantity)
        return

    qty = int(text)
    stock_list = p.setdefault("stock_list", [])

    if qty > len(stock_list):
        bot.send_message(
            message.chat.id,
            "❌ <b>Stock Not Enough!</b>\n\n"
            f"📦 বর্তমান স্টক: {len(stock_list)} পিস\nএর বেশি পরিমাণ এখন কেনা সম্ভব নয়।",
            reply_markup=product_detail_keyboard(),
        )
        return

    u = get_user(message)
    u["id"] = uid
    total_price = p["price"] * qty

    if u["balance"] < total_price:
        bot.send_message(
            message.chat.id,
            "❌ <b>Insufficient Balance!</b>\n\n"
            f"💰 আপনার ব্যালেন্স: {fmt_amount(u['balance'])}\n"
            f"💵 প্রয়োজন: {fmt_amount(total_price)} ({qty} x {p['price']})\n\n"
            "অনুগ্রহ করে আগে Deposit করুন।",
            reply_markup=product_detail_keyboard(),
        )
        return

    # --- fulfill purchase (real balance + stock deduction) ---
    purchased_items = [stock_list.pop(0) for _ in range(qty)]
    p["stock"] = len(stock_list)   # TODO: database তে persist করুন (এখনো in-memory)

    u["balance"] -= total_price
    u["total_purchased"] += qty
    u["today_spent"] += total_price

    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    order = {
        "order_id": order_id,
        "user_id": uid,
        "product_name": p["name"],
        "qty": qty,
        "price": p["price"],
        "total": total_price,
        "remaining_balance": u["balance"],
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "number": purchased_items[0]["number"] if qty == 1 else "📎 নিচের ফাইলে দেখুন",
        "otp_link": purchased_items[0]["otp_link"] if qty == 1 else "📎 নিচের ফাইলে দেখুন",
        "items": purchased_items,
    }
    orders[order_id] = order

    bot.send_message(message.chat.id, purchase_success_text(order))

    if qty > 1:
        lines = [f"{it['number']}|{it['otp_link']}" for it in purchased_items]
        file_content = "\n".join(lines).encode("utf-8")
        filename = f"{order_id}_{product_key}.txt"
        bot.send_document(
            message.chat.id,
            io.BytesIO(file_content),
            visible_file_name=filename,
            caption=f"📥 আপনার {qty} টি {p['name']}",
        )

    bot.send_message(message.chat.id, "আরও কিছু কিনতে চান?", reply_markup=product_detail_keyboard())


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "👮 Admin Panel" and is_admin(m.from_user.id))
def menu_admin_panel(message):
    user_state[message.from_user.id] = {"menu": "admin_panel"}
    bot.send_message(message.chat.id, "👮 <b>Admin Panel</b>", reply_markup=admin_panel_inline())


# ---------------------------------------------------------------------------
# ADMIN PANEL: inline button callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_") and is_admin(c.from_user.id))
def cb_admin_panel(call):
    uid = call.from_user.id
    data = call.data
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    if data == "admin_upload_file":
        user_state[uid] = {"menu": "admin_upload_file_select"}
        bot.send_message(
            chat_id,
            "📤 কোন প্রোডাক্টের জন্য স্টক ফাইল আপলোড করবেন?",
            reply_markup=upload_file_product_inline(),
        )

    elif data == "admin_set_price_stock":
        user_state[uid] = {"menu": "admin_set_price_select"}
        bot.send_message(
            chat_id,
            "💰 কোন প্রোডাক্টের price পরিবর্তন করবেন?",
            reply_markup=set_price_product_inline(),
        )

    elif data == "admin_users_list":
        # TODO: pagination সহ real user list
        bot.send_message(chat_id, f"👥 মোট ইউজার: {len(users)}\n(তালিকা লজিক পরে যুক্ত হবে)")

    elif data == "admin_statistics":
        # TODO: real total sales, revenue, today stats ইত্যাদি
        bot.send_message(
            chat_id,
            "📊 <b>Statistics</b>\n\n"
            f"👥 Total Users: {len(users)}\n"
            f"🧾 Total Orders: {len(orders)}\n"
            "💰 Total Revenue: TODO\n"
            "📅 Today's Sales: TODO",
        )

    elif data == "admin_broadcast":
        user_state[uid] = {"menu": "admin_broadcast"}
        bot.send_message(chat_id, "📢 যে মেসেজটি সব ইউজারকে পাঠাতে চান লিখুন।")
        # TODO: bot.register_next_step_handler(call.message, process_broadcast_message)

    elif data == "admin_balance_edit":
        # TODO: user_id চাইবে, তারপর amount +/- করবে
        bot.send_message(chat_id, "💵 Balance যোগ/বিয়োগ করার লজিক এখানে যুক্ত হবে।")

    elif data == "admin_orders":
        # TODO: pagination সহ real orders list / filter
        bot.send_message(chat_id, f"🧾 মোট অর্ডার: {len(orders)}\n(তালিকা লজিক পরে যুক্ত হবে)")

    elif data == "admin_bot_settings":
        user_state[uid] = {"menu": "admin_bot_settings"}
        bot.send_message(
            chat_id,
            "⚙️ <b>Bot Settings</b>\n\nযে সেটিংসটি বদলাতে চান, তাতে ক্লিক করুন।",
            reply_markup=bot_settings_inline(),
        )

    elif data == "admin_back_to_menu":
        user_state[uid] = {"menu": "main"}
        bot.send_message(chat_id, "🏠 Main Menu", reply_markup=main_menu_keyboard(uid))


# ---------------------------------------------------------------------------
# ADMIN PANEL: ⚙️ Bot Settings সাব-মেনুর callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("settings_") and is_admin(c.from_user.id))
def cb_bot_settings(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "settings_back":
        user_state[uid] = {"menu": "admin_panel"}
        bot.send_message(chat_id, "👮 <b>Admin Panel</b>", reply_markup=admin_panel_inline())
        return

    if data == "settings_toggle_maintenance":
        bot_settings["maintenance_mode"] = not bot_settings["maintenance_mode"]   # TODO: DB তে persist করুন
        status = "চালু ✅" if bot_settings["maintenance_mode"] else "বন্ধ ❌"
        bot.send_message(
            chat_id,
            f"🛠️ Maintenance Mode এখন {status} করা হয়েছে।",
            reply_markup=bot_settings_inline(),
        )
        return

    if data == "settings_referral_bonus":
        user_state[uid] = {"menu": "admin_settings_referral_bonus"}
        bot.send_message(
            chat_id,
            "🎁 প্রতি রেফারেলে নতুন বোনাস অ্যামাউন্ট লিখে পাঠান।\n"
            f"বর্তমান: {fmt_amount(bot_settings['referral_bonus'])}\n\n"
            "উদাহরণ: 15 অথবা 20.50",
        )
        bot.register_next_step_handler(call.message, process_settings_referral_bonus)
        return

    if data == "settings_deposit_limits":
        user_state[uid] = {"menu": "admin_settings_deposit_limits"}
        bot.send_message(
            chat_id,
            "💳 Min ও Max Deposit লিখে পাঠান, কমা দিয়ে আলাদা করে।\n"
            f"বর্তমান: {bot_settings['min_deposit']} , {bot_settings['max_deposit']}\n\n"
            "উদাহরণ: 50,5000  (সীমা রাখতে না চাইলে 0,0 লিখুন)",
        )
        bot.register_next_step_handler(call.message, process_settings_deposit_limits)
        return


def process_settings_referral_bonus(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    try:
        new_bonus = float(text)
    except ValueError:
        new_bonus = None

    if new_bonus is None or new_bonus < 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি সংখ্যা লিখুন (যেমন: 15 অথবা 20.50)।")
        bot.register_next_step_handler(message, process_settings_referral_bonus)
        return

    if new_bonus == int(new_bonus):
        new_bonus = int(new_bonus)

    bot_settings["referral_bonus"] = new_bonus   # TODO: DB তে persist করুন
    bot.send_message(
        message.chat.id,
        f"✅ Referral Bonus আপডেট হয়েছে: {fmt_amount(new_bonus)}",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


def process_settings_deposit_limits(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(p.replace(".", "", 1).isdigit() for p in parts):
        bot.send_message(message.chat.id, "⚠️ সঠিক ফরম্যাটে লিখুন, যেমন: 50,5000")
        bot.register_next_step_handler(message, process_settings_deposit_limits)
        return

    min_dep, max_dep = float(parts[0]), float(parts[1])
    min_dep = int(min_dep) if min_dep == int(min_dep) else min_dep
    max_dep = int(max_dep) if max_dep == int(max_dep) else max_dep

    if max_dep and min_dep > max_dep:
        bot.send_message(message.chat.id, "⚠️ Min, Max এর থেকে বেশি হতে পারবে না। আবার লিখুন।")
        bot.register_next_step_handler(message, process_settings_deposit_limits)
        return

    bot_settings["min_deposit"] = min_dep   # TODO: DB তে persist করুন
    bot_settings["max_deposit"] = max_dep
    bot.send_message(
        message.chat.id,
        f"✅ Deposit Limit আপডেট হয়েছে: {min_dep} - {max_dep} BDT",
        reply_markup=bot_settings_inline(),
    )
    user_state[uid] = {"menu": "admin_bot_settings"}


@bot.callback_query_handler(func=lambda c: c.data.startswith("uploadprod_") and is_admin(c.from_user.id))
def cb_admin_upload_product_select(call):
    product_key = call.data.replace("uploadprod_", "")
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    user_state[call.from_user.id] = {"menu": "admin_upload_file", "product": product_key}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"📤 <b>{p['name']}</b> এর জন্য স্টক ফাইল পাঠান (.txt / .csv / .xlsx)।\n\n"
        "📄 .txt বা .csv এ প্রতি লাইনে একটি এন্ট্রি এই ফরম্যাটে দিন:\n"
        "<code>number|otp_link</code>\n\n"
        "📊 .xlsx এ প্রথম কলামে number, দ্বিতীয় কলামে otp_link দিন।\n\n"
        "OTP লিংক না থাকলে শুধু:\n"
        "<code>number</code>\n\n"
        "ফাইল পাঠানোর পর stock এ যুক্ত করার আগে আপনাকে confirm করতে বলা হবে।",
    )


def parse_stock_from_xlsx(file_bytes):
    """xlsx ফাইলের প্রথম শীট থেকে (number, otp_link) কলাম দুটো রিড করে।
    হেডার-জাতীয় লাইন (number/phone ইত্যাদি) স্বয়ংক্রিয়ভাবে স্কিপ হয়।"""
    from openpyxl import load_workbook  # লেজি ইমপোর্ট: শুধু .xlsx আপলোড হলেই দরকার

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb.active
    items = []
    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        number = row[0]
        otp_link = row[1] if len(row) > 1 else None
        if number is None:
            continue
        number = str(number).strip()
        if not number or number.lower() in ("number", "phone", "phone number", "otp_link", "otp"):
            continue
        otp_link = str(otp_link).strip() if otp_link not in (None, "") else "N/A"
        items.append({"number": number, "otp_link": otp_link})
    return items


@bot.message_handler(content_types=["document"])
def handle_document_upload(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = user_state.get(uid, {})
    if state.get("menu") != "admin_upload_file":
        return

    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ প্রোডাক্ট খুঁজে পাওয়া যায়নি। Admin Panel থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith((".txt", ".csv", ".xlsx")):
        bot.send_message(message.chat.id, "⚠️ শুধুমাত্র .txt, .csv বা .xlsx ফাইল সাপোর্ট করে। আবার পাঠান।")
        return

    # ফাইল ডাউনলোড ও parse করা হচ্ছে - এখনো stock এ যুক্ত হয়নি, শুধু preview/confirmation এর জন্য
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে: {e}")
        return

    parsed_items = []
    try:
        if file_name.lower().endswith(".xlsx"):
            parsed_items = parse_stock_from_xlsx(downloaded)
        else:
            text = downloaded.decode("utf-8", errors="ignore")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if "|" in line:
                    number, otp_link = line.split("|", 1)
                elif "," in line:
                    number, otp_link = line.split(",", 1)
                else:
                    number, otp_link = line, "N/A"
                number = number.strip()
                otp_link = otp_link.strip() or "N/A"
                if number:
                    parsed_items.append({"number": number, "otp_link": otp_link})
    except ImportError:
        bot.send_message(
            message.chat.id,
            "❌ .xlsx ফাইল পড়ার জন্য সার্ভারে openpyxl লাইব্রেরি ইনস্টল নেই।\n"
            "ইনস্টল করুন: pip install openpyxl",
        )
        return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল পার্স করতে সমস্যা হয়েছে: {e}")
        return

    if not parsed_items:
        bot.send_message(message.chat.id, "⚠️ ফাইলে কোনো valid লাইন পাওয়া যায়নি। ফরম্যাট চেক করে আবার পাঠান।")
        return

    # confirm করার আগ পর্যন্ত pending_items এ রাখা হচ্ছে, stock_list এ যুক্ত করা হয়নি
    user_state[uid] = {
        "menu": "admin_upload_confirm",
        "product": product_key,
        "pending_items": parsed_items,
    }

    preview = "\n".join(f"• {it['number']} | {it['otp_link']}" for it in parsed_items[:3])
    if len(parsed_items) > 3:
        preview += f"\n...আরও {len(parsed_items) - 3} টি"

    current_stock = len(p.setdefault("stock_list", []))
    bot.send_message(
        message.chat.id,
        "🔎 <b>Confirm Stock Upload</b>\n\n"
        f"📦 প্রোডাক্ট: {p['name']}\n"
        f"📄 ফাইল: {file_name}\n"
        f"🔢 ফাইলে পাওয়া গেছে: {len(parsed_items)} পিস\n\n"
        f"প্রিভিউ:\n{preview}\n\n"
        f"📊 বর্তমান স্টক: {current_stock} → Confirm করলে হবে: {current_stock + len(parsed_items)}\n\n"
        "এই এন্ট্রিগুলো stock এ যুক্ত করতে চান?",
        reply_markup=upload_confirm_inline(),
    )


@bot.callback_query_handler(
    func=lambda c: c.data in ("stockup_confirm", "stockup_cancel") and is_admin(c.from_user.id)
)
def cb_admin_upload_confirm(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    state = user_state.get(uid, {})
    bot.answer_callback_query(call.id)

    if state.get("menu") != "admin_upload_confirm":
        bot.send_message(chat_id, "⚠️ কোনো pending upload নেই। আগে একটা ফাইল পাঠান।")
        return

    if call.data == "stockup_cancel":
        bot.send_message(
            chat_id,
            "❌ Upload বাতিল করা হয়েছে। কিছুই stock এ যুক্ত হয়নি।",
            reply_markup=admin_panel_inline(),
        )
        user_state[uid] = {"menu": "admin_panel"}
        return

    # stockup_confirm
    product_key = state.get("product")
    pending_items = state.get("pending_items", [])
    p = products.get(product_key)

    if not p or not pending_items:
        bot.send_message(chat_id, "⚠️ Pending ডেটা খুঁজে পাওয়া যায়নি। আবার আপলোড করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    stock_list = p.setdefault("stock_list", [])
    stock_list.extend(pending_items)
    p["stock"] = len(stock_list)   # TODO: database তে persist করুন (এখনো in-memory)

    bot.send_message(
        chat_id,
        "✅ <b>ফাইল আপলোড সম্পন্ন!</b>\n\n"
        f"📦 প্রোডাক্ট: {p['name']}\n"
        f"➕ যুক্ত হয়েছে: {len(pending_items)} পিস\n"
        f"📊 বর্তমান মোট স্টক: {p['stock']} পিস",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# ADMIN: Price পরিবর্তন (Set Price)
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("priceprod_") and is_admin(c.from_user.id))
def cb_admin_price_product_select(call):
    product_key = call.data.replace("priceprod_", "")
    p = products.get(product_key)
    if not p:
        bot.answer_callback_query(call.id, "Product not found.")
        return

    user_state[call.from_user.id] = {"menu": "admin_set_price", "product": product_key}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"💰 <b>{p['name']}</b> এর জন্য নতুন price লিখে পাঠান।\n"
        f"বর্তমান price: {fmt_amount(p['price'])}\n\n"
        "উদাহরণ: 50 অথবা 49.99",
    )
    bot.register_next_step_handler(call.message, process_set_price)


def process_set_price(message):
    """Admin এর দেওয়া নতুন price validate করে products dict এ সেট করে।"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    text = (message.text or "").strip()

    # ইউজার মাঝপথে Back/Menu এ চলে যেতে চাইলে next-step এর সাথে conflict এড়ানো হচ্ছে
    if text in ["⬅️ Back", "⬅️ Back to Menu"]:
        go_back(message)
        return

    state = user_state.get(uid, {})
    product_key = state.get("product")
    p = products.get(product_key)
    if not p:
        bot.send_message(message.chat.id, "⚠️ প্রোডাক্ট খুঁজে পাওয়া যায়নি। Admin Panel থেকে আবার চেষ্টা করুন।")
        user_state[uid] = {"menu": "admin_panel"}
        return

    try:
        new_price = float(text)
    except ValueError:
        new_price = None

    if new_price is None or new_price <= 0:
        bot.send_message(message.chat.id, "⚠️ সঠিক একটি price সংখ্যায় লিখুন (যেমন: 50 অথবা 49.99)।")
        bot.register_next_step_handler(message, process_set_price)
        return

    if new_price == int(new_price):
        new_price = int(new_price)

    old_price = p["price"]
    p["price"] = new_price   # TODO: database তে persist করুন (এখনো in-memory)

    bot.send_message(
        message.chat.id,
        "✅ <b>Price আপডেট হয়েছে!</b>\n\n"
        f"📦 প্রোডাক্ট: {p['name']}\n"
        f"💵 পুরাতন Price: {fmt_amount(old_price)}\n"
        f"💵 নতুন Price: {fmt_amount(p['price'])}",
        reply_markup=admin_panel_inline(),
    )
    user_state[uid] = {"menu": "admin_panel"}


# ---------------------------------------------------------------------------
# FALLBACK
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    # TODO: register_next_step_handler গুলো active থাকলে সেগুলো এখানে conflict না করার
    #       ব্যাপারে খেয়াল রাখবেন
    bot.send_message(
        message.chat.id,
        "❓ বুঝতে পারিনি। নিচের মেনু থেকে বেছে নিন।",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# RUN (Polling for local test / Webhook for Railway)
# ---------------------------------------------------------------------------
def run_polling():
    print("Bot running in POLLING mode...")
    bot.remove_webhook()
    bot.infinity_polling()


def run_webhook():
    from flask import Flask, request

    app = Flask(__name__)
    webhook_path = f"/webhook/{BOT_TOKEN}"

    @app.route(webhook_path, methods=["POST"])
    def telegram_webhook():
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200

    @app.route("/", methods=["GET"])
    def index():
        return "Bot is running.", 200

    bot.remove_webhook()
    bot.set_webhook(url=f"{RAILWAY_URL}{webhook_path}")
    print(f"Bot running in WEBHOOK mode on port {PORT} -> {RAILWAY_URL}{webhook_path}")
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    if RAILWAY_URL:
        run_webhook()
    else:
        run_polling()
