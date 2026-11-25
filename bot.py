"""
TajStudio — Telegram-бот + Mini App: сценарий -> мультфильм (Blender) -> видео в чат.

Всё, что умеет бот в чате, умеет и Mini App (общая логика, HTTP API внизу файла):
  • загрузка персонажа: имя + предпросмотр, прогресс в процентах (и в чате, и в Mini App)
  • удаление персонажей
  • озвучка на выбор: Piper (бесплатно, на сервере) или ElevenLabs
  • предпросмотр / рендер со статусом в процентах, кнопка «Скачать» (видео придёт в чат)

Запуск:  python bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
import firebase_admin
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from firebase_admin import credentials, db

import tts
from prepare_character import prepare

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cartoon")

# ───────────────────────── настройки (env на Bothost) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
FIREBASE_CRED_JSON = os.environ["FIREBASE_CRED_JSON"]      # содержимое service-account.json
FIREBASE_DB_URL = os.environ["FIREBASE_DB_URL"]
YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")
ELEVEN_VOICES = json.loads(os.environ.get("ELEVEN_VOICES", "{}"))  # старый формат {"Маша": "voice_id"}
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WORK = Path(os.environ.get("WORK_DIR", "/tmp/cartoon"))
WORK.mkdir(parents=True, exist_ok=True)
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")   # https-ссылка на Mini App (домен Bothost, где отдаётся index.html)
MAX_RENDERS = int(os.environ.get("MAX_RENDERS", "1"))   # сколько рендеров одновременно
MAX_UPLOAD = 25 * 1024 * 1024
TG_LIMIT = 49 * 1024 * 1024                              # Telegram: боты шлют файлы до 50 МБ
FPS = 24
MENU_TITLE = "TajStudio"


async def set_menu_button(bot: Bot, chat_id: int | None = None) -> None:
    """Кнопка TajStudio слева от поля ввода — открывает мини-апп."""
    if not MINIAPP_URL:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text=MENU_TITLE, web_app=WebAppInfo(url=MINIAPP_URL)))
    except Exception:
        log.exception("set_chat_menu_button failed")


firebase_admin.initialize_app(
    credentials.Certificate(json.loads(FIREBASE_CRED_JSON)),
    {"databaseURL": FIREBASE_DB_URL},
)

CHARACTER_PROMPT = (
    "Нарисуй этого человека как 2D-персонажа в стиле классической студии Дисней: "
    "полный рост, прямо, лицом в камеру, руки слегка в стороны, ноги расставлены, "
    "рот закрыт, глаза открыты, нейтральное выражение, чистый однотонный фон без теней, "
    "чёткий контур, высокое разрешение, PNG."
)

STYLES = {
    "classic": "Классика Disney (мягкие контуры, тёплая палитра)",
    "modern": "Современный Disney (объёмный свет, глубокие цвета)",
    "storybook": "Книжная иллюстрация (акварельные фоны)",
}
STYLE_ALIASES = {"water": "storybook", "noir": "modern", "rose": "classic"}   # стили Mini App -> стили Blender
ENGINE_TITLES = {"piper": "Piper (бесплатно, на сервере)", "eleven": "ElevenLabs"}


class Flow(StatesGroup):
    waiting_character = State()
    waiting_name = State()
    waiting_script = State()
    choosing_style = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Новый мультфильм", callback_data="new")],
        [InlineKeyboardButton(text="🧍 Мои персонажи", callback_data="chars")],
        [InlineKeyboardButton(text="🎙 Озвучка", callback_data="voice")],
        [InlineKeyboardButton(text="📁 Мои проекты", callback_data="projects")],
        [InlineKeyboardButton(text="📋 Промпт для ChatGPT", callback_data="prompt")],
    ])


router = Router()
BG_TASKS: set = set()
JOBS: dict[str, dict] = {}          # живой прогресс: "uid/c/имя" (персонаж), "uid/p/id" (проект)
RENDER_SEM = asyncio.Semaphore(MAX_RENDERS)


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


def job_set(key: str, **kw) -> None:
    JOBS.setdefault(key, {"ts": time.time()}).update(kw, upd=time.time())


def bar(pct: int) -> str:
    n = max(0, min(10, round(pct / 10)))
    return "▰" * n + "▱" * (10 - n) + f" {pct}%"


def cid(name: str) -> str:
    """Короткий id персонажа для callback_data (лимит Telegram 64 байта)."""
    return hashlib.md5(name.encode()).hexdigest()[:8]


def clean_name(raw: str) -> str:
    name = re.sub(r"[.$#\[\]/\\:\n\r\t]", " ", raw or "")      # запрещено в ключах Firebase / именах файлов / сценарии
    return re.sub(r"\s+", " ", name).strip()[:30]


def find_char(pool: dict, who: str):
    """Ищет персонажа без учёта регистра. Возвращает (имя, запись) или (None, None)."""
    if who in pool:
        return who, pool[who]
    low = who.lower()
    for n, v in pool.items():
        if n.lower() == low:
            return n, v
    return None, None


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:
        pass        # «message is not modified», лимит правок и т.п. — не критично


# ───────────────────────────── Firebase ─────────────────────────────
def fb(path: str):
    return db.reference(path)


async def fb_get(path):
    return await asyncio.to_thread(lambda: fb(path).get())


async def fb_set(path, value):
    await asyncio.to_thread(lambda: fb(path).set(value))


async def fb_update(path, value):
    await asyncio.to_thread(lambda: fb(path).update(value))


async def fb_delete(path):
    await asyncio.to_thread(lambda: fb(path).delete())


# ───────────────────────────── Яндекс Диск ─────────────────────────────
YD = "https://cloud-api.yandex.net/v1/disk/resources"


async def yadisk_upload(local: Path, remote: str) -> str | None:
    """Грузит файл на Яндекс Диск, возвращает путь. Без токена — молча пропускает."""
    if not YADISK_TOKEN:
        return None
    async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
        cur = ""
        for p in remote.strip("/").split("/")[:-1]:
            cur += "/" + p
            await s.put(YD, params={"path": cur})
        async with s.get(f"{YD}/upload", params={"path": remote, "overwrite": "true"}) as r:
            url = (await r.json()).get("href")
        if not url:
            return None
        async with s.put(url, data=local.read_bytes()):
            pass
    return remote


async def yadisk_download(remote: str, local: Path) -> bool:
    if not YADISK_TOKEN or not remote:
        return False
    local.parent.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
        async with s.get(f"{YD}/download", params={"path": remote}) as r:
            href = (await r.json()).get("href")
        if not href:
            return False
        async with s.get(href) as r:
            if r.status != 200:
                return False
            local.write_bytes(await r.read())
    return True


async def yadisk_delete(remote: str | None) -> None:
    if not YADISK_TOKEN or not remote:
        return
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"OAuth {YADISK_TOKEN}"}) as s:
            await s.delete(YD, params={"path": remote, "permanently": "true"})
    except Exception:
        log.exception("yadisk delete failed")


# ───────────────────────────── сценарий ─────────────────────────────
def parse_script(text: str) -> list[dict]:
    """
    [Сцена: лес, утро]
    Маша: Привет, Миша!
    Миша: (радостно) Привет!
    """
    scenes, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[?\s*сцена\s*[:\-–]\s*(.+?)\]?$", line, re.I)
        if m:
            cur = {"setting": m.group(1), "lines": []}
            scenes.append(cur)
            continue
        m = re.match(r"^([^:]{1,30}):\s*(?:\(([^)]*)\))?\s*(.+)$", line)
        if m:
            if cur is None:
                cur = {"setting": "студия", "lines": []}
                scenes.append(cur)
            cur["lines"].append({"who": m.group(1).strip(),
                                 "emotion": (m.group(2) or "neutral").strip(),
                                 "text": m.group(3).strip()})
    return scenes


# ───────────────────────────── настройки озвучки ─────────────────────────────
async def get_tts_settings(uid: str) -> dict:
    s = await fb_get(f"clients/{uid}/settings") or {}
    engine = s.get("engine") if s.get("engine") in ("piper", "eleven") else "piper"
    return {"engine": engine, "voice": tts.resolve_voice(engine, s.get("voice", ""))}


async def save_tts_settings(uid: str, engine: str, voice: str) -> None:
    await fb_update(f"clients/{uid}/settings", {"engine": engine, "voice": voice})


# ───────────────────────────── персонажи ─────────────────────────────
def files_ok(ch: dict) -> bool:
    paths = [ch.get("file")] + list((ch.get("meta") or {}).get("faces", []))
    return all(p and Path(p).exists() for p in paths)


async def ensure_character(uid: str, name: str, ch: dict) -> dict:
    """Если после перезапуска сервера файлы персонажа пропали — восстанавливает с Яндекс Диска."""
    if files_ok(ch):
        return ch
    orig = WORK / uid / "characters" / f"{name}.png"
    if not (await yadisk_download(ch.get("disk"), orig)):
        raise RuntimeError(f"Файлы персонажа «{name}» потеряны. Загрузите его заново.")
    meta = await asyncio.to_thread(prepare, orig, orig.parent / name)
    return {**ch, "file": meta["body"], "thumb": meta.get("thumb"), "meta": meta}


async def create_character(uid: str, name: str, orig: Path, to_library: bool = False) -> bool:
    """Обработка персонажа с прогрессом в JOBS. Не бросает исключений: итог — в JOBS[key]['status']."""
    key = f"{uid}/c/{name}"
    job_set(key, kind="char", name=name, pct=5, text="Файл получен", status="processing", error="")
    try:
        old = await fb_get(f"clients/{uid}/characters/{name}") or {}
        shutil.rmtree(orig.parent / name, ignore_errors=True)
        cb = lambda p, t="": job_set(key, pct=10 + int(p * 0.75), text=t)       # 10..85 %
        meta = await asyncio.to_thread(prepare, orig, orig.parent / name, cb)
        job_set(key, pct=88, text="Сохраняю на Яндекс Диск")
        try:
            remote = await yadisk_upload(orig, f"/CartoonStudio/{uid}/characters/{name}.png")
        except Exception:
            log.exception("yadisk upload failed")
            remote = None
        job_set(key, pct=96, text="Записываю в базу")
        rec = {"id": cid(name), "file": meta["body"], "thumb": meta.get("thumb"), "disk": remote,
               "voice": old.get("voice", ""), "meta": meta, "created": int(time.time())}
        await fb_set(f"clients/{uid}/characters/{name}", rec)
        if to_library:
            await fb_set(f"library/characters/{name}", {**rec, "owner": uid})
        job_set(key, pct=100, text="Готово", status="done")
        return True
    except Exception as e:
        log.exception("create_character failed")
        msg = "Не удалось прочитать картинку. Пришлите PNG/JPG." if "cannot identify" in str(e) else str(e)[:300]
        job_set(key, status="error", error=msg, text="Ошибка")
        return False


async def delete_character(uid: str, name: str, library: bool = False) -> bool:
    JOBS.pop(f"{uid}/c/{name}", None)
    if library:
        if int(uid) not in ADMIN_IDS:
            return False
        await fb_delete(f"library/characters/{name}")
        return True
    rec = await fb_get(f"clients/{uid}/characters/{name}")
    await fb_delete(f"clients/{uid}/characters/{name}")
    if int(uid) in ADMIN_IDS:
        lib = await fb_get(f"library/characters/{name}")
        if lib and lib.get("owner") == uid:
            await fb_delete(f"library/characters/{name}")
    cdir = WORK / uid / "characters"
    shutil.rmtree(cdir / name, ignore_errors=True)
    (cdir / f"{name}.png").unlink(missing_ok=True)
    if rec:
        await yadisk_delete(rec.get("disk"))
    return True


# ───────────────────────────── рендер ─────────────────────────────
FRA_RE = re.compile(r"Fra:(\d+)")


async def run_blender(scene_json: Path, out_mp4: Path, preview: bool, total: int, on_pct) -> None:
    worker = Path(__file__).with_name("blender_worker.py")
    cmd = [BLENDER_BIN, "-b", "-P", str(worker), "--", "--scene", str(scene_json), "--out", str(out_mp4)]
    if preview:
        cmd.append("--preview")
    if shutil.which("stdbuf"):                       # чтобы строки «Fra:N» приходили сразу, а не пачками
        cmd = ["stdbuf", "-oL", "-eL", *cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail: deque = deque(maxlen=25)
    async for raw in proc.stdout:
        line = raw.decode(errors="ignore").rstrip()
        tail.append(line)
        m = FRA_RE.search(line)
        if m:
            on_pct(min(1.0, int(m.group(1)) / max(1, total)))
    await proc.wait()
    if proc.returncode != 0 or not out_mp4.exists():
        raise RuntimeError("Blender error:\n" + "\n".join(tail)[-1500:])


async def start_render(uid: str, scenes: list[dict], style: str, preview: bool, engine: str,
                       voices: dict | None = None) -> tuple[str, asyncio.Task, str]:
    """Проверяет входные данные и запускает рендер в фоне. Возвращает (id проекта, задача, название).
    Ошибки входных данных — RuntimeError с понятным текстом."""
    voices = voices or {}
    chars = await fb_get(f"clients/{uid}/characters") or {}
    lib = await fb_get("library/characters") or {}
    settings = await get_tts_settings(uid)
    if engine == "piper" and not tts.piper_available():
        raise RuntimeError("Piper не установлен на сервере. Выберите ElevenLabs или добавьте piper-tts в requirements.txt.")
    if engine == "eleven" and not tts.ELEVEN_KEY:
        raise RuntimeError("Не задан ключ ElevenLabs. Выберите озвучку Piper.")

    plan, missing = {}, []
    for who in dict.fromkeys(ln["who"] for sc in scenes for ln in sc["lines"]):
        real, ch = find_char(chars, who)
        if ch is None:
            real, ch = find_char(lib, who)
        if ch is None:
            missing.append(who)
            continue
        legacy = "eleven:" + ELEVEN_VOICES[who] if who in ELEVEN_VOICES else ""
        voice = tts.resolve_voice(engine, voices.get(who, ""), ch.get("voice", ""), legacy,
                                  settings["voice"] if settings["engine"] == engine else "")
        plan[who] = (real, ch, voice)
    if missing:
        raise RuntimeError("Нет персонажа: " + ", ".join(f"«{m}»" for m in missing) + ". Загрузите в «Мои персонажи».")

    pid = uuid.uuid4().hex[:8]
    key = f"{uid}/p/{pid}"
    title = (scenes[0]["setting"] or "Мультфильм")[:40].capitalize()
    job_set(key, kind="proj", pct=0, text="В очереди", status="queued")
    await fb_set(f"clients/{uid}/projects/{pid}",
                 {"status": "queued", "preview": preview, "created": int(time.time()),
                  "title": title, "engine": engine})
    task = spawn(_render_task(uid, pid, scenes, style, preview, plan))
    return pid, task, title


async def _render_task(uid: str, pid: str, scenes: list[dict], style: str, preview: bool, plan: dict) -> None:
    key = f"{uid}/p/{pid}"
    pdir = WORK / uid / pid
    pdir.mkdir(parents=True, exist_ok=True)
    path = f"clients/{uid}/projects/{pid}"
    try:
        async with RENDER_SEM:
            job_set(key, status="rendering", pct=1, text="Озвучиваю реплики")
            await fb_update(path, {"status": "rendering"})
            ready = {}
            for who, (real, ch, voice) in plan.items():
                ready[who] = await ensure_character(uid, real, ch)

            n_lines = sum(len(sc["lines"]) for sc in scenes)
            done_lines, total_frames, scene_out = 0, 0, []
            for si, sc in enumerate(scenes):
                lines_out = []
                for li, ln in enumerate(sc["lines"]):
                    wav = pdir / f"s{si}_l{li}.wav"
                    voice = plan[ln["who"]][2]
                    job_set(key, text=f"Озвучиваю: {ln['who']} ({voice.split(':', 1)[0]})")
                    dur = await tts.tts_line(ln["text"], voice, wav)
                    ch = ready[ln["who"]]
                    lines_out.append({**ln, "audio": str(wav), "duration": dur,
                                      "image": ch.get("file"), "meta": ch.get("meta")})
                    total_frames += max(int(dur * FPS) + 6, 12)
                    done_lines += 1
                    job_set(key, pct=1 + int(29 * done_lines / n_lines))          # 1..30 %
                scene_out.append({"setting": sc["setting"], "lines": lines_out})
            total_frames = max(total_frames, 24)

            scene_json = pdir / "scene.json"
            scene_json.write_text(json.dumps({"style": STYLE_ALIASES.get(style, style), "scenes": scene_out},
                                             ensure_ascii=False))
            job_set(key, pct=30, text="Blender строит скелеты и анимацию")
            out = pdir / "result.mp4"
            on_pct = lambda f: job_set(key, pct=30 + int(65 * f), text="Рендер кадров")     # 30..95 %
            await run_blender(scene_json, out, preview, total_frames, on_pct)

        job_set(key, pct=96, text="Сохраняю видео")
        remote = None
        try:
            remote = await yadisk_upload(out, f"/CartoonStudio/{uid}/projects/{pid}/result.mp4")
        except Exception:
            log.exception("yadisk upload failed")
        await fb_update(path, {"status": "done", "file": str(out), "disk": remote,
                               "dur": int(total_frames / FPS), "finished": int(time.time())})
        job_set(key, pct=100, text="Готово", status="done")
    except Exception as e:
        log.exception("render failed")
        err = str(e)[:500]
        await fb_update(path, {"status": "error", "error": err})
        job_set(key, status="error", error=err, text="Ошибка")


async def fit_telegram(src: Path) -> Path:
    """Если видео больше лимита Telegram (50 МБ) — пережимает под лимит."""
    if src.stat().st_size <= TG_LIMIT:
        return src
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src),
            stdout=asyncio.subprocess.PIPE)
        out, _ = await probe.communicate()
    except FileNotFoundError:
        raise RuntimeError("Видео больше 50 МБ, а на сервере нет ffmpeg, чтобы его сжать.")
    dur = max(1.0, float(out.decode().strip() or 1))
    kbps = max(200, int(46 * 8192 / dur) - 96)
    dst = src.with_name("result_tg.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if kbps < 1500:
        cmd += ["-vf", "scale=-2:720"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst)]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await p.wait()
    if p.returncode != 0 or not dst.exists():
        raise RuntimeError("Видео больше 50 МБ, а сжать его не получилось (нет ffmpeg).")
    return dst


async def send_project_video(bot: Bot, uid: str, pid: str, chat_id: int, caption: str = "") -> None:
    pr = await fb_get(f"clients/{uid}/projects/{pid}")
    if not pr or pr.get("status") != "done":
        raise RuntimeError("Видео ещё не готово.")
    path = Path(pr.get("file") or WORK / uid / pid / "result.mp4")
    if not path.exists() and pr.get("disk"):
        await yadisk_download(pr["disk"], path)
    if not path.exists():
        raise RuntimeError("Файл видео не найден на сервере (он мог удалиться после перезапуска).")
    path = await fit_telegram(path)
    await bot.send_video(chat_id, FSInputFile(path), supports_streaming=True,
                         caption=caption or f"🎬 {pr.get('title', 'Мультфильм')}")


async def watch(msg: Message, key: str, task: asyncio.Task, head: str) -> None:
    """Пока задача идёт — обновляет сообщение в чате: этап + проценты."""
    last = ""
    while not task.done():
        await asyncio.sleep(2)
        j = JOBS.get(key, {})
        text = f"{head}\n{j.get('text', '')}\n{bar(int(j.get('pct', 0)))}"
        if text != last:
            last = text
            await safe_edit(msg, text)


# ───────────────────────────── чат: старт, промпт ─────────────────────────────
@router.message(Command("start"))
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await set_menu_button(bot, m.chat.id)
    uid = str(m.from_user.id)
    if not await fb_get(f"clients/{uid}"):
        await fb_set(f"clients/{uid}", {"name": m.from_user.full_name, "created": int(time.time())})
    await m.answer("🎬 TajStudio. Загрузи персонажей, выбери озвучку, напиши сценарий — получишь мультфильм.\n"
                   "Всё то же самое есть в приложении — кнопка TajStudio слева от поля ввода.",
                   reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_menu())


@router.callback_query(F.data == "prompt")
async def show_prompt(c: CallbackQuery):
    await c.message.answer(
        "Скопируй, вставь в ChatGPT вместе со своим фото, результат пришли мне как файл:\n\n"
        f"<code>{CHARACTER_PROMPT}</code>", parse_mode="HTML")
    await c.answer()


# ───────────────────────────── чат: персонажи ─────────────────────────────
@router.callback_query(F.data == "chars")
async def chars_menu(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    lib = await fb_get("library/characters") or {}
    rows = [[InlineKeyboardButton(text=f"🗑 Удалить «{n}»", callback_data=f"delask:{cid(n)}")] for n in mine]
    rows.append([InlineKeyboardButton(text="➕ Загрузить персонажа", callback_data="addchar")])
    await c.message.answer(f"Твои: {', '.join(mine) or 'пока нет'}\nБиблиотека: {', '.join(lib) or 'пусто'}",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("delask:"))
async def delete_ask(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    name = next((n for n in mine if cid(n) == c.data.split(":")[1]), None)
    if not name:
        return await c.answer("Персонаж уже удалён", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, удалить", callback_data=f"deldo:{cid(name)}"),
        InlineKeyboardButton(text="Отмена", callback_data="chars")]])
    await c.message.answer(f"Удалить персонажа «{name}»? Это нельзя отменить.", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("deldo:"))
async def delete_do(c: CallbackQuery):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    name = next((n for n in mine if cid(n) == c.data.split(":")[1]), None)
    if name:
        await delete_character(uid, name)
    await safe_edit(c.message, f"🗑 Персонаж «{name}» удалён." if name else "Персонаж уже удалён.")
    await c.answer()


@router.callback_query(F.data == "addchar")
async def addchar(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.waiting_character)
    await c.message.answer("Пришли PNG персонажа как ФАЙЛ (скрепка → Файл, не фото — иначе сожмётся). "
                           "Потом я спрошу имя.")
    await c.answer()


@router.message(F.document | F.photo)
async def got_file(m: Message, state: FSMContext, bot: Bot):
    """Файл персонажа в любой момент: качаем, спрашиваем имя, создаём с процентами."""
    cur = await state.get_state()
    if cur in (Flow.waiting_script.state, Flow.choosing_style.state):
        return await m.answer("Сейчас жду сценарий. Для загрузки персонажа: «Мои персонажи» → «Загрузить».")
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        return await m.answer("Это не картинка. Пришли PNG/JPG листа персонажа как файл.")
    uid = str(m.from_user.id)
    tmp = WORK / uid / "incoming" / f"{uuid.uuid4().hex}.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        await bot.download(m.document or m.photo[-1], destination=tmp)
    except Exception:
        return await m.answer("Не смог скачать файл (Telegram отдаёт боту файлы до 20 МБ). Уменьши картинку.")
    note = "\n(Это фото, а не файл — качество ниже. Лучше слать как файл.)" if m.photo else ""
    caption = clean_name(m.caption or "")
    if caption:
        return await create_in_chat(m, state, uid, caption, tmp)
    await state.set_state(Flow.waiting_name)
    await state.update_data(tmp=str(tmp))
    await m.answer("✅ Файл получен." + note + "\n\n✏️ Как зовут персонажа? Напиши имя — "
                   "оно же будет в сценарии («Имя: реплика»).")


@router.message(Flow.waiting_character, F.text)
async def need_file(m: Message):
    await m.answer("Жду файл с картинкой (скрепка → Файл). Отмена: /cancel")


@router.message(Flow.waiting_name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = clean_name(m.text)
    if not name:
        return await m.answer("Имя не подошло. Напиши короткое имя (до 30 символов), например: Маша")
    tmp = Path((await state.get_data()).get("tmp", ""))
    if not tmp.exists():
        await state.clear()
        return await m.answer("Файл потерялся, пришли его ещё раз.", reply_markup=main_menu())
    await create_in_chat(m, state, str(m.from_user.id), name, tmp)


async def create_in_chat(m: Message, state: FSMContext, uid: str, name: str, tmp: Path):
    await state.clear()
    orig = WORK / uid / "characters" / f"{name}.png"
    orig.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), orig)
    key = f"{uid}/c/{name}"
    if JOBS.get(key, {}).get("status") == "processing":
        return await m.answer(f"«{name}» уже создаётся, подожди.")
    head = f"⏳ Создаю персонажа «{name}»"
    status = await m.answer(f"{head}\n{bar(0)}")
    task = spawn(create_character(uid, name, orig, int(uid) in ADMIN_IDS))    # админ из чата -> ещё и в библиотеку
    await watch(status, key, task, head)
    if await task:
        await safe_edit(status, f"✅ Персонаж «{name}» создан\n{bar(100)}")
        await m.answer("Нашёл тело и 9 выражений лица — скелет и анимацию добавлю при рендере.",
                       reply_markup=main_menu())
    else:
        await safe_edit(status, f"❌ Не получилось создать «{name}»: {JOBS[key].get('error')}")


# ───────────────────────────── чат: озвучка ─────────────────────────────
@router.callback_query(F.data == "voice")
async def voice_menu(c: CallbackQuery):
    cur = await get_tts_settings(str(c.from_user.id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if cur["engine"] == e else "") + t, callback_data=f"eng:{e}")]
        for e, t in ENGINE_TITLES.items()])
    await c.message.answer(f"🎙 Озвучка. Сейчас: {ENGINE_TITLES[cur['engine']]}\nВыбери способ:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("eng:"))
async def pick_engine(c: CallbackQuery):
    engine = c.data.split(":")[1]
    cat = await tts.voice_catalog(engine)
    if not cat["ok"]:
        return await c.message.answer(f"⚠️ {cat['error']}")
    cur = await get_tts_settings(str(c.from_user.id))
    btns = [InlineKeyboardButton(text=("✅ " if v["key"] == cur["voice"] else "") + v["name"],
                                 callback_data=f"vs:{engine}:{i}")
            for i, v in enumerate(cat["voices"][:24])]
    kb = InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])
    await c.message.answer(f"Голоса — {ENGINE_TITLES[engine]}. Выбери голос, и я сразу озвучу пример:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("vs:"))
async def pick_voice(c: CallbackQuery, bot: Bot):
    _, engine, idx = c.data.split(":")
    cat = await tts.voice_catalog(engine)
    voices = cat["voices"]
    if not cat["ok"] or int(idx) >= len(voices):
        return await c.answer("Список голосов изменился, открой «Озвучка» заново", show_alert=True)
    v = voices[int(idx)]
    await c.answer("Готовлю голос…")
    wait = await c.message.answer(f"⏳ Озвучиваю пример голосом «{v['name']}»…" +
                                  ("\nПервый раз Piper скачивает модель — это до минуты." if not v.get("ready") else ""))
    try:
        wav = await tts.make_sample(v["key"])
        await save_tts_settings(str(c.from_user.id), engine, v["key"])
        ogg = await tts.wav_to_ogg(wav)
        cap = f"✅ Выбран голос: {v['name']} — {ENGINE_TITLES[engine]}"
        if ogg:
            await bot.send_voice(c.message.chat.id, FSInputFile(ogg), caption=cap)
        else:
            await bot.send_document(c.message.chat.id, FSInputFile(wav), caption=cap)
        await wait.delete()
    except Exception as e:
        log.exception("voice sample failed")
        await safe_edit(wait, f"❌ {str(e)[:500]}")


# ───────────────────────────── чат: новый мультфильм ─────────────────────────────
@router.callback_query(F.data == "new")
async def new_project(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.waiting_script)
    await c.message.answer(
        "Пришли сценарий текстом:\n\n<code>[Сцена: лес, утро]\nМаша: Привет, Миша!\n"
        "Миша: (радостно) Привет!</code>\n\nИмена в сценарии = имена загруженных персонажей.",
        parse_mode="HTML")
    await c.answer()


@router.message(Flow.waiting_script, F.text)
async def got_script(m: Message, state: FSMContext):
    scenes = parse_script(m.text)
    if not scenes or not any(s["lines"] for s in scenes):
        return await m.answer("Не нашёл реплик. Формат: «Имя: текст».")
    await state.update_data(scenes=scenes)
    await state.set_state(Flow.choosing_style)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=v, callback_data=f"style:{k}")] for k, v in STYLES.items()])
    await m.answer(f"Сцен: {len(scenes)}. Выбери стиль:", reply_markup=kb)


@router.callback_query(Flow.choosing_style, F.data.startswith("style:"))
async def style_chosen(c: CallbackQuery, state: FSMContext):
    await state.update_data(style=c.data.split(":")[1])
    cur = await get_tts_settings(str(c.from_user.id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("⭐ " if cur["engine"] == e else "") + t, callback_data=f"tts:{e}")]
        for e, t in ENGINE_TITLES.items()])
    await c.message.answer("Чем озвучить? (голос берётся из настроек «🎙 Озвучка»)", reply_markup=kb)
    await c.answer()


@router.callback_query(Flow.choosing_style, F.data.startswith("tts:"))
async def tts_chosen(c: CallbackQuery, state: FSMContext):
    await state.update_data(engine=c.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Предпросмотр (бесплатно)", callback_data="go:preview")],
        [InlineKeyboardButton(text="🎞 Полный рендер", callback_data="go:full")]])
    await c.message.answer("Начнём с предпросмотра — он быстрый и не списывает баланс.", reply_markup=kb)
    await c.answer()


@router.callback_query(Flow.choosing_style, F.data.startswith("go:"))
async def go(c: CallbackQuery, state: FSMContext, bot: Bot):
    preview = c.data.endswith("preview")
    data = await state.get_data()
    await state.clear()
    await c.answer()
    uid = str(c.from_user.id)
    engine = data.get("engine") or (await get_tts_settings(uid))["engine"]
    try:
        pid, task, title = await start_render(uid, data["scenes"], data.get("style", "classic"), preview, engine)
    except RuntimeError as e:
        return await c.message.answer(f"❌ {e}")
    started = time.time()
    head = f"🎬 «{title}» — {'предпросмотр' if preview else 'рендер'}"
    status = await c.message.answer(f"{head}\n{bar(0)}")
    await watch(status, f"{uid}/p/{pid}", task, head)
    await task
    j = JOBS.get(f"{uid}/p/{pid}", {})
    if j.get("status") == "done":
        await safe_edit(status, f"✅ Готово за {int(time.time() - started)} с\n{bar(100)}")
        try:
            await send_project_video(bot, uid, pid, c.message.chat.id,
                                     f"✅ {'Предпросмотр' if preview else 'Финал'} · {title}")
        except Exception as e:
            await c.message.answer(f"Видео готово, но отправить не вышло: {e}\nСкачайте его в приложении → Проекты.")
    else:
        await safe_edit(status, f"❌ Ошибка: {j.get('error', 'неизвестная')[:600]}")


@router.callback_query(F.data == "projects")
async def projects(c: CallbackQuery):
    pr = await fb_get(f"clients/{c.from_user.id}/projects") or {}
    if not pr:
        await c.message.answer("Проектов пока нет.")
        return await c.answer()
    rows, items = [], sorted(pr.items(), key=lambda kv: -kv[1].get("created", 0))[:15]
    names = {"done": "готов", "rendering": "рендерится", "queued": "в очереди", "error": "ошибка"}
    for k, v in items:
        st = names.get(v.get("status"), v.get("status"))
        if v.get("status") == "done":
            rows.append([InlineKeyboardButton(text=f"📥 {v.get('title', k)} · {st}", callback_data=f"send:{k}")])
    txt = "\n".join(f"• {v.get('title', k)} — {names.get(v.get('status'), v.get('status'))}" for k, v in items)
    await c.message.answer(txt + ("\n\nНажми на проект, чтобы получить видео:" if rows else ""),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    await c.answer()


@router.callback_query(F.data.startswith("send:"))
async def send_cb(c: CallbackQuery, bot: Bot):
    await c.answer("Отправляю видео…")
    try:
        await send_project_video(bot, str(c.from_user.id), c.data.split(":")[1], c.message.chat.id)
    except Exception as e:
        await c.message.answer(f"❌ {e}")


# ───────────────────────────── HTTP API для Mini App ─────────────────────────────
def verify_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram: возвращает данные пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def auth(request: web.Request) -> dict:
    u = verify_init_data(request.headers.get("X-Init-Data", ""))
    if not u:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    return u


@web.middleware
async def api_middleware(request: web.Request, handler):
    """CORS (Mini App может жить на другом домене) + JSON-ошибки."""
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        except Exception as e:
            log.exception("api error")
            resp = err(str(e)[:300], 500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _gc_jobs() -> None:
    now = time.time()
    for k in [k for k, j in JOBS.items() if j.get("status") in ("done", "error") and now - j.get("upd", now) > 900]:
        JOBS.pop(k, None)


async def api_me(request: web.Request) -> web.Response:
    u = auth(request)
    uid = str(u["id"])
    _gc_jobs()
    client = await fb_get(f"clients/{uid}") or {}
    lib = await fb_get("library/characters") or {}
    chars = client.get("characters") or {}
    projects = client.get("projects") or {}

    characters = {}
    for n, v in chars.items():
        characters[n] = {"name": n, "voice": v.get("voice", ""), "status": "ready", "pct": 100, "text": "",
                         "error": "", "thumb": bool(v.get("thumb"))}
    for k, j in JOBS.items():                       # персонажи, которые прямо сейчас создаются / упали
        if k.startswith(uid + "/c/") and j["status"] in ("processing", "error"):
            n = j["name"]
            characters[n] = {**characters.get(n, {"voice": "", "thumb": False}), "name": n, "status": j["status"],
                             "pct": j.get("pct", 0), "text": j.get("text", ""), "error": j.get("error", "")}

    plist = []
    for k, v in projects.items():
        j = JOBS.get(f"{uid}/p/{k}")
        status = v.get("status", "done")
        if status in ("queued", "rendering") and not j:            # бот перезапускался посреди рендера
            status = "error"
            v["error"] = "Рендер прервался (перезапуск сервера). Запустите заново."
            await fb_update(f"clients/{uid}/projects/{k}", {"status": "error", "error": v["error"]})
        if j and status in ("queued", "rendering"):
            status = j["status"]
        plist.append({"id": k, "title": v.get("title") or f"Проект {k}", "status": status,
                      "mode": "preview" if v.get("preview") else "render", "created": v.get("created", 0),
                      "pct": j.get("pct", 0) if j and status in ("queued", "rendering") else 100,
                      "text": (j or {}).get("text", ""), "error": v.get("error", ""),
                      "dur": v.get("dur", 0), "engine": v.get("engine", "")})
    plist.sort(key=lambda p: -p["created"])

    return web.json_response({
        "admin": u["id"] in ADMIN_IDS,
        "characters": list(characters.values()),
        "library": [{"name": n, "thumb": bool(v.get("thumb"))} for n, v in lib.items()],
        "projects": plist,
        "settings": await get_tts_settings(uid),
        "tts": {"piper": tts.piper_available(), "eleven": bool(tts.ELEVEN_KEY)},
        "profile": {"plan": client.get("plan", "Без подписки"), "until": client.get("until", ""),
                    "balance": client.get("balance", 0),
                    "films": sum(1 for v in projects.values() if v.get("status") == "done" and not v.get("preview"))},
    })


async def api_voices(request: web.Request) -> web.Response:
    auth(request)
    return web.json_response(await tts.voice_catalog(request.query.get("engine", "piper")))


async def api_settings(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    engine = d.get("engine")
    if engine not in ("piper", "eleven"):
        return err("Неизвестный движок озвучки")
    await save_tts_settings(uid, engine, tts.resolve_voice(engine, d.get("voice", "")))
    return web.json_response(await get_tts_settings(uid))


async def api_sample(request: web.Request) -> web.Response:
    """Пример голоса: синтезируется именно выбранной моделью."""
    auth(request)
    d = await request.json()
    try:
        wav = await tts.make_sample(d.get("voice", ""))
    except tts.TTSError as e:
        return err(str(e), 502)
    return web.Response(body=wav.read_bytes(), content_type="audio/wav")


async def api_character_add(request: web.Request) -> web.Response:
    u = auth(request)
    uid = str(u["id"])
    name, to_lib, tmp = "", False, None
    async for part in await request.multipart():
        if part.name == "name":
            name = clean_name(await part.text())
        elif part.name == "library":
            to_lib = (await part.text()) == "1" and u["id"] in ADMIN_IDS
        elif part.name == "file":
            tmp = WORK / uid / "incoming" / f"{uuid.uuid4().hex}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        tmp.unlink(missing_ok=True)
                        return err("Файл больше 25 МБ")
                    f.write(chunk)
    if not tmp or not tmp.exists():
        return err("Не выбран файл")
    if not name:
        tmp.unlink(missing_ok=True)
        return err("Введите имя персонажа")
    if JOBS.get(f"{uid}/c/{name}", {}).get("status") == "processing":
        return err("Этот персонаж уже создаётся", 409)
    orig = WORK / uid / "characters" / f"{name}.png"
    orig.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), orig)
    job_set(f"{uid}/c/{name}", kind="char", name=name, pct=3, text="Файл получен", status="processing", error="")
    spawn(create_character(uid, name, orig, to_lib))
    return web.json_response({"ok": True, "name": name})


async def api_character_delete(request: web.Request) -> web.Response:
    u = auth(request)
    d = await request.json()
    name = clean_name(d.get("name", ""))
    if not name or not await delete_character(str(u["id"]), name, bool(d.get("library"))):
        return err("Нельзя удалить", 403)
    return web.json_response({"ok": True})


async def api_character_voice(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    name = clean_name(d.get("name", ""))
    if not await fb_get(f"clients/{uid}/characters/{name}"):
        return err("Персонаж не найден", 404)
    await fb_update(f"clients/{uid}/characters/{name}", {"voice": d.get("voice", "")})
    return web.json_response({"ok": True})


async def api_character_thumb(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    name = clean_name(request.query.get("name", ""))
    rec = await fb_get(f"clients/{uid}/characters/{name}")
    if request.query.get("lib") == "1" or not rec:
        rec = await fb_get(f"library/characters/{name}")
    thumb = Path((rec or {}).get("thumb") or "/nonexistent")
    if not thumb.exists() or WORK.resolve() not in thumb.resolve().parents:
        return err("нет превью", 404)
    return web.FileResponse(thumb, headers={"Cache-Control": "private, max-age=300"})


async def api_render(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    scenes = parse_script(str(d.get("script", ""))[:20000])
    if not scenes or not any(s["lines"] for s in scenes):
        return err("Не нашёл реплик. Формат: «Имя: текст».")
    engine = d.get("engine") if d.get("engine") in ("piper", "eleven") else (await get_tts_settings(uid))["engine"]
    voices = {k: v for k, v in (d.get("voices") or {}).items() if isinstance(v, str)}
    try:
        pid, _task, _title = await start_render(uid, scenes, str(d.get("style", "classic")),
                                                d.get("mode") == "preview", engine, voices)
    except RuntimeError as e:
        return err(str(e))
    request.app["notify"](uid, pid)
    return web.json_response({"ok": True, "id": pid})


async def api_project_send(request: web.Request) -> web.Response:
    uid = str(auth(request)["id"])
    d = await request.json()
    try:
        await send_project_video(request.app["bot"], uid, str(d.get("id", "")), int(uid))
    except RuntimeError as e:
        return err(str(e))
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(Path(__file__).with_name("index.html"), headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_web(bot: Bot) -> None:
    """Отдаёт Mini App (index.html) и API по домену Bothost, порт PORT (по умолчанию 3000)."""
    app = web.Application(middlewares=[api_middleware], client_max_size=MAX_UPLOAD + 1024 * 1024)
    app["bot"] = bot

    def notify(uid: str, pid: str) -> None:
        """Когда Mini App-рендер закончится — пришлём в чат сообщение с кнопкой «Скачать»."""
        async def _w():
            while JOBS.get(f"{uid}/p/{pid}", {}).get("status") in ("queued", "rendering"):
                await asyncio.sleep(3)
            j = JOBS.get(f"{uid}/p/{pid}", {})
            try:
                if j.get("status") == "done":
                    pr = await fb_get(f"clients/{uid}/projects/{pid}") or {}
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="📥 Скачать видео", callback_data=f"send:{pid}")]])
                    await bot.send_message(int(uid), f"✅ «{pr.get('title', 'Мультфильм')}» готов! "
                                                     "Нажмите, чтобы получить видео в этот чат.", reply_markup=kb)
                else:
                    await bot.send_message(int(uid), f"❌ Рендер не удался: {j.get('error', 'ошибка')[:400]}")
            except Exception:
                log.exception("notify failed")
        spawn(_w())
    app["notify"] = notify

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/voices", api_voices)
    app.router.add_get("/api/character/thumb", api_character_thumb)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/tts/sample", api_sample)
    app.router.add_post("/api/character", api_character_add)
    app.router.add_post("/api/character/delete", api_character_delete)
    app.router.add_post("/api/character/voice", api_character_voice)
    app.router.add_post("/api/render", api_render)
    app.router.add_post("/api/project/send", api_project_send)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "3000"))).start()
    log.info("mini app served on port %s", os.environ.get("PORT", "3000"))


async def main():
    bot = Bot(BOT_TOKEN)
    await run_web(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await set_menu_button(bot)          # кнопка по умолчанию для всех чатов
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
