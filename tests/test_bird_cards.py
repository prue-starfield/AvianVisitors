import hashlib
import json
from pathlib import Path
import re

import pytest

from avian.archive.site import server as site


def source(source_id="cornell-robin"):
    return {
        "id": source_id,
        "publisher": "Cornell Lab of Ornithology",
        "title": "American Robin Life History",
        "url": "https://www.allaboutbirds.org/guide/American_Robin/lifehistory",
        "accessed_at": "2026-07-25",
    }


def robin_facts():
    citation = ["cornell-robin"]
    return {
        "research_status": "verified",
        "habitat": {"value": "Open woodland, gardens, and lawns.", "sources": citation},
        "diet": {"value": "Invertebrates and fruit.", "sources": citation},
        "wingspan_cm": {"min": 31.0, "max": 40.0, "sources": citation},
        "length_cm": {"min": 20.0, "max": 28.0, "sources": citation},
        "nest": {"type": "cup", "value": "Mud-lined cup on a firm support.", "sources": citation},
        "clutch_size": {"min": 3, "max": 5, "sources": citation},
        "migration": {"category": "partial", "value": "Resident or short-distance migrant depending on latitude.", "sources": citation},
        "conservation": {"system": "IUCN", "status": "Least Concern", "assessed_at": "2024", "sources": citation},
        "fact": {"value": "Robins can produce several broods in a season.", "sources": citation},
    }


