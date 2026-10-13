import asyncio
import sys
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import importlib.metadata
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

MAX_WORKERS = 4
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

def get_deno_path() -> Optional[str]:
    """Locate Deno executable across PATH and standard installation paths."""
    found = shutil.which("deno")
    if found:
        return found

    candidates = [
        Path.home() / ".deno" / "bin" / ("deno.exe" if os.name == "nt" else "deno"),
        Path("/usr/local/bin/deno"),
        Path("/usr/bin/deno"),
    ]
    if os.name == "nt":
        candidates.extend([
            Path(os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe")),
            Path(r"C:\Program Files\deno\deno.exe"),
        ])
    for c in candidates:
        if c.is_file():
            return str(c)
    return None

def get_node_path() -> Optional[str]:
    """Locate Node.js executable across PATH and standard installation paths."""
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
    ]
    if os.name == "nt":
        candidates.extend([
            Path(r"C:\Program Files\nodejs\node.exe"),
            Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\node\node.exe")),
        ])
    for c in candidates:
        if c.is_file():
            return str(c)
    return None

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

# Auto-bootstrap PATH with discovered runtimes
FFMPEG_PATH = get_ffmpeg_path()
if FFMPEG_PATH:
    FFMPEG_DIR = str(Path(FFMPEG_PATH).parent)
    if FFMPEG_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

DENO_PATH = get_deno_path()
if DENO_PATH:
    DENO_DIR = str(Path(DENO_PATH).parent)
    if DENO_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")

NODE_PATH = get_node_path()
if NODE_PATH:
    NODE_DIR = str(Path(NODE_PATH).parent)
    if NODE_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = NODE_DIR + os.pathsep + os.environ.get("PATH", "")

def get_js_runtime_info() -> Dict[str, Optional[str]]:
    """Locate supported JavaScript runtimes on PATH or discovered paths."""
    return {
        "deno": get_deno_path(),
        "node": get_node_path(),
    }

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

# Dependency constants
YTDLP_VERSION = yt_dlp.version.__version__
EJS_VERSION = get_installed_pkg_version("yt-dlp-ejs")
BGUTIL_VERSION = get_installed_pkg_version("bgutil-ytdlp-pot-provider")
CURL_CFFI_VERSION = get_installed_pkg_version("curl-cffi")
JS_RUNTIMES = get_js_runtime_info()
AVAILABLE_JS = [name for name, p in JS_RUNTIMES.items() if p]

# ---------------------------------------------------------------------------
# Local PO Token Provider Manager (Section 1, 2, 5)
# ---------------------------------------------------------------------------
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

class POTProviderDiagnostics:
    def __init__(self):
        self.cmd: List[str] = []
        self.pid: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.listening: bool = False
        self.ping_status: bool = False
        self.ping_version: Optional[str] = None
        self.ping_uptime: Optional[float] = None
        self.startup_seconds: float = 0.0
        self.error_reason: Optional[str] = None
        self.log_file: Path = LOGS_DIR / "pot_provider.log"
        self.stderr_tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cmd": self.cmd,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "listening": self.listening,
            "ping": self.ping_status,
            "version": self.ping_version,
            "uptime": self.ping_uptime,
            "startup_seconds": round(self.startup_seconds, 2),
            "error_reason": self.error_reason,
            "log_file": str(self.log_file),
        }

