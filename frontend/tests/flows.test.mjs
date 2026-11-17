// Execute the actual production bundle with mocked HTTP responses. No sockets.
import { Window } from 'happy-dom';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
const html = await readFile(new URL('../dist/index.html', import.meta.url), 'utf8');
const asset = html.match(/src="(\/assets\/[^\"]+\.js)"/)[1];
const script = await readFile(new URL('../dist' + asset, import.meta.url), 'utf8');
const photo = { id: 'a'.repeat(64), url: '/media/test.jpg', sourceUrl: 'https://example.invalid/observation', license: 'cc-by', licenseUrl: 'https://creativecommons.org/licenses/by/4.0/', attribution: 'Test photographer, CC BY', creator: 'Test photographer' };
const plant = { taxonId: 'test:1', name: 'Test pine', scientificName: 'Pinus testii', image: photo };
const course = { id: 'co-epa3-test', state: 'Colorado', stateCode: 'CO', ecoregionCode: 'test', habitat: 'Test ecoregion', description: 'A synthetic test course', available: true, reason: null, total: 1, unitCount: 1, catalogVersion: 'test-version', units: [{ id: 'test-unit', title: 'Field notes 1', plants: [plant], questions: [{ id: 'question-1', prompt: 'Which plant is shown?', image: { ...photo, id: 'b'.repeat(64) }, options: ['Other plant', 'Test pine', 'Third plant', 'Fourth plant'] }] }] };
const unavailable = { ...course, id: 'az-epa3-test', state: 'Arizona', stateCode: 'AZ', habitat: 'Test desert', available: false, reason: 'Not enough licensed photos.', total: 0, unitCount: 0, units: [] };
async function settle(window) { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); await window.happyDOM.whenAsyncComplete(); }
async function setup(path = '/', ready = true) {
  const window = new Window({ url: 'http://localhost:5173' + path });
  const requests = []; let count = 0;
  window.fetch = async (url, options) => {
    requests.push({ url, options });
    let data;
    if (url === '/api/catalog') data = { ready, version: ready ? 'test-version' : null, coverage: null };
    else if (url === '/api/courses') data = ready ? [course, unavailable] : [];
    else if (url === '/api/progress') data = count ? { [course.id]: count } : {};
    else if (url === '/api/courses/' + course.id) data = course;
    else if (url === '/api/courses/' + unavailable.id) data = unavailable;
    else if (url.endsWith('/answers')) { const body = JSON.parse(options.body); const correct = body.choice === 1; if (correct) count = 1; data = { correct, explanation: 'This is Test pine (Pinus testii).', correctName: 'Test pine' }; }
    else if (url.endsWith('/reports')) data = { received: true };
    else throw new Error('Unexpected request: ' + url);
    return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } });
  };
  window.document.body.innerHTML = '<div id="root"></div>';
  window.eval(script);
  await settle(window);
  return { window, document: window.document, requests };
}
function button(document, text) { const node = [...document.querySelectorAll('button')].find(b => b.textContent.includes(text)); assert.ok(node, 'Missing button: ' + text); return node; }
function link(document, text) { const node = [...document.querySelectorAll('a')].find(a => a.textContent.includes(text)); assert.ok(node, 'Missing link: ' + text); return node; }

test('landing and habitats are separate, with state navigation and unavailable courses', async () => {
  const { window, document } = await setup();
  try {
    assert.equal(document.querySelectorAll('.course').length, 0);
    assert.match(document.querySelector('h1').textContent, /Learn your/);
    link(document, 'Find your habitat').click(); await settle(window);
    assert.equal(window.location.pathname, '/habitats');
    assert.equal(document.querySelectorAll('.course').length, 2);
    const select = document.querySelector('.section-heading select');
    select.value = 'CO'; select.dispatchEvent(new window.Event('change', { bubbles: true })); await settle(window);
    assert.equal(window.location.search, '?state=CO');
    assert.equal(document.querySelectorAll('.course').length, 1);
    window.history.pushState(null, '', '/habitats?state=AZ');
    window.dispatchEvent(new window.PopStateEvent('popstate')); await settle(window);
    assert.match(document.querySelector('.course').textContent, /More source data needed/);
    link(document, 'View availability').click(); await settle(window);
    assert.match(document.querySelector('main').textContent, /Not enough licensed photos/);
    assert.equal(document.querySelectorAll('.units button').length, 0);
  } finally { await window.happyDOM.close(); }
});

test('direct course links support introductions, held-out quizzes, retries, reporting and progress', async () => {
  const { window, document, requests } = await setup('/courses/co-epa3-test?state=CO');
  try {
    button(document, 'Field notes 1').click(); await settle(window);
    assert.equal(document.querySelector('.plant-photo img').getAttribute('alt'), 'Test pine introduction photograph');
    button(document, 'Ready to practice').click(); await settle(window);
    assert.equal(document.querySelector('.plant-photo img').getAttribute('alt'), 'Plant to identify');
    button(document, 'Other plant').click(); button(document, 'Check answer').click(); await settle(window);
    assert.match(document.querySelector('.feedback').textContent, /Take another look/);
    button(document, 'Try again').click(); await settle(window);
    button(document, 'Test pine').click(); button(document, 'Check answer').click(); await settle(window);
    assert.match(document.querySelector('.feedback').textContent, /You spotted it/);
    const answer = requests.filter(r => r.url.endsWith('/answers')).at(-1);
    assert.deepEqual(JSON.parse(answer.options.body), { question: 'question-1', choice: 1, catalogVersion: 'test-version' });
    const form = document.querySelector('.report form');
    form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })); await settle(window);
    assert.match(form.textContent, /Report received/);
    button(document, 'Continue').click(); await settle(window);
    assert.match(document.querySelector('main').textContent, /finished this unit/);
    link(document, 'All habitats').click(); await settle(window);
    assert.equal(window.location.search, '?state=CO');
    assert.match(document.querySelector('.course-bottom').textContent, /1 \/ 1 plants practiced/);
  } finally { await window.happyDOM.close(); }
});

test('missing published catalog is explicit and never replaced by sample courses', async () => {
  const { window, document } = await setup('/habitats', false);
  try {
    assert.match(document.querySelector('main').textContent, /catalog has not been published/);
    assert.equal(document.querySelectorAll('.course').length, 0);
  } finally { await window.happyDOM.close(); }
});
