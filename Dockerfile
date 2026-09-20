FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY pyproject.toml ./
COPY backend ./backend

RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Runs migrations before starting the server, baked in here rather than
# relying on a deploy platform's own "start command" override - found live
# deploying to Coolify: its custom start_command field (set via the API)
# was silently not applied for a plain Dockerfile build, uvicorn started
# with no migration step at all, and the app crashed on boot with
# `relation "users" does not exist`. docker-compose.yml's own `command:`
# override still takes precedence locally (compose overrides a Dockerfile's
# CMD the same way either way), so local dev is unaffected - this is what
# makes the image self-sufficient for any other deploy target too.
CMD ["sh", "-c", "alembic -c backend/app/db/migrations/alembic.ini upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000"]
