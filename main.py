import os
import asyncio
import asyncpg
from datetime import datetime, timedelta

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
waiting_for_schedule_text = False
temp_schedule_text = None


# ================== БАЗА ==================
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
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id SERIAL PRIMARY KEY,
            send_time TIMESTAMP,
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


# ================== ПРОВЕРКА ПОДПИСКИ ==================
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# ================== СТАРТ ==================
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


# ================== АДМИН ПАНЕЛЬ ==================
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("⏳ Запланировать рассылку", callback_data="schedule")],
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================== СТАТИСТИКА ==================
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


# ================== JOB ==================
async def broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    text = context.job.data
    users = await get_all_users()

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            await asyncio.sleep(0.05)
        except:
            pass


# ================== КНОПКИ ==================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global temp_schedule_text

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Проверка подписки
    if query.data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)

        if is_subscribed:
            await query.edit_message_text("✅ Ну все, тусим! 🚀")
        else:
            await query.answer("❌ Так че, тусим то будем?", show_alert=True)

    # Мгновенная рассылка
    if query.data == "broadcast" and user_id == ADMIN_ID:
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")

    # Запланировать
    if query.data == "schedule" and user_id == ADMIN_ID:
        waiting_for_schedule_text = True
        await query.message.reply_text("✍ Напиши текст для отложенной рассылки")

    # Выбор времени
    if query.data.startswith("delay_") and user_id == ADMIN_ID:
        delay_map = {
            "delay_1h": (3600, "1 час"),
            "delay_6h": (21600, "6 часов"),
            "delay_12h": (43200, "12 часов"),
            "delay_24h": (86400, "1 день"),
        }

        delay, text_time = delay_map.get(query.data)

        send_time = datetime.utcnow() + timedelta(seconds=delay)

        await db.execute("""
            INSERT INTO scheduled_broadcasts (send_time, message)
            VALUES ($1, $2)
        """, send_time, temp_schedule_text)

        context.application.job_queue.run_once(
            broadcast_job,
            delay,
            data=temp_schedule_text
        )

        formatted_time = send_time.strftime("%d.%m.%Y в %H:%M")

        await query.message.reply_text(
            f"🚀 Рассылка запланирована!\n"
            f"🕒 Отправка через {text_time}\n"
            f"📅 {formatted_time} (UTC)"
        )

        waiting_for_schedule_text = False
        temp_schedule_text = None


# ================== ОБРАБОТКА ТЕКСТА ==================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast
    global waiting_for_schedule_text
    global temp_schedule_text

    user_id = update.effective_user.id
    await save_user(user_id)

    # Мгновенная рассылка
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

    # Запланированная
    elif user_id == ADMIN_ID and waiting_for_schedule_text:
        temp_schedule_text = update.message.text

        keyboard = [
            [InlineKeyboardButton("Через 1 час", callback_data="delay_1h")],
            [InlineKeyboardButton("Через 6 часов", callback_data="delay_6h")],
            [InlineKeyboardButton("Через 12 часов", callback_data="delay_12h")],
            [InlineKeyboardButton("Через 1 день", callback_data="delay_24h")],
        ]

        await update.message.reply_text(
            "⏳ Когда отправить?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ================== ЗАПУСК ==================
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
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
