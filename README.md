# localfy

Self-hosted Spotify sync — connect your Spotify account, select playlists to track, and localfy downloads new songs automatically on your chosen schedule (daily / weekly / monthly) using [spotdl](https://github.com/spotDL/spotify-downloader).

Unchecking a playlist pauses syncing without deleting any already-downloaded files.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A free [Spotify Developer app](https://developer.spotify.com/dashboard) (takes ~2 minutes to create)

---

## 1 — Create a Spotify Developer App

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and create an app.
2. In the app settings, add a **Redirect URI**:
   - Local: `http://127.0.0.1:8080/auth/callback`
   - Server: `https://localfy.yourdomain.com/auth/callback`
3. Copy the **Client ID** and **Client Secret**.

> **Note:** Spotify does not accept `localhost` as a redirect URI (blocked for all apps created after April 2025).
> For local dev use the explicit loopback IP `http://127.0.0.1:8080/auth/callback`.
> Any non-loopback host must use `https://`.
> See [Server deployment with HTTPS](#server-deployment-with-https) below.

---

## 2 — Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080/auth/callback
```

---

## 3 — Run

### Option A — Pull the pre-built image (recommended)

Replace `YOUR_GITHUB_USERNAME` and `YOUR_REPO_NAME` with your GitHub username and repository name.

```bash
# docker-compose.yml — swap the build section for an image pull
```

Create a `docker-compose.yml` (or edit the existing one, replacing `build: .` with the image line):

```yaml
services:
  localfy:
    image: ghcr.io/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME:latest
    ports:
      - "8080:8080"
    volumes:
      - ./music:/music
      - ./data:/data
    env_file: .env
    restart: unless-stopped
```

Then:

```bash
docker compose pull
docker compose up -d
```

### Option B — Build locally from source

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME.git
cd YOUR_REPO_NAME
cp .env.example .env   # fill in your values
docker compose up -d --build
```

---

## 4 — Use it

1. Open `http://localhost:8080` (or `http://YOUR_SERVER:8080`).
2. Click **Connect with Spotify** and authorize the app.
3. Your playlists, liked songs, and saved albums appear on the dashboard.
4. Check the ones you want to track and pick a sync frequency (daily / weekly / monthly).
5. Hit **↻ Sync** to download immediately, or let the scheduler handle it.

Downloaded files land in `./music/` on the host, organized by playlist name.

---

## Updating

```bash
docker compose pull
docker compose up -d
```

A new image is published automatically on every push to `main`.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | — | **Required.** From your Spotify Developer app. |
| `SPOTIFY_CLIENT_SECRET` | — | **Required.** From your Spotify Developer app. |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8080/auth/callback` | Must match exactly what's registered in the Spotify dashboard. Use loopback IP for local dev; HTTPS domain for servers. |
| `DOWNLOAD_PATH` | `/music` | Where audio files are saved inside the container. |
| `DATA_PATH` | `/data` | Where the database and sync state are stored. |
| `AUDIO_FORMAT` | `mp3` | Output format: `mp3`, `flac`, `ogg`, `opus`, `m4a`. |
| `AUDIO_BITRATE` | `320k` | Bitrate: `128k`, `256k`, `320k`. |

---

## How it works

- **Auth** — localfy handles the Spotify OAuth flow in-browser. Your token is cached at `/data/.cache`. spotdl uses your app's Client ID/Secret for metadata lookup (no separate login needed in the container).
- **Sync** — playlists and albums use spotdl's `.spotdl` sync files (stored in `/data/sync/`), so only newly added tracks are downloaded on each run.
- **Liked songs** — fetched via Spotify API, diffed against a local database, and only new additions are downloaded.
- **No deletions** — unchecking a playlist stops future downloads. Files already on disk are never touched.

---

## Server deployment with HTTPS

Spotify requires HTTPS for any non-loopback redirect URI. The simplest approach is nginx as a reverse proxy with a free certificate from Let's Encrypt.

**1. Install nginx and certbot**

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

**2. Create an nginx site config** at `/etc/nginx/sites-available/localfy`:

```nginx
server {
    server_name localfy.yourdomain.com;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

**3. Enable and get a certificate**

```bash
sudo ln -s /etc/nginx/sites-available/localfy /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d localfy.yourdomain.com
```

Certbot patches the config automatically and sets up auto-renewal.

**4. Update your `.env`**

```env
SPOTIFY_REDIRECT_URI=https://localfy.yourdomain.com/auth/callback
```

**5. Update the Spotify dashboard**

Add `https://localfy.yourdomain.com/auth/callback` as a Redirect URI in your Spotify app settings.

---

## CI / Docker image

Every push to `main` builds and publishes a new image to the GitHub Container Registry via GitHub Actions (`.github/workflows/docker.yml`). No configuration needed — it uses the built-in `GITHUB_TOKEN`.

To make the published package public, go to your GitHub repository → **Packages** → the `localfy` package → **Package settings** → change visibility to **Public**. This lets anyone `docker pull` without authenticating.
