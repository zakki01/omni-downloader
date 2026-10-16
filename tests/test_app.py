import os
import sys
import time
from pathlib import Path
from starlette.testclient import TestClient

# Ensure root dir is in path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app import (
    app,
    rate_limiter,
    sanitize_filename,
    is_ip_disallowed,
    check_ssrf,
    validate_url,
    normalize_formats,
    categorize_error_text,
    ErrorCategory,
    jobs,
    DOWNLOAD_DIR,
)

client = TestClient(app)

def reset_limiter():
    rate_limiter.client_timestamps.clear()

def test_health_endpoint():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_root_endpoint():
    res = client.get("/")
    assert res.status_code == 200
    text = res.text
    assert "Omni Downloader" in text
    assert "#D4AF37" in text
    assert "#050505" in text
    assert "/terms" in text
    assert "/privacy" in text

def test_terms_endpoint():
    res = client.get("/terms")
    assert res.status_code == 200
    text = res.text
    assert "Terms &amp; Conditions" in text or "Terms & Conditions" in text
    assert "DRM" in text

def test_privacy_endpoint():
    res = client.get("/privacy")
    assert res.status_code == 200
    text = res.text
    assert "Privacy Policy" in text
    assert "Data We Process" in text

def test_invalid_url_schemes():
    reset_limiter()
    invalid_cases = [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<h1>test</h1>",
        "ftp://example.com/stream.mp4",
        "gopher://example.com/",
        "blob:http://example.com/xyz",
        "not-a-valid-url",
        "",
    ]
    for url in invalid_cases:
        reset_limiter()
        res = client.post("/api/info", json={"url": url})
        assert res.status_code in (400, 422), f"URL '{url}' should be rejected, got {res.status_code}"

def test_localhost_and_private_ip_rejection():
    reset_limiter()
    ssrf_cases = [
        "http://localhost:8080/test",
        "http://localhost.localdomain/video",
        "http://127.0.0.1:8000/health",
        "http://10.0.0.1/video.mp4",
        "http://172.16.0.1/video.mp4",
        "http://192.168.1.1/video.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/video.mp4",
        "http://0.0.0.0:8000/",
    ]
    for url in ssrf_cases:
        reset_limiter()
        res = client.post("/api/info", json={"url": url})
        assert res.status_code == 400
        detail = res.json().get("detail", "")
        assert "Please enter a valid HTTP or HTTPS URL." in detail or "video could not be reached" in detail

def test_filename_sanitization():
    raw_title = 'Video: Test / 2026? "Final" <Cool> | [1080p]* ..traversal'
    sanitized = sanitize_filename(raw_title)
    for bad_char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        assert bad_char not in sanitized
    assert ".." not in sanitized
    assert sanitize_filename("") == "video"
    assert sanitize_filename("   ") == "video"

def test_rate_limiter():
    reset_limiter()
    for _ in range(10):
        res = client.post("/api/info", json={"url": "http://example.com/invalid"})
        assert res.status_code in (400, 500, 502, 504)
    # 11th request should be rate limited
    res = client.post("/api/info", json={"url": "http://example.com/invalid"})
    assert res.status_code == 429
    reset_limiter()

def test_format_normalization():
    mock_info = {
        "title": "Sample Media",
        "formats": [
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "height": 1080, "ext": "mp4", "tbr": 4000},
            {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080, "ext": "webm", "tbr": 3500},
            {"format_id": "136", "vcodec": "avc1", "acodec": "none", "height": 720, "ext": "mp4", "tbr": 2000},
            {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a.40.2", "height": 360, "ext": "mp4", "tbr": 500},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "tbr": 128},
        ]
    }
    fmts, has_any_audio = normalize_formats(mock_info)
    assert has_any_audio is True
    fids = [f["format_id"] for f in fmts]

    # Best 1080p by bitrate (137 has tbr 4000 > 248 tbr 3500)
    assert "137" in fids
    assert "248" not in fids  # Replaced by higher bitrate 1080p
    assert "136" in fids
    assert "18" in fids
    assert "mp3" in fids  # Audio option present

    # Check properties
    fmt_137 = next(f for f in fmts if f["format_id"] == "137")
    assert fmt_137["height"] == 1080
    assert fmt_137["has_video"] is True
    assert fmt_137["has_audio"] is False

    fmt_18 = next(f for f in fmts if f["format_id"] == "18")
    assert fmt_18["has_video"] is True
    assert fmt_18["has_audio"] is True

    fmt_mp3 = next(f for f in fmts if f["format_id"] == "mp3")
    assert fmt_mp3["has_video"] is False
    assert fmt_mp3["has_audio"] is True

def test_error_classification():
    # 1. YouTube bot check
    cat, msg = categorize_error_text(
        "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot. Use --cookies-from-browser",
        is_youtube=True,
    )
    assert cat == ErrorCategory.BOT_CHALLENGE
    assert "YouTube is currently unavailable from this server" in msg

    # 2. DRM error
    cat, msg = categorize_error_text("ERROR: [generic] This video is DRM-protected and cannot be downloaded.")
    assert cat == ErrorCategory.DRM_PROTECTED
    assert "DRM-protected" in msg

    # 3. Private content
    cat, msg = categorize_error_text("ERROR: [youtube] Private video. Sign in if you've been granted access.")
    assert cat == ErrorCategory.PRIVATE_CONTENT
    assert "private or requires authentication" in msg

    # 4. Unsupported source
    cat, msg = categorize_error_text("ERROR: Unsupported URL: https://unknown-site.xyz/media")
    assert cat == ErrorCategory.UNSUPPORTED_SOURCE
    assert "not supported" in msg

    # 5. Network/Host error
    cat, msg = categorize_error_text("ERROR: HTTP Error 403: Forbidden")
    assert cat == ErrorCategory.DOWNLOAD_FAILED

def test_job_system_and_file_serving(tmp_path):
    reset_limiter()
    # Create fake completed job
    job_id = "test-job-uuid-123"
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    media_file = job_dir / "media.mp4"
    media_file.write_bytes(b"dummy mp4 file content for test")

    jobs[job_id] = {
        "job_id": job_id,
        "status": "done",
        "progress": 100.0,
        "filename": "Safe_Video_Title.mp4",
        "filepath": str(media_file),
        "error": None,
        "created_at": time.time(),
    }

    # Status check
    status_res = client.get(f"/api/status/{job_id}")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["status"] == "done"
    assert data["progress"] == 100.0
    assert data["filename"] == "Safe_Video_Title.mp4"

    # File download check
    file_res = client.get(f"/api/file/{job_id}")
    assert file_res.status_code == 200
    assert file_res.content == b"dummy mp4 file content for test"
    assert "Safe_Video_Title.mp4" in file_res.headers.get("content-disposition", "")

    # Non-existent job check
    err_res = client.get("/api/file/non-existent-job")
    assert err_res.status_code == 404

    # Traversal attack check
    traversal_res = client.get("/api/file/.._.._etc_passwd")
    assert traversal_res.status_code in (400, 404)
