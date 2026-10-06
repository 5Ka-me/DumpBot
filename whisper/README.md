# Локальный Whisper для Dump Bot

Небольшой HTTP-сервис распознавания речи: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(CTranslate2, процессор, `int8`) за API на aiohttp. Бот обращается к нему, когда в `.env` задан
`LOCAL_STT_URL`; порядок «chutes ↔ локальный» выбирается в боте (`/settings` → «🎙 Голос»).
Как включить и сколько нужно железа — в [README бота](../README.md#локальный-whisper-необязательно);
решения — в [docs/features/iteration-2.md](../docs/features/iteration-2.md), раздел B.

## Запуск

В составе стека бота (сервис `whisper`, профиль compose `whisper`, наружу не публикуется):

```bash
docker compose --profile whisper up -d --build
docker compose --profile whisper run --rm whisper python app.py --download   # скачать модель заранее (необязательно)
docker compose logs -f whisper
```

Отдельно (например, для проверки):

```bash
docker build -t dumpbot-whisper:local whisper/
docker run --rm -p 127.0.0.1:9000:9000 -e WHISPER_MODEL=tiny -v whisper-models:/models dumpbot-whisper:local
curl http://127.0.0.1:9000/health
```

## API

`POST /transcribe` — JSON `{"audio_b64": "<base64 файла>", "language": "ru" | "en" | null}`
(`null` — язык определится сам). Формат файла любой, который читает FFmpeg: OGG/Opus (голосовые Telegram), MP3,
M4A, WAV… Декодирует PyAV, который идёт вместе с faster-whisper (FFmpeg в образе не нужен).

| Ответ | Когда |
|---|---|
| `200 {"text", "language", "duration", "processing_ms", "model"}` | распознано (`text` может быть пустым — тишина) |
| `400 {"error"}` | не JSON, нет `audio_b64`, не base64, неизвестный язык, файл не декодируется |
| `413 {"error"}` | аудио длиннее `WHISPER_MAX_AUDIO_S` или тело больше `WHISPER_MAX_BODY_MB` |
| `503 {"error"}` | модель не загрузилась (например, нет интернета при первом скачивании) — повторите позже |

`GET /health` — `{"status": "ok", "model": "small", "loaded": false}` (`loaded` — модель уже в памяти).

Запросы обрабатываются по одному (остальные ждут в очереди): так память ограничена, а ядра не делятся между
двумя распознаваниями. Пока идёт распознавание, `/health` отвечает.

## Настройки (переменные окружения)

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium` (или id CTranslate2-модели на Hugging Face) |
| `WHISPER_THREADS` | `0` | потоков CPU; `0` — все доступные ядра |
| `WHISPER_BEAM_SIZE` | `1` | 1 — быстрее всего; 5 — чуть точнее, но медленнее |
| `WHISPER_COMPUTE_TYPE` | `int8` | тип вычислений CTranslate2 |
| `WHISPER_VAD` | `1` | вырезать тишину перед распознаванием (меньше «галлюцинаций» на паузах) |
| `WHISPER_MAX_AUDIO_S` | `120` | аудио длиннее — ответ 413 (бот отдаст его в chutes) |
| `WHISPER_MAX_BODY_MB` | `32` | максимальный размер JSON-запроса |
| `WHISPER_MODELS_DIR` | `/models` | кэш моделей (том `whisper-models`) |
| `WHISPER_PORT` | `9000` | порт |

В `compose.yaml` из `.env` пробрасываются `WHISPER_MODEL`, `WHISPER_THREADS`, `WHISPER_BEAM_SIZE`,
`WHISPER_MAX_AUDIO_S` и лимит памяти `WHISPER_MEM_LIMIT` (по умолчанию `2g`).

## Модель и сеть

Модель загружается лениво — при первом запросе (или командой `--download`): сначала ищется в `WHISPER_MODELS_DIR`
без сети, и только если её там нет, скачивается с Hugging Face (один раз, в том). Затем она остаётся в памяти до
остановки контейнера. Больше сервис в интернет не ходит. Звук и тексты в логи не пишутся — только длительность,
время обработки и число символов.

## Разработка

Зависимости — свои (`requirements.txt`), отдельно от бота; все пакеты ставятся готовыми wheel для amd64 и arm64.
PyAV закреплён ниже 19: faster-whisper 1.2.1 передаёт в `av.open` параметр `metadata_errors`, которого в PyAV 19 нет.

Тесты сервиса — в общем наборе бота (`tests/whisper/`), с фейковой моделью и декодером, без faster-whisper и
скачиваний: `uv run pytest tests/whisper`.
