import os
import json
import requests # Still needed for some specific non-session calls if any, but mostly replaced by fast_session
import datetime
import pytz
import uuid
import hashlib # For password hashing
import secrets # For temporary codes
import html # For escaping user names
import traceback # For detailed error logging
from urllib.parse import quote # 🆕 ADDED FOR URL ENCODING
from flask import Flask, request, jsonify, send_file
import atexit # 🆕 ADDED for scheduler cleanup
from apscheduler.schedulers.background import BackgroundScheduler # 🆕 ADDED for scheduler

# ⚡ SPEED OPTIMIZATION IMPORTS
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 🚨 REAL API DEPENDENCY: requests_oauthlib is required for posting
try:
    from requests_oauthlib import OAuth1Session
except ImportError:
    print("Warning: requests_oauthlib not found. Real API posting will fail.")
    class OAuth1Session:
        def __init__(self, *args, **kwargs): pass
        def post(self, url, data, **kwargs): # Changed json to data for v1.1 compatibility
            return type('MockResponse', (object,), {'json': lambda: {'errors': [{'message': 'OAuth library missing.', 'code': 999}]}, 'status_code': 401})()


# ==============================
# 🔒 HARDCODED SECRETS & CONFIG
# ==============================
WEBHOOK_SECRET = "c4a8b97e3c92a6ff12d7f183f5e65bc1"
MAIN_BOT_TOKEN = "8428126884:AAEjbdgawLFrNqrxTsMS95MfMH0LouDJbYw"
LINK_BOT_TOKEN = "8196170842:AAHf85B9JPC8ARw2p4Ax0mkRZ0UdU1nyb3s"
ADMIN_BOT_TOKEN = "8272656277:AAEUXGW5MMWwgWuk7W388ntIXFS0nEGniZ8" # Admin Bot Token
PUBLIC_BASE_URL = "https://tweet2telegram-nqgw.onrender.com" # ◀️ আপনার আসল URL ব্যবহার করুন
WEBSITE_URL = "https://bit.ly/flashautomation" # ◀️ ADDED

# Supabase Configuration (PRIMARY for ALL data)
SUPABASE_URL = "https://xebasaxioqlbnrzkmyjh.supabase.co"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhlYmFzYXhpb3FsYm5yemtteWpoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MzEzMjk3OCwiZXhwIjoyMDc4NzA4OTc4fQ.n2tGsrKODd6Dp7OtbsE3xOQ_wPtdU4QHLaBqtT7tcRA"
SB_TABLE_ACCOUNTS = "user_x_accounts" # This was already in Supabase
SB_HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}
SB_HEADERS_UPSERT = {**SB_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}

# Super admins (can manage keys/admins, broadcast, block/unblock)
ADMIN_IDS = [6535216093]
ADMIN_CONTACT = "@ox_anonymous"

# Bot usernames (for UX hints)
MAIN_BOT_USERNAME = "@TweetAutomation_bot"
LINK_BOT_USERNAME = "@TweetXLinks_bot"
ADMIN_BOT_USERNAME = "@TweetAdminBotplusWeb_bot"

# ❌ REMOVED: DATABASE_NAME = "automation.db"
TIMEZONE = "Asia/Kolkata" # Indian Time
kolkata_tz = pytz.timezone(TIMEZONE)

MAIN_BOT_API = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}"
LINK_BOT_API = f"https://api.telegram.org/bot{LINK_BOT_TOKEN}"
ADMIN_BOT_API = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}"

# Configuration for new features
MAX_ACCOUNTS_PER_USER = 250  # 🔧 Maximum 250 accounts/tweets
MAX_TWEET_LINES = 25
MAX_MESSAGE_LENGTH = 4000
MAX_SCHEDULE_DAYS = 365
PASSWORD_RESET_TIMEOUT_MINUTES = 10

API_KEY_STEPS = [
    {"name": "X Username", "field": "username", "prompt": "🔑 Enter the **X Username** (e.g., myhandle)."},
    {"name": "API Key", "field": "api_key", "prompt": "🔒 Enter the **API Key (Consumer Key)**."},
    {"name": "API Secret Key", "field": "api_secret", "prompt": "🔐 Enter the **API Key Secret (Consumer Secret)**."},
    {"name": "Access Token", "field": "access_token", "prompt": "🧰 Enter the **Access Token**."},
    {"name": "Access Token Secret", "field": "access_token_secret", "prompt": "🗝️ Enter the **Access Token Secret**."},
    {"name": "Bearer Token (Optional)", "field": "bearer_token", "prompt": "🪪 (Optional) Enter the **Bearer Token** (or type 'skip')."}
]

# ⚡ SPEED OPTIMIZATION: Thread pool for concurrent posting
posting_executor = ThreadPoolExecutor(max_workers=50)

# ⚡ SPEED OPTIMIZATION: Fast HTTP session with connection pooling
def create_fast_session():
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=50,
        pool_maxsize=100,
        max_retries=Retry(total=2, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
    )
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

fast_session = create_fast_session()

