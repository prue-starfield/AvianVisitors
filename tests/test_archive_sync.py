import importlib.util
import sqlite3
from pathlib import Path


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
