# bot.py
# Host Nexa-style Telegram Python hosting bot
#
# Install:
#   pip install pyTelegramBotAPI
#
# Set your BotFather token:
#   Linux/macOS: export BOT_TOKEN="123:ABC..."
#   Windows:     set BOT_TOKEN=123:ABC...
#
# Set admin Telegram IDs (comma-separated):
#   export ADMIN_IDS="123456789,987654321"
#
# Then:
#   python bot.py
#
# IMPORTANT:
# This example runs uploaded Python files as real OS processes.
# Do NOT expose it to untrusted users on your main machine/server.
# For real public hosting, run user programs inside isolated containers/VMs
# with CPU, RAM, disk, network and process limits.

import os
import re
import html
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8871738513:AAFY7JgAecIcJS9VCIYUC1AMOOccZpUgyfI")

# Example: ADMIN_IDS="123456789,987654321"
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "8660772312").split(",")
    if x.strip().isdigit()
}

DB_FILE = "hostnexa.db"
DATA_DIR = Path("hosted_files")
DATA_DIR.mkdir(exist_ok=True)

TRIAL_DAYS = 2
DEFAULT_FILE_LIMIT = 1
MAX_UPLOAD_MB = 2
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Change these plans/prices whenever you want.
PLANS = {
    "starter": {"name": "🚀 Starter", "days": 7, "file_limit": 1, "price": 50},
    "pro": {"name": "⚡ Pro", "days": 30, "file_limit": 3, "price": 120},
    "premium": {"name": "👑 Premium", "days": 90, "file_limit": 5, "price": 300},
}
PAYMENT_NUMBER = os.getenv("PAYMENT_NUMBER", "01964431970")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@SHUVODIP_BRO")

# Uploaded scripts are started with the same Python interpreter running this bot.
PYTHON_BIN = os.getenv("PYTHON_BIN", os.sys.executable)


# =========================
# BOT
# =========================

if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise SystemExit("Set BOT_TOKEN first.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Runtime process registry: {user_id: {filename: Popen}}
processes = {}
process_lock = threading.Lock()


# =========================
# DATABASE
# =========================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            plan TEXT DEFAULT 'Trial',
            expires_at TEXT NOT NULL,
            file_limit INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            banned INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, filename)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS plan_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            txid TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
    """)

    # Old versions had a free Trial. New SHUVO X HOST requires plan purchase.
    cur.execute("""
        UPDATE users
        SET plan='No Plan', file_limit=0, expires_at=?
        WHERE plan='Trial'
    """, (iso(now_utc()),))

    conn.commit()
    conn.close()


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return now_utc()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row


def create_user(tg_user):
    user_id = tg_user.id
    existing = get_user(user_id)

    if existing:
        conn = db()
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (tg_user.username or "", tg_user.first_name or "", user_id)
        )
        conn.commit()
        conn.close()
        return get_user(user_id)

    expires = now_utc()

    conn = db()
    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name, plan, expires_at,
         file_limit, created_at, banned)
        VALUES (?, ?, ?, 'No Plan', ?, 0, ?, 0)
    """, (
        user_id,
        tg_user.username or "",
        tg_user.first_name or "",
        iso(expires),
        iso(now_utc())
    ))
    conn.commit()
    conn.close()

    user_dir(user_id)
    return get_user(user_id)


def user_dir(user_id):
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_expired(row):
    return now_utc() >= parse_iso(row["expires_at"])


def access_ok(user_id):
    row = get_user(user_id)
    if not row:
        return False, "Account not found. Send /start first."

    if row["banned"]:
        return False, "🚫 Your account is banned."

    if is_admin(user_id):
        return True, ""

    if row["plan"] in ("No Plan", "Expired", "") or row["file_limit"] <= 0 or is_expired(row):
        return False, "💎 Hosting করতে আগে একটি Plan কিনে Admin approval নিতে হবে।"

    return True, ""


def remaining_text(row):
    if is_admin(row["user_id"]):
        return "♾️ Admin"

    if is_expired(row):
        return "Expired"

    seconds = int((parse_iso(row["expires_at"]) - now_utc()).total_seconds())
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 0)}m"


# =========================
# FILE HELPERS
# =========================

def safe_filename(name):
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)

    if not name.lower().endswith(".py"):
        return None

    if name in ("", ".", ".."):
        return None

    return name[:120]


def user_files(user_id):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM files WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_file(user_id, filename):
    conn = db()
    row = conn.execute(
        "SELECT * FROM files WHERE user_id=? AND filename=?",
        (user_id, filename)
    ).fetchone()
    conn.close()
    return row


def is_running(user_id, filename):
    with process_lock:
        proc = processes.get(user_id, {}).get(filename)

    if not proc:
        return False

    if proc.poll() is None:
        return True

    with process_lock:
        processes.get(user_id, {}).pop(filename, None)

    return False


