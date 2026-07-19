"use strict";

const Routes = window.ListeningGardenRoutes;
const ART_VERSION = "r2";
const ART_RETRY_DELAYS_MS = [5000, 30000, 300000];
const PAGE_SIZE = 50;
const state = { summary: null, species: [], seasonality: [], explore: null };
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
async function fetchJSON(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) { let message = `${response.status}`; try { message = (await response.json()).error || message; } catch (_) { /* non-JSON error */ } throw new Error(message); }
  return response.json();
}
function showToast(message) { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.classList.remove("show"), 4000); }
function showError(holder, message) { holder.innerHTML = `<div class="error-state"><strong>The record could not be opened.</strong><p>${escapeHTML(message)}</p></div>`; }
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
  ["Detections", "Species", "Days", "Clips"].forEach((name, index) => { const node = $(`#metric${name}`); if (node) node.textContent = formatNumber([summary.totals.detections, summary.totals.species, summary.totals.days_listening, summary.totals.clips_preserved][index]); });
  const sync = summary.latest_sync; const status = $("#archiveStatus");
  if (!sync?.completed_at) { status.innerHTML = '<i class="pulse stale"></i><div><strong>Mirror status unknown</strong><small>No successful sync recorded</small></div>'; return; }
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(sync.completed_at).getTime()) / 60000)); const stale = minutes > 30;
  status.innerHTML = `<i class="pulse ${stale ? "stale" : ""}" aria-hidden="true"></i><div><strong>${stale ? "Mirror needs attention" : "Archive mirror current"}</strong><small>Synced ${minutes ? `${formatNumber(minutes)} min` : "less than a minute"} ago · ${formatNumber(sync.source_rows)} source rows</small></div>`;
}
async function loadCommonStatus() { try { renderSummary(await fetchJSON("api/summary")); } catch (_) { $("#archiveStatus").innerHTML = '<i class="pulse stale"></i><div><strong>Archive unavailable</strong><small>The private mirror could not be read</small></div>'; } }

function renderToday(data) {
  $("#todayDate").textContent = displayDate(data.date);
  const holder = $("#todayGrid");
  if (!data.species.length) { holder.innerHTML = '<p class="empty-state">The garden is listening. No accepted recognitions yet today.</p>'; return; }
  holder.innerHTML = data.species.map((bird, index) => `<a class="bird-card" href="${escapeHTML(speciesHistoryHref(bird.slug))}"><span class="bird-number">${String(index + 1).padStart(2, "0")}</span><img src="${escapeHTML(artURL(bird.slug))}" alt="Illustration of ${escapeHTML(bird.common_name)}"><h2>${escapeHTML(bird.common_name)}</h2><em>${escapeHTML(bird.scientific_name)}</em><span class="bird-facts">${formatNumber(bird.detections)} recognition${Number(bird.detections) === 1 ? "" : "s"} · ${displayTime(bird.first_heard)}–${displayTime(bird.last_heard)}</span></a>`).join("");
  makeIllustrationsSelfHealing(holder);
}
async function initToday() { try { renderToday(await fetchJSON("api/today")); } catch (error) { showError($("#todayGrid"), error.message); } }

