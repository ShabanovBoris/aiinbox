# Personal AI Inbox

Personal AI Inbox — личный Telegram-бот, который принимает текст, ссылки,
голосовые сообщения, YouTube и видео, сохраняет их в SQLite, извлекает
содержимое, анализирует его через сменный LLM-провайдер и возвращает компактный
результат с приоритетом и действиями.

## Требования

- Python 3.12+
- `uv` для локальной разработки
- Telegram bot token и allowlist user id
- OpenAI или OpenRouter API key и model ids для анализа (и transcription для voice/audio)
- `ffmpeg` для video visual analysis и long-audio OpenRouter STT

## Архитектура

Это single-process modular monolith:

```text
Telegram → ingestion → SQLite queue → extraction/normalization
         → structured LLM analysis → deterministic priority → Telegram
```

SQLite остаётся canonical source of truth для Items, checkpoints, профиля,
очереди, actions и reminders. Внешние adapters изолируют Telegram, HTTP,
yt-dlp, ffmpeg и OpenAI от domain/application logic.

Playwright fallback намеренно отключён: текущий SSRF boundary не позволяет
безопасно выпускать браузер в сеть. Страница с недостаточным текстом получает
контролируемую ошибку, а не unrestricted browser access.

## Локальный запуск

```bash
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS,
# OPENAI_API_KEY/OPENROUTER_API_KEY и соответствующие model ids
uv sync
uv run python -m app.main
```

Миграции применяются автоматически при старте. Без `TELEGRAM_BOT_TOKEN` процесс
запускается в headless worker mode, что удобно для smoke-проверки.

### Создание Telegram-бота

1. Откройте `@BotFather` в Telegram и выполните `/newbot`.
2. Сохраните выданный token только в `.env` как `TELEGRAM_BOT_TOKEN`.
3. Узнайте numeric Telegram user id и укажите его в
   `ALLOWED_TELEGRAM_USER_IDS`.
4. Запустите приложение и отправьте боту `/start`.

Профиль пользователя можно предварительно задать в `profile.yaml`; пример
формата находится в `profile.example.yaml`. Путь настраивается через
`PROFILE_SEED_FILE`.

## Docker

Образ содержит Python 3.12 и `ffmpeg`; Python-зависимость `yt-dlp` входит в
package dependencies. SQLite хранится в volume, чтобы перезапуск контейнера
не терял очередь и checkpoints:

```bash
docker build -t personal-ai-inbox .
docker run --rm --env-file .env \
  -e DATABASE_URL=sqlite+aiosqlite:////data/app.db \
  -v "$(pwd)/data:/data" \
  personal-ai-inbox
```

Для Docker нужен абсолютный SQLite URL `sqlite+aiosqlite:////data/app.db`:
третья форма (`///data/app.db`) означает относительный путь внутри `/app`, а
четыре слеша направляют SQLite в persistent volume `/data`.
Не запускайте дополнительные инфраструктурные контейнеры: MVP рассчитан на
один процесс и SQLite.

Эквивалентный compose-запуск:

```bash
mkdir -p data temp
docker compose up --build -d
docker compose logs -f app
docker compose down
```

Compose монтирует SQLite в `./data` и временные media files в `./temp`.

## Проверки

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Для диагностики SQLite, зависших Items и ручного retry см. [docs/RUNBOOK.md](docs/RUNBOOK.md).
Полный продуктовый контракт находится в [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md),
а решения и история внешних review — в `docs/DECISIONS.md` и `docs/REVIEWS.md`.

## Конфигурация

Все настройки читаются из окружения через Pydantic Settings. Секреты не должны
попадать в Git. Важные параметры:

- `DATABASE_URL`, `PROCESSING_CONCURRENCY`, `PROCESSING_POLL_SECONDS`;
- `PROCESSING_TIMEOUT_SECONDS`, `SHUTDOWN_TIMEOUT_SECONDS`;
- `WEB_TIMEOUT_SECONDS`, `MAX_DOWNLOAD_BYTES`, `WEB_MAX_ATTEMPTS`;
- `MAX_AUDIO_BYTES`, `TRANSCRIPTION_TIMEOUT_SECONDS`;
- `YOUTUBE_MAX_*`, `VIDEO_FRAME_INTERVAL_SECONDS`, `VIDEO_MAX_FRAMES`;
- `OPENAI_*`, `OPENROUTER_*`, `CONTENT_CHUNK_MAX_CHARS`, `CONTENT_CHUNK_OVERLAP_CHARS`,
  `DEFAULT_TIMEZONE`, `PROFILE_SEED_FILE`.

Чтобы сменить LLM, измените `LLM_PROVIDER` и соответствующие model IDs в `.env`
после остановки приложения. Поддержаны `LLM_PROVIDER=openai` и
`LLM_PROVIDER=openrouter`. OpenRouter использует OpenAI-compatible adapters
через `OPENROUTER_BASE_URL=https://openrouter.ai/api/v1`; analysis-модель должна
поддерживать JSON Schema structured output, vision-модель — image input, а
transcription-модель — `/audio/transcriptions`. Для OpenRouter длинное или
крупное аудио автоматически режется ffmpeg на 5-минутные AAC-сегменты перед STT:
это удерживает multipart upload ниже 25 MB и снижает риск upstream timeout.
Ollama остаётся post-MVP.

External content is data, not instructions: analysis prompts explicitly isolate
prompt injection, and Telegram/LLM provider credentials are supplied only through
environment configuration.
