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

The shipped `docker-compose.yml` already points at the published image
(`ghcr.io/joelwilsonmt/localfy:latest`). Grab just the two files you need:

```bash
mkdir localfy && cd localfy
curl -O https://raw.githubusercontent.com/joelwilsonmt/localfy/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/joelwilsonmt/localfy/main/.env.example
# edit .env with your Spotify credentials, then:
docker compose pull
docker compose up -d
```

### Option B — Build locally from source

```bash
git clone https://github.com/joelwilsonmt/localfy.git
cd localfy
cp .env.example .env   # fill in your values
# edit docker-compose.yml: comment `image:`, uncomment `build: .`
docker compose up -d --build
```

---

## 3.5 — HTTPS for server installs

**Why this matters:** Spotify only accepts `http://` for the `127.0.0.1` loopback
address. For any other host (a server IP or domain) the redirect URI **must be
HTTPS**, or the login will fail. Pick whichever fits your setup.

### Option 1 — Caddy (public domain, automatic certs)

Easiest if you have a domain. Requires an A/AAAA record pointed at the server and
ports **80 + 443** open. A ready-made `docker-compose.caddy.yml` and `Caddyfile`
are included.

```bash
# in your .env:
#   LOCALFY_DOMAIN=localfy.yourdomain.com
#   SPOTIFY_REDIRECT_URI=https://localfy.yourdomain.com/auth/callback
docker compose -f docker-compose.caddy.yml up -d
```

Register `https://localfy.yourdomain.com/auth/callback` in the Spotify dashboard
(must match exactly). Caddy fetches and auto-renews the Let's Encrypt cert.

### Option 2 — Tailscale (no public domain, no open ports)

If your server is on a [Tailscale](https://tailscale.com) tailnet, Tailscale can
serve localfy over HTTPS on your machine's `*.ts.net` name with a valid cert — and
nothing is exposed to the public internet (only your tailnet can reach it).

1. In the Tailscale **admin console**, enable **MagicDNS** and **HTTPS
   certificates** (Settings → Keys / DNS).
2. Run localfy normally (the default `docker-compose.yml`, listening on
   `127.0.0.1:8080` is fine — you don't need to expose it publicly).
3. Find your machine's full name with `tailscale status` (e.g.
   `myserver.tailnet-abcd.ts.net`).
4. Put localfy behind Tailscale's HTTPS proxy:
   ```bash
   tailscale serve --bg --https=443 localhost:8080
   ```
   - Check it: `tailscale serve status`
   - Undo it:  `tailscale serve --https=443 localhost:8080 off`  (or `tailscale serve reset`)
5. In `.env` set
   `SPOTIFY_REDIRECT_URI=https://myserver.tailnet-abcd.ts.net/auth/callback`,
   restart localfy, and register that same URL in the Spotify dashboard.

> The OAuth redirect happens in **your browser**, so the machine you log in from
> must also be on the tailnet. If you need to reach it from outside the tailnet,
> use `tailscale funnel` instead of `serve` to expose it publicly.

### Option 3 — nginx (you already manage certs)

Point a server block at the container and proxy to it:

```nginx
server {
    listen 443 ssl;
    server_name localfy.yourdomain.com;
    # ssl_certificate / ssl_certificate_key managed by you (certbot, etc.)
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Set `SPOTIFY_REDIRECT_URI=https://localfy.yourdomain.com/auth/callback` and
register it in the Spotify dashboard.

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
