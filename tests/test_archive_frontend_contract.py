import re
import json
from pathlib import Path
import subprocess


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
        "viewCards",
        "viewSpecies",
        "viewSpeciesDetail",
        "viewDetectionDetail",
        "viewAbout",
        "viewNotFound",
    ):
        assert f'id="{view_id}"' in html
    for href in ("/birds/", "/birds/explore", "/birds/cards", "/birds/species", "/birds/about"):
        assert f'href="{href}"' in html


def test_frontend_uses_real_species_and_detection_links():
    app = (STATIC / "app.js").read_text()
    assert 'Routes.href("species-detail"' in app
    assert 'Routes.href("detection-detail"' in app
    assert '<a class="bird-card' in app
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
    assert "Routes.pageCount" in app
    assert 'window.addEventListener("popstate"' in app
    assert "history.pushState" in app
    assert "history.replaceState" in app
    assert "initToday" in app
    assert "initExplore" in app
    assert "initCards" in app
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


def test_canonical_species_art_detection_navigation_and_safe_fragment_contracts():
    app = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert "bird.art_slug || bird.slug" in app
    assert "bird.slug || bird.art_slug" in app
    assert "route.slug !== bird.slug" in app
    assert 'id="newerDetection"' in html
    assert 'id="olderDetection"' in html
    assert "newer_detection_id" in app
    assert "older_detection_id" in app
    assert "Newer recognition" in html
    assert "Older recognition" in html
    assert 'href="/birds/species#speciesOccurrences"' in html
    assert 'href="#speciesOccurrences"' not in html


def run_app_behavior(assertions):
    app_path = STATIC / "app.js"
    script = f"""
const fs = require('fs'), vm = require('vm');
const elements = new Map();
function element(tag='DIV') {{ return {{ tagName: tag, innerHTML: '', textContent: '', value: '', hidden: false, href: '', classList: {{add(){{}}, remove(){{}}}}, addEventListener(){{}}, querySelectorAll(){{return []}}, hasAttribute(){{return true}}, setAttribute(){{}}, focus(){{}} }}; }}
const document = {{ querySelector(sel) {{ if (!elements.has(sel)) elements.set(sel, element(sel === '#ledgerRows' ? 'TBODY' : 'DIV')); return elements.get(sel); }}, querySelectorAll() {{ return []; }}, addEventListener() {{}} }};
const context = {{ console, process, element, URL, URLSearchParams, Intl, Date, setTimeout, clearTimeout, document, history: {{replaceState(){{}}, pushState(){{}}}}, location: {{pathname:'/birds/explore',search:'',hash:''}}, window: {{ListeningGardenRoutes: require({json.dumps(str(STATIC / 'routes.js'))}), location: {{pathname:'/birds/explore',search:'',hash:''}}, addEventListener(){{}}, setTimeout, requestAnimationFrame(fn){{fn()}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(app_path))}, 'utf8') + `\n(async () => {{ {assertions} }})().catch(error => {{ console.error(error); process.exitCode=1; }});`, context);
"""
    return subprocess.run(["node", "-e", script], capture_output=True, text=True)


def test_show_error_uses_semantic_child_markup_in_vm():
    result = run_app_behavior("""
const tbody = document.querySelector('#ledgerRows'); showError(tbody, '<unsafe>');
if (!tbody.innerHTML.startsWith('<tr><td colspan="5"')) throw new Error(tbody.innerHTML);
const list = element('OL'); showError(list, 'broken');
if (!list.innerHTML.startsWith('<li')) throw new Error(list.innerHTML);
const div = element('SECTION'); showError(div, 'broken');
if (!div.innerHTML.startsWith('<div')) throw new Error(div.innerHTML);
""")
    assert result.returncode == 0, result.stderr


def test_explore_restore_ignores_an_out_of_order_stale_response_in_vm():
    result = run_app_behavior("""
let resolvers = [];
fetchJSON = () => new Promise(resolve => resolvers.push(resolve));
window.location.search = '?q=first'; location.search = '?q=first'; const first = restoreExplore();
window.location.search = '?q=second'; location.search = '?q=second'; const second = restoreExplore();
resolvers[1]({detections:[], total:0}); await second;
resolvers[0]({detections:[{detection_id:'a'.repeat(64),slug:'robin',common_name:'Old',scientific_name:'Old',review_status:'pending'}], total:1}); await first;
if (document.querySelector('#ledgerRows').innerHTML.includes('Old')) throw new Error('stale response rendered');
if (state.explore.q !== 'second') throw new Error('stale state won');
""")
    assert result.returncode == 0, result.stderr


def test_detection_view_has_exact_taxon_art_audio_and_species_occurrences():
    html = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()
    assert 'id="detectionArtwork"' in html
    assert 'id="detectionArt"' in html
    assert 'art.src = artURL(item.art_slug)' in app
    assert 'makeIllustrationsSelfHealing(artwork)' in app
    assert 'Field-guide illustration of ${item.common_name}' in app
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


def test_species_index_and_evidence_make_uncorroborated_claims_visually_explicit():
    html = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()
    styles = (STATIC / "styles.css").read_text()
    assert 'id="corroboratedSpeciesIndex"' in html
    assert 'id="uncorroboratedSpeciesIndex"' in html
    assert "Corroborated species" in html
    assert "Uncorroborated candidates" in html
    assert 'bird.standing === "corroborated"' in app
    assert 'bird.standing !== "corroborated"' in app
    assert 'bird.days_heard === 1 ? "day" : "days"' in app
    assert 'bird.detections === 1 ? "recognition" : "recognitions"' in app
    assert "model_conflict" in app
    assert "rank #${formatNumber(item.review_rank)}" in app
    assert "model score—not probability" in app
    assert "alternative score reconstructed from stored 0.1%-rounded notes" in app
    assert ".candidate-species-section" in styles
    assert ".review-callout.model_conflict" in styles
    assert ".review-callout.uncorroborated" in styles


def test_field_cards_are_source_grounded_accessible_and_keep_candidates_visible():
    html = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()
    styles = (STATIC / "styles.css").read_text()
    assert 'id="birdCardGallery"' in html
    assert 'id="speciesFieldCard"' in html
    assert "Garden rarity" in html
    assert "rarest in this archive" in html.lower()
    assert "Illustrated natural-history cards for every" not in html
    assert 'fetchJSON("api/cards")' in app
    assert "renderBirdCard" in app
    assert "research pending" in app.lower()
    assert "voice-first species" in app.lower()
    assert "illustration-missing" in app
    assert "garden_rarity_rank" in app
    assert "independently reviewed by" in app.lower()
    assert "fact-checked by" not in app.lower()
    assert "bird.standing" in app
    assert 'rel="noopener noreferrer"' in app
    assert "escapeHTML(source.url)" in app
    assert "sourceSectionLabel" in app
    assert "identification & measurements" in app
    assert 'sounds: "sounds & calls"' in app
    assert ".field-card-grid" in styles
    assert ".field-card" in styles
    assert "@media(max-width:650px)" in styles
    assert ".field-card-grid{grid-template-columns:1fr}" in styles
