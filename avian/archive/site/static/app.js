"use strict";

const ART_VERSION = "r2";
const ART_RETRY_DELAYS_MS = [5000, 30000, 300000];

const state = {
  summary: null,
  species: [],
  seasonality: [],
  ledgerOffset: 0,
  ledgerLimit: 75,
  ledgerTotal: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US").format(Number(value || 0));
}

function formatConfidence(value) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function displayDate(isoDate, options = {}) {
  if (!isoDate) return "—";
  const [year, month, day] = isoDate.slice(0, 10).split("-").map(Number);
  return new Intl.DateTimeFormat("en-US", {
    month: options.short ? "short" : "long",
    day: "numeric",
    year: options.year === false ? undefined : "numeric",
  }).format(new Date(year, month - 1, day));
}

function displayTime(time) {
  if (!time) return "—";
  const [hour, minute] = time.slice(0, 5).split(":").map(Number);
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" })
    .format(new Date(2000, 0, 1, hour, minute));
}

async function fetchJSON(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).error || detail; } catch (_) { /* response was not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3500);
}

function makeIllustrationsSelfHealing(root) {
  root.querySelectorAll("img").forEach(img => {
    let retries = 0;
    img.addEventListener("load", () => {
      img.style.visibility = "visible";
    });
    img.addEventListener("error", () => {
      img.style.visibility = "hidden";
      if (retries >= ART_RETRY_DELAYS_MS.length) {
        if (!img.alt.endsWith(" (illustration unavailable)")) {
          img.alt = `${img.alt} (illustration unavailable)`;
        }
        return;
      }
      const delay = ART_RETRY_DELAYS_MS[retries++];
      window.setTimeout(() => {
        const retryURL = new URL(img.src, window.location.href);
        retryURL.searchParams.set("retry", `${Date.now()}`);
        img.src = retryURL.toString();
      }, delay);
    });
  });
}

function renderSummary(summary) {
  state.summary = summary;
  const totals = summary.totals;
  $("#metricDetections").textContent = formatNumber(totals.detections);
  $("#metricSpecies").textContent = formatNumber(totals.species);
  $("#metricDays").textContent = formatNumber(totals.days_listening);
  $("#metricClips").textContent = formatNumber(totals.clips_preserved);

  const status = $("#archiveStatus");
  const sync = summary.latest_sync;
  if (!sync?.completed_at) {
    status.innerHTML = '<span class="pulse stale"></span><div><strong>Mirror status unknown</strong><small>No successful sync recorded</small></div>';
    return;
  }
  const syncDate = new Date(sync.completed_at);
  const ageMinutes = Math.max(0, Math.floor((Date.now() - syncDate.getTime()) / 60000));
  const stale = ageMinutes > 30;
  status.innerHTML = `<span class="pulse ${stale ? "stale" : ""}" aria-hidden="true"></span>
    <div><strong>${stale ? "Mirror needs attention" : "Archive mirror current"}</strong>
    <small>Synced ${ageMinutes === 0 ? "less than a minute" : `${ageMinutes} min`} ago · ${formatNumber(sync.source_rows)} source rows</small></div>`;
}

function renderToday(data) {
  $("#todayDate").textContent = displayDate(data.date);
  const grid = $("#todayGrid");
  if (!data.species.length) {
    grid.innerHTML = '<p class="empty-state">The garden is listening. No accepted recognitions yet today.</p>';
    return;
  }
  grid.innerHTML = data.species.map((bird, index) => `
    <article class="bird-card">
      <span class="bird-number">${String(index + 1).padStart(2, "0")}</span>
      <img src="art/${encodeURIComponent(bird.slug)}.png?v=${ART_VERSION}" alt="Illustration of ${escapeHTML(bird.common_name)}">
      <h3>${escapeHTML(bird.common_name)}</h3>
      <span class="latin">${escapeHTML(bird.scientific_name)}</span>
      <div class="bird-facts"><span>${formatNumber(bird.detections)} call${bird.detections === 1 ? "" : "s"}</span><span>${displayTime(bird.first_heard)}–${displayTime(bird.last_heard)}</span></div>
    </article>`).join("");
  makeIllustrationsSelfHealing(grid);
}

function dateRange(days, endKey) {
  const result = [];
  const [year, month, day] = (endKey || "").split("-").map(Number);
  const end = Number.isFinite(year) && Number.isFinite(month) && Number.isFinite(day)
    ? new Date(year, month - 1, day, 12)
    : new Date();
  end.setHours(12, 0, 0, 0);
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(end);
    date.setDate(end.getDate() - offset);
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    result.push({ date, key });
  }
  return result;
}

function renderActivity(data) {
  const byDate = new Map(data.daily.map(day => [day.date, day]));
  const max = Math.max(1, ...data.daily.map(day => Number(day.detections)));
  const heat = $("#activityHeatmap");
  heat.innerHTML = dateRange(365, state.summary?.today?.date).map(({ date, key }) => {
    const count = Number(byDate.get(key)?.detections || 0);
    const level = count === 0 ? 0 : Math.min(4, Math.max(1, Math.ceil((count / max) * 4)));
    const label = `${displayDate(key, { short: true })}: ${count} recognition${count === 1 ? "" : "s"}`;
    return `<i data-level="${level}" title="${escapeHTML(label)}" aria-hidden="true"></i>`;
  }).join("");

  const hourCounts = new Map(data.by_hour.map(row => [Number(row.hour), Number(row.detections)]));
  const hourMax = Math.max(1, ...hourCounts.values());
  $("#hourChart").innerHTML = Array.from({ length: 24 }, (_, hour) => {
    const count = hourCounts.get(hour) || 0;
    const height = count ? Math.max(3, Math.round((count / hourMax) * 100)) : 1;
    const label = `${hour}:00 — ${count} recognition${count === 1 ? "" : "s"}`;
    return `<i class="hour-bar" data-hour="${String(hour).padStart(2, "0")}" style="height:${height}%" title="${escapeHTML(label)}"></i>`;
  }).join("");
}

function populateSpeciesControls(species) {
  state.species = species;
  const options = species.map(bird => `<option value="${escapeHTML(bird.scientific_name)}">${escapeHTML(bird.common_name)}</option>`).join("");
  $("#seasonSpecies").innerHTML = options || '<option value="">No species yet</option>';
  $("#filterSpecies").innerHTML = `<option value="">All species</option>${options}`;
}

function renderSpecies(species) {
  const holder = $("#speciesIndex");
  if (!species.length) {
    holder.innerHTML = '<p class="empty-state">No species have entered the permanent record yet.</p>';
    return;
  }
  const maxDays = Math.max(...species.map(item => Number(item.days_heard)), 1);
  holder.innerHTML = species.map((bird, index) => `
    <article class="species-row">
      <span class="species-rank">${String(index + 1).padStart(2, "0")}</span>
      <div class="species-name"><strong>${escapeHTML(bird.common_name)}</strong><em>${escapeHTML(bird.scientific_name)}</em></div>
      <span class="species-stat">${formatNumber(bird.days_heard)} day${bird.days_heard === 1 ? "" : "s"} heard</span>
      <div class="species-line" aria-hidden="true"><i style="width:${Math.max(3, (bird.days_heard / maxDays) * 100)}%"></i></div>
      <span class="species-stat hide-mobile">${formatNumber(bird.detections)} calls</span>
    </article>`).join("");
}

function renderMonthChart(scientificName) {
  const rows = state.seasonality.filter(row => row.scientific_name === scientificName);
  const byMonth = new Map(rows.map(row => [Number(row.calendar_month), Number(row.detections)]));
  const max = Math.max(1, ...byMonth.values());
  const bird = state.species.find(item => item.scientific_name === scientificName);
  $("#seasonSpeciesNote").textContent = bird
    ? `${formatNumber(bird.detections)} recognitions across ${formatNumber(bird.days_heard)} listening days`
    : "Choose a species to trace its year";
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  $("#monthChart").innerHTML = names.map((name, index) => {
    const count = byMonth.get(index + 1) || 0;
    const height = count ? Math.max(2, Math.round((count / max) * 145)) : 1;
    return `<div class="month-column"><i style="height:${height}px" data-count="${count || ""}"></i><span>${name}</span></div>`;
  }).join("");
}

function renderSeasonality(data) {
  state.seasonality = data.by_species_month;
  const selected = $("#seasonSpecies").value || state.species[0]?.scientific_name || "";
  if (selected) {
    $("#seasonSpecies").value = selected;
    renderMonthChart(selected);
  }
}

function renderEpochs(data) {
  const holder = $("#epochTimeline");
  if (!data.epochs.length) {
    holder.innerHTML = '<li class="empty-state">No configuration epochs recorded.</li>';
    return;
  }
  holder.innerHTML = data.epochs.map((epoch, index) => `
    <li class="epoch">
      <time datetime="${escapeHTML(epoch.effective_at)}">${escapeHTML(epoch.effective_at.replace("T", " ").slice(0, 16))}</time>
      <div><h3>${escapeHTML(epoch.reason || `Configuration epoch ${index + 1}`)}</h3>
      <p>${escapeHTML(epoch.audio_model || "Detector model recorded")} · range ${escapeHTML(epoch.range_model || "—")}</p>
      <div class="epoch-details"><span>confidence ${formatConfidence(epoch.confidence_threshold)}</span><span>occurrence ${formatConfidence(epoch.occurrence_threshold)}</span><span>sensitivity ${escapeHTML(epoch.sensitivity)}</span><span>overlap ${escapeHTML(epoch.overlap)}</span></div></div>
    </li>`).join("");
}

function ledgerParams(reset) {
  if (reset) state.ledgerOffset = 0;
  const params = new URLSearchParams({ limit: state.ledgerLimit, offset: state.ledgerOffset });
  const values = {
    q: $("#filterQuery").value.trim(),
    species: $("#filterSpecies").value,
    date_from: $("#filterFrom").value,
    confidence_min: $("#filterConfidence").value,
    review: $("#filterReview").value,
  };
  Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
  return params;
}

function ledgerRow(item) {
  const reviewStatus = String(item.review_status || "unreviewed");
  const reviewClass = ["pending", "unreviewed", "confirmed", "uncertain", "rejected"].includes(reviewStatus)
    ? reviewStatus : "unreviewed";
  const detectionId = String(item.detection_id || "");
  const audio = item.has_audio && /^[0-9a-f]{64}$/.test(detectionId)
    ? `<button class="play-button" type="button" data-id="${escapeHTML(detectionId)}" data-name="${escapeHTML(item.common_name)}">Play clip</button>`
    : '<button class="play-button" type="button" disabled>No clip</button>';
  return `<tr>
    <td><time datetime="${escapeHTML(item.observed_at_local)}">${displayDate(item.date, { short: true })}<br>${displayTime(item.time)}</time></td>
    <td class="ledger-bird"><strong>${escapeHTML(item.common_name)}</strong><em>${escapeHTML(item.scientific_name)}</em></td>
    <td><span class="score">${formatConfidence(item.confidence)}</span></td>
    <td><span class="status-pill ${reviewClass}">${escapeHTML(reviewStatus)}</span></td>
    <td>${audio}</td>
  </tr>`;
}

async function loadLedger(reset = false) {
  const data = await fetchJSON(`api/detections?${ledgerParams(reset)}`);
  state.ledgerTotal = data.total;
  const rows = $("#ledgerRows");
  if (reset) rows.innerHTML = "";
  if (!data.detections.length && reset) {
    rows.innerHTML = '<tr><td colspan="5" class="empty-state">No recognitions match these filters.</td></tr>';
  } else {
    rows.insertAdjacentHTML("beforeend", data.detections.map(ledgerRow).join(""));
  }
  state.ledgerOffset += data.detections.length;
  $("#ledgerCount").textContent = `Showing ${formatNumber(Math.min(state.ledgerOffset, data.total))} of ${formatNumber(data.total)} recognitions`;
  $("#loadMore").hidden = state.ledgerOffset >= data.total;
}

function bindAudio() {
  const player = $("#audioPlayer");
  $("#ledgerRows").addEventListener("click", async event => {
    const button = event.target.closest(".play-button[data-id]");
    if (!button) return;
    const isCurrent = player.dataset.id === button.dataset.id && !player.paused;
    if (isCurrent) {
      player.pause();
      $("#playingNow").textContent = "Paused";
      button.textContent = "Play clip";
      return;
    }
    $$(".play-button[data-id]").forEach(item => { item.textContent = "Play clip"; });
    player.src = `api/audio/${encodeURIComponent(button.dataset.id)}`;
    player.dataset.id = button.dataset.id;
    button.textContent = "Pause";
    $("#playingNow").textContent = `Playing ${button.dataset.name}`;
    try { await player.play(); }
    catch (_) { showToast("That evidence clip is temporarily unavailable."); button.textContent = "Play clip"; }
  });
  player.addEventListener("ended", () => {
    $$(".play-button[data-id]").forEach(item => { item.textContent = "Play clip"; });
    $("#playingNow").textContent = "";
  });
}

function bindEvents() {
  $("#seasonSpecies").addEventListener("change", event => renderMonthChart(event.target.value));
  $("#ledgerFilters").addEventListener("submit", async event => {
    event.preventDefault();
    try { await loadLedger(true); } catch (error) { showToast(`Ledger filter failed: ${error.message}`); }
  });
  $("#loadMore").addEventListener("click", async () => {
    try { await loadLedger(false); } catch (error) { showToast(`Could not load older records: ${error.message}`); }
  });
  bindAudio();
}

async function init() {
  bindEvents();
  try {
    const [summary, today, activity, speciesData, seasonality, epochs] = await Promise.all([
      fetchJSON("api/summary"), fetchJSON("api/today"), fetchJSON("api/activity?days=365"),
      fetchJSON("api/species"), fetchJSON("api/seasonality"), fetchJSON("api/epochs"),
    ]);
    renderSummary(summary);
    renderToday(today);
    renderActivity(activity);
    populateSpeciesControls(speciesData.species);
    renderSpecies(speciesData.species);
    renderSeasonality(seasonality);
    renderEpochs(epochs);
    await loadLedger(true);
  } catch (error) {
    console.error(error);
    $("#archiveStatus").innerHTML = '<span class="pulse stale"></span><div><strong>Archive unavailable</strong><small>The local mirror could not be read</small></div>';
    showToast(`Archive unavailable: ${error.message}`);
  }
}

document.addEventListener("DOMContentLoaded", init);
