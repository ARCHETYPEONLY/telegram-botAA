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

SUPER_ADMIN = 963261169  # 👑 главный админ (никогда не удаляется)

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

        # Пользователи
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Админы
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Запланированные рассылки
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                send_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled'
            )
        """)

        # Автомиграция status
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='scheduled_messages'
                    AND column_name='status'
                ) THEN
                    ALTER TABLE scheduled_messages
                    ADD COLUMN status TEXT DEFAULT 'scheduled';
                END IF;
            END
            $$;
        """)

        # Добавляем главного админа если его нет
        await conn.execute("""
            INSERT INTO admins (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, SUPER_ADMIN)

    await restore_jobs(app)


# ================= ADMIN CHECK =================

async def is_admin(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        admin = await conn.fetchval(
            "SELECT user_id FROM admins WHERE user_id=$1",
            user_id
        )
        return admin is not None


async def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN


# ================= RESTORE JOBS =================

async def restore_jobs(app):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, send_time
            FROM scheduled_messages
            WHERE status='scheduled'
        """)

    for row in rows:
        send_time = row["send_time"].replace(tzinfo=timezone.utc)

        if send_time > datetime.now(timezone.utc):
            job = app.job_queue.run_once(
                send_scheduled_broadcast,
                when=send_time,
                data={
                    "text": row["text"],
                    "id": row["id"]
                },
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
        return [row["user_id"] for row in rows]


# ================= USER =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user.id)
    await update.message.reply_text("🚀 Бот работает")


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
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


# ================= ADMIN MANAGEMENT =================

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_super_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только главный админ может добавлять админов")
        return

    if not context.args:
        await update.message.reply_text("Используй: /addadmin 123456789")
        return

    try:
        new_admin = int(context.args[0])

        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO admins (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
            """, new_admin)

        await update.message.reply_text(f"✅ Админ {new_admin} добавлен")

    except:
        await update.message.reply_text("❌ Неверный ID")


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_super_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только главный админ может удалять админов")
        return

    if not context.args:
        await update.message.reply_text("Используй: /removeadmin 123456789")
        return

    try:
        admin_id = int(context.args[0])

        if admin_id == SUPER_ADMIN:
            await update.message.reply_text("🚫 Нельзя удалить главного админа")
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM admins WHERE user_id=$1",
                admin_id
            )

        await update.message.reply_text(f"❌ Админ {admin_id} удалён")

    except:
        await update.message.reply_text("❌ Неверный ID")


# ================= STATS =================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        scheduled_count = await conn.fetchval(
            "SELECT COUNT(*) FROM scheduled_messages WHERE status='scheduled'"
        )
        sent_count = await conn.fetchval(
            "SELECT COUNT(*) FROM scheduled_messages WHERE status='sent'"
        )
        admins_count = await conn.fetchval("SELECT COUNT(*) FROM admins")

    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"👮 Админов: {admins_count}\n"
        f"🕒 Запланировано: {scheduled_count}\n"
        f"✅ Отправлено: {sent_count}"
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
app.add_handler(CommandHandler("addadmin", add_admin))
app.add_handler(CommandHandler("removeadmin", remove_admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("🚀 Bot started")
app.run_polling()
