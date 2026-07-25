#!/usr/bin/env python3
"""Tailnet-only web service for the append-only BirdNET archive.

Serves a read-only API, static field-journal UI, evidence audio by detection ID,
and a local illustration mirror. It binds loopback by default; Caddy/Tailscale
is the intended security boundary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import sys
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

try:
    from .. import corroboration
except ImportError:  # Direct execution of this script.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from avian.archive import corroboration  # type: ignore[no-redef]

HEX_ID = re.compile(r"^[0-9a-f]{64}$")
SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TZ = ZoneInfo("America/New_York")
PUBLICATION_MIN_CONFIDENCE = 0.70
MAX_OFFSET = 1_000_000
MAX_RELATED_CANDIDATES = 5_000
MAX_PUBLIC_AUDIO_BYTES = (1 << 53) - 1
PUBLIC_TIMEZONES = frozenset({"America/New_York"})
PUBLIC_AUDIO_QUALITY_SCHEMA_HASHES = {
    "audio_quality": "32cd0b17d64e21ee02dd85999b0188489424a1d9702a4ba883378f40477ddb1a",
    "audio_quality_algorithm_idx": "64978545e34e5707ebc1390afc04c8afce669e8934b52709c32c9748786f0007",
    "audio_quality_no_update": "71a76deb389a5ef6efae29e1d0a280bc24006abd896a6d0ce7241acc62ac032d",
    "audio_quality_no_delete": "7ff42732da039373cf9f697c5a8e334d3959a6f83882800417c54c87005c8546",
    "audio_quality_no_replace": "4689b3095fae1d91a32423dcbfbfbbbecf85de95ca42a26b43606499b81a4bbd",
    "audio_quality_no_explicit_rowid": "597d09ce3c3c11ae8f12fb9187990f38b8f23b7d901aa4a2da5e98d80f27c38a",
    "audio_quality_positive_rowid": "005d47c6514ad0507a70e3f019c6a8c4434b37889fa054135b05cc9f5e83d252",
}
PUBLIC_REVIEW_INTERPRETATION_SCHEMA_HASHES = {
    "review_interpretations": "402cd11f4a957fff4f3fc5feb2fdfd4c37e3183a236c8b823a8a75c19b7aed87",
    "review_interpretations_policy_idx": "eac467566460e7c89bbffb070439ae836b717e5b4f687d72d4b4843d4d35c29a",
    "review_interpretations_no_update": "efe613ec0faa00f9abfc140fde1ddf221aa824292876c794213dfc985aa683ec",
    "review_interpretations_no_delete": "a9f62a0c24c48251beb26d7cd13d696d7f608a7a58b57d53eccee133d8951018",
    "review_interpretations_no_replace": "936d6eaf0d104cd3ee15945e425aeca8152acb4adb2fa81946fc57062aa75c11",
}
PUBLIC_REVIEW_GUARD_SCHEMA_HASHES = {
    "reviews_no_update": "74314dcc9be778c8958fa352a0736833237c8a37b45adbae430be98708d1b2b1",
    "reviews_no_delete": "f00b471843ebfbb068ca1aa1f6645b2546c8d8be135d08fcd38791656e9a5cd7",
    "reviews_no_replace": "827005e60b478b65560386ccc7322e89efaae8d10cc57348cd16ebf3c6a373e6",
}
PUBLIC_REVIEW_MODEL = "Google Perch 2.0 ONNX (inat2024_fsd50k)"
PUBLIC_REVIEW_POLICY_VERSION = "perch-corroboration-v2"
PUBLIC_REVIEW_OUTCOMES = frozenset({
    "corroborated", "uncorroborated", "model_conflict",
})
REVIEW_STATUS_SQL = (
    "COALESCE(i.outcome, CASE WHEN r.detection_id IS NOT NULL THEN 'pending' "
    "WHEN d.confidence < 0.90 THEN 'pending' ELSE 'unreviewed' END)"
)
REVIEW_INTERPRETATION_JOIN_SQL = (
    "LEFT JOIN review_interpretations i ON i.detection_id=d.detection_id "
    f"AND i.policy_version='{PUBLIC_REVIEW_POLICY_VERSION}' "
    "AND valid_current_interpretation(i.outcome,i.claim_score,i.claim_rank,"
    "i.label_count,i.top_label,i.top_score,i.top_score_provenance)=1"
)
PUBLIC_AUDIO_QUALITY_ALGORITHM = "frame-level-percentiles-v1"
PERCH_LABELS_PATH = (
    Path.home() / "Library/Application Support/AvianVisitorsArchive/perch/assets/labels.csv"
)
PINNED_PERCH_LABELS_SHA256 = "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057"
BIRD_CARD_DATA_DIR = Path(__file__).resolve().parent / "data"
BIRD_CARD_CATALOGUE_PATH = BIRD_CARD_DATA_DIR / "bird_cards.json"
BIRD_CARD_REVIEW_PATH = BIRD_CARD_DATA_DIR / "bird_cards.review.json"
BIRD_CARD_FACT_FIELDS = (
    "habitat", "diet", "wingspan_cm", "length_cm", "nest", "clutch_size",
    "migration", "conservation", "fact",
)
BIRD_CARD_OPTIONAL_FACT_FIELDS = ("taxonomy_note",)
BIRD_CARD_SOURCE_HOSTS = frozenset({
    "allaboutbirds.org", "audubon.org", "birdlife.org", "fws.gov",
    "iucnredlist.org", "stateofthebirds.org",
})
BIRD_CARD_NEST_TYPES = frozenset({
    "cavity", "cup", "ground", "platform", "pendant", "other",
})
BIRD_CARD_MIGRATION_CATEGORIES = frozenset({
    "resident", "partial", "short-distance", "long-distance", "nomadic",
})
PERCH_CONCLUSIONS = (
    "Perch independently supports the BirdNET species claim.",
    "Perch strongly favours another species and does not support the BirdNET claim.",
    "Perch is inconclusive or disagrees; human review remains appropriate.",
)
TAXON_PATTERN = (
    r"(?:[A-Z][a-z]{1,30}(?: [a-z][a-z-]{1,30}){1,2}"
    r"|Door|Tools|Explosion|Domestic_sounds_and_home_sounds)"
)
PERCH_NOTE_PATTERN = re.compile(
    rf"^(?P<conclusion>{'|'.join(map(re.escape, PERCH_CONCLUSIONS))}) "
    r"Claimed species rank (?P<rank>[1-9][0-9]*) of (?P<total>[1-9][0-9]*) "
    r"with score (?P<claim>[0-9]{1,3}\.[0-9])%\. Perch top results: "
    rf"(?P<taxon1>{TAXON_PATTERN}) (?P<score1>[0-9]{{1,3}}\.[0-9])%; "
    rf"(?P<taxon2>{TAXON_PATTERN}) (?P<score2>[0-9]{{1,3}}\.[0-9])%; "
    rf"(?P<taxon3>{TAXON_PATTERN}) (?P<score3>[0-9]{{1,3}}\.[0-9])%\. "
    r"Scores are independent classifier outputs, not calibrated probabilities\.$"
)


class SpeciesSlugConflict(Exception):
    """A legacy species slug identifies more than one published name."""


class RelatedQueueChanged(Exception):
    """The ordered comparison queue changed between page requests."""


class RelatedQueueTooLarge(Exception):
    """The candidate population exceeds the endpoint's bounded work budget."""