class POTProviderManager:
    """Manages the internal PO Token Provider server lifecycle (bound strictly to localhost 127.0.0.1:4416)."""
    def __init__(self, host: str = "127.0.0.1", port: int = 4416):
        self.host = host
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self.url = f"http://{host}:{port}"
        self.diagnostics = POTProviderDiagnostics()
        self.log_file_path = self.diagnostics.log_file

    def check_port_listening(self) -> bool:
        """Verify whether port 4416 is actively listening on localhost."""
        try:
            with socket.create_connection((self.host, self.port), timeout=0.3):
                return True
        except (OSError, socket.timeout):
            return False

    def ping(self) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Query /ping and require an affirmative HTTP 200 JSON payload with version."""
        try:
            req = urllib.request.Request(
                f"{self.url}/ping",
                headers={"User-Agent": "OmniDownloader-HealthCheck"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    return True, payload, None
                return False, None, f"HTTP {resp.status}"
        except Exception as e:
            return False, None, str(e)

    def is_running(self) -> bool:
        """Check both socket connection and valid /ping HTTP response."""
        if not self.check_port_listening():
            self.diagnostics.listening = False
            return False
        self.diagnostics.listening = True
        ok, payload, err = self.ping()
        if ok and payload:
            self.diagnostics.ping_status = True
            self.diagnostics.ping_version = payload.get("version")
            self.diagnostics.ping_uptime = payload.get("server_uptime")
            self.diagnostics.error_reason = None
            return True
        self.diagnostics.ping_status = False
        self.diagnostics.error_reason = err
        return False

    def start(self) -> bool:
        """Start local background provider process with readiness retry and detailed diagnostics."""
        t0 = time.time()

        # 1. Check if provider is already running (e.g. started by start.sh container entrypoint or background service)
        if self.is_running():
            self.diagnostics.startup_seconds = time.time() - t0
            self.diagnostics.error_reason = None
            logger.info(
                f"[POT PROVIDER] Provider already running and verified healthy at {self.url} "
                f"(version: {self.diagnostics.ping_version})"
            )
            return True

        # 2. Locate provider distribution
        provider_paths = [
            Path("/opt/pot-provider"),
            BASE_DIR / "pot-provider",
        ]
        provider_dir = None
        for p in provider_paths:
            if (p / "build" / "main.js").is_file() or (p / "src" / "main.ts").is_file():
                provider_dir = p
                break

        if not provider_dir:
            msg = "Local provider distribution not found at /opt/pot-provider or pot-provider"
            self.diagnostics.error_reason = msg
            logger.warning(f"[POT PROVIDER] {msg}")
            return False

        # 3. Determine best runtime command (Node.js preferred for build/main.js, Deno for src/main.ts or fallback)
        node_bin = NODE_PATH or shutil.which("node") or "/usr/local/bin/node" or "/usr/bin/node"
        deno_bin = DENO_PATH or shutil.which("deno") or "/usr/local/bin/deno" or "/usr/bin/deno"

        main_js = provider_dir / "build" / "main.js"
        main_ts = provider_dir / "src" / "main.ts"

        cmd: Optional[List[str]] = None
        if shutil.which(node_bin) and main_js.is_file():
            cmd = [node_bin, str(main_js), "-H", self.host, "-p", str(self.port)]
        elif shutil.which(deno_bin) and main_ts.is_file():
            cmd = [
                deno_bin, "run",
                "--allow-env", "--allow-net", "--allow-ffi", "--allow-read",
                str(main_ts),
                "-H", self.host,
                "-p", str(self.port),
            ]
        elif main_js.is_file() and node_bin:
            cmd = [node_bin, str(main_js), "-H", self.host, "-p", str(self.port)]
        elif main_ts.is_file() and deno_bin:
            cmd = [
                deno_bin, "run",
                "--allow-env", "--allow-net", "--allow-ffi", "--allow-read",
                str(main_ts),
                "-H", self.host,
                "-p", str(self.port),
            ]

        if not cmd:
            msg = f"No supported JS runtime (node={node_bin}, deno={deno_bin}) found to execute provider in {provider_dir}"
            self.diagnostics.error_reason = msg
            logger.warning(f"[POT PROVIDER] {msg}")
            return False

        self.diagnostics.cmd = cmd

        try:
            logger.info(f"[POT PROVIDER] Spawning local provider process on {self.host}:{self.port}...")
            logger.info(f"[POT PROVIDER] Command: {' '.join(cmd)} (cwd={provider_dir})")

            # Open dedicated log file for provider process output
            log_fp = open(self.log_file_path, "a", encoding="utf-8")
            log_fp.write(f"\n--- POT Provider Spawning at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log_fp.write(f"Command: {' '.join(cmd)}\nCWD: {provider_dir}\n")
            log_fp.flush()

            self.process = subprocess.Popen(
                cmd,
                cwd=str(provider_dir),
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
            )
            self.diagnostics.pid = self.process.pid
            logger.info(f"[POT PROVIDER] Process spawned with PID {self.process.pid}. Awaiting readiness on {self.host}:{self.port}...")

            # 4. Readiness loop: poll socket and /ping endpoint up to 20 seconds (40 x 0.5s)
            max_attempts = 40
            for attempt in range(1, max_attempts + 1):
                time.sleep(0.5)

                # Check if process terminated prematurely
                exit_code = self.process.poll()
                if exit_code is not None:
                    self.diagnostics.exit_code = exit_code
                    log_tail = self._read_log_tail()
                    self.diagnostics.stderr_tail = log_tail
                    self.diagnostics.error_reason = f"Process exited prematurely with code {exit_code}. Log output: {log_tail}"
                    logger.error(
                        f"[POT PROVIDER] Process PID {self.process.pid} terminated prematurely with exit code {exit_code}!\n"
                        f"Log tail:\n{log_tail}"
                    )
                    return False

                # Check socket listening and /ping
                if self.check_port_listening():
                    self.diagnostics.listening = True
                    ok, data, err = self.ping()
                    if ok and data:
                        elapsed = time.time() - t0
                        self.diagnostics.startup_seconds = elapsed
                        self.diagnostics.ping_status = True
                        self.diagnostics.ping_version = data.get("version")
                        self.diagnostics.ping_uptime = data.get("server_uptime")
                        self.diagnostics.error_reason = None
                        logger.info(
                            f"[POT PROVIDER] Provider is listening on port {self.port} and responding to /ping! "
                            f"(version: {data.get('version')}, took {elapsed:.2f}s)"
                        )
                        return True

            # If loop timed out
            log_tail = self._read_log_tail()
            self.diagnostics.stderr_tail = log_tail
            self.diagnostics.error_reason = f"Readiness check timed out after {max_attempts * 0.5}s on {self.url}/ping. Listening: {self.diagnostics.listening}. Log tail: {log_tail}"
            logger.error(
                f"[POT PROVIDER] Readiness check timed out after {max_attempts * 0.5}s on {self.url}/ping! "
                f"Listening: {self.diagnostics.listening}\nLog tail:\n{log_tail}"
            )
            return False

        except Exception as e:
            self.diagnostics.error_reason = str(e)
            logger.error(f"[POT PROVIDER] Exception while spawning provider: {e}", exc_info=True)
            return False

    def _read_log_tail(self, max_lines: int = 25) -> str:
        """Read recent lines from the provider process log file."""
        if not self.log_file_path.is_file():
            return "(no log file created)"
        try:
            lines = self.log_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-max_lines:])
        except Exception as e:
            return f"(error reading log: {e})"

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
# Startup Self-Check & Validation Engine (Section 11)
# ---------------------------------------------------------------------------
def run_startup_self_check() -> Dict[str, Any]:
    """Verify all downloader engine components and log structured diagnostic status."""
    is_writable = False
    try:
        test_file = DOWNLOAD_DIR / f".write_test_{uuid.uuid4().hex}"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        is_writable = True
    except Exception:
        is_writable = False

    active_pot = pot_manager.is_running()
    primary_js = "deno" if JS_RUNTIMES.get("deno") else ("node" if JS_RUNTIMES.get("node") else None)

    check_status = {
        "ytdlp": bool(YTDLP_VERSION),
        "ytdlp_version": YTDLP_VERSION,
        "ejs": EJS_VERSION is not None,
        "ejs_version": EJS_VERSION,
        "bgutil_plugin": BGUTIL_VERSION is not None,
        "bgutil_plugin_version": BGUTIL_VERSION,
        "js_runtime": primary_js,
        "js_runtimes_available": AVAILABLE_JS,
        "ffmpeg": is_ffmpeg_available(),
        "ffmpeg_path": FFMPEG_PATH,
        "pot_provider": active_pot,
        "pot_provider_url": pot_manager.url if active_pot else None,
        "pot_provider_diagnostics": pot_manager.diagnostics.to_dict(),
        "download_dir_writable": is_writable,
        "download_dir": str(DOWNLOAD_DIR),
        "curl_cffi": CURL_CFFI_VERSION is not None,
    }

    ok_sym = "[OK]"
    fail_sym = "[FAIL]"
    try:
        "\u2713".encode(sys.stdout.encoding or "ascii")
        ok_sym = "\u2713"
        fail_sym = "\u2717"
    except Exception:
        ok_sym = "[OK]"
        fail_sym = "[FAIL]"

    # Print clean formatted diagnostics
    print("=" * 60)
    print("Omni Downloader - Self-Contained yt-dlp Engine")
    print("=" * 60)
    print("Downloader engine:")
    print(f"  {ok_sym if check_status['ytdlp'] else fail_sym} yt-dlp (v{YTDLP_VERSION})")
    print(f"  {ok_sym if check_status['ejs'] else fail_sym} EJS (v{EJS_VERSION or 'not installed'})")
    print(f"  {ok_sym if primary_js else fail_sym} JS Runtime ({primary_js or 'none detected'}: {JS_RUNTIMES.get(primary_js or '')})")
    print(f"  {ok_sym if check_status['ffmpeg'] else fail_sym} FFmpeg ({FFMPEG_PATH or 'not found'})")
    pot_diag_msg = (
        f"online at {pot_manager.url} (v{pot_manager.diagnostics.ping_version or '2.0.2'})"
        if active_pot
        else f"OFFLINE ({pot_manager.diagnostics.error_reason or 'process not running'})"
    )
    print(f"  {ok_sym if active_pot else fail_sym} PO Token Provider ({pot_diag_msg})")
    print(f"  {ok_sym if is_writable else fail_sym} Download Directory ({'writable' if is_writable else 'read-only error'})")
    print("=" * 60)

    return check_status

# Initial self-check will run during lifespan startup
_INITIAL_SELF_CHECK = None

# ---------------------------------------------------------------------------
# Job Storage Abstraction (Section 7 & 13)
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
    """Thread-safe in-memory job store with rich lifecycle state tracking."""
    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self, job_id: str) -> Dict[str, Any]:
        job_data = {
            "status": "running",
            "stage": "running",
            "progress": 0,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
            "error": None,
            "file_path": None,
            "filename": None,
            "file_size": None,
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
    """Placeholder extension point for future distributed Redis-backed job tracking."""
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url

    def create_job(self, job_id: str) -> Dict[str, Any]:
        raise NotImplementedError("RedisJobStore is reserved for distributed queueing.")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError("RedisJobStore is reserved for distributed queueing.")

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError("RedisJobStore is reserved for distributed queueing.")

    def cleanup_jobs(self, max_age_seconds: float) -> int:
        raise NotImplementedError("RedisJobStore is reserved for distributed queueing.")

job_store: JobStore = InMemoryJobStore()
# Backward-compatibility aliases for tests inspecting internal state
jobs = getattr(job_store, "_jobs", {})
jobs_lock = getattr(job_store, "_lock", threading.Lock())

# ---------------------------------------------------------------------------
# File Storage Abstraction
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
    """Local filesystem storage in downloads/ directory."""
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
    """Placeholder extension point for future cloud object storage."""
    def __init__(self, bucket_name: str = "omni-downloads"):
        self.bucket_name = bucket_name

    def get_file_path(self, job_id: str) -> Optional[Path]:
        raise NotImplementedError("S3Storage is reserved for cloud object storage.")

    def save_output(self, job_id: str, raw_file: Path) -> Path:
        raise NotImplementedError("S3Storage is reserved for cloud object storage.")

    def delete_job_files(self, job_id: str) -> None:
        raise NotImplementedError("S3Storage is reserved for cloud object storage.")

    def cleanup_old_files(self, max_age_seconds: float, active_filenames: Set[str]) -> int:
        raise NotImplementedError("S3Storage is reserved for cloud object storage.")

storage_backend: StorageBackend = LocalStorage(DOWNLOAD_DIR)

# ---------------------------------------------------------------------------
# Rate Limiter & Thread Pool
# ---------------------------------------------------------------------------
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

rate_limiter = InMemoryRateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# Filename Sanitization & URL Validation (SSRF Security)
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
    """Validate URL syntax and enforce SSRF restrictions for all external user requests."""
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="Please enter a valid HTTP or HTTPS URL.")
    url = url.strip()
    check_ssrf(url)
    return url

def install_ssrf_guards() -> None:
    """Intercept yt-dlp network requests and redirects to ensure no SSRF occurs downstream.
    Explicitly allows trusted local PO Token Provider communication while guarding all outbound fetches."""
    orig_send = RequestDirector.send

    def guarded_send(self: Any, request: Request) -> Response:
        url_str = str(request.url)

        # Allow internal communication with the trusted local PO Token Provider
        configured_pot_url = os.environ.get("POT_PROVIDER_URL", pot_manager.url).rstrip("/")
        if url_str.startswith(configured_pot_url) or url_str.startswith("http://127.0.0.1:4416"):
            return orig_send(self, request)

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
# Error Classification Architecture (Section 8)
# ---------------------------------------------------------------------------
class ErrorCategory:
    AUTH_REQUIRED = "AUTH_REQUIRED"
    BOT_CHALLENGE = "BOT_CHALLENGE"
    PO_TOKEN_REQUIRED = "PO_TOKEN_REQUIRED"
    DRM_PROTECTED = "DRM_PROTECTED"
    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    NO_FORMATS = "NO_FORMATS"
    GEO_RESTRICTED = "GEO_RESTRICTED"
    UNAVAILABLE = "UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"

    # Backward compatibility aliases for existing tests
    YOUTUBE_BOT_CHALLENGE = "BOT_CHALLENGE"
    YOUTUBE_PO_TOKEN_REQUIRED = "PO_TOKEN_REQUIRED"
    YOUTUBE_AUTH_REQUIRED = "AUTH_REQUIRED"
    YOUTUBE_NO_FORMATS = "NO_FORMATS"

def detect_bot_check_error(e: Exception) -> bool:
    """Detect if an exception indicates bot verification or challenge requirement."""
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
    """Normalize raw yt-dlp exception into a clear category and user-facing message across all platforms."""
    msg = str(e).lower()

    # 1. DRM protected content
    if detect_drm_error(e):
        return ErrorCategory.DRM_PROTECTED, "This video is DRM-protected and cannot be downloaded."

    # 2. Proof-of-Origin Token missing
    if "gvs po token" in msg or "po token which was not provided" in msg or "po token is required" in msg:
        return ErrorCategory.PO_TOKEN_REQUIRED, "YouTube requires proof-of-origin verification for this request. Please configure authorized server access or try another source."

    # 3. Bot verification challenge (never claim DRM)
    if detect_bot_check_error(e):
        if "instagram" in msg:
            return ErrorCategory.AUTH_REQUIRED, "This Instagram media requires authentication or authorized cookies."
        return ErrorCategory.BOT_CHALLENGE, "YouTube requires additional verification for this server request. Please try again later or use another supported source."

    # 4. Account / login authorization required
    if any(k in msg for k in (
        "sign in to view this video", "sign in to confirm your age", "confirm your age",
        "private video", "this video is private", "only works when logged-in",
        "login required", "requires authentication", "not granting access",
        "empty media response", "please log in"
    )):
        if "instagram" in msg:
            return ErrorCategory.AUTH_REQUIRED, "This Instagram media requires authentication or authorized cookies."
        if "youtube" in msg:
            return ErrorCategory.AUTH_REQUIRED, "This YouTube video requires account authorization or sign-in."
        return ErrorCategory.AUTH_REQUIRED, "This media requires authentication."

    # 5. FFmpeg missing
    if any(k in msg for k in ("ffmpeg is not installed", "ffmpeg not found", "ffmpeg is required")):
        return ErrorCategory.FFMPEG_ERROR, "FFmpeg is required for this download. Please ensure FFmpeg is available and try again."

    # 6. Unsupported URL / Extractor
    if "unsupported url" in msg or "no suitable extractor" in msg:
        return ErrorCategory.UNSUPPORTED_URL, "Unable to extract media from this URL. This website is not supported."

    # 7. No formats extracted
    if any(k in msg for k in ("requested format is not available", "no video formats found", "no formats")):
        return ErrorCategory.NO_FORMATS, "No downloadable formats were found."

    # 8. Geo-restriction
    if "geo restriction" in msg or "not available from your location" in msg or "geoblocked" in msg:
        return ErrorCategory.GEO_RESTRICTED, "This media is not available in the server's geographic location."

    # 9. 404 / Video removed
    if "http error 404" in msg or "not found" in msg or "video has been removed" in msg:
        return ErrorCategory.UNAVAILABLE, "Unable to extract media from this URL. The media may have been removed or made private."

    # 10. Network / resolution errors
    if any(k in msg for k in ("unable to download webpage", "name or service not known", "failed to resolve", "connection refused", "timed out", "network", "getaddrinfo failed")):
        return ErrorCategory.NETWORK_ERROR, "The video could not be reached. Please check the URL and try again."

    if "ssrf" in msg or "disallowed" in msg or "forbidden" in msg:
        return ErrorCategory.UNSUPPORTED_URL, "Please enter a valid HTTP or HTTPS URL."

    return ErrorCategory.DOWNLOAD_FAILED, "Download failed. Please try again."

def categorize_error(e: Exception) -> str:
    """User-facing error message wrapper for backward compatibility."""
    _, msg = classify_error(e)
    return msg

# ---------------------------------------------------------------------------
# Format Builder & Resolution Filtering (Section 6)
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
    """Generate yt-dlp format selector and postprocessors dynamically without unnecessary transcoding."""
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
        f"[METADATA] extractor={info.get('extractor')} title={info.get('title')} duration={int_duration}s heights={sorted_heights}"
    )

    return {
        "title": info.get("title") or "Unknown Video",
        "thumbnail": info.get("thumbnail"),
        "duration": int_duration,
        "heights": sorted_heights,
        "resolved_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor") or "generic",
    }

# ---------------------------------------------------------------------------
# Media Extractor Pipeline (Sections 1, 3, 4)
# ---------------------------------------------------------------------------
def build_extractor_args() -> Dict[str, Any]:
    """Construct extractor args for YouTube, bgutil PO Token Provider, and player clients."""
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

    pot_url = os.environ.get("POT_PROVIDER_URL", "").strip() or pot_manager.url
    pot_args: Dict[str, List[str]] = {"base_url": [pot_url]}

    res: Dict[str, Any] = {}
    if yt_args:
        res["youtube"] = yt_args
    if os.environ.get("POT_PROVIDER_DISABLE", "").lower() not in ("1", "true", "yes"):
        res["youtubepot-bgutilhttp"] = pot_args
    return res

def build_unified_ydl_opts(
    url: str,
    is_download: bool = False,
    format_str: Optional[str] = None,
    postprocessors: Optional[List[Dict[str, Any]]] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct unified, self-contained yt-dlp options for all supported platforms."""
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

    ext_args = build_extractor_args()
    if ext_args:
        opts["extractor_args"] = ext_args

    if is_download:
        opts["format"] = format_str or "bv*+ba/b"
        if postprocessors:
            opts["postprocessors"] = postprocessors

    return opts