def stop_process(user_id, filename):
    with process_lock:
        proc = processes.get(user_id, {}).get(filename)

    if not proc:
        return False, "Process is not running."

    if proc.poll() is not None:
        with process_lock:
            processes.get(user_id, {}).pop(filename, None)
        return False, "Process has already stopped."

    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

        with process_lock:
            processes.get(user_id, {}).pop(filename, None)

        return True, "Stopped."
    except Exception as e:
        return False, str(e)


def start_process(user_id, filename):
    row = get_file(user_id, filename)

    if not row:
        return False, "File not found."

    if is_running(user_id, filename):
        return False, "Already running."

    path = Path(row["path"]).resolve()

    if not path.exists():
        return False, "File is missing from disk."

    # One process per file. For public hosting, replace this with a container.
    try:
        creationflags = 0
        kwargs = {
            "cwd": str(path.parent),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": os.environ.copy(),
        }

        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [PYTHON_BIN, "-u", str(path)],
            **kwargs
        )

        with process_lock:
            processes.setdefault(user_id, {})[filename] = proc

        return True, f"PID: {proc.pid}"

    except Exception as e:
        return False, str(e)


def delete_file(user_id, filename):
    row = get_file(user_id, filename)
    if not row:
        return False, "File not found."

    if is_running(user_id, filename):
        stop_process(user_id, filename)

    try:
        Path(row["path"]).unlink(missing_ok=True)
    except Exception:
        pass

    conn = db()
    conn.execute(
        "DELETE FROM files WHERE user_id=? AND filename=?",
        (user_id, filename)
    )
    conn.commit()
    conn.close()

    return True, "Deleted."


# =========================
# KEYBOARDS
# =========================

def main_keyboard(user_id):
    # Reply keyboard: matches the screenshot layout and stays at the bottom
    # of the Telegram chat instead of appearing under the bot message.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(types.KeyboardButton("🟢 📤 Upload File"))
    kb.row(
        types.KeyboardButton("🔵 📂 My Files"),
        types.KeyboardButton("🟣 💎 Plans"),
    )
    kb.row(
        types.KeyboardButton("🟡 ⚡ Latency"),
        types.KeyboardButton("🟠 📊 Statistics"),
    )
    kb.row(
        types.KeyboardButton("🔷 👤 Account"),
        types.KeyboardButton("🩷 🎬 Tutorial"),
    )
    kb.row(types.KeyboardButton("🟢 💬 Support"))
    if is_admin(user_id):
        kb.row(types.KeyboardButton("🔴 🛠 Admin Panel"))
    return kb


def back_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="home"))
    return kb


def file_keyboard(filename):
    kb = types.InlineKeyboardMarkup(row_width=2)

    if is_running(current_callback_user_id(), filename):
        kb.add(types.InlineKeyboardButton(
            "⏹ Stop", callback_data=f"stop:{filename}"
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "▶️ Start", callback_data=f"start:{filename}"
        ))

    kb.row(
        types.InlineKeyboardButton(
            "📊 Status", callback_data=f"status:{filename}"
        ),
        types.InlineKeyboardButton(
            "🗑 Delete", callback_data=f"delete:{filename}"
        ),
    )
    kb.add(types.InlineKeyboardButton("🔙 Files", callback_data="files"))
    return kb


# Callback helper. We store the user ID temporarily in thread-local storage.
callback_local = threading.local()

def current_callback_user_id():
    return getattr(callback_local, "user_id", 0)


def home_text(user):
    files = user_files(user["user_id"])
    running = sum(1 for f in files if is_running(user["user_id"], f["filename"]))

    if is_admin(user["user_id"]):
        status = "♾️ <b>Admin • Unlimited</b>"
        run_limit = "∞"
    elif user["plan"] not in ("No Plan", "Expired", "") and parse_iso(user["expires_at"]) and parse_iso(user["expires_at"]) > now_utc():
        status = f"💎 <b>{html.escape(user['plan'])}</b> • {remaining_text(user)}"
        run_limit = str(user["file_limit"])
    else:
        status = "🔒 <b>No active plan</b>"
        run_limit = "0"

    return f"""✨ <b>WELCOME TO SHUVO X HOST</b> ✨
━━━━━━━━━━━━━━━━━━━━━━━━

👋 হ্যালো, <b>{html.escape(user['first_name'] or 'User')}!</b>
🆔 User ID: <code>{user['user_id']}</code>
🔰 Status: {status}
📁 Running Files: <b>{running}/{run_limit}</b>

━━━━━━━━━━━━━━━━━━━━━━━━
💡 <b>এই বট দিয়ে আপনি:</b>

📤 আপনার Python (.py) ফাইল Upload করতে পারবেন
⚙️ আপনার ফাইল Server-এ চালু রাখতে পারবেন
📊 ফাইলের Status দেখতে পারবেন
⛔ যেকোনো সময় ফাইল Stop করতে পারবেন
🗑️ প্রয়োজন হলে ফাইল Delete করতে পারবেন

💎 <b>Hosting করতে হলে আগে একটি Plan কিনুন।</b>
━━━━━━━━━━━━━━━━━━━━━━━━"""