def _json_without_duplicate_keys(raw: bytes, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RuntimeError(f"duplicate key in {label}: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise RuntimeError(f"non-finite JSON constant in {label}: {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label} JSON") from exc


def _exact_card_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        raise RuntimeError(f"unknown {label} fields: {sorted(extra)}")
    if missing:
        raise RuntimeError(f"missing {label} fields: {sorted(missing)}")


def _card_text(value: Any, field: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RuntimeError(f"invalid {field}")
    if any(ord(char) < 32 or char in "\u2028\u2029" for char in value):
        raise RuntimeError(f"invalid control character in {field}")
    return value.strip()


def _card_date(value: Any, field: str, *, allow_year: bool = False) -> str:
    text = _card_text(value, field, 20)
    if allow_year and re.fullmatch(r"20\d{2}", text):
        return text
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
        raise RuntimeError(f"invalid {field}")
    try:
        dt.date.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"invalid {field}") from exc
    return text


def _card_sources(value: Any, field: str, known: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{field}.sources must not be empty")
    if any(not isinstance(item, str) or item not in known for item in value):
        raise RuntimeError(f"{field}.sources references an unknown source")
    if len(value) != len(set(value)):
        raise RuntimeError(f"{field}.sources contains duplicates")
    return list(value)


def _card_number(value: Any, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"invalid {field}")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise RuntimeError(f"invalid {field}")
    if round(number, 1) != number:
        raise RuntimeError(f"false precision in {field}")
    return number


def _card_range(
    value: Any, field: str, known_sources: set[str], maximum: float,
    *, integer: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {field}")
    _exact_card_keys(value, {"min", "max", "sources"}, field)
    low = _card_number(value.get("min"), f"{field}.min", maximum)
    high = _card_number(value.get("max"), f"{field}.max", maximum)
    if low > high:
        raise RuntimeError(f"invalid {field} range")
    if integer and (not low.is_integer() or not high.is_integer()):
        raise RuntimeError(f"invalid integer {field}")
    return {
        "min": int(low) if integer else low,
        "max": int(high) if integer else high,
        "sources": _card_sources(value.get("sources"), field, known_sources),
    }


def _allowed_card_source_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 500:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
        and any(host == allowed or host.endswith("." + allowed)
                for allowed in BIRD_CARD_SOURCE_HOSTS)
        and all(ord(char) >= 32 and char not in "\u2028\u2029" for char in value)
    )


def load_bird_card_catalogue(
    catalogue_path: Path = BIRD_CARD_CATALOGUE_PATH,
    review_path: Path = BIRD_CARD_REVIEW_PATH,
) -> dict[str, Any]:
    """Load the immutable, independently reviewed natural-history catalogue."""
    raw = catalogue_path.read_bytes()
    if not raw or len(raw) > 2_000_000:
        raise RuntimeError("invalid bird-card catalogue size")
    catalogue = _json_without_duplicate_keys(raw, "bird-card catalogue")
    review = _json_without_duplicate_keys(
        review_path.read_bytes(), "bird-card review manifest",
    )
    if not isinstance(catalogue, dict) or catalogue.get("schema_version") != 1:
        raise RuntimeError("unsupported bird-card catalogue schema")
    if not isinstance(review, dict) or review.get("schema_version") != 1:
        raise RuntimeError("unsupported bird-card review schema")
    _exact_card_keys(
        catalogue,
        {"schema_version", "catalogue_version", "sources", "species"},
        "bird-card catalogue",
    )
    _exact_card_keys(
        review,
        {
            "schema_version", "catalogue_sha256", "reviewed_at", "verdict",
            "reviewers", "unresolved_conflicts",
        },
        "bird-card review",
    )
    expected_hash = hashlib.sha256(raw).hexdigest()
    if review.get("catalogue_sha256") != expected_hash:
        raise RuntimeError("bird-card review hash does not match catalogue")
    if review.get("verdict") != "passed":
        raise RuntimeError("bird-card review did not pass")
    conflicts = review.get("unresolved_conflicts")
    if conflicts != []:
        raise RuntimeError("bird-card review has unresolved conflicts")
    reviewers = review.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) < 2:
        raise RuntimeError("bird-card catalogue needs two independent reviewers")
    identities: set[tuple[str, str]] = set()
    model_ids: set[str] = set()
    for index, reviewer in enumerate(reviewers):
        if not isinstance(reviewer, dict) or reviewer.get("verdict") != "passed":
            raise RuntimeError(f"bird-card reviewer {index + 1} did not pass")
        _exact_card_keys(
            reviewer, {"provider", "model", "verdict"},
            f"bird-card reviewer {index + 1}",
        )
        provider = _card_text(reviewer.get("provider"), "reviewer.provider", 80)
        model = _card_text(reviewer.get("model"), "reviewer.model", 120)
        identities.add((provider, model))
        model_ids.add(model.casefold().rsplit("/", 1)[-1])
    if len(identities) < 2:
        raise RuntimeError("bird-card reviewers are not independent")
    if len(model_ids) < 2:
        raise RuntimeError("bird-card reviewer models are not distinct")
    reviewed_at = _card_text(review.get("reviewed_at"), "reviewed_at", 80)
    try:
        dt.datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise RuntimeError("invalid reviewed_at") from exc
    version = _card_text(catalogue.get("catalogue_version"), "catalogue_version", 80)

    source_rows = catalogue.get("sources")
    if not isinstance(source_rows, dict) or not source_rows:
        raise RuntimeError("bird-card source catalogue is empty")
    sources: dict[str, dict[str, str]] = {}
    for source_id, source in source_rows.items():
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", source_id):
            raise RuntimeError("invalid bird-card source id")
        if not isinstance(source, dict) or source.get("id") != source_id:
            raise RuntimeError(f"invalid source record: {source_id}")
        _exact_card_keys(
            source, {"id", "publisher", "title", "url", "accessed_at"},
            f"source {source_id}",
        )
        url = source.get("url")
        if not _allowed_card_source_url(url):
            raise RuntimeError(f"unsafe source URL: {source_id}")
        accessed_at = _card_date(
            source.get("accessed_at"), f"{source_id}.accessed_at",
        )
        sources[source_id] = {
            "id": source_id,
            "publisher": _card_text(source.get("publisher"), f"{source_id}.publisher", 120),
            "title": _card_text(source.get("title"), f"{source_id}.title", 200),
            "url": str(url),
            "accessed_at": accessed_at,
        }
    known_sources = set(sources)

    species_rows = catalogue.get("species")
    if not isinstance(species_rows, dict) or not species_rows:
        raise RuntimeError("bird-card species catalogue is empty")
    species: dict[str, dict[str, Any]] = {}
    for scientific_name, raw_facts in species_rows.items():
        if not isinstance(scientific_name, str) or not re.fullmatch(
            r"[A-Z][a-z-]{1,40}(?: [a-z][a-z-]{1,40}){1,2}", scientific_name,
        ):
            raise RuntimeError("invalid bird-card scientific name")
        if not isinstance(raw_facts, dict) or raw_facts.get("research_status") != "verified":
            raise RuntimeError(f"unverified bird-card entry: {scientific_name}")
        required_fact_keys = {"research_status", *BIRD_CARD_FACT_FIELDS}
        extra_fact_keys = (
            set(raw_facts) - required_fact_keys - set(BIRD_CARD_OPTIONAL_FACT_FIELDS)
        )
        missing_fact_keys = required_fact_keys - set(raw_facts)
        if extra_fact_keys:
            raise RuntimeError(
                f"unknown bird-card entry {scientific_name} fields: "
                f"{sorted(extra_fact_keys)}"
            )
        if missing_fact_keys:
            raise RuntimeError(
                f"missing bird-card entry {scientific_name} fields: "
                f"{sorted(missing_fact_keys)}"
            )

        facts: dict[str, Any] = {"research_status": "verified"}
        for field in ("habitat", "diet", "fact"):
            item = raw_facts[field]
            if not isinstance(item, dict):
                raise RuntimeError(f"invalid {scientific_name}.{field}")
            _exact_card_keys(
                item, {"value", "sources"}, f"{scientific_name}.{field}",
            )
            facts[field] = {
                "value": _card_text(item.get("value"), f"{scientific_name}.{field}"),
                "sources": _card_sources(
                    item.get("sources"), f"{scientific_name}.{field}", known_sources,
                ),
            }
        facts["wingspan_cm"] = _card_range(
            raw_facts["wingspan_cm"], f"{scientific_name}.wingspan_cm",
            known_sources, 300.0,
        )
        facts["length_cm"] = _card_range(
            raw_facts["length_cm"], f"{scientific_name}.length_cm",
            known_sources, 250.0,
        )
        facts["clutch_size"] = _card_range(
            raw_facts["clutch_size"], f"{scientific_name}.clutch_size",
            known_sources, 30.0, integer=True,
        )
        nest = raw_facts["nest"]
        if not isinstance(nest, dict) or nest.get("type") not in BIRD_CARD_NEST_TYPES:
            raise RuntimeError(f"invalid {scientific_name}.nest")
        _exact_card_keys(
            nest, {"type", "value", "sources"}, f"{scientific_name}.nest",
        )
        facts["nest"] = {
            "type": nest["type"],
            "value": _card_text(nest.get("value"), f"{scientific_name}.nest"),
            "sources": _card_sources(
                nest.get("sources"), f"{scientific_name}.nest", known_sources,
            ),
        }
        migration = raw_facts["migration"]
        if not isinstance(migration, dict) or migration.get("category") not in BIRD_CARD_MIGRATION_CATEGORIES:
            raise RuntimeError(f"invalid {scientific_name}.migration")
        _exact_card_keys(
            migration, {"category", "value", "sources"},
            f"{scientific_name}.migration",
        )
        facts["migration"] = {
            "category": migration["category"],
            "value": _card_text(
                migration.get("value"), f"{scientific_name}.migration",
            ),
            "sources": _card_sources(
                migration.get("sources"), f"{scientific_name}.migration",
                known_sources,
            ),
        }
        conservation = raw_facts["conservation"]
        if not isinstance(conservation, dict):
            raise RuntimeError(f"invalid {scientific_name}.conservation")
        _exact_card_keys(
            conservation, {"system", "status", "assessed_at", "sources"},
            f"{scientific_name}.conservation",
        )
        assessed_at = _card_date(
            conservation.get("assessed_at"),
            f"{scientific_name}.conservation.assessed_at",
            allow_year=True,
        )
        facts["conservation"] = {
            "system": _card_text(
                conservation.get("system"), f"{scientific_name}.conservation.system", 80,
            ),
            "status": _card_text(
                conservation.get("status"), f"{scientific_name}.conservation.status", 80,
            ),
            "assessed_at": assessed_at,
            "sources": _card_sources(
                conservation.get("sources"), f"{scientific_name}.conservation",
                known_sources,
            ),
        }
        taxonomy_note = raw_facts.get("taxonomy_note")
        if taxonomy_note is not None:
            _exact_card_keys(
                taxonomy_note, {"value", "sources"},
                f"{scientific_name}.taxonomy_note",
            )
            facts["taxonomy_note"] = {
                "value": _card_text(
                    taxonomy_note.get("value"),
                    f"{scientific_name}.taxonomy_note",
                ),
                "sources": _card_sources(
                    taxonomy_note.get("sources"),
                    f"{scientific_name}.taxonomy_note", known_sources,
                ),
            }
        species[scientific_name] = facts

    return {
        "schema_version": 1,
        "catalogue_version": version,
        "species": species,
        "sources": sources,
        "review": {
            "reviewed_at": review["reviewed_at"],
            "verdict": "passed",
            "reviewers": [
                {"provider": item["provider"], "model": item["model"], "verdict": "passed"}
                for item in reviewers
            ],
        },
    }


def _card_source_ids(facts: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for field in BIRD_CARD_FACT_FIELDS:
        result.update(facts[field]["sources"])
    for field in BIRD_CARD_OPTIONAL_FACT_FIELDS:
        if field in facts:
            result.update(facts[field]["sources"])
    return result


def bird_card_for_species(scientific_name: str, catalogue: dict[str, Any]) -> dict[str, Any]:
    facts = catalogue["species"].get(scientific_name)
    if facts is None:
        return {
            "research_status": "research_pending",
            "facts": None,
            "sources": [],
        }
    source_ids = _card_source_ids(facts)
    return {
        "research_status": "verified",
        "facts": {key: value for key, value in facts.items() if key != "research_status"},
        "sources": [catalogue["sources"][key] for key in sorted(source_ids)],
        "catalogue_version": catalogue["catalogue_version"],
        "reviewed_at": catalogue["review"]["reviewed_at"],
        "reviewers": catalogue["review"]["reviewers"],
    }


def _first_heard_sort_value(value: Any) -> float:
    try:
        return -dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def join_bird_cards(
    species_rows: Iterable[dict[str, Any]], catalogue: dict[str, Any],
) -> list[dict[str, Any]]:
    cards = [
        {**row, "bird_card": bird_card_for_species(row["scientific_name"], catalogue)}
        for row in species_rows
    ]
    cards.sort(key=lambda row: (
        int(row.get("days_heard") or 0),
        int(row.get("detections") or 0),
        _first_heard_sort_value(row.get("first_heard")),
        str(row.get("common_name") or ""),
        str(row.get("scientific_name") or ""),
    ))
    for rank, card in enumerate(cards, 1):
        card["garden_rarity_rank"] = rank
    return cards


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def slugify(scientific_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", scientific_name.lower()).strip("-")


def canonical_species_slug(scientific_name: str) -> str:
    """Return a deterministic, collision-resistant public species identifier."""
    digest = hashlib.sha256(scientific_name.encode("utf-8")).hexdigest()[:10]
    base = slugify(scientific_name) or "species"
    return f"{base}-{digest}"


def is_frontend_route(path: str) -> bool:
    """Allow only the app's canonical document routes; reject catch-all paths."""
    if path in {"/", "/index.html", "/explore", "/explore/", "/cards",
                "/cards/", "/species", "/species/", "/about", "/about/"}:
        return True
    detection_match = re.fullmatch(r"/detection/([^/]+)/?", path)
    if detection_match:
        return bool(HEX_ID.fullmatch(detection_match.group(1)))
    species_match = re.fullmatch(r"/species/([^/]+)/?", path)
    return bool(species_match and SAFE_SLUG.fullmatch(species_match.group(1)))


def public_model_label(value: Any) -> str:
    """Return only a model filename with an allow-listed executable-model suffix."""
    label = re.split(r"[\\/]", str(value or ""))[-1].strip()
    if re.fullmatch(r"[A-Za-z0-9_.()+-]{1,120}\.(?:bin|onnx|tflite)", label, re.I):
        return label
    return "redacted" if label else ""


def public_review_model(value: Any) -> Optional[str]:
    """Publish a known reviewer label, never arbitrary operator metadata."""
    if not value:
        return None
    return PUBLIC_REVIEW_MODEL if str(value) == PUBLIC_REVIEW_MODEL else "Independent review"


def public_review_kind(value: Any) -> Optional[str]:
    if not value:
        return None
    return "perch" if str(value) == PUBLIC_REVIEW_MODEL else "independent"


def public_score(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) and 0 <= score <= 1 else None


def public_positive_int(value: Any, maximum: int) -> Optional[int]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer() or not 0 < number <= maximum:
        return None
    return int(number)


def public_timezone(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in PUBLIC_TIMEZONES else None


@lru_cache(maxsize=1)
def pinned_perch_taxa() -> frozenset[str]:
    """Load the exact checksum-pinned Perch taxonomy; fail closed if unavailable."""
    try:
        content = PERCH_LABELS_PATH.read_bytes()
    except OSError:
        return frozenset()
    if hashlib.sha256(content).hexdigest() != PINNED_PERCH_LABELS_SHA256:
        return frozenset()
    lines = content.decode("utf-8").splitlines()
    if len(lines) != 14_796 or lines[0] != "inat2024_fsd50k":
        return frozenset()
    return frozenset(lines[1:])


def public_review_notes(value: Any) -> Optional[str]:
    """Parse and reconstruct only notes emitted by the pinned Perch reviewer."""
    match = PERCH_NOTE_PATTERN.fullmatch(str(value or ""))
    if not match:
        return None
    groups = match.groupdict()
    taxa = pinned_perch_taxa()
    if not taxa or any(groups[name] not in taxa for name in ("taxon1", "taxon2", "taxon3")):
        return None
    rank, total = int(groups["rank"]), int(groups["total"])
    scores = [float(groups[name]) for name in ("claim", "score1", "score2", "score3")]
    if rank > total or total > 100_000 or any(score > 100 for score in scores):
        return None
    return (
        f"{groups['conclusion']} Claimed species rank {groups['rank']} of {groups['total']} "
        f"with score {groups['claim']}%. Perch top results: "
        f"{groups['taxon1']} {groups['score1']}%; {groups['taxon2']} {groups['score2']}%; "
        f"{groups['taxon3']} {groups['score3']}%. "
        "Scores are independent classifier outputs, not calibrated probabilities."
    )


def public_audio_quality(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Publish only finite, bounded metrics from the current quality algorithm."""
    method = row.pop("quality_algorithm", None)
    sample_rate = row.pop("quality_sample_rate", None)
    duration = row.pop("quality_duration_seconds", None)
    quiet_level = row.pop("quality_noise_floor_dbfs", None)
    high_energy_level = row.pop("quality_signal_level_dbfs", None)
    contrast = row.pop("quality_signal_contrast_db", None)
    near_full_scale = row.pop("quality_clipping_fraction", None)
    if method is None or method != PUBLIC_AUDIO_QUALITY_ALGORITHM:
        return None
    try:
        values = tuple(float(value) for value in (
            sample_rate, duration, quiet_level, high_energy_level,
            contrast, near_full_scale,
        ))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    (
        sample_rate_value, duration_value, quiet_value, high_energy_value,
        contrast_value, near_full_scale_value,
    ) = values
    if not (
        sample_rate_value.is_integer()
        and 8_000 <= sample_rate_value <= 192_000
        and 0 < duration_value <= 3_600
        and -120 <= quiet_value <= high_energy_value <= 0
        and 0 <= contrast_value <= 120
        and 0 <= near_full_scale_value <= 1
    ):
        return None
    return {
        "method": PUBLIC_AUDIO_QUALITY_ALGORITHM,
        "sample_rate_hz": int(sample_rate_value),
        "duration_seconds": round(duration_value, 3),
        "quiet_frame_level_dbfs": round(quiet_value, 1),
        "high_energy_frame_level_dbfs": round(high_energy_value, 1),
        "audio_contrast_db": round(contrast_value, 1),
        "near_full_scale_fraction": round(near_full_scale_value, 6),
    }


def db_connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    taxa = pinned_perch_taxa()
    conn.create_function(
        "valid_current_interpretation",
        7,
        lambda outcome, claim_score, claim_rank, label_count, top_label,
        top_score, provenance: int(corroboration.valid_interpretation(
            outcome, claim_score, claim_rank, label_count, top_label,
            top_score, provenance, taxa,
        )),
        deterministic=True,
    )
    conn.execute("PRAGMA query_only=ON")
    return conn


def _normalised_schema_hash(value: str) -> str:
    normalised = re.sub(r"\s+", " ", value.strip().rstrip(";")).lower()
    return hashlib.sha256(normalised.encode()).hexdigest()


def validate_public_archive_schema(path: Path) -> None:
    """Refuse startup unless the published mirror has the exact feature gate."""
    expected_columns = (
        "detection_id", "algorithm_version", "sample_rate", "duration_seconds",
        "noise_floor_dbfs", "signal_level_dbfs", "signal_contrast_db",
        "clipping_fraction", "computed_at",
    )
    expected_triggers = {
        "audio_quality_no_update",
        "audio_quality_no_delete",
        "audio_quality_no_replace",
        "audio_quality_no_explicit_rowid",
        "audio_quality_positive_rowid",
    }
    with db_connect(path) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("published archive failed quick_check")
        for name, expected_hash in PUBLIC_AUDIO_QUALITY_SCHEMA_HASHES.items():
            object_type = (
                "table" if name == "audio_quality"
                else "index" if name == "audio_quality_algorithm_idx"
                else "trigger"
            )
            schema_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                (object_type, name),
            ).fetchone()
            if (
                schema_row is None
                or not schema_row["sql"]
                or _normalised_schema_hash(schema_row["sql"]) != expected_hash
            ):
                raise RuntimeError(f"published archive has noncanonical schema object: {name}")
        columns = tuple(
            row["name"] for row in db.execute("PRAGMA table_info(audio_quality)")
        )
        if columns != expected_columns:
            raise RuntimeError("published archive lacks canonical audio-quality schema")
        primary_key = {
            row["name"]: row["pk"]
            for row in db.execute("PRAGMA table_info(audio_quality)")
            if row["pk"]
        }
        if primary_key != {"detection_id": 1, "algorithm_version": 2}:
            raise RuntimeError("published archive lacks audio-quality primary key")
        indexes = {
            row["name"] for row in db.execute("PRAGMA index_list(audio_quality)")
        }
        expected_indexes = {
            "audio_quality_algorithm_idx", "sqlite_autoindex_audio_quality_1",
        }
        if indexes != expected_indexes:
            raise RuntimeError("published archive has noncanonical audio-quality indexes")
        index = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='index' AND name='audio_quality_algorithm_idx'"""
        ).fetchone()
        if index is None:
            raise RuntimeError("published archive lacks audio-quality index")
        triggers = {
            row["name"] for row in db.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='trigger' AND tbl_name='audio_quality'"""
            )
        }
        if triggers != expected_triggers:
            raise RuntimeError("published archive lacks append-only quality guards")

        for name, expected_hash in PUBLIC_REVIEW_GUARD_SCHEMA_HASHES.items():
            schema_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()
            if (
                schema_row is None
                or not schema_row["sql"]
                or _normalised_schema_hash(schema_row["sql"]) != expected_hash
            ):
                raise RuntimeError(
                    f"published archive has noncanonical schema object: {name}"
                )
        review_triggers = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='reviews'"
            )
        }
        if review_triggers != set(PUBLIC_REVIEW_GUARD_SCHEMA_HASHES):
            raise RuntimeError("published archive lacks append-only review guards")

        expected_interpretation_columns = (
            "detection_id", "policy_version", "outcome", "claim_score",
            "claim_rank", "label_count", "top_label", "top_score",
            "top_score_provenance", "interpreted_at",
        )
        for name, expected_hash in PUBLIC_REVIEW_INTERPRETATION_SCHEMA_HASHES.items():
            object_type = (
                "table" if name == "review_interpretations"
                else "index" if name == "review_interpretations_policy_idx"
                else "trigger"
            )
            schema_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                (object_type, name),
            ).fetchone()
            if (
                schema_row is None
                or not schema_row["sql"]
                or _normalised_schema_hash(schema_row["sql"]) != expected_hash
            ):
                raise RuntimeError(
                    f"published archive has noncanonical schema object: {name}"
                )
        interpretation_columns = tuple(
            row["name"] for row in db.execute(
                "PRAGMA table_info(review_interpretations)"
            )
        )
        if interpretation_columns != expected_interpretation_columns:
            raise RuntimeError("published archive lacks canonical review interpretation schema")
        interpretation_indexes = {
            row["name"] for row in db.execute(
                "PRAGMA index_list(review_interpretations)"
            )
        }
        if interpretation_indexes != {
            "review_interpretations_policy_idx",
            "sqlite_autoindex_review_interpretations_1",
        }:
            raise RuntimeError("published archive has noncanonical interpretation indexes")
        interpretation_triggers = {
            row["name"] for row in db.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='trigger' AND tbl_name='review_interpretations'"""
            )
        }
        if interpretation_triggers != {
            "review_interpretations_no_update", "review_interpretations_no_delete",
            "review_interpretations_no_replace",
        }:
            raise RuntimeError("published archive lacks append-only interpretation guards")


def sanitize_public_scores(row: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "confidence", "best_confidence", "mean_confidence", "review_score",
        "review_top_score", "confidence_threshold",
    ):
        if key in row:
            row[key] = public_score(row[key])
    return row


def rows_dict(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [sanitize_public_scores(dict(row)) for row in rows]


def get_summary(db_path: Path) -> dict[str, Any]:
    today = now_local().date().isoformat()
    seven_start = (now_local().date() - dt.timedelta(days=6)).isoformat()
    previous_start = (now_local().date() - dt.timedelta(days=13)).isoformat()
    previous_end = (now_local().date() - dt.timedelta(days=7)).isoformat()
    with db_connect(db_path) as db:
        totals = dict(db.execute(
            """SELECT count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      count(DISTINCT date) AS days_listening,
                      min(observed_at_local) AS first_record,
                      max(observed_at_local) AS latest_record,
                      sum(audio_sha256 IS NOT NULL) AS clips_preserved
               FROM detections WHERE confidence BETWEEN ? AND 1.0""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone())
        standings = db.execute(
            f"""SELECT
                 sum(CASE WHEN corroborated > 0 THEN 1 ELSE 0 END) AS corroborated_species,
                 sum(CASE WHEN corroborated = 0 THEN 1 ELSE 0 END) AS uncorroborated_species
               FROM (
                 SELECT d.scientific_name,
                        sum(CASE WHEN i.outcome='corroborated' THEN 1 ELSE 0 END)
                          AS corroborated
                 FROM detections d
                 {REVIEW_INTERPRETATION_JOIN_SQL}
                 WHERE d.confidence BETWEEN ? AND 1.0
                 GROUP BY d.scientific_name
               )""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone()
        totals["corroborated_species"] = int(standings["corroborated_species"] or 0)
        totals["uncorroborated_species"] = int(standings["uncorroborated_species"] or 0)
        today_row = dict(db.execute(
            """SELECT count(*) AS detections,
                      count(DISTINCT scientific_name) AS species
               FROM detections WHERE date=? AND confidence BETWEEN ? AND 1.0""",
            (today, PUBLICATION_MIN_CONFIDENCE),
        ).fetchone())
        recent = db.execute(
            """SELECT
                 sum(CASE WHEN date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS last_7,
                 sum(CASE WHEN date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS previous_7
               FROM detections WHERE confidence BETWEEN ? AND 1.0""",
            (seven_start, today, previous_start, previous_end,
             PUBLICATION_MIN_CONFIDENCE),
        ).fetchone()
        sync = db.execute(
            """SELECT completed_at, source_rows, inserted_rows, indexed_clips,
                      snapshot_sha256
               FROM sync_runs WHERE status='ok'
               ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        pending = db.execute(
            "SELECT count(*) FROM review_queue WHERE confidence BETWEEN ? AND 1.0",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone()[0]
        reviewed = db.execute(
            """SELECT count(*) FROM reviews r JOIN detections d USING(detection_id)
               WHERE d.confidence BETWEEN ? AND 1.0
                 AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone()[0]
    last_7 = int(recent["last_7"] or 0)
    previous_7 = int(recent["previous_7"] or 0)
    delta = None if previous_7 == 0 else round((last_7 - previous_7) / previous_7 * 100, 1)
    return {
        "totals": totals,
        "today": {**today_row, "date": today},
        "trend": {"last_7": last_7, "previous_7": previous_7, "percent_change": delta},
        "review": {"pending": pending, "reviewed": reviewed},
        "latest_sync": dict(sync) if sync else None,
        "policy": {
            "name": "publication-v1",
            "description": "Accepted BirdNET detections for the local calendar day",
            "minimum_confidence": PUBLICATION_MIN_CONFIDENCE,
            "timezone": "America/New_York",
            "frame_parity": True,
        },
        "generated_at": now_local().isoformat(timespec="seconds"),
    }


def get_today(db_path: Path) -> dict[str, Any]:
    today = now_local().date().isoformat()
    with db_connect(db_path) as db:
        rows = rows_dict(db.execute(
            """SELECT d.scientific_name,
                      (SELECT d2.common_name FROM detections d2
                       WHERE d2.scientific_name=d.scientific_name
                         AND d2.confidence BETWEEN ? AND 1.0
                       ORDER BY d2.observed_at_local DESC, d2.detection_id DESC
                       LIMIT 1) AS common_name,
                      count(*) AS detections,
                      min(d.time) AS first_heard, max(d.time) AS last_heard,
                      max(d.confidence) AS best_confidence,
                      avg(d.confidence) AS mean_confidence,
                      sum(d.audio_sha256 IS NOT NULL) AS clips_preserved
               FROM detections d WHERE d.date=? AND d.confidence BETWEEN ? AND 1.0
               GROUP BY d.scientific_name
               ORDER BY detections DESC, common_name""",
            (PUBLICATION_MIN_CONFIDENCE, today, PUBLICATION_MIN_CONFIDENCE),
        ))
    standing_by_species = {
        item["scientific_name"]: item["standing"]
        for item in get_species(db_path)["species"]
    }
    for row in rows:
        row["slug"] = canonical_species_slug(row["scientific_name"])
        row["art_slug"] = slugify(row["scientific_name"])
        row["standing"] = standing_by_species.get(
            row["scientific_name"], "uncorroborated"
        )
    return {
        "date": today,
        "timezone": "America/New_York",
        "publication_policy": "publication-v1",
        "review_policy_version": PUBLIC_REVIEW_POLICY_VERSION,
        "species": rows,
    }


def get_activity(db_path: Path, days: int) -> dict[str, Any]:
    days = max(7, min(days, 730))
    start = (now_local().date() - dt.timedelta(days=days - 1)).isoformat()
    with db_connect(db_path) as db:
        daily = rows_dict(db.execute(
            """SELECT date, count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      max(confidence) AS best_confidence
               FROM detections WHERE date>=? AND confidence BETWEEN ? AND 1.0
               GROUP BY date ORDER BY date""",
            (start, PUBLICATION_MIN_CONFIDENCE),
        ))
        hourly = rows_dict(db.execute(
            """SELECT CAST(substr(time,1,2) AS INTEGER) AS hour,
                      count(*) AS detections
               FROM detections WHERE date>=? AND confidence BETWEEN ? AND 1.0
               GROUP BY hour ORDER BY hour""",
            (start, PUBLICATION_MIN_CONFIDENCE),
        ))
    return {"days": days, "start": start, "daily": daily, "by_hour": hourly}


def get_species(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        species = rows_dict(db.execute(
            f"""SELECT d.scientific_name,
                      (SELECT d2.common_name FROM detections d2
                       WHERE d2.scientific_name=d.scientific_name
                         AND d2.confidence BETWEEN ? AND 1.0
                       ORDER BY d2.observed_at_local DESC, d2.detection_id DESC
                       LIMIT 1) AS common_name,
                      count(*) AS detections,
                      count(DISTINCT d.date) AS days_heard,
                      min(d.observed_at_local) AS first_heard,
                      max(d.observed_at_local) AS last_heard,
                      max(d.confidence) AS best_confidence,
                      avg(d.confidence) AS mean_confidence,
                      sum(CASE WHEN i.outcome='corroborated' THEN 1 ELSE 0 END)
                        AS corroborated,
                      sum(CASE WHEN i.outcome='uncorroborated' THEN 1 ELSE 0 END)
                        AS uncorroborated,
                      sum(CASE WHEN i.outcome='model_conflict' THEN 1 ELSE 0 END)
                        AS model_conflict,
                      sum(CASE WHEN i.outcome IS NULL AND
                          (r.detection_id IS NOT NULL OR d.confidence < 0.90)
                          THEN 1 ELSE 0 END) AS pending,
                      sum(CASE WHEN i.outcome IS NULL AND r.detection_id IS NULL
                          AND d.confidence >= 0.90 THEN 1 ELSE 0 END) AS unreviewed
               FROM detections d
               LEFT JOIN reviews r ON r.detection_id=d.detection_id
                 AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)
               {REVIEW_INTERPRETATION_JOIN_SQL}
               WHERE d.confidence BETWEEN ? AND 1.0
               GROUP BY d.scientific_name
               ORDER BY days_heard DESC, detections DESC, common_name""",
            (PUBLICATION_MIN_CONFIDENCE, PUBLICATION_MIN_CONFIDENCE),
        ))
    for row in species:
        counts = {
            outcome: int(row.pop(outcome) or 0)
            for outcome in (
                "corroborated", "uncorroborated", "model_conflict",
                "pending", "unreviewed",
            )
        }
        row["standing"] = (
            "corroborated" if counts["corroborated"] else "uncorroborated"
        )
        row["review_counts"] = counts
        row["slug"] = canonical_species_slug(row["scientific_name"])
        row["art_slug"] = slugify(row["scientific_name"])
    return {
        "species": species,
        "policy_version": PUBLIC_REVIEW_POLICY_VERSION,
    }


def get_cards(db_path: Path, catalogue: dict[str, Any]) -> dict[str, Any]:
    cards = join_bird_cards(get_species(db_path)["species"], catalogue)
    return {
        "cards": cards,
        "rarity_method": "garden-days-then-recognitions-v1",
        "rarity_description": (
            "Fewest distinct days heard, then fewest recognitions, then most "
            "recently first heard. This is rarity in this archive, not global rarity."
        ),
        "catalogue_version": catalogue["catalogue_version"],
        "review": catalogue["review"],
    }


def get_species_detail(
    db_path: Path, slug: str, catalogue: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    if not SAFE_SLUG.fullmatch(slug):
        raise ValueError("invalid species slug")
    with db_connect(db_path) as db:
        published = db.execute(
            """SELECT d.scientific_name,
                      (SELECT d2.common_name FROM detections d2
                       WHERE d2.scientific_name=d.scientific_name
                         AND d2.confidence BETWEEN ? AND 1.0
                       ORDER BY d2.observed_at_local DESC, d2.detection_id DESC
                       LIMIT 1) AS common_name
               FROM detections d WHERE d.confidence BETWEEN ? AND 1.0
               GROUP BY d.scientific_name""",
            (PUBLICATION_MIN_CONFIDENCE, PUBLICATION_MIN_CONFIDENCE),
        )
        published = list(published)
        canonical_matches = [
            row for row in published
            if canonical_species_slug(row["scientific_name"]) == slug
        ]
        if canonical_matches:
            species = canonical_matches[0]
        else:
            legacy_matches = [
                row for row in published if slugify(row["scientific_name"]) == slug
            ]
            if len(legacy_matches) > 1:
                raise SpeciesSlugConflict("ambiguous legacy species slug")
            species = legacy_matches[0] if legacy_matches else None
        if species is None:
            return None
        row = dict(db.execute(
            f"""SELECT count(*) AS detections,
                      count(DISTINCT d.date) AS days_heard,
                      min(d.observed_at_local) AS first_heard,
                      max(d.observed_at_local) AS last_heard,
                      max(d.confidence) AS best_confidence,
                      avg(d.confidence) AS mean_confidence,
                      sum(d.audio_sha256 IS NOT NULL) AS clips_preserved,
                      sum(CASE WHEN i.outcome='corroborated' THEN 1 ELSE 0 END)
                        AS corroborated,
                      sum(CASE WHEN i.outcome='uncorroborated' THEN 1 ELSE 0 END)
                        AS uncorroborated,
                      sum(CASE WHEN i.outcome='model_conflict' THEN 1 ELSE 0 END)
                        AS model_conflict,
                      sum(CASE WHEN i.outcome IS NULL AND
                          (r.detection_id IS NOT NULL OR d.confidence < 0.90)
                          THEN 1 ELSE 0 END) AS pending,
                      sum(CASE WHEN i.outcome IS NULL AND r.detection_id IS NULL
                          AND d.confidence >= 0.90 THEN 1 ELSE 0 END) AS unreviewed
               FROM detections d
               LEFT JOIN reviews r ON r.detection_id=d.detection_id
                 AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)
               {REVIEW_INTERPRETATION_JOIN_SQL}
               WHERE d.scientific_name=? AND d.confidence BETWEEN ? AND 1.0""",
            (species["scientific_name"], PUBLICATION_MIN_CONFIDENCE),
        ).fetchone())
    sanitize_public_scores(row)
    review_counts = {
        status: int(row.pop(status) or 0)
        for status in (
            "corroborated", "uncorroborated", "model_conflict",
            "pending", "unreviewed",
        )
    }
    result = {
        "scientific_name": species["scientific_name"],
        "common_name": species["common_name"],
        "slug": canonical_species_slug(species["scientific_name"]),
        "art_slug": slugify(species["scientific_name"]),
        **row,
        "standing": (
            "corroborated" if review_counts["corroborated"] else "uncorroborated"
        ),
        "review_policy_version": PUBLIC_REVIEW_POLICY_VERSION,
        "review_counts": review_counts,
    }
    if catalogue is not None:
        result["bird_card"] = bird_card_for_species(
            species["scientific_name"], catalogue,
        )
    return result


def get_seasonality(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        rows = rows_dict(db.execute(
            """SELECT CAST(strftime('%m',d.date) AS INTEGER) AS calendar_month,
                      d.scientific_name,
                      (SELECT d2.common_name FROM detections d2
                       WHERE d2.scientific_name=d.scientific_name
                         AND d2.confidence BETWEEN ? AND 1.0
                       ORDER BY d2.observed_at_local DESC, d2.detection_id DESC
                       LIMIT 1) AS common_name,
                      count(*) AS detections,
                      count(DISTINCT d.date) AS days_heard,
                      count(DISTINCT substr(d.date,1,4)) AS years_observed
               FROM detections d WHERE d.confidence BETWEEN ? AND 1.0
               GROUP BY calendar_month,d.scientific_name
               ORDER BY d.scientific_name,calendar_month""",
            (PUBLICATION_MIN_CONFIDENCE, PUBLICATION_MIN_CONFIDENCE),
        ))
        monthly = rows_dict(db.execute(
            """SELECT substr(date,1,7) AS month, count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      count(DISTINCT date) AS listening_days
               FROM detections WHERE confidence BETWEEN ? AND 1.0
               GROUP BY substr(date,1,7) ORDER BY month""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ))
    return {"by_species_month": rows, "timeline": monthly}


def get_epochs(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        epochs = rows_dict(db.execute(
            """SELECT effective_at, audio_model, range_model,
                      occurrence_threshold, confidence_threshold,
                      sensitivity, overlap, reason
               FROM configuration_epochs ORDER BY effective_at"""
        ))
    for epoch in epochs:
        epoch["audio_model"] = public_model_label(epoch.get("audio_model"))
        epoch["range_model"] = public_model_label(epoch.get("range_model"))
        epoch["reason"] = None
    return {"epochs": epochs}


def get_detections(
    db_path: Path,
    params: dict[str, list[str]],
    audio_root: Optional[Path] = None,
) -> dict[str, Any]:
    def one(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0].strip()

    try:
        limit = max(1, min(int(one("limit", "75")), 200))
        offset = int(one("offset", "0"))
        confidence_value = float(one("confidence_min", str(PUBLICATION_MIN_CONFIDENCE)))
        if offset < 0 or offset > MAX_OFFSET or not math.isfinite(confidence_value):
            raise ValueError
        confidence_min = max(
            PUBLICATION_MIN_CONFIDENCE,
            min(confidence_value, 1.0),
        )
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid numeric filter") from exc
    conditions = [
        "d.confidence BETWEEN ? AND 1.0",
        "length(d.detection_id) = 64",
        "d.detection_id NOT GLOB '*[^0-9a-f]*'",
    ]
    values: list[Any] = [confidence_min]
    species = one("species")
    detection_id = one("detection_id")
    date_from, date_to = one("date_from"), one("date_to")
    review = one("review")
    query = one("q")
    if detection_id:
        if not re.fullmatch(r"[0-9a-f]{64}", detection_id):
            raise ValueError("invalid detection_id")
        conditions.append("d.detection_id = ?")
        values.append(detection_id)
    if species:
        conditions.append("d.scientific_name = ?")
        values.append(species)
    for label, value, op in (("date_from", date_from, ">="), ("date_to", date_to, "<=")):
        if value:
            try:
                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"invalid {label}") from exc
            conditions.append(f"d.date {op} ?")
            values.append(value)
    if query:
        conditions.append("(d.common_name LIKE ? OR d.scientific_name LIKE ?)")
        like = "%" + query[:80].replace("%", "") + "%"
        values += [like, like]
    status_expr = REVIEW_STATUS_SQL
    if review:
        if review not in {
            "pending", "unreviewed", "corroborated",
            "uncorroborated", "model_conflict",
        }:
            raise ValueError("invalid review filter")
        conditions.append(status_expr + " = ?")
        values.append(review)
    where = " AND ".join(conditions)
    with db_connect(db_path) as db:
        total = db.execute(
            f"""SELECT count(*) FROM detections d
                LEFT JOIN reviews r ON r.detection_id=d.detection_id
                  AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)
                {REVIEW_INTERPRETATION_JOIN_SQL}
                WHERE {where}""",
            values,
        ).fetchone()[0]
        result = rows_dict(db.execute(
            f"""SELECT d.detection_id, d.date, d.time, d.observed_at_local,
                       d.timezone, d.scientific_name, d.common_name, d.confidence,
                       d.audio_relpath, d.audio_sha256, d.audio_bytes,
                       {status_expr} AS review_status,
                       r.review_model, r.review_score, r.notes,
                       i.policy_version AS review_policy_version,
                       i.claim_score AS review_interpretation_claim_score,
                       i.claim_rank AS review_rank,
                       i.label_count AS review_label_count,
                       i.top_label AS review_top_label,
                       i.top_score AS review_top_score,
                       i.top_score_provenance AS review_top_score_provenance,
                       q.algorithm_version AS quality_algorithm,
                       q.sample_rate AS quality_sample_rate,
                       q.duration_seconds AS quality_duration_seconds,
                       q.noise_floor_dbfs AS quality_noise_floor_dbfs,
                       q.signal_level_dbfs AS quality_signal_level_dbfs,
                       q.signal_contrast_db AS quality_signal_contrast_db,
                       q.clipping_fraction AS quality_clipping_fraction
                FROM detections d
                LEFT JOIN reviews r
                  ON r.detection_id=d.detection_id
                 AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)
                {REVIEW_INTERPRETATION_JOIN_SQL}
                LEFT JOIN audio_quality q
                  ON q.detection_id=d.detection_id AND q.algorithm_version=?
                WHERE {where}
                ORDER BY d.date DESC, d.time DESC, d.detection_id DESC
                LIMIT ? OFFSET ?""",
            [PUBLIC_AUDIO_QUALITY_ALGORITHM] + values + [limit, offset],
        ))
    prepare_detection_rows(result, audio_root)
    return {"total": total, "limit": limit, "offset": offset, "detections": result}


def prepare_detection_rows(
    rows: list[dict[str, Any]],
    audio_root: Optional[Path],
) -> None:
    """Apply the explicit public DTO boundary to detection query rows in place."""
    for row in rows:
        relpath = row.pop("audio_relpath", None)
        row["confidence"] = public_score(row.get("confidence"))
        row["review_score"] = public_score(row.get("review_score"))
        policy = row.get("review_policy_version")
        interpretation_claim_score = public_score(
            row.pop("review_interpretation_claim_score", None)
        )
        rank = public_positive_int(row.get("review_rank"), 100_000)
        label_count = public_positive_int(row.get("review_label_count"), 100_000)
        top_score = public_score(row.get("review_top_score"))
        top_label = row.get("review_top_label")
        provenance = row.get("review_top_score_provenance")
        valid_interpretation = (
            policy == PUBLIC_REVIEW_POLICY_VERSION
            and interpretation_claim_score == row["review_score"]
            and corroboration.valid_interpretation(
                row.get("review_status"), interpretation_claim_score, rank,
                label_count, top_label, top_score, provenance, pinned_perch_taxa(),
            )
        )
        if valid_interpretation:
            row["review_rank"] = rank
            row["review_label_count"] = label_count
            row["review_top_score"] = top_score
        else:
            if row.get("review_status") in PUBLIC_REVIEW_OUTCOMES:
                row["review_status"] = "pending"
            row["review_policy_version"] = None
            row["review_rank"] = None
            row["review_label_count"] = None
            row["review_top_label"] = None
            row["review_top_score"] = None
            row["review_top_score_provenance"] = None
        row["timezone"] = public_timezone(row.get("timezone"))
        row["audio_bytes"] = public_positive_int(
            row.get("audio_bytes"), MAX_PUBLIC_AUDIO_BYTES,
        )
        if row["audio_bytes"] is None:
            row["audio_sha256"] = None
        archived = bool(row.get("audio_sha256") and row["audio_bytes"] is not None)
        if archived and relpath and audio_root is not None:
            root = audio_root.resolve()
            candidate = (root / relpath).resolve()
            row["has_audio"] = root in candidate.parents and candidate.is_file()
        else:
            row["has_audio"] = archived
        row["audio_quality"] = public_audio_quality(row)
        raw_review_model = row.get("review_model")
        row["review_kind"] = public_review_kind(raw_review_model)
        row["review_model"] = public_review_model(raw_review_model)
        row["notes"] = (
            public_review_notes(row.get("notes"))
            if raw_review_model == PUBLIC_REVIEW_MODEL else None
        )
        row["slug"] = canonical_species_slug(row["scientific_name"])
        row["art_slug"] = slugify(row["scientific_name"])


def get_detection(
    db_path: Path,
    detection_id: str,
    audio_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    if not HEX_ID.fullmatch(detection_id):
        raise ValueError("invalid detection id")
    payload = get_detections(
        db_path,
        {"detection_id": [detection_id], "limit": ["1"]},
        audio_root,
    )
    if not payload["detections"]:
        return None
    detection = payload["detections"][0]
    ordering = (detection["date"], detection["time"], detection_id)
    with db_connect(db_path) as db:
        neighbours = {}
        for label, operator, direction in (
            ("newer_detection_id", ">", "ASC"),
            ("older_detection_id", "<", "DESC"),
        ):
            row = db.execute(
                f"""SELECT detection_id FROM detections
                    WHERE scientific_name=? AND confidence BETWEEN ? AND 1.0
                      AND length(detection_id)=64
                      AND detection_id NOT GLOB '*[^0-9a-f]*'
                      AND (date,time,detection_id) {operator} (?,?,?)
                    ORDER BY date {direction}, time {direction},
                             detection_id {direction} LIMIT 1""",
                (detection["scientific_name"], PUBLICATION_MIN_CONFIDENCE, *ordering),
            ).fetchone()
            neighbours[label] = row["detection_id"] if row else None
    detection.update(neighbours)
    return detection


def _descending_text(value: Any) -> tuple[int, ...]:
    return tuple(-ord(character) for character in str(value or ""))


def _descending_number(value: Any) -> tuple[bool, float]:
    return value is None, -float(value) if value is not None else 0.0


def related_sort_key(row: dict[str, Any], sort: str) -> tuple[Any, ...]:
    status = str(row.get("review_status") or "")
    status_rank = {
        "corroborated": 0, "uncorroborated": 1,
        "pending": 2, "unreviewed": 2, "model_conflict": 3,
    }.get(status, 3)
    recency = (
        _descending_text(row.get("date")),
        _descending_text(row.get("time")),
        _descending_text(row.get("detection_id")),
    )
    review_score = _descending_number(row.get("review_score"))
    confidence = _descending_number(row.get("confidence"))
    contrast = _descending_number(
        (row.get("audio_quality") or {}).get("audio_contrast_db")
    )
    if sort == "best":
        return (status_rank, *review_score, *confidence, *contrast, *recency)
    if sort == "contrast":
        return (*contrast, status_rank, *review_score, *confidence, *recency)
    return recency


def get_related_detections(
    db_path: Path,
    detection_id: str,
    params: dict[str, list[str]],
    audio_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Return a bounded, explicitly ranked same-species listening queue."""
    if not HEX_ID.fullmatch(detection_id):
        raise ValueError("invalid detection id")
    allowed_params = {"sort", "limit", "offset", "revision"}
    if set(params) - allowed_params or any(len(values) != 1 for values in params.values()):
        raise ValueError("invalid related filter")

    sort = (params.get("sort") or ["best"])[0].strip()
    if sort not in {"best", "contrast", "recent"}:
        raise ValueError("invalid related sort")
    expected_revision = (params.get("revision") or [None])[0]
    if expected_revision is not None and not HEX_ID.fullmatch(expected_revision):
        raise ValueError("invalid related revision")
    try:
        limit = int((params.get("limit") or ["12"])[0])
        offset = int((params.get("offset") or ["0"])[0])
        if limit < 1 or limit > 12 or offset < 0 or offset > MAX_OFFSET:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid related pagination") from exc

    status_expr = REVIEW_STATUS_SQL

    with db_connect(db_path) as db:
        current = db.execute(
            """SELECT scientific_name,common_name FROM detections
               WHERE detection_id=? AND confidence BETWEEN ? AND 1.0""",
            (detection_id, PUBLICATION_MIN_CONFIDENCE),
        ).fetchone()
        if current is None:
            return None
        where_values = (
            current["scientific_name"], detection_id, PUBLICATION_MIN_CONFIDENCE,
        )
        rows = rows_dict(db.execute(
            f"""SELECT d.detection_id,d.date,d.time,d.observed_at_local,d.timezone,
                       d.scientific_name,d.common_name,d.confidence,
                       d.audio_relpath,d.audio_sha256,d.audio_bytes,
                       {status_expr} AS review_status,
                       r.review_model,r.review_score,r.notes,
                       i.policy_version AS review_policy_version,
                       i.claim_score AS review_interpretation_claim_score,
                       i.claim_rank AS review_rank,
                       i.label_count AS review_label_count,
                       i.top_label AS review_top_label,
                       i.top_score AS review_top_score,
                       i.top_score_provenance AS review_top_score_provenance,
                       q.algorithm_version AS quality_algorithm,
                       q.sample_rate AS quality_sample_rate,
                       q.duration_seconds AS quality_duration_seconds,
                       q.noise_floor_dbfs AS quality_noise_floor_dbfs,
                       q.signal_level_dbfs AS quality_signal_level_dbfs,
                       q.signal_contrast_db AS quality_signal_contrast_db,
                       q.clipping_fraction AS quality_clipping_fraction
                FROM detections d
                LEFT JOIN reviews r
                  ON r.detection_id=d.detection_id
                 AND (r.review_score IS NULL OR r.review_score BETWEEN 0.0 AND 1.0)
                {REVIEW_INTERPRETATION_JOIN_SQL}
                LEFT JOIN audio_quality q
                  ON q.detection_id=d.detection_id AND q.algorithm_version=?
                WHERE d.scientific_name=? AND d.detection_id<>?
                  AND d.confidence BETWEEN ? AND 1.0 AND d.audio_sha256 IS NOT NULL
                  AND length(d.detection_id)=64
                  AND d.detection_id NOT GLOB '*[^0-9a-f]*'
                LIMIT ?""",
            (PUBLIC_AUDIO_QUALITY_ALGORITHM, *where_values, MAX_RELATED_CANDIDATES + 1),
        ))
    if len(rows) > MAX_RELATED_CANDIDATES:
        raise RelatedQueueTooLarge("related call queue exceeds bounded work limit")
    prepare_detection_rows(rows, audio_root)
    playable_rows = [row for row in rows if row.get("has_audio")]
    playable_rows.sort(key=lambda row: related_sort_key(row, sort))
    revision_payload = [
        (
            row.get("detection_id"), row.get("review_status"),
            row.get("review_kind"), row.get("review_score"),
            row.get("confidence"),
            (row.get("audio_quality") or {}).get("audio_contrast_db"),
            row.get("date"), row.get("time"),
        )
        for row in playable_rows
    ]
    revision = hashlib.sha256(
        json.dumps(
            revision_payload, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    if expected_revision is not None and expected_revision != revision:
        raise RelatedQueueChanged("related call queue changed; refresh required")
    total = len(playable_rows)
    page_rows = playable_rows[offset:offset + limit]
    calls = [
        {
            key: row.get(key)
            for key in (
                "detection_id", "date", "time", "observed_at_local", "timezone",
                "scientific_name", "common_name", "confidence", "review_status",
                "review_model", "review_kind", "review_score", "has_audio",
                "audio_quality", "slug",
            )
        }
        for row in page_rows
    ]
    return {
        "species": {
            "scientific_name": current["scientific_name"],
            "common_name": current["common_name"],
            "slug": canonical_species_slug(current["scientific_name"]),
        },
        "sort": sort,
        "revision": revision,
        "total": total,
        "limit": limit,
        "offset": offset,
        "calls": calls,
    }


class ArchiveHandler(BaseHTTPRequestHandler):
    server_version = "ListeningGarden/1.0"

    @property
    def config(self) -> argparse.Namespace:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {format % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def error_json(self, message: str, status: int = 400) -> None:
        self.json_response({"error": message}, status)

    def range_not_satisfiable(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_file(self, path: Path, content_type: Optional[str] = None,
                   cache_control: str = "no-cache") -> None:
        if not path.is_file():
            self.error_json("not found", 404)
            return
        # Open before headers: a permission or media failure must produce one
        # coherent error response, not a partial 200 followed by a second reply.
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start, end = 0, size - 1
            partial = False
            range_header = self.headers.get("Range", "")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match or (not match.group(1) and not match.group(2)):
                    self.range_not_satisfiable(size)
                    return
                first, last = match.group(1), match.group(2)
                if not first and last:
                    start = max(0, size - int(last))
                    end = size - 1
                else:
                    if first:
                        start = int(first)
                    if last:
                        end = int(last)
                if start > end or start >= size:
                    self.range_not_satisfiable(size)
                    return
                end = min(end, size - 1)
                partial = True
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache_control)
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if "%" in parsed.path:
            self.error_json("invalid encoded path", 400)
            return
        path = unquote(parsed.path)
        if path == "/birds":
            path = "/"
        elif path.startswith("/birds/"):
            path = path[len("/birds"):]
        params = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/health":
                with db_connect(self.config.db) as db:
                    count = db.execute("SELECT count(*) FROM detections").fetchone()[0]
                    integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                self.json_response({
                    "status": "ok" if integrity == "ok" else "degraded",
                    "service": "listening-garden",
                    "detections": count,
                    "database": integrity,
                    "audio_mounted": self.config.audio.exists(),
                    "art_mounted": self.config.art.exists(),
                })
            elif path == "/api/summary":
                self.json_response(get_summary(self.config.db))
            elif path == "/api/today":
                self.json_response(get_today(self.config.db))
            elif path == "/api/activity":
                try:
                    days = int((params.get("days") or ["365"])[0])
                except ValueError:
                    raise ValueError("invalid days")
                self.json_response(get_activity(self.config.db, days))
            elif path == "/api/species":
                self.json_response(get_species(self.config.db))
            elif path == "/api/cards":
                catalogue = getattr(self.config, "bird_cards", None)
                if catalogue is None:
                    catalogue = load_bird_card_catalogue()
                self.json_response(get_cards(self.config.db, catalogue))
            elif re.fullmatch(r"/api/species/[a-z0-9]+(?:-[a-z0-9]+)*", path):
                catalogue = getattr(self.config, "bird_cards", None)
                if catalogue is None:
                    catalogue = load_bird_card_catalogue()
                species = get_species_detail(
                    self.config.db,
                    path[len("/api/species/"):],
                    catalogue,
                )
                if species is None:
                    self.error_json("species not found", 404)
                    return
                self.json_response({"species": species})
            elif re.fullmatch(r"/api/species/[^/]+", path):
                self.error_json("invalid species slug", 400)
            elif path == "/api/seasonality":
                self.json_response(get_seasonality(self.config.db))
            elif path == "/api/epochs":
                self.json_response(get_epochs(self.config.db))
            elif path == "/api/detections":
                self.json_response(get_detections(self.config.db, params, self.config.audio))
            elif re.fullmatch(r"/api/detections/[0-9a-f]{64}/related", path):
                did = path[len("/api/detections/"):-len("/related")]
                related = get_related_detections(
                    self.config.db, did, params, self.config.audio,
                )
                if related is None:
                    self.error_json("detection not found", 404)
                    return
                self.json_response(related)
            elif re.fullmatch(r"/api/detections/[0-9a-f]{64}", path):
                detection = get_detection(
                    self.config.db,
                    path[len("/api/detections/"):],
                    self.config.audio,
                )
                if detection is None:
                    self.error_json("detection not found", 404)
                    return
                self.json_response({"detection": detection})
            elif re.fullmatch(r"/api/detections/[^/]+", path):
                self.error_json("invalid detection id", 400)
            elif re.fullmatch(r"/api/audio/[0-9a-f]{64}", path):
                did = path[len("/api/audio/"):]
                with db_connect(self.config.db) as db:
                    row = db.execute(
                        """SELECT audio_relpath FROM detections
                           WHERE detection_id=? AND audio_sha256 IS NOT NULL
                             AND confidence BETWEEN ? AND 1.0""",
                        (did, PUBLICATION_MIN_CONFIDENCE),
                    ).fetchone()
                if not row:
                    self.error_json("audio unavailable", 404)
                    return
                root = self.config.audio.resolve()
                candidate = (root / row["audio_relpath"]).resolve()
                if root not in candidate.parents:
                    self.error_json("invalid archive path", 500)
                    return
                self.serve_file(candidate, "audio/mpeg", "private, max-age=86400")
            elif re.fullmatch(r"/api/audio/[^/]+", path):
                self.error_json("invalid detection id", 400)
            elif re.fullmatch(r"/art/[a-z0-9]+(?:-[a-z0-9]+)*\.png", path):
                name = path[len("/art/"):]
                self.serve_file(self.config.art / name, "image/png", "private, max-age=86400")
            elif re.fullmatch(r"/art/[^/]+", path):
                self.error_json("invalid illustration", 400)
            elif path.startswith("/api/"):
                self.error_json("API endpoint not found", 404)
            else:
                static_types = {
                    "/app.js": "application/javascript; charset=utf-8",
                    "/routes.js": "application/javascript; charset=utf-8",
                    "/styles.css": "text/css; charset=utf-8",
                    "/favicon.svg": "image/svg+xml",
                    "/favicon-32.png": "image/png",
                    "/apple-touch-icon.png": "image/png",
                }
                if path in static_types:
                    self.serve_file(
                        self.config.static / path.lstrip("/"),
                        static_types[path],
                        "no-cache, must-revalidate",
                    )
                elif is_frontend_route(path):
                    self.serve_file(
                        self.config.static / "index.html",
                        "text/html; charset=utf-8",
                        "no-cache, must-revalidate",
                    )
                else:
                    self.error_json("page not found", 404)
        except ValueError as exc:
            self.error_json(str(exc), 400)
        except SpeciesSlugConflict as exc:
            self.error_json(str(exc), 409)
        except RelatedQueueChanged as exc:
            self.error_json(str(exc), 409)
        except RelatedQueueTooLarge as exc:
            self.error_json(str(exc), 503)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            print(f"request failed: {exc}")
            self.error_json("archive temporarily unavailable", 503)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("BIRD_ARCHIVE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BIRD_ARCHIVE_PORT", "9137")))
    ap.add_argument("--db", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_DB",
        "~/Library/Application Support/AvianVisitorsArchive/detections.sqlite3",
    )).expanduser())
    ap.add_argument("--audio", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_AUDIO",
        "~/Library/Application Support/AvianVisitorsArchive/audio",
    )))
    ap.add_argument("--art", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_ART",
        "~/Library/Application Support/AvianVisitorsArchive/illustrations",
    )))
    ap.add_argument("--static", type=Path, default=here / "static")
    args = ap.parse_args(argv)
    args.db = args.db.expanduser().resolve()
    args.audio = args.audio.expanduser().resolve()
    args.art = args.art.expanduser().resolve()
    args.static = args.static.expanduser().resolve()
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.db.is_file():
        raise SystemExit(f"archive database not found: {args.db}")
    try:
        validate_public_archive_schema(args.db)
        args.bird_cards = load_bird_card_catalogue()
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise SystemExit(f"archive publication validation failed: {exc}") from exc
    server = ThreadingHTTPServer((args.host, args.port), ArchiveHandler)
    server.config = args  # type: ignore[attr-defined]
    print(f"Listening Garden on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
