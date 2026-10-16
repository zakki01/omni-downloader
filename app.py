"""
Omni Downloader - Precision Media Retrieval
Engineered as a lightweight, reliable FastAPI wrapper around yt-dlp CLI + FFmpeg.
"""

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, HTTPException, Request as FastApiRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging & Directories
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("omni-downloader")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Binary Discovery: yt-dlp & FFmpeg
# ---------------------------------------------------------------------------
def get_ytdlp_bin() -> str:
    """Locate yt-dlp binary across PATH and platform-specific standard directories."""
    found = shutil.which("yt-dlp")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Python\pythoncore-3.14-64\Scripts\yt-dlp.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\Scripts\yt-dlp.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python311\Scripts\yt-dlp.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\yt-dlp.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    for unix_path in ("/usr/local/bin/yt-dlp", "/usr/bin/yt-dlp"):
        if os.path.isfile(unix_path) and os.access(unix_path, os.X_OK):
            return unix_path
    return "yt-dlp"

def get_ffmpeg_path() -> Optional[str]:
    """Locate FFmpeg binary across PATH and system directories."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
            os.path.expandvars(r"%PROGRAMFILES%\ffmpeg\bin\ffmpeg.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    for unix_path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(unix_path) and os.access(unix_path, os.X_OK):
            return unix_path
    return None

def is_ffmpeg_available() -> bool:
    return get_ffmpeg_path() is not None

YTDLP_BIN = get_ytdlp_bin()
FFMPEG_PATH = get_ffmpeg_path()

if FFMPEG_PATH:
    ffmpeg_dir = str(Path(FFMPEG_PATH).parent)
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

# ---------------------------------------------------------------------------
# SSRF Protection & URL Validation
# ---------------------------------------------------------------------------
def is_ip_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Block loopback, private, link-local, reserved, multicast, and special networks."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True

    special_networks = [
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("192.0.0.0/24"),
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("240.0.0.0/4"),
        ipaddress.ip_network("255.255.255.255/32"),
    ]
    return any(ip in net for net in special_networks if isinstance(ip, ipaddress.IPv4Address))

def check_ssrf(url_str: str) -> None:
    """Validate that the URL is strictly HTTP(S) and does not point to internal/private hosts."""
    try:
        parsed = urllib.parse.urlparse(url_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

    lower_host = hostname.lower()
    if (
        lower_host in ("localhost", "localhost.localdomain", "broadcasthost")
        or lower_host.endswith(".local")
        or lower_host.endswith(".localhost")
    ):
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

    try:
        direct_ip = ipaddress.ip_address(hostname.strip("[]"))
        if is_ip_disallowed(direct_ip):
            raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")
    except ValueError:
        pass

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="The video could not be reached. Please check the URL and try again.")
    except Exception:
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

    if not addr_infos:
        raise HTTPException(status_code=400, detail="The video could not be reached. Please check the URL and try again.")

    for item in addr_infos:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
            if is_ip_disallowed(resolved_ip):
                raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")

def validate_url(url: str) -> str:
    """Validate URL syntax and enforce SSRF restrictions."""
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")
    url = url.strip()
    check_ssrf(url)
    return url

# ---------------------------------------------------------------------------
# Filename Sanitization & Rate Limiter
# ---------------------------------------------------------------------------
def sanitize_filename(name: Optional[str], fallback: str = "video", max_length: int = 150) -> str:
    """Sanitize title to produce a safe filesystem filename."""
    if not name:
        return fallback

    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if c.isprintable() and ord(c) >= 32)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.replace("..", "_")
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip(" ._")

    if len(name) > max_length:
        name = name[:max_length].rstrip(" ._")

    return name if name else fallback

class InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter per client IP."""
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.client_timestamps: Dict[str, List[float]] = {}
        self.lock = threading.Lock()

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        with self.lock:
            timestamps = self.client_timestamps.get(client_ip, [])
            timestamps = [t for t in timestamps if now - t < self.window_seconds]
            if len(timestamps) >= self.max_requests:
                self.client_timestamps[client_ip] = timestamps
                return False
            timestamps.append(now)
            self.client_timestamps[client_ip] = timestamps
            return True

