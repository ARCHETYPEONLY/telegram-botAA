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

waiting_for_broadcast = False
db = None


# ---------- ПОДКЛЮЧЕНИЕ К БАЗЕ ----------
async def init_db():
    global db
    db = await asyncpg.connect(DATABASE_URL)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY
        )
    """)


# ---------- СОХРАНЕНИЕ ПОЛЬЗОВАТЕЛЯ ----------
async def save_user(user_id):
    try:
        await db.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id
        )
    except:
        pass


# ---------- ПОЛУЧИТЬ ВСЕХ ----------
async def get_all_users():
    rows = await db.fetch("SELECT user_id FROM users")
    return [row["user_id"] for row in rows]


# ---------- ПРОВЕРКА ПОДПИСКИ ----------
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# ---------- СТАРТ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await save_user(user_id)

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ 🔥")
    else:
        keyboard = [
            [InlineKeyboardButton("Подпишись", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
            [InlineKeyboardButton("✅ Проверить", callback_data="check_sub")]
        ]
        await update.message.reply_text(
            "❌ Подпишись на канал",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------- АДМИН ----------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ---------- КНОПКИ ----------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)

        if is_subscribed:
            await query.edit_message_text("✅ Ну все, тусим!")
        else:
            await query.answer("❌ Ты не подписан", show_alert=True)

    if query.data == "broadcast" and user_id == ADMIN_ID:
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")


# ---------- ТЕКСТ ----------
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
                await context.bot.send_message(chat_id=uid, text=text)
                await asyncio.sleep(0.05)
            except:
                pass

        await update.message.reply_text("✅ Рассылка завершена")


# ---------- ЗАПУСК ----------
async def main():
    await init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
