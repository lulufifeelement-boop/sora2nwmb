import os
import re
import time
import asyncio
import tempfile
import threading
import requests
import http.server
import socketserver
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
    CommandHandler,
)
from telegram.error import Conflict


API_URL = "https://sorasave.questloops.com/api/video-info"
SORA_RE = re.compile(r"^https://sora\.chatgpt\.com/p/s_[\w-]+", re.IGNORECASE)

# Кнопки
BTN_NO_WM = "⬇️ Без вотермарки"
BTN_ORIG  = "⬇️ Оригинал"
BTN_NEW   = "🔁 Новая ссылка"
BTN_HELP  = "ℹ️ Помощь"

# Кэш ссылок на пользователя
CACHE: dict[int, dict] = {}
TTL_SEC = 10 * 60


def panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_NO_WM), KeyboardButton(BTN_ORIG)],
            [KeyboardButton(BTN_NEW), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True
    )


def help_text() -> str:
    return (
        "Как пользоваться:\n"
        "1) Пришли ссылку Sora вида: https://sora.chatgpt.com/p/s_...\n"
        "2) Нажми кнопку внизу: «Без вотермарки» или «Оригинал»\n"
        "3) Я скачаю и пришлю файлом\n\n"
        "Если долго — это CDN/сеть, иногда нужно чуть подождать."
    )


# Requests session
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://sorasave.questloops.com/",
    "Origin": "https://sorasave.questloops.com",
    "Accept": "*/*",
    "Connection": "keep-alive",
})


def cache_put(user_id: int, sora_url: str, hq: Optional[str], alt: Optional[str]):
    CACHE[user_id] = {"sora": sora_url, "hq": hq, "alt": alt, "ts": time.time()}


def cache_get(user_id: int) -> Optional[dict]:
    item = CACHE.get(user_id)
    if not item:
        return None
    if time.time() - item["ts"] > TTL_SEC:
        CACHE.pop(user_id, None)
        return None
    return item


def fetch_video_info(sora_url: str) -> dict:
    r = SESSION.post(API_URL, json={"url": sora_url}, timeout=40)
    r.raise_for_status()
    return r.json()


# -----------------------------
# Render health server (PORT)
# -----------------------------
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    with socketserver.TCPServer(("", port), _HealthHandler) as httpd:
        print(f"[health] listening on :{port}")
        httpd.serve_forever()


# -----------------------------
# Download helpers
# -----------------------------
def _fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return None


def _download_file_with_progress(url: str, progress: dict, cancel_event: threading.Event) -> str:
    progress.update({
        "downloaded": 0,
        "total": None,
        "done": False,
        "error": None,
        "path": None,
        "status": None,
    })

    backoffs = [0, 2, 5, 10]  # 4 попытки
    last_err = None

    for attempt, delay in enumerate(backoffs, start=1):
        if delay:
            time.sleep(delay)

        if cancel_event.is_set():
            progress["error"] = "cancelled"
            progress["done"] = True
            raise RuntimeError("cancelled")

        try:
            headers = {"Range": "bytes=0-"}  # помогает некоторым CDN
            with SESSION.get(url, stream=True, headers=headers, timeout=(30, 900), allow_redirects=True) as r:
                progress["status"] = r.status_code

                if r.status_code in (401, 403):
                    raise RuntimeError(f"HTTP {r.status_code} forbidden/unauthorized")
                if r.status_code == 404:
                    raise RuntimeError("HTTP 404 not found")
                if r.status_code == 429:
                    raise RuntimeError("HTTP 429 rate limited")
                if 500 <= r.status_code <= 599:
                    raise RuntimeError(f"HTTP {r.status_code} server error")

                r.raise_for_status()

                cl = r.headers.get("content-length")
                progress["total"] = _safe_int(cl)

                fd, path = tempfile.mkstemp(suffix=".mp4")
                try:
                    with os.fdopen(fd, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 512):
                            if cancel_event.is_set():
                                raise RuntimeError("cancelled")
                            if not chunk:
                                continue
                            f.write(chunk)
                            progress["downloaded"] += len(chunk)

                    progress["path"] = path
                    progress["done"] = True
                    return path

                except Exception:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    raise

        except Exception as e:
            last_err = e
            progress["error"] = f"{type(e).__name__}: {e}"
            print(f"[download] attempt {attempt}/{len(backoffs)} failed: {progress['error']}")
            continue

    progress["done"] = True
    raise RuntimeError(f"download failed after retries: {last_err}")


