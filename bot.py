import os
import sqlite3
import random
import re
from urllib.parse import urlparse
from aiohttp import web, ClientSession, ClientTimeout
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable
from dotenv import load_dotenv


# ==================================================
# НАЛАШТУВАННЯ
# ==================================================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у файлі .env")

# Твій Telegram ID
ALLOWED_USERS = {504686977}


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return

        if user.id not in ALLOWED_USERS:
            return

        return await handler(event, data)


bot = Bot(token=TOKEN)
dp = Dispatcher()

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


# ==================================================
# БАЗА ДАНИХ
# ==================================================

db = sqlite3.connect("guitar.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    song_key TEXT,
    lyrics TEXT NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0
)
""")

# Додаємо нові поля без втрати вже збережених пісень
cursor.execute("PRAGMA table_info(songs)")
existing_columns = {row[1] for row in cursor.fetchall()}

if "source_url" not in existing_columns:
    cursor.execute("ALTER TABLE songs ADD COLUMN source_url TEXT")

if "capo" not in existing_columns:
    cursor.execute("ALTER TABLE songs ADD COLUMN capo INTEGER NOT NULL DEFAULT 0")

db.commit()


# ==================================================
# СТАНИ ДОДАВАННЯ / ПОШУКУ
# ==================================================

ADD_STATE = {}
SEARCH_WAITING = set()
TRANSPOSE_STATE = {}


# ==================================================
# ДОПОМІЖНІ ФУНКЦІЇ
# ==================================================

async def safe_answer(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


async def edit_screen(callback: CallbackQuery, text: str, keyboard=None):
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise

    await safe_answer(callback)


def song_counts():
    cursor.execute("SELECT COUNT(*) FROM songs")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM songs WHERE favorite = 1")
    favorites = cursor.fetchone()[0]

    return total, favorites


def main_text(name=None):
    total, favorites = song_counts()

    greeting = f"Привіт, <b>{name}</b>! 👋\n\n" if name else ""

    return (
        "🎸 <b>Мій пісенник</b>\n\n"
        f"{greeting}"
        "Тут зберігаються твої пісні та акорди.\n\n"
        f"🎵 Пісень: <b>{total}</b>\n"
        f"⭐ Улюблених: <b>{favorites}</b>\n\n"
        "Обери, що хочеш зробити 👇"
    )


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 Мої пісні",
                    callback_data="songs"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Знайти пісню",
                    callback_data="search"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Додати пісню",
                    callback_data="add_song"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Улюблені",
                    callback_data="favorites"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎲 Випадкова пісня",
                    callback_data="random_song"
                )
            ],
        ]
    )


def bottom_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎵 Мої пісні"),
                KeyboardButton(text="🔎 Пошук"),
            ],
            [
                KeyboardButton(text="➕ Додати"),
                KeyboardButton(text="⭐ Улюблені"),
            ],
            [
                KeyboardButton(text="🏠 Головна"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_song(song_id):
    cursor.execute("""
        SELECT id, title, artist, song_key, lyrics, favorite,
               COALESCE(source_url, ''), COALESCE(capo, 0)
        FROM songs
        WHERE id = ?
    """, (song_id,))
    return cursor.fetchone()


CHROMATIC_SHARPS = ["C", "C#", "D", "D#", "E", "F",
                    "F#", "G", "G#", "A", "A#", "B"]

NOTE_TO_INDEX = {
    "C": 0, "B#": 0,
    "C#": 1, "Db": 1,
    "D": 2,
    "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7,
    "G#": 8, "Ab": 8,
    "A": 9,
    "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

CHORD_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яІіЇїЄєҐґ])"
    r"([A-G])([#b]?)(m|maj|min|dim|aug|sus)?"
    r"(\d{0,2})?([+#-]?\d*)?"
    r"(?:/([A-G])([#b]?))?"
    r"(?![A-Za-zА-Яа-яІіЇїЄєҐґ])"
)


def transpose_note(note, accidental, semitones):
    raw = note + (accidental or "")
    if raw not in NOTE_TO_INDEX:
        return raw
    return CHROMATIC_SHARPS[(NOTE_TO_INDEX[raw] + semitones) % 12]


def transpose_chord_match(match, semitones):
    root, accidental, quality, number, extension, bass, bass_acc = match.groups()
    result = transpose_note(root, accidental, semitones)
    result += quality or ""
    result += number or ""
    result += extension or ""
    if bass:
        result += "/" + transpose_note(bass, bass_acc, semitones)
    return result


def transpose_text(text, semitones):
    if semitones == 0:
        return text
    return CHORD_RE.sub(lambda m: transpose_chord_match(m, semitones), text)


def transpose_key(song_key, semitones):
    if not song_key:
        return ""
    return transpose_text(song_key, semitones)


def escape_html(text):
    return (
        str(text).replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
    )


def clean_song_text(text):
    text = text.replace("\r", "")
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned = []
    blank = False

    for line in lines:
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if cleaned and not blank:
                cleaned.append("")
            blank = True
            continue
        cleaned.append(line)
        blank = False

    return "\n".join(cleaned).strip()


def detect_key_from_text(text):
    # Для імпорту беремо перший знайдений акорд як орієнтовну тональність.
    match = CHORD_RE.search(text or "")
    return match.group(0) if match else ""


def split_artist_title(heading):
    heading = " ".join((heading or "").split())
    for sep in (" - ", " — ", " – "):
        if sep in heading:
            artist, title = heading.split(sep, 1)
            return artist.strip(), title.strip()
    return "Невідомий виконавець", heading.strip()


def trim_mychords_text(text):
    text = clean_song_text(text)

    # Початок самої пісні: секція або перший типовий музичний маркер.
    starts = []
    patterns = [
        r"(?im)^\[?Вступ\]?\s*:?",
        r"(?im)^\|?Вступ\|?\s*:?",
        r"(?im)^\[?Куплет\s*\d*\]?\s*:?",
        r"(?im)^\|?Куплет\s*\d*\|?\s*:?",
        r"(?im)^\[?Приспів\]?\s*:?",
        r"(?im)^\|?Приспів\|?\s*:?",
        r"(?im)^Капо(?:дастр)?\b",
        r"(?im)^акорди (?:усієї|всієї) пісні\s*:",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            starts.append(m.start())

    if starts:
        text = text[min(starts):]

    # Відсікаємо службовий хвіст MyChords.
    stops = [
        "Все ще шукаєш правильні акорди?",
        "\nРедагувати\n",
        "\nПовідомити про помилку",
        "\nВідео від користувачів",
        "\nВідео\n",
        "\nКоментарі",
    ]
    cut = len(text)
    for phrase in stops:
        pos = text.find(phrase)
        if pos >= 80:
            cut = min(cut, pos)
    return text[:cut].strip()


async def fetch_html(url):
    timeout = ClientTimeout(total=20)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GuitarBot/3.0; +https://t.me/)"
    }
    async with ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                raise ValueError(f"Сайт повернув помилку HTTP {response.status}")
            return await response.text()


async def import_from_mychords(url):
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if not h1:
        raise ValueError("Не вдалося знайти назву пісні на MyChords.")

    artist, title = split_artist_title(h1.get_text(" ", strip=True))

    # На MyChords потрібний текст зазвичай міститься в основній частині сторінки.
    # Беремо текст сторінки, а потім обрізаємо його від першої музичної секції.
    page_text = soup.get_text("\n", strip=True)
    lyrics = trim_mychords_text(page_text)

    if len(lyrics) < 80 or len(CHORD_RE.findall(lyrics)) < 2:
        raise ValueError("Не вдалося чисто витягнути акорди з MyChords.")

    return {
        "title": title,
        "artist": artist,
        "song_key": detect_key_from_text(lyrics),
        "lyrics": lyrics,
        "source_url": url.strip(),
        "source": "MyChords",
    }


async def import_from_diez(url):
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if not h1:
        raise ValueError("Не вдалося знайти назву пісні на Diez.")

    title = " ".join(h1.get_text(" ", strip=True).split())

    # У Diez виконавець зазвичай стоїть безпосередньо перед H1.
    artist = ""
    prev = h1.find_previous()
    checked = 0
    while prev and checked < 15:
        txt = " ".join(prev.get_text(" ", strip=True).split())
        if txt and txt != title and len(txt) <= 100:
            # Відсікаємо типові елементи інтерфейсу.
            low = txt.lower()
            if not any(x in low for x in (
                "акорди", "текст пісні", "транспон", "увійти",
                "реєстра", "головна", "пісні"
            )):
                artist = txt
                break
        prev = prev.find_previous()
        checked += 1

    if not artist:
        # Часто title сторінки має формат "Назва — Виконавець: ..."
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        m = re.match(r"(.+?)\s+[—–-]\s+(.+?)(?::| \|)", page_title)
        if m:
            title = m.group(1).strip()
            artist = m.group(2).strip()

    if not artist:
        artist = "Невідомий виконавець"

    # Вибираємо контейнер, у якому є H1 і найбільше акордів.
    candidates = []
    node = h1
    for _ in range(8):
        node = node.parent
        if not node:
            break
        txt = node.get_text("\n", strip=True)
        chord_count = len(CHORD_RE.findall(txt))
        if 80 <= len(txt) <= 25000 and chord_count >= 2:
            score = chord_count * 50 - len(txt) / 100
            if re.search(r"(?i)\b(Вступ|Куплет|Приспів|Кода)\b", txt):
                score += 500
            candidates.append((score, txt))

    if not candidates:
        raise ValueError("Не вдалося знайти текст з акордами на Diez.")

    candidates.sort(key=lambda x: x[0], reverse=True)
    lyrics = clean_song_text(candidates[0][1])

    # Прибираємо заголовок/виконавця з початку контейнера.
    lines = lyrics.splitlines()
    while lines and lines[0].strip() in {artist, title}:
        lines.pop(0)
    lyrics = "\n".join(lines).strip()

    # Обрізаємо SEO-пояснення після пісні.
    for phrase in (
        "Це повний текст пісні",
        "Схожі пісні",
        "Інші пісні",
        "Коментарі",
    ):
        pos = lyrics.find(phrase)
        if pos >= 80:
            lyrics = lyrics[:pos].strip()

    if len(lyrics) < 80:
        raise ValueError("Знайдений текст Diez виглядає неповним.")

    return {
        "title": title,
        "artist": artist,
        "song_key": detect_key_from_text(lyrics),
        "lyrics": lyrics,
        "source_url": url.strip(),
        "source": "Diez",
    }


async def import_from_telegram(url):
    parsed = urlparse(url.strip())
    parts = [p for p in parsed.path.split("/") if p]

    if len(parts) < 2 or parts[0].lower() != "easy_chords" or not parts[1].isdigit():
        raise ValueError(
            "Для Telegram надішли посилання саме на конкретний допис, "
            "наприклад t.me/easy_chords/123."
        )

    post_url = f"https://t.me/easy_chords/{parts[1]}?embed=1&mode=tme"
    html = await fetch_html(post_url)
    soup = BeautifulSoup(html, "html.parser")

    text_node = soup.select_one(".tgme_widget_message_text")
    if not text_node:
        raise ValueError(
            "У цьому дописі не знайдено тексту. "
            "Якщо акорди тільки на фото/відео, v3 поки не може їх прочитати."
        )

    post_text = clean_song_text(text_node.get_text("\n", strip=True))
    if len(post_text) < 20:
        raise ValueError("Текст допису занадто короткий для імпорту.")

    # Спроба визначити назву/виконавця з перших змістовних рядків.
    lines = [x.strip() for x in post_text.splitlines() if x.strip()]
    artist = "easy_chords"
    title = lines[0][:120] if lines else f"Допиc {parts[1]}"

    # Якщо перший рядок схожий на "Виконавець — Назва".
    a, t = split_artist_title(title)
    if a != "Невідомий виконавець":
        artist, title = a, t

    return {
        "title": title,
        "artist": artist,
        "song_key": detect_key_from_text(post_text),
        "lyrics": post_text,
        "source_url": url.strip(),
        "source": "Telegram easy_chords",
    }


async def import_song_from_url(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    if host == "mychords.net":
        return await import_from_mychords(url)

    if host == "diez.net.ua":
        return await import_from_diez(url)

    if host in {"t.me", "telegram.me"}:
        return await import_from_telegram(url)

    raise ValueError(
        "Цей сайт поки не підтримується.\n"
        "Підтримуються: MyChords, Diez та дописи t.me/easy_chords."
    )


def song_card(song, semitones=0):
    song_id, title, artist, song_key, lyrics, favorite, source_url, capo = song

    star = "⭐" if favorite else "☆"
    shown_key = transpose_key(song_key, semitones) if song_key else "не вказана"
    shown_lyrics = transpose_text(lyrics, semitones)

    transpose_label = "Оригінал" if semitones == 0 else f"{semitones:+d}"

    header = (
        f"🎵 <b>{escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}\n"
        f"🎸 Тональність: <b>{escape_html(shown_key)}</b>\n"
        f"🎼 Транспонування: <b>{transpose_label}</b>\n"
        f"📎 Капо: <b>{capo}</b>\n"
        f"{star} {'Улюблена' if favorite else 'Не в улюблених'}"
    )

    # Telegram має ліміт довжини повідомлення. Для картки залишаємо запас.
    body = escape_html(shown_lyrics)
    max_body = 3300
    if len(body) > max_body:
        body = body[:max_body] + "\n\n…текст скорочено"

    text = header + "\n\n<pre>" + body + "</pre>"

    keyboard_rows = [
        [
            InlineKeyboardButton(text="⬇️ −1", callback_data=f"tr_-_{song_id}"),
            InlineKeyboardButton(text=f"🎵 {transpose_label}", callback_data=f"tr_0_{song_id}"),
            InlineKeyboardButton(text="⬆️ +1", callback_data=f"tr_+_{song_id}"),
        ],
        [
            InlineKeyboardButton(
                text="⭐ Прибрати" if favorite else "⭐ В улюблені",
                callback_data=f"favorite_{song_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Видалити",
                callback_data=f"delete_request_{song_id}"
            )
        ],
        [
            InlineKeyboardButton(text="🎵 До пісень", callback_data="songs"),
            InlineKeyboardButton(text="🏠 Головна", callback_data="home"),
        ],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def songs_view(only_favorites=False):
    if only_favorites:
        cursor.execute("""
            SELECT id, title, artist
            FROM songs
            WHERE favorite = 1
            ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE
        """)
        title = "⭐ <b>Улюблені пісні</b>"
    else:
        cursor.execute("""
            SELECT id, title, artist
            FROM songs
            ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE
        """)
        title = "🎵 <b>Мої пісні</b>"

    songs = cursor.fetchall()

    if not songs:
        text = (
            f"{title}\n\n"
            + ("Тут поки немає улюблених пісень 🙂"
               if only_favorites
               else "Пісенник поки порожній.\n\nНатисни «➕ Додати пісню» 👇")
        )
    else:
        text = f"{title}\n\nЗнайдено: <b>{len(songs)}</b>\n\nОбери пісню 👇"

    keyboard = []

    for song_id, song_title, artist in songs:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🎸 {artist} — {song_title}",
                callback_data=f"song_{song_id}"
            )
        ])

    if not only_favorites:
        keyboard.append([
            InlineKeyboardButton(
                text="➕ Додати пісню",
                callback_data="add_song"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="🏠 Головна",
            callback_data="home"
        )
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def search_results(query):
    pattern = f"%{query}%"

    cursor.execute("""
        SELECT id, title, artist
        FROM songs
        WHERE title LIKE ? COLLATE NOCASE
           OR artist LIKE ? COLLATE NOCASE
        ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE
    """, (pattern, pattern))

    return cursor.fetchall()


# ==================================================
# /START ТА ГОЛОВНА
# ==================================================

@dp.message(CommandStart())
async def start(message: Message):
    name = message.from_user.first_name or "друже"

    await message.answer(
        main_text(name),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

    await message.answer(
        "🎸 Меню GuitarBot увімкнено",
        reply_markup=bottom_keyboard()
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(
        f"👤 {message.from_user.first_name}\n"
        f"🆔 Telegram ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    ADD_STATE.pop(callback.from_user.id, None)
    SEARCH_WAITING.discard(callback.from_user.id)

    await edit_screen(
        callback,
        main_text(),
        main_keyboard()
    )


@dp.message(F.text == "🏠 Головна")
async def bottom_home(message: Message):
    ADD_STATE.pop(message.from_user.id, None)
    SEARCH_WAITING.discard(message.from_user.id)

    await message.answer(
        main_text(),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ==================================================
# СПИСОК ПІСЕНЬ
# ==================================================

@dp.callback_query(F.data == "songs")
async def show_songs(callback: CallbackQuery):
    text, keyboard = songs_view()
    await edit_screen(callback, text, keyboard)


@dp.message(F.text == "🎵 Мої пісні")
async def bottom_songs(message: Message):
    text, keyboard = songs_view()

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("song_"))
async def open_song(callback: CallbackQuery):
    song_id = int(callback.data.replace("song_", "", 1))
    song = get_song(song_id)

    if not song:
        await callback.answer(
            "Пісню вже видалено.",
            show_alert=True
        )
        return

    TRANSPOSE_STATE[song_id] = 0
    text, keyboard = song_card(song, 0)
    await edit_screen(callback, text, keyboard)


# ==================================================
# ТРАНСПОНУВАННЯ
# ==================================================

@dp.callback_query(F.data.startswith("tr_"))
async def transpose_song(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 3:
        await safe_answer(callback)
        return

    action, song_id_text = parts[1], parts[2]
    song_id = int(song_id_text)
    song = get_song(song_id)

    if not song:
        await callback.answer("Пісню вже видалено.", show_alert=True)
        return

    current = TRANSPOSE_STATE.get(song_id, 0)

    if action == "+":
        current += 1
    elif action == "-":
        current -= 1
    else:
        current = 0

    # Тримаємо значення у зрозумілому діапазоні.
    if current > 11:
        current = 0
    if current < -11:
        current = 0

    TRANSPOSE_STATE[song_id] = current
    text, keyboard = song_card(song, current)
    await edit_screen(callback, text, keyboard)


# ==================================================
# ДОДАВАННЯ ПІСНІ
# ==================================================

async def begin_add_song(user_id, send_func):
    ADD_STATE.pop(user_id, None)
    SEARCH_WAITING.discard(user_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 За посиланням",
                    callback_data="add_by_url"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Вручну",
                    callback_data="add_manual"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Скасувати",
                    callback_data="cancel_add"
                )
            ]
        ]
    )

    await send_func(
        "➕ <b>Додати пісню</b>\n\n"
        "Як хочеш додати пісню?",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "add_song")
async def add_song_start(callback: CallbackQuery):
    await begin_add_song(callback.from_user.id, callback.message.answer)
    await safe_answer(callback)


@dp.message(F.text == "➕ Додати")
async def bottom_add(message: Message):
    await begin_add_song(message.from_user.id, message.answer)


@dp.callback_query(F.data == "add_by_url")
async def add_by_url(callback: CallbackQuery):
    ADD_STATE[callback.from_user.id] = {
        "step": "url",
        "data": {}
    }

    await edit_screen(
        callback,
        "🔗 <b>Імпорт за посиланням</b>\n\n"
        "Надішли посилання на пісню.\n\n"
        "Підтримую:\n"
        "• <b>Diez</b>\n"
        "• <b>MyChords</b>\n"
        "• конкретні дописи <b>t.me/easy_chords/...</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_add")
            ]]
        )
    )


@dp.callback_query(F.data == "add_manual")
async def add_manual(callback: CallbackQuery):
    ADD_STATE[callback.from_user.id] = {
        "step": "title",
        "data": {}
    }

    await edit_screen(
        callback,
        "📝 <b>Додавання вручну</b>\n\n"
        "1/4. Напиши <b>назву пісні</b>:",
        InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_add")
            ]]
        )
    )


@dp.callback_query(F.data == "save_import")
async def save_import(callback: CallbackQuery):
    state = ADD_STATE.get(callback.from_user.id)

    if not state or state.get("step") != "confirm_import":
        await callback.answer("Імпорт уже завершено або скасовано.", show_alert=True)
        return

    data = state["data"]

    cursor.execute("""
        INSERT INTO songs
        (title, artist, song_key, lyrics, source_url)
        VALUES (?, ?, ?, ?, ?)
    """, (
        data["title"],
        data["artist"],
        data.get("song_key", ""),
        data["lyrics"],
        data.get("source_url", "")
    ))

    song_id = cursor.lastrowid
    db.commit()
    ADD_STATE.pop(callback.from_user.id, None)

    await callback.answer("✅ Пісню збережено")
    song = get_song(song_id)
    text, keyboard = song_card(song, 0)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "cancel_add")
async def cancel_add(callback: CallbackQuery):
    ADD_STATE.pop(callback.from_user.id, None)

    await edit_screen(
        callback,
        "❌ <b>Додавання скасовано</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Головна", callback_data="home")
            ]]
        )
    )


# ==================================================
# ПОШУК
# ==================================================

async def begin_search(user_id, send_func):
    SEARCH_WAITING.add(user_id)
    ADD_STATE.pop(user_id, None)

    await send_func(
        "🔎 <b>Пошук пісні</b>\n\n"
        "Напиши назву пісні або виконавця:",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "search")
async def search_start(callback: CallbackQuery):
    await begin_search(callback.from_user.id, callback.message.answer)
    await safe_answer(callback)


@dp.message(F.text == "🔎 Пошук")
async def bottom_search(message: Message):
    await begin_search(message.from_user.id, message.answer)


# ==================================================
# УЛЮБЛЕНІ
# ==================================================

@dp.callback_query(F.data == "favorites")
async def favorites(callback: CallbackQuery):
    text, keyboard = songs_view(only_favorites=True)
    await edit_screen(callback, text, keyboard)


@dp.message(F.text == "⭐ Улюблені")
async def bottom_favorites(message: Message):
    text, keyboard = songs_view(only_favorites=True)

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("favorite_"))
async def toggle_favorite(callback: CallbackQuery):
    song_id = int(callback.data.replace("favorite_", "", 1))
    song = get_song(song_id)

    if not song:
        await callback.answer(
            "Пісню вже видалено.",
            show_alert=True
        )
        return

    new_value = 0 if song[5] else 1

    cursor.execute(
        "UPDATE songs SET favorite = ? WHERE id = ?",
        (new_value, song_id)
    )
    db.commit()

    updated_song = get_song(song_id)
    semitones = TRANSPOSE_STATE.get(song_id, 0)
    text, keyboard = song_card(updated_song, semitones)

    await edit_screen(callback, text, keyboard)


# ==================================================
# ВИПАДКОВА ПІСНЯ
# ==================================================

@dp.callback_query(F.data == "random_song")
async def random_song(callback: CallbackQuery):
    cursor.execute("SELECT id FROM songs")
    ids = [row[0] for row in cursor.fetchall()]

    if not ids:
        await callback.answer(
            "Пісенник поки порожній 🙂",
            show_alert=True
        )
        return

    song_id = random.choice(ids)
    song = get_song(song_id)

    TRANSPOSE_STATE[song_id] = 0
    text, keyboard = song_card(song, 0)
    await edit_screen(callback, text, keyboard)


# ==================================================
# ВИДАЛЕННЯ
# ==================================================

@dp.callback_query(F.data.startswith("delete_request_"))
async def delete_request(callback: CallbackQuery):
    song_id = int(callback.data.replace("delete_request_", "", 1))
    song = get_song(song_id)

    if not song:
        await callback.answer(
            "Пісню вже видалено.",
            show_alert=True
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Так, видалити",
                    callback_data=f"delete_confirm_{song_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Скасувати",
                    callback_data=f"song_{song_id}"
                )
            ],
        ]
    )

    await edit_screen(
        callback,
        f"⚠️ <b>Видалити пісню?</b>\n\n"
        f"🎵 {song[1]}\n"
        f"👤 {song[2]}",
        keyboard
    )


@dp.callback_query(F.data.startswith("delete_confirm_"))
async def delete_confirm(callback: CallbackQuery):
    song_id = int(callback.data.replace("delete_confirm_", "", 1))

    cursor.execute(
        "DELETE FROM songs WHERE id = ?",
        (song_id,)
    )
    db.commit()

    text, keyboard = songs_view()

    try:
        await callback.answer("🗑 Пісню видалено")
    except TelegramBadRequest:
        pass

    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except TelegramBadRequest:
        pass


# ==================================================
# ОБРОБКА ТЕКСТУ:
# ДОДАВАННЯ / ПОШУК
# ==================================================

@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    # ---------- Додавання пісні ----------
    state = ADD_STATE.get(user_id)

    if state:
        step = state["step"]

        if step == "url":
            if not text.lower().startswith(("http://", "https://")):
                await message.answer(
                    "⚠️ Надішли повне посилання, яке починається з <b>https://</b>.",
                    parse_mode="HTML"
                )
                return

            wait_msg = await message.answer("⏳ Завантажую пісню…")

            try:
                imported = await import_song_from_url(text)
            except Exception as error:
                await wait_msg.edit_text(
                    "❌ <b>Не вдалося імпортувати пісню.</b>\n\n"
                    f"{escape_html(str(error))}\n\n"
                    "Можеш надіслати посилання ще раз або скасувати додавання.",
                    parse_mode="HTML"
                )
                return

            state["step"] = "confirm_import"
            state["data"] = imported

            preview = imported["lyrics"][:900]
            if len(imported["lyrics"]) > 900:
                preview += "\n…"

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Зберегти",
                            callback_data="save_import"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Скасувати",
                            callback_data="cancel_add"
                        )
                    ]
                ]
            )

            await wait_msg.edit_text(
                "🔎 <b>Перевір імпорт</b>\n\n"
                f"🌐 Джерело: <b>{escape_html(imported.get('source', 'Посилання'))}</b>\n"
                f"🎵 <b>{escape_html(imported['title'])}</b>\n"
                f"👤 {escape_html(imported['artist'])}\n"
                f"🎸 Тональність: <b>{escape_html(imported['song_key'] or 'не визначена')}</b>\n\n"
                f"<pre>{escape_html(preview)}</pre>\n\n"
                "Якщо все виглядає правильно — натисни «✅ Зберегти».",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        if step == "confirm_import":
            await message.answer(
                "Спочатку натисни <b>«✅ Зберегти»</b> або <b>«❌ Скасувати»</b> під попереднім переглядом.",
                parse_mode="HTML"
            )
            return

        if step == "title":
            state["data"]["title"] = text
            state["step"] = "artist"

            await message.answer(
                "2/4. Тепер напиши <b>виконавця</b>:",
                parse_mode="HTML"
            )
            return

        if step == "artist":
            state["data"]["artist"] = text
            state["step"] = "key"

            await message.answer(
                "3/4. Напиши <b>тональність</b>.\n\n"
                "Наприклад: <code>Am</code>, <code>Em</code>, <code>G</code>\n\n"
                "Якщо не знаєш — напиши <code>-</code>",
                parse_mode="HTML"
            )
            return

        if step == "key":
            state["data"]["song_key"] = "" if text == "-" else text
            state["step"] = "lyrics"

            await message.answer(
                "4/4. Надішли <b>текст пісні разом з акордами</b>.\n\n"
                "Наприклад:\n"
                "<pre>Am        F\n"
                "Перший рядок пісні\n"
                "C         G\n"
                "Другий рядок пісні</pre>",
                parse_mode="HTML"
            )
            return

        if step == "lyrics":
            data = state["data"]

            cursor.execute("""
                INSERT INTO songs
                (title, artist, song_key, lyrics)
                VALUES (?, ?, ?, ?)
            """, (
                data["title"],
                data["artist"],
                data["song_key"],
                text
            ))

            song_id = cursor.lastrowid
            db.commit()

            ADD_STATE.pop(user_id, None)

            song = get_song(song_id)
            card_text, keyboard = song_card(song)

            await message.answer(
                "✅ <b>Пісню збережено!</b>",
                parse_mode="HTML"
            )

            await message.answer(
                card_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

    # ---------- Пошук ----------
    if user_id in SEARCH_WAITING:
        SEARCH_WAITING.discard(user_id)

        results = search_results(text)

        if not results:
            await message.answer(
                f"🔎 За запитом <b>{escape_html(text)}</b> нічого не знайдено.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔎 Шукати ще",
                                callback_data="search"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 Головна",
                                callback_data="home"
                            )
                        ],
                    ]
                )
            )
            return

        keyboard = []

        for song_id, title, artist in results:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🎸 {artist} — {title}",
                    callback_data=f"song_{song_id}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                text="🔎 Шукати ще",
                callback_data="search"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                text="🏠 Головна",
                callback_data="home"
            )
        ])

        await message.answer(
            f"🔎 <b>Результати пошуку</b>\n\n"
            f"Знайдено: <b>{len(results)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        return


# ==================================================
# ЗАПУСК НА RENDER
# ==================================================

WEBHOOK_PATH = "/webhook"


async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL + WEBHOOK_PATH)


async def on_shutdown(bot: Bot):
    try:
        cursor.close()
        db.close()
    except Exception:
        pass


def main():
    app = web.Application()

    async def health(request):
        return web.Response(text="GuitarBot is running 🎸")

    app.router.add_get("/", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    port = int(os.getenv("PORT", "10000"))

    web.run_app(
        app,
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    main()
