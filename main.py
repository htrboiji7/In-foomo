import os
import asyncio
import logging
import re
import threading
from datetime import datetime, timedelta
from io import BytesIO
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import qrcode
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))
FORCE_CHANNELS = [c.strip() for c in os.getenv("FORCE_CHANNELS", "").split(',') if c.strip()]
TARGET_BOT = os.getenv("TARGET_BOT_USERNAME")
USER_SESSION_STRING = os.getenv("USER_SESSION_STRING")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
REFERRAL_CREDITS = int(os.getenv("REFERRAL_CREDITS", 2))
BONUS_CREDITS = int(os.getenv("BONUS_CREDITS", 1))
CACHE_EXPIRE = int(os.getenv("CACHE_EXPIRE", 3600))
UPI_ID = os.getenv("UPI_ID", "your_upi_id@fampay")
BOT_USERNAME = os.getenv("BOT_USERNAME")

client = MongoClient(MONGO_URI)
db = client['number_info_bot']
users = db['users']
protected = db['protected_numbers']
payments = db['pending_payments']
requests_db = db['pending_requests']
referrals_db = db['referrals']

cache = {}

def get_cached(number):
    if number in cache:
        result, expiry = cache[number]
        if datetime.now().timestamp() < expiry:
            return result
        else:
            del cache[number]
    return None

def set_cache(number, result):
    expiry = datetime.now().timestamp() + CACHE_EXPIRE
    cache[number] = (result, expiry)

def init_user(user_id, referrer_id=None):
    if users.find_one({"user_id": user_id}):
        return
    users.insert_one({
        "user_id": user_id,
        "credits": 20,
        "lifetime": False,
        "total_searches": 0,
        "banned": False,
        "joined_at": datetime.now(),
        "referred_by": None,
        "referral_count": 0,
        "last_bonus": datetime.now() - timedelta(days=1)
    })
    if referrer_id and referrer_id != user_id:
        referrer = users.find_one({"user_id": referrer_id, "banned": False})
        if referrer:
            add_credits(referrer_id, REFERRAL_CREDITS)
            add_credits(user_id, REFERRAL_CREDITS)
            users.update_one({"user_id": referrer_id}, {"$inc": {"referral_count": 1}})
            referrals_db.insert_one({
                "referrer": referrer_id,
                "referee": user_id,
                "timestamp": datetime.now()
            })

def get_user_credits(user_id):
    user = users.find_one({"user_id": user_id})
    if not user:
        return 0
    if user.get("lifetime"):
        return float('inf')
    return user.get("credits", 0)

def deduct_credits(user_id, amount):
    users.update_one({"user_id": user_id}, {"$inc": {"credits": -amount}})

def add_credits(user_id, amount):
    users.update_one({"user_id": user_id}, {"$inc": {"credits": amount}})

def set_lifetime(user_id):
    users.update_one({"user_id": user_id}, {"$set": {"lifetime": True}})

def is_banned(user_id):
    user = users.find_one({"user_id": user_id})
    return user.get("banned", False) if user else False

def ban_user(user_id):
    users.update_one({"user_id": user_id}, {"$set": {"banned": True}})

def unban_user(user_id):
    users.update_one({"user_id": user_id}, {"$set": {"banned": False}})

def remove_user(user_id):
    users.delete_one({"user_id": user_id})

def increment_searches(user_id):
    users.update_one({"user_id": user_id}, {"$inc": {"total_searches": 1}})

def is_number_protected(number):
    return protected.find_one({
        "number": number,
        "paid_until": {"$gt": datetime.now()}
    }) is not None

def protect_number(number, user_id, duration_days):
    protected.update_one(
        {"number": number},
        {"$set": {
            "owner_id": user_id,
            "paid_until": datetime.now() + timedelta(days=duration_days),
            "protected_at": datetime.now()
        }},
        upsert=True
    )

def add_pending_payment(user_id, tx_id, amount, credits):
    payments.insert_one({
        "user_id": user_id,
        "transaction_id": tx_id,
        "amount": amount,
        "credits": credits,
        "status": "pending",
        "timestamp": datetime.now()
    })

def get_pending_payment(tx_id):
    return payments.find_one({"transaction_id": tx_id, "status": "pending"})

def verify_payment(tx_id):
    payments.update_one({"transaction_id": tx_id}, {"$set": {"status": "verified"}})

def add_pending_request(user_id, phone_number):
    return requests_db.insert_one({
        "user_id": user_id,
        "phone_number": phone_number,
        "status": "pending",
        "created_at": datetime.now()
    }).inserted_id

