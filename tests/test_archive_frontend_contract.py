import re
from pathlib import Path


STATIC = Path(__file__).parents[1] / "avian/archive/site/static"


def test_shell_declares_task_led_route_views_and_canonical_navigation():
    html = (STATIC / "index.html").read_text()
    assert '<base href="/birds/">' in html
    assert '<script src="routes.js"' in html
    assert '<span aria-hidden="true">聴</span>' in html
    assert 'aria-label="The Listening Garden home"' not in html
    for view_id in (
        "viewToday",
        "viewExplore",
        "viewSpecies",
        "viewSpeciesDetail",
        "viewDetectionDetail",
        "viewAbout",
        "viewNotFound",
    ):
        assert f'id="{view_id}"' in html
    for href in ("/birds/", "/birds/explore", "/birds/species", "/birds/about"):
        assert f'href="{href}"' in html


def test_frontend_uses_real_species_and_detection_links():
    app = (STATIC / "app.js").read_text()
    assert 'Routes.href("species-detail"' in app
    assert 'Routes.href("detection-detail"' in app
    assert '<a class="bird-card"' in app
    assert '<a class="species-row"' in app
    assert 'class="ledger-bird-link"' in app
    assert 'class="evidence-link"' in app
    assert "#evidencePanel" not in app


def test_frontend_loads_only_the_current_route_and_restores_explore_state():
    app = (STATIC / "app.js").read_text()
    assert "Routes.parseRoute(window.location.pathname)" in app
    assert "Routes.legacyDetectionHref" in app
    assert "window.location.replace(legacyHref)" in app
    assert "Routes.parseExplore(window.location.search)" in app
    assert 'window.addEventListener("popstate"' in app
    assert "history.pushState" in app
    assert "history.replaceState" not in app
    assert "initToday" in app
    assert "initExplore" in app
    assert "initSpeciesIndex" in app
    assert "initSpeciesDetail" in app
    assert "renderSpeciesPagination" in app
    assert 'instancesLink.href = `${Routes.href("species-detail"' in app
    assert "speciesHistoryHref" in app
    assert 'location.hash === "#speciesOccurrences"' in app
    assert "focusRouteDestination" in app
    assert "window.location.pathname" in app
    assert "initDetectionDetail" in app
    assert "initAbout" in app


def test_detection_view_is_audio_first_and_species_view_has_occurrences():
    html = (STATIC / "index.html").read_text()
    assert 'id="detectionAudioLabel"' in html
    assert 'aria-labelledby="detectionAudioLabel"' in html
    assert 'label for="detectionAudio"' not in html
    assert 'id="detectionAudio"' in html
    assert 'id="detectionTitle"' in html
    assert 'id="detectionSpeciesLink"' in html
    assert 'id="detectionHash"' in html
    assert 'id="speciesOccurrences"' in html
    assert 'id="speciesOccurrenceGrid"' in html
    assert 'aria-labelledby="speciesOccurrencesTitle"' in html
    assert 'id="skipLink"' in html
    assert 'id="speciesInstancesLink"' in html
    assert 'id="speciesReviewSummary"' in html
    assert 'id="speciesFirstHeard"' in html
    assert 'id="speciesLastHeard"' in html


def test_mobile_navigation_and_accessible_focus_styles_exist():
    html = (STATIC / "index.html").read_text()
    styles = (STATIC / "styles.css").read_text()
    assert 'class="mobile-nav"' in html
    assert 'aria-label="Primary navigation"' in html
    assert ":focus-visible" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ".route-view[hidden]" in styles
    assert re.search(r"(?:^|})\[hidden\]\{display:none!important\}", styles)
    assert ".table-wrap table{min-width:0" in styles
    assert ".table-wrap tbody tr{display:grid" in styles
    assert re.search(r"\.hero,\.route-header\{[^}]*overflow:hidden", styles)
