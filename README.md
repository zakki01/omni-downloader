# Omni Downloader

A precision media downloader web application built with FastAPI, `yt-dlp`, and FFmpeg, styled with a luxury black and metallic gold spatial aesthetic. Designed for clean local execution and containerized deployment on Render.

---

## Features

- **Format & Resolution Extraction**: Automatically discovers available numeric video heights and stream containers.
- **Dynamic Quality Selectors**: Choose between **Best Quality**, specific resolutions (`1080p`, `720p`, `480p`, etc.), or **Audio Only (MP3)**.
- **Non-blocking Worker**: In-process background worker processes downloads concurrently while streaming progress updates.
- **Real-time Polling**: Frontend polls progress (`0%` to `100%`) with automated file triggering upon completion.
- **Audited Security Controls**: Full SSRF protection against private IP ranges/redirects, safe filename sanitization, rate limiting, and path traversal defense.
- **Production Container Ready**: Built-in Dockerfile with FFmpeg pre-configured, dynamic `$PORT` binding, and `/health` monitoring.

---

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, Uvicorn, `yt-dlp`, FFmpeg, Pydantic
- **Frontend**: Vanilla HTML5, Vanilla CSS3 (Black & Metallic Gold 2D Spatial Theme), Vanilla JavaScript (no frameworks)
- **Containerization**: Docker (Debian slim + FFmpeg)
- **Deployment**: Render Web Service (`render.yaml`)

---

## Local Development

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/your-username/omni-downloader.git
cd omni-downloader
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
pip install pytest httpx
```

### 3. Run the Development Server

```bash
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Open your browser at `http://127.0.0.1:8000`.

### 4. Run Automated Tests

```bash
pytest tests/test_app.py -v
```

---

## FFmpeg Installation

FFmpeg is required to merge separated video/audio streams into browser-compatible MP4 files and extract MP3 audio.

- **Windows (via WinGet)**:
  ```powershell
  winget install Gyan.FFmpeg
  ```
- **macOS (via Homebrew)**:
  ```bash
  brew install ffmpeg
  ```
- **Linux (Debian / Ubuntu)**:
  ```bash
  sudo apt-get update && sudo apt-get install -y ffmpeg
  ```

---

## Docker

The included `Dockerfile` packages Python 3.11-slim, installs FFmpeg via `apt-get`, sets up dependencies, and binds dynamically to `$PORT`.

### Build Image

```bash
docker build -t omni-downloader .
```

### Run Container Locally

```bash
docker run -p 8000:8000 -e PORT=8000 omni-downloader
```

Visit `http://localhost:8000` to verify the UI and test media downloads.

---

## GitHub Setup

1. Initialize git and commit the codebase:
   ```bash
   git init
   git add .
   git commit -m "feat: production-ready Omni Downloader with black/gold UI and Render deployment"
   ```

2. Link to your GitHub repository and push:
   ```bash
   git remote add origin https://github.com/<your-username>/omni-downloader.git
   git branch -M main
   git push -u origin main
   ```

3. GitHub Actions CI (`.github/workflows/test.yml`) will automatically run unit and security tests on push/PR without consuming external bandwidth.

---

## Deploy to Render

Deploying Omni Downloader to Render takes only a few minutes:

### Method 1: Render Blueprint (`render.yaml` - Recommended)

1. Log in to [Render Dashboard](https://dashboard.render.com/).
2. Click **New +** and select **Blueprint**.
3. Connect your GitHub repository.
4. Render detects `render.yaml` and configures a **Docker Web Service**.
5. Click **Apply**. Render will build the container, install FFmpeg, assign a public URL, and start the app.

### Method 2: Manual Web Service

1. On the Render Dashboard, click **New +** > **Web Service**.
2. Connect your GitHub repository.
3. Select **Docker** as the Environment / Runtime.
4. Leave Root Directory blank and Build/Start commands default (handled by `Dockerfile`).
5. Set Health Check Path to `/health`.
6. Select your desired instance type (e.g. Free) and click **Create Web Service**.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Port assigned by the hosting environment (Render injects this dynamically). |
| `PYTHONUNBUFFERED` | `1` | Ensures real-time stdout/stderr log streaming in Docker containers. |

---

## Security

- **Strict SSRF & Private Network Shield**: All submitted URLs and downstream HTTP redirects are resolved and blocked if they point to loopback (`127.0.0.1`, `localhost`), link-local (`169.254.169.254`), private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), or multicast/carrier-grade NAT IP blocks.
- **Filename Sanitization**: Reserved filesystem characters (`/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`) and path traversal sequences (`..`) are stripped before writing or serving files.
- **In-Memory Rate Limiting**: Action endpoints (`/api/info` and `/api/download`) limit requests to 5 per minute per client IP.
- **Sensitive Data Isolation**: `cookies.txt` and `.env` are protected by `.gitignore` and `.dockerignore`, and are never served through static routes or logged in output.

---

## Temporary Download Storage

- **Ephemeral Storage**: Media files are saved into `./downloads/` solely for immediate user retrieval.
- **Automated Lifecycle Purge**: A background task cleans up completed files older than **1 hour**.
- **Hosting Considerations**: On free hosting tiers (such as Render Free), instance disks are ephemeral. Files are not persisted across container restarts, deploys, or sleep cycles. Users must download files promptly upon completion.

---

## Limitations

- **DRM Protection**: Media requiring Widevine, PlayReady, or FairPlay DRM circumvention cannot be downloaded. The server responds with: `"This video is DRM-protected and cannot be downloaded."`
- **Supported Sources**: Downloads are subject to `yt-dlp` extractor compatibility. Not all arbitrary websites or paywalled streams are supported.
- **Single-Item Scope**: Phase 1 processes single video URLs only (playlist mode and concurrent multi-item queues are deferred).

---

## Roadmap

- **Phase 1 (Complete)**: Core `yt-dlp` downloader with FFmpeg integration, Black + Gold UI, and Docker/Render configuration.
- **Phase 2**: Playwright headless browser fallback for JS-rendered players and challenging bot gates.
- **Phase 3**: Playlist discovery, batch resolution, and multi-file selection.
- **Phase 4**: Redis-backed persistent job queue with distributed background workers.
- **Phase 5**: Object storage integration (S3/GCS) and user quotas for horizontal production scaling.