# =========================
# START
# =========================

@bot.message_handler(commands=["start"])
def start_cmd(message):
    user = create_user(message.from_user)

    if user["banned"]:
        bot.reply_to(message, "🚫 Your account is banned.")
        return

    bot.send_message(
        message.chat.id,
        home_text(user),
        reply_markup=main_keyboard(message.from_user.id)
    )


# =========================
# UPLOAD
# =========================

@bot.callback_query_handler(func=lambda c: c.data == "upload")
def upload_cb(call):
    callback_local.user_id = call.from_user.id

    ok, reason = access_ok(call.from_user.id)
    if not ok:
        bot.answer_callback_query(call.id, reason, show_alert=True)
        return

    user = get_user(call.from_user.id)
    files = user_files(call.from_user.id)

    if len(files) >= user["file_limit"]:
        bot.answer_callback_query(
            call.id,
            f"File limit reached: {user['file_limit']}",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📤 <b>Upload File</b>\n\n"
        "Send me your <code>.py</code> file.\n"
        f"Maximum size: <b>{MAX_UPLOAD_MB} MB</b>\n\n"
        "⚠️ Only upload code you trust.",
        reply_markup=back_keyboard()
    )


@bot.message_handler(content_types=["document"])
def document_handler(message):
    user = create_user(message.from_user)

    ok, reason = access_ok(message.from_user.id)
    if not ok:
        bot.reply_to(message, reason)
        return

    document = message.document
    filename = safe_filename(document.file_name or "")

    if not filename:
        bot.reply_to(message, "❌ Only <b>.py</b> files are allowed.")
        return

    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        bot.reply_to(
            message,
            f"❌ File too large. Maximum is {MAX_UPLOAD_MB} MB."
        )
        return

    files = user_files(message.from_user.id)

    if len(files) >= user["file_limit"] and not get_file(
        message.from_user.id, filename
    ):
        bot.reply_to(
            message,
            f"❌ File limit reached: {user['file_limit']}."
        )
        return

    bot.send_chat_action(message.chat.id, "upload_document")

    try:
        file_info = bot.get_file(document.file_id)
        data = bot.download_file(file_info.file_path)

        path = user_dir(message.from_user.id) / filename
        path.write_bytes(data)

        conn = db()
        conn.execute("""
            INSERT INTO files(user_id, filename, path, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, filename)
            DO UPDATE SET path=excluded.path
        """, (
            message.from_user.id,
            filename,
            str(path),
            iso(now_utc())
        ))
        conn.commit()
        conn.close()

        bot.send_message(
            message.chat.id,
            f"✅ <b>{html.escape(filename)}</b> uploaded successfully!\n\n"
            "Use 📂 My Files to start it.",
            reply_markup=main_keyboard(message.from_user.id)
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Upload failed:\n<code>{html.escape(str(e))}</code>")


# =========================
# CALLBACKS
# =========================

@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    callback_local.user_id = call.from_user.id

    user = create_user(call.from_user)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data

    if data == "home":
        bot.edit_message_text(
            home_text(user),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_keyboard(call.from_user.id)
        )
        return

    if data == "upload":
        upload_cb(call)
        return

    if data == "files":
        show_files(call)
        return

    if data == "plans":
        show_plans(call)
        return

    if data.startswith("buy:"):
        key = data.split(":", 1)[1]
        if key not in PLANS:
            edit(call, "❌ Plan not found.", back_keyboard()); return
        if pending_request(call.from_user.id):
            edit(call, "⏳ আপনার একটি plan request pending আছে।", back_keyboard()); return
        p = PLANS[key]
        edit(call, f"🛒 <b>{p['name']}</b>\n\n💰 Price: <b>৳{p['price']}</b>\n💳 Payment-Bkash/Nagad: <code>{html.escape(os.getenv('PAYMENT_NUMBER','01964431970'))}</code>\n\nPayment করার পর আপনার <b>Transaction ID</b> পাঠান।", back_keyboard())
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, lambda m, k=key: receive_txid(m, k))
        return

    if data == "admin:requests":
        if is_admin(call.from_user.id): admin_requests(call)
        return

    if data.startswith("approve:"):
        if is_admin(call.from_user.id): approve_request(call, int(data.split(":")[1]))
        return

    if data.startswith("reject:"):
        if is_admin(call.from_user.id): reject_request(call, int(data.split(":")[1]))
        return

    if data == "latency":
        start = time.perf_counter()
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        latency = round((time.perf_counter() - start) * 1000, 2)
        edit(
            call,
            f"⚡ <b>Latency</b>\n\nTelegram response check: <b>{latency} ms</b>",
            back_keyboard()
        )
        return

    if data == "stats":
        show_stats(call)
        return

    if data == "account":
        show_account(call)
        return

    if data == "tutorial":
        edit(
            call,
            "🎬 <b>Tutorial</b>\n\n"
            "1️⃣ Tap 📤 Upload File\n"
            "2️⃣ Send your .py file\n"
            "3️⃣ Open 📂 My Files\n"
            "4️⃣ Tap ▶️ Start\n"
            "5️⃣ Use ⏹ Stop when needed\n"
            "6️⃣ Use 🗑 Delete to remove it\n\n"
            "⚠️ Uploaded Python code runs on the hosting server.",
            back_keyboard()
        )
        return

    if data == "support":
        edit(
            call,
            "💬 <b>Support</b>\n\n"
            "Contact the bot administrator for support.\n\n"
            "Admin can configure this text inside bot.py.",
            back_keyboard()
        )
        return

    if data == "admin":
        if not is_admin(call.from_user.id):
            return
        show_admin(call)
        return

    if data.startswith("start:"):
        filename = data.split(":", 1)[1]
        handle_start(call, filename)
        return

    if data.startswith("stop:"):
        filename = data.split(":", 1)[1]
        handle_stop(call, filename)
        return

    if data.startswith("status:"):
        filename = data.split(":", 1)[1]
        handle_status(call, filename)
        return

    if data.startswith("delete:"):
        filename = data.split(":", 1)[1]
        confirm_delete(call, filename)
        return

    if data.startswith("confirmdelete:"):
        filename = data.split(":", 1)[1]
        ok, msg = delete_file(call.from_user.id, filename)
        edit(
            call,
            ("✅ " if ok else "❌ ") + html.escape(msg),
            back_keyboard()
        )
        return

    if data.startswith("admin:"):
        if not is_admin(call.from_user.id):
            return
        admin_action(call, data)
        return


