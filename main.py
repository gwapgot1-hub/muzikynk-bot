import os, logging
from urllib.parse import quote_plus
import aiohttp, aiosqlite
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
PORT=int(os.getenv("PORT","10000"))
BASE_URL=os.getenv("BASE_URL","").rstrip("/")
DB=os.getenv("DB_PATH","music.db")
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("muzikynk")

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,query TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.commit()

async def save_search(uid,q):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO searches(user_id,query) VALUES(?,?)",(uid,q))
        await db.commit()

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти музыку",callback_data="search")],
        [InlineKeyboardButton("🔥 Популярное",callback_data="popular"),InlineKeyboardButton("🕘 История",callback_data="history")],
        [InlineKeyboardButton("❓ Помощь",callback_data="help")]
    ])

async def search_music(q):
    headers={"User-Agent":"MuzikYNK/1.0 Telegram bot"}
    params={"query":q,"fmt":"json","limit":8}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get("https://musicbrainz.org/ws/2/recording/",params=params,timeout=15) as r:
            data=await r.json()
    out=[]
    for x in data.get("recordings",[]):
        artists=", ".join(a.get("name","") for a in x.get("artist-credit",[]) if a.get("name"))
        rel=x.get("releases",[])
        out.append({"title":x.get("title","Без названия"),"artist":artists or "Неизвестный исполнитель","album":rel[0].get("title","") if rel else ""})
    return out

async def start(update,context):
    await update.message.reply_text("🎵 *MuzikYNK*\n\nНапиши название песни или исполнителя.\nНапример: `The Weeknd Blinding Lights`",parse_mode="Markdown",reply_markup=menu())

async def help_cmd(update,context):
    await update.message.reply_text("❓ Поиск выполняется по каталогу MusicBrainz. Для прослушивания используются официальные источники. Защищённые треки бот не скачивает.",reply_markup=menu())

async def text(update,context):
    q=update.message.text.strip()
    if not q or len(q)>150: return
    await save_search(update.effective_user.id,q)
    await update.message.reply_text("🔎 Ищу…")
    try: results=await search_music(q)
    except Exception:
        await update.message.reply_text("Не удалось выполнить поиск. Попробуй ещё раз.",reply_markup=menu()); return
    if not results:
        await update.message.reply_text("😕 Ничего не найдено.",reply_markup=menu()); return
    context.user_data["results"]=results
    buttons=[[InlineKeyboardButton(f"🎵 {x['title']} — {x['artist']}"[:60],callback_data=f"track:{i}")] for i,x in enumerate(results)]
    await update.message.reply_text(f"🎵 Найдено: {len(results)}\nВыбери трек:",reply_markup=InlineKeyboardMarkup(buttons))

async def callback(update,context):
    q=update.callback_query
    await q.answer()
    if q.data=="search":
        await q.message.reply_text("🔎 Напиши название песни или исполнителя:")
    elif q.data=="help":
        await help_cmd(update,context)
    elif q.data=="history":
        async with aiosqlite.connect(DB) as db:
            cur=await db.execute("SELECT query FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 10",(q.from_user.id,))
            rows=await cur.fetchall()
        await q.message.reply_text("🕘 История:\n\n"+("\n".join("• "+r[0] for r in rows) if rows else "Пусто."),reply_markup=menu())
    elif q.data=="popular":
        async with aiosqlite.connect(DB) as db:
            cur=await db.execute("SELECT query,COUNT(*) c FROM searches GROUP BY query ORDER BY c DESC LIMIT 10")
            rows=await cur.fetchall()
        await q.message.reply_text("🔥 Популярное:\n\n"+("\n".join(f"{i+1}. {r[0]}" for i,r in enumerate(rows)) if rows else "Пока пусто."),reply_markup=menu())
    elif q.data.startswith("track:"):
        i=int(q.data.split(":")[1]); results=context.user_data.get("results",[])
        if i>=len(results): return
        t=results[i]; s=f"{t['artist']} {t['title']}"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 YouTube",url="https://www.youtube.com/results?search_query="+quote_plus(s)),InlineKeyboardButton("☁️ SoundCloud",url="https://soundcloud.com/search?q="+quote_plus(s))],[InlineKeyboardButton("🔎 Новый поиск",callback_data="search")]])
        await q.message.reply_text(f"🎵 *{t['title']}*\n👤 {t['artist']}\n"+(f"💿 {t['album']}\n" if t["album"] else "")+"\nОфициальные источники:",parse_mode="Markdown",reply_markup=kb)

async def health(request): return web.Response(text="ok")
async def telegram(request):
    data=await request.json()
    await application.process_update(Update.de_json(data,application.bot))
    return web.Response(text="ok")

async def post_init(app):
    await init_db()
    if BASE_URL: await app.bot.set_webhook(BASE_URL+"/telegram")

if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
application=Application.builder().token(TOKEN).post_init(post_init).build()
application.add_handler(CommandHandler("start",start))
application.add_handler(CommandHandler("help",help_cmd))
application.add_handler(CallbackQueryHandler(callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
app=web.Application()
app.router.add_get("/",health)
app.router.add_post("/telegram",telegram)

if __name__=="__main__": web.run_app(app,port=PORT)
