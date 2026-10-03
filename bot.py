import asyncio, logging, os, sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")   # https://your-app.up.railway.app
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
DB = "bot.db"

logging.basicConfig(level=logging.INFO)

# ---------- DATABASE ----------
def init_db():
    c = sqlite3.connect(DB).cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        balance REAL DEFAULT 1000, staked REAL DEFAULT 0,
        referrals INTEGER DEFAULT 0, referred_by INTEGER, joined_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stakes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        plan TEXT, apy REAL, start_ts TEXT, unlock_ts TEXT, active INTEGER DEFAULT 1)""")
    c.connection.commit()

def q(sql, args=(), fetch=None):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(sql, args)
    conn.commit()
    r = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
    conn.close()
    return r

def get_user(uid):
    return q("SELECT * FROM users WHERE user_id=?", (uid,), "one")

def create_user(uid, uname, fname, ref=None):
    q("INSERT OR IGNORE INTO users(user_id,username,first_name,referred_by,joined_at) VALUES(?,?,?,?,?)",
      (uid, uname, fname, ref, datetime.utcnow().isoformat()))
    if ref:
        q("UPDATE users SET referrals=referrals+1, balance=balance+100 WHERE user_id=?", (ref,))

def create_stake(uid, amount, plan, apy, days):
    now = datetime.utcnow()
    q("INSERT INTO stakes(user_id,amount,plan,apy,start_ts,unlock_ts) VALUES(?,?,?,?,?,?)",
      (uid, amount, plan, apy, now.isoformat(), (now + timedelta(days=days)).isoformat()))
    q("UPDATE users SET balance=balance-?, staked=staked+? WHERE user_id=?", (amount, amount, uid))

def active_stakes(uid):
    return q("SELECT id,amount,plan,apy,unlock_ts FROM stakes WHERE user_id=? AND active=1", (uid,), "all")

def claim_stake(sid, uid):
    row = q("SELECT amount,apy,unlock_ts FROM stakes WHERE id=? AND user_id=? AND active=1", (sid, uid), "one")
    if not row: return None
    amount, apy, unlock = row
    if datetime.fromisoformat(unlock) > datetime.utcnow(): return "locked"
    total = amount * (1 + apy / 100)
    q("UPDATE stakes SET active=0 WHERE id=?", (sid,))
    q("UPDATE users SET balance=balance+?, staked=staked-? WHERE user_id=?", (total, amount, uid))
    return total

# ---------- KEYBOARDS ----------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Balance", callback_data="balance"),
         InlineKeyboardButton(text="📊 Stake", callback_data="stake")],
        [InlineKeyboardButton(text="🎁 Referral", callback_data="referral"),
         InlineKeyboardButton(text="ℹ️ About", callback_data="about")],
        [InlineKeyboardButton(text="📈 My Stakes", callback_data="my_stakes")],
    ])

def plans_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥉 Bronze — 30d @ 5%", callback_data="plan_bronze")],
        [InlineKeyboardButton(text="🥈 Silver — 60d @ 12%", callback_data="plan_silver")],
        [InlineKeyboardButton(text="🥇 Gold — 90d @ 25%", callback_data="plan_gold")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="menu")],
    ])

PLANS = {
    "bronze": ("Bronze", 5.0, 30, 100),
    "silver": ("Silver", 12.0, 60, 500),
    "gold":   ("Gold",   25.0, 90, 1000),
}

# ---------- HANDLERS ----------
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message, bot: Bot):
    ref = None
    parts = m.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try: ref = int(parts[1][4:])
        except ValueError: pass
    create_user(m.from_user.id, m.from_user.username, m.from_user.first_name, ref)
    await m.answer(
        f"👋 Welcome to <b>VIP StakeBot</b>, {m.from_user.first_name}!\n\n"
        "🚀 Your all-in-one staking simulator on Telegram.\n\n"
        "✅ 1,000 free VIP Credits to start\n"
        "✅ Earn simulated APY up to 25%\n"
        "✅ 100 credits per referral\n\n"
        "Choose an option below:",
        reply_markup=main_menu())

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("🏠 <b>Main Menu</b>", reply_markup=main_menu())
    await c.answer()

@dp.callback_query(F.data == "balance")
async def cb_balance(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        f"💰 <b>Your Wallet</b>\n\n"
        f"Balance: <b>{u[3]:.2f}</b>\n"
        f"Staked:  <b>{u[4]:.2f}</b>\n"
        f"Referrals: <b>{u[5]}</b>",
        reply_markup=main_menu())
    await c.answer()

@dp.callback_query(F.data == "stake")
async def cb_stake(c: CallbackQuery):
    await c.message.edit_text(
        "📊 <b>Staking Plans</b>\n\n"
        "• Bronze — 30 days @ 5% APY\n"
        "• Silver — 60 days @ 12% APY\n"
        "• Gold — 90 days @ 25% APY",
        reply_markup=plans_kb())
    await c.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def cb_plan(c: CallbackQuery):
    key = c.data[5:]
    name, apy, days, min_amt = PLANS[key]
    u = get_user(c.from_user.id)
    if u[3] < min_amt:
        await c.answer(f"❌ Need {min_amt} credits. Invite friends to earn more!", show_alert=True)
        return
    amount = min(u[3], min_amt)
    create_stake(c.from_user.id, amount, name, apy, days)
    await c.message.edit_text(
        f"✅ <b>Stake Created</b>\n\nPlan: {name}\nAmount: {amount:.2f}\nAPY: {apy}%\nDuration: {days} days",
        reply_markup=main_menu())
    await c.answer("Stake created!")

@dp.callback_query(F.data == "my_stakes")
async def cb_my(c: CallbackQuery):
    rows = active_stakes(c.from_user.id)
    if not rows:
        await c.message.edit_text("📭 No active stakes.", reply_markup=main_menu())
        await c.answer(); return
    kb = []
    for sid, amount, plan, apy, unlock in rows:
        ready = "✅" if datetime.fromisoformat(unlock) <= datetime.utcnow() else "⏳"
        kb.append([InlineKeyboardButton(text=f"{ready} {plan} | {amount:.0f} | Claim", callback_data=f"claim_{sid}")])
    kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu")])
    await c.message.edit_text("📈 <b>Your Stakes</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()

@dp.callback_query(F.data.startswith("claim_"))
async def cb_claim(c: CallbackQuery):
    sid = int(c.data[6:])
    r = claim_stake(sid, c.from_user.id)
    if r is None: await c.answer("Not found.", show_alert=True)
    elif r == "locked": await c.answer("⏳ Not unlocked yet.", show_alert=True)
    else:
        await c.answer(f"✅ Claimed {r:.2f} credits!", show_alert=True)
        await cb_my(c)

@dp.callback_query(F.data == "referral")
async def cb_ref(c: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{c.from_user.id}"
    u = get_user(c.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Share", url=f"https://t.me/share/url?url={link}&text=Join%20VIP%20StakeBot!")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="menu")],
    ])
    await c.message.edit_text(
        f"🎁 <b>Referral Program</b>\n\nEarn <b>100 credits</b> per friend!\n\n"
        f"<code>{link}</code>\n\nTotal: <b>{u[5]}</b>", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "about")
async def cb_about(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>About VIP StakeBot</b>\n\n"
        "A staking simulator on Telegram — no real money.\n"
        "• 1,000 free starter credits\n"
        "• 3 staking tiers\n"
        "• Referral rewards\n"
        "• 24/7 uptime", reply_markup=main_menu())
    await c.answer()

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("/start — Menu\n/balance — Credits\n/stake — Plans\n/help — This")

@dp.message(Command("balance"))
async def bal_cmd(m: Message):
    u = get_user(m.from_user.id)
    await m.answer(f"💰 Balance: <b>{u[3]:.2f}</b> | Staked: <b>{u[4]:.2f}</b>")

@dp.message(Command("stake"))
async def stake_cmd(m: Message):
    await m.answer("📊 Choose a plan:", reply_markup=plans_kb())

# ---------- STARTUP ----------
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Main menu"),
        BotCommand(command="balance", description="Check credits"),
        BotCommand(command="stake", description="Staking plans"),
        BotCommand(command="help", description="Help"),
    ])
    logging.info(f"Webhook set: {WEBHOOK_URL}")

async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.startup.register(on_startup)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
