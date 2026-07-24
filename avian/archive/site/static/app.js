"use strict";

const Routes = window.ListeningGardenRoutes;
const ART_VERSION = "r2";
const ART_RETRY_DELAYS_MS = [5000, 30000, 300000];
const PAGE_SIZE = Routes.PAGE_SIZE;
const state = { summary: null, species: [], seasonality: [], explore: null };
let exploreRequestToken = 0;
let relatedRequestToken = 0;
let relatedAbortController = null;
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}
function formatNumber(value) { return new Intl.NumberFormat("en-US").format(Number(value || 0)); }
function formatConfidence(value) { return `${Math.round(Number(value || 0) * 100)}%`; }
function displayDate(value, short = false) {
  if (!value) return "—";
  const parts = String(value).slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some(part => !Number.isFinite(part))) return escapeHTML(value);
  return new Intl.DateTimeFormat("en-US", { month: short ? "short" : "long", day: "numeric", year: "numeric" }).format(new Date(parts[0], parts[1] - 1, parts[2]));
}
function displayTime(value) {
  if (!value) return "—";
  const parts = String(value).slice(0, 5).split(":").map(Number);
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(new Date(2000, 0, 1, parts[0], parts[1]));
}
async function fetchJSON(path, options = {}) {
  const response = await fetch(path, { headers: { Accept: "application/json" }, signal: options.signal });
  if (!response.ok) { let message = `${response.status}`; try { message = (await response.json()).error || message; } catch (_) { /* non-JSON error */ } const error = new Error(message); error.status = response.status; throw error; }
  return response.json();
}
function showToast(message) { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.classList.remove("show"), 4000); }
function showError(holder, message) { const content = `<strong>The record could not be opened.</strong><p>${escapeHTML(message)}</p>`; const tag = String(holder.tagName || "").toUpperCase(); holder.innerHTML = tag === "TBODY" ? `<tr><td colspan="5" class="error-state">${content}</td></tr>` : (tag === "OL" || tag === "UL") ? `<li class="error-state">${content}</li>` : `<div class="error-state">${content}</div>`; }
function artURL(slug) { return `art/${encodeURIComponent(String(slug || ""))}.png?v=${ART_VERSION}`; }
function speciesHistoryHref(slug) { return `${Routes.href("species-detail", { slug })}#speciesOccurrences`; }
function focusRouteDestination(element) {
  if (!element) return;
  if (!element.hasAttribute("tabindex")) element.setAttribute("tabindex", "-1");
  element.focus({ preventScroll: true });
}
function makeIllustrationsSelfHealing(root) {
  root.querySelectorAll("img").forEach(img => { let retries = 0; img.addEventListener("load", () => { img.style.visibility = "visible"; }); img.addEventListener("error", () => { img.style.visibility = "hidden"; if (retries >= ART_RETRY_DELAYS_MS.length) { img.alt += img.alt.endsWith("illustration unavailable") ? "" : " (illustration unavailable)"; return; } const delay = ART_RETRY_DELAYS_MS[retries++]; window.setTimeout(() => { const retry = new URL(img.src); retry.searchParams.set("retry", Date.now()); img.src = retry; }, delay); }); });
}

function setCurrentRoute(route) {
  const ids = { today: "viewToday", explore: "viewExplore", "species-index": "viewSpecies", "species-detail": "viewSpeciesDetail", "detection-detail": "viewDetectionDetail", about: "viewAbout", "not-found": "viewNotFound" };
  $$(".route-view").forEach(view => { view.hidden = view.id !== ids[route.name]; });
  document.body.dataset.route = route.name;
  $$("[data-route]").forEach(link => { const current = link.dataset.route === route.name || (route.name === "species-detail" && link.dataset.route === "species-index"); if (current) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
}
function renderSummary(summary) {
  state.summary = summary;
  ["Detections", "Species", "Days", "Clips"].forEach((name, index) => { const node = $(`#metric${name}`); if (node) node.textContent = formatNumber([summary.totals.detections, summary.totals.corroborated_species, summary.totals.days_listening, summary.totals.clips_preserved][index]); });
  const sync = summary.latest_sync; const status = $("#archiveStatus");
  if (!sync?.completed_at) { status.innerHTML = '<i class="pulse stale"></i><div><strong><span class="wide-status">Mirror status </span>unknown</strong><small>No successful sync recorded</small></div>'; return; }
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(sync.completed_at).getTime()) / 60000)); const stale = minutes > 30;
  status.innerHTML = `<i class="pulse ${stale ? "stale" : ""}" aria-hidden="true"></i><div><strong>${stale ? '<span class="wide-status">Mirror </span>needs attention' : '<span class="wide-status">Archive mirror </span>current'}</strong><small>Synced ${minutes ? `${formatNumber(minutes)} min` : "less than a minute"} ago · ${formatNumber(sync.source_rows)} source rows</small></div>`;
}
async function loadCommonStatus() { try { renderSummary(await fetchJSON("api/summary")); } catch (_) { $("#archiveStatus").innerHTML = '<i class="pulse stale"></i><div><strong><span class="wide-status">Archive </span>unavailable</strong><small>The private mirror could not be read</small></div>'; } }

