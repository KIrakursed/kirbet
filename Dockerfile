FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements.txt
# Zendriver (all browser-based bookmaker adapters) drives the system Chromium
ENV BROWSER_PATH=/usr/bin/chromium BROWSER_NO_SANDBOX=true
COPY app ./app
COPY config ./config
COPY docs ./docs
COPY migrations ./migrations
COPY alembic.ini .
RUN useradd -m scanner && mkdir -p /app/data && chown -R scanner /app
USER scanner
CMD ["sh", "-c", "alembic upgrade head && python -m app.cli bot"]
