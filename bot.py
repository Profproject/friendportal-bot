import os
import sqlite3
import logging
import time
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiocryptopay import AioCryptoPay, Networks

from locales import LOCALES
DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

# ================= CONFIG =================
DB = "database.db"

ACTIVATION_PRICE = 1.0
REF_VISIT_BONUS = 0.125
REF_PAID_LVL1 = 0.5
REF_PAID_LVL2 = 0.25
MIN_WITHDRAW = 5.0

waiting_for_withdraw = {}

# ================= TEXT =================
def t(user, key):
    lang = user.language_code or "en"
    if lang not in LOCALES:
        lang = "en"
    return LOCALES[lang].get(key, LOCALES["en"].get(key, key))

def t_by_id(user_id, key):
    with db() as con:
        r = con.execute(
            "SELECT language_code FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    lang = r[0] if r and r[0] else "en"
    if lang not in LOCALES:
        lang = "en"
    return LOCALES[lang].get(key, LOCALES["en"].get(key, key))

# ================= ENV =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
crypto = AioCryptoPay(CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

# ================= DB =================
def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            ref_id INTEGER,
            activated INTEGER DEFAULT 0,
            balance REAL DEFAULT 0,
            last_invoice_id INTEGER,
            ref_bonus INTEGER DEFAULT 0,
            language_code TEXT,
            created_at INTEGER
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS withdraws (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            address TEXT,
            memo TEXT,
            status TEXT DEFAULT 'pending'
        )
        """)

def add_user(uid, ref_id=None, language_code='en'):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO users
        (user_id, ref_id, language_code, created_at)
        VALUES (?, ?, ?, strftime('%s','now'))
        """,
        (uid, ref_id, language_code)
    )
    conn.commit()
    conn.close()

def get_balance(uid):
    with db() as con:
        r = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else 0

def add_balance(uid, amount):
    with db() as con:
        con.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, uid)
        )

