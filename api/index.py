import os
import json
import telebot
from flask import Flask, request
from datetime import datetime
from supabase import create_client
from google import genai

# Inisialisasi App Flask untuk Vercel Serverless
app = Flask(__name__)

# Mengambil kunci dari Environment Variables Vercel
TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# 🔒 KEAMANAN 1: DAFTAR USER YANG DIIZINKAN (Whitelist)
# Ganti angka ini dengan ID Telegram-mu. 
# Tambahkan juga ID Telegram Gung Diah (pisahkan dengan koma) jika kalian mencatat pengeluaran/tabungan bersama.
ALLOWED_USERS = [5440248988] 

# DAFTAR KATEGORI
EXPENSE_CATS = ['Makan', 'Transport', 'Tagihan', 'Suplemen/Gym', 'Hiburan', 'Investasi', 'Lain-lain']
INCOME_CATS = ['Gaji', 'Kupon Investasi', 'Bonus', 'Pencairan', 'Lain-lain']

pending_transactions = {}

def parse_chat_with_ai(chat_text):
    prompt = f"""
    Kamu adalah asisten keuangan AI cerdas. Ekstrak pesan menjadi JSON.
    1. "30" = 30000. Kalikan 1000 jika disingkat.
    2. TIPE: transfer (pindah uang), income (dapat uang), expense (keluar uang).
    3. KATEGORI: transfer HANYA "Transfer Internal". income HANYA {INCOME_CATS}. expense HANYA {EXPENSE_CATS}. Jika expense/income RAGU, isi "UNKNOWN".
    4. DOMPET: wallet_name (asal), to_wallet_name (tujuan - HANYA transfer, selain itu isi "").
    Chat: "{chat_text}"
    Keys: "description", "amount", "wallet_name", "to_wallet_name", "category", "type".
    """
    try:
        response = ai_client.models.generate_content(model='gemini-3.1-flash-lite', contents=prompt)
        return json.loads(response.text.replace('```json', '').replace('```', '').strip())
    except: return None

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    
    # 🔒 SATPAM BERAKSI: Tolak jika ID tidak terdaftar
    if chat_id not in ALLOWED_USERS:
        bot.send_message(chat_id, "⛔ Akses Ditolak! Anda tidak memiliki izin untuk mengakses database keuangan ini.")
        return

    text = message.text.strip().lower()

    if chat_id in pending_transactions:
        data = pending_transactions[chat_id]['data']
        cats_to_choose = pending_transactions[chat_id]['cats']
        try:
            pilihan = int(text) - 1
            if 0 <= pilihan < len(cats_to_choose):
                data['category'] = cats_to_choose[pilihan]
                simpan_ke_supabase(chat_id, data)
                del pending_transactions[chat_id] 
            else: bot.reply_to(message, "⚠️ Masukkan angka dari daftar.")
        except: bot.reply_to(message, "⚠️ Masukkan angkanya saja.")
        return

    bot.send_message(chat_id, "⏳ Menganalisis...")
    parsed_data = parse_chat_with_ai(text)
    
    if not parsed_data:
        return bot.reply_to(message, "❌ Gagal memahami pesan.")

    if parsed_data.get('category') == 'UNKNOWN':
        cats_to_choose = INCOME_CATS if parsed_data['type'] == 'income' else EXPENSE_CATS
        pending_transactions[chat_id] = {'data': parsed_data, 'cats': cats_to_choose}
        pilihan_teks = f"🤔 '{parsed_data['description'].title()}' masuk mana?\n"
        for i, k in enumerate(cats_to_choose): pilihan_teks += f"{i+1}. {k}\n"
        return bot.reply_to(message, pilihan_teks)

    simpan_ke_supabase(chat_id, parsed_data)

def simpan_ke_supabase(chat_id, data):
    try:
        wallet_name_query = data.get('wallet_name', '').strip()
        if not wallet_name_query: return bot.send_message(chat_id, "⚠️ Sebutkan nama dompet asal.")
            
        wallet_res = supabase.table('wallets').select('id, name, user_id').ilike('name', f"%{wallet_name_query}%").execute()
        if len(wallet_res.data) == 0: return bot.send_message(chat_id, f"⚠️ Dompet '{wallet_name_query}' tidak ditemukan.")
            
        w_id = wallet_res.data[0]['id']
        w_name = wallet_res.data[0]['name']
        u_id = wallet_res.data[0]['user_id'] 

        to_w_id = None
        to_w_name = None
        if data['type'] == 'transfer':
            to_name_query = data.get('to_wallet_name', '').strip()
            if not to_name_query: return bot.send_message(chat_id, "⚠️ Sebutkan dompet tujuan.")
            to_res = supabase.table('wallets').select('id, name').ilike('name', f"%{to_name_query}%").execute()
            if len(to_res.data) == 0: return bot.send_message(chat_id, f"⚠️ Dompet '{to_name_query}' tidak ditemukan.")
            to_w_id = to_res.data[0]['id']
            to_w_name = to_res.data[0]['name']

        supabase.table('transactions').insert({
            "type": data['type'], "amount": data['amount'], "category": data['category'],
            "description": data['description'], "wallet_id": w_id, "to_wallet_id": to_w_id, 
            "user_id": u_id, "transaction_date": datetime.today().strftime('%Y-%m-%d')
        }).execute()
        
        rp = "{:,}".format(data['amount']).replace(',', '.')
        if data['type'] == 'transfer':
            bot.send_message(chat_id, f"🔄 **Transfer**\nRp {rp}\n📤 {w_name.title()} ➔ 📥 {to_w_name.title()}\n📝 {data['description'].title()}", parse_mode='Markdown')
        else:
            bot.send_message(chat_id, f"✅ **{'🟢 Masuk' if data['type'] == 'income' else '🔴 Keluar'}**\n📝 {data['description'].title()}\n💰 Rp {rp}\n📁 {data['category']}\n💳 {w_name.title()}", parse_mode='Markdown')
    except Exception as e: bot.send_message(chat_id, f"❌ Error: {str(e)}")

# ==========================================
# RUTINITAS FLASK & WEBHOOK UNTUK VERCEL
# ==========================================

@app.route('/', methods=['GET'])
def home():
    return "Bot Serverless Vercel Hidup & Aman! 🚀"

# 🔒 KEAMANAN 2: SECRET PATH
# URL Webhook sekarang menggunakan Token Telegram-mu, sehingga mustahil ditebak peretas.
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403