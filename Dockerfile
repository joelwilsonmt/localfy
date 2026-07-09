FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install spotdl in its own isolated environment so it can't conflict with our app.
# Install to a world-readable location (not the default /root/.local, which is
# mode 700) so the container also works when run as a non-root user, e.g.
# `user: "1000:1000"` in docker-compose to get correctly-owned music files.
RUN pip install pipx && PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install 'spotdl==4.5.0'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
