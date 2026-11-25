# TajStudio — что изменилось

Файлы: `bot.py`, `tts.py` (новый), `prepare_character.py`, `index.html`, `requirements.txt`.
`blender_worker.py` и `split_sheet.py` без изменений. Замените файлы в репозитории целиком.

## Переменные окружения (Bothost)
| Переменная | Зачем |
|---|---|
| `BOT_TOKEN`, `FIREBASE_CRED_JSON`, `FIREBASE_DB_URL` | как раньше |
| `MINIAPP_URL` | адрес вашего бота на Bothost (`https://…`), **не GitHub Pages** — оттуда Mini App берёт API |
| `ELEVENLABS_API_KEY` | нужен только для озвучки ElevenLabs |
| `ELEVEN_DEFAULT_VOICE` | необязательно: id голоса ElevenLabs по умолчанию. Если пусто — берётся первый голос аккаунта |
| `PIPER_DEFAULT_VOICE` | необязательно, по умолчанию `ru_RU-irina-medium` |
| `MAX_RENDERS` | сколько рендеров одновременно (по умолчанию 1) |
| `YADISK_TOKEN`, `ADMIN_IDS`, `BLENDER_BIN` | как раньше |

`ELEVEN_VOICES` (старый формат `{"Маша":"voice_id"}`) всё ещё работает.

## Piper
* Ставится из `requirements.txt` (`piper-tts`).
* Голоса качаются сами с huggingface при первом выборе/использовании (около 60 МБ каждый) в папку `voices/`.
* Если хостинг не пускает на huggingface: скачайте `NAME.onnx` и `NAME.onnx.json` вручную, положите в `voices/` — голос появится в списке.
* Голоса в списке: Ирина, Дмитрий, Денис, Руслан (русские), Lessac, Amy, Ryan (английские). Список — `PIPER_CATALOG` в `tts.py`.
* Таджикского голоса в Piper нет.

## Не реализовано
* Музыка и звуковые эффекты — убраны из Mini App (в рендере их не было).
* Баланс/оплата — списания в боте нет; в интерфейсе скрыто (`CONFIG.BILLING` в `index.html`).