function populateSpeciesControls(species) { state.species = species; const options = species.map(bird => `<option value="${escapeHTML(bird.scientific_name)}">${escapeHTML(bird.common_name)}</option>`).join(""); $("#seasonSpecies").innerHTML = options || '<option value="">No species recorded</option>'; $("#filterSpecies").innerHTML = `<option value="">All species</option>${options}`; }
function dateRange(days, endKey) { const result = []; const parts = String(endKey || "").split("-").map(Number); const end = parts.length === 3 ? new Date(parts[0], parts[1] - 1, parts[2], 12) : new Date(); for (let offset = days - 1; offset >= 0; offset -= 1) { const date = new Date(end); date.setDate(end.getDate() - offset); result.push(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`); } return result; }
function renderActivity(data) { const daily = new Map(data.daily.map(row => [row.date, Number(row.detections)])); const max = Math.max(1, ...daily.values()); $("#activityHeatmap").innerHTML = dateRange(365, state.summary?.today?.date).map(date => { const count = daily.get(date) || 0; const level = count ? Math.max(1, Math.ceil(count / max * 4)) : 0; return `<i data-level="${level}" title="${escapeHTML(`${displayDate(date, true)}: ${count} recognitions`)}"></i>`; }).join(""); const hours = new Map(data.by_hour.map(row => [Number(row.hour), Number(row.detections)])); const high = Math.max(1, ...hours.values()); $("#hourChart").innerHTML = Array.from({ length: 24 }, (_, hour) => { const count = hours.get(hour) || 0; return `<i class="hour-bar" data-hour="${String(hour).padStart(2, "0")}" title="${count} recognitions" style="height:${count ? Math.max(3, count / high * 100) : 1}%"></i>`; }).join(""); }
function renderMonthChart(scientificName) { const rows = state.seasonality.filter(row => row.scientific_name === scientificName); const months = new Map(rows.map(row => [Number(row.calendar_month), Number(row.detections)])); const max = Math.max(1, ...months.values()); const names = ["J","F","M","A","M","J","J","A","S","O","N","D"]; $("#monthChart").innerHTML = names.map((name, index) => { const count = months.get(index + 1) || 0; return `<div><i style="height:${count ? Math.max(2, count / max * 130) : 1}px" data-count="${count || ""}"></i><span>${name}</span></div>`; }).join(""); const bird = state.species.find(item => item.scientific_name === scientificName); $("#seasonSpeciesNote").textContent = bird ? `${formatNumber(bird.detections)} recognitions across ${formatNumber(bird.days_heard)} days` : "Choose a species"; }
function applyExploreToForm(explore) { $("#filterQuery").value = explore.q; $("#filterSpecies").value = explore.species; $("#filterFrom").value = explore.date_from; $("#filterConfidence").value = explore.confidence_min; $("#filterReview").value = explore.review; }
function ledgerRow(item) { const detectionId = String(item.detection_id || ""); const speciesHref = speciesHistoryHref(item.slug); const detectionHref = Routes.href("detection-detail", { detectionId }); const status = ["pending","unreviewed","confirmed","uncertain","rejected"].includes(item.review_status) ? item.review_status : "unreviewed"; return `<tr data-id="${escapeHTML(detectionId)}"><td><time datetime="${escapeHTML(item.observed_at_local)}">${displayDate(item.date, true)}<br>${displayTime(item.time)}</time></td><td><a class="ledger-bird-link" href="${escapeHTML(speciesHref)}"><strong>${escapeHTML(item.common_name)}</strong><em>${escapeHTML(item.scientific_name)}</em></a></td><td>${formatConfidence(item.confidence)}</td><td><span class="status-pill ${escapeHTML(status)}">${escapeHTML(status)}</span></td><td><a class="evidence-link" href="${escapeHTML(detectionHref)}">${item.has_audio ? "Inspect + listen" : "Inspect claim"}</a></td></tr>`; }
async function loadLedger(explore) { const offset = (explore.page - 1) * PAGE_SIZE; const params = new URLSearchParams({ limit: PAGE_SIZE, offset }); ["q","species","date_from","confidence_min","review"].forEach(key => { if (explore[key]) params.set(key, explore[key]); }); const data = await fetchJSON(`api/detections?${params}`); $("#ledgerRows").innerHTML = data.detections.length ? data.detections.map(ledgerRow).join("") : '<tr><td colspan="5" class="empty-state">No recognitions match these filters.</td></tr>'; $("#ledgerCount").textContent = `Showing ${formatNumber(offset + (data.detections.length ? 1 : 0))}–${formatNumber(offset + data.detections.length)} of ${formatNumber(data.total)}`; $("#pageNumber").textContent = `Page ${explore.page}`; for (const [selector, page, visible] of [["#previousPage", explore.page - 1, explore.page > 1], ["#nextPage", explore.page + 1, offset + data.detections.length < data.total]]) { const link = $(selector); link.hidden = !visible; link.href = Routes.href("explore") + Routes.exploreSearch({ ...explore, page }); } }
async function restoreExplore() { const explore = Routes.parseExplore(window.location.search); state.explore = explore; applyExploreToForm(explore); try { await loadLedger(explore); } catch (error) { showError($("#ledgerRows"), error.message); } }
async function initExplore() { state.explore = Routes.parseExplore(window.location.search); try { const [activity, speciesData, seasonality] = await Promise.all([fetchJSON("api/activity?days=365"), fetchJSON("api/species"), fetchJSON("api/seasonality")]); renderActivity(activity); populateSpeciesControls(speciesData.species); state.seasonality = seasonality.by_species_month; const selected = state.species[0]?.scientific_name || ""; if (selected) { $("#seasonSpecies").value = selected; renderMonthChart(selected); } applyExploreToForm(state.explore); await loadLedger(state.explore); } catch (error) { showToast(`Explore unavailable: ${error.message}`); $("#ledgerCount").textContent = "The archive could not be read. Try again shortly."; } $("#seasonSpecies").addEventListener("change", event => renderMonthChart(event.target.value)); $("#ledgerFilters").addEventListener("submit", event => { event.preventDefault(); const next = { q: $("#filterQuery").value.trim(), species: $("#filterSpecies").value, date_from: $("#filterFrom").value, confidence_min: $("#filterConfidence").value, review: $("#filterReview").value, page: 1 }; history.pushState({}, "", Routes.href("explore") + Routes.exploreSearch(next)); restoreExplore(); }); window.addEventListener("popstate", restoreExplore); }

function renderSpecies(species) { const holder = $("#speciesIndex"); if (!species.length) { holder.innerHTML = '<p class="empty-state">No species have entered the permanent record yet.</p>'; return; } const max = Math.max(1, ...species.map(item => Number(item.days_heard))); holder.innerHTML = species.map((bird, index) => `<a class="species-row" href="${escapeHTML(speciesHistoryHref(bird.slug))}"><span class="species-rank">${String(index + 1).padStart(2, "0")}</span><span class="species-name"><strong>${escapeHTML(bird.common_name)}</strong><em>${escapeHTML(bird.scientific_name)}</em></span><span>${formatNumber(bird.days_heard)} days heard</span><i aria-hidden="true"><b style="width:${Math.max(3, Number(bird.days_heard) / max * 100)}%"></b></i><span>${formatNumber(bird.detections)} recognitions</span></a>`).join(""); }
async function initSpeciesIndex() { try { const data = await fetchJSON("api/species"); renderSpecies(data.species); } catch (error) { showError($("#speciesIndex"), error.message); } }
function occurrenceCard(item) {
  const detectionId = String(item.detection_id || "");
  return `<a class="occurrence-card" data-id="${escapeHTML(detectionId)}" href="${escapeHTML(Routes.href("detection-detail", { detectionId }))}"><time datetime="${escapeHTML(item.observed_at_local)}">${displayDate(item.date)} · ${displayTime(item.time)}</time><strong>${formatConfidence(item.confidence)} BirdNET claim</strong><span class="status-pill">${escapeHTML(item.review_status || "unreviewed")}</span><span>${item.has_audio ? "Audio preserved" : "Claim metadata preserved"} →</span></a>`;
}
function speciesPageFromURL() {
  const raw = new URLSearchParams(window.location.search).get("page") || "1";
  const page = /^\d+$/.test(raw) ? Number(raw) : 1;
  return Number.isSafeInteger(page) && page >= 1 ? page : 1;
}
function renderSpeciesPagination(bird, total, page) {
  const holder = $("#speciesPagination");
  const pages = Math.max(1, Math.ceil(Number(total || 0) / PAGE_SIZE));
  const hrefFor = value => `${Routes.href("species-detail", { slug: bird.slug })}?page=${value}#speciesOccurrences`;
  holder.innerHTML = `${page > 1 ? `<a href="${escapeHTML(hrefFor(page - 1))}">← Newer</a>` : ""}<span>Page ${formatNumber(page)} of ${formatNumber(pages)}</span>${page < pages ? `<a href="${escapeHTML(hrefFor(page + 1))}">Older →</a>` : ""}`;
}
async function initSpeciesDetail(route) {
  const holder = $("#speciesOccurrenceGrid");
  try {
    const detail = await fetchJSON(`api/species/${encodeURIComponent(route.slug)}`);
    const bird = detail.species;
    const page = speciesPageFromURL();
    const offset = (page - 1) * PAGE_SIZE;
    document.title = `${bird.common_name} · The Listening Garden`;
    $("#speciesTitle").textContent = bird.common_name;
    $("#speciesCrumb").textContent = bird.common_name;
    $("#speciesLatin").textContent = bird.scientific_name;
    $("#speciesRecognitions").textContent = formatNumber(bird.detections);
    $("#speciesDays").textContent = formatNumber(bird.days_heard);
    $("#speciesFirstHeard").textContent = displayDate(bird.first_heard, true);
    $("#speciesLastHeard").textContent = displayDate(bird.last_heard, true);
    const instancesLink = $("#speciesInstancesLink");
    instancesLink.href = `${Routes.href("species-detail", { slug: bird.slug })}#speciesOccurrences`;
    instancesLink.textContent = `View all ${formatNumber(bird.detections)} recognition${Number(bird.detections) === 1 ? "" : "s"} ↓`;
    const art = $("#speciesArt");
    art.src = artURL(bird.slug);
    art.alt = `Illustration of ${bird.common_name}`;
    makeIllustrationsSelfHealing($(".species-profile"));
    const reviews = bird.review_counts || {};
    $("#speciesReviewSummary").innerHTML = `<strong>Review summary</strong><span>${formatNumber(reviews.confirmed)} confirmed · ${formatNumber(reviews.uncertain)} uncertain · ${formatNumber(reviews.rejected)} rejected · ${formatNumber(reviews.pending)} pending · ${formatNumber(reviews.unreviewed)} unreviewed</span>`;
    const params = new URLSearchParams({ species: bird.scientific_name, limit: PAGE_SIZE, offset });
    const occurrences = await fetchJSON(`api/detections?${params}`);
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

function reviewText(item) { const status = String(item.review_status || "unreviewed"); if (!item.review_model) return status === "pending" ? "Pending independent review" : "Not independently reviewed"; return `${item.review_model} · ${status}${item.review_score == null ? "" : ` · claimed-species score ${formatConfidence(item.review_score)}`}`; }
async function initDetectionDetail(route) { try { const data = await fetchJSON(`api/detections/${encodeURIComponent(route.detectionId)}`); const item = data.detection; const detectionId = String(item.detection_id || ""); if (!/^[0-9a-f]{64}$/.test(detectionId)) throw new Error("invalid evidence identifier"); document.title = `${item.common_name} evidence · The Listening Garden`; $("#detectionTitle").textContent = item.common_name; $("#detectionRecorded").textContent = `${displayDate(item.date)} at ${displayTime(item.time)}${item.timezone ? ` · ${item.timezone}` : ""}`; $("#detectionScore").textContent = `${formatConfidence(item.confidence)} raw classifier score`; $("#detectionReview").textContent = reviewText(item); $("#detectionReviewDetails").textContent = reviewText(item); $("#detectionAlternatives").textContent = item.notes || "No independent alternatives recorded."; const digest = String(item.audio_sha256 || ""); $("#detectionHash").textContent = /^[0-9a-f]{64}$/.test(digest) ? `SHA-256 ${digest} · ${formatNumber(item.audio_bytes)} bytes` : "Evidence digest unavailable"; const speciesLink = $("#detectionSpeciesLink"); speciesLink.href = speciesHistoryHref(item.slug); speciesLink.textContent = `${item.common_name} · ${item.scientific_name} →`; $("#detectionBack").href = speciesHistoryHref(item.slug); $("#detectionBack").textContent = `Back to ${item.common_name}`; const audio = $("#detectionAudio"); if (item.has_audio) audio.src = `api/audio/${encodeURIComponent(detectionId)}`; else { audio.hidden = true; audio.insertAdjacentHTML("afterend", '<p class="empty-state">The audio segment is not currently available, but its claim metadata remains auditable.</p>'); } $("#detectionIdentity").innerHTML = `<span class="digest-label">Immutable detection identity</span><code data-id="${escapeHTML(detectionId)}">${escapeHTML(detectionId)}</code>`; focusRouteDestination($("#detectionTitle")); } catch (error) { document.title = "Evidence unavailable · The Listening Garden"; $("#detectionTitle").textContent = "Evidence unavailable"; $("#detectionRecorded").textContent = "This preserved recognition could not be opened."; $("#detectionReview").textContent = error.message; } }

function renderEpochs(data) { const holder = $("#epochTimeline"); holder.innerHTML = data.epochs.length ? data.epochs.map((epoch, index) => `<li><time datetime="${escapeHTML(epoch.effective_at)}">${escapeHTML(String(epoch.effective_at).replace("T", " ").slice(0, 16))}</time><div><h3>${escapeHTML(epoch.reason || `Configuration epoch ${index + 1}`)}</h3><p>${escapeHTML(epoch.audio_model || "Detector recorded")} · range ${escapeHTML(epoch.range_model || "—")} · confidence ${formatConfidence(epoch.confidence_threshold)}</p></div></li>`).join("") : '<li class="empty-state">No configuration epochs recorded.</li>'; }
async function initAbout() { try { renderEpochs(await fetchJSON("api/epochs")); } catch (error) { showError($("#epochTimeline"), error.message); } }

async function init() { const legacyHref = Routes.legacyDetectionHref(window.location.pathname, window.location.search); if (legacyHref) { window.location.replace(legacyHref); return; } const documentURL = `${window.location.pathname}${window.location.search}`; $("#skipLink").href = `${documentURL}#main`; const route = Routes.parseRoute(window.location.pathname); setCurrentRoute(route); if (route.name !== "not-found") loadCommonStatus(); const initializers = { today: initToday, explore: initExplore, "species-index": initSpeciesIndex, "species-detail": () => initSpeciesDetail(route), "detection-detail": () => initDetectionDetail(route), about: initAbout, "not-found": async () => {} }; try { await initializers[route.name](); } catch (error) { console.error(error); showToast(`Archive unavailable: ${error.message}`); } }
document.addEventListener("DOMContentLoaded", init);
