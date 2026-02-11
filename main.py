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

ADMIN_ID = 963261169  # ТВОЙ ID

db_pool = None

waiting_for_broadcast = False
waiting_for_schedule_text = False
waiting_for_schedule_time = False
scheduled_text = None


# ================= DATABASE =================

async def init_db(app):
    global db_pool

    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден")

    db_pool = await asyncpg.create_pool(DATABASE_URL)
    print("✅ Database connected")

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                text TEXT,
                send_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

    await restore_scheduled_jobs(app)


async def restore_scheduled_jobs(app):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM scheduled_messages")

    for row in rows:
        schedule_id = row["id"]
        text = row["text"]
        send_time = row["send_time"]

        if send_time > datetime.now(send_time.tzinfo):
            app.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data={"id": schedule_id, "text": text},
                name=str(schedule_id)
            )


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


# ================= USER =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user.id)
    await update.message.reply_text("🚀 Бот работает")


# ================= ADMIN =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать рассылку", callback_data="schedule")],
        [InlineKeyboardButton("📋 Список рассылок", callback_data="list_schedules")]
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

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "broadcast":
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")

    elif query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text("✍ Напиши текст для запланированной рассылки")

    elif query.data == "list_schedules":

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, send_time FROM scheduled_messages
                ORDER BY send_time
            """)

        if not rows:
            await query.message.reply_text("📭 Нет запланированных рассылок")
            return

        for row in rows:
            schedule_id = row["id"]
            send_time = row["send_time"]

            keyboard = [[
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data=f"cancel_{schedule_id}"
                )
            ]]

            await query.message.reply_text(
                f"ID: {schedule_id}\nВремя: {send_time}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif query.data.startswith("cancel_"):

        schedule_id = int(query.data.split("_")[1])

        jobs = context.job_queue.get_jobs_by_name(str(schedule_id))
        for job in jobs:
            job.schedule_removal()

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM scheduled_messages WHERE id=$1",
                schedule_id
            )

        await query.message.reply_text(f"❌ Рассылка {schedule_id} отменена")


# ================= MESSAGES =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global waiting_for_schedule_time
    global scheduled_text

    user_id = update.effective_user.id
    await save_user(user_id)

    # Обычная рассылка
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

    # Шаг 1 текст
    if user_id == ADMIN_ID and waiting_for_schedule_text:
        scheduled_text = update.message.text
        waiting_for_schedule_text = False
        waiting_for_schedule_time = True

        await update.message.reply_text(
            "🕒 Введи дату и время по МСК:\n\n"
            "11.02.2026 17:52"
        )
        return

    # Шаг 2 дата
    if user_id == ADMIN_ID and waiting_for_schedule_time:
        try:
            moscow = pytz.timezone("Europe/Moscow")
            clean_input = update.message.text.strip()
            send_time = datetime.strptime(clean_input, "%d.%m.%Y %H:%M")
            send_time = moscow.localize(send_time)

            waiting_for_schedule_time = False

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO scheduled_messages (text, send_time)
                    VALUES ($1, $2)
                    RETURNING id
                """, scheduled_text, send_time)

            schedule_id = row["id"]

            context.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data={"id": schedule_id, "text": scheduled_text},
                name=str(schedule_id)
            )

            await update.message.reply_text(
                f"✅ Рассылка запланирована\nID: {schedule_id}"
            )

        except Exception as e:
            print("SCHEDULE ERROR:", e)
            await update.message.reply_text("❌ Неправильный формат")


# ================= SEND SCHEDULED =================

async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    schedule_id = data["id"]
    text = data["text"]

    users = await get_all_users()

    for uid in users:
        try:
            await context.bot.send_message(uid, text)
            await asyncio.sleep(0.05)
        except:
            pass

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM scheduled_messages WHERE id=$1",
            schedule_id
        )


# ================= RUN =================

app = (
    ApplicationBuilder()
    .token(TOKEN)
    .post_init(init_db)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
