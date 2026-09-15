# -*- coding: utf-8 -*-
"""
Virtual Number Shop - Telegram Bot (STRUCTURE / UI SKELETON ONLY)
====================================================================
এই ফাইলে শুধুমাত্র বাটন, মেনু নেভিগেশন এবং স্ট্যাকচার তৈরি করা হয়েছে।
সব জায়গায় যেখানে আসল লজিক (payment, database, real stock ইত্যাদি) বসবে
সেখানে  # TODO: ... কমেন্ট দিয়ে চিহ্নিত করা আছে।

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
        "price": 0,          # TODO: real price
        "stock": 0,          # TODO: real stock count
        "description": "WhatsApp verification number.",  # TODO
    },
    "telegram": {
        "name": "Telegram Number",
        "price": 0,          # TODO: real price
        "stock": 0,          # TODO: real stock count
        "description": "Telegram verification number.",  # TODO
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


def admin_panel_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Upload File", callback_data="admin_upload_file"),
        types.InlineKeyboardButton("💰 Set Price/Stock", callback_data="admin_set_price_stock"),
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

    # TODO: real balance check + deduct balance + fetch real number/otp link from stock
    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    order = {
        "order_id": order_id,
        "product_name": p["name"],
        "qty": 1,
        "price": p["price"],
        "total": p["price"],
        "remaining_balance": u["balance"],   # TODO: subtract after real balance check
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "number": "TODO_ASSIGN_NUMBER",
        "otp_link": "TODO_OTP_LINK",
    }
    orders[order_id] = order

    # TODO: total_purchased / today_spent আপডেট করুন real logic দিয়ে
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

    # TODO: quantity জিজ্ঞাসা করা, balance check, তারপর admin এর আপলোড করা ফাইল থেকে
    #       bulk order অনুযায়ী ইউজারকে ফাইল আকারে পাঠানো (point 10)
    bot.send_message(
        message.chat.id,
        "📦 <b>Bulk Buy</b>\n\nকত পিস কিনতে চান লিখে পাঠান।\n(এই লজিকটা পরে যুক্ত হবে)",
    )
    # TODO: bot.register_next_step_handler(message, process_bulk_quantity)


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
        user_state[uid] = {"menu": "admin_upload_file"}
        bot.send_message(
            chat_id,
            "📤 কোন প্রোডাক্টের জন্য ফাইল আপলোড করছেন সেটার ফাইল পাঠান।\n"
            "(WhatsApp Number / Telegram Number stock ফাইল - .txt/.csv/.xlsx)",
        )
        # TODO: bot.register_next_step_handler(call.message, process_admin_file_upload)

    elif data == "admin_set_price_stock":
        # TODO: প্রোডাক্ট বাছাই -> নতুন price/stock ইনপুট -> products dict/DB আপডেট
        bot.send_message(chat_id, "💰 Price/Stock পরিবর্তনের লজিক এখানে যুক্ত হবে।")

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


@bot.message_handler(content_types=["document"])
def handle_document_upload(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    state = user_state.get(uid, {})
    if state.get("menu") != "admin_upload_file":
        return

    # TODO: ফাইল ডাউনলোড করে stock/database এ parse ও সংরক্ষণ করুন
    file_info = bot.get_file(message.document.file_id)
    # downloaded = bot.download_file(file_info.file_path)
    bot.send_message(message.chat.id, f"✅ ফাইল রিসিভ হয়েছে: {message.document.file_name}\n(processing logic পরে যুক্ত হবে)")
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
