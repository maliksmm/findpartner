import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update,
)
from telegram.constants import ChatType
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Find Partner VIP - single-file Telegram bot
# Secrets belong in Render Environment Variables, never in this file.
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("find_partner_vip")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7044754988"))
DB_PATH = os.getenv("DB_PATH", "data/find_partner_vip.sqlite3")
OFFICIAL_GROUP = os.getenv("OFFICIAL_GROUP", "https://t.me/chatgcv1").strip()
OFFICIAL_CHANNEL = os.getenv("OFFICIAL_CHANNEL", "https://t.me/arx_smediaexpert").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "").strip()
MIN_AGE = 18
BROADCAST_PAUSE = 0.05
MAX_REPORT_REASON = 500

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable. Add it in Render Environment settings.")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)


def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db_connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                age_confirmed INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                banned INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT,
                vip_lifetime INTEGER NOT NULL DEFAULT 0,
                matching INTEGER NOT NULL DEFAULT 0,
                partner_id INTEGER,
                gender TEXT,
                looking_for TEXT,
                bio TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocks (
                user_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, blocked_id)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reported_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                handled_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created_at TEXT NOT NULL, qualified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS referral_rewards (user_id INTEGER PRIMARY KEY, granted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS required_chats (
                username TEXT PRIMARY KEY,
                chat_type TEXT NOT NULL CHECK(chat_type IN ('channel','group')),
                title TEXT NOT NULL DEFAULT '',
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS battle_queue (user_id INTEGER PRIMARY KEY, category TEXT NOT NULL, queued_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS battles (id INTEGER PRIMARY KEY AUTOINCREMENT, player1 INTEGER NOT NULL, player2 INTEGER NOT NULL, category TEXT NOT NULL, question_index INTEGER NOT NULL DEFAULT 0, score1 INTEGER NOT NULL DEFAULT 0, score2 INTEGER NOT NULL DEFAULT 0, answer TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS saved_partners (user_id INTEGER NOT NULL, partner_id INTEGER NOT NULL, saved_at TEXT NOT NULL, PRIMARY KEY(user_id, partner_id));
            CREATE TABLE IF NOT EXISTS friends (user_id INTEGER NOT NULL, friend_id INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id, friend_id));
            CREATE TABLE IF NOT EXISTS friend_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER NOT NULL, to_id INTEGER NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', UNIQUE(from_id,to_id));
            """
        )
        # Safe migration for databases created by older bot versions.
        user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        if "points" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
        # Additive profile migration; preserves existing users and records.
        if "country" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN country TEXT")
        if "age" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN age INTEGER")
        if "profile_complete" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN profile_complete INTEGER NOT NULL DEFAULT 0")
        if "age_blocked" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN age_blocked INTEGER NOT NULL DEFAULT 0")
        if "display_name" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        con.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_by, added_at) VALUES(?,?,?)",
            (OWNER_ID, OWNER_ID, now_iso()),
        )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def user_row(user_id):
    with db_connect() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def setting(key, default=""):
    with db_connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db_connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def is_admin_id(user_id):
    if user_id == OWNER_ID:
        return True
    with db_connect() as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def vip_active(row):
    if not row:
        return False
    if row["vip_lifetime"]:
        return True
    if not row["vip_until"]:
        return False
    try:
        return datetime.fromisoformat(row["vip_until"]) > datetime.now(timezone.utc)
    except ValueError:
        return False


def upsert_user(tg_user):
    stamp = now_iso()
    with db_connect() as con:
        con.execute(
            """
            INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_seen=excluded.last_seen
            """,
            (tg_user.id, tg_user.username or "", tg_user.first_name or "", stamp, stamp),
        )


def blocked_by_either(a, b):
    with db_connect() as con:
        return (
            con.execute("SELECT 1 FROM blocks WHERE user_id=? AND blocked_id=?", (a, b)).fetchone()
            or con.execute("SELECT 1 FROM blocks WHERE user_id=? AND blocked_id=?", (b, a)).fetchone()
        ) is not None


def set_matching(user_id, enabled, partner_id=None):
    with db_connect() as con:
        con.execute(
            "UPDATE users SET matching=?, partner_id=? WHERE user_id=?",
            (1 if enabled else 0, partner_id, user_id),
        )


async def safe_send(bot, chat_id, text, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError:
        return None


def persistent_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❤️ Find Partner"), KeyboardButton("🎮 Games")],
         [KeyboardButton("💎 VIP Benefits"), KeyboardButton("💳 VIP Plans")],
         [KeyboardButton("🎁 Free VIP — 39 Days"), KeyboardButton("🤝 Friends")],
         [KeyboardButton("👤 My Profile"), KeyboardButton("🆘 Safety / Report")],
         [KeyboardButton("🛑 Stop Chat")]],
        resize_keyboard=True, is_persistent=True, input_field_placeholder="Choose a bot feature…"
    )


def main_keyboard(user_id=None):
    rows = [
        [InlineKeyboardButton("❤️ Find / Next Partner", callback_data="find")],
        [InlineKeyboardButton("🎮 GameVerse — Play Games", callback_data="games")],
        [InlineKeyboardButton("➕ Add Bot to Your Group", url="https://t.me/FindPartnerVipBot?startgroup=true")],
        [InlineKeyboardButton("📰 Read Posts", callback_data="posts"), InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("⭐ Save Partner", callback_data="save_partner"), InlineKeyboardButton("🤝 Add Friend", callback_data="friend_request")],
        [InlineKeyboardButton("👥 My Friends", callback_data="friends"), InlineKeyboardButton("📌 Saved Partners", callback_data="saved")],
        [InlineKeyboardButton("👤 My Profile", callback_data="profile"), InlineKeyboardButton("💎 VIP Club", callback_data="vip")],
        [InlineKeyboardButton("💎 What Do I Get with VIP?", callback_data="vip_features")],
        [InlineKeyboardButton("🎁 Free VIP — 39 Days", callback_data="referral"), InlineKeyboardButton("🆘 Safety", callback_data="help")],
    ]
    if user_id and is_admin_id(user_id):
        rows.append([InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def required_chat_rows():
    with db_connect() as con:
        return con.execute("SELECT username, chat_type, title FROM required_chats ORDER BY added_at, username").fetchall()


def total_users_count():
    with db_connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def join_keyboard():
    rows = []
    # Keep the original two requirements (slots 1 and 2), then append admin-added chats.
    if OFFICIAL_CHANNEL:
        rows.append([InlineKeyboardButton("📢 Join Channel 1", url=OFFICIAL_CHANNEL)])
    if OFFICIAL_GROUP:
        rows.append([InlineKeyboardButton("👥 Join Group 2", url=OFFICIAL_GROUP)])
    for index, chat in enumerate(required_chat_rows(), start=3):
        username = chat["username"].lstrip("@")
        link = f"https://t.me/{username}"
        label = "📢 Channel" if chat["chat_type"] == "channel" else "👥 Group"
        title = chat["title"] or f"{label} {index}"
        rows.append([InlineKeyboardButton(f"{label} {index}: {title}"[:60], url=link)])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)


def age_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("I confirm I am 18 or older", callback_data="age_yes")],
         [InlineKeyboardButton("I am under 18", callback_data="age_no")]]
    )


async def check_membership(bot, user_id):
    checks = []
    # Legacy required chats (slots 1 and 2) use numeric IDs for reliable membership checks.
    if GROUP_CHAT_ID:
        checks.append((GROUP_CHAT_ID, setting("official_group", OFFICIAL_GROUP)))
    else:
        return False, OFFICIAL_GROUP
    if CHANNEL_CHAT_ID:
        checks.append((CHANNEL_CHAT_ID, setting("official_channel", OFFICIAL_CHANNEL)))
    else:
        return False, OFFICIAL_CHANNEL
    # Additional public @usernames added by the owner/admin (slot 3 onwards).
    for chat in required_chat_rows():
        uname = chat["username"].lstrip("@")
        checks.append((f"@{uname}", f"https://t.me/{uname}"))
    for chat_id, link in checks:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked", "banned"):
                return False, link
        except TelegramError:
            # Fail closed: the bot must be able to inspect membership.
            return False, link
    return True, ""


async def access_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == OWNER_ID or is_admin_id(user.id):
        return True
    row = user_row(user.id)
    if not row or not row["age_confirmed"]:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(
                "🔞 This bot is for adults aged 18+ only. Please confirm your age.",
                reply_markup=age_keyboard(),
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "🔞 This bot is for adults aged 18+ only. Please confirm your age.",
                reply_markup=age_keyboard(),
            )
        return False
    if row["banned"]:
        if update.effective_message:
            await update.effective_message.reply_text("Your account is restricted. Contact support if you think this is a mistake.")
        elif update.callback_query:
            await update.callback_query.answer("Your account is restricted.", show_alert=True)
        return False
    ok, link = await check_membership(context.bot, user.id)
    if not ok:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Join required chat", url=link)]])
        msg = "Please join the required official group/channel first, then press /start again."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(msg, reply_markup=markup)
        elif update.effective_message:
            await update.effective_message.reply_text(msg, reply_markup=markup)
        return False
    return True


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin_id(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        # Group start is for public games only; personal matching/profile setup stays private.
        await msg.reply_text(
            "🎮✨ GAMEVERSE GROUP ARCADE ✨🎮\n\n"
            "Play together right here! Use /games or /play to open the group game menu.\n"
            "🎲 Dice • 🎯 Darts • ⚽ Football • 🏀 Basketball • 🎳 Bowling • 🎰 Slot Machine\n"
            "🧠 Try quiz battles, riddles and word challenges with your group.\n\n"
            "❤️ Partner matching and personal profiles are handled in private chat only.\n"
            "👇 Add me to your group or open the game menu:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 Open Group Games", callback_data="groupgames_menu")],
                [InlineKeyboardButton("➕ Add to Another Group", url="https://t.me/FindPartnerVipBot?startgroup=true")],
                [InlineKeyboardButton("💬 Open Private Bot", url="https://t.me/FindPartnerVipBot")],
            ]),
        )
        return
    upsert_user(user)
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer = int(context.args[0][4:])
            if referrer != user.id and user_row(referrer):
                with db_connect() as con:
                    con.execute("INSERT OR IGNORE INTO referrals(referred_id,referrer_id,created_at) VALUES(?,?,?)", (user.id, referrer, now_iso()))
        except (ValueError, TypeError):
            pass
    row = user_row(user.id)
    if row and row["age_blocked"] and not is_admin_id(user.id):
        await msg.reply_text("🔞 Access is unavailable because the age entered for this account was under 18.")
        return
    if row and row["banned"] and not is_admin_id(user.id):
        await msg.reply_text("Your account is restricted.")
        return
    if not is_admin_id(user.id):
        ok, _ = await check_membership(context.bot, user.id)
        if not ok:
            await msg.reply_text("👋 Welcome to Find Partner VIP!\n\nFirst join both our official channel and group, then tap **I've Joined — Verify**.\n\nIf verification keeps failing, the owner must configure the numeric GROUP_CHAT_ID and CHANNEL_CHAT_ID in Render and ensure the bot can check membership.", reply_markup=join_keyboard(), parse_mode="Markdown")
            return
        row = user_row(user.id)
        if not row or not row["age_confirmed"]:
            await msg.reply_text("🔞 This service is strictly for adults aged 18+ only. Confirm your age to continue. Never share passwords, verification codes, financial details, your home address, or private images with strangers.", reply_markup=age_keyboard())
            return
    row = user_row(user.id)
    if not is_admin_id(user.id) and row and row["age_confirmed"] and not row["profile_complete"]:
        await begin_profile(update, context)
        return
    total = total_users_count()
    await msg.reply_text(f"💜✨ FIND PARTNER VIP ✨💜\n👥 Community members: {total}\n━━━━━━━━━━━━━━━━━━\n💬 Meet real people. Start conversations.\n❤️ Find an available partner instantly when someone is searching.\n⚔️ GameVerse: live Mathematics, Science & History battles.\n🏆 Earn XP, climb the leaderboard and build your profile.\n🤝 Add friends only with mutual consent.\n💎 VIP Club • 🎁 Referral rewards\n━━━━━━━━━━━━━━━━━━\n🛡️ Use /report or /block whenever needed.\n\nPowered by @z3rv_x", reply_markup=main_keyboard(user.id))
    await safe_send(context.bot, user.id, "📌 Your menu stays available below.", reply_markup=persistent_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    await update.effective_message.reply_text(
        "Commands:\n"
        "/start — open menu\n/find — find a partner\n/stop — end current match\n/profile — view profile\n"
        "/setbio text — set your short bio\n/report reason — report your current partner\n"
        "/block — block current partner\n/vip — VIP plans\n/games or /play — games\n/leaderboard — top quiz players\n/mypoints — your XP\n/friends — mutual friends\n/saved — saved partners\n\n"
        "Safety: never send passwords, verification codes, financial details, or your address. "
        "You can end a chat with /stop at any time."
    )


def country_keyboard():
    countries = ["India", "Pakistan", "Bangladesh", "Nepal", "Sri Lanka", "UAE", "Saudi Arabia", "United Kingdom", "United States", "Canada", "Other"]
    rows = []
    for i in range(0, len(countries), 2):
        rows.append([InlineKeyboardButton(c, callback_data="country_" + c.lower().replace(" ", "_")) for c in countries[i:i+2]])
    return InlineKeyboardMarkup(rows)


def gender_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👩 Woman", callback_data="profile_gender_woman"),
         InlineKeyboardButton("👨 Man", callback_data="profile_gender_man")]
    ])


async def profile_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Replace the previous onboarding prompt after the user completes that step."""
    previous_id = context.user_data.pop("profile_prompt_message_id", None)
    chat_id = update.effective_chat.id if update.effective_chat else None
    if previous_id and chat_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=previous_id)
        except TelegramError:
            pass
    message = await update.effective_message.reply_text(text, reply_markup=reply_markup)
    context.user_data["profile_prompt_message_id"] = message.message_id
    return message


async def begin_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["profile_step"] = "country"
    await profile_prompt(
        update, context,
        "👤 Let's set up your profile. This matching service is strictly for adults aged 18+ only.\n\n🌍 Select your country:",
        reply_markup=country_keyboard(),
    )


def profile_details(row, viewer_row):
    # Gender is intentionally hidden from non-VIP viewers.
    gender_text = ""
    if vip_active(viewer_row):
        gender_text = f"\\nGender: {row['gender'] or 'Not set'}"
    return f"🌍 Country: {row['country'] or 'Not set'}\\n🎂 Age: {row['age'] or 'Not set'}{gender_text}"


def compatible_for_match(uid, other_id):
    current = user_row(uid)
    other = user_row(other_id)
    if not current or not other:
        return False
    if not current["profile_complete"] or not other["profile_complete"]:
        return False
    # All ages are eligible; the only age rule is that both profiles must be 18+.
    if not current["age"] or current["age"] < MIN_AGE or not other["age"] or other["age"] < MIN_AGE:
        return False
    current_pref = (current["looking_for"] or "any") if vip_active(current) else "any"
    other_pref = (other["looking_for"] or "any") if vip_active(other) else "any"
    current_gender = (current["gender"] or "").lower()
    other_gender = (other["gender"] or "").lower()
    # Explicit branches keep gender matching easy to audit.
    if current_pref == "women" and other_gender != "woman":
        return False
    if current_pref == "men" and other_gender != "man":
        return False
    if other_pref == "women" and current_gender != "woman":
        return False
    if other_pref == "men" and current_gender != "man":
        return False
    return True


async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    uid = update.effective_user.id
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    current = user_row(uid)
    if not current or not current["profile_complete"]:
        await begin_profile(update, context)
        return
    if current["partner_id"]:
        await update.effective_message.reply_text("You're already connected. Use /stop before finding another partner.")
        return
    with db_connect() as con:
        candidates = con.execute(
            "SELECT user_id FROM users WHERE user_id<>? AND matching=1 AND partner_id IS NULL "
            "AND age_confirmed=1 AND banned=0 AND profile_complete=1 ORDER BY RANDOM() LIMIT 100",
            (uid,),
        ).fetchall()
    partner_id = None
    for candidate in candidates:
        other = int(candidate["user_id"])
        if not blocked_by_either(uid, other) and compatible_for_match(uid, other):
            partner_id = other
            break
    if partner_id is None:
        set_matching(uid, True, None)
        await update.effective_message.reply_text(
            "🔎 Searching for a compatible partner…\nAge preference: any adult age (18+).\nUse /stop to cancel."
        )
        return
    with db_connect() as con:
        con.execute("UPDATE users SET matching=0, partner_id=? WHERE user_id=? AND partner_id IS NULL", (partner_id, uid))
        con.execute("UPDATE users SET matching=0, partner_id=? WHERE user_id=? AND partner_id IS NULL", (uid, partner_id))
        a = con.execute("SELECT partner_id FROM users WHERE user_id=?", (uid,)).fetchone()
        b = con.execute("SELECT partner_id FROM users WHERE user_id=?", (partner_id,)).fetchone()
    if not a or not b or a["partner_id"] != partner_id or b["partner_id"] != uid:
        await update.effective_message.reply_text("Someone else got matched first. Tap Find Partner again.")
        return
    current = user_row(uid)
    other = user_row(partner_id)
    await update.effective_message.reply_text(
        "💜✨ PARTNER FOUND! ✨💜\nYou are now connected. Be respectful; /stop ends the chat.\n\n"
        + profile_details(other, current)
    )
    await safe_send(
        context.bot, partner_id,
        "💜✨ PARTNER FOUND! ✨💜\nYou are now connected. Be respectful; /stop ends the chat.\n\n"
        + profile_details(current, other),
    )


async def stop_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id
    row = user_row(uid)
    partner = row["partner_id"] if row else None
    set_matching(uid, False, None)
    if partner:
        other = user_row(partner)
        if other and other["partner_id"] == uid:
            set_matching(partner, False, None)
            await safe_send(context.bot, partner, "Your partner ended the chat. Use /find to meet someone new.")
        await update.effective_message.reply_text("Chat ended. Your partner has been notified.")
    else:
        await update.effective_message.reply_text("Search cancelled / no active chat.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    row = user_row(update.effective_user.id)
    if not row:
        return await update.effective_message.reply_text("Send /start first.")
    status = "Lifetime VIP" if row["vip_lifetime"] else ("VIP active" if vip_active(row) else "Standard")
    await update.effective_message.reply_text(
        f"👤 Your profile\nDisplay name: {row['display_name'] or row['first_name'] or 'Not set'}\n"
        f"Country: {row['country'] or 'Not set'}\nAge: {row['age'] or 'Not set'}\n"
        f"Gender: {row['gender'] or 'Not set'}\nVIP: {status}\n"
        f"VIP match preference: {(row['looking_for'] or 'any').title() if vip_active(row) else 'Any (standard)'}\n"
        f"⭐ XP points: {row['points']}\nBio: {row['bio'] or 'No bio yet'}\n\n"
        "Set your bio with /setbio your text",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")]])
    )


async def set_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    bio = " ".join(context.args).strip()[:300]
    if not bio:
        return await update.effective_message.reply_text("Usage: /setbio A short description about me")
    with db_connect() as con:
        con.execute("UPDATE users SET bio=? WHERE user_id=?", (bio, update.effective_user.id))
    await update.effective_message.reply_text("Bio updated.")


async def report_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    row = user_row(update.effective_user.id)
    if not row or not row["partner_id"]:
        return await update.effective_message.reply_text("You don't have an active partner to report.")
    reason = " ".join(context.args).strip()[:MAX_REPORT_REASON] or "No reason supplied"
    with db_connect() as con:
        cur = con.execute(
            "INSERT INTO reports(reporter_id,reported_id,reason,created_at) VALUES(?,?,?,?)",
            (update.effective_user.id, row["partner_id"], reason, now_iso()),
        )
        report_id = cur.lastrowid
    await update.effective_message.reply_text(f"Report submitted (#{report_id}). Thank you. You can also use /block and /stop.")
    await safe_send(context.bot, OWNER_ID, f"🚩 Report #{report_id}\nFrom: {update.effective_user.id}\nAgainst: {row['partner_id']}\nReason: {reason}")


async def block_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    uid = update.effective_user.id
    row = user_row(uid)
    if not row or not row["partner_id"]:
        return await update.effective_message.reply_text("No active partner to block.")
    pid = row["partner_id"]
    with db_connect() as con:
        con.execute("INSERT OR IGNORE INTO blocks(user_id,blocked_id,created_at) VALUES(?,?,?)", (uid, pid, now_iso()))
    await stop_match(update, context)
    await update.effective_message.reply_text("Partner blocked; they won't be paired with you again.")



async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    with db_connect() as con:
        rows = con.execute("SELECT user_id, first_name, points FROM users WHERE age_confirmed=1 AND banned=0 ORDER BY points DESC, joined_at ASC LIMIT 10").fetchall()
    if not rows or all(int(r["points"] or 0) == 0 for r in rows):
        return await update.effective_message.reply_text("🏆 Leaderboard is ready! Earn XP by winning real-player quiz battle questions.")
    lines = ["🏆 GAMEVERSE LEADERBOARD", "━━━━━━━━━━━━━━━━━━"]
    for i, r in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {r['first_name'] or 'Player'} — {r['points']} XP")
    await update.effective_message.reply_text("\n".join(lines))


async def mypoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    row = user_row(update.effective_user.id)
    await update.effective_message.reply_text(f"✨ YOUR GAMEVERSE STATS\n\n⭐ XP: {row['points']}\n🎮 Earn 5 XP for each correct answer in a real-player battle.\n🏆 Use /leaderboard to see the top players.")


async def saved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    uid = update.effective_user.id
    with db_connect() as con:
        rows = con.execute("SELECT s.partner_id,u.first_name,u.username,u.display_name FROM saved_partners s LEFT JOIN users u ON u.user_id=s.partner_id WHERE s.user_id=? ORDER BY s.saved_at DESC LIMIT 30", (uid,)).fetchall()
    if not rows:
        return await update.effective_message.reply_text("📌 No saved partners yet. Connect with someone and tap ⭐ Save Partner.")
    lines = ["📌 SAVED PARTNERS"]
    for r in rows:
        lines.append(f"• {r['display_name'] or r['first_name'] or 'Partner'}")
    await update.effective_message.reply_text("\n".join(lines)[:3900])


async def friends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    uid = update.effective_user.id
    with db_connect() as con:
        rows = con.execute("SELECT f.friend_id,u.first_name,u.username,u.display_name FROM friends f LEFT JOIN users u ON u.user_id=f.friend_id WHERE f.user_id=? ORDER BY f.created_at DESC LIMIT 30", (uid,)).fetchall()
        pending = con.execute("SELECT id,from_id FROM friend_requests WHERE to_id=? AND status='pending' ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    lines = ["🤝 YOUR FRIENDS"]
    lines += [f"• {r['display_name'] or r['first_name'] or 'Friend'}" for r in rows] or ["No friends added yet."]
    if pending:
        lines.append("\n📩 PENDING REQUESTS")
        for r in pending:
            lines.append(f"Friend request #{r['id']}")
    buttons = [[InlineKeyboardButton(f"✅ Accept #{r['id']}", callback_data=f"friend_accept_{r['id']}"), InlineKeyboardButton("❌ Decline", callback_data=f"friend_decline_{r['id']}")] for r in pending]
    await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


def vip_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("30 days — $2.99", callback_data="vipreq_30d")],
            [InlineKeyboardButton("90 days — $6.99", callback_data="vipreq_90d")],
            [InlineKeyboardButton("Lifetime — $14.99", callback_data="vipreq_life")],
            [InlineKeyboardButton("🎯 VIP Gender Matching", callback_data="vip_gender_menu")],
            [InlineKeyboardButton("✨ See All VIP Benefits", callback_data="vip_features")],
            [InlineKeyboardButton("👑 Contact VIP Owner", url="https://t.me/z3rv_x")],
        ]
    )


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    await update.effective_message.reply_text(
        "💎 VIP Plans\n\nVIP is manually reviewed by the owner. No automatic payment gateway is connected.\n"
        "Choose a plan to send a request. Do not send card details or passwords.",
        reply_markup=vip_markup(),
    )


async def send_group_games_menu(message, context):
    """Public, age-neutral games menu for Telegram groups."""
    if not message:
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Dice", callback_data="groupgame_dice"), InlineKeyboardButton("🎯 Darts", callback_data="groupgame_darts")],
        [InlineKeyboardButton("⚽ Football", callback_data="groupgame_football"), InlineKeyboardButton("🏀 Basketball", callback_data="groupgame_basketball")],
        [InlineKeyboardButton("🎳 Bowling", callback_data="groupgame_bowling"), InlineKeyboardButton("🎰 Slot Machine", callback_data="groupgame_slot")],
        [InlineKeyboardButton("🧠 Group Trivia", callback_data="groupquiz_new"), InlineKeyboardButton("🧩 Riddle Round", callback_data="groupriddle_new")],
        [InlineKeyboardButton("🪙 Coin Flip", callback_data="groupgame_coin"), InlineKeyboardButton("🔢 Number Guess", callback_data="groupguess_new")],
        [InlineKeyboardButton("🧠 Quiz Battle (private queue)", url="https://t.me/FindPartnerVipBot?start=games")],
        [InlineKeyboardButton("➕ Add Bot to Your Group", url="https://t.me/FindPartnerVipBot?startgroup=true")],
    ])
    await message.reply_text(
        "🎮💎 GAMEVERSE — GROUP ARCADE 💎🎮\n\n"
        "Tap any Telegram game and it will appear in this group for everyone to play.\n\n"
        "🧠 Trivia, riddles, number guessing and coin flips are available right here. Correct answers earn points.\n"
        "🏆 Tip: pin this menu so members can launch supported games anytime.\n"
        "🔒 Personal partner matching and profiles are available only in private chat.",
        reply_markup=markup,
    )


