#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Premium Emoji Bot - SINGLE FILE / NO EXTERNAL MODULES
Runs on Termux with only Python installed:
    python bot.py

No aiogram, no requests, no dotenv needed.
Uses Telegram Bot API directly with urllib + SQLite.
"""

import json
import os
import random
import re
import sqlite3
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ===================== CONFIG =====================
# Your token/admin are embedded so bot.py works alone.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8959798352:AAEQTgsIdwz0RYRAezYEPT8C9PicxHem5g0")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7227172211").replace(" ", "").split(",") if x.isdigit()]
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# Custom emoji entities need a 1-character text range. This is NOT used as a
# normal emoji fallback; Telegram clients render the custom_emoji_id entity over it.
CUSTOM_EMOJI_PLACEHOLDER = "¤"

# In-memory user state. Good for long polling on one Termux process.
STATES: Dict[int, Dict[str, Any]] = {}

# ===================== BASIC HELPERS =====================
def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_error(where: str, exc: Exception):
    log(f"ERROR in {where}: {exc}")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
    except Exception:
        pass


def safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def html_escape(text: Any) -> str:
    s = str(text or "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_offset(text: str) -> int:
    return utf16_len(text)


def utf16_substr(text: str, offset: int, length: int) -> str:
    """Telegram entity offsets are UTF-16 code units; extract the exact entity text."""
    try:
        raw = text.encode("utf-16-le")
        return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", "ignore")
    except Exception:
        return ""


# ===================== TELEGRAM API =====================
def api_call(method: str, data: Optional[Dict[str, Any]] = None, timeout: int = 35) -> Dict[str, Any]:
    url = API + method
    payload = data or {}
    encoded = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()}).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw)
            return result
    except Exception as e:
        return {"ok": False, "description": str(e)}


def answer_callback(callback_id: str, text: str = "", show_alert: bool = False):
    api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": show_alert})


def send_message(chat_id: int, text: str, reply_markup=None, parse_mode: Optional[str] = None, entities=None):
    # If caller already passes entities or parse_mode, keep it untouched to avoid
    # breaking existing custom emoji previews/HTML formatting.
    if entities is None and parse_mode is None:
        text, entities = premiumize_bot_response(text)
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if parse_mode:
        data["parse_mode"] = parse_mode
    if entities:
        data["entities"] = entities
    return api_call("sendMessage", data)


def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode: Optional[str] = None, entities=None):
    if entities is None and parse_mode is None:
        text, entities = premiumize_bot_response(text)
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if parse_mode:
        data["parse_mode"] = parse_mode
    if entities:
        data["entities"] = entities
    return api_call("editMessageText", data)


def edit_message_caption(chat_id: int, message_id: int, caption: str, reply_markup=None, caption_entities=None):
    if caption_entities is None:
        caption, caption_entities = premiumize_bot_response(caption)
    data = {"chat_id": chat_id, "message_id": message_id, "caption": caption}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if caption_entities:
        data["caption_entities"] = caption_entities
    return api_call("editMessageCaption", data)


def delete_message(chat_id: int, message_id: int):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def send_media(chat_id: int, content_type: str, file_id: Optional[str], text: str, entities=None, reply_markup=None):
    if content_type == "text":
        return send_message(chat_id, text, reply_markup=reply_markup, entities=entities)

    method_map = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "document": ("sendDocument", "document"),
        "animation": ("sendAnimation", "animation"),
    }
    if content_type not in method_map:
        return {"ok": False, "description": "Unsupported media type"}
    method, field = method_map[content_type]
    data = {"chat_id": chat_id, field: file_id, "caption": text}
    if entities:
        data["caption_entities"] = entities
    if reply_markup:
        data["reply_markup"] = reply_markup
    return api_call(method, data)


def publish_final_preview(chat_id: int, content_type: str, file_id: Optional[str], final_text: str, final_entities: List[Dict[str, Any]]):
    """Publish ONLY the generated premium preview content.

    This never forwards/copies the original user message. Text/caption and
    custom_emoji entities must come from preview state.
    """
    if content_type == "text":
        return api_call("sendMessage", {
            "chat_id": chat_id,
            "text": final_text,
            "entities": final_entities,
        })

    method_map = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "document": ("sendDocument", "document"),
        "animation": ("sendAnimation", "animation"),
    }
    if content_type not in method_map:
        return {"ok": False, "description": "Unsupported media type"}
    method, media_field = method_map[content_type]
    return api_call(method, {
        "chat_id": chat_id,
        media_field: file_id,
        "caption": final_text,
        "caption_entities": final_entities,
    })


def forward_message(chat_id: int, from_chat_id: int, message_id: int):
    return api_call("forwardMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})


def get_chat(chat_id_or_username: Any):
    return api_call("getChat", {"chat_id": chat_id_or_username})


def get_chat_member(chat_id: Any, user_id: int):
    return api_call("getChatMember", {"chat_id": chat_id, "user_id": user_id})


def get_me():
    return api_call("getMe", {})


def create_join_request_invite_link(chat_id: int, name: str = "Bot Join Request"):
    return api_call("createChatInviteLink", {
        "chat_id": chat_id,
        "name": name,
        "creates_join_request": True,
    })


# ===================== DATABASE =====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        total_posts INTEGER DEFAULT 0,
        added_channel_id INTEGER,
        added_channel_username TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS force_join_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER UNIQUE,
        channel_username TEXT,
        emoji TEXT,
        added_by INTEGER,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_emojis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emoji_id TEXT,
        emoji_char TEXT,
        user_id INTEGER,
        added_by_admin INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        message_id INTEGER,
        content_type TEXT,
        text TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        channel_username TEXT,
        channel_title TEXT,
        created_at TEXT,
        UNIQUE(user_id, channel_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS scheduled_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        channel_username TEXT,
        from_chat_id INTEGER,
        preview_message_id INTEGER,
        content_type TEXT,
        text TEXT,
        run_at INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS auto_delete_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        message_id INTEGER,
        post_id INTEGER,
        delete_at INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    # Lightweight migration for old bot.db files
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(force_join_channels)").fetchall()]
        if "emoji_id" not in cols:
            c.execute("ALTER TABLE force_join_channels ADD COLUMN emoji_id TEXT")
        if "join_link" not in cols:
            c.execute("ALTER TABLE force_join_channels ADD COLUMN join_link TEXT")
    except Exception:
        pass
    try:
        post_cols = [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
        if "channel_username" not in post_cols:
            c.execute("ALTER TABLE posts ADD COLUMN channel_username TEXT")
    except Exception:
        pass
    try:
        old_channels = c.execute("SELECT user_id, added_channel_id, added_channel_username FROM users WHERE added_channel_id IS NOT NULL").fetchall()
        for row in old_channels:
            c.execute("INSERT OR IGNORE INTO user_channels (user_id, channel_id, channel_username, channel_title, created_at) VALUES (?, ?, ?, ?, ?)",
                      (row[0], row[1], row[2] or str(row[1]), row[2] or str(row[1]), datetime.now().isoformat()))
    except Exception:
        pass

    c.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('bot_on', '1')")
    c.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('welcome_text', ?)",
              ("👉 BOT USE KARNE KE LIYE\n\n📢 NEECHE DIYE GAYE SAARE CHANNELS JOIN KARNA ZAROORI HAI.\n\n✅ JOIN KARNE KE BAAD VERIFY DABAO.",))
    c.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('welcome_entities', '[]')")
    for aid in ADMIN_IDS:
        c.execute("INSERT OR IGNORE INTO admins (user_id, added_by, created_at) VALUES (?, ?, ?)", (aid, 0, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def query_one(sql: str, params=()):
    conn = db()
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def query_all(sql: str, params=()):
    conn = db()
    c = conn.cursor()
    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def execute(sql: str, params=()):
    conn = db()
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    conn.close()


def add_user(u: Dict[str, Any]):
    execute("INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
            (u["id"], u.get("username"), u.get("first_name"), datetime.now().isoformat()))


def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    return query_one("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) is not None


def bot_on() -> bool:
    row = query_one("SELECT value FROM bot_settings WHERE key='bot_on'")
    return not row or row["value"] == "1"


def set_bot_on(v: bool):
    execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('bot_on', ?)", ("1" if v else "0",))


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM bot_settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))


def next_emoji_rotation_index(user_id: int, emoji_count: int) -> int:
    """Return a different starting premium emoji index for each new post."""
    if emoji_count <= 0:
        return 0
    key = f"emoji_rotation_{user_id}"
    current_raw = get_setting(key, "0")
    try:
        current = int(current_raw)
    except Exception:
        current = 0
    idx = current % emoji_count
    set_setting(key, str((current + 1) % emoji_count))
    return idx


def main_reply_keyboard() -> Dict[str, Any]:
    # Telegram keyboards do not support custom_emoji entities in button text.
    # Keep button names plain so no normal Unicode emoji is used.
    return {
        "keyboard": [
            [{"text": "Create Post"}, {"text": "Check Post"}],
            [{"text": "Scheduled Posts"}, {"text": "Statistics"}],
            [{"text": "Leaderboard"}, {"text": "Channels"}],
            [{"text": "Profile"}, {"text": "Add Emoji"}],
            [{"text": "Owner"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": True,
    }


def premium_main_menu_text_entities() -> Tuple[str, List[Dict[str, Any]]]:
    items = [
        ("6129532314146838421", "Create Post"),
        ("6129532314146838421", "Check Post"),
        ("6255572871091849620", "Scheduled Posts"),
        ("6091280734113243257", "Statistics"),
        ("6129532314146838421", "Leaderboard"),
        ("6228693084557809235", "Channels"),
        ("6334666063942263327", "Profile"),
        ("6228601868042377131", "Add Emoji"),
        ("6129817830687775854", "Owner"),
    ]
    text = "Premium Bot Main Menu\n\n"
    entities: List[Dict[str, Any]] = []
    for custom_id, label in items:
        offset = utf16_offset(text)
        text += "⭐ " + label + "\n"
        entities.append({
            "type": "custom_emoji",
            "offset": offset,
            "length": utf16_len("⭐"),
            "custom_emoji_id": custom_id,
        })
    return text.rstrip(), entities


# ===================== KEYBOARDS =====================
def ik(rows: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {"inline_keyboard": rows}


def btn(text: str, callback_data: Optional[str] = None, url: Optional[str] = None):
    b = {"text": text}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    return b


def pair_rows(buttons: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def join_keyboard(channels: List[Dict[str, Any]]):
    buttons = []
    for ch in channels:
        stored_link = str(ch.get("join_link") or "")
        username = str(ch.get("channel_username") or "")
        if stored_link.startswith("http"):
            url = stored_link
        elif username.startswith("@"):
            url = "https://t.me/" + username[1:]
        elif username.startswith("http"):
            url = username
        elif username and not username.startswith("-"):
            url = "https://t.me/" + username
        else:
            url = "https://t.me/"
        # Telegram Bot API does not support MessageEntity(custom_emoji) inside
        # inline keyboard button text, so keep JOIN plain instead of normal emoji.
        buttons.append(btn("JOIN", url=url))
    rows = pair_rows(buttons)
    rows.append([btn("✅ Check Joined", "check_joined")])
    return ik(rows)


def setup_keyboard():
    return ik([[btn("➕  ADD CHANNEL  ➕", "add_channel")]])


def main_keyboard():
    return ik([
        [btn("📝 Create Post", "create_post"), btn("📋 Check Post", "check_post")],
        [btn("👤 Profile", "profile"), btn("😀 Add Emoji", "add_emoji")],
    ])


def preview_keyboard():
    return ik([
        [btn("📤 Send", "send_post"), btn("🔄 Again", "again_post")],
        [btn("❌ Close", "close_post")],
    ])


def admin_keyboard():
    return ik([
        [btn("➕ Add Force Join", "admin_add_force"), btn("➖ Remove Force Join", "admin_remove_force")],
        [btn("📢 Broadcast", "admin_broadcast"), btn("🔘 Bot On/Off", "admin_bot_toggle")],
        [btn("📊 Statistics", "admin_stats"), btn("💬 Welcome Msg", "admin_set_welcome")],
        [btn("📣 Update Channel", "admin_set_update_channel"), btn("➕ Add Admin", "admin_add_admin")],
        [btn("➖ Remove Admin", "admin_remove_admin")],
        [btn("😀 Add Emoji", "admin_add_emoji"), btn("🗑 Remove Emoji", "admin_remove_emoji")],
        [btn("🔙 Back", "admin_back")],
    ])


def back_keyboard():
    return ik([[btn("🔙 Back", "back_to_main")]])


def cancel_keyboard():
    return ik([[btn("❌ Cancel", "cancel_action")]])


def owner_keyboard():
    return ik([[btn("👑  OWNER  👑", url="https://t.me/wrczt")]])


def channels_manage_keyboard(user_id: int):
    return ik([
        [btn("➕ ADD CHANNEL", "channels_add"), btn("🗑 DELETE CHANNELS", "channels_delete_menu")],
        [btn("🔙 BACK", "back_to_main")],
    ])


def channel_select_keyboard(user_id: int):
    channels = get_user_channels(user_id)
    buttons = []
    for ch in channels:
        title = ch.get("channel_title") or ch.get("channel_username") or str(ch["channel_id"])
        buttons.append(btn(f"📢 {title}", f"select_post_channel_{ch['channel_id']}"))
    rows = pair_rows(buttons)
    rows.append([btn("❌ CANCEL", "cancel_send_post")])
    return ik(rows)


def send_options_keyboard():
    return ik([
        [btn("🚀 SEND NOW", "send_now_post"), btn("🕒 SCHEDULE", "schedule_post")],
        [btn("❌ CANCEL", "cancel_send_post")],
    ])


def auto_delete_unit_keyboard(post_id: int):
    return ik([
        [btn("Seconds", f"ad_unit_{post_id}_seconds"), btn("Minutes", f"ad_unit_{post_id}_minutes")],
        [btn("Hours", f"ad_unit_{post_id}_hours"), btn("Days", f"ad_unit_{post_id}_days")],
        [btn("❌ Cancel", "back_to_main")],
    ])


def channels_delete_keyboard(user_id: int):
    channels = get_user_channels(user_id)
    buttons = []
    for ch in channels:
        title = ch.get("channel_title") or ch.get("channel_username") or str(ch["channel_id"])
        buttons.append(btn(f"🗑 {title}", f"delete_user_channel_{ch['channel_id']}"))
    rows = pair_rows(buttons)
    rows.append([btn("🔙 BACK", "channels_menu")])
    return ik(rows)


def admin_delete_emoji_keyboard():
    emojis = query_all("SELECT id, emoji_id, emoji_char FROM user_emojis WHERE added_by_admin=1 AND emoji_id IS NOT NULL AND emoji_id != '' ORDER BY id DESC LIMIT 50")
    buttons = []
    for e in emojis:
        shown = e.get("emoji_char") or "⭐"
        buttons.append(btn(f"🗑 {shown} ID {e['id']}", f"admin_del_emoji_{e['id']}"))
    rows = pair_rows(buttons)
    if emojis:
        rows.append([btn("🗑 DELETE ALL GLOBAL EMOJIS", "admin_del_all_emojis")])
    rows.append([btn("🔙 BACK", "admin_back_to_panel")])
    return ik(rows)


# ===================== EMOJI + CHANNEL HELPERS =====================
def is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF or
        0x2600 <= cp <= 0x27BF or
        0x2300 <= cp <= 0x23FF
    )


def extract_custom_emojis(message: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Extract Telegram Premium custom_emoji_id values with their required text range.

    Telegram Bot API requires a custom_emoji entity to cover an emoji character
    in the message text/caption. We store that character only as the entity range;
    rendering is controlled by custom_emoji_id.
    """
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    result: List[Tuple[str, str]] = []
    seen = set()
    for e in entities:
        if e.get("type") == "custom_emoji" and e.get("custom_emoji_id"):
            cid = str(e["custom_emoji_id"])
            emoji_char = utf16_substr(text, int(e.get("offset", 0)), int(e.get("length", 1))) or "⭐"
            if cid not in seen:
                result.append((cid, emoji_char))
                seen.add(cid)
    # Raw IDs are supported, but Bot API still needs an emoji character range.
    for cid in re.findall(r"\b\d{10,}\b", text):
        if cid not in seen:
            result.append((cid, "⭐"))
            seen.add(cid)
    return result


