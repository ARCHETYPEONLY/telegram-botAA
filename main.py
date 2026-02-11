import os
import asyncio
import asyncpg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_USERNAME = "@ECLIPSEPARTY1"
ADMIN_ID = 963261169

db = None
waiting_for_broadcast = False


# ---------------- БАЗА ----------------
async def init_db(app):
    global db
    db = await asyncpg.connect(DATABASE_URL)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            joined_at TIMESTAMP DEFAULT NOW(),
            funnel_step INTEGER DEFAULT 0,
            funnel_active BOOLEAN DEFAULT TRUE
        )
    """)

    await db.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT NOW()
    """)

    await db.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS funnel_step INTEGER DEFAULT 0
    """)

    await db.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS funnel_active BOOLEAN DEFAULT TRUE
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS funnel_steps (
            id SERIAL PRIMARY KEY,
            step_number INTEGER,
            delay_seconds INTEGER,
            message TEXT
        )
    """)


async def save_user(user_id):
    await db.execute("""
        INSERT INTO users (user_id)
        VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
    """, user_id)


async def get_all_users():
    rows = await db.fetch("SELECT user_id FROM users")
    return [row["user_id"] for row in rows]


async def get_users_count():
    row = await db.fetchrow("SELECT COUNT(*) FROM users")
    return row["count"]


async def get_new_users_24h():
    row = await db.fetchrow("""
        SELECT COUNT(*) FROM users
        WHERE joined_at >= NOW() - INTERVAL '24 HOURS'
    """)
    return row["count"]


# ---------------- ПРОВЕРКА ПОДПИСКИ ----------------
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# ---------------- АВТОВОРОНКА ----------------
async def start_funnel(user_id, context):
    rows = await db.fetch("""
        SELECT step_number, delay_seconds, message
        FROM funnel_steps
        ORDER BY step_number
    """)

    for row in rows:
        await asyncio.sleep(row["delay_seconds"])
        try:
            await context.bot.send_message(user_id, row["message"])
        except:
            pass


# ---------------- СТАРТ ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await save_user(user_id)

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ 🔥")
        asyncio.create_task(start_funnel(user_id, context))
    else:
        keyboard = [
            [InlineKeyboardButton(
                "Подпишись уже, мы же там инфу кидаем))",
                url=f"https://t.me/{CHANNEL_USERNAME[1:]}"
            )],
            [InlineKeyboardButton("✅ Давай проверим", callback_data="check_sub")]
        ]

        await update.message.reply_text(
            "❌ Давай подписывайся, я все вижу)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------------- СТАТИСТИКА ----------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    total = await get_users_count()
    new_24h = await get_new_users_24h()

    await update.message.reply_text(
        f"📊 Статистика:\n\n"
        f"👥 Всего: {total}\n"
        f"🆕 Новых за 24 часа: {new_24h}"
    )


# ---------------- РАССЫЛКА ----------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast

    query = update.callback_query
    await query.answer()

    if query.data == "check_sub":
        is_subscribed = await check_subscription(query.from_user.id, context)

        if is_subscribed:
            await query.edit_message_text("✅ Ну все, тусим! 🚀")
            asyncio.create_task(start_funnel(query.from_user.id, context))
        else:
            await query.answer("❌ Так че, тусим то будем?", show_alert=True)

    if query.data == "broadcast" and query.from_user.id == ADMIN_ID:
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast

    user_id = update.effective_user.id
    await save_user(user_id)

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


# ---------------- ЗАПУСК ----------------
app = ApplicationBuilder().token(TOKEN).post_init(init_db).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
