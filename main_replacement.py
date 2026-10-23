import asyncio
import logging
import os
import random
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.constants import ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# Find Partner VIP — single-file replacement
# Required Render env var: BOT_TOKEN
# Optional: OWNER_ID, DB_PATH, OFFICIAL_GROUP, OFFICIAL_CHANNEL,
# GROUP_CHAT_ID, CHANNEL_CHAT_ID
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("find_partner")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7044754988"))
DB_PATH = os.getenv("DB_PATH", "data/find_partner_vip.sqlite3")
OFFICIAL_GROUP = os.getenv("OFFICIAL_GROUP", "https://t.me/chatgcv1")
OFFICIAL_CHANNEL = os.getenv("OFFICIAL_CHANNEL", "https://t.me/arx_smediaexpert")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "").strip()
MIN_AGE = 18
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment settings.")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def now():
    return datetime.now(timezone.utc)

def stamp():
    return now().isoformat()

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def columns(con, table):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT NOT NULL DEFAULT '',
          age_confirmed INTEGER NOT NULL DEFAULT 0, joined_at TEXT NOT NULL DEFAULT '',
          last_seen TEXT NOT NULL DEFAULT '', banned INTEGER NOT NULL DEFAULT 0,
          vip_until TEXT, vip_lifetime INTEGER NOT NULL DEFAULT 0,
          matching INTEGER NOT NULL DEFAULT 0, partner_id INTEGER,
          gender TEXT, looking_for TEXT, bio TEXT NOT NULL DEFAULT '',
          points INTEGER NOT NULL DEFAULT 0, country TEXT, age INTEGER,
          profile_complete INTEGER NOT NULL DEFAULT 0,
          age_blocked INTEGER NOT NULL DEFAULT 0, display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS blocks(user_id INTEGER, blocked_id INTEGER,
          created_at TEXT, PRIMARY KEY(user_id,blocked_id));
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT,
          reporter_id INTEGER, reported_id INTEGER, reason TEXT, created_at TEXT,
          status TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS referrals(referred_id INTEGER PRIMARY KEY,
          referrer_id INTEGER NOT NULL, created_at TEXT NOT NULL,
          qualified INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY,
          added_by INTEGER NOT NULL, added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS game_rounds(chat_id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL, prompt TEXT NOT NULL, answer TEXT, started_by INTEGER,
          started_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        # Add columns safely when upgrading the older database.
        needed = {
          "username":"TEXT", "first_name":"TEXT NOT NULL DEFAULT ''",
          "age_confirmed":"INTEGER NOT NULL DEFAULT 0", "joined_at":"TEXT NOT NULL DEFAULT ''",
          "last_seen":"TEXT NOT NULL DEFAULT ''", "banned":"INTEGER NOT NULL DEFAULT 0",
          "vip_until":"TEXT", "vip_lifetime":"INTEGER NOT NULL DEFAULT 0",
          "matching":"INTEGER NOT NULL DEFAULT 0", "partner_id":"INTEGER",
          "gender":"TEXT", "looking_for":"TEXT", "bio":"TEXT NOT NULL DEFAULT ''",
          "points":"INTEGER NOT NULL DEFAULT 0", "country":"TEXT", "age":"INTEGER",
          "profile_complete":"INTEGER NOT NULL DEFAULT 0",
          "age_blocked":"INTEGER NOT NULL DEFAULT 0", "display_name":"TEXT NOT NULL DEFAULT ''"
        }
        present = columns(con, "users")
        for name, decl in needed.items():
            if name not in present:
                con.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        con.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
                    (OWNER_ID, OWNER_ID, stamp()))

def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def upsert(tg):
    with db() as con:
        con.execute("""INSERT INTO users(user_id,username,first_name,joined_at,last_seen)
        VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen""",
        (tg.id, tg.username or "", tg.first_name or "", stamp(), stamp()))

def total_users():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def admin(uid):
    if uid == OWNER_ID:
        return True
    with db() as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def vip_active(row):
    if not row:
        return False
    if row["vip_lifetime"]:
        return True
    try:
        return bool(row["vip_until"] and datetime.fromisoformat(row["vip_until"]) > now())
    except (ValueError, TypeError):
        return False

def menu():
    return ReplyKeyboardMarkup([
      [KeyboardButton("❤️ Find Partner"), KeyboardButton("⏭️ Next Partner")],
      [KeyboardButton("🛑 Stop Chat"), KeyboardButton("🎮 Games")],
      [KeyboardButton("👥 Total Users"), KeyboardButton("🏆 Leaderboard")],
      [KeyboardButton("✨ My Points"), KeyboardButton("👤 My Profile")],
      [KeyboardButton("🎁 Free VIP — 30 Days"), KeyboardButton("🆘 Safety / Report")],
    ], resize_keyboard=True, is_persistent=True)

def country_keys():
    countries = ["India","Pakistan","Bangladesh","Nepal","Sri Lanka","UAE",
                 "Saudi Arabia","United Kingdom","United States","Canada","Other"]
    rows = [[InlineKeyboardButton(c, callback_data="country:"+c)] for c in countries]
    return InlineKeyboardMarkup(rows)

def gender_keys():
    return InlineKeyboardMarkup([[
      InlineKeyboardButton("Woman", callback_data="gender:woman"),
      InlineKeyboardButton("Man", callback_data="gender:man")
    ]])

async def safe_send(bot, uid, text, **kwargs):
    try:
        await bot.send_message(chat_id=uid, text=text, **kwargs)
        return True
    except TelegramError:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    m = update.effective_message
    if not u or not m:
        return
    upsert(u)
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref = int(context.args[0][4:])
            if ref != u.id and get_user(ref):
                with db() as con:
                    con.execute("INSERT OR IGNORE INTO referrals(referred_id,referrer_id,created_at) VALUES(?,?,?)",
                                (u.id, ref, stamp()))
        except ValueError:
            pass
    row = get_user(u.id)
    if row["banned"] and not admin(u.id):
        return await m.reply_text("Your account is restricted.")
    if row["age_blocked"] and not admin(u.id):
        return await m.reply_text("Access is unavailable because this account entered an age under 18.")
    if not row["age_confirmed"] and not admin(u.id):
        return await m.reply_text("🔞 This matching service is for adults aged 18+ only. Confirm you are 18 or older.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("I am 18+", callback_data="age:yes"),
                                                InlineKeyboardButton("I am under 18", callback_data="age:no")]]))
    if not row["profile_complete"] and not admin(u.id):
        await begin_profile(update, context)
        return
    await m.reply_text(f"💜 FIND PARTNER VIP\n👥 Total registered users: {total_users()}\n\nFind a partner, chat safely, or play group games with real members.\nUse /find, /games, /truthdare, /totalusers.", reply_markup=menu())