async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await send_group_games_menu(update.effective_message, context)
        return
    if not await access_check(update, context):
        return
    bot_username = "FindPartnerVipBot"
    try:
        me = await context.bot.get_me()
        bot_username = me.username or bot_username
    except TelegramError:
        pass
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ 1v1 Mathematics", callback_data="battle_math")],
        [InlineKeyboardButton("🧪 1v1 Science", callback_data="battle_science")],
        [InlineKeyboardButton("📜 1v1 History", callback_data="battle_history")],
        [InlineKeyboardButton("✊ Rock Paper Scissors (vs bot)", callback_data="rps_menu")],
        [InlineKeyboardButton("🎲 Dice — play in this chat", callback_data="groupgame_dice")],
        [InlineKeyboardButton("🎯 Darts — play in this chat", callback_data="groupgame_darts")],
        [InlineKeyboardButton("⚽ Football — play in this chat", callback_data="groupgame_football")],
        [InlineKeyboardButton("🏀 Basketball — play in this chat", callback_data="groupgame_basketball")],
        [InlineKeyboardButton("🎳 Bowling — play in this chat", callback_data="groupgame_bowling")],
        [InlineKeyboardButton("🎰 Slot Machine — play in this chat", callback_data="groupgame_slot")],
        [InlineKeyboardButton("➕ Add bot to your group", url=f"https://t.me/{bot_username}?startgroup=true")],
    ])
    await update.effective_message.reply_text(
        "🎮 GAMEVERSE ARCADE\n\n"
        "⚔️ 1v1 quiz battles: Mathematics, Science and History match you with another available player.\n"
        "🎲 Group-friendly Telegram mini-games: Dice, Darts, Football, Basketball and Bowling. Add the bot to a group, open /games there, and tap a game to post a playable Telegram animation for everyone.\n"
        "🏆 Quiz battles earn XP and appear on the leaderboard. Use /cancelbattle to leave a 1v1 queue.\n\n"
        "Safety: this partner-matching service stays strictly 18+. Reports help moderation but cannot replace an age gate. Use /report, /block or /stop for safety.",
        reply_markup=markup,
    )

