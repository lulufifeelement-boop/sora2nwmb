import os
import re
import time
import asyncio
import tempfile
import threading
import requests

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
    CommandHandler,
)

API_URL = "https://sorasave.questloops.com/api/video-info"
SORA_RE = re.compile(r"^https://sora\.chatgpt\.com/p/s_[\w-]+", re.IGNORECASE)

# Кнопки (панель над вводом)
BTN_NO_WM = "⬇️ Без вотермарки"
BTN_ORIG  = "⬇️ Оригинал"
BTN_NEW   = "🔁 Новая ссылка"
BTN_HELP  = "ℹ️ Помощь"

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
        "2) Нажми внизу кнопку: «Без вотермарки» или «Оригинал»\n"
        "3) Я скачаю и пришлю файлом\n\n"
        "Если долго — это из-за CDN, иногда надо чуть подождать."
    )

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://sorasave.questloops.com/",
    "Origin": "https://sorasave.questloops.com",
})

# user_id -> {"hq": url, "alt": url, "ts": epoch}
CACHE: dict[int, dict] = {}
TTL_SEC = 10 * 60

def cache_put(user_id: int, hq: str | None, alt: str | None):
    CACHE[user_id] = {"hq": hq, "alt": alt, "ts": time.time()}

def cache_get(user_id: int) -> dict | None:
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

def _fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f} MB"

def _download_file_with_progress(url: str, progress: dict, cancel_event: threading.Event) -> str:
    """
    Скачивает в temp. progress:
      downloaded (bytes), total (bytes|None), done (bool), error (str|None), path (str|None)
    """
    progress["downloaded"] = 0
    progress["total"] = None
    progress["done"] = False
    progress["error"] = None
    progress["path"] = None

    try:
        # важное: timeout=(connect, read). read ставим большим.
        with SESSION.get(url, stream=True, timeout=(20, 300)) as r:
            r.raise_for_status()
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                progress["total"] = int(cl)

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
        progress["error"] = str(e)
        progress["done"] = True
        raise

async def _progress_updater(msg, label: str, progress: dict):
    """
    Обновляем прогресс в сообщении, но без “убийства” загрузки по таймеру.
    """
    last_text = ""
    while not progress.get("done"):
        downloaded = int(progress.get("downloaded") or 0)
        total = progress.get("total")

        if total and total > 0:
            pct = min(99, int(downloaded * 100 / total))
            text = f"⏳ Скачиваю «{label}»… {pct}%  ({_fmt_mb(downloaded)} / {_fmt_mb(total)})"
        else:
            text = f"⏳ Скачиваю «{label}»… ({_fmt_mb(downloaded)})"

        if text != last_text:
            try:
                await msg.edit_text(text)
                last_text = text
            except Exception:
                pass

        await asyncio.sleep(1.2)

async def _download_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, label: str, filename: str):
    """
    Скачиваем в отдельном потоке, шлём как video.
    1 автоповтор при таймауте.
    Ошибку показываем аккуратно, без возврата исходной ссылки.
    """
    progress_msg = await update.message.reply_text(f"⏳ Скачиваю «{label}»…")
    progress = {}
    cancel_event = threading.Event()
    updater_task = asyncio.create_task(_progress_updater(progress_msg, label, progress))

    path = None
    try:
        # 1-я попытка
        try:
            path = await asyncio.to_thread(_download_file_with_progress, url, progress, cancel_event)
        except Exception as e1:
            # 2-я попытка только если похоже на таймаут
            if "timed out" in str(e1).lower() or "timeout" in str(e1).lower():
                # сообщаем мягко
                try:
                    await progress_msg.edit_text("⏳ Сеть притормозила… пробую ещё раз.")
                except Exception:
                    pass

                # сброс прогресса
                progress["done"] = False
                progress["error"] = None
                progress["downloaded"] = 0
                progress["total"] = None

                path = await asyncio.to_thread(_download_file_with_progress, url, progress, cancel_event)
            else:
                raise

        # загрузка завершилась
        try:
            await progress_msg.edit_text("📤 Загружено. Отправляю в Telegram…")
        except Exception:
            pass

        with open(path, "rb") as f:
            await update.message.reply_video(video=f, caption=f"Готово ✅ «{label}» отправлено.")

    except Exception:
        # Не возвращаем ссылку и не показываем “страшную” тех.ошибку
        await update.message.reply_text(
            "Не получилось скачать видео (сеть/сервер временно тормозит).\n"
            "Попробуй нажать кнопку ещё раз через 10–20 секунд."
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), reply_markup=panel_keyboard())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # Кнопки меню (панель)
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
            await update.message.reply_text("Для этого варианта ссылка не найдена. Пришли Sora-ссылку заново.")
            return

        await _download_and_send(update, context, url, label, filename)
        return

    # Если это ссылка Sora — обрабатываем
    if SORA_RE.match(text):
        await update.message.reply_text("Принял ✅ Получаю ссылки…", reply_markup=panel_keyboard())
        try:
            data = fetch_video_info(text)
            cache_put(user_id, data.get("videoUrlHQ"), data.get("url"))

            item = cache_get(user_id)
            if not item or (not item.get("hq") and not item.get("alt")):
                await update.message.reply_text("Не нашёл ссылок в ответе API. Попробуй другую ссылку.")
                return

            await update.message.reply_text("Готово ✅ Теперь нажми кнопку внизу: «Без вотермарки» или «Оригинал».",
                                           reply_markup=panel_keyboard())

            # Удаляем сообщение пользователя со ссылкой (если есть права)
            try:
                await update.message.delete()
            except Exception:
                pass

        except Exception:
            await update.message.reply_text("Не смог получить видео по этой ссылке. Попробуй ещё раз позже.")
        return

    # Любой другой текст
    await update.message.reply_text("Пришли ссылку Sora (https://sora.chatgpt.com/p/s_...) или нажми кнопку внизу.",
                                    reply_markup=panel_keyboard())

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()