async def begin_profile(update, context):
    context.user_data["profile_step"] = "country"
    await update.effective_message.reply_text("🌍 Set up your profile once. Choose your country:", reply_markup=country_keys())

def blocked(a,b):
    with db() as con:
        return bool(con.execute("SELECT 1 FROM blocks WHERE (user_id=? AND blocked_id=?) OR (user_id=? AND blocked_id=?)",
                                (a,b,b,a)).fetchone())

def compatible(a,b):
    x,y=get_user(a),get_user(b)
    if not x or not y or not x["profile_complete"] or not y["profile_complete"]:
        return False
    if not x["age_confirmed"] or not y["age_confirmed"] or x["age_blocked"] or y["age_blocked"]:
        return False
    if x["banned"] or y["banned"] or x["age"] is None or y["age"] is None:
        return False
    xp=(x["looking_for"] or "any") if vip_active(x) else "any"
    yp=(y["looking_for"] or "any") if vip_active(y) else "any"
    xg,yg=(x["gender"] or "").lower(),(y["gender"] or "").lower()
    return not ((xp=="women" and yg!="woman") or (xp=="men" and yg!="man")
                or (yp=="women" and xg!="woman") or (yp=="men" and xg!="man"))

async def find_partner(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return await update.effective_message.reply_text("Please use Find Partner in a private chat with the bot.")
    uid=update.effective_user.id
    if not await access_ok(update, context):
        return
    row=get_user(uid)
    if not row["profile_complete"]:
        await begin_profile(update,context); return
    # Clear the user's old queue state before every fresh search.
    with db() as con:
        con.execute("UPDATE users SET matching=0 WHERE user_id=?", (uid,))
        con.execute("UPDATE users SET matching=0 WHERE partner_id=?", (uid,))
        con.execute("UPDATE users SET partner_id=NULL WHERE user_id=?", (uid,))
        con.execute("UPDATE users SET partner_id=NULL WHERE partner_id=?", (uid,))
        candidates=con.execute("""SELECT user_id FROM users WHERE user_id<>? AND matching=1
        AND partner_id IS NULL AND profile_complete=1 AND age_confirmed=1 AND age_blocked=0
        AND banned=0 ORDER BY last_seen DESC LIMIT 200""",(uid,)).fetchall()
    pid=None
    for item in candidates:
        other=int(item["user_id"])
        if not blocked(uid,other) and compatible(uid,other):
            pid=other; break
    if pid is None:
        with db() as con:
            con.execute("UPDATE users SET matching=1,partner_id=NULL WHERE user_id=?", (uid,))
        return await update.effective_message.reply_text("🔎 Searching for a real compatible member…\nYou are in the queue. Press ⏭️ Next Partner to refresh, or 🛑 Stop Chat to leave.", reply_markup=menu())
    with db() as con:
        con.execute("UPDATE users SET matching=0,partner_id=? WHERE user_id=? AND partner_id IS NULL",(pid,uid))
        con.execute("UPDATE users SET matching=0,partner_id=? WHERE user_id=? AND partner_id IS NULL",(uid,pid))
        a=con.execute("SELECT partner_id FROM users WHERE user_id=?",(uid,)).fetchone()
        b=con.execute("SELECT partner_id FROM users WHERE user_id=?",(pid,)).fetchone()
    if not a or not b or a["partner_id"]!=pid or b["partner_id"]!=uid:
        return await update.effective_message.reply_text("A member was matched at the same time. Tap ⏭️ Next Partner once.")
    x,y=get_user(uid),get_user(pid)
    await update.effective_message.reply_text(f"💜 PARTNER FOUND!\nCountry: {y['country'] or 'Not set'}\nAge: {y['age']}\nChat is connected. /stop ends it.",reply_markup=menu())
    await safe_send(context.bot,pid,f"💜 PARTNER FOUND!\nCountry: {x['country'] or 'Not set'}\nAge: {x['age']}\nChat is connected. /stop ends it.",reply_markup=menu())

async def access_ok(update, context):
    u=update.effective_user
    if not u: return False
    upsert(u)
    r=get_user(u.id)
    if r["banned"] and not admin(u.id):
        await update.effective_message.reply_text("Your account is restricted."); return False
    if r["age_blocked"] and not admin(u.id):
        await update.effective_message.reply_text("Access unavailable for this account."); return False
    if not r["age_confirmed"] and not admin(u.id):
        await update.effective_message.reply_text("Use /start and confirm age first."); return False
    return True

async def stop_match(update, context):
    uid=update.effective_user.id
    row=get_user(uid); partner=row["partner_id"] if row else None
    with db() as con:
        con.execute("UPDATE users SET matching=0,partner_id=NULL WHERE user_id=?",(uid,))
        if partner: con.execute("UPDATE users SET matching=0,partner_id=NULL WHERE user_id=? AND partner_id=?",(partner,uid))
    if partner: await safe_send(context.bot,partner,"Your partner ended the chat. Use /find to find someone new.")
    await update.effective_message.reply_text("Chat ended or search cancelled.",reply_markup=menu())

async def games(update, context):
    if update.effective_chat.type == ChatType.PRIVATE:
        return await update.effective_message.reply_text(
          "🎮 REAL-MEMBER GAMES\n\nOpen the bot's official group and use /truthdare there. Group games need real group members to participate.\n\nCommands: /truthdare, /riddle, /guess 5, /battle, /leaderboard.",
          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open official group",url=OFFICIAL_GROUP)]])
        )
    await update.effective_message.reply_text("🎮 Game menu — choose a live game:",
      reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🎭 Truth or Dare",callback_data="game:truthdare")],
        [InlineKeyboardButton("🧩 Riddle",callback_data="game:riddle")],
        [InlineKeyboardButton("🔢 Guess 1–10",callback_data="game:guess")],
        [InlineKeyboardButton("⚔️ Quiz battle queue",callback_data="game:battle")]
      ]))

