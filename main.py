import os
import asyncio
import asyncpg
import logging
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

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
RAILWAY_URL = os.getenv("RAILWAY_STATIC_URL")

ADMIN_ID = 963261169
CHANNEL_USERNAME = "@username_твоего_канала"  # <-- ЗАМЕНИ

# ================= GLOBALS =================

db_pool = None
scheduled_jobs = {}

waiting_for_broadcast = False
waiting_for_schedule_text = False
waiting_for_schedule_time = False
scheduled_content = None

waiting_for_name = {}

# ================= LOGGING =================

# Настройка логирования
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ================= DATABASE =================

async def init_db(app):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    logger.debug("✅ Database connected")

    async with db_pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS username TEXT
        """)

        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS full_name TEXT
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
        if row["send_time"] > datetime.utcnow():
            job = app.job_queue.run_once(
                send_scheduled_broadcast,
                when=row["send_time"],
                data=dict(row),
                name=str(row["id"])
            )
            scheduled_jobs[row["id"]] = job

async def save_user(user):
    logger.debug(f"Saving user: {user.id}")
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

# ================= SUB CHECK =================

async def check_subscription(user_id, context):
    try:
        logger.debug(f"Checking subscription for user {user_id}.")
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return False

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await save_user(user)

    logger.debug(f"User {user.id} started the bot.")

    waiting_for_name[user.id] = True

    await update.message.reply_text(
        "Привет!\n\n"
        "Для того чтобы попасть в список, напиши фамилию и имя 👇"
    )

# ================= ADMIN =================

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

    logger.debug(f"Button pressed: {query.data} by {query.from_user.id}")

    if query.data == "check_sub":
        is_subscribed = await check_subscription(query.from_user.id, context)

        if not is_subscribed:
            await query.answer("❌ Ты ещё не подписан", show_alert=True)
            return

        waiting_for_name.pop(query.from_user.id, None)

        await query.message.edit_text(
            "🔥 Ты в списке!\n\n"
            "Вся информация будет в канале 😉"
        )
        return

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "broadcast":
        waiting_for_broadcast = True
        await query.message.reply_text("Отправь контент для рассылки")

    elif query.data == "schedule":
        waiting_for_schedule_text = True
        await query.message.reply_text("Отправь контент для планирования")

# ================= MESSAGE HANDLER =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast, waiting_for_schedule_text
    global waiting_for_schedule_time, scheduled_content

    user = update.effective_user
    message = update.message

    await save_user(user)

    # ===== РЕГИСТРАЦИЯ =====
    if user.id in waiting_for_name:
        full_name = message.text.strip()

        if len(full_name.split()) < 2:
            await message.reply_text("❌ Введи фамилию и имя")
            return

        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET full_name=$1
                WHERE user_id=$2
            """, full_name, user.id)

        keyboard = [
            [InlineKeyboardButton(
                "📢 Подписаться",
                url=f"https://t.me/{CHANNEL_USERNAME.replace('@','')}"
            )],
            [InlineKeyboardButton(
                "✅ Проверить подписку",
                callback_data="check_sub"
            )]
        ]

        await message.reply_text(
            "Ура! ты практически в списке 🎉\n\n"
            "Подпишись на канал и нажми проверить 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return

    # ===== АДМИН =====
    if user.id == ADMIN_ID:

        if waiting_for_broadcast:
            waiting_for_broadcast = False
            users = await get_all_users()

            for uid in users:
                try:
                    await context.bot.copy_message(
                        uid,
                        message.chat.id,
                        message.message_id
                    )
                    await asyncio.sleep(0.05)
                except:
                    pass

            await message.reply_text("✅ Рассылка завершена")
            return

        return

    # ===== ПЕРЕСЫЛКА АДМИНУ =====
    await context.bot.send_message(
        ADMIN_ID,
        f"📩 Новое сообщение\nID: {user.id}\nUsername: @{user.username}"
    )

    await context.bot.forward_message(
        ADMIN_ID,
        update.effective_chat.id,
        message.message_id
    )

# ================= STATS =================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")

    await update.message.reply_text(
        f"📊 Пользователей: {users}"
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
