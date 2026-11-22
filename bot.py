import asyncio, os, re, tempfile, shutil, logging, time
import yt_dlp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO)
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
API_ID = os.getenv("TG_API_ID", "").strip()
API_HASH = os.getenv("TG_API_HASH", "").strip()
USE_TELETHON = bool(API_ID and API_HASH)
URL_RE = re.compile(r"(?:https?://|(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/)\S+", re.I)
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
COOKIE_PATH = "/tmp/yt_cookies.txt"
lock = asyncio.Semaphore(1)  # one job at a time (queue)
tl_client = None

def init_cookies():
    raw = os.getenv("YT_COOKIES", "").strip()
    if raw and not os.path.exists(COOKIE_PATH):
        with open(COOKIE_PATH, "w", encoding="utf-8", newline="\n") as fp:
            if not raw.startswith("# Netscape"):
                fp.write("# Netscape HTTP Cookie File\n")
            fp.write(raw.replace("\r\n", "\n") + "\n")

def yt_service():
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def bar(p):
    p = max(0, min(100, p)); f = int(p // 5)
    return "[" + "\u2588" * f + "\u2591" * (20 - f) + f"] {p:.1f}%"

def fmt(n):
    return f"{n/1048576:.1f} MB" if n else "?"

def clean_title(t):
    t = re.sub(r"[\x00-\x1f<>]", " ", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:100] or "Video"

def upload(path, title, state):
    ext = os.path.splitext(path)[1].lower()
    mime = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
            ".mov": "video/quicktime", ".ts": "video/mp2t"}.get(ext, "video/mp4")
    body = {"snippet": {"title": clean_title(title), "description": "", "categoryId": "22"},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}}
    req = yt_service().videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(path, mimetype=mime, chunksize=32*1024*1024, resumable=True))
    resp = None
    t0 = time.time(); size = os.path.getsize(path)
    state["text"] = state["prefix"] + f"\u2b06\ufe0f Uploading to YouTube\n{bar(0)}\n0.0 MB / {fmt(size)}"
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            pr = status.progress()
            state["text"] = (state["prefix"] + f"\u2b06\ufe0f Uploading to YouTube\n{bar(pr*100)}\n"
                             f"{fmt(size*pr)} / {fmt(size)} \u2022 {size*pr/1048576/max(time.time()-t0,1):.1f} MB/s")
    return resp["id"]

def dir_size(d):
    t = 0
    for r, _, fs in os.walk(d):
        for f in fs:
            try: t += os.path.getsize(os.path.join(r, f))
            except OSError: pass
    return t

class TooBig(Exception):
    pass

def download_url_sync(url, d, state):
    init_cookies()
    def attempt(height, limit):
        def hook(h):
            if h["status"] == "downloading":
                if dir_size(d) > limit:
                    state["toobig"] = True
                    raise TooBig("size limit")
                total = h.get("total_bytes") or h.get("total_bytes_estimate") or 0
                done = h.get("downloaded_bytes", 0)
                if total:
                    pct = done * 100 / total
                else:
                    fi, fc = h.get("fragment_index"), h.get("fragment_count")
                    pct = (fi * 100 / fc) if fi and fc else 0
                spd = h.get("speed") or 0
                eta = h.get("eta")
                q = f"up to {height}p" if height else "best quality"
                state["text"] = (state["prefix"] + f"\u2b07\ufe0f Downloading ({q})\n{bar(pct)}\n"
                                 f"{fmt(done)}" + (f" / {fmt(total)}" if total else "") +
                                 f" \u2022 {spd/1048576:.1f} MB/s" + (f" \u2022 ETA {eta}s" if eta else ""))
            elif h["status"] == "finished":
                state["text"] = state["prefix"] + "\u2699\ufe0f Processing / merging..."
        f = f"bestvideo*[height<={height}]+bestaudio/best[height<={height}]" if height else "bestvideo*+bestaudio/best"
        opts = {"format": f, "merge_output_format": "mkv", "fixup": "never",
                "outtmpl": os.path.join(d, "video.%(ext)s"), "noplaylist": True,
                "progress_hooks": [hook], "quiet": True, "no_warnings": True,
                "noprogress": True, "concurrent_fragment_downloads": 16,
                "buffersize": 1024 * 1024, "retries": 10, "fragment_retries": 10}
        if os.path.exists(COOKIE_PATH):
            cf = os.path.join(d, "cookies.txt")
            shutil.copyfile(COOKIE_PATH, cf)
            opts["cookiefile"] = cf
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if opts.get("cookiefile") and os.path.exists(opts["cookiefile"]):
            try: shutil.copyfile(opts["cookiefile"], COOKIE_PATH)
            except OSError: pass
        return info

    for height in (None,):
        for f in os.listdir(d):
            try: os.remove(os.path.join(d, f))
            except OSError: pass
        state["toobig"] = False
        try:
            info = attempt(height, 10**13)
            ext_title = None
            if isinstance(info, dict):
                if "entries" in info and info["entries"]:
                    info = next((x for x in info["entries"] if x), info)
                if (info.get("extractor_key") or "").lower() != "generic":
                    ext_title = (info.get("title") or "").strip() or None
            cands = [os.path.join(d, f) for f in os.listdir(d)
                     if f.startswith("video.") and not f.endswith((".part", ".ytdl", ".temp"))]
            if cands:
                return max(cands, key=os.path.getsize), ext_title
            raise RuntimeError("Download failed: no output file")
        except Exception:
            if state.get("toobig"):
                state["text"] = state["prefix"] + "\u26a0\ufe0f Too big for server disk, retrying in lower quality..."
                continue
            raise
    raise RuntimeError("Video is too large for the server disk.")