TRUTHS = [
 "What is one skill you want to learn this year?",
 "What is a small thing that always makes your day better?",
 "What is a funny harmless misunderstanding you once had?",
 "Which fictional character would you invite to your team, and why?",
 "What is one goal you are proud of working toward?",
 "What is a kind thing someone did that you still remember?"
]
DARES = [
 "Send a kind compliment to someone in this group.",
 "Describe your day using exactly three emojis.",
 "Write a tongue twister and challenge the next player to repeat it.",
 "Share one useful study or hobby tip with the group.",
 "Invent a funny team name in 10 seconds.",
 "Say your favourite movie or game without using its title; let the group guess."
]
RIDDLES = [
 ("What has many teeth but cannot bite?","comb"),
 ("What gets wetter as it dries?","towel"),
 ("What has keys but cannot open locks?","piano"),
 ("What has a face and two hands but no arms or legs?","clock"),
 ("What goes up but never comes down?","age")
]

async def truthdare(update, context):
    chat=update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return await update.effective_message.reply_text("Use /truthdare inside your group so real group members can join.")
    await update.effective_message.reply_text("🎭 TRUTH OR DARE — real group members!\nTap a button, then complete it by replying in the group.",
      reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🟦 Truth",callback_data="td:truth"),
        InlineKeyboardButton("🟥 Dare",callback_data="td:dare")
      ]]))

