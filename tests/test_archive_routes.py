import json
from pathlib import Path
import subprocess


ROUTES = Path(__file__).parents[1] / "avian/archive/site/static/routes.js"


def run_routes(expression):
    script = f"const routes=require({json.dumps(str(ROUTES))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_parse_route_supports_public_and_direct_paths():
    cases = {
        "/birds/": {"name": "today"},
        "/birds/index.html": {"name": "today"},
        "/index.html": {"name": "today"},
        "/": {"name": "today"},
        "/birds/explore": {"name": "explore"},
        "/birds/species": {"name": "species-index"},
        "/birds/species/turdus-migratorius": {
            "name": "species-detail",
            "slug": "turdus-migratorius",
        },
        "/birds/detection/" + "a" * 64: {
            "name": "detection-detail",
            "detectionId": "a" * 64,
        },
        "/birds/about": {"name": "about"},
        "/birds/not-real": {"name": "not-found"},
        "/birds/detection/not-safe": {"name": "not-found"},
        "/birds/species/not_safe": {"name": "not-found"},
    }
    for pathname, expected in cases.items():
        assert run_routes(f"routes.parseRoute({json.dumps(pathname)})") == expected


def test_route_grammar_allows_only_one_optional_trailing_slash():
    for pathname in (
        "/birds/index.html/",
        "/index.html/",
        "/birds//explore",
        "/birds/explore//",
        "/birds/species//turdus-migratorius",
        "/birds/species/turdus-migratorius//",
    ):
        assert run_routes(f"routes.parseRoute({json.dumps(pathname)})") == {
            "name": "not-found"
        }


def test_canonical_hrefs_are_safe_and_prefixed():
    assert run_routes("routes.href('today')") == "/birds/"
    assert run_routes("routes.href('explore')") == "/birds/explore"
    assert run_routes("routes.href('species-index')") == "/birds/species"
    assert run_routes("routes.href('species-detail',{slug:'corvus'})") == (
        "/birds/species/corvus"
    )
    assert run_routes("routes.href('species-detail',{slug:'turdus-migratorius'})") == (
        "/birds/species/turdus-migratorius"
    )
    assert run_routes("routes.href('detection-detail',{detectionId:'" + "a" * 64 + "'})") == (
        "/birds/detection/" + "a" * 64
    )
    for expression in (
        "routes.href('species-detail',{slug:'../etc'})",
        "routes.href('detection-detail',{detectionId:'not-safe'})",
        "routes.href('unknown')",
    ):
        completed = subprocess.run(
            ["node", "-e", f"const routes=require({json.dumps(str(ROUTES))}); {expression}"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0


def test_legacy_detection_queries_redirect_only_from_the_app_root():
    detection_id = "a" * 64
    assert run_routes(
        f"routes.legacyDetectionHref('/birds/', '?detection={detection_id}')"
    ) == f"/birds/detection/{detection_id}"
    assert run_routes(
        f"routes.legacyDetectionHref('/', '?detection={detection_id}')"
    ) == f"/birds/detection/{detection_id}"
    assert run_routes(
        f"routes.legacyDetectionHref('/birds/explore', '?detection={detection_id}')"
    ) is None
    assert run_routes(
        "routes.legacyDetectionHref('/birds/', '?detection=not-an-id')"
    ) is None


def test_explore_state_round_trips_and_clamps_page():
    parsed = run_routes(
        "routes.parseExplore('?q=Robin&species=Turdus%20migratorius&date_from=2026-07-18"
        "&confidence_min=0.8&review=confirmed&page=3&ignored=secret')"
    )
    assert parsed == {
        "q": "Robin",
        "species": "Turdus migratorius",
        "date_from": "2026-07-18",
        "confidence_min": "0.8",
        "review": "confirmed",
        "page": 3,
    }
    assert run_routes("routes.parseExplore('?page=-9&review=evil&confidence_min=0.42')") == {
        "q": "",
        "species": "",
        "date_from": "",
        "confidence_min": "",
        "review": "",
        "page": 1,
    }
    for review in ("pending", "unreviewed", "confirmed", "uncertain", "rejected"):
        assert run_routes(f"routes.parseExplore('?review={review}')")["review"] == review
    for confidence in ("0", "0.8", "0.9"):
        assert run_routes(
            f"routes.parseExplore('?confidence_min={confidence}')"
        )["confidence_min"] == confidence
    query = run_routes(
        "routes.exploreSearch({q:'Robin',species:'Turdus migratorius',date_from:'2026-07-18',"
        "confidence_min:'0.8',review:'confirmed',page:3})"
    )
    assert query == (
        "?q=Robin&species=Turdus+migratorius&date_from=2026-07-18"
        "&confidence_min=0.8&review=confirmed&page=3"
    )


def test_page_parsing_and_serialization_are_bounded_by_server_offset_limit():
    assert run_routes("routes.PAGE_SIZE") == 50
    assert run_routes("routes.MAX_PAGE") == 20001
    assert run_routes("routes.safePage('999999999')") == 20001
    assert run_routes("routes.safePage('0')") == 1
    assert run_routes("routes.parseExplore('?page=999999999').page") == 20001
    assert run_routes("routes.exploreSearch({page:999999999})") == "?page=20001"
    assert run_routes("routes.pageCount(0)") == 1
    assert run_routes("routes.pageCount(86)") == 2
    assert run_routes("routes.pageCount(999999999)") == 20001
