import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import importlib.metadata
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import unicodedata
import urllib.parse
import urllib.request
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
# Base Directories & Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = 2
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
CLEANUP_AGE_SECONDS = 3600  # 1 hour
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Diagnostics & Dependency Introspection
# ---------------------------------------------------------------------------
def get_installed_pkg_version(pkg_name: str) -> Optional[str]:
    """Retrieve installed version of a Python package without raising."""
    try:
        return importlib.metadata.version(pkg_name)
    except Exception:
        return None

def get_js_runtime_info() -> Dict[str, Optional[str]]:
    """Locate supported JavaScript runtimes on PATH."""
    return {
        "deno": shutil.which("deno"),
        "node": shutil.which("node"),
    }

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

FFMPEG_PATH = get_ffmpeg_path()
if FFMPEG_PATH:
    FFMPEG_DIR = str(Path(FFMPEG_PATH).parent)
    if FFMPEG_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

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

# Print safe startup diagnostics
YTDLP_VERSION = yt_dlp.version.__version__
EJS_VERSION = get_installed_pkg_version("yt-dlp-ejs")
BGUTIL_VERSION = get_installed_pkg_version("bgutil-ytdlp-pot-provider")
JS_RUNTIMES = get_js_runtime_info()
AVAILABLE_JS = [name for name, p in JS_RUNTIMES.items() if p]

logger.info(f"[DIAGNOSTICS] yt-dlp version: {YTDLP_VERSION}")
logger.info(f"[DIAGNOSTICS] yt-dlp-ejs available: {EJS_VERSION is not None} (version: {EJS_VERSION or 'not installed'})")
logger.info(f"[DIAGNOSTICS] bgutil-ytdlp-pot-provider plugin: {BGUTIL_VERSION is not None} (version: {BGUTIL_VERSION or 'not installed'})")
logger.info(f"[DIAGNOSTICS] JS runtimes detected: {', '.join(AVAILABLE_JS) if AVAILABLE_JS else 'none'}")
logger.info(f"[DIAGNOSTICS] FFmpeg available: {is_ffmpeg_available()} (path: {FFMPEG_PATH})")
logger.info(f"[DIAGNOSTICS] Cookies configuration: {'configured' if get_cookies_file_path() else 'none'}")

