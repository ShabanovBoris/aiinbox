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
