import os
import subprocess
import uuid
import time
import re
# ffmpeg PATH
if os.name == "nt":
    os.environ["PATH"] += r";C:\ffmpeg\bin"
else:
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ffmpeg_dir = os.path.dirname(ffmpeg_exe)
        os.environ["PATH"] = ffmpeg_dir + ":" + os.environ.get("PATH", "")
        print(f"[ffmpeg] imageio_ffmpeg: {ffmpeg_exe}")
    except Exception as e:
        print(f"[ffmpeg] imageio_ffmpeg topilmadi: {e}")

import telebot
from telebot import types
import yt_dlp
import requests

BOT_TOKEN = "8856575714:AAEfcKrrG7Z99o8dyOV7PXrPKe4E8j3wS0U"
RAPIDAPI_KEY = "6dc49d8cb3msh6f05457aa55d958p12a921jsnde8cd5768511"

ADMIN_ID = 264008630

bot = telebot.TeleBot(BOT_TOKEN)
os.makedirs("downloads", exist_ok=True)

# Holatlar
pending_music = {}        # chat_id → True  (qo'lda nom yozish kutilmoqda)
search_results = {}       # chat_id → [ {title, url, duration}, ... ]
broadcast_mode = {}       # admin broadcast kutish holati

USERS_FILE = "users.txt"

def load_users():
    if not os.path.exists(USERS_FILE):
        return set()
    with open(USERS_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_user(user_id):
    users = load_users()
    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(f"{user_id}\n")

def get_unique_path(ext):
    return f"downloads/{uuid.uuid4().hex}.{ext}"

def seconds_to_mmss(sec):
    try:
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"
    except Exception:
        return ""

# ──────────────────────────────────────────
# 1. METADATA DAN QO'SHIQ NOMI OLISH
# ──────────────────────────────────────────
def get_music_from_metadata(url):
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        for m in info.get('music', []):
            artist = m.get('artist', '')
            title = m.get('title', '')
            if title:
                return f"{artist} - {title}".strip(" -") if artist else title

        track = info.get('track', '')
        artist = info.get('artist', '')
        if track:
            return f"{artist} - {track}".strip(" -") if artist else track

        desc  = info.get('description', '') or ''
        title = info.get('title', '') or ''
        patterns = [
            r'(?:music|song|track|audio)[:\s]+([^\n#@]{3,60})',
            r'[🎵♪🎶]\s*([^\n#@]{3,60})',
            r'(?:by|feat\.?|ft\.?)\s+([A-Za-z0-9 &\-]{3,40})',
        ]
        for text in [desc, title]:
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    candidate = match.group(1).strip()
                    if len(candidate) > 3:
                        return candidate
        return None
    except Exception as e:
        print(f"[Metadata Error] {e}")
        return None

# ──────────────────────────────────────────
# 2. SHAZAM
# ──────────────────────────────────────────

def call_shazam_api(audio_path, retry_wait=0):
    url = "https://shazam-song-recognition-api.p.rapidapi.com/recognize/file"
    headers = {
        "x-rapidapi-host": "shazam-song-recognition-api.p.rapidapi.com",
        "x-rapidapi-key": RAPIDAPI_KEY
    }
    try:
        if retry_wait > 0:
            print(f"[Shazam] {retry_wait}s kutilmoqda...")
            time.sleep(retry_wait)
        with open(audio_path, 'rb') as f:
            files = {"file": ("audio.mp3", f, "audio/mpeg")}
            response = requests.post(url, headers=headers, files=files, timeout=40)
        print(f"[Shazam] {response.status_code}: {response.text[:300]}")
        if response.status_code != 200:
            return None, 0
        data = response.json()
        retry_ms = data.get("retryms", 0)
        if data.get("matches"):
            track = data.get("track", {})
            title  = track.get("title", "").strip()
            artist = track.get("subtitle", "").strip()
            if title:
                return (f"{artist} - {title}" if artist else title), 0
        return None, retry_ms
    except requests.exceptions.Timeout:
        print("[Shazam Error] Timeout")
        return None, 0
    except Exception as e:
        print(f"[Shazam Error] {e}")
        return None, 0

def recognize_with_shazam(audio_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True
        )
        total = float(result.stdout.strip() or "0")
    except Exception:
        total = 0

    segments_to_try = []

    def make_seg(suffix, ss, duration):
        seg_path = audio_path.replace(".mp3", f"_{suffix}.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ss), "-i", audio_path,
            "-t", str(duration), "-acodec", "libmp3lame",
            "-q:a", "2", "-ar", "44100", seg_path
        ], capture_output=True)
        if os.path.exists(seg_path) and os.path.getsize(seg_path) > 1000:
            segments_to_try.append(seg_path)

    if total > 0:
        make_seg("seg1", max(5, total / 3), 20)   # O'rtadan 20s
        make_seg("seg2", 0, 20)                    # Boshidan 20s
        if total > 40:
            make_seg("seg3", max(5, total / 2), 30)  # O'rtadan 30s
    else:
        segments_to_try.append(audio_path)

    found = None
    for seg in segments_to_try:
        result, retry_ms = call_shazam_api(seg)
        if result:
            found = result
            break
        # retryms bo'lsa shu segmentni qayta sinab ko'r
        if retry_ms > 0:
            wait_sec = min(retry_ms / 1000, 15)
            result2, _ = call_shazam_api(seg, retry_wait=wait_sec)
            if result2:
                found = result2
                break
        time.sleep(1)

    for seg in segments_to_try:
        if seg != audio_path and os.path.exists(seg):
            try:
                os.remove(seg)
            except Exception:
                pass

    return found