def edit(call, text, markup=None):
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


def show_files(call):
    ok, reason = access_ok(call.from_user.id)
    if not ok:
        edit(call, "❌ " + reason, back_keyboard())
        return

    files = user_files(call.from_user.id)

    if not files:
        edit(
            call,
            "📂 <b>My Files</b>\n\nNo files uploaded yet.\n\n"
            "Tap 📤 Upload File to add one.",
            main_keyboard(call.from_user.id)
        )
        return

    text = "📂 <b>My Files</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)

    for f in files:
        state = "🟢 Running" if is_running(
            call.from_user.id, f["filename"]
        ) else "🔴 Stopped"

        text += f"• <code>{html.escape(f['filename'])}</code> — {state}\n"
        kb.add(types.InlineKeyboardButton(
            f"⚙️ {f['filename']}",
            callback_data=f"status:{f['filename']}"
        ))

    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="home"))
    edit(call, text, kb)


def handle_start(call, filename):
    ok, reason = access_ok(call.from_user.id)
    if not ok:
        edit(call, "❌ " + reason, back_keyboard())
        return

    # Enforce running-process limit.
    user = get_user(call.from_user.id)
    running = sum(
        1 for f in user_files(call.from_user.id)
        if is_running(call.from_user.id, f["filename"])
    )

    if running >= user["file_limit"] and not is_running(
        call.from_user.id, filename
    ):
        edit(
            call,
            f"❌ Running process limit reached: <b>{user['file_limit']}</b>",
            back_keyboard()
        )
        return

    success, msg = start_process(call.from_user.id, filename)

    edit(
        call,
        f"{'✅ Started' if success else '❌ Start failed'}\n\n"
        f"📄 <code>{html.escape(filename)}</code>\n"
        f"ℹ️ {html.escape(msg)}",
        back_keyboard()
    )


def handle_stop(call, filename):
    success, msg = stop_process(call.from_user.id, filename)

    edit(
        call,
        f"{'✅ Stopped' if success else '❌ Stop'}\n\n"
        f"📄 <code>{html.escape(filename)}</code>\n"
        f"ℹ️ {html.escape(msg)}",
        back_keyboard()
    )


def handle_status(call, filename):
    row = get_file(call.from_user.id, filename)

    if not row:
        edit(call, "❌ File not found.", back_keyboard())
        return

    running = is_running(call.from_user.id, filename)

    if running:
        with process_lock:
            proc = processes[call.from_user.id][filename]
        state = f"🟢 Running (PID {proc.pid})"
    else:
        state = "🔴 Stopped"

    text = (
        "📊 <b>File Status</b>\n\n"
        f"📄 File: <code>{html.escape(filename)}</code>\n"
        f"📌 Status: {state}\n"
        f"📅 Uploaded: <code>{html.escape(row['created_at'])}</code>"
    )

    edit(call, text, file_keyboard(filename))


def confirm_delete(call, filename):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            "✅ Yes, Delete",
            callback_data=f"confirmdelete:{filename}"
        ),
        types.InlineKeyboardButton(
            "❌ Cancel",
            callback_data="files"
        )
    )

    edit(
        call,
        f"⚠️ Delete <code>{html.escape(filename)}</code>?",
        kb
    )


