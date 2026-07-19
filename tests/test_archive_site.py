import argparse
import datetime as dt
import hashlib
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


def canonical_slug(scientific_name):
    base = site.slugify(scientific_name) or "species"
    digest = hashlib.sha256(scientific_name.encode()).hexdigest()[:10]
    return f"{base}-{digest}"


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "~/Library/secret",
        r"C:\\secret",
        r"\\server\\share",
        "relative/private.txt",
        "internal.example.io",
        "fe80::1",
    ],
)
def test_public_review_notes_reject_unstructured_or_private_text(value):
    assert site.public_review_notes(value) is None


def test_public_review_notes_accepts_only_reconstructed_perch_grammar():
    note = (
        "Perch independently supports the BirdNET species claim. "
        "Claimed species rank 1 of 14795 with score 26.8%. "
        "Perch top results: Catharus fuscescens 26.8%; "
        "Sphecotheres vieilloti 26.3%; Cyanocorax violaceus 17.1%. "
        "Scores are independent classifier outputs, not calibrated probabilities."
    )
    assert site.public_review_notes(note) == note
    forged = note.replace("Catharus fuscescens", "Evil com")
    assert site.public_review_notes(forged) is None
    assert "Catharus fuscescens" in site.pinned_perch_taxa()
    assert "Evil com" not in site.pinned_perch_taxa()


def test_canonical_species_slug_is_valid_when_name_has_no_ascii_slug():
    slug = site.canonical_species_slug("🦉")
    assert slug.startswith("species-")
    assert site.SAFE_SLUG.fullmatch(slug)


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


def test_detection_pagination_uses_detection_id_as_stable_tiebreaker(archive_site):
    source = Path(site.__file__).read_text()
    assert "ORDER BY d.date DESC, d.time DESC, d.detection_id DESC" in source
    base, db_path = archive_site
    today = site.now_local().date().isoformat()
    with sqlite3.connect(db_path) as conn:
        insert_detection(
            conn,
            "e" * 64,
            today,
            "07:20:00",
            "Turdus migratorius",
            "American Robin",
            0.93,
        )
        conn.commit()
    _, _, first = get_json(base + "/api/detections?limit=1&offset=0")
    _, _, second = get_json(base + "/api/detections?limit=1&offset=1")
    assert first["detections"][0]["detection_id"] == "e" * 64
    assert second["detections"][0]["detection_id"] == "b" * 64


def test_detection_id_filter_returns_one_safe_evidence_record(archive_site):
    base, _ = archive_site
    detection_id = "b" * 64
    _, _, payload = get_json(base + f"/api/detections?detection_id={detection_id}&limit=1")
    assert payload["total"] == 1
    assert payload["detections"][0]["detection_id"] == detection_id
    assert payload["detections"][0]["review_status"] == "unreviewed"
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/detections?detection_id=not-safe", timeout=3)
    assert error.value.code == 400


def test_singular_detection_endpoint_returns_one_safe_record(archive_site):
    base, _ = archive_site
    detection_id = "a" * 64
    status, _, payload = get_json(base + f"/api/detections/{detection_id}")
    assert status == 200
    assert payload["detection"]["detection_id"] == detection_id
    assert payload["detection"]["common_name"] == "American Robin"
    serialised = json.dumps(payload)
    assert "latitude" not in serialised
    assert "longitude" not in serialised
    assert "audio_relpath" not in serialised
    assert "file_name" not in serialised
    assert payload["detection"]["newer_detection_id"] == "b" * 64
    assert payload["detection"]["older_detection_id"] is None


def test_singular_detection_endpoint_fails_closed(archive_site):
    base, _ = archive_site
    for path, expected in (
        ("/api/detections/not-safe", 400),
        ("/api/detections/" + "e" * 64, 404),
    ):
        with pytest.raises(HTTPError) as error:
            urlopen(base + path, timeout=3)
        assert error.value.code == expected


def test_species_detail_endpoint_returns_summary_without_private_fields(archive_site):
    base, _ = archive_site
    slug = canonical_slug("Turdus migratorius")
    status, _, payload = get_json(base + f"/api/species/{slug}")
    assert status == 200
    assert payload["species"] == {
        "scientific_name": "Turdus migratorius",
        "common_name": "American Robin",
        "slug": slug,
        "art_slug": "turdus-migratorius",
        "detections": 2,
        "days_heard": 1,
        "first_heard": payload["species"]["first_heard"],
        "last_heard": payload["species"]["last_heard"],
        "best_confidence": 0.94,
        "mean_confidence": pytest.approx(0.91),
        "clips_preserved": 1,
        "review_counts": {
            "confirmed": 0,
            "uncertain": 0,
            "rejected": 0,
            "pending": 1,
            "unreviewed": 1,
        },
    }
    serialised = json.dumps(payload)
    assert "latitude" not in serialised
    assert "longitude" not in serialised
    assert "audio_relpath" not in serialised


