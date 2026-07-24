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

from avian.archive import corroboration, sync_archive
from avian.archive.sync_archive import SCHEMA, ensure_audio_quality_schema
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


def insert_interpretation(
    conn, did, outcome, claim_score, claim_rank, top_label, top_score,
):
    conn.execute(
        "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
        (
            did, "uncertain", "automated independent model",
            corroboration.PERCH_MODEL_NAME, claim_score, "review note",
            "2026-07-24T12:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO review_interpretations
           (detection_id,policy_version,outcome,claim_score,claim_rank,label_count,
            top_label,top_score,top_score_provenance,interpreted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            did, corroboration.POLICY_VERSION, outcome, claim_score, claim_rank,
            14795, top_label, top_score, "live_model_output_exact",
            "2026-07-24T12:00:00+00:00",
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
        sync_archive.initialise_archive(conn)
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


def test_public_audio_quality_rejects_fractional_sample_rate():
    row = {
        "quality_algorithm": site.PUBLIC_AUDIO_QUALITY_ALGORITHM,
        "quality_sample_rate": 32000.5,
        "quality_duration_seconds": 6.0,
        "quality_noise_floor_dbfs": -50.0,
        "quality_signal_level_dbfs": -30.0,
        "quality_signal_contrast_db": 20.0,
        "quality_clipping_fraction": 0.0,
    }
    assert site.public_audio_quality(row) is None
    larger_preserved_clip = 6 * 1024 * 1024
    assert site.public_positive_int(
        larger_preserved_clip, site.MAX_PUBLIC_AUDIO_BYTES,
    ) == larger_preserved_clip


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
        "standing": "uncorroborated",
        "review_policy_version": corroboration.POLICY_VERSION,
        "review_counts": {
            "corroborated": 0,
            "uncorroborated": 0,
            "model_conflict": 0,
            "pending": 1,
            "unreviewed": 1,
        },
    }
    serialised = json.dumps(payload)
    assert "latitude" not in serialised
    assert "longitude" not in serialised
    assert "audio_relpath" not in serialised


def test_species_and_detection_apis_expose_versioned_corroboration_standing(archive_site):
    base, db_path = archive_site
    today = site.now_local().date().isoformat()
    with sqlite3.connect(db_path) as conn:
        insert_interpretation(
            conn, "a" * 64, "corroborated", 0.676, 1,
            "Turdus migratorius", 0.676,
        )
        insert_interpretation(
            conn, "c" * 64, "model_conflict", 0.021, 8,
            "Catharus fuscescens", 0.475,
        )
        insert_detection(
            conn, "e" * 64, today, "20:34:15", "Pandion haliaetus", "Osprey", 0.8078,
        )
        insert_interpretation(
            conn, "e" * 64, "uncorroborated", 0.0784806982, 2,
            "Icterus galbula", 0.099,
        )
        conn.commit()

    species = get_json(base + "/api/species")[2]
    by_name = {row["common_name"]: row for row in species["species"]}
    assert species["policy_version"] == corroboration.POLICY_VERSION
    assert by_name["American Robin"]["standing"] == "corroborated"
    assert by_name["American Robin"]["review_counts"] == {
        "corroborated": 1,
        "uncorroborated": 0,
        "model_conflict": 0,
        "pending": 0,
        "unreviewed": 1,
    }
    assert by_name["Osprey"]["standing"] == "uncorroborated"
    assert by_name["Osprey"]["review_counts"]["uncorroborated"] == 1
    assert by_name["Northern Cardinal"]["standing"] == "uncorroborated"
    assert by_name["Northern Cardinal"]["review_counts"]["model_conflict"] == 1

    detection = get_json(base + "/api/detections/" + "e" * 64)[2]["detection"]
    assert detection["review_status"] == "uncorroborated"
    assert detection["review_policy_version"] == corroboration.POLICY_VERSION
    assert detection["review_rank"] == 2
    assert detection["review_label_count"] == 14795
    assert detection["review_top_label"] == "Icterus galbula"
    assert detection["review_top_score"] == 0.099

    filtered = get_json(base + "/api/detections?review=model_conflict")[2]
    assert [row["common_name"] for row in filtered["detections"]] == ["Northern Cardinal"]
    summary = get_json(base + "/api/summary")[2]
    assert summary["totals"]["corroborated_species"] == 1
    assert summary["totals"]["uncorroborated_species"] == 2


def test_public_surfaces_share_one_fail_closed_interpretation_boundary(archive_site):
    base, db_path = archive_site
    before = get_json(base + "/api/summary")[2]["totals"]
    did = "9" * 64
    with sqlite3.connect(db_path) as conn:
        insert_detection(
            conn, did, "2026-07-24", "09:00:00", "Pandion haliaetus",
            "Poison Osprey", 0.81, f"By_Date/2026/07/{did}.mp3",
        )
        conn.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
            (
                did, "uncertain", "reviewer", corroboration.PERCH_MODEL_NAME,
                0.01, "forged review", "2026-07-24T13:00:00+00:00",
            ),
        )
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            """INSERT INTO review_interpretations
               (detection_id,policy_version,outcome,claim_score,claim_rank,
                label_count,top_label,top_score,top_score_provenance,interpreted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                did, corroboration.POLICY_VERSION, "corroborated", 0.01, 8,
                14795, "Evil com", 0.90, "live_model_output_exact",
                "2026-07-24T13:00:00+00:00",
            ),
        )
        conn.commit()

    detail = get_json(base + f"/api/detections/{did}")[2]["detection"]
    assert detail["review_status"] == "pending"
    assert detail["review_policy_version"] is None
    filtered = get_json(
        base + "/api/detections?q=Poison&review=corroborated"
    )[2]
    assert filtered["total"] == 0
    assert filtered["detections"] == []
    poison = next(
        item for item in get_json(base + "/api/species")[2]["species"]
        if item["common_name"] == "Poison Osprey"
    )
    assert poison["standing"] == "uncorroborated"
    assert poison["review_counts"] == {
        "corroborated": 0,
        "uncorroborated": 0,
        "model_conflict": 0,
        "pending": 1,
        "unreviewed": 0,
    }
    after = get_json(base + "/api/summary")[2]["totals"]
    assert after["corroborated_species"] == before["corroborated_species"]


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
    for asset, expected_type, signature in (
        ("favicon.svg?v=2", "image/svg+xml", b"<svg"),
        ("favicon-32.png?v=2", "image/png", b"\x89PNG\r\n\x1a\n"),
        ("apple-touch-icon.png?v=2", "image/png", b"\x89PNG\r\n\x1a\n"),
    ):
        with urlopen(base + f"/birds/{asset}", timeout=3) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith(expected_type)
            assert response.read().lstrip().startswith(signature)
    with urlopen(base + "/birds/index.html", timeout=3) as response:
        html = response.read().decode("utf-8")
    assert 'href="favicon-32.png?v=2" sizes="32x32" type="image/png"' in html
    assert 'href="favicon.svg?v=2" sizes="any" type="image/svg+xml"' in html
    assert 'href="apple-touch-icon.png?v=2" sizes="180x180"' in html
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
    assert detection["review_status"] == "pending"
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


def test_related_calls_are_bounded_ranked_and_quality_sanitised(archive_site):
    base, db_path = archive_site
    today = site.now_local().date().isoformat()
    relpath = "By_Date/2026/07/sample.mp3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE detections SET audio_relpath=?,audio_sha256=?,audio_bytes=?
               WHERE detection_id=?""",
            (relpath, "b" * 64, 9, "b" * 64),
        )
        insert_detection(conn, "e" * 64, today, "08:20:00", "Turdus migratorius", "American Robin", 0.96, relpath)
        insert_detection(conn, "f" * 64, today, "09:20:00", "Turdus migratorius", "American Robin", 0.99, relpath)
        insert_detection(conn, "g" * 64, today, "10:20:00", "Turdus migratorius", "American Robin", 0.99, "By_Date/2026/07/missing.mp3")
        conn.executemany(
            """INSERT INTO reviews
               (detection_id,status,reviewer,review_model,review_score,notes,reviewed_at)
               VALUES (?,?,?,?,?,?,?)""",
            [
                ("b" * 64, "confirmed", "reviewer", site.PUBLIC_REVIEW_MODEL, 0.90, None, "now"),
                ("e" * 64, "confirmed", "reviewer", site.PUBLIC_REVIEW_MODEL, 0.80, None, "now"),
                ("f" * 64, "uncertain", "reviewer", site.PUBLIC_REVIEW_MODEL, 0.99, None, "now"),
            ],
        )
        conn.executemany(
            """INSERT INTO review_interpretations
               (detection_id,policy_version,outcome,claim_score,claim_rank,
                label_count,top_label,top_score,top_score_provenance,interpreted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                ("b" * 64, corroboration.POLICY_VERSION, "corroborated", 0.90,
                 1, 14795, "Turdus migratorius", 0.90,
                 "live_model_output_exact", "now"),
                ("e" * 64, corroboration.POLICY_VERSION, "corroborated", 0.80,
                 1, 14795, "Turdus migratorius", 0.80,
                 "live_model_output_exact", "now"),
                ("f" * 64, corroboration.POLICY_VERSION, "uncorroborated", 0.99,
                 2, 14795, "Icterus galbula", 0.995,
                 "live_model_output_exact", "now"),
            ],
        )
        conn.executemany(
            """INSERT INTO audio_quality
               (detection_id,algorithm_version,sample_rate,duration_seconds,
                noise_floor_dbfs,signal_level_dbfs,signal_contrast_db,
                clipping_fraction,computed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                ("a" * 64, site.PUBLIC_AUDIO_QUALITY_ALGORITHM, 32000, 6.0, -50.0, -45.0, 5.0, 0.0, "now"),
                ("b" * 64, site.PUBLIC_AUDIO_QUALITY_ALGORITHM, 32000, 6.0, -45.0, -35.0, 10.0, 0.0, "now"),
                ("e" * 64, site.PUBLIC_AUDIO_QUALITY_ALGORITHM, 32000, 6.0, -50.0, -30.0, 20.0, 0.0, "now"),
                ("f" * 64, site.PUBLIC_AUDIO_QUALITY_ALGORITHM, 32000, 6.0, -60.0, -30.0, 30.0, 0.0, "now"),
            ],
        )
        conn.commit()

    current = get_json(base + "/api/detections/" + "a" * 64)[2]["detection"]
    assert current["audio_quality"] == {
        "method": site.PUBLIC_AUDIO_QUALITY_ALGORITHM,
        "sample_rate_hz": 32000,
        "duration_seconds": 6.0,
        "quiet_frame_level_dbfs": -50.0,
        "high_energy_frame_level_dbfs": -45.0,
        "audio_contrast_db": 5.0,
        "near_full_scale_fraction": 0.0,
    }

    best = get_json(base + "/api/detections/" + "a" * 64 + "/related?sort=best")[2]
    assert best["total"] == 3
    assert best["sort"] == "best"
    assert site.HEX_ID.fullmatch(best["revision"])
    assert [row["detection_id"] for row in best["calls"]] == ["b" * 64, "e" * 64, "f" * 64]
    assert best["calls"][0]["review_kind"] == "perch"
    assert all(row["detection_id"] not in {"a" * 64, "g" * 64} for row in best["calls"])
    assert best["calls"][0]["audio_quality"]["audio_contrast_db"] == 10.0

    contrast = get_json(base + "/api/detections/" + "a" * 64 + "/related?sort=contrast&limit=2")[2]
    assert [row["detection_id"] for row in contrast["calls"]] == ["f" * 64, "e" * 64]
    assert contrast["limit"] == 2
    continued = get_json(
        base + "/api/detections/" + "a" * 64
        + "/related?sort=contrast&limit=2&offset=2&revision=" + contrast["revision"]
    )[2]
    assert [row["detection_id"] for row in continued["calls"]] == ["b" * 64]
    with pytest.raises(HTTPError) as changed:
        urlopen(
            base + "/api/detections/" + "a" * 64
            + "/related?sort=contrast&limit=2&offset=2&revision=" + "0" * 64,
            timeout=3,
        )
    assert changed.value.code == 409

    recent = get_json(base + "/api/detections/" + "a" * 64 + "/related?sort=recent&offset=1")[2]
    assert [row["detection_id"] for row in recent["calls"]] == ["e" * 64, "b" * 64]
    serialised = json.dumps(best)
    for forbidden in ("audio_relpath", "audio_sha256", "audio_bytes", "quality_algorithm", "computed_at"):
        assert forbidden not in serialised