BATTLE_QUESTIONS = {
    "math": [
        ("What is 12 × 8?", "96"), ("What is 144 ÷ 12?", "12"),
        ("What is 15²?", "225"), ("Solve: 7x = 56. What is x?", "8"),
        ("What is the square root of 169?", "13"), ("What is 25% of 200?", "50"),
        ("What is 3/4 as a decimal?", "0.75"), ("What is 19 + 27?", "46"),
        ("What is 11 × 11?", "121"), ("What is 1000 − 375?", "625"),
    ],
    "science": [
        ("Which gas makes up most of Earth's atmosphere?", "nitrogen"),
        ("What is the chemical formula for water?", "h2o"),
        ("Which planet is known as the Red Planet?", "mars"),
        ("What force pulls objects toward Earth?", "gravity"),
        ("What is the center of an atom called?", "nucleus"),
        ("Which organ pumps blood around the body?", "heart"),
        ("What is the process plants use to make food?", "photosynthesis"),
        ("What is the closest star to Earth?", "sun"),
        ("At sea level, water boils at how many degrees Celsius?", "100"),
        ("What type of blood vessel carries blood away from the heart?", "artery"),
    ],
    "history": [
        ("In which year did World War II end?", "1945"),
        ("Who was the first person to walk on the Moon?", "neil armstrong"),
        ("Which ancient civilization built the pyramids at Giza?", "egyptians"),
        ("In which year did the Berlin Wall fall?", "1989"),
        ("Who was the first President of the United States?", "george washington"),
        ("Which ship sank on its maiden voyage in 1912?", "titanic"),
        ("Which city was buried by the eruption of Mount Vesuvius?", "pompeii"),
        ("In which country did the Renaissance begin?", "italy"),
        ("Who was known as the Maid of Orléans?", "joan of arc"),
        ("Which ancient people developed democracy in Athens?", "greeks"),
    ],
}