def test_species_detail_endpoint_rejects_invalid_or_missing_slug(archive_site):
    base, _ = archive_site
    for path, expected in (
        ("/api/species/../etc", 404),
        ("/api/species/not_a_slug", 400),
        ("/api/species/not-a-bird", 404),
    ):
        with pytest.raises(HTTPError) as error:
            urlopen(base + path, timeout=3)
        assert error.value.code == expected


def test_birds_prefixed_routes_and_assets_resolve_to_the_app(archive_site):
    base, _ = archive_site
    with urlopen(base + "/birds/detection/" + "a" * 64, timeout=3) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert b"The Listening Garden" in response.read()
    for route in ("/index.html", "/birds/index.html", "/species/corvus", "/birds/species/corvus"):
        with urlopen(base + route, timeout=3) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
    for asset in ("app.js", "routes.js"):
        with urlopen(base + f"/birds/{asset}", timeout=3) as response:
            assert response.headers["Content-Type"].startswith("application/javascript")
    status, _, payload = get_json(base + "/birds/api/species/turdus-migratorius")
    assert status == 200
    assert payload["species"]["common_name"] == "American Robin"


def test_unknown_or_malformed_frontend_routes_fail_closed(archive_site):
    base, _ = archive_site
    for path in (
        "/not-real",
        "/etc/passwd",
        "/detection/not-an-id",
        "/species/not_a_slug",
        "/birds/etc/passwd",
    ):
        with pytest.raises(HTTPError) as error:
            urlopen(base + path, timeout=3)
        assert error.value.code == 404


def test_malformed_ids_and_model_paths_never_reach_public_api(archive_site):
    base, db_path = archive_site
    with sqlite3.connect(db_path) as conn:
        insert_detection(
            conn, '\"><img src=x onerror=alert(1)>',
            site.now_local().date().isoformat(), "09:00:00",
            "Malicia avis", "Injected Candidate", 0.99,
        )
        conn.execute(
            """UPDATE configuration_epochs
               SET audio_model='/private/models/BirdNET.bin',
                   range_model='C:\\private\\models\\range.tflite'"""
        )
        conn.commit()

    _, _, ledger = get_json(base + "/api/detections?limit=20")
    assert ledger["total"] == 3
    serialised = json.dumps(ledger)
    assert "Injected Candidate" not in serialised
    assert "source_model" not in serialised

    _, _, epochs = get_json(base + "/api/epochs")
    assert epochs["epochs"][0]["audio_model"] == "BirdNET.bin"
    assert epochs["epochs"][0]["range_model"] == "range.tflite"
    assert "/private/" not in json.dumps(epochs)
    assert "C:\\private" not in json.dumps(epochs)


def test_public_dtos_redact_adversarial_operator_metadata(archive_site):
    base, db_path = archive_site
    marker = (
        "leak /private/secret /Users/prue/key C:\\Users\\prue\\key "
        "https://internal.example.test/a host.local internal.example.io "
        "192.168.1.9 fe80::1 prue@example.com 41.12345,-73.98765\x01"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO reviews
               (detection_id,status,reviewer,review_model,review_score,notes,reviewed_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("a" * 64, "uncertain", marker, marker, 0.42, marker, marker),
        )
        conn.execute(
            """INSERT INTO context_scores VALUES (?,?,?,?,?,?,?,?)""",
            ("a" * 64, marker, 0.1, 0.2, 3, 0.4, marker, marker),
        )
        conn.execute("UPDATE configuration_epochs SET reason=?", (marker,))
        conn.commit()

    payloads = [
        get_json(base + "/api/detections")[2],
        get_json(base + "/api/detections/" + "a" * 64)[2],
        get_json(base + "/api/epochs")[2],
    ]
    forbidden_keys = {
        "reviewer", "reviewed_at", "occurrence_prior", "local_prior",
        "repetition_count", "posterior", "algorithm_version", "ingested_at",
    }
    for payload in payloads:
        serialised = json.dumps(payload)
        assert not forbidden_keys.intersection(_all_keys(payload))
        for marker_part in (
            "/private", "/Users", "C:\\\\Users", "https://", ".local",
            "192.168.1.9", "fe80::1", "internal.example.io", "prue@example.com",
            "41.12345,-73.98765", "\\u0001",
        ):
            assert marker_part not in serialised

    detection = payloads[1]["detection"]
    assert detection["review_status"] == "uncertain"
    assert detection["review_score"] == 0.42
    assert detection["review_model"] == "Independent review"
    assert detection["notes"] is None
    assert payloads[2]["epochs"][0]["reason"] is None


def _all_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(map(_all_keys, value.values())))
    if isinstance(value, list):
        return set().union(*(map(_all_keys, value)), set())
    return set()


