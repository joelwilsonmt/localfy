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
2. Run localfy with the default `docker-compose.yml`, but set **`BIND_ADDR=127.0.0.1`**
   in `.env` first — this keeps the port off your LAN/public interfaces so the
   only way in is through `tailscale serve`.
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

### Option 3 — nginx + certbot

Install nginx and certbot (`sudo apt install nginx certbot python3-certbot-nginx`),
then create `/etc/nginx/sites-available/localfy`:

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

Enable it and get a certificate (certbot patches the config and sets up renewal):

```bash
sudo ln -s /etc/nginx/sites-available/localfy /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d localfy.yourdomain.com
```

Set `SPOTIFY_REDIRECT_URI=https://localfy.yourdomain.com/auth/callback` in `.env`
and register the same URL in the Spotify dashboard.

---

## 4 — Use it

1. Open `http://localhost:8080` (or `http://YOUR_SERVER:8080`).
2. Click **Connect with Spotify** and authorize the app.
3. Your playlists, liked songs, and saved albums appear on the dashboard.
4. Check the ones you want to track and pick a sync frequency (daily / weekly / monthly).
5. Hit **↻ Sync** to download immediately, or let the scheduler handle it.

Downloaded files land in `MUSIC_HOST_PATH` on the host (`./music/` by default),
one folder per playlist, plus `Liked Songs/`, `Albums/<Artist — Album>/` and
`Singles/` (one-off tracks downloaded from the nav search). Each synced folder
also gets a `<Name>.m3u8` playlist in Spotify order for Jellyfin, Navidrome, VLC
and friends.

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
| `AUDIO_FORMAT` | `mp3` | Output format: `mp3`, `flac`, `ogg`, `opus`, `m4a`, `wav`. Can be changed in Settings. |
| `AUDIO_BITRATE` | `320k` | Bitrate: `auto`, `128k`, `256k`, `320k`. Can be changed in Settings. |
| `MUSIC_HOST_PATH` | `./music` | Host folder mounted at `/music`. |
| `DATA_HOST_PATH` | `./data` | Host folder mounted at `/data`. |
| `PUID` / `PGID` | `1000` | Host user/group the app runs as, so downloads aren't owned by root. Use `id -u` / `id -g`. |
| `HOST_PORT` | `8080` | Host port to publish on. |
| `BIND_ADDR` | `0.0.0.0` | Host interface to bind. `127.0.0.1` keeps it reachable only through a local proxy (Tailscale, nginx). |
| `LOCALFY_DISABLE_SCHEDULER` | — | Set to `1` to turn off scheduled syncs (manual syncs still work). Handy for development. |

---

## How it works

- **Auth** — localfy handles the Spotify OAuth flow in the browser and caches your token at `/data/.cache`. spotdl only gets your app's Client ID/Secret.
- **Sync** — localfy reads each playlist, album or Liked Songs list from Spotify with your login (so private playlists work), then hands spotdl the track metadata directly. spotdl goes straight to YouTube with no per-track Spotify lookups, which is what Spotify rate-limits. Tracks are downloaded in small batches and recorded as they land.
- **Only new tracks** — every downloaded track is recorded per playlist in `/data/localfy.db`. A track that's already in a playlist's folder is recorded, not downloaded again.
- **Failed tracks** — tracks spotdl couldn't find are listed on the playlist page. Retry them, or paste the right YouTube link for a track.
- **Fast pages** — each target's track list is saved when it syncs. Pages and the player read that copy and refresh it from Spotify in the background, so they stay quick and keep working when Spotify is slow or down.
- **No deletions** — unchecking a playlist, or removing a track from it on Spotify, never deletes files already on disk.
- **Duplicates** — a track in several playlists is stored once per folder. Settings → Storage can replace the extra copies with hardlinks.

---

## Development

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check app tests
.venv/bin/pytest -q
```

To run from source, add a `docker-compose.override.yml` (gitignored) containing
`services: {localfy: {build: .}}`, then `docker compose up --build -d`. App code is
baked into the image, so rebuild after every change. Prefix with
`LOCALFY_DISABLE_SCHEDULER=1` to keep scheduled syncs from running while you work.

---

## CI / Docker image

Every push to `main` runs the lint and tests, then builds and publishes a new `linux/amd64` + `linux/arm64` image to the GitHub Container Registry via GitHub Actions (`.github/workflows/docker.yml`). Pull requests run the same checks and a test build. No configuration needed — it uses the built-in `GITHUB_TOKEN`.

To make the published package public, go to your GitHub repository → **Packages** → the `localfy` package → **Package settings** → change visibility to **Public**. This lets anyone `docker pull` without authenticating.
