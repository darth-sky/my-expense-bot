import os
import json
import telebot
from datetime import datetime
from supabase import create_client
from dotenv import load_dotenv
from google import genai

# 1. LOAD KONFIGURASI
load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Ganti langsung kredensial jika .env masih nge-bug
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# 2. DAFTAR KATEGORI
EXPENSE_CATS = ['Makan', 'Transport', 'Tagihan', 'Suplemen/Gym', 'Hiburan', 'Investasi', 'Lain-lain']
INCOME_CATS = ['Gaji', 'Kupon Investasi', 'Bonus', 'Pencairan', 'Lain-lain']

# MEMORI SEMENTARA (Struktur baru untuk simpan jenis kategori)
pending_transactions = {}

# 3. FUNGSI ANALISIS TEKS DENGAN GEMINI AI
def parse_chat_with_ai(chat_text):
    prompt = f"""
    Kamu adalah asisten keuangan AI super cerdas. Ekstrak pesan user menjadi format JSON murni.
    
    ATURAN BACA:
    1. ANGKA: "30" = 30000, "15" = 15000, "200" = 200000. Selalu kalikan 1000 jika angkanya rasional untuk disingkat.
    2. TIPE (type): 
       - Jika user memindahkan uang (cth: "im ke bri", "transfer bca ke tunai"), isi "transfer".
       - Jika user mendapat uang (cth: "gaji", "bunga", "cair"), isi "income".
       - Selain itu, isi "expense".
    3. KATEGORI (category):
       - Jika "transfer", WAJIB isi: "Transfer Internal".
       - Jika "income", pilih dari: {INCOME_CATS}.
       - Jika "expense", pilih dari: {EXPENSE_CATS}.
       - JIKA "expense" ATAU "income" DAN KAMU RAGU (bahasa gaul, tidak umum), WAJIB isi "UNKNOWN".
    4. DOMPET (wallet_name & to_wallet_name):
       - wallet_name = Dompet asal/sumber dana. (WAJIB ADA).
       - to_wallet_name = Dompet tujuan (HANYA DIISI JIKA transfer, cth: "im ke bri" -> wallet_name="im", to_wallet_name="bri"). Jika bukan transfer, isi string kosong "".
    
    Chat: "{chat_text}"
    
    Output JSON wajib berisi keys: 
    "description" (string), "amount" (integer), "wallet_name" (string), "to_wallet_name" (string), "category" (string), "type" (string: "expense"/"income"/"transfer").
    """
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.1-flash-lite', 
            contents=prompt
        )
        clean_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_text)
    except Exception as e:
        print("Error AI:", e)
        return None

# 4. HANDLER PESAN MASUK
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id
    text = message.text.strip().lower()

    # JIKA USER SEDANG MEMILIH KATEGORI (BALASAN ANGKA)
    if chat_id in pending_transactions:
        data = pending_transactions[chat_id]['data']
        cats_to_choose = pending_transactions[chat_id]['cats']
        
        try:
            pilihan = int(text) - 1
            if 0 <= pilihan < len(cats_to_choose):
                data['category'] = cats_to_choose[pilihan]
                simpan_ke_supabase(chat_id, data)
                del pending_transactions[chat_id] 
            else:
                bot.reply_to(message, "⚠️ Masukkan angka yang sesuai dari daftar di atas ya.")
        except ValueError:
            bot.reply_to(message, "⚠️ Masukkan angkanya saja ya (contoh: 1).")
        return

    bot.send_message(chat_id, "⏳ Menganalisis transaksi...")

    # PROSES CHAT BARU
    parsed_data = parse_chat_with_ai(text)
    
    if not parsed_data:
        bot.reply_to(message, "❌ Maaf, saya gagal memahami pesanmu.")
        return

    # JIKA AI RAGU (UNKNOWN KATEGORI)
    if parsed_data.get('category') == 'UNKNOWN':
        # Tentukan menu pilihan berdasarkan tipe (Pemasukan atau Pengeluaran)
        cats_to_choose = INCOME_CATS if parsed_data['type'] == 'income' else EXPENSE_CATS
        
        # Simpan ke memori
        pending_transactions[chat_id] = {
            'data': parsed_data,
            'cats': cats_to_choose
        }
        
        pilihan_teks = f"🤔 Saya kurang tahu '{parsed_data['description'].title()}' masuk kategori mana. Tolong pilih angkanya:\n\n"
        for i, kat in enumerate(cats_to_choose):
            pilihan_teks += f"{i+1}. {kat}\n"
        bot.reply_to(message, pilihan_teks)
        return

    # JIKA AI YAKIN, SIMPAN
    simpan_ke_supabase(chat_id, parsed_data)


