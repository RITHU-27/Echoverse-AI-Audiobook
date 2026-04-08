# app.py
import streamlit as st
from datetime import datetime
import tempfile
import os
import textwrap
from dataclasses import dataclass
from typing import List
import sqlite3
import hashlib
import pyttsx3
import requests
from dotenv import load_dotenv
from huggingface_hub import InferenceClient
import base64
import re
import html as html_escape
import streamlit.components.v1 as components
import urllib.parse  # for building share URLs

# Optional multi-language TTS (gTTS)
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except Exception:
    GTTS_AVAILABLE = False

# -------------------------
# Load environment & config
# -------------------------
load_dotenv()

# IBM (kept in case you want to use later)
IBM_API_KEY = os.getenv("IBM_API_KEY")
IBM_WATSONX_URL = os.getenv("IBM_WATSONX_URL")
IBM_TTS_APIKEY = os.getenv("IBM_TTS_APIKEY")
IBM_TTS_URL = os.getenv("IBM_TTS_URL")

# Hugging Face config
HF_API_KEY = os.getenv("HF_API_KEY")
HF_TTS_MODEL = os.getenv("HF_TTS_MODEL", "espnet/kan-bayashi_ljspeech_vits")
HF_REWRITE_MODEL = os.getenv("HF_REWRITE_MODEL", "meta-llama/Llama-3.1-8B-Instruct")

hf_client = InferenceClient(api_key=HF_API_KEY) if HF_API_KEY else None

MAX_INPUT_CHARS = 40000

# -------------------------
# Dataclass
# -------------------------
@dataclass
class Narration:
    timestamp: str
    original_text: str
    rewritten_text: str
    tone: str
    voice: str
    speed_multiplier: float
    audio_format: str
    audio_bytes: bytes
    filename: str
    word_count: int
    sentence_count: int
    estimated_time_sec: float
    language: str = "en"

# -------------------------
# Page config and theme selector (appears before UI)
# -------------------------
st.set_page_config(page_title="EchoVerse — AI Audiobook Creator", layout="centered")
if "theme" not in st.session_state:
    st.session_state.theme = "Dark"  # default
theme_choice = st.sidebar.radio("Theme", ["Dark", "Light"], 
                                index=0 if st.session_state.theme == "Dark" else 1)
st.session_state.theme = theme_choice

# -------------------------
# Database setup with migration
# -------------------------
DB_PATH = "users.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()

# Ensure users table exists and migrate if required (add email column if missing)
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
table_exists = c.fetchone() is not None

if table_exists:
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if "email" not in columns:
        # create new table with email column and migrate data
        c.execute("""
        CREATE TABLE IF NOT EXISTS users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            email TEXT UNIQUE,
            password TEXT
        )
        """)
        # copy existing data (assumes old table had id, username, password)
        try:
            c.execute("INSERT OR IGNORE INTO users_new (id, username, password) SELECT id, username, password FROM users")
        except sqlite3.OperationalError:
            pass
        c.execute("DROP TABLE IF EXISTS users")
        c.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()
