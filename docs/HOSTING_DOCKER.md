# Хостинг AIInbox на отдельном VPS через Docker Compose

Этот документ описывает production-размещение **всего AIInbox runtime** на одном VPS, а не только HTTP API.

Актуальная runtime-модель репозитория:

~~~text
один Compose-сервис: app
    ├── Telegram long polling
    ├── ProcessingWorker(s)
    ├── ProfileUpdateWorker
    ├── AskWorker
    ├── ExportWorker
    ├── DeliveryWorker       (если Telegram включён)
    ├── ReminderWorker       (если Telegram включён)
    ├── optional FastAPI /v1
    └── canonical SQLite /data/app.db
~~~

Отдельных контейнеров для Telegram, workers, HTTP API или SQLite сейчас нет. SQLite — файл в persistent Docker volume. Эта схема соответствует текущему modular-monolith runtime.

Операционные backup/restore/status команды остаются подробно описаны в [RUNBOOK.md](RUNBOOK.md). Здесь они собраны в единый VPS-сценарий: подготовка host/domain/TLS, перенос базы, безопасное переключение Telegram polling, подключение browser extension, обновление и rollback.

## 1. Что реально задаёт текущий Compose

Проверенный docker-compose.yml запускает один non-root контейнер app и монтирует:

- aiinbox_data:/data — canonical SQLite;
- aiinbox_backups:/backups — verified SQLite backup generations;
- aiinbox_exports:/exports — временные user export ZIP;
- tmpfs /tmp/aiinbox — временные media-файлы.

Также включены:

~~~text
restart: unless-stopped
healthcheck: python -m app.ops health
~~~

HTTP API публикуется **только на loopback VPS**:

~~~yaml
ports:
  - "127.0.0.1:\${HTTP_API_PORT:-8080}:\${HTTP_API_PORT:-8080}"
~~~

Compose явно задаёт внутри контейнера:

~~~text
DATABASE_URL=sqlite+aiosqlite:////data/app.db
BACKUP_DIR=/backups
EXPORT_DIR=/exports
TEMP_DIR=/tmp/aiinbox
HTTP_API_HOST=0.0.0.0
~~~

HTTP_API_HOST=0.0.0.0 относится только к container network namespace. На VPS host API остаётся доступен как 127.0.0.1:<HTTP_API_PORT>.

**Не меняйте** production mapping на 0.0.0.0:8080:8080. Публичный доступ должен идти через TLS reverse proxy на том же VPS.

## 2. Значения, которые оператор задаёт вручную

Примеры используют placeholders:

~~~text
<VPS_PUBLIC_IP>
<SSH_USER>
<REPOSITORY_URL>
<API_DOMAIN>               например aiinbox.example.com
<TELEGRAM_USER_ID>
<PREVIOUS_GOOD_SHA>
<OFFSITE_BACKUP_TARGET>    optional, но рекомендуется
~~~

Репозиторий не создаёт автоматически VPS, DNS/domain, firewall rules, SSH users/keys, TLS reverse proxy, off-host backup host или provider credentials/model IDs.

## 3. Подготовка VPS

Нужны:

- Linux VPS;
- Docker Engine;
- Docker Compose plugin (docker compose);
- Git;
- SSH;
- rsync, если используется scripts/offsite_backup.sh;
- достаточно постоянного диска для Docker images, SQLite, backups и exports.

Docker image уже содержит Python 3.12, uv, ffmpeg/ffprobe, application code и migrations.

Пример deployment directory:

~~~bash
sudo mkdir -p /opt/aiinbox
sudo chown "$USER":"$USER" /opt/aiinbox
cd /opt/aiinbox
git clone <REPOSITORY_URL> .
~~~

Используйте обычного deployment user с Docker access. Сам application container уже переключается Dockerfile на пользователя aiinbox и не работает как root.

## 4. DNS и домен HTTP API

Рекомендуется отдельный hostname:

~~~text
aiinbox.example.com
~~~

Создайте A record на <VPS_PUBLIC_IP>.

AAAA добавляйте только если IPv6 реально настроен и inbound 80/443 доступен по IPv6. Неработающий AAAA может давать перемежающиеся TLS/API ошибки у IPv6 clients.

Перед выпуском public TLS certificate убедитесь, что hostname резолвится на VPS.