@pytest.mark.parametrize("path", [
    "/api/audio/" + "a" * 64 + "/extra",
    "/api/audio//" + "a" * 64,
    "/api/audio/%2e%2e/" + "a" * 64,
    "/api/audio/" + "a" * 64 + "%2fextra",
    "/art/turdus-migratorius.png/extra",
    "/art//turdus-migratorius.png",
    "/art/%2e%2e%2fturdus-migratorius.png",
    "/api/detections/" + "a" * 64 + "/extra",
    "/api/species/turdus-migratorius/extra",
])
def test_resource_dispatch_requires_an_exact_full_path(archive_site, path):
    base, _ = archive_site
    with pytest.raises(HTTPError) as error:
        urlopen(base + path, timeout=3)
    assert error.value.code in {400, 404}


@pytest.mark.parametrize("path", [
    "/explore//", "/explore////", "/about////", "/about/%2e%2e",
    "/species//", "/birds/explore////", "/birds/about/%2fextra",
])
def test_document_routes_allow_at_most_one_trailing_slash(archive_site, path):
    base, _ = archive_site
    with pytest.raises(HTTPError) as error:
        urlopen(base + path, timeout=3)
    assert error.value.code in {400, 404}

    for valid in ("/explore", "/explore/", "/about", "/about/"):
        with urlopen(base + valid, timeout=3) as response:
            assert response.status == 200


@pytest.mark.parametrize("query", [
    "confidence_min=nan", "confidence_min=inf", "confidence_min=-inf",
    "confidence_min=1e9999", "offset=1000001", "offset=1e9999",
])
def test_detection_numeric_filters_fail_closed(archive_site, query):
    base, _ = archive_site
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/detections?" + query, timeout=3)
    assert error.value.code == 400
    assert json.load(error.value) == {"error": "invalid numeric filter"}


def test_species_slugs_are_canonical_collision_resistant_and_legacy_safe(archive_site):
    base, db_path = archive_site
    today = site.now_local().date().isoformat()
    with sqlite3.connect(db_path) as conn:
        insert_detection(conn, "1" * 64, today, "09:00:00", "Foo bar", "First", 0.9)
        insert_detection(conn, "2" * 64, today, "09:01:00", "Foo-bar", "Second", 0.9)
        insert_detection(conn, "3" * 64, today, "09:02:00", "Dog", "Dog", 0.9)
        conn.commit()

    species = get_json(base + "/api/species")[2]["species"]
    by_name = {row["scientific_name"]: row for row in species}
    assert len({row["slug"] for row in species}) == len(species)
    for name in ("Foo bar", "Foo-bar", "Dog"):
        assert by_name[name]["slug"] == canonical_slug(name)
        assert by_name[name]["art_slug"] == site.slugify(name)
        detail = get_json(base + "/api/species/" + by_name[name]["slug"])[2]
        assert detail["species"]["scientific_name"] == name

    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/species/foo-bar", timeout=3)
    assert error.value.code == 409
    legacy = get_json(base + "/api/species/dog")[2]
    assert legacy["species"]["scientific_name"] == "Dog"


def test_detection_navigation_is_same_species_and_deterministic(archive_site):
    base, db_path = archive_site
    today = site.now_local().date().isoformat()
    with sqlite3.connect(db_path) as conn:
        insert_detection(conn, "e" * 64, today, "07:20:00", "Turdus migratorius", "Robin", 0.95)
        conn.commit()

    current = get_json(base + "/api/detections/" + "b" * 64)[2]["detection"]
    assert current["newer_detection_id"] == "e" * 64
    assert current["older_detection_id"] == "a" * 64


def test_frontend_escapes_detection_id_in_attribute_context():
    app_js = (Path(site.__file__).parent / "static" / "app.js").read_text()
    assert 'data-id="${escapeHTML(detectionId)}"' in app_js


def test_evidence_panel_renders_full_digest_with_safe_wrapping():
    static = Path(site.__file__).parent / "static"
    app_js = (static / "app.js").read_text()
    styles = (static / "styles.css").read_text()
    assert "${digest} · ${formatNumber(item.audio_bytes)} bytes" in app_js
    assert "digest.slice" not in app_js
    assert "#evidenceHash { overflow-wrap: anywhere;" in styles


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


def test_archive_database_remains_byte_identical_after_http_reads(archive_site):
    base, db_path = archive_site
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    for path in (
        "/api/detections?q=Robin",
        "/api/detections/" + "a" * 64,
        "/api/species",
        "/api/epochs",
        "/api/detections?offset=1000001",
    ):
        try:
            urlopen(base + path, timeout=3).read()
        except HTTPError as error:
            assert error.code == 400
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after
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