rate_limiter = InMemoryRateLimiter()

def check_rate_limit(request: FastApiRequest) -> None:
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 10 requests per minute.",
        )

# ---------------------------------------------------------------------------
# Error Classification
# ---------------------------------------------------------------------------
class ErrorCategory:
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    BOT_CHALLENGE = "BOT_CHALLENGE"
    PRIVATE_CONTENT = "PRIVATE_CONTENT"
    DRM_PROTECTED = "DRM_PROTECTED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"

    # Backward compatibility aliases
    YOUTUBE_BOT_CHALLENGE = "BOT_CHALLENGE"
    YOUTUBE_PO_TOKEN_REQUIRED = "BOT_CHALLENGE"
    YOUTUBE_AUTH_REQUIRED = "PRIVATE_CONTENT"
    AUTH_REQUIRED = "PRIVATE_CONTENT"
    PO_TOKEN_REQUIRED = "BOT_CHALLENGE"
    UNSUPPORTED_URL = "UNSUPPORTED_SOURCE"
    NO_FORMATS = "DOWNLOAD_FAILED"
    GEO_RESTRICTED = "DOWNLOAD_FAILED"
    UNAVAILABLE = "DOWNLOAD_FAILED"
    NETWORK_ERROR = "DOWNLOAD_FAILED"
    FFMPEG_ERROR = "DOWNLOAD_FAILED"

def is_youtube_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        h = (parsed.hostname or "").lower()
        return any(d in h for d in ["youtube.com", "youtu.be", "youtube-nocookie.com"])
    except Exception:
        return False

def categorize_error_text(err_text: str, is_youtube: bool = False) -> Tuple[str, str]:
    """Map raw stderr into concise categories and user-friendly messages."""
    lower = err_text.lower()

    if any(w in lower for w in [
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "bot verification",
        "unusual traffic",
        "solve a captcha",
        "use --cookies",
    ]):
        if is_youtube or "youtube" in lower:
            return (
                ErrorCategory.BOT_CHALLENGE,
                "YouTube is currently unavailable from this server. Please try another supported source.",
            )
        return (
            ErrorCategory.BOT_CHALLENGE,
            "The source requires a bot verification challenge. Please try another supported source.",
        )

    if any(w in lower for w in ["drm", "drm-protected", "widevine", "playready", "fairplay"]):
        return ErrorCategory.DRM_PROTECTED, "This video is DRM-protected and cannot be downloaded."

    if any(w in lower for w in [
        "private video",
        "sign in if you've been granted access",
        "login required",
        "members-only",
        "account required",
        "this video is private",
    ]):
        return ErrorCategory.PRIVATE_CONTENT, "This media is private or requires authentication."

    if any(w in lower for w in ["unsupported url", "is not a valid url", "no suitable extractor"]):
        return ErrorCategory.UNSUPPORTED_SOURCE, "This URL or platform is not supported. Please check the URL."

    if any(w in lower for w in [
        "http error 403",
        "http error 404",
        "forbidden",
        "unable to download webpage",
        "getaddrinfo failed",
        "connection refused",
    ]):
        return ErrorCategory.DOWNLOAD_FAILED, "The media could not be retrieved from the host server. Please verify the URL."

    if "timed out" in lower or "timeout" in lower:
        return ErrorCategory.TIMEOUT, "The request timed out while contacting the video source."

    return ErrorCategory.UNKNOWN, "An error occurred while processing this media. Please try again."

def categorize_error(e: Exception) -> str:
    _, msg = categorize_error_text(str(e))
    return msg

def classify_error(e: Exception) -> Tuple[str, str]:
    return categorize_error_text(str(e))

def detect_bot_check_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(w in msg for w in ["not a bot", "captcha", "unusual traffic", "use --cookies"])

