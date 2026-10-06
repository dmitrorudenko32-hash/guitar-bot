import os
import sqlite3
import random
from aiohttp import web

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

db.commit()


# ==================================================
# СТАНИ ДОДАВАННЯ / ПОШУКУ
# ==================================================

ADD_STATE = {}
SEARCH_WAITING = set()


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
        SELECT id, title, artist, song_key, lyrics, favorite
        FROM songs
        WHERE id = ?
    """, (song_id,))
    return cursor.fetchone()


def song_card(song):
    song_id, title, artist, song_key, lyrics, favorite = song

    star = "⭐" if favorite else "☆"
    key_text = song_key if song_key else "не вказана"

    text = (
        f"🎵 <b>{title}</b>\n"
        f"👤 {artist}\n"
        f"🎸 Тональність: <b>{key_text}</b>\n"
        f"{star} {'Улюблена' if favorite else 'Не в улюблених'}\n\n"
        f"<pre>{escape_html(lyrics)}</pre>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
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
                InlineKeyboardButton(
                    text="🎵 До пісень",
                    callback_data="songs"
                ),
                InlineKeyboardButton(
                    text="🏠 Головна",
                    callback_data="home"
                ),
            ],
        ]
    )

    return text, keyboard


def escape_html(text):
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


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

    text, keyboard = song_card(song)
    await edit_screen(callback, text, keyboard)


# ==================================================
# ДОДАВАННЯ ПІСНІ
# ==================================================

async def begin_add_song(user_id, send_func):
    ADD_STATE[user_id] = {
        "step": "title",
        "data": {}
    }

    SEARCH_WAITING.discard(user_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Скасувати",
                    callback_data="cancel_add"
                )
            ]
        ]
    )

    await send_func(
        "➕ <b>Додаємо нову пісню</b>\n\n"
        "1/4. Напиши <b>назву пісні</b>:",
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


@dp.callback_query(F.data == "cancel_add")
async def cancel_add(callback: CallbackQuery):
    ADD_STATE.pop(callback.from_user.id, None)

    await edit_screen(
        callback,
        "❌ <b>Додавання скасовано</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏠 Головна",
                        callback_data="home"
                    )
                ]
            ]
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
    text, keyboard = song_card(updated_song)

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

    text, keyboard = song_card(song)
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