function renderToday(data) {
  $("#todayDate").textContent = displayDate(data.date);
  const holder = $("#todayGrid");
  if (!data.species.length) { holder.innerHTML = '<p class="empty-state">The garden is listening. No published recognitions yet today.</p>'; return; }
  holder.innerHTML = data.species.map((bird, index) => { const slug = bird.slug || bird.art_slug; const artSlug = bird.art_slug || bird.slug; const standing = bird.standing === "corroborated" ? "corroborated" : "uncorroborated"; return `<a class="bird-card ${standing}" href="${escapeHTML(speciesHistoryHref(slug))}"><span class="bird-number">${String(index + 1).padStart(2, "0")}</span><span class="species-standing ${standing}">${standing}</span><img src="${escapeHTML(artURL(artSlug))}" alt="Illustration of ${escapeHTML(bird.common_name)}"><h2>${escapeHTML(bird.common_name)}</h2><em>${escapeHTML(bird.scientific_name)}</em><span class="bird-facts">${formatNumber(bird.detections)} recognition${Number(bird.detections) === 1 ? "" : "s"} · ${displayTime(bird.first_heard)}–${displayTime(bird.last_heard)}</span></a>`; }).join("");
  makeIllustrationsSelfHealing(holder);
}
async function initToday() { try { renderToday(await fetchJSON("api/today")); } catch (error) { showError($("#todayGrid"), error.message); } }