async def updater(edit, state):
    last = ""
    while not state.get("stop"):
        t = state.get("text", "")
        if t and t != last:
            try:
                await edit(t); last = t
            except Exception:
                pass
        await asyncio.sleep(3)

ptb_bot = None
limit_hit = False
pending = []   # jobs waiting for resume after YouTube upload limit

class Job:
    def __init__(self, chat_id, msg_id, title, label, dl, custom_title=True):
        self.chat_id, self.msg_id, self.title, self.label, self.dl = chat_id, msg_id, title, label, dl
        self.custom_title = custom_title

    async def edit(self, text):
        await ptb_bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.msg_id)

def is_limit_error(e):
    return "uploadLimitExceeded" in str(e) or "exceeded the number of videos" in str(e)

async def run_job(job):
    """returns 'ok' | 'limit' | 'error'"""
    prefix = (job.label + "\n") if job.label else ""
    state = {"prefix": prefix, "text": prefix + "\u23f3 Starting..."}
    d = tempfile.mkdtemp()
    task = asyncio.create_task(updater(job.edit, state))
    try:
        res_dl = await job.dl(d, state)
        out, det_title = res_dl if isinstance(res_dl, tuple) else (res_dl, None)
        if det_title and not job.custom_title:
            job.title = det_title
            if job.label.startswith("["):
                job.label = f"{job.label.split(' ', 1)[0]} {det_title[:50]}"
                prefix = job.label + "\n"
                state["prefix"] = prefix
        state["text"] = prefix + f"\u2705 Downloaded ({fmt(os.path.getsize(out))}). Starting upload..."
        vid = await asyncio.to_thread(upload, out, job.title, state)
        state["stop"] = True; await task
        await job.edit(prefix + f"\u2705 Done (unlisted)\nhttps://youtu.be/{vid}")
        return "ok"
    except Exception as e:
        state["stop"] = True; await task
        if is_limit_error(e):
            await job.edit(prefix + "\u23f8 Paused: YouTube upload limit reached. Waiting for resume.")
            return "limit"
        await job.edit(prefix + f"\u274c Error: {e}"[:4000])
        return "error"
    finally:
        shutil.rmtree(d, ignore_errors=True)

def resume_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u25b6\ufe0f Resume", callback_data="resume")]])

async def execute(job):
    global limit_hit
    async with lock:
        if limit_hit:
            pending.append(job)
            try:
                await job.edit((job.label + "\n" if job.label else "") + "\u23f8 Waiting for resume (YouTube limit).")
            except Exception:
                pass
            return
        res = await run_job(job)
        if res == "limit":
            limit_hit = True
            pending.append(job)
            await ptb_bot.send_message(job.chat_id,
                "\u26a0\ufe0f YouTube upload limit reached.\nWait until the limit resets (usually up to 24 hours), then press Resume. "
                "Paused videos will download and upload again from where it stopped.",
                reply_markup=resume_markup())

async def on_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global limit_hit
    q = u.callback_query
    if not allowed(q.from_user.id):
        return await q.answer("Not allowed.", show_alert=True)
    if not limit_hit and not pending:
        await q.answer("Nothing to resume.")
        return
    limit_hit = False
    jobs = pending[:]; pending.clear()
    await q.answer("Resuming...")
    try:
        await q.edit_message_text(f"\u25b6\ufe0f Resumed {len(jobs)} video(s).")
    except Exception:
        pass
    for j in jobs:
        asyncio.create_task(execute(j))

def allowed(uid):
    return not ALLOWED or uid in ALLOWED