# Media Extractor Abstraction (Preserved for compatibility and architectural decoupling)
class MediaExtractor(ABC):
    @abstractmethod
    def supports(self, url: str) -> bool:
        pass

    @abstractmethod
    def build_ydl_opts(
        self,
        url: str,
        is_download: bool = False,
        format_str: Optional[str] = None,
        postprocessors: Optional[List[Dict[str, Any]]] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    def extract_info(self, url: str) -> Dict[str, Any]:
        pass

class GenericMediaExtractor(MediaExtractor):
    """Unified extractor engine delegating platform-specific mechanics directly to yt-dlp."""
    def supports(self, url: str) -> bool:
        return True

    def build_ydl_opts(
        self,
        url: str,
        is_download: bool = False,
        format_str: Optional[str] = None,
        postprocessors: Optional[List[Dict[str, Any]]] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return build_unified_ydl_opts(
            url,
            is_download=is_download,
            format_str=format_str,
            postprocessors=postprocessors,
            job_id=job_id,
        )

    def extract_info(self, url: str) -> Dict[str, Any]:
        ydl_opts = self.build_ydl_opts(url, is_download=False)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Failed to extract media information.")
            return process_extracted_metadata(info, url)

class YouTubeExtractor(GenericMediaExtractor):
    """YouTube-specific extractor specialization."""
    def supports(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            return any(d in host for d in ("youtube.com", "youtu.be", "youtube-nocookie.com"))
        except Exception:
            return False

    def _build_extractor_args(self) -> Dict[str, Any]:
        return build_extractor_args()

class YtDlpExtractor(GenericMediaExtractor):
    """Generic non-YouTube extractor specialization."""
    pass

class BrowserExtractor(MediaExtractor):
    """Extension point for future browser automation if ever necessary."""
    def supports(self, url: str) -> bool:
        return False

    def build_ydl_opts(self, url: str, is_download: bool = False, format_str: Optional[str] = None, postprocessors: Optional[List[Dict[str, Any]]] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError("BrowserExtractor is reserved for future fallback.")

    def extract_info(self, url: str) -> Dict[str, Any]:
        raise NotImplementedError("BrowserExtractor is reserved for future fallback.")

EXTRACTORS: List[MediaExtractor] = [
    YouTubeExtractor(),
    BrowserExtractor(),
    YtDlpExtractor(),
]

def get_extractor(url: str) -> MediaExtractor:
    """Resolve extractor instance for target URL."""
    for ext in EXTRACTORS:
        if ext.supports(url):
            return ext
    return GenericMediaExtractor()

# Backward-compatibility helper functions
def is_youtube_url(url: str) -> bool:
    return YouTubeExtractor().supports(url)

def get_youtube_extractor_args() -> Dict[str, Any]:
    return YouTubeExtractor()._build_extractor_args()

def build_ydl_base_opts(url: str) -> Dict[str, Any]:
    return get_extractor(url).build_ydl_opts(url, is_download=False)

def extract_video_info(url: str) -> Dict[str, Any]:
    """Extract metadata using the unified yt-dlp pipeline with clean error classification."""
    try:
        extractor = get_extractor(url)
        return extractor.extract_info(url)
    except Exception as e:
        logger.error(f"Metadata extraction error for {url}: {e}", exc_info=True)
        category, clean_message = classify_error(e)
        logger.info(f"Classified error: category={category}, message='{clean_message}'")
        raise HTTPException(status_code=400, detail=clean_message)

# ---------------------------------------------------------------------------
# Download Worker & Progress Tracking (Sections 6, 7)
# ---------------------------------------------------------------------------
def progress_hook(d: Dict[str, Any], job_id: str) -> None:
    """Track transfer percentage, speed, and ETA without blocking the download worker."""
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")

        pct = 0
        if total and total > 0:
            pct = min(99, max(0, int((downloaded / total) * 100)))

        job_store.update_job(
            job_id,
            status="running",
            stage="downloading",
            progress=pct,
            downloaded_bytes=downloaded,
            total_bytes=total,
            speed=speed,
            eta=eta,
        )
    elif status == "finished":
        job_store.update_job(
            job_id,
            status="processing",
            stage="processing",
            progress=99,
        )
    elif status == "error":
        logger.error(f"[YT-DLP ERROR] Hook reported error for job {job_id}: {d}")

def postprocessor_hook(d: Dict[str, Any], job_id: str) -> None:
    """Track FFmpeg post-processing / audio extraction / stream merging stage."""
    status = d.get("status")
    pp = d.get("postprocessor", "FFmpeg")
    if status == "started":
        job_store.update_job(
            job_id,
            status="processing",
            stage=f"processing_{pp}".lower(),
            progress=99,
        )

def download_media(job_id: str, url: str, quality: str) -> None:
    """Execute background download pipeline using unified extractor, storage, and FFmpeg."""
    format_str, postprocessors = build_format_selector(quality)
    out_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    extractor = get_extractor(url)

    job_store.update_job(job_id, status="running", stage="downloading", progress=0)

    logger.info(
        f"[DOWNLOAD START] job_id={job_id} url={url} quality={quality} selector='{format_str}'"
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
            "postprocessor_hooks": [lambda d: postprocessor_hook(d, job_id)],
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
            stage="done",
            progress=100,
            file_path=str(final_file),
            filename=download_filename,
            file_size=file_size,
            error=None,
        )

        logger.info(
            f"[DOWNLOAD COMPLETED] job_id={job_id} file_path={final_file} size={file_size}B filename='{download_filename}'"
        )

    except Exception as e:
        logger.error(f"[DOWNLOAD FAILED] job_id={job_id}: {e}", exc_info=True)
        _, clean_msg = classify_error(e)
        job_store.update_job(job_id, status="error", stage="error", error=clean_msg)
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
            with jobs_lock:
                active_files = {
                    Path(j["file_path"]).name
                    for j in jobs.values()
                    if j.get("file_path") and j.get("status") in ("queued", "running", "processing")
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
# FastAPI Lifespan & App Setup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Start local POT Provider
    pot_manager.start()

    # 2. Re-run self-check after POT startup
    run_startup_self_check()

    # 3. Start background cleanup loop
    cleanup_task = asyncio.create_task(cleanup_old_files())

    yield

    # 4. Shutdown cleanup loop
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # 5. Stop local POT Provider
    pot_manager.stop()

    # 6. Shutdown thread pool
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
            detail="Rate limit exceeded. Maximum 10 requests per minute.",
        )

# ---------------------------------------------------------------------------
# HTTP Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """Health check endpoint for Render and container orchestrators (Section 11 & 16)."""
    check_status = run_startup_self_check()
    is_healthy = check_status["ytdlp"] and check_status["download_dir_writable"]
    if not is_healthy:
        raise HTTPException(status_code=503, detail="Downloader engine degraded")
    return {"status": "ok"}

@app.get("/health/diagnostics")
async def health_diagnostics():
    """Detailed engine diagnostics endpoint."""
    check_status = run_startup_self_check()
    return {
        "status": "ok",
        "engine": check_status,
    }

def run_live_diagnostics(test_url: str = "https://www.youtube.com/shorts/HEOC4hUu6vU") -> Dict[str, Any]:
    """Execute live 6-step diagnostic suite inside the running container environment."""
    report: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "test_url": test_url,
    }

    # Step 1 & 2: Verify Provider Server & /ping
    listening = pot_manager.check_port_listening()
    ping_ok, ping_data, ping_err = pot_manager.ping()
    provider_log_tail = pot_manager._read_log_tail(max_lines=30)

    report["step1_and_2_provider_server"] = {
        "listening_on_127_0_0_1_4416": listening,
        "ping_ok": ping_ok,
        "ping_payload": ping_data,
        "ping_error": ping_err,
        "process_pid": pot_manager.diagnostics.pid,
        "exit_code": pot_manager.diagnostics.exit_code,
        "command": pot_manager.diagnostics.cmd,
        "provider_log_tail": provider_log_tail,
    }

    # Step 3: Test Direct Token Generation via /get_pot
    token_test: Dict[str, Any] = {"status": "untested"}
    if listening:
        try:
            t0 = time.time()
            req = urllib.request.Request(
                f"{pot_manager.url}/get_pot",
                data=json.dumps({"bypass_cache": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                pot_payload = json.loads(resp.read().decode("utf-8"))
                duration = time.time() - t0
                has_token = bool(pot_payload.get("poToken"))
                token_val = pot_payload.get("poToken") or ""
                token_test = {
                    "status": "success" if has_token else "missing_token",
                    "duration_seconds": round(duration, 2),
                    "has_po_token": has_token,
                    "po_token_preview": token_val[:35] + ("..." if len(token_val) > 35 else ""),
                    "content_binding_preview": (pot_payload.get("contentBinding") or "")[:35] + "...",
                    "expires_at": pot_payload.get("expiresAt"),
                }
        except Exception as e:
            token_test = {
                "status": "failed",
                "error": str(e),
            }
    else:
        token_test = {
            "status": "skipped",
            "reason": "Provider not listening",
        }
    report["step3_token_generation"] = token_test

    # Step 4: Outbound Network Check
    outbound: Dict[str, Any] = {}
    try:
        yt_req = urllib.request.Request("https://www.youtube.com", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(yt_req, timeout=10) as yt_resp:
            yt_html = yt_resp.read().decode("utf-8", errors="ignore")
            outbound["youtube_homepage_status"] = yt_resp.status
            outbound["html_bytes"] = len(yt_html)
            outbound["ytAtN_challenge_detected"] = "ytAtN" in yt_html
            outbound["bot_challenge_redirect"] = "confirm you’re not a bot" in yt_html or "captcha" in yt_html.lower()
    except Exception as e:
        outbound["youtube_homepage_error"] = str(e)
    report["step4_outbound_network"] = outbound

    # Step 5: YouTube Extractor Configuration
    extractor_args = build_extractor_args()
    report["step5_extractor_configuration"] = {
        "extractor_args": extractor_args,
        "expected_bgutil_arg": "youtubepot-bgutilhttp:base_url=" + pot_manager.url,
        "player_clients_env": os.environ.get("YOUTUBE_PLAYER_CLIENTS", "(default: visionos, web)"),
    }

    # Step 6: Real yt-dlp Video Extraction with Verbose Log Capture
    class DiagnosticLogCapture:
        def __init__(self):
            self.lines: List[str] = []

        def debug(self, msg: str):
            self.lines.append(f"[debug] {msg}")

        def info(self, msg: str):
            self.lines.append(f"[info] {msg}")

        def warning(self, msg: str):
            self.lines.append(f"[warning] {msg}")

        def error(self, msg: str):
            self.lines.append(f"[error] {msg}")

    cap = DiagnosticLogCapture()
    opts = build_unified_ydl_opts(test_url)
    opts.update({
        "logger": cap,
        "verbose": True,
        "quiet": False,
        "no_warnings": False,
    })

    ext_result: Dict[str, Any] = {}
    try:
        ydl = yt_dlp.YoutubeDL(opts)
        info = ydl.extract_info(test_url, download=False)
        ext_result["status"] = "success"
        ext_result["title"] = info.get("title")
        ext_result["extractor"] = info.get("extractor")
        ext_result["formats_count"] = len(info.get("formats", []))
        ext_result["duration"] = info.get("duration")
    except Exception as e:
        cat, msg = classify_error(e)
        ext_result["status"] = "failed"
        ext_result["error"] = str(e)
        ext_result["category"] = cat
        ext_result["clean_message"] = msg

    # Analyze log lines for PO Token activity
    bgutil_detected = any("PO Token Providers:" in l and "bgutil:http" in l for l in cap.lines)
    pot_generating = any("[pot:bgutil:http] Generating" in l for l in cap.lines)
    pot_retrieved = any("Retrieved a" in l and "PO Token" in l for l in cap.lines)

    ext_result["bgutil_provider_detected_in_ytdlp"] = bgutil_detected
    ext_result["po_token_generating_logged"] = pot_generating
    ext_result["po_token_retrieved_logged"] = pot_retrieved
    ext_result["log_capture_lines"] = cap.lines
    report["step6_actual_test"] = ext_result

    # Final Root Cause Classification
    if not listening or not ping_ok:
        report["root_cause_classification"] = "CATEGORY_A_APPLICATION_CONFIG: BGUtil provider process is not listening or ping failed on loopback."
    elif token_test.get("status") != "success":
        report["root_cause_classification"] = "CATEGORY_B_BGUTIL_FAILURE: BGUtil server is reachable on /ping, but failed to generate a PO Token via /get_pot."
    elif ext_result.get("status") != "success" and "confirm you’re not a bot" in str(ext_result.get("error", "")).lower():
        report["root_cause_classification"] = "CATEGORY_C_YOUTUBE_DATACENTER_IP_ENFORCEMENT: BGUtil server and token generation are functional, but YouTube anti-bot infrastructure has blocked this datacenter IP range."
    elif ext_result.get("status") == "success":
        report["root_cause_classification"] = "SUCCESS: Full extraction pipeline functional. BGUtil generated PO token and YouTube returned formats."
    else:
        report["root_cause_classification"] = f"OTHER: {ext_result.get('error')}"

    return report

@app.get("/api/diagnostics/live")
async def api_diagnostics_live(url: str = "https://www.youtube.com/shorts/HEOC4hUu6vU"):
    """Execute live container diagnostic suite (Steps 1 through 6) inside Render environment."""
    return run_live_diagnostics(url)

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
    """Serve the single-page web interface."""
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
    """Return status, stage, speed, and progress of a background job (Section 7)."""
    if not job_id or not job_id.isalnum():
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    return {
        "status": job["status"],
        "stage": job.get("stage", job["status"]),
        "progress": job.get("progress", 0),
        "downloaded_bytes": job.get("downloaded_bytes", 0),
        "total_bytes": job.get("total_bytes"),
        "speed": job.get("speed"),
        "eta": job.get("eta"),
        "error": job.get("error"),
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
