import importlib.util
import sqlite3
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "avian" / "archive" / "sync_archive.py"
spec = importlib.util.spec_from_file_location("sync_archive", MODULE_PATH)
assert spec and spec.loader
sync_archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_archive)


def make_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE detections (
          Date DATE, Time TIME, Sci_Name VARCHAR(100) NOT NULL,
          Com_Name VARCHAR(100) NOT NULL, Confidence FLOAT,
          Lat FLOAT, Lon FLOAT, Cutoff FLOAT, Week INT, Sens FLOAT,
          Overlap FLOAT, File_Name VARCHAR(100) NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-07-14", "06:00:00", "Turdus migratorius", "American Robin",
             0.95, 41.0, -72.0, 0.7, 29, 1.25, 0.0,
             "American_Robin-95-2026-07-14-birdnet-06:00:00.mp3"),
            ("2026-07-15", "07:00:00", "Sayornis phoebe", "Eastern Phoebe",
             0.81, 41.0, -72.0, 0.7, 29, 1.25, 0.0,
             "Eastern_Phoebe-81-2026-07-15-birdnet-07:00:00.mp3"),
        ],
    )
    conn.commit()
    conn.close()


def test_import_is_idempotent_and_preserves_review(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    archive = tmp_path / "archive.db"
    make_source(source)

    assert sync_archive.import_snapshot(source, archive, "model-v1", "birdnet") == (2, 2)
    assert sync_archive.import_snapshot(source, archive, "model-v1", "birdnet") == (2, 0)

    conn = sqlite3.connect(archive)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM detections ORDER BY date"))
    assert len(rows) == 2
    assert rows[0]["observed_at_local"] == "2026-07-14T06:00:00"

    second_id = rows[1]["detection_id"]
    conn.execute(
        "INSERT INTO reviews(detection_id,status,reviewer,notes) VALUES (?,?,?,?)",
        (second_id, "confirmed", "human", "heard clearly"),
    )
    conn.commit()
    conn.close()

    assert sync_archive.import_snapshot(source, archive, "model-v1", "birdnet") == (2, 0)
    conn = sqlite3.connect(archive)
    assert conn.execute("SELECT status FROM reviews").fetchone()[0] == "confirmed"
    assert conn.execute("SELECT count(*) FROM daily_species").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM monthly_species").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='configuration_epochs'"
    ).fetchone()[0] == 1
    conn.close()