def show_plans(call):
    text = "💎 <b>SHUVO X HOST — HOSTING PLANS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, p in PLANS.items():
        text += (
            f"{p['name']}\n"
            f"⏳ {p['days']} Days • 📁 {p['file_limit']} File\n"
            f"💰 Price: <b>৳{p['price']}</b>\n\n"
        )
        kb.add(types.InlineKeyboardButton(
            f"🛒 Buy {p['name']} • ৳{p['price']}", callback_data=f"buy:{key}"
        ))
    text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛒 Plan কিনে Transaction ID দিলে request Admin-এর কাছে যাবে।\n"
        "✅ Admin approve করলে hosting access চালু হবে।"
    )
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="home"))
    edit(call, text, kb)


def pending_request(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM plan_requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()
    return row


def receive_txid(message, plan_key):
    user = create_user(message.from_user)
    if message.content_type != "text":
        bot.reply_to(message, "❌ Transaction ID text হিসেবে পাঠান।")
        return
    txid = message.text.strip()[:100]
    if len(txid) < 3:
        bot.reply_to(message, "❌ Valid Transaction ID দিন।")
        return
    if pending_request(user["user_id"]):
        bot.reply_to(message, "⏳ আপনার একটি request already pending আছে।")
        return
    conn = db()
    cur = conn.execute("INSERT INTO plan_requests(user_id, plan_key, txid, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (user["user_id"], plan_key, txid, iso(now_utc())))
    rid = cur.lastrowid
    conn.commit(); conn.close()
    p = PLANS[plan_key]
    bot.send_message(message.chat.id, f"✅ <b>Purchase Request Sent!</b>\n\n💎 Plan: {p['name']}\n💰 Amount: ৳{p['price']}\n🧾 Request: <code>#{rid}</code>\n🔑 TXID: <code>{html.escape(txid)}</code>\n\n⏳ Admin approve করলে hosting চালু হবে।", reply_markup=main_keyboard(message.from_user.id))
    for aid in ADMIN_IDS:
        try:
            kb=types.InlineKeyboardMarkup(row_width=2)
            kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{rid}"), types.InlineKeyboardButton("❌ Reject", callback_data=f"reject:{rid}"))
            bot.send_message(aid, f"🔔 <b>NEW PLAN REQUEST</b>\n\n🧾 #{rid}\n👤 {html.escape(user['first_name'] or 'User')}\n🆔 <code>{user['user_id']}</code>\n💎 {p['name']}\n💰 ৳{p['price']}\n🔑 TXID: <code>{html.escape(txid)}</code>", reply_markup=kb)
        except Exception:
            pass


def approve_request(call, rid):
    conn=db(); req=conn.execute("SELECT * FROM plan_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
    if not req: conn.close(); return
    p=PLANS.get(req['plan_key']);
    if not p: conn.close(); return
    expires=now_utc()+timedelta(days=p['days'])
    conn.execute("UPDATE plan_requests SET status='approved', reviewed_at=? WHERE id=?", (iso(now_utc()),rid))
    conn.execute("UPDATE users SET plan=?, expires_at=?, file_limit=? WHERE user_id=?", (p['name'],iso(expires),p['file_limit'],req['user_id']))
    conn.commit(); conn.close()
    bot.send_message(call.message.chat.id, f"✅ Request <b>#{rid}</b> approved.")
    try: bot.send_message(req['user_id'], f"🎉 <b>PLAN APPROVED!</b>\n\n💎 {p['name']}\n⏳ {p['days']} Days\n📁 {p['file_limit']} File\n\n📤 এখন Upload File করতে পারবেন।", reply_markup=main_keyboard(req['user_id']))
    except Exception: pass


def reject_request(call, rid):
    conn=db(); req=conn.execute("SELECT * FROM plan_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
    if not req: conn.close(); return
    conn.execute("UPDATE plan_requests SET status='rejected', reviewed_at=? WHERE id=?", (iso(now_utc()),rid)); conn.commit(); conn.close()
    bot.send_message(call.message.chat.id, f"❌ Request <b>#{rid}</b> rejected.")
    try: bot.send_message(req['user_id'], "❌ আপনার plan request reject করা হয়েছে। Support-এ যোগাযোগ করুন।", reply_markup=main_keyboard(req['user_id']))
    except Exception: pass


def admin_requests(call):
    conn=db(); rows=conn.execute("SELECT r.*,u.first_name FROM plan_requests r JOIN users u ON u.user_id=r.user_id WHERE r.status='pending' ORDER BY r.id DESC LIMIT 20").fetchall(); conn.close()
    if not rows:
        edit(call,"🧾 <b>PURCHASE REQUESTS</b>\n\nকোনো pending request নেই।",back_keyboard()); return
    for r in rows:
        p=PLANS.get(r['plan_key']); kb=types.InlineKeyboardMarkup(row_width=2)
        kb.row(types.InlineKeyboardButton("✅ Approve",callback_data=f"approve:{r['id']}"),types.InlineKeyboardButton("❌ Reject",callback_data=f"reject:{r['id']}"))
        bot.send_message(call.message.chat.id,f"🧾 <b>Request #{r['id']}</b>\n👤 {html.escape(r['first_name'] or 'User')}\n🆔 <code>{r['user_id']}</code>\n💎 {p['name']}\n💰 ৳{p['price']}\n🔑 TXID: <code>{html.escape(r['txid'])}</code>",reply_markup=kb)
    edit(call,"🧾 <b>PURCHASE REQUESTS</b>\n\nউপরের request-গুলো Approve/Reject করুন।",back_keyboard())


def show_stats(call):
    files = user_files(call.from_user.id)
    running = sum(
        1 for f in files if is_running(call.from_user.id, f["filename"])
    )

    edit(
        call,
        "📊 <b>Statistics</b>\n\n"
        f"📁 Total files: <b>{len(files)}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
        f"🔴 Stopped: <b>{len(files) - running}</b>",
        back_keyboard()
    )


def show_account(call):
    user = get_user(call.from_user.id)

    username = (
        "@" + html.escape(user["username"])
        if user["username"] else "Not set"
    )

    edit(
        call,
        "👤 <b>Account</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"👤 Username: {username}\n"
        f"💎 Plan: <b>{html.escape(user['plan'])}</b>\n"
        f"⏳ Remaining: <b>{remaining_text(user)}</b>\n"
        f"📁 File limit: <b>{user['file_limit']}</b>",
        back_keyboard()
    )


# =========================
# ADMIN PANEL
# =========================

def show_admin(call):
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = conn.execute(
        "SELECT COUNT(*) FROM users WHERE banned=1"
    ).fetchone()[0]
    conn.close()

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("🧾 Purchase Requests", callback_data="admin:requests"),
        types.InlineKeyboardButton("👥 Users", callback_data="admin:users"),
    )
    kb.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="admin:stats"),
        types.InlineKeyboardButton("💎 Plans", callback_data="admin:plans"),
    )
    kb.row(
        types.InlineKeyboardButton("➕ Grant", callback_data="admin:granthelp"),
        types.InlineKeyboardButton("🚫 Ban", callback_data="admin:banhelp"),
    )
    kb.add(types.InlineKeyboardButton("♻️ Unban", callback_data="admin:unbanhelp"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="home"))

    edit(
        call,
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{total}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n\n"
        "Use the buttons below or admin commands.",
        kb
    )


def admin_action(call, data):
    action = data.split(":", 1)[1]

    if action == "users":
        conn = db()
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()

        if not rows:
            text = "👥 No users."
        else:
            lines = ["👥 <b>Users</b>\n"]
            for u in rows:
                state = "🚫" if u["banned"] else "✅"
                lines.append(
                    f"{state} <code>{u['user_id']}</code> "
                    f"{html.escape(u['first_name'] or '')} "
                    f"— {html.escape(u['plan'])} "
                    f"— {remaining_text(u)}"
                )
            text = "\n".join(lines)

        edit(call, text, back_keyboard())
        return

    if action == "stats":
        conn = db()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        trials = conn.execute(
            "SELECT COUNT(*) FROM users WHERE plan='Trial'"
        ).fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()

        edit(
            call,
            "📊 <b>Admin Statistics</b>\n\n"
            f"👥 Users: <b>{total}</b>\n"
            f"🆓 Trial users: <b>{trials}</b>\n"
            f"📁 Uploaded files: <b>{files}</b>",
            back_keyboard()
        )
        return

    if action == "granthelp":
        edit(
            call,
            "➕ <b>Grant Plan</b>\n\n"
            "Use:\n"
            "<code>/grant USER_ID DAYS FILE_LIMIT</code>\n\n"
            "Example:\n"
            "<code>/grant 123456789 30 2</code>",
            back_keyboard()
        )
        return

    if action == "banhelp":
        edit(
            call,
            "🚫 <b>Ban User</b>\n\n"
            "Use:\n"
            "<code>/ban USER_ID</code>",
            back_keyboard()
        )
        return

    if action == "unbanhelp":
        edit(
            call,
            "♻️ <b>Unban User</b>\n\n"
            "Use:\n"
            "<code>/unban USER_ID</code>",
            back_keyboard()
        )
        return

    if action == "plans":
        edit(
            call,
            "💎 <b>Plan Commands</b>\n\n"
            "<code>/grant USER_ID DAYS FILE_LIMIT</code>\n"
            "<code>/revoke USER_ID</code>\n"
            "<code>/limit USER_ID FILE_LIMIT</code>",
            back_keyboard()
        )
        return


# =========================
# ADMIN COMMANDS
# =========================

def admin_only(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "🚫 Admin only.")
        return False
    return True


@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if not admin_only(message):
        return

    user = create_user(message.from_user)

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🛠 Open Admin Panel", callback_data="admin"
    ))

    bot.reply_to(
        message,
        "🛠 <b>Admin Panel</b>",
        reply_markup=kb
    )


