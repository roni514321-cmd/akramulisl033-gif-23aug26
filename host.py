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
    pip install pyTelegramBotAPI flask

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

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

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
        }
    return users[uid]


def is_admin(user_id):
    return user_id in ADMIN_IDS


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
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 WhatsApp Number", callback_data="prod_whatsapp"),
        types.InlineKeyboardButton("✈️ Telegram Number", callback_data="prod_telegram"),
    )
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


# ---------------------------------------------------------------------------
# TEXT TEMPLATES
# ---------------------------------------------------------------------------
def profile_text(u):
    return (
        f"🆔 User ID: {u['id']}\n"
        f"👤 Full Name: {u['full_name']}\n"
        f"📝 Username: {u['username']}\n"
        f"💰 Balance: {u['balance']} $/BDT\n"
        f"📊 Total Purchased: {u['total_purchased']}\n"
        f"💸 Today Spent: {u['today_spent']} $/BDT\n"
        f"💳 Today Deposit: {u['today_deposit']} $/BDT"
    )


def referral_text(user_id, data):
    return (
        "🎁 <b>Referral Program</b>\n\n\n"
        f"🎁 Your Referral Link:\nhttps://t.me/vertual_shop_bot?start={user_id}\n\n"
        f"💰 Bonus per referral: $/BDT\n"          # TODO: real bonus amount
        f"👥 Total referrals: {data['referrals']}\n"
        f"💵 Total earned: {data['earned']} $/BDT"
    )


def product_detail_text(p):
    return (
        f"<b>{p['name']}</b>\n\n"
        f"💵 Price: {p['price']} $/BDT\n"
        f"📦 Total Stock: {p['stock']}\n"
        f"📝 Description: {p['description']}"
    )


def purchase_success_text(order):
    return (
        "🎉 <b>Purchase Successful!</b>\n\n"
        f"🆔 Order ID: {order['order_id']}\n"
        f"📦 Product: {order['product_name']}\n"
        f"🔢 Quantity: {order['qty']} pcs\n"
        f"💵 Per piece: {order['price']} $/BDT\n"
        f"💰 Total: {order['total']} $/BDT\n"
        f"💳 Remaining Balance: {order['remaining_balance']} $/BDT\n"
        f"📅 Date: {order['date']}\n\n"
        f"👨‍💻 Number: {order.get('number', 'N/A')}\n"
        f"📥 OTP Link: {order.get('otp_link', 'N/A')}"
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "main"}

    # TODO: এখানে referral parameter (/start=xxxxx) handle করে referral bonus যোগ করবেন
    welcome = (
        f"🆔 User ID: {u['id']}\n"
        f"👤 Full Name: {u['full_name']}\n"
        f"📝 Username: {u['username']}\n"
        f"💰 Balance: {u['balance']} $/BDT\n"
        f"📊 Total Purchased: {u['total_purchased']}\n"
        f"💸 Today Spent: {u['today_spent']} $/BDT\n"
        f"💳 Today Deposit: {u['today_deposit']} $/BDT"
    )
    bot.send_message(message.chat.id, welcome, reply_markup=main_menu_keyboard(u["id"]))


# ---------------------------------------------------------------------------
# MAIN MENU (Reply keyboard) HANDLERS
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🛒 Buy Number")
def menu_buy_number(message):
    user_state[message.from_user.id] = {"menu": "buy_number"}
    bot.send_message(message.chat.id, "Select number type:", reply_markup=buy_number_type_inline())


@bot.message_handler(func=lambda m: m.text == "👤 Profile")
def menu_profile(message):
    u = get_user(message)
    u["id"] = message.from_user.id
    user_state[message.from_user.id] = {"menu": "profile"}
    bot.send_message(message.chat.id, profile_text(u), reply_markup=main_menu_keyboard(u["id"]))


@bot.message_handler(func=lambda m: m.text == "💳 Deposit")
def menu_deposit(message):
    user_state[message.from_user.id] = {"menu": "deposit"}
    # TODO: Deposit method list / payment gateway integration বসবে এখানে
    bot.send_message(
        message.chat.id,
        "💳 <b>Deposit</b>\n\nDeposit করার পদ্ধতি এখানে যুক্ত হবে।",
        reply_markup=back_to_main_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "🎁 Referral")
def menu_referral(message):
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
    user_state[message.from_user.id] = {"menu": "support"}
    # TODO: real support username/link
    bot.send_message(
        message.chat.id,
        "🆘 <b>Support</b>\n\nযেকোনো সমস্যায় যোগাযোগ করুন: @your_support_username",
        reply_markup=back_to_main_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Method")
def menu_method(message):
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
    state = user_state.get(uid, {"menu": "main"})

    if state.get("menu") == "product_detail":
        # product detail -> product type selection
        user_state[uid] = {"menu": "buy_number"}
        bot.send_message(message.chat.id, "Select number type:", reply_markup=buy_number_type_inline())
        bot.send_message(message.chat.id, "মেনুতে ফিরে যেতে নিচের বাটন ব্যবহার করুন।", reply_markup=back_to_main_keyboard())
        return

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
            f"💰 আপনার ব্যালেন্স: {u['balance']} $/BDT\n"
            f"💵 প্রয়োজন: {p['price']} $/BDT\n\n"
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
        f"💵 প্রতি পিস: {p['price']} $/BDT\n\n"
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
            f"💰 আপনার ব্যালেন্স: {u['balance']} $/BDT\n"
            f"💵 প্রয়োজন: {total_price} $/BDT ({qty} x {p['price']})\n\n"
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
        # TODO: referral bonus amount, deposit methods, min/max deposit, maintenance mode ইত্যাদি
        bot.send_message(chat_id, "⚙️ <b>Bot Settings</b>\n\nসেটিংস অপশনগুলো এখানে যুক্ত হবে।")

    elif data == "admin_back_to_menu":
        user_state[uid] = {"menu": "main"}
        bot.send_message(chat_id, "🏠 Main Menu", reply_markup=main_menu_keyboard(uid))


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
        f"📤 <b>{p['name']}</b> এর জন্য স্টক ফাইল পাঠান (.txt / .csv)।\n\n"
        "প্রতি লাইনে একটি এন্ট্রি এই ফরম্যাটে দিন:\n"
        "<code>number|otp_link</code>\n\n"
        "OTP লিংক না থাকলে শুধু:\n"
        "<code>number</code>\n\n"
        "ফাইল পাঠানোর পর stock এ যুক্ত করার আগে আপনাকে confirm করতে বলা হবে।",
    )


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
    if not file_name.lower().endswith((".txt", ".csv")):
        bot.send_message(message.chat.id, "⚠️ শুধুমাত্র .txt বা .csv ফাইল সাপোর্ট করে। আবার পাঠান।")
        return

    # ফাইল ডাউনলোড ও parse করা হচ্ছে - এখনো stock এ যুক্ত হয়নি, শুধু preview/confirmation এর জন্য
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        text = downloaded.decode("utf-8", errors="ignore")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল প্রসেস করতে সমস্যা হয়েছে: {e}")
        return

    parsed_items = []
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
        f"বর্তমান price: {p['price']} $/BDT\n\n"
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
        f"💵 পুরাতন Price: {old_price} $/BDT\n"
        f"💵 নতুন Price: {p['price']} $/BDT",
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