async def new_job(chat_id, title, label, dl, custom_title=True):
    pre = (label + "\n") if label else ""
    wait = lock.locked() or limit_hit
    m = await ptb_bot.send_message(chat_id, pre + ("\u23f3 In queue..." if wait else "\u23f3 Starting..."))
    asyncio.create_task(execute(Job(chat_id, m.message_id, title, label, dl, custom_title=custom_title)))

def guess_title(url):
    ym = YT_ID_RE.search(url)
    if ym:
        return "YouTube " + ym.group(1)
    parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
    g = parts[-2] if parts and parts[-1].endswith(".m3u8") and len(parts) > 1 else (parts[-1] if parts else "video")
    return "Video " + g[:20]

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Send one or more YouTube/m3u8/video links (one per line, optional title after the link), "
                               "or forward/send any video. I will download it in best quality and upload to YouTube as unlisted.")

async def handle_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id):
        return await u.message.reply_text("Not allowed.")
    text = u.message.text or ""
    if "# Netscape HTTP Cookie File" in text or ("\t__Secure-" in text and ".youtube.com" in text):
        with open(COOKIE_PATH, "w", encoding="utf-8", newline="\n") as fp:
            if not text.lstrip().startswith("# Netscape"):
                fp.write("# Netscape HTTP Cookie File\n")
            fp.write(text.replace("\r\n", "\n") + "\n")
        return await u.message.reply_text("\u2705 YouTube cookies updated.")
    items = []
    for line in text.splitlines():
        m = URL_RE.search(line)
        if m:
            raw_url = m.group(0)
            url = raw_url if raw_url.lower().startswith(("http://", "https://")) else "https://" + raw_url
            items.append((url, line.replace(raw_url, "").strip()))
    n = len(items)
    for i, (url, t) in enumerate(items, 1):
        has_custom = bool(t)
        title = t or guess_title(url)
        label = f"[{i}/{n}] {title[:50]}" if n > 1 else ""
        async def dl(d, state, url=url):
            return await asyncio.to_thread(download_url_sync, url, d, state)
        await new_job(u.effective_chat.id, title, label, dl, custom_title=has_custom)

async def handle_media(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if USE_TELETHON:
        return  # Telethon handler supports big files
    if not allowed(u.effective_user.id):
        return await u.message.reply_text("Not allowed.")
    m = u.message
    att = m.video or m.document or m.animation
    title = (m.caption or getattr(att, "file_name", None) or "Telegram video").rsplit(".", 1)[0]
    if att.file_size and att.file_size > 20 * 1024 * 1024:
        return await m.reply_text("File is bigger than 20 MB. Set TG_API_ID and TG_API_HASH to enable big files.")
    async def dl(d, state):
        f = await att.get_file()
        p = os.path.join(d, "video.mp4")
        await f.download_to_drive(p)
        return p
    await new_job(u.effective_chat.id, title, "", dl)

async def setup_telethon():
    global tl_client
    from telethon import TelegramClient, events, tl
    tl_client = TelegramClient("/tmp/tlbot", int(API_ID), API_HASH)
    await tl_client.start(bot_token=os.environ["BOT_TOKEN"])

    @tl_client.on(events.NewMessage(incoming=True, func=lambda e: isinstance(e.message.media, tl.types.MessageMediaDocument) and (e.video or e.document or e.gif)))
    async def on_media(e):
        if not allowed(e.sender_id):
            return await e.reply("Not allowed.")
        mime = (e.document.mime_type if e.document else "") or ""
        fname = (e.file.name if e.file and e.file.name else "") or ""
        if fname.lower().endswith(".txt") and "cookie" in fname.lower():
            await tl_client.download_media(e.message, file=COOKIE_PATH)
            return await e.reply("\u2705 YouTube cookies file updated.")
        if e.document and not (mime.startswith("video") or e.video):
            return
        name = fname.rsplit(".", 1)[0] if fname else ""
        title = e.raw_text or name or "Telegram video"
        async def dl(d, state):
            p = os.path.join(d, "video.mp4")
            def cb(cur, tot):
                state["text"] = (state["prefix"] + f"\u2b07\ufe0f Downloading from Telegram\n{bar(cur*100/tot if tot else 0)}\n"
                                 f"{fmt(cur)} / {fmt(tot)}")
            await tl_client.download_media(e.message, file=p, progress_callback=cb)
            return p
        await new_job(e.chat_id, title, "", dl)

async def health(_): return web.Response(text="ok")

async def main():
    global ptb_bot
    init_cookies()
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    ptb_bot = app.bot
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_resume, pattern="^resume$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.ANIMATION, handle_media))
    web_app = web.Application(); web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    if USE_TELETHON:
        await setup_telethon()
    async with app:
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