## 5. Firewall и закрытие API port

Снаружи обычно нужны только:

~~~text
22/tcp   SSH или ваш вручную выбранный SSH port
80/tcp   ACME HTTP challenge / HTTP→HTTPS redirect
443/tcp  HTTPS
~~~

HTTP_API_PORT (default 8080) **не должен быть открыт публично**.

После startup проверьте host bind:

~~~bash
ss -ltnp | grep ':8080'
~~~

Ожидается:

~~~text
127.0.0.1:8080
~~~

а не:

~~~text
0.0.0.0:8080
~~~

Если используются и cloud firewall/security group, и host firewall, сохраните это ограничение в обоих местах.


## 6. TLS reverse proxy

Текущий репозиторий **не содержит** reverse-proxy Compose service. Чтобы не менять проверенную runtime-топологию, production guide предполагает reverse proxy как host-level service на том же VPS.

### Пример с Caddy

Caddy умеет автоматически получать и обновлять сертификат для public domain, если DNS указывает на VPS и 80/443 доступны.

Пример /etc/caddy/Caddyfile:

~~~caddyfile
<API_DOMAIN> {
    @aiinbox path /healthz /v1/*
    handle @aiinbox {
        reverse_proxy 127.0.0.1:8080
    }

    handle {
        respond 404
    }
}
~~~

Так наружу проксируются только runtime endpoints browser/API client:

~~~text
/healthz
/v1/*
~~~

FastAPI /docs и /openapi.json публично не нужны для browser extension.

Официальные reference:

- <https://caddyserver.com/docs/quick-starts/reverse-proxy>
- <https://caddyserver.com/docs/automatic-https>

Можно использовать Nginx, Traefik или другой TLS terminator, но boundary должен остаться тем же:

~~~text
Internet HTTPS :443
        ↓
reverse proxy на VPS
        ↓
http://127.0.0.1:<HTTP_API_PORT>
~~~

Не отключайте TLS verification в extension и не публикуйте bearer-token API через public HTTP.

## 7. Production .env

Создайте файл из template:

~~~bash
cd /opt/aiinbox
cp .env.example .env
chmod 600 .env
~~~

Минимальный skeleton:

~~~dotenv
TELEGRAM_BOT_TOKEN=<BotFather token>
ALLOWED_TELEGRAM_USER_IDS=<TELEGRAM_USER_ID>

LLM_PROVIDER=openai
OPENAI_API_KEY=<secret>
OPENAI_ANALYSIS_MODEL=<manual model id>
OPENAI_TRANSCRIPTION_MODEL=<manual model id>
OPENAI_VISION_MODEL=<manual model id if used>

# Либо существующий OpenRouter path:
# LLM_PROVIDER=openrouter
# OPENROUTER_API_KEY=<secret>
# OPENROUTER_ANALYSIS_MODEL=<manual model id>
# OPENROUTER_TRANSCRIPTION_MODEL=<manual model id>
# OPENROUTER_VISION_MODEL=<manual model id if used>

DEFAULT_TIMEZONE=<manual IANA timezone, например Europe/Helsinki>

HTTP_API_ENABLED=true
HTTP_API_PORT=8080
HTTP_API_TOKEN=<strong random token, минимум 32 символа>
HTTP_API_USER_TELEGRAM_ID=<TELEGRAM_USER_ID>
HTTP_ASK_RESULT_TTL_SECONDS=3600
~~~

Сгенерировать HTTP token можно, например:

~~~bash
openssl rand -hex 32
~~~

Не храните .env в Git. Telegram token, LLM credentials, HTTP token, SSH keys и TLS private keys не должны попадать в repo.

### Пути, которыми управляет Compose

В Compose deployment не нужно переопределять через .env:

~~~text
DATABASE_URL
BACKUP_DIR
EXPORT_DIR
TEMP_DIR
HTTP_API_HOST
~~~

потому что docker-compose.yml задаёт их container values явно.

### Optional Instagram cookies

INSTAGRAM_COOKIES_FILE поддерживается приложением, но текущий Compose не монтирует host cookie file автоматически. Если эта optional возможность нужна, добавьте отдельно review'ed read-only bind mount и укажите **container path** в INSTAGRAM_COOKIES_FILE.

Не переносите browser profile/cookies в Git, SQLite backup или user export.

## 8. Сборка image до cutover

Для миграции существующего инстанса сначала подготовьте VPS, не запуская второй Telegram polling consumer:

~~~bash
cd /opt/aiinbox
docker compose build
~~~

Не запускайте два процесса с одним TELEGRAM_BOT_TOKEN в long-polling режиме.

Для совершенно нового AIInbox без существующей базы можно перейти сразу к разделу «Запуск runtime».

## 9. Безопасный перенос существующей SQLite базы

Не копируйте live app.db из-под работающего приложения. При WAL могут существовать app.db-wal/app.db-shm, а простое копирование одного файла не является поддерживаемой backup-процедурой.

Используйте существующий repository path:

~~~text
canonical SQLite
→ SQLite Online Backup API
→ integrity_check
→ foreign_key_check
→ .db + .sha256
→ transfer
→ verify-copy
→ restore
~~~

### 9.1 Финальный snapshot источника

Чтобы после snapshot не появлялись новые Telegram writes, сначала остановите старый application runtime.

Старый Docker Compose deployment:

~~~bash
cd <OLD_DEPLOYMENT_DIR>
docker compose stop app
docker compose run --rm --no-deps app python -m app.ops backup
~~~

Старый local uv deployment:

~~~bash
# сначала остановить uv run python -m app.main
uv run python -m app.ops backup
~~~

Команда печатает exact generation, например:

~~~text
backup=/backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db checksum=/backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256 integrity=ok rotated=0
~~~

Для Docker source можно вывести exact generation из backup volume на host через stdout:

~~~bash
mkdir -p transfer

docker compose run --rm --no-deps app \
  cat /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db \
  > transfer/aiinbox-YYYYMMDDTHHMMSSffffffZ.db

docker compose run --rm --no-deps app \
  cat /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256 \
  > transfer/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256
~~~

После этого старый instance оставьте остановленным до окончания cutover.

### 9.2 Передача поколения на VPS

~~~bash
rsync -av transfer/aiinbox-YYYYMMDDTHHMMSSffffffZ.db* \
  <SSH_USER>@<VPS_PUBLIC_IP>:/opt/aiinbox/recovery/
~~~

На VPS:

~~~bash
cd /opt/aiinbox
ls -l recovery/
~~~

### 9.3 Перенос в Compose backup volume

Сохраните исходные filenames: .sha256 sidecar проверяет имя DB.

~~~bash
docker compose run --rm --no-deps --user 0 \
  -v "$PWD/recovery:/recovery:ro" \
  app sh -c '
    set -eu
    cp /recovery/aiinbox-YYYYMMDDTHHMMSSffffffZ.db /backups/
    cp /recovery/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256 /backups/
    chown aiinbox:aiinbox /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db*
    chmod 600 /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db*
  '
~~~

Проверьте transferred bytes и SQLite contents:

~~~bash
docker compose run --rm --no-deps app \
  python -m app.ops verify-copy \
  --database /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db \
  --checksum-file /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db.sha256
~~~

Ожидается checksum=ok integrity=ok.

### 9.4 Restore в fresh data volume

Если VPS app ещё не запускался и /data/app.db не существует:

~~~bash
docker compose run --rm --no-deps app \
  python -m app.ops restore \
  --backup /backups/aiinbox-YYYYMMDDTHHMMSSffffffZ.db \
  --target /data/app.db
~~~

restore намеренно отказывается перезаписывать existing target.

Если /data/app.db уже появился, не удаляйте его вслепую. Остановите app, восстановите snapshot в /data/app-restored.db, проверьте его, затем используйте штатный swap из [RUNBOOK.md](RUNBOOK.md#restore-drill--recovery), архивируя старый app.db и WAL/SHM sidecars вместе.

## 10. Preflight restored deployment

До запуска основного process:

~~~bash
cd /opt/aiinbox
docker compose run --rm --no-deps app python -m app.ops verify
docker compose run --rm --no-deps app python -m app.ops status
~~~

Если база создана более старой версией приложения, app.main при обычном startup выполнит alembic upgrade head автоматически.

Сохраните transferred verified generation до полного acceptance нового deployment. Для rollback после schema changes используйте matching pre-upgrade backup, а не предположение, что любой alembic downgrade безопасен.
