import json
import sqlite3

from avian.archive import corroboration, new_species_watch, sync_archive


def add_detection(
    conn, detection_id, scientific_name, common_name, confidence, observed_at,
):
    date, time = observed_at.split("T")
    conn.execute(
        """INSERT INTO detections
           (detection_id,date,time,observed_at_local,timezone,scientific_name,
            common_name,confidence,file_name,source_model,source_host,ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            detection_id, date, time, observed_at, "America/New_York",
            scientific_name, common_name, confidence, "clip.wav", "BirdNET",
            "test", "2026-07-24T12:00:00+00:00",
        ),
    )


def add_review(conn, detection_id, outcome=None, claim_score=0.8, rank=1,
               top_label="Turdus migratorius", top_score=0.8):
    conn.execute(
        "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
        (
            detection_id, "confirmed", "automated independent model",
            corroboration.PERCH_MODEL_NAME, claim_score, "review note",
            "2026-07-24T12:00:00+00:00",
        ),
    )
    if outcome:
        conn.execute(
            """INSERT INTO review_interpretations
               (detection_id,policy_version,outcome,claim_score,claim_rank,
                label_count,top_label,top_score,interpreted_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                detection_id, corroboration.POLICY_VERSION, outcome,
                claim_score, rank, 14795, top_label, top_score,
                "2026-07-24T12:00:00+00:00",
            ),
        )


def archive(tmp_path):
    path = tmp_path / "archive.sqlite3"
    with sqlite3.connect(path) as conn:
        sync_archive.initialise_archive(conn)
    return path


def test_species_is_ineligible_until_versioned_review_interpretation_exists(tmp_path):
    path = archive(tmp_path)
    with sqlite3.connect(path) as conn:
        add_detection(
            conn, "a" * 64, "Pandion haliaetus", "Osprey", 0.8078,
            "2026-07-23T20:34:15",
        )
        add_review(conn, "a" * 64, outcome=None, claim_score=0.0784806982)
        conn.commit()
    assert new_species_watch.load_species(path) == {}


def test_uncorroborated_and_model_conflict_candidates_are_explicit(tmp_path):
    path = archive(tmp_path)
    with sqlite3.connect(path) as conn:
        add_detection(
            conn, "a" * 64, "Pandion haliaetus", "Osprey", 0.8078,
            "2026-07-23T20:34:15",
        )
        add_review(
            conn, "a" * 64, "uncorroborated", 0.0784806982, 2,
            "Icterus galbula", 0.099,
        )
        add_detection(
            conn, "b" * 64, "Progne subis", "Purple Martin", 0.91,
            "2026-07-24T07:10:00",
        )
        add_review(
            conn, "b" * 64, "model_conflict", 0.021, 8,
            "Catharus fuscescens", 0.475,
        )
        conn.commit()

    species = new_species_watch.load_species(path)
    assert species["Pandion haliaetus"]["standing"] == "uncorroborated"
    assert species["Pandion haliaetus"]["review_rank"] == 2
    assert species["Pandion haliaetus"]["review_top_label"] == "Icterus galbula"
    assert species["Progne subis"]["standing"] == "model_conflict"
    message = new_species_watch.alert_message(
        sorted(species.items()), [],
    )
    assert "UNCORROBORATED" in message
    assert "MODEL CONFLICT" in message
    assert "rank #2 of 14,795" in message
    assert "model score—not probability" in message
    assert "not counted as a corroborated field-record species" in message


def test_one_corroborated_clip_promotes_species_but_counts_all_claims(tmp_path):
    path = archive(tmp_path)
    with sqlite3.connect(path) as conn:
        add_detection(
            conn, "a" * 64, "Turdus migratorius", "American Robin", 0.91,
            "2026-07-20T06:00:00",
        )
        add_review(
            conn, "a" * 64, "uncorroborated", 0.20, 1,
            "Turdus migratorius", 0.20,
        )
        add_detection(
            conn, "b" * 64, "Turdus migratorius", "American Robin", 0.93,
            "2026-07-21T06:00:00",
        )
        add_review(
            conn, "b" * 64, "corroborated", 0.70, 1,
            "Turdus migratorius", 0.70,
        )
        conn.commit()

    robin = new_species_watch.load_species(path)["Turdus migratorius"]
    assert robin["standing"] == "corroborated"
    assert robin["recognitions"] == 2
    assert robin["evidence_id"] == "b" * 64
    assert robin["first_heard"] == "2026-07-20T06:00:00"


def test_v1_state_migrates_without_realerting_historical_species(tmp_path, monkeypatch):
    path = archive(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "seen": {"Pandion haliaetus": {"common_name": "Osprey"}},
    }))
    with sqlite3.connect(path) as conn:
        add_detection(
            conn, "a" * 64, "Pandion haliaetus", "Osprey", 0.8078,
            "2026-07-23T20:34:15",
        )
        add_review(
            conn, "a" * 64, "uncorroborated", 0.0784806982, 2,
            "Icterus galbula", 0.099,
        )
        conn.commit()
    monkeypatch.setattr(new_species_watch, "prepare_artwork", lambda *args: None)

    assert new_species_watch.run(path, state_path) == ""
    state = json.loads(state_path.read_text())
    assert state["version"] == 2
    assert state["policy_version"] == corroboration.POLICY_VERSION
    assert state["seen"]["Pandion haliaetus"]["standing"] == "uncorroborated"