def detect_drm_error(e: Exception) -> bool:
    return "drm" in str(e).lower()

# ---------------------------------------------------------------------------
# Metadata Extraction & Format Normalization
# ---------------------------------------------------------------------------
# In-memory TTL metadata cache: url -> (timestamp, metadata_dict)
_metadata_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

def extract_metadata_subprocess(url: str, timeout: int = 45) -> Dict[str, Any]:
    """Execute yt-dlp CLI to dump single JSON metadata."""
    cmd = [YTDLP_BIN, "--dump-single-json", "--no-warnings", "--no-playlist"]
    if FFMPEG_PATH:
        cmd.extend(["--ffmpeg-location", FFMPEG_PATH])
    cmd.append(url)

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="The request timed out while contacting the video source.")

    if res.returncode != 0:
        logger.warning(f"yt-dlp extraction failed for {url}: {res.stderr.strip()[-300:]}")
        cat, msg = categorize_error_text(res.stderr, is_youtube=is_youtube_url(url))
        raise HTTPException(status_code=400, detail=msg)

    # Resilient JSON parsing
    raw_output = res.stdout.strip()
    try:
        return json.loads(raw_output)
    except Exception:
        # Fallback: scan lines for valid JSON
        for line in raw_output.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        logger.error(f"Failed to parse yt-dlp JSON: {raw_output[:200]}")
        raise HTTPException(status_code=500, detail="Failed to process media metadata.")

def format_bytes_human(num_bytes: Optional[int | float]) -> Optional[str]:
    """Format byte count into human-readable size string (e.g., 24.5 MB)."""
    if not num_bytes or num_bytes <= 0:
        return None
    val = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if val < 1024.0:
            return f"{val:.1f} {unit}" if unit in ("MB", "GB") else f"{int(val)} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"

