import asyncio
import logging
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
                bio TEXT NOT NULL DEFAULT ''
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
            CREATE TABLE IF NOT EXISTS battle_queue (user_id INTEGER PRIMARY KEY, category TEXT NOT NULL, queued_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS battles (id INTEGER PRIMARY KEY AUTOINCREMENT, player1 INTEGER NOT NULL, player2 INTEGER NOT NULL, category TEXT NOT NULL, question_index INTEGER NOT NULL DEFAULT 0, score1 INTEGER NOT NULL DEFAULT 0, score2 INTEGER NOT NULL DEFAULT 0, answer TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS saved_partners (user_id INTEGER NOT NULL, partner_id INTEGER NOT NULL, saved_at TEXT NOT NULL, PRIMARY KEY(user_id, partner_id));
            """
        )
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


def main_keyboard(user_id=None):
    rows = [
        [InlineKeyboardButton("❤️ Next", callback_data="find")],
        [InlineKeyboardButton("📰 Read Posts", callback_data="posts"), InlineKeyboardButton("🎮 Play Games", callback_data="games")],
        [InlineKeyboardButton("⭐ Save Partner", callback_data="save_partner"), InlineKeyboardButton("⏹ Stop chat", callback_data="stop")],
        [InlineKeyboardButton("👤 My profile", callback_data="profile"), InlineKeyboardButton("💎 VIP", callback_data="vip")],
        [InlineKeyboardButton("🎁 Free VIP 30 Days", callback_data="referral")],
        [InlineKeyboardButton("🆘 Help & safety", callback_data="help")],
    ]
    if user_id and is_admin_id(user_id):
        rows.append([InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=OFFICIAL_CHANNEL)],
        [InlineKeyboardButton("👥 Join Group", url=OFFICIAL_GROUP)],
        [InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join")],
    ])


def age_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("I confirm I am 18 or older", callback_data="age_yes")],
         [InlineKeyboardButton("I am under 18", callback_data="age_no")]]
    )


async def check_membership(bot, user_id):
    if not GROUP_CHAT_ID or not CHANNEL_CHAT_ID:
        return False, OFFICIAL_GROUP
    checks = []
    if GROUP_CHAT_ID:
        checks.append((GROUP_CHAT_ID, setting("official_group", OFFICIAL_GROUP)))
    if CHANNEL_CHAT_ID:
        checks.append((CHANNEL_CHAT_ID, setting("official_channel", OFFICIAL_CHANNEL)))
    for chat_id, link in checks:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False, link
        except TelegramError:
            # A membership check only works when the bot can access that chat.
            # Fail closed rather than silently bypassing a configured requirement.
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
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        if msg:
            await msg.reply_text("Please open me in a private chat.")
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
    await msg.reply_text("✨ Find Partner VIP\n\n❤️ Next — find a real available adult.\n📰 Read Posts\n🎮 Play Games — Battle Mathematics, Science or History with another online player.\n\nUse /report or /block for safety.", reply_markup=main_keyboard(user.id))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    await update.effective_message.reply_text(
        "Commands:\n"
        "/start — open menu\n/find — find a partner\n/stop — end current match\n/profile — view profile\n"
        "/setbio text — set your short bio\n/report reason — report your current partner\n"
        "/block — block current partner\n/vip — VIP plans\n/games — games\n\n"
        "Safety: never send passwords, verification codes, financial details, or your address. "
        "You can end a chat with /stop at any time."
    )


async def find_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    uid = update.effective_user.id
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    current = user_row(uid)
    if current and current["partner_id"]:
        await update.effective_message.reply_text("You already have a partner. Use /stop before finding another.")
        return
    with db_connect() as con:
        candidates = con.execute(
            "SELECT user_id FROM users WHERE user_id<>? AND matching=1 AND partner_id IS NULL "
            "AND age_confirmed=1 AND banned=0 ORDER BY RANDOM() LIMIT 30",
            (uid,),
        ).fetchall()
    partner_id = None
    for candidate in candidates:
        other = candidate["user_id"]
        if not blocked_by_either(uid, other):
            partner_id = other
            break
    if partner_id is None:
        set_matching(uid, True, None)
        await update.effective_message.reply_text(
            "🔎 Searching for an available partner…\nYou'll be connected when someone else is searching. Use /stop to cancel."
        )
        return
    with db_connect() as con:
        # Atomic pairing prevents both users being assigned to different people.
        con.execute("UPDATE users SET matching=0, partner_id=? WHERE user_id=? AND partner_id IS NULL", (partner_id, uid))
        con.execute("UPDATE users SET matching=0, partner_id=? WHERE user_id=? AND partner_id IS NULL", (uid, partner_id))
        a = con.execute("SELECT partner_id FROM users WHERE user_id=?", (uid,)).fetchone()
        b = con.execute("SELECT partner_id FROM users WHERE user_id=?", (partner_id,)).fetchone()
    if not a or not b or a["partner_id"] != partner_id or b["partner_id"] != uid:
        await update.effective_message.reply_text("Someone else got matched first. Try /find again.")
        return
    await update.effective_message.reply_text("✅ Partner found! Send a message here to chat. /stop ends the chat.")
    await safe_send(context.bot, partner_id, "✅ Partner found! Send a message here to chat. /stop ends the chat.")


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
        f"👤 Your profile\nID: {row['user_id']}\nUsername: @{row['username'] or 'not set'}\n"
        f"VIP: {status}\nBio: {row['bio'] or 'No bio yet'}\n\nSet your bio with /setbio your text"
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


def vip_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("30 days — $2.99", callback_data="vipreq_30d")],
            [InlineKeyboardButton("90 days — $6.99", callback_data="vipreq_90d")],
            [InlineKeyboardButton("Lifetime — $14.99", callback_data="vipreq_life")],
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


async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_check(update, context):
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Battle: Mathematics", callback_data="battle_math")],
        [InlineKeyboardButton("🧪 Battle: Science", callback_data="battle_science")],
        [InlineKeyboardButton("📜 Battle: History", callback_data="battle_history")],
        [InlineKeyboardButton("✊ Rock Paper Scissors (vs bot)", callback_data="rps_menu")],
    ])
    await update.effective_message.reply_text(
        "🎮 GameVerse — play with a real online partner\n\n"
        "Choose a Battle category. The bot will match you with another available player in the same category.\n"
        "Each battle has 10 questions. The first correct answer earns the point. If nobody answers correctly, the next question starts after 25 seconds.\n\n"
        "Other listed games are not presented as playable until their multiplayer engines are implemented.",
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
        con.execute(f"UPDATE battles SET {col}={col}+1, question_index=question_index+1 WHERE id=? AND status='active' AND question_index=?", (b["id"], idx))
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
                    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                    con.execute("UPDATE users SET vip_until=?, vip_lifetime=0 WHERE user_id=?", (expires, referrer))
                    con.execute("INSERT OR IGNORE INTO referral_rewards(user_id,granted_at) VALUES(?,?)", (referrer, now_iso()))
        if referrer:
            with db_connect() as con:
                count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1", (referrer,)).fetchone()[0]
                rewarded = con.execute("SELECT 1 FROM referral_rewards WHERE user_id=?", (referrer,)).fetchone()
            if count >= 20 and rewarded:
                await safe_send(context.bot, referrer, "🎉 Your 20 qualified referrals are complete. Free VIP has been activated for 30 days!")
        await q.message.reply_text("✅ Confirmed. Welcome to Find Partner VIP!", reply_markup=main_keyboard(uid))
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
    if data == "save_partner":
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
        await q.message.reply_text(f"🎁 Free VIP for 30 days\nQualified referrals: {count}/20\nYour link: https://t.me/{me.username}?start=ref_{uid}\nA referral counts after the invited person joins both chats and confirms they are 18+.")
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
        await q.message.reply_text(f"VIP request #{pid} sent to the owner for manual review. This is not a payment confirmation.")
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
    if not msg or not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    if not await access_check(update, context):
        return
    if await handle_battle_answer(update, context):
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
        f"📊 Bot stats\nUsers: {users}\nIn matching/chat: {active}\nBanned: {banned}\nVIP: {vip}\n"
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
