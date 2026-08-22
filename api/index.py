import os
import json
import logging
from datetime import datetime, timedelta

import telebot
from flask import Flask, request
from supabase import create_client
from google import genai

# ==========================================
# SETUP LOGGING (biar error kelihatan di log Vercel)
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance_bot")

# ==========================================
# Inisialisasi App Flask untuk Vercel Serverless
# ==========================================
app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")  # baru: token rahasia webhook

bot = telebot.TeleBot(TOKEN, threaded=False)
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# 🔒 KEAMANAN 1: WHITELIST + MAPPING ke user_id Supabase
# Sebelumnya cuma list ID Telegram. Sekarang di-map eksplisit ke user_id
# di Supabase, supaya query wallet tidak bergantung pada "siapa yang
# namanya nyangkut duluan di hasil ilike" saat ada >1 user berbagi bot.
#
# Ganti value "GANTI_DENGAN_UUID_..." dengan user_id asli dari tabel users/auth Supabase.
ALLOWED_USERS = {
    5440248988: "f17f773a-5809-42ef-87ad-47a586c1b480",
    # 111111111: "GANTI_DENGAN_UUID_GUNG_DIAH",
}

EXPENSE_CATS = ['Makan', 'Transport', 'Tagihan', 'Suplemen/Gym', 'Hiburan', 'Investasi', 'Lain-lain']
INCOME_CATS = ['Gaji', 'Kupon Investasi', 'Bonus', 'Pencairan', 'Lain-lain']

MAX_REASONABLE_AMOUNT = 500_000_000  # sanity check, sesuaikan kalau perlu


# ==========================================
# STATE MANAGEMENT (Supabase, bukan dict di memori)
# Penting: di Vercel serverless, tiap request BISA jalan di instance
# proses yang berbeda. Dict Python di memori tidak dijamin persisten
# antar-request, jadi wajib disimpan ke database.
# ==========================================
def get_pending(chat_id: int):
    res = supabase.table("pending_transactions").select("*").eq("chat_id", chat_id).execute()
    if res.data:
        return res.data[0]
    return None