# 5. FUNGSI SIMPAN KE SUPABASE
def simpan_ke_supabase(chat_id, data):
    try:
        # A. PROSES DOMPET ASAL
        wallet_name_query = data.get('wallet_name', '').strip()
        if not wallet_name_query:
            bot.send_message(chat_id, "⚠️ Tolong sebutkan nama dompet asalnya (contoh: 30 bri).")
            return
            
        wallet_res = supabase.table('wallets').select('id, name, user_id').ilike('name', f"%{wallet_name_query}%").execute()
        
        if len(wallet_res.data) == 0:
            bot.send_message(chat_id, f"⚠️ Dompet asal '{wallet_name_query}' tidak ditemukan.")
            return
            
        wallet_id = wallet_res.data[0]['id']
        found_wallet_name = wallet_res.data[0]['name']
        user_id = wallet_res.data[0]['user_id'] 

        # B. PROSES DOMPET TUJUAN (KHUSUS TRANSFER)
        to_wallet_id = None
        found_to_wallet_name = None
        
        if data['type'] == 'transfer':
            to_wallet_name_query = data.get('to_wallet_name', '').strip()
            if not to_wallet_name_query:
                bot.send_message(chat_id, "⚠️ Ini transfer, tapi kamu belum menyebutkan dompet tujuannya (contoh: im ke bri 50).")
                return
                
            to_wallet_res = supabase.table('wallets').select('id, name').ilike('name', f"%{to_wallet_name_query}%").execute()
            if len(to_wallet_res.data) == 0:
                bot.send_message(chat_id, f"⚠️ Dompet tujuan '{to_wallet_name_query}' tidak ditemukan.")
                return
                
            to_wallet_id = to_wallet_res.data[0]['id']
            found_to_wallet_name = to_wallet_res.data[0]['name']

        # C. SIAPKAN PAYLOAD
        payload = {
            "type": data['type'],
            "amount": data['amount'],
            "category": data['category'],
            "description": data['description'],
            "wallet_id": wallet_id,
            "to_wallet_id": to_wallet_id, 
            "user_id": user_id, 
            "transaction_date": datetime.today().strftime('%Y-%m-%d') 
        }

        # D. INSERT KE DATABASE
        supabase.table('transactions').insert(payload).execute()
        
        # E. BIKIN PESAN LAPORAN YANG CANTIK
        rp_format = "{:,}".format(data['amount']).replace(',', '.')
        
        if data['type'] == 'transfer':
            laporan = f"🔄 **Transfer Berhasil!**\n\n" \
                      f"💰 Nominal: Rp {rp_format}\n" \
                      f"📤 Dari: {found_wallet_name.title()}\n" \
                      f"📥 Ke: {found_to_wallet_name.title()}\n" \
                      f"📝 Ket: {data['description'].title()}"
        else:
            tipe_emoji = "🟢 Pemasukan" if data['type'] == 'income' else "🔴 Pengeluaran"
            laporan = f"✅ **{tipe_emoji} Dicatat!**\n\n" \
                      f"📝 Item: {data['description'].title()}\n" \
                      f"💰 Nominal: Rp {rp_format}\n" \
                      f"📁 Kategori: {data['category']}\n" \
                      f"💳 Dompet: {found_wallet_name.title()}"
                  
        bot.send_message(chat_id, laporan, parse_mode='Markdown')
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Gagal menyimpan ke database. Error: {str(e)}")

# --- TAMBAHAN UNTUK CLOUD SERVER 24/7 (FLASK) ---
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Expense Tracker Bot sedang hidup 24/7! 🚀"

def run_web_server():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    # Jalankan server Flask di latar belakang (thread terpisah)
    server_thread = threading.Thread(target=run_web_server)
    server_thread.start()

    print("🤖 Bot Generasi 2.0 Siap! Server web berjalan.")
    bot.polling(none_stop=True)