def normalize_answer(value):
    return " ".join((value or "").lower().strip().replace(".", "").replace(",", "").split())

async def ask_battle_question(bot, battle_id):
    with db_connect() as con:
        b = con.execute("SELECT * FROM battles WHERE id=? AND status='active'", (battle_id,)).fetchone()
    if not b:
        return
    questions = BATTLE_QUESTIONS[b["category"]]
    idx = b["question_index"]
    if idx >= 10:
        winner = "It's a draw!" if b["score1"] == b["score2"] else (f"Player {b['player1']} wins!" if b["score1"] > b["score2"] else f"Player {b['player2']} wins!")
        with db_connect() as con:
            con.execute("UPDATE battles SET status='finished' WHERE id=?", (battle_id,))
        result = f"🏁 Battle finished!\nFinal score: {b['score1']} - {b['score2']}\n{winner}\nUse /battle to play again."
        await safe_send(bot, b["player1"], result)
        await safe_send(bot, b["player2"], result)
        return
    question, answer = questions[idx]
    with db_connect() as con:
        con.execute("UPDATE battles SET answer=? WHERE id=?", (normalize_answer(answer), battle_id))
    text = f"⚔️ {b['category'].title()} Battle — Question {idx+1}/10\n\n{question}\n\nType your answer in this chat. First correct answer gets 1 point!"
    await safe_send(bot, b["player1"], text)
    await safe_send(bot, b["player2"], text)
    async def timeout_next():
        await asyncio.sleep(25)
        with db_connect() as con:
            cur = con.execute("UPDATE battles SET question_index=question_index+1 WHERE id=? AND status='active' AND question_index=?", (battle_id, idx))
            advanced = cur.rowcount
        if advanced:
            await ask_battle_question(bot, battle_id)
    asyncio.create_task(timeout_next())

async def queue_battle(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str):
    uid = update.effective_user.id
    if not await access_check(update, context):
        return
    with db_connect() as con:
        active = con.execute("SELECT id FROM battles WHERE status='active' AND (player1=? OR player2=?)", (uid, uid)).fetchone()
        if active:
            return await update.effective_message.reply_text("You're already in a battle. Answer the current question or wait for it to finish.")
        con.execute("DELETE FROM battle_queue WHERE user_id=?", (uid,))
        opponent = con.execute("SELECT user_id FROM battle_queue WHERE category=? AND user_id<>? ORDER BY queued_at LIMIT 1", (category, uid)).fetchone()
        if not opponent:
            con.execute("INSERT OR REPLACE INTO battle_queue(user_id,category,queued_at) VALUES(?,?,?)", (uid, category, now_iso()))
            return await update.effective_message.reply_text(f"⏳ Searching for a real opponent in {category.title()} Battle…\nKeep the bot open. When another user chooses {category.title()}, the battle starts automatically. Use /cancelbattle to leave the queue.")
        other = int(opponent["user_id"])
        con.execute("DELETE FROM battle_queue WHERE user_id IN (?,?)", (uid, other))
        cur = con.execute("INSERT INTO battles(player1,player2,category,created_at,answer) VALUES(?,?,?,?,?)", (other, uid, category, now_iso(), ""))
        battle_id = cur.lastrowid
    await safe_send(context.bot, other, f"⚔️ Opponent found! Your {category.title()} Battle is starting. 10 questions; first correct answer scores.")
    await update.effective_message.reply_text(f"⚔️ Opponent found! Your {category.title()} Battle is starting. 10 questions; first correct answer scores.")
    await ask_battle_question(context.bot, battle_id)

async def handle_battle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not msg.text or msg.text.startswith("/"):
        return False
    with db_connect() as con:
        b = con.execute("SELECT * FROM battles WHERE status='active' AND (player1=? OR player2=?) ORDER BY id DESC LIMIT 1", (user.id, user.id)).fetchone()
        if not b:
            return False
        if normalize_answer(msg.text) != normalize_answer(b["answer"]):
            return True
        idx = b["question_index"]
        col = "score1" if user.id == b["player1"] else "score2"
        cur = con.execute(f"UPDATE battles SET {col}={col}+1, question_index=question_index+1 WHERE id=? AND status='active' AND question_index=?", (b["id"], idx))
        if cur.rowcount:
            con.execute("UPDATE users SET points=points+5 WHERE user_id=?", (user.id,))
        scores = con.execute("SELECT score1,score2 FROM battles WHERE id=?", (b["id"],)).fetchone()
    await safe_send(context.bot, b["player1"], f"✅ {user.first_name} got it first! Answer: {b['answer']}\nPoints: You {scores['score1']} | Opponent {scores['score2']}")
    await safe_send(context.bot, b["player2"], f"✅ {user.first_name} got it first! Answer: {b['answer']}\nPoints: You {scores['score2']} | Opponent {scores['score1']}")
    await ask_battle_question(context.bot, b["id"])
    return True

async def battle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    await games_cmd(update, context)