async def riddle(update, context):
    chat=update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return await update.effective_message.reply_text("Use /riddle in a group to play with real members.")
    q,a=random.choice(RIDDLES)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO game_rounds(chat_id,kind,prompt,answer,started_by,started_at) VALUES(?,?,?,?,?,?)",
                    (chat.id,"riddle",q,a,update.effective_user.id,stamp()))
    await update.effective_message.reply_text(f"🧩 GROUP RIDDLE\n{q}\nFirst correct answer earns +5 points!")

async def guess(update, context):
    chat=update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return await update.effective_message.reply_text("Use /guess in a group to start a round, then /guess 5 to answer.")
    if context.args:
        try:
            attempt = int(context.args[0])
        except ValueError:
            return await update.effective_message.reply_text("Usage: /guess 5")
        with db() as con:
            roundrow = con.execute("SELECT * FROM game_rounds WHERE chat_id=? AND kind='guess'", (chat.id,)).fetchone()
            if roundrow and str(attempt) == str(roundrow["answer"]):
                con.execute("UPDATE users SET points=points+5 WHERE user_id=?", (update.effective_user.id,))
                con.execute("DELETE FROM game_rounds WHERE chat_id=?", (chat.id,))
                return await update.effective_message.reply_text(f"🎉 Correct, {update.effective_user.first_name}! +5 points.")
        return await update.effective_message.reply_text("Not correct yet — try another number from 1–10.")
    n=random.randint(1,10)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO game_rounds(chat_id,kind,prompt,answer,started_by,started_at) VALUES(?,?,?,?,?,?)",
                    (chat.id,"guess","Guess a number from 1–10",str(n),update.effective_user.id,stamp()))
    await update.effective_message.reply_text("🔢 NUMBER GUESS\\nGuess a number from 1–10. Use /guess 5 to answer. First correct guess earns +5 points.")

