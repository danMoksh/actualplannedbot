import json
import logging
import os
import uuid
import random
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

# load environment variables from .env if present
env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

DB_FILE = "bot_data.json"
IST = pytz.timezone("Asia/Kolkata")

CHEERS = [
    "proud of u.", 
    "u got this.", 
    "take a breath, ur doing great.", 
    "ur crushing it.", 
    "keep flowing.", 
    "one step at a time, u got it."
]

# quadrant definitions (celestial & minimal aesthetic)
QUADRANTS = {
    "morning": "☼ morning",
    "afternoon": "☁︎ afternoon",
    "evening": "☾ evening",
    "night": "⋆｡°✩ night",
    "midnight": "✦ midnight",
    "archived": "⨯ archived"
}
QUAD_ORDER = list(QUADRANTS.keys())

# minimal persistent keyboard
MAIN_KB = ReplyKeyboardMarkup(
    [["▤ show tasks"]],
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


def schedule_jobs_for_user(context, uid_str):
    uid_str = str(uid_str)
    jq = context.job_queue if hasattr(context, "job_queue") else getattr(context.application, "job_queue", None)
    if not jq: return
    
    if jq.get_jobs_by_name(f"ping_{uid_str}"):
        return
        
    now = datetime.now(IST)
    delay = ((now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now).total_seconds()
    if delay < 0: delay += 3600 # safety
    
    jq.run_daily(send_midnight_report, time=time(hour=0, minute=0, second=0, tzinfo=IST), chat_id=int(uid_str), name=f"midnight_{uid_str}")
    jq.run_repeating(hourly_ping, interval=3600, first=delay, chat_id=int(uid_str), name=f"ping_{uid_str}")
    jq.run_monthly(monthly_archive_dump, when=time(0, 0, tzinfo=IST), day=1, chat_id=int(uid_str), name=f"monthly_{uid_str}")

def init_user(uid, context=None):
    uid = str(uid)
    if context:
        schedule_jobs_for_user(context, uid)
    if uid not in db:
        db[uid] = {"logs": {}, "msg_map": {}, "last_ping": None, "tasks": {}, "session_start": 0, "is_sleeping": False}
        save_db()
    else:
        if "tasks" not in db[uid]: db[uid]["tasks"] = {}
        if "session_start" not in db[uid]: db[uid]["session_start"] = 0
        if "is_sleeping" not in db[uid]: db[uid]["is_sleeping"] = False
        if "archive" not in db[uid]: db[uid]["archive"] = []
        if "targets" not in db[uid]: db[uid]["targets"] = {"today": "", "weekly": "", "monthly": ""}
        if "logs" not in db[uid]:
            old_data = db[uid].copy()
            now_ist = datetime.now(IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            db[uid] = {"logs": {today_str: old_data}, "msg_map": {}, "last_ping": None, "tasks": {}, "session_start": 0, "is_sleeping": False}
            save_db()
            
    # migration: ensure subtasks and priority exist on all tasks
    for tid, tdata in db[uid]["tasks"].items():
        if "subtasks" not in tdata:
            tdata["subtasks"] = []
        if "priority" not in tdata:
            tdata["priority"] = False
            
    # migration: ensure today target exists for older users
    if "today" not in db[uid].get("targets", {}):
        db[uid]["targets"]["today"] = ""
        
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

# --- task domain helpers ---
def get_task_mapping(uid):
    visible_tasks = [
        (tid, tdata) for tid, tdata in db[uid]["tasks"].items() 
        if tdata["status"] in ["active", "completed", "deferred"]
    ]
    visible_tasks.sort(key=lambda x: (QUAD_ORDER.index(x[1]["quadrant"]) if x[1]["quadrant"] in QUAD_ORDER else 99, x[1]["created_at"]))
    return [t[0] for t in visible_tasks]

def schedule_quadrant_alerts(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, base_ts: float):
    # Clear old quad end jobs
    for job in context.job_queue.get_jobs_by_name(f"quad_end_{uid}"):
        job.schedule_removal()
    
    base_dt = datetime.fromtimestamp(base_ts, IST)
    # Match the hours defined in time_blocks
    offsets = [
        ("morning", "afternoon", 4),
        ("afternoon", "evening", 8),
        ("evening", "night", 11),
        ("night", "midnight", 15),
        ("midnight", "done", 17)
    ]
    
    for cur_q, next_q, hrs in offsets:
        run_time = base_dt + timedelta(hours=hrs)
        if run_time > datetime.now(IST):
            context.job_queue.run_once(
                quad_boundary_alert,
                when=run_time,
                chat_id=chat_id,
                name=f"quad_end_{uid}",
                data={"uid": uid, "cur_q": cur_q, "next_q": next_q}
            )

async def quad_boundary_alert(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    uid = data["uid"]
    cur_q = data["cur_q"]
    next_q = data["next_q"]
    
    tasks = [t for t in db[uid]["tasks"].values() if t["quadrant"] == cur_q and t["status"] == "active"]
    
    msg = f"✦ {QUADRANTS[cur_q]} ended.\n_{random.choice(CHEERS)}_"
    kb = []
    if tasks and next_q != "done":
        kb = [
            [InlineKeyboardButton(f"shift pending to {next_q}", callback_data=f"sa_{cur_q}_{next_q}")],
            [InlineKeyboardButton("leave them", callback_data="sa_ignore")]
        ]
        
    markup = InlineKeyboardMarkup(kb) if kb else None
    await context.bot.send_message(context.job.chat_id, text=msg, parse_mode="Markdown", reply_markup=markup)

async def monthly_archive_dump(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(context.job.chat_id)
    init_user(uid)
    archive = db[uid]["archive"]
    if not archive: return
    
    report = "⨯ *monthly archive dump*\n\n"
    for t in archive:
        report += f" - {t}\n"
    
    await context.bot.send_message(context.job.chat_id, text=report, parse_mode="Markdown")
    db[uid]["archive"] = []
    save_db()

# --- core triage & state machine ---
async def check_triage(uid, update: Update, context: ContextTypes.DEFAULT_TYPE, force=False):
    now_ist = datetime.now(IST)
    today_6am = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist.hour < 6:
        today_6am -= timedelta(days=1)
        
    last_start = datetime.fromtimestamp(db[uid].get("session_start", 0), IST)
    
    if force or (last_start < today_6am and now_ist >= today_6am):
        
        if db[uid].get("is_sleeping"):
            if not force:
                db[uid]["session_start"] = now_ist.timestamp()
                schedule_quadrant_alerts(context, update.effective_chat.id, uid, db[uid]["session_start"])
            db[uid]["is_sleeping"] = False
            save_db()
            return
            
        if not force:
            db[uid]["session_start"] = now_ist.timestamp()
            schedule_quadrant_alerts(context, update.effective_chat.id, uid, db[uid]["session_start"])
            db[uid]["is_sleeping"] = False
        
        completed_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] == "completed"]
        if completed_ids:
            wins_text = "✦ *yesterday's wins:*\n"
            for tid in completed_ids:
                wins_text += f" - {db[uid]['tasks'][tid]['text'].lower()}\n"
                del db[uid]["tasks"][tid] 
            await context.bot.send_message(update.effective_chat.id, text=wins_text, parse_mode="Markdown")
            
        triage_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] in ["active", "deferred"]]
        for tid in triage_ids:
            task = db[uid]["tasks"][tid]
            kb = [
                [InlineKeyboardButton("☼ morn", callback_data=f"tr_{tid}_morning"),
                 InlineKeyboardButton("☁︎ aft", callback_data=f"tr_{tid}_afternoon"),
                 InlineKeyboardButton("☾ eve", callback_data=f"tr_{tid}_evening")],
                [InlineKeyboardButton("⋆｡°✩ night", callback_data=f"tr_{tid}_night"),
                 InlineKeyboardButton("✦ mid", callback_data=f"tr_{tid}_midnight")],
                [InlineKeyboardButton("⨯ arc", callback_data=f"tr_{tid}_archived"),
                 InlineKeyboardButton("⨯ del", callback_data=f"tr_{tid}_delete"),
                 InlineKeyboardButton("⤅ tmrw", callback_data=f"tr_{tid}_deferred")]
            ]
            await context.bot.send_message(
                update.effective_chat.id, 
                text=f"⌕ *rollover:* {task['text'].lower()}\n_where does this go today?_",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        save_db()

# --- task dashboard rendering ---
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    await check_triage(uid, update, context)
    mapping = get_task_mapping(uid)
    
    def esc(text):
        for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!', '^']:
            text = text.replace(char, f"\\{char}")
        return text

    focus_tid = context.user_data.get("focus_tid")
    view_quad = context.user_data.get("view_quad")
    
    # Mode 1: Focus mode
    if focus_tid and focus_tid in db[uid]["tasks"]:
        task = db[uid]["tasks"][focus_tid]
        dashboard = f"⌕ *focus:* {esc(task['text'].lower())}\n\n"
        subtasks = task.get("subtasks", [])
        
        for j, sub in enumerate(subtasks):
            sub_text = esc(sub['text'].lower())
            if sub["status"] == "completed":
                dashboard += f"  {j+1}\\. \\[✓\\] ~{sub_text}~\n"
            else:
                dashboard += f"  {j+1}\\. \\[\\] {sub_text}\n"
                
        kb = []
        row = []
        for j, sub in enumerate(subtasks):
            if sub["status"] == "active":
                row.append(InlineKeyboardButton(f"[✓] sub {j+1}", callback_data=f"subdone_{focus_tid}_{sub['id']}"))
                if len(row) == 3:
                    kb.append(row)
                    row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("« back", callback_data="focus_back")])
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text=dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")
        return

    # Mode 2 & 3: ALWAYS render the full Bird's-Eye View text
    sleep_status = " _\\(sleeping\\)_" if db[uid].get("is_sleeping") else ""
    dashboard = f"✦ *daily board*{sleep_status}\n\n_{esc(random.choice(CHEERS))}_\n\n"
    
    start_ts = db[uid].get("session_start", datetime.now(IST).timestamp())
    base_dt = datetime.fromtimestamp(start_ts, IST)
    
    time_blocks = {
        "morning": (base_dt, base_dt + timedelta(hours=4)),
        "afternoon": (base_dt + timedelta(hours=4), base_dt + timedelta(hours=8)),
        "evening": (base_dt + timedelta(hours=8), base_dt + timedelta(hours=11)),
        "night": (base_dt + timedelta(hours=11), base_dt + timedelta(hours=15)),
        "midnight": (base_dt + timedelta(hours=15), base_dt + timedelta(hours=17))
    }

    # Generate the text for ALL quadrants
    for quad_key, quad_name in QUADRANTS.items():
        if quad_key == "archived": continue
        
        t_start = time_blocks[quad_key][0].strftime("%H:%M")
        t_end = time_blocks[quad_key][1].strftime("%H:%M")
        
        dashboard += f"*{esc(quad_name)}* \\({t_start} \\- {t_end}\\)\n"
        
        quad_empty = True
        for i, tid in enumerate(mapping):
            task = db[uid]["tasks"][tid]
            if task["quadrant"] == quad_key and task["status"] in ["active", "completed"]:
                quad_empty = False
                text = esc(task['text'].lower())
                
                subtasks = task.get("subtasks", [])
                sub_count = len(subtasks)
                sub_tag = rf" \\[\+{sub_count}\\]" if sub_count > 0 else ""
                prio_tag = r"☯︎ " if task.get("priority") else ""
                
                if task["status"] == "completed":
                    text = f"~{text}~"
                    
                doing_tag = r" ⟡" if task.get("is_doing") and task["status"] != "completed" else ""
                dashboard += f"  {i+1}\\. {prio_tag}{text}{doing_tag}{sub_tag}\n"
                
                for j, sub in enumerate(subtasks):
                    sub_text = esc(sub['text'].lower())
                    if sub["status"] == "completed":
                        dashboard += f"     ↳ \\[✓\\] ~{sub_text}~\n"
                    else:
                        dashboard += f"     ↳ \\[\\] {sub_text}\n"

        if quad_empty:
            dashboard += "  \\(empty\\)\n"
        dashboard += "\n"

    # Determine which buttons go at the bottom
    kb = []
    if not view_quad:
        # Standard View: Show Quad Selectors
        kb = [
            [InlineKeyboardButton("☼ morn", callback_data="vq_morning"),
             InlineKeyboardButton("☁︎ aft", callback_data="vq_afternoon"),
             InlineKeyboardButton("☾ eve", callback_data="vq_evening")],
            [InlineKeyboardButton("⋆｡°✩ night", callback_data="vq_night"),
             InlineKeyboardButton("✦ mid", callback_data="vq_midnight")]
        ]
    else:
        # Isolated Quad View: Buttons show action items for selected quad
        for i, tid in enumerate(mapping):
            task = db[uid]["tasks"][tid]
            if task["quadrant"] == view_quad and task["status"] == "active":
                row = [
                    InlineKeyboardButton(f"[✓] {i+1}", callback_data=f"done_{tid}"),
                    InlineKeyboardButton(f"[⟡]", callback_data=f"toggled_{tid}"),
                    InlineKeyboardButton(f"[⤹]", callback_data=f"shift_{tid}")
                ]
                if task.get("subtasks"):
                    row.append(InlineKeyboardButton(f"[⌕]", callback_data=f"focus_{tid}"))
                kb.append(row)
        kb.append([InlineKeyboardButton("« back", callback_data="vq_back")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=dashboard, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --- task crud commands ---
async def wakeup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    parts = update.message.text.split()
    now_ist = datetime.now(IST)
    
    target_ts = now_ist.timestamp()
    if len(parts) > 1:
        try:
            target_hour = int(parts[1])
            if 0 <= target_hour <= 23:
                new_start = now_ist.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                if new_start > now_ist:
                    new_start -= timedelta(days=1)
                target_ts = new_start.timestamp()
        except ValueError:
            pass
            
    was_sleeping = db[uid].get("is_sleeping", False)
    db[uid]["session_start"] = target_ts
    db[uid]["is_sleeping"] = False
    save_db()
    
    schedule_quadrant_alerts(context, update.effective_chat.id, uid, target_ts)
    
    if was_sleeping:
        msg = "⤹ time machine: session start set." if len(parts) > 1 else "☼ manual wakeup triggered. session reset to now."
        await update.message.reply_text(msg, reply_markup=MAIN_KB)
    else:
        await check_triage(uid, update, context, force=True)
        msg = "⤹ time machine: session start set." if len(parts) > 1 else "☼ manual wakeup triggered. session reset to now."
        await update.message.reply_text(msg, reply_markup=MAIN_KB)

    # Separated Targets Message sent ONLY during wakeup
    targets = db[uid].get("targets", {"today": "", "weekly": "", "monthly": ""})
    target_msg = f"✦ *targets*\n\n*today:* {targets.get('today', '') or 'none'}\n*weekly:* {targets.get('weekly', '') or 'none'}\n*monthly:* {targets.get('monthly', '') or 'none'}"
    await update.message.reply_text(target_msg, parse_mode="Markdown")

    # Automatically show full board
    context.user_data["view_quad"] = None
    await show_tasks(update, context)

async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    
    if db[uid].get("is_sleeping"):
        await update.message.reply_text("you are already in sleep mode. use `/add` to prep tomorrow's tasks.", parse_mode="Markdown")
        return

    completed_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] == "completed"]
    if completed_ids:
        wins_text = "✦ *today's wins:*\n"
        for tid in completed_ids:
            wins_text += f" - {db[uid]['tasks'][tid]['text'].lower()}\n"
            del db[uid]["tasks"][tid] 
        await update.message.reply_text(wins_text, parse_mode="Markdown")
        
    active_ids = [tid for tid, tdata in db[uid]["tasks"].items() if tdata["status"] == "active"]
    for tid in active_ids:
        task = db[uid]["tasks"][tid]
        kb = [
            [InlineKeyboardButton("☼ morn", callback_data=f"tr_{tid}_morning"),
             InlineKeyboardButton("☁︎ aft", callback_data=f"tr_{tid}_afternoon"),
             InlineKeyboardButton("☾ eve", callback_data=f"tr_{tid}_evening")],
            [InlineKeyboardButton("⋆｡°✩ night", callback_data=f"tr_{tid}_night"),
             InlineKeyboardButton("✦ mid", callback_data=f"tr_{tid}_midnight")],
            [InlineKeyboardButton("⨯ arc", callback_data=f"tr_{tid}_archived"),
             InlineKeyboardButton("⨯ del", callback_data=f"tr_{tid}_delete"),
             InlineKeyboardButton("⤅ tmrw", callback_data=f"tr_{tid}_deferred")]
        ]
        await update.message.reply_text(
            text=f"⌕ *leftover:* {task['text'].lower()}\n_where does this go tomorrow?_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        
    db[uid]["is_sleeping"] = True
    save_db()
    
    for job in context.job_queue.get_jobs_by_name(f"quad_end_{uid}"):
        job.schedule_removal()
        
    await update.message.reply_text("⋆｡°✩ sleep mode activated. goodnight. you can now `/add` tomorrow's tasks.", parse_mode="Markdown")

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("format: `/add [quadrant] [task 1 \n task 2]`", parse_mode="Markdown")
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
                "priority": False,
                "created_at": datetime.now(IST).timestamp(),
                "subtasks": []
            }
    save_db()
    await update.message.reply_text(f"[✓] added {len([t for t in tasks_to_add if t.strip()])} task(s).")