def test_audio_is_hashed_once_and_linked_by_filename(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    archive = tmp_path / "archive.db"
    make_source(source)
    sync_archive.import_snapshot(source, archive, "model-v1", "birdnet")

    audio_root = tmp_path / "audio" / "By_Date"
    robin = audio_root / "2026-07-14" / "American_Robin"
    phoebe = audio_root / "2026-07-15" / "Eastern_Phoebe"
    robin.mkdir(parents=True)
    phoebe.mkdir(parents=True)
    (robin / "American_Robin-95-2026-07-14-birdnet-06:00:00.mp3").write_bytes(b"robin-audio")
    (phoebe / "Eastern_Phoebe-81-2026-07-15-birdnet-07:00:00.mp3").write_bytes(b"phoebe-audio")

    assert sync_archive.index_audio(archive, audio_root) == 2
    assert sync_archive.index_audio(archive, audio_root) == 0

    conn = sqlite3.connect(archive)
    rows = list(conn.execute(
        "SELECT audio_relpath,audio_sha256,audio_bytes FROM detections ORDER BY date"
    ))
    assert rows[0][0].startswith("By_Date/2026-07-14/")
    assert len(rows[0][1]) == 64
    assert rows[0][2] == len(b"robin-audio")
    assert rows[1][2] == len(b"phoebe-audio")
    conn.close()


def test_archive_mirror_is_atomic_valid_and_checksummed(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    archive = tmp_path / "archive.db"
    mirror = tmp_path / "second-device" / "detections.sqlite3"
    make_source(source)
    sync_archive.import_snapshot(source, archive, "model-v1", "birdnet")

    digest = sync_archive.mirror_archive_db(archive, mirror)
    assert mirror.exists()
    assert not mirror.with_name(mirror.name + ".partial").exists()
    assert mirror.with_suffix(".sqlite3.sha256").read_text() == (
        f"{digest}  detections.sqlite3\n"
    )
    conn = sqlite3.connect(mirror)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM detections").fetchone()[0] == 2
    conn.close()


def test_web_audio_cache_is_checksum_verified_bounded_and_newest_first(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    archive = tmp_path / "archive.db"
    canonical = tmp_path / "canonical-audio"
    audio_root = canonical / "By_Date"
    cache = tmp_path / "web-cache"
    make_source(source)
    sync_archive.import_snapshot(source, archive, "model-v1", "birdnet")

    robin = audio_root / "2026-07-14" / "American_Robin"
    phoebe = audio_root / "2026-07-15" / "Eastern_Phoebe"
    robin.mkdir(parents=True)
    phoebe.mkdir(parents=True)
    robin_bytes = b"robin-audio"
    phoebe_bytes = b"phoebe-audio"
    (robin / "American_Robin-95-2026-07-14-birdnet-06:00:00.mp3").write_bytes(robin_bytes)
    (phoebe / "Eastern_Phoebe-81-2026-07-15-birdnet-07:00:00.mp3").write_bytes(phoebe_bytes)
    assert sync_archive.index_audio(archive, audio_root) == 2

    copied, selected_bytes = sync_archive.refresh_web_audio_cache(
        archive, canonical, cache, len(robin_bytes) + len(phoebe_bytes)
    )
    assert copied == 2
    assert selected_bytes == len(robin_bytes) + len(phoebe_bytes)
    assert len(list(cache.rglob("*.mp3"))) == 2

    newest = next(cache.rglob("Eastern_Phoebe*.mp3"))
    newest.write_bytes(b"x" * len(phoebe_bytes))
    (cache / "stale.partial").write_bytes(b"stale")
    (cache / "rogue.bin").write_bytes(b"rogue")

    copied, selected_bytes = sync_archive.refresh_web_audio_cache(
        archive, canonical, cache, len(phoebe_bytes)
    )
    assert copied == 1
    assert selected_bytes == len(phoebe_bytes)
    cached = list(cache.rglob("*.mp3"))
    assert len(cached) == 1
    assert cached[0].name.startswith("Eastern_Phoebe")
    assert cached[0].read_bytes() == phoebe_bytes
    assert not (cache / "stale.partial").exists()
    assert not (cache / "rogue.bin").exists()


def test_web_audio_cache_rejects_path_escape_and_dangerous_root(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    archive = tmp_path / "archive.db"
    canonical = tmp_path / "canonical-audio"
    make_source(source)
    sync_archive.import_snapshot(source, archive, "model-v1", "birdnet")
    with sqlite3.connect(archive) as conn:
        conn.execute(
            """UPDATE detections SET audio_relpath='../../escape.mp3',
               audio_sha256=?,audio_bytes=9
               WHERE detection_id=(SELECT detection_id FROM detections LIMIT 1)""",
            ("f" * 64,),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="invalid archive audio path"):
        sync_archive.refresh_web_audio_cache(
            archive, canonical, tmp_path / "web-cache", 1024
        )
    with pytest.raises(RuntimeError, match="unsafe web cache root"):
        sync_archive.refresh_web_audio_cache(archive, canonical, tmp_path, 1024)


def test_default_known_hosts_path_is_durable_user_state() -> None:
    args = sync_archive.parse_args([])
    expected = Path.home() / ".ssh/birdnet_known_hosts"
    assert args.known_hosts == expected
    assert not str(args.known_hosts).startswith("/tmp/")


def test_missing_known_hosts_fails_before_sync(tmp_path: Path) -> None:
    missing = tmp_path / "missing-known-hosts"
    dest = tmp_path / "archive"
    with pytest.raises(RuntimeError, match="SSH known-hosts trust file is missing"):
        sync_archive.main([
            "--dest", str(dest),
            "--known-hosts", str(missing),
        ])
    assert not dest.exists()
    assert not (dest / ".sync.lock").exists()


def test_runtime_paths_expand_user_known_hosts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    trust = tmp_path / ".ssh" / "birdnet_known_hosts"
    trust.parent.mkdir()
    trust.write_text("verified-host-key\n", encoding="utf-8")
    args = sync_archive.parse_args([
        "--dest", str(tmp_path / "archive"),
        "--known-hosts", "~/.ssh/birdnet_known_hosts",
    ])
    resolved = sync_archive.resolve_runtime_paths(args)
    assert resolved.known_hosts == trust.resolve()
    assert resolved.dest == (tmp_path / "archive").resolve()


def test_sync_run_status_is_updated_not_duplicated(tmp_path: Path) -> None:
    archive = tmp_path / "archive.sqlite3"
    started = "2026-07-15T12:00:00+00:00"
    sync_archive.record_run(archive, started, "running")
    sync_archive.record_run(
        archive, started, "error", source_rows=4, inserted=2,
        indexed=1, error="cache failed",
    )
    conn = sqlite3.connect(archive)
    try:
        rows = conn.execute(
            "SELECT status,source_rows,inserted_rows,error,completed_at FROM sync_runs"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][:4] == ("error", 4, 2, "cache failed")
    assert rows[0][4] is not None
