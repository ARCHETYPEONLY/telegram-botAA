import os
import asyncio
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
CHANNEL_USERNAME = "@ECLIPSEPARTY1"

# 🔴 ВСТАВЬ СВОЙ TELEGRAM ID
ADMIN_ID = 123456789  

users = set()
waiting_for_broadcast = False

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
    users.add(user_id)

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ 🔥")
    else:
        keyboard = [
            [InlineKeyboardButton("Подпишись уже, мы же там инфу кидаем))",
                                  url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
            [InlineKeyboardButton("✅ Давай проверим", callback_data="check_sub")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "❌ Давай подписывайся, я все вижу)",
            reply_markup=reply_markup
        )

# ---------------- АДМИН ПАНЕЛЬ ----------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != 963261169:
        return

    keyboard = [
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="broadcast")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("⚙ Админ панель", reply_markup=reply_markup)

# ---------------- ОБРАБОТКА КНОПОК ----------------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast

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

    # Рассылка
    if query.data == "broadcast" and user_id == 963261169:
        waiting_for_broadcast = True
        await query.message.reply_text("✍ Напиши текст для рассылки")

# ---------------- ОБРАБОТКА ТЕКСТА ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_broadcast

    user_id = update.effective_user.id
    users.add(user_id)

    if user_id == ADMIN_ID and waiting_for_broadcast:
        waiting_for_broadcast = False

        text = update.message.text
        await update.message.reply_text("📢 Начинаю рассылку...")

        for uid in users:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                await asyncio.sleep(0.05)
            except:
                pass

        await update.message.reply_text("✅ Рассылка завершена")

# ---------------- ЗАПУСК ----------------
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot started")
app.run_polling()
