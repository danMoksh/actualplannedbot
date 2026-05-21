import json
import logging
import os
import uuid
from datetime import datetime, time, timedelta

import pytz
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# enable logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)

DB_FILE = "bot_data.json"
IST = pytz.timezone("Asia/Kolkata")

# quadrant definitions (smallcase aesthetic, small squares)
QUADRANTS = {
    "morning": "▪️ morning",
    "afternoon": "▪️ afternoon",
    "evening": "▪️ evening",
    "night": "▪️ night",
    "midnight": "▪️ midnight",
    "archived": "⏳ archived"
}
QUAD_ORDER = list(QUADRANTS.keys())

# minimal persistent keyboard
MAIN_KB = ReplyKeyboardMarkup(
    [["📋 show tasks", "🟩 toggle doing"]],
    resize_keyboard=True,
    is_persistent=True
)

# --- persistent state management ---
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
        db[uid] = {"logs": {}, "msg_map": {}, "last_ping": None, "tasks": {}, "session_start": 0}
        save_db()
    else:
        # Data structure migrations for existing users
        if "tasks" not in db[uid]: db[uid]["tasks"] = {}
        if "session_start" not in db[uid]: db[uid]["session_start"] = 0
        if "logs" not in db[uid]:
            old_data = db[uid].copy()
            now_ist = datetime.now(IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            db[uid] = {"logs": {today_str: old_data}, "msg_map": {}, "last_ping": None, "tasks": {}, "session_start": 0}
            save_db()
    return uid

def init_day(uid, date_str):
    if date_str not in db[uid]["logs"]:
        db[uid]["logs"][date_str] = {
            f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)
        }
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

# --- Task Domain Helpers ---
def get_task_mapping(uid):
    """returns a deterministic list of active/completed tasks for ui numbering."""
    visible_tasks = [
        (tid, tdata) for tid, tdata in db[uid]["tasks"].items() 
        if tdata["status"] in ["active", "completed"]
    ]
    # Sort by quadrant order, then by creation time
    visible_tasks.sort(key=lambda x: (QUAD_ORDER.index(x[1]["quadrant"]), x[1]["created_at"]))
    return [t[0] for t in visible_tasks]

