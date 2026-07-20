import hashlib
import importlib.util
import io
from pathlib import Path
import sqlite3

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "avian" / "archive" / "audio_quality.py"
spec = importlib.util.spec_from_file_location("audio_quality", MODULE_PATH)
assert spec and spec.loader
audio_quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio_quality)


def make_archive(db_path: Path, audio_root: Path, count: int = 1) -> None:
    audio_root.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE detections (
              detection_id TEXT PRIMARY KEY,
              common_name TEXT,
              observed_at_local TEXT,
              audio_relpath TEXT,
              audio_sha256 TEXT,
              audio_bytes INTEGER
            );
            """
        )
        for index in range(count):
            payload = f"immutable audio {index}".encode()
            name = f"clip-{index}.mp3"
            (audio_root / name).write_bytes(payload)
            conn.execute(
                "INSERT INTO detections VALUES (?,?,?,?,?,?)",
                (
                    format(index + 1, "064x"), "Example Bird",
                    f"2026-07-20T07:00:0{index}", name,
                    hashlib.sha256(payload).hexdigest(), len(payload),
                ),
            )


def result(version=None, contrast=18.5):
    return {
        "algorithm_version": version or audio_quality.QUALITY_ALGORITHM,
        "sample_rate": 32000,
        "duration_seconds": 15.0,
        "noise_floor_dbfs": -48.2,
        "signal_level_dbfs": -29.7,
        "signal_contrast_db": contrast,
        "clipping_fraction": 0.0001,
    }


def test_archived_audio_bytes_require_actual_bounded_integer():
    assert audio_quality.validated_archived_audio_bytes(9, "d") == 9
    invalid = (9.5, float("nan"), float("inf"), "9", True, None)
    for value in invalid:
        with pytest.raises(RuntimeError, match="invalid archived audio size"):
            audio_quality.validated_archived_audio_bytes(value, "d")
    for value in (-1, 0, audio_quality.MAX_AUDIO_BYTES + 1):
        with pytest.raises(RuntimeError, match="exceeds analysis limit"):
            audio_quality.validated_archived_audio_bytes(value, "d")


def test_frame_level_metric_has_pinned_semantics():
    np = pytest.importorskip("numpy")
    frame = audio_quality.FRAME_LENGTH

    silence = audio_quality.measure_samples(np.zeros(frame * 3), 32000)
    assert silence["noise_floor_dbfs"] == -120.0
    assert silence["signal_level_dbfs"] == -120.0
    assert silence["signal_contrast_db"] == 0.0
    assert silence["clipping_fraction"] == 0.0

    tone = audio_quality.measure_samples(np.full(frame * 3, 0.5), 32000)
    assert tone["noise_floor_dbfs"] == pytest.approx(-6.0206, abs=0.001)
    assert tone["signal_level_dbfs"] == pytest.approx(-6.0206, abs=0.001)
    assert tone["signal_contrast_db"] == pytest.approx(0.0, abs=0.001)

    stepped = np.concatenate((np.full(frame * 5, 0.01), np.full(frame * 5, 0.5)))
    contrast = audio_quality.measure_samples(stepped, 32000)
    assert contrast["noise_floor_dbfs"] == pytest.approx(-40.0, abs=0.01)
    assert contrast["signal_level_dbfs"] == pytest.approx(-6.0206, abs=0.01)
    assert contrast["signal_contrast_db"] == pytest.approx(33.9794, abs=0.02)


def test_frame_level_metric_rejects_short_and_non_finite_audio():
    np = pytest.importorskip("numpy")
    with pytest.raises(RuntimeError, match="too short"):
        audio_quality.measure_samples(np.zeros(audio_quality.FRAME_LENGTH - 1), 32000)
    corrupt = np.zeros(audio_quality.FRAME_LENGTH)
    corrupt[0] = np.nan
    with pytest.raises(RuntimeError, match="non-finite"):
        audio_quality.measure_samples(corrupt, 32000)


def test_decode_metadata_is_bounded_before_allocation():
    audio_quality.validate_decode_metadata(48_000 * 6, 48_000, 2)
    with pytest.raises(RuntimeError, match="metadata"):
        audio_quality.validate_decode_metadata(32_000, 1_000, 1)
    with pytest.raises(RuntimeError, match="channel"):
        audio_quality.validate_decode_metadata(32_000, 32_000, 9)
    with pytest.raises(RuntimeError, match="duration"):
        audio_quality.validate_decode_metadata(32_000 * 61, 32_000, 1)
    with pytest.raises(RuntimeError, match="allocation"):
        audio_quality.validate_decode_metadata(192_000 * 60, 192_000, 2)


def test_native_decode_downmix_and_resample_are_deterministic():
    np = pytest.importorskip("numpy")
    soundfile = pytest.importorskip("soundfile")
    native_rate = 48_000
    seconds = 0.2
    samples = int(native_rate * seconds)
    wave = np.concatenate((
        np.full(samples // 2, 0.02, dtype=np.float32),
        np.full(samples - samples // 2, 0.4, dtype=np.float32),
    ))

    def wav_bytes(values):
        buffer = io.BytesIO()
        soundfile.write(buffer, values, native_rate, format="WAV", subtype="FLOAT")
        return buffer.getvalue()

    mono = audio_quality.measure_audio(wav_bytes(wave))
    stereo = audio_quality.measure_audio(wav_bytes(np.column_stack((wave, wave))))
    for key in (
        "duration_seconds", "noise_floor_dbfs", "signal_level_dbfs",
        "signal_contrast_db", "clipping_fraction",
    ):
        assert stereo[key] == pytest.approx(mono[key], abs=1e-6)
    assert mono["sample_rate"] == audio_quality.TARGET_SAMPLE_RATE
    assert mono["algorithm_version"] == "frame-level-percentiles-v1"


def test_near_full_scale_fraction_is_separate_from_level_clamping():
    np = pytest.importorskip("numpy")
    values = np.full(audio_quality.FRAME_LENGTH, 1.1, dtype=np.float32)
    measured = audio_quality.measure_samples(values, 32000)
    assert measured["clipping_fraction"] == 1.0
    assert measured["noise_floor_dbfs"] == 0.0
    assert measured["signal_level_dbfs"] == 0.0


def test_measurement_is_hash_checked_versioned_and_idempotent(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "archive.sqlite3", tmp_path / "audio"
    make_archive(db_path, audio_root)
    seen = []

    def measure(payload):
        seen.append(payload)
        return result()

    monkeypatch.setattr(audio_quality, "measure_audio", measure)
    assert audio_quality.measure_archive(db_path, audio_root, None, 50, False) == 1
    assert seen == [b"immutable audio 0"]
    assert audio_quality.measure_archive(db_path, audio_root, None, 50, False) == 0
    assert seen == [b"immutable audio 0"]

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT algorithm_version,sample_rate,duration_seconds,
                      noise_floor_dbfs,signal_level_dbfs,signal_contrast_db,
                      clipping_fraction
               FROM audio_quality"""
        ).fetchone()
    assert row == (
        audio_quality.QUALITY_ALGORITHM, 32000, 15.0,
        -48.2, -29.7, 18.5, 0.0001,
    )

    next_version = "frame-level-percentiles-v2"
    monkeypatch.setattr(audio_quality, "QUALITY_ALGORITHM", next_version)
    monkeypatch.setattr(audio_quality, "measure_audio", lambda _: result(next_version, 22.0))
    assert audio_quality.measure_archive(db_path, audio_root, None, 50, False) == 1
    with sqlite3.connect(db_path) as conn:
        versions = conn.execute(
            "SELECT algorithm_version,signal_contrast_db FROM audio_quality ORDER BY algorithm_version"
        ).fetchall()
    assert versions == [
        ("frame-level-percentiles-v1", 18.5),
        ("frame-level-percentiles-v2", 22.0),
    ]


