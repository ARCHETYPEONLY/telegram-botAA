import os
import asyncio
import asyncpg
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
RAILWAY_URL = os.getenv("RAILWAY_STATIC_URL")

ADMIN_ID = 963261169  # твой ID
LOCAL_TZ = ZoneInfo("Europe/Moscow")  # поменяй если другой часовой пояс

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
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # если колонка username отсутствует — добавим
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS username TEXT
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
            INSERT INTO users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username
        """, user.id, user.username)


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
        [InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать", callback_data="schedule")],
        [InlineKeyboardButton("📋 Список", callback_data="list")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= BUTTONS =================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast, waiting_for_schedule_text

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "broadcast":
        waiting_for_broadcast = True
        await query.message.reply_text("Отправь текст / фото / видео / гиф")

    elif query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text("Отправь контент для планирования")

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
            SELECT * FROM scheduled_messages
            WHERE status='scheduled'
            ORDER BY send_time
        """)

    if not rows:
        await query.message.reply_text("📭 Нет рассылок")
        return

    for row in rows:
        local_time = row["send_time"].replace(
            tzinfo=timezone.utc
        ).astimezone(LOCAL_TZ)

        keyboard = [[
            InlineKeyboardButton(
                "❌ Удалить",
                callback_data=f"delete_{row['id']}"
            )
        ]]

        await query.message.reply_text(
            f"🆔 ID: {row['id']}\n"
            f"🕒 {local_time.strftime('%d.%m.%Y %H:%M')}\n"
            f"📦 Тип: {row['file_type'] or 'text'}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ================= MESSAGE HANDLER =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global waiting_for_schedule_time
    global scheduled_content

    user = update.effective_user
    message = update.message

    await save_user(user)

    # ===== ADMIN =====
    if user.id == ADMIN_ID:

        # ответ пользователю через reply
        if message.reply_to_message:
            text = message.reply_to_message.text

            if text and "ID:" in text:
                try:
                    target_id = int(text.split("ID:")[1].split("\n")[0])
                    await context.bot.send_message(
                        target_id,
                        message.text
                    )
                    await message.reply_text("✅ Ответ отправлен")
                except Exception as e:
                    await message.reply_text(f"❌ Ошибка: {e}")
                return

        if waiting_for_broadcast:
            waiting_for_broadcast = False
            await broadcast_content(context, message)
            await message.reply_text("✅ Рассылка завершена")
            return

        if waiting_for_schedule_text:
            scheduled_content = extract_content(message)
            waiting_for_schedule_text = False
            waiting_for_schedule_time = True
            await message.reply_text("🕒 Введи дату: 11.02.2026 21:40")
            return

        if waiting_for_schedule_time:
            try:
                local_dt = datetime.strptime(
                    message.text.strip(),
                    "%d.%m.%Y %H:%M"
                ).replace(tzinfo=LOCAL_TZ)

                utc_dt = local_dt.astimezone(timezone.utc)

                if utc_dt <= datetime.now(timezone.utc):
                    await message.reply_text("❌ Нельзя в прошлое")
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
                        utc_dt.replace(tzinfo=None)
                    )

                job = context.job_queue.run_once(
                    send_scheduled_broadcast,
                    when=utc_dt,
                    data=dict(row),
                    name=str(row["id"])
                )

                scheduled_jobs[row["id"]] = job

                await message.reply_text(
                    f"✅ Запланировано на {local_dt.strftime('%d.%m.%Y %H:%M')}"
                )

            except Exception as e:
                await message.reply_text(f"❌ Ошибка:\n{e}")

            return

        return

    # ===== USER → ADMIN =====
    await context.bot.send_message(
        ADMIN_ID,
        f"📩 Новое сообщение\nID: {user.id}\nUsername: @{user.username}"
    )

    await context.bot.forward_message(
        ADMIN_ID,
        update.effective_chat.id,
        message.message_id
    )


# ================= CONTENT =================

def extract_content(message):
    if message.photo:
        return {"text": message.caption, "file_id": message.photo[-1].file_id, "file_type": "photo"}
    elif message.video:
        return {"text": message.caption, "file_id": message.video.file_id, "file_type": "video"}
    elif message.animation:
        return {"text": message.caption, "file_id": message.animation.file_id, "file_type": "animation"}
    else:
        return {"text": message.text, "file_id": None, "file_type": "text"}


async def send_content(context, user_id, content):
    if content["file_type"] == "photo":
        await context.bot.send_photo(user_id, content["file_id"], caption=content["text"])
    elif content["file_type"] == "video":
        await context.bot.send_video(user_id, content["file_id"], caption=content["text"])
    elif content["file_type"] == "animation":
        await context.bot.send_animation(user_id, content["file_id"], caption=content["text"])
    else:
        await context.bot.send_message(user_id, content["text"])


async def broadcast_content(context, message):
    users = await get_all_users()
    content = extract_content(message)

    for uid in users:
        try:
            await send_content(context, uid, content)
            await asyncio.sleep(0.05)
        except:
            pass


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

    scheduled_jobs.pop(data["id"], None)


# ================= STATS =================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        scheduled = await conn.fetchval("SELECT COUNT(*) FROM scheduled_messages WHERE status='scheduled'")
        sent = await conn.fetchval("SELECT COUNT(*) FROM scheduled_messages WHERE status='sent'")

    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {users}\n"
        f"🕒 Запланировано: {scheduled}\n"
        f"✅ Отправлено: {sent}"
    )


# ================= APP INIT =================

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
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

print("🚀 Bot started (webhook mode)")

PORT = int(os.environ.get("PORT", 8000))

WEBHOOK_PATH = "webhook"
WEBHOOK_URL = f"https://{RAILWAY_URL}/{WEBHOOK_PATH}"

app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    url_path=WEBHOOK_PATH,
    webhook_url=WEBHOOK_URL,
)
