import os
import asyncio
import asyncpg
from datetime import datetime, timezone

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

ADMIN_ID = 963261169  # твой Telegram ID

db_pool = None
scheduled_jobs = {}

waiting_for_broadcast = False
waiting_for_schedule_text = False
waiting_for_schedule_time = False

scheduled_content = {}


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
                text TEXT,
                media_type TEXT,
                file_id TEXT,
                send_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled'
            )
        """)

        # автомиграция если колонок нет
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='scheduled_messages'
                    AND column_name='media_type'
                ) THEN
                    ALTER TABLE scheduled_messages ADD COLUMN media_type TEXT;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='scheduled_messages'
                    AND column_name='file_id'
                ) THEN
                    ALTER TABLE scheduled_messages ADD COLUMN file_id TEXT;
                END IF;
            END
            $$;
        """)

    await restore_jobs(app)


async def restore_jobs(app):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, media_type, file_id, send_time
            FROM scheduled_messages
            WHERE status='scheduled'
        """)

    for row in rows:
        send_time = row["send_time"].replace(tzinfo=timezone.utc)

        if send_time > datetime.now(timezone.utc):
            job = app.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data=dict(row),
                name=str(row["id"])
            )
            scheduled_jobs[row["id"]] = job


# ================= USERS =================

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
        return [r["user_id"] for r in rows]


# ================= USER =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user.id)
    await update.message.reply_text("🚀 Бот работает")


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать", callback_data="schedule")],
        [InlineKeyboardButton("📋 Список", callback_data="list")],
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= BUTTONS =================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "broadcast":
        waiting_for_broadcast = True
        await query.message.reply_text(
            "Отправь текст / фото / видео / GIF для рассылки"
        )

    elif query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text(
            "Отправь текст / фото / видео / GIF для запланированной рассылки"
        )

    elif query.data == "list":
        await show_schedules(query)

    elif query.data.startswith("delete_"):
        message_id = int(query.data.split("_")[1])

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM scheduled_messages WHERE id=$1",
                message_id
            )

        job = scheduled_jobs.get(message_id)
        if job:
            job.schedule_removal()
            scheduled_jobs.pop(message_id, None)

        await query.message.edit_text("❌ Удалено")


# ================= LIST =================

async def show_schedules(query):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, send_time
            FROM scheduled_messages
            WHERE status='scheduled'
            ORDER BY send_time
        """)

    if not rows:
        await query.message.reply_text("📭 Нет запланированных")
        return

    for row in rows:
        keyboard = [[
            InlineKeyboardButton(
                "❌ Удалить",
                callback_data=f"delete_{row['id']}"
            )
        ]]

        await query.message.reply_text(
            f"🆔 ID: {row['id']}\n"
            f"🕒 {row['send_time'].strftime('%d.%m.%Y %H:%M')}\n"
            f"✉ {row['text'] or ''}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ================= HANDLE MESSAGE =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global waiting_for_schedule_time
    global scheduled_content

    user_id = update.effective_user.id
    await save_user(user_id)

    if user_id != ADMIN_ID:
        return

    message = update.message

    media_type = None
    file_id = None
    text = message.text

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        text = message.caption

    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        text = message.caption

    elif message.animation:
        media_type = "gif"
        file_id = message.animation.file_id
        text = message.caption

    # ===== МГНОВЕННАЯ РАССЫЛКА =====
    if waiting_for_broadcast:
        waiting_for_broadcast = False
        await send_to_all(context, text, media_type, file_id)
        await message.reply_text("✅ Рассылка завершена")
        return

    # ===== СОХРАНЕНИЕ КОНТЕНТА =====
    if waiting_for_schedule_text:
        scheduled_content = {
            "text": text,
            "media_type": media_type,
            "file_id": file_id
        }
        waiting_for_schedule_text = False
        waiting_for_schedule_time = True
        await message.reply_text("🕒 Введи дату: 11.02.2026 19:30")
        return

    # ===== ПЛАНИРОВАНИЕ =====
    if waiting_for_schedule_time:
        try:
            send_time = datetime.strptime(
                message.text.strip(),
                "%d.%m.%Y %H:%M"
            )

            waiting_for_schedule_time = False

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO scheduled_messages
                    (text, media_type, file_id, send_time)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                """,
                    scheduled_content["text"],
                    scheduled_content["media_type"],
                    scheduled_content["file_id"],
                    send_time
                )

            message_id = row["id"]

            job = context.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time.replace(tzinfo=timezone.utc),
                data={
                    "id": message_id,
                    **scheduled_content
                },
                name=str(message_id)
            )

            scheduled_jobs[message_id] = job

            await message.reply_text("✅ Запланировано")

        except:
            await message.reply_text("❌ Неверный формат")


# ================= SEND =================

async def send_to_all(context, text, media_type, file_id):
    users = await get_all_users()

    for uid in users:
        try:
            if media_type == "photo":
                await context.bot.send_photo(uid, file_id, caption=text)

            elif media_type == "video":
                await context.bot.send_video(uid, file_id, caption=text)

            elif media_type == "gif":
                await context.bot.send_animation(uid, file_id, caption=text)

            else:
                await context.bot.send_message(uid, text)

            await asyncio.sleep(0.05)

        except:
            pass


async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data

    await send_to_all(
        context,
        data.get("text"),
        data.get("media_type"),
        data.get("file_id")
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE scheduled_messages SET status='sent' WHERE id=$1",
            data["id"]
        )

    scheduled_jobs.pop(data["id"], None)


# ================= STATS =================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        scheduled = await conn.fetchval(
            "SELECT COUNT(*) FROM scheduled_messages WHERE status='scheduled'"
        )
        sent = await conn.fetchval(
            "SELECT COUNT(*) FROM scheduled_messages WHERE status='sent'"
        )

    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {users}\n"
        f"🕒 Запланировано: {scheduled}\n"
        f"✅ Отправлено: {sent}"
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
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(
    filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION,
    handle_message
))

print("🚀 Bot started")
app.run_polling()
