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

ADMIN_ID = 963261169

db_pool = None
scheduled_jobs = {}

waiting_for_broadcast = False
waiting_for_schedule_text = False
waiting_for_schedule_time = False
scheduled_content = None


# ================= DATABASE =================

async def init_db(app):
    global db_pool

    db_pool = await asyncpg.create_pool(DATABASE_URL)
    print("✅ Database connected")

    async with db_pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                text TEXT,
                file_id TEXT,
                file_type TEXT,
                send_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled'
            )
        """)

        # добавляем колонку file_type если её нет
        await conn.execute("""
            ALTER TABLE scheduled_messages
            ADD COLUMN IF NOT EXISTS file_type TEXT
        """)

    await restore_jobs(app)


async def restore_jobs(app):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM scheduled_messages
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


async def save_user(user):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
        """, user.id, user.username, user.first_name)


async def get_all_users():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]


# ================= USER =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user)
    await update.message.reply_text("🚀 Бот работает")


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать", callback_data="schedule")],
        [InlineKeyboardButton("📋 Список", callback_data="list")]
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
            "Отправь текст / фото / видео / гиф для рассылки"
        )

    elif query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text(
            "Отправь контент для запланированной рассылки"
        )

    elif query.data == "list":
        await show_schedules(query)


# ================= MESSAGES =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global waiting_for_schedule_time
    global scheduled_content

    user = update.effective_user
    message = update.message

    await save_user(user)

    # ===== ЕСЛИ ПИШЕТ ПОЛЬЗОВАТЕЛЬ =====
    if user.id != ADMIN_ID:
        try:
            info = (
                f"📩 Новое сообщение\n\n"
                f"👤 ID: {user.id}\n"
                f"📛 Username: @{user.username}\n"
            )

            await context.bot.send_message(ADMIN_ID, info)

            forwarded = await context.bot.forward_message(
                chat_id=ADMIN_ID,
                from_chat_id=update.effective_chat.id,
                message_id=message.message_id
            )

            context.bot_data[forwarded.message_id] = user.id

        except:
            pass
        return

    # ===== ОТВЕТ РЕПЛАЕМ =====
    if user.id == ADMIN_ID and message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id

        if replied_msg_id in context.bot_data:
            target_user_id = context.bot_data[replied_msg_id]

            try:
                await context.bot.send_message(
                    target_user_id,
                    message.text
                )
                await message.reply_text("✅ Ответ отправлен")
            except:
                await message.reply_text("❌ Ошибка отправки")

        return

    # ===== BROADCAST =====

    if waiting_for_broadcast:
        waiting_for_broadcast = False
        await broadcast_content(context, message)
        await message.reply_text("✅ Рассылка завершена")
        return

    # ===== SCHEDULE STEP 1 =====

    if waiting_for_schedule_text:
        scheduled_content = extract_content(message)
        waiting_for_schedule_text = False
        waiting_for_schedule_time = True
        await message.reply_text("🕒 Введи дату: 11.02.2026 21:40")
        return

    # ===== SCHEDULE STEP 2 =====

    if waiting_for_schedule_time:
        try:
            send_time = datetime.strptime(
                message.text.strip(),
                "%d.%m.%Y %H:%M"
            )

            send_time = send_time.replace(tzinfo=timezone.utc)

            if send_time <= datetime.now(timezone.utc):
                await message.reply_text("❌ Время уже прошло")
                return

            waiting_for_schedule_time = False

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO scheduled_messages
                    (text, file_id, file_type, send_time)
                    VALUES ($1, $2, $3, $4)
                    RETURNING *
                """,
                scheduled_content["text"],
                scheduled_content["file_id"],
                scheduled_content["file_type"],
                send_time)

            job = context.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data=dict(row),
                name=str(row["id"])
            )

            scheduled_jobs[row["id"]] = job

            await message.reply_text("✅ Запланировано")

        except Exception as e:
            await message.reply_text(f"❌ Ошибка: {e}")


# ================= CONTENT =================

def extract_content(message):
    if message.photo:
        return {
            "text": message.caption,
            "file_id": message.photo[-1].file_id,
            "file_type": "photo"
        }
    elif message.video:
        return {
            "text": message.caption,
            "file_id": message.video.file_id,
            "file_type": "video"
        }
    elif message.animation:
        return {
            "text": message.caption,
            "file_id": message.animation.file_id,
            "file_type": "animation"
        }
    else:
        return {
            "text": message.text,
            "file_id": None,
            "file_type": "text"
        }


async def broadcast_content(context, message):
    users = await get_all_users()
    content = extract_content(message)

    for uid in users:
        try:
            await send_content(context, uid, content)
            await asyncio.sleep(0.05)
        except:
            pass


async def send_content(context, user_id, content):
    if content["file_type"] == "photo":
        await context.bot.send_photo(user_id, content["file_id"], caption=content["text"])

    elif content["file_type"] == "video":
        await context.bot.send_video(user_id, content["file_id"], caption=content["text"])

    elif content["file_type"] == "animation":
        await context.bot.send_animation(user_id, content["file_id"], caption=content["text"])

    else:
        await context.bot.send_message(user_id, content["text"])


async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    users = await get_all_users()

    for uid in users:
        try:
            await send_content(context, uid, data)
            await asyncio.sleep(0.05)
        except:
            pass

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE scheduled_messages SET status='sent' WHERE id=$1",
            data["id"]
        )


# ================= WEBHOOK RUN =================

app = (
    ApplicationBuilder()
    .token(TOKEN)
    .post_init(init_db)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

print("🚀 Bot started (webhook mode)")

PORT = int(os.environ.get("PORT", 8000))
RAILWAY_URL = os.getenv("RAILWAY_STATIC_URL")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{RAILWAY_URL}{WEBHOOK_PATH}"

app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    webhook_url=WEBHOOK_URL,
    url_path="webhook",
)