def normalize_formats(info: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """Build normalized list of real, usable formats returned by yt-dlp, and indicate if audio is present."""
    formats: List[Dict[str, Any]] = []
    best_by_height: Dict[int, Dict[str, Any]] = {}
    has_any_audio = False

    raw_formats = info.get("formats", [])
    if not raw_formats:
        fid = str(info.get("format_id") or "0")
        ext = info.get("ext", "mp4")
        has_video = (info.get("video_ext", "") != "none") or (ext in ("mp4", "webm", "mkv", "avi", "mov", "ogv"))
        has_audio = (info.get("audio_ext", "") not in ("none", "")) and (info.get("acodec", "none") != "none")
        raw_formats = [{
            "format_id": fid,
            "ext": ext,
            "height": info.get("height"),
            "vcodec": info.get("vcodec", "unknown") if has_video else "none",
            "acodec": info.get("acodec", "unknown") if has_audio else "none",
            "tbr": info.get("tbr") or 0,
        }]

    for f in raw_formats:
        fid = str(f.get("format_id", ""))
        if not fid:
            continue
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        if acodec not in ("none", "", None):
            has_any_audio = True

        if vcodec != "none":
            height = f.get("height")
            tbr = f.get("tbr") or 0
            if height:
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f
            else:
                sz = f.get("filesize") or f.get("filesize_approx")
                sz_str = format_bytes_human(sz)
                sz_part = f" • {sz_str}" if sz_str else ""
                formats.append({
                    "format_id": fid,
                    "label": f"Original Video ({f.get('ext', 'mp4').upper()}){sz_part}",
                    "height": None,
                    "ext": f.get("ext", "mp4"),
                    "fps": f.get("fps"),
                    "filesize": sz,
                    "filesize_human": sz_str,
                    "has_video": True,
                    "has_audio": (acodec not in ("none", "", None)),
                })

    for h in sorted(best_by_height.keys(), reverse=True):
        f = best_by_height[h]
        ext = f.get("ext", "mp4")
        label_extra = " (Full HD)" if h >= 1080 else (" (HD)" if h >= 720 else "")
        sz = f.get("filesize") or f.get("filesize_approx")
        sz_str = format_bytes_human(sz)
        sz_part = f" • {sz_str}" if sz_str else ""
        formats.append({
            "format_id": str(f.get("format_id")),
            "label": f"{h}p{label_extra} ({ext.upper()}){sz_part}",
            "height": h,
            "ext": ext,
            "fps": f.get("fps"),
            "filesize": sz,
            "filesize_human": sz_str,
            "has_video": True,
            "has_audio": (f.get("acodec", "none") not in ("none", "", None)),
        })

    # Include MP3 audio option if media has audio
    if has_any_audio or info.get("acodec") not in ("none", "", None) or info.get("audio_channels"):
        has_any_audio = True
        formats.append({
            "format_id": "mp3",
            "label": "Audio Only (MP3)",
            "height": None,
            "ext": "mp3",
            "fps": None,
            "filesize": None,
            "filesize_human": None,
            "has_video": False,
            "has_audio": True,
        })

    return formats, has_any_audio

def get_available_heights(formats: List[Dict[str, Any]]) -> List[int]:
    """Extract distinct heights for backward compatibility."""
    heights = set()
    for f in formats:
        if f.get("vcodec", "none") != "none" and f.get("height"):
            heights.add(int(f["height"]))
    return sorted(list(heights))

# ---------------------------------------------------------------------------
# In-Memory Job System
# ---------------------------------------------------------------------------
jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

def _run_download_job(
    job_id: str,
    url: str,
    format_id: str,
    format_meta: Dict[str, Any],
    title: str,
) -> None:
    """Asynchronous background worker for executing yt-dlp download."""
    with _jobs_lock:
        job = jobs[job_id]
        job["status"] = "running"
        job["progress"] = 0

    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(job_dir / "media.%(ext)s")

    cmd = [YTDLP_BIN, "--newline", "--no-warnings", "--no-playlist"]
    if FFMPEG_PATH:
        cmd.extend(["--ffmpeg-location", FFMPEG_PATH])

    if format_id == "mp3":
        cmd.extend(["-o", out_template, "-x", "--audio-format", "mp3", url])
    else:
        # Merge if video-only format and media actually possesses audio to merge
        has_any_audio = format_meta.get("has_any_audio", False)
        if format_meta.get("has_video") and not format_meta.get("has_audio") and has_any_audio:
            cmd.extend(["-o", out_template, "-f", f"{format_id}+bestaudio/{format_id}/best", "--merge-output-format", "mp4", url])
        else:
            cmd.extend(["-o", out_template, "-f", format_id, "--merge-output-format", "mp4", url])

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        recent_lines: List[str] = []

        # Parse progress and output line-by-line
        if proc.stdout:
            for line in proc.stdout:
                line_str = line.strip()
                if line_str:
                    recent_lines.append(line_str)
                    if len(recent_lines) > 50:
                        recent_lines.pop(0)

                if any(k in line_str for k in ["[Merger]", "[ExtractAudio]", "[FixupM3u8]", "[ffmpeg]", "[VideoConvertor]"]):
                    with _jobs_lock:
                        job["status"] = "processing"
                        job["progress"] = 99.0
                        job["speed"] = None
                        job["eta"] = None
                    continue

                m = re.search(r"\[download\]\s+([0-9.]+)%", line_str)
                if m:
                    try:
                        pct = float(m.group(1))
                        m_spd = re.search(r"at\s+([~0-9.]+\s*[a-zA-Z/]+)", line_str)
                        spd_val = m_spd.group(1).strip() if m_spd else None

                        m_eta = re.search(r"ETA\s+([0-9:]+)", line_str)
                        eta_val = m_eta.group(1).strip() if m_eta else None

                        with _jobs_lock:
                            if job["status"] != "processing":
                                job["status"] = "running"
                            job["progress"] = pct
                            if spd_val:
                                job["speed"] = spd_val
                            if eta_val:
                                job["eta"] = eta_val
                    except ValueError:
                        pass

        proc.wait(timeout=300)

        if proc.returncode != 0:
            err_output = "\n".join(recent_lines)
            logger.warning(f"Download job {job_id} exited {proc.returncode}: {err_output}")
            cat, friendly_err = categorize_error_text(err_output, is_youtube=is_youtube_url(url))
            with _jobs_lock:
                job["status"] = "error"
                job["error"] = friendly_err
            return

        # Find completed media file
        candidate_files = [
            p for p in job_dir.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))
        ]

        if not candidate_files:
            with _jobs_lock:
                job["status"] = "error"
                job["error"] = "Media download finished, but no output file was generated."
            return

        output_file = max(candidate_files, key=lambda p: p.stat().st_size)
        ext = output_file.suffix.lstrip(".")
        sanitized_title = f"{sanitize_filename(title)}.{ext}"

        with _jobs_lock:
            job["status"] = "done"
            job["progress"] = 100
            job["filepath"] = str(output_file)
            job["filename"] = sanitized_title

    except subprocess.TimeoutExpired:
        proc.kill()
        with _jobs_lock:
            job["status"] = "error"
            job["error"] = "Download request timed out after 300 seconds."
    except Exception as e:
        logger.exception(f"Unexpected error in download worker for job {job_id}: {e}")
        with _jobs_lock:
            job["status"] = "error"
            job["error"] = "An unexpected error occurred during download."

