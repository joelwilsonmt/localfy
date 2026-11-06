FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install spotdl in its own isolated environment so it can't conflict with our
# app. Installed system-wide (not under /root) so the non-root runtime user can
# run it.
ENV PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
RUN pip install --no-cache-dir pipx && pipx install 'spotdl==4.5.0'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# spotdl and yt-dlp keep small caches under $HOME. The runtime user's uid comes
# from PUID at start-up, so give it a home any uid can write to.
ENV HOME=/home/localfy
RUN mkdir -p "$HOME" && chmod 1777 "$HOME" && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