@bot.message_handler(commands=["users"])
def users_cmd(message):
    if not admin_only(message):
        return

    conn = db()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()

    if not rows:
        bot.reply_to(message, "No users.")
        return

    text = "👥 <b>Users</b>\n\n"
    for u in rows:
        text += (
            f"• <code>{u['user_id']}</code> "
            f"{html.escape(u['first_name'] or '')} — "
            f"{html.escape(u['plan'])} — "
            f"{remaining_text(u)}\n"
        )

    bot.reply_to(message, text)


@bot.message_handler(commands=["grant"])
def grant_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 4:
        bot.reply_to(
            message,
            "Usage:\n<code>/grant USER_ID DAYS FILE_LIMIT</code>"
        )
        return

    try:
        user_id = int(parts[1])
        days = int(parts[2])
        limit = int(parts[3])

        if days < 1 or limit < 1:
            raise ValueError

    except ValueError:
        bot.reply_to(message, "❌ Invalid values.")
        return

    target = get_user(user_id)

    if not target:
        bot.reply_to(message, "❌ User not found. They must /start first.")
        return

    expires = now_utc() + timedelta(days=days)

    conn = db()
    conn.execute("""
        UPDATE users
        SET plan='Premium', expires_at=?, file_limit=?, banned=0
        WHERE user_id=?
    """, (iso(expires), limit, user_id))
    conn.commit()
    conn.close()

    bot.reply_to(
        message,
        f"✅ Granted Premium to <code>{user_id}</code>\n"
        f"⏳ {days} days\n"
        f"📁 Limit: {limit}"
    )

    try:
        bot.send_message(
            user_id,
            f"🎉 <b>Your plan was upgraded!</b>\n\n"
            f"💎 Premium\n"
            f"⏳ {days} days\n"
            f"📁 File limit: {limit}"
        )
    except Exception:
        pass


