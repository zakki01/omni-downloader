# Omni Downloader

A secure, self-hostable media downloader powered by FastAPI, `yt-dlp`, and FFmpeg, featuring a luxury Black + Metallic Gold spatial interface.

---

## Features

- **URL-based Media Extraction**: Discover and extract available stream streams and video containers.
- **Dynamic Quality Selection**: Dynamic selection for Best Quality, specific heights (`1080p`, `720p`, `480p`, `360p`), or Audio Only.
- **Audio Extraction**: Automated MP3 extraction powered by FFmpeg.
- **Real-Time Download Progress**: Background workers track downloading and post-processing percentage through live polling.
- **HLS / DASH Support**: Merges multi-stream segmented media streams supported by `yt-dlp`.
- **Temporary File Lifecycle**: Safe temporary storage with automatic 1-hour purge routine.
- **Hardened Security**: Server-Side Request Forgery (SSRF) defense across initial requests and redirects, strict loopback/private IP blocking, and path traversal guards.
- **Abuse Prevention**: Sliding-window rate limiting protecting system resources.
- **DRM Detection**: Transparent identification and rejection of encrypted DRM streams.
- **Production Container Ready**: Built-in Dockerfile with FFmpeg pre-installed and dynamic `$PORT` handling.
- **Render Deployment Support**: Blueprint deployment via `render.yaml`.
- **Premium Black-and-Gold Interface**: Clean, accessible spatial layout built with vanilla HTML, CSS, and JavaScript.

---

## Architecture

- **Frontend**: Vanilla HTML5, Vanilla CSS3, Vanilla JavaScript (zero external UI frameworks).
- **Backend**: FastAPI & Starlette (asynchronous Python runtime).
- **Media Extraction**: `yt-dlp`.
- **Media Processing**: FFmpeg.
- **Deployment**: Docker container on Render Web Services.

---

## Requirements

- Python 3.11+
- FFmpeg
- Node.js (or Deno) — required by `yt-dlp` for YouTube signature and JavaScript challenge solving
- Docker (recommended for containerized deployment)

---

## Local Development

### 1. Clone & Create Virtual Environment

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
uvicorn app:app --reload
```

Open your browser to `http://127.0.0.1:8000`.

### 4. Run Automated Tests

```bash
pytest tests/test_app.py -v
```

---

## FFmpeg Installation

FFmpeg is required for merging separated audio/video streams into MP4 files and converting audio to MP3:

- **Windows (WinGet)**: `winget install Gyan.FFmpeg`
- **macOS (Homebrew)**: `brew install ffmpeg`
- **Linux (Debian/Ubuntu)**: `sudo apt-get update && sudo apt-get install -y ffmpeg`

---

## Docker

The included `Dockerfile` starts from `python:3.11-slim`, installs FFmpeg and CA certificates, and binds to `0.0.0.0:${PORT:-8000}`.

### Build the Image

```bash
docker build -t omni-downloader .
```

### Run the Container Locally

```bash
docker run -p 8000:8000 -e PORT=8000 omni-downloader
```

Navigate to `http://localhost:8000` to verify the UI and test downloads.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port on which the Uvicorn web server listens (injected dynamically by Render). |
| `PYTHONUNBUFFERED` | `1` | Ensures real-time stdout/stderr log output in Docker containers. |
| `POT_PROVIDER_URL` | *None (`http://127.0.0.1:4416`)* | URL of the local or internal PO Token Provider server. |
| `YOUTUBE_PO_TOKEN` | *None* | Fallback manual Proof of Origin (PO) token / GVS credentials string (e.g. `web.gvs+TOKEN` or `TOKEN`). |
| `YOUTUBE_PLAYER_CLIENTS` | *None (default)* | Comma-separated list of player clients (e.g. `android,web,mweb`). |
| `COOKIES_FILE_PATH` | *None* | Absolute path to a Netscape-formatted `cookies.txt` file on the server. |
| `YOUTUBE_COOKIES_TEXT` | *None* | Raw Netscape cookies text content passed directly via environment variable. |

---

## YouTube Extraction & Cloud / Datacenter Deployments

### The Datacenter IP Challenge
Major cloud hosting providers (Render, AWS, GCP, DigitalOcean, Hetzner) operate IP ranges that are actively monitored and challenged by YouTube's automated bot protection systems. When an unauthenticated server request originates from these cloud ranges, YouTube returns:

```text
ERROR: [youtube] <video_id>: Sign in to confirm you’re not a bot.
Use --cookies-from-browser or --cookies for the authentication.
```

Omni Downloader adheres strictly to ethical, legal, and security guidelines:
- **No CAPTCHA bypass**: We do not implement automated CAPTCHA solvers or anti-bot circumvention hacks.
- **Accurate Error Messaging**: When YouTube triggers a bot verification challenge, Omni Downloader returns a concise, clear explanation:
  > *"YouTube requires additional verification for this server request. Please try again later or use another supported source."*
- **No False DRM Claims**: Bot challenges are strictly classified and never mislabeled as DRM.

### The Smart Production Architecture

Omni Downloader isolates YouTube extraction using a provider-aware architecture:

1. **JavaScript Challenge Solver (`yt-dlp-ejs`)**:
   Runs using bundled Deno or Node.js >= 22 inside the Docker container to solve signature ciphering and `n` challenges.
2. **Dynamic PO Token Provider (`bgutil-ytdlp-pot-provider`)**:
   Rather than relying on static, hardcoded tokens that expire, Omni Downloader integrates the `bgutil-ytdlp-pot-provider` plugin and runs a local, unexposed HTTP provider server on `127.0.0.1:4416` inside the container.
3. **Optional Authorized Access Fallback (`cookies.txt`)**:
   - Render Secret Files: Add a Secret File named `cookies.txt` in the Render Dashboard. It is automatically mounted at `/etc/secrets/cookies.txt` and detected on startup.
   - Alternatively, pass the raw cookie contents via `YOUTUBE_COOKIES_TEXT`.
   - Never commit cookies to GitHub or Docker images.

---

## Limitations

- **Platform Compatibility**: Compatibility with third-party platforms can change without notice. Omni Downloader does not claim to support every website or private service.
- **DRM Content**: DRM-protected media (Widevine, PlayReady, FairPlay) is strictly rejected and cannot be downloaded.
- **Ephemeral Storage**: Render Web Services operate on an ephemeral filesystem. Downloaded files are temporarily stored for immediate retrieval and automatically purged after 1 hour. Files do not persist across instance restarts or sleep cycles.
- **Single-Instance In-Memory State**: Active job tracking is currently managed in-memory via `InMemoryJobStore`. For multi-instance scaling, Redis will be introduced in Phase 4.

---

## Roadmap

- **Phase 1 (Completed)**: Core downloader with FFmpeg integration, Black + Gold UI, Docker/Render multi-stage container, EJS solver, and PO Token Provider architecture.
- **Phase 2**: `BrowserExtractor` extension point backed by headless Playwright for JS-rendered players.
- **Phase 3**: Playlist discovery, batch resolution, and multi-file selection.
- **Phase 4**: `RedisJobStore` for horizontal multi-instance queueing.
- **Phase 5**: `S3Storage` backend for persistent cloud object storage (AWS S3 / GCS).

---

## License

This repository currently does not grant permission to copy, modify, redistribute or commercially reuse the source code unless explicitly authorized by the copyright holder.
