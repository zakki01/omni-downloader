import os
from starlette.testclient import TestClient
from app import (
    BrowserExtractor,
    ErrorCategory,
    InMemoryJobStore,
    LocalStorage,
    MediaExtractor,
    YouTubeExtractor,
    YtDlpExtractor,
    app,
    build_format_selector,
    build_ydl_base_opts,
    categorize_error,
    classify_error,
    detect_bot_check_error,
    detect_drm_error,
    get_available_heights,
    get_extractor,
    get_youtube_extractor_args,
    is_youtube_url,
    job_store,
    rate_limiter,
    sanitize_filename,
    storage_backend,
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
    assert "#D4AF37" in text

def test_privacy_endpoint():
    res = client.get("/privacy")
    assert res.status_code == 200
    text = res.text
    assert "Privacy Policy" in text
    assert "Data We Process" in text
    assert "#D4AF37" in text

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

def test_youtube_url_detection():
    assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True
    assert is_youtube_url("https://youtu.be/dQw4w9WgXcQ") is True
    assert is_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ") is True
    assert is_youtube_url("https://youtube-nocookie.com/embed/dQw4w9WgXcQ") is True
    assert is_youtube_url("https://example.com/video.mp4") is False
    assert is_youtube_url("https://vimeo.com/123456") is False

def test_youtube_extractor_args_and_po_token(monkeypatch):
    monkeypatch.delenv("YOUTUBE_PLAYER_CLIENTS", raising=False)
    monkeypatch.delenv("YOUTUBE_PO_TOKEN", raising=False)
    monkeypatch.delenv("POT_PROVIDER_DISABLE", raising=False)
    args = get_youtube_extractor_args()
    assert "youtubepot-bgutilhttp" in args
    assert args["youtubepot-bgutilhttp"]["base_url"] == ["http://127.0.0.1:4416"]

    monkeypatch.setenv("POT_PROVIDER_DISABLE", "1")
    assert get_youtube_extractor_args() == {}
    monkeypatch.delenv("POT_PROVIDER_DISABLE", raising=False)

    monkeypatch.setenv("YOUTUBE_PLAYER_CLIENTS", "android,web,mweb")
    monkeypatch.setenv("YOUTUBE_PO_TOKEN", "sample_po_token_123")
    args = get_youtube_extractor_args()
    assert "youtube" in args
    assert args["youtube"]["player_client"] == ["android", "web", "mweb"]
    assert "web.gvs+sample_po_token_123" in args["youtube"]["po_token"]
    assert "youtubepot-bgutilhttp" in args

def test_error_classification_bot_check_vs_drm():
    # 1. YouTube bot check error (Render/Datacenter IP scenario)
    bot_err_1 = Exception("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.")
    bot_err_2 = Exception("ERROR: [youtube] 12345: Sign in to confirm you're not a bot.")
    bot_err_3 = Exception("ERROR: [youtube] 12345: Unusual traffic from your computer network, solve a captcha")
    
    assert detect_bot_check_error(bot_err_1) is True
    assert detect_bot_check_error(bot_err_2) is True
    assert detect_bot_check_error(bot_err_3) is True

    cat_msg_1 = categorize_error(bot_err_1)
    assert "additional verification" in cat_msg_1
    assert "DRM" not in cat_msg_1  # Crucial: Must NEVER falsely claim DRM

    # 2. Genuine DRM error
    drm_err = Exception("ERROR: [generic] This video is DRM-protected and cannot be downloaded.")
    assert detect_drm_error(drm_err) is True
    assert categorize_error(drm_err) == "This video is DRM-protected and cannot be downloaded."

    # 3. Network unreachable
    net_err = Exception("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>")
    assert categorize_error(net_err) == "The video could not be reached. Please check the URL and try again."

def test_build_ydl_base_opts():
    yt_opts = build_ydl_base_opts("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert yt_opts["noplaylist"] is True
    assert "node" in yt_opts["js_runtimes"]
    assert "deno" in yt_opts["js_runtimes"]
    assert "ejs:github" in yt_opts["remote_components"]

    generic_opts = build_ydl_base_opts("https://example.com/sample.mp4")
    assert generic_opts["noplaylist"] is True

def test_media_extractor_routing():
    yt_ext = get_extractor("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert isinstance(yt_ext, YouTubeExtractor)

    generic_ext = get_extractor("https://example.com/video.mp4")
    assert isinstance(generic_ext, YtDlpExtractor)

    browser_ext = BrowserExtractor()
    assert browser_ext.supports("https://anything.com") is False

def test_in_memory_job_store():
    store = InMemoryJobStore()
    job = store.create_job("test_job_1")
    assert job["status"] == "running"
    assert job["progress"] == 0

    retrieved = store.get_job("test_job_1")
    assert retrieved is not None
    assert retrieved["status"] == "running"

    updated = store.update_job("test_job_1", progress=50, status="running")
    assert updated["progress"] == 50

    cleaned = store.cleanup_jobs(max_age_seconds=0)
    assert cleaned == 1
    assert store.get_job("test_job_1") is None

def test_storage_backend_and_sanitization(tmp_path):
    storage = LocalStorage(tmp_path)
    test_file = tmp_path / "test_job_2.mp4"
    test_file.write_text("dummy media content")

    found = storage.get_file_path("test_job_2")
    assert found == test_file

    storage.delete_job_files("test_job_2")
    assert not test_file.exists()

def test_detailed_error_classification():
    cat, msg = classify_error(Exception("sign in to confirm you're not a bot"))
    assert cat == ErrorCategory.YOUTUBE_BOT_CHALLENGE
    assert "additional verification" in msg

    cat, msg = classify_error(Exception("mweb client requires a GVS PO Token which was not provided"))
    assert cat == ErrorCategory.YOUTUBE_PO_TOKEN_REQUIRED
    assert "proof-of-origin verification" in msg

    cat, msg = classify_error(Exception("Private video. Sign in if you've been granted access."))
    assert cat == ErrorCategory.YOUTUBE_AUTH_REQUIRED

    cat, msg = classify_error(Exception("This video is DRM-protected and cannot be downloaded."))
    assert cat == ErrorCategory.DRM_PROTECTED

def test_pot_provider_manager_and_ping():
    from app import pot_manager
    was_running = pot_manager.is_running()
    if not was_running:
        started = pot_manager.start()
        assert started is True, f"Failed to start POT provider: {pot_manager.diagnostics.error_reason}"
    
    assert pot_manager.check_port_listening() is True
    ok, data, err = pot_manager.ping()
    assert ok is True
    assert data is not None
    assert "version" in data
    assert data["version"] == "2.0.2"
    
    diag = pot_manager.diagnostics.to_dict()
    assert diag["listening"] is True
    assert diag["ping"] is True
    assert diag["version"] == "2.0.2"

    if not was_running:
        pot_manager.stop()
        assert pot_manager.is_running() is False

def test_health_diagnostics_endpoint():
    client = TestClient(app)
    resp = client.get("/health/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "engine" in data
    engine = data["engine"]
    assert "ytdlp" in engine
    assert "ejs" in engine
    assert "ffmpeg" in engine
    assert "pot_provider" in engine
    assert "pot_provider_diagnostics" in engine

def test_api_diagnostics_live_endpoint():
    from app import pot_manager
    pot_manager.start()
    try:
        client = TestClient(app)
        resp = client.get("/api/diagnostics/live")
        assert resp.status_code == 200
        data = resp.json()
        assert "step1_and_2_provider_server" in data
        assert "step3_token_generation" in data
        assert "step4_outbound_network" in data
        assert "step5_extractor_configuration" in data
        assert "step6_actual_test" in data
        assert "root_cause_classification" in data
        assert data["step1_and_2_provider_server"]["listening_on_127_0_0_1_4416"] is True
        assert data["step1_and_2_provider_server"]["ping_ok"] is True
    finally:
        pot_manager.stop()




