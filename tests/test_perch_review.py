import hashlib
import sqlite3
import sys
import types

import pytest

from avian.archive import review_perch


def test_conservative_verdict_policy():
    assert review_perch.classify_verdict(0.25, 1, 0.25) == "confirmed"
    assert review_perch.classify_verdict(0.24, 1, 0.24) == "uncertain"
    assert review_perch.classify_verdict(0.09, 4, 0.75) == "rejected"
    assert review_perch.classify_verdict(0.11, 20, 0.95) == "uncertain"
    assert review_perch.classify_verdict(0.70, 2, 0.80) == "uncertain"


def test_safe_audio_path_rejects_escape(tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    clip = root / "clip.mp3"
    clip.write_bytes(b"audio")
    assert review_perch.safe_audio_path(root, "clip.mp3") == clip
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    with pytest.raises(RuntimeError, match="escapes"):
        review_perch.safe_audio_path(root, "../outside.mp3")


def test_model_assets_are_checksum_verified(tmp_path):
    model = tmp_path / "perch_v2.onnx"
    labels = tmp_path / "labels.csv"
    model.write_bytes(b"model")
    labels.write_text("taxonomy\nBirdus example\n", encoding="utf-8")
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (model, labels)
    )
    (tmp_path / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    assert review_perch.verify_model_assets(tmp_path) == (model, labels)
    model.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        review_perch.verify_model_assets(tmp_path)


@pytest.mark.parametrize("manifest", ["not-a-checksum labels.csv\n", "a" * 64 + "\n"])
def test_model_asset_manifest_rejects_malformed_lines(tmp_path, manifest):
    (tmp_path / "perch_v2.onnx").write_bytes(b"model")
    (tmp_path / "labels.csv").write_bytes(b"labels")
    (tmp_path / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    with pytest.raises(RuntimeError, match="Malformed Perch checksum manifest"):
        review_perch.verify_model_assets(tmp_path)


def test_review_archive_is_hash_checked_insert_only_and_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "detections.sqlite3"
    audio_root = tmp_path / "audio"
    clip = audio_root / "By_Date" / "clip.mp3"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"exact preserved bytes")
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    detection_id = "a" * 64
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE detections (
              detection_id TEXT PRIMARY KEY, scientific_name TEXT, common_name TEXT,
              confidence REAL, observed_at_local TEXT, audio_relpath TEXT,
              audio_sha256 TEXT, audio_bytes INTEGER
            );
            CREATE TABLE reviews (
              detection_id TEXT PRIMARY KEY,
              status TEXT CHECK(status IN ('confirmed','rejected','uncertain')),
              reviewer TEXT,
              review_model TEXT, review_score REAL, notes TEXT, reviewed_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?)",
            (
                detection_id, "Birdus example", "Example Bird", 0.91,
                "2026-07-16T12:00:00", "By_Date/clip.mp3", digest, clip.stat().st_size,
            ),
        )

    monkeypatch.setattr(review_perch, "load_model", lambda _: (object(), object()))
    monkeypatch.setattr(
        review_perch,
        "infer_review",
        lambda *args: {
            "status": "confirmed", "claim_score": 0.8, "claim_rank": 1,
            "top": [("Birdus example", 0.8)], "notes": "independent support",
        },
    )
    assert review_perch.review_archive(
        db_path, audio_root, tmp_path / "assets", None, 25, False,
    ) == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status,review_model,review_score,notes FROM reviews"
        ).fetchone()
    assert row == (
        "confirmed", review_perch.MODEL_NAME, 0.8, "independent support",
    )

    monkeypatch.setattr(
        review_perch, "load_model",
        lambda _: (_ for _ in ()).throw(AssertionError("model reloaded")),
    )
    assert review_perch.review_archive(
        db_path, audio_root, tmp_path / "assets", None, 25, False,
    ) == 0


def _review_db(db_path, audio_root, count=1):
    audio_root.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE detections (
              detection_id TEXT PRIMARY KEY, scientific_name TEXT, common_name TEXT,
              confidence REAL, observed_at_local TEXT, audio_relpath TEXT,
              audio_sha256 TEXT, audio_bytes INTEGER
            );
            CREATE TABLE reviews (
              detection_id TEXT PRIMARY KEY,
              status TEXT CHECK(status IN ('confirmed','rejected','uncertain')),
              reviewer TEXT,
              review_model TEXT, review_score REAL, notes TEXT, reviewed_at TEXT
            );
            """
        )
        for index in range(count):
            payload = f"immutable clip {index}".encode()
            name = f"clip-{index}.mp3"
            (audio_root / name).write_bytes(payload)
            conn.execute(
                "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?)",
                (
                    format(index + 1, "064x"), "Birdus example", "Example Bird", .9,
                    f"2026-07-16T12:00:0{index}", name,
                    hashlib.sha256(payload).hexdigest(), len(payload),
                ),
            )


def _result():
    return {
        "status": "confirmed", "claim_score": .8, "claim_rank": 1,
        "top": [("Birdus example", .8)], "notes": "support",
    }


def test_review_decodes_the_same_bytes_that_were_verified(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "db.sqlite3", tmp_path / "audio"
    _review_db(db_path, audio_root)
    original = (audio_root / "clip-0.mp3").read_bytes()
    monkeypatch.setattr(review_perch, "load_model", lambda _: (object(), object()))

    def infer(_model, _classes, audio_bytes, _scientific_name):
        (audio_root / "clip-0.mp3").write_bytes(b"replacement")
        assert audio_bytes == original
        return _result()

    monkeypatch.setattr(review_perch, "infer_review", infer)
    assert review_perch.review_archive(db_path, audio_root, tmp_path, None, 25, False) == 1


def test_mid_batch_inference_failure_inserts_zero_reviews(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "db.sqlite3", tmp_path / "audio"
    _review_db(db_path, audio_root, count=2)
    monkeypatch.setattr(review_perch, "load_model", lambda _: (object(), object()))
    calls = 0

    def infer(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("inference failed")
        return _result()

    monkeypatch.setattr(review_perch, "infer_review", infer)
    with pytest.raises(RuntimeError, match="inference failed"):
        review_perch.review_archive(db_path, audio_root, tmp_path, None, 25, False)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_batch_constraint_failure_rolls_back_every_review(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "db.sqlite3", tmp_path / "audio"
    _review_db(db_path, audio_root, count=2)
    monkeypatch.setattr(review_perch, "load_model", lambda _: (object(), object()))
    calls = 0

    def infer(*_args):
        nonlocal calls
        calls += 1
        result = _result()
        if calls == 2:
            result["status"] = "invalid"
        return result

    monkeypatch.setattr(review_perch, "infer_review", infer)
    with pytest.raises(sqlite3.IntegrityError):
        review_perch.review_archive(db_path, audio_root, tmp_path, None, 25, False)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def test_no_pending_rows_still_refreshes_mirror(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "db.sqlite3", tmp_path / "audio"
    _review_db(db_path, audio_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
            ("1".zfill(64), "confirmed", "reviewer", "model", .8, "notes", "now"),
        )
    mirror = tmp_path / "mirror.sqlite3"
    calls = []
    monkeypatch.setitem(
        sys.modules, "sync_archive",
        types.SimpleNamespace(mirror_archive_db=lambda source, target: calls.append((source, target))),
    )
    assert review_perch.review_archive(db_path, audio_root, tmp_path, mirror, 25, False) == 0
    assert calls == [(db_path, mirror)]
