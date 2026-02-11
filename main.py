import os
import asyncio
import asyncpg
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

ADMIN_ID = 963261169

db_pool = None
scheduled_jobs = {}

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

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                send_time TIMESTAMP NOT NULL
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
        [InlineKeyboardButton("📋 Список рассылок", callback_data="list")]
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
        await query.message.reply_text(
            "✍ Напиши текст для запланированной рассылки"
        )

    elif query.data == "list":
        await show_schedules(query)

    elif query.data.startswith("delete_"):
        message_id = int(query.data.split("_")[1])

        # Удаляем из БД
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM scheduled_messages WHERE id = $1",
                message_id
            )

        # Останавливаем job
        job = scheduled_jobs.get(message_id)
        if job:
            job.schedule_removal()
            scheduled_jobs.pop(message_id, None)

        await query.message.edit_text(
            f"❌ Рассылка ID {message_id} удалена"
        )


# ================= SHOW LIST =================

async def show_schedules(query):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, send_time
            FROM scheduled_messages
            ORDER BY send_time
        """)

    if not rows:
        await query.message.reply_text("📭 Нет запланированных рассылок")
        return

    for row in rows:
        keyboard = [
            [InlineKeyboardButton(
                "❌ Удалить",
                callback_data=f"delete_{row['id']}"
            )]
        ]

        preview = row["text"][:40]

        await query.message.reply_text(
            f"🆔 ID: {row['id']}\n"
            f"🕒 {row['send_time']}\n"
            f"✉ {preview}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


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
            "🕒 Введи дату и время:\n\n"
            "11.02.2026 19:30"
        )
        return

    # ШАГ 2 — дата
    if user_id == ADMIN_ID and waiting_for_schedule_time:
        try:
            send_time = datetime.strptime(
                update.message.text.strip(),
                "%d.%m.%Y %H:%M"
            )

            waiting_for_schedule_time = False

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO scheduled_messages (text, send_time)
                    VALUES ($1, $2)
                    RETURNING id
                """, scheduled_text, send_time)

            message_id = row["id"]

            job = context.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data={
                    "text": scheduled_text,
                    "id": message_id
                },
                name=str(message_id)
            )

            scheduled_jobs[message_id] = job

            await update.message.reply_text(
                f"✅ Запланировано на {update.message.text}"
            )

        except Exception as e:
            print("SCHEDULE ERROR:", e)
            await update.message.reply_text(
                "❌ Неправильный формат.\nПример: 11.02.2026 19:30"
            )


# ================= SEND =================

async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    text = data["text"]
    message_id = data["id"]

    users = await get_all_users()

    for uid in users:
        try:
            await context.bot.send_message(uid, text)
            await asyncio.sleep(0.05)
        except:
            pass

    # Удаляем из БД
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM scheduled_messages WHERE id = $1",
            message_id
        )

    scheduled_jobs.pop(message_id, None)


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

print("🚀 Bot started")
app.run_polling()
