import sys
import time
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from starlette.testclient import TestClient
from app import app, job_store, storage_backend, classify_error, ErrorCategory, rate_limiter, DOWNLOAD_DIR

client = TestClient(app)
results = {}

def reset_limiter():
    rate_limiter.client_timestamps.clear()

print("=== STARTING 10-TEST MATRIX ===")

# TEST 1: Direct MP4
try:
    reset_limiter()
    url = "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/person-bicycle-car-detection.mp4"
    r_info = client.post("/api/info", json={"url": url})
    assert r_info.status_code == 200, f"Status {r_info.status_code}"
    title = r_info.json()["title"]
    r_dl = client.post("/api/download", json={"url": url, "quality": "best"})
    jid1 = r_dl.json()["job_id"]
    for _ in range(30):
        time.sleep(1)
        st = client.get(f"/api/status/{jid1}").json()
        if st["status"] in ("done", "error"):
            break
    assert st["status"] == "done", f"Status: {st}"
    results["TEST 1: Direct MP4"] = f"PASSED (Title: '{title}', Job: {jid1})"
except Exception as e:
    results["TEST 1: Direct MP4"] = f"FAILED: {e}"

# TEST 2: Non-YouTube yt-dlp site / direct media stream
try:
    reset_limiter()
    url = "https://github.com/mediaelement/mediaelement-files/raw/master/big_buck_bunny.mp4"
    r_info = client.post("/api/info", json={"url": url})
    assert r_info.status_code == 200, f"Status {r_info.status_code}: {r_info.text}"
    title = r_info.json()["title"]
    results["TEST 2: Non-YouTube yt-dlp site"] = f"PASSED (Title: '{title}')"
except Exception as e:
    results["TEST 2: Non-YouTube yt-dlp site"] = f"FAILED: {e}"

# TEST 3: YouTube normal video
try:
    reset_limiter()
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    r_info = client.post("/api/info", json={"url": url})
    assert r_info.status_code == 200, f"Status {r_info.status_code}: {r_info.text}"
    title = r_info.json()["title"]
    r_dl = client.post("/api/download", json={"url": url, "quality": "best"})
    jid3 = r_dl.json()["job_id"]
    for _ in range(30):
        time.sleep(1)
        st = client.get(f"/api/status/{jid3}").json()
        if st["status"] in ("done", "error"):
            break
    assert st["status"] == "done", f"Status: {st}"
    results["TEST 3: YouTube normal video"] = f"PASSED (Title: '{title}', Job: {jid3})"
except Exception as e:
    results["TEST 3: YouTube normal video"] = f"FAILED: {e}"

# TEST 4: YouTube Shorts
try:
    reset_limiter()
    url = "https://www.youtube.com/shorts/kJQP7kiw5Fk"
    r_info = client.post("/api/info", json={"url": url})
    assert r_info.status_code == 200, f"Status {r_info.status_code}: {r_info.text}"
    title = r_info.json()["title"]
    results["TEST 4: YouTube Shorts"] = f"PASSED (Title: '{title}')"
except Exception as e:
    results["TEST 4: YouTube Shorts"] = f"FAILED: {e}"

# TEST 5: YouTube audio
try:
    reset_limiter()
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    r_dl = client.post("/api/download", json={"url": url, "quality": "audio"})
    assert r_dl.status_code == 200, f"Status {r_dl.status_code}: {r_dl.text}"
    jid5 = r_dl.json()["job_id"]
    for _ in range(30):
        time.sleep(1)
        st = client.get(f"/api/status/{jid5}").json()
        if st["status"] in ("done", "error"):
            break
    assert st["status"] == "done", f"Status: {st}"
    f_path = job_store.get_job(jid5)["file_path"]
    assert f_path.endswith(".mp3"), f"Expected .mp3, got {f_path}"
    assert os.path.exists(f_path) and os.path.getsize(f_path) > 0
    results["TEST 5: YouTube audio"] = f"PASSED (Extracted MP3, Job: {jid5})"
except Exception as e:
    results["TEST 5: YouTube audio"] = f"FAILED: {e}"

# TEST 6: Different quality (240p)
try:
    reset_limiter()
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    r_dl = client.post("/api/download", json={"url": url, "quality": "240"})
    assert r_dl.status_code == 200, f"Status {r_dl.status_code}: {r_dl.text}"
    jid6 = r_dl.json()["job_id"]
    for _ in range(30):
        time.sleep(1)
        st = client.get(f"/api/status/{jid6}").json()
        if st["status"] in ("done", "error"):
            break
    assert st["status"] == "done", f"Status: {st}"
    f_path = job_store.get_job(jid6)["file_path"]
    assert os.path.exists(f_path) and os.path.getsize(f_path) > 0
    results["TEST 6: Different quality (240p)"] = f"PASSED (Job: {jid6}, Size: {os.path.getsize(f_path)} bytes)"
except Exception as e:
    results["TEST 6: Different quality (240p)"] = f"FAILED: {e}"

# TEST 7: Invalid URL
try:
    reset_limiter()
    r = client.post("/api/info", json={"url": "invalid://bad-scheme"})
    assert r.status_code == 400
    detail = r.json().get("detail")
    results["TEST 7: Invalid URL"] = f"PASSED (HTTP 400, detail: '{detail}')"
except Exception as e:
    results["TEST 7: Invalid URL"] = f"FAILED: {e}"

# TEST 8: SSRF URL
try:
    reset_limiter()
    r = client.post("/api/info", json={"url": "http://127.0.0.1:8000/secret"})
    assert r.status_code == 400
    detail = r.json().get("detail")
    results["TEST 8: SSRF URL"] = f"PASSED (HTTP 400 blocked, detail: '{detail}')"
except Exception as e:
    results["TEST 8: SSRF URL"] = f"FAILED: {e}"

# TEST 9: DRM-protected content
try:
    cat, msg = classify_error(Exception("This video is DRM-protected and cannot be downloaded."))
    assert cat == ErrorCategory.DRM_PROTECTED
    results["TEST 9: DRM-protected content"] = f"PASSED (Clean classification: '{msg}')"
except Exception as e:
    results["TEST 9: DRM-protected content"] = f"FAILED: {e}"

# TEST 10: Download output verification
try:
    r_file = client.get(f"/api/file/{jid1}")
    assert r_file.status_code == 200
    assert len(r_file.content) > 1000000
    results["TEST 10: Download output verification"] = f"PASSED (Binary payload verified: {len(r_file.content)} bytes)"
except Exception as e:
    results["TEST 10: Download output verification"] = f"FAILED: {e}"

# Clean up downloaded artifacts
for f in DOWNLOAD_DIR.iterdir():
    if f.is_file() and f.name != ".gitkeep":
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass

print("\n=== RESULTS SUMMARY ===")
for k, v in results.items():
    print(f"{k}: {v}")
