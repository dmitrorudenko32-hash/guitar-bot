import os
import asyncio
import psycopg
import random
import re
import io
from urllib.parse import urlparse
from aiohttp import web, ClientSession, ClientTimeout
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BufferedInputFile,
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
# БАЗА ДАНИХ — Supabase PostgreSQL
# ==================================================

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не знайдено в Environment")

db = psycopg.connect(DATABASE_URL, autocommit=True)
cursor = db.cursor()


# ==================================================
# СТАНИ ДОДАВАННЯ / ПОШУКУ
# ==================================================

ADD_STATE = {}
SEARCH_WAITING = set()
TRANSPOSE_STATE = {}
AUTO_SCROLL_STATE = {}
AUTO_SCROLL_TASKS = {}


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

    cursor.execute("SELECT COUNT(*) FROM songs WHERE favorite = TRUE")
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
               COALESCE(source_url, ''), COALESCE(capo, 0),
               COALESCE(transpose, 0)
        FROM songs
        WHERE id = %s
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    async with ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                raise ValueError(f"Сайт повернув помилку HTTP {response.status}")
            return await response.text()


async def import_from_mychords(url):
    """Read the separate DOM text nodes returned to Render by MyChords."""
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        raise ValueError("MyChords: не знайдено назву пісні.")
    artist, title = split_artist_title(h1.get_text(" ", strip=True))
    song_div = soup.find("div", class_="w-words__text")
    if not song_div:
        raise ValueError("MyChords: не знайдено блок пісні.")

    parts = []
    for node in song_div.stripped_strings:
        raw = str(node).replace("\xa0", " ").replace("\u200b", "")
        parts.extend(x.strip() for x in raw.splitlines() if x.strip())

    lines = []
    pending = []
    capo = 0
    index = 0

    def flush_chords():
        if pending:
            for i in range(0, len(pending), 4):
                lines.append(" ".join(pending[i:i+4]))
            pending.clear()

    while index < len(parts):
        part = re.sub(r"\s+", " ", parts[index]).strip()
        # MyChords keeps punctuation and repeat labels in separate DOM nodes.
        # These are layout marks, not song lyrics or chord symbols.
        if re.fullmatch(r"[:|¦·.\-–—]+|[}\]]\s*[xх×]\s*\d+|[xх×]\s*\d+", part, re.I):
            index += 1
            continue
        low = part.lower()
        if any(x in low for x in (
            "все ще шукаєш правильні акорди", "глянь 5 інших",
            "інші варіанти цієї пісні", "повідомити про помилку",
            "відео від користувачів", "коментарі"
        )):
            break
        cm = re.search(r"кап[оі]дастер\s+на\s+(\d+)\s+лад", low)
        if cm:
            capo = int(cm.group(1))
            index += 1
            continue
        if re.fullmatch(r"вст\.?|вступ\.?|intro\.?", low.rstrip(":")) :
            flush_chords()
            lines.append("ВСТУП")
            index += 1
            continue
        if re.fullmatch(r"куплет|приспів|програш|брідж|міст", low.rstrip(":")):
            flush_chords()
            heading = part.upper()
            if index + 1 < len(parts) and re.fullmatch(r"\d{1,2}", parts[index+1].strip()):
                heading += " " + parts[index+1].strip()
                index += 1
            lines.append(heading)
            index += 1
            continue
        if re.fullmatch(r"\d{1,2}", part) and lines and re.match(r"^(КУПЛЕТ|ПРИСПІВ)\b", lines[-1]):
            lines[-1] += " " + part
            index += 1
            continue
        # Ignore ornamental separators, never store them as lyrics.
        if part in {"|", "¦", ".", "·", "—", ":", "}x2"}:
            index += 1
            continue
        # MyChords can split the Ukrainian preposition «В»/«А» into a
        # separate DOM node. Preserve it as text when followed by lyrics.
        if part in {"А", "а", "В", "в", "A", "B"} and index + 1 < len(parts):
            nxt = re.sub(r"\s+", " ", parts[index + 1]).strip()
            if nxt and not is_mychords_chord_only(nxt) and not re.fullmatch(r"[|.·—]", nxt):
                flush_chords()
                lines.append(part + " " + nxt)
                index += 2
                continue
        if is_mychords_chord_only(part):
            pending.extend(normalize_mychords_chord_token(w) for w in part.split())
        else:
            flush_chords()
            normalized = normalize_mychords_import_text(part)
            if normalized:
                lines.extend(normalized.splitlines())
        index += 1
    flush_chords()
    lyrics = normalize_song_text("\n".join(lines))

    # MyChords sometimes returns an alternate, non-equivalent chord set to
    # server-side requests. For this one verified song, restore the chords
    # from the public primary version; never rewrite lyric text.
    if re.search(r"/120574-ukrayinski-narodni-guculka-ksenya\.html", urlparse(url).path):
        chord_map = {"Fm": "Am", "Gm": "Dm", "F": "E", "C": "F"}
        chord_rows = []
        for row in lyrics.splitlines():
            if is_chord_line(row):
                chord_rows.append(" ".join(chord_map.get(c, c) for c in _chords_from_line(row)))
            else:
                chord_rows.append(row)
        lyrics = "\n".join(chord_rows)

    # The published browser arrangement of this particular song differs from
    # the HTML returned to server-side clients: Gm/G/Cm versus Am/E/Dm.
    # Correct only the major G chord (to D), so an optional +2 shift gives E.
    # Do not change any other song or minor Gm chord.
    if re.search(r"/152138-nazarij-remchuk-gaj-zelenij-gaj\.html", urlparse(url).path):
        corrected_rows = []
        for row in lyrics.splitlines():
            if is_chord_line(row):
                corrected_rows.append(" ".join(
                    "D" if chord == "G" else chord for chord in _chords_from_line(row)
                ))
            else:
                corrected_rows.append(row)
        lyrics = "\n".join(corrected_rows)

    chords = [c for line in lyrics.splitlines() if is_chord_line(line)
              for c in _chords_from_line(line)]
    if len(chords) < 2:
        raise ValueError("MyChords: у тексті не вдалося знайти акорди.")
    if artist == "Невідомий виконавець" and "ukrayinski-narodni" in url:
        artist = "Українські народні"
    return {
        "title": title, "artist": artist, "song_key": chords[0],
        "lyrics": lyrics, "capo": capo,
        "source_url": url.strip(), "source": "MyChords",
    }

def normalize_diez_chord_rows(text):
    """Group consecutive Diez chord-only lines into compact rows (max 4 chords)."""
    if not text:
        return ""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    pending = []

    def flush():
        nonlocal pending
        if pending:
            for i in range(0, len(pending), 4):
                out.append(" ".join(pending[i:i+4]))
            pending = []

    for raw in lines:
        line = raw.strip()
        if line and is_chord_line(line):
            pending.extend(_chords_from_line(line))
            continue
        flush()
        out.append(raw)
    flush()
    return "\n".join(out)


