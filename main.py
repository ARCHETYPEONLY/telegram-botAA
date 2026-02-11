import os
import asyncio
import asyncpg
from datetime import datetime

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
ADMIN_ID = 963261169  # твой id

waiting_for_broadcast = False
waiting_for_schedule_time = False
db = None


# ---------------- ПОДКЛЮЧЕНИЕ К БАЗЕ (С RETRY) ----------------
async def init_db(app):
    global db

    for i in range(10):  # 10 попыток
        try:
            db = await asyncpg.connect(
                DATABASE_URL,
                ssl="require"
            )
            print("✅ Database connected")
            break
        except Exception as e:
            print(f"DB connection failed... retry {i+1}/10")
            await asyncio.sleep(3)
    else:
        raise Exception("❌ Could not connect to database")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            joined_at TIMESTAMP DEFAULT NOW()
        )
    """)


# ---------------- БАЗА ----------------
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


# ---------------- СТАРТ ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await save_user(user_id)

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ 🔥")
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


# ---------------- АДМИН ПАНЕЛЬ ----------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("⏰ Запланировать рассылку", callback_data="schedule")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
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
        f"👥 Всего пользователей: {total}\n"
        f"🆕 Новых за 24 часа: {new_24h}"
    )


# ---------------- КНОПКИ ----------------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_time

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)

        if is_subscribed:
            await query.edit_message_text("✅ Ну все, тусим! 🚀")
        else:
            await query.answer("❌ Так че, тусим то будем?", show_alert=True)

    if query.data == "broadcast" and user_id == ADMIN_ID:
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")

    if query.data == "schedule" and user_id == ADMIN_ID:
        waiting_for_schedule_time = True
        await query.message.reply_text(
            "🕒 Введи дату и время по МСК в формате:\n\n"
            "11.02.2026 17:52"
        )


# ---------------- ОБРАБОТКА ТЕКСТА ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_time

    user_id = update.effective_user.id
    await save_user(user_id)

    # Обычная рассылка
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

    # Планирование
    elif user_id == ADMIN_ID and waiting_for_schedule_time:
        try:
            dt = datetime.strptime(update.message.text, "%d.%m.%Y %H:%M")
            waiting_for_schedule_time = False

            delay = (dt - datetime.now()).total_seconds()

            if delay <= 0:
                await update.message.reply_text("❌ Время уже прошло")
                return

            await update.message.reply_text(
                f"✅ Рассылка запланирована на {update.message.text} (МСК)"
            )

            async def scheduled_broadcast():
                await asyncio.sleep(delay)
                users = await get_all_users()
                for uid in users:
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text="📢 Запланированная рассылка"
                        )
                        await asyncio.sleep(0.05)
                    except:
                        pass

            asyncio.create_task(scheduled_broadcast())

        except:
            await update.message.reply_text("❌ Неправильный формат")


# ---------------- ЗАПУСК ----------------
app = ApplicationBuilder().token(TOKEN).post_init(init_db).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