def get_text_emojis(text: str) -> List[str]:
    return [ch for ch in text if is_emoji_char(ch)]


def get_user_emojis(user_id: int) -> List[Dict[str, Any]]:
    return query_all(
        "SELECT emoji_id, emoji_char FROM user_emojis WHERE (user_id=? OR added_by_admin=1) AND emoji_id IS NOT NULL AND emoji_id != ''",
        (user_id,)
    )


def get_admin_response_emojis() -> List[Dict[str, Any]]:
    return query_all(
        "SELECT emoji_id, emoji_char FROM user_emojis WHERE added_by_admin=1 AND emoji_id IS NOT NULL AND emoji_id != '' ORDER BY id ASC"
    )


def premiumize_bot_response(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Make every normal bot response use admin-set Premium Custom Emojis.

    - Replaces normal Unicode emoji chars in response text with custom_emoji IDs.
    - If no emoji is present, adds one admin premium emoji at the beginning.
    - If admin did not set emojis yet, returns text unchanged.
    """
    try:
        emojis = get_admin_response_emojis()
    except Exception:
        return text, []
    if not emojis or not text:
        return text, []
    idx = next_emoji_rotation_index(0, len(emojis))
    return build_premium_text(text, emojis, idx)


def get_user_channels(user_id: int) -> List[Dict[str, Any]]:
    return query_all("SELECT * FROM user_channels WHERE user_id=? ORDER BY id DESC", (user_id,))


def add_user_channel(user_id: int, ch: Dict[str, Any]):
    execute("INSERT OR IGNORE INTO user_channels (user_id, channel_id, channel_username, channel_title, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, ch["id"], ch.get("username") or str(ch["id"]), ch.get("title") or ch.get("username") or str(ch["id"]), datetime.now().isoformat()))
    execute("UPDATE users SET added_channel_id=?, added_channel_username=? WHERE user_id=?",
            (ch["id"], ch.get("username") or str(ch["id"]), user_id))


def delete_user_channel(user_id: int, channel_id: int):
    execute("DELETE FROM user_channels WHERE user_id=? AND channel_id=?", (user_id, channel_id))
    remaining = get_user_channels(user_id)
    if remaining:
        execute("UPDATE users SET added_channel_id=?, added_channel_username=? WHERE user_id=?",
                (remaining[0]["channel_id"], remaining[0].get("channel_username") or str(remaining[0]["channel_id"]), user_id))
    else:
        execute("UPDATE users SET added_channel_id=NULL, added_channel_username=NULL WHERE user_id=?", (user_id,))


def build_post_stats_text(user_id: int) -> str:
    total = query_one("SELECT COUNT(*) AS c FROM posts WHERE user_id=?", (user_id,))
    rows = query_all("""
        SELECT p.channel_id, COALESCE(p.channel_username, uc.channel_title, uc.channel_username, CAST(p.channel_id AS TEXT)) AS name, COUNT(*) AS c
        FROM posts p
        LEFT JOIN user_channels uc ON uc.user_id=p.user_id AND uc.channel_id=p.channel_id
        WHERE p.user_id=?
        GROUP BY p.channel_id, name
        ORDER BY c DESC
    """, (user_id,))
    text = f"📊 Total posts sent: {total['c'] if total else 0}"
    if rows:
        text += "\n\nChannel wise:"
        for r in rows:
            text += f"\n• {r.get('name') or r['channel_id']}: {r['c']}"
    else:
        text += "\n\nAbhi koi post send nahi hui."
    return text


def check_post_keyboard(user_id: int):
    posts = query_all("""
        SELECT p.id, p.channel_id, p.message_id, COALESCE(p.channel_username, uc.channel_title, uc.channel_username, CAST(p.channel_id AS TEXT)) AS name
        FROM posts p
        LEFT JOIN user_channels uc ON uc.user_id=p.user_id AND uc.channel_id=p.channel_id
        WHERE p.user_id=?
        ORDER BY p.id DESC
        LIMIT 20
    """, (user_id,))
    rows = []
    for p in posts:
        rows.append([btn(f"Post #{p['id']} • {p.get('name') or p['channel_id']}", f"noop_post_{p['id']}"), btn("Auto Delete", f"auto_delete_{p['id']}")])
    rows.append([btn("🔙 BACK", "back_to_main")])
    return ik(rows)


def scheduled_posts_text(user_id: int) -> str:
    rows = query_all("SELECT * FROM scheduled_posts WHERE user_id=? AND status='pending' ORDER BY run_at ASC", (user_id,))
    if not rows:
        return "🕒 No pending scheduled posts."
    text = "🕒 Pending Scheduled Posts:"
    for r in rows[:20]:
        when = datetime.fromtimestamp(int(r["run_at"])).strftime("%d-%m-%Y %I:%M %p")
        text += f"\n• #{r['id']} → {r.get('channel_username') or r['channel_id']} at {when}"
    return text


def scheduled_posts_keyboard(user_id: int):
    rows_data = query_all("SELECT id FROM scheduled_posts WHERE user_id=? AND status='pending' ORDER BY run_at ASC LIMIT 20", (user_id,))
    rows = []
    for r in rows_data:
        rows.append([btn(f"🚀 Send #{r['id']}", f"sched_send_{r['id']}"), btn(f"🗑 Delete #{r['id']}", f"sched_del_{r['id']}")])
    rows.append([btn("🔙 BACK", "back_to_main")])
    return ik(rows)


def user_statistics_text(user_id: int) -> str:
    user = query_one("SELECT * FROM users WHERE user_id=?", (user_id,)) or {}
    posts = query_one("SELECT COUNT(*) c FROM posts WHERE user_id=?", (user_id,))["c"]
    channels = query_one("SELECT COUNT(*) c FROM user_channels WHERE user_id=?", (user_id,))["c"]
    sched_pending = query_one("SELECT COUNT(*) c FROM scheduled_posts WHERE user_id=? AND status='pending'", (user_id,))["c"]
    sched_sent = query_one("SELECT COUNT(*) c FROM scheduled_posts WHERE user_id=? AND status='sent'", (user_id,))["c"]
    ad_pending = query_one("SELECT COUNT(*) c FROM auto_delete_jobs WHERE user_id=? AND status='pending'", (user_id,))["c"]
    ad_done = query_one("SELECT COUNT(*) c FROM auto_delete_jobs WHERE user_id=? AND status='done'", (user_id,))["c"]
    username = f"@{user.get('username')}" if user.get("username") else "N/A"
    return (
        "📊 Your Activity\n\n"
        f"Name: {user.get('first_name') or 'N/A'}\n"
        f"Username: {username}\n"
        f"Total Posts Sent: {posts}\n"
        f"Added Channels: {channels}\n"
        f"Scheduled Pending: {sched_pending}\n"
        f"Scheduled Sent: {sched_sent}\n"
        f"Auto Delete Pending: {ad_pending}\n"
        f"Auto Deleted: {ad_done}"
    )


def leaderboard_text() -> str:
    rows = query_all("SELECT first_name, username, total_posts FROM users ORDER BY total_posts DESC, user_id ASC LIMIT 10")
    text = "📈 Leaderboard - Top Posters"
    if not rows:
        return text + "\n\nNo users yet."
    rank = 1
    for r in rows:
        name = r.get("first_name") or "Unknown"
        username = f"@{r.get('username')}" if r.get("username") else "N/A"
        text += f"\n{rank}. {name} ({username}) - {r.get('total_posts', 0)} posts"
        rank += 1
    return text


def build_premium_text(text: str, emojis: List[Dict[str, Any]], start_index: int = 0) -> Tuple[str, List[Dict[str, Any]]]:
    """Build preview/channel text using only saved Telegram custom_emoji_id values.

    Normal emojis in the source text are removed and replaced with a neutral
    placeholder covered by MessageEntity(type='custom_emoji'). If no emoji is
    present, one custom emoji entity is inserted at the beginning.
    """
    premium_items = [e for e in emojis if e.get("emoji_id")]
    if not premium_items:
        return text, []

    def item_at(i: int) -> Tuple[str, str]:
        e = premium_items[i % len(premium_items)]
        return str(e.get("emoji_id")), (e.get("emoji_char") or "⭐")

    def custom_entity(offset_text: str, emoji_char: str, cid: str) -> Dict[str, Any]:
        return {
            "type": "custom_emoji",
            "offset": utf16_offset(offset_text),
            "length": utf16_len(emoji_char),
            "custom_emoji_id": cid,
        }

    if not text:
        cid, emoji_char = item_at(start_index)
        return emoji_char, [custom_entity("", emoji_char, cid)]

    contains = any(is_emoji_char(ch) for ch in text)
    if not contains:
        cid, emoji_char = item_at(start_index)
        new_text = emoji_char + " " + text
        return new_text, [custom_entity("", emoji_char, cid)]

    out = ""
    entities: List[Dict[str, Any]] = []
    idx = start_index
    i = 0
    while i < len(text):
        ch = text[i]
        if is_emoji_char(ch):
            cid, emoji_char = item_at(idx)
            entities.append(custom_entity(out, emoji_char, cid))
            out += emoji_char
            idx += 1
            i += 1
            # Skip common emoji modifiers/joiners so multi-codepoint emoji clusters
            # are replaced by a single Premium Custom Emoji entity.
            while i < len(text) and (text[i] == "\ufe0f" or text[i] == "\u200d" or 0x1F3FB <= ord(text[i]) <= 0x1F3FF):
                i += 1
            continue
        out += ch
        i += 1
    return out, entities


def extract_channel(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Forwarded channel message
    origin_chat = safe_get(message, "forward_origin", "chat")
    if origin_chat and origin_chat.get("type") == "channel":
        return {"id": origin_chat["id"], "username": origin_chat.get("username") or str(origin_chat["id"]), "title": origin_chat.get("title", "Channel")}
    fchat = message.get("forward_from_chat")
    if fchat and fchat.get("type") == "channel":
        return {"id": fchat["id"], "username": fchat.get("username") or str(fchat["id"]), "title": fchat.get("title", "Channel")}

    text = (message.get("text") or "").strip()
    if not text:
        return None
    username = text
    if username.startswith("https://t.me/"):
        username = username.split("/")[-1].split("?")[0]
    if not username.startswith("@"):
        username = "@" + username
    res = get_chat(username)
    if res.get("ok") and res.get("result", {}).get("type") == "channel":
        ch = res["result"]
        return {"id": ch["id"], "username": ch.get("username") or username.replace("@", ""), "title": ch.get("title", "Channel")}
    return None


def is_bot_admin(chat_id: int, bot_id: int) -> bool:
    res = get_chat_member(chat_id, bot_id)
    if not res.get("ok"):
        return False
    status = res.get("result", {}).get("status")
    return status in ("administrator", "creator")


def not_joined_channels(user_id: int) -> List[Dict[str, Any]]:
    channels = query_all("SELECT * FROM force_join_channels")
    missing = []
    for ch in channels:
        res = get_chat_member(ch["channel_id"], user_id)
        if not res.get("ok") or res.get("result", {}).get("status") in ("left", "kicked"):
            missing.append(ch)
    return missing


def get_welcome() -> Tuple[str, List[Dict[str, Any]]]:
    text = get_setting("welcome_text", "Please join the following channel(s) to use this bot:")
    try:
        entities = json.loads(get_setting("welcome_entities", "[]"))
        if not isinstance(entities, list):
            entities = []
    except Exception:
        entities = []
    return text, entities


def send_force_join(chat_id: int, channels: List[Dict[str, Any]]):
    text, entities = get_welcome()
    send_message(chat_id, text, reply_markup=join_keyboard(channels), entities=entities)


def edit_force_join(chat_id: int, message_id: int, channels: List[Dict[str, Any]]):
    text, entities = get_welcome()
    edit_message_text(chat_id, message_id, text, reply_markup=join_keyboard(channels), entities=entities)


def parse_schedule_date(date_text: str) -> Optional[Tuple[int, int, int]]:
    date_text = date_text.strip().replace("/", "-")
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%m-%y"):
        try:
            dt = datetime.strptime(date_text, fmt)
            return dt.year, dt.month, dt.day
        except Exception:
            pass
    return None


def parse_schedule_time(time_text: str, y: int, m: int, d: int) -> Optional[int]:
    raw = time_text.strip().upper().replace(".", "")
    raw = re.sub(r"\s+", " ", raw)
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%H"):
        try:
            t = datetime.strptime(raw, fmt)
            dt = datetime(y, m, d, t.hour, t.minute)
            return int(dt.timestamp())
        except Exception:
            pass
    return None


def process_due_jobs():
    now = int(time.time())
    scheduled = query_all("SELECT * FROM scheduled_posts WHERE status='pending' AND run_at<=? ORDER BY id LIMIT 10", (now,))
    for job in scheduled:
        r = forward_message(job["channel_id"], job["from_chat_id"], job["preview_message_id"])
        if r.get("ok"):
            execute("UPDATE scheduled_posts SET status='sent' WHERE id=?", (job["id"],))
            execute("INSERT INTO posts (user_id, channel_id, channel_username, message_id, content_type, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job["user_id"], job["channel_id"], job.get("channel_username"), r["result"]["message_id"], job.get("content_type"), job.get("text"), datetime.now().isoformat()))
            execute("UPDATE users SET total_posts=total_posts+1 WHERE user_id=?", (job["user_id"],))
        else:
            log("scheduled post failed: " + str(r.get("description")))
    deletes = query_all("SELECT * FROM auto_delete_jobs WHERE status='pending' AND delete_at<=? ORDER BY id LIMIT 20", (now,))
    for j in deletes:
        r = delete_message(j["channel_id"], j["message_id"])
        execute("UPDATE auto_delete_jobs SET status=? WHERE id=?", ("done" if r.get("ok") else "failed", j["id"]))


# ===================== MENUS =====================
def show_main(chat_id: int, user_id: int):
    channels = get_user_channels(user_id)
    if not channels:
        welcome_text, welcome_entities = get_welcome()
        setup_text = welcome_text + "\n\nPehle is bot ko apne channel me admin banao, fir channel add karo."
        send_message(chat_id, setup_text, reply_markup=setup_keyboard(), entities=welcome_entities)
    else:
        menu_text, menu_entities = premium_main_menu_text_entities()
        send_message(chat_id, menu_text, reply_markup=main_reply_keyboard(), entities=menu_entities)


# ===================== UPDATE HANDLERS =====================
def handle_start(message: Dict[str, Any], bot_id: int):
    chat_id = message["chat"]["id"]
    user = message["from"]
    add_user(user)
    if not bot_on() and not is_admin(user["id"]):
        send_message(chat_id, "Bot is currently off. Please try again later.")
        return
    missing = not_joined_channels(user["id"])
    if missing:
        send_force_join(chat_id, missing)
        return
    show_main(chat_id, user["id"])


def handle_admin_cmd(message: Dict[str, Any]):
    chat_id = message["chat"]["id"]
    uid = message["from"]["id"]
    add_user(message["from"])
    if not is_admin(uid):
        send_message(chat_id, "You are not authorized to use this command.")
        return
    send_message(chat_id, "🔧 Admin Panel", reply_markup=admin_keyboard())


def content_from_message(message: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], str]:
    if message.get("text"):
        return "text", None, message.get("text", "")
    if message.get("photo"):
        return "photo", message["photo"][-1]["file_id"], message.get("caption") or ""
    if message.get("video"):
        return "video", message["video"]["file_id"], message.get("caption") or ""
    if message.get("document"):
        return "document", message["document"]["file_id"], message.get("caption") or ""
    if message.get("animation"):
        return "animation", message["animation"]["file_id"], message.get("caption") or ""
    return None, None, ""


def handle_state_message(message: Dict[str, Any], bot_id: int):
    uid = message["from"]["id"]
    chat_id = message["chat"]["id"]
    state = STATES.get(uid, {})
    name = state.get("name")

    if name == "waiting_for_channel":
        ch = extract_channel(message)
        if not ch:
            send_message(chat_id, "Invalid channel. @channel username bhejo ya private channel se message forward karo.", reply_markup=cancel_keyboard())
            return
        if not is_bot_admin(ch["id"], bot_id):
            send_message(chat_id, "Bot is not admin in this channel. Pehle bot ko channel me admin banao.", reply_markup=cancel_keyboard())
            return
        add_user_channel(uid, ch)
        STATES.pop(uid, None)
        send_message(chat_id, f"✅ Channel '{html_escape(ch['title'])}' added successfully!", reply_markup=main_reply_keyboard())
        return

    if name == "waiting_for_emoji" or name == "waiting_for_admin_emoji":
        added = 0
        admin_global = name == "waiting_for_admin_emoji"
        custom_emojis = extract_custom_emojis(message)
        for emoji_id, emoji_char in custom_emojis:
            execute("INSERT INTO user_emojis (emoji_id, emoji_char, user_id, added_by_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                    (emoji_id, emoji_char, uid, 1 if admin_global else 0, datetime.now().isoformat()))
            added += 1
        # Do not save normal Unicode emojis. Premium system uses custom_emoji_id only.
        if added:
            STATES.pop(uid, None)
            send_message(chat_id, f"✅ {added} emoji(s) added successfully!", reply_markup=admin_keyboard() if admin_global else main_keyboard())
        else:
            send_message(chat_id, "No custom_emoji_id found. Premium custom emoji send karo ya raw custom_emoji_id paste karo.", reply_markup=cancel_keyboard())
        return

    if name == "waiting_for_post":
        ctype, file_id, text = content_from_message(message)
        if not ctype:
            send_message(chat_id, "Unsupported content. Text, photo, video, document, animation bhejo.")
            return
        user = query_one("SELECT * FROM users WHERE user_id=?", (uid,))
        emojis = get_user_emojis(uid)
        if not emojis:
            STATES.pop(uid, None)
            send_message(chat_id, "Please add premium emojis first!", reply_markup=main_reply_keyboard())
            return
        start_idx = next_emoji_rotation_index(uid, len(emojis))
        new_text, entities = build_premium_text(text, emojis, start_idx)
        res = send_media(chat_id, ctype, file_id, new_text, entities=entities, reply_markup=preview_keyboard())
        if res.get("ok"):
            STATES[uid] = {
                "name": "preview",
                "content_type": ctype,
                "file_id": file_id,
                "original_text": text,
                # final_* is the source of truth for Send button publishing.
                "final_text": new_text,
                "final_entities": entities,
                # Keep old keys only for backward compatibility in this running process.
                "premium_text": new_text,
                "entities": entities,
                "channel_id": user.get("added_channel_id") if user else None,
                "preview_message_id": res["result"]["message_id"],
                "emoji_index": start_idx,
            }
        else:
            send_message(chat_id, "Preview create nahi hua. Try again.")
        return

    if name == "waiting_for_force_emoji":
        custom = extract_custom_emojis(message)
        emoji_id = custom[0][0] if custom else None
        if not emoji_id:
            send_message(chat_id, "Invalid custom_emoji_id. Premium custom emoji send karo ya raw custom_emoji_id paste karo.", reply_markup=cancel_keyboard())
            return
        STATES[uid] = {"name": "waiting_for_force_channel", "force_emoji": "", "force_emoji_id": emoji_id}
        send_message(chat_id, "Ab force join channel username bhejo ya private channel se message forward karo.", reply_markup=cancel_keyboard())
        return

    if name == "waiting_update_channel":
        ch = extract_channel(message)
        if not ch:
            send_message(chat_id, "Invalid update channel. @channel username bhejo ya channel se message forward karo.", reply_markup=cancel_keyboard())
            return
        if not is_bot_admin(ch["id"], bot_id):
            send_message(chat_id, "Bot update channel me admin hona chahiye. Pehle admin banao, fir try karo.", reply_markup=cancel_keyboard())
            return
        set_setting("update_channel_id", str(ch["id"]))
        set_setting("update_channel_username", ch.get("username") or str(ch["id"]))
        STATES.pop(uid, None)
        send_message(chat_id, f"✅ Update channel set: {html_escape(ch['title'])}", reply_markup=admin_keyboard())
        return

    if name == "waiting_for_force_channel":
        ch = extract_channel(message)
        if not ch:
            send_message(chat_id, "Invalid channel. Username bhejo ya channel message forward karo.", reply_markup=cancel_keyboard())
            return
        emoji_id = state.get("force_emoji_id")
        join_link = ""
        invite = create_join_request_invite_link(ch["id"], "Force Join Request")
        if invite.get("ok"):
            join_link = invite.get("result", {}).get("invite_link", "")
        elif str(ch.get("username") or "").startswith("-"):
            send_message(chat_id, "Bot private channel ke invite link create nahi kar pa raha. Bot ko admin banao with invite users permission, fir try karo.", reply_markup=cancel_keyboard())
            return
        execute("INSERT OR REPLACE INTO force_join_channels (channel_id, channel_username, emoji, emoji_id, join_link, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ch["id"], ch["username"], "", emoji_id, join_link, uid, datetime.now().isoformat()))
        STATES.pop(uid, None)
        extra = "\nJoin requests enabled." if join_link else ""
        send_message(chat_id, f"✅ Force join channel added: {html_escape(ch['title'])}{extra}\ncustom_emoji_id: <code>{html_escape(emoji_id)}</code>", parse_mode="HTML", reply_markup=admin_keyboard())
        return

    if name == "waiting_schedule_date":
        parsed = parse_schedule_date(message.get("text") or "")
        if not parsed:
            send_message(chat_id, "Invalid date. Use DD-MM-YYYY, example: 28-06-2026", reply_markup=cancel_keyboard())
            return
        state.update({"name": "waiting_schedule_time", "schedule_year": parsed[0], "schedule_month": parsed[1], "schedule_day": parsed[2]})
        STATES[uid] = state
        send_message(chat_id, "Enter schedule time with AM/PM:\nExample: 10:30 PM\n(24-hour format like 22:30 also supported)", reply_markup=cancel_keyboard())
        return

    if name == "waiting_schedule_time":
        ts = parse_schedule_time(message.get("text") or "", state["schedule_year"], state["schedule_month"], state["schedule_day"])
        if not ts:
            send_message(chat_id, "Invalid time. Use like 10:30 PM or 22:30", reply_markup=cancel_keyboard())
            return
        if ts <= int(time.time()):
            send_message(chat_id, "Schedule time future me hona chahiye. Time dobara bhejo.", reply_markup=cancel_keyboard())
            return
        execute("INSERT INTO scheduled_posts (user_id, channel_id, channel_username, from_chat_id, preview_message_id, content_type, text, run_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (uid, state.get("selected_channel_id"), state.get("selected_channel_name"), chat_id, state.get("preview_message_id"), state.get("content_type"), state.get("final_text") or state.get("premium_text"), ts, datetime.now().isoformat()))
        STATES.pop(uid, None)
        send_message(chat_id, f"✅ Post scheduled for {datetime.fromtimestamp(ts).strftime('%d-%m-%Y %I:%M %p')}", reply_markup=main_reply_keyboard())
        return

    if name == "waiting_auto_delete_value":
        raw = (message.get("text") or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            send_message(chat_id, "Please send a positive number.", reply_markup=cancel_keyboard())
            return
        value = int(raw)
        unit = state.get("ad_unit")
        mult = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 1)
        post = query_one("SELECT * FROM posts WHERE id=? AND user_id=?", (state.get("ad_post_id"), uid))
        if not post:
            STATES.pop(uid, None)
            send_message(chat_id, "Post not found.", reply_markup=main_reply_keyboard())
            return
        delete_at = int(time.time()) + value * mult
        execute("INSERT INTO auto_delete_jobs (user_id, channel_id, message_id, post_id, delete_at, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (uid, post["channel_id"], post["message_id"], post["id"], delete_at, datetime.now().isoformat()))
        STATES.pop(uid, None)
        send_message(chat_id, f"✅ Auto delete active. Post #{post['id']} will delete in {value} {unit}.", reply_markup=main_reply_keyboard())
        return

    if name == "waiting_for_welcome":
        text = message.get("text") or message.get("caption") or ""
        entities = message.get("entities") or message.get("caption_entities") or []
        custom_entities = [e for e in entities if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")]
        if not text:
            send_message(chat_id, "Welcome message text bhejo. Telegram Premium Custom Emoji entities supported hain.", reply_markup=cancel_keyboard())
            return
        set_setting("welcome_text", text)
        set_setting("welcome_entities", json.dumps(custom_entities, ensure_ascii=False))
        STATES.pop(uid, None)
        send_message(chat_id, "✅ Welcome message saved with Premium Custom Emoji entities.", reply_markup=admin_keyboard())
        return

    if name == "waiting_for_new_admin":
        text = (message.get("text") or "").strip()
        if not text.isdigit():
            send_message(chat_id, "Invalid user ID. Numeric user ID bhejo.", reply_markup=cancel_keyboard())
            return
        execute("INSERT OR IGNORE INTO admins (user_id, added_by, created_at) VALUES (?, ?, ?)", (int(text), uid, datetime.now().isoformat()))
        STATES.pop(uid, None)
        send_message(chat_id, f"✅ Admin added: <code>{text}</code>", parse_mode="HTML", reply_markup=admin_keyboard())
        return

    if name == "waiting_for_broadcast":
        users = query_all("SELECT user_id FROM users")
        sent = failed = 0
        ctype, file_id, text = content_from_message(message)
        original_entities = message.get("entities") if ctype == "text" else message.get("caption_entities")
        custom_original_entities = [e for e in (original_entities or []) if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")]
        broadcast_text = text
        broadcast_entities = custom_original_entities
        if ctype and not custom_original_entities:
            saved_ids = get_user_emojis(uid)
            if saved_ids:
                b_idx = next_emoji_rotation_index(uid, len(saved_ids))
                broadcast_text, broadcast_entities = build_premium_text(text, saved_ids, b_idx)
        for u in users:
            try:
                target = u["user_id"]
                if message.get("forward_origin") or message.get("forward_from") or message.get("forward_from_chat"):
                    r = forward_message(target, chat_id, message["message_id"])
                elif ctype:
                    r = send_media(target, ctype, file_id, broadcast_text, entities=broadcast_entities)
                else:
                    r = {"ok": False}
                if r.get("ok"):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        STATES.pop(uid, None)
        send_message(chat_id, f"📢 Broadcast complete!\nSent: {sent}\nFailed: {failed}", reply_markup=admin_keyboard())
        return


def handle_callback(cb: Dict[str, Any], bot_id: int):
    data = cb.get("data")
    uid = cb["from"]["id"]
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    add_user(cb["from"])

    if data == "check_joined":
        missing = not_joined_channels(uid)
        if missing:
            edit_force_join(chat_id, mid, missing)
            answer_callback(cb["id"], "You haven't joined all channels yet!", True)
        else:
            delete_message(chat_id, mid)
            show_main(chat_id, uid)
            answer_callback(cb["id"])
        return

    if data == "add_channel" or data == "channels_add":
        STATES[uid] = {"name": "waiting_for_channel"}
        edit_message_text(chat_id, mid, "Channel add karne ke liye @channel username bhejo ya private channel se koi message forward karo. Bot us channel me admin hona chahiye.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "channels_menu":
        channels = get_user_channels(uid)
        text_channels = f"📢 Your added channels: {len(channels)}"
        if channels:
            for ch in channels:
                text_channels += f"\n• {ch.get('channel_title') or ch.get('channel_username') or ch['channel_id']}"
        edit_message_text(chat_id, mid, text_channels, reply_markup=channels_manage_keyboard(uid))
        answer_callback(cb["id"])
        return

    if data == "channels_delete_menu":
        channels = get_user_channels(uid)
        if not channels:
            answer_callback(cb["id"], "No channels to delete.", True)
            return
        edit_message_text(chat_id, mid, "Aapke sab added channels niche show ho rahe hain. Jise delete karna ho select karo; multiple delete kar sakte ho:", reply_markup=channels_delete_keyboard(uid))
        answer_callback(cb["id"])
        return

    if data and data.startswith("delete_user_channel_"):
        channel_id = int(data.replace("delete_user_channel_", ""))
        delete_user_channel(uid, channel_id)
        channels = get_user_channels(uid)
        if channels:
            edit_message_text(chat_id, mid, "Deleted. Aur delete karna ho to select karo:", reply_markup=channels_delete_keyboard(uid))
        else:
            edit_message_text(chat_id, mid, "All channels deleted.", reply_markup=channels_manage_keyboard(uid))
        answer_callback(cb["id"], "Channel deleted.")
        return

    if data == "create_post":
        if not get_user_channels(uid):
            answer_callback(cb["id"], "Please add a channel first!", True)
            return
        if not get_user_emojis(uid):
            answer_callback(cb["id"], "Please add premium emojis first!", True)
            return
        STATES[uid] = {"name": "waiting_for_post"}
        edit_message_text(chat_id, mid, "Apna post bhejo. Text, photo, video, document, animation sab allow hain.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "check_post":
        text = build_post_stats_text(uid)
        edit_message_text(chat_id, mid, text, reply_markup=check_post_keyboard(uid))
        answer_callback(cb["id"])
        return

    if data and data.startswith("noop_post_"):
        answer_callback(cb["id"], "Use Auto Delete button for this post.")
        return

    if data and data.startswith("auto_delete_"):
        post_id = int(data.replace("auto_delete_", ""))
        post = query_one("SELECT * FROM posts WHERE id=? AND user_id=?", (post_id, uid))
        if not post:
            answer_callback(cb["id"], "Post not found.", True)
            return
        edit_message_text(chat_id, mid, f"Select auto delete duration unit for Post #{post_id}:", reply_markup=auto_delete_unit_keyboard(post_id))
        answer_callback(cb["id"])
        return

    if data and data.startswith("ad_unit_"):
        parts = data.split("_")
        post_id = int(parts[2])
        unit = parts[3]
        STATES[uid] = {"name": "waiting_auto_delete_value", "ad_post_id": post_id, "ad_unit": unit}
        edit_message_text(chat_id, mid, f"Send number of {unit}.\nExample: 2", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "profile":
        u = query_one("SELECT * FROM users WHERE user_id=?", (uid,))
        if not u:
            answer_callback(cb["id"], "User not found!", True)
            return
        channel_count = len(get_user_channels(uid))
        text = (f"👤 <b>Profile</b>\n\n"
                f"Name: {html_escape(u.get('first_name') or 'N/A')}\n"
                f"ID: <code>{uid}</code>\n"
                f"Username: @{html_escape(u.get('username')) if u.get('username') else 'N/A'}\n"
                f"Total Posts: {u.get('total_posts', 0)}\n"
                f"Added Channels: {channel_count}")
        edit_message_text(chat_id, mid, text, parse_mode="HTML", reply_markup=back_keyboard())
        answer_callback(cb["id"])
        return

    if data == "add_emoji":
        STATES[uid] = {"name": "waiting_for_emoji"}
        edit_message_text(chat_id, mid, "Telegram Premium Custom Emoji send karo ya one/multiple raw custom_emoji_id paste karo.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "back_to_main":
        delete_message(chat_id, mid)
        show_main(chat_id, uid)
        answer_callback(cb["id"])
        return

    if data == "cancel_action":
        STATES.pop(uid, None)
        edit_message_text(chat_id, mid, "Action cancelled.")
        show_main(chat_id, uid)
        answer_callback(cb["id"])
        return

    # Preview callbacks
    if data == "send_post":
        st = STATES.get(uid, {})
        if st.get("name") != "preview":
            answer_callback(cb["id"], "No preview found!", True)
            return

        if not get_user_emojis(uid):
            answer_callback(cb["id"], "No Premium Custom Emoji ID saved. Send not allowed.", True)
            return

        final_text = st.get("final_text")
        final_entities = st.get("final_entities")
        if final_text is None:
            final_text = st.get("premium_text", "")
        if final_entities is None:
            final_entities = st.get("entities", [])

        has_custom_entity = any(e.get("type") == "custom_emoji" and e.get("custom_emoji_id") for e in (final_entities or []))
        if not has_custom_entity:
            answer_callback(cb["id"], "Premium preview entities missing. Please press Again or create post again.", True)
            return

        # User wants the exact bot-generated preview message to be forwarded to
        # the channel, so the channel post shows "Forwarded from <bot>". This
        # forwards ONLY the bot preview message, never the user's original post.
        # Telegram forwardMessage has no reply_markup parameter, so inline
        # preview buttons are not attached to the channel post.
        channels = get_user_channels(uid)
        if not channels:
            answer_callback(cb["id"], "No channel added. Please add a channel first.", True)
            return
        st.update({"final_text": final_text, "final_entities": final_entities, "name": "preview_select_channel"})
        selector = send_message(chat_id, "📢 Select channel for this post:", reply_markup=channel_select_keyboard(uid))
        if selector.get("ok"):
            st["control_message_id"] = selector["result"]["message_id"]
        STATES[uid] = st
        answer_callback(cb["id"])
        return

    if data and data.startswith("select_post_channel_"):
        st = STATES.get(uid, {})
        if st.get("name") not in ("preview", "preview_select_channel"):
            answer_callback(cb["id"], "No preview found!", True)
            return
        channel_id = int(data.replace("select_post_channel_", ""))
        ch = query_one("SELECT * FROM user_channels WHERE user_id=? AND channel_id=?", (uid, channel_id))
        if not ch:
            answer_callback(cb["id"], "Channel not found.", True)
            return
        st.update({"name": "preview_channel_selected", "selected_channel_id": channel_id, "selected_channel_name": ch.get("channel_title") or ch.get("channel_username") or str(channel_id)})
        STATES[uid] = st
        edit_message_text(chat_id, mid, f"Selected channel:\n{st['selected_channel_name']}\n\nChoose action:", reply_markup=send_options_keyboard())
        answer_callback(cb["id"])
        return

    if data == "send_now_post":
        st = STATES.get(uid, {})
        if st.get("name") != "preview_channel_selected":
            answer_callback(cb["id"], "No selected preview found!", True)
            return
        final_text = st.get("final_text") or st.get("premium_text", "")
        final_entities = st.get("final_entities") or st.get("entities", [])
        has_custom_entity = any(e.get("type") == "custom_emoji" and e.get("custom_emoji_id") for e in (final_entities or []))
        if not has_custom_entity:
            answer_callback(cb["id"], "Premium preview entities missing. Please create post again.", True)
            return
        target_channel = st.get("selected_channel_id")
        r = forward_message(target_channel, chat_id, st.get("preview_message_id"))
        if r.get("ok"):
            execute("INSERT INTO posts (user_id, channel_id, channel_username, message_id, content_type, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, target_channel, st.get("selected_channel_name"), r["result"]["message_id"], st["content_type"], final_text, datetime.now().isoformat()))
            execute("UPDATE users SET total_posts=total_posts+1 WHERE user_id=?", (uid,))
            delete_message(chat_id, st.get("preview_message_id"))
            try:
                delete_message(chat_id, mid)
            except Exception:
                pass
            STATES.pop(uid, None)
            send_message(chat_id, "✅ Post forwarded successfully!", reply_markup=main_reply_keyboard())
            answer_callback(cb["id"])
        else:
            log("forward preview failed: " + str(r.get("description")))
            answer_callback(cb["id"], "Failed to forward preview. Bot admin hai ya nahi check karo.", True)
        return

    if data == "schedule_post":
        st = STATES.get(uid, {})
        if st.get("name") != "preview_channel_selected":
            answer_callback(cb["id"], "No selected preview found!", True)
            return
        st["name"] = "waiting_schedule_date"
        STATES[uid] = st
        edit_message_text(chat_id, mid, "Enter schedule date:\nFormat: DD-MM-YYYY\nExample: 28-06-2026", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "cancel_send_post":
        st = STATES.get(uid, {})
        if st.get("preview_message_id"):
            try:
                delete_message(chat_id, st.get("preview_message_id"))
            except Exception:
                pass
        STATES.pop(uid, None)
        try:
            delete_message(chat_id, mid)
        except Exception:
            pass
        send_message(chat_id, "Post sending cancelled.", reply_markup=main_reply_keyboard())
        answer_callback(cb["id"])
        return

    if data and data.startswith("sched_send_"):
        job_id = int(data.replace("sched_send_", ""))
        job = query_one("SELECT * FROM scheduled_posts WHERE id=? AND user_id=? AND status='pending'", (job_id, uid))
        if not job:
            answer_callback(cb["id"], "Scheduled post not found.", True)
            return
        r = forward_message(job["channel_id"], job["from_chat_id"], job["preview_message_id"])
        if r.get("ok"):
            execute("UPDATE scheduled_posts SET status='sent' WHERE id=?", (job_id,))
            execute("INSERT INTO posts (user_id, channel_id, channel_username, message_id, content_type, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, job["channel_id"], job.get("channel_username"), r["result"]["message_id"], job.get("content_type"), job.get("text"), datetime.now().isoformat()))
            execute("UPDATE users SET total_posts=total_posts+1 WHERE user_id=?", (uid,))
            edit_message_text(chat_id, mid, scheduled_posts_text(uid), reply_markup=scheduled_posts_keyboard(uid))
            answer_callback(cb["id"], "Post sent now.")
        else:
            answer_callback(cb["id"], "Failed to send scheduled post.", True)
        return

    if data and data.startswith("sched_del_"):
        job_id = int(data.replace("sched_del_", ""))
        execute("UPDATE scheduled_posts SET status='canceled' WHERE id=? AND user_id=? AND status='pending'", (job_id, uid))
        edit_message_text(chat_id, mid, scheduled_posts_text(uid), reply_markup=scheduled_posts_keyboard(uid))
        answer_callback(cb["id"], "Scheduled post deleted.")
        return

    if data == "again_post":
        st = STATES.get(uid, {})
        if st.get("name") not in ("preview", "preview_select_channel", "preview_channel_selected"): 
            answer_callback(cb["id"], "No preview found!", True)
            return
        emojis = get_user_emojis(uid)
        idx = (st.get("emoji_index", 0) + 1) % max(len(emojis), 1)
        new_text, entities = build_premium_text(st.get("original_text", ""), emojis, idx)
        if st.get("content_type") == "text":
            r = edit_message_text(chat_id, st["preview_message_id"], new_text, entities=entities, reply_markup=preview_keyboard())
        else:
            r = edit_message_caption(chat_id, st["preview_message_id"], new_text, caption_entities=entities, reply_markup=preview_keyboard())
        if r.get("ok"):
            st.update({
                "name": "preview",
                "emoji_index": idx,
                "final_text": new_text,
                "final_entities": entities,
                "premium_text": new_text,
                "entities": entities,
            })
            STATES[uid] = st
            answer_callback(cb["id"], "Preview updated!")
        else:
            answer_callback(cb["id"], "Preview update failed.", True)
        return

    if data == "close_post":
        st = STATES.get(uid, {})
        delete_message(chat_id, st.get("preview_message_id", mid))
        STATES.pop(uid, None)
        send_message(chat_id, "Preview closed.", reply_markup=main_reply_keyboard())
        answer_callback(cb["id"])
        return

    # Admin callbacks
    if data and data.startswith("admin") or data and data.startswith("remove_"):
        if not is_admin(uid):
            answer_callback(cb["id"], "Unauthorized!", True)
            return

    if data == "admin_back":
        delete_message(chat_id, mid)
        menu_text, menu_entities = premium_main_menu_text_entities()
        send_message(chat_id, menu_text, reply_markup=main_reply_keyboard(), entities=menu_entities)
        answer_callback(cb["id"])
        return

    if data == "admin_add_force":
        STATES[uid] = {"name": "waiting_for_force_emoji"}
        edit_message_text(chat_id, mid, "JOIN setup ke liye Telegram Premium Custom Emoji send karo ya raw custom_emoji_id paste karo. Button text plain JOIN rahega kyunki inline button custom emoji entities support nahi karta.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_remove_force":
        channels = query_all("SELECT * FROM force_join_channels")
        if not channels:
            answer_callback(cb["id"], "No force join channels set!", True)
            return
        rows = [[btn("❌ " + str(ch.get("channel_username") or ch["channel_id"]), f"remove_force_{ch['channel_id']}")] for ch in channels]
        rows.append([btn("🔙 Back", "admin_back_to_panel")])
        edit_message_text(chat_id, mid, "Select channel to remove:", reply_markup=ik(rows))
        answer_callback(cb["id"])
        return

    if data and data.startswith("remove_force_"):
        channel_id = int(data.replace("remove_force_", ""))
        execute("DELETE FROM force_join_channels WHERE channel_id=?", (channel_id,))
        edit_message_text(chat_id, mid, "✅ Channel removed from force join.", reply_markup=admin_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_back_to_panel":
        edit_message_text(chat_id, mid, "🔧 Admin Panel", reply_markup=admin_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_broadcast":
        STATES[uid] = {"name": "waiting_for_broadcast"}
        edit_message_text(chat_id, mid, "Broadcast message bhejo. Text, photo, video, document, animation, forwarded message sab support hain.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_bot_toggle":
        cur = bot_on()
        set_bot_on(not cur)
        answer_callback(cb["id"], f"Bot is now {'ON' if not cur else 'OFF'}!", True)
        edit_message_text(chat_id, mid, "🔧 Admin Panel", reply_markup=admin_keyboard())
        return

    if data == "admin_stats":
        users = query_one("SELECT COUNT(*) c FROM users")["c"]
        posts = query_one("SELECT COUNT(*) c FROM posts")["c"]
        chans = query_one("SELECT COUNT(*) c FROM force_join_channels")["c"]
        update_channel = get_setting("update_channel_username", "Not set")
        text = f"📊 <b>Statistics</b>\n\nTotal Users: {users}\nTotal Posts: {posts}\nForce Join Channels: {chans}\nUpdate Channel: {html_escape(update_channel)}"
        edit_message_text(chat_id, mid, text, parse_mode="HTML", reply_markup=admin_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_set_welcome":
        STATES[uid] = {"name": "waiting_for_welcome"}
        edit_message_text(chat_id, mid, "Welcome message bhejo. Isme Telegram premium emoji use kar sakte ho; bot same premium emoji entities save karega.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_set_update_channel":
        STATES[uid] = {"name": "waiting_update_channel"}
        edit_message_text(chat_id, mid, "Update channel set karne ke liye @channel username bhejo ya channel se message forward karo. Bot us channel me admin hona chahiye.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_add_admin":
        STATES[uid] = {"name": "waiting_for_new_admin"}
        edit_message_text(chat_id, mid, "New admin ka user ID bhejo.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_remove_admin":
        admins = query_all("SELECT user_id FROM admins")
        if not admins:
            answer_callback(cb["id"], "No admins found!", True)
            return
        rows = [[btn(f"❌ {a['user_id']}", f"remove_admin_{a['user_id']}")] for a in admins]
        rows.append([btn("🔙 Back", "admin_back_to_panel")])
        edit_message_text(chat_id, mid, "Select admin to remove:", reply_markup=ik(rows))
        answer_callback(cb["id"])
        return

    if data and data.startswith("remove_admin_"):
        aid = int(data.replace("remove_admin_", ""))
        execute("DELETE FROM admins WHERE user_id=?", (aid,))
        edit_message_text(chat_id, mid, "✅ Admin removed.", reply_markup=admin_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_add_emoji":
        STATES[uid] = {"name": "waiting_for_admin_emoji"}
        edit_message_text(chat_id, mid, "Global Telegram Premium Custom Emojis send karo ya one/multiple raw custom_emoji_id paste karo.", reply_markup=cancel_keyboard())
        answer_callback(cb["id"])
        return

    if data == "admin_remove_emoji":
        emojis = query_all("SELECT id FROM user_emojis WHERE added_by_admin=1 AND emoji_id IS NOT NULL AND emoji_id != '' LIMIT 1")
        if not emojis:
            answer_callback(cb["id"], "No global emojis found.", True)
            return
        edit_message_text(chat_id, mid, "Select emoji to delete:", reply_markup=admin_delete_emoji_keyboard())
        answer_callback(cb["id"])
        return

    if data and data.startswith("admin_del_emoji_"):
        emoji_row_id = int(data.replace("admin_del_emoji_", ""))
        execute("DELETE FROM user_emojis WHERE id=? AND added_by_admin=1", (emoji_row_id,))
        emojis_left = query_all("SELECT id FROM user_emojis WHERE added_by_admin=1 AND emoji_id IS NOT NULL AND emoji_id != '' LIMIT 1")
        if emojis_left:
            edit_message_text(chat_id, mid, "Deleted. Select another emoji to delete:", reply_markup=admin_delete_emoji_keyboard())
        else:
            edit_message_text(chat_id, mid, "All global emojis deleted.", reply_markup=admin_keyboard())
        answer_callback(cb["id"], "Emoji deleted.")
        return

    if data == "admin_del_all_emojis":
        execute("DELETE FROM user_emojis WHERE added_by_admin=1")
        edit_message_text(chat_id, mid, "All global emojis deleted.", reply_markup=admin_keyboard())
        answer_callback(cb["id"], "All deleted.")
        return

    answer_callback(cb["id"])


def handle_update(update: Dict[str, Any], bot_id: int):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"], bot_id)
            return
        if "message" not in update:
            return
        message = update["message"]
        user = message.get("from") or {}
        if not user:
            return
        add_user(user)
        text = message.get("text") or ""
        uid = user["id"]

        if text.startswith("/start"):
            handle_start(message, bot_id)
            return
        if text.startswith("/admin"):
            handle_admin_cmd(message)
            return

        if STATES.get(uid):
            handle_state_message(message, bot_id)
            return

        clean_text = text.replace("📝", "").replace("📋", "").replace("👤", "").replace("😀", "").replace("📢", "").replace("👑", "").replace("🕒", "").replace("📊", "").replace("📈", "").strip().lower()
        if clean_text == "create post":
            if not get_user_channels(uid):
                send_message(message["chat"]["id"], "Please add a channel first.", reply_markup=channels_manage_keyboard(uid))
                return
            if not get_user_emojis(uid):
                send_message(message["chat"]["id"], "Please add premium emojis first!", reply_markup=main_reply_keyboard())
                return
            STATES[uid] = {"name": "waiting_for_post"}
            send_message(message["chat"]["id"], "Apna post bhejo. Text, photo, video, document, animation sab allow hain.", reply_markup=cancel_keyboard())
            return
        if clean_text == "check post":
            send_message(message["chat"]["id"], build_post_stats_text(uid), reply_markup=check_post_keyboard(uid))
            return
        if clean_text == "scheduled posts":
            send_message(message["chat"]["id"], scheduled_posts_text(uid), reply_markup=scheduled_posts_keyboard(uid))
            return
        if clean_text == "statistics":
            send_message(message["chat"]["id"], user_statistics_text(uid), reply_markup=main_reply_keyboard())
            return
        if clean_text == "leaderboard":
            send_message(message["chat"]["id"], leaderboard_text(), reply_markup=main_reply_keyboard())
            return
        if clean_text == "channels":
            channels = get_user_channels(uid)
            text_channels = f"📢 Your added channels: {len(channels)}"
            if channels:
                for ch in channels:
                    text_channels += f"\n• {ch.get('channel_title') or ch.get('channel_username') or ch['channel_id']}"
            send_message(message["chat"]["id"], text_channels, reply_markup=channels_manage_keyboard(uid))
            return
        if clean_text == "owner":
            send_message(message["chat"]["id"], "👑 Owner:", reply_markup=owner_keyboard())
            return
        if clean_text == "profile":
            u = query_one("SELECT * FROM users WHERE user_id=?", (uid,))
            if u:
                channel_count = len(get_user_channels(uid))
                profile_text = (f"👤 <b>Profile</b>\n\n"
                                f"Name: {html_escape(u.get('first_name') or 'N/A')}\n"
                                f"ID: <code>{uid}</code>\n"
                                f"Username: @{html_escape(u.get('username')) if u.get('username') else 'N/A'}\n"
                                f"Total Posts: {u.get('total_posts', 0)}\n"
                                f"Added Channels: {channel_count}")
                send_message(message["chat"]["id"], profile_text, parse_mode="HTML", reply_markup=main_reply_keyboard())
            return
        if clean_text == "add emoji":
            STATES[uid] = {"name": "waiting_for_emoji"}
            send_message(message["chat"]["id"], "Telegram Premium Custom Emoji send karo ya one/multiple raw custom_emoji_id paste karo.", reply_markup=cancel_keyboard())
            return

        # Default fallback
        if bot_on() or is_admin(uid):
            show_main(message["chat"]["id"], uid)
        else:
            send_message(message["chat"]["id"], "Bot is currently off. Please try again later.")
    except Exception as e:
        log_error("handle_update", e)
        try:
            chat_id = safe_get(update, "message", "chat", "id") or safe_get(update, "callback_query", "message", "chat", "id")
            if chat_id:
                send_message(chat_id, "Temporary error occurred. Please try again.")
        except Exception:
            pass


# ===================== MAIN LONG POLLING =====================
def main():
    print("Starting Telegram Premium Emoji Bot...")
    print("Premium Custom Emoji rotation enabled. Fast polling enabled.")
    print("This bot.py needs only Python. No aiogram / no modules required.")
    init_db()

    me = get_me()
    if not me.get("ok"):
        print("Bot token invalid ya internet problem hai.")
        print("Telegram API response:", me.get("description"))
        return
    bot_id = me["result"]["id"]
    print(f"Bot started: @{me['result'].get('username')} | Admins: {ADMIN_IDS}")
    log(f"Bot started. bot_id={bot_id}, admins={ADMIN_IDS}")

    offset = 0
    while True:
        try:
            res = api_call("getUpdates", {"offset": offset, "timeout": 1, "allowed_updates": ["message", "callback_query"]}, timeout=5)
            if not res.get("ok"):
                log("getUpdates failed: " + str(res.get("description")))
                continue
            process_due_jobs()
            for upd in res.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                handle_update(upd, bot_id)
            process_due_jobs()
        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            log_error("main_loop", e)
            continue


if __name__ == "__main__":
    main()