async def addsub_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("format: `/addsub [task_number] [subtask 1 \n subtask 2]`", parse_mode="Markdown")
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
        await update.message.reply_text(f"[✓] added subtask(s) to task #{display_id+1}.")
    except (IndexError, ValueError):
        await update.message.reply_text("invalid task number.")

async def done_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    try:
        parts = update.message.text.split()[1].split('.')
        t_idx = int(parts[0]) - 1
        tid = mapping[t_idx]
        
        if len(parts) == 2:
            sub_idx = int(parts[1]) - 1
            db[uid]["tasks"][tid]["subtasks"][sub_idx]["status"] = "completed"
            save_db()
            await update.message.reply_text(f"[✓] checked off subtask {parts[0]}.{parts[1]}")
        else:
            db[uid]["tasks"][tid]["status"] = "completed"
            db[uid]["tasks"][tid]["is_doing"] = False
            save_db()
            await update.message.reply_text(f"[✓] checked off task {parts[0]}\n_{random.choice(CHEERS)}_", parse_mode="Markdown")
    except (IndexError, ValueError, KeyError):
        await update.message.reply_text("format: `/done 1` or `/done 1.2`", parse_mode="Markdown")

async def rev_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    try:
        display_id = int(update.message.text.split()[1]) - 1
        tid = mapping[display_id]
        db[uid]["tasks"][tid]["status"] = "active"
        save_db()
        await update.message.reply_text(f"⤹ reverted task {display_id+1} to active.")
    except:
        await update.message.reply_text("format: `/rev [number]`")

