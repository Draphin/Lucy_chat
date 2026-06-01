import os
import sys
import datetime
import pytz
import sqlite3
import asyncio
import io
import threading
import time
import requests
import psycopg2 
from telegram import Update
from telegram.ext import CallbackContext, Application, MessageHandler, CommandHandler, filters

from health_server import start_health_server

# --- 1. CONFIGURATION ---
ai_name = "Lucy"
version = "4.4.0_Permanent_Cloud_Memory"
NEURAL_VOICE = "en-US-AvaNeural"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY") 
DATABASE_URL = os.environ.get("DATABASE_URL") 

# --- 2. PERMANENT CLOUD MEMORY ENGINE ---
def get_db_connection():
    """Establishes a secure connection to the permanent cloud cluster."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Clean up old text table formats if they exist to prevent schema collisions
    cursor.execute("DROP TABLE IF EXISTS history CASCADE;")
    cursor.execute("DROP TABLE IF EXISTS core_profile CASCADE;")
    
    # 2. Re-build fresh, cloud-optimized table layouts using clean PostgreSQL notation
    cursor.execute('''
        CREATE TABLE history (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id BIGINT,
            username TEXT,
            role TEXT,
            content TEXT
        );
    ''')
    cursor.execute('''
        CREATE TABLE core_profile (
            fact_key TEXT PRIMARY KEY,
            fact_value TEXT
        );
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()
    print("[Cloud Memory]: Database tables successfully reset and synchronized!", flush=True)

def save_message(user_id, username, role, content):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO history (user_id, username, role, content) VALUES (%s, %s, %s, %s)", 
            (user_id, username, role, content)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error writing conversation step to Cloud DB: {e}")

def get_recent_memory(user_id, limit=30):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM history WHERE user_id = %s ORDER BY id DESC LIMIT %s", 
            (user_id, limit)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        messages = []
        for role, content in reversed(rows):
            messages.append({'role': role, 'content': content})
        return messages
    except Exception as e:
        print(f"Error reading conversation history from Cloud DB: {e}")
        return []

def save_core_fact(fact_key, fact_value):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO core_profile (fact_key, fact_value) VALUES (%s, %s) "
            "ON CONFLICT (fact_key) DO UPDATE SET fact_value = EXCLUDED.fact_value",
            (fact_key.strip(), fact_value.strip())
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error saving permanent profile fact: {e}")

def delete_core_fact(fact_key):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM core_profile WHERE LOWER(fact_key) = %s", (fact_key.lower().strip(),))
        changes = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        return changes > 0
    except Exception as e:
        return False

def get_all_core_facts():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT fact_key, fact_value FROM core_profile")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        if not rows:
            return "No profile traits parsed yet."
        return "\n".join([f"- {key}: {val}" for key, val in rows])
    except Exception as e:
        return "Profile traits temporarily unavailable."
def extract_and_learn_facts(text):
    text_lower = text.lower()
    patterns = [
        ("my name is ", "User Name"), ("i live in ", "Current Location"),
        ("my dog's name is ", "Dog's Name"), ("my cat's name is ", "Cat's Name"),
        ("i love to eat ", "Favorite Food"), ("i love drinking ", "Favorite Beverage"),
        ("i love ", "Hobby/Interest"), ("my favorite color is ", "Favorite Color"),
        ("my birthday is ", "User Birthday"), ("my job is ", "Job Title"),
        ("i work as a ", "Job Title"), ("i drive a ", "Car Model"),
        ("my car is a ", "Car Model"), ("i code in ", "Coding Language"),
        ("i program in ", "Coding Language")
    ]
    for pattern, descriptor in patterns:
        if pattern in text_lower:
            start_pos = text_lower.find(pattern) + len(pattern)
            extracted_fact = text[start_pos:].strip(".!? ")
            if extracted_fact:
                save_core_fact(descriptor, extracted_fact)
                return True
    return False

# --- 3. EXTERNAL LLAMA 3 API THINKING LAYER ---
def query_external_llama(messages):
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {LLAMA_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://render.com",
            "X-Title": "Lucy Assistant"
        }
        data = {
            "model": "openrouter/free", 
            "messages": messages
        }
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response_json = response.json()
        
        # FIX: Added [0] index to cleanly parse the nested dictionary string from the API array
        return response_json['choices'][0]['message']['content'].strip()
        
    except Exception as e:
        print(f"External API Inference Failure: {e}")
        return "My internal networks are experiencing a temporary external connection delay."

# --- 4. CLOUD AUDIO ENGINE (EDGE-TTS) ---
async def generate_voice_bytes(text):
    try:
        import edge_tts
        clean_text = text.replace("*", "").replace("#", "")
        communicate = edge_tts.Communicate(clean_text, NEURAL_VOICE, rate="+5%")
        audio_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
        return audio_data
    except Exception as e:
        return None

# --- 5. PROCESSING & TELEGRAM DISPATCHER ---
async def cmd_profile(update: Update, context: CallbackContext):
    facts = get_all_core_facts()
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📋 *Lucy's Core Profile Memory Bank*:\n\n{facts}", parse_mode="Markdown")

async def cmd_forget(update: Update, context: CallbackContext):
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Specify the trait key name to delete.")
        return
    target_key = " ".join(context.args).strip()
    if delete_core_fact(target_key):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🗑️ I have removed **{target_key}** from memory.")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ No trait named **{target_key}** found.")

async def handle_telegram_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return
    user_text = update.message.text
    user_id = update.effective_user.id
    username_from_telegram = update.effective_user.username or update.effective_user.full_name or f"User {user_id}"
    
    extract_and_learn_facts(user_text)
    
    lucy_pre_response_parts = []
    if any(k in user_text.lower() for k in ["user id", "my id", "your id"]):
        lucy_pre_response_parts.append(f"Your user ID is **{user_id}**.")
    if any(k in user_text.lower() for k in ["my name", "my username", "who am i"]):
        lucy_pre_response_parts.append(f"Your name is **{username_from_telegram}**.")
        
    if lucy_pre_response_parts:
        reply = " ".join(lucy_pre_response_parts)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        save_message(user_id, username_from_telegram, "assistant", reply)
        return

    save_message(user_id, username_from_telegram, "user", user_text)
    recent_history = get_recent_memory(user_id, limit=30)
    permanent_profile_context = get_all_core_facts()
    
    system_instruction = (
        f"You are {ai_name}, a helpful personal assistant running version {version}. "
        f"User name: '{username_from_telegram}', ID: '{user_id}'.\n\n"
        f"### KNOWN PERMANENT FACTS ABOUT USER:\n{permanent_profile_context}"
    )
    
    messages = [{"role": "system", "content": system_instruction}] + recent_history
    
    raw_api_reply = query_external_llama(messages)
    
    # Clean up and strip raw model formatting wrappers leaked by the API
    lucy_response = raw_api_reply.replace("</assistant>", "").replace("<|eot_id|>", "").strip()
    
    save_message(user_id, username_from_telegram, "assistant", lucy_response)

    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=lucy_response)
    audio_bytes = await generate_voice_bytes(lucy_response)
    if audio_bytes:
        voice_file = io.BytesIO(audio_bytes)
        voice_file.name = "lucy_voice.ogg"
        await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_file)

# --- 6. RUNNER PRODUCTION ENTRY ---
async def async_main():
    init_db()
    if not TELEGRAM_TOKEN or not LLAMA_API_KEY or not DATABASE_URL:
        print("[CRITICAL ERROR]: Required environment cluster strings are missing!", flush=True)
        return
        
    web_thread = threading.Thread(target=start_health_server, daemon=True)
    web_thread.start()
        
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
    
    print("Lucy Secure Permanent Web Service node successfully initiated...", flush=True)
    
    await app.initialize()
    await app.updater.start_polling()
    await app.start()
    while True:
        await asyncio.sleep(3600)

def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("Server execution halted cleanly.")

if __name__ == "__main__":
    main()
