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

*Note: Omni Downloader does not require external database connection strings or third-party secret API keys.*

---

## Security

- **SSRF Defense**: Evaluates all URLs and downstream HTTP redirects. Strictly blocks connections targeting `localhost`, `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, `::1`, link-local, and reserved IP ranges.
- **Path Traversal & Filename Sanitization**: Strips dangerous filesystem tokens (`/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`) and directory traversal sequences (`..`). Files are safely isolated in `./downloads/`.
- **Rate Limiting**: Limits requests on `/api/info` and `/api/download` to 5 requests per minute per IP address.
- **DRM Protection**: Omni Downloader does not decrypt or circumvent Widevine, PlayReady, or FairPlay DRM. Content identified with DRM protection returns a clean, explanatory error.
- **Credential Protection**: Optional authentication files (such as `cookies.txt`) are excluded from Git and Docker images via `.gitignore` and `.dockerignore`, and are never exposed via APIs or static routes.

---

## Legal & Responsible Use

Omni Downloader is a technical tool designed solely for personal media archiving and retrieval of publicly accessible or authorized content. Users are strictly responsible for ensuring they possess the necessary rights, licenses, or authorizations to process and download submitted content.

Please review our policies:
- [Terms & Conditions](/terms)
- [Privacy Policy](/privacy)

---

## Deployment to Render

You can deploy Omni Downloader directly to Render as a Docker Web Service:

### Method 1: Render Blueprint (`render.yaml` - Recommended)

1. Push this repository to your GitHub account.
2. Go to the [Render Dashboard](https://dashboard.render.com/) and select **New +** > **Blueprint**.
3. Connect your repository.
4. Render will read `render.yaml`, configure the Docker container, and deploy the service.

### Method 2: Manual Web Service

1. On the Render Dashboard, click **New +** > **Web Service**.
2. Connect your GitHub repository.
3. Select **Docker** as the Environment.
4. Leave Root Directory and Build/Start commands default (handled by `Dockerfile`).
5. Set Health Check Path to `/health`.
6. Click **Create Web Service**.

> **Important Storage Notice:** Render's free tier uses an ephemeral filesystem. Processed files are temporarily stored for immediate retrieval and automatically deleted after 1 hour. Files do not persist across instance redeployments, restarts, or sleep cycles.

---

## Limitations

- **Platform Compatibility**: Compatibility is subject to `yt-dlp` extractors. Third-party platforms frequently change their stream formats, and not every URL is guaranteed to work.
- **DRM Content**: DRM-protected videos cannot be downloaded.
- **Ephemeral Storage**: Not designed as a permanent media host or storage archive.

---

## Roadmap

- **Phase 1 (Current)**: Core `yt-dlp` downloader with FFmpeg integration, Black + Gold UI, and Docker/Render configuration.
- **Phase 2**: Playwright headless browser fallback for JS-rendered players.
- **Phase 3**: Playlist discovery, batch resolution, and multi-file selection.
- **Phase 4**: Redis-backed persistent job queue with distributed background workers.
- **Phase 5**: Object storage integration (S3/GCS) and user quotas for horizontal production scaling.

---

## License

This repository currently does not grant permission to copy, modify, redistribute or commercially reuse the source code unless explicitly authorized by the copyright holder.