# ======================
# 🚀 NEW: SUPABASE HELPERS (OPTIMIZED WITH FAST SESSION)
# ======================
def sb_select(table, params, single=False, select="*"):
    """Generic Supabase SELECT"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
        for key, value in params.items():
            if isinstance(value, str) and ("in." in value or "neq." in value or "lte." in value or "is." in value):
                url += f"&{key}={value}"
            else:
                url += f"&{key}=eq.{value}"

        if single:
            url += "&limit=1"
            
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if single:
            return data[0] if data else None
        return data
    except Exception as e:
        print(f"Supabase SELECT error on table {table}: {e}")
        return None if single else []

def sb_insert(table, data):
    """Generic Supabase INSERT"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        # ⚡ USE FAST SESSION
        r = fast_session.post(url, headers=SB_HEADERS, json=data, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Supabase INSERT error on table {table}: {e}")
        return None

def sb_update(table, data, params):
    """Generic Supabase UPDATE"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?"
        param_list = []
        for key, value in params.items():
            param_list.append(f"{key}=eq.{value}")
        url += "&".join(param_list) # <-- ফিক্স করা হয়েছে
            
        # ⚡ USE FAST SESSION
        r = fast_session.patch(url, headers=SB_HEADERS, json=data, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Supabase UPDATE error on table {table}: {e}")
        return False

def sb_upsert(table, data_list, on_conflict=None):
    """Generic Supabase UPSERT"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        
        headers = SB_HEADERS_UPSERT
        if on_conflict:
             headers = {**headers, "Prefer": f"return=representation,resolution=merge-duplicates,on_conflict={on_conflict}"}

        # ⚡ USE FAST SESSION
        r = fast_session.post(url, headers=headers, json=data_list, timeout=10)
        
        if r.status_code not in [200, 201]:
            print(f"Supabase UPSERT error on table {table} ({r.status_code}): {r.text}")
            r.raise_for_status()
            
        return r.json()
    except Exception as e:
        print(f"Supabase UPSERT error on table {table}: {e}")
        return None

def sb_delete(table, params):
    """Generic Supabase DELETE"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?"
        query_params = []
        for key, value in params.items():
            # Support for "in" operator, e.g., {"id": "in.(1,2,3)"}
            if isinstance(value, str) and ("in." in value or "neq." in value or "lte." in value or "is." in value):
                query_params.append(f"{key}={value}")
            else:
                query_params.append(f"{key}=eq.{value}")
        
        url += "&".join(query_params)
            
        # ⚡ USE FAST SESSION
        r = fast_session.delete(url, headers=SB_HEADERS, timeout=10)
        
        if r.status_code not in [200, 204]:
            print(f"Supabase DELETE error on table {table} ({r.status_code}): {r.text}")
            r.raise_for_status()
            
        # ✅ FIX: Safe parsing of Content-Range header
        content_range = r.headers.get('Content-Range', '0-0/0')
        
        # Handle both "0-4/5" and "*/0" formats
        try:
            if '/' in content_range:
                count = int(content_range.split('/')[-1])
            else:
                count = 0
        except (ValueError, IndexError):
            count = 0
            
        return count

    except Exception as e:
        print(f"Supabase DELETE error on table {table}: {e}")
        return 0

# ============
# HELPERS (Updated to use Supabase)
# ============
def now_utc_iso():
    return datetime.datetime.now(pytz.utc).isoformat(timespec='milliseconds')

def tz_now_str():
    return datetime.datetime.now(kolkata_tz).strftime("%Y-%m-%d %I:%M:%S %p %Z")

def hash_password(password: str) -> str:
    """Hashes a password using SHA256."""
    return hashlib.sha256(password.encode()).hexdigest()

def check_password(password: str, hash: str) -> bool:
    """Checks if a given password matches the hash."""
    return hash_password(password) == hash

def send_msg(api_url, chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        # ⚡ USE FAST SESSION
        r = fast_session.post(f"{api_url}/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return True, r.status_code
    except Exception as e:
        print(f"send_msg error to {chat_id}: {e}")
        return False, str(e)

def is_admin(uid: int) -> bool:
    row = sb_select("admins", {"tg_id": uid}, single=True)
    return row is not None

def is_super_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_blocked(uid: int) -> bool:
    row = sb_select("users", {"tg_id": uid}, single=True, select="blocked")
    return bool(row and row["blocked"])

def set_state(uid: int, scope: str, state: str, data: dict = None):
    data_to_upsert = {
        "tg_id": uid,
        "scope": scope,
        "state": state,
        "data": data if data is not None else {},
        "updated_at": now_utc_iso()
    }
    sb_upsert("user_state", [data_to_upsert], on_conflict="tg_id")

def get_state(uid: int):
    row = sb_select("user_state", {"tg_id": uid}, single=True, select="scope,state,data")
    if row:
        data = row.get("data", {})
        if not isinstance(data, dict):
            data = {}
        return (row["scope"], row["state"], data)
    return (None, None, {})

def clear_state(uid: int):
    sb_delete("user_state", {"tg_id": uid})

def record_user(user):
    uid = user["id"]
    uname = user.get("username")
    data_to_upsert = {
        "tg_id": uid,
        "username": uname,
        "joined_at": now_utc_iso()
    }
    # Only update username on conflict
    sb_upsert("users", [data_to_upsert], on_conflict="tg_id")


def set_webhook(bot_token, path):
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    full = f"{PUBLIC_BASE_URL}{path}"
    try:
        r = fast_session.get(url, params={"url": full}, timeout=10)
        print("setWebhook", path, r.status_code, r.text)
    except Exception as e:
        print("setWebhook error:", path, e)

def setup_webhooks():
    set_webhook(MAIN_BOT_TOKEN, "/webhook_main")
    set_webhook(LINK_BOT_TOKEN, "/webhook_link")
    set_webhook(ADMIN_BOT_TOKEN, "/webhook_admin")

def parse_indian_datetime(text: str) -> datetime.datetime or None:
    # ◀️ UPDATED: This function is now simpler, it only parses "YYYY-MM-DD HH:MM AM/PM"
    india_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(india_tz).replace(microsecond=0)
    text = text.strip()
    
    dt_format = "%Y-%m-%d %I:%M %p"  
    
    try:
        dt = datetime.datetime.strptime(text, dt_format)
        dt = india_tz.localize(dt)

        max_dt = now + datetime.timedelta(days=MAX_SCHEDULE_DAYS)
        if dt > now and dt < max_dt:
            return dt.astimezone(pytz.utc).replace(microsecond=0)
            
    except Exception as e:
        print(f"Parse datetime error: {e}")
        pass
        
    return None

# --- Supabase API Functions (NOW PRIMARY) ---

# ◀️ UPDATED: Using your new debug function
def sb_upsert_account(uid: int, keys: dict) -> bool:
    username = keys.get("username", "").strip().lower()
    
    if not username:
        print("❌ Username is empty!")
        return False
    
    payload = {
        "tg_id": uid,
        "username": username,
        "api_key": keys.get("api_key", ""),
        "api_secret": keys.get("api_secret", ""),
        "access_token": keys.get("access_token", ""),
        "access_token_secret": keys.get("access_token_secret", ""),
        "bearer_token": keys.get("bearer_token")
    }
    
    print(f"\n{'='*60}")
    print(f"🔍 SUPABASE UPSERT ATTEMPT (user_x_accounts)")
    print(f"{'='*60}")
    print(f"User ID: {uid}")
    print(f"Username: {username}")
    
    try:
        # We can use upsert directly
        url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}"
        # Upsert based on the composite primary key (tg_id, username)
        headers = {**SB_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates,on_conflict=tg_id,username"}
        
        # ⚡ USE FAST SESSION
        r = fast_session.post(url, headers=headers, json=payload, timeout=10)
        
        print(f"\n📊 UPSERT Request:")
        print(f"URL: {url}")
        print(f"Status Code: {r.status_code}")
        print(f"Response Body: {r.text}")
        
        if r.status_code in (200, 201):
            print(f"\n✅ SUCCESS! Account @{username} linked for user {uid}")
            print(f"{'='*60}\n")
            return True
        else:
            print(f"\n❌ FAILED!")
            try:
                error_detail = r.json()
                print(f"Error Details: {json.dumps(error_detail, indent=2)}")
            except:
                pass
            print(f"{'='*60}\n")
            return False
            
    except requests.exceptions.Timeout:
        print(f"\n⏱️ TIMEOUT! Supabase request took too long")
        print(f"{'='*60}\n")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"\n🔌 CONNECTION ERROR! Cannot reach Supabase")
        print(f"Error: {e}")
        print(f"{'='*60}\n")
        return False
    except Exception as e:
        print(f"\n💥 EXCEPTION!")
        print(f"Error: {e}")
        traceback.print_exc()
        print(f"{'='*60}\n")
        return False

def sb_list_accounts(uid: int):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}?tg_id=eq.{uid}&select=username,api_key,api_secret,access_token,access_token_secret,bearer_token"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"Supabase list exception: {e}")
    return [] # Return empty list on failure

def sb_delete_account(uid: int, username: str) -> bool:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}?tg_id=eq.{uid}&username=eq.{username}"
        # ⚡ USE FAST SESSION
        r = fast_session.delete(url, headers=SB_HEADERS, timeout=10)
        if r.status_code in (200, 204):
            return True
    except Exception as e:
        print(f"Supabase delete exception: {e}")
    return False

# --- Local Fallback/Primary DB Helpers (Updated) ---

def get_user_x_accounts(uid: int):
    return sb_list_accounts(uid) # Directly call the Supabase function

def delete_user_account(uid: int, username: str) -> bool:
    return sb_delete_account(uid, username) # Directly call the Supabase function

# 
# 🛠️ *** FIX 1 ***
# `get_unscheduled_tweets` ফাংশনটি প্রতিস্থাপন করা হয়েছে
# 
def get_unscheduled_tweets(uid: int):
    # ✅ FIX: সব প্রয়োজনীয় columns SELECT করুন
    params = {
        "tg_id": uid,
        "post_status": "PENDING",
        "scheduled_time": "is.null"
    }
    
    # Direct Supabase query with proper select
    try:
        url = f"{SUPABASE_URL}/rest/v1/scheduled_tweets?select=id,tweet_text,tg_id,post_status,scheduled_time"
        url += f"&tg_id=eq.{uid}"
        url += "&post_status=eq.PENDING"
        url += "&scheduled_time=is.null"
        url += "&order=id.asc"
            
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Supabase get_unscheduled_tweets error: {e}")
        return []

# 
# ✅ *** FINAL FIX: API v2 ***
# `post_tweet_to_x` ফাংশনটি প্রতিস্থাপন করা হয়েছে
#
def post_tweet_to_x(tweet_id, user_id, account_username, tweet_text):
    accounts = get_user_x_accounts(user_id)  
    
    keys = next((acc for acc in accounts if acc['username'] == account_username), None)
    
    if not keys or not keys['api_key'] or not keys['access_token'] or not keys['access_token_secret']:
        return None, "Account credentials missing or incomplete."

    # ✅ USE API v2 (Free tier compatible)
    url = "https://api.twitter.com/2/tweets"
    
    auth = OAuth1Session(
        keys['api_key'],
        client_secret=keys['api_secret'],
        resource_owner_key=keys['access_token'],
        resource_owner_secret=keys['access_token_secret']
    )
    
    payload = {"text": tweet_text}
    
    try:
        # ⚡ USE FAST SESSION TIMEOUT
        response = auth.post(url, json=payload, timeout=10)
        
        if response.status_code == 201:
            data = response.json()
            tweet_id_str = data.get('data', {}).get('id')
            
            if tweet_id_str:
                tweet_link = f"https://x.com/{account_username}/status/{tweet_id_str}?t=19"
                return tweet_link, None
            else:
                return None, "Tweet posted but ID not found"
        else:
            error_json = None
            error_message = f'HTTP Error {response.status_code}'
            try:
                error_json = response.json()
            except:
                pass
                
            print(f"CRITICAL API ERROR {response.status_code}: {response.text}")
            
            if error_json:
                if 'errors' in error_json and isinstance(error_json['errors'], list) and error_json['errors']:
                    error_message = error_json['errors'][0].get('message', error_message)
                elif 'detail' in error_json:
                    error_message = error_json['detail']
                elif 'title' in error_json:
                    error_message = error_json['title']
            
            if response.status_code == 429:
                error_message = "Rate limit: 17 tweets/day (Free tier). Try tomorrow."
            
            return None, error_message
            
    except Exception as e:
        return None, f"Connection/Library Error: {e}"


# ==========================
# MAIN BOT: COMMAND HANDLER 
# ==========================
def main_bot_handle(update):
    # ❌ Removed: conn = None 
    chat_id = None
    try:
        # ◀️ UPDATED: Handle callback queries for AM/PM
        if "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            uid = cb["from"]["id"]
            data = cb["data"]
            
            scope, state, flow_data = get_state(uid)
            
            if state == "schedule_flow_ampm":
                # Pass the full callback update to the handler
                return main_bot_flow_continue(uid, chat_id, data, state, flow_data, is_callback=True, callback_update=update)
            else:
                # Answer callback to remove "loading"
                # ⚡ USE FAST SESSION
                fast_session.post(f"{MAIN_BOT_API}/answerCallbackQuery", json={"callback_query_id": cb["id"]})
                return jsonify({"ok": True})

        if "message" not in update:
            return jsonify({"ok": True})

        msg = update["message"]
        chat_id = msg["chat"]["id"]
        from_user = msg["from"]
        text = msg.get("text", "") or ""
        uid = from_user["id"]
        
        record_user(from_user)

        if is_blocked(uid):
            send_msg(MAIN_BOT_API, chat_id, f"🚫 You have been blocked by the administrator. Contact {ADMIN_CONTACT} for details.")
            return jsonify({"ok": True})
        
        is_authorized = is_admin(uid)
        
        scope, state, flow_data = get_state(uid)

        # 1. Handle Flow State
        if state:
            if scope == "main":
                return main_bot_flow_continue(uid, chat_id, text, state, flow_data)
            else:
                clear_state(uid)

        # Parse command
        cmd = text.split()[0].lower() if text.startswith("/") else None

        AUTH_COMMANDS = ["/add_account", "/delete_account", "/accounts", "/add_tweet", "/schedule_tweet", "/connectlinkbot", "/status", "/delete_tweet_text", "/connect_web"]
        
        if cmd == "/start":
            first_name = html.escape(from_user.get("first_name", "User"))
            start_text = (
                f"👋 Welcome, <b>{first_name}</b>! This is the <b>Tweet ➜ Telegram Automation Main Bot</b>!\n\n"
                f"Use this bot to manage your accounts and schedule tweets.\n\n"
                f"🌐 <b>Website Dashboard:</b> {WEBSITE_URL}" # ◀️ ADDED LINK
            )
            if not is_authorized:
                start_text += "\n\nAsk admin for an access key and then use <b>/connect</b>."
            else:
                start_text += "\n\nUse <b>/help</b> to see all commands."
                
            send_msg(MAIN_BOT_API, chat_id, start_text)
            return jsonify({"ok": True})

        if cmd == "/help":
            base = (
                "✨ <b>User Commands</b>\n"
                "/start - Welcome message\n"
                "/connect - Use access key to get authorized\n"
                "/cancel - ❌ Cancel any active operation\n"  # 🆕 ADD THIS
            )
            if is_authorized:
                base += (
                    "\n🌐 <b>Authorized Commands</b>\n"
                    "/add_account - Start 6-step X API key setup\n"
                    "/add_tweet - Add a new tweet text (supports bulk add)\n"
                    "/schedule_tweet - Schedule all pending tweets for all accounts\n" # ◀️ UPDATED
                    "/delete_tweet_text - Delete a saved tweet text\n"
                    "/delete_account - Remove a linked account\n"
                    "/accounts - List your linked accounts\n"
                    "/connect_web - Generate web sign-up key for dashboard\n"
                    "/connectlinkbot - Generate Link Bot handshake key\n"
                    "/status - Link Bot connection status\n"
                    "/cancel - ❌ Cancel any active operation"  # 🆕 ADD THIS
                )
            send_msg(MAIN_BOT_API, chat_id, base)
            return jsonify({"ok": True})

        if cmd == "/connect":
            set_state(uid, "main", "waiting_for_access_key", data={})
            send_msg(MAIN_BOT_API, chat_id, "🔑 Please send your <b>access key</b> now.")
            return jsonify({"ok": True})
        
        # 🆕 ==================== /CANCEL COMMAND ====================
        if cmd == "/cancel":
            scope, state, _ = get_state(uid)
            
            if state:
                clear_state(uid)
                
                # Different messages for different operations
                if "add_account" in state:
                    msg = "❌ <b>Account Setup Cancelled</b>"
                elif state == "waiting_for_tweet_text":
                    msg = "❌ <b>Add Tweet Cancelled</b>"
                elif "schedule_flow" in state:
                    msg = "❌ <b>Schedule Cancelled</b>"
                elif "delete" in state:
                    msg = "❌ <b>Delete Cancelled</b>"
                elif state == "waiting_for_access_key":
                    msg = "❌ <b>Authorization Cancelled</b>"
                else:
                    msg = "❌ <b>Operation Cancelled</b>"
                
                send_msg(MAIN_BOT_API, chat_id, msg + "\n\n💡 Use /help for commands.")
            else:
                send_msg(MAIN_BOT_API, chat_id, "⚠️ No active operation to cancel.")
            
            return jsonify({"ok": True})
        # ===========================================================
        
        # 2. Command Guard
        if cmd and not is_authorized and (cmd in AUTH_COMMANDS):
            send_msg(MAIN_BOT_API, chat_id, f"🚫 You are not authorized.\nMessage admin {ADMIN_CONTACT} for access.")
            return jsonify({"ok": True})

        # 3. Authorized Command Execution
        if is_authorized:
            
            if cmd == "/connect_web":
                return handle_connect_web(uid, chat_id)
            
            if cmd == "/add_account":
                return handle_add_account_start(uid, chat_id)
            
            if cmd == "/add_tweet":
                return handle_add_tweet_start(uid, chat_id)
                
            if cmd == "/schedule_tweet":
                # ◀️ UPDATED: New flow starts here
                return handle_schedule_tweet_start(uid, chat_id)

            if cmd == "/delete_tweet_text":
                return handle_delete_tweet_text_start(uid, chat_id)

            if cmd == "/delete_account":
                return handle_delete_account_start(uid, chat_id)

            if cmd == "/accounts":
                return handle_list_accounts(uid, chat_id)

            if cmd == "/connectlinkbot":
                return handle_generate_link_bot_key(uid, chat_id)

            if cmd == "/status":
                return handle_link_bot_status(uid, chat_id)

        # 4. Unknown
        if cmd:
            send_msg(MAIN_BOT_API, chat_id, "❌ Unknown command. Use <b>/help</b>.")
        return jsonify({"ok": True})

    except Exception as e:
        print(f"FATAL ERROR in main webhook: {e}")
        traceback.print_exc()
        if chat_id:
            try:
                send_msg(MAIN_BOT_API, chat_id, "❌ An unexpected error occurred. Please notify the admin.")
            except:
                pass
        return jsonify({"ok": True})

# --- MAIN BOT FLOW CONTINUATION ---
# ◀️ UPDATED: Added is_callback
def main_bot_flow_continue(uid, chat_id, text, state, flow_data, is_callback=False, callback_update=None):
    
    # --- New Schedule Flow ---
    if state == "schedule_flow_date":
        return handle_schedule_flow_date(uid, chat_id, text)
        
    if state == "schedule_flow_time":
        return handle_schedule_flow_time(uid, chat_id, text, flow_data)

    if state == "schedule_flow_ampm":
        # Answer callback if it is one
        if is_callback and callback_update:
            # ⚡ USE FAST SESSION
            fast_session.post(f"{MAIN_BOT_API}/answerCallbackQuery", json={"callback_query_id": callback_update["callback_query"]["id"]})
        return handle_schedule_flow_ampm(uid, chat_id, text, flow_data)
    # --- End New Schedule Flow ---

    if state == "waiting_for_access_key":
        clear_state(uid)
        return handle_access_key(uid, chat_id, text.strip())

    if state.startswith("add_account_step_"):
        return handle_add_account_flow(uid, chat_id, text, state, flow_data)
        
    if state == "waiting_for_tweet_text":
        clear_state(uid)
        return handle_add_tweet_text(uid, chat_id, text)

    # ◀️ DELETED: Old schedule flow states

    if state == "waiting_for_delete_tweet_serial":
        return handle_delete_tweet_text_final(uid, chat_id, text.strip())

    if state == "waiting_for_account_to_delete":
        clear_state(uid)
        return handle_delete_account(uid, chat_id, text.strip())

    # Fallback
    clear_state(uid)
    send_msg(MAIN_BOT_API, chat_id, "⚠️ Flow reset. Please send the command again.")
    return jsonify({"ok": True})

# --- MAIN BOT HANDLERS (Updated to Supabase) ---

def handle_access_key(uid, chat_id, key):
    try:
        row = sb_select("keys", {"key": key, "active": 1}, single=True, select="created_by")
        
        if row:
            # Deactivate key
            updated = sb_update("keys", {"active": 0}, {"key": key, "active": 1})
            
            if not updated:
                print(f"Key update failed for key: {key}") # Log failure
                raise Exception("Key deactivation failed")

            # Add user to admins
            sb_upsert("admins", [{"tg_id": uid}], on_conflict="tg_id")
            
            send_msg(MAIN_BOT_API, chat_id, f"✅ Access granted! Welcome to the system. Use <b>/help</b> for commands.")
        else:
            send_msg(MAIN_BOT_API, chat_id, "❌ Invalid or expired key.")
            
    except Exception as e:
        print(f"Access key handle failed: {e}")
        traceback.print_exc()
        send_msg(MAIN_BOT_API, chat_id, "❌ An error occurred during access check.")
        
    return jsonify({"ok": True})

def handle_add_account_start(uid, chat_id):
    accounts = get_user_x_accounts(uid)
    if len(accounts) >= MAX_ACCOUNTS_PER_USER:
        send_msg(MAIN_BOT_API, chat_id, f"❌ Maximum limit of {MAX_ACCOUNTS_PER_USER} accounts reached.")
        return jsonify({"ok": True})
    
    step = API_KEY_STEPS[0]
    set_state(uid, "main", f"add_account_step_0", data={"current_step": 0, "keys": {}})
    send_msg(MAIN_BOT_API, chat_id, f"📝 **Add Account**\nStep 1/{len(API_KEY_STEPS)}: {step['prompt']}")
    return jsonify({"ok": True})

def handle_add_account_flow(uid, chat_id, text, state, flow_data):
    current_step = flow_data.get("current_step", 0)
    keys = flow_data.get("keys", {})
    
    current_field = API_KEY_STEPS[current_step]["field"]
    
    if text.lower() == "/cancel":
        clear_state(uid)
        send_msg(MAIN_BOT_API, chat_id, "❌ Account setup cancelled.")
        return jsonify({"ok": True})
        
    if current_field == "bearer_token" and text.lower() == "skip":
        keys["bearer_token"] = None
    else:
        keys[current_field] = text.strip()
    
    next_step = current_step + 1

    if next_step < len(API_KEY_STEPS):
        # Continue to next step
        step = API_KEY_STEPS[next_step]
        set_state(uid, "main", f"add_account_step_{next_step}", data={"current_step": next_step, "keys": keys})
        send_msg(MAIN_BOT_API, chat_id, f"📝 **Add Account**\nStep {next_step + 1}/{len(API_KEY_STEPS)}: {step['prompt']}")
    else:
        # Finalize
        clear_state(uid)
        
        username = keys.get('username', '').strip().lstrip('@').lower()
        if not username:
            send_msg(MAIN_BOT_API, chat_id, "❌ Username is required. Account setup failed.")
            return jsonify({"ok": True})
        
        keys['username'] = username # Normalize for storage

        saved = sb_upsert_account(uid, keys)

        if saved:
            send_msg(MAIN_BOT_API, chat_id, f"✅ Account @{username} successfully linked to Supabase! Use <b>/accounts</b> to view.")
        else:
            send_msg(MAIN_BOT_API, chat_id, f"❌ Failed to link account @{username} to Supabase. Please check credentials or contact admin.")

    return jsonify({"ok": True})


def handle_add_tweet_start(uid, chat_id):
    set_state(uid, "main", "waiting_for_tweet_text", data={})
    send_msg(MAIN_BOT_API, chat_id, "✍️ Send the tweet text(s) now.\n\nTo add multiple tweets, separate them with **one blank line**.")
    return jsonify({"ok": True})
    
def handle_add_tweet_text(uid, chat_id, text):
    tweets_to_add = []
    failed_tweets = []
    
    potential_tweets = text.strip().split("\n\n")
    
    for tweet_text in potential_tweets:
        tweet_text = tweet_text.strip()
        if not tweet_text:
            continue
            
        if len(tweet_text.split('\n')) > MAX_TWEET_LINES or len(tweet_text) > 280:
            failed_tweets.append(tweet_text[:50] + "...") # Add preview of failed tweet
        else:
            tweets_to_add.append(tweet_text)

    if not tweets_to_add and not failed_tweets:
        send_msg(MAIN_BOT_API, chat_id, "❌ Tweet text cannot be empty.")
        return jsonify({"ok": True})

    try:
        tweets_to_insert = []
        for tweet_text in tweets_to_add:
            tweets_to_insert.append({
                "tg_id": uid,
                "tweet_text": tweet_text,
                "post_status": "PENDING",
                "created_at": now_utc_iso()
            })
            
        if tweets_to_insert:
            sb_insert("scheduled_tweets", tweets_to_insert)
        
        ok_count = len(tweets_to_add)
        fail_count = len(failed_tweets)
        
        msg = ""
        if ok_count > 0:
            msg += f"✅ {ok_count} tweet(s) saved successfully to Supabase.\nUse <b>/schedule_tweet</b> to set a time and account."
        
        if fail_count > 0:
            msg += f"\n❌ {fail_count} tweet(s) failed (too long: max {MAX_TWEET_LINES} lines / 280 chars)."
        
        send_msg(MAIN_BOT_API, chat_id, msg)
        
    except Exception as e:
        print(f"Add bulk tweet failed: {e}")
        send_msg(MAIN_BOT_API, chat_id, "❌ Failed to save tweet texts to Supabase due to a database error.")
        
    return jsonify({"ok": True})

# ◀️ --- START: NEW MASS SCHEDULE FLOW ---

# STEP 1: Start the flow
def handle_schedule_tweet_start(uid, chat_id):
    tweets = get_unscheduled_tweets(uid)
    accounts = get_user_x_accounts(uid)
    
    if not tweets:
        send_msg(MAIN_BOT_API, chat_id, "❌ No saved tweets. Use <b>/add_tweet</b> first.")
        return jsonify({"ok": True})
        
    if not accounts:
        send_msg(MAIN_BOT_API, chat_id, "❌ No accounts. Use <b>/add_account</b> first.")
        return jsonify({"ok": True})
    
    # 🔧 Enforce 250 limit
    total_accounts = len(accounts)
    usable_accounts = min(total_accounts, 250)
    num_to_schedule = min(usable_accounts, len(tweets))
    num_to_delete = len(tweets) - num_to_schedule

    msg = f"📊 <b>Schedule Status:</b>\n\n"
    msg += f"📱 Accounts: {total_accounts}"
    if total_accounts > 250:
        msg += f" (using first 250)\n"
    else:
        msg += "\n"
    
    msg += f"📝 Unscheduled tweets: {len(tweets)}\n\n"
    msg += f"✅ <b>Will schedule: {num_to_schedule} tweets</b>\n"
    
    if num_to_delete > 0:
        msg += f"🗑️ <b>Will delete: {num_to_delete} extra tweets</b>\n"
    
    msg += f"\n📅 Send the <b>Date</b> (e.g., <code>2025-11-20</code>):"

    set_state(uid, "main", "schedule_flow_date", data={})
    send_msg(MAIN_BOT_API, chat_id, msg)
    return jsonify({"ok": True})

# STEP 2: Get Date, Ask for Time
def handle_schedule_flow_date(uid, chat_id, text):
    try:
        # Simple validation: Check if it's a valid date format
        datetime.datetime.strptime(text.strip(), '%Y-%m-%d')
    except ValueError:
        send_msg(MAIN_BOT_API, chat_id, "❌ Invalid date format. Please send the <b>Date</b> in <code>YYYY-MM-DD</code> format:")
        return jsonify({"ok": True}) # Keep state

    set_state(uid, "main", "schedule_flow_time", data={"date": text.strip()})
    send_msg(MAIN_BOT_API, chat_id, "⏰ Send the <b>Time</b> (e.g., <code>09:30</code> or <code>14:00</code>):")
    return jsonify({"ok": True})

# STEP 3: Get Time, Ask for AM/PM
def handle_schedule_flow_time(uid, chat_id, text, flow_data):
    try:
        # Simple validation: Check if it's a valid time format
        datetime.datetime.strptime(text.strip(), '%H:%M')
    except ValueError:
        send_msg(MAIN_BOT_API, chat_id, "❌ Invalid time format. Please send the <b>Time</b> in <code>HH:MM</code> (24-hour) format:")
        return jsonify({"ok": True}) # Keep state

    # Convert HH:MM to HH:MM AM/PM
    time_obj = datetime.datetime.strptime(text.strip(), '%H:%M')
    time_12hr = time_obj.strftime('%I:%M')
    am_pm = time_obj.strftime('%p')

    flow_data["time"] = time_12hr
    flow_data["ampm"] = am_pm
    
    # We have all data, now call the final scheduling function
    clear_state(uid)
    return mass_schedule_tweets(uid, flow_data["date"], flow_data["time"], flow_data["ampm"], chat_id=chat_id)

# STEP 4: (DEPRECATED - Merged into Step 3)
# def handle_schedule_flow_ampm(uid, chat_id, text, flow_data): ...

# STEP 5: Final Scheduling Logic (◀️ *** NEW LOGIC IMPLEMENTED ***)
def mass_schedule_tweets(uid, date_str, time_str, ampm_str, chat_id=None):
    full_time_str = f"{date_str} {time_str} {ampm_str}"
    parsed_dt_utc = parse_indian_datetime(full_time_str)
    
    if not parsed_dt_utc:
        if chat_id:
            send_msg(MAIN_BOT_API, chat_id, f"❌ Invalid or past schedule time (<code>{full_time_str}</code>). Flow reset.")
        return jsonify({"status": "error", "message": "Invalid or past schedule time."})

    accounts = get_user_x_accounts(uid)
    tweets = get_unscheduled_tweets(uid) # Fetches in ASC order
    
    # 🔧 Enforce 250 account limit
    if len(accounts) > 250:
        accounts = accounts[:250]

    if not accounts or not tweets:
        if chat_id:
            send_msg(MAIN_BOT_API, chat_id, "❌ No accounts or unscheduled tweets found. Flow reset.")
        return jsonify({"status": "error", "message": "No accounts or unscheduled tweets found."})

    num_to_schedule = min(len(accounts), len(tweets))
    tweets_to_schedule = tweets[:num_to_schedule]
    tweets_to_delete = tweets[num_to_schedule:]
    
    scheduled_count = 0
    deleted_count = 0
    
    try:
        # 
        # 🛠️ *** FIX 2 ***
        # `mass_schedule_tweets` এর UPDATE লজিকটি প্রতিস্থাপন করা হয়েছে
        # 
        
        # ✅ FIX: Remove timezone for Supabase storage
        scheduled_time_iso = parsed_dt_utc.strftime('%Y-%m-%dT%H:%M:%S')

        # --- Schedule Tweets ---
        for i in range(num_to_schedule):
            tweet = tweets_to_schedule[i]
            account = accounts[i]
            
            update_data = {
                "scheduled_time": scheduled_time_iso,  # ← Use cleaned format
                "account_username": account['username']
            }
            
            # ✅ FIX: শুধু id দিয়ে UPDATE করুন, বাকি conditions verify করা হয়েছে GET-এ
            try:
                url = f"{SUPABASE_URL}/rest/v1/scheduled_tweets?id=eq.{tweet['id']}"
                # ⚡ USE FAST SESSION
                r = fast_session.patch(url, headers=SB_HEADERS, json=update_data, timeout=10)
                
                if r.status_code in (200, 204):
                    scheduled_count += 1 # <-- Simplified count fix
            except Exception as e:
                print(f"Error scheduling tweet ID {tweet['id']}: {e}")

        # --- Delete Remaining Tweets ---
        if tweets_to_delete:
            ids_to_delete = [t['id'] for t in tweets_to_delete]
            # Build "in" query param: (1,2,3)
            ids_param = f"in.({','.join(map(str, ids_to_delete))})"
            
            deleted_count = sb_delete("scheduled_tweets", {"id": ids_param, "tg_id": uid, "post_status": "PENDING"})

        
        ist_time = parsed_dt_utc.astimezone(kolkata_tz).strftime('%Y-%m-%d %I:%M %p %Z')
        message = f"✅ Successfully scheduled <b>{scheduled_count}</b> tweets for <b>{ist_time}</b>."
        
        if deleted_count > 0:
            message += f"\n🗑️ <b>{deleted_count}</b> extra unscheduled tweets were deleted."
        
        if chat_id:
            send_msg(MAIN_BOT_API, chat_id, message)
        return jsonify({"status": "ok", "message": message, "scheduled_count": scheduled_count, "deleted_count": deleted_count})

    except Exception as e:
        print(f"Mass schedule failed: {e}")
        traceback.print_exc()
        message = f"❌ An internal error occurred during scheduling: {e}. Flow reset."
        if chat_id:
            send_msg(MAIN_BOT_API, chat_id, message)
        return jsonify({"status": "error", "message": "Database error during scheduling."})


# ◀️ --- END: NEW MASS SCHEDULE FLOW ---


def handle_delete_tweet_text_start(uid, chat_id):
    tweets = get_unscheduled_tweets(uid)
    
    if not tweets:
        send_msg(MAIN_BOT_API, chat_id, "❌ No unscheduled tweet text found to delete.")
        return jsonify({"ok": True})

    tweet_list = []
    for t in tweets:
        preview = t["tweet_text"].split('\n')[0][:50]
        tweet_list.append(f"ID <code>{t['id']}</code>: {preview}...")
        
    set_state(uid, "main", "waiting_for_delete_tweet_serial", data={})
    
    msg = (
        "🗑️ <b>Delete Tweet Text</b>\n"
        "<b>Available Unscheduled Tweets:</b>\n" + "\n".join(tweet_list) +
        "\n\nSend the **Tweet ID** (e.g., `123`) to permanently delete the text."
    )
    send_msg(MAIN_BOT_API, chat_id, msg)
    return jsonify({"ok": True})

def handle_delete_tweet_text_final(uid, chat_id, tweet_id_text):
    clear_state(uid)
    try:
        tweet_id = int(tweet_id_text)
    except ValueError:
        send_msg(MAIN_BOT_API, chat_id, "❌ Invalid Tweet ID format. Must be a number. Flow reset.")
        return jsonify({"ok": True})

    try:
        # We can only delete tweets that are PENDING and NOT SCHEDULED (scheduled_time is null)
        delete_params = {
            "id": tweet_id,
            "tg_id": uid,
            "post_status": "PENDING",
            "scheduled_time": "is.null"
        }
        deleted_count = sb_delete("scheduled_tweets", delete_params)
        
        if deleted_count == 0:
            # Check why it failed
            row = sb_select("scheduled_tweets", {"id": tweet_id, "tg_id": uid}, single=True, select="post_status,scheduled_time")
            if row:
                status = row['post_status']
                if status == 'POSTED':
                    send_msg(MAIN_BOT_API, chat_id, f"❌ Cannot delete Tweet ID <code>{tweet_id}</code>. It has already been posted.")
                elif row['scheduled_time']:
                    send_msg(MAIN_BOT_API, chat_id, f"❌ Cannot delete scheduled Tweet ID <code>{tweet_id}</code>. You must use the web dashboard to manage scheduled ones.")
                else:
                    send_msg(MAIN_BOT_API, chat_id, f"❌ Tweet ID <code>{tweet_id}</code> not found or already processed.")
            else:
                send_msg(MAIN_BOT_API, chat_id, f"❌ Tweet ID <code>{tweet_id}</code> not found or not owned by you.")
        else:
            send_msg(MAIN_BOT_API, chat_id, f"✅ Tweet text <code>{tweet_id}</code> successfully deleted.")
            
    except Exception as e:
        print(f"Delete tweet failed: {e}")
        send_msg(MAIN_BOT_API, chat_id, "❌ An error occurred during deletion. Flow reset.")
        
    return jsonify({"ok": True})

def handle_delete_account_start(uid, chat_id):
    accounts = get_user_x_accounts(uid)
    if not accounts:
        send_msg(MAIN_BOT_API, chat_id, "❌ No linked accounts to delete. Use <b>/add_account</b> first.")
        return jsonify({"ok": True})
        
    account_list = ", ".join([f"@{a['username']}" for a in accounts])
    
    set_state(uid, "main", "waiting_for_account_to_delete", data={})
    send_msg(MAIN_BOT_API, chat_id, f"🗑️ **Delete Account**\nYour accounts: {account_list}\n\nSend the **Username** (e.g., `myhandle`) to unlink it.")
    return jsonify({"ok": True})

def handle_delete_account(uid, chat_id, username):
    username = username.strip().lstrip('@').lower()

    if delete_user_account(uid, username):
        try:
            # Delete associated pending/scheduled tweets
            delete_params = {
                "account_username": username,
                "tg_id": uid,
                "post_status": "neq.POSTED" # Delete all except POSTED
            }
            sb_delete("scheduled_tweets", delete_params)
            
            send_msg(MAIN_BOT_API, chat_id, f"✅ Account @{username} unlinked from Supabase. Pending schedules involving this account have been removed.")
        except Exception as e:
            print(f"Delete account scheduled tweets cleanup failed: {e}")
            send_msg(MAIN_BOT_API, chat_id, f"⚠️ Account @{username} unlinked, but cleanup of old schedules failed.")
    else:
        send_msg(MAIN_BOT_API, chat_id, f"❌ Account @{username} not found in Supabase or not linked to your user ID.")

    return jsonify({"ok": True})

def handle_list_accounts(uid, chat_id):
    accounts = get_user_x_accounts(uid)
    if not accounts:
        send_msg(MAIN_BOT_API, chat_id, "❌ No linked accounts. Use <b>/add_account</b> first.")
        return jsonify({"ok": True})
    
    account_list = []
    for i, a in enumerate(accounts, 1):
        has_bearer = "✅" if a.get('bearer_token') else "❌"
        account_list.append(f"<b>{i}. @{a['username']}</b> (Bearer: {has_bearer})")
        
    msg = (
        "📚 <b>Linked Accounts (from Supabase)</b>\n" + 
        "\n".join(account_list) +
        "\n\nUse <b>/delete_account</b> to remove a linked account."
    )
    send_msg(MAIN_BOT_API, chat_id, msg)
    return jsonify({"ok": True})

def handle_connect_web(uid, chat_id):
    try:
        web_user = sb_select("web_users", {"tg_id": uid}, single=True, select="email")
        
        if web_user:
            send_msg(MAIN_BOT_API, chat_id, 
                     f"✅ You are already registered with the email: <code>{web_user['email']}</code>\n\n"
                     "You can log in on the website. This command is only for generating a *sign-up* key.")
            return jsonify({"ok": True})

        web_key = str(uuid.uuid4().hex[:10]).upper()
        expires_at = datetime.datetime.now(pytz.utc) + datetime.timedelta(minutes=10) # 10 minute expiry for signup
        
        # Delete old keys for this user
        sb_delete("web_keys", {"tg_id": uid})
        
        # Insert new key
        sb_insert("web_keys", {
            "key": web_key,
            "tg_id": uid,
            "created_at": now_utc_iso(),
            "expires_at": expires_at.isoformat()
        })
        
        msg = (
            "🌐 <b>Web Dashboard Sign Up Key</b>\n\n"
            "Use this key on the website sign-up page. It is valid for **10 minutes**.\n\n"
            f"Key: <code>{web_key}</code>\n"
            f"Expires: {expires_at.astimezone(kolkata_tz).strftime('%Y-%m-%d %I:%M %p %Z')}"
        )
        send_msg(MAIN_BOT_API, chat_id, msg)
        
    except Exception as e:
        print(f"Handle connect web error: {e}")
        send_msg(MAIN_BOT_API, chat_id, f"❌ Database error: {e}")
    
    return jsonify({"ok": True})

def handle_generate_link_bot_key(uid, chat_id):
    try:
        key = str(uuid.uuid4())
        exp = datetime.datetime.now(pytz.utc) + datetime.timedelta(minutes=5)
        
        # Delete old pending handshakes for this user
        sb_delete("user_link_bot_connections", {"main_bot_tg_id": uid, "link_bot_chat_id": "is.null"})
        
        # Insert new handshake key
        sb_insert("user_link_bot_connections", {
            "main_bot_tg_id": uid,
            "handshake_key": key,
            "handshake_expire": exp.isoformat(),
            "link_bot_chat_id": None
        })
        
        send_msg(MAIN_BOT_API, chat_id,
            "🔗 <b>Link Bot Handshake Key</b>\n"
            f"<code>{key}</code>\n\n"
            f"Go to {LINK_BOT_USERNAME}, send <b>/connectmainbot</b>, and then send this key when prompted.\n"
            "This key expires in <b>5 minutes</b>.")
    except Exception as e:
        print(f"Link bot key generation failed: {e}")
        # Check for unique constraint violation (less likely now but good practice)
        if "unique constraint" in str(e):
            send_msg(MAIN_BOT_API, chat_id, "⚠️ **Wait!** You already have a connection pending or active. Please try again in a minute, or run `/status`.")
        else:
            send_msg(MAIN_BOT_API, chat_id, "❌ Failed to generate Link Bot key.")
        
    return jsonify({"ok": True})

# 
# 🛠️ *** BUG FIX ***
# আপনার দেওয়া কোড দিয়ে `handle_link_bot_status` ফাংশনটি প্রতিস্থাপন করা হয়েছে
# 
def handle_link_bot_status(uid, chat_id):
    # ✅ FIX: Raw query দিয়ে properly fetch করা
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_link_bot_connections?select=link_bot_chat_id&main_bot_tg_id=eq.{uid}&link_bot_chat_id=not.is.null"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"Link bot status fetch failed: {e}")
        rows = []
    
    connected_chats = [r['link_bot_chat_id'] for r in rows if r.get('link_bot_chat_id') is not None]
    
    reply_markup = None
    
    if connected_chats:
        chat_list = "\n".join([f"- <code>{cid}</code>" for cid in connected_chats])
        status_msg = (
            f"📊 <b>Status</b>\nLink Bot: ✅ Connected to {len(connected_chats)} chat(s).\n"
            f"Connected Chats:\n{chat_list}\n\n"
            f"To disconnect any of these, use <code>/disconnect</code> in the respective Link Bot chat, or click below."
        )
        
        reply_markup = {
            'inline_keyboard': [
                [
                    {
                        'text': "🛑 Disconnect Link Bot (in Link Bot)",
                        'url': f"tg://resolve?domain={LINK_BOT_USERNAME.lstrip('@')}&text=/disconnect"
                    }
                ]
            ]
        }
    else:
        status_msg = (
            f"📊 <b>Status</b>\nLink Bot: ❌ Not connected\nUse <b>/connectlinkbot</b> to pair.\n"
            f"🕒 Local Time: {tz_now_str()}"
        )

    send_msg(MAIN_BOT_API, chat_id, status_msg, reply_markup=reply_markup)
    return jsonify({"ok": True})


# ==========================
# ADMIN BOT: COMMAND HANDLER 
# ==========================
def admin_bot_handle(update):
    chat_id = None
    try:
        if "message" not in update:
            return jsonify({"ok": True})

        msg = update["message"]
        chat_id = msg["chat"]["id"]
        from_user = msg["from"]
        text = msg.get("text", "") or ""
        uid = from_user["id"]
        
        if not is_super_admin(uid):
            send_msg(ADMIN_BOT_API, chat_id, "🚫 You are not authorized to use the Admin Bot.")
            return jsonify({"ok": True})

        scope, state, flow_data = get_state(uid)

        if state and scope == "admin":
            return admin_bot_flow_continue(uid, chat_id, text, state, flow_data)
        
        cmd = text.split()[0].lower() if text.startswith("/") else None

        if cmd == "/start" or cmd == "/help":
            base = (
                "👑 <b>Admin Bot Commands</b>\n"
                "/genkey - Generate one-time access key\n"
                "/revoke - Revoke a key\n"
                "/keys - List active keys\n"
                "/users - Manage user authorization and blocking\n"
                "/block_user [ID] - Block user access\n"
                "/unblock_user [ID] - Unblock user access\n"
                "/add_admin - Add authorized user by Telegram ID\n"
                "/remove_admin - Remove authorized user by Telegram ID\n"
                "/broadcast - Send a message to all active users\n"
                "<i>Note: Password Reset notifications are sent here automatically.</i>"
            )
            send_msg(ADMIN_BOT_API, chat_id, base)
            return jsonify({"ok": True})

        if cmd == "/genkey":
            return handle_generate_key(uid, chat_id, ADMIN_BOT_API)
        
        if cmd == "/revoke":
            set_state(uid, "admin", "waiting_for_revoke_key", data={})
            send_msg(ADMIN_BOT_API, chat_id, "🔒 Send the <b>access key</b> to revoke.")
            return jsonify({"ok": True})
        
        if cmd == "/keys":
            return handle_list_keys(chat_id, ADMIN_BOT_API)
        
        if cmd == "/users":
            return handle_list_users_for_management(uid, chat_id, ADMIN_BOT_API)
        
        if cmd == "/add_admin":
            set_state(uid, "admin", "waiting_for_admin_id_to_add", data={})
            send_msg(ADMIN_BOT_API, chat_id, "👑 Send the <b>Telegram User ID</b> to add as authorized.")
            return jsonify({"ok": True})
            
        if cmd == "/remove_admin":
            set_state(uid, "admin", "waiting_for_admin_id_to_remove", data={})
            send_msg(ADMIN_BOT_API, chat_id, "👑 Send the <b>Telegram User ID</b> to remove from authorized.")
            return jsonify({"ok": True})
            
        if cmd == "/broadcast":
            set_state(uid, "admin", "waiting_for_broadcast_message", data={})
            send_msg(ADMIN_BOT_API, chat_id, "📣 Send the message to broadcast to all active users now.")
            return jsonify({"ok": True})

        if cmd in ["/block_user", "/unblock_user"]:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send_msg(ADMIN_BOT_API, chat_id, f"❌ Usage: <b>{cmd} [User ID]</b>. Find User IDs using /users.")
            else:
                return handle_set_user_block_status_direct(chat_id, parts[1].strip(), cmd == "/block_user", ADMIN_BOT_API)
        
        if cmd:
            send_msg(ADMIN_BOT_API, chat_id, "❌ Unknown Admin command. Use <b>/help</b>.")
        
        return jsonify({"ok": True})

    except Exception as e:
        print(f"FATAL ERROR in admin webhook: {e}")
        if chat_id:
            try:
                send_msg(ADMIN_BOT_API, chat_id, "❌ An unexpected error occurred in Admin Bot. Please notify another admin.")
            except:
                pass
        return jsonify({"ok": True})

# --- ADMIN BOT FLOW CONTINUATION ---
def admin_bot_flow_continue(uid, chat_id, text, state, flow_data):
    if state == "waiting_for_revoke_key":
        clear_state(uid)
        return handle_key_revocation(uid, chat_id, text.strip(), ADMIN_BOT_API)
    
    if state == "waiting_for_admin_id_to_add":
        clear_state(uid)
        return handle_add_admin_id(uid, chat_id, text.strip(), ADMIN_BOT_API)
        
    if state == "waiting_for_admin_id_to_remove":
        clear_state(uid)
        return handle_remove_admin_id(uid, chat_id, text.strip(), ADMIN_BOT_API)
        
    if state == "waiting_for_broadcast_message":
        clear_state(uid)
        return handle_broadcast_do(uid, chat_id, text, ADMIN_BOT_API)

    # Fallback
    clear_state(uid)
    send_msg(ADMIN_BOT_API, chat_id, "⚠️ Admin flow reset. Please send the command again.")
    return jsonify({"ok": True})

# --- GENERIC ADMIN HANDLERS (Updated to Supabase) ---
def handle_generate_key(uid, chat_id, api_url):
    try:
        new_key = str(uuid.uuid4().hex[:16]).upper()
        sb_insert("keys", {
            "key": new_key,
            "active": 1,
            "created_by": uid,
            "created_at": now_utc_iso()
        })
        send_msg(api_url, chat_id,
            f"🔑 <b>New Access Key</b> (one-time):\n<code>{new_key}</code>")
    except Exception as e:
        print(f"Key generation failed: {e}")
        send_msg(api_url, chat_id, "❌ Failed to generate key.")
    return jsonify({"ok": True})

def handle_key_revocation(uid, chat_id, key, api_url):
    try:
        updated = sb_update("keys", {"active": 0}, {"key": key, "active": 1})
        if updated:
            send_msg(api_url, chat_id, f"✅ Key <code>{key}</code> revoked.")
        else:
            send_msg(api_url, chat_id, f"❌ Key <code>{key}</code> not found or already inactive.")
    except Exception as e:
        print(f"Key revocation failed: {e}")
        send_msg(api_url, chat_id, "❌ An error occurred during key revocation.")
    return jsonify({"ok": True})

def handle_list_keys(chat_id, api_url):
    try:
        url = f"{SUPABASE_URL}/rest/v1/keys?select=key,created_by,created_at&active=eq.1&order=created_at.desc"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"List keys failed: {e}")
        rows = []
    
    if not rows:
        send_msg(api_url, chat_id, "📝 No active access keys.")
    else:
        lines = ["📝 <b>Active Access Keys</b>"]
        MAX_MESSAGE_LENGTH_SAFE = MAX_MESSAGE_LENGTH - 100
        
        for i, r in enumerate(rows):
            try:
                created_dt_utc = datetime.datetime.fromisoformat(r["created_at"]).astimezone(pytz.utc)
                day = created_dt_utc.astimezone(kolkata_tz).strftime("%Y-%m-%d %I:%M %p")
            except:
                day = "Unknown Date"

            new_line = f"<code>{r['key']}</code> (Created: {day} by {r['created_by']})"
            
            if len("\n".join(lines + [new_line])) > MAX_MESSAGE_LENGTH_SAFE:
                lines.append(f"... and {len(rows) - i} more keys. Use database to see all.")
                break
            lines.append(new_line)
            
        send_msg(api_url, chat_id, "\n".join(lines))
    return jsonify({"ok": True})

def handle_list_users_for_management(uid, chat_id, api_url):
    try:
        # RPC ফাংশন সরিয়ে সরাসরি ডেটা আনা হচ্ছে
        users_list = sb_select("users", {}, select="tg_id,username,blocked")
        admins_list_rows = sb_select("admins", {}, select="tg_id")
        admin_id_set = {a['tg_id'] for a in admins_list_rows}

        if not users_list:
            send_msg(api_url, chat_id, "👥 No users found.")
            return jsonify({"ok": True})

        combined_list = []
        for u in users_list:
            is_admin = u['tg_id'] in admin_id_set
            combined_list.append({
                "tg_id": u['tg_id'],
                "username": u['username'],
                "blocked": u['blocked'],
                "is_admin": is_admin
            })
        
        # Python-এ ৩-ধাপে সর্ট (Sort) করা
        combined_list.sort(key=lambda u: u['username'] if u['username'] else '')
        combined_list.sort(key=lambda u: u['is_admin'], reverse=True)
        combined_list.sort(key=lambda u: u['blocked'], reverse=True)
        
        rows = combined_list[:50] # Limit to 50

        lines = ["👥 <b>User Management List (Top 50)</b>"]
        for i, r in enumerate(rows, 1):
            status = "👑 AUTH" if r["is_admin"] else "👤 Guest"
            blocked_status = "🔴 BLOCKED" if r["blocked"] else "🟢 Active"
            uname = r["username"] or "<i>No Username</i>"
            lines.append(f"<b>{i}.</b> [ID: <code>{r['tg_id']}</code>] {status} | {blocked_status} | @{uname}")

        lines.append("\nUse <b>/block_user [User ID]</b> or <b>/unblock_user [User ID]</b>.")
        send_msg(api_url, chat_id, "\n".join(lines))

    except Exception as e:
        print(f"List users failed: {e}")
        traceback.print_exc()
        send_msg(api_url, chat_id, f"❌ Failed to list users due to an internal error: {e}")
        
    return jsonify({"ok": True})

def handle_set_user_block_status_direct(chat_id, user_id_text, block_status: bool, api_url):
    try:
        target_uid = int(user_id_text)
    except ValueError:
        send_msg(api_url, chat_id, "❌ Invalid User ID format. Must be an integer.")
        return jsonify({"ok": True})

    if target_uid in ADMIN_IDS and block_status:
        send_msg(api_url, chat_id, "🚫 Cannot block Super Admin.")
        return jsonify({"ok": True})

    try:
        new_status = 1 if block_status else 0
        action = "BLOCKED" if block_status else "UNBLOCKED"
        
        row = sb_select("users", {"tg_id": target_uid}, single=True, select="blocked")
        
        if not row:
            # User doesn't exist, create them
            record_user({"id": target_uid, "username": f"guest_{target_uid}"})
            
        updated = sb_update("users", {"blocked": new_status}, {"tg_id": target_uid})

        if updated:
            send_msg(api_url, chat_id, f"✅ User <code>{target_uid}</code> successfully {action}.")
        else:
            send_msg(api_url, chat_id, f"⚠️ User <code>{target_uid}</code> status could not be updated or was already set.")
            
    except Exception as e:
        print(f"Block/Unblock failed: {e}")
        send_msg(api_url, chat_id, "❌ An error occurred while updating block status.")
        
    return jsonify({"ok": True})

def handle_add_admin_id(uid, chat_id, text, api_url):
    try:
        new_id = int(text)
    except:
        send_msg(api_url, chat_id, "❌ Send a valid <b>integer</b> Telegram User ID.")
        return jsonify({"ok": True})
    
    try:
        r = sb_select("users", {"tg_id": new_id}, single=True, select="username")
        uname = r["username"] if r and r["username"] else None
        
        if is_admin(new_id):
            send_msg(api_url, chat_id, f"⚠️ User ID <code>{new_id}</code> is already Authorized.")
        else:
            sb_insert("admins", {"tg_id": new_id, "username": uname})
            send_msg(api_url, chat_id, f"✅ User ID <code>{new_id}</code> added as <b>Authorized</b>.")
    except Exception as e:
        print(f"Add admin failed: {e}")
        send_msg(api_url, chat_id, "❌ Failed to add admin.")
    return jsonify({"ok": True})

def handle_remove_admin_id(uid, chat_id, text, api_url):
    try:
        rid = int(text)
    except:
        send_msg(api_url, chat_id, "❌ Send a valid <b>integer</b> Telegram User ID.")
        return jsonify({"ok": True})
    if rid == uid:
        send_msg(api_url, chat_id, "🚫 You cannot remove yourself.")
        return jsonify({"ok": True})
    
    try:
        deleted_count = sb_delete("admins", {"tg_id": rid})
        if deleted_count > 0:
            send_msg(api_url, chat_id, f"✅ User ID <code>{rid}</code> removed from Authorized.")
        else:
            send_msg(api_url, chat_id, f"❌ User ID <code>{rid}</code> not found.")
    except Exception as e:
        print(f"Remove admin failed: {e}")
        send_msg(api_url, chat_id, "❌ An error occurred while removing admin.")
    return jsonify({"ok": True})

def handle_broadcast_do(uid, chat_id, message_text, api_url):
    try:
        rows = sb_select("users", {"blocked": 0}, select="tg_id")
        
        total = len(rows)
        ok = 0
        fail = 0
        
        send_msg(api_url, chat_id, f"📣 Starting broadcast to {total} users...")
        
        for i, r in enumerate(rows):
            target_id = r["tg_id"]
            success, _ = send_msg(MAIN_BOT_API, target_id, f"📣 <b>Broadcast</b>\n\n{message_text}")
            if success:
                ok += 1
            else:
                fail += 1
                
            if (i + 1) % 50 == 0:
                send_msg(api_url, chat_id, f"✅ Broadcast progress: {ok}/{total} sent so far.")
                
        send_msg(api_url, chat_id, f"✅ Broadcast finished.\nDelivered: {ok}/{total}. Failed: {fail}.")
    except Exception as e:
        print(f"Broadcast DB error: {e}")
        send_msg(api_url, chat_id, f"❌ Broadcast failed due to DB error: {e}")
        
    return jsonify({"ok": True})

# ==========================
# LINK BOT: COMMAND HANDLER
# ==========================
def link_bot_handle(update):
    chat_id = None
    try:
        if "message" not in update:
            return jsonify({"ok": True})
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        from_user = msg["from"]
        text = msg.get("text", "") or ""
        uid = from_user["id"] 

        scope, state, flow_data = get_state(uid)
        if state and scope == "link":
            return link_bot_flow_continue(uid, chat_id, text, state, flow_data) 

        if not text.startswith("/"):
            return jsonify({"ok": True})

        cmd = text.split()[0].lower()

        if cmd == "/start":
            # ◀️ UPDATED: Added website link
            send_msg(LINK_BOT_API, chat_id,
                "👋 Welcome to the <b>Tweet Link Bot</b>!\n\n"
                "I forward successful post links from the Main Bot to this chat.\n\n"
                "<b>To connect this chat to your main account:</b>\n"
                f"1. Go to {MAIN_BOT_USERNAME} and use <b>/connectlinkbot</b> to get a key.\n"
                "2. Come back here and send: <b>/connectmainbot</b>\n"
                "3. I will ask for your key.\n\n"
                f"🌐 <b>Website Dashboard:</b> {WEBSITE_URL}")
            return jsonify({"ok": True})

        if cmd == "/connectmainbot":
            set_state(uid, "link", "waiting_for_handshake_key", data={"chat_id": chat_id})
            send_msg(LINK_BOT_API, chat_id, "🔑 Send your <b>Handshake Key</b> now.")
            return jsonify({"ok": True})
            
            
        if cmd == "/status":
            clear_state(uid) 
            row = sb_select("user_link_bot_connections", {"link_bot_chat_id": chat_id}, single=True, select="main_bot_tg_id")
            
            if row:
                send_msg(LINK_BOT_API, chat_id, f"✅ <b>Connected</b> to the Main Bot (User ID: <code>{row['main_bot_tg_id']}</code>). Links are being sent here.")
            else:
                send_msg(LINK_BOT_API, chat_id, "❌ <b>Not Connected</b>. Use <b>/connectmainbot</b> to pair.")
            return jsonify({"ok": True})
                    
                    
        if cmd == "/disconnect":
            clear_state(uid) 
            return handle_link_bot_disconnection(chat_id)

        return jsonify({"ok": True})

    except Exception as e:
        print(f"FATAL ERROR in link webhook: {e}")
        return jsonify({"ok": True})

# 
# 3️⃣ FIXED: link_bot_flow_continue (Line 1318)
# 
def link_bot_flow_continue(uid, chat_id, text, state, flow_data):
    if state == "waiting_for_handshake_key":
        target_chat_id = flow_data.get("chat_id")
        
        if chat_id != target_chat_id:
            clear_state(uid)
            send_msg(LINK_BOT_API, chat_id, 
                "❌ Please send the key in the *same chat* where you started /connectmainbot.")
            return jsonify({"ok": True})
            
        key = text.strip()
        
        if not key:
            clear_state(uid)
            send_msg(LINK_BOT_API, chat_id, "❌ Key cannot be empty.")
            return jsonify({"ok": True})
            
        # State clear হবে link_bot_try_connect এর ভিতরে (সফল হলে)
        return link_bot_try_connect(uid, target_chat_id, key)

    clear_state(uid)
    send_msg(LINK_BOT_API, chat_id, "⚠️ Unknown flow state. Flow reset.")
    return jsonify({"ok": True})


def handle_link_bot_disconnection(chat_id):
    try:
        deleted_count = sb_delete("user_link_bot_connections", {"link_bot_chat_id": chat_id})
        
        if deleted_count > 0:
            msg = "🛑 <b>Link Bot Disconnected!</b>\n\nYour connection with the Main Bot has been disconnected. Use <b>/connectmainbot</b> to reconnect."
        else:
            msg = "⚠️ Link Bot is already disconnected."
            
    except Exception as e:
        print(f"Link Bot Disconnection failed: {e}")
        msg = "❌ An error occurred during disconnection."
        
    send_msg(LINK_BOT_API, chat_id, msg)
    return jsonify({"ok": True})

# 
# 1️⃣ FIXED: link_bot_try_connect (Line 1344)
# 
def link_bot_try_connect(uid, chat_id, key_or_text):
    key = key_or_text.strip()
    
    try:
        temp_key_row = sb_select("user_link_bot_connections", 
            {"handshake_key": key, "link_bot_chat_id": "is.null"}, 
            single=True, select="main_bot_tg_id,handshake_expire")
        
        if not temp_key_row:
            send_msg(LINK_BOT_API, chat_id, 
                "❌ Invalid or already used handshake key. Generate a new one in the Main Bot (<b>/connectlinkbot</b>).")
            return jsonify({"ok": True})
            
        main_bot_tg_id = temp_key_row["main_bot_tg_id"]
        exp = datetime.datetime.fromisoformat(temp_key_row["handshake_expire"]).astimezone(pytz.utc)
        now = datetime.datetime.now(pytz.utc)
        
        if now >= exp:
            clear_state(uid)
            send_msg(LINK_BOT_API, chat_id, "❌ Expired handshake key.")
            sb_delete("user_link_bot_connections", {"handshake_key": key})
            return jsonify({"ok": True})

        # ✅ FIX: একবারই chat_id চেক করুন
        existing_conn = sb_select("user_link_bot_connections", 
            {"link_bot_chat_id": chat_id}, 
            single=True, select="main_bot_tg_id")
        
        if existing_conn:
            if existing_conn['main_bot_tg_id'] == main_bot_tg_id:
                clear_state(uid)
                sb_delete("user_link_bot_connections", {"handshake_key": key}) 
                send_msg(LINK_BOT_API, chat_id, 
                    f"⚠️ Already connected to user <code>{main_bot_tg_id}</code>.")
                return jsonify({"ok": True})
            else:
                send_msg(LINK_BOT_API, chat_id, 
                    f"❌ This chat is already connected to another user. /disconnect first.")
                return jsonify({"ok": True})

        # ✅ FIX: প্রথমে insert, তারপর delete
        new_conn = sb_insert("user_link_bot_connections", {
            "main_bot_tg_id": main_bot_tg_id,
            "link_bot_chat_id": chat_id
        })
        
        if new_conn:
            sb_delete("user_link_bot_connections", {"handshake_key": key})
            clear_state(uid)
            send_msg(LINK_BOT_API, chat_id, 
                f"✅ <b>Connection successful!</b>\nLinks will be posted here for user <code>{main_bot_tg_id}</code>.")
        else:
            send_msg(LINK_BOT_API, chat_id, "❌ Failed to save connection. Please try again.")
            
    except Exception as e:
        print(f"Link bot connect failed: {e}")
        traceback.print_exc()
        send_msg(LINK_BOT_API, chat_id, "❌ An error occurred during connection.")
        
    return jsonify({"ok": True})

# ⚡ NEW HELPER FOR THREAD POOL PROCESSING
def post_single_tweet_task(tweet):
    """
    Single tweet posting task (runs in separate thread)
    Returns: (tweet_id, success, post_link, error, tg_id, account_username)
    """
    tweet_id = tweet['id']
    tg_id = tweet['tg_id']
    tweet_text = tweet['tweet_text']
    account_username = tweet['account_username']
    
    print(f"📤 [Thread {threading.current_thread().name}] Processing ID:{tweet_id} → @{account_username}")
    
    # Skip blocked users
    if is_blocked(tg_id):
        print(f"    ⚠️ SKIPPED (user blocked)")
        return (tweet_id, False, None, "User blocked", tg_id, account_username)
    
    # Mark as processing
    sb_update("scheduled_tweets", {"post_status": "PROCESSING"}, {"id": tweet_id})
    
    # Post to X/Twitter
    post_link, error = post_tweet_to_x(tweet_id, tg_id, account_username, tweet_text)
    
    if post_link:
        print(f"    ✅ SUCCESS: {post_link}")
        return (tweet_id, True, post_link, None, tg_id, account_username)
    else:
        print(f"    ❌ FAILED: {error}")
        return (tweet_id, False, None, error, tg_id, account_username)

# ============================
#  SCHEDULER TRIGGER ROUTE (CONCURRENT OPTIMIZED)
# ============================
def check_and_post_scheduled_tweets():
    """
    Posts all scheduled tweets that are due now.
    🆕 WITH CONCURRENT PROCESSING (50 threads)
    """
    now_utc_for_check = datetime.datetime.now(pytz.utc).replace(microsecond=0)
    now_iso = now_utc_for_check.strftime('%Y-%m-%dT%H:%M:%S')
    
    print(f"\n{'='*70}")
    print(f"🕐 SCHEDULER RUN (CONCURRENT MODE)")
    print(f"    UTC Time: {now_iso}")
    print(f"    IST Time: {datetime.datetime.now(kolkata_tz).strftime('%Y-%m-%d %I:%M:%S %p %Z')}")
    print(f"{'='*70}")
    
    try:
        encoded_time = quote(now_iso, safe='')
        
        url = (
            f"{SUPABASE_URL}/rest/v1/scheduled_tweets?"
            f"select=id,tg_id,tweet_text,account_username,scheduled_time"
            f"&post_status=eq.PENDING"
            f"&scheduled_time=lte.{encoded_time}"
            f"&order=scheduled_time.asc"
        )
        
        print(f"🔍 Query URL: {url}")
        
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        tweets_to_post = r.json()
        
        print(f"✅ Found {len(tweets_to_post)} tweets ready to post")
        
        if len(tweets_to_post) == 0:
            print(f"{'='*70}\n")
            return 0
            
    except Exception as e:
        print(f"❌ Fetch failed: {e}")
        traceback.print_exc()
        print(f"{'='*70}\n")
        return 0

    posted_count = 0
    failed_count = 0
    skipped_count = 0

    # 🚀 POST ALL TWEETS CONCURRENTLY (50 at once!)
    print(f"\n⚡ MAXIMUM SPEED MODE: Posting {len(tweets_to_post)} tweets with {min(len(tweets_to_post), 50)} parallel threads...\n")
    
    futures = []
    for tweet in tweets_to_post:
        future = posting_executor.submit(post_single_tweet_task, tweet)
        futures.append(future)
    
    # Wait for all posts to complete
    for i, future in enumerate(as_completed(futures), 1):
        try:
            tweet_id, success, post_link, error, tg_id, account_username = future.result()
            
            if success:
                posted_count += 1
                
                # Update database
                sb_update("scheduled_tweets", {"post_status": "POSTED", "post_link": post_link}, {"id": tweet_id})
                
                # Notify user
                send_msg(MAIN_BOT_API, tg_id, 
                         f"🎉 <b>Tweet Posted!</b>\n"
                         f"Account: @{account_username}\n"
                         f"Link: {post_link}")
                
                # Send to Link Bot (if connected)
                try:
                    url = f"{SUPABASE_URL}/rest/v1/user_link_bot_connections?select=link_bot_chat_id&main_bot_tg_id=eq.{tg_id}&link_bot_chat_id=not.is.null"
                    # ⚡ USE FAST SESSION
                    r = fast_session.get(url, headers=SB_HEADERS, timeout=5)
                    
                    if r.status_code == 200:
                        link_rows = r.json()
                        
                        for row in link_rows:
                            chat_id = row.get('link_bot_chat_id')
                            if chat_id:
                                send_msg(LINK_BOT_API, chat_id, post_link)
                except:
                    pass
                    
            elif error == "User blocked":
                skipped_count += 1
                
            else:
                failed_count += 1
                
                # Mark as failed
                sb_update("scheduled_tweets", {"post_status": "FAILED"}, {"id": tweet_id})
                
                # Notify user
                send_msg(MAIN_BOT_API, tg_id, 
                         f"❌ <b>Tweet Failed</b>\n"
                         f"Account: @{account_username}\n"
                         f"Error: {error}\n\n"
                         f"Use /schedule_tweet to retry.")
                         
        except Exception as e:
            print(f"❌ Error processing result: {e}")
            failed_count += 1
    
    # Summary
    print(f"\n{'='*70}")
    print(f"📊 SUMMARY:")
    print(f"    ✅ Posted: {posted_count}")
    print(f"    ❌ Failed: {failed_count}")
    print(f"    ⚠️ Skipped: {skipped_count}")
    print(f"{'='*70}\n")
    
    return posted_count

# ============================
# FLASK APP ROUTES
# ============================
app = Flask(__name__)

# 🆕 ==================== AUTO-SCHEDULER ====================
def scheduled_job():
    """Runs every 1 minute automatically"""
    try:
        with app.app_context():
            print(f"\n🔔 AUTO-SCHEDULER TRIGGERED at {datetime.datetime.now(kolkata_tz).strftime('%I:%M:%S %p')}")
            posted = check_and_post_scheduled_tweets()
            print(f"✅ Scheduler done. Posted: {posted}\n")
    except Exception as e:
        print(f"❌ Scheduler error: {e}")
        traceback.print_exc()

# Initialize scheduler
scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
scheduler.add_job(
    func=scheduled_job, 
    trigger="interval", 
    seconds=5,  # ⚡ CHANGED: Every 5 seconds (was 60)
    id='tweet_auto_poster'
)
scheduler.start()

# Cleanup on exit
atexit.register(lambda: scheduler.shutdown())

print("✅ Auto-scheduler started! Runs every 5 seconds.") # ⚡ UPDATED MESSAGE
# ===========================================================

@app.route("/webhook_main", methods=["POST"])
def webhook_main():
    upd = request.get_json(silent=True) or {}
    return main_bot_handle(upd)

@app.route("/webhook_link", methods=["POST"])
def webhook_link():
    upd = request.get_json(silent=True) or {}
    return link_bot_handle(upd)

@app.route("/webhook_admin", methods=["POST"])
def webhook_admin():
    upd = request.get_json(silent=True) or {}
    return admin_bot_handle(upd)

@app.route("/scraper_notify", methods=["POST"])
def scraper_notify():
    data = request.get_json(silent=True) or {}
    secret = request.headers.get("X-Webhook-Secret") or data.get("webhook_secret")
    
    if secret != WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Invalid WEBHOOK_SECRET"}), 401

    print(f"Scraper Notify called but scraping is disabled. Data: {data}")
    return jsonify({"status": "warning", "message": "Scraping is disabled. This endpoint is inactive."}), 200

@app.route("/check_scheduler", methods=["GET"])
def scheduler_trigger():
    posted = check_and_post_scheduled_tweets()
    return jsonify({"status": "ok", "message": f"Attempted posting scheduled tweets. Posted {posted}."})

@app.route("/health") # ◀️ পরিবর্তন করা হয়েছে
def health():
    return jsonify({"ok": True, "time": tz_now_str()})

# --- WEBSITE API ENDPOINTS ---

def validate_web_access(func):
    def wrapper(*args, **kwargs):
        access_key = request.headers.get("X-Access-Key")
        
        if not access_key:
            access_key = request.args.get("key")
        
        if not access_key:
            return jsonify({"status": "error", "message": "Authorization required"}), 401

        try:
            row = sb_select("web_keys", {"key": access_key}, single=True, select="tg_id,expires_at")

            if not row:
                return jsonify({"status": "error", "message": "Invalid Access Key"}), 401
                
            expires = datetime.datetime.fromisoformat(row["expires_at"]).replace(tzinfo=pytz.utc)
            if expires < datetime.datetime.now(pytz.utc):
                return jsonify({"status": "error", "message": "Access Key expired"}), 401
            
            tg_id = row['tg_id']
            if is_blocked(tg_id):
                return jsonify({"status": "error", "message": "User is blocked. Access denied."}), 401
            
            kwargs['tg_id'] = tg_id
            return func(*args, **kwargs)
        except Exception as e:
            print(f"Web access validation error: {e}")
            return jsonify({"status": "error", "message": "Internal server error during validation"}), 500
            
    wrapper.__name__ = func.__name__ 
    return wrapper

@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password')
    web_key = data.get('web_key') 

    if not email or not password or not web_key:
        return jsonify({"status": "error", "message": "Email, password, and sign-up key are required"}), 400
    
    tg_id = None
    try:
        key_row = sb_select("web_keys", {"key": web_key}, single=True, select="tg_id,expires_at")
        
        if not key_row:
            return jsonify({"status": "error", "message": "Invalid or expired Sign Up Key."}), 401
            
        expires = datetime.datetime.fromisoformat(key_row["expires_at"]).replace(tzinfo=pytz.utc)
        if expires < datetime.datetime.now(pytz.utc):
            return jsonify({"status": "error", "message": "Sign Up Key has expired. Please get a new one from the bot."}), 401
            
        tg_id = key_row['tg_id']
        
        sb_delete("web_keys", {"key": web_key})
        
    except Exception as e:
        print(f"API signup key check failed: {e}")
        return jsonify({"status": "error", "message": f"Key validation error: {e}"}), 500
    
    if not tg_id:
        return jsonify({"status": "error", "message": "Failed to resolve Telegram ID from key."}), 500

    try:
        if sb_select("web_users", {"email": email}, single=True):
            return jsonify({"status": "error", "message": "Email already registered."}), 409
            
        if sb_select("web_users", {"tg_id": tg_id}, single=True):
            return jsonify({"status": "error", "message": "Telegram ID is already linked to another email."}), 409

        if not sb_select("users", {"tg_id": tg_id}, single=True):
            return jsonify({"status": "error", "message": "Invalid Telegram ID. Please start the main bot first."}), 400

        password_hash = hash_password(password)
        
        sb_insert("web_users", {
            "email": email,
            "password_hash": password_hash,
            "tg_id": tg_id,
            "created_at": now_utc_iso()
        })
        
        send_msg(MAIN_BOT_API, tg_id, "✅ **Web Account Linked!** You can now log in to the web dashboard using your email and password.")
        
        return jsonify({"status": "ok", "message": "Sign up successful. You can now log in."}), 201
        
    except Exception as e:
        print(f"API signup failed: {e}")
        return jsonify({"status": "error", "message": f"Database error: {e}"}), 500

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password')

    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required"}), 400
    
    try:
        row = sb_select("web_users", {"email": email}, single=True, select="tg_id,password_hash")
        
        if not row or not check_password(password, row['password_hash']):
            return jsonify({"status": "error", "message": "Invalid email or password"}), 401
            
        tg_id = row['tg_id']

        if is_blocked(tg_id):
            return jsonify({"status": "error", "message": "Your account is blocked. Contact admin."}), 403

        web_key = str(uuid.uuid4().hex[:10]).upper()
        expires_at = datetime.datetime.now(pytz.utc) + datetime.timedelta(days=7)

        sb_upsert("web_keys", [{
            "key": web_key,
            "tg_id": tg_id,
            "created_at": now_utc_iso(),
            "expires_at": expires_at.isoformat()
        }], on_conflict="key")

        return jsonify({"status": "ok", "message": "Login successful.", "tg_id": tg_id, "access_key": web_key}), 200
        
    except Exception as e:
        print(f"API login failed: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route("/api/forgot_password", methods=["POST"])
def api_forgot_password():
    data = request.json
    email = data.get('email', '').strip().lower()

    if not email:
        return jsonify({"status": "error", "message": "Email is required"}), 400

    try:
        row = sb_select("web_users", {"email": email}, single=True, select="tg_id")
        
        if not row:
            return jsonify({"status": "error", "message": "Email not registered."}), 404
            
        target_tg_id = row['tg_id']
        
        reset_code = ''.join(secrets.choice('0123456789') for _ in range(4))
        expires_at = datetime.datetime.now(pytz.utc) + datetime.timedelta(minutes=PASSWORD_RESET_TIMEOUT_MINUTES)
        
        sb_upsert("forgot_password_codes", [{
            "email": email,
            "code": reset_code,
            "tg_id": target_tg_id,
            "expires_at": expires_at.isoformat()
        }], on_conflict="email")

        admin_message = (
            f"🔔 **URGENT: Password Reset Request** (Code Generated)\n"
            f"User Email: <b>{email}</b> (TG ID: <code>{target_tg_id}</code>)\n"
            f"**CODE (Manual Email):** <code>{reset_code}</code>\n"
            f"This code expires in {PASSWORD_RESET_TIMEOUT_MINUTES} minutes.\n"
            "You must manually email this code to the user."
        )
        send_msg(ADMIN_BOT_API, ADMIN_IDS[0], admin_message)

        user_message = (
            f"🔑 **Password Reset Requested!**\n"
            f"An admin has been notified about your request for email: <b>{email}</b>.\n"
            "The admin will send a 4-digit reset code to your email. Please check your inbox/spam."
        )
        send_msg(MAIN_BOT_API, target_tg_id, user_message)

        return jsonify({"status": "ok", "message": f"Admin notified. Please check your email: {email} for the 4-digit code."}), 200

    except Exception as e:
        print(f"API forgot password failed: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route("/api/verify_forgot_code", methods=["POST"])
def api_verify_forgot_code():
    data = request.json
    email = data.get('email', '').strip().lower()
    code = data.get('code')

    if not email or not code:
        return jsonify({"status": "error", "message": "Email and 4-digit code are required"}), 400
    
    try:
        row = sb_select("forgot_password_codes", {"email": email, "code": code}, single=True, select="expires_at")
        
        if not row:
            return jsonify({"status": "error", "message": "Invalid code or email."}), 401

        expires = datetime.datetime.fromisoformat(row["expires_at"]).replace(tzinfo=pytz.utc)
        if expires < datetime.datetime.now(pytz.utc):
            sb_delete("forgot_password_codes", {"email": email})
            return jsonify({"status": "error", "message": "Reset code expired."}), 401

        return jsonify({"status": "ok", "message": "Code verified. You can now set a new password."}), 200

    except Exception as e:
        print(f"API verify code failed: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route("/api/reset_password", methods=["POST"])
def api_reset_password():
    data = request.json
    email = data.get('email', '').strip().lower()
    code = data.get('code')
    new_password = data.get('new_password')

    if not email or not code or not new_password:
        return jsonify({"status": "error", "message": "Email, code, and new password are required"}), 400
    
    try:
        forgot_row = sb_select("forgot_password_codes", {"email": email, "code": code}, single=True, select="tg_id,expires_at")
        
        if not forgot_row:
            return jsonify({"status": "error", "message": "Invalid or expired reset code."}), 401
        
        expires = datetime.datetime.fromisoformat(forgot_row["expires_at"]).replace(tzinfo=pytz.utc)
        if expires < datetime.datetime.now(pytz.utc):
            sb_delete("forgot_password_codes", {"email": email})
            return jsonify({"status": "error", "message": "Reset code expired."}), 401

        new_password_hash = hash_password(new_password)
        
        sb_update("web_users", {"password_hash": new_password_hash}, {"email": email})
        
        sb_delete("forgot_password_codes", {"email": email})
        
        user_tg_id = forgot_row['tg_id']

        send_msg(MAIN_BOT_API, user_tg_id, "✅ **Password Reset Success!** Your web dashboard password has been updated. You can now log in.")

        return jsonify({"status": "ok", "message": "Password successfully reset. You can now log in."}), 200

    except Exception as e:
        print(f"API reset password failed: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

# 
# 2️⃣ FIXED: api_verify_key (Line 1677)
# 
@app.route("/api/verify_key", methods=["GET", "POST"])
def api_verify_key():
    key = request.headers.get("X-Access-Key")
    
    if not key:
        if request.method == "GET":
            key = request.args.get("key")
        elif request.method == "POST":
            key = request.json.get("key") if request.json else None
    
    if not key:
        return jsonify({"status": "error", "message": "Access key required"}), 400
    
    try:
        row = sb_select("web_keys", {"key": key}, single=True, select="tg_id,expires_at")

        if row:
            expires = datetime.datetime.fromisoformat(row["expires_at"]).replace(tzinfo=pytz.utc)
            if expires > datetime.datetime.now(pytz.utc):
                if is_blocked(row['tg_id']):
                    return jsonify({"status": "error", "message": "User is blocked"}), 401 
                return jsonify({"status": "ok", "tg_id": row["tg_id"]}), 200
            else:
                return jsonify({"status": "error", "message": "Key expired"}), 401
    except Exception as e:
        print(f"API verify key error: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500
    
    return jsonify({"status": "error", "message": "Invalid key"}), 401


@app.route("/api/get_accounts", methods=["GET"])
@validate_web_access
def api_get_accounts(tg_id):
    accounts = get_user_x_accounts(tg_id)
    return jsonify(accounts), 200

# ◀️ --- NEW WEBSITE API: Get single account for editing ---
@app.route("/api/get_account_details/<string:username>", methods=["GET"])
@validate_web_access
def api_get_account_details(tg_id, username):
    accounts = get_user_x_accounts(tg_id)
    account = next((acc for acc in accounts if acc['username'].lower() == username.lower()), None)
    
    if account:
        return jsonify(account), 200
    else:
        return jsonify({"status": "error", "message": "Account not found or not owned by user"}), 404

# ◀️ --- NEW WEBSITE API: Update/Edit an account ---
@app.route("/api/update_account", methods=["POST"]) # Using POST, but PATCH is also fine
@validate_web_access
def api_update_account(tg_id):
    keys_to_save = request.json
    username = keys_to_save.get('username', '').strip().lstrip('@').lower()

    if not username or not keys_to_save.get('api_key') or not keys_to_save.get('api_secret') or not keys_to_save.get('access_token') or not keys_to_save.get('access_token_secret'):
        return jsonify({"status": "error", "message": "Missing critical keys (Username, API Key/Secret, Access Token/Secret)"}), 400
    
    keys_to_save['username'] = username # normalize
    
    saved = sb_upsert_account(tg_id, keys_to_save) # Upsert handles both create and update

    if saved:
        return jsonify({"status": "ok", "message": f"Account @{username} updated."}), 200
    else:
        return jsonify({"status": "error", "message": f"Failed to update account @{username}."}), 500


@app.route("/api/get_tweets", methods=["GET"])
@validate_web_access
def api_get_tweets(tg_id):
    try:
        url = f"{SUPABASE_URL}/rest/v1/scheduled_tweets?select=id,tweet_text,scheduled_time,account_username,post_status,post_link&tg_id=eq.{tg_id}&order=scheduled_time.desc,created_at.desc"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        tweets = r.json()
        return jsonify(tweets), 200
    except Exception as e:
        print(f"API get tweets failed: {e}")
        return jsonify([]), 500


@app.route("/api/delete_tweet", methods=["POST"])
@validate_web_access
def api_delete_tweet(tg_id):
    data = request.json
    tweet_id = data.get("tweet_id")
    
    if not tweet_id:
        return jsonify({"status": "error", "message": "Tweet ID required"}), 400

    try:
        # ◀️ UPDATED: Allow deleting ANY non-posted tweet (scheduled or not)
        delete_params = {
            "id": tweet_id,
            "tg_id": tg_id,
            "post_status": "neq.POSTED"
        }
        deleted_count = sb_delete("scheduled_tweets", delete_params)
        
        if deleted_count > 0:
            return jsonify({"status": "ok", "message": "Tweet deleted"}), 200
        else:
            return jsonify({"status": "error", "message": "Tweet not found or already posted"}), 404
            
    except Exception as e:
        print(f"API delete tweet failed: {e}")
        return jsonify({"status": "error", "message": "Database error"}), 500

# ◀️ --- NEW WEBSITE API: Delete ALL tweets ---
@app.route("/api/delete_all_tweets", methods=["POST"])
@validate_web_access
def api_delete_all_tweets(tg_id):
    try:
        # Delete all non-posted tweets for this user
        delete_params = {
            "tg_id": tg_id,
            "post_status": "neq.POSTED"
        }
        deleted_count = sb_delete("scheduled_tweets", delete_params)
        
        return jsonify({"status": "ok", "message": f"Deleted {deleted_count} tweets."}), 200
            
    except Exception as e:
        print(f"API delete all tweets failed: {e}")
        return jsonify({"status": "error", "message": "Database error"}), 500


@app.route("/api/post_tweet_now", methods=["POST"])
@validate_web_access
def api_post_tweet_now(tg_id):
    data = request.json
    tweet_text = data.get("tweet_text")
    account_username = data.get("account_username")

    if not tweet_text or not account_username:
        return jsonify({"status": "error", "message": "Tweet text and account required"}), 400

    if len(tweet_text) > 280:
        return jsonify({"status": "error", "message": "Tweet too long (max 280 characters)"}), 400
    if len(tweet_text.split('\n')) > MAX_TWEET_LINES:
        return jsonify({"status": "error", "message": f"Tweet too long (max {MAX_TWEET_LINES} lines)"}), 400


    post_id = str(uuid.uuid4()) 
    post_link, error = post_tweet_to_x(post_id, tg_id, account_username, tweet_text)

    if post_link:
        return jsonify({"status": "ok", "link": post_link}), 200
    else:
        return jsonify({"status": "error", "message": error}), 500


@app.route("/api/add_tweet", methods=["POST"])
@validate_web_access
def api_add_tweet(tg_id):
    data = request.json
    text = data.get("tweet_text")
    
    if not text:
        return jsonify({"status": "error", "message": "Tweet text cannot be empty"}), 400
        
    tweets_to_add = []
    failed_tweets = 0
    
    potential_tweets = text.strip().split("\n\n")
    
    for tweet_text in potential_tweets:
        tweet_text = tweet_text.strip()
        if not tweet_text:
            continue
            
        if len(tweet_text.split('\n')) > MAX_TWEET_LINES or len(tweet_text) > 280:
            failed_tweets += 1
        else:
            tweets_to_add.append(tweet_text)

    if not tweets_to_add and failed_tweets == 0:
        return jsonify({"status": "error", "message": "Tweet text cannot be empty."}), 400

    try:
        tweets_to_insert = []
        for tweet_text in tweets_to_add:
            tweets_to_insert.append({
                "tg_id": tg_id,
                "tweet_text": tweet_text,
                "post_status": "PENDING",
                "created_at": now_utc_iso()
            })
            
        if tweets_to_insert:
            sb_insert("scheduled_tweets", tweets_to_insert)
        
        ok_count = len(tweets_to_add)
        
        msg = ""
        if ok_count > 0:
            msg += f"{ok_count} tweet(s) saved. "
        if failed_tweets > 0:
            msg += f"{failed_tweets} tweet(s) failed (too long)."
        
        return jsonify({"status": "ok", "saved": ok_count, "failed": failed_tweets, "message": msg.strip()}), 201
        
    except Exception as e:
        print(f"API add bulk tweet failed: {e}")
        return jsonify({"status": "error", "message": "Database error"}), 500

# ◀️ DEPRECATED: Old API for scheduling
# @app.route("/api/schedule_existing_tweet", methods=["POST"])
# ... (This is now replaced by /api/schedule_tweets)

# ◀️ --- NEW WEBSITE API: Mass Schedule ---
@app.route("/api/schedule_tweets", methods=["POST"])
@validate_web_access
def api_schedule_tweets(tg_id):
    data = request.json
    date_str = data.get("date")
    time_str = data.get("time")
    ampm_str = data.get("ampm")

    if not date_str or not time_str or not ampm_str:
        return jsonify({"status": "error", "message": "Missing date, time, or am/pm"}), 400

    # ✅ ADD: Debugging log
    print(f"Website Schedule Request: tg_id={tg_id}, date={date_str}, time={time_str}, ampm={ampm_str}")

    result = mass_schedule_tweets(tg_id, date_str, time_str, ampm_str, chat_id=None)
    
    # mass_schedule_tweets already returns a Flask jsonify response
    return result


@app.route("/api/add_account", methods=["POST"])
@validate_web_access
def api_add_account(tg_id):
    keys_to_save = request.json
    username = keys_to_save.get('username', '').strip().lstrip('@').lower()

    if not username or not keys_to_save.get('api_key') or not keys_to_save.get('api_secret') or not keys_to_save.get('access_token') or not keys_to_save.get('access_token_secret'):
        return jsonify({"status": "error", "message": "Missing critical keys (Username, API Key/Secret, Access Token/Secret)"}), 400
    
    keys_to_save['username'] = username # normalize
    
    saved = sb_upsert_account(tg_id, keys_to_save)

    if saved:
        return jsonify({"status": "ok", "message": f"Account @{username} linked to Supabase."}), 201
    else:
        return jsonify({"status": "error", "message": f"Failed to link account @{username} to Supabase. Check credentials or contact admin."}), 409


@app.route("/api/delete_account", methods=["POST"])
@validate_web_access
def api_delete_account(tg_id):
    data = request.json
    username = data.get("username")
    
    if not username:
        return jsonify({"status": "error", "message": "Username required"}), 400
    
    username = username.strip().lstrip('@').lower()
    
    if delete_user_account(tg_id, username):
        try:
            delete_params = {
                "account_username": username,
                "tg_id": tg_id,
                "post_status": "neq.POSTED"
            }
            sb_delete("scheduled_tweets", delete_params)
            
            return jsonify({"status": "ok", "message": f"Account @{username} unlinked from Supabase. Pending schedules deleted."}), 200
        except Exception as e:
            print(f"API delete account scheduled tweets cleanup failed: {e}")
            return jsonify({"status": "warning", "message": f"Account unlinked, but cleanup failed: {e}"}), 200
    else:
        return jsonify({"status": "error", "message": f"Account @{username} not found in Supabase or not linked to user."}), 404


# 
# 🛠️ *** BUG FIX ***
# আপনার দেওয়া কোড দিয়ে `api_status` ফাংশনটি প্রতিস্থাপন করা হয়েছে
# 
@app.route("/api/status", methods=["GET"])
@validate_web_access
def api_status(tg_id):
    accounts = get_user_x_accounts(tg_id)
    account_count = len(accounts)
    
    pending_tweets = sb_select("scheduled_tweets", {"tg_id": tg_id, "post_status": "PENDING", "scheduled_time": "is.null"}, select="id")
    pending_count = len(pending_tweets)
    
    # ✅ FIX: Raw query দিয়ে properly fetch করা
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_link_bot_connections?select=link_bot_chat_id&main_bot_tg_id=eq.{tg_id}&link_bot_chat_id=not.is.null"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        r.raise_for_status()
        link_rows = r.json()
        link_status = len(link_rows) > 0
    except Exception as e:
        print(f"Link bot status check failed: {e}")
        link_status = False
    
    return jsonify({
        "status": "ok",
        "account_count": account_count,
        "pending_tweets": pending_count,
        "link_bot_connected": link_status, 
        "server_time_ist": tz_now_str()
    }), 200

# ◀️ NEW: Added your test route
@app.route("/test_supabase_insert", methods=["GET"])
def test_supabase_insert():
    """Direct Supabase insert test"""
    
    test_uid = 6535216093  # Your admin ID
    test_payload = {
        "tg_id": test_uid,
        "username": "test_raj_delete_me",
        "api_key": "test_key_123",
        "api_secret": "test_secret_456",
        "access_token": "test_token_789",
        "access_token_secret": "test_token_secret_012",
        "bearer_token": "test_bearer_345"
    }
    
    results = []
    
    # Test 1: Table exists?
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}?limit=1"
        # ⚡ USE FAST SESSION
        r = fast_session.get(url, headers=SB_HEADERS, timeout=10)
        results.append({
            "test": "Table Check",
            "status": r.status_code,
            "response": r.text,
            "success": r.status_code == 200
        })
    except Exception as e:
        results.append({
            "test": "Table Check",
            "error": str(e),
            "success": False
        })
    
    # Test 2: INSERT
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}"
        # ⚡ USE FAST SESSION
        r = fast_session.post(url, headers=SB_HEADERS, json=test_payload, timeout=10)
        results.append({
            "test": "INSERT",
            "status": r.status_code,
            "response": r.text,
            "success": r.status_code in (200, 201, 204, 409) # 409 (Conflict) is ok if test wasn't cleaned up
        })
        
        # Test 3: Cleanup
        delete_url = f"{SUPABASE_URL}/rest/v1/{SB_TABLE_ACCOUNTS}?tg_id=eq.{test_uid}&username=eq.test_raj_delete_me"
        # ⚡ USE FAST SESSION
        r_del = fast_session.delete(delete_url, headers=SB_HEADERS, timeout=10)
        results.append({
            "test": "Cleanup",
            "status": r_del.status_code,
            "success": r_del.status_code in (200, 204)
        })
    except Exception as e:
        results.append({
            "test": "INSERT/DELETE",
            "error": str(e),
            "success": False
        })
    
    return jsonify({
        "supabase_url": SUPABASE_URL,
        "table_name": SB_TABLE_ACCOUNTS,
        "results": results
    }), 200

# =============
# 🚀 NEW: WEBSITE SERVING ROUTES
# =============
@app.route("/")
def serve_index():
    """Serves the main index.html file"""
    try:
        # Assumes index.html is in the same directory as app.py
        return send_file("index.html")
    except Exception as e:
        print(f"Error serving index.html: {e}")
        return "index.html not found! Make sure it is in the same folder as app.py.", 404

# =============
# 🆕 PWA FILES SERVING
# =============
@app.route("/manifest.json")
def serve_manifest():
    """Serves the PWA manifest file"""
    try:
        return send_file("manifest.json")
    except Exception as e:
        print(f"Error serving manifest.json: {e}")
        return "manifest.json not found!", 404

@app.route("/sw.js")
def serve_sw():
    """Serves the service worker file"""
    try:
        return send_file("sw.js")
    except Exception as e:
        print(f"Error serving sw.js: {e}")
        return "sw.js not found!", 404

@app.route("/icon.svg")
def serve_icon():
    """Serves the app icon"""
    try:
        return send_file("icon.svg")
    except Exception as e:
        print(f"Error serving icon.svg: {e}")
        return "icon.svg not found!", 404

# =============
# 🆕 PWA ICON ROUTES (Direct Server Root)
# =============
@app.route("/icon-192.png")
def serve_icon_192():
    """Serves 192x192 PNG icon"""
    try:
        return send_file("icon-192.png", mimetype="image/png")
    except Exception as e:
        print(f"Error serving icon-192.png: {e}")
        return "icon-192.png not found!", 404

@app.route("/icon-512.png")
def serve_icon_512():
    """Serves 512x512 PNG icon"""
    try:
        return send_file("icon-512.png", mimetype="image/png")
    except Exception as e:
        print(f"Error serving icon-512.png: {e}")
        return "icon-512.png not found!", 404

@app.route("/icon-1024.png")
def serve_icon_1024():
    """Serves 1024x1024 PNG icon"""
    try:
        return send_file("icon-1024.png", mimetype="image/png")
    except Exception as e:
        print(f"Error serving icon-1024.png: {e}")
        return "icon-1024.png not found!", 404

# Screenshot routes (optional but needed for PWA)
@app.route("/screen1.png")
def serve_screen1():
    try:
        return send_file("screen1.png", mimetype="image/png")
    except Exception as e:
        print(f"Error serving screen1.png: {e}")
        # Return placeholder or 404
        return "Screenshot not available", 404

@app.route("/screen2.png")
def serve_screen2():
    try:
        return send_file("screen2.png", mimetype="image/png")
    except Exception as e:
        print(f"Error serving screen2.png: {e}")
        return "Screenshot not available", 404

# =============
# BOOTSTRAP
# =============
if __name__ == "__main__":
    # ❌ Removed: setup_db()
    print("DB is on Supabase (no local setup needed).")
    setup_webhooks()
    print("Webhooks set.")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
