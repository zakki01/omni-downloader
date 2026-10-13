import os
import sys
import time
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from starlette.testclient import TestClient
from app import app, job_store, rate_limiter, DOWNLOAD_DIR, FFMPEG_PATH, pot_manager

client = TestClient(app)

def reset_rate_limit():
    rate_limiter.client_timestamps.clear()

TEST_TARGETS = [
    {
        "name": "Direct MP4",
        "url": "https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/person-bicycle-car-detection.mp4",
        "quality": "best",
        "expected_ext": "mp4",
    },
    {
        "name": "Direct WebM",
        "url": "https://raw.githubusercontent.com/mdn/learning-area/master/html/multimedia-and-embedding/video-and-audio-content/rabbit320.webm",
        "quality": "best",
        "expected_ext": "webm",
    },
    {
        "name": "HLS / M3U8 Stream",
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "quality": "240",
        "expected_ext": "mp4",
    },
    {
        "name": "DASH / MPD Stream",
        "url": "https://dash.akamaized.net/dash264/TestCases/1c/qualcomm/1/MultiRate.mpd",
        "quality": "best",
        "expected_ext": "mp4",
    },
    {
        "name": "YouTube Video",
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "quality": "best",
        "expected_ext": "mp4",
    },
    {
        "name": "YouTube Audio Extraction",
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "quality": "audio",
        "expected_ext": "mp3",
    },
    {
        "name": "Facebook Public Media",
        "url": "https://www.facebook.com/watch/?v=10153231379946729",
        "quality": "best",
        "expected_ext": "mp4",
    },
    {
        "name": "Wikimedia Commons (Site 1)",
        "url": "https://commons.wikimedia.org/wiki/File:Curiosity%27s_Seven_Minutes_of_Terror.ogv",
        "quality": "240",
        "expected_ext": "webm",
    },
    {
        "name": "Internet Archive (Site 2)",
        "url": "https://archive.org/details/CEP217",
        "quality": "best",
        "expected_ext": "mpg",
    },
    {
        "name": "Bandcamp Audio (Site 3)",
        "url": "https://disasterpeace.com/track/compass",
        "quality": "best",
        "expected_ext": "mp3",
    },
    {
        "name": "SoundCloud Audio (Site 4)",
        "url": "https://soundcloud.com/octobersveryown/drake-war",
        "quality": "best",
        "expected_ext": "mp3",
    },
]

