import os
import logging
import tempfile
from urllib.parse import quote_plus
from html import escape

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
            CREATE TABLE IF NOT EXISTS searches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                artist TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def save_search(user_id, query):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO searches(user_id, query) VALUES(?, ?)",
            (user_id, query),
        )
        await db.commit()


async def save_track(user_id, title, artist, file_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO tracks(user_id, title, artist, file_id) VALUES(?,?,?,?)",
            (user_id, title, artist, file_id),
        )
        await db.commit()


def menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Найти музыку", callback_data="search")],
            [InlineKeyboardButton("📤 Добавить свою музыку", callback_data="upload")],
            [InlineKeyboardButton("🔥 Популярное", callback_data="popular")],
            [InlineKeyboardButton("🕘 История", callback_data="history")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        ]
    )


def web_result_button(index):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Получить аудио", callback_data=f"ia:{index}")],
            [InlineKeyboardButton("🔎 Новый поиск", callback_data="search")],
        ]
    )


def local_result_button(track_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Слушать в Telegram", callback_data=f"local:{track_id}")],
            [InlineKeyboardButton("🔎 Новый поиск", callback_data="search")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_upload", None)
    context.user_data.pop("ia_results", None)

    await update.message.reply_text(
        "🎵 <b>МузыкаYNK</b>\n\n"
        "Ищу музыку и могу прислать разрешённое аудио прямо сюда в Telegram.\n\n"
        "Напиши название песни или исполнителя:",
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Как пользоваться</b>\n\n"
        "• Напиши исполнителя или название песни.\n"
        "• Если трек есть в открытом аудиокаталоге с разрешённым использованием, "
        "я предложу отправить его прямо в Telegram.\n"
        "• Можно также отправить боту свой MP3/аудиофайл — он сохранится в твоей "
        "личной библиотеке, и потом его можно слушать через поиск.\n\n"
        "⚠️ Бот не скачивает защищённые коммерческие треки из YouTube, Spotify "
        "или SoundCloud в обход правил этих сервисов."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=menu(),
        )
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=menu(),
        )


async def request_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_upload"] = True

    await update.callback_query.message.reply_text(
        "📤 Пришли мне MP3 или другой аудиофайл.\n\n"
        "Я сохраню его в твоей личной библиотеке и смогу потом присылать "
        "его прямо в Telegram.",
        reply_markup=menu(),
    )


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio
    document = update.message.document

    if audio:
        file_id = audio.file_id
        title = audio.title or (audio.file_name or "Без названия")
        artist = audio.performer or "Неизвестный исполнитель"

    elif document and document.mime_type and document.mime_type.startswith("audio/"):
        file_id = document.file_id
        title = document.file_name or "Без названия"
        artist = "Неизвестный исполнитель"

    else:
        return

    await save_track(
        update.effective_user.id,
        title,
        artist,
        file_id,
    )

    context.user_data["waiting_upload"] = False

    await update.message.reply_text(
        f"✅ Сохранил:\n\n"
        f"🎵 <b>{escape(title)}</b>\n"
        f"👤 {escape(artist)}\n\n"
        "Теперь можешь написать название трека — я попробую найти его "
        "в твоей библиотеке.",
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def find_local(user_id, query):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            SELECT id, title, artist, file_id
            FROM tracks
            WHERE user_id = ?
              AND (
                    lower(title) LIKE lower(?)
                 OR lower(artist) LIKE lower(?)
              )
            ORDER BY id DESC
            LIMIT 5
            """,
            (
                user_id,
                f"%{query}%",
                f"%{query}%",
            ),
        )

        return await cursor.fetchall()


async def search_archive(query):
    # Ищем только аудио с явно указанной
    # Creative Commons / Public Domain лицензией.
    url = "https://archive.org/advancedsearch.php"

    params = {
        "q": f'mediatype:audio AND ({query})',
        "fl[]": ["identifier", "title", "creator"],
        "rows": "8",
        "page": "1",
        "output": "json",
    }

    headers = {
        "User-Agent": "MuzikYNK/1.0 (Telegram music bot)"
    }

    results = []

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:

        async with session.get(
            url,
            params=params,
        ) as resp:

            if resp.status != 200:
                raise RuntimeError(
                    f"Archive search HTTP {resp.status}"
                )

            data = await resp.json()

        docs = data.get(
            "response",
            {},
        ).get(
            "docs",
            [],
        )

        for doc in docs:
            identifier = doc.get("identifier")

            if not identifier:
                continue

            meta_url = (
                f"https://archive.org/metadata/{identifier}"
            )

            async with session.get(meta_url) as meta_resp:

                if meta_resp.status != 200:
                    continue

                meta = await meta_resp.json()

            md = meta.get(
                "metadata",
                {},
            )

            license_text = " ".join(
                str(md.get(k, ""))
                for k in (
                    "licenseurl",
                    "license",
                    "rights",
                )
            ).lower()

            allowed = (
                "creativecommons.org" in license_text
                or "public domain" in license_text
                or "publicdomain" in license_text
            )

            if not allowed:
                continue

            files = meta.get(
                "files",
                [],
            )

            candidates = []

            for f in files:
                name = str(
                    f.get("name", "")
                )

                fmt = str(
                    f.get("format", "")
                ).lower()

                size_raw = str(
                    f.get("size", "0")
                )

                if size_raw.isdigit():
                    size = int(size_raw)
                else:
                    size = 0

                if (
                    name.lower().endswith(
                        (
                            ".mp3",
                            ".m4a",
                            ".ogg",
                            ".flac",
                            ".wav",
                        )
                    )
                    and size <= 20 * 1024 * 1024
                ):
                    candidates.append(
                        (
                            name,
                            size,
                            fmt,
                        )
                    )

            if not candidates:
                continue

            name, size, fmt = candidates[0]

            title = str(
                md.get("title")
                or doc.get("title")
                or identifier
            )

            creator = str(
                md.get("creator")
                or doc.get("creator")
                or "Unknown"
            )

            if isinstance(creator, list):
                creator = ", ".join(
                    map(str, creator)
                )

            results.append(
                {
                    "identifier": identifier,
                    "title": title,
                    "artist": creator,
                    "file": name,
                    "size": size,
                    "license": license_text[:300],
                }
            )

            if len(results) >= 5:
                break

    return results


async def send_archive_track(
    update,
    context,
    index,
):
    results = context.user_data.get(
        "ia_results"
    ) or []

    if index < 0 or index >= len(results):
        await update.callback_query.message.reply_text(
            "⚠️ Этот результат уже недоступен. Сделай новый поиск.",
            reply_markup=menu(),
        )
        return

    item = results[index]

    identifier = item["identifier"]
    filename = item["file"]

    url = (
        "https://archive.org/download/"
        f"{quote_plus(identifier)}/"
        f"{quote_plus(filename)}"
    )

    safe_name = (
        os.path.basename(filename)
        or "track.audio"
    )

    temp_path = os.path.join(
        tempfile.gettempdir(),
        f"muzikynk_"
        f"{update.effective_user.id}_"
        f"{index}_"
        f"{safe_name}",
    )

    await update.callback_query.message.reply_text(
        "⬇️ Получаю аудио..."
    )

    timeout = aiohttp.ClientTimeout(
        total=120
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                allow_redirects=True,
            ) as resp:

                if resp.status != 200:
                    raise RuntimeError(
                        f"download HTTP {resp.status}"
                    )

                data = await resp.read()

        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError(
                "file too large"
            )

        with open(
            temp_path,
            "wb",
        ) as f:
            f.write(data)

        with open(
            temp_path,
            "rb",
        ) as audio_file:

            await update.callback_query.message.reply_audio(
                audio=audio_file,
                title=item["title"][:64],
                performer=item["artist"][:64],
                caption=(
                    f"🎵 <b>{escape(item['title'])}</b>\n"
                    f"👤 {escape(item['artist'])}\n"
                    f"📚 Источник: Internet Archive\n"
                    f"🔓 Лицензия указана в метаданных источника."
                ),
                parse_mode="HTML",
            )

    except Exception:
        log.exception(
            "Could not download archive audio"
        )

        await update.callback_query.message.reply_text(
            "⚠️ Не получилось получить этот аудиофайл. "
            "Попробуй другой результат.",
            reply_markup=menu(),
        )

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


async def search_music(
    update,
    context,
):
    if not update.message:
        return

    if not update.message.text:
        return

    query = update.message.text.strip()

    if not query:
        return

    await save_search(
        update.effective_user.id,
        query,
    )

    await update.message.reply_text(
        "🔎 Ищу музыку..."
    )

    # Сначала ищем в личной библиотеке пользователя.
    local = await find_local(
        update.effective_user.id,
        query,
    )

    for (
        track_id,
        title,
        artist,
        file_id,
    ) in local:

        await update.message.reply_text(
            f"🎵 <b>{escape(title)}</b>\n"
            f"👤 {escape(artist)}\n\n"
            "Трек уже есть в твоей библиотеке:",
            parse_mode="HTML",
            reply_markup=local_result_button(
                track_id
            ),
        )

    try:
        archive_results = await search_archive(
            query
        )

    except Exception:
        log.exception(
            "Internet Archive search failed"
        )

        archive_results = []

    if archive_results:

        context.user_data["ia_results"] = (
            archive_results
        )

        await update.message.reply_text(
            "🎧 Нашёл аудио, для которого источник "
            "указывает Creative Commons или public domain:",
            reply_markup=menu(),
        )

        for i, item in enumerate(
            archive_results
        ):

            await update.message.reply_text(
                f"🎵 <b>{escape(item['title'])}</b>\n"
                f"👤 <b>{escape(item['artist'])}</b>\n"
                f"📦 {item['size'] / 1024 / 1024:.1f} MB",
                parse_mode="HTML",
                reply_markup=web_result_button(
                    i
                ),
            )

    elif not local:

        await update.message.reply_text(
            "😔 Не нашёл подходящего разрешённого аудиофайла.\n\n"
            "Попробуй другой запрос или пришли свой MP3 — "
            "я сохраню его в твоей библиотеке.",
            reply_markup=menu(),
        )


async def send_local(
    update,
    context,
    track_id,
):
    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT title, artist, file_id
            FROM tracks
            WHERE id = ?
            AND user_id = ?
            """,
            (
                track_id,
                update.effective_user.id,
            ),
        )

        row = await cursor.fetchone()

    if not row:
        await update.callback_query.message.reply_text(
            "⚠️ Трек не найден.",
            reply_markup=menu(),
        )
        return

    title, artist, file_id = row

    await update.callback_query.message.reply_audio(
        audio=file_id,
        title=title[:64],
        performer=artist[:64],
        caption=(
            f"🎵 <b>{escape(title)}</b>\n"
            f"👤 {escape(artist)}"
        ),
        parse_mode="HTML",
    )


async def popular(
    update,
    context,
):
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
        text = (
            "🔥 Пока нет популярного поиска."
        )

    else:
        text = (
            "🔥 <b>Популярное</b>\n\n"
        )

        for i, (
            query,
            count,
        ) in enumerate(
            rows,
            1,
        ):
            text += (
                f"{i}. {escape(query)} "
                f"— {count} раз\n"
            )

    await update.callback_query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def history(
    update,
    context,
):
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
            (
                update.effective_user.id,
            ),
        )

        rows = await cursor.fetchall()

    if not rows:
        text = (
            "🕘 История пока пустая."
        )

    else:
        text = (
            "🕘 <b>Последние поиски</b>\n\n"
        )

        for i, (
            query,
        ) in enumerate(
            rows,
            1,
        ):
            text += (
                f"{i}. {escape(query)}\n"
            )

    await update.callback_query.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def callback_handler(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if query.data == "search":

        await query.message.reply_text(
            "🔎 Напиши название песни или исполнителя:"
        )

    elif query.data == "upload":

        await request_upload(
            update,
            context,
        )

    elif query.data == "help":

        await help_cmd(
            update,
            context,
        )

    elif query.data == "popular":

        await popular(
            update,
            context,
        )

    elif query.data == "history":

        await history(
            update,
            context,
        )

    elif query.data.startswith("ia:"):

        index = int(
            query.data.split(
                ":",
                1,
            )[1]
        )

        await send_archive_track(
            update,
            context,
            index,
        )

    elif query.data.startswith("local:"):

        track_id = int(
            query.data.split(
                ":",
                1,
            )[1]
        )

        await send_local(
            update,
            context,
            track_id,
        )


async def health(request):
    return web.Response(
        text="ok"
    )


async def telegram_webhook(request):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            application.bot,
        )

        await application.process_update(
            update
        )

        return web.Response(
            text="ok"
        )

    except Exception:
        log.exception(
            "Webhook update processing failed"
        )

        return web.Response(
            status=500,
            text="error",
        )


async def on_startup(app_web):
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
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_cmd,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.Document.AUDIO,
            receive_audio,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            search_music,
        )
    )

    await application.initialize()
    await application.start()

    webhook_url = (
        f"{BASE_URL}/telegram"
    )

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


async def on_cleanup(app_web):
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