async def battle(update, context):
    await update.effective_message.reply_text("⚔️ Quiz battles are group-based in this build. Play /truthdare, /riddle or /guess in your group; /leaderboard shows earned points.")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    uid=q.from_user.id
    data=q.data or ""
    if data.startswith("age:"):
        upsert(q.from_user)
        if data=="age:no":
            with db() as con:
                con.execute("UPDATE users SET age_blocked=1,age_confirmed=0,profile_complete=0 WHERE user_id=?",(uid,))
            return await q.edit_message_text("Access is unavailable because this service is for adults aged 18+ only.")
        with db() as con:
            con.execute("UPDATE users SET age_confirmed=1,age_blocked=0 WHERE user_id=?",(uid,))
        return await q.edit_message_text("Age confirmed. Send /start to set up your profile.")
    if data.startswith("country:"):
        country=data.split(":",1)[1]
        context.user_data["profile_step"]="name" if country=="Other" else "name"
        context.user_data["chosen_country"]=country
        return await q.edit_message_text(f"Country selected: {country}.\nNow type the display name you want to use.")
    if data.startswith("gender:"):
        gender=data.split(":",1)[1]
        with db() as con:
            con.execute("UPDATE users SET gender=?,profile_complete=1 WHERE user_id=?",(gender,uid))
        context.user_data.pop("profile_step",None)
        context.user_data.pop("chosen_country",None)
        return await q.edit_message_text("✅ Profile saved. Country, name and gender won't be asked again unless you edit the database.")
    if data.startswith("td:"):
        kind=data.split(":",1)[1]
        text=random.choice(TRUTHS if kind=="truth" else DARES)
        await q.message.reply_text(f"🎭 {kind.upper()} for {q.from_user.first_name}:\n{text}\n\nReal members can reply below.")
        return
    if data=="game:truthdare":
        return await q.message.reply_text("🎭 Choose Truth or Dare:",reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🟦 Truth",callback_data="td:truth"),
            InlineKeyboardButton("🟥 Dare",callback_data="td:dare")]]))
    if data=="game:riddle":
        return await riddle(update,context)
    if data=="game:guess":
        return await guess(update,context)
    if data=="game:battle":
        return await q.message.reply_text("Use /battle to see the current group game options.")

async def relay(update, context):
    m=update.effective_message; u=update.effective_user; chat=update.effective_chat
    if not m or not u: return
    upsert(u)
    if chat.type != ChatType.PRIVATE:
        txt=(m.text or "").strip().lower()
        with db() as con:
            roundrow=con.execute("SELECT * FROM game_rounds WHERE chat_id=?",(chat.id,)).fetchone()
        if roundrow and txt:
            answer=roundrow["answer"]
            if answer and txt.lstrip("/").strip()==str(answer).lower():
                with db() as con:
                    con.execute("UPDATE users SET points=points+5 WHERE user_id=?",(u.id,))
                    con.execute("DELETE FROM game_rounds WHERE chat_id=?",(chat.id,))
                await m.reply_text(f"🎉 Correct, {u.first_name}! +5 points. Start another round with /riddle or /guess 5.")
        return
    if not await access_ok(update,context): return
    text=(m.text or "").strip()
    actions={
      "❤️ Find Partner":find_partner, "⏭️ Next Partner":find_partner,
      "🛑 Stop Chat":stop_match, "🎮 Games":games, "👥 Total Users":totalusers,
      "🏆 Leaderboard":leaderboard, "✨ My Points":mypoints,
      "👤 My Profile":profile, "🎁 Free VIP — 30 Days":freevip,
      "🆘 Safety / Report":help_cmd
    }
    if text in actions:
        return await actions[text](update,context)
    step=context.user_data.get("profile_step")
    if step:
        if step=="name":
            name=" ".join(text.split())[:32]
            if len(name)<2:
                return await m.reply_text("Enter a display name with at least 2 characters.")
            context.user_data["display_name"]=name
            context.user_data["profile_step"]="age"
            return await m.reply_text("🎂 Enter your age (18–100).")
        if step=="age":
            try: age=int(text)
            except ValueError: return await m.reply_text("Enter your age as a whole number.")
            if age<18:
                with db() as con:
                    con.execute("UPDATE users SET age=?,age_confirmed=0,age_blocked=1,profile_complete=0 WHERE user_id=?",(age,u.id))
                context.user_data.clear()
                return await m.reply_text("This service is for adults aged 18+ only.")
            if age>100: return await m.reply_text("Enter an age from 18 to 100.")
            with db() as con:
                con.execute("UPDATE users SET country=?,display_name=?,age=?,age_confirmed=1 WHERE user_id=?",
                    (context.user_data.get("chosen_country","Other"),context.user_data.get("display_name","Member"),age,u.id))
            context.user_data["profile_step"]="gender"
            return await m.reply_text("Choose your gender:",reply_markup=gender_keys())
    row=get_user(u.id)
    if not row or not row["profile_complete"]:
        await begin_profile(update,context); return
    partner=row["partner_id"]
    if not partner:
        return await m.reply_text("You're not in a chat. Tap ❤️ Find Partner.")
    if blocked(u.id,partner):
        return await stop_match(update,context)
    try:
        await context.bot.copy_message(chat_id=partner,from_chat_id=chat.id,message_id=m.message_id)
    except Forbidden:
        await stop_match(update,context)
    except TelegramError:
        await m.reply_text("Couldn't deliver that message. Try another message or media.")

