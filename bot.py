import json
import os
import logging
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- FLASK WEB SERVER (To satisfy Hugging Face health checks) ---
server = Flask(__name__)

@server.route('/')
def health_check():
    return "Bot is alive!"

def run_flask():
    # Hugging Face strictly requires port 7860
    server.run(host="0.0.0.0", port=7860)

# --- BOT LOGIC ---
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
DB_FILE = "bot_data.json"
pending_logs = {}

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: return json.load(f)
        except: return {}
    return {}

db = load_db()

def save_db():
    with open(DB_FILE, "w") as f: json.dump(db, f)

def init_user(user_id):
    uid = str(user_id)
    if uid not in db:
        db[uid] = {f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)}
        save_db()
    return uid

async def start(update, context):
    uid = init_user(update.message.chat_id)
    now = datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    delay = (next_hour - now).total_seconds()
    
    context.job_queue.run_repeating(hourly_ping, interval=3600, first=delay, chat_id=update.message.chat_id, name=uid)
    await update.message.reply_text("🚀 Bot started on Hugging Face!\n/log - Log hour\n/export - Get table")

async def hourly_ping(context):
    now = datetime.now()
    curr, prev = now.strftime("%H:00"), (now - timedelta(hours=1)).strftime("%H:00")
    keyboard = [[InlineKeyboardButton(f"Log {prev}", callback_data=prev)], [InlineKeyboardButton(f"Log {curr}", callback_data=curr)]]
    await context.bot.send_message(context.job.chat_id, text="⏰ Time to log!", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_grid(update, context):
    init_user(update.message.chat_id)
    keyboard = [[InlineKeyboardButton(f"{i:02d}:00", callback_data=f"{i:02d}:00") for i in range(j, j+4)] for j in range(0, 24, 4)]
    await update.message.reply_text("Select hour:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    pending_logs[str(query.message.chat_id)] = query.data
    await query.edit_message_text(f"✍️ Logging for {query.data}\nSend 3 lines (Actual, Plan, Notes):")

async def handle_message(update, context):
    uid = str(update.message.chat_id)
    if uid not in pending_logs: return
    hour = pending_logs.pop(uid)
    lines = update.message.text.split('\n')
    db[uid][hour] = {
        "actual": lines[0] if len(lines) > 0 else "",
        "plan": lines[1] if len(lines) > 1 else "",
        "notes": lines[2] if len(lines) > 2 else ""
    }
    save_db()
    await update.message.reply_text(f"✅ Saved {hour}")

async def export(update, context):
    uid = init_user(update.message.chat_id)
    table = "| time | plan | actual | notes |\n| :--- | :--- | :--- | :--- |\n"
    for i in range(24):
        h = f"{i:02d}:00"
        d = db[uid][h]
        table += f"| {h} | {d['plan']} | {d['actual']} | {d['notes']} |\n"
    await update.message.reply_text(f"```markdown\n{table}\n```", parse_mode="MarkdownV2")

if __name__ == "__main__":
    # Start Flask heartbeat
    Thread(target=run_flask, daemon=True).start()
    
    # Get token from Secrets
    token = os.environ.get("TELEGRAM_TOKEN")
    app = Application.builder().token(token).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("log", show_grid))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("clear", CommandHandler("clear", lambda u, c: (db.update({str(u.message.chat_id): {f"{i:02d}:00": {"plan":"","actual":"","notes":""} for i in range(24)}}), save_db()))))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()
