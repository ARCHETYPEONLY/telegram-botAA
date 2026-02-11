import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@ECLIPSEPARTY1"

# Проверка подписки
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        print("STATUS:", member.status)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        print("ERROR:", e)
        return False

# Команда старт
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    is_subscribed = await check_subscription(user_id, context)

    if is_subscribed:
        await update.message.reply_text("ТЫ В БАНДЕ 🔥")
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
        await query.edit_message_text("✅ СПАСИБО ЧТО ВСТУПИЛ, ДАВАЙ ТУСИТЬ! 🚀")
    else:
        await query.answer("❌ ТЫ НЕ ПОДПИСАН!", show_alert=True)

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button))

print("Bot started")
app.run_polling()