def write_catalogue(tmp_path, species=None, sources=None, review_hash=None):
    catalogue = {
        "schema_version": 1,
        "catalogue_version": "2026-07-25",
        "species": species or {"Turdus migratorius": robin_facts()},
        "sources": sources or {"cornell-robin": source()},
    }
    catalogue_path = tmp_path / "bird_cards.json"
    raw = (json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n").encode()
    catalogue_path.write_bytes(raw)
    review = {
        "schema_version": 1,
        "catalogue_sha256": review_hash or hashlib.sha256(raw).hexdigest(),
        "reviewed_at": "2026-07-25T12:00:00-04:00",
        "verdict": "passed",
        "reviewers": [
            {"provider": "anthropic", "model": "claude-opus", "verdict": "passed"},
            {"provider": "xai", "model": "grok", "verdict": "passed"},
        ],
        "unresolved_conflicts": [],
    }
    review_path = tmp_path / "bird_cards.review.json"
    review_path.write_text(json.dumps(review))
    return catalogue_path, review_path


def update_review_hash(catalogue_path, review_path):
    review = json.loads(review_path.read_text())
    review["catalogue_sha256"] = hashlib.sha256(catalogue_path.read_bytes()).hexdigest()
    review_path.write_text(json.dumps(review))


def test_catalogue_requires_hash_bound_two_reviewer_pass(tmp_path):
    catalogue_path, review_path = write_catalogue(tmp_path)
    loaded = site.load_bird_card_catalogue(catalogue_path, review_path)
    assert loaded["catalogue_version"] == "2026-07-25"
    assert loaded["review"]["verdict"] == "passed"
    assert len(loaded["review"]["reviewers"]) == 2

    review = json.loads(review_path.read_text())
    review["catalogue_sha256"] = "0" * 64
    review_path.write_text(json.dumps(review))
    with pytest.raises(RuntimeError, match="review hash"):
        site.load_bird_card_catalogue(catalogue_path, review_path)

    catalogue_path, review_path = write_catalogue(tmp_path)
    review = json.loads(review_path.read_text())
    review["reviewers"][1] = {
        "provider": "different-provider",
        "model": "claude-opus",
        "verdict": "passed",
    }
    review_path.write_text(json.dumps(review))
    with pytest.raises(RuntimeError, match="models are not distinct"):
        site.load_bird_card_catalogue(catalogue_path, review_path)


def test_catalogue_rejects_unsafe_sources_and_unresolved_review(tmp_path):
    catalogue_path, review_path = write_catalogue(
        tmp_path,
        sources={"cornell-robin": {**source(), "url": "javascript:alert(1)"}},
    )
    with pytest.raises(RuntimeError, match="source URL"):
        site.load_bird_card_catalogue(catalogue_path, review_path)

    catalogue_path, review_path = write_catalogue(tmp_path)
    review = json.loads(review_path.read_text())
    review["unresolved_conflicts"] = ["Turdus migratorius.wingspan_cm"]
    review_path.write_text(json.dumps(review))
    with pytest.raises(RuntimeError, match="unresolved"):
        site.load_bird_card_catalogue(catalogue_path, review_path)


def test_catalogue_rejects_missing_field_citations_and_false_precision(tmp_path):
    bad = robin_facts()
    bad["diet"] = {"value": "Anything.", "sources": []}
    catalogue_path, review_path = write_catalogue(
        tmp_path, species={"Turdus migratorius": bad},
    )
    with pytest.raises(RuntimeError, match="diet.*sources"):
        site.load_bird_card_catalogue(catalogue_path, review_path)

    bad = robin_facts()
    bad["wingspan_cm"]["min"] = 31.123
    catalogue_path, review_path = write_catalogue(
        tmp_path, species={"Turdus migratorius": bad},
    )
    with pytest.raises(RuntimeError, match="precision"):
        site.load_bird_card_catalogue(catalogue_path, review_path)


def test_join_cards_orders_by_garden_rarity_and_keeps_missing_facts_pending(tmp_path):
    catalogue_path, review_path = write_catalogue(tmp_path)
    catalogue = site.load_bird_card_catalogue(catalogue_path, review_path)
    species = [
        {
            "scientific_name": "Cardinalis cardinalis",
            "common_name": "Northern Cardinal",
            "days_heard": 1,
            "detections": 2,
            "first_heard": "2026-07-20T06:00:00",
            "standing": "corroborated",
        },
        {
            "scientific_name": "Turdus migratorius",
            "common_name": "American Robin",
            "days_heard": 2,
            "detections": 3,
            "first_heard": "2026-07-19T06:00:00",
            "standing": "corroborated",
        },
        {
            "scientific_name": "Ficta avis",
            "common_name": "Future Bird",
            "days_heard": 1,
            "detections": 1,
            "first_heard": "2026-07-25T06:00:00",
            "standing": "uncorroborated",
        },
    ]
    joined = site.join_bird_cards(species, catalogue)
    assert [row["common_name"] for row in joined] == [
        "Future Bird", "Northern Cardinal", "American Robin",
    ]
    assert [row["garden_rarity_rank"] for row in joined] == [1, 2, 3]
    assert joined[0]["bird_card"] == {
        "research_status": "research_pending",
        "facts": None,
        "sources": [],
    }
    assert joined[2]["bird_card"]["research_status"] == "verified"


def test_catalogue_rejects_unknown_keys_nonfinite_json_and_lookalike_hosts(tmp_path):
    catalogue, review = write_catalogue(tmp_path)
    payload = json.loads(catalogue.read_text())
    payload["unknown_top_level"] = True
    catalogue.write_text(json.dumps(payload, sort_keys=True))
    update_review_hash(catalogue, review)
    with pytest.raises(RuntimeError, match="unknown bird-card catalogue fields"):
        site.load_bird_card_catalogue(catalogue, review)

    catalogue, review = write_catalogue(tmp_path)
    payload = json.loads(catalogue.read_text())
    payload["species"]["Turdus migratorius"]["wingspan_cm"]["min"] = float("nan")
    catalogue.write_text(json.dumps(payload, sort_keys=True))
    update_review_hash(catalogue, review)
    with pytest.raises(RuntimeError, match="non-finite JSON constant"):
        site.load_bird_card_catalogue(catalogue, review)

    for url in (
        "https://evilallaboutbirds.org/guide/American_Robin/overview",
        "https://user@example.com/path",
        "https://www.allaboutbirds.org:bad/path",
    ):
        catalogue, review = write_catalogue(tmp_path)
        payload = json.loads(catalogue.read_text())
        payload["sources"]["cornell-robin"]["url"] = url
        catalogue.write_text(json.dumps(payload, sort_keys=True))
        update_review_hash(catalogue, review)
        with pytest.raises(RuntimeError, match="unsafe source URL"):
            site.load_bird_card_catalogue(catalogue, review)

    catalogue, review = write_catalogue(tmp_path)
    payload = json.loads(catalogue.read_text())
    payload["sources"]["cornell-robin"]["accessed_at"] = "2026-99-99"
    catalogue.write_text(json.dumps(payload, sort_keys=True))
    update_review_hash(catalogue, review)
    with pytest.raises(RuntimeError, match="accessed_at"):
        site.load_bird_card_catalogue(catalogue, review)

    catalogue, review = write_catalogue(tmp_path)
    payload = json.loads(catalogue.read_text())
    payload["species"]["Turdus migratorius"]["conservation"]["assessed_at"] = "2026-99-99"
    catalogue.write_text(json.dumps(payload, sort_keys=True))
    update_review_hash(catalogue, review)
    with pytest.raises(RuntimeError, match="conservation.assessed_at"):
        site.load_bird_card_catalogue(catalogue, review)


def test_retained_measurement_evidence_matches_catalogue_exactly():
    catalogue = json.loads(site.BIRD_CARD_CATALOGUE_PATH.read_text())
    evidence_path = site.BIRD_CARD_DATA_DIR / "bird_cards.evidence.json"
    evidence = json.loads(evidence_path.read_text())
    assert len(evidence["records"]) == 10
    pattern = re.compile(
        r"Length:.*?\(([0-9.]+)-([0-9.]+) cm\).*?"
        r"Wingspan:.*?\(([0-9.]+)-([0-9.]+) cm\)"
    )
    for scientific_name, record in evidence["records"].items():
        match = pattern.search(record["excerpt"])
        assert match, scientific_name
        expected = tuple(float(value) for value in match.groups())
        facts = catalogue["species"][scientific_name]
        actual = (
            float(facts["length_cm"]["min"]),
            float(facts["length_cm"]["max"]),
            float(facts["wingspan_cm"]["min"]),
            float(facts["wingspan_cm"]["max"]),
        )
        assert actual == expected, scientific_name
        for field in ("length_cm", "wingspan_cm"):
            urls = {
                catalogue["sources"][source_id]["url"]
                for source_id in facts[field]["sources"]
            }
            assert urls == {record["source_url"]}, (scientific_name, field)
        assert record["snapshot_url"].startswith("https://web.archive.org/web/")
        assert re.fullmatch(r"[A-Z2-7]{32}", record["digest"])

    assert "Leuconotopicus villosus" in catalogue["species"]["Dryobates villosus"]["taxonomy_note"]["value"]
    assert "Yellow-shafted Flicker" in catalogue["species"]["Colaptes auratus"]["taxonomy_note"]["value"]


def test_tracked_catalogue_is_complete_cited_and_review_hash_matches():
    catalogue = site.load_bird_card_catalogue(
        site.BIRD_CARD_CATALOGUE_PATH,
        site.BIRD_CARD_REVIEW_PATH,
    )
    assert catalogue["schema_version"] == 1
    assert len(catalogue["species"]) >= 40
    for scientific_name, facts in catalogue["species"].items():
        assert scientific_name and facts["research_status"] == "verified"
        for field in site.BIRD_CARD_FACT_FIELDS:
            assert facts[field]["sources"], f"{scientific_name}.{field} lacks sources"
