import sqlite3

import pytest

from avian.archive import corroboration, sync_archive


OSPREY_NOTE = (
    "Perch is inconclusive or disagrees; human review remains appropriate. "
    "Claimed species rank 2 of 14795 with score 7.8%. "
    "Perch top results: Icterus galbula 9.9%; Pandion haliaetus 7.8%; "
    "Pyrrhula pyrrhula 5.1%. Scores are independent classifier outputs, "
    "not calibrated probabilities."
)
PURPLE_MARTIN_NOTE = (
    "Perch is inconclusive or disagrees; human review remains appropriate. "
    "Claimed species rank 8 of 14795 with score 2.1%. "
    "Perch top results: Catharus fuscescens 47.5%; Calcarius lapponicus 12.3%; "
    "Plectrophenax nivalis 9.5%. Scores are independent classifier outputs, "
    "not calibrated probabilities."
)
WOOD_THRUSH_NOTE = (
    "Perch independently supports the BirdNET species claim. "
    "Claimed species rank 1 of 14795 with score 67.6%. "
    "Perch top results: Hylocichla mustelina 67.6%; Vermivora cyanoptera 6.0%; "
    "Turdus migratorius 5.3%. Scores are independent classifier outputs, "
    "not calibrated probabilities."
)


def test_versioned_policy_separates_support_ambiguity_and_conflict():
    assert corroboration.classify_outcome(0.25, 1, 0.25) == "corroborated"
    assert corroboration.classify_outcome(0.24, 1, 0.24) == "uncorroborated"
    assert corroboration.classify_outcome(0.078, 2, 0.099) == "uncorroborated"
    assert corroboration.classify_outcome(0.021, 8, 0.475) == "model_conflict"
    assert corroboration.classify_outcome(0.099, 4, 0.249) == "uncorroborated"
    assert corroboration.classify_outcome(0.10, 4, 0.80) == "uncorroborated"


@pytest.mark.parametrize(
    ("note", "review_score", "expected"),
    [
        (WOOD_THRUSH_NOTE, 0.6761059165, ("corroborated", 1, 14795, "Hylocichla mustelina", 0.676)),
        (OSPREY_NOTE, 0.0784806982, ("uncorroborated", 2, 14795, "Icterus galbula", 0.099)),
        (PURPLE_MARTIN_NOTE, 0.0212280136, ("model_conflict", 8, 14795, "Catharus fuscescens", 0.475)),
    ],
)
def test_historical_perch_notes_reclassify_without_rerunning_inference(note, review_score, expected):
    parsed = corroboration.interpret_review(review_score, note)
    assert (
        parsed.outcome,
        parsed.claim_rank,
        parsed.label_count,
        parsed.top_label,
    ) == expected[:4]
    assert parsed.top_score == pytest.approx(expected[4])
    assert parsed.claim_score == pytest.approx(review_score)
    assert parsed.policy_version == corroboration.POLICY_VERSION


@pytest.mark.parametrize(
    "note",
    [
        "",
        "Claimed species rank 2 with score 7.8%",
        OSPREY_NOTE.replace("14795", "0"),
        OSPREY_NOTE.replace("Icterus galbula", "../../private"),
        OSPREY_NOTE.replace("9.9%", "999.9%"),
    ],
)
def test_historical_interpretation_fails_closed_on_malformed_notes(note):
    with pytest.raises(ValueError):
        corroboration.interpret_review(0.078, note)


def _insert_detection_and_review(conn, detection_id, note, score):
    conn.execute(
        """INSERT INTO detections (
             detection_id,date,time,observed_at_local,timezone,scientific_name,
             common_name,confidence,file_name,source_model,source_host,ingested_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            detection_id, "2026-07-22", "20:34:15", "2026-07-22T20:34:15",
            "America/New_York", "Pandion haliaetus", "Osprey", 0.8078,
            "clip.mp3", "BirdNET", "test", "2026-07-23T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
        (
            detection_id, "uncertain", "automated independent model",
            corroboration.PERCH_MODEL_NAME, score, note, "2026-07-23T00:01:00+00:00",
        ),
    )


def test_backfill_is_append_only_idempotent_and_preserves_original_reviews():
    conn = sqlite3.connect(":memory:")
    sync_archive.initialise_archive(conn)
    detection_id = "a" * 64
    _insert_detection_and_review(conn, detection_id, OSPREY_NOTE, 0.0784806982)
    original_review = conn.execute("SELECT * FROM reviews").fetchone()

    assert corroboration.backfill_interpretations(
        conn, interpreted_at="2026-07-24T12:00:00+00:00"
    ) == 1
    assert corroboration.backfill_interpretations(
        conn, interpreted_at="2026-07-24T12:01:00+00:00"
    ) == 0
    assert conn.execute("SELECT * FROM reviews").fetchone() == original_review
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE reviews SET review_score=0.99 WHERE detection_id=?",
            (detection_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM reviews WHERE detection_id=?", (detection_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            """INSERT OR REPLACE INTO reviews
               SELECT detection_id,status,reviewer,review_model,0.99,notes,reviewed_at
               FROM reviews WHERE detection_id=?""",
            (detection_id,),
        )
    row = conn.execute(
        """SELECT policy_version,outcome,claim_score,claim_rank,label_count,
                  top_label,top_score,interpreted_at
           FROM review_interpretations"""
    ).fetchone()
    assert row == (
        corroboration.POLICY_VERSION, "uncorroborated", 0.0784806982, 2,
        14795, "Icterus galbula", 0.099, "2026-07-24T12:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE review_interpretations SET outcome='corroborated' "
            "WHERE detection_id=?",
            (detection_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM review_interpretations WHERE detection_id=?",
            (detection_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            """INSERT OR REPLACE INTO review_interpretations
               SELECT detection_id,policy_version,'corroborated',claim_score,
                      claim_rank,label_count,top_label,top_score,interpreted_at
               FROM review_interpretations WHERE detection_id=?""",
            (detection_id,),
        )
    conn.close()


def test_new_review_result_and_historical_backfill_use_identical_policy():
    result = {
        "claim_score": 0.0784806982,
        "claim_rank": 2,
        "label_count": 14795,
        "top": [
            ("Icterus galbula", 0.099),
            ("Pandion haliaetus", 0.0784806982),
            ("Pyrrhula pyrrhula", 0.051),
        ],
    }
    fresh = corroboration.interpret_result(result)
    historical = corroboration.interpret_review(0.0784806982, OSPREY_NOTE)
    assert fresh.outcome == historical.outcome == "uncorroborated"
    assert fresh.claim_rank == historical.claim_rank == 2
    assert fresh.top_label == historical.top_label == "Icterus galbula"
