"""
Озвучка для TajStudio: два движка на выбор.

  piper   — бесплатный, работает на самом сервере (папка voices/, модели качаются сами)
  eleven  — ElevenLabs (нужен ELEVENLABS_API_KEY)

Голос везде записывается ключом  «движок:id»:
  piper:ru_RU-irina-medium
  eleven:21m00Tcm4TlvDq8ikWAM
Модуль не зависит от aiogram/Firebase — его можно проверять отдельно.
"""
import asyncio
import hashlib
import os
import threading
import time
import uuid
import wave
from pathlib import Path

ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_DEFAULT = os.environ.get("ELEVEN_DEFAULT_VOICE", "")       # id голоса; пусто = первый голос аккаунта
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")
PIPER_DIR = Path(os.environ.get("PIPER_VOICES_DIR", str(Path(__file__).with_name("voices"))))
PIPER_DEFAULT = os.environ.get("PIPER_DEFAULT_VOICE", "ru_RU-irina-medium")
SAMPLES_DIR = Path(os.environ.get("WORK_DIR", "/tmp/cartoon")) / "samples"

# Голоса Piper, которые показываем в списке (имена как на huggingface.co/rhasspy/piper-voices).
# Таджикского голоса в Piper нет. Свои модели: положите NAME.onnx + NAME.onnx.json в папку voices/ —
# они появятся в списке сами.
PIPER_CATALOG = [
    ("ru_RU-irina-medium", "Ирина", "женский, русский"),
    ("ru_RU-dmitri-medium", "Дмитрий", "мужской, русский"),
    ("ru_RU-denis-medium", "Денис", "мужской, русский"),
    ("ru_RU-ruslan-medium", "Руслан", "мужской, русский"),
    ("en_US-lessac-medium", "Lessac", "женский, английский"),
    ("en_US-amy-medium", "Amy", "женский, английский"),
    ("en_US-ryan-high", "Ryan", "мужской, английский"),
]

SAMPLE_TEXT = {
    "ru": "Привет! Я голос твоего мультфильма. Давай начнём!",
    "en": "Hello! I am the voice of your cartoon. Let's begin!",
}


class TTSError(RuntimeError):
    """Понятная пользователю ошибка озвучки."""


def split_key(key: str) -> tuple[str, str]:
    engine, _, vid = (key or "").partition(":")
    return engine, vid


def default_voice(engine: str) -> str:
    return "piper:" + PIPER_DEFAULT if engine == "piper" else "eleven:" + ELEVEN_DEFAULT


def resolve_voice(engine: str, *candidates: str) -> str:
    """Первый подходящий голос этого движка из списка кандидатов, иначе голос по умолчанию."""
    for c in candidates:
        e, vid = split_key(c or "")
        if e == engine and vid:
            return c
    return default_voice(engine)


# ───────────────────────────── Piper ─────────────────────────────
_piper_voices: dict = {}
_piper_lock = threading.Lock()


def piper_available() -> bool:
    try:
        import piper  # noqa: F401
        return True
    except ImportError:
        return False


def _piper_load(name: str):
    """Загружает модель (скачивает при первом использовании). Вызывать под _piper_lock."""
    v = _piper_voices.get(name)
    if v is not None:
        return v
    try:
        from piper import PiperVoice
    except ImportError:
        raise TTSError("Piper не установлен на сервере. Добавьте piper-tts в requirements.txt и перезапустите бота.")
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    model = PIPER_DIR / f"{name}.onnx"
    if not model.exists():
        try:
            from piper.download_voices import download_voice
            download_voice(name, PIPER_DIR)
        except Exception as e:
            raise TTSError(f"Не удалось скачать голос Piper «{name}» ({e}). Скачайте {name}.onnx и "
                           f"{name}.onnx.json вручную и положите в папку voices/ рядом с bot.py.")
    try:
        v = PiperVoice.load(str(model))
    except Exception as e:
        raise TTSError(f"Не удалось загрузить голос Piper «{name}»: {e}")
    _piper_voices[name] = v
    return v


def _piper_synth(name: str, text: str, out_wav: Path) -> float:
    with _piper_lock:
        voice = _piper_load(name)
        with wave.open(str(out_wav), "wb") as w:
            voice.synthesize_wav(text, w)
    with wave.open(str(out_wav)) as w:
        dur = w.getnframes() / w.getframerate()
    if dur <= 0:
        raise TTSError(f"Piper не смог озвучить реплику: «{text[:40]}»")
    return dur


# ───────────────────────────── ElevenLabs ─────────────────────────────
_eleven_cache = {"t": 0.0, "voices": []}


