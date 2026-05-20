import json
import os
import logging
import pytz
from datetime import datetime, time, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# Enable logging
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

DB_FILE = "bot_data.json"
IST = pytz.timezone('Asia/Kolkata')

# --- Persistent State Management ---
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: 
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

db = load_db()

def save_db():
    with open(DB_FILE, "w") as f: 
        json.dump(db, f)

def init_user(uid):
    uid = str(uid)
    if uid not in db:
        db[uid] = {"logs": {}, "msg_map": {}, "last_ping": None}
        save_db()
    elif "logs" not in db[uid]:
        logging.info(f"Upgrading user {uid} from old JSON schema to V3...")
        old_data = db[uid].copy()
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        db[uid] = {"logs": {today_str: old_data}, "msg_map": {}, "last_ping": None}
        save_db()
    return uid

def init_day(uid, date_str):
    if date_str not in db[uid]["logs"]:
        db[uid]["logs"][date_str] = {f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)}
        save_db()

def cleanup_old_data(uid, now_ist):
    keys_to_delete = []
    for d_str in db[uid]["logs"]:
        d_date = IST.localize(datetime.strptime(d_str, "%Y-%m-%d"))
        if (now_ist - d_date).days > 3:
            keys_to_delete.append(d_str)
    for k in keys_to_delete:
        del db[uid]["logs"][k]
    save_db()

async def send_table_for_date(context_or_bot, chat_id, uid, date_str, header_text):
    table = "| time  | plan    | actual  | notes |\n| :---- | :------ | :------ | :---- |\n"
    data = db[uid]["logs"][date_str]
    for i in range(24):
        h = f"{i:02d}:00"
        row = data.get(h, {"plan": "", "actual": "", "notes": ""})
        table += f"| {h} | {row.get('plan', '')} | {row.get('actual', '')} | {row.get('notes', '')} |\n"
    
    bot = context_or_bot.bot if hasattr(context_or_bot, 'bot') else context_or_bot
    await bot.send_message(
        chat_id, 
        text=f"""{header_text}\n\n```markdown\n{table}\n
```""", 
        parse_mode="MarkdownV2"
    )

# --- THE FIX: Restore Jobs on Startup ---
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Initialize the bot and automation"),
        BotCommand("log", "Manually overwrite a specific hour"),
        BotCommand("export", "Get the current state of the table"),
        BotCommand("clear", "Wipe today's data to start fresh"),
        BotCommand("help", "Show instructions and formatting")
    ]
    await application.bot.set_my_commands(commands)
    logging.info("Command menu synced with Telegram!")

    # Loop through saved users in your database and restart their clocks
    for uid_str in db.keys():
        chat_id = int(uid_str)
        
        # Restore midnight report
        application.job_queue.run_daily(
            send_midnight_report, 
            time=time(hour=0, minute=0, second=0, tzinfo=IST), 
            chat_id=chat_id
        )
        
        # Restore ping (TEST MODE: 60 seconds, starting in 10 seconds)
        application.job_queue.run_repeating(
            hourly_ping, 
            interval=60, 
            first=10, 
            chat_id=chat_id
        )
    logging.info(f"Restored background jobs for {len(db)} users.")

# --- Core Bot Logic ---
async def send_midnight_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST)
    yesterday_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    uid = str(context.job.chat_id)
    init_user(uid)
    init_day(uid, yesterday_str)
    
    safe_date = yesterday_str.replace('-', r'\-')
    header = fr"📅 *Daily Obsidian Export \({safe_date}\)*"
    
    await send_table_for_date(context, context.job.chat_id, uid, yesterday_str, header)
    init_day(uid, now_ist.strftime("%Y-%m-%d"))
    cleanup_old_data(uid, now_ist)