def get_pending_requests():
    return list(requests_db.find({"status": "pending"}))

def mark_request_processing(req_id):
    requests_db.update_one({"_id": req_id}, {"$set": {"status": "processing"}})

def mark_request_done(req_id, response):
    requests_db.update_one({"_id": req_id}, {"$set": {"status": "done", "response": response}})

def mark_request_failed(req_id, error):
    requests_db.update_one({"_id": req_id}, {"$set": {"status": "failed", "error": error}})

def get_completed_requests():
    return list(requests_db.find({"status": "done", "sent": {"$ne": True}}))

def mark_sent(req_id):
    requests_db.update_one({"_id": req_id}, {"$set": {"sent": True}})

def apply_daily_bonus(user_id):
    user = users.find_one({"user_id": user_id})
    if not user:
        return False
    last = user.get("last_bonus")
    if not last or last.date() < datetime.now().date():
        add_credits(user_id, BONUS_CREDITS)
        users.update_one({"user_id": user_id}, {"$set": {"last_bonus": datetime.now()}})
        return True
    return False

def generate_referral_link(user_id):
    if not BOT_USERNAME:
        return "Bot username not set"
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

def generate_upi_qr(upi_id, payee_name, amount, note):
    upi_url = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu=INR&tn={note}"
    qr = qrcode.make(upi_url)
    bio = BytesIO()
    qr.save(bio, 'PNG')
    bio.seek(0)
    return bio, upi_url

