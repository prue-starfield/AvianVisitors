import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../avian/archive/site/static/app.js', import.meta.url), 'utf8');

function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return data; },
  };
}

function harness(fetchImpl = async () => response({})) {
  const elements = {
    '#relatedCalls': {
      dataset: {}, innerHTML: '', cards: [],
      querySelectorAll() { return []; },
      insertAdjacentHTML(_where, html) { this.innerHTML += html; },
    },
    '#relatedMore': { disabled: false, hidden: false },
    '#relatedSort': { value: 'best' },
    '#relatedCount': { textContent: '' },
    '#comparisonExplanation': { textContent: '' },
    '#detectionAudio': { pauseCount: 0, pause() { this.pauseCount += 1; } },
    '#skipLink': { href: '' },
    '#toast': { textContent: '', classList: { add() {}, remove() {} } },
  };
  const location = {
    href: 'http://archive.test/birds/detection/' + 'a'.repeat(64),
    pathname: '/birds/detection/' + 'a'.repeat(64),
    search: '', hash: '',
  };
  const history = {
    state: {},
    replaceState(state, _title, href) {
      this.state = state;
      const parsed = new URL(href, location.href);
      location.href = parsed.href;
      location.pathname = parsed.pathname;
      location.search = parsed.search;
      location.hash = parsed.hash;
    },
  };
  const document = {
    querySelector(selector) { return elements[selector] || null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const routes = {
    PAGE_SIZE: 12,
    href(_name, params) { return `/birds/detection/${params.detectionId}`; },
  };
  const window = { location, ListeningGardenRoutes: routes };
  const context = vm.createContext({
    AbortController, clearTimeout, console, document, fetch: fetchImpl,
    history, Intl, location, setTimeout, URL, URLSearchParams, window,
  });
  window.history = history;
  vm.runInContext(source, context, { filename: 'app.js' });
  return { context, elements, history, location };
}

const emptyQueue = (sort, revision) => ({
  sort, revision, offset: 0, total: 0, calls: [],
});

test('older related response cannot overwrite a newer sort', async () => {
  const pending = [];
  const h = harness((url) => new Promise(resolve => pending.push({ url: String(url), resolve })));
  const first = vm.runInContext(`loadRelatedCalls('${'a'.repeat(64)}')`, h.context);
  h.elements['#relatedSort'].value = 'recent';
  const second = vm.runInContext(`loadRelatedCalls('${'a'.repeat(64)}')`, h.context);
  pending[1].resolve(response(emptyQueue('recent', '2'.repeat(64))));
  assert.equal(await second, true);
  pending[0].resolve(response(emptyQueue('best', '1'.repeat(64))));
  assert.equal(await first, false);
  assert.equal(h.elements['#relatedCalls'].dataset.revision, '2'.repeat(64));
});

test('failed replacement resets pagination and hides load more', async () => {
  const h = harness(async () => response({ error: 'broken' }, 503));
  const holder = h.elements['#relatedCalls'];
  holder.dataset.loaded = '12';
  holder.dataset.revision = '1'.repeat(64);
  assert.equal(await vm.runInContext(`loadRelatedCalls('${'a'.repeat(64)}')`, h.context), false);
  assert.equal(holder.dataset.loaded, '0');
  assert.equal(holder.dataset.revision, undefined);
  assert.equal(h.elements['#relatedMore'].hidden, true);
  assert.equal(h.elements['#relatedCount'].textContent, 'Related calls unavailable.');
});

test('revision refresh pauses players before replacing the queue', async () => {
  let request = 0;
  const h = harness(async () => {
    request += 1;
    return request === 1
      ? response({ error: 'related call queue changed; refresh required' }, 409)
      : response(emptyQueue('best', '2'.repeat(64)));
  });
  const holder = h.elements['#relatedCalls'];
  const relatedPlayer = { pauseCount: 0, pause() { this.pauseCount += 1; } };
  holder.dataset.loaded = '12';
  holder.dataset.revision = '1'.repeat(64);
  holder.querySelectorAll = selector => selector === 'audio' ? [relatedPlayer] : [];
  assert.equal(
    await vm.runInContext(`loadRelatedCalls('${'a'.repeat(64)}', true)`, h.context),
    true,
  );
  assert.equal(relatedPlayer.pauseCount, 1);
  assert.equal(h.elements['#detectionAudio'].pauseCount, 1);
  assert.equal(holder.dataset.revision, '2'.repeat(64));
});

test('non-Perch reviewer is labelled generically', () => {
  const h = harness();
  h.context.item = {
    detection_id: 'b'.repeat(64), review_status: 'confirmed',
    review_kind: 'independent', review_score: 0.9, confidence: 0.8,
    audio_quality: null, date: '2026-07-20', time: '12:00:00',
    observed_at_local: '2026-07-20T12:00:00', common_name: 'Robin',
  };
  const card = vm.runInContext('comparisonCard(item, 1)', h.context);
  assert.match(card, /Independent review supports/);
  assert.match(card, /Independent review claim score/);
  assert.doesNotMatch(card, /Perch claim score/);
});

test('related sort and loaded page round-trip through the URL', () => {
  const h = harness();
  vm.runInContext("persistRelatedState('contrast', 3)", h.context);
  assert.equal(h.location.search, '?related_sort=contrast&related_page=3');
  assert.equal(
    h.elements['#skipLink'].href,
    `/birds/detection/${'a'.repeat(64)}?related_sort=contrast&related_page=3#main`,
  );
  assert.equal(
    JSON.stringify(vm.runInContext('relatedStateFromURL()', h.context)),
    JSON.stringify({ sort: 'contrast', page: 3 }),
  );
});

test('persisted related page never exceeds the restorable ceiling', () => {
  const h = harness();
  vm.runInContext("persistRelatedState('best', 25)", h.context);
  assert.equal(h.location.search, '?related_page=20');
  assert.equal(h.history.state.relatedPage, 20);
  assert.equal(
    JSON.stringify(vm.runInContext('relatedStateFromURL()', h.context)),
    JSON.stringify({ sort: 'best', page: 20 }),
  );
});
