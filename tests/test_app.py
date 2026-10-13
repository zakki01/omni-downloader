from starlette.testclient import TestClient
from app import (
    app,
    build_format_selector,
    get_available_heights,
    rate_limiter,
    sanitize_filename,
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

def test_format_selectors():
    best_fmt, best_pp = build_format_selector("best")
    assert best_fmt == "bv*+ba/b"
    assert len(best_pp) == 0

    h_fmt, h_pp = build_format_selector("720")
    assert h_fmt == "bv*[height<=720]+ba/b[height<=720]"
    assert len(h_pp) == 0

    audio_fmt, audio_pp = build_format_selector("audio")
    assert audio_fmt == "ba/b"
    assert len(audio_pp) == 1
    assert audio_pp[0]["key"] == "FFmpegExtractAudio"
    assert audio_pp[0]["preferredcodec"] == "mp3"

def test_get_available_heights():
    formats = [
        {"format_id": "1", "vcodec": "none", "height": None},  # Audio-only
        {"format_id": "2", "vcodec": "avc1", "height": 360},
        {"format_id": "3", "vcodec": "avc1", "height": 720},
        {"format_id": "4", "vcodec": "vp9", "height": 720},   # Duplicate
        {"format_id": "5", "vcodec": "avc1", "height": 1080},
        {"format_id": "6", "vcodec": "none", "height": 1080}, # Audio with bad metadata
    ]
    heights = get_available_heights(formats)
    assert heights == [360, 720, 1080]

def test_rate_limiter():
    reset_limiter()
    for _ in range(5):
        res = client.post("/api/info", json={"url": "invalid://test"})
        assert res.status_code == 400
    res = client.post("/api/info", json={"url": "invalid://test"})
    assert res.status_code == 429
    reset_limiter()