async def _progress_updater(msg, label: str, progress: dict):
    last_text = ""
    while not progress.get("done"):
        downloaded = int(progress.get("downloaded") or 0)
        total = progress.get("total")
        status = progress.get("status")

        if total and total > 0:
            pct = min(99, int(downloaded * 100 / total))
            text = f"⏳ Скачиваю «{label}»… {pct}% ({_fmt_mb(downloaded)} / {_fmt_mb(total)})"
        else:
            text = f"⏳ Скачиваю «{label}»… ({_fmt_mb(downloaded)})"

        if status:
            text += f"  [HTTP {status}]"

        if text != last_text:
            try:
                await msg.edit_text(text)
                last_text = text
            except Exception:
                pass

        await asyncio.sleep(1.2)


async def _download_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, label: str, filename: str):
    progress_msg = await update.message.reply_text(f"⏳ Скачиваю «{label}»…")
    progress = {}
    cancel_event = threading.Event()
    updater_task = asyncio.create_task(_progress_updater(progress_msg, label, progress))

    path = None
    try:
        path = await asyncio.to_thread(_download_file_with_progress, url, progress, cancel_event)

        try:
            await progress_msg.edit_text("📤 Загружено. Отправляю в Telegram…")
        except Exception:
            pass

        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=f"Готово ✅ «{label}» отправлено."
            )

    except Exception as e:
        print("[send] error:", repr(e))
        await update.message.reply_text(
            "Не получилось скачать видео (сеть/сервер временно тормозит).\n"
            "Попробуй ещё раз через 10–20 секунд.\n"
            "Если повторяется — нажми «Новая ссылка» и пришли ссылку заново."
        )
    finally:
        cancel_event.set()
        try:
            await updater_task
        except Exception:
            pass
        try:
            await progress_msg.delete()
        except Exception:
            pass
        if path:
            try:
                os.remove(path)
            except Exception:
                pass


# -----------------------------
# Telegram handlers
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), reply_markup=panel_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == BTN_HELP:
        await update.message.reply_text(help_text(), reply_markup=panel_keyboard())
        return

    if text == BTN_NEW:
        CACHE.pop(user_id, None)
        await update.message.reply_text("Ок ✅ Пришли новую ссылку Sora.", reply_markup=panel_keyboard())
        return

    if text in (BTN_NO_WM, BTN_ORIG):
        item = cache_get(user_id)
        if not item:
            await update.message.reply_text("Сначала пришли ссылку Sora.", reply_markup=panel_keyboard())
            return

        if text == BTN_NO_WM:
            url = item.get("alt")
            label = "Без вотермарки"
            filename = "sora_no_watermark.mp4"
        else:
            url = item.get("hq")
            label = "Оригинал"
            filename = "sora_original.mp4"

        if not url:
            await update.message.reply_text(
                "Для этого варианта ссылка не найдена. Пришли Sora-ссылку заново.",
                reply_markup=panel_keyboard()
            )
            return

        await _download_and_send(update, context, url, label, filename)
        return

    if SORA_RE.match(text):
        await update.message.reply_text("Принял ✅ Получаю ссылки…", reply_markup=panel_keyboard())
        try:
            data = fetch_video_info(text)
            cache_put(user_id, text, data.get("videoUrlHQ"), data.get("url"))

            item = cache_get(user_id)
            if not item or (not item.get("hq") and not item.get("alt")):
                await update.message.reply_text("Не нашёл ссылок в ответе API. Попробуй другую ссылку.",
                                                reply_markup=panel_keyboard())
                return

            await update.message.reply_text(
                "Готово ✅ Теперь нажми кнопку внизу: «Без вотермарки» или «Оригинал».",
                reply_markup=panel_keyboard()
            )

            try:
                await update.message.delete()
            except Exception:
                pass

        except Exception as e:
            print("[api] error:", repr(e))
            await update.message.reply_text("Не смог получить видео по этой ссылке. Попробуй ещё раз позже.",
                                            reply_markup=panel_keyboard())
        return

    await update.message.reply_text(
        "Пришли ссылку Sora (https://sora.chatgpt.com/p/s_...) или нажми кнопку внизу.",
        reply_markup=panel_keyboard()
    )


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной BOT_TOKEN")

    # 1) Render Web Service: обязательно открыть порт
    threading.Thread(target=start_health_server, daemon=True).start()

    # 2) FIX для Python 3.13/3.14: задаём event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("BOT STARTED ✅")

    # 3) Умный рестарт polling
    while True:
        try:
            app.run_polling()
        except Conflict as e:
            print("[polling] Conflict: another instance is polling. Waiting 60s...", repr(e))
            time.sleep(60)
        except Exception as e:
            print("[polling] crashed, restarting in 10s:", repr(e))
            time.sleep(10)


if __name__ == "__main__":
    main()