async def cancel_battle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db_connect() as con:
        con.execute("DELETE FROM battle_queue WHERE user_id=?", (uid,))
    await update.effective_message.reply_text("Battle queue cancelled.")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    uid = q.from_user.id
    data = q.data or ""
    await q.answer()

    if data == "edit_profile":
        if not await access_check(update, context):
            return
        await begin_profile(update, context)
        return

    if data.startswith("country_"):
        if not await access_check(update, context):
            return
        raw_country = data[len("country_"):]
        country_map = {
            "india": "India", "pakistan": "Pakistan", "bangladesh": "Bangladesh",
            "nepal": "Nepal", "sri_lanka": "Sri Lanka", "uae": "UAE",
            "saudi_arabia": "Saudi Arabia", "united_kingdom": "United Kingdom",
            "united_states": "United States", "canada": "Canada", "other": "Other"
        }
        country = country_map.get(raw_country)
        if not country:
            return
        if country == "Other":
            context.user_data["profile_step"] = "country_other"
            await profile_prompt(update, context, "Type your country name (letters only, up to 50 characters):")
            return
        with db_connect() as con:
            con.execute("UPDATE users SET country=? WHERE user_id=?", (country, uid))
        context.user_data["profile_step"] = "name"
        await profile_prompt(update, context, "✍️ What name should other members see? Use a nickname; your Telegram account name/ID will not be shown as your public profile name.")
        return

    if data.startswith("profile_gender_"):
        if not await access_check(update, context):
            return
        gender = "woman" if data.endswith("_woman") else "man"
        with db_connect() as con:
            con.execute("UPDATE users SET gender=?, profile_complete=1 WHERE user_id=?", (gender, uid))
        context.user_data.pop("profile_step", None)
        previous_id = context.user_data.pop("profile_prompt_message_id", None)
        if previous_id:
            try:
                await context.bot.delete_message(chat_id=q.message.chat_id, message_id=previous_id)
            except TelegramError:
                pass
        await q.message.reply_text(
            "✅ Profile complete!\nAge matching: any adult age (18+).\n"
            "Your chosen display name is shown to others. Country and age appear after a match; gender details are visible to active VIP users only.",
            reply_markup=main_keyboard(uid),
        )
        await safe_send(context.bot, uid, "Use the buttons below anytime.", reply_markup=persistent_keyboard())
        return

    if data == "vip_features":
        if not await access_check(update, context):
            return
        row = user_row(uid)
        active = bool(vip_active(row))
        status = "✅ Your VIP is active" if active else "🔒 These benefits unlock with approved VIP"
        await q.message.reply_text(
            "💎 FIND PARTNER VIP — VIP BENEFITS\n\n"
            "🎯 Gender matching: choose Women, Men, or Any (mutual preferences are respected).\n"
            "👀 See a matched partner's gender; standard users see it hidden.\n"
            "🌍 Country + age profile details after a match.\n"
            "✨ VIP profile badge and priority placement in the matching queue (only when a compatible person is available).\n"
            "🎯 Choose Women / Men / Any; both users’ preferences must match.\n"
            "🏆 GameVerse battles, XP and leaderboard are available to everyone; VIP does not promise fake wins or bots.\n"
            "🛡️ Report, block and stop a chat whenever you feel uncomfortable.\n"
            "🎁 Referral rewards and profile tools remain available.\n\n"
            f"{status}\nVIP activation is manually reviewed by the owner; payment requests are not automatic.\n\n"
            "Use /vip to view plans or choose VIP Gender Matching below.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Set Gender Preference", callback_data="vip_gender_menu")],
                [InlineKeyboardButton("💳 View VIP Plans", callback_data="vip")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")],
            ]),
        )
        return

    if data == "main_menu":
        if not await access_check(update, context):
            return
        await q.message.reply_text("🏠 Main menu", reply_markup=main_keyboard(uid))
        return

    if data == "vip_gender_menu":
        if not await access_check(update, context):
            return
        row = user_row(uid)
        if not vip_active(row):
            await q.message.reply_text("👑 Gender matching preferences are a VIP feature. Activate VIP first with /vip.")
            return
        current = row["looking_for"] or "any"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(("✅ " if current == "women" else "") + "Women", callback_data="vip_gender_women")],
            [InlineKeyboardButton(("✅ " if current == "men" else "") + "Men", callback_data="vip_gender_men")],
            [InlineKeyboardButton(("✅ " if current == "any" else "") + "Any", callback_data="vip_gender_any")],
        ])
        await q.message.reply_text("💎 Choose who you'd like to be matched with:", reply_markup=markup)
        return

    if data.startswith("vip_gender_"):
        if not await access_check(update, context):
            return
        row = user_row(uid)
        if not vip_active(row):
            await q.message.reply_text("VIP is not active on your account. Use /vip to view plans.")
            return
        pref = data[len("vip_gender_"):]
        if pref not in ("women", "men", "any"):
            return
        with db_connect() as con:
            con.execute("UPDATE users SET looking_for=? WHERE user_id=?", (pref, uid))
        await q.message.reply_text(f"✅ VIP matching preference saved: {pref.title()}. This preference is checked against both users before pairing.")
        return

    if data == "verify_join":
        if uid != OWNER_ID and not is_admin_id(uid):
            ok, link = await check_membership(context.bot, uid)
            if not ok:
                await q.message.reply_text("❌ I can't verify both memberships yet. Join both chats and try again. If you already joined, the owner must check the numeric chat IDs and bot permissions.", reply_markup=join_keyboard())
                return
        row = user_row(uid)
        if not row or not row["age_confirmed"]:
            await q.message.reply_text("🔞 Membership verified. Now confirm you are 18 or older.", reply_markup=age_keyboard())
        else:
            await q.message.reply_text("✅ Membership verified. Welcome back!", reply_markup=main_keyboard(uid))
        return
    if data == "age_yes":
        existing_user = user_row(uid)
        if existing_user and existing_user["age_blocked"]:
            await q.message.reply_text("🔞 Access is unavailable because the age entered for this account was under 18.")
            return
        if uid != OWNER_ID and not is_admin_id(uid):
            ok, _ = await check_membership(context.bot, uid)
            if not ok:
                await q.message.reply_text("Please join the group and channel first.", reply_markup=join_keyboard())
                return
        upsert_user(q.from_user)
        referrer = None
        with db_connect() as con:
            con.execute("UPDATE users SET age_confirmed=1 WHERE user_id=?", (uid,))
            ref = con.execute("SELECT referrer_id FROM referrals WHERE referred_id=? AND qualified=0", (uid,)).fetchone()
            if ref:
                con.execute("UPDATE referrals SET qualified=1 WHERE referred_id=?", (uid,))
                referrer = int(ref["referrer_id"])
                count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1", (referrer,)).fetchone()[0]
                already = con.execute("SELECT 1 FROM referral_rewards WHERE user_id=?", (referrer,)).fetchone()
                if count >= 20 and not already:
                    expires = (datetime.now(timezone.utc) + timedelta(days=39)).isoformat()
                    con.execute("UPDATE users SET vip_until=?, vip_lifetime=0 WHERE user_id=?", (expires, referrer))
                    con.execute("INSERT OR IGNORE INTO referral_rewards(user_id,granted_at) VALUES(?,?)", (referrer, now_iso()))
        if referrer:
            with db_connect() as con:
                count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1", (referrer,)).fetchone()[0]
                rewarded = con.execute("SELECT 1 FROM referral_rewards WHERE user_id=?", (referrer,)).fetchone()
            if count >= 20 and rewarded:
                await safe_send(context.bot, referrer, "🎉 Your 20 qualified referrals are complete. Free VIP has been activated for 39 days!")
        await begin_profile(update, context)
        return
    if data == "age_no":
        await q.message.reply_text("Sorry, this bot is only for adults aged 18+. You cannot use matching features.")
        return

    if data.startswith("battle_"):
        category = data.split("_", 1)[1]
        if category in BATTLE_QUESTIONS:
            await queue_battle(update, context, category)
        return
    if data == "rps_menu":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("✊ Rock", callback_data="rps_rock"), InlineKeyboardButton("✋ Paper", callback_data="rps_paper"), InlineKeyboardButton("✌️ Scissors", callback_data="rps_scissors")]])
        await q.message.reply_text("✊ Rock Paper Scissors — vs bot", reply_markup=markup)
        return
    if data == "admin_panel":
        if not is_admin_id(uid):
            return await q.answer("Admin only", show_alert=True)
        await q.message.reply_text("🛡️ Admin Panel\n/stats /users /reports /payments\n/ban ID /unban ID /addadmin ID /removeadmin ID\n/vipadd ID DAYS /viplifetime ID /vipremove ID /broadcast text")
        return
    if data == "posts":
        await q.message.reply_text("📰 Posts: the bot's post feed is not connected yet. Owner can publish announcements with /broadcast.")
        return
    if data == "leaderboard":
        await leaderboard_cmd(update, context)
        return
    if data == "friends":
        await friends_cmd(update, context)
        return
    if data == "saved":
        await saved_cmd(update, context)
        return
    if data == "friend_request":
        if not await access_check(update, context):
            return
        row = user_row(uid)
        if not row or not row["partner_id"]:
            await q.message.reply_text("🤝 Connect with a partner first. Both people must agree to become friends.")
            return
        partner = int(row["partner_id"])
        with db_connect() as con:
            exists = con.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (uid, partner)).fetchone()
            if exists:
                await q.message.reply_text("You are already friends with this partner.")
                return
            cur = con.execute("INSERT OR IGNORE INTO friend_requests(from_id,to_id,created_at,status) VALUES(?,?,?,'pending')", (uid, partner, now_iso()))
            request = con.execute("SELECT id FROM friend_requests WHERE from_id=? AND to_id=?", (uid, partner)).fetchone()
        if cur.rowcount:
            sender = user_row(uid)
            sender_name = (sender["display_name"] or sender["first_name"] or "A member") if sender else "A member"
            await safe_send(context.bot, partner, f"🤝 {sender_name} wants to add you as a friend. Accept only if you agree.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Accept friend request", callback_data=f"friend_accept_{request['id']}"), InlineKeyboardButton("❌ Decline", callback_data=f"friend_decline_{request['id']}")]]))
            await q.message.reply_text("Friend request sent. The other person must accept it.")
        else:
            await q.message.reply_text("A friend request is already pending.")
        return
    if data.startswith("friend_accept_") or data.startswith("friend_decline_"):
        accept = data.startswith("friend_accept_")
        try:
            rid = int(data.rsplit("_", 1)[1])
        except ValueError:
            return
        with db_connect() as con:
            req = con.execute("SELECT * FROM friend_requests WHERE id=? AND to_id=? AND status='pending'", (rid, uid)).fetchone()
            if not req:
                await q.message.reply_text("This request is unavailable or already handled.")
                return
            status = "accepted" if accept else "declined"
            con.execute("UPDATE friend_requests SET status=? WHERE id=?", (status, rid))
            if accept:
                con.execute("INSERT OR IGNORE INTO friends(user_id,friend_id,created_at) VALUES(?,?,?)", (uid, req["from_id"], now_iso()))
                con.execute("INSERT OR IGNORE INTO friends(user_id,friend_id,created_at) VALUES(?,?,?)", (req["from_id"], uid, now_iso()))
        await q.message.reply_text("✅ Friend request accepted. You are now mutual friends." if accept else "Friend request declined.")
        await safe_send(context.bot, req["from_id"], f"Your friend request was {status}.")
        return
    if data == "save_partner":
        if not await access_check(update, context):
            return
        row = user_row(uid)
        if not row or not row["partner_id"]:
            await q.message.reply_text("Connect to a partner first, then tap Save Partner.")
        else:
            with db_connect() as con:
                con.execute("INSERT OR IGNORE INTO saved_partners(user_id, partner_id, saved_at) VALUES(?,?,?)", (uid, row["partner_id"], now_iso()))
            await q.message.reply_text("⭐ Partner saved.")
        return
    if data == "referral":
        me = await context.bot.get_me()
        with db_connect() as con:
            count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1", (uid,)).fetchone()[0]
        await q.message.reply_text(f"🎁 Free VIP for 39 days\nQualified referrals: {count}/20\nYour link: https://t.me/{me.username}?start=ref_{uid}\nA referral counts after the invited person joins both chats and confirms they are 18+.")
        return

    if data.startswith("vipreq_"):
        if not await access_check(update, context):
            return
        plan_key = data.split("_", 1)[1]
        plans = {"30d": ("30 days", "$2.99"), "90d": ("90 days", "$6.99"), "life": ("lifetime", "$14.99")}
        if plan_key not in plans:
            return
        plan, amount = plans[plan_key]
        with db_connect() as con:
            cur = con.execute(
                "INSERT INTO payments(user_id,plan,amount,created_at) VALUES(?,?,?,?)",
                (uid, plan, amount, now_iso()),
            )
            pid = cur.lastrowid
        await q.message.reply_text(f"💎 VIP request #{pid} sent for manual review.\n\n👑 Owner: @z3rv_x\n🆔 Owner Telegram ID: {OWNER_ID}\n\nThis is not a payment confirmation. Never send passwords or login codes.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact VIP Owner", url="https://t.me/z3rv_x")]]))
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Approve", callback_data=f"payapprove_{pid}"),
              InlineKeyboardButton("❌ Reject", callback_data=f"payreject_{pid}")]]
        )
        await safe_send(
            context.bot, OWNER_ID,
            f"💳 VIP request #{pid}\nUser: {uid}\nPlan: {plan}\nListed price: {amount}\n"
            "Review only after checking payment through your own trusted method.",
            reply_markup=markup,
        )
        return

    if data.startswith("payapprove_") or data.startswith("payreject_"):
        if not is_admin_id(uid):
            return await q.answer("Admin only.", show_alert=True)
        action, raw_pid = data.split("_", 1)
        try:
            pid = int(raw_pid)
        except ValueError:
            return
        with db_connect() as con:
            payment = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            if not payment or payment["status"] != "pending":
                return await q.message.reply_text("Request missing or already handled.")
            if action == "payapprove":
                days = {"30 days": 30, "90 days": 90}.get(payment["plan"])
                if days:
                    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
                    con.execute("UPDATE users SET vip_until=?, vip_lifetime=0 WHERE user_id=?", (expires, payment["user_id"]))
                elif payment["plan"] == "lifetime":
                    con.execute("UPDATE users SET vip_lifetime=1, vip_until=NULL WHERE user_id=?", (payment["user_id"],))
                con.execute("UPDATE payments SET status='approved', handled_by=? WHERE id=?", (uid, pid))
                outcome = "approved"
            else:
                con.execute("UPDATE payments SET status='rejected', handled_by=? WHERE id=?", (uid, pid))
                outcome = "rejected"
        await q.message.reply_text(f"VIP request #{pid} {outcome}.")
        await safe_send(context.bot, payment["user_id"], f"Your VIP request #{pid} was {outcome} by the admin.")
        return

    if data == "groupgames_menu":
        await send_group_games_menu(q.message, context)
        return

    if data == "groupquiz_new":
        questions = [
            ("🌍 Which is the largest ocean?", ["Atlantic", "Pacific", "Indian", "Arctic"], 1),
            ("🪐 Which planet is called the Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1),
            ("🧪 What is the chemical formula for water?", ["CO2", "O2", "H2O", "NaCl"], 2),
            ("🐝 How many legs does an insect have?", ["4", "6", "8", "10"], 1),
            ("🌈 How many colours are traditionally named in a rainbow?", ["5", "6", "7", "8"], 2),
            ("🏔️ Which is the highest mountain above sea level?", ["K2", "Everest", "Kangchenjunga", "Denali"], 1),
        ]
        question, options, correct = random.choice(questions)
        buttons = [[InlineKeyboardButton(label, callback_data=f"groupquiz_answer_{correct}_{i}")] for i, label in enumerate(options)]
        buttons.append([InlineKeyboardButton("🔄 New Question", callback_data="groupquiz_new")])
        await q.message.reply_text(f"🧠 GROUP TRIVIA\n\n{question}\n\nFirst correct answer earns +5 points!", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("groupquiz_answer_"):
        try:
            _, _, correct_s, chosen_s = data.split("_")
            correct_i, chosen_i = int(correct_s), int(chosen_s)
        except (ValueError, TypeError):
            return
        if chosen_i == correct_i:
            upsert_user(q.from_user)
            with db_connect() as con:
                con.execute("UPDATE users SET points=points+5 WHERE user_id=?", (uid,))
            await q.answer("Correct! +5 points 🎉", show_alert=True)
            await q.message.reply_text(f"🏆 {q.from_user.first_name} got it right and earned 5 points! Use /mypoints in private chat to see your total.")
        else:
            await q.answer("Not this one — try the next round!", show_alert=True)
        return

    if data == "groupriddle_new":
        riddles = [("I have keys but no locks, and space but no room. What am I?", "keyboard"), ("What has a face and two hands but no arms or legs?", "clock"), ("What gets wetter as it dries?", "towel"), ("What has many teeth but cannot bite?", "comb")]
        prompt, answer = random.choice(riddles)
        context.bot_data.setdefault("group_riddles", {})[str(q.message.chat_id)] = answer
        await q.message.reply_text("🧩 RIDDLE ROUND\n" + prompt + "\nReply in the group with your answer. First correct answer earns +5 points!")
        return

    if data == "groupguess_new":
        number = random.randint(1, 10)
        context.bot_data.setdefault("group_guesses", {})[str(q.message.chat_id)] = number
        await q.message.reply_text("🔢 NUMBER GUESS\nA number from 1–10 is chosen. Send `/guess 5` (replace 5 with your guess). Correct guess earns +5 points.", parse_mode="Markdown")
        return

    if data.startswith("groupgame_"):

        game_map = {"dice": "🎲", "darts": "🎯", "football": "⚽", "basketball": "🏀", "bowling": "🎳", "slot": "🎰"}
        game = data.split("_", 1)[1]
        if game == "coin":
            await q.message.reply_text("🪙 Coin Flip: " + random.choice(["Heads! 🟡", "Tails! ⚪"]))
            return
        emoji = game_map.get(game)
        if not emoji:
            return
        try:
            await context.bot.send_dice(chat_id=q.message.chat_id, emoji=emoji)
        except TelegramError:
            await q.message.reply_text("I couldn't post the game here. Check that the bot can send messages in this chat.")
        return

    if data.startswith("rps_"):
        choice = data.split("_", 1)[1]
        computer = random.choice(["rock", "paper", "scissors"])
        if choice == computer:
            result = "It's a draw!"
        elif (choice, computer) in {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}:
            result = "You win!"
        else:
            result = "Bot wins this round."
        await q.message.reply_text(f"You: {choice}\nBot: {computer}\n{result}")
        return

    actions = {
        "find": find_partner,
        "stop": stop_match,
        "profile": profile,
        "vip": vip_cmd,
        "games": games_cmd,
        "help": help_cmd,
    }
    if data in actions:
        # Reuse command logic with the callback update's message.
        await actions[data](update, context)


async def relay_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        txt = (msg.text or "").strip()
        chat_key = str(update.effective_chat.id)
        if txt.lower().startswith("/guess"):
            try:
                guess = int(txt.split(maxsplit=1)[1].split("@")[0])
                target = context.bot_data.get("group_guesses", {}).get(chat_key)
                if target is not None and guess == int(target):
                    upsert_user(user)
                    with db_connect() as con:
                        con.execute("UPDATE users SET points=points+5 WHERE user_id=?", (user.id,))
                    context.bot_data["group_guesses"].pop(chat_key, None)
                    await msg.reply_text(f"🎯 {user.first_name} guessed {target} and earned +5 points!")
                else:
                    await msg.reply_text("Not quite! Try another number from 1–10.")
            except (ValueError, IndexError):
                await msg.reply_text("Usage: /guess 5")
            return
        riddle_answer = context.bot_data.get("group_riddles", {}).get(chat_key)
        if riddle_answer and normalize_answer(txt) == riddle_answer:
            upsert_user(user)
            with db_connect() as con:
                con.execute("UPDATE users SET points=points+5 WHERE user_id=?", (user.id,))
            context.bot_data["group_riddles"].pop(chat_key, None)
            await msg.reply_text(f"🧩 Correct, {user.first_name}! +5 points 🏆")
        return
    if not await access_check(update, context):
        return
    menu_text = (msg.text or "").strip()
    menu_actions = {
        "❤️ Find Partner": find_partner, "🎮 Games": games_cmd,
        "💳 VIP Plans": vip_cmd, "🤝 Friends": friends_cmd,
        "👤 My Profile": profile, "🆘 Safety / Report": help_cmd,
        "🛑 Stop Chat": stop_match,
    }
    if menu_text == "🎁 Free VIP — 39 Days" and not context.user_data.get("profile_step"):
        me = await context.bot.get_me()
        with db_connect() as con:
            count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1", (user.id,)).fetchone()[0]
        await msg.reply_text(f"🎁 FREE VIP — 39 DAYS\nQualified referrals: {count}/20\nInvite link: https://t.me/{me.username}?start=ref_{user.id}\n\nVIP is granted once after 20 invited adults complete membership and age confirmation. No fake or duplicate accounts.")
        return
    if menu_text == "💎 VIP Benefits" and not context.user_data.get("profile_step"):
        row = user_row(user.id)
        active = bool(vip_active(row))
        await msg.reply_text(
            "💎 FIND PARTNER VIP — BENEFITS\n\n"
            "🎯 Choose Women / Men / Any for matching (mutual preferences apply).\n"
            "👀 View a matched partner's gender; standard members see it hidden.\n"
            "✨ VIP badge in profile.\n"
            "🌍 See a matched partner's country and age.\n"
            "🛡️ Reporting, blocking and ending chats remain available to everyone.\n\n"
            + ("✅ VIP is active on your account." if active else "🔒 VIP features unlock after the owner approves your VIP request.")
            + "\n\nVIP does not guarantee instant matches; availability depends on compatible members.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Set Gender Preference", callback_data="vip_gender_menu")],
                [InlineKeyboardButton("💳 View VIP Plans", callback_data="vip")],
            ])
        )
        return
    if menu_text in menu_actions and not context.user_data.get("profile_step"):
        await menu_actions[menu_text](update, context)
        return
    if await handle_battle_answer(update, context):
        return
    profile_step = context.user_data.get("profile_step")
    if profile_step in ("country_other", "name", "age"):
        value = (msg.text or "").strip()
        if not value:
            await msg.reply_text("Please send text only for this profile step.")
            return
        if profile_step == "country_other":
            country = value[:50]
            if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{0,49}", country):
                await msg.reply_text("Please enter a valid country name using letters.")
                return
            with db_connect() as con:
                con.execute("UPDATE users SET country=? WHERE user_id=?", (country, user.id))
            context.user_data["profile_step"] = "name"
            await profile_prompt(update, context, "✍️ What name should other members see? You can use a nickname; your Telegram account name/ID will not be shown as your public profile name.")
            return
        if profile_step == "name":
            display_name = " ".join(value.split())[:32]
            if len(display_name) < 2 or not re.fullmatch(r"[\wÀ-ÿ .'-]{2,32}", display_name, re.UNICODE):
                await msg.reply_text("Please enter a display name of 2–32 letters/numbers, spaces, dots, apostrophes or hyphens.")
                return
            with db_connect() as con:
                con.execute("UPDATE users SET display_name=? WHERE user_id=?", (display_name, user.id))
            context.user_data["profile_step"] = "age"
            await profile_prompt(update, context, "🎂 Type your age as a number. Only adults aged 18+ can continue.")
            return
        try:
            age = int(value)
        except ValueError:
            await msg.reply_text("Please enter your age as a whole number.")
            return
        if age < MIN_AGE:
            with db_connect() as con:
                con.execute("UPDATE users SET age=?, age_confirmed=0, age_blocked=1, profile_complete=0, matching=0 WHERE user_id=?", (age, user.id))
            context.user_data.pop("profile_step", None)
            await msg.reply_text("🔞 This bot is only for adults aged 18+. Access to matching is unavailable.")
            return
        if age > 100:
            await msg.reply_text("Please enter an age between 18 and 100.")
            return
        with db_connect() as con:
            con.execute("UPDATE users SET age=?, age_confirmed=1 WHERE user_id=?", (age, user.id))
        context.user_data["profile_step"] = "gender"
        await profile_prompt(update, context, "⚧ Select your gender:", reply_markup=gender_keyboard())
        return

    row = user_row(user.id)
    if not row or not row["partner_id"]:
        await msg.reply_text("You're not in a chat right now. Use /find to find a partner.")
        return
    partner = row["partner_id"]
    if blocked_by_either(user.id, partner):
        set_matching(user.id, False, None)
        return await msg.reply_text("This chat is no longer available. Use /find to search again.")
    try:
        await context.bot.copy_message(
            chat_id=partner,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
    except Forbidden:
        set_matching(user.id, False, None)
        await msg.reply_text("Your partner is unavailable. Use /find to search again.")
    except TelegramError:
        await msg.reply_text("I couldn't deliver that message. Try a shorter text or another media item.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not is_admin_id(user.id):
        return await msg.reply_text("⛔ Admin only.")
    source = msg.reply_to_message
    text_arg = " ".join(context.args).strip()

    if not source and not text_arg:
        return await msg.reply_text(
            "📢 BROADCAST HELP\n\n"
            "Text: /broadcast Your announcement\n\n"
            "Any media (photo/video/document/audio/voice/animation/sticker):\n"
            "1) Send the media to this bot.\n2) Reply to it with /broadcast\n\n"
            "You can also reply to a text message with /broadcast to copy it.\n"
            "This sends to registered users. Use responsibly."
        )

    with db_connect() as con:
        rows = con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    targets = [r["user_id"] for r in rows if r["user_id"] != user.id]
    status = await msg.reply_text(f"📣 Broadcast started for {len(targets)} users…")
    sent = failed = 0

    for target in targets:
        try:
            if source:
                await context.bot.copy_message(
                    chat_id=target,
                    from_chat_id=source.chat_id,
                    message_id=source.message_id,
                )
            else:
                await context.bot.send_message(chat_id=target, text=text_arg)
            sent += 1
        except RetryAfter as exc:
            delay = getattr(exc, "retry_after", 1)
            await asyncio.sleep(float(delay) + 0.5)
            try:
                if source:
                    await context.bot.copy_message(chat_id=target, from_chat_id=source.chat_id, message_id=source.message_id)
                else:
                    await context.bot.send_message(chat_id=target, text=text_arg)
                sent += 1
            except TelegramError:
                failed += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(BROADCAST_PAUSE)

    await status.edit_text(f"✅ Broadcast finished.\nDelivered: {sent}\nFailed/blocked: {failed}")


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = con.execute("SELECT COUNT(*) n FROM users WHERE matching=1 OR partner_id IS NOT NULL").fetchone()["n"]
        banned = con.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
        vip = con.execute("SELECT COUNT(*) n FROM users WHERE vip_lifetime=1 OR vip_until>?", (now_iso(),)).fetchone()["n"]
        reports = con.execute("SELECT COUNT(*) n FROM reports WHERE status='open'").fetchone()["n"]
        payments = con.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
    await update.effective_message.reply_text(
        f"📊 Bot stats\n👥 Total registered users: {users}\n🔎 Searching or chatting: {active}\n🚫 Banned: {banned}\n💎 VIP: {vip}\n"
        f"Open reports: {reports}\nPending VIP requests: {payments}"
    )


@admin_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute("SELECT user_id, username, first_name, banned FROM users ORDER BY joined_at DESC LIMIT 50").fetchall()
    if not rows:
        return await update.effective_message.reply_text("No users yet.")
    lines = ["👥 Latest users (max 50):"]
    for r in rows:
        lines.append(f"{r['user_id']} | @{r['username'] or '-'} | {r['first_name']} | banned={r['banned']}")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@admin_only
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        return await update.effective_message.reply_text("Usage: /user USER_ID")
    uid = int(context.args[0])
    row = user_row(uid)
    if not row:
        return await update.effective_message.reply_text("User not found.")
    await update.effective_message.reply_text(
        f"User {uid}\nUsername: @{row['username'] or '-'}\nName: {row['first_name']}\n"
        f"Age confirmed: {row['age_confirmed']}\nBanned: {row['banned']}\nVIP lifetime: {row['vip_lifetime']}\n"
        f"VIP until: {row['vip_until'] or '-'}\nMatching: {row['matching']}\nPartner: {row['partner_id'] or '-'}"
    )


@admin_only
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        return await update.effective_message.reply_text("Usage: /ban USER_ID [reason]")
    uid = int(context.args[0])
    if uid == OWNER_ID:
        return await update.effective_message.reply_text("Owner cannot be banned.")
    reason = " ".join(context.args[1:])[:300] or "No reason provided"
    with db_connect() as con:
        con.execute("UPDATE users SET banned=1, matching=0, partner_id=NULL WHERE user_id=?", (uid,))
        con.execute("UPDATE users SET matching=0, partner_id=NULL WHERE partner_id=?", (uid,))
    await update.effective_message.reply_text(f"User {uid} banned. Reason: {reason}")
    await safe_send(context.bot, uid, f"Your account has been restricted. Reason: {reason}")


@admin_only
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        return await update.effective_message.reply_text("Usage: /unban USER_ID")
    uid = int(context.args[0])
    with db_connect() as con:
        con.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"User {uid} unbanned (if the account existed).")


@admin_only
async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can add admins.")
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /addadmin USER_ID")
    uid = int(context.args[0])
    with db_connect() as con:
        con.execute("INSERT OR IGNORE INTO admins(user_id, added_by, added_at) VALUES(?,?,?)", (uid, OWNER_ID, now_iso()))
    await update.effective_message.reply_text(f"Admin added: {uid}. Owner privileges remain protected.")


@admin_only
async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can remove admins.")
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /removeadmin USER_ID")
    uid = int(context.args[0])
    if uid == OWNER_ID:
        return await update.effective_message.reply_text("Owner cannot be removed.")
    with db_connect() as con:
        con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"Admin removed if present: {uid}")


