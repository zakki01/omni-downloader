import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
import urllib.parse
import uuid

from fastapi import FastAPI, HTTPException, Request as FastApiRequest
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import yt_dlp
from yt_dlp.networking import _urllib
from yt_dlp.networking.common import Request, RequestDirector, RequestError, Response

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("omni_downloader")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Diagnostics & Runtime Detection
# ---------------------------------------------------------------------------
logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")

def get_js_runtime_info() -> Dict[str, Optional[str]]:
    """Locate supported JavaScript runtimes for yt-dlp challenge solving."""
    return {
        "node": shutil.which("node"),
        "deno": shutil.which("deno"),
    }

JS_RUNTIMES = get_js_runtime_info()
available_js = [name for name, path in JS_RUNTIMES.items() if path]
if available_js:
    logger.info(f"[JS RUNTIME] Detected JavaScript runtimes: {', '.join(available_js)}")
else:
    logger.warning("[JS RUNTIME] No supported JavaScript runtime found on PATH (node or deno). YouTube extraction may fail or be restricted.")

def get_cookies_file_path() -> Optional[Path]:
    """Resolve cookies file from environment variable or standard paths without leaking secrets."""
    env_path = os.environ.get("COOKIES_FILE_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
    render_secret = Path("/etc/secrets/cookies.txt")
    if render_secret.is_file():
        return render_secret
    local_file = BASE_DIR / "cookies.txt"
    if local_file.is_file():
        return local_file
    cookies_text = os.environ.get("YOUTUBE_COOKIES_TEXT", "").strip()
    if cookies_text:
        cached_cookie = BASE_DIR / ".cookies_env.txt"
        if not cached_cookie.is_file() or cached_cookie.stat().st_size == 0:
            try:
                cached_cookie.write_text(cookies_text, encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not cache YOUTUBE_COOKIES_TEXT: {e}")
        if cached_cookie.is_file():
            return cached_cookie
    return None

if get_cookies_file_path():
    logger.info("[AUTH] Cookies configuration detected and enabled")
else:
    logger.info("[AUTH] No cookies provided (running in anonymous mode)")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_WORKERS = 2
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
CLEANUP_AGE_SECONDS = 3600  # 1 hour
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# FFmpeg Detection and Environment Configuration
# ---------------------------------------------------------------------------
def get_ffmpeg_path() -> Optional[str]:
    """Locate the FFmpeg executable across PATH and standard OS installation paths."""
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
    """Check if FFmpeg is available on the host system."""
    return get_ffmpeg_path() is not None

# Ensure FFmpeg directory is in os.environ['PATH']
FFMPEG_PATH = get_ffmpeg_path()
if FFMPEG_PATH:
    FFMPEG_DIR = str(Path(FFMPEG_PATH).parent)
    if FFMPEG_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
    logger.info(f"[FFMPEG] available=true path={FFMPEG_PATH}")
else:
    logger.warning("[FFMPEG] available=false (Merging and audio conversion will be unavailable)")

# ---------------------------------------------------------------------------
# Thread-Safe In-Memory State
# ---------------------------------------------------------------------------
jobs_lock = threading.Lock()
jobs: Dict[str, Dict[str, Any]] = {}
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# In-Memory Rate Limiter
# ---------------------------------------------------------------------------
class InMemoryRateLimiter:
    """Sliding-window in-memory rate limiter per client IP."""
    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
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

rate_limiter = InMemoryRateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)

# ---------------------------------------------------------------------------
# Filename Sanitization
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
    name = name.strip(" .-_")

    if len(name) > max_length:
        name = name[:max_length].rstrip(" .-_")

    return name if name else fallback

# ---------------------------------------------------------------------------
# URL Validation & SSRF Guard
# ---------------------------------------------------------------------------
def is_ip_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address belongs to private, loopback, link-local, or reserved ranges."""
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
# Install Network-Level SSRF Guards for yt-dlp & Redirects
# ---------------------------------------------------------------------------
def install_ssrf_guards() -> None:
    """Intercept yt-dlp network requests and redirects to ensure no SSRF occurs downstream."""
    orig_send = RequestDirector.send

    def guarded_send(self: Any, request: Request) -> Response:
        url_str = str(request.url)
        try:
            check_ssrf(url_str)
        except Exception as e:
            logger.warning(f"Blocked outgoing yt-dlp request to {url_str}: {e}")
            raise RequestError(f"SSRF blocked: Disallowed destination URL ({url_str})") from e
        return orig_send(self, request)

    RequestDirector.send = guarded_send

    orig_redirect = _urllib.RedirectHandler.redirect_request

    def guarded_redirect(self: Any, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            check_ssrf(str(newurl))
        except Exception as e:
            logger.warning(f"Blocked yt-dlp redirect to {newurl}: {e}")
            raise urllib.error.HTTPError(newurl, 403, "SSRF Blocked redirect", headers, fp)
        return orig_redirect(self, req, fp, code, msg, headers, newurl)

    _urllib.RedirectHandler.redirect_request = guarded_redirect

install_ssrf_guards()

# ---------------------------------------------------------------------------
# Error Classification & Verification Detection
# ---------------------------------------------------------------------------
def detect_bot_check_error(e: Exception) -> bool:
    """Detect if an exception from yt-dlp indicates YouTube bot verification or sign-in requirement."""
    msg = str(e).lower()
    bot_indicators = [
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "use --cookies-from-browser",
        "use --cookies",
        "bot verification",
        "bot detection",
        "automated queries",
        "unusual traffic",
        "solve a captcha",
        "robot",
        "sign in to view this video",
        "sign in to confirm your age",
        "confirm your age",
        "private video. sign in",
        "this video is private",
    ]
    return any(indicator in msg for indicator in bot_indicators)

def detect_drm_error(e: Exception) -> bool:
    """Detect if an exception from yt-dlp indicates DRM-protected content."""
    msg = str(e).lower()
    drm_keywords = [
        "drm",
        "widevine",
        "playready",
        "fairplay",
        "drm-protected",
        "drm protected",
        "protected content",
        "encrypted media",
        "encryption key",
        "license request",
        "content is protected",
        "this video is drm-protected",
    ]
    return any(keyword in msg for keyword in drm_keywords)

def categorize_error(e: Exception) -> str:
    """Map raw exceptions to clean, user-facing error messages."""
    # Priority 1: Bot challenge or sign-in required (do not confuse with DRM)
    if detect_bot_check_error(e):
        return (
            "YouTube is currently requiring additional verification for this request. "
            "This server cannot bypass that protection. "
            "Try another supported URL or configure authorized YouTube access for the server."
        )

    # Priority 2: DRM protection
    if detect_drm_error(e):
        return "This video is DRM-protected and cannot be downloaded."

    # Priority 3: Other environmental or network issues
    msg = str(e).lower()
    if "ffmpeg is not installed" in msg or "ffmpeg not found" in msg or "ffmpeg is required" in msg:
        return "FFmpeg is required for this download. Please install FFmpeg and try again."
    if "unsupported url" in msg or "no suitable extractor" in msg:
        return "This website is not supported by the downloader."
    if (
        "http error 404" in msg
        or "not found" in msg
        or "unable to download webpage" in msg
        or "name or service not known" in msg
        or "failed to resolve" in msg
        or "connection refused" in msg
        or "timed out" in msg
        or "network" in msg
    ):
        return "The video could not be reached. Please check the URL and try again."
    if "ssrf" in msg or "disallowed" in msg or "forbidden" in msg:
        return "Please enter a valid HTTP or HTTPS URL."

    return "The download could not be completed. Please try again."

# ---------------------------------------------------------------------------
# Format Selector & Height Extraction
# ---------------------------------------------------------------------------
def get_available_heights(formats: List[Dict[str, Any]], top_level_height: Optional[int] = None) -> List[int]:
    """Extract and sort available numeric video heights."""
    raw_heights: Set[int] = set()
    for fmt in formats:
        if fmt.get("vcodec") == "none":
            continue
        h = fmt.get("height")
        if isinstance(h, (int, float)) and h > 0:
            raw_heights.add(int(h))

    if not raw_heights and top_level_height and top_level_height > 0:
        raw_heights.add(int(top_level_height))

    return sorted(list(raw_heights))

def build_format_selector(quality: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Generate yt-dlp format selector and postprocessors according to specification."""
    postprocessors: List[Dict[str, Any]] = []

    if quality == "best":
        format_str = "bv*+ba/b"
    elif quality == "audio":
        format_str = "ba/b"
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
    else:
        try:
            h = int(quality)
            format_str = f"bv*[height<={h}]+ba/b[height<={h}]"
        except ValueError:
            format_str = "bv*+ba/b"

    return format_str, postprocessors

# ---------------------------------------------------------------------------
# YouTube Extractor Architecture & Options Builder
# ---------------------------------------------------------------------------
def is_youtube_url(url: str) -> bool:
    """Identify if the given URL belongs to YouTube domains."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        return any(d in host for d in ("youtube.com", "youtu.be", "youtube-nocookie.com"))
    except Exception:
        return False

def get_youtube_extractor_args() -> Dict[str, Any]:
    """Build extractor arguments specifically for YouTube (client selection and PO token)."""
    yt_args: Dict[str, List[str]] = {}

    clients_env = os.environ.get("YOUTUBE_PLAYER_CLIENTS", "").strip()
    if clients_env:
        yt_args["player_client"] = [c.strip() for c in clients_env.split(",") if c.strip()]

    po_token_env = os.environ.get("YOUTUBE_PO_TOKEN", "").strip()
    if po_token_env:
        if "+" in po_token_env:
            yt_args["po_token"] = [po_token_env]
        else:
            yt_args["po_token"] = [f"web.gvs+{po_token_env}", f"web+{po_token_env}"]

    return {"youtube": yt_args} if yt_args else {}

def build_ydl_base_opts(url: str) -> Dict[str, Any]:
    """Construct base YoutubeDL configuration with JS runtimes and extractor-specific tuning."""
    opts: Dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "js_runtimes": {"node": {}, "deno": {}},
        "remote_components": ["ejs:github"],
    }

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = str(Path(FFMPEG_PATH).parent)

    cookies_path = get_cookies_file_path()
    if cookies_path:
        opts["cookiefile"] = str(cookies_path)

    if is_youtube_url(url):
        yt_args = get_youtube_extractor_args()
        if yt_args:
            opts["extractor_args"] = yt_args

    return opts

def _process_extracted_info(info: Dict[str, Any], url: str) -> Dict[str, Any]:
    """Process raw info dict into standardized API output format."""
    formats = info.get("formats", [])
    top_height = info.get("height")
    int_top_height = int(top_height) if isinstance(top_height, (int, float)) else None
    sorted_heights = get_available_heights(formats, int_top_height)

    duration = info.get("duration")
    int_duration = int(duration) if duration is not None and isinstance(duration, (int, float)) else None

    logger.info(
        f"[YT-DLP INFO]\n"
        f"extractor={info.get('extractor')}\n"
        f"title={info.get('title')}\n"
        f"duration={int_duration}s\n"
        f"available_heights={sorted_heights}"
    )

    return {
        "title": info.get("title") or "Unknown Video",
        "thumbnail": info.get("thumbnail"),
        "duration": int_duration,
        "heights": sorted_heights,
        "resolved_url": info.get("webpage_url") or url,
    }

def extract_youtube_info(url: str) -> Dict[str, Any]:
    """Dedicated extraction pipeline for YouTube URLs."""
    ydl_opts = build_ydl_base_opts(url)
    ydl_opts["download"] = False

    logger.info(f"[EXTRACTION] Dedicated YouTube extractor executing for: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Failed to extract YouTube video information.")
        return _process_extracted_info(info, url)

def extract_generic_info(url: str) -> Dict[str, Any]:
    """Extraction pipeline for non-YouTube URLs."""
    ydl_opts = build_ydl_base_opts(url)
    ydl_opts["download"] = False

    logger.info(f"[EXTRACTION] Generic media extractor executing for: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Failed to extract media information.")
        return _process_extracted_info(info, url)

def extract_video_info(url: str) -> Dict[str, Any]:
    """Extract metadata using either dedicated YouTube or generic pipeline."""
    try:
        if is_youtube_url(url):
            return extract_youtube_info(url)
        return extract_generic_info(url)
    except Exception as e:
        logger.error(f"Metadata extraction error for {url}: {e}", exc_info=True)
        clean_message = categorize_error(e)
        raise HTTPException(status_code=400, detail=clean_message)

# ---------------------------------------------------------------------------
# Output File Inspection
# ---------------------------------------------------------------------------
def find_output_file(job_id: str) -> Optional[Path]:
    """Locate the actual media file generated for job_id, ignoring in-flight temporary files."""
    candidates = []
    for f in DOWNLOAD_DIR.iterdir():
        if not f.is_file():
            continue
        # Check files matching job_id.<ext> or job_id_temp.<ext> or job_id.*
        if f.stem == job_id or f.name.startswith(f"{job_id}."):
            # Exclude in-flight temporary partial chunks
            if f.suffix in (".part", ".ytdl", ".temp"):
                continue
            try:
                if f.stat().st_size > 0:
                    candidates.append(f)
            except OSError:
                continue

    if not candidates:
        return None

    # Pick the most recently modified matching media file
    return max(candidates, key=lambda f: f.stat().st_mtime)

# ---------------------------------------------------------------------------
# Progress Hook
# ---------------------------------------------------------------------------
def progress_hook(d: Dict[str, Any], job_id: str) -> None:
    """Track transfer percentage without prematurely declaring job completion."""
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        if total and total > 0:
            pct = min(100, max(0, int((downloaded / total) * 100)))
            with jobs_lock:
                if job_id in jobs and jobs[job_id]["status"] == "running":
                    # Keep at 99 while transfer/post-processing is active
                    jobs[job_id]["progress"] = min(pct, 99)
    elif status == "finished":
        # Raw download chunks finished; post-processing / ffmpeg merge starting
        with jobs_lock:
            if job_id in jobs and jobs[job_id]["status"] == "running":
                jobs[job_id]["progress"] = 99
    elif status == "error":
        logger.error(f"[YT-DLP ERROR] Hook reported error for job {job_id}: {d}")

# ---------------------------------------------------------------------------
# Download Execution Routine
# ---------------------------------------------------------------------------
def download_media(job_id: str, url: str, quality: str) -> None:
    """Execute background download pipeline using yt-dlp and FFmpeg."""
    format_str, postprocessors = build_format_selector(quality)
    out_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    logger.info(
        f"\n[DOWNLOAD START]\n"
        f"job_id={job_id}\n"
        f"url={url}\n"
        f"quality={quality}\n"
        f"selector={format_str}\n"
        f"output_template={out_template}\n"
        f"ffmpeg_available={is_ffmpeg_available()}"
    )

    try:
        ydl_opts = build_ydl_base_opts(url)
        ydl_opts.update({
            "format": format_str,
            "outtmpl": out_template,
            "progress_hooks": [lambda d: progress_hook(d, job_id)],
        })

        # Explicitly configure FFmpeg check
        if not FFMPEG_PATH and (quality == "audio" or "+" in format_str):
            raise RuntimeError("FFmpeg is required for this download. Please install FFmpeg and try again.")

        if quality != "audio":
            ydl_opts["merge_output_format"] = "mp4"

        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            raw_title = (info.get("title") if isinstance(info, dict) else None) or "video"

        # Inspect downloads directory and locate generated media file
        final_file = find_output_file(job_id)
        if not final_file:
            raise RuntimeError("Generated download file could not be located on disk.")

        # Ensure file has non-zero size
        file_size = final_file.stat().st_size
        if file_size == 0:
            raise RuntimeError("Generated download file is empty.")

        ext = final_file.suffix.lstrip(".")
        expected_path = DOWNLOAD_DIR / f"{job_id}.{ext}"

        # Standardize filename to job_id.<ext> if needed
        if final_file != expected_path:
            final_file.rename(expected_path)
            final_file = expected_path

        # If audio was requested, confirm .mp3 file was produced
        if quality == "audio" and ext.lower() != "mp3":
            # Check if an mp3 version exists
            mp3_candidate = DOWNLOAD_DIR / f"{job_id}.mp3"
            if mp3_candidate.is_file() and mp3_candidate.stat().st_size > 0:
                final_file = mp3_candidate
                ext = "mp3"

        safe_title = sanitize_filename(raw_title)
        download_filename = f"{safe_title}.{ext}"

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["file_path"] = str(final_file)
                jobs[job_id]["filename"] = download_filename
                jobs[job_id]["error"] = None

        logger.info(
            f"\n[DOWNLOAD COMPLETED]\n"
            f"job_id={job_id}\n"
            f"file_path={final_file}\n"
            f"file_size={file_size} bytes\n"
            f"download_filename={download_filename}"
        )

    except Exception as e:
        logger.error(f"[DOWNLOAD FAILED] job_id={job_id}: {e}", exc_info=True)
        clean_msg = categorize_error(e)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = clean_msg

        # Clean partial artifacts for this job_id
        for partial in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass

def create_download_job(url: str, quality: str) -> str:
    """Create a new download job record and dispatch to background thread pool."""
    if not is_ffmpeg_available() and (quality == "audio" or quality != "best"):
        raise HTTPException(
            status_code=400,
            detail="FFmpeg is required for this download. Please install FFmpeg and try again.",
        )

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "progress": 0,
            "error": None,
            "file_path": None,
            "filename": None,
            "created_at": time.time(),
        }

    executor.submit(download_media, job_id, url, quality)
    return job_id

# ---------------------------------------------------------------------------
# Periodic File Cleanup Task
# ---------------------------------------------------------------------------
async def cleanup_old_files() -> None:
    """Remove completed files older than 1 hour, skipping active transfers."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            now = time.time()
            with jobs_lock:
                active_files = {
                    Path(j["file_path"]).name
                    for j in jobs.values()
                    if j.get("file_path") and j.get("status") == "running"
                }

            for file_path in DOWNLOAD_DIR.iterdir():
                if not file_path.is_file():
                    continue
                if file_path.name in active_files:
                    continue
                try:
                    file_age = now - file_path.stat().st_mtime
                    if file_age > CLEANUP_AGE_SECONDS:
                        file_path.unlink(missing_ok=True)
                        logger.info(f"Cleaned up aged file: {file_path.name}")
                except Exception as file_err:
                    logger.debug(f"Could not delete {file_path.name}: {file_err}")

        except asyncio.CancelledError:
            break
        except Exception as loop_err:
            logger.error(f"Error in cleanup task: {loop_err}")

# ---------------------------------------------------------------------------
# FastAPI Lifespan and App Setup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(cleanup_old_files())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    executor.shutdown(wait=False)

app = FastAPI(title="Omni Downloader", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------
class InfoRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)

class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    quality: str = Field(default="best", max_length=32)

# ---------------------------------------------------------------------------
# Rate Limiting Dependency
# ---------------------------------------------------------------------------
def check_rate_limit(request: FastApiRequest) -> None:
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 5 requests per minute.",
        )