def is_active(uid):
    with db() as con:
        r = con.execute("SELECT activated FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0])

def activate(uid):
    with db() as con:
        con.execute("UPDATE users SET activated=1 WHERE user_id=?", (uid,))

def get_ref(uid):
    with db() as con:
        r = con.execute("SELECT ref_id FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else None

def save_invoice(uid, inv_id):
    with db() as con:
        con.execute(
            "UPDATE users SET last_invoice_id=? WHERE user_id=?",
            (inv_id, uid)
        )

def last_invoice(uid):
    with db() as con:
        r = con.execute(
            "SELECT last_invoice_id FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()
    return r[0] if r else None

# ================= KEYBOARDS =================
def menu_kb(user):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(t(user, "unlock"), callback_data="unlock"))
    kb.add(InlineKeyboardButton(t(user, "invite"), callback_data="invite"))
    kb.add(InlineKeyboardButton(t(user, "balance"), callback_data="balance"))
    kb.add(InlineKeyboardButton(t(user, "withdraw"), callback_data="withdraw"))
    kb.add(InlineKeyboardButton(t(user, "stats"), callback_data="stats"))
    kb.add(InlineKeyboardButton(t(user, "how_it_works_button"), callback_data="how_it_works"))
    return kb

# ================= START =================
@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    ref = int(msg.get_args()) if msg.get_args().isdigit() else None
    uid = msg.from_user.id
    is_new = add_user(uid, ref, msg.from_user.language_code)


    await bot.send_photo(
        msg.chat.id,
        InputFile("start.jpg"),
        caption=f"<b>{t(msg.from_user,'start_title')}</b>\n\n{t(msg.from_user,'start_text')}",
        reply_markup=menu_kb(msg.from_user)
    )

# ================= UNLOCK =================
@dp.callback_query_handler(lambda c: c.data == "unlock")
async def unlock(call: types.CallbackQuery):
    inv = await crypto.create_invoice(
        asset="TON",
        amount=ACTIVATION_PRICE,
        payload=str(call.from_user.id)
    )
    save_invoice(call.from_user.id, inv.invoice_id)

    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton(t(call.from_user,"pay_button"), url=inv.bot_invoice_url),
        InlineKeyboardButton(t(call.from_user,"paid_button"), callback_data="check")
    )
    await call.message.answer(t(call.from_user,"pay_title"), reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "check")
async def check(call: types.CallbackQuery):
    uid = call.from_user.id
    inv_id = last_invoice(uid)
    if not inv_id:
        await call.answer(t(call.from_user,"payment_not_received"), show_alert=True)
        return

    invoices = await crypto.get_invoices(invoice_ids=[inv_id])
    if not invoices or invoices[0].status != "paid":
        await call.answer(t(call.from_user,"payment_not_received"), show_alert=True)
        return

    if not is_active(uid):
        activate(uid)
        ref1 = get_ref(uid)
        if ref1:
            add_balance(ref1, REF_PAID_LVL1)
            ref2 = get_ref(ref1)
            if ref2:
                add_balance(ref2, REF_PAID_LVL2)

    await call.message.answer(t(call.from_user,"access"), reply_markup=menu_kb(call.from_user))
    await call.answer()

# ================= MENU =================
@dp.callback_query_handler(lambda c: c.data == "balance")
async def balance(call: types.CallbackQuery):
    await call.message.answer(
        f"💰 <b>{get_balance(call.from_user.id):.2f} TON</b>"
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "invite")
async def invite(call: types.CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={call.from_user.id}"
    await call.message.answer(link)
    await call.answer()

# ================= STATS =================
@dp.callback_query_handler(lambda c: c.data == "stats")
async def stats(call: types.CallbackQuery):
    uid = call.from_user.id
    with db() as con:
        cur = con.cursor()
        total = cur.execute(
            "SELECT COUNT(*) FROM users WHERE ref_id=?",(uid,)
        ).fetchone()[0]
        active = cur.execute(
            "SELECT COUNT(*) FROM users WHERE ref_id=? AND activated=1",(uid,)
        ).fetchone()[0]
        second = cur.execute("""
            SELECT COUNT(*) FROM users
            WHERE ref_id IN (SELECT user_id FROM users WHERE ref_id=?)
            AND activated=1
        """,(uid,)).fetchone()[0]
        earned = get_balance(uid)

    text = t(call.from_user,"stats_text").format(
        total=total,
        active=active,
        second=second,
        earned=f"{earned:.2f}"
    )
    await call.message.answer(f"<b>{t(call.from_user,'stats_title')}</b>\n\n{text}")
    await call.answer()

# ================= HOW IT WORKS =================
@dp.callback_query_handler(lambda c: c.data == "how_it_works")
async def how_it_works(call: types.CallbackQuery):
    await bot.send_photo(
        call.message.chat.id,
        InputFile("how_it_works.jpg"),
        caption=f"<b>{t(call.from_user,'how_it_works_title')}</b>\n\n{t(call.from_user,'how_it_works_text')}"
    )
    await call.answer()

# ================= WITHDRAW =================
@dp.callback_query_handler(lambda c: c.data == "withdraw")
async def withdraw(call: types.CallbackQuery):
    uid = call.from_user.id
    bal = get_balance(uid)

    if bal < MIN_WITHDRAW:
        await call.answer(
            t(call.from_user,"min_withdraw"),
            show_alert=True
        )
        return

    if not is_active(uid):
        await call.answer(
            t(call.from_user,"withdraw_not_activated"),
            show_alert=True
        )
        return

    waiting_for_withdraw[uid] = bal
    await call.message.answer(t(call.from_user,"withdraw_request_text"))
    await call.answer()

@dp.message_handler()
async def handle_withdraw(msg: types.Message):
    uid = msg.from_user.id
    if uid not in waiting_for_withdraw:
        return

    amount = waiting_for_withdraw.pop(uid)
    parts = msg.text.strip().split(maxsplit=1)
    address = parts[0]
    memo = parts[1] if len(parts) > 1 else None

    with db() as con:
        con.execute(
            "INSERT INTO withdraws (user_id, amount, address, memo) VALUES (?,?,?,?)",
            (uid, amount, address, memo)
        )
        con.execute(
            "UPDATE users SET balance=0 WHERE user_id=?",
            (uid,)
        )

    await msg.answer(
        f"✅ <b>Withdraw request accepted</b>\n\n"
        f"💰 {amount} TON\n"
        f"📮 <code>{address}</code>\n"
        f"{'📝 '+memo if memo else ''}"
    )

    await bot.send_message(
        ADMIN_ID,
        f"🆕 Withdraw\n👤 {uid}\n💰 {amount} TON\n📮 {address}\n{memo or ''}"
    )

# ================= RUN =================
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)


