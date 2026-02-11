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

# ⚡ Главный админ (никогда не теряется)
MAIN_ADMIN_ID = 963261169

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

        # USERS
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ADMINS
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            )
        """)

        # SCHEDULED
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                send_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled'
            )
        """)

        # автомиграция status
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

        # гарантируем что главный админ всегда есть
        await conn.execute("""
            INSERT INTO admins (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, MAIN_ADMIN_ID)

    await restore_jobs(app)


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
                data={"text": row["text"], "id": row["id"]},
                name=str(row["id"])
            )
            scheduled_jobs[row["id"]] = job


# ================= ADMIN CHECK =================

async def is_admin(user_id: int):
    if user_id == MAIN_ADMIN_ID:
        return True

    async with db_pool.acquire() as conn:
        admin = await conn.fetchrow(
            "SELECT 1 FROM admins WHERE user_id=$1",
            user_id
        )
        return admin is not None


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


# ================= USER COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_user(update.effective_user.id)
    await update.message.reply_text("🚀 Бот работает")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_status = await is_admin(update.effective_user.id)
    await update.message.reply_text(
        f"Ваш ID: {update.effective_user.id}\n"
        f"Админ: {admin_status}"
    )


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("🕒 Запланировать рассылку", callback_data="schedule")],
        [InlineKeyboardButton("📋 Список рассылок", callback_data="list")],
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MAIN_ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("Используй: /addadmin ID")
        return

    new_admin = int(context.args[0])

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO admins (user_id)
            VALUES ($1)
            ON CONFLICT DO NOTHING
        """, new_admin)

    await update.message.reply_text(f"✅ Админ {new_admin} добавлен")


# ================= BUTTONS =================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
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

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM scheduled_messages WHERE id=$1",
                message_id
            )

        job = scheduled_jobs.get(message_id)
        if job:
            job.schedule_removal()
            scheduled_jobs.pop(message_id, None)

        await query.message.edit_text("❌ Рассылка удалена")


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
        await query.message.reply_text("📭 Нет запланированных рассылок")
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
            f"✉ {row['text'][:40]}",
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

    if not await is_admin(user_id):
        return

    if waiting_for_broadcast:
        waiting_for_broadcast = False
        text = update.message.text
        users = await get_all_users()

        await update.message.reply_text("📢 Рассылка...")

        for uid in users:
            try:
                await context.bot.send_message(uid, text)
                await asyncio.sleep(0.05)
            except:
                pass

        await update.message.reply_text("✅ Готово")
        return

    if waiting_for_schedule_text:
        scheduled_text = update.message.text
        waiting_for_schedule_text = False
        waiting_for_schedule_time = True
        await update.message.reply_text("🕒 Введи дату: 11.02.2026 19:30")
        return

    if waiting_for_schedule_time:
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
                when=send_time.replace(tzinfo=timezone.utc),
                data={"text": scheduled_text, "id": message_id},
                name=str(message_id)
            )

            scheduled_jobs[message_id] = job

            await update.message.reply_text("✅ Запланировано")

        except:
            await update.message.reply_text("❌ Неверный формат")


# ================= SEND =================

async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    users = await get_all_users()

    for uid in users:
        try:
            await context.bot.send_message(uid, data["text"])
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
    if not await is_admin(update.effective_user.id):
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
app.add_handler(CommandHandler("whoami", whoami))
app.add_handler(CommandHandler("addadmin", add_admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("🚀 Bot started")
app.run_polling()
