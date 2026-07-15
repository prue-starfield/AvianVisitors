import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from avian.archive.sync_archive import SCHEMA
from avian.archive.site import server as site


def insert_detection(conn, did, date, time, sci, common, confidence, relpath=None):
    conn.execute(
        """INSERT INTO detections (
             detection_id, source_rowid, date, time, observed_at_local, timezone,
             scientific_name, common_name, confidence, latitude, longitude,
             cutoff, week, sensitivity, overlap, file_name, source_model,
             source_host, ingested_at, audio_relpath, audio_sha256, audio_bytes
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            did, 1, date, time, f"{date}T{time}", "America/New_York",
            sci, common, confidence, 41.0, -73.0, 0.7, 28, 1.25, 0.0,
            "sample.mp3", "BirdNET test", "test", "2026-07-15T16:00:00+00:00",
            relpath, "f" * 64 if relpath else None, 9 if relpath else None,
        ),
    )


@pytest.fixture()
def archive_site(tmp_path):
    db_path = tmp_path / "archive.sqlite3"
    audio_root = tmp_path / "audio"
    art_root = tmp_path / "art"
    static_root = Path(site.__file__).parent / "static"
    audio_file = audio_root / "By_Date" / "2026" / "07" / "sample.mp3"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"ID3abcdef")
    art_root.mkdir()
    (art_root / "turdus-migratorius.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    today = site.now_local().date()
    yesterday = today - dt.timedelta(days=1)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        insert_detection(
            conn, "a" * 64, today.isoformat(), "06:15:00",
            "Turdus migratorius", "American Robin", 0.88,
            "By_Date/2026/07/sample.mp3",
        )
        insert_detection(
            conn, "b" * 64, today.isoformat(), "07:20:00",
            "Turdus migratorius", "American Robin", 0.94,
        )
        insert_detection(
            conn, "c" * 64, yesterday.isoformat(), "18:00:00",
            "Cardinalis cardinalis", "Northern Cardinal", 0.91,
        )
        insert_detection(
            conn, "d" * 64, today.isoformat(), "08:00:00",
            "Ficta avis", "Below-threshold Candidate", 0.69,
        )
        conn.execute(
            """INSERT INTO sync_runs
               (started_at, completed_at, source_rows, inserted_rows, indexed_clips,
                snapshot_sha256, status)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-07-15T16:00:00+00:00", "2026-07-15T16:00:02+00:00", 3, 3, 1, "d" * 64, "ok"),
        )
        conn.execute(
            """INSERT INTO configuration_epochs VALUES (?,?,?,?,?,?,?,?)""",
            ("2026-07-15T09:35:00-04:00", "BirdNET test", "V2", 0.03, 0.7, 1.25, 0.0, "test epoch"),
        )
        conn.commit()

    config = argparse.Namespace(db=db_path, audio=audio_root, art=art_root, static=static_root)
    httpd = site.ThreadingHTTPServer(("127.0.0.1", 0), site.ArchiveHandler)
    httpd.config = config  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base, db_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def get_json(url):
    with urlopen(url, timeout=3) as response:
        return response.status, dict(response.headers), json.load(response)


def test_summary_and_today_share_calendar_day_policy(archive_site):
    base, _ = archive_site
    _, _, summary = get_json(base + "/api/summary")
    _, _, today = get_json(base + "/api/today")
    assert summary["totals"]["detections"] == 3
    assert summary["today"] == {
        "detections": 2,
        "species": 1,
        "date": site.now_local().date().isoformat(),
    }
    assert summary["policy"]["frame_parity"] is True
    assert len(today["species"]) == 1
    assert today["species"][0]["common_name"] == "American Robin"
    assert today["species"][0]["detections"] == 2


def test_api_does_not_expose_coordinates_or_audio_paths(archive_site):
    base, _ = archive_site
    _, _, payload = get_json(base + "/api/detections?limit=10")
    serialised = json.dumps(payload)
    assert "latitude" not in serialised
    assert "longitude" not in serialised
    assert "audio_relpath" not in serialised
    assert "file_name" not in serialised
    assert payload["total"] == 3
    assert payload["detections"][1]["review_status"] == "pending"


def test_publication_floor_applies_to_every_public_view(archive_site):
    base, _ = archive_site
    for endpoint in ("/api/summary", "/api/today", "/api/activity", "/api/species", "/api/seasonality"):
        _, _, payload = get_json(base + endpoint)
        assert "Below-threshold Candidate" not in json.dumps(payload)
    _, _, ledger = get_json(base + "/api/detections?confidence_min=0")
    assert ledger["total"] == 3
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/audio/" + "d" * 64, timeout=3)
    assert error.value.code == 404


def test_audio_is_only_resolved_by_detection_id_and_supports_range(archive_site):
    base, _ = archive_site
    with urlopen(base + "/api/audio/" + "a" * 64, timeout=3) as response:
        assert response.status == 200
        assert response.read() == b"ID3abcdef"
    request = Request(base + "/api/audio/" + "a" * 64, headers={"Range": "bytes=3-5"})
    with urlopen(request, timeout=3) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 3-5/9"
        assert response.read() == b"abc"
    suffix_request = Request(base + "/api/audio/" + "a" * 64, headers={"Range": "bytes=-3"})
    with urlopen(suffix_request, timeout=3) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 6-8/9"
        assert response.read() == b"def"
    open_ended = Request(
        base + "/api/audio/" + "a" * 64,
        headers={"Range": "bytes=6-"},
    )
    with urlopen(open_ended, timeout=3) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 6-8/9"
        assert response.read() == b"def"
    head = Request(
        base + "/api/audio/" + "a" * 64,
        headers={"Range": "bytes=0-2"},
        method="HEAD",
    )
    with urlopen(head, timeout=3) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 0-2/9"
        assert response.read() == b""
    empty_request = Request(base + "/api/audio/" + "a" * 64, headers={"Range": "bytes=-"})
    with pytest.raises(HTTPError) as error:
        urlopen(empty_request, timeout=3)
    assert error.value.code == 416
    assert error.value.headers["Content-Range"] == "bytes */9"
    for invalid_range in ("bytes=-0", "bytes=0-1,3-4"):
        request = Request(
            base + "/api/audio/" + "a" * 64,
            headers={"Range": invalid_range},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=3)
        assert error.value.code == 416
        assert error.value.headers["Content-Range"] == "bytes */9"
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/audio/../../etc/passwd", timeout=3)
    assert error.value.code in {400, 404}


def test_file_open_failure_returns_one_coherent_503(archive_site):
    base, db_path = archive_site
    audio_file = db_path.parent / "audio" / "By_Date" / "2026" / "07" / "sample.mp3"
    audio_file.chmod(0)
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/audio/" + "a" * 64, timeout=3)
        assert error.value.code == 503
        assert json.load(error.value) == {"error": "archive temporarily unavailable"}
    finally:
        audio_file.chmod(0o600)


def test_archive_database_remains_read_only(archive_site):
    base, db_path = archive_site
    before = sqlite3.connect(db_path).execute("SELECT count(*) FROM detections").fetchone()[0]
    status, _, _ = get_json(base + "/api/detections?q=Robin")
    after = sqlite3.connect(db_path).execute("SELECT count(*) FROM detections").fetchone()[0]
    assert status == 200
    assert before == after == 4
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/not-real", timeout=3)
    assert error.value.code == 404


def test_health_and_security_headers(archive_site):
    base, _ = archive_site
    status, headers, health = get_json(base + "/health")
    assert status == 200
    assert health["database"] == "ok"
    assert health["detections"] == 4
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
