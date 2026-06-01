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
from telegram import Update
from telegram.ext import CallbackContext, Application, MessageHandler, CommandHandler, filters

# --- 1. CONFIGURATION ---
ai_name = "Lucy"
version = "4.0.0_Permanent"
NEURAL_VOICE = "en-US-AvaNeural"

# Pull keys securely from Render's Environment Dashboard variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY") 
DB_PATH = "lucy_memory.db" 

# --- 2. MEMORY ENGINE (SQLITE) ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            username TEXT,
            role TEXT,
            content TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(history)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'user_id' not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN user_id INTEGER")
    if 'username' not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN username TEXT")
        
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS core_profile (
            fact_key TEXT PRIMARY KEY,
            fact_value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_message(user_id, username, role, content):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO history (user_id, username, role, content) VALUES (?, ?, ?, ?)", (user_id, username, role, content))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving message: {e}")

def get_recent_memory(user_id, limit=30):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        messages = []
        for role, content in reversed(rows):
            messages.append({'role': role, 'content': content})
        return messages
    except Exception as e:
        print(f"Error retrieving memory: {e}")
        return []

def save_core_fact(fact_key, fact_value):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO core_profile (fact_key, fact_value) VALUES (?, ?)", (fact_key.strip(), fact_value.strip()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving core fact to DB: {e}")

def delete_core_fact(fact_key):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM core_profile WHERE LOWER(fact_key) = ?", (fact_key.lower().strip(),))
        changes = conn.total_changes
        conn.commit()
        conn.close()
        return changes > 0
    except Exception as e:
        return False

def get_all_core_facts():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT fact_key, fact_value FROM core_profile")
        rows = cursor.fetchall()
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
        url = "https://openrouter.ai"
        headers = {
            "Authorization": f"Bearer {LLAMA_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://render.com", # Identifies traffic layout safely
            "X-Title": "Lucy Assistant"
        }
        data = {
            "model": "meta-llama/llama-3-8b-instruct:free", 
            "messages": messages
        }
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response_json = response.json()
        return response_json['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"External API Inference Failure: {e}")
        return "My internal processing networks are running into a brief external connection delay."

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
    
    lucy_response = query_external_llama(messages)
    save_message(user_id, username_from_telegram, "assistant", lucy_response)
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=lucy_response)
    audio_bytes = await generate_voice_bytes(lucy_response)
    if audio_bytes:
        voice_file = io.BytesIO(audio_bytes)
        voice_file.name = "lucy_voice.ogg"
        await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_file)

# --- 6. RUNNER PROD LOOP ---
def main():
    init_db()
    if not TELEGRAM_TOKEN or not LLAMA_API_KEY:
        print("[CRITICAL ERROR]: Required environment config tokens are missing!", flush=True)
        return
        
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
    
    print("Lucy is officially deployed live on Render production nodes...", flush=True)
    app.run_polling()

if __name__ == "__main__":
    main()
