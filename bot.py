"""
Telegram-бот «Мульт-студия»: сценарий -> 2D-мультфильм (Blender) -> видео в чат.

Стек: aiogram 3, Firebase RTDB (клиенты/проекты), Яндекс Диск (файлы),
ElevenLabs (озвучка), Blender (blender_worker.py), Bothost.ru (хостинг).

Запуск:  python bot.py
"""
import asyncio
import io
import json
import logging
import os
import re
import subprocess
import time
import uuid
import wave
from pathlib import Path

import aiohttp
import firebase_admin
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from firebase_admin import credentials, db

from prepare_character import prepare

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cartoon")

# ───────────────────────── настройки (env на Bothost) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
FIREBASE_CRED_JSON = os.environ["FIREBASE_CRED_JSON"]      # содержимое service-account.json
FIREBASE_DB_URL = os.environ["FIREBASE_DB_URL"]
YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICES = json.loads(os.environ.get("ELEVEN_VOICES", "{}"))  # {"Маша": "voice_id", ...}
DEFAULT_VOICE = os.environ.get("ELEVEN_DEFAULT_VOICE", "")
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WORK = Path(os.environ.get("WORK_DIR", "/tmp/cartoon"))
WORK.mkdir(parents=True, exist_ok=True)
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")   # https-ссылка на index.html (GitHub Pages)
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

# Промпт, который пользователь вставляет в ChatGPT вместе со своим фото
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

# ───────────────────────────── состояния и меню ─────────────────────────────
class Flow(StatesGroup):
    waiting_character = State()
    waiting_script = State()
    choosing_style = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Новый мультфильм", callback_data="new")],
        [InlineKeyboardButton(text="🧍 Мои персонажи", callback_data="chars")],
        [InlineKeyboardButton(text="📁 Мои проекты", callback_data="projects")],
        [InlineKeyboardButton(text="📋 Промпт для ChatGPT", callback_data="prompt")],
    ])


router = Router()


# ───────────────────────────── Firebase ─────────────────────────────
def fb(path: str):
    return db.reference(path)


async def fb_get(path):
    return await asyncio.to_thread(lambda: fb(path).get())


async def fb_set(path, value):
    await asyncio.to_thread(lambda: fb(path).set(value))


async def fb_update(path, value):
    await asyncio.to_thread(lambda: fb(path).update(value))


# ───────────────────────────── Яндекс Диск ─────────────────────────────
async def yadisk_upload(local: Path, remote: str) -> str | None:
    """Грузит файл на Яндекс Диск, возвращает путь. Без токена — молча пропускает."""
    if not YADISK_TOKEN:
        return None
    h = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    base = "https://cloud-api.yandex.net/v1/disk/resources"
    async with aiohttp.ClientSession(headers=h) as s:
        # создаём вложенные папки
        parts = remote.strip("/").split("/")[:-1]
        cur = ""
        for p in parts:
            cur += "/" + p
            await s.put(base, params={"path": cur})
        async with s.get(f"{base}/upload", params={"path": remote, "overwrite": "true"}) as r:
            url = (await r.json()).get("href")
        if not url:
            return None
        async with s.put(url, data=local.read_bytes()):
            pass
    return remote


# ───────────────────────────── сценарий ─────────────────────────────
def parse_script(text: str) -> list[dict]:
    """
    Формат сценария:
      [Сцена: лес, утро]
      Маша: Привет, Миша!
      Миша: (радостно) Привет!
    Возвращает список сцен: {"setting": str, "lines": [{"who", "text", "emotion"}]}
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


# ───────────────────────────── озвучка ─────────────────────────────
async def tts_line(text: str, voice_id: str, out_wav: Path) -> float:
    """ElevenLabs -> WAV (PCM 22050). Возвращает длительность в секундах."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_22050"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": "eleven_multilingual_v2"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"ElevenLabs {r.status}: {await r.text()}")
            pcm = await r.read()
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(pcm)
    return len(pcm) / 2 / 22050


# ───────────────────────────── рендер ─────────────────────────────
async def run_blender(scene_json: Path, out_mp4: Path, preview: bool) -> None:
    worker = Path(__file__).with_name("blender_worker.py")
    cmd = [BLENDER_BIN, "-b", "-P", str(worker), "--",
           "--scene", str(scene_json), "--out", str(out_mp4)]
    if preview:
        cmd.append("--preview")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out_mp4.exists():
        raise RuntimeError("Blender error:\n" + out.decode(errors="ignore")[-1500:])


# ───────────────────────────── хэндлеры ─────────────────────────────
@router.message(Command("start"))
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await set_menu_button(bot, m.chat.id)
    uid = str(m.from_user.id)
    if not await fb_get(f"clients/{uid}"):
        await fb_set(f"clients/{uid}", {"name": m.from_user.full_name,
                                        "created": int(time.time())})
    await m.answer("🎬 Мульт-студия. Загрузи персонажей, напиши сценарий — получишь мультфильм.",
                   reply_markup=main_menu())


@router.callback_query(F.data == "prompt")
async def show_prompt(c: CallbackQuery):
    await c.message.answer(
        "Скопируй, вставь в ChatGPT вместе со своим фото, результат пришли мне как файл:\n\n"
        f"<code>{CHARACTER_PROMPT}</code>", parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data == "chars")