# ──────────────────────────────────────────
# 3. YOUTUBE QIDIRUV → 10 TA NATIJA
# ──────────────────────────────────────────
def search_youtube(query, max_results=10):
    search_query = f"ytsearch{max_results}:{query}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get('entries', [])
            print(f"[Search] '{query}' → {len(entries)} ta natija topildi")
            for entry in entries:
                if not entry:
                    continue
                vid_id  = entry.get('id', '')
                vid_url = entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
                if not vid_url:
                    continue
                results.append({
                    'title':    entry.get('title', "Noma'lum"),
                    'url':      vid_url,
                    'duration': entry.get('duration') or 0,
                })
    except Exception as e:
        print(f"[Search Error] {e}")
    return results

# ──────────────────────────────────────────
# 4. QIDIRUV NATIJALARINI INLINE TUGMALAR BILAN KO'RSATISH
# ──────────────────────────────────────────
def show_search_results(chat_id, results, query):
    if not results:
        bot.send_message(chat_id, "❌ Hech narsa topilmadi.")
        return

    lines = [f"🔎 *{query}* bo'yicha natijalar:\n"]
    for i, r in enumerate(results, 1):
        dur = seconds_to_mmss(r['duration'])
        dur_str = f"  `{dur}`" if dur else ""
        lines.append(f"{i}. {r['title']}{dur_str}")
    text = "\n".join(lines)

    markup = types.InlineKeyboardMarkup(row_width=5)
    buttons = [
        types.InlineKeyboardButton(str(i), callback_data=f"pick_{chat_id}_{i-1}")
        for i in range(1, len(results) + 1)
    ]
    markup.add(*buttons[:5])
    if len(buttons) > 5:
        markup.add(*buttons[5:])

    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# ──────────────────────────────────────────