else:
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT
    )
    """)
    conn.commit()

# -------------------------
# Session state defaults
# -------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""
if "last_narration" not in st.session_state:
    st.session_state.last_narration = None
if "library" not in st.session_state:
    # store list of Narration objects
    st.session_state["library"] = []  # type: List[Narration]
if "bookmarks" not in st.session_state:
    st.session_state["bookmarks"] = []  # type: List[Narration]
if "page" not in st.session_state:
    st.session_state.page = "Home"
if "last_share_links" not in st.session_state:
    st.session_state.last_share_links = {}  # filename -> url cache

# -------------------------
# Auth helpers
# -------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username: str, email: str, password: str) -> bool:
    username = username.strip()
    email = email.strip()
    if not username or not email or not password:
        return False
    c.execute("SELECT * FROM users WHERE username=? OR email= ?", (username, email))
    if c.fetchone():
        return False
    c.execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
              (username, email, hash_password(password)))
    conn.commit()
    return True

def login_user(username: str, password: str) -> bool:
    c.execute("SELECT * FROM users WHERE username=? AND password=?", (username, hash_password(password)))
    return c.fetchone() is not None

# -------------------------
# Voice helpers
# -------------------------
def get_system_voices():
    engine = None
    male_voices, female_voices = [], []
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        for v in voices:
            name_lower = v.name.lower()
            if any(f in name_lower for f in ["female", "zira", "lisa", "allison", "kate", "samantha"]):
                female_voices.append(v.name)
            else:
                male_voices.append(v.name)
    except Exception:
        pass
    if not male_voices:
        male_voices = ["default"]
    if not female_voices:
        female_voices = ["default"]
    return male_voices, female_voices

male_voices, female_voices = get_system_voices()

def synthesize_with_pyttsx3(text, voice_name, out_path, rate=150):
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    for v in voices:
        if v.name == voice_name:
            engine.setProperty("voice", v.id)
            break
    engine.setProperty("rate", rate)
    engine.save_to_file(text, out_path)
    engine.runAndWait()
    engine.stop()

# -------------------------
# Simple rewrite fallback + HF rewrite
# -------------------------
def simple_rewrite_fallback(text: str, tone: str) -> str:
    text = text.strip()
    if not text:
        return text
    if tone.lower() == "neutral":
        sentences = text.replace("\n", " ").split(".")
        cleaned = ". ".join(s.strip().capitalize() for s in sentences if s.strip())
        return cleaned.strip() + ("" if cleaned.endswith(".") else ".")
    elif tone.lower() == "suspenseful":
        s = text.replace("\n", " ")
        chunks = textwrap.wrap(s, width=80)
        spooky = " ...\n".join(chunk.rstrip() for chunk in chunks)
        return spooky + "\n\nSomething unexpected awaits..."
    elif tone.lower() == "inspiring":
        s = text.replace("\n", " ")
        phrases = ["Imagine the possibilities.", "You can do this.", "Step forward with confidence."]
        return s + "\n\n" + " ".join(phrases)
    return text

def rewrite_text_hf(text: str, tone: str) -> str:
    if not HF_API_KEY or not hf_client:
        return simple_rewrite_fallback(text, tone)
    prompt = f"Rewrite the following text in a {tone.lower()} tone. Preserve the original meaning and make it expressive:\n\n{text}"
    try:
        response = hf_client.chat.completions.create(
            model=HF_REWRITE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.7
        )
        rewritten = response.choices[0].message["content"].strip()
        return rewritten
    except Exception:
        return simple_rewrite_fallback(text, tone)

# -------------------------
# Hugging Face TTS
# -------------------------
def call_hf_tts(text: str, model: str = None, audio_format: str = "mp3", voice: str = None):
    if not HF_API_KEY:
        return None
    model = model or HF_TTS_MODEL
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {
        "Authorization": f"Bearer {HF_API_KEY}",
        "Accept": f"audio/{audio_format}",
        "Content-Type": "application/json",
    }
    payload = {"inputs": text}
    params = {}
    if voice and voice != "default":
        params["voice"] = voice
    if params:
        payload["parameters"] = params
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code == 200 and resp.content:
            return resp.content
        else:
            return None
    except requests.exceptions.RequestException:
        return None

# -------------------------
# gTTS helper (multi-language)
# -------------------------
LANG_CODES = {
    "English": "en",
    "Hindi": "hi",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Japanese": "ja",
    "Chinese": "zh-cn"
}

def call_gtts(text: str, lang_code: str = "en", audio_format: str = "mp3"):
    if not GTTS_AVAILABLE:
        return None
    # gTTS only outputs mp3; if caller asked for wav we still return mp3 bytes but adjust filename
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.close()
        tts = gTTS(text=text, lang=lang_code)
        tts.save(tmp.name)
        with open(tmp.name, "rb") as f:
            data = f.read()
        try:
            os.remove(tmp.name)
        except Exception:
            pass
        return data
    except Exception:
        return None

# -------------------------
# Robust upload/share helpers (returns (url, error_message))
# -------------------------
def upload_to_transfer_sh(audio_bytes: bytes, filename: str, max_attempts: int = 2):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()

        errors = []

        # 1) 0x0.st - POST files with key 'file'
        try:
            with open(tmp.name, "rb") as f:
                resp = requests.post("https://0x0.st", files={"file": f}, timeout=120)
            if resp.status_code in (200, 201) and resp.text:
                return resp.text.strip(), None
            else:
                errors.append(f"0x0.st returned {resp.status_code}: {resp.text}")
        except Exception as e:
            errors.append(f"0x0.st exception: {repr(e)}")

        # 2) transfer.sh - PUT to https://transfer.sh/<filename> (may be unreliable)
        try:
            with open(tmp.name, "rb") as f:
                resp = requests.put(f"https://transfer.sh/{filename}", data=f, timeout=120)
            if resp.status_code in (200, 201) and resp.text:
                return resp.text.strip(), None
            else:
                errors.append(f"transfer.sh returned {resp.status_code}: {resp.text}")
        except Exception as e:
            errors.append(f"transfer.sh exception: {repr(e)}")

        # 3) file.io - POST (response may be JSON)
        try:
            with open(tmp.name, "rb") as f:
                resp = requests.post("https://file.io", files={"file": f}, timeout=120)
            try:
                j = resp.json()
            except Exception:
                j = None
            if resp.status_code in (200, 201) and j:
                link = j.get("link") or j.get("url") or j.get("data") or j.get("success") or None
                if not link and isinstance(resp.text, str) and resp.text.strip().startswith("http"):
                    link = resp.text.strip()
                if link:
                    return link, None
                else:
                    errors.append(f"file.io returned JSON without link: {j}")
            else:
                errors.append(f"file.io returned {resp.status_code}: {resp.text}")
        except Exception as e:
            errors.append(f"file.io exception: {repr(e)}")

        # All attempts failed
        return None, " | ".join(errors)
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass

def build_share_buttons_html(public_url: str, filename: str, text_prefix: str = "Listen to my audio:"):
    encoded_url = urllib.parse.quote_plus(public_url)
    tweet_text = urllib.parse.quote_plus(f"{text_prefix} {public_url}")
    whatsapp_text = urllib.parse.quote_plus(f"{text_prefix} {public_url}")
    telegram_text = urllib.parse.quote_plus(f"{text_prefix} {public_url}")
    email_subject = urllib.parse.quote_plus(f"Audio from EchoVerse: {filename}")
    email_body = urllib.parse.quote_plus(f"{text_prefix} {public_url}")

    html = f"""
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <a target="_blank" href="https://twitter.com/intent/tweet?text={tweet_text}" style="padding:8px 10px; background:#1DA1F2; color:white; border-radius:8px; text-decoration:none;">Share on X</a>
      <a target="_blank" href="https://www.facebook.com/sharer/sharer.php?u={encoded_url}" style="padding:8px 10px; background:#1877F2; color:white; border-radius:8px; text-decoration:none;">Share on Facebook</a>
      <a target="_blank" href="https://api.whatsapp.com/send?text={whatsapp_text}" style="padding:8px 10px; background:#25D366; color:white; border-radius:8px; text-decoration:none;">Share on WhatsApp</a>
      <a target="_blank" href="https://t.me/share/url?url={encoded_url}&text={telegram_text}" style="padding:8px 10px; background:#0088cc; color:white; border-radius:8px; text-decoration:none;">Share on Telegram</a>
      <a target="_blank" href="mailto:?subject={email_subject}&body={email_body}" style="padding:8px 10px; background:#555; color:white; border-radius:8px; text-decoration:none;">Share by Email</a>
      <button id="copy_link_btn" style="padding:8px 10px; background:rgba(255,255,255,0.06); color:white; border-radius:8px; border:none; cursor:pointer;" onclick="navigator.clipboard.writeText('{public_url}').then(()=>{{this.innerText='Link Copied'}}).catch(()=>{{alert('Copy failed')}})">Copy Link</button>
    </div>
    """
    return html

# -------------------------
# Generate narration
# -------------------------
def generate_narration(user_text, tone, selected_voice, audio_format, speed_multiplier, speech_rate, use_gtts=False, gtts_lang_code="en"):
    text = (user_text or "").strip()
    if not text:
        return None
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]

    # 1) Rewrite using HF Llama or fallback
    rewritten = rewrite_text_hf(text, tone)

    word_count = len(re.findall(r'\S+', rewritten))
    sentence_count = rewritten.count(".")
    estimated_time_sec = (word_count / (speech_rate if speech_rate > 0 else 150)) * 60

    # If gTTS is requested, force mp3 format (gTTS only outputs mp3)
    if use_gtts:
        audio_format = "mp3"

    filename = f"{st.session_state.username or 'user'}_echoverse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{audio_format}"

    audio_bytes = None

    # 2) If user selected gTTS and it's available, try it
    if use_gtts and GTTS_AVAILABLE:
        audio_bytes = call_gtts(rewritten, lang_code=gtts_lang_code, audio_format=audio_format)

    # 3) Try Hugging Face TTS next (if not already produced)
    if audio_bytes is None and HF_API_KEY:
        audio_bytes = call_hf_tts(rewritten, model=HF_TTS_MODEL, audio_format=audio_format, voice=selected_voice)

    # 4) Fallback to pyttsx3
    if audio_bytes is None:
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{audio_format}")
        tmp_file.close()
        try:
            synthesize_with_pyttsx3(rewritten, selected_voice, tmp_file.name, rate=speech_rate)
            with open(tmp_file.name, "rb") as f:
                audio_bytes = f.read()
        finally:
            try:
                os.remove(tmp_file.name)
            except Exception:
                pass

    return Narration(
        datetime.now().isoformat(),
        text,
        rewritten,
        tone,
        selected_voice,
        speed_multiplier,
        audio_format,
        audio_bytes,
        filename,
        word_count,
        sentence_count,
        estimated_time_sec,
        language=gtts_lang_code
    )

# -------------------------
# Helper: render karaoke player (theme-aware)
# -------------------------
def render_karaoke_player(narr: Narration, height: int = 320):
    """
    Render a karaoke-style player for a Narration.
    - Uses a word-based approximation (split on whitespace) to highlight words as audio plays.
    - Clicking a word seeks the audio approximately to that position.
    """
    # Safety guards
    if not narr:
        st.warning("No narration to render")
        return
    if not getattr(narr, "audio_bytes", None):
        st.warning("No audio available for this narration")
        return
    if not getattr(narr, "rewritten_text", "").strip():
        st.warning("No text available to render karaoke")
        return

    is_light = st.session_state.get("theme", "Dark") == "Light"
    text_color = "#111" if is_light else "#fff"
    box_bg = "linear-gradient(180deg, #ffffff, #FFEDE0)" if is_light else "linear-gradient(135deg, rgba(0,0,0,0.25), rgba(255,255,255,0.03))"
    box_shadow = "0 6px 18px rgba(0,0,0,0.06)" if is_light else "0 8px 24px rgba(0,0,0,0.2)"
    word_active_bg = "rgba(0,0,0,0.08)" if is_light else "rgba(255,255,255,0.22)"
    play_btn_bg = "linear-gradient(90deg,#FFB4A2,#FF7A59)" if is_light else "linear-gradient(90deg,#9D50BB,#6E48AA)"
    download_link_bg = "rgba(0,0,0,0.06)" if is_light else "rgba(255,255,255,0.06)"
    wrapper_font = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial"

    # create data url for audio
    try:
        b64 = base64.b64encode(narr.audio_bytes).decode("utf-8")
    except Exception:
        st.error("Failed to prepare audio for playback")
        return
    data_url = f"data:audio/{narr.audio_format};base64,{b64}"

    # Tokenize into words (simple but robust)
    words = re.findall(r"\S+", narr.rewritten_text)
    if not words:
        st.warning("No words found in rewritten text")
        return

    spans = []
    for i, w in enumerate(words):
        safe = html_escape.escape(w)
        spans.append(f'<span class="word" data-index="{i}">{safe}</span>')
    text_html = " ".join(spans)  # spacing keeps readability

    html_player = f"""
    <style>
    .karaoke-wrapper {{ font-family: {wrapper_font}; color: {text_color}; }}
    .karaoke-box {{ background: {box_bg}; border-radius: 12px; padding: 16px; box-shadow: {box_shadow}; color: {text_color}; }}
    .text-area {{ line-height: 1.6; font-size: 18px; max-height: 220px; overflow-y: auto; padding: 8px; border-radius: 8px; background: transparent; color: {text_color}; }}
    .word {{ display:inline-block; padding:2px 4px; margin:1px 0; border-radius:6px; transition:all .12s ease; cursor:pointer; }}
    .word.active {{ background:{word_active_bg}; transform: translateY(-2px); font-weight:700; color:#000; }}
    .controls {{ display:flex; align-items:center; gap:8px; margin-top:12px; }}
    .progress {{ flex:1; }}
    .time {{ min-width:88px; text-align:right; font-size:14px; opacity:.9; color:{text_color}; }}
    .play-btn {{ padding:8px 12px; border-radius:8px; background:{play_btn_bg}; color:white; border:none; cursor:pointer; font-weight:600; }}
    a.download-link {{ padding:8px 10px; background:{download_link_bg}; color:{text_color}; border-radius:8px; text-decoration:none; font-size:14px; margin-left:8px; }}
    </style>

    <div class="karaoke-wrapper">
      <div class="karaoke-box">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div style="font-weight:700; font-size:16px;">{html_escape.escape(narr.filename)}</div>
          <div style="font-size:13px; opacity:0.9;">Words: {narr.word_count} • Est: {narr.estimated_time_sec:.1f}s</div>
        </div>

        <div id="karaoke_text" class="text-area">{text_html}</div>

        <div class="controls">
          <button id="play_btn" class="play-btn">Play ▶</button>
          <input id="seek" class="progress" type="range" min="0" max="100" value="0" />
          <div class="time"><span id="cur_time">0:00</span> / <span id="dur_time">0:00</span></div>
          <a id="download_link" class="download-link" download="{narr.filename}">Download</a>
        </div>

        <audio id="karaoke_audio" preload="metadata"></audio>
      </div>
    </div>

    <script>
    (function(){{
      const audio = document.getElementById('karaoke_audio');
      const playBtn = document.getElementById('play_btn');
      const seek = document.getElementById('seek');
      const curTime = document.getElementById('cur_time');
      const durTime = document.getElementById('dur_time');
      const downloadLink = document.getElementById('download_link');
      const words = Array.from(document.querySelectorAll('.word'));
      const dataUrl = '{data_url}';

      audio.src = dataUrl;
      downloadLink.href = dataUrl;

      function fmt(s) {{ if (isNaN(s)) return '0:00'; const m = Math.floor(s/60); const ss = Math.floor(s%60); return m + ':' + (ss<10?('0'+ss):ss); }};

      audio.addEventListener('loadedmetadata', () => {{ durTime.textContent = fmt(audio.duration); }});

      let lastActive = -1;
      function highlightByTime() {{
        if (!audio.duration || words.length === 0) return;
        const ratio = Math.min(1, Math.max(0, audio.currentTime / audio.duration));
        const idx = Math.min(words.length - 1, Math.floor(ratio * words.length));
        if (idx !== lastActive) {{
          if (lastActive >= 0 && words[lastActive]) words[lastActive].classList.remove('active');
          if (words[idx]) {{
            words[idx].classList.add('active');
            try {{ words[idx].scrollIntoView({{ behavior:'smooth', block:'center', inline:'nearest' }}); }} catch(e){{}}
          }}
          lastActive = idx;
        }}
      }}

      audio.addEventListener('timeupdate', () => {{
        if (!isNaN(audio.duration)) {{
          seek.value = (audio.currentTime / audio.duration) * 100;
          curTime.textContent = fmt(audio.currentTime);
          highlightByTime();
        }}
      }});

      playBtn.addEventListener('click', () => {{
        if (audio.paused) {{ audio.play(); playBtn.textContent = 'Pause ⏸'; }} else {{ audio.pause(); playBtn.textContent = 'Play ▶'; }}
      }});

      seek.addEventListener('input', (e) => {{
        if (!isNaN(audio.duration)) audio.currentTime = (Number(e.target.value)/100) * audio.duration;
      }});

      // clicking a word seeks to its approximate position
      words.forEach((w, i) => {{
        w.addEventListener('click', () => {{
          if (!isNaN(audio.duration)) {{
            audio.currentTime = (i / Math.max(1, words.length)) * audio.duration;
            highlightByTime();
            if (audio.paused) {{ audio.play(); playBtn.textContent = 'Pause ⏸'; }}
          }}
        }});
      }});

      window.addEventListener('beforeunload', () => {{ try {{ audio.pause(); audio.src = ''; }} catch(e){{}} }});
    }})();
    </script>
    """

    components.html(html_player, height=height, scrolling=True)

# -------------------------
# Page-level CSS for Light and Dark (keeps original look & feel but adapts colors)
# -------------------------
if st.session_state.theme == "Light":
    page_bg = """
    <style>
    .stApp {
        background: linear-gradient(135deg, #ffffff, #FFEDE0);
        color: #111;
    }
    .stButton>button {
        background: linear-gradient(90deg, #FFB4A2, #FF7A59);
        color:#fff;
        border-radius: 12px;
        font-weight: bold;
    }
    h1,h2,h3,h4,h5,h6,p,label {
        color:#111 !important;
    }
    .stTextArea textarea {
        background-color: #ffffff;
        color:#111;
    }
    </style>
    """
else:
    page_bg = """
    <style>
    .stApp {
        background: linear-gradient(135deg, #7F00FF, #E100FF);
        color: white;
    }
    .stButton>button {
        background: linear-gradient(90deg, #9D50BB, #6E48AA);
        color:white;
        border-radius:12px;
        font-weight:bold;
    }
    h1,h2,h3,h4,h5,h6,p,label {
        color:white !important;
    }
    .stTextArea textarea {
        background-color: rgba(255,255,255,0.06);
        color:white;
    }
    </style>
    """
st.markdown(page_bg, unsafe_allow_html=True)

# -------------------------
# LOGIN / SIGNUP PAGE
# -------------------------
if not st.session_state.logged_in:
    st.title("🔐 EchoVerse Login / Sign Up")
    option = st.radio("Choose Action:", ["Login", "Sign Up"]) 

    if option == "Sign Up":
        username = st.text_input("Username", key="su_username")
        email = st.text_input("Email", key="su_email")
        password = st.text_input("Password", type="password", key="su_password")
        confirm_password = st.text_input("Confirm Password", type="password", key="su_confirm")
        if st.button("Sign Up"):
            if not username.strip() or not email.strip() or not password.strip() or not confirm_password.strip():
                st.warning("All fields are required")
            elif password != confirm_password:
                st.warning("Passwords do not match")
            else:
                ok = register_user(username, email, password)
                if ok:
                    st.success("Account created! Please login.")
                else:
                    st.error("Username or email already exists or invalid input.")

    elif option == "Login":
        username = st.text_input("Username", key="li_username")
        password = st.text_input("Password", type="password", key="li_password")
        if st.button("Login"):
            if not username.strip() or not password.strip():
                st.warning("Enter username and password")
            elif login_user(username, password):
                st.session_state.logged_in = True
                st.session_state.username = username
                st.success(f"Welcome {username}!")
                st.rerun()
            else:
                st.error("Invalid username or password")
    st.stop()

# -------------------------
# After login UI + Sidebar
# -------------------------
st.sidebar.markdown(f"**Signed in as:** `{st.session_state.username}`")
if st.sidebar.button("Logout"):
    st.session_state.logged_in = False
    st.session_state.username = ""
    st.session_state.page = "Home"
    st.rerun()

# Navigation
if st.session_state.page != "Result":
    st.session_state.page = st.sidebar.radio(
        "📌 Navigation",
        ["Home", "Library", "Bookmarks"],
        index=["Home", "Library", "Bookmarks"].index(st.session_state.page) 
              if st.session_state.page in ["Home", "Library", "Bookmarks"] else 0
    )

# -------------------------
# HOME (Input)
# -------------------------
if st.session_state.page == "Home":
    st.title("🎙️ EchoVerse — AI Audiobook Creator")

    col1, col2 = st.columns([3, 1])
    with col2:
        uploaded_file = st.file_uploader("Or upload a .txt file", type=["txt"], key="upload_txt")
        if uploaded_file:
            try:
                file_text = uploaded_file.read().decode("utf-8", errors="ignore")
            except Exception:
                file_text = uploaded_file.read().decode("latin-1", errors="ignore")
            # Only set the input text if it's currently empty
            if not st.session_state.get("input_text", "").strip():
                st.session_state["input_text"] = file_text
                st.success("File loaded!")
    with col1:
        user_text = st.text_area("Enter or paste your text:", 
                                  height=220, key="input_text")

    tone = st.selectbox("Choose a tone:", 
                        ["Neutral", "Suspenseful", "Inspiring", "Friendly", 
                         "Formal", "Sarcastic", "Excited", "Dramatic"], 
                        key="tone_choice")

    voice_gender = st.selectbox("Choose Voice Gender:", ["Male", "Female"], 
                                key="voice_gender_choice")
    voice_options = male_voices if voice_gender == "Male" else female_voices
    if not voice_options:
        voice_options = ["default"]
    selected_voice = st.selectbox("Choose Voice:", voice_options, key="selected_voice")

    audio_format = st.radio("Audio Format:", ["mp3"], index=0, key="audio_format_choice")

    # gTTS multi-language option
    st.markdown("---")
    use_gtts = st.checkbox("Optional: Multi-accent", value=False)
    gtts_lang = "English"
    if use_gtts:
        if not GTTS_AVAILABLE:
            st.warning("gTTS library not installed. Install with `pip install gTTS` to enable this feature.")
            use_gtts = False
        else:
            gtts_lang = st.selectbox("Select Accent", list(LANG_CODES.keys()), index=0)

    speed_multiplier = st.slider("Speech Speed (0.25x – 2x)", 0.25, 2.0, 1.0, 0.25, 
                                 key="speed_multiplier")
    base_rate = 150
    speech_rate = int(base_rate * speed_multiplier)
    speech_rate = max(100, min(speech_rate, 300))

    if st.button("Generate Audiobook ➡️"):
        gtts_lang_code = LANG_CODES.get(gtts_lang, "en")
        narr = generate_narration(user_text, tone, selected_voice, audio_format, 
                                   speed_multiplier, speech_rate, use_gtts=use_gtts, 
                                   gtts_lang_code=gtts_lang_code)
        if narr:
            st.session_state.last_narration = narr
            st.session_state.library.append(narr)
            st.session_state.page = "Result"
            st.rerun()

# -------------------------
# RESULT
# -------------------------
if st.session_state.page == "Result":
    st.title("📖 Your Audiobook Result")
    narr = st.session_state.last_narration

    if not narr:
        st.info("No result yet. Go to **Home** and generate one.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Original Text")
            st.write(narr.original_text[:1000] + "..." 
                     if len(narr.original_text) > 1000 else narr.original_text)
        with c2:
            st.subheader("Rewritten Text")
            st.write(narr.rewritten_text[:1000] + "..." 
                     if len(narr.rewritten_text) > 1000 else narr.rewritten_text)

        st.markdown(f"**Words:** {narr.word_count} | **Sentences:** {narr.sentence_count} | **Duration (est):** {narr.estimated_time_sec:.1f}s")

        render_karaoke_player(narr, height=360)

        d_col, b_col, s_col = st.columns([2, 2, 3])
        with d_col:
            st.download_button("⬇️ Download Audio", narr.audio_bytes, file_name=narr.filename)
        with b_col:
            if st.button("🔖 Bookmark this Audio"):
                st.session_state.bookmarks.append(narr)
                st.success("✅ Added to Bookmarks")
        with s_col:
            cached = st.session_state.last_share_links.get(narr.filename)
            if cached:
                st.markdown("**Shareable Link (cached):**")
                st.text_input("Public URL", cached, key=f"share_url_{narr.filename}")
                st.markdown(build_share_buttons_html(cached, narr.filename), unsafe_allow_html=True)
            else:
                if st.button("🔗 Upload & Get Shareable Link"):
                    with st.spinner("Uploading audio (anonymous) and generating share links..."):
                        public_url, err = upload_to_transfer_sh(narr.audio_bytes, narr.filename)
                        if public_url:
                            st.session_state.last_share_links[narr.filename] = public_url
                            st.success("Upload successful! Shareable link generated.")
                            st.experimental_rerun()
                        else:
                            st.error("Upload failed: " + (err or "unknown error."))

        if st.button("⬅️ Back to Home"):
            st.session_state.page = "Home"
            st.rerun()

# -------------------------
# LIBRARY
# -------------------------
elif st.session_state.page == "Library":
    st.title("📚 My Audiobook Library")
    if not st.session_state.library:
        st.info("No audiobooks generated yet. Go to **Home** and create one!")
    else:
        for idx, narr in enumerate(st.session_state.library):
            with st.expander(f"🎧 {narr.filename} — {narr.tone} voice {narr.voice}"):
                st.markdown(f"**Words:** {narr.word_count} | **Sentences:** {narr.sentence_count} | **Duration (est):** {narr.estimated_time_sec:.1f}s")
                render_karaoke_player(narr, height=240)

                d_col, b_col, s_col = st.columns([2, 2, 3])
                with d_col:
                    st.download_button("⬇️ Download", narr.audio_bytes, file_name=narr.filename, key=f"dl_{idx}")
                with b_col:
                    if st.button("🔖 Bookmark", key=f"bm_{idx}"):
                        st.session_state.bookmarks.append(narr)
                        st.success("✅ Added to Bookmarks")
                with s_col:
                    cached = st.session_state.last_share_links.get(narr.filename)
                    if cached:
                        st.markdown("**Shareable Link (cached):**")
                        st.text_input("Public URL", cached, key=f"share_url_lib_{idx}")
                        st.markdown(build_share_buttons_html(cached, narr.filename), unsafe_allow_html=True)
                    else:
                        if st.button("🔗 Upload & Get Shareable Link", key=f"share_lib_{idx}"):
                            with st.spinner("Uploading audio (anonymous) and generating share links..."):
                                public_url, err = upload_to_transfer_sh(narr.audio_bytes, narr.filename)
                                if public_url:
                                    st.session_state.last_share_links[narr.filename] = public_url
                                    st.success("Upload successful! Shareable link generated.")
                                    st.experimental_rerun()
                                else:
                                    st.error("Upload failed: " + (err or "unknown error."))

# -------------------------
# BOOKMARKS
# -------------------------
elif st.session_state.page == "Bookmarks":
    st.title("🔖 My Bookmarks")
    if not st.session_state.bookmarks:
        st.info("No bookmarks yet. Go to **Library** and add some!")
    else:
        for idx, narr in enumerate(st.session_state.bookmarks):
            with st.expander(f"🔖 {narr.filename} — {narr.tone} voice {narr.voice}"):
                render_karaoke_player(narr, height=240)
                d_col, s_col = st.columns([2, 4])
                with d_col:
                    st.download_button("⬇️ Download", narr.audio_bytes, file_name=narr.filename, key=f"bm_dl_{idx}")
                with s_col:
                    cached = st.session_state.last_share_links.get(narr.filename)
                    if cached:
                        st.markdown("**Shareable Link (cached):**")
                        st.text_input("Public URL", cached, key=f"share_url_bm_{idx}")
                        st.markdown(build_share_buttons_html(cached, narr.filename), unsafe_allow_html=True)
                    else:
                        if st.button("🔗 Upload & Get Shareable Link", key=f"share_bm_{idx}"):
                            with st.spinner("Uploading audio (anonymous) and generating share links..."):
                                public_url, err = upload_to_transfer_sh(narr.audio_bytes, narr.filename)
                                if public_url:
                                    st.session_state.last_share_links[narr.filename] = public_url
                                    st.success("Upload successful! Shareable link generated.")
                                    st.experimental_rerun()
                                else:
                                    st.error("Upload failed: " + (err or "unknown error."))