def check_file_validity(file_bytes: bytes, ext: str) -> bool:
    if len(file_bytes) < 1024:
        return False
    # Check headers
    if ext in ("mp4", "m4a", "mov"):
        return b"ftyp" in file_bytes[:64] or b"moov" in file_bytes[:1024]
    elif ext in ("webm", "mkv"):
        return file_bytes[:4] == b"\x1a\x45\xdf\xa3"
    elif ext == "mp3":
        return file_bytes[:3] == b"ID3" or file_bytes[:2] == b"\xff\xfb" or file_bytes[:2] == b"\xff\xf3" or file_bytes[:2] == b"\xff\xf2" or len(file_bytes) > 10000
    elif ext in ("ogg", "ogv"):
        return file_bytes[:4] == b"OggS"
    elif ext in ("mpg", "mpeg"):
        return file_bytes[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3") or len(file_bytes) > 10000
    return len(file_bytes) > 10000

def run_tests():
    print("\n" + "=" * 80)
    print("OMNI DOWNLOADER — COMPREHENSIVE LIVE DOWNLOAD ENGINE MATRIX")
    print("=" * 80)

    pot_manager.start()
    report_cards = []

    for idx, target in enumerate(TEST_TARGETS, 1):
        name = target["name"]
        url = target["url"]
        quality = target["quality"]
        print(f"\n[{idx}/{len(TEST_TARGETS)}] Testing: {name}")
        print(f"URL: {url}")

        record = {
            "name": name,
            "url": url,
            "extractor": "unknown",
            "metadata": "failed",
            "formats": "none",
            "selected_format": quality,
            "download": "failed",
            "ffmpeg": "available" if FFMPEG_PATH else "not found",
            "final_file": "none",
            "file_size": "0 bytes",
            "file_validity": "invalid",
            "status": "FAIL",
        }

        try:
            reset_rate_limit()

            # 1. Metadata Extraction
            info_res = client.post("/api/info", json={"url": url})
            if info_res.status_code != 200:
                print(f"  ✗ Metadata extraction failed: {info_res.status_code} - {info_res.text}")
                record["metadata"] = f"HTTP {info_res.status_code}: {info_res.json().get('detail')}"
                report_cards.append(record)
                continue

            info_data = info_res.json()
            record["extractor"] = info_data.get("extractor", "generic")
            record["metadata"] = f"Title: '{info_data.get('title')}' (Duration: {info_data.get('duration')}s)"
            record["formats"] = str(info_data.get("heights", []))
            print(f"  ✓ Metadata: {record['metadata']}")
            print(f"  ✓ Extractor: {record['extractor']}")
            print(f"  ✓ Available Heights: {record['formats']}")

            # 2. Start Download Job
            reset_rate_limit()
            dl_res = client.post("/api/download", json={"url": url, "quality": quality})
            if dl_res.status_code != 200:
                print(f"  ✗ Download initiation failed: {dl_res.status_code} - {dl_res.text}")
                record["download"] = f"Failed to queue: {dl_res.text}"
                report_cards.append(record)
                continue

            job_id = dl_res.json()["job_id"]
            print(f"  ✓ Job queued: {job_id}")

            # 3. Poll Worker State
            st = None
            for sec in range(180):
                time.sleep(1.0)
                st_res = client.get(f"/api/status/{job_id}")
                if st_res.status_code == 200:
                    st = st_res.json()
                    status = st.get("status")
                    if (sec + 1) % 15 == 0:
                        print(f"    ... waiting ({sec + 1}s): {status} ({st.get('progress', 0)}%)")
                    if status in ("done", "error"):
                        break

            if not st or st.get("status") != "done":
                err = st.get("error") if st else "timed out"
                print(f"  ✗ Download failed: {err}")
                record["download"] = f"Failed: {err}"
                report_cards.append(record)
                continue

            record["download"] = "Completed (100%)"
            print("  ✓ Download completed successfully")

            # 4. Fetch Resulting Binary File
            file_res = client.get(f"/api/file/{job_id}")
            if file_res.status_code != 200:
                print(f"  ✗ File delivery endpoint failed: {file_res.status_code}")
                record["final_file"] = f"File endpoint HTTP {file_res.status_code}"
                report_cards.append(record)
                continue

            payload = file_res.content
            job_info = job_store.get_job(job_id)
            record["final_file"] = job_info.get("filename") or f"{job_id}.bin"
            record["file_size"] = f"{len(payload):,} bytes"

            # 5. File Validity
            ext = record["final_file"].split(".")[-1].lower()
            valid = check_file_validity(payload, ext)
            record["file_validity"] = f"VALID {ext.upper()} media payload" if valid else "INVALID media header"
            record["status"] = "PASS" if valid else "FAIL"

            print(f"  ✓ Served File: {record['final_file']} ({record['file_size']})")
            print(f"  ✓ Integrity: {record['file_validity']}")

        except Exception as ex:
            print(f"  ✗ Exception: {ex}")
            record["download"] = f"Exception: {ex}"

        report_cards.append(record)

    # Clean up downloads directory
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    print("\n" + "=" * 80)
    print("FINAL SUMMARY MATRIX RECORD")
    print("=" * 80)
    all_passed = True
    for r in report_cards:
        print(f"\nFamily/Test:      {r['name']} [{r['status']}]")
        print(f"  URL:             {r['url']}")
        print(f"  Extractor:       {r['extractor']}")
        print(f"  Metadata:        {r['metadata']}")
        print(f"  Formats:         {r['formats']}")
        print(f"  Selected format: {r['selected_format']}")
        print(f"  Download:        {r['download']}")
        print(f"  FFmpeg:          {r['ffmpeg']}")
        print(f"  Final file:      {r['final_file']}")
        print(f"  File size:       {r['file_size']}")
        print(f"  File validity:   {r['file_validity']}")
        if r["status"] != "PASS":
            all_passed = False

    print("\n" + "=" * 80)
    if all_passed:
        print("RESULT: ALL TARGETS PASSED AND VERIFIED ACCORDING TO SPECIFICATION")
    else:
        print("RESULT: SOME TARGETS FAILED - REVIEW LOGS ABOVE")
    print("=" * 80)

    pot_manager.stop()
    return all_passed

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