# --- Core Triage & State Machine ---
async def check_triage(uid, update: Update, context: ContextTypes.DEFAULT_TYPE, force=False):
    now_ist = datetime.now(IST)
    # Define "Today's 6 AM"
    today_6am = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist.hour < 6:
        today_6am -= timedelta(days=1)
        
    last_start = datetime.fromtimestamp(db[uid].get("session_start", 0), IST)
    
    # Trigger if forced (/wakeup) OR if crossing the 6 AM threshold for the first time
    if force or (last_start < today_6am and now_ist >= today_6am):
        db[uid]["session_start"] = now_ist.timestamp()
        
        # 1. Process Completed Tasks (Yesterday's Wins)
        completed_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] == "completed"]
        if completed_ids:
            wins_text = "🏆 **yesterday's wins:**\n"
            for tid in completed_ids:
                wins_text += f"• {db[uid]['tasks'][tid]['text'].lower()}\n"
                del db[uid]["tasks"][tid] # Hard delete
            await context.bot.send_message(update.effective_chat.id, text=wins_text, parse_mode="Markdown")
            
        # 2. Loose Mode Triage for Active Tasks
        active_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] == "active"]
        for tid in active_ids:
            task = db[uid]["tasks"][tid]
            kb = [
                [InlineKeyboardButton("▪️ morn", callback_data=f"tr_{tid}_morning"),
                 InlineKeyboardButton("▪️ aft", callback_data=f"tr_{tid}_afternoon"),
                 InlineKeyboardButton("▪️ eve", callback_data=f"tr_{tid}_evening")],
                [InlineKeyboardButton("▪️ night", callback_data=f"tr_{tid}_night"),
                 InlineKeyboardButton("▪️ mid", callback_data=f"tr_{tid}_midnight")],
                [InlineKeyboardButton("⏳ archive", callback_data=f"tr_{tid}_archived"),
                 InlineKeyboardButton("🗑️ delete", callback_data=f"tr_{tid}_delete")]
            ]
            await context.bot.send_message(
                update.effective_chat.id, 
                text=f"⚠️ **rollover:** {task['text'].lower()}\n*where does this go today?*",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        save_db()

async def wakeup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    await check_triage(uid, update, context, force=True)
    await update.message.reply_text("🌅 manual wakeup triggered. session reset.", reply_markup=MAIN_KB)

# --- Task Dashboard Rendering ---
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    await check_triage(uid, update, context)
    
    mapping = get_task_mapping(uid)
    
    # calculate rolling hours based on session start
    start_ts = db[uid].get("session_start", datetime.now(IST).timestamp())
    base_dt = datetime.fromtimestamp(start_ts, IST)
    
    time_blocks = {
        "morning": (base_dt, base_dt + timedelta(hours=4)),
        "afternoon": (base_dt + timedelta(hours=4), base_dt + timedelta(hours=8)),
        "evening": (base_dt + timedelta(hours=8), base_dt + timedelta(hours=11)),
        "night": (base_dt + timedelta(hours=11), base_dt + timedelta(hours=15)),
        "midnight": (base_dt + timedelta(hours=15), base_dt + timedelta(hours=17))
    }

    def esc(text):
        # Escape markdownv2 characters securely
        for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
            text = text.replace(char, f"\\{char}")
        return text

    dashboard = "🎯 *daily quadrants*\n\n"

    for quad_key, quad_name in QUADRANTS.items():
        if quad_key == "archived": continue
        
        t_start = time_blocks[quad_key][0].strftime("%H:%M")
        t_end = time_blocks[quad_key][1].strftime("%H:%M")
        
        dashboard += f"{esc(quad_name)} \\({t_start} \\- {t_end}\\)\n"
        
        quad_empty = True
        for i, tid in enumerate(mapping):
            task = db[uid]["tasks"][tid]
            if task["quadrant"] == quad_key:
                quad_empty = False
                text = esc(task['text'].lower())
                
                # Apply strikethrough if completed
                if task["status"] == "completed":
                    text = f"~{text}~"
                    
                # Add green square if active and marked as DOING
                doing_tag = " 🟩" if task.get("is_doing") and task["status"] != "completed" else ""
                
                dashboard += f"  {i+1}\\. {text}{doing_tag}\n"
                
        if quad_empty:
            dashboard += "  \\(empty\\)\n"
        dashboard += "\n"

    # build inline buttons for active tasks (check, doing toggle, shift)
    kb = []
    for i, tid in enumerate(mapping):
        task = db[uid]["tasks"][tid]
        if task["status"] == "active":
            kb.append([
                InlineKeyboardButton(f"✅ {i+1}", callback_data=f"done_{tid}"),
                InlineKeyboardButton(f"🟩 doing", callback_data=f"toggled_{tid}"),
                InlineKeyboardButton(f"⏩ shift", callback_data=f"shift_{tid}")
            ])

    if not mapping:
        dashboard += "_no tasks yet\\. type /add to start\\._\n"

    await update.message.reply_text(dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --- Task CRUD Commands ---
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    await check_triage(uid, update, context)
    
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("format: `/add [morning/afternoon/evening/night/midnight] [task 1 \\n task 2]`", parse_mode="Markdown")
        return
        
    quad = parts[1].lower()
    if quad not in QUADRANTS:
        await update.message.reply_text(f"invalid quadrant. choose from: {', '.join(QUAD_ORDER[:-1])}")
        return
        
    tasks_to_add = parts[2].strip().split('\n')
    for text in tasks_to_add:
        if text.strip():
            tid = str(uuid.uuid4())[:8]
            db[uid]["tasks"][tid] = {
                "text": text.strip(),
                "quadrant": quad,
                "status": "active",
                "is_doing": False,
                "created_at": datetime.now(IST).timestamp()
            }
    save_db()
    await update.message.reply_text(f"✅ added {len(tasks_to_add)} task(s) to {QUADRANTS[quad]}.", reply_markup=MAIN_KB)

async def edit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    mapping = get_task_mapping(uid)
    try:
        parts = update.message.text.split(maxsplit=2)
        display_id = int(parts[1]) - 1
        new_text = parts[2]
        tid = mapping[display_id]
        db[uid]["tasks"][tid]["text"] = new_text
        save_db()
        await update.message.reply_text(f"✅ updated task #{display_id+1}")
    except (IndexError, ValueError):
        await update.message.reply_text("format: `/edit [number] [new text]`", parse_mode="Markdown")

async def del_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    mapping = get_task_mapping(uid)
    try:
        display_id = int(update.message.text.split()[1]) - 1
        tid = mapping[display_id]
        task_text = db[uid]["tasks"][tid]["text"].lower()
        del db[uid]["tasks"][tid]
        save_db()
        await update.message.reply_text(f"deleted `{task_text}`", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("format: `/del [number]`", parse_mode="Markdown")

# --- Original Time Logging Logic (Domain A) ---
async def send_table_for_date(context_or_bot, chat_id, uid, date_str, header_text):
    table = (
        "| time  | plan    | actual  | notes |\n| :---- | :------ | :------ | :---- |\n"
    )
    data = db[uid]["logs"][date_str]
    for i in range(24):
        h = f"{i:02d}:00"
        row = data.get(h, {"plan": "", "actual": "", "notes": ""})
        table += f"| {h} | {row.get('plan', '')} | {row.get('actual', '')} | {row.get('notes', '')} |\n"

    bot = context_or_bot.bot if hasattr(context_or_bot, "bot") else context_or_bot
    await bot.send_message(
        chat_id,
        text=f"""{header_text}\n\n```markdown\n{table}\n```""",
        parse_mode="MarkdownV2",
        reply_markup=MAIN_KB
    )

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "initialize the bot"),
        BotCommand("log", "manually overwrite an hour"),
        BotCommand("export", "get today's table"),
        BotCommand("clear", "wipe today's data"),
        BotCommand("tasks", "show quadrant dashboard"),
        BotCommand("wakeup", "force morning rollover"),
        BotCommand("help", "show instructions"),
    ]
    await application.bot.set_my_commands(commands)

    now = datetime.now(IST)
    delay = (
        (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now
    ).total_seconds()

    for uid_str in db.keys():
        chat_id = int(uid_str)
        application.job_queue.run_daily(
            send_midnight_report,
            time=time(hour=0, minute=0, second=0, tzinfo=IST),
            chat_id=chat_id,
        )
        application.job_queue.run_repeating(
            hourly_ping, interval=3600, first=delay, chat_id=chat_id
        )

async def send_midnight_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST)
    yesterday_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    uid = str(context.job.chat_id)
    init_user(uid)
    init_day(uid, yesterday_str)

    safe_date = yesterday_str.replace("-", r"\-")
    header = rf"📅 daily obsidian export \({safe_date}\)"

    await send_table_for_date(context, context.job.chat_id, uid, yesterday_str, header)
    init_day(uid, now_ist.strftime("%Y-%m-%d"))
    cleanup_old_data(uid, now_ist)

async def hourly_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(context.job.chat_id)
    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%Y-%m-%d")
    hour_str = now_ist.strftime("%H:00")

    init_user(uid)
    init_day(uid, date_str)

    msg = await context.bot.send_message(
        context.job.chat_id,
        text=f"⏰ {hour_str} check-in.\nreply to this message with:\n1. plan\n2. actual\n3. notes\n\n(tip: leave a line blank to keep your previous text)",
    )
    mid = str(msg.message_id)
    db[uid]["msg_map"][mid] = {"date": date_str, "hour": hour_str}
    db[uid]["last_ping"] = mid
    save_db()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    text = update.message.text.strip()
    
    # Intercept Persistent Keyboard Buttons
    if text == "📋 show tasks":
        await show_tasks(update, context)
        return
        
    elif text == "🟩 toggle doing":
        mapping = get_task_mapping(uid)
        kb = []
        row = []
        for i, tid in enumerate(mapping):
            # Only show numbers for tasks that aren't already completed
            if db[uid]["tasks"][tid]["status"] == "active":
                prefix = "🟩 " if db[uid]["tasks"][tid].get("is_doing") else ""
                row.append(InlineKeyboardButton(f"{prefix}{i+1}", callback_data=f"toggled_{tid}"))
                
            if len(row) == 4:
                kb.append(row)
                row = []
        if row: kb.append(row)
        
        if not kb:
            await update.message.reply_text("no active tasks to toggle.")
            return
            
        await update.message.reply_text("select task to toggle 🟩:", reply_markup=InlineKeyboardMarkup(kb))
        return
        
    # Trigger Heuristic Triage
    await check_triage(uid, update, context)

    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    target_date, target_hour = today_str, now_ist.strftime("%H:00")

    is_success_reply = False

    if update.message.reply_to_message:
        mid = str(update.message.reply_to_message.message_id)
        replied_text = update.message.reply_to_message.text or ""

        if replied_text.startswith("✅"):
            is_success_reply = True

        if mid in db[uid]["msg_map"]:
            target_date = db[uid]["msg_map"][mid]["date"]
            target_hour = db[uid]["msg_map"][mid]["hour"]
            if db[uid]["msg_map"][mid].get("is_success_reply"):
                is_success_reply = True

    elif db[uid]["last_ping"] and db[uid]["last_ping"] in db[uid]["msg_map"]:
        mid = db[uid]["last_ping"]
        target_date, target_hour = db[uid]["msg_map"][mid]["date"], db[uid]["msg_map"][mid]["hour"]

    init_day(uid, target_date)
    existing_data = db[uid]["logs"][target_date].get(target_hour, {"plan": "", "actual": "", "notes": ""})
    lines = text.split('\n')

    if is_success_reply and len(lines) == 1:
        input_actual = lines[0].strip()
        db[uid]["logs"][target_date][target_hour] = {
            "plan": existing_data.get("plan", ""),
            "actual": input_actual if input_actual else existing_data.get("actual", ""),
            "notes": existing_data.get("notes", "")
        }
    else:
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
        sent_msg = await update.message.reply_text(f"✅ late log updated for {target_date} ({target_hour}).")
        safe_date = target_date.replace('-', r'\-')
        header = fr"🔄 updated export \({safe_date}\)"
        await send_table_for_date(context, update.message.chat_id, uid, target_date, header)
    else:
        sent_msg = await update.message.reply_text(f"✅ logged for {target_hour}.")

    db[uid]["msg_map"][str(sent_msg.message_id)] = {
        "date": target_date,
        "hour": target_hour,
        "is_success_reply": True
    }
    save_db()

# --- Callback Router ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = str(update.effective_chat.id)
    data = query.data
    
    # 1. Task Completion
    if data.startswith("done_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            db[uid]["tasks"][tid]["status"] = "completed"
            db[uid]["tasks"][tid]["is_doing"] = False
            save_db()
            await query.answer("task completed")
            await show_tasks(update, context)
            await query.message.delete()
        else:
            await query.answer("task not found.")
            
    # 2. Doing Toggle
    elif data.startswith("toggled_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            current = db[uid]["tasks"][tid].get("is_doing", False)
            db[uid]["tasks"][tid]["is_doing"] = not current
            save_db()
            await query.answer("doing toggled")
            await show_tasks(update, context)
            await query.message.delete()

    # 3. Shift Task UI
    elif data.startswith("shift_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            task_text = db[uid]["tasks"][tid]["text"].lower()
            kb = [
                [InlineKeyboardButton("▪️ morn", callback_data=f"tr_{tid}_morning"),
                 InlineKeyboardButton("▪️ aft", callback_data=f"tr_{tid}_afternoon"),
                 InlineKeyboardButton("▪️ eve", callback_data=f"tr_{tid}_evening")],
                [InlineKeyboardButton("▪️ night", callback_data=f"tr_{tid}_night"),
                 InlineKeyboardButton("▪️ mid", callback_data=f"tr_{tid}_midnight")]
            ]
            await query.edit_message_text(f"shift where?\n`{task_text}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    # 4. Triage Routing / Deleting / Finalizing Shift
    elif data.startswith("tr_"):
        _, tid, action = data.split("_", 2)
        if tid in db[uid]["tasks"]:
            if action == "delete":
                task_text = db[uid]["tasks"][tid]["text"].lower()
                del db[uid]["tasks"][tid]
                save_db()
                await query.edit_message_text(f"deleted `{task_text}`", parse_mode="Markdown")
            else:
                db[uid]["tasks"][tid]["quadrant"] = action
                save_db()
                await query.answer(f"moved to {action}")
                await show_tasks(update, context)
                await query.message.delete()
        else:
             await query.edit_message_text("task already processed.")
             
    # 5. Legacy Hourly Grid Logic
    else:
        await query.answer()
        now_ist = datetime.now(IST)
        dummy_mid = f"manual_{data}_{now_ist.timestamp()}"
        db[uid]["msg_map"][dummy_mid] = {
            "date": now_ist.strftime("%Y-%m-%d"),
            "hour": data,
        }
        db[uid]["last_ping"] = dummy_mid
        save_db()
        await query.edit_message_text(
            f"✍️ manual log for {data}\nsend 3 lines now (plan, actual, notes):"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    context.job_queue.run_daily(
        send_midnight_report,
        time=time(hour=0, minute=0, second=0, tzinfo=IST),
        chat_id=update.effective_chat.id,
    )

    now = datetime.now(IST)
    delay = (
        (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now
    ).total_seconds()
    context.job_queue.run_repeating(
        hourly_ping, interval=3600, first=delay, chat_id=update.effective_chat.id
    )

    await update.message.reply_text(
        "🤖 automated logger online. i will ping you at the top of the next hour.",
        reply_markup=MAIN_KB
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 **obsidian logger & task bot**\n\n"
        "**time tracking:**\n"
        "reply to any hourly text with:\n"
        "1. plan\n2. actual\n3. notes\n"
        "(blank lines keep previous text. 1-line replies to ✅ logs overwrite actual.)\n\n"
        "**tasks & quadrants:**\n"
        "`/tasks` - show board (or use the persistent button below)\n"
        "`/add [quadrant] [tasks...]` - batch add tasks (e.g. `/add morning laundry`)\n"
        "`/edit [number] [new text]` - fix typo\n"
        "`/del [number]` - delete task\n"
        "`/wakeup` - force morning rollover\n\n"
        "**general:**\n"
        "`/export` - see current table\n"
        "`/log` - manually log an hour\n"
        "`/clear` - wipe today's slate clean"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def export_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    init_day(uid, today_str)
    safe_date = today_str.replace("-", r"\-")
    header = rf"📊 current export \({safe_date}\)"
    await send_table_for_date(context, update.effective_chat.id, uid, today_str, header)

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, today_str = (
        init_user(str(update.effective_chat.id)),
        datetime.now(IST).strftime("%Y-%m-%d"),
    )
    db[uid]["logs"][today_str] = {
        f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)
    }
    save_db()
    await update.message.reply_text(f"🧹 wiped all data for {today_str}.")

async def show_grid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton(f"{i:02d}:00", callback_data=f"{i:02d}:00")
            for i in range(j, j + 4)
        ]
        for j in range(0, 24, 4)
    ]
    await update.message.reply_text(
        "select hour to log:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

if __name__ == "__main__":
    app = (
        Application.builder()
        .token("8688342710:AAFT05PL2rlTjV7niWiNVqB6IR_5KSBOY-U")
        .request(HTTPXRequest(connect_timeout=30, read_timeout=30))
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("export", export_table))
    app.add_handler(CommandHandler("clear", clear_data))
    app.add_handler(CommandHandler("log", show_grid))
    app.add_handler(CommandHandler("tasks", show_tasks))
    app.add_handler(CommandHandler("wakeup", wakeup_command))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("edit", edit_task))
    app.add_handler(CommandHandler("del", del_task))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()