# 5. TANLANGAN QO'SHIQNI YUKLAB YUBORISH (TO'G'IRLANDI)
# ──────────────────────────────────────────
def download_and_send_song(chat_id, url, title, status_msg_id=None):
    audio_base = get_unique_path("mp3").replace(".mp3", "")
    ydl_opts = {
        'outtmpl': audio_base + ".%(ext)s",
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'age_limit': 99,
        'ignoreerrors': False,
        'ffmpeg_location': os.path.dirname(os.environ.get('PATH', '/usr/bin').split(':')[0]) if os.name != 'nt' else None,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        actual_audio = audio_base + ".mp3"
        if not os.path.exists(actual_audio):
            for f in sorted(os.listdir("downloads"),
                            key=lambda x: os.path.getmtime(f"downloads/{x}"), reverse=True):
                if f.endswith(".mp3") and "_seg" not in f:
                    actual_audio = f"downloads/{f}"
                    break

        if os.path.exists(actual_audio):
            with open(actual_audio, 'rb') as f:
                bot.send_audio(chat_id, f, title=title, caption=f"🎵 {title}")
            if status_msg_id:
                try:
                    bot.delete_message(chat_id, status_msg_id)
                except Exception:
                    pass
        else:
            bot.send_message(chat_id, "❌ Fayl yuklanmadi. Boshqa variant tanlang.")

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "age" in err or "sign in" in err:
            bot.send_message(chat_id, "🔞 Bu video yoshga oid cheklov tufayli yuklanmadi. Boshqa variant tanlang.")
        elif "unavailable" in err or "private" in err:
            bot.send_message(chat_id, "🚫 Bu video mavjud emas yoki yopiq. Boshqa variant tanlang.")
        elif "copyright" in err:
            bot.send_message(chat_id, "©️ Bu video mualliflik huquqi tufayli bloklangan. Boshqa variant tanlang.")
        else:
            bot.send_message(
                chat_id,
                f"❌ Yuklab bo'lmadi. Boshqa variant tanlang.\n`{str(e)[:100]}`",
                parse_mode="Markdown"
            )
        print(f"[Download Error] {e}")

    except Exception as e:
        bot.send_message(chat_id, "❌ Xatolik yuz berdi. Boshqa variant tanlang.")
        print(f"[Download Error] {e}")

    finally:
        for f in os.listdir("downloads"):
            if "_seg" not in f:
                try:
                    os.remove(f"downloads/{f}")
                except Exception:
                    pass

# ──────────────────────────────────────────
# 6. CALLBACK: FOYDALANUVCHI RAQAM BOSDI (TO'G'IRLANDI)
# ──────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: call.data.startswith("pick_"))
def handle_pick(call):
    parts = call.data.split("_")
    chat_id = int(parts[1])
    index   = int(parts[2])

    results = search_results.get(chat_id, [])
    if not results or index >= len(results):
        bot.answer_callback_query(call.id, "❌ Natija topilmadi.")
        return

    chosen = results[index]

    # Faqat callback javobini yuborish — xabar va tugmalar O'ZGARTIRILMAYDI
    bot.answer_callback_query(call.id, f"⬇️ Yuklanmoqda: {chosen['title'][:30]}...")

    # Yuklanmoqda xabari alohida yangi xabar sifatida yuboriladi
    status_msg = bot.send_message(chat_id, f"⬇️ Yuklanmoqda: *{chosen['title']}*", parse_mode="Markdown")

    download_and_send_song(chat_id, chosen['url'], chosen['title'],
                           status_msg_id=status_msg.message_id)

    # search_results saqlanib qoladi — foydalanuvchi boshqa variant ham tanlay olsin

# ──────────────────────────────────────────
# 7. BOT HANDLERLARI
# ──────────────────────────────────────────
@bot.message_handler(commands=['start'])
def start(message):
    save_user(message.chat.id)
    bot.send_message(
        message.chat.id,
        "👋 Salom! Men video/audio yuklovchi botman.\n\n"
        "📹 Link yuboring → video + MP3 + original qo'shiq!\n"
        "🔍 Qo'shiq nomini yozing → 10 ta variant, siz tanlaysiz!\n\n"
        "Qo'llab-quvvatlanadi: Instagram, YouTube, TikTok va boshqalar.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['stats'])
def stats(message):
    if message.chat.id != ADMIN_ID:
        return
    users = load_users()
    bot.send_message(message.chat.id, f"👥 Jami foydalanuvchilar: *{len(users)} ta*", parse_mode="Markdown")

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.chat.id != ADMIN_ID:
        return
    broadcast_mode[ADMIN_ID] = True
    bot.send_message(message.chat.id, "✍️ Hammaga yuboriladigan xabarni yozing:")

@bot.message_handler(func=lambda m: m.text and m.text.startswith("http"))
def download_all(message):
    save_user(message.chat.id)
    url = message.text.strip()
    status_msg = bot.send_message(message.chat.id, "⏳ Video yuklanmoqda...")
    video_path = get_unique_path("mp4")
    audio_base = get_unique_path("mp3").replace(".mp3", "")
    title = "video"

    # ── Video ──
    try:
        ydl_opts_video = {
            'outtmpl': video_path,
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

        if os.path.exists(video_path):
            size = os.path.getsize(video_path)
            if size > 50 * 1024 * 1024:
                bot.edit_message_text("⚠️ Video 50MB dan katta, faqat audio yuboriladi.",
                                      message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text("📹 Video yuborilmoqda...", message.chat.id, status_msg.message_id)
                with open(video_path, 'rb') as f:
                    bot.send_video(message.chat.id, f, caption=f"📹 {title}", supports_streaming=True)
    except Exception as e:
        bot.edit_message_text("❌ Video yuklab bo'lmadi.", message.chat.id, status_msg.message_id)
        print(f"[Video Error] {e}")

    # ── Audio ──
    try:
        bot.edit_message_text("🎵 MP3 yuklanmoqda...", message.chat.id, status_msg.message_id)
        ydl_opts_audio = {
            'outtmpl': audio_base + ".%(ext)s",
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
            'ffmpeg_location': os.path.dirname(os.environ.get('PATH', '/usr/bin').split(':')[0]) if os.name != 'nt' else None,
        }
        with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
            ydl.extract_info(url, download=True)

        actual_audio = audio_base + ".mp3"
        if not os.path.exists(actual_audio):
            for f in sorted(os.listdir("downloads"),
                            key=lambda x: os.path.getmtime(f"downloads/{x}"), reverse=True):
                if f.endswith(".mp3") and "_seg" not in f:
                    actual_audio = f"downloads/{f}"
                    break

        if os.path.exists(actual_audio):
            with open(actual_audio, 'rb') as f:
                bot.send_audio(message.chat.id, f, title=title, caption=f"🎵 {title}")

            # ── Qo'shiq aniqlash ──
            bot.edit_message_text("🎧 Qo'shiq aniqlanmoqda...", message.chat.id, status_msg.message_id)

            song_name = get_music_from_metadata(url)
            print(f"[Metadata Music] {song_name}")

            if not song_name:
                bot.edit_message_text("🔍 Shazam bilan aniqlanmoqda...", message.chat.id, status_msg.message_id)
                song_name = recognize_with_shazam(actual_audio)
                print(f"[Shazam Music] {song_name}")

            if song_name:
                bot.edit_message_text(
                    f"✅ Topildi: *{song_name}*\n🔎 Variantlar qidirilmoqda...",
                    message.chat.id, status_msg.message_id, parse_mode="Markdown"
                )
                results = search_youtube(song_name)
                if results:
                    search_results[message.chat.id] = results
                    try:
                        bot.delete_message(message.chat.id, status_msg.message_id)
                    except Exception:
                        pass
                    show_search_results(message.chat.id, results, song_name)
                else:
                    bot.edit_message_text("❌ YouTube'dan topilmadi.", message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text(
                    "❓ Qo'shiq avtomatik aniqlanmadi.\n\n"
                    "🎵 Qo'shiq nomini yozing — 10 ta variant ko'rsataman!",
                    message.chat.id, status_msg.message_id, parse_mode="Markdown"
                )
                pending_music[message.chat.id] = True

    except Exception as e:
        bot.edit_message_text("❌ MP3 yuklab bo'lmadi.", message.chat.id, status_msg.message_id)
        print(f"[Audio Error] {e}")

    finally:
        for f in os.listdir("downloads"):
            if "_seg" not in f:
                try:
                    os.remove(f"downloads/{f}")
                except Exception:
                    pass

# Qo'lda qo'shiq nomi yozilganda
@bot.message_handler(func=lambda m: m.text and pending_music.get(m.chat.id))
def manual_song_search(message):
    # Agar link kelsa — pending_music ni tozalab download_all ga yo'naltir
    if message.text.strip().startswith("http"):
        pending_music.pop(message.chat.id, None)
        download_all(message)
        return

    query = message.text.strip()
    pending_music.pop(message.chat.id, None)
    status_msg = bot.send_message(message.chat.id, f"🔍 *{query}* qidirilmoqda...", parse_mode="Markdown")

    results = search_youtube(query)
    if results:
        search_results[message.chat.id] = results
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass
        show_search_results(message.chat.id, results, query)
    else:
        bot.edit_message_text("❌ Hech narsa topilmadi.", message.chat.id, status_msg.message_id)

# Oddiy matn → qo'shiq qidirish
@bot.message_handler(func=lambda m: m.text and not m.text.startswith("http"))
def text_search(message):
    query = message.text.strip()
    if len(query) < 2:
        bot.send_message(message.chat.id, "📎 Link yuboring yoki qo'shiq nomini yozing!\n/start — yordam")
        return

    status_msg = bot.send_message(message.chat.id, f"🔍 *{query}* qidirilmoqda...", parse_mode="Markdown")
    results = search_youtube(query)
    if results:
        search_results[message.chat.id] = results
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass
        show_search_results(message.chat.id, results, query)
    else:
        bot.edit_message_text("❌ Hech narsa topilmadi.", message.chat.id, status_msg.message_id)


@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and broadcast_mode.get(ADMIN_ID))
def do_broadcast(message):
    broadcast_mode.pop(ADMIN_ID, None)
    users = load_users()
    success, failed = 0, 0
    status = bot.send_message(ADMIN_ID, f"📤 Yuborilmoqda... (0/{len(users)})")
    for i, uid in enumerate(users):
        try:
            bot.copy_message(int(uid), ADMIN_ID, message.message_id)
            success += 1
        except Exception:
            failed += 1
        if (i + 1) % 10 == 0:
            try:
                bot.edit_message_text(f"📤 Yuborilmoqda... ({i+1}/{len(users)})", ADMIN_ID, status.message_id)
            except Exception:
                pass
        time.sleep(0.05)
    bot.edit_message_text(
        f"✅ Yuborildi: *{success}* ta\n❌ Xato: *{failed}* ta",
        ADMIN_ID, status.message_id, parse_mode="Markdown"
    )

print("Bot ishga tushdi...")
bot.infinity_polling()