async def profile(update, context):
    if not await access_ok(update,context): return
    r=get_user(update.effective_user.id)
    await update.effective_message.reply_text(
      f"👤 My Profile\nName: {r['display_name'] or r['first_name']}\nCountry: {r['country'] or 'Not set'}\nAge: {r['age'] or 'Not set'}\nGender: {r['gender'] or 'Not set'}\nPoints: {r['points']}\nBio: {r['bio'] or 'Not set'}")

async def mypoints(update, context):
    r=get_user(update.effective_user.id)
    await update.effective_message.reply_text(f"✨ Your points: {r['points'] if r else 0}")

async def leaderboard(update, context):
    with db() as con:
        rows=con.execute("SELECT display_name,first_name,points FROM users ORDER BY points DESC LIMIT 10").fetchall()
    text="🏆 GAME LEADERBOARD\n\n"+"\n".join(f"{i}. {r['display_name'] or r['first_name'] or 'Member'} — {r['points']} points" for i,r in enumerate(rows,1))
    await update.effective_message.reply_text(text or "No points yet. Play group games!")

async def totalusers(update, context):
    total=total_users()
    with db() as con:
        active=con.execute("SELECT COUNT(*) FROM users WHERE last_seen>=?",((now()-timedelta(days=1)).isoformat(),)).fetchone()[0]
    await update.effective_message.reply_text(f"👥 COMMUNITY STATS\nTotal registered users: {total}\nActive in last 24 hours: {active}")

async def freevip(update, context):
    uid=update.effective_user.id
    with db() as con:
        count=con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND qualified=1",(uid,)).fetchone()[0]
    me=await context.bot.get_me()
    await update.effective_message.reply_text(
      f"🎁 FREE VIP — 30 DAYS\nQualified referrals: {count}/20\nInvite link: https://t.me/{me.username}?start=ref_{uid}\n\nWhen 20 invited adults complete age confirmation and join requirements, the owner can grant 30 days with /grantfree USER_ID. Paid plans have been removed from this menu.")

async def help_cmd(update, context):
    await update.effective_message.reply_text("Commands:\n/start\n/find — find a partner\n/next — find another partner\n/stop — stop chat/search\n/games — game menu\n/truthdare — Truth or Dare in a group\n/riddle — group riddle\n/guess — group number guessing\n/totalusers — community count\n/leaderboard\n/mypoints\n/freevip — free 30-day referral info\n/profile")