@bot.message_handler(commands=["revoke"])
def revoke_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/revoke USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    expires = now_utc()

    conn = db()
    conn.execute(
        "UPDATE users SET plan='Expired', expires_at=?, file_limit=0 WHERE user_id=?",
        (iso(expires), user_id)
    )
    conn.commit()
    conn.close()

    # Stop all user processes.
    for f in user_files(user_id):
        stop_process(user_id, f["filename"])

    bot.reply_to(message, f"✅ Access revoked for <code>{user_id}</code>")


@bot.message_handler(commands=["limit"])
def limit_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: <code>/limit USER_ID FILE_LIMIT</code>"
        )
        return

    try:
        user_id = int(parts[1])
        limit = int(parts[2])

        if limit < 1:
            raise ValueError

    except ValueError:
        bot.reply_to(message, "❌ Invalid values.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET file_limit=? WHERE user_id=?",
        (limit, user_id)
    )
    conn.commit()
    conn.close()

    bot.reply_to(
        message,
        f"✅ File/process limit updated to <b>{limit}</b> "
        f"for <code>{user_id}</code>"
    )


@bot.message_handler(commands=["ban"])
def ban_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/ban USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET banned=1 WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()

    for f in user_files(user_id):
        stop_process(user_id, f["filename"])

    bot.reply_to(message, f"🚫 Banned <code>{user_id}</code>")


@bot.message_handler(commands=["unban"])
def unban_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/unban USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET banned=0 WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()

    bot.reply_to(message, f"♻️ Unbanned <code>{user_id}</code>")


# =========================
# CLEANUP
# =========================

def cleanup_expired_users():
    while True:
        try:
            conn = db()
            rows = conn.execute("""
                SELECT * FROM users
                WHERE banned=0 AND plan != 'Expired'
            """).fetchall()
            conn.close()

            for user in rows:
                if is_admin(user["user_id"]):
                    continue

                if is_expired(user):
                    for f in user_files(user["user_id"]):
                        stop_process(user["user_id"], f["filename"])

                    conn = db()
                    conn.execute(
                        "UPDATE users SET plan='Expired', file_limit=0 WHERE user_id=?",
                        (user["user_id"],)
                    )
                    conn.commit()
                    conn.close()

        except Exception as e:
            print("Cleanup error:", e)

        time.sleep(60)


# =========================
# REPLY KEYBOARD BUTTONS
# =========================

