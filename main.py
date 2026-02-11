import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает 24/7 🚀")

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))

print("Bot started")
app.run_polling()

import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@ECLIPSEPARTY1"  # <-- замени на свой канал

# Проверка подписки
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# Команда старт
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ")
    else:
        keyboard = [
            [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "❌ Я ХОЧУ УБЕДИТЬСЯ ЧТО ТЫ ИДЕШЬ",
            reply_markup=reply_markup
        )

# Проверка по кнопке
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await query.edit_message_text("✅ СПАСИБО ЧТО ВСТУПИЛ, ДАВАЙ ТУСИТЬ!🚀")
    else:
        await query.answer("❌ТЫ НЕ ХОЧЕШЬ ПРИНЯТЬ УЧАСТИЕ В ТАКОЙ ТУСОВКЕ?", show_alert=True)

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button))

print("Bot started")
app.run_polling()
