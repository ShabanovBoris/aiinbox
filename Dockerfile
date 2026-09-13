FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/app.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY profile.example.yaml ./
RUN pip install --no-cache-dir .

COPY .env.example ./
COPY docs ./docs

RUN mkdir -p /data /app/temp

CMD ["python", "-m", "app.main"]
