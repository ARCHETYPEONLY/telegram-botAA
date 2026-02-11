import os
import asyncio
import asyncpg
import pytz
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 123456789  # ← ВСТАВЬ СВОЙ TELEGRAM ID

db_pool = None

waiting_for_broadcast = False
waiting_for_schedule_text = False
waiting_for_schedule_time = False
scheduled_text = None


# ================= DATABASE =================

async def init_db(app):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    print("✅ Database connected")

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)


async def save_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)


async def get_all_users():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [row["user_id"] for row in rows]


# ================= USER COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user.id)
    await update.message.reply_text("🚀 Бот работает")


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать рассылку", callback_data="schedule")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id != ADMIN_ID:
        return

    if query.data == "broadcast":
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")

    if query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text("✍ Напиши текст для запланированной рассылки")


# ================= MESSAGES =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global waiting_for_schedule_time
    global scheduled_text

    user_id = update.effective_user.id
    await save_user(user_id)

    # ОБЫЧНАЯ РАССЫЛКА
    if user_id == ADMIN_ID and waiting_for_broadcast:
        waiting_for_broadcast = False
        text = update.message.text
        users = await get_all_users()

        await update.message.reply_text("📢 Начинаю рассылку...")

        for uid in users:
            try:
                await context.bot.send_message(uid, text)
                await asyncio.sleep(0.05)
            except:
                pass

        await update.message.reply_text("✅ Рассылка завершена")
        return

    # ШАГ 1 — текст
    if user_id == ADMIN_ID and waiting_for_schedule_text:
        scheduled_text = update.message.text
        waiting_for_schedule_text = False
        waiting_for_schedule_time = True

        await update.message.reply_text(
            "🕒 Введи дату и время по МСК в формате:\n\n"
            "11.02.2026 17:52"
        )
        return

    # ШАГ 2 — дата
    if user_id == ADMIN_ID and waiting_for_schedule_time:
        try:
            moscow = pytz.timezone("Europe/Moscow")
            send_time = datetime.strptime(update.message.text, "%d.%m.%Y %H:%M")
            send_time = moscow.localize(send_time)

            waiting_for_schedule_time = False

            context.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data=scheduled_text
            )

            await update.message.reply_text(
                f"✅ Рассылка будет отправлена {update.message.text} (МСК)"
            )

        except:
            await update.message.reply_text(
                "❌ Неправильный формат.\nПример: 11.02.2026 17:52"
            )


# ================= SCHEDULED SEND =================

async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    text = context.job.data
    users = await get_all_users()

    for uid in users:
        try:
            await context.bot.send_message(uid, text)
            await asyncio.sleep(0.05)
        except:
            pass


# ================= RUN =================

app = ApplicationBuilder().token(TOKEN).post_init(init_db).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