async def high_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    try:
        display_id = int(update.message.text.split()[1]) - 1
        tid = mapping[display_id]
        current = db[uid]["tasks"][tid].get("priority", False)
        db[uid]["tasks"][tid]["priority"] = not current
        save_db()
        await update.message.reply_text(f"☯︎ task {display_id+1} priority toggled.")
    except:
        await update.message.reply_text("format: `/high [number]`")

async def edit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    try:
        parts = update.message.text.split(maxsplit=2)
        display_id = int(parts[1]) - 1
        db[uid]["tasks"][mapping[display_id]]["text"] = parts[2]
        save_db()
        await update.message.reply_text(f"[⌕] updated task #{display_id+1}")
    except:
        await update.message.reply_text("format: `/edit [number] [new text]`", parse_mode="Markdown")

async def del_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    mapping = get_task_mapping(uid)
    try:
        display_id = int(update.message.text.split()[1]) - 1
        tid = mapping[display_id]
        task_text = db[uid]["tasks"][tid]["text"].lower()
        del db[uid]["tasks"][tid]
        save_db()
        await update.message.reply_text(f"⨯ deleted `{task_text}`", parse_mode="Markdown")
    except:
        await update.message.reply_text("format: `/del [number]`", parse_mode="Markdown")

async def set_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    try:
        text = update.message.text.split(" ", 1)[1]
        db[uid]["targets"]["today"] = text
        save_db()
        await update.message.reply_text("✦ today target set.")
    except:
        pass