async def hourly_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(context.job.chat_id)
    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%Y-%m-%d")
    
    # Keep the DB writing to the current hour slot (e.g., 11:00) so the table works
    hour_str = now_ist.strftime("%H:00") 
    
    # Exact time just to show you the minute/second in the telegram message
    exact_time = now_ist.strftime("%H:%M:%S") 
    
    init_user(uid)
    init_day(uid, date_str)
    
    msg = await context.bot.send_message(
        context.job.chat_id, 
        text=f"🧪 **TEST PING ({exact_time})**\nReply to this message with:\n1. Plan\n2. Actual\n3. Notes\n\n*(Test mode active: saving to the {hour_str} slot)*"
    )
    mid = str(msg.message_id)
    db[uid]["msg_map"][mid] = {"date": date_str, "hour": hour_str}
    db[uid]["last_ping"] = mid
    save_db()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    uid = init_user(str(update.effective_chat.id))
    target_date, target_hour = today_str, now_ist.strftime("%H:00")
    
    if update.message.reply_to_message:
        mid = str(update.message.reply_to_message.message_id)
        if mid in db[uid]["msg_map"]:
            target_date = db[uid]["msg_map"][mid]["date"]
            target_hour = db[uid]["msg_map"][mid]["hour"]
    elif db[uid]["last_ping"] and db[uid]["last_ping"] in db[uid]["msg_map"]:
        mid = db[uid]["last_ping"]
        target_date, target_hour = db[uid]["msg_map"][mid]["date"], db[uid]["msg_map"][mid]["hour"]

    init_day(uid, target_date)
    
    existing_data = db[uid]["logs"][target_date].get(target_hour, {"plan": "", "actual": "", "notes": ""})
    lines = update.message.text.split('\n')
    
    input_plan = lines[0].strip() if len(lines) > 0 else ""
    input_actual = lines[1].strip() if len(lines) > 1 else ""
    input_notes = lines[2].strip() if len(lines) > 2 else ""

    db[uid]["logs"][target_date][target_hour] = {
        "plan": input_plan if input_plan else existing_data.get("plan", ""),
        "actual": input_actual if input_actual else existing_data.get("actual", ""),
        "notes": input_notes if input_notes else existing_data.get("notes", "")
    }
    save_db()

    if target_date != today_str:
        await update.message.reply_text(f"✅ Late log updated for {target_date} ({target_hour}).")
        safe_date = target_date.replace('-', r'\-')
        header = fr"🔄 *Updated Export \({safe_date}\)*"
        await send_table_for_date(context, update.message.chat_id, uid, target_date, header)
    else:
        await update.message.reply_text(f"✅ Logged for {target_hour}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    
    # Schedule midnight report
    context.job_queue.run_daily(send_midnight_report, time=time(hour=0, minute=0, second=0, tzinfo=IST), chat_id=update.effective_chat.id)
    
    # Schedule TEST PING (Every 60 seconds, starting in 10 seconds)
    context.job_queue.run_repeating(hourly_ping, interval=60, first=10, chat_id=update.effective_chat.id)
    
    await update.message.reply_text("🤖 **Test Mode Online**\nYou will receive a ping every 60 seconds.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 **Obsidian Logger Bot Help**\n\n"
        "**Commands:**\n"
        "/export - Get current table\n"
        "/log - Overwrite manually\n"
        "/clear - Wipe today\n"
        "/help - Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def export_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    init_day(uid, today_str)
    safe_date = today_str.replace('-', r'\-')
    header = fr"📊 *Current Export \({safe_date}\)*"
    await send_table_for_date(context, update.effective_chat.id, uid, today_str, header)

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, today_str = init_user(str(update.effective_chat.id)), datetime.now(IST).strftime("%Y-%m-%d")
    db[uid]["logs"][today_str] = {f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)}
    save_db()
    await update.message.reply_text(f"🧹 Data for {today_str} wiped clean!")

async def show_grid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton(f"{i:02d}:00", callback_data=f"{i:02d}:00") for i in range(j, j+4)] for j in range(0, 24, 4)]
    await update.message.reply_text("Select hour to log:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid, now_ist = init_user(str(update.effective_chat.id)), datetime.now(IST)
    dummy_mid = f"manual_{query.data}_{now_ist.timestamp()}"
    db[uid]["msg_map"][dummy_mid] = {"date": now_ist.strftime("%Y-%m-%d"), "hour": query.data}
    db[uid]["last_ping"] = dummy_mid
    save_db()
    await query.edit_message_text(f"✍️ Manual Log for **{query.data}**\nSend 3 lines now (Plan, Actual, Notes):", parse_mode="Markdown")

if __name__ == "__main__":
    # REPLACE 'BOTTOKEN' WITH YOUR ACTUAL TOKEN
    app = Application.builder().token("8688342710:AAFT05PL2rlTjV7niWiNVqB6IR_5KSBOY-U").request(HTTPXRequest(connect_timeout=30, read_timeout=30)).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("export", export_table)) 
    app.add_handler(CommandHandler("clear", clear_data))
    app.add_handler(CommandHandler("log", show_grid))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()