# ---------------------------------------------------------------------------
# Background File & Job Cleanup
# ---------------------------------------------------------------------------
async def _cleanup_loop() -> None:
    """Hourly background loop cleaning up orphaned temp files and expired jobs."""
    while True:
        try:
            await asyncio.sleep(900)  # Run every 15 minutes
            cutoff = time.time() - 3600  # 1 hour expiry

            # 1. Clean jobs
            with _jobs_lock:
                expired_jobs = [jid for jid, j in jobs.items() if j.get("created_at", 0) < cutoff]
                for jid in expired_jobs:
                    del jobs[jid]

            # 2. Clean directories
            for item in DOWNLOAD_DIR.iterdir():
                if item.is_dir() and item.stat().st_mtime < cutoff:
                    try:
                        shutil.rmtree(item)
                    except Exception as e:
                        logger.warning(f"Failed to remove expired dir {item}: {e}")

            # 3. Clean metadata cache
            with _cache_lock:
                expired_urls = [u for u, (ts, _) in _metadata_cache.items() if ts < cutoff]
                for u in expired_urls:
                    del _metadata_cache[u]

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in cleanup loop: {e}")

# ---------------------------------------------------------------------------
# Lifespan & FastAPI App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Omni Downloader starting. yt-dlp: {YTDLP_BIN}, ffmpeg: {FFMPEG_PATH or 'None'}")
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    logger.info("Omni Downloader shutting down.")