def set_pending(chat_id: int, data: dict, cats: list):
    supabase.table("pending_transactions").upsert({
        "chat_id": chat_id,
        "data": data,
        "cats": cats,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()


def clear_pending(chat_id: int):
    supabase.table("pending_transactions").delete().eq("chat_id", chat_id).execute()


# ==========================================
# PARSING DENGAN AI
# ==========================================
def parse_chat_with_ai(chat_text: str):
    prompt = f"""
    Kamu adalah asisten keuangan AI cerdas. Ekstrak pesan menjadi JSON.

    ATURAN STRUKTUR (Pola umum user: [Deskripsi/Item] [Nominal] [Dompet Asal]):
    1. ANGKA: "30" = 30000, "101" = 101000. Kalikan 1000 jika disingkat.
    2. TIPE (type):
       - "transfer": JIKA ADA DUA DOMPET dan kata "ke" (contoh: "bca ke tunai 50").
       - "income": JIKA MENDAPAT UANG (contoh: "gaji", "bonus").
       - "expense": JIKA KELUAR UANG ATAU TOPUP E-WALLET (contoh: "shopeepay 101 dmnn", "topup 50 bri", "makan 20 tunai"). Topup e-wallet dihitung sebagai "expense" jika hanya menyebut 1 dompet asal.
    3. DOMPET (wallet_name & to_wallet_name):
       - wallet_name: Dompet SUMBER uang (BIASANYA KATA TERAKHIR). Jangan jadikan kata pertama (seperti shopeepay/topup/dana) sebagai dompet asal!
       - to_wallet_name: Hanya diisi jika "transfer". Jika bukan, isi "".
    4. KATEGORI (category):
       - "transfer" = "Transfer Internal"
       - "income" = Pilih HANYA dari {INCOME_CATS}.
       - "expense" = Pilih HANYA dari {EXPENSE_CATS}. (Jika ragu, WAJIB isi "UNKNOWN").

    CONTOH WAJIB DIIKUTI:
    - Chat: "shopeepay 101 dmnn" -> desc="shopeepay", amount=101000, wallet_name="dmnn", type="expense", category="UNKNOWN".
    - Chat: "topup 50 bca" -> desc="topup", amount=50000, wallet_name="bca", type="expense", category="UNKNOWN".
    - Chat: "makan 20 tunai" -> desc="makan", amount=20000, wallet_name="tunai", type="expense", category="Makan".

    Chat: "{chat_text}"
    Keys wajib: "description", "amount", "wallet_name", "to_wallet_name", "category", "type".
    """
    try:
        response = ai_client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        clean = response.text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
    except Exception as e:
        logger.error(f"Gagal parsing AI untuk chat '{chat_text}': {e}")
        return None

    # Validasi dasar hasil AI sebelum dipakai
    if not isinstance(parsed.get("amount"), (int, float)) or parsed["amount"] <= 0:
        logger.warning(f"AI mengembalikan amount tidak valid: {parsed.get('amount')} untuk chat '{chat_text}'")
        return None
    if parsed["amount"] > MAX_REASONABLE_AMOUNT:
        logger.warning(f"Amount melebihi batas wajar: {parsed['amount']} untuk chat '{chat_text}'")
        return None
    if parsed.get("type") not in ("expense", "income", "transfer"):
        logger.warning(f"AI mengembalikan type tidak dikenal: {parsed.get('type')}")
        return None

    return parsed


# ==========================================
# HANDLER PESAN
# ==========================================
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    chat_id = message.chat.id

    # 🔒 SATPAM BERAKSI: Tolak jika ID tidak terdaftar
    if chat_id not in ALLOWED_USERS:
        bot.send_message(chat_id, "⛔ Akses Ditolak! Anda tidak memiliki izin untuk mengakses database keuangan ini.")
        return

    text = message.text.strip().lower()

    pending = get_pending(chat_id)
    if pending:
        data = pending["data"]
        cats_to_choose = pending["cats"]
        try:
            pilihan = int(text) - 1
            if 0 <= pilihan < len(cats_to_choose):
                data["category"] = cats_to_choose[pilihan]
                simpan_ke_supabase(chat_id, data)
                clear_pending(chat_id)
            else:
                bot.reply_to(message, "⚠️ Masukkan angka dari daftar.")
        except ValueError:
            bot.reply_to(message, "⚠️ Masukkan angkanya saja.")
        except Exception as e:
            logger.error(f"Error saat proses pending transaction chat_id={chat_id}: {e}")
            bot.reply_to(message, "❌ Terjadi kesalahan, coba lagi.")
        return

    bot.send_message(chat_id, "⏳ Menganalisis...")
    parsed_data = parse_chat_with_ai(text)

    if not parsed_data:
        bot.reply_to(message, "❌ Gagal memahami pesan. Coba diperjelas ya.")
        return

    if parsed_data.get("category") == "UNKNOWN":
        cats_to_choose = INCOME_CATS if parsed_data["type"] == "income" else EXPENSE_CATS
        set_pending(chat_id, parsed_data, cats_to_choose)
        pilihan_teks = f"🤔 '{parsed_data['description'].title()}' masuk mana?\n"
        for i, k in enumerate(cats_to_choose):
            pilihan_teks += f"{i + 1}. {k}\n"
        bot.reply_to(message, pilihan_teks)
        return

    simpan_ke_supabase(chat_id, parsed_data)


# ==========================================
# SIMPAN KE SUPABASE
# ==========================================
def simpan_ke_supabase(chat_id, data):
    try:
        user_id = ALLOWED_USERS.get(chat_id)
        if not user_id or user_id.startswith("GANTI_DENGAN_UUID"):
            bot.send_message(chat_id, "⚠️ Konfigurasi user_id belum diisi di ALLOWED_USERS. Hubungi admin bot.")
            logger.error(f"user_id belum di-mapping untuk chat_id={chat_id}")
            return

        wallet_name_query = data.get("wallet_name", "").strip()
        if not wallet_name_query:
            bot.send_message(chat_id, "⚠️ Sebutkan nama dompet asal.")
            return

        # 1. Cari Dompet Asal
        wallet_res = (
            supabase.table("wallets")
            .select("id, name, user_id")
            .eq("user_id", user_id)
            .ilike("name", f"%{wallet_name_query}%")
            .execute()
        )
        if len(wallet_res.data) == 0:
            bot.send_message(chat_id, f"⚠️ Dompet '{wallet_name_query}' tidak ditemukan.")
            return

        w_id = wallet_res.data[0]["id"]
        w_name = wallet_res.data[0]["name"]

        to_w_id = None
        to_w_name = None
        
        # 2. Cari Dompet Tujuan (Jika Transfer)
        if data["type"] == "transfer":
            to_name_query = data.get("to_wallet_name", "").strip()
            if not to_name_query:
                bot.send_message(chat_id, "⚠️ Sebutkan dompet tujuan.")
                return
            to_res = (
                supabase.table("wallets")
                .select("id, name")
                .eq("user_id", user_id)
                .ilike("name", f"%{to_name_query}%")
                .execute()
            )
            if len(to_res.data) == 0:
                bot.send_message(chat_id, f"⚠️ Dompet '{to_name_query}' tidak ditemukan.")
                return
            to_w_id = to_res.data[0]["id"]
            to_w_name = to_res.data[0]["name"]

        # 3. Masukkan Transaksi Baru
        supabase.table("transactions").insert({
            "type": data["type"],
            "amount": data["amount"],
            "category": data["category"],
            "description": data["description"],
            "wallet_id": w_id,
            "to_wallet_id": to_w_id,
            "user_id": user_id,
            "transaction_date": datetime.today().strftime("%Y-%m-%d"),
        }).execute()

        # 4. AMBIL SALDO TERBARU DARI TABEL VIRTUAL (wallet_balances)
        sisa_saldo_asal = 0
        res_asal = supabase.table("wallet_balances").select("balance").eq("wallet_id", w_id).execute()
        if res_asal.data:
            sisa_saldo_asal = res_asal.data[0]["balance"]

        sisa_saldo_tujuan = 0
        if data["type"] == "transfer" and to_w_id:
            res_tujuan = supabase.table("wallet_balances").select("balance").eq("wallet_id", to_w_id).execute()
            if res_tujuan.data:
                sisa_saldo_tujuan = res_tujuan.data[0]["balance"]

        # 5. Format Angka ke Rupiah
        rp_amount = "{:,}".format(int(data["amount"])).replace(",", ".")
        rp_asal = "{:,}".format(int(sisa_saldo_asal)).replace(",", ".")
        rp_tujuan = "{:,}".format(int(sisa_saldo_tujuan)).replace(",", ".")

        # 6. Susun Pesan Laporan
        if data["type"] == "transfer":
            pesan = (
                f"🔄 **Transfer Berhasil**\n"
                f"Rp {rp_amount}\n"
                f"📤 {w_name.title()} ➔ 📥 {to_w_name.title()}\n"
                f"📝 {data['description'].title()}\n\n"
                f"📊 **Update Saldo:**\n"
                f"- {w_name.title()}: Rp {rp_asal}\n"
                f"- {to_w_name.title()}: Rp {rp_tujuan}"
            )
        else:
            label = "🟢 Masuk" if data["type"] == "income" else "🔴 Keluar"
            pesan = (
                f"✅ **{label}**\n"
                f"📝 {data['description'].title()}\n"
                f"💰 Rp {rp_amount}\n"
                f"📁 {data['category']}\n"
                f"💳 {w_name.title()}\n\n"
                f"📊 Saldo sekarang: Rp {rp_asal}"
            )
            
        bot.send_message(chat_id, pesan, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Gagal simpan transaksi chat_id={chat_id}: {e}")
        bot.send_message(chat_id, "❌ Gagal menyimpan transaksi. Coba lagi atau hubungi admin.")
        

# ==========================================
# RUTINITAS FLASK & WEBHOOK UNTUK VERCEL
# ==========================================
@app.route("/", methods=["GET"])
def home():
    return "Bot Serverless Vercel Hidup & Aman! 🚀"


# 🔒 KEAMANAN 2: SECRET PATH + SECRET TOKEN
# Selain URL yang mengandung TOKEN, sekarang juga divalidasi via header
# resmi Telegram (X-Telegram-Bot-Api-Secret-Token). Set secret ini saat
# register webhook: bot.set_webhook(url=..., secret_token=WEBHOOK_SECRET)
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != WEBHOOK_SECRET:
            logger.warning("Webhook ditolak: secret token tidak cocok.")
            return "Forbidden", 403

    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        try:
            bot.process_new_updates([update])
        except Exception as e:
            logger.error(f"Error memproses update: {e}")
        return "OK", 200
    return "Forbidden", 403