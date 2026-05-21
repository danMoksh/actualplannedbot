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
        if "tasks" not in db[uid]: db[uid]["tasks"] = {}
        if "session_start" not in db[uid]: db[uid]["session_start"] = 0
        if "logs" not in db[uid]:
            old_data = db[uid].copy()
            now_ist = datetime.now(IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            db[uid] = {"logs": {today_str: old_data}, "msg_map": {}, "last_ping": None, "tasks": {}, "session_start": 0}
            save_db()
            
    # Migration: Ensure subtasks array exists on all tasks
    for tid, tdata in db[uid]["tasks"].items():
        if "subtasks" not in tdata:
            tdata["subtasks"] = []
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
    visible_tasks.sort(key=lambda x: (QUAD_ORDER.index(x[1]["quadrant"]), x[1]["created_at"]))
    return [t[0] for t in visible_tasks]

# --- Core Triage & State Machine ---
async def check_triage(uid, update: Update, context: ContextTypes.DEFAULT_TYPE, force=False):
    now_ist = datetime.now(IST)
    today_6am = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist.hour < 6:
        today_6am -= timedelta(days=1)
        
    last_start = datetime.fromtimestamp(db[uid].get("session_start", 0), IST)
    
    if force or (last_start < today_6am and now_ist >= today_6am):
        if not force: # Only auto-update if not being forced by a manual time machine
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

# --- Task Dashboard Rendering (Dual Mode: Board vs Focus) ---
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    await check_triage(uid, update, context)
    mapping = get_task_mapping(uid)
    
    def esc(text):
        for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
            text = text.replace(char, f"\\{char}")
        return text

    # Check if we are in Focus Mode
    focus_tid = context.user_data.get("focus_tid")
    
    # Mode 1: FOCUS MODE -----------------------------------------------------
    if focus_tid and focus_tid in db[uid]["tasks"]:
        task = db[uid]["tasks"][focus_tid]
        dashboard = f"🎯 *focus:* {esc(task['text'].lower())}\n\n"
        subtasks = task.get("subtasks", [])
        
        for j, sub in enumerate(subtasks):
            sub_text = esc(sub['text'].lower())
            if sub["status"] == "completed":
                dashboard += f"  {j+1}\\. \\[x\\] ~{sub_text}~\n"
            else:
                dashboard += f"  {j+1}\\. \\[\\] {sub_text}\n"
                
        kb = []
        row = []
        for j, sub in enumerate(subtasks):
            if sub["status"] == "active":
                row.append(InlineKeyboardButton(f"✅ sub {j+1}", callback_data=f"subdone_{focus_tid}_{sub['id']}"))
                if len(row) == 3:
                    kb.append(row)
                    row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("<< back to board", callback_data="focus_back")])
        
        await update.message.reply_text(dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")
        return

    # Mode 2: MAIN BOARD -----------------------------------------------------
    if not mapping:
        await update.message.reply_text("_no tasks yet\\. type /add to start\\._\n", parse_mode="MarkdownV2", reply_markup=MAIN_KB)
        return

    start_ts = db[uid].get("session_start", datetime.now(IST).timestamp())
    base_dt = datetime.fromtimestamp(start_ts, IST)
    
    time_blocks = {
        "morning": (base_dt, base_dt + timedelta(hours=4)),
        "afternoon": (base_dt + timedelta(hours=4), base_dt + timedelta(hours=8)),
        "evening": (base_dt + timedelta(hours=8), base_dt + timedelta(hours=11)),
        "night": (base_dt + timedelta(hours=11), base_dt + timedelta(hours=15)),
        "midnight": (base_dt + timedelta(hours=15), base_dt + timedelta(hours=17))
    }

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
                
                subtasks = task.get("subtasks", [])
                sub_count = len(subtasks)
                sub_tag = f" \\[\\+{sub_count}\\]" if sub_count > 0 else ""
                
                if task["status"] == "completed":
                    text = f"~{text}~"
                    
                doing_tag = " 🟩" if task.get("is_doing") and task["status"] != "completed" else ""
                dashboard += f"  {i+1}\\. {text}{doing_tag}{sub_tag}\n"
                
                # Render subtasks beneath parent
                for j, sub in enumerate(subtasks):
                    sub_text = esc(sub['text'].lower())
                    if sub["status"] == "completed":
                        dashboard += f"     ↳ \\[x\\] ~{sub_text}~\n"
                    else:
                        dashboard += f"     ↳ \\[\\] {sub_text}\n"
                
        if quad_empty:
            dashboard += "  \\(empty\\)\n"
        dashboard += "\n"

    # Inline KB for Main Board
    kb = []
    for i, tid in enumerate(mapping):
        task = db[uid]["tasks"][tid]
        if task["status"] == "active":
            row = [
                InlineKeyboardButton(f"✅ {i+1}", callback_data=f"done_{tid}"),
                InlineKeyboardButton(f"🟩", callback_data=f"toggled_{tid}"),
                InlineKeyboardButton(f"⏩", callback_data=f"shift_{tid}")
            ]
            if task.get("subtasks"):
                row.append(InlineKeyboardButton(f"🔍", callback_data=f"focus_{tid}"))
            kb.append(row)

    await update.message.reply_text(dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --- Task CRUD Commands ---
async def wakeup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    parts = update.message.text.split()
    now_ist = datetime.now(IST)
    
    if len(parts) > 1:
        try:
            target_hour = int(parts[1])
            if 0 <= target_hour <= 23:
                # Time Machine Logic: force session start to requested hour
                new_start = now_ist.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                if new_start > now_ist:
                    new_start -= timedelta(days=1)
                db[uid]["session_start"] = new_start.timestamp()
                save_db()
                await update.message.reply_text(f"🌅 time machine: session start forced to {target_hour}:00.", reply_markup=MAIN_KB)
                return
        except ValueError:
            pass
            
    # Default behavior
    db[uid]["session_start"] = now_ist.timestamp()
    save_db()
    await check_triage(uid, update, context, force=True)
    await update.message.reply_text("🌅 manual wakeup triggered. session reset to now.", reply_markup=MAIN_KB)

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("format: `/add [quadrant] [task 1 \\n task 2]`", parse_mode="Markdown")
        return
    quad = parts[1].lower()
    if quad not in QUADRANTS:
        await update.message.reply_text("invalid quadrant.")
        return
    tasks_to_add = parts[2].strip().split('\n')
    for text in tasks_to_add:
        if text.strip():
            db[uid]["tasks"][str(uuid.uuid4())[:8]] = {
                "text": text.strip(),
                "quadrant": quad,
                "status": "active",
                "is_doing": False,
                "created_at": datetime.now(IST).timestamp(),
                "subtasks": []
            }
    save_db()
    await update.message.reply_text(f"✅ added {len(tasks_to_add)} task(s).")

async def addsub_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    mapping = get_task_mapping(uid)
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("format: `/addsub [task_number] [subtask 1 \\n subtask 2]`", parse_mode="Markdown")
        return
    try:
        display_id = int(parts[1]) - 1
        tid = mapping[display_id]
        subs_to_add = parts[2].strip().split('\n')
        for text in subs_to_add:
            if text.strip():
                db[uid]["tasks"][tid]["subtasks"].append({
                    "id": str(uuid.uuid4())[:8],
                    "text": text.strip(),
                    "status": "active"
                })
        save_db()
        await update.message.reply_text(f"✅ added {len(subs_to_add)} subtask(s) to task #{display_id+1}.")
    except (IndexError, ValueError):
        await update.message.reply_text("invalid task number.")

async def done_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    mapping = get_task_mapping(uid)
    try:
        parts = update.message.text.split()[1].split('.')
        t_idx = int(parts[0]) - 1
        tid = mapping[t_idx]
        
        if len(parts) == 2: # Subtask: /done 1.2
            sub_idx = int(parts[1]) - 1
            db[uid]["tasks"][tid]["subtasks"][sub_idx]["status"] = "completed"
            save_db()
            await update.message.reply_text(f"✅ checked off subtask {parts[0]}.{parts[1]}")
        else: # Parent task: /done 1
            db[uid]["tasks"][tid]["status"] = "completed"
            db[uid]["tasks"][tid]["is_doing"] = False
            save_db()
            await update.message.reply_text(f"✅ checked off task {parts[0]}")
    except (IndexError, ValueError, KeyError):
        await update.message.reply_text("format: `/done 1` or `/done 1.2`", parse_mode="Markdown")

async def edit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    mapping = get_task_mapping(uid)
    try:
        parts = update.message.text.split(maxsplit=2)
        display_id = int(parts[1]) - 1
        db[uid]["tasks"][mapping[display_id]]["text"] = parts[2]
        save_db()
        await update.message.reply_text(f"✅ updated task #{display_id+1}")
    except:
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
    except:
        await update.message.reply_text("format: `/del [number]`", parse_mode="Markdown")

# --- Original Time Logging Logic ---
async def send_table_for_date(context_or_bot, chat_id, uid, date_str, header_text):
    table = "| time  | plan    | actual  | notes |\n| :---- | :------ | :------ | :---- |\n"
    data = db[uid]["logs"][date_str]
    for i in range(24):
        h = f"{i:02d}:00"
        row = data.get(h, {"plan": "", "actual": "", "notes": ""})
        table += f"| {h} | {row.get('plan', '')} | {row.get('actual', '')} | {row.get('notes', '')} |\n"
    bot = context_or_bot.bot if hasattr(context_or_bot, "bot") else context_or_bot
    await bot.send_message(chat_id, text=f"""{header_text}\n\n```markdown\n{table}\n```""", parse_mode="MarkdownV2", reply_markup=MAIN_KB)

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("tasks", "show quadrant dashboard"),
        BotCommand("wakeup", "force morning rollover"),
        BotCommand("log", "manually overwrite an hour"),
        BotCommand("export", "get today's table"),
        BotCommand("clear", "wipe today's data"),
        BotCommand("help", "show instructions"),
    ]
    await application.bot.set_my_commands(commands)
    now = datetime.now(IST)
    delay = ((now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now).total_seconds()
    for uid_str in db.keys():
        application.job_queue.run_daily(send_midnight_report, time=time(hour=0, minute=0, second=0, tzinfo=IST), chat_id=int(uid_str))
        application.job_queue.run_repeating(hourly_ping, interval=3600, first=delay, chat_id=int(uid_str))

async def send_midnight_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST)
    yesterday_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    uid = str(context.job.chat_id)
    init_user(uid)
    init_day(uid, yesterday_str)
    header = rf"📅 daily obsidian export \({yesterday_str.replace('-', r'\-')}\)"
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
    db[uid]["msg_map"][str(msg.message_id)] = {"date": date_str, "hour": hour_str}
    db[uid]["last_ping"] = str(msg.message_id)
    save_db()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    text = update.message.text.strip()
    
    # Intercept Persistent Keyboard Buttons
    if text == "📋 show tasks":
        context.user_data["focus_tid"] = None # Reset focus on fresh load
        await show_tasks(update, context)
        return
        
    elif text == "🟩 toggle doing":
        mapping = get_task_mapping(uid)
        kb, row = [], []
        for i, tid in enumerate(mapping):
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
        
    await check_triage(uid, update, context)

    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    target_date, target_hour = today_str, now_ist.strftime("%H:00")
    is_success_reply = False

    if update.message.reply_to_message:
        mid = str(update.message.reply_to_message.message_id)
        if (update.message.reply_to_message.text or "").startswith("✅"): is_success_reply = True
        if mid in db[uid]["msg_map"]:
            target_date, target_hour = db[uid]["msg_map"][mid]["date"], db[uid]["msg_map"][mid]["hour"]
            if db[uid]["msg_map"][mid].get("is_success_reply"): is_success_reply = True

    elif db[uid]["last_ping"] and db[uid]["last_ping"] in db[uid]["msg_map"]:
        mid = db[uid]["last_ping"]
        target_date, target_hour = db[uid]["msg_map"][mid]["date"], db[uid]["msg_map"][mid]["hour"]

    init_day(uid, target_date)
    existing_data = db[uid]["logs"][target_date].get(target_hour, {"plan": "", "actual": "", "notes": ""})
    lines = text.split('\n')

    if is_success_reply and len(lines) == 1:
        db[uid]["logs"][target_date][target_hour] = {
            "plan": existing_data.get("plan", ""),
            "actual": lines[0].strip() if lines[0].strip() else existing_data.get("actual", ""),
            "notes": existing_data.get("notes", "")
        }
    else:
        db[uid]["logs"][target_date][target_hour] = {
            "plan": lines[0].strip() if len(lines) > 0 and lines[0].strip() else existing_data.get("plan", ""),
            "actual": lines[1].strip() if len(lines) > 1 and lines[1].strip() else existing_data.get("actual", ""),
            "notes": lines[2].strip() if len(lines) > 2 and lines[2].strip() else existing_data.get("notes", "")
        }

    save_db()

    if target_date != today_str:
        sent_msg = await update.message.reply_text(f"✅ late log updated for {target_date} ({target_hour}).")
        header = fr"🔄 updated export \({target_date.replace('-', r'\-')}\)"
        await send_table_for_date(context, update.message.chat_id, uid, target_date, header)
    else:
        sent_msg = await update.message.reply_text(f"✅ logged for {target_hour}.")

    db[uid]["msg_map"][str(sent_msg.message_id)] = {"date": target_date, "hour": target_hour, "is_success_reply": True}
    save_db()

# --- Callback Router ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = str(update.effective_chat.id)
    data = query.data
    
    # Mode Switching
    if data.startswith("focus_"):
        if data == "focus_back":
            context.user_data["focus_tid"] = None
        else:
            context.user_data["focus_tid"] = data.split("_")[1]
        await show_tasks(update, context)
        await query.message.delete()
        
    # Completing Subtask
    elif data.startswith("subdone_"):
        _, tid, subid = data.split("_", 2)
        for sub in db[uid]["tasks"][tid].get("subtasks", []):
            if sub["id"] == subid:
                sub["status"] = "completed"
        save_db()
        await query.answer("subtask completed")
        await show_tasks(update, context) # Focus mode will automatically re-render
        await query.message.delete()
        
    # Completing Task
    elif data.startswith("done_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            db[uid]["tasks"][tid]["status"] = "completed"
            db[uid]["tasks"][tid]["is_doing"] = False
            save_db()
            await query.answer("task completed")
            await show_tasks(update, context)
            await query.message.delete()
            
    # Doing Toggle
    elif data.startswith("toggled_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            db[uid]["tasks"][tid]["is_doing"] = not db[uid]["tasks"][tid].get("is_doing", False)
            save_db()
            await query.answer("doing toggled")
            await show_tasks(update, context)
            await query.message.delete()

    # Shift UI
    elif data.startswith("shift_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            kb = [
                [InlineKeyboardButton("▪️ morn", callback_data=f"tr_{tid}_morning"),
                 InlineKeyboardButton("▪️ aft", callback_data=f"tr_{tid}_afternoon"),
                 InlineKeyboardButton("▪️ eve", callback_data=f"tr_{tid}_evening")],
                [InlineKeyboardButton("▪️ night", callback_data=f"tr_{tid}_night"),
                 InlineKeyboardButton("▪️ mid", callback_data=f"tr_{tid}_midnight")]
            ]
            await query.edit_message_text(f"shift where?\n`{db[uid]['tasks'][tid]['text'].lower()}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    # Shift Processing
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
             
    # Hourly Grid
    else:
        await query.answer()
        dummy_mid = f"manual_{data}_{datetime.now(IST).timestamp()}"
        db[uid]["msg_map"][dummy_mid] = {"date": datetime.now(IST).strftime("%Y-%m-%d"), "hour": data}
        db[uid]["last_ping"] = dummy_mid
        save_db()
        await query.edit_message_text(f"✍️ manual log for {data}\nsend 3 lines now (plan, actual, notes):")

# --- Boring Commands ---
async def export_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    init_day(uid, today_str)
    await send_table_for_date(context, update.effective_chat.id, uid, today_str, rf"📊 current export \({today_str.replace('-', r'\-')}\)")

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id))
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    db[uid]["logs"][today_str] = {f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)}
    save_db()
    await update.message.reply_text(f"🧹 wiped all data for {today_str}.")

async def show_grid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton(f"{i:02d}:00", callback_data=f"{i:02d}:00") for i in range(j, j + 4)] for j in range(0, 24, 4)]
    await update.message.reply_text("select hour to log:", reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 **bot instructions**\n\n"
        "`/tasks` - show board\n"
        "`/wakeup 10` - force day to start at 10 AM\n"
        "`/add morning walk` - add task\n"
        "`/addsub 1 get shoes` - add subtask to task #1\n"
        "`/done 1` - complete task #1\n"
        "`/done 1.2` - complete subtask #2 under task #1\n"
        "`/del 1` - delete task #1\n"
        "`/edit 1 new text` - rewrite task #1"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)

if __name__ == "__main__":
    app = Application.builder().token("8688342710:AAFT05PL2rlTjV7niWiNVqB6IR_5KSBOY-U").request(HTTPXRequest(connect_timeout=30, read_timeout=30)).post_init(post_init).build()
    
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("export", export_table))
    app.add_handler(CommandHandler("clear", clear_data))
    app.add_handler(CommandHandler("log", show_grid))
    app.add_handler(CommandHandler("tasks", show_tasks))
    app.add_handler(CommandHandler("wakeup", wakeup_command))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("addsub", addsub_task))
    app.add_handler(CommandHandler("done", done_shortcut))
    app.add_handler(CommandHandler("edit", edit_task))
    app.add_handler(CommandHandler("del", del_task))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