@bot.message_handler(func=lambda m: m.text in {
    "🟢 📤 Upload File", "🔵 📂 My Files", "🟣 💎 Plans",
    "🟡 ⚡ Latency", "🟠 📊 Statistics", "🔷 👤 Account",
    "🩷 🎬 Tutorial", "🟢 💬 Support", "🔴 🛠 Admin Panel"
})
def reply_keyboard_buttons(message):
    user = create_user(message.from_user)
    text = message.text

    if text == "🟢 📤 Upload File":
        ok, reason = access_ok(message.from_user.id)
        if not ok:
            bot.send_message(message.chat.id, "❌ " + reason, reply_markup=main_keyboard(message.from_user.id))
            return
        bot.send_message(
            message.chat.id,
            "📤 <b>Upload File</b>\n\n"
            "Send your Python <code>.py</code> file here.\n"
            f"Maximum size: <b>{MAX_UPLOAD_MB} MB</b>",
            reply_markup=main_keyboard(message.from_user.id)
        )
        return

    if text == "🔵 📂 My Files":
        files = user_files(message.from_user.id)
        if not files:
            bot.send_message(message.chat.id, "📂 <b>My Files</b>\n\nNo files uploaded yet.", reply_markup=main_keyboard(message.from_user.id))
            return
        msg = "📂 <b>My Files</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for f in files:
            state = "🟢 Running" if is_running(message.from_user.id, f["filename"]) else "🔴 Stopped"
            msg += f"• <code>{html.escape(f['filename'])}</code> — {state}\n"
            kb.add(types.InlineKeyboardButton(f"⚙️ {f['filename']}", callback_data=f"status:{f['filename']}"))
        bot.send_message(message.chat.id, msg, reply_markup=kb)
        return

    if text == "🟣 💎 Plans":
        msg = "💎 <b>SHUVO X HOST — HOSTING PLANS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            msg += f"{p['name']}\n⏳ {p['days']} Days • 📁 {p['file_limit']} File\n💰 Price: <b>৳{p['price']}</b>\n\n"
            kb.add(types.InlineKeyboardButton(f"🛒 Buy {p['name']} • ৳{p['price']}", callback_data=f"buy:{key}"))
        msg += "━━━━━━━━━━━━━━━━━━━━\n🛒 Plan কিনে Transaction ID দিলে request Admin-এর কাছে যাবে।\n✅ Admin approve করলে hosting access চালু হবে।"
        bot.send_message(message.chat.id, msg, reply_markup=kb)
        return

    if text == "🟡 ⚡ Latency":
        start = time.perf_counter()
        latency = round((time.perf_counter() - start) * 1000, 2)
        bot.send_message(message.chat.id, f"⚡ <b>Latency</b>\n\nTelegram response check: <b>{latency} ms</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🟠 📊 Statistics":
        files = user_files(message.from_user.id)
        running = sum(1 for f in files if is_running(message.from_user.id, f["filename"]))
        bot.send_message(message.chat.id, f"📊 <b>Statistics</b>\n\n📁 Total files: <b>{len(files)}</b>\n🟢 Running: <b>{running}</b>\n🔴 Stopped: <b>{len(files)-running}</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🔷 👤 Account":
        username = "@" + html.escape(user["username"]) if user["username"] else "Not set"
        bot.send_message(message.chat.id, f"👤 <b>Account</b>\n\n🆔 ID: <code>{user['user_id']}</code>\n👤 Username: {username}\n💎 Plan: <b>{html.escape(user['plan'])}</b>\n⏳ Remaining: <b>{remaining_text(user)}</b>\n📁 File limit: <b>{'∞' if is_admin(user['user_id']) else user['file_limit']}</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🩷 🎬 Tutorial":
        bot.send_message(message.chat.id, "🎬 <b>Tutorial</b>\n\n1️⃣ 💎 Plans থেকে একটি plan নিন\n2️⃣ Payment করে TXID submit করুন\n3️⃣ Admin approve করলে hosting চালু হবে\n4️⃣ 📤 Upload File চাপুন\n5️⃣ 📂 My Files → ▶️ Start\n6️⃣ ⏹ Stop / 🗑 Delete ব্যবহার করুন", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🟢 💬 Support":
        bot.send_message(message.chat.id, f"💬 <b>Support</b>\n\nSupport: {html.escape(SUPPORT_USERNAME)}", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🔴 🛠 Admin Panel":
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "🚫 Admin only.", reply_markup=main_keyboard(message.from_user.id))
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(types.InlineKeyboardButton("👥 Users", callback_data="admin:users"), types.InlineKeyboardButton("📊 Stats", callback_data="admin:stats"))
        kb.row(types.InlineKeyboardButton("🧾 Requests", callback_data="admin:requests"), types.InlineKeyboardButton("💎 Plans", callback_data="admin:plans"))
        bot.send_message(message.chat.id, "🛠 <b>SHUVO X HOST — ADMIN PANEL</b>\n\nManage users, plan requests and hosting access.", reply_markup=kb)


# =========================
# ERROR HANDLER
# =========================

@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    if message.text.startswith("/"):
        bot.reply_to(
            message,
            "❓ Unknown command.\nUse /start."
        )


# =========================
# RUN
# =========================

if __name__ == "__main__":
    init_db()

    threading.Thread(
        target=cleanup_expired_users,
        daemon=True
    ).start()

    print("====================================")
    print(" Host Nexa Bot is starting...")
    print("====================================")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )
        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)
