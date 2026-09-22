FROM python:3.12.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/app.db \
    TEMP_DIR=/tmp/aiinbox \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY profile.example.yaml ./
RUN uv sync --frozen --no-dev

COPY .env.example ./
COPY docs ./docs

RUN groupadd --system --gid 10001 aiinbox \
    && useradd --system --uid 10001 --gid aiinbox --home-dir /app aiinbox \
    && mkdir -p /data /tmp/aiinbox \
    && chown -R aiinbox:aiinbox /data /tmp/aiinbox

USER 10001:10001

CMD ["python", "-m", "app.main"]