async def set_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    try:
        text = update.message.text.split(" ", 1)[1]
        db[uid]["targets"]["weekly"] = text
        save_db()
        await update.message.reply_text("✦ weekly target set.")
    except:
        pass

async def set_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    try:
        text = update.message.text.split(" ", 1)[1]
        db[uid]["targets"]["monthly"] = text
        save_db()
        await update.message.reply_text("✦ monthly target set.")
    except:
        pass

async def view_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    archive = db[uid]["archive"]
    if not archive:
        await update.message.reply_text("archive is empty.")
        return
    text = "⨯ *this month's archive*\n\n"
    for t in archive:
        text += f" - {t}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def view_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    targets = db[uid].get("targets", {"today": "", "weekly": "", "monthly": ""})
    target_msg = f"✦ *current goals*\n\n*today:* {targets.get('today', '') or 'none'}\n*weekly:* {targets.get('weekly', '') or 'none'}\n*monthly:* {targets.get('monthly', '') or 'none'}"
    await update.message.reply_text(target_msg, parse_mode="Markdown")

# --- original time logging logic ---
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
        BotCommand("tasks", "show full dashboard"),
        BotCommand("sleep", "trigger end-of-day routine"),
        BotCommand("wakeup", "force morning rollover"),
        BotCommand("log", "manually overwrite an hour"),
        BotCommand("export", "get today's table"),
        BotCommand("clear", "wipe today's data"),
        BotCommand("help", "show instructions"),
        BotCommand("add", "batch add tasks"),
        BotCommand("high", "toggle high priority"),
        BotCommand("done", "mark task complete"),
        BotCommand("rev", "un-complete a task"),
        BotCommand("today", "set daily target"),
        BotCommand("week", "set weekly target"),
        BotCommand("month", "set monthly target"),
        BotCommand("archive", "view archived tasks"),
        BotCommand("goals", "view weekly and monthly goals")
    ]
    await application.bot.set_my_commands(commands)
    now = datetime.now(IST)
    delay = ((now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now).total_seconds()
    class AppContext:
        def __init__(self, app): self.application = app; self.job_queue = app.job_queue
    app_ctx = AppContext(application)
    for uid_str in db.keys():
        schedule_jobs_for_user(app_ctx, uid_str)

async def send_midnight_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ist = datetime.now(IST)
    yesterday_str = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    uid = str(context.job.chat_id)
    init_user(uid)
    init_day(uid, yesterday_str)
    header = rf"☾ daily obsidian export \({yesterday_str.replace('-', r'\-')}\)"
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
        text=f"⌕ {hour_str} check-in.\nreply to this message with:\n1. plan\n2. actual\n3. notes\n\n(tip: leave a line blank to keep your previous text)",
    )
    db[uid]["msg_map"][str(msg.message_id)] = {"date": date_str, "hour": hour_str}
    db[uid]["last_ping"] = str(msg.message_id)
    save_db()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    text = update.message.text.strip()
    
    if text == "▤ show tasks":
        context.user_data["focus_tid"] = None 
        context.user_data["view_quad"] = None 
        await show_tasks(update, context)
        return
        
    await check_triage(uid, update, context)

    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    target_date, target_hour = today_str, now_ist.strftime("%H:00")
    is_success_reply = False

    if update.message.reply_to_message:
        mid = str(update.message.reply_to_message.message_id)
        if (update.message.reply_to_message.text or "").startswith("✓") or (update.message.reply_to_message.text or "").startswith("[✓]"): is_success_reply = True
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
        sent_msg = await update.message.reply_text(f"[✓] late log updated for {target_date} ({target_hour}).")
        header = fr"⤹ updated export \({target_date.replace('-', r'\-')}\)"
        await send_table_for_date(context, update.message.chat_id, uid, target_date, header)
    else:
        sent_msg = await update.message.reply_text(f"[✓] logged for {target_hour}.")

    db[uid]["msg_map"][str(sent_msg.message_id)] = {"date": target_date, "hour": target_hour, "is_success_reply": True}
    save_db()