async def is_member(update, context):
    if not FORCE_CHANNELS:
        return True
    for channel in FORCE_CHANNELS:
        try:
            chat_member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
            if chat_member.status not in ["member", "administrator", "creator"]:
                return False
        except:
            return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    referrer_id = None
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].split("_")[1])
        except:
            pass
    init_user(user_id, referrer_id)
    if is_banned(user_id):
        await update.message.reply_text("You are banned from using this bot.")
        return
    if not await is_member(update, context):
        keyboard = []
        for ch in FORCE_CHANNELS:
            if ch.startswith('@'):
                url = f"https://t.me/{ch[1:]}"
            else:
                url = ch
            keyboard.append([InlineKeyboardButton(f"Join {ch}", url=url)])
        await update.message.reply_text(
            "Please join all required channels/groups before using this bot.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    if apply_daily_bonus(user_id):
        await update.message.reply_text(f"🎁 Daily login bonus: +{BONUS_CREDITS} credits!")
    keyboard = [
        [
            InlineKeyboardButton("🔍 Search Number", callback_data='search'),
            InlineKeyboardButton("🛡️ Protect Number", callback_data='protect')
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data='stats'),
            InlineKeyboardButton("🔗 Referral Program", callback_data='referral')
        ],
        [InlineKeyboardButton("💰 Buy Credits", callback_data='buy')]
    ]
    await update.message.reply_text(
        "🌟 *Premium Number Info Bot* 🌟\n\n"
        "Get detailed information about any phone number.\n\n"
        "🔹 10 credits per search\n"
        "🔹 2 free searches (20 credits) on start\n"
        "🔹 Protect your number from being searched\n\n"
        "Use the buttons below or commands: /help, /buy, /protect, /stats",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Help Menu*\n\n"
        "🔍 *Search Number*\n"
        "   Send a number with country code (e.g., +919876543210) after pressing 'Search Number'.\n\n"
        "🛡️ *Protect Number*\n"
        "   Use /protect or the button, then choose a plan, then send the number.\n\n"
        "💰 *Buy Credits*\n"
        "   Use /buy or the button, choose a plan, pay via UPI, send transaction ID.\n\n"
        "📊 *My Stats*\n"
        "   Use /stats to see your credits, total searches, and referrals.\n\n"
        "🔗 *Referral Program*\n"
        "   Share your referral link; both you and your friend get 2 credits.\n\n"
        "💡 *Commands*\n"
        "   /start – Main menu\n"
        "   /help – This message\n"
        "   /buy – Buy credits\n"
        "   /protect – Protect a number\n"
        "   /stats – Your statistics\n"
        "   /referral – Show your referral link (via button)"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("You are banned from using this bot.")
        return
    if not await is_member(update, context):
        keyboard = []
        for ch in FORCE_CHANNELS:
            if ch.startswith('@'):
                url = f"https://t.me/{ch[1:]}"
            else:
                url = ch
            keyboard.append([InlineKeyboardButton(f"Join {ch}", url=url)])
        await update.message.reply_text(
            "Please join all required channels/groups before using this bot.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    keyboard = [
        [InlineKeyboardButton("100 Credits - ₹50", callback_data='buy_100')],
        [InlineKeyboardButton("1000 Credits - ₹250", callback_data='buy_1000')],
        [InlineKeyboardButton("2000 Credits - ₹599", callback_data='buy_2000')],
        [InlineKeyboardButton("🌟 Lifetime Access - ₹899", callback_data='buy_lifetime')],
        [InlineKeyboardButton("🔙 Back", callback_data='start')]
    ]
    await update.message.reply_text(
        "💰 *Choose your plan:*\n\n"
        "🔹 10 credits = 1 search\n"
        "🔹 Lifetime = unlimited searches",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("You are banned from using this bot.")
        return
    if not await is_member(update, context):
        keyboard = []
        for ch in FORCE_CHANNELS:
            if ch.startswith('@'):
                url = f"https://t.me/{ch[1:]}"
            else:
                url = ch
            keyboard.append([InlineKeyboardButton(f"Join {ch}", url=url)])
        await update.message.reply_text(
            "Please join all required channels/groups before using this bot.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    keyboard = [
        [InlineKeyboardButton("30 Days - 50 Credits", callback_data='protect_30')],
        [InlineKeyboardButton("6 Months - 250 Credits", callback_data='protect_180')],
        [InlineKeyboardButton("Lifetime Protection - 1000 Credits", callback_data='protect_lifetime')],
        [InlineKeyboardButton("🔙 Back", callback_data='start')]
    ]
    await update.message.reply_text(
        "🛡️ *Protection Plans*\n\n"
        "Choose a plan to protect your number from being searched:\n\n"
        "🔹 30 Days – 50 Credits\n"
        "🔹 6 Months – 250 Credits\n"
        "🔹 Lifetime – 1000 Credits\n\n"
        "Send the number after selecting a plan.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("You are banned from using this bot.")
        return
    user = users.find_one({"user_id": user_id})
    credits = user.get('credits', 0)
    searches = user.get('total_searches', 0)
    lifetime = user.get('lifetime', False)
    referral_count = user.get('referral_count', 0)
    if lifetime:
        credits = "Unlimited"
    text = f"📊 *Your Stats*\n\nCredits: `{credits}`\nTotal Searches: `{searches}`\nReferrals: `{referral_count}`"
    await update.message.reply_text(text, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == 'search':
        await query.edit_message_text("📞 Send the phone number (with country code, e.g., +919876543210):")
        context.user_data['action'] = 'search'
    elif data == 'protect':
        await show_protect_menu(query, user_id)
    elif data == 'buy':
        await show_buy_menu(query, user_id)
    elif data == 'stats':
        await show_stats(query, user_id)
    elif data == 'referral':
        await show_referral(query, user_id)
    elif data.startswith('protect_'):
        plan = data.split('_')[1]
        await process_protect_plan(query, user_id, plan)
    elif data.startswith('buy_'):
        plan_key = data.split('_')[1]
        await process_buy_plan(query, user_id, plan_key)

async def show_protect_menu(query, user_id):
    keyboard = [
        [InlineKeyboardButton("30 Days - 50 Credits", callback_data='protect_30')],
        [InlineKeyboardButton("6 Months - 250 Credits", callback_data='protect_180')],
        [InlineKeyboardButton("Lifetime Protection - 1000 Credits", callback_data='protect_lifetime')],
        [InlineKeyboardButton("🔙 Back", callback_data='start')]
    ]
    await query.edit_message_text(
        "🛡️ *Protection Plans*\n\n"
        "Choose a plan to protect your number from being searched:\n\n"
        "🔹 30 Days – 50 Credits\n"
        "🔹 6 Months – 250 Credits\n"
        "🔹 Lifetime – 1000 Credits\n\n"
        "Send the number after selecting a plan.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def process_protect_plan(query, user_id, plan):
    plans = {
        '30': (50, 30),
        '180': (250, 180),
        'lifetime': (1000, None)
    }
    cost, days = plans[plan]
    credits = get_user_credits(user_id)
    user = users.find_one({"user_id": user_id})
    if not user.get("lifetime") and credits < cost:
        await query.edit_message_text(f"⚠️ You need {cost} credits for this plan. Please buy credits.")
        return
    context = query.message._context
    context.user_data['protect_plan'] = {'cost': cost, 'days': days}
    await query.edit_message_text("🔒 Send the number you want to protect (with country code):")
    context.user_data['action'] = 'protect'

async def show_buy_menu(query, user_id):
    keyboard = [
        [InlineKeyboardButton("100 Credits - ₹50", callback_data='buy_100')],
        [InlineKeyboardButton("1000 Credits - ₹250", callback_data='buy_1000')],
        [InlineKeyboardButton("2000 Credits - ₹599", callback_data='buy_2000')],
        [InlineKeyboardButton("🌟 Lifetime Access - ₹899", callback_data='buy_lifetime')],
        [InlineKeyboardButton("🔙 Back", callback_data='start')]
    ]
    await query.edit_message_text(
        "💰 *Choose your plan:*\n\n"
        "🔹 10 credits = 1 search\n"
        "🔹 Lifetime = unlimited searches",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def process_buy_plan(query, user_id, plan_key):
    plans = {
        '100': (50, 100),
        '1000': (250, 1000),
        '2000': (599, 2000),
        'lifetime': (899, 0)
    }
    amount, credits = plans[plan_key]
    payee_name = "NumberInfoBot"
    note = f"Credits: {credits}" if credits else "Lifetime Access"
    qr_img, upi_url = generate_upi_qr(UPI_ID, payee_name, amount, note)
    context = query.message._context
    context.user_data['pending_payment'] = {
        'amount': amount,
        'credits': credits,
        'plan': plan_key,
        'tx_id_expected': True
    }
    await query.edit_message_text(
        f"💳 *Payment Instructions*\n\n"
        f"1️⃣ Scan the QR code below or click the button to pay.\n"
        f"2️⃣ After payment, send the Transaction ID here.\n"
        f"3️⃣ Admin will verify and add credits.\n\n"
        f"💰 Amount: ₹{amount}\n"
        f"🎁 Credits: {credits if credits else 'Lifetime Access'}",
        parse_mode='Markdown'
    )
    await query.message.reply_photo(photo=qr_img, caption="Scan to pay")
    keyboard = [[InlineKeyboardButton("Pay via UPI", url=upi_url)]]
    await query.message.reply_text("Click the button to pay:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_stats(query, user_id):
    user = users.find_one({"user_id": user_id})
    credits = user.get('credits', 0)
    searches = user.get('total_searches', 0)
    lifetime = user.get('lifetime', False)
    referral_count = user.get('referral_count', 0)
    if lifetime:
        credits = "Unlimited"
    text = f"📊 *Your Stats*\n\nCredits: `{credits}`\nTotal Searches: `{searches}`\nReferrals: `{referral_count}`"
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data='start')
    ]]))

async def show_referral(query, user_id):
    user = users.find_one({"user_id": user_id})
    referral_count = user.get('referral_count', 0)
    link = generate_referral_link(user_id)
    text = (
        f"🔗 *Referral Program*\n\n"
        f"Share your unique link with friends. When they join using your link, **both** get {REFERRAL_CREDITS} free credits!\n\n"
        f"Your referral link:\n`{link}`\n\n"
        f"Total referrals: `{referral_count}`\n\n"
        f"Use this link to invite new users."
    )
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data='start')
    ]]))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    action = context.user_data.get('action')

    if context.user_data.get('pending_payment', {}).get('tx_id_expected'):
        pending = context.user_data['pending_payment']
        add_pending_payment(user_id, text, pending['amount'], pending['credits'])
        for admin_id in ADMIN_IDS:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"💸 New payment pending\nUser: {user_id}\nTransaction ID: {text}\nAmount: ₹{pending['amount']}\nCredits: {pending['credits'] if pending['credits'] else 'Lifetime'}"
            )
        await update.message.reply_text("✅ Payment recorded! Our admin will verify and add credits shortly. Thank you.")
        context.user_data.pop('pending_payment')
        return

    if action == 'search':
        if is_banned(user_id):
            await update.message.reply_text("You are banned from using this bot.")
            context.user_data['action'] = None
            return
        if not re.match(r'^\+\d{10,15}$', text):
            await update.message.reply_text("Please send a valid number with country code (e.g., +919876543210).")
            return
        cached = get_cached(text)
        if cached:
            await update.message.reply_text(cached, parse_mode='Markdown')
            context.user_data['action'] = None
            return
        credits = get_user_credits(user_id)
        if credits < 10 and not users.find_one({"user_id": user_id}).get("lifetime"):
            await update.message.reply_text("⚠️ You need 10 credits for a search. Use /buy to purchase credits.")
            context.user_data['action'] = None
            return
        if is_number_protected(text):
            await update.message.reply_text("🔒 This number is protected and cannot be searched.")
            context.user_data['action'] = None
            return
        user = users.find_one({"user_id": user_id})
        if not user.get("lifetime"):
            deduct_credits(user_id, 10)
        increment_searches(user_id)
        add_pending_request(user_id, text)
        await update.message.reply_text("🔍 Searching... Please wait a moment.")
        context.user_data['action'] = None

    elif action == 'protect':
        if is_banned(user_id):
            await update.message.reply_text("You are banned from using this bot.")
            context.user_data['action'] = None
            return
        if not re.match(r'^\+\d{10,15}$', text):
            await update.message.reply_text("Please send a valid number with country code (e.g., +919876543210).")
            return
        if is_number_protected(text):
            await update.message.reply_text("🔒 This number is already protected by someone else.")
            context.user_data['action'] = None
            return
        plan = context.user_data.get('protect_plan')
        if not plan:
            await update.message.reply_text("Please select a protection plan first using the button or /protect.")
            context.user_data['action'] = None
            return
        cost = plan['cost']
        days = plan['days']
        credits = get_user_credits(user_id)
        user = users.find_one({"user_id": user_id})
        if not user.get("lifetime") and credits < cost:
            await update.message.reply_text(f"⚠️ You need {cost} credits for this plan. Please buy credits.")
            context.user_data['action'] = None
            return
        if not user.get("lifetime"):
            deduct_credits(user_id, cost)
        if days is None:
            duration_days = 365 * 100
            protect_number(text, user_id, duration_days)
            await update.message.reply_text(f"✅ Number {text} is now protected for life!")
        else:
            protect_number(text, user_id, days)
            await update.message.reply_text(f"✅ Number {text} is now protected for {days} days.")
        context.user_data['action'] = None
        context.user_data.pop('protect_plan', None)