@admin_only
async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
    await update.effective_message.reply_text("Admins:\n" + "\n".join(str(r["user_id"]) for r in rows))


@admin_only
async def vipadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        return await update.effective_message.reply_text("Usage: /vipadd USER_ID DAYS")
    uid, days = int(context.args[0]), int(context.args[1])
    if days < 1 or days > 3650:
        return await update.effective_message.reply_text("Days must be between 1 and 3650.")
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with db_connect() as con:
        con.execute("UPDATE users SET vip_until=?, vip_lifetime=0 WHERE user_id=?", (expires, uid))
    await update.effective_message.reply_text(f"VIP granted to {uid} for {days} days (if user exists).")
    await safe_send(context.bot, uid, f"💎 VIP activated for {days} days.")


@admin_only
async def viplifetime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /viplifetime USER_ID")
    uid = int(context.args[0])
    with db_connect() as con:
        con.execute("UPDATE users SET vip_lifetime=1, vip_until=NULL WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"Lifetime VIP granted to {uid} (if user exists).")
    await safe_send(context.bot, uid, "💎 Lifetime VIP activated.")


@admin_only
async def vipremove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /vipremove USER_ID")
    uid = int(context.args[0])
    with db_connect() as con:
        con.execute("UPDATE users SET vip_lifetime=0, vip_until=NULL WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"VIP removed from {uid} (if user exists).")


@admin_only
async def viplist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute("SELECT user_id, vip_until, vip_lifetime FROM users WHERE vip_lifetime=1 OR vip_until>?", (now_iso(),)).fetchall()
    if not rows:
        return await update.effective_message.reply_text("No active VIP users.")
    await update.effective_message.reply_text(
        "💎 Active VIP users:\n" + "\n".join(
            f"{r['user_id']} — {'Lifetime' if r['vip_lifetime'] else r['vip_until']}" for r in rows
        )[:3900]
    )


@admin_only
async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute("SELECT * FROM reports WHERE status='open' ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        return await update.effective_message.reply_text("No open reports.")
    lines = ["🚩 Open reports:"]
    for r in rows:
        lines.append(f"#{r['id']} from {r['reporter_id']} against {r['reported_id']}: {r['reason']}")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@admin_only
async def resolve_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /resolvereport REPORT_ID")
    rid = int(context.args[0])
    with db_connect() as con:
        con.execute("UPDATE reports SET status='resolved' WHERE id=?", (rid,))
    await update.effective_message.reply_text(f"Report #{rid} marked resolved if it existed.")


@admin_only
async def pending_payments_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as con:
        rows = con.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        return await update.effective_message.reply_text("No pending VIP requests.")
    lines = ["💳 Pending VIP requests:"]
    for r in rows:
        lines.append(f"#{r['id']} user={r['user_id']} plan={r['plan']} amount={r['amount']}")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@admin_only
async def setgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only owner can change this setting.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /setgroup @publicgroup or https://t.me/publicgroup")
    value = context.args[0].strip()
    set_setting("official_group", value)
    await update.effective_message.reply_text("Saved group link setting. For membership verification, also set GROUP_CHAT_ID in Render.")


@admin_only
async def setchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only owner can change this setting.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /setchannel @publicchannel or https://t.me/publicchannel")
    value = context.args[0].strip()
    set_setting("official_channel", value)
    await update.effective_message.reply_text("Saved channel link setting. For membership verification, also set CHANNEL_CHAT_ID in Render.")




@admin_only
async def addchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can change required chats.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /addchannel @publicchannel")
    username = context.args[0].strip()
    if username.startswith("https://t.me/"):
        username = "@" + username.rstrip("/").split("/")[-1]
    if not username.startswith("@") or len(username) < 6 or not username[1:].replace("_", "").isalnum():
        return await update.effective_message.reply_text("Use a public Telegram username, e.g. /addchannel @MyChannel")
    username = username.lower()
    try:
        chat = await context.bot.get_chat(username)
        if chat.type != "channel":
            return await update.effective_message.reply_text("That username is not a channel. Use /addgc for a group.")
    except TelegramError:
        return await update.effective_message.reply_text("I can't access that channel. Check the @username and add the bot as an administrator so membership can be verified.")
    with db_connect() as con:
        con.execute("INSERT OR REPLACE INTO required_chats(username,chat_type,title,added_by,added_at) VALUES(?,?,?,?,?)", (username, "channel", chat.title or username, update.effective_user.id, now_iso()))
    await update.effective_message.reply_text(f"✅ Required channel added as slot 3+. Users must join {username} before continuing. Use /requiredchats to view the list.")


@admin_only
async def addgc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can change required chats.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /addgc @publicgroup")
    username = context.args[0].strip()
    if username.startswith("https://t.me/"):
        username = "@" + username.rstrip("/").split("/")[-1]
    if not username.startswith("@") or len(username) < 6 or not username[1:].replace("_", "").isalnum():
        return await update.effective_message.reply_text("Use a public Telegram username, e.g. /addgc @MyGroup")
    username = username.lower()
    try:
        chat = await context.bot.get_chat(username)
        if chat.type not in ("group", "supergroup"):
            return await update.effective_message.reply_text("That username is not a group. Use /addchannel for a channel.")
    except TelegramError:
        return await update.effective_message.reply_text("I can't access that group. Check the public @username and add the bot to the group so membership can be verified.")
    with db_connect() as con:
        con.execute("INSERT OR REPLACE INTO required_chats(username,chat_type,title,added_by,added_at) VALUES(?,?,?,?,?)", (username, "group", chat.title or username, update.effective_user.id, now_iso()))
    await update.effective_message.reply_text(f"✅ Required group added as slot 3+. Users must join {username} before continuing. Use /requiredchats to view the list.")


@admin_only
async def removechat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can change required chats.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /removechat @username")
    username = context.args[0].strip().lower().lstrip("@")
    with db_connect() as con:
        cur = con.execute("DELETE FROM required_chats WHERE lower(ltrim(username,'@'))=?", (username,))
    if cur.rowcount:
        await update.effective_message.reply_text(f"✅ Removed @{username} from required chats. Slots 1 and 2 are unchanged.")
    else:
        await update.effective_message.reply_text("That extra chat wasn't found. Slots 1 and 2 are fixed by the existing Render settings.")


@admin_only
async def removechannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can change required chats.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /removechannel @username")
    username = context.args[0].strip().lower().lstrip("@")
    with db_connect() as con:
        cur = con.execute("DELETE FROM required_chats WHERE lower(ltrim(username,'@'))=? AND chat_type='channel'", (username,))
    await update.effective_message.reply_text(f"✅ Removed channel @{username}." if cur.rowcount else "That extra channel wasn't found.")


@admin_only
async def removegc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can change required chats.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /removegc @username")
    username = context.args[0].strip().lower().lstrip("@")
    with db_connect() as con:
        cur = con.execute("DELETE FROM required_chats WHERE lower(ltrim(username,'@'))=? AND chat_type='group'", (username,))
    await update.effective_message.reply_text(f"✅ Removed group @{username}." if cur.rowcount else "That extra group wasn't found.")


@admin_only
async def requiredchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("Only the owner can view required chat configuration.")
    lines = ["📌 Required join chats", "1. Existing official channel (Render CHANNEL_CHAT_ID)", "2. Existing official group (Render GROUP_CHAT_ID)"]
    for i, chat in enumerate(required_chat_rows(), start=3):
        lines.append(f"{i}. {chat['chat_type'].title()}: @{chat['username'].lstrip('@')} — {chat['title']}")
    lines.append("Commands: /addchannel @username | /addgc @username | /removechat @username")
    await update.effective_message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


# Admin command registration uses the same callback functions for all admins.
def build_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("find", find_partner))
    app.add_handler(CommandHandler("stop", stop_match))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("setbio", set_bio))
    app.add_handler(CommandHandler("report", report_partner))
    app.add_handler(CommandHandler("block", block_partner))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("games", games_cmd))
    app.add_handler(CommandHandler("play", games_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("mypoints", mypoints_cmd))
    app.add_handler(CommandHandler("saved", saved_cmd))
    app.add_handler(CommandHandler("friends", friends_cmd))
    app.add_handler(CommandHandler("battle", battle_cmd))
    app.add_handler(CommandHandler("cancelbattle", cancel_battle_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("vipadd", vipadd_cmd))
    app.add_handler(CommandHandler("viplifetime", viplifetime_cmd))
    app.add_handler(CommandHandler("vipremove", vipremove_cmd))
    app.add_handler(CommandHandler("viplist", viplist_cmd))
    app.add_handler(CommandHandler("reports", reports_cmd))
    app.add_handler(CommandHandler("resolvereport", resolve_report_cmd))
    app.add_handler(CommandHandler("payments", pending_payments_cmd))
    app.add_handler(CommandHandler("setgroup", setgroup_cmd))
    app.add_handler(CommandHandler("setchannel", setchannel_cmd))
    app.add_handler(CommandHandler("addchannel", addchannel_cmd))
    app.add_handler(CommandHandler("addgc", addgc_cmd))
    app.add_handler(CommandHandler("removechat", removechat_cmd))
    app.add_handler(CommandHandler("removechannel", removechannel_cmd))
    app.add_handler(CommandHandler("removegc", removegc_cmd))
    app.add_handler(CommandHandler("requiredchats", requiredchats_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))
    app.add_error_handler(error_handler)
    return app


init_db()
bot_app = build_bot()


@asynccontextmanager
async def lifespan(api: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    if bot_app.updater:
        await bot_app.updater.start_polling(drop_pending_updates=False)
    log.info("Find Partner VIP bot started")
    try:
        yield
    finally:
        if bot_app.updater and bot_app.updater.running:
            await bot_app.updater.stop()
        if bot_app.running:
            await bot_app.stop()
        await bot_app.shutdown()


web = FastAPI(title="Find Partner VIP", version="1.0.0", lifespan=lifespan)


@web.get("/")
async def home():
    return {
        "ok": True,
        "service": "Find Partner VIP",
        "version": "1.0.0",
        "bot": "running when application lifespan is active",
    }


@web.get("/health")
async def health():
    return {"ok": True, "service": "Find Partner VIP"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:web", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
