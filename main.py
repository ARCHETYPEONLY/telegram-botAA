import os
import asyncio
import asyncpg
from datetime import datetime
from zoneinfo import ZoneInfo

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
        [InlineKeyboardButton("📢 Мгновенная рассылка", callback_data="broadcast")],
        [InlineKeyboardButton("⏳ Запланировать рассылку", callback_data="schedule")]
    ]

    await update.message.reply_text(
        "⚙ Админ панель",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ---------------- JOB ДЛЯ РАССЫЛКИ ----------------
async def broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    users = await get_all_users()
    text = context.job.data

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            await asyncio.sleep(0.05)
        except:
            pass


# ---------------- КНОПКИ ----------------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast, waiting_for_schedule_text

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
        await query.message.reply_text("✍ Напиши текст для мгновенной рассылки")

    if query.data == "schedule" and user_id == ADMIN_ID:
        waiting_for_schedule_text = True
        await query.message.reply_text("✍ Напиши текст для запланированной рассылки")


# ---------------- ОБРАБОТКА СООБЩЕНИЙ ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast, waiting_for_schedule_text, temp_schedule_text

    user_id = update.effective_user.id
    await save_user(user_id)

    # МГНОВЕННАЯ РАССЫЛКА
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
        return

    # ВВОД ТЕКСТА ДЛЯ ПЛАНИРОВАНИЯ
    if user_id == ADMIN_ID and waiting_for_schedule_text:
        temp_schedule_text = update.message.text
        waiting_for_schedule_text = False

        await update.message.reply_text(
            "📅 Введи дату и время по МСК\n\n"
            "Формат: 25.02.2026 18:30"
        )

        context.user_data["waiting_for_datetime"] = True
        return

    # ВВОД ДАТЫ
    if user_id == ADMIN_ID and context.user_data.get("waiting_for_datetime"):
        context.user_data["waiting_for_datetime"] = False

        try:
            user_input = update.message.text

            msk = ZoneInfo("Europe/Moscow")
            utc = ZoneInfo("UTC")

            send_time_msk = datetime.strptime(user_input, "%d.%m.%Y %H:%M")
            send_time_msk = send_time_msk.replace(tzinfo=msk)

            send_time_utc = send_time_msk.astimezone(utc)
            delay = (send_time_utc - datetime.now(utc)).total_seconds()

            if delay <= 0:
                await update.message.reply_text("❌ Это время уже прошло")
                return

            await db.execute("""
                INSERT INTO scheduled_broadcasts (send_time, message)
                VALUES ($1, $2)
            """, send_time_utc, temp_schedule_text)

            context.application.job_queue.run_once(
                broadcast_job,
                delay,
                data=temp_schedule_text
            )

            await update.message.reply_text(
                f"🚀 Рассылка запланирована!\n"
                f"📅 {send_time_msk.strftime('%d.%m.%Y')}\n"
                f"🕒 {send_time_msk.strftime('%H:%M')} МСК"
            )

        except:
            await update.message.reply_text("❌ Неверный формат даты")


# ---------------- ЗАПУСК ----------------
app = ApplicationBuilder().token(TOKEN).post_init(init_db).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()