async def add_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addcredit <user_id> <amount>")
        return
    user_id = int(args[0])
    amount = int(args[1])
    add_credits(user_id, amount)
    await update.message.reply_text(f"Added {amount} credits to user {user_id}.")
    await context.bot.send_message(chat_id=user_id, text=f"🎉 {amount} credits added to your account!")

async def remove_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /removecredit <user_id> <amount>")
        return
    user_id = int(args[0])
    amount = int(args[1])
    deduct_credits(user_id, amount)
    await update.message.reply_text(f"Removed {amount} credits from user {user_id}.")

async def ban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    user_id = int(args[0])
    ban_user(user_id)
    await update.message.reply_text(f"User {user_id} banned.")

async def unban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    user_id = int(args[0])
    unban_user(user_id)
    await update.message.reply_text(f"User {user_id} unbanned.")

async def remove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    user_id = int(args[0])
    remove_user(user_id)
    await update.message.reply_text(f"User {user_id} removed from database.")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    cursor = users.find().limit(10)
    msg = "Recent users:\n"
    for user in cursor:
        msg += f"ID: {user['user_id']}, Credits: {user['credits']}, Banned: {user.get('banned', False)}\n"
    await update.message.reply_text(msg or "No users found.")

async def verify_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /verify <user_id> <transaction_id>")
        return
    user_id = int(args[0])
    tx_id = args[1]
    payment = get_pending_payment(tx_id)
    if not payment:
        await update.message.reply_text("Payment not found or already verified.")
        return
    if payment['user_id'] != user_id:
        await update.message.reply_text("User ID does not match payment record.")
        return
    verify_payment(tx_id)
    if payment['credits'] == 0:
        set_lifetime(user_id)
        await update.message.reply_text(f"Lifetime access granted to user {user_id}.")
        await context.bot.send_message(chat_id=user_id, text="🌟 Congratulations! You now have lifetime access to unlimited searches.")
    else:
        add_credits(user_id, payment['credits'])
        await update.message.reply_text(f"Added {payment['credits']} credits to user {user_id}.")
        await context.bot.send_message(chat_id=user_id, text=f"✅ Your payment of ₹{payment['amount']} is verified! {payment['credits']} credits added.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = ' '.join(context.args)
    all_users = [u['user_id'] for u in users.find({}, {'user_id': 1})]
    if not all_users:
        await update.message.reply_text("No users found.")
        return
    success = 0
    for uid in all_users:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {success} out of {len(all_users)} users.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    total_users = users.count_documents({})
    active_users = users.count_documents({"banned": False})
    total_searches = 0
    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$total_searches"}}}]
    result = list(users.aggregate(pipeline))
    if result:
        total_searches = result[0]['total']
    total_credits = 0
    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$credits"}}}]
    result = list(users.aggregate(pipeline))
    if result:
        total_credits = result[0]['total']
    lifetime_users = users.count_documents({"lifetime": True})
    text = (
        f"📊 *Bot Statistics*\n\n"
        f"Total Users: `{total_users}`\n"
        f"Active Users: `{active_users}`\n"
        f"Total Searches: `{total_searches}`\n"
        f"Total Credits in System: `{total_credits}`\n"
        f"Lifetime Users: `{lifetime_users}`"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

def format_output(raw_text):
    mapping = {
        'name': '👤 Name',
        'fname': '👨‍👩‍👦 Father',
        'address': '🏠 Address',
        'alt': '📞 Alt Number',
        'circle': '🌐 Circle',
        'email': '📧 Email',
        'id': '🆔 Aadhar'
    }
    lines = raw_text.split('\n')
    result = []
    for line in lines:
        if ':' in line:
            key, val = line.split(':', 1)
            key = key.strip().lower()
            val = val.strip()
            if key in mapping:
                result.append(f"{mapping[key]}: `{val}`")
            else:
                result.append(line.strip())
        else:
            if line.strip():
                result.append(line.strip())
    result.append("\n📢 *Credits:*")
    result.append("Channel 👩🏻‍💻: @ClanCosmo007")
    result.append("Credit 🛐 : @Shub_Rajput")
    return '\n'.join(result)

async def send_results(app: Application):
    while True:
        completed = get_completed_requests()
        for req in completed:
            user_id = req['user_id']
            response = req['response']
            formatted = format_output(response)
            set_cache(req['phone_number'], formatted)
            await app.bot.send_message(chat_id=user_id, text=formatted, parse_mode='Markdown')
            mark_sent(req['_id'])
        await asyncio.sleep(2)

async def worker():
    client = TelegramClient(StringSession(USER_SESSION_STRING), API_ID, API_HASH)
    await client.start()
    target = await client.get_entity(TARGET_BOT)
    reply_queue = asyncio.Queue()

    @client.on(events.NewMessage(from_users=target))
    async def handler(event):
        await reply_queue.put(event.message)

    banned_phrases = [
        "channel\": \"@Hacker_krishna",
        "by the Nexa ox1",
        "CREDIT :- @Hacker_krishna",
        "Dev :- @Hacker_krishna",
        "Ig :- @Nomercyhac4er",
        "Credit :- @Hacker_krishna",
        "Dev :- @Hacker_krishna",
        "êœ°á´œÊŸÊŸ á´…á´€á´›á´€ êœ°á´‡á´›á´„Êœá´‡á´… Ê™Ê É´á´‡xá´€ á´x1 :-"
    ]

    def clean_text(text):
        for phrase in banned_phrases:
            text = text.replace(phrase, "")
        lines = text.split('\n')
        cleaned = [line.strip() for line in lines if line.strip()]
        return '\n'.join(cleaned)

    while True:
        pending = get_pending_requests()
        if not pending:
            await asyncio.sleep(5)
            continue
        for req in pending:
            req_id = req['_id']
            number = req['phone_number']
            mark_request_processing(req_id)
            await client.send_message(target, number)
            try:
                reply = await asyncio.wait_for(reply_queue.get(), timeout=30)
                if reply.document and reply.document.mime_type == 'text/plain':
                    file_bytes = await client.download_media(reply, file=bytes)
                    text = file_bytes.decode('utf-8')
                else:
                    text = reply.text
                cleaned = clean_text(text)
                mark_request_done(req_id, cleaned)
            except asyncio.TimeoutError:
                mark_request_failed(req_id, "Timeout: No response from target bot")
            except Exception as e:
                mark_request_failed(req_id, str(e))
        await asyncio.sleep(2)

async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("protect", protect_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addcredit", add_credit))
    app.add_handler(CommandHandler("removecredit", remove_credit))
    app.add_handler(CommandHandler("ban", ban_user_cmd))
    app.add_handler(CommandHandler("unban", unban_user_cmd))
    app.add_handler(CommandHandler("removeuser", remove_user_cmd))
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(CommandHandler("verify", verify_payment))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    asyncio.create_task(send_results(app))
    asyncio.create_task(worker())

    flask_app = Flask('')
    @flask_app.route('/')
    def health():
        return "Bot is running"
    def run_flask():
        flask_app.run(host='0.0.0.0', port=8000)
    threading.Thread(target=run_flask, daemon=True).start()

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