async def chars(c: CallbackQuery, state: FSMContext):
    uid = str(c.from_user.id)
    mine = await fb_get(f"clients/{uid}/characters") or {}
    lib = await fb_get("library/characters") or {}
    names = ", ".join(mine) or "пока нет"
    libn = ", ".join(lib) or "пусто"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Загрузить персонажа", callback_data="addchar")]])
    await c.message.answer(f"Твои: {names}\nБиблиотека: {libn}", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "addchar")
async def addchar(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.waiting_character)
    await c.message.answer("Пришли PNG персонажа как ФАЙЛ (не фото — иначе сожмётся) "
                           "с подписью: имя персонажа.")
    await c.answer()


@router.message(Flow.waiting_character, F.document)
async def got_character(m: Message, state: FSMContext, bot: Bot):
    name = (m.caption or "").strip() or f"char_{uuid.uuid4().hex[:4]}"
    uid = str(m.from_user.id)
    local = WORK / uid / "characters" / f"{name}.png"
    local.parent.mkdir(parents=True, exist_ok=True)
    await bot.download(m.document, destination=local)
    remote = await yadisk_upload(local, f"/CartoonStudio/{uid}/characters/{name}.png")
    # лист ChatGPT -> тело без фона + 9 эмоций лица
    meta = await asyncio.to_thread(prepare, local, local.parent / name)
    await fb_set(f"clients/{uid}/characters/{name}",
                 {"file": meta["body"], "disk": remote, "voice": DEFAULT_VOICE, "meta": meta})
    if uid and int(uid) in ADMIN_IDS:
        await fb_set(f"library/characters/{name}", {"disk": remote})
    await state.clear()
    await m.answer(f"✅ Персонаж «{name}» сохранён. Нашёл тело и 9 выражений лица — скелет и анимацию добавлю при рендере.",
                   reply_markup=main_menu())


@router.message(Flow.waiting_character)
async def need_file(m: Message):
    await m.answer("Нужен именно файл (скрепка → Файл).")


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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Предпросмотр (бесплатно)", callback_data="go:preview")],
        [InlineKeyboardButton(text="🎞 Полный рендер", callback_data="go:full")]])
    await c.message.answer("Начнём с предпросмотра — он быстрый и не списывает баланс.",
                           reply_markup=kb)
    await c.answer()


@router.callback_query(Flow.choosing_style, F.data.startswith("go:"))
async def go(c: CallbackQuery, state: FSMContext, bot: Bot):
    preview = c.data.endswith("preview")
    data = await state.get_data()
    await state.clear()
    uid = str(c.from_user.id)
    pid = uuid.uuid4().hex[:8]
    pdir = WORK / uid / pid
    pdir.mkdir(parents=True, exist_ok=True)
    status = await c.message.answer("⏳ Озвучиваю реплики…")
    await c.answer()
    started = time.time()
    try:
        chars = await fb_get(f"clients/{uid}/characters") or {}
        lib = await fb_get("library/characters") or {}
        scene_out = []
        for si, sc in enumerate(data["scenes"]):
            lines_out = []
            for li, ln in enumerate(sc["lines"]):
                ch = chars.get(ln["who"]) or lib.get(ln["who"])
                if not ch:
                    raise RuntimeError(f"Нет персонажа «{ln['who']}». Загрузи его в «Мои персонажи».")
                wav = pdir / f"s{si}_l{li}.wav"
                voice = ELEVEN_VOICES.get(ln["who"]) or ch.get("voice") or DEFAULT_VOICE
                dur = await tts_line(ln["text"], voice, wav)
                lines_out.append({**ln, "audio": str(wav), "duration": dur,
                                  "image": ch.get("file"), "meta": ch.get("meta")})
            scene_out.append({"setting": sc["setting"], "lines": lines_out})

        scene_json = pdir / "scene.json"
        scene_json.write_text(json.dumps({"style": data.get("style", "classic"),
                                          "scenes": scene_out}, ensure_ascii=False))
        await fb_set(f"clients/{uid}/projects/{pid}",
                     {"status": "rendering", "preview": preview, "created": int(time.time())})
        await status.edit_text("🎨 Blender строит скелеты и анимацию…")
        out = pdir / "result.mp4"
        await run_blender(scene_json, out, preview)

        await yadisk_upload(out, f"/CartoonStudio/{uid}/projects/{pid}/result.mp4")
        await fb_update(f"clients/{uid}/projects/{pid}", {"status": "done"})
        await bot.send_video(c.message.chat.id, FSInputFile(out),
                             caption=f"✅ Готово за {int(time.time() - started)} с "
                                     f"({'предпросмотр' if preview else 'финал'})")
    except Exception as e:
        log.exception("render failed")
        await fb_update(f"clients/{uid}/projects/{pid}", {"status": "error", "error": str(e)[:500]})
        await c.message.answer(f"❌ Ошибка: {str(e)[:600]}")


@router.callback_query(F.data == "projects")
async def projects(c: CallbackQuery):
    pr = await fb_get(f"clients/{c.from_user.id}/projects") or {}
    txt = "\n".join(f"• {k} — {v.get('status')}" for k, v in pr.items()) or "Проектов пока нет."
    await c.message.answer(txt)
    await c.answer()


async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await set_menu_button(bot)          # кнопка по умолчанию для всех чатов
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