async def report(update, context):
    row=get_user(update.effective_user.id)
    if not row or not row["partner_id"]:
        return await update.effective_message.reply_text("You need an active chat to report a partner.")
    reason=" ".join(context.args).strip()[:500] or "No reason provided"
    with db() as con:
        con.execute("INSERT INTO reports(reporter_id,reported_id,reason,created_at) VALUES(?,?,?,?)",
                    (update.effective_user.id,row["partner_id"],reason,stamp()))
    await update.effective_message.reply_text("Report submitted. Use /stop to end the chat.")

async def block(update, context):
    row=get_user(update.effective_user.id)
    if not row or not row["partner_id"]:
        return await update.effective_message.reply_text("No active partner to block.")
    pid=row["partner_id"]
    with db() as con:
        con.execute("INSERT OR IGNORE INTO blocks(user_id,blocked_id,created_at) VALUES(?,?,?)",(update.effective_user.id,pid,stamp()))
    await stop_match(update,context)

async def stats(update, context):
    if not admin(update.effective_user.id):
        return await update.effective_message.reply_text("Admin only.")
    with db() as con:
        active=con.execute("SELECT COUNT(*) FROM users WHERE matching=1 OR partner_id IS NOT NULL").fetchone()[0]
        banned=con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    await update.effective_message.reply_text(f"📊 Stats\nTotal users: {total_users()}\nSearching/chatting: {active}\nBanned: {banned}")

async def grantfree(update, context):
    if not admin(update.effective_user.id):
        return await update.effective_message.reply_text("Admin only.")
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /grantfree USER_ID")
    uid=int(context.args[0])
    until=(now()+timedelta(days=30)).isoformat()
    with db() as con:
        con.execute("UPDATE users SET vip_until=?,vip_lifetime=0 WHERE user_id=?",(until,uid))
    await update.effective_message.reply_text("30-day free VIP granted if that user exists.")
    await safe_send(context.bot,uid,"🎁 Your free 30-day VIP has been activated.")

async def main_error(update, context):
    log.exception("Unhandled bot error",exc_info=context.error)

def build_bot():
    app=Application.builder().token(BOT_TOKEN).build()
    for cmd,fn in [
      ("start",start),("help",help_cmd),("find",find_partner),("next",find_partner),
      ("stop",stop_match),("games",games),("play",games),("truthdare",truthdare),
      ("truth",truthdare),("dare",truthdare),("riddle",riddle),("guess",guess),
      ("battle",battle),("leaderboard",leaderboard),("mypoints",mypoints),
      ("totalusers",totalusers),("profile",profile),("freevip",freevip),
      ("report",report),("block",block),("stats",stats),("grantfree",grantfree)
    ]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,relay))
    app.add_error_handler(main_error)
    return app

init_db()
bot_app=build_bot()

@asynccontextmanager
async def lifespan(api: FastAPI):
    await bot_app.initialize()
    await bot_app.bot.set_my_commands([
      ("start","Open main menu"),("find","Find a partner"),("next","Find next partner"),
      ("stop","End chat or search"),("games","Open games menu"),
      ("truthdare","Play Truth or Dare in a group"),("riddle","Start a group riddle"),
      ("guess","Start group number guessing"),("leaderboard","Game leaderboard"),
      ("mypoints","View points"),("totalusers","Community statistics"),
      ("profile","View profile"),("freevip","Free 30-day VIP info"),("help","Help")
    ])
    await bot_app.start()
    if bot_app.updater:
        await bot_app.updater.start_polling(drop_pending_updates=False)
    try:
        yield
    finally:
        if bot_app.updater and bot_app.updater.running:
            await bot_app.updater.stop()
        if bot_app.running:
            await bot_app.stop()
        await bot_app.shutdown()

web=FastAPI(title="Find Partner VIP",version="2.0.0",lifespan=lifespan)

@web.get("/")
async def home():
    return {"ok":True,"service":"Find Partner VIP","version":"2.0.0"}

@web.get("/health")
async def health():
    return {"ok":True,"service":"Find Partner VIP"}

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:web",host="0.0.0.0",port=int(os.getenv("PORT","8000")))