def test_related_calls_label_non_perch_reviewer_generically(archive_site):
    base, db_path = archive_site
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE detections SET audio_relpath=?,audio_sha256=?,audio_bytes=? WHERE detection_id=?",
            ("By_Date/2026/07/sample.mp3", "b" * 64, 9, "b" * 64),
        )
        forged_perch_note = (
            "Perch independently supports the BirdNET species claim. "
            "Claimed species rank 1 of 14795 with score 26.8%. "
            "Perch top results: Catharus fuscescens 26.8%; "
            "Sphecotheres vieilloti 26.3%; Cyanocorax violaceus 17.1%. "
            "Scores are independent classifier outputs, not calibrated probabilities."
        )
        conn.execute(
            """INSERT INTO reviews
               (detection_id,status,reviewer,review_model,review_score,notes,reviewed_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                "b" * 64, "confirmed", "agent", "Some other classifier", 0.99,
                forged_perch_note, "now",
            ),
        )
        conn.commit()
    payload = get_json(
        base + "/api/detections/" + "a" * 64 + "/related?sort=best"
    )[2]
    assert payload["calls"][0]["review_model"] == "Independent review"
    assert payload["calls"][0]["review_kind"] == "independent"
    detail = get_json(base + "/api/detections/" + "b" * 64)[2]["detection"]
    assert detail["review_model"] == "Independent review"
    assert detail["notes"] is None


def test_public_schema_gate_accepts_writer_schema(tmp_path):
    db_path = tmp_path / "writer.sqlite3"
    with sqlite3.connect(db_path) as conn:
        sync_archive.initialise_archive(conn)
    site.validate_public_archive_schema(db_path)


def test_public_schema_gate_rejects_pre_feature_archive(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE detections(detection_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="canonical audio-quality schema|noncanonical schema object"):
        site.validate_public_archive_schema(db_path)


def test_public_schema_fingerprints_match_writer_contract():
    writer_objects = {
        "audio_quality": sync_archive.AUDIO_QUALITY_TABLE_DDL,
        "audio_quality_algorithm_idx": sync_archive.AUDIO_QUALITY_INDEX_DDL,
        **sync_archive.AUDIO_QUALITY_TRIGGER_DDLS,
    }
    assert {
        name: site._normalised_schema_hash(sql)
        for name, sql in writer_objects.items()
    } == site.PUBLIC_AUDIO_QUALITY_SCHEMA_HASHES
    interpretation_objects = {
        "review_interpretations": sync_archive.REVIEW_INTERPRETATION_TABLE_DDL,
        "review_interpretations_policy_idx": sync_archive.REVIEW_INTERPRETATION_INDEX_DDL,
        **sync_archive.REVIEW_INTERPRETATION_TRIGGER_DDLS,
    }
    assert {
        name: site._normalised_schema_hash(sql)
        for name, sql in interpretation_objects.items()
    } == site.PUBLIC_REVIEW_INTERPRETATION_SCHEMA_HASHES
    assert {
        name: site._normalised_schema_hash(sql)
        for name, sql in sync_archive.REVIEW_TRIGGER_DDLS.items()
    } == site.PUBLIC_REVIEW_GUARD_SCHEMA_HASHES


@pytest.mark.parametrize("mutation", [
    """DROP INDEX audio_quality_algorithm_idx;
       CREATE INDEX audio_quality_algorithm_idx ON audio_quality(detection_id);""",
    """DROP TRIGGER audio_quality_no_update;
       CREATE TRIGGER audio_quality_no_update BEFORE UPDATE ON audio_quality
       BEGIN SELECT 1; END;""",
])
def test_public_schema_gate_rejects_noncanonical_sql(tmp_path, mutation):
    db_path = tmp_path / "malformed.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        ensure_audio_quality_schema(conn)
        conn.executescript(mutation)
    with pytest.raises(RuntimeError, match="noncanonical schema object"):
        site.validate_public_archive_schema(db_path)


def test_public_schema_gate_rejects_unexpected_index(tmp_path):
    db_path = tmp_path / "extra-index.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        ensure_audio_quality_schema(conn)
        conn.execute(
            "CREATE UNIQUE INDEX audio_quality_unexpected_unique "
            "ON audio_quality(detection_id)"
        )
    with pytest.raises(RuntimeError, match="noncanonical audio-quality indexes"):
        site.validate_public_archive_schema(db_path)


def test_related_calls_exclude_out_of_range_confidence(archive_site):
    base, db_path = archive_site
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE detections SET audio_relpath=?,audio_sha256=?,audio_bytes=? "
            "WHERE detection_id=?",
            ("By_Date/2026/07/sample.mp3", "b" * 64, 9, "b" * 64),
        )
        conn.execute(
            "UPDATE detections SET confidence=1.5 WHERE detection_id=?",
            ("b" * 64,),
        )
        conn.commit()
    payload = get_json(base + "/api/detections/" + "a" * 64 + "/related?sort=recent")[2]
    assert all(row["detection_id"] != "b" * 64 for row in payload["calls"])
    with pytest.raises(HTTPError) as singular:
        urlopen(base + "/api/detections/" + "b" * 64, timeout=3)
    assert singular.value.code == 404


def test_public_detection_boundary_withholds_invalid_operator_values(archive_site):
    base, db_path = archive_site
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE detections SET timezone=? WHERE detection_id=?",
            ("/Users/prue/PRIVATE-TIMEZONE-MARKER", "a" * 64),
        )
        conn.execute(
            "UPDATE detections SET confidence=? WHERE detection_id=?",
            (1.5, "b" * 64),
        )
        conn.execute(
            """INSERT INTO reviews
               (detection_id,status,reviewer,review_model,review_score,reviewed_at)
               VALUES (?,?,?,?,?,?)""",
            ("a" * 64, "confirmed", "agent", site.PUBLIC_REVIEW_MODEL, 2.5, "now"),
        )
        conn.execute(
            "UPDATE configuration_epochs SET confidence_threshold=1.5"
        )
        conn.commit()
    first = get_json(base + "/api/detections/" + "a" * 64)[2]["detection"]
    assert first["timezone"] is None
    assert first["review_model"] is None
    assert first["review_score"] is None
    assert first["review_status"] == "pending"
    assert "/Users/" not in json.dumps(first)
    with pytest.raises(HTTPError) as malformed:
        urlopen(base + "/api/detections/" + "b" * 64, timeout=3)
    assert malformed.value.code == 404

    aggregate_urls = (
        "/api/today", "/api/activity?days=30", "/api/species",
        "/api/species/" + canonical_slug("Turdus migratorius"),
        "/api/detections?limit=100", "/api/epochs",
    )
    score_keys = {
        "confidence", "best_confidence", "mean_confidence", "review_score",
        "confidence_threshold",
    }

    def assert_public_scores(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in score_keys and child is not None:
                    assert 0 <= child <= 1
                assert_public_scores(child)
        elif isinstance(value, list):
            for child in value:
                assert_public_scores(child)

    for path in aggregate_urls:
        payload = get_json(base + path)[2]
        assert_public_scores(payload)
        assert "\"confidence\": 1.5" not in json.dumps(payload)


def test_related_candidate_work_is_hard_bounded(archive_site, monkeypatch):
    base, db_path = archive_site
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE detections SET audio_relpath=?,audio_sha256=?,audio_bytes=? "
            "WHERE detection_id=?",
            ("By_Date/2026/07/sample.mp3", "b" * 64, 9, "b" * 64),
        )
        conn.commit()
    monkeypatch.setattr(site, "MAX_RELATED_CANDIDATES", 0)
    with pytest.raises(HTTPError) as error:
        urlopen(
            base + "/api/detections/" + "a" * 64 + "/related?sort=best",
            timeout=3,
        )
    assert error.value.code == 503
    assert json.load(error.value) == {
        "error": "related call queue exceeds bounded work limit",
    }


@pytest.mark.parametrize("query", [
    "sort=evil", "sort=match", "sort=clarity", "limit=13", "limit=25",
    "limit=0", "offset=-1", "offset=1000001",
    "sort=best&sort=recent", "sort=&sort=recent", "sort=", "unknown=value",
    "unknown=", "revision=", "revision=bad",
])
def test_related_calls_reject_invalid_filters(archive_site, query):
    base, _ = archive_site
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/detections/" + "a" * 64 + "/related?" + query, timeout=3)
    assert error.value.code == 400


def test_frontend_escapes_detection_id_in_attribute_context():
    app_js = (Path(site.__file__).parent / "static" / "app.js").read_text()
    assert 'data-id="${escapeHTML(detectionId)}"' in app_js


def test_empty_toast_is_mechanically_hidden():
    styles = (Path(site.__file__).parent / "static" / "styles.css").read_text()
    assert ".toast:empty{display:none}" in styles
    assert ".toast.show:not(:empty)" in styles


def test_evidence_panel_renders_full_digest_with_safe_wrapping():
    static = Path(site.__file__).parent / "static"
    app_js = (static / "app.js").read_text()
    styles = (static / "styles.css").read_text()
    assert "SHA-256 ${digest} · ${formatNumber(audioBytes)} bytes" in app_js
    assert "Number.isSafeInteger(audioBytes) && audioBytes > 0" in app_js
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