@app.get("/health")
async def health_check():
    """Health check endpoint for Render and container orchestrators."""
    return {"status": "ok"}

@app.get("/terms")
async def get_terms():
    """Serve Terms & Conditions page."""
    terms_file = BASE_DIR / "terms.html"
    if not terms_file.is_file():
        raise HTTPException(status_code=404, detail="terms.html not found")
    return FileResponse(terms_file, media_type="text/html")

@app.get("/privacy")
async def get_privacy():
    """Serve Privacy Policy page."""
    privacy_file = BASE_DIR / "privacy.html"
    if not privacy_file.is_file():
        raise HTTPException(status_code=404, detail="privacy.html not found")
    return FileResponse(privacy_file, media_type="text/html")

@app.get("/")
async def get_index():
    """Serve the single-page vanilla web interface."""
    index_file = BASE_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_file, media_type="text/html")

@app.post("/api/info")
async def api_info(req: InfoRequest, request: FastApiRequest):
    """Extract metadata for the given video URL."""
    check_rate_limit(request)
    valid_url = validate_url(req.url)
    info = extract_video_info(valid_url)
    return info

@app.post("/api/download")
async def api_download(req: DownloadRequest, request: FastApiRequest):
    """Submit background download job and return job_id."""
    check_rate_limit(request)
    valid_url = validate_url(req.url)
    job_id = create_download_job(valid_url, req.quality)
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    """Return status and progress of a background job."""
    if not job_id or not job_id.isalnum():
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    return {
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"],
    }

@app.get("/api/file/{job_id}")
async def api_file(job_id: str):
    """Serve completed file as an attachment download with a safe filename."""
    if not job_id or not job_id.isalnum():
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Downloaded file is no longer available.")

    if job["status"] != "done" or not job.get("file_path"):
        raise HTTPException(status_code=400, detail="File is not ready for download.")

    file_path = Path(job["file_path"]).resolve()

    try:
        file_path.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        logger.error(f"Unauthorized path escape attempt for job {job_id}: {file_path}")
        raise HTTPException(status_code=403, detail="Unauthorized file path.")

    if not file_path.is_file() or file_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="Downloaded file is no longer available.")

    download_name = job.get("filename") or file_path.name
    return FileResponse(
        path=file_path,
        filename=download_name,
        media_type="application/octet-stream",
    )

if __name__ == "__main__":
    import uvicorn
    port_env = int(os.environ.get("PORT", 8000))
    logger.info(f"[INFO] Server starting via direct run on host 0.0.0.0 port {port_env}")
    uvicorn.run("app:app", host="0.0.0.0", port=port_env, reload=False)