def test_oversized_archive_row_is_rejected_before_read_or_decode(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "archive.sqlite3", tmp_path / "audio"
    make_archive(db_path, audio_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE detections SET audio_bytes=?",
            (audio_quality.MAX_AUDIO_BYTES + 1,),
        )
    called = False

    def measure(_payload):
        nonlocal called
        called = True
        return result()

    monkeypatch.setattr(audio_quality, "measure_audio", measure)
    with pytest.raises(RuntimeError, match="exceeds analysis limit"):
        audio_quality.measure_archive(db_path, audio_root, None, 50, False)
    assert called is False


def test_checksum_mismatch_inserts_no_measurement(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "archive.sqlite3", tmp_path / "audio"
    make_archive(db_path, audio_root)
    (audio_root / "clip-0.mp3").write_bytes(b"replacement same size"[:17])
    monkeypatch.setattr(audio_quality, "measure_audio", lambda _: result())
    with pytest.raises(RuntimeError, match="archived audio checksum mismatch"):
        audio_quality.measure_archive(db_path, audio_root, None, 50, False)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM audio_quality").fetchone()[0] == 0


def test_mid_batch_failure_inserts_zero_measurements(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "archive.sqlite3", tmp_path / "audio"
    make_archive(db_path, audio_root, count=2)
    calls = 0

    def measure(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("quality failed")
        return result()

    monkeypatch.setattr(audio_quality, "measure_audio", measure)
    with pytest.raises(RuntimeError, match="quality failed"):
        audio_quality.measure_archive(db_path, audio_root, None, 50, False)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM audio_quality").fetchone()[0] == 0


def test_path_escape_and_invalid_metric_fail_closed(tmp_path, monkeypatch):
    db_path, audio_root = tmp_path / "archive.sqlite3", tmp_path / "audio"
    make_archive(db_path, audio_root)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE detections SET audio_relpath='../outside.mp3',
                      audio_sha256=?,audio_bytes=?""",
            (hashlib.sha256(outside.read_bytes()).hexdigest(), outside.stat().st_size),
        )
    with pytest.raises(RuntimeError, match="escapes"):
        audio_quality.measure_archive(db_path, audio_root, None, 50, False)

    with sqlite3.connect(db_path) as conn:
        payload = (audio_root / "clip-0.mp3").read_bytes()
        conn.execute(
            """UPDATE detections SET audio_relpath='clip-0.mp3',
                      audio_sha256=?,audio_bytes=?""",
            (hashlib.sha256(payload).hexdigest(), len(payload)),
        )
    monkeypatch.setattr(audio_quality, "measure_audio", lambda _: result(contrast=999.0))
    with pytest.raises(sqlite3.IntegrityError):
        audio_quality.measure_archive(db_path, audio_root, None, 50, False)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM audio_quality").fetchone()[0] == 0