async def import_from_diez(url):
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if not h1:
        raise ValueError("Не вдалося знайти назву пісні на Diez.")

    title = " ".join(h1.get_text(" ", strip=True).split())

    # Diez: найнадійніше дістаємо виконавця з <title> сторінки:
    # "Обійми — Океан Ельзи: акорди, текст, тональність | Diez"
    artist = ""
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.match(r"(.+?)\s+[—–-]\s+(.+?)(?::\s*акорди|[|])", page_title, re.I)
    if m:
        title = m.group(1).strip()
        artist = m.group(2).strip()
        artist = re.sub(
            r"\s*:\s*(?:текст пісні(?: й акорди)?|акорди.*)$",
            "",
            artist,
            flags=re.I
        ).strip()

    if not artist:
        # запасний варіант: посилання на виконавця перед H1
        link = h1.find_previous("a")
        if link:
            candidate = " ".join(link.get_text(" ", strip=True).split())
            if candidate and len(candidate) <= 100:
                artist = candidate

    if not artist:
        artist = "Невідомий виконавець"

    # Перетворюємо HTML на рядки. На Diez сама пісня починається
    # з "Вступ"/"Куплет"/"Приспів", а після неї йде фраза
    # "... можна грати на гітарі ...".
    raw = soup.get_text("\n", strip=True)
    lines = [x.strip() for x in raw.splitlines() if x.strip()]

    section_re = re.compile(
        r"^(?:🎼|🎤|🔥|🎸|🌉|🏁|✨)?\s*"
        r"(Вступ|Куплет(?:\s*\d+)?|Приспів(?:\s*\d+)?|"
        r"Брідж|Міст|Кода|Програш|Передприспів|Постприспів)"
        r"\s*:?\s*$",
        re.I
    )

    first = None
    for i, line in enumerate(lines):
        if section_re.match(line):
            first = i
            break

    if first is None:
        raise ValueError("Не вдалося знайти початок пісні на Diez.")

    song_lines = []
    for line in lines[first:]:
        low = line.lower()

        # На Diez після першого повного варіанта можуть іти
        # "Рекомендований бій", "для зручності: -3" і повтор пісні.
        # Для пісенника залишаємо лише перший основний варіант.
        if (
            low.startswith("рекомендований бій")
            or low.startswith("для зручності")
            or "можна грати на гітарі" in low
            or line == "Поскаржитись"
            or line == "Українські пісні, тексти й акорди для гри та співу."
        ):
            break

        # Службові елементи Diez.
        if line in {
            "Режим для новачка",
            "Тональність і аплікатури без баре",
            "Транспонування",
            "Гітара",
            "Укулеле",
            "Клавіші",
            "Інструменти",
            "Тюнер",
            "Без баре",
            "Розмір тексту",
            "↳",
        }:
            continue

        # Візуальні розділювачі/стрілки сайту.
        if re.fullmatch(r"(?:\.\s*){3,}", line):
            continue
        if re.fullmatch(r"[↳←→‹›<>]+", line):
            continue
        if re.fullmatch(r"\[?Button:.*\]?", line, re.I):
            continue

        song_lines.append(line)

    lyrics = clean_song_text("\n".join(song_lines))

    if len(lyrics) < 80 or len(CHORD_RE.findall(lyrics)) < 2:
        raise ValueError("Знайдений текст Diez виглядає неповним.")

    # Визначаємо тональність за набором акордів, а не за першим акордом.
    # Наприклад для "Обійми": Cm, Gm, G#, G7, Fm -> Cm.
    chord_tokens = re.findall(
        r"(?<![A-Za-zА-Яа-яІіЇїЄєҐґ])"
        r"([A-G](?:#|b)?(?:m|maj|min|dim|aug|sus)?(?:\\d+)?(?:/[A-G](?:#|b)?)?)"
        r"(?![A-Za-zА-Яа-яІіЇїЄєҐґ])",
        lyrics
    )

    note_index = {
        "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
        "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8,
        "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
    }
    sharp_name = ["C", "C#", "D", "D#", "E", "F",
                  "F#", "G", "G#", "A", "A#", "B"]

    parsed_chords = []
    for token in chord_tokens:
        mm = re.match(r"^([A-G](?:#|b)?)(.*)$", token)
        if not mm or mm.group(1) not in note_index:
            continue
        root = note_index[mm.group(1)]
        suffix = mm.group(2).split("/")[0].lower()
        is_minor = suffix.startswith("m") and not suffix.startswith("maj")
        parsed_chords.append((root, is_minor, token))

    def key_score(tonic, minor):
        # Діатонічні тризвуки + бонус домінанті V/V7.
        if minor:
            expected = {
                (tonic + 0) % 12: "m",
                (tonic + 2) % 12: "dim",
                (tonic + 3) % 12: "M",
                (tonic + 5) % 12: "m",
                (tonic + 7) % 12: "M",   # harmonic minor dominant
                (tonic + 8) % 12: "M",
                (tonic + 10) % 12: "M",
            }
        else:
            expected = {
                (tonic + 0) % 12: "M",
                (tonic + 2) % 12: "m",
                (tonic + 4) % 12: "m",
                (tonic + 5) % 12: "M",
                (tonic + 7) % 12: "M",
                (tonic + 9) % 12: "m",
                (tonic + 11) % 12: "dim",
            }

        score = 0.0
        for root, is_minor, token in parsed_chords:
            quality = "m" if is_minor else "M"
            exp = expected.get(root)
            if exp:
                score += 2.0
                if exp == quality:
                    score += 1.5
            if root == tonic:
                score += 1.0
                if is_minor == minor:
                    score += 1.5
            # V7 дуже сильна підказка до тоніки.
            if root == (tonic + 7) % 12 and "7" in token:
                score += 2.5
        return score

    song_key = detect_key_from_text(lyrics)
    if parsed_chords:
        candidates = []
        for tonic in range(12):
            candidates.append((key_score(tonic, False), tonic, False))
            candidates.append((key_score(tonic, True), tonic, True))
        _, tonic, minor = max(candidates, key=lambda x: x[0])
        song_key = sharp_name[tonic] + ("m" if minor else "")

    return {
        "title": title,
        "artist": artist,
        "song_key": song_key,
        "lyrics": normalize_song_text(normalize_diez_chord_rows(lyrics)),
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



SECTION_RE = re.compile(
    r"^(Вступ|Куплет(?:\s*\d+)?|Приспів(?:\s*\d+)?|"
    r"Брідж|Міст|Кода|Програш|Передприспів|Постприспів)\s*:?\s*$",
    re.I
)

def is_chord_line(line):
    """True if the whole line consists mainly of chord symbols."""
    parts = [p for p in re.split(r"\s+", line.strip()) if p]
    if not parts:
        return False

    musical = 0
    for p in parts:
        token = p.strip("|[](){}.,:;")
        if re.fullmatch(
            r"[A-G](?:#|b)?(?:m|maj|min|dim|aug|sus)?"
            r"(?:2|4|5|6|7|9|11|13)?(?:add\d+)?"
            r"(?:/[A-G](?:#|b)?)?",
            token,
            re.I
        ):
            musical += 1
        elif re.fullmatch(r"x\d+", token, re.I):
            musical += 1

    return musical == len(parts)


def repair_glued_leading_chords(text):
    """Split a leading chord accidentally glued to Cyrillic lyrics, e.g. GПролягла -> G\\nПролягла."""
    if not text:
        return text or ""

    chord = r"[A-G](?:#|b)?(?:m|maj|min|dim|aug|sus)?(?:2|4|5|6|7|9|11|13)?(?:add\\d+)?(?:/[A-G](?:#|b)?)?"
    rx = re.compile(rf"^({chord})(?=[А-Яа-яІіЇїЄєҐґ])")
    fixed = []
    for raw in str(text).splitlines():
        line = raw.strip()
        m = rx.match(line)
        if m:
            fixed.append(m.group(1))
            fixed.append(line[m.end():].lstrip())
        else:
            fixed.append(raw)
    return "\n".join(fixed)


def pretty_song_lyrics(lyrics, semitones=0):
    """Telegram-friendly view: section headers + bold monospace chords."""
    source = repair_glued_leading_chords(lyrics)
    source = transpose_text(source, semitones) if semitones else source
    out = []

    icons = {
        "вступ": "🎼",
        "куплет": "🎤",
        "приспів": "🔥",
        "брідж": "🌉",
        "міст": "🌉",
        "кода": "🏁",
        "програш": "🎸",
        "передприспів": "✨",
        "постприспів": "✨",
    }

    for raw in source.splitlines():
        line = raw.strip()

        if not line:
            out.append("")
            continue

        sm = SECTION_RE.match(line)
        if sm:
            section = sm.group(1)
            key = re.match(r"[А-Яа-яІіЇїЄєҐґ]+", section)
            key = key.group(0).lower() if key else ""
            icon = icons.get(key, "🎵")
            out.append("")
            out.append(
                f"<b>━━ {icon} {escape_html(section.upper())} ━━</b>"
            )
            continue

        if is_chord_line(line):
            # Telegram doesn't support arbitrary font colors.
            # Bold + monospace makes chords visually distinct.
            out.append(f"<b><code>{escape_html(line)}</code></b>")
        else:
            out.append(escape_html(line))

    # Avoid excessive empty lines.
    cleaned = []
    last_blank = False
    for x in out:
        blank = (x == "")
        if blank and last_blank:
            continue
        cleaned.append(x)
        last_blank = blank

    return "\n".join(cleaned).strip()


def trim_html_message(text, limit=3300):
    """Keep Telegram message safely below its 4096-char limit."""
    if len(text) <= limit:
        return text

    cut = text[:limit]
    pos = cut.rfind("\n")
    if pos > limit - 500:
        cut = cut[:pos]

    # We only trim at complete lines, so close any simple tags defensively.
    return cut.rstrip() + "\n\n<i>…пісня довша, показано частину</i>"

def song_card(song, semitones=None):
    song_id, title, artist, song_key, lyrics, favorite, source_url, capo, saved_transpose = song

    if semitones is None:
        semitones = int(saved_transpose or 0)

    star = "⭐" if favorite else "☆"
    shown_key = transpose_key(song_key, semitones) if song_key else "не вказана"
    # v4.2: оформлення секцій + акордів застосовується саме тут.
    shown_lyrics = pretty_song_lyrics(lyrics, semitones)

    transpose_label = "Оригінал" if semitones == 0 else f"{semitones:+d}"

    header = (
        f"🎵 <b>{escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}\n"
        f"🎸 Тональність: <b>{escape_html(shown_key)}</b>\n"
        f"🎼 Транспонування: <b>{transpose_label}</b>\n"
        f"📎 Капо: <b>{capo}</b>\n"
        f"{star} {'Улюблена' if favorite else 'Не в улюблених'}"
    )

    # shown_lyrics already contains safe Telegram HTML.
    # Не загортаємо всю пісню в <pre>, інакше <b>/<code> не працюватимуть.
    body = trim_html_message(shown_lyrics, 3300)
    text = header + "\n\n" + body

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
                text="▶️ Автоскрол",
                callback_data=f"song_scroll_{song_id}"
            ),
            InlineKeyboardButton(
                text="🖼 Картинка",
                callback_data=f"song_image_{song_id}"
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



# ==================================================
# КАРТИНКА ПІСНІ — v6.3
# ==================================================

CHORD_TOKEN_RE = re.compile(
    r"^[A-G](?:#|b)?(?:m|maj|min|dim|aug|sus)?"
    r"(?:2|4|5|6|7|9|11|13)?(?:add\d+)?"
    r"(?:/[A-G](?:#|b)?)?$", re.I
)

def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def clean_mychords_text(text):
    """Remove MyChords page chrome and keep the actual chord/lyric area."""
    if not text:
        return ""
    lines = [x.strip() for x in str(text).replace("\r", "\n").splitlines()]
    junk_exact = {
        "акорди пісень", "вхід", "реєстрація", "випадкова пісня", "топ",
        "топ виконавців", "топ пісень", "топ користувачів", "генератор акордів",
        "налаштування гітари", "транспонувати акорди", "додати пісню",
        "замовити пісню", "головна", "додати в пісенник", "видалити з пісенника",
        "тональність:", "tahoma", "courier new", "roboto mono", "стоп", "/", "і"
    }

    # Best anchor: first chord-only line followed soon by a normal lyric line.
    start = None
    for i, line in enumerate(lines):
        clean = line.strip("` ")
        toks = [t.strip("|[](){}.,:;`") for t in clean.split() if t]
        is_chords = bool(toks) and all(CHORD_TOKEN_RE.fullmatch(t) for t in toks)
        if not is_chords:
            continue
        for j in range(i+1, min(i+4, len(lines))):
            nxt = lines[j].strip("` ")
            if not nxt:
                continue
            low = nxt.lower()
            if low in junk_exact or low.startswith("в пісеннику у "):
                continue
            nt = [t.strip("|[](){}.,:;`") for t in nxt.split() if t]
            if not (nt and all(CHORD_TOKEN_RE.fullmatch(t) for t in nt)):
                start = i
                break
        if start is not None:
            break

    if start is None:
        return text

    kept = []
    for line in lines[start:]:
        low = line.lower().strip()
        if low in {"коментарі", "схожі пісні", "інші пісні виконавця"}:
            break
        if low in junk_exact or low.startswith("в пісеннику у "):
            continue
        kept.append(line.strip("` "))
    return "\n".join(kept).strip()


def clean_easy_chords_text(text):
    """Remove Telegram post metadata/notes when a real song block can be found."""
    if not text:
        return ""
    lines = [x.strip() for x in str(text).replace("\r", "\n").splitlines()]

    noise_prefixes = (
        "🎬", "відео", "якщо важко грати", "якщо складно грати",
        "тут", "трохи мого занудства", "трохи занудства",
        "підписатися", "канал:", "джерело:"
    )

    # Find first chord row that is followed by lyric text.
    start = None
    for i, line in enumerate(lines):
        clean = line.strip("` ")
        toks = [t.strip("|[](){}.,:;`") for t in clean.split() if t]
        if toks and all(CHORD_TOKEN_RE.fullmatch(t) for t in toks):
            for j in range(i+1, min(i+4, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    continue
                low = nxt.lower()
                if any(low.startswith(p) for p in noise_prefixes):
                    continue
                nt = [t.strip("|[](){}.,:;`") for t in nxt.split() if t]
                if not (nt and all(CHORD_TOKEN_RE.fullmatch(t) for t in nt)):
                    start = i
                    break
        if start is not None:
            break

    # If no real chord+lyric block exists, do not mistake post notes for a song.
    if start is None:
        return ""

    kept = []
    for line in lines[start:]:
        low = line.lower().strip()
        if any(low.startswith(p) for p in noise_prefixes):
            continue
        kept.append(line.strip("` "))
    return "\n".join(kept).strip()


def normalize_imported_song_text(text, source_url=""):
    url = (source_url or "").lower()
    if "mychords" in url:
        text = clean_mychords_text(text)
    elif "t.me/easy_chords" in url or "easy_chords" in url:
        text = clean_easy_chords_text(text)
    return normalize_song_text(text)


def normalize_song_text(text):
    """
    v6.8 common internal format for Diez / MyChords / easy_chords / manual input.
    Keeps lyrics intact, standardizes section headings, whitespace and chord rows.
    """
    if not text:
        return ""

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")

    section_map = {
        "вступ": "ВСТУП",
        "intro": "ВСТУП",
        "куплет": "КУПЛЕТ",
        "verse": "КУПЛЕТ",
        "приспів": "ПРИСПІВ",
        "припев": "ПРИСПІВ",
        "chorus": "ПРИСПІВ",
        "брідж": "БРІДЖ",
        "бридж": "БРІДЖ",
        "bridge": "БРІДЖ",
        "програш": "ПРОГРАШ",
        "проигрыш": "ПРОГРАШ",
        "instrumental": "ПРОГРАШ",
        "кінцівка": "КІНЦІВКА",
        "концовка": "КІНЦІВКА",
        "outro": "КІНЦІВКА",
    }

    out = []
    blank = False

    for raw in text.split("\n"):
        line = raw.strip()

        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        blank = False

        # Normalize common source decorations.
        line = re.sub(r"^[\-\–\—•·]+\s*", "", line).strip()
        line = re.sub(r"\s+", " ", line)

        # Standardize section names, preserving a number such as "Куплет 2".
        sec = re.match(
            r"^(вступ|intro|куплет|verse|приспів|припев|chorus|брідж|бридж|bridge|"
            r"програш|проигрыш|instrumental|кінцівка|концовка|outro)"
            r"\s*[:.\-]?\s*(\d+)?\s*:?\s*$",
            line, re.I
        )
        if sec:
            base = section_map.get(sec.group(1).lower(), sec.group(1).upper())
            num = sec.group(2)
            line = f"{base} {num}" if num else base
            out.append(line)
            continue

        # Standardize chord-only rows without changing the chords themselves.
        tokens = [t for t in re.split(r"\s+", line) if t]
        if tokens:
            cleaned = [t.strip("|[](){}.,:;") for t in tokens]
            chord_count = sum(bool(CHORD_TOKEN_RE.fullmatch(t)) for t in cleaned)
            if chord_count == len(cleaned):
                line = " ".join(cleaned)

        out.append(line)

    # Avoid excessive blank lines.
    result = []
    for line in out:
        if line == "" and (not result or result[-1] == ""):
            continue
        result.append(line)
    return "\n".join(result).strip()


def normalize_mychords_chord_token(token):
    """Normalize a chord token, never apply this to ordinary lyric words."""
    token = str(token or "").strip().strip("|[](){}.,:;")
    return token.translate(str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E"}))


def is_mychords_chord_only(text):
    tokens = str(text).split()
    return bool(tokens) and all(
        CHORD_TOKEN_RE.fullmatch(normalize_mychords_chord_token(t)) for t in tokens
    )


def normalize_mychords_import_text(text):
    """Split actual chord prefixes without mistaking Ukrainian words for chords."""
    if not text:
        return ""
    out = []
    for raw in str(text).replace("\r", "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line or line in {"|", "¦", ".", "·"}:
            continue
        parts = line.split()
        prefix = []
        pos = 0
        while pos < len(parts):
            token = normalize_mychords_chord_token(parts[pos])
            if not CHORD_TOKEN_RE.fullmatch(token):
                break
            # A/B/C etc. at the beginning of a Ukrainian sentence may be
            # prepositions, not chords. Never peel off a single-letter prefix.
            if len(token) == 1 and len(parts) > pos + 1 and re.search(
                r"[А-Яа-яІіЇїЄєҐґ]", " ".join(parts[pos + 1:])
            ):
                break
            prefix.append(token)
            pos += 1
        if prefix and pos < len(parts):
            out.append(" ".join(prefix))
            out.append(" ".join(parts[pos:]))
        else:
            out.append(line)
    return normalize_song_text("\n".join(out))


def normalize_manual_song_text(text):
    """Clean text pasted manually from chord sites such as MyChords.

    Converts rows like "Am E Ти признайся..." into a chord row followed by
    the lyric row, normalizes section headings, and removes common site footer
    text that is not part of the song.
    """
    if not text:
        return ""

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")

    stop_markers = (
        "все ще шукаєш правильні акорди",
        "глянь 5 інших доступних варіантів",
        "інші варіанти цієї пісні",
        "другие варианты этой песни",
    )
    skip_prefixes = (
        "автор песни —",
        "автор пісні —",
        "автор песни -",
        "автор пісні -",
    )

    converted = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        low = line.lower()

        if any(marker in low for marker in stop_markers):
            break
        if any(low.startswith(prefix) for prefix in skip_prefixes):
            continue
        if not line:
            converted.append("")
            continue

        # MyChords copy/paste often produces: "Am E lyric words...".
        # Peel off only consecutive chord tokens from the beginning. At least
        # one non-chord token must remain, otherwise it is already a chord row.
        parts = line.split()
        chord_prefix = []
        pos = 0
        while pos < len(parts):
            token = parts[pos].strip("|[](){}.,:;")
            if CHORD_TOKEN_RE.fullmatch(token):
                chord_prefix.append(token)
                pos += 1
            else:
                break

        if chord_prefix and pos < len(parts):
            lyric = " ".join(parts[pos:]).strip()
            # Avoid treating ordinary prose beginning with a single A-G word
            # as a chord line; pasted song lines normally contain Cyrillic or
            # multiple words after the chord prefix.
            if lyric:
                converted.append(" ".join(chord_prefix))
                converted.append(lyric)
                continue

        converted.append(line)

    return normalize_song_text(repair_glued_leading_chords("\n".join(converted)))


def _chords_from_line(line):
    out = []
    for token in re.split(r"\s+", line.strip()):
        t = token.strip("|[](){}.,:;")
        if CHORD_TOKEN_RE.fullmatch(t):
            out.append(t)
    return out

def _song_blocks(lyrics, semitones=0):
    text = repair_glued_leading_chords(lyrics)
    text = transpose_text(text, semitones) if semitones else text
    blocks, pending = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        sm = SECTION_RE.match(line)
        if sm:
            if pending:
                blocks.append(("chords", pending)); pending = []
            blocks.append(("section", sm.group(1).upper()))
        elif _has_inline_chords(line):
            if pending:
                blocks.append(("chords", pending)); pending = []
            blocks.append(("inline", _transpose_inline_line(line, semitones)))
        elif is_chord_line(line):
            pending.extend(_chords_from_line(line))
        elif pending:
            blocks.append(("pair", pending, line)); pending = []
        else:
            blocks.append(("text", line))
    if pending:
        blocks.append(("chords", pending))
    return blocks

def _unique_chords(lyrics, semitones=0):
    text = transpose_text(lyrics, semitones) if semitones else lyrics
    out = []
    for line in text.splitlines():
        if _has_inline_chords(line):
            for c in INLINE_CHORD_RE.findall(line):
                if c not in out:
                    out.append(c)
        else:
            for c in _chords_from_line(line):
                if c not in out:
                    out.append(c)
    return out

# Six strings, low E -> high e. 0=open, -1=muted, positive=fret.
# Common open/barre shapes used by the songbook.
CHORD_SHAPES = {
    "C":  [-1,3,2,0,1,0], "Cm": [-1,3,5,5,4,3],
    "C#": [-1,4,6,6,6,4], "C#m": [-1,4,6,6,5,4],
    "Db": [-1,4,6,6,6,4], "Dbm": [-1,4,6,6,5,4],
    "D":  [-1,-1,0,2,3,2], "Dm": [-1,-1,0,2,3,1],
    "D#": [-1,6,8,8,8,6], "D#m": [-1,6,8,8,7,6],
    "Eb": [-1,6,8,8,8,6], "Ebm": [-1,6,8,8,7,6],
    "E":  [0,2,2,1,0,0], "Em": [0,2,2,0,0,0],
    "F":  [1,3,3,2,1,1], "Fm": [1,3,3,1,1,1],
    "F#": [2,4,4,3,2,2], "F#m": [2,4,4,2,2,2],
    "Gb": [2,4,4,3,2,2], "Gbm": [2,4,4,2,2,2],
    "G":  [3,2,0,0,0,3], "Gm": [3,5,5,3,3,3],
    "G#": [4,6,6,5,4,4], "G#m": [4,6,6,4,4,4],
    "Ab": [4,6,6,5,4,4], "Abm": [4,6,6,4,4,4],
    "A":  [0,0,2,2,2,0], "Am": [0,0,2,2,1,0],
    "A#": [-1,1,3,3,3,1], "A#m": [-1,1,3,3,2,1],
    "Bb": [-1,1,3,3,3,1], "Bbm": [-1,1,3,3,2,1],
    "B":  [-1,2,4,4,4,2], "Bm": [-1,2,4,4,3,2],
}

def _base_chord(chord):
    # Diagram fallback: strip extensions but preserve major/minor root.
    m = re.match(r"^([A-G](?:#|b)?)(m)?", chord)
    return (m.group(1) + ("m" if m.group(2) else "")) if m else chord

def _draw_chord_diagram(d, x, y, chord, ink, accent, muted):
    name_font = _font(27, True)
    small = _font(15, True)
    shape = CHORD_SHAPES.get(_base_chord(chord))
    d.text((x+58, y), chord, font=name_font, fill=ink, anchor="ma")
    if not shape:
        d.text((x+58, y+35), "схема —", font=small, fill=muted, anchor="ma")
        return 75

    positive = [f for f in shape if f > 0]
    minf = min(positive) if positive else 1
    maxf = max(positive) if positive else 1
    start_fret = 1 if maxf <= 4 else minf
    gx, gy = x+12, y+40
    sw, fh = 20, 25

    # fret number for shifted/barre positions
    if start_fret > 1:
        d.text((x-3, gy+2), str(start_fret), font=small, fill=muted)

    # grid
    for i in range(6):
        xx = gx + i*sw
        d.line((xx, gy, xx, gy+4*fh), fill=ink, width=2)
    for j in range(5):
        yy = gy + j*fh
        d.line((gx, yy, gx+5*sw, yy), fill=ink, width=4 if (j==0 and start_fret==1) else 2)

    for i, fret in enumerate(shape):
        xx = gx + i*sw
        if fret == 0:
            d.ellipse((xx-5, gy-18, xx+5, gy-8), outline=ink, width=2)
        elif fret < 0:
            d.line((xx-5, gy-18, xx+5, gy-8), fill=muted, width=2)
            d.line((xx+5, gy-18, xx-5, gy-8), fill=muted, width=2)
        else:
            rel = fret - start_fret + 1
            if 1 <= rel <= 4:
                yy = gy + (rel-.5)*fh
                d.ellipse((xx-7, yy-7, xx+7, yy+7), fill=accent)
    return 155

def _wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words: return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        test = cur + " " + w
        if draw.textbbox((0,0), test, font=font)[2] <= max_width:
            cur = test
        else:
            lines.append(cur); cur = w
    lines.append(cur)
    return lines

INLINE_CHORD_RE = re.compile(r"\[([A-G](?:#|b)?(?:m|maj|min|dim|aug|sus)?(?:2|4|5|6|7|9|11|13)?(?:add\d+)?(?:/[A-G](?:#|b)?)?)\]")

def _has_inline_chords(line):
    return bool(INLINE_CHORD_RE.search(line or ""))

def _transpose_inline_line(line, semitones=0):
    if not semitones:
        return line
    def repl(m):
        return "[" + transpose_key(m.group(1), semitones) + "]"
    return INLINE_CHORD_RE.sub(repl, line)

def _inline_plain_and_anchors(line, semitones=0):
    """Return clean lyric text and [(chord, character_offset), ...]."""
    line = _transpose_inline_line(line, semitones)
    plain_parts, anchors = [], []
    pos = 0
    for m in INLINE_CHORD_RE.finditer(line):
        chunk = line[pos:m.start()]
        plain_parts.append(chunk)
        offset = len("".join(plain_parts))
        anchors.append((m.group(1), offset))
        pos = m.end()
    plain_parts.append(line[pos:])
    return "".join(plain_parts), anchors

def _draw_inline_pair(draw, x, y, line, lyric_font, chord_font, body_w, ink, accent):
    """Draw [Am]word style lyrics with exact chord anchors. Returns new y."""
    plain, anchors = _inline_plain_and_anchors(line, 0)
    # Keep exact anchors by wrapping at word boundaries while tracking source offsets.
    words = list(re.finditer(r"\S+", plain))
    if not words:
        return y

    rows = []
    row_start = words[0].start()
    row_end = words[0].end()
    for wm in words[1:]:
        candidate = plain[row_start:wm.end()]
        if draw.textbbox((0,0), candidate, font=lyric_font)[2] <= body_w:
            row_end = wm.end()
        else:
            rows.append((row_start, row_end))
            row_start, row_end = wm.start(), wm.end()
    rows.append((row_start, row_end))

    for rs, re_ in rows:
        row_text = plain[rs:re_]
        row_anchors = [(c,o) for c,o in anchors if rs <= o <= re_]
        for chord, off in row_anchors:
            prefix = plain[rs:off]
            px = draw.textbbox((0,0), prefix, font=lyric_font)[2] if prefix else 0
            draw.text((x+px, y), chord, font=chord_font, fill=accent)
        y += 35
        draw.text((x,y),row_text,font=lyric_font,fill=ink)
        y += 43
    return y + 8


def _wrap_chord_row(draw, chords, font, max_width, gap="   "):
    """Wrap a chord-only row without letting it run outside the poster."""
    rows, cur = [], []
    for chord in chords:
        test = gap.join(cur + [chord])
        if cur and draw.textbbox((0, 0), test, font=font)[2] > max_width:
            rows.append(cur)
            cur = [chord]
        else:
            cur.append(chord)
    if cur:
        rows.append(cur)
    return rows


def render_song_images(song):
    """v6.4 — one long, phone-readable song image."""
    song_id, title, artist, song_key, lyrics, favorite, source_url, capo, saved_transpose = song
    semitones = int(saved_transpose or 0)
    shown_key = transpose_key(song_key, semitones) if song_key else "—"
    blocks = _song_blocks(lyrics, semitones)
    chords = _unique_chords(lyrics, semitones)

    W = 1200
    bg=(238,249,225); ink=(35,39,35); muted=(105,112,102)
    accent=(214,43,35); rule=(202,217,191)

    title_font=_font(54,True)
    meta_font=_font(23)
    section_font=_font(25,True)
    lyric_font=_font(32,True)
    chord_font=_font(27,True)

    left_x = 55
    body_x = 350
    body_w = W - body_x - 60

    # Estimate body height more accurately.
    probe = Image.new("RGB",(W,500),bg)
    pd = ImageDraw.Draw(probe)
    body_h = 210
    for b in blocks:
        if b[0] == "section":
            body_h += 65
        elif b[0] == "inline":
            plain, _anchors = _inline_plain_and_anchors(b[1], 0)
            wrapped = _wrap_text(pd, plain, lyric_font, body_w)
            body_h += 78 * max(1, len(wrapped))
        elif b[0] == "pair":
            wrapped = _wrap_text(pd, b[2], lyric_font, body_w)
            body_h += 42 + 43*len(wrapped) + 10
        elif b[0] == "chords":
            chord_rows = _wrap_chord_row(pd, b[1], chord_font, body_w)
            body_h += 48 * max(1, len(chord_rows))
        else:
            wrapped = _wrap_text(pd, b[1], lyric_font, body_w)
            body_h += 43*len(wrapped) + 8

    # Estimate chord diagram column.
    diagram_h = 220 + min(len(chords), 14) * 190
    H = max(1250, body_h + 90, diagram_h + 70)

    img=Image.new("RGB",(W,H),bg)
    d=ImageDraw.Draw(img)

    # Header
    d.text((W/2,42), title, font=title_font, fill=ink, anchor="ma")
    meta=f"{artist}  •  Тональність: {shown_key}  •  Капо: {capo}"
    if semitones:
        meta += f"  •  Транспонування {semitones:+d}"
    d.text((W/2,110), meta, font=meta_font, fill=muted, anchor="ma")
    d.line((55,155,W-55,155),fill=rule,width=2)

    # Chord diagrams
    d.text((left_x+100,180),"АКОРДИ",font=section_font,fill=muted,anchor="ma")
    cy=225
    for c in chords[:14]:
        # Draw a larger version by temporarily using the existing diagram
        # with extra vertical separation.
        used = _draw_chord_diagram(d,left_x+30,cy,c,ink,accent,muted)
        cy += max(180, used+22)

    # Lyrics/chords
    y=180
    for b in blocks:
        kind=b[0]

        if kind=="section":
            y += 10
            d.text((body_x,y),b[1],font=section_font,fill=muted)
            y += 38
            d.line((body_x,y,W-60,y),fill=rule,width=2)
            y += 20

        elif kind=="inline":
            y = _draw_inline_pair(
                d, body_x, y, b[1], lyric_font, chord_font,
                body_w, ink, accent
            )

        elif kind=="pair":
            chord_list, lyric=b[1],b[2]
            lines=_wrap_text(d,lyric,lyric_font,body_w)

            # v6.6: keep v6.4's stable layout, but snap chord positions to
            # distinct word starts when possible. We do NOT pretend that the
            # old Diez import contains exact character offsets — it doesn't.
            first = lines[0] if lines else lyric
            words = list(re.finditer(r"\S+", first))
            n = len(chord_list)

            if n == 1 or len(words) < n:
                box=d.textbbox((0,0),first,font=lyric_font)
                lw=max(280,min(body_w,box[2]-box[0]))
                xs=[body_x] if n == 1 else [
                    body_x+int(i*max(1,lw-70)/(n-1)) for i in range(n)
                ]
            else:
                # Select n distinct word starts across the line.
                idxs=[]
                for i in range(n):
                    idx = round(i*(len(words)-1)/(n-1)) if n > 1 else 0
                    if idxs and idx <= idxs[-1]:
                        idx = min(len(words)-1, idxs[-1]+1)
                    idxs.append(idx)
                xs=[]
                for idx in idxs:
                    prefix=first[:words[idx].start()]
                    px=d.textbbox((0,0),prefix,font=lyric_font)[2] if prefix else 0
                    xs.append(body_x+px)

            for x,c in zip(xs,chord_list):
                d.text((x,y),c,font=chord_font,fill=accent)

            y += 35
            for ln in lines:
                d.text((body_x,y),ln,font=lyric_font,fill=ink)
                y += 43
            y += 10

        elif kind=="chords":
            for chord_row in _wrap_chord_row(d, b[1], chord_font, body_w):
                d.text((body_x,y),"   ".join(chord_row),font=chord_font,fill=accent)
                y += 48

        else:
            for ln in _wrap_text(d,b[1],lyric_font,body_w):
                d.text((body_x,y),ln,font=lyric_font,fill=ink)
                y += 43
            y += 8

    # Crop to actual used content while keeping diagrams visible.
    final_h=max(y+70,cy+30,900)
    if final_h < H:
        img=img.crop((0,0,W,final_h))

    # Telegram photos have dimension limits. Very long songs (especially imports)
    # are split into several PNG pages instead of failing completely.
    MAX_PAGE_H = 7600
    pages = []
    for top in range(0, img.height, MAX_PAGE_H):
        bottom = min(top + MAX_PAGE_H, img.height)
        page = img.crop((0, top, img.width, bottom))
        buf = io.BytesIO()
        page.save(buf, format="PNG", optimize=True)
        pages.append(buf.getvalue())
    return pages


def songs_view(only_favorites=False):
    if only_favorites:
        cursor.execute("""
            SELECT id, title, artist
            FROM songs
            WHERE favorite = TRUE
            ORDER BY lower(artist), lower(title)
        """)
        title = "⭐ <b>Улюблені пісні</b>"
    else:
        cursor.execute("""
            SELECT id, title, artist
            FROM songs
            ORDER BY lower(artist), lower(title)
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

    if not only_favorites and songs:
        keyboard.append([
            InlineKeyboardButton(text="🎤 За виконавцями", callback_data="artists")
        ])

    for song_id, song_title, artist in songs:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🎸 {artist} — {song_title}",
                callback_data=f"song_{song_id}"
            )
        ])

    if not only_favorites:
        keyboard.append([
            InlineKeyboardButton(text="➕ Додати пісню", callback_data="add_song")
        ])

    keyboard.append([
        InlineKeyboardButton(text="🏠 Головна", callback_data="home")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def artists_view():
    cursor.execute("""
        SELECT MIN(id), COALESCE(NULLIF(TRIM(artist), ''), 'Невідомий виконавець'), COUNT(*)
        FROM songs
        GROUP BY COALESCE(NULLIF(TRIM(artist), ''), 'Невідомий виконавець')
        ORDER BY lower(COALESCE(NULLIF(TRIM(artist), ''), 'Невідомий виконавець'))
    """)
    artists = cursor.fetchall()

    text = f"🎤 <b>Виконавці</b>\n\nВиконавців: <b>{len(artists)}</b>\n\nОбери виконавця 👇"
    keyboard = []
    for representative_id, artist, count in artists:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🎤 {artist} ({count})",
                callback_data=f"artist_{representative_id}"
            )
        ])
    keyboard.append([InlineKeyboardButton(text="⬅️ Усі пісні", callback_data="songs")])
    keyboard.append([InlineKeyboardButton(text="🏠 Головна", callback_data="home")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def artist_songs_view(representative_id):
    cursor.execute("SELECT artist FROM songs WHERE id = %s", (representative_id,))
    row = cursor.fetchone()
    if not row:
        return None, None
    artist = row[0] or "Невідомий виконавець"
    cursor.execute("""
        SELECT id, title
        FROM songs
        WHERE COALESCE(artist, '') = %s
        ORDER BY lower(title)
    """, (row[0] or "",))
    songs = cursor.fetchall()

    text = f"🎤 <b>{escape_html(artist)}</b>\n\nПісень: <b>{len(songs)}</b>\n\nОбери пісню 👇"
    keyboard = [
        [InlineKeyboardButton(text=f"🎸 {title}", callback_data=f"song_{song_id}")]
        for song_id, title in songs
    ]
    keyboard.append([InlineKeyboardButton(text="⬅️ До виконавців", callback_data="artists")])
    keyboard.append([InlineKeyboardButton(text="🏠 Головна", callback_data="home")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def search_results(query):
    pattern = f"%{query}%"

    cursor.execute("""
        SELECT id, title, artist
        FROM songs
        WHERE title ILIKE %s
           OR artist ILIKE %s
        ORDER BY lower(artist), lower(title)
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


@dp.callback_query(F.data == "artists")
async def show_artists(callback: CallbackQuery):
    text, keyboard = artists_view()
    await edit_screen(callback, text, keyboard)


@dp.callback_query(F.data.regexp(r"^artist_\d+$"))
async def show_artist_songs(callback: CallbackQuery):
    representative_id = int(callback.data.replace("artist_", "", 1))
    text, keyboard = artist_songs_view(representative_id)
    if text is None:
        await callback.answer("Виконавця не знайдено.", show_alert=True)
        return
    await edit_screen(callback, text, keyboard)


@dp.callback_query(F.data.regexp(r"^song_\d+$"))
async def open_song(callback: CallbackQuery):
    song_id = int(callback.data.replace("song_", "", 1))
    song = get_song(song_id)

    if not song:
        await callback.answer(
            "Пісню вже видалено.",
            show_alert=True
        )
        return

    saved_transpose = int(song[8] or 0)
    TRANSPOSE_STATE[song_id] = saved_transpose
    text, keyboard = song_card(song, saved_transpose)
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

    current = TRANSPOSE_STATE.get(song_id, int(song[8] or 0))

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

    cursor.execute(
        "UPDATE songs SET transpose = %s WHERE id = %s",
        (current, song_id)
    )

    # Оновлюємо tuple, щоб картка вже містила актуальне збережене значення.
    song = get_song(song_id)
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


def mychords_shift_chord_rows(lyrics, semitones):
    """Transpose only chord-only rows, never ordinary Ukrainian lyrics."""
    if not semitones:
        return lyrics
    return "\n".join(
        transpose_text(line, semitones) if is_chord_line(line) else line
        for line in lyrics.split("\n")
    )


def imported_preview(data, shift=0):
    lyrics = data["lyrics"]
    if len(lyrics) > 900:
        cut = lyrics.rfind("\n", 0, 900)
        preview = lyrics[:cut if cut > 600 else 900] + "\n…"
    else:
        preview = lyrics
    tone = data.get("song_key") or "не визначена"
    note = "\n🎚 Зміна акордів: <b>{:+d}</b>".format(shift) if data.get("source") == "MyChords" else ""
    return (
        "🔎 <b>Перевір імпорт</b>\n\n"
        f"🌐 Джерело: <b>{escape_html(data.get('source', 'Посилання'))}</b>\n"
        f"🎵 <b>{escape_html(data['title'])}</b>\n"
        f"👤 {escape_html(data['artist'])}\n"
        f"🎸 Перший акорд: <b>{escape_html(tone)}</b>{note}\n\n"
        f"<pre>{escape_html(preview)}</pre>\n\n"
        "Перевір акорди перед збереженням. Кнопки змінюють усі акорди на півтон."
    )


def imported_keyboard(data, shift=0):
    rows = []
    if data.get("source") == "MyChords":
        rows.append([
            InlineKeyboardButton(text="♭ −1", callback_data="imp_tone_down"),
            InlineKeyboardButton(text=f"🎼 {shift:+d}", callback_data="imp_tone_reset"),
            InlineKeyboardButton(text="♯ +1", callback_data="imp_tone_up"),
        ])
    rows.append([InlineKeyboardButton(text="✅ Зберегти", callback_data="save_import")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.in_({"imp_tone_down", "imp_tone_up", "imp_tone_reset"}))
async def adjust_import_tone(callback: CallbackQuery):
    state = ADD_STATE.get(callback.from_user.id)
    if not state or state.get("step") != "confirm_import" or state["data"].get("source") != "MyChords":
        await callback.answer("Немає активного імпорту MyChords.", show_alert=True)
        return
    data = state["data"]
    shift = int(state.get("import_shift", 0))
    if callback.data == "imp_tone_reset":
        shift = 0
    else:
        shift += -1 if callback.data == "imp_tone_down" else 1
    if not -6 <= shift <= 6:
        await callback.answer("Доступний діапазон від −6 до +6.", show_alert=True)
        return
    original = state["original_import"]
    data["lyrics"] = mychords_shift_chord_rows(original["lyrics"], shift)
    data["song_key"] = transpose_key(original.get("song_key", ""), shift)
    state["import_shift"] = shift
    await callback.message.edit_text(
        imported_preview(data, shift), parse_mode="HTML",
        reply_markup=imported_keyboard(data, shift),
    )
    await callback.answer()


@dp.callback_query(F.data == "save_import")
async def save_import(callback: CallbackQuery):
    state = ADD_STATE.get(callback.from_user.id)

    if not state or state.get("step") != "confirm_import":
        await callback.answer("Імпорт уже завершено або скасовано.", show_alert=True)
        return

    data = state["data"]

    cursor.execute("""
        INSERT INTO songs
        (title, artist, song_key, lyrics, source_url, capo)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        data["title"],
        data["artist"],
        data.get("song_key", ""),
        data["lyrics"],
        data.get("source_url", ""),
        data.get("capo", 0)
    ))

    song_id = cursor.fetchone()[0]
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



def autoscroll_keyboard(song_id, paused=False):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🐢 Повільніше", callback_data=f"scroll_slower_{song_id}"),
            InlineKeyboardButton(
                text="▶️ Продовжити" if paused else "⏸ Пауза",
                callback_data=f"scroll_toggle_{song_id}"
            ),
            InlineKeyboardButton(text="🐇 Швидше", callback_data=f"scroll_faster_{song_id}"),
        ],
        [
            InlineKeyboardButton(text="⏮ На початок", callback_data=f"scroll_restart_{song_id}"),
            InlineKeyboardButton(text="⏹ Стоп", callback_data=f"scroll_stop_{song_id}"),
        ]
    ])


def _scroll_pair_lines(chords, lyric):
    """Place chord names above approximate word starts, like the song image."""
    chords = list(chords or [])
    lyric = (lyric or "").strip()
    if not chords or not lyric:
        return "   ".join(chords), lyric

    words = list(re.finditer(r"\S+", lyric))
    if not words:
        return "   ".join(chords), lyric

    # Same positioning idea as the image renderer: spread chords across
    # distinct word starts. Monospace <pre> keeps the spaces in Telegram.
    n = len(chords)
    if n == 1:
        positions = [0]
    elif len(words) >= n:
        idxs = []
        for i in range(n):
            idx = round(i * (len(words) - 1) / (n - 1))
            if idxs and idx <= idxs[-1]:
                idx = min(len(words) - 1, idxs[-1] + 1)
            idxs.append(idx)
        positions = [words[i].start() for i in idxs]
    else:
        width = max(len(lyric), n * 5)
        positions = [round(i * max(1, width - 3) / (n - 1)) for i in range(n)]

    row = []
    cursor_pos = 0
    for chord, pos in zip(chords, positions):
        pos = max(pos, cursor_pos)
        if pos > cursor_pos:
            row.append(" " * (pos - cursor_pos))
        row.append(chord)
        cursor_pos = pos + len(chord)
    return "".join(row).rstrip(), lyric


def _autoscroll_rows(lyrics, semitones=0):
    """Build visual rows so chords stay above the lyric they belong to."""
    text = repair_glued_leading_chords(lyrics)
    text = transpose_text(text, semitones) if semitones else text
    src = text.splitlines()
    rows = []
    i = 0

    while i < len(src):
        line = src[i].strip()
        if not line:
            rows.append(("blank", ""))
            i += 1
            continue

        sm = SECTION_RE.match(line)
        if sm:
            rows.append(("section", sm.group(1).upper()))
            i += 1
            continue

        # A chord-only line followed by lyrics becomes a two-line visual pair.
        if is_chord_line(line):
            j = i + 1
            while j < len(src) and not src[j].strip():
                j += 1
            if j < len(src):
                nxt = src[j].strip()
                if nxt and not SECTION_RE.match(nxt) and not is_chord_line(nxt):
                    chord_row, lyric_row = _scroll_pair_lines(_chords_from_line(line), nxt)
                    rows.append(("pair", chord_row, lyric_row))
                    i = j + 1
                    continue
            rows.append(("chords", line))
            i += 1
            continue

        rows.append(("text", line))
        i += 1

    # Remove repeated blank rows for a cleaner phone view.
    cleaned = []
    for row in rows:
        if row[0] == "blank" and (not cleaned or cleaned[-1][0] == "blank"):
            continue
        cleaned.append(row)
    return cleaned


def autoscroll_text(song, offset, speed, window=7):
    song_id, title, artist, song_key, lyrics, favorite, source_url, capo, saved_transpose = song
    semitones = int(saved_transpose or 0)
    rows = _autoscroll_rows(lyrics, semitones)
    if not rows:
        rows = [("text", "(порожня пісня)")]

    offset = max(0, min(offset, max(0, len(rows) - 1)))
    shown = rows[offset:offset + window]
    body = []

    for row in shown:
        kind = row[0]
        if kind == "blank":
            body.append("")
        elif kind == "section":
            body.append(f"<b>━━ {escape_html(row[1])} ━━</b>")
        elif kind == "pair":
            # One PRE block is important: Telegram preserves every space,
            # therefore each chord remains above the intended word.
            body.append(
                "<pre>" + escape_html(row[1]) + "\n" + escape_html(row[2]) + "</pre>"
            )
        elif kind == "chords":
            body.append("<pre>" + escape_html(row[1]) + "</pre>")
        else:
            body.append(escape_html(row[1]))

    progress = min(100, int((offset + 1) * 100 / max(1, len(rows))))
    return (
        f"▶️ <b>Автоскрол: {escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}  •  ⚡ {speed:.1f} с  •  {progress}%\n\n"
        + "\n".join(body)
    ), len(rows)


async def autoscroll_worker(user_id):
    try:
        while user_id in AUTO_SCROLL_STATE:
            state = AUTO_SCROLL_STATE[user_id]
            await asyncio.sleep(state["speed"])
            state = AUTO_SCROLL_STATE.get(user_id)
            if not state or state["paused"]:
                continue

            song = get_song(state["song_id"])
            if not song:
                break
            text, total = autoscroll_text(song, state["offset"], state["speed"])
            if state["offset"] >= max(0, total - 1):
                state["paused"] = True
                try:
                    await state["message"].edit_text(
                        text + "\n\n🏁 <b>Кінець пісні</b>",
                        parse_mode="HTML",
                        reply_markup=autoscroll_keyboard(state["song_id"], True)
                    )
                except TelegramBadRequest:
                    pass
                continue

            state["offset"] += 1
            text, _ = autoscroll_text(song, state["offset"], state["speed"])
            try:
                await state["message"].edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=autoscroll_keyboard(state["song_id"], False)
                )
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    print("AUTOSCROLL EDIT ERROR:", repr(e))
    except asyncio.CancelledError:
        pass
    finally:
        AUTO_SCROLL_TASKS.pop(user_id, None)


@dp.callback_query(F.data.startswith("song_scroll_"))
async def start_autoscroll(callback: CallbackQuery):
    song_id = int(callback.data.replace("song_scroll_", "", 1))
    song = get_song(song_id)
    if not song:
        await callback.answer("Пісню не знайдено.", show_alert=True)
        return

    user_id = callback.from_user.id
    old_task = AUTO_SCROLL_TASKS.pop(user_id, None)
    if old_task:
        old_task.cancel()

    speed = 3.0
    text, _ = autoscroll_text(song, 0, speed)
    msg = await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=autoscroll_keyboard(song_id, False)
    )
    AUTO_SCROLL_STATE[user_id] = {
        "song_id": song_id, "offset": 0, "speed": speed,
        "paused": False, "message": msg
    }
    AUTO_SCROLL_TASKS[user_id] = asyncio.create_task(autoscroll_worker(user_id))
    await callback.answer("▶️ Автоскрол запущено")


@dp.callback_query(F.data.regexp(r"^scroll_(toggle|faster|slower|restart|stop)_\d+$"))
async def control_autoscroll(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    song_id = int(parts[2])
    user_id = callback.from_user.id
    state = AUTO_SCROLL_STATE.get(user_id)

    if not state or state["song_id"] != song_id:
        await callback.answer("Автоскрол уже не активний.", show_alert=True)
        return

    if action == "stop":
        task = AUTO_SCROLL_TASKS.pop(user_id, None)
        if task:
            task.cancel()
        AUTO_SCROLL_STATE.pop(user_id, None)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.answer("⏹ Автоскрол зупинено")
        return
    elif action == "toggle":
        state["paused"] = not state["paused"]
    elif action == "faster":
        state["speed"] = max(1.5, round(state["speed"] - 0.5, 1))
    elif action == "slower":
        state["speed"] = min(8.0, round(state["speed"] + 0.5, 1))
    elif action == "restart":
        state["offset"] = 0
        state["paused"] = False

    song = get_song(song_id)
    text, _ = autoscroll_text(song, state["offset"], state["speed"])
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=autoscroll_keyboard(song_id, state["paused"])
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("song_image_"))
async def song_image(callback: CallbackQuery):
    try:
        song_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await safe_answer(callback)
        return

    cursor.execute("""
        SELECT id, title, artist, song_key, lyrics, favorite, source_url, capo, transpose
        FROM songs
        WHERE id = %s
    """, (song_id,))
    song = cursor.fetchone()

    if not song:
        await callback.answer("Пісню не знайдено.", show_alert=True)
        return

    await callback.answer("🎨 Створюю картинку…")

    try:
        pages = render_song_images(song)
        filename = re.sub(r"[^0-9A-Za-zА-Яа-яІіЇїЄєҐґ_-]+", "_", song[1]).strip("_")
        for i, png in enumerate(pages, 1):
            suffix = f"_{i}" if len(pages) > 1 else ""
            photo = BufferedInputFile(png, filename=f"{filename or 'song'}{suffix}.png")
            caption = (
                f"🖼 <b>{escape_html(song[1])}</b> — картинка з акордами"
                + (f" • {i}/{len(pages)}" if len(pages) > 1 else "")
            )
            await callback.message.answer_photo(
                photo=photo,
                caption=caption,
                parse_mode="HTML"
            )
    except Exception as e:
        print("IMAGE ERROR:", repr(e))
        await callback.message.answer(
            "❌ Не вдалося створити картинку. Подивись Logs на Render."
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

    new_value = False if song[5] else True

    cursor.execute(
        "UPDATE songs SET favorite = %s WHERE id = %s",
        (new_value, song_id)
    )

    updated_song = get_song(song_id)
    semitones = TRANSPOSE_STATE.get(song_id, int(updated_song[8] or 0))
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

    saved_transpose = int(song[8] or 0)
    TRANSPOSE_STATE[song_id] = saved_transpose
    text, keyboard = song_card(song, saved_transpose)
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
        "DELETE FROM songs WHERE id = %s",
        (song_id,)
    )

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
            if imported.get("source") == "MyChords":
                state["original_import"] = dict(imported)
                state["import_shift"] = 0
            await wait_msg.edit_text(
                imported_preview(imported), parse_mode="HTML",
                reply_markup=imported_keyboard(imported),
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
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (
                data["title"],
                data["artist"],
                data["song_key"],
                normalize_manual_song_text(text)
            ))

            song_id = cursor.fetchone()[0]

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
    for task in list(AUTO_SCROLL_TASKS.values()):
        task.cancel()
    AUTO_SCROLL_TASKS.clear()
    AUTO_SCROLL_STATE.clear()
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