async def eleven_list_voices() -> list[dict]:
    if not ELEVEN_KEY:
        raise TTSError("Не задан ELEVENLABS_API_KEY (переменная окружения на хостинге).")
    import aiohttp
    if time.time() - _eleven_cache["t"] < 600 and _eleven_cache["voices"]:
        return _eleven_cache["voices"]
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": ELEVEN_KEY}) as r:
            if r.status != 200:
                raise TTSError(f"ElevenLabs {r.status}: {(await r.text())[:200]}")
            data = await r.json()
    out = []
    for v in data.get("voices", []):
        labels = v.get("labels") or {}
        desc = ", ".join(x for x in (labels.get("gender"), labels.get("accent"), labels.get("description")) if x)
        out.append({"key": "eleven:" + v["voice_id"], "name": v.get("name", v["voice_id"]), "desc": desc})
    _eleven_cache.update(t=time.time(), voices=out)
    return out


async def _tts_eleven(text: str, voice_id: str, out_wav: Path) -> float:
    if not ELEVEN_KEY:
        raise TTSError("Не задан ELEVENLABS_API_KEY. Выберите озвучку Piper или добавьте ключ на хостинге.")
    import aiohttp
    if not voice_id:
        # Раньше тут уходил пустой voice_id -> ElevenLabs отвечал 404 «Not Found».
        voices = await eleven_list_voices()
        if not voices:
            raise TTSError("В вашем аккаунте ElevenLabs нет голосов. Добавьте голос или выберите Piper.")
        voice_id = voices[0]["key"].split(":", 1)[1]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_22050"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVEN_MODEL}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=body) as r:
            if r.status == 404:
                raise TTSError(f"ElevenLabs 404: голос «{voice_id}» не найден в вашем аккаунте. "
                               "Выберите другой голос в разделе «Озвучка».")
            if r.status != 200:
                raise TTSError(f"ElevenLabs {r.status}: {(await r.text())[:200]}")
            pcm = await r.read()
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(pcm)
    dur = len(pcm) / 2 / 22050
    if dur <= 0:
        raise TTSError("ElevenLabs вернул пустое аудио.")
    return dur


# ───────────────────────────── общий вход ─────────────────────────────
async def tts_line(text: str, key: str, out_wav: Path) -> float:
    """Озвучивает реплику выбранным голосом, пишет WAV (16 бит, моно). Возвращает секунды."""
    engine, vid = split_key(key)
    text = text.strip()
    if not text:
        raise TTSError("Пустая реплика.")
    if engine == "piper":
        return await asyncio.to_thread(_piper_synth, vid or PIPER_DEFAULT, text, out_wav)
    if engine == "eleven":
        return await _tts_eleven(text, vid, out_wav)
    raise TTSError(f"Неизвестный голос «{key}».")


async def voice_catalog(engine: str) -> dict:
    """Список голосов движка для Mini App и чата: {ok, error, voices:[{key,name,desc,ready}]}."""
    if engine == "piper":
        if not piper_available():
            return {"ok": False, "error": "Piper не установлен на сервере (pip install piper-tts).", "voices": []}
        voices, seen = [], set()
        for vid, name, desc in PIPER_CATALOG:
            seen.add(vid)
            voices.append({"key": "piper:" + vid, "name": name, "desc": desc,
                           "ready": (PIPER_DIR / f"{vid}.onnx").exists()})
        if PIPER_DIR.exists():
            for f in sorted(PIPER_DIR.glob("*.onnx")):
                if f.stem not in seen:
                    voices.append({"key": "piper:" + f.stem, "name": f.stem, "desc": "свой файл", "ready": True})
        return {"ok": True, "error": "", "voices": voices}
    if engine == "eleven":
        try:
            return {"ok": True, "error": "", "voices": [{**v, "ready": True} for v in await eleven_list_voices()]}
        except TTSError as e:
            return {"ok": False, "error": str(e), "voices": []}
    return {"ok": False, "error": "Неизвестный движок.", "voices": []}


async def make_sample(key: str) -> Path:
    """Короткий пример голоса (кэшируется). Именно выбранной моделью — для кнопки «Прослушать»."""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / (hashlib.md5(key.encode()).hexdigest() + ".wav")
    if not path.exists():
        _, vid = split_key(key)
        text = SAMPLE_TEXT["en" if vid.startswith("en_") else "ru"]
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:6]}.tmp.wav")
        try:
            await tts_line(text, key, tmp)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    return path


async def wav_to_ogg(wav: Path) -> Path | None:
    """WAV -> OGG/Opus для голосового сообщения Telegram. None, если нет ffmpeg."""
    out = wav.with_suffix(".ogg")
    if out.exists():
        return out
    try:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
    except FileNotFoundError:
        return None
    return out if p.returncode == 0 and out.exists() else None