# --- callback router ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = str(update.effective_chat.id)
    data = query.data
    
    # View Quadrant Isolator
    if data.startswith("vq_"):
        quad_key = data.split("_")[1]
        if quad_key == "back":
            context.user_data["view_quad"] = None
        else:
            context.user_data["view_quad"] = quad_key
        await show_tasks(update, context)
        return

    if data.startswith("focus_"):
        if data == "focus_back":
            context.user_data["focus_tid"] = None
        else:
            context.user_data["focus_tid"] = data.split("_")[1]
        await show_tasks(update, context)
        
    elif data.startswith("subdone_"):
        _, tid, subid = data.split("_", 2)
        for sub in db[uid]["tasks"][tid].get("subtasks", []):
            if sub["id"] == subid:
                sub["status"] = "completed"
        save_db()
        await query.answer(random.choice(CHEERS)) 
        await show_tasks(update, context) 
        
    elif data.startswith("done_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            db[uid]["tasks"][tid]["status"] = "completed"
            db[uid]["tasks"][tid]["is_doing"] = False
            save_db()
            await query.answer(random.choice(CHEERS)) 
            await show_tasks(update, context)
            
    elif data.startswith("toggled_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            db[uid]["tasks"][tid]["is_doing"] = not db[uid]["tasks"][tid].get("is_doing", False)
            save_db()
            await query.answer("doing toggled")
            await show_tasks(update, context)

    elif data.startswith("shift_"):
        tid = data.split("_")[1]
        if tid in db[uid]["tasks"]:
            kb = [
                [InlineKeyboardButton("☼ morn", callback_data=f"tr_{tid}_morning"),
                 InlineKeyboardButton("☁︎ aft", callback_data=f"tr_{tid}_afternoon"),
                 InlineKeyboardButton("☾ eve", callback_data=f"tr_{tid}_evening")],
                [InlineKeyboardButton("⋆｡°✩ night", callback_data=f"tr_{tid}_night"),
                 InlineKeyboardButton("✦ mid", callback_data=f"tr_{tid}_midnight")]
            ]
            await query.edit_message_text(f"shift where?\n`{db[uid]['tasks'][tid]['text'].lower()}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data.startswith("sa_"):
        if data == "sa_ignore":
            await query.edit_message_text("left tasks as is.")
        else:
            _, cur_q, next_q = data.split("_", 2)
            for tid, task in db[uid]["tasks"].items():
                if task["quadrant"] == cur_q and task["status"] == "active":
                    task["quadrant"] = next_q
            save_db()
            await query.edit_message_text(f"shifted pending to {QUADRANTS[next_q]}.")

    # Safety catch for old buttons
    elif data.startswith("sh_all_"):
        await query.answer("Old button!")
        await query.edit_message_text("this button has expired. please use the new ones.")

    elif data.startswith("tr_"):
        _, tid, action = data.split("_", 2)
        if tid in db[uid]["tasks"]:
            if action == "delete":
                task_text = db[uid]["tasks"][tid]["text"].lower()
                del db[uid]["tasks"][tid]
                save_db()
                await query.edit_message_text(f"⨯ deleted `{task_text}`", parse_mode="Markdown")
            elif action == "archived":
                db[uid]["archive"].append(db[uid]["tasks"][tid]["text"])
                del db[uid]["tasks"][tid]
                save_db()
                await query.edit_message_text("⨯ archived.", parse_mode="Markdown")
            elif action == "deferred":
                db[uid]["tasks"][tid]["status"] = "deferred"
                save_db()
                await query.edit_message_text("⤅ deferred to tomorrow.", parse_mode="Markdown")
            else:
                db[uid]["tasks"][tid]["quadrant"] = action
                db[uid]["tasks"][tid]["status"] = "active"
                save_db()
                await query.edit_message_text(f"moved to {action}")
             
    else:
        await query.answer()
        dummy_mid = f"manual_{data}_{datetime.now(IST).timestamp()}"
        db[uid]["msg_map"][dummy_mid] = {"date": datetime.now(IST).strftime("%Y-%m-%d"), "hour": data}
        db[uid]["last_ping"] = dummy_mid
        save_db()
        await query.edit_message_text(f"⌕ manual log for {data}\nsend 3 lines now (plan, actual, notes):")

# --- boring commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    init_user(str(update.effective_chat.id), context)
    text = (
        "✦ *obsidian logger & task bot*\n\n"
        "*time tracking (the 24h grid):*\n"
        "reply to any hourly ping with up to 3 lines:\n"
        "1. plan\n2. actual\n3. notes\n"
        "_tip: leave a line blank to keep your previous text. if you reply with just 1 line to a '[✓] logged' confirmation, it safely overwrites just the actual column._\n\n"
        "*task management (the 5 quadrants):*\n"
        "`/tasks` - show full dashboard (or tap ▤ show tasks).\n"
        "`/sleep` - trigger end-of-day routine.\n"
        "`/add [quadrant] [tasks...]` - batch add tasks.\n"
        "`/high [number]` - toggle ☯︎ priority flag.\n"
        "`/done [number]` - complete a task.\n"
        "`/rev [number]` - un-complete a task.\n"
        "`/del [number]` - delete a task.\n"
        "`/today [text]` - set today's focus.\n"
        "`/week [text]` - set weekly target.\n"
        "`/month [text]` - set monthly target.\n"
        "`/goals` - view today's, weekly, and monthly goals.\n"
        "`/archive` - view this month's archive.\n"
        "`/wakeup [optional_hour]` - force morning rollover.\n"
        "`/export` - print your current markdown table for obsidian.\n"
        "`/log` - manually select an hour to update.\n"
        "`/clear` - wipe today's hourly tracking data clean."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)

async def export_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    init_day(uid, today_str)
    await send_table_for_date(context, update.effective_chat.id, uid, today_str, rf"▤ current export \({today_str.replace('-', r'\-')}\)")

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = init_user(str(update.effective_chat.id), context)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    db[uid]["logs"][today_str] = {f"{i:02d}:00": {"plan": "", "actual": "", "notes": ""} for i in range(24)}
    save_db()
    await update.message.reply_text(f"⨯ wiped all data for {today_str}.")

async def show_grid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton(f"{i:02d}:00", callback_data=f"{i:02d}:00") for i in range(j, j + 4)] for j in range(0, 24, 4)]
    await update.message.reply_text("select hour to log:", reply_markup=InlineKeyboardMarkup(keyboard))

if __name__ == "__main__":
    app = Application.builder().token(os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")).request(HTTPXRequest(connect_timeout=30, read_timeout=30)).post_init(post_init).build()
    
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("export", export_table))
    app.add_handler(CommandHandler("clear", clear_data))
    app.add_handler(CommandHandler("log", show_grid))
    app.add_handler(CommandHandler("tasks", show_tasks))
    app.add_handler(CommandHandler("sleep", sleep_command))
    app.add_handler(CommandHandler("wakeup", wakeup_command))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("addsub", addsub_task))
    app.add_handler(CommandHandler("done", done_shortcut))
    app.add_handler(CommandHandler("rev", rev_task))
    app.add_handler(CommandHandler("high", high_priority))
    app.add_handler(CommandHandler("edit", edit_task))
    app.add_handler(CommandHandler("del", del_task))
    app.add_handler(CommandHandler("archive", view_archive))
    app.add_handler(CommandHandler("today", set_today))
    app.add_handler(CommandHandler("week", set_week))
    app.add_handler(CommandHandler("month", set_month))
    app.add_handler(CommandHandler("goals", view_goals))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