app = FastAPI(
    title="Omni Downloader",
    description="Precision Media Retrieval Engine",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# API Models
# ---------------------------------------------------------------------------
class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    quality: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def serve_root():
    index_file = BASE_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return FileResponse(index_file, media_type="text/html")

@app.get("/terms", response_class=HTMLResponse)
async def serve_terms():
    terms_file = BASE_DIR / "terms.html"
    if not terms_file.is_file():
        raise HTTPException(status_code=404, detail="terms.html not found.")
    return FileResponse(terms_file, media_type="text/html")

@app.get("/privacy", response_class=HTMLResponse)
async def serve_privacy():
    privacy_file = BASE_DIR / "privacy.html"
    if not privacy_file.is_file():
        raise HTTPException(status_code=404, detail="privacy.html not found.")
    return FileResponse(privacy_file, media_type="text/html")

@app.post("/api/info")
async def api_info(req: InfoRequest, request: FastApiRequest):
    check_rate_limit(request)
    url = validate_url(req.url)

    # Check cache first
    cached_info = None
    with _cache_lock:
        if url in _metadata_cache:
            ts, cached_info = _metadata_cache[url]
            if time.time() - ts > 600:
                cached_info = None

    if not cached_info:
        raw_info = extract_metadata_subprocess(url)
        normalized_fmts, has_any_audio = normalize_formats(raw_info)
        cached_info = {
            "title": raw_info.get("title") or "Untitled Media",
            "thumbnail": raw_info.get("thumbnail"),
            "duration": raw_info.get("duration"),
            "webpage_url": raw_info.get("webpage_url") or url,
            "extractor": raw_info.get("extractor"),
            "formats": normalized_fmts,
            "has_any_audio": has_any_audio,
            "heights": get_available_heights(raw_info.get("formats", [])),
        }
        with _cache_lock:
            _metadata_cache[url] = (time.time(), cached_info)

    return cached_info

@app.post("/api/download")
async def api_download(req: DownloadRequest, request: FastApiRequest):
    check_rate_limit(request)
    url = validate_url(req.url)

    # Resolve format ID
    requested_fid = req.format_id or req.quality
    if not requested_fid:
        requested_fid = "best"

    # Get cached metadata or fetch
    meta = None
    with _cache_lock:
        if url in _metadata_cache:
            _, meta = _metadata_cache[url]

    if not meta:
        raw_info = extract_metadata_subprocess(url)
        norm_fmts, has_any_audio = normalize_formats(raw_info)
        meta = {
            "title": raw_info.get("title") or "Untitled Media",
            "formats": norm_fmts,
            "has_any_audio": has_any_audio,
        }
        with _cache_lock:
            _metadata_cache[url] = (time.time(), meta)

    formats = meta.get("formats", [])
    valid_format_ids = {f["format_id"]: f for f in formats}

    # Format verification
    matched_format = None
    if requested_fid in valid_format_ids:
        matched_format = dict(valid_format_ids[requested_fid])
    elif requested_fid == "audio" and "mp3" in valid_format_ids:
        matched_format = dict(valid_format_ids["mp3"])
        requested_fid = "mp3"
    elif requested_fid == "best":
        # Select highest quality video format, or first format
        video_fmts = [f for f in formats if f.get("has_video")]
        matched_format = dict(video_fmts[0] if video_fmts else (formats[0] if formats else {}))
        requested_fid = matched_format.get("format_id", "best")
    else:
        # Check if requested_fid matches a height string like "1080"
        matching_by_height = [f for f in formats if str(f.get("height")) == requested_fid]
        if matching_by_height:
            matched_format = dict(matching_by_height[0])
            requested_fid = matched_format["format_id"]

    if not matched_format and requested_fid != "best":
        raise HTTPException(
            status_code=400,
            detail="The requested format is not available for this media source.",
        )

    if not matched_format:
        matched_format = {"format_id": "best", "has_video": True, "has_audio": True}

    matched_format["has_any_audio"] = meta.get("has_any_audio", False)

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0.0,
            "speed": None,
            "eta": None,
            "filename": None,
            "filepath": None,
            "error": None,
            "created_at": time.time(),
        }

    # Start download worker in daemon thread
    worker_thread = threading.Thread(
        target=_run_download_job,
        args=(job_id, url, requested_fid, matched_format, meta.get("title", "video")),
        daemon=True,
    )
    worker_thread.start()

    return {"job_id": job_id, "status": "queued"}

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job.get("progress"),
        "speed": job.get("speed"),
        "eta": job.get("eta"),
        "filename": job.get("filename"),
        "error": job.get("error"),
    }

@app.get("/api/file/{job_id}")
async def api_file(job_id: str):
    # Validate job_id format against path traversal
    if not re.match(r"^[a-zA-Z0-9_-]+$", job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID.")

    with _jobs_lock:
        job = jobs.get(job_id)

    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Download file is not ready or has expired.")

    filepath = Path(job["filepath"])
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Generated media file was not found on server.")

    # Prevent path traversal outside DOWNLOAD_DIR
    try:
        filepath.resolve().relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")

    return FileResponse(
        path=str(filepath),
        filename=job.get("filename") or filepath.name,
        media_type="application/octet-stream",
    )
