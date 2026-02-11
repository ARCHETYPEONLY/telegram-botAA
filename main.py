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
waiting_for_funnel = False


# ---------------- БАЗА ----------------
async def init_db(app):
    global db
    db = await asyncpg.connect(DATABASE_URL)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            joined_at TIMESTAMP DEFAULT NOW()
        )
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

    await update.message.reply_text("Добро пожаловать 🔥")
    asyncio.create_task(start_funnel(user_id, context))


# ---------------- АДМИН ПАНЕЛЬ ----------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("➕ Добавить шаг", callback_data="add_step")],
        [InlineKeyboardButton("📋 Показать шаги", callback_data="show_steps")],
        [InlineKeyboardButton("❌ Очистить воронку", callback_data="clear_funnel")],
    ]

    await update.message.reply_text(
        "⚙ Управление автоворонкой",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ---------------- КНОПКИ ----------------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_funnel

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "add_step":
        waiting_for_funnel = True
        await query.message.reply_text(
            "Отправь шаг в формате:\n\n"
            "номер_шага | задержка_в_секундах | текст\n\n"
            "Пример:\n"
            "1 | 10 | Привет, это первое сообщение"
        )

    elif query.data == "show_steps":
        rows = await db.fetch("""
            SELECT step_number, delay_seconds, message
            FROM funnel_steps
            ORDER BY step_number
        """)

        if not rows:
            await query.message.reply_text("Воронка пустая")
            return

        text = "📋 Текущие шаги:\n\n"
        for row in rows:
            text += f"Шаг {row['step_number']} | {row['delay_seconds']} сек\n{row['message']}\n\n"

        await query.message.reply_text(text)

    elif query.data == "clear_funnel":
        await db.execute("DELETE FROM funnel_steps")
        await query.message.reply_text("❌ Воронка очищена")


# ---------------- ОБРАБОТКА ТЕКСТА ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_funnel

    user_id = update.effective_user.id

    if user_id == ADMIN_ID and waiting_for_funnel:
        try:
            step, delay, message = update.message.text.split("|", 2)

            step = int(step.strip())
            delay = int(delay.strip())
            message = message.strip()

            await db.execute("""
                INSERT INTO funnel_steps (step_number, delay_seconds, message)
                VALUES ($1, $2, $3)
            """, step, delay, message)

            await update.message.reply_text("✅ Шаг добавлен")
            waiting_for_funnel = False

        except:
            await update.message.reply_text("❌ Неверный формат. Попробуй ещё раз.")
        return


# ---------------- ЗАПУСК ----------------
app = ApplicationBuilder().token(TOKEN).post_init(init_db).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
