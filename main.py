import os
import logging
from urllib.parse import quote_plus

import aiohttp
import aiosqlite
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DB = os.getenv("DB_PATH", "music.db")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("muzikynk")

application = None


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def save_search(user_id: int, query: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO searches(user_id, query) VALUES(?, ?)",
            (user_id, query),
        )
        await db.commit()


def menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Найти музыку", callback_data="search")],
            [InlineKeyboardButton("🔥 Популярное", callback_data="popular")],
            [InlineKeyboardButton("🕘 История", callback_data="history")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        ]
    )


def result_buttons(title: str, artist: str):
    query = quote_plus(f"{artist} {title}")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ YouTube",
                    url=f"https://www.youtube.com/results?search_query={query}",
                ),
                InlineKeyboardButton(
                    "☁️ SoundCloud",
                    url=f"https://soundcloud.com/search?q={query}",
                ),
            ],
            [InlineKeyboardButton("🔎 Новый поиск", callback_data="search")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 <b>МузыкаYNK</b>\n\n"
        "Найду информацию о треке по названию или исполнителю.\n\n"
        "Напиши название песни или исполнителя:",
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Как пользоваться</b>\n\n"
        "• Напиши исполнителя или название песни.\n"
        "• Я покажу найденные треки.\n"
        "• Кнопки ниже откроют официальный поиск YouTube или SoundCloud.\n\n"
        "Бот не скачивает защищённую авторским правом музыку."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, parse_mode="HTML", reply_markup=menu()
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=menu()
        )


async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    query = update.message.text.strip()
    if not query:
        return

    await save_search(update.effective_user.id, query)

    await update.message.reply_text("🔎 Ищу музыку...")

    url = "https://musicbrainz.org/ws/2/recording/"
    params = {
        "query": query,
        "fmt": "json",
        "limit": "8",
    }
    headers = {"User-Agent": "MuzikYNK/1.0 (Telegram music bot)"}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    log.error("MusicBrainz HTTP %s", resp.status)
                    await update.message.reply_text(
                        "⚠️ Сервис поиска временно недоступен. Попробуй ещё раз."
                    )
                    return

                data = await resp.json()
    except Exception:
        log.exception("MusicBrainz request failed")
        await update.message.reply_text(
            "⚠️ Не удалось выполнить поиск. Попробуй ещё раз."
        )
        return

    recordings = data.get("recordings", [])
    if not recordings:
        await update.message.reply_text(
            "😔 Ничего не нашёл.\n\n"
            "Попробуй написать название или исполнителя иначе.",
            reply_markup=menu(),
        )
        return

    for recording in recordings[:8]:
        title = recording.get("title") or "Без названия"
        artists = recording.get("artist-credit") or []
        artist_names = []

        for item in artists:
            artist = item.get("artist") or {}
            name = artist.get("name")
            if name:
                artist_names.append(name)

        artist = ", ".join(artist_names) or "Неизвестный исполнитель"

        releases = recording.get("releases") or []
        album = ""
        if releases:
            album = releases[0].get("title") or ""

        text = f"🎵 <b>{title}</b>\n👤 {artist}"

        if album:
            text += f"\n💿 {album}"

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=result_buttons(title, artist),
        )


async def popular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return

    await update.callback_query.answer()

    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            SELECT query, COUNT(*) AS cnt
            FROM searches
            GROUP BY query
            ORDER BY cnt DESC
            LIMIT 10
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        text = "🔥 Пока нет популярного поиска."
    else:
        text = "🔥 <b>Популярное</b>\n\n"

        for i, (query, count) in enumerate(rows, 1):
            text += f"{i}. {query} — {count} раз\n"

    await update.callback_query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return

    await update.callback_query.answer()

    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            SELECT query
            FROM searches
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (update.effective_user.id,),
        )
        rows = await cursor.fetchall()

    if not rows:
        text = "🕘 История пока пустая."
    else:
        text = "🕘 <b>Последние поиски</b>\n\n"

        for i, (query,) in enumerate(rows, 1):
            text += f"{i}. {query}\n"

    await update.callback_query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    if query.data == "search":
        await query.message.reply_text(
            "🔎 Напиши название песни или исполнителя:"
        )

    elif query.data == "help":
        await help_cmd(update, context)

    elif query.data == "popular":
        await popular(update, context)

    elif query.data == "history":
        await history(update, context)


async def health(request):
    return web.Response(text="ok")


async def telegram_webhook(request):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            application.bot,
        )

        await application.process_update(update)

        return web.Response(text="ok")

    except Exception:
        log.exception("Webhook update processing failed")

        return web.Response(
            status=500,
            text="error",
        )


async def on_startup(app_web: web.Application):
    global application

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set"
        )

    if not BASE_URL:
        raise RuntimeError(
            "BASE_URL is not set"
        )

    await init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_cmd)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            search_music,
        )
    )

    await application.initialize()
    await application.start()

    webhook_url = f"{BASE_URL}/telegram"

    await application.bot.set_webhook(
        webhook_url
    )

    log.info(
        "Telegram webhook set to %s",
        webhook_url,
    )

    log.info(
        "MuzikYNK started on port %s",
        PORT,
    )


async def on_cleanup(app_web: web.Application):
    global application

    if application is not None:

        try:
            await application.bot.delete_webhook()

        except Exception:
            log.exception(
                "Could not delete webhook"
            )

        await application.stop()
        await application.shutdown()


def create_app():
    app_web = web.Application()

    app_web.router.add_get(
        "/",
        health,
    )

    app_web.router.add_post(
        "/telegram",
        telegram_webhook,
    )

    app_web.on_startup.append(
        on_startup
    )

    app_web.on_cleanup.append(
        on_cleanup
    )

    return app_web


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT,
    )