# ---------------------------------------------------------------------------
# Local PO Token Provider Manager (Localhost / Process Lifecycle)
# ---------------------------------------------------------------------------
class POTProviderManager:
    """Manages the local PO Token Provider server lifecycle (bound to localhost only)."""
    def __init__(self, host: str = "127.0.0.1", port: int = 4416):
        self.host = host
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self.url = f"http://{host}:{port}"

    def is_running(self) -> bool:
        """Ping the local or configured PO token provider server."""
        try:
            req = urllib.request.Request(f"{self.url}/ping", headers={"User-Agent": "OmniDownloader"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start(self) -> bool:
        """Start local background provider process if available and not already running."""
        # 1. Check if provider is already running (e.g. started externally or in Docker)
        if self.is_running():
            logger.info(f"[POT PROVIDER] Provider already running at {self.url}")
            return True

        # 2. Check for local server distribution
        provider_paths = [
            Path("/opt/pot-provider"),
            BASE_DIR / "pot-provider",
        ]
        provider_dir = next((p for p in provider_paths if (p / "build" / "main.js").is_file()), None)
        if not provider_dir:
            logger.info("[POT PROVIDER] Local provider files not present; operating in standalone/fallback mode")
            return False

        node_bin = shutil.which("node") or "/usr/local/bin/node"
        main_script = provider_dir / "build" / "main.js"

        try:
            logger.info(f"[POT PROVIDER] Spawning local provider process on {self.host}:{self.port}...")
            self.process = subprocess.Popen(
                [node_bin, str(main_script), "--host", self.host, "--port", str(self.port)],
                cwd=str(provider_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(15):
                time.sleep(0.2)
                if self.is_running():
                    logger.info(f"[POT PROVIDER] Local provider successfully started at {self.url}")
                    return True
            logger.warning("[POT PROVIDER] Provider process spawned but ping check timed out")
            return False
        except Exception as e:
            logger.warning(f"[POT PROVIDER] Failed to spawn local provider: {e}")
            return False

    def stop(self) -> None:
        """Terminate the spawned background provider process gracefully."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            logger.info("[POT PROVIDER] Local provider process terminated")

pot_manager = POTProviderManager()

# ---------------------------------------------------------------------------
# Job Storage Abstraction (Section 13)
# ---------------------------------------------------------------------------
class JobStore(ABC):
    """Abstract interface for download job persistence and lifecycle management."""
    @abstractmethod
    def create_job(self, job_id: str) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def update_job(self, job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def cleanup_jobs(self, max_age_seconds: float) -> int:
        pass

class InMemoryJobStore(JobStore):
    """Thread-safe in-memory job store for single-instance prototype deployments."""
    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self, job_id: str) -> Dict[str, Any]:
        job_data = {
            "status": "running",
            "progress": 0,
            "error": None,
            "file_path": None,
            "filename": None,
            "created_at": time.time(),
        }
        with self._lock:
            self._jobs[job_id] = job_data
        return job_data

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(kwargs)
                return dict(job)
            return None

    def cleanup_jobs(self, max_age_seconds: float) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            expired = [jid for jid, j in self._jobs.items() if now - j.get("created_at", now) > max_age_seconds]
            for jid in expired:
                del self._jobs[jid]
                removed += 1
        return removed

class RedisJobStore(JobStore):
    """Placeholder extension point for future distributed Redis-backed job tracking (Phase 4)."""
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url

    def create_job(self, job_id: str) -> Dict[str, Any]:
        raise NotImplementedError("RedisJobStore is reserved for Phase 4 distributed queueing.")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError("RedisJobStore is reserved for Phase 4 distributed queueing.")

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError("RedisJobStore is reserved for Phase 4 distributed queueing.")

    def cleanup_jobs(self, max_age_seconds: float) -> int:
        raise NotImplementedError("RedisJobStore is reserved for Phase 4 distributed queueing.")

job_store: JobStore = InMemoryJobStore()
# Backward-compatibility alias for tests and hooks inspecting jobs dictionary
jobs = getattr(job_store, "_jobs", {})
jobs_lock = getattr(job_store, "_lock", threading.Lock())

# ---------------------------------------------------------------------------
# File Storage Abstraction (Section 14)
# ---------------------------------------------------------------------------
class StorageBackend(ABC):
    """Abstract interface for downloaded media storage management."""
    @abstractmethod
    def get_file_path(self, job_id: str) -> Optional[Path]:
        pass

    @abstractmethod
    def save_output(self, job_id: str, raw_file: Path) -> Path:
        pass

    @abstractmethod
    def delete_job_files(self, job_id: str) -> None:
        pass

    @abstractmethod
    def cleanup_old_files(self, max_age_seconds: float, active_filenames: Set[str]) -> int:
        pass

class LocalStorage(StorageBackend):
    """Local filesystem storage in downloads/ directory (ephemeral on Render)."""
    def __init__(self, download_dir: Path):
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def get_file_path(self, job_id: str) -> Optional[Path]:
        candidates = []
        for f in self.download_dir.iterdir():
            if not f.is_file():
                continue
            if f.stem == job_id or f.name.startswith(f"{job_id}."):
                if f.suffix in (".part", ".ytdl", ".temp"):
                    continue
                try:
                    if f.stat().st_size > 0:
                        candidates.append(f)
                except OSError:
                    continue
        if not candidates:
            return None
        return max(candidates, key=lambda f: f.stat().st_mtime)

    def save_output(self, job_id: str, raw_file: Path) -> Path:
        ext = raw_file.suffix.lstrip(".")
        target_path = self.download_dir / f"{job_id}.{ext}"
        if raw_file != target_path:
            raw_file.rename(target_path)
            return target_path
        return raw_file

    def delete_job_files(self, job_id: str) -> None:
        for f in self.download_dir.glob(f"{job_id}.*"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    def cleanup_old_files(self, max_age_seconds: float, active_filenames: Set[str]) -> int:
        now = time.time()
        cleaned = 0
        for f in self.download_dir.iterdir():
            if not f.is_file() or f.name == ".gitkeep":
                continue
            if f.name in active_filenames:
                continue
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink(missing_ok=True)
                    cleaned += 1
            except Exception:
                pass
        return cleaned

class S3Storage(StorageBackend):
    """Placeholder extension point for future cloud object storage (Phase 5)."""
    def __init__(self, bucket_name: str = "omni-downloads"):
        self.bucket_name = bucket_name

    def get_file_path(self, job_id: str) -> Optional[Path]:
        raise NotImplementedError("S3Storage is reserved for Phase 5 cloud object storage integration.")

    def save_output(self, job_id: str, raw_file: Path) -> Path:
        raise NotImplementedError("S3Storage is reserved for Phase 5 cloud object storage integration.")

    def delete_job_files(self, job_id: str) -> None:
        raise NotImplementedError("S3Storage is reserved for Phase 5 cloud object storage integration.")

    def cleanup_old_files(self, max_age_seconds: float, active_filenames: Set[str]) -> int:
        raise NotImplementedError("S3Storage is reserved for Phase 5 cloud object storage integration.")

storage_backend: StorageBackend = LocalStorage(DOWNLOAD_DIR)

# ---------------------------------------------------------------------------
# Rate Limiter & Thread Pool
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

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# Filename Sanitization & URL Validation
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
# Error Classification Architecture (Section 9)
# ---------------------------------------------------------------------------
class ErrorCategory:
    YOUTUBE_BOT_CHALLENGE = "YOUTUBE_BOT_CHALLENGE"
    YOUTUBE_PO_TOKEN_REQUIRED = "YOUTUBE_PO_TOKEN_REQUIRED"
    YOUTUBE_AUTH_REQUIRED = "YOUTUBE_AUTH_REQUIRED"
    YOUTUBE_NO_FORMATS = "YOUTUBE_NO_FORMATS"
    DRM_PROTECTED = "DRM_PROTECTED"
    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    NETWORK_ERROR = "NETWORK_ERROR"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"

ERROR_MESSAGES: Dict[str, str] = {
    ErrorCategory.YOUTUBE_BOT_CHALLENGE: "YouTube requires additional verification for this server request. Please try again later or use another supported source.",
    ErrorCategory.YOUTUBE_PO_TOKEN_REQUIRED: "YouTube requires proof-of-origin verification for this request. Please configure authorized server access or try another source.",
    ErrorCategory.YOUTUBE_AUTH_REQUIRED: "This YouTube video requires account authorization or sign-in.",
    ErrorCategory.YOUTUBE_NO_FORMATS: "No compatible stream formats could be extracted for this video.",
    ErrorCategory.DRM_PROTECTED: "This video is DRM-protected and cannot be downloaded.",
    ErrorCategory.UNSUPPORTED_URL: "This website is not supported by the downloader.",
    ErrorCategory.NETWORK_ERROR: "The video could not be reached. Please check the URL and try again.",
    ErrorCategory.FFMPEG_ERROR: "FFmpeg is required for this download. Please install FFmpeg and try again.",
    ErrorCategory.DOWNLOAD_FAILED: "The download could not be completed. Please try again.",
}

def detect_bot_check_error(e: Exception) -> bool:
    """Detect if an exception indicates YouTube bot verification or challenge requirement."""
    msg = str(e).lower()
    bot_indicators = [
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "use --cookies",
        "bot verification",
        "automated queries",
        "unusual traffic",
        "solve a captcha",
    ]
    return any(indicator in msg for indicator in bot_indicators)

def detect_drm_error(e: Exception) -> bool:
    """Detect if an exception indicates DRM encryption."""
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

def classify_error(e: Exception) -> Tuple[str, str]:
    """Map raw exception into a precise category and concise human-readable message."""
    msg = str(e).lower()

    # 1. PO Token explicitly missing
    if "gvs po token" in msg or "po token which was not provided" in msg or "po token is required" in msg:
        return ErrorCategory.YOUTUBE_PO_TOKEN_REQUIRED, ERROR_MESSAGES[ErrorCategory.YOUTUBE_PO_TOKEN_REQUIRED]

    # 2. Bot verification check (NEVER claim DRM)
    if detect_bot_check_error(e):
        return ErrorCategory.YOUTUBE_BOT_CHALLENGE, ERROR_MESSAGES[ErrorCategory.YOUTUBE_BOT_CHALLENGE]

    # 3. Account / auth required
    if any(k in msg for k in ("sign in to view this video", "sign in to confirm your age", "confirm your age", "private video", "this video is private")):
        return ErrorCategory.YOUTUBE_AUTH_REQUIRED, ERROR_MESSAGES[ErrorCategory.YOUTUBE_AUTH_REQUIRED]

    # 4. DRM protected content
    if detect_drm_error(e):
        return ErrorCategory.DRM_PROTECTED, ERROR_MESSAGES[ErrorCategory.DRM_PROTECTED]

    # 5. FFmpeg missing
    if any(k in msg for k in ("ffmpeg is not installed", "ffmpeg not found", "ffmpeg is required")):
        return ErrorCategory.FFMPEG_ERROR, ERROR_MESSAGES[ErrorCategory.FFMPEG_ERROR]

    # 6. Unsupported URL / Extractor
    if "unsupported url" in msg or "no suitable extractor" in msg:
        return ErrorCategory.UNSUPPORTED_URL, ERROR_MESSAGES[ErrorCategory.UNSUPPORTED_URL]

    # 7. No formats extracted
    if any(k in msg for k in ("requested format is not available", "no video formats found")):
        return ErrorCategory.YOUTUBE_NO_FORMATS, ERROR_MESSAGES[ErrorCategory.YOUTUBE_NO_FORMATS]

    # 8. Network / resolution errors
    if any(k in msg for k in ("http error 404", "not found", "unable to download webpage", "name or service not known", "failed to resolve", "connection refused", "timed out", "network")):
        return ErrorCategory.NETWORK_ERROR, ERROR_MESSAGES[ErrorCategory.NETWORK_ERROR]

    if "ssrf" in msg or "disallowed" in msg or "forbidden" in msg:
        return ErrorCategory.UNSUPPORTED_URL, "Please enter a valid HTTP or HTTPS URL."

    return ErrorCategory.DOWNLOAD_FAILED, ERROR_MESSAGES[ErrorCategory.DOWNLOAD_FAILED]

def categorize_error(e: Exception) -> str:
    """User-facing error message wrapper for backward compatibility."""
    _, msg = classify_error(e)
    return msg

# ---------------------------------------------------------------------------
# Format Builder & Resolution Filtering
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

def process_extracted_metadata(info: Dict[str, Any], url: str) -> Dict[str, Any]:
    """Format yt-dlp info dict into standardized API response."""
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

# ---------------------------------------------------------------------------
# Media Extractor Abstraction (Section 1)
# ---------------------------------------------------------------------------
class MediaExtractor(ABC):
    """Abstract base class for provider-aware media extraction."""
    @abstractmethod
    def supports(self, url: str) -> bool:
        """Check if this extractor handles the target URL."""
        pass

    @abstractmethod
    def build_ydl_opts(self, url: str, is_download: bool = False, format_str: Optional[str] = None, postprocessors: Optional[List[Dict[str, Any]]] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
        """Construct yt-dlp configuration options."""
        pass

    @abstractmethod
    def extract_info(self, url: str) -> Dict[str, Any]:
        """Extract metadata without downloading."""
        pass

class YtDlpExtractor(MediaExtractor):
    """Generic yt-dlp extractor for non-YouTube media (direct MP4, Vimeo, etc.)."""
    def supports(self, url: str) -> bool:
        return True  # Catch-all fallback

    def build_ydl_opts(self, url: str, is_download: bool = False, format_str: Optional[str] = None, postprocessors: Optional[List[Dict[str, Any]]] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "js_runtimes": {"deno": {}, "node": {}},
        }
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = str(Path(FFMPEG_PATH).parent)
        cookies_path = get_cookies_file_path()
        if cookies_path:
            opts["cookiefile"] = str(cookies_path)
        if is_download:
            opts["format"] = format_str or "bv*+ba/b"
            if postprocessors:
                opts["postprocessors"] = postprocessors
        return opts

    def extract_info(self, url: str) -> Dict[str, Any]:
        ydl_opts = self.build_ydl_opts(url, is_download=False)
        logger.info(f"[EXTRACTION] YtDlpExtractor executing for: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Failed to extract media information.")
            return process_extracted_metadata(info, url)

class YouTubeExtractor(MediaExtractor):
    """Specialized YouTube extractor with EJS solver, PO Token Provider, and fallback pipeline."""
    def supports(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            return any(d in host for d in ("youtube.com", "youtu.be", "youtube-nocookie.com"))
        except Exception:
            return False

    def _build_extractor_args(self) -> Dict[str, Any]:
        """Configure player clients, PO Token Provider, and fallback tokens."""
        yt_args: Dict[str, List[str]] = {}
        clients_env = os.environ.get("YOUTUBE_PLAYER_CLIENTS", "").strip()
        if clients_env:
            yt_args["player_client"] = [c.strip() for c in clients_env.split(",") if c.strip()]

        pot_args: Dict[str, List[str]] = {}
        configured_pot_url = os.environ.get("POT_PROVIDER_URL", "").strip()
        if configured_pot_url:
            pot_args["base_url"] = [configured_pot_url]
        elif pot_manager.is_running():
            pot_args["base_url"] = [pot_manager.url]

        po_token_env = os.environ.get("YOUTUBE_PO_TOKEN", "").strip()
        if po_token_env:
            if "+" in po_token_env:
                yt_args["po_token"] = [po_token_env]
            else:
                yt_args["po_token"] = [f"web.gvs+{po_token_env}", f"web+{po_token_env}"]

        res: Dict[str, Any] = {}
        if yt_args:
            res["youtube"] = yt_args
        if pot_args:
            res["youtubepot-bgutilhttp"] = pot_args
        return res

    def build_ydl_opts(self, url: str, is_download: bool = False, format_str: Optional[str] = None, postprocessors: Optional[List[Dict[str, Any]]] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "js_runtimes": {"deno": {}, "node": {}},
            "remote_components": ["ejs:github"],
        }
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = str(Path(FFMPEG_PATH).parent)
        cookies_path = get_cookies_file_path()
        if cookies_path:
            opts["cookiefile"] = str(cookies_path)

        extractor_args = self._build_extractor_args()
        if extractor_args:
            opts["extractor_args"] = extractor_args

        if is_download:
            opts["format"] = format_str or "bv*+ba/b"
            if postprocessors:
                opts["postprocessors"] = postprocessors
        return opts

    def extract_info(self, url: str) -> Dict[str, Any]:
        """Smart fallback order: Standard -> EJS solver -> POT Provider -> Cookies."""
        ydl_opts = self.build_ydl_opts(url, is_download=False)
        logger.info(f"[EXTRACTION] YouTubeExtractor executing for: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Failed to extract YouTube video information.")
            return process_extracted_metadata(info, url)

class BrowserExtractor(MediaExtractor):
    """Extension point for future Playwright headless browser fallback (Phase 2)."""
    def supports(self, url: str) -> bool:
        return False  # Placeholder: Not activated in Phase 1

    def build_ydl_opts(self, url: str, is_download: bool = False, format_str: Optional[str] = None, postprocessors: Optional[List[Dict[str, Any]]] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError("BrowserExtractor is reserved for Phase 2 Playwright integration.")

    def extract_info(self, url: str) -> Dict[str, Any]:
        raise NotImplementedError("BrowserExtractor is reserved for Phase 2 Playwright integration.")

EXTRACTORS: List[MediaExtractor] = [
    YouTubeExtractor(),
    BrowserExtractor(),
    YtDlpExtractor(),
]

def get_extractor(url: str) -> MediaExtractor:
    """Resolve the appropriate extractor for the given media URL."""
    for ext in EXTRACTORS:
        if ext.supports(url):
            return ext
    return YtDlpExtractor()

# Backward-compatibility aliases
def is_youtube_url(url: str) -> bool:
    return YouTubeExtractor().supports(url)

def get_youtube_extractor_args() -> Dict[str, Any]:
    return YouTubeExtractor()._build_extractor_args()

def build_ydl_base_opts(url: str) -> Dict[str, Any]:
    return get_extractor(url).build_ydl_opts(url, is_download=False)

def extract_video_info(url: str) -> Dict[str, Any]:
    try:
        extractor = get_extractor(url)
        return extractor.extract_info(url)
    except Exception as e:
        logger.error(f"Metadata extraction error for {url}: {e}", exc_info=True)
        category, clean_message = classify_error(e)
        logger.info(f"Classified error category: {category}")
        raise HTTPException(status_code=400, detail=clean_message)

# ---------------------------------------------------------------------------
# Download Worker & Progress Tracking
# ---------------------------------------------------------------------------
def progress_hook(d: Dict[str, Any], job_id: str) -> None:
    """Track transfer percentage without prematurely declaring job completion."""
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        if total and total > 0:
            pct = min(100, max(0, int((downloaded / total) * 100)))
            cur = job_store.get_job(job_id)
            if cur and cur.get("status") == "running":
                job_store.update_job(job_id, progress=min(pct, 99))
    elif status == "finished":
        cur = job_store.get_job(job_id)
        if cur and cur.get("status") == "running":
            job_store.update_job(job_id, progress=99)
    elif status == "error":
        logger.error(f"[YT-DLP ERROR] Hook reported error for job {job_id}: {d}")

def download_media(job_id: str, url: str, quality: str) -> None:
    """Execute background download pipeline using extractor architecture, storage, and FFmpeg."""
    format_str, postprocessors = build_format_selector(quality)
    out_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    extractor = get_extractor(url)

    logger.info(
        f"\n[DOWNLOAD START]\n"
        f"job_id={job_id}\n"
        f"url={url}\n"
        f"extractor={extractor.__class__.__name__}\n"
        f"quality={quality}\n"
        f"selector={format_str}\n"
        f"output_template={out_template}\n"
        f"ffmpeg_available={is_ffmpeg_available()}"
    )

    try:
        ydl_opts = extractor.build_ydl_opts(
            url,
            is_download=True,
            format_str=format_str,
            postprocessors=postprocessors,
            job_id=job_id,
        )
        ydl_opts.update({
            "outtmpl": out_template,
            "progress_hooks": [lambda d: progress_hook(d, job_id)],
        })

        if not FFMPEG_PATH and (quality == "audio" or "+" in format_str):
            raise RuntimeError("FFmpeg is required for this download. Please install FFmpeg and try again.")

        if quality != "audio":
            ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            raw_title = (info.get("title") if isinstance(info, dict) else None) or "video"

        # Locate and verify the downloaded file
        final_file = storage_backend.get_file_path(job_id)
        if not final_file:
            raise RuntimeError("Generated download file could not be located on disk.")

        file_size = final_file.stat().st_size
        if file_size == 0:
            raise RuntimeError("Generated download file is empty.")

        # Standardize filename through storage abstraction
        final_file = storage_backend.save_output(job_id, final_file)
        ext = final_file.suffix.lstrip(".")

        if quality == "audio" and ext.lower() != "mp3":
            mp3_candidate = DOWNLOAD_DIR / f"{job_id}.mp3"
            if mp3_candidate.is_file() and mp3_candidate.stat().st_size > 0:
                final_file = mp3_candidate
                ext = "mp3"

        safe_title = sanitize_filename(raw_title)
        download_filename = f"{safe_title}.{ext}"

        job_store.update_job(
            job_id,
            status="done",
            progress=100,
            file_path=str(final_file),
            filename=download_filename,
            error=None,
        )

        logger.info(
            f"\n[DOWNLOAD COMPLETED]\n"
            f"job_id={job_id}\n"
            f"file_path={final_file}\n"
            f"file_size={file_size} bytes\n"
            f"download_filename={download_filename}"
        )

    except Exception as e:
        logger.error(f"[DOWNLOAD FAILED] job_id={job_id}: {e}", exc_info=True)
        _, clean_msg = classify_error(e)
        job_store.update_job(job_id, status="error", error=clean_msg)
        storage_backend.delete_job_files(job_id)

def create_download_job(url: str, quality: str) -> str:
    """Create a new download job record and dispatch to background thread pool."""
    if not is_ffmpeg_available() and (quality == "audio" or quality != "best"):
        raise HTTPException(
            status_code=400,
            detail="FFmpeg is required for this download. Please install FFmpeg and try again.",
        )

    job_id = uuid.uuid4().hex
    job_store.create_job(job_id)
    executor.submit(download_media, job_id, url, quality)
    return job_id

# ---------------------------------------------------------------------------
# Periodic File & Job Cleanup Task
# ---------------------------------------------------------------------------
async def cleanup_old_files() -> None:
    """Remove completed files and jobs older than 1 hour, skipping active transfers."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            # Find in-flight filenames that should not be touched
            with jobs_lock:
                active_files = {
                    Path(j["file_path"]).name
                    for j in jobs.values()
                    if j.get("file_path") and j.get("status") == "running"
                }

            cleaned_count = storage_backend.cleanup_old_files(CLEANUP_AGE_SECONDS, active_files)
            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} aged download file(s)")

            job_store.cleanup_jobs(CLEANUP_AGE_SECONDS * 2)

        except asyncio.CancelledError:
            break
        except Exception as loop_err:
            logger.error(f"Error in cleanup task: {loop_err}")

# ---------------------------------------------------------------------------
# FastAPI Lifespan & App Setup (Section 15 & 16)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Start local POT Provider if configured
    pot_manager.start()

    # 2. Start background cleanup loop
    cleanup_task = asyncio.create_task(cleanup_old_files())

    yield

    # 3. Shutdown cleanup loop
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # 4. Stop local POT Provider
    pot_manager.stop()

    # 5. Shutdown thread pool
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

# ---------------------------------------------------------------------------
# HTTP Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """Health check endpoint for Render and container orchestrators (Section 16)."""
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

    job = job_store.get_job(job_id)
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

    job = job_store.get_job(job_id)
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