function populateSpeciesControls(species) { state.species = species; const options = species.map(bird => `<option value="${escapeHTML(bird.scientific_name)}">${escapeHTML(bird.common_name)}</option>`).join(""); $("#seasonSpecies").innerHTML = options || '<option value="">No species recorded</option>'; $("#filterSpecies").innerHTML = `<option value="">All species</option>${options}`; }
function dateRange(days, endKey) { const result = []; const parts = String(endKey || "").split("-").map(Number); const end = parts.length === 3 ? new Date(parts[0], parts[1] - 1, parts[2], 12) : new Date(); for (let offset = days - 1; offset >= 0; offset -= 1) { const date = new Date(end); date.setDate(end.getDate() - offset); result.push(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`); } return result; }
function renderActivity(data) { const daily = new Map(data.daily.map(row => [row.date, Number(row.detections)])); const max = Math.max(1, ...daily.values()); $("#activityHeatmap").innerHTML = dateRange(365, state.summary?.today?.date).map(date => { const count = daily.get(date) || 0; const level = count ? Math.max(1, Math.ceil(count / max * 4)) : 0; return `<i data-level="${level}" title="${escapeHTML(`${displayDate(date, true)}: ${count} recognitions`)}"></i>`; }).join(""); const hours = new Map(data.by_hour.map(row => [Number(row.hour), Number(row.detections)])); const high = Math.max(1, ...hours.values()); $("#hourChart").innerHTML = Array.from({ length: 24 }, (_, hour) => { const count = hours.get(hour) || 0; return `<i class="hour-bar" data-hour="${String(hour).padStart(2, "0")}" title="${count} recognitions" style="height:${count ? Math.max(3, count / high * 100) : 1}%"></i>`; }).join(""); }
function renderMonthChart(scientificName) { const rows = state.seasonality.filter(row => row.scientific_name === scientificName); const months = new Map(rows.map(row => [Number(row.calendar_month), Number(row.detections)])); const max = Math.max(1, ...months.values()); const names = ["J","F","M","A","M","J","J","A","S","O","N","D"]; $("#monthChart").innerHTML = names.map((name, index) => { const count = months.get(index + 1) || 0; return `<div><i style="height:${count ? Math.max(2, count / max * 130) : 1}px" data-count="${count || ""}"></i><span>${name}</span></div>`; }).join(""); const bird = state.species.find(item => item.scientific_name === scientificName); $("#seasonSpeciesNote").textContent = bird ? `${formatNumber(bird.detections)} recognitions across ${formatNumber(bird.days_heard)} days` : "Choose a species"; }
function applyExploreToForm(explore) { $("#filterQuery").value = explore.q; $("#filterSpecies").value = explore.species; $("#filterFrom").value = explore.date_from; $("#filterConfidence").value = explore.confidence_min; $("#filterReview").value = explore.review; }
function ledgerRow(item) { const detectionId = String(item.detection_id || ""); const speciesHref = speciesHistoryHref(item.slug); const detectionHref = Routes.href("detection-detail", { detectionId }); const status = ["pending","unreviewed","corroborated","uncorroborated","model_conflict"].includes(item.review_status) ? item.review_status : "unreviewed"; const label = status.replace("_", " "); return `<tr data-id="${escapeHTML(detectionId)}"><td><time datetime="${escapeHTML(item.observed_at_local)}">${displayDate(item.date, true)}<br>${displayTime(item.time)}</time></td><td><a class="ledger-bird-link" href="${escapeHTML(speciesHref)}"><strong>${escapeHTML(item.common_name)}</strong><em>${escapeHTML(item.scientific_name)}</em></a></td><td>${formatConfidence(item.confidence)}</td><td><span class="status-pill ${escapeHTML(status)}">${escapeHTML(label)}</span></td><td><a class="evidence-link" href="${escapeHTML(detectionHref)}">${item.has_audio ? "Inspect + listen" : "Inspect claim"}</a></td></tr>`; }
async function loadLedger(explore) { const offset = (explore.page - 1) * PAGE_SIZE; const params = new URLSearchParams({ limit: PAGE_SIZE, offset }); ["q","species","date_from","confidence_min","review"].forEach(key => { if (explore[key]) params.set(key, explore[key]); }); return fetchJSON(`api/detections?${params}`); }
function renderLedger(explore, data) { const offset = (explore.page - 1) * PAGE_SIZE; $("#ledgerRows").innerHTML = data.detections.length ? data.detections.map(ledgerRow).join("") : '<tr><td colspan="5" class="empty-state">No recognitions match these filters.</td></tr>'; const first = data.total ? offset + 1 : 0; $("#ledgerCount").textContent = `Showing ${formatNumber(first)}–${formatNumber(offset + data.detections.length)} of ${formatNumber(data.total)}`; const pages = Routes.pageCount(data.total); $("#pageNumber").textContent = `Page ${explore.page} of ${pages}`; for (const [selector, page, visible] of [["#previousPage", explore.page - 1, explore.page > 1], ["#nextPage", explore.page + 1, explore.page < pages]]) { const link = $(selector); link.hidden = !visible; link.href = Routes.href("explore") + Routes.exploreSearch({ ...explore, page }); } }
async function restoreExplore() { const token = ++exploreRequestToken; let explore = Routes.parseExplore(window.location.search); try { let data = await loadLedger(explore); if (token !== exploreRequestToken) return; const lastPage = Routes.pageCount(data.total); if (explore.page > lastPage) { explore = { ...explore, page: lastPage }; history.replaceState({}, "", Routes.href("explore") + Routes.exploreSearch(explore) + window.location.hash); data = await loadLedger(explore); if (token !== exploreRequestToken) return; } state.explore = explore; applyExploreToForm(explore); renderLedger(explore, data); } catch (error) { if (token === exploreRequestToken) showError($("#ledgerRows"), error.message); } }
async function initExplore() { const token = ++exploreRequestToken; try { const [activity, speciesData, seasonality] = await Promise.all([fetchJSON("api/activity?days=365"), fetchJSON("api/species"), fetchJSON("api/seasonality")]); if (token !== exploreRequestToken) return; renderActivity(activity); populateSpeciesControls(speciesData.species); state.seasonality = seasonality.by_species_month; const selected = state.species[0]?.scientific_name || ""; if (selected) { $("#seasonSpecies").value = selected; renderMonthChart(selected); } } catch (error) { if (token === exploreRequestToken) { showToast(`Explore unavailable: ${error.message}`); $("#ledgerCount").textContent = "The archive could not be read. Try again shortly."; } } $("#seasonSpecies").addEventListener("change", event => renderMonthChart(event.target.value)); $("#ledgerFilters").addEventListener("submit", event => { event.preventDefault(); const next = { q: $("#filterQuery").value.trim(), species: $("#filterSpecies").value, date_from: $("#filterFrom").value, confidence_min: $("#filterConfidence").value, review: $("#filterReview").value, page: 1 }; history.pushState({}, "", Routes.href("explore") + Routes.exploreSearch(next)); restoreExplore(); }); window.addEventListener("popstate", restoreExplore); await restoreExplore(); }

function renderSpecies(holder, species, candidate = false) { if (!species.length) { holder.innerHTML = `<p class="empty-state">${candidate ? "No uncorroborated species candidates." : "No species have been independently corroborated yet."}</p>`; return; } const max = Math.max(1, ...species.map(item => Number(item.days_heard))); holder.innerHTML = species.map((bird, index) => { const counts = bird.review_counts || {}; const warning = candidate ? `<span class="species-standing ${Number(counts.model_conflict) ? "model_conflict" : "uncorroborated"}">${Number(counts.model_conflict) ? "Model conflict" : "Uncorroborated"}</span>` : '<span class="species-standing corroborated">Corroborated</span>'; const dayLabel = bird.days_heard === 1 ? "day" : "days"; const recognitionLabel = bird.detections === 1 ? "recognition" : "recognitions"; return `<a class="species-row" href="${escapeHTML(speciesHistoryHref(bird.slug))}"><span class="species-rank">${String(index + 1).padStart(2, "0")}</span><span class="species-name"><strong>${escapeHTML(bird.common_name)}</strong><em>${escapeHTML(bird.scientific_name)}</em>${warning}</span><span>${formatNumber(bird.days_heard)} ${dayLabel} heard</span><i aria-hidden="true"><b style="width:${Math.max(3, Number(bird.days_heard) / max * 100)}%"></b></i><span>${formatNumber(bird.detections)} ${recognitionLabel}</span></a>`; }).join(""); }
async function initSpeciesIndex() { try { const data = await fetchJSON("api/species"); const corroborated = data.species.filter(bird => bird.standing === "corroborated"); const uncorroborated = data.species.filter(bird => bird.standing !== "corroborated"); renderSpecies($("#corroboratedSpeciesIndex"), corroborated); renderSpecies($("#uncorroboratedSpeciesIndex"), uncorroborated, true); } catch (error) { showError($("#corroboratedSpeciesIndex"), error.message); showError($("#uncorroboratedSpeciesIndex"), error.message); } }
function occurrenceCard(item) {
  const detectionId = String(item.detection_id || "");
  const status = String(item.review_status || "unreviewed");
  return `<a class="occurrence-card" data-id="${escapeHTML(detectionId)}" href="${escapeHTML(Routes.href("detection-detail", { detectionId }))}"><time datetime="${escapeHTML(item.observed_at_local)}">${displayDate(item.date)} · ${displayTime(item.time)}</time><strong>${formatConfidence(item.confidence)} BirdNET claim</strong><span class="status-pill ${escapeHTML(status)}">${escapeHTML(status.replace("_", " "))}</span><span>${item.has_audio ? "Audio preserved" : "Claim metadata preserved"} →</span></a>`;
}
function speciesPageFromURL() {
  const raw = new URLSearchParams(window.location.search).get("page") || "1";
  return Routes.safePage(raw);
}
function renderSpeciesPagination(bird, total, page) {
  const holder = $("#speciesPagination");
  const pages = Routes.pageCount(total);
  const hrefFor = value => `${Routes.href("species-detail", { slug: bird.slug || bird.art_slug })}?page=${value}#speciesOccurrences`;
  holder.innerHTML = `${page > 1 ? `<a href="${escapeHTML(hrefFor(page - 1))}">← Newer</a>` : ""}<span>Page ${formatNumber(page)} of ${formatNumber(pages)}</span>${page < pages ? `<a href="${escapeHTML(hrefFor(page + 1))}">Older →</a>` : ""}`;
}
async function initSpeciesDetail(route) {
  const holder = $("#speciesOccurrenceGrid");
  try {
    const detail = await fetchJSON(`api/species/${encodeURIComponent(route.slug)}`);
    const bird = detail.species;
    const canonicalSlug = bird.slug || bird.art_slug;
    if (route.slug !== bird.slug) history.replaceState({}, "", `${Routes.href("species-detail", { slug: canonicalSlug })}${window.location.search}${window.location.hash}`);
    let page = speciesPageFromURL();
    document.title = `${bird.common_name} · The Listening Garden`;
    $("#speciesTitle").textContent = bird.common_name;
    $("#speciesCrumb").textContent = bird.common_name;
    $("#speciesLatin").textContent = bird.scientific_name;
    $("#speciesRecognitions").textContent = formatNumber(bird.detections);
    $("#speciesDays").textContent = formatNumber(bird.days_heard);
    $("#speciesFirstHeard").textContent = displayDate(bird.first_heard, true);
    $("#speciesLastHeard").textContent = displayDate(bird.last_heard, true);
    const instancesLink = $("#speciesInstancesLink");
    instancesLink.href = `${Routes.href("species-detail", { slug: bird.slug || bird.art_slug })}#speciesOccurrences`;
    instancesLink.textContent = `View all ${formatNumber(bird.detections)} recognition${Number(bird.detections) === 1 ? "" : "s"} ↓`;
    const art = $("#speciesArt");
    art.src = artURL(bird.art_slug || bird.slug);
    art.alt = `Illustration of ${bird.common_name}`;
    makeIllustrationsSelfHealing($(".species-profile"));
    const reviews = bird.review_counts || {};
    const reviewSummary = $("#speciesReviewSummary");
    reviewSummary.className = `review-summary ${bird.standing === "corroborated" ? "corroborated" : "uncorroborated"}`;
    reviewSummary.innerHTML = `<strong>${bird.standing === "corroborated" ? "Corroborated species" : "Uncorroborated candidate"}</strong><span>${formatNumber(reviews.corroborated)} corroborated · ${formatNumber(reviews.uncorroborated)} uncorroborated · ${formatNumber(reviews.model_conflict)} model conflict · ${formatNumber(reviews.pending)} pending · ${formatNumber(reviews.unreviewed)} unreviewed</span>`;
    const loadOccurrences = value => fetchJSON(`api/detections?${new URLSearchParams({ species: bird.scientific_name, limit: PAGE_SIZE, offset: (value - 1) * PAGE_SIZE })}`);
    let occurrences = await loadOccurrences(page);
    const lastPage = Routes.pageCount(occurrences.total);
    if (page > lastPage) { page = lastPage; history.replaceState({}, "", `${Routes.href("species-detail", { slug: canonicalSlug })}?page=${page}${window.location.hash}`); occurrences = await loadOccurrences(page); }
    holder.innerHTML = occurrences.detections.length ? occurrences.detections.map(occurrenceCard).join("") : '<p class="empty-state">No published instances are available on this page.</p>';
    renderSpeciesPagination(bird, occurrences.total, page);
    if (location.hash === "#speciesOccurrences") {
      window.requestAnimationFrame(() => {
        const destination = $("#speciesOccurrences");
        destination.scrollIntoView({ block: "start" });
        focusRouteDestination(destination);
      });
    } else {
      focusRouteDestination($("#speciesTitle"));
    }
  } catch (error) {
    document.title = "Species unavailable · The Listening Garden";
    showError(holder, error.message);
    $("#speciesTitle").textContent = "Species unavailable";
  }
}

function reviewText(item) { const status = String(item.review_status || "unreviewed"); if (!item.review_model) return status === "pending" ? "Pending independent review" : "Not independently reviewed"; const rank = item.review_rank ? ` · rank #${formatNumber(item.review_rank)} of ${formatNumber(item.review_label_count)}` : ""; const score = item.review_score == null ? "" : ` · claimed-species model score ${formatConfidence(item.review_score)}`; const leader = item.review_top_label && item.review_top_score != null && item.review_top_label !== item.scientific_name ? ` · leading alternative ${item.review_top_label} ${formatConfidence(item.review_top_score)}` : ""; return `${item.review_model} · ${status.replace("_", " ")}${rank}${score}${leader} · model score—not probability`; }
function setDetectionNeighbor(selector, detectionId) { const link = $(selector); const id = String(detectionId || ""); const valid = /^[0-9a-f]{64}$/.test(id); link.hidden = !valid; if (valid) link.href = Routes.href("detection-detail", { detectionId: id }); else link.removeAttribute("href"); }

const RELATED_PAGE_SIZE = 12;
const MAX_RELATED_PAGE = 20;
const RELATED_SORTS = new Set(["best", "contrast", "recent"]);
const RELATED_EXPLANATIONS = {
  best: "Best evidence groups independent review outcomes first, then uses review score, BirdNET score, audio contrast and recency as separate tie-breakers.",
  contrast: "Highest audio contrast orders broadband P95–P20 frame-level variation. A loud gust, vehicle or another bird can also raise it.",
  recent: "Newest recording orders playable clips only by their original local timestamp.",
};
function relatedStateFromURL() {
  const params = new URLSearchParams(window.location.search);
  const requestedSort = params.get("related_sort") || "best";
  const sort = RELATED_SORTS.has(requestedSort) ? requestedSort : "best";
  const rawPage = params.get("related_page") || "1";
  const page = /^\d{1,2}$/.test(rawPage) ? Math.min(MAX_RELATED_PAGE, Math.max(1, Number(rawPage))) : 1;
  return { sort, page };
}
function persistRelatedState(sort, page) {
  const url = new URL(window.location.href);
  const safePage = Math.min(MAX_RELATED_PAGE, Math.max(1, Number(page) || 1));
  if (sort === "best") url.searchParams.delete("related_sort");
  else url.searchParams.set("related_sort", sort);
  if (safePage <= 1) url.searchParams.delete("related_page");
  else url.searchParams.set("related_page", String(safePage));
  history.replaceState({ ...(history.state || {}), relatedSort: sort, relatedPage: safePage }, "", `${url.pathname}${url.search}${url.hash}`);
  const skip = $("#skipLink");
  if (skip) skip.href = `${url.pathname}${url.search}#main`;
}
function qualityText(quality) {
  if (!quality) return "Waveform analysis pending";
  const fraction = Number(quality.near_full_scale_fraction || 0);
  const nearFullScale = fraction > 0 ? ` · ${(fraction * 100).toFixed(3)}% near-full-scale samples` : " · no near-full-scale samples";
  return `${Number(quality.audio_contrast_db).toFixed(1)} dB audio contrast · quiet-frame level ${Number(quality.quiet_frame_level_dbfs).toFixed(1)} dBFS · high-energy level ${Number(quality.high_energy_frame_level_dbfs).toFixed(1)} dBFS${nearFullScale}`;
}
function comparisonCard(item, rank) {
  const detectionId = String(item.detection_id || "");
  if (!/^[0-9a-f]{64}$/.test(detectionId)) return "";
  const status = ["pending", "unreviewed", "corroborated", "uncorroborated", "model_conflict"].includes(item.review_status) ? item.review_status : "unreviewed";
  const reviewer = item.review_kind === "perch" ? "Perch" : item.review_kind === "independent" ? "Independent review" : null;
  const statusText = reviewer ? {
    corroborated: `${reviewer} corroborates`,
    uncorroborated: `${reviewer} does not corroborate`,
    model_conflict: `${reviewer} conflicts`,
    pending: `${reviewer} pending`,
    unreviewed: "No independent review",
  }[status] : "No independent review";
  const quality = item.audio_quality;
  const contrast = quality ? `${Number(quality.audio_contrast_db).toFixed(1)} dB` : "Pending";
  const quiet = quality ? `${Number(quality.quiet_frame_level_dbfs).toFixed(1)} dBFS` : "Pending";
  const when = `${displayDate(item.date)} at ${displayTime(item.time)}`;
  return `<article class="comparison-card" data-id="${escapeHTML(detectionId)}"><header><span class="comparison-rank" aria-label="Position ${rank}">${String(rank).padStart(2, "0")}</span><div><time datetime="${escapeHTML(item.observed_at_local)}">${escapeHTML(when)}</time><span class="status-pill ${escapeHTML(status)}">${escapeHTML(statusText)}</span></div></header><dl class="comparison-metrics"><div><dt>BirdNET score</dt><dd>${formatConfidence(item.confidence)}</dd></div><div><dt>${escapeHTML(reviewer ? `${reviewer} claim score` : "Review claim score")}</dt><dd>${item.review_score == null ? "—" : formatConfidence(item.review_score)}</dd></div><div><dt>Audio contrast</dt><dd>${escapeHTML(contrast)}</dd></div><div><dt>Quiet-frame level</dt><dd>${escapeHTML(quiet)}</dd></div></dl><audio controls preload="none" src="api/audio/${encodeURIComponent(detectionId)}" aria-label="Other ${escapeHTML(item.common_name)} call, recorded ${escapeHTML(when)}"></audio><a class="comparison-evidence" href="${escapeHTML(Routes.href("detection-detail", { detectionId }))}">Open full evidence →</a></article>`;
}
async function loadRelatedCalls(detectionId, append = false) {
  const holder = $("#relatedCalls");
  const more = $("#relatedMore");
  const sort = $("#relatedSort").value;
  const offset = append ? Number(holder.dataset.loaded || 0) : 0;
  const revision = append ? String(holder.dataset.revision || "") : "";
  const replacingLoadedQueue = !append && Number(holder.dataset.loaded || 0) > 0;
  const token = ++relatedRequestToken;
  if (relatedAbortController) relatedAbortController.abort();
  const controller = new AbortController();
  relatedAbortController = controller;
  more.disabled = true;
  if (!append) {
    if (replacingLoadedQueue) {
      holder.querySelectorAll("audio").forEach(player => player.pause());
      const primary = $("#detectionAudio");
      if (primary && typeof primary.pause === "function") primary.pause();
    }
    holder.dataset.loaded = "0";
    delete holder.dataset.revision;
    holder.innerHTML = '<p class="loading">Ranking preserved calls…</p>';
    $("#relatedCount").textContent = "Loading related calls…";
    more.hidden = true;
  }
  try {
    const params = new URLSearchParams({ sort, limit: RELATED_PAGE_SIZE, offset });
    if (revision) params.set("revision", revision);
    const data = await fetchJSON(
      `api/detections/${encodeURIComponent(detectionId)}/related?${params}`,
      { signal: controller.signal },
    );
    if (token !== relatedRequestToken || $("#relatedSort").value !== sort) return false;
    if (data.sort !== sort || data.offset !== offset || !/^[0-9a-f]{64}$/.test(String(data.revision || ""))) throw new Error("invalid related queue response");
    const existing = new Set([...holder.querySelectorAll(".comparison-card[data-id]")].map(card => card.dataset.id));
    const freshCalls = data.calls.filter(item => !existing.has(String(item.detection_id || "")));
    const cards = freshCalls.map((item, index) => comparisonCard(item, offset + index + 1)).join("");
    if (append) holder.insertAdjacentHTML("beforeend", cards);
    else holder.innerHTML = cards || '<p class="empty-state">No other playable calls carrying this scientific species label are available yet.</p>';
    const loaded = offset + data.calls.length;
    holder.dataset.loaded = String(loaded);
    holder.dataset.revision = data.revision;
    const viewLimit = RELATED_PAGE_SIZE * MAX_RELATED_PAGE;
    const viewCapped = loaded >= viewLimit && loaded < data.total;
    $("#relatedCount").textContent = data.total
      ? `Showing ${formatNumber(loaded)} of ${formatNumber(data.total)} other playable clips${viewCapped ? ` · listening view capped at ${formatNumber(viewLimit)}` : ""}`
      : "No other playable clip carrying this scientific species label is available yet.";
    more.hidden = loaded >= data.total || viewCapped;
    persistRelatedState(sort, Math.max(1, Math.ceil(loaded / RELATED_PAGE_SIZE)));
    return true;
  } catch (error) {
    if (error.name === "AbortError" || token !== relatedRequestToken) return false;
    if (append && error.status === 409) {
      showToast("The ranked queue changed as new evidence arrived; refreshing it now.");
      return loadRelatedCalls(detectionId, false);
    }
    if (!append) {
      holder.dataset.loaded = "0";
      delete holder.dataset.revision;
      showError(holder, error.message);
      $("#relatedCount").textContent = "Related calls unavailable.";
      more.hidden = true;
    } else {
      showToast(`More calls unavailable: ${error.message}`);
    }
    return false;
  } finally {
    if (token === relatedRequestToken) {
      relatedAbortController = null;
      more.disabled = false;
    }
  }
}
async function initDetectionDetail(route) {
  try {
    const data = await fetchJSON(`api/detections/${encodeURIComponent(route.detectionId)}`);
    const item = data.detection;
    const detectionId = String(item.detection_id || "");
    if (!/^[0-9a-f]{64}$/.test(detectionId)) throw new Error("invalid evidence identifier");
    document.title = `${item.common_name} evidence · The Listening Garden`;
    const artwork = $("#detectionArtwork");
    const art = $("#detectionArt");
    art.alt = `Field-guide illustration of ${item.common_name}`;
    $("#detectionArtCaption").textContent = `${item.common_name} · ${item.scientific_name}`;
    artwork.hidden = false;
    makeIllustrationsSelfHealing(artwork);
    art.src = artURL(item.art_slug);
    $("#detectionTitle").textContent = item.common_name;
    $("#detectionRecorded").textContent = `${displayDate(item.date)} at ${displayTime(item.time)}${item.timezone ? ` · ${item.timezone}` : ""}`;
    $("#detectionScore").textContent = `${formatConfidence(item.confidence)} raw classifier score`;
    const reviewCallout = $("#detectionReview");
    reviewCallout.className = `review-callout ${escapeHTML(item.review_status || "unreviewed")}`;
    reviewCallout.textContent = reviewText(item);
    $("#detectionReviewDetails").textContent = reviewText(item);
    $("#detectionQuality").textContent = qualityText(item.audio_quality);
    $("#detectionAlternatives").textContent = item.notes || "No independent alternatives recorded.";
    const digest = String(item.audio_sha256 || "");
    const audioBytes = Number(item.audio_bytes);
    const validEvidenceMetadata = /^[0-9a-f]{64}$/.test(digest) && Number.isSafeInteger(audioBytes) && audioBytes > 0;
    $("#detectionHash").textContent = validEvidenceMetadata ? `SHA-256 ${digest} · ${formatNumber(audioBytes)} bytes` : "Evidence digest or size unavailable";
    const speciesLink = $("#detectionSpeciesLink");
    speciesLink.href = speciesHistoryHref(item.slug);
    speciesLink.textContent = `${item.common_name} · ${item.scientific_name} →`;
    $("#detectionBack").href = speciesHistoryHref(item.slug);
    $("#detectionBack").textContent = `Back to ${item.common_name}`;
    $("#comparisonTitle").textContent = `Hear more ${item.common_name} clips`;
    setDetectionNeighbor("#newerDetection", item.newer_detection_id);
    setDetectionNeighbor("#olderDetection", item.older_detection_id);
    const audio = $("#detectionAudio");
    if (item.has_audio) audio.src = `api/audio/${encodeURIComponent(detectionId)}`;
    else { audio.hidden = true; audio.insertAdjacentHTML("afterend", '<p class="empty-state">The audio segment is not currently available, but its claim metadata remains auditable.</p>'); }
    $("#detectionIdentity").innerHTML = `<span class="digest-label">Immutable detection identity</span><code data-id="${escapeHTML(detectionId)}">${escapeHTML(detectionId)}</code>`;
    const holder = $("#relatedCalls");
    holder.addEventListener("play", event => {
      holder.querySelectorAll("audio").forEach(player => { if (player !== event.target) player.pause(); });
      if (audio !== event.target) audio.pause();
    }, true);
    audio.addEventListener("play", () => holder.querySelectorAll("audio").forEach(player => player.pause()));
    const relatedState = relatedStateFromURL();
    $("#relatedSort").value = relatedState.sort;
    $("#comparisonExplanation").textContent = RELATED_EXPLANATIONS[relatedState.sort];
    $("#relatedSort").addEventListener("change", async event => {
      holder.querySelectorAll("audio").forEach(player => player.pause());
      $("#comparisonExplanation").textContent = RELATED_EXPLANATIONS[event.target.value] || RELATED_EXPLANATIONS.best;
      persistRelatedState(event.target.value, 1);
      await loadRelatedCalls(detectionId);
    });
    $("#relatedMore").addEventListener("click", () => loadRelatedCalls(detectionId, true));
    focusRouteDestination($("#detectionTitle"));
    const firstPageLoaded = await loadRelatedCalls(detectionId);
    if (firstPageLoaded) {
      for (let page = 2; page <= relatedState.page && !$("#relatedMore").hidden; page += 1) {
        if (!await loadRelatedCalls(detectionId, true)) break;
      }
    }
  } catch (error) {
    document.title = "Evidence unavailable · The Listening Garden";
    $("#detectionTitle").textContent = "Evidence unavailable";
    $("#detectionRecorded").textContent = "This preserved recognition could not be opened.";
    $("#detectionReview").textContent = error.message;
  }
}

function renderEpochs(data) { const holder = $("#epochTimeline"); holder.innerHTML = data.epochs.length ? data.epochs.map((epoch, index) => `<li><time datetime="${escapeHTML(epoch.effective_at)}">${escapeHTML(String(epoch.effective_at).replace("T", " ").slice(0, 16))}</time><div><h3>${escapeHTML(epoch.reason || `Configuration epoch ${index + 1}`)}</h3><p>${escapeHTML(epoch.audio_model || "Detector recorded")} · range ${escapeHTML(epoch.range_model || "—")} · confidence ${formatConfidence(epoch.confidence_threshold)}</p></div></li>`).join("") : '<li class="empty-state">No configuration epochs recorded.</li>'; }
async function initAbout() { try { renderEpochs(await fetchJSON("api/epochs")); } catch (error) { showError($("#epochTimeline"), error.message); } }

async function init() { const legacyHref = Routes.legacyDetectionHref(window.location.pathname, window.location.search); if (legacyHref) { window.location.replace(legacyHref); return; } const documentURL = `${window.location.pathname}${window.location.search}`; $("#skipLink").href = `${documentURL}#main`; const route = Routes.parseRoute(window.location.pathname); setCurrentRoute(route); if (route.name !== "not-found") loadCommonStatus(); const initializers = { today: initToday, explore: initExplore, "species-index": initSpeciesIndex, "species-detail": () => initSpeciesDetail(route), "detection-detail": () => initDetectionDetail(route), about: initAbout, "not-found": async () => {} }; try { await initializers[route.name](); } catch (error) { console.error(error); showToast(`Archive unavailable: ${error.message}`); } }
document.addEventListener("DOMContentLoaded", init);
