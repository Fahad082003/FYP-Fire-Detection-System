// Browser tests for the Django dashboard (ports and extensions of frontend/e2e/sentinel.e2e.ts). Run via e2e/run.py.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { BASE, api, banner, closeBrowser, newPage, postFrame, resolveAll, setAiMode } from './helpers.js';

after(closeBrowser);
const POLL = { timeout: 15_000 }; // the AI status poll runs every 10 s

/** A signed-in page at `path` (default the dashboard) once its live link is up. */
async function dashboard(path = '/') {
  await setAiMode('ok');
  await resolveAll();
  const page = await newPage();
  await page.goto(`${BASE}${path}`);
  await page.locator('#link-label', { hasText: 'Live' }).waitFor();
  return page;
}

test('login: validation, wrong password, then sign-in reaches the dashboard', async () => {
  const page = await newPage({ signedIn: false });
  await page.goto(`${BASE}/`);
  assert.match(page.url(), /\/login\?next=\/$/); // protected page -> sign-in
  await page.click('button[type=submit]');
  await page.getByText('Enter a valid email address.').waitFor();
  await page.fill('input[name=email]', process.env.ADMIN_EMAIL);
  await page.fill('input[name=password]', 'wrong-password');
  await page.click('button[type=submit]');
  assert.equal(await page.getByRole('alert').textContent(), 'Invalid email or password.');
  await page.fill('input[name=password]', process.env.ADMIN_PASSWORD);
  await page.click('button[type=submit]');
  await page.waitForURL(`${BASE}/`);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('dashboard: cameras, sensors, sprinklers and a green banner only once the AI and the live link are confirmed', async () => {
  await setAiMode('ok');
  await resolveAll();
  const page = await newPage();
  // Hold the live link and the AI status until released: reading the banner "right after load" raced the data, which may
  // correctly arrive first and turn it green.
  const hold = async (url) => {
    let release;
    const released = new Promise((ok) => { release = ok; });
    await page.route(url, async (route) => { await released; await route.continue(); });
    return release;
  };
  const releaseLive = await hold('**/api/events'), releaseAi = await hold('**/api/system/health');
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await banner(page).filter({ hasText: /^MONITORING DEGRADED · LIVE LINK DOWN$/ }).waitFor(); // neither confirmed: never green
  releaseLive();
  await banner(page).filter({ hasText: /^MONITORING DEGRADED · AI STATUS UNKNOWN$/ }).waitFor(); // live data alone: still not green
  releaseAi();
  await banner(page).filter({ hasText: 'ALL SYSTEMS NOMINAL' }).waitFor(POLL);
  assert.equal(await page.locator('.tile').count(), 4);
  assert.equal(await page.locator('#ai-dot').getAttribute('class'), 'dot dot-green');
  assert.equal(await page.locator('#kpis .kpi', { hasText: 'Open Incidents' }).locator('.kpi-value').textContent(), '0'); // the card's number, not its status chip
  for (const [path, selector, count] of [['/sensors', '.sensor', 6], ['/sprinklers', '.relay', 5]]) {
    await page.goto(`${BASE}${path}`);
    await page.locator(selector).nth(count - 1).waitFor();
    assert.equal(await page.locator(selector).count(), count, path);
  }
  await page.goto(`${BASE}/activity`);
  await page.locator('#activity', { hasText: 'Realtime link established' }).waitFor();
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('a dashboard whose page modules load after the live snapshot still renders it (was an empty page until the next event)', async () => {
  await setAiMode('ok');
  const page = await newPage();
  await page.route('**/static/sentinel/js/planmap.js', async (route) => { await new Promise((ok) => setTimeout(ok, 1500)); await route.continue(); });
  await page.goto(`${BASE}/sensors`);
  await page.locator('.sensor').nth(5).waitFor();
  assert.equal(await page.locator('.sensor').count(), 6);
  await page.goto(`${BASE}/`);
  await page.locator('.tile').nth(3).waitFor();
  assert.equal(await page.locator('.tile').count(), 4);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('status banner follows AI health: offline, loading, model missing, camera offline, then nominal again', async () => {
  const page = await dashboard();
  for (const [mode, text] of [['offline', 'MONITORING DEGRADED · AI OFFLINE'], ['loading', 'MONITORING DEGRADED · AI LOADING'],
    ['model-missing', 'MONITORING DEGRADED · AI MODEL MISSING'],
    ['camera-error', 'MONITORING DEGRADED · CAMERA OFFLINE'], ['ok', 'ALL SYSTEMS NOMINAL']]) {
    await setAiMode(mode);
    await banner(page).filter({ hasText: new RegExp(`^${text}$`) }).waitFor(POLL);
    assert.equal(await banner(page).getAttribute('class'), `status-pill level-${mode === 'ok' ? 'nominal' : 'elevated'}`);
  }
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('system health panel: component states from the server, AI loading then ready, a failed check is critical and old data is marked', async () => {
  const page = await dashboard('/notifications');
  await page.locator('#health-details-toggle').click(); // the table is behind "Show details"
  const row = (name) => page.locator('#health tr', { hasText: name }).first();
  const overall = page.locator('#health-overall');
  await row('Database').filter({ hasText: 'healthy' }).waitFor(POLL);
  for (const name of ['Django Backend', 'AI Service', 'AI Models', 'Cameras', 'Sensors', 'Live Updates', 'Simulator']) await row(name).waitFor();
  // This page's own stream, seen by the server (the first check on page load may run before the stream is open: next poll).
  await row('Live Updates').filter({ hasText: /healthy.*1 dashboard\(s\) connected/s }).waitFor(POLL);
  assert.match(await row('Simulator').textContent(), /\(optional\)/);

  await setAiMode('loading');
  await row('AI Service').filter({ hasText: /loading.*models still loading/s }).waitFor(POLL);
  assert.match(await row('AI Models').textContent(), /loading/);
  assert.notEqual(await overall.textContent(), 'HEALTHY'); // never healthy while the AI loads
  await setAiMode('model-missing');
  await row('AI Models').filter({ hasText: /degraded.*fire_smoke: file not found/s }).waitFor(POLL);
  await setAiMode('ok');
  await row('AI Service').filter({ hasText: /healthy.*Ready/s }).waitFor(POLL);
  const checked = await page.locator('#health-checked').textContent();
  assert.match(checked, /^Last successful check /);

  await page.route('**/api/system/health', (route) => route.abort()); // the server stops answering
  await overall.filter({ hasText: /^CRITICAL$/ }).waitFor(POLL);
  await page.locator('#health-checked', { hasText: 'latest check failed' }).waitFor();
  assert.match(await row('Database').textContent(), /healthy \(old\)/); // the last answer stays visible, never as current
  await banner(page).filter({ hasText: 'MONITORING DEGRADED · AI STATUS UNKNOWN' }).waitFor();
  await page.unroute('**/api/system/health');
  await page.waitForFunction(() => !document.getElementById('health').textContent.includes('(old)'), null, POLL); // current again
  assert.notEqual(await overall.textContent(), 'CRITICAL');
  await page.context().close();
});

test('IL-9: frames stop -> "AI DATA STALE" within 9 s; with the AI offline an open incident stays listed and the reason stays visible', async () => {
  const page = await dashboard();
  for (let i = 0; i < 3; i++) {
    await postFrame({ cameraId: 'cam-lobby', detections: [{ category: 'Human', confidence: 0.9, bbox: [100, 100, 50, 150], trackId: 'p1' }] });
    await page.waitForTimeout(1000);
  }
  await page.locator('.tile', { hasText: 'Main Lobby' }).getByText('LIVE', { exact: true }).waitFor();
  const stoppedAt = Date.now();
  await banner(page).filter({ hasText: 'MONITORING DEGRADED · AI DATA STALE' }).waitFor({ timeout: 12_000 });
  assert.ok(Date.now() - stoppedAt < 10_000, 'stale data must be flagged within ~9 s');

  const smoke = await postFrame({ cameraId: 'cam-warehouse', detections: [{ category: 'Smoke', confidence: 0.6, bbox: [10, 10, 200, 100] }] });
  assert.equal(smoke.status, 202);
  await setAiMode('offline');
  // Also open by now: the watchdog's "Camera lost" for cameras that went silent for more than 10 s.
  await banner(page).filter({ hasText: /^ELEVATED RISK · \d+ OPEN · AI OFFLINE$/ }).waitFor(POLL);
  const list = await newPage();
  await list.goto(`${BASE}/incidents`);
  const rows = list.locator('#incident-rows');
  await rows.locator('tr', { hasText: 'Warehouse Floor' }).waitFor();
  assert.match(await rows.locator('tr', { hasText: 'Warehouse Floor' }).textContent(), /Medium.*AI Vision.*Open/s); // still listed while the AI is down
  assert.match(await rows.locator('tr', { hasText: 'Main Lobby' }).textContent(), /Medium.*Camera lost.*Open/s);

  await setAiMode('ok');
  await banner(page).filter({ hasText: /^ELEVATED RISK · \d+ OPEN · AI DATA STALE$/ }).waitFor(POLL); // AI back, incidents still open
  assert.equal(await rows.locator('tr', { hasText: 'Warehouse Floor' }).filter({ hasText: 'AI Vision' }).count(), 1);
  assert.deepEqual([...page.problems, ...list.problems], []);
  await list.context().close();
  await page.context().close();
});

test('incidents: a fire alert appears live with a toast, raises the alarm frame, and is resolved from the alert log', async () => {
  const page = await dashboard('/incidents');
  await postFrame({ cameraId: 'cam-server', fireSeverity: 'High', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 200, 200] }] });
  await page.locator('.toast', { hasText: /HIGH — Incident #\d+/ }).waitFor();
  await banner(page).filter({ hasText: 'CRITICAL ALERT · 1 OPEN' }).waitFor();
  assert.equal(await page.locator('#alarm-frame').isVisible(), true);
  const row = page.locator('#incident-rows tr', { hasText: 'Server Hall' });
  assert.match(await row.textContent(), /High.*First Floor - Server Hall \(Server Hall\).*AI Vision.*High/s);
  await row.getByRole('button', { name: 'Resolve' }).click();
  await page.getByText('No open incidents.').waitFor();
  await banner(page).filter({ hasText: 'ALL SYSTEMS NOMINAL' }).waitFor(POLL);
  assert.equal(await page.locator('#alarm-frame').isVisible(), false);
  await page.locator('#incident-filter').getByText('All').click();
  assert.match(await page.locator('#incident-rows').textContent(), /Resolved/);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('false alarm: an operator marks an open incident as a false alarm after confirming; it stays in the history', async () => {
  const page = await dashboard('/incidents');
  const { body } = await postFrame({ cameraId: 'cam-lobby', fireSeverity: 'Medium', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 200, 200] }] });
  const row = page.locator('#incident-rows tr', { hasText: 'Ground Floor - Lobby (Main Lobby)' });
  await row.getByRole('button', { name: 'False alarm' }).click();
  await page.locator('#confirm-dialog').getByRole('button', { name: 'Cancel' }).click();
  assert.match(await row.textContent(), /Open/); // cancelled: nothing changed
  await row.getByRole('button', { name: 'False alarm' }).click();
  await page.locator('#confirm-dialog').getByRole('button', { name: 'False alarm' }).click();
  await page.getByText('No open incidents.').waitFor();
  await page.locator('#incident-filter').getByText('All').click();
  await row.filter({ hasText: 'False alarm' }).waitFor();
  assert.equal((await api(`/api/incidents/${body.incidentId}`)).falseAlarm, true);
  assert.equal((await api(`/api/incidents/logs?incidentId=${body.incidentId}&type=FalseAlarm`)).length, 1);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('fire size labels: the AI fire severity keeps its name and adds Small/Medium/Large in the tile, alert log and history', async () => {
  const page = await dashboard();
  const texts = await page.evaluate(async () => {
    const { fireSeverityText } = await import('/static/sentinel/js/core.js');
    return ['Low', 'Medium', 'High', 'Critical', null, 'Unknown'].map(fireSeverityText);
  });
  assert.deepEqual(texts, ['Low · Small', 'Medium · Medium', 'High · Large', 'Critical · Large', null, 'Unknown']); // no guessed size
  await postFrame({ cameraId: 'cam-warehouse', fireSeverity: 'Low', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 60, 60] }] });
  await page.locator('.tile', { hasText: 'Warehouse Floor' }).locator('.chips', { hasText: 'FIRE Low · Small' }).waitFor();
  await page.goto(`${BASE}/incidents`);
  await page.locator('#incident-rows tr', { hasText: 'Warehouse Floor' }).filter({ hasText: 'Low · Small' }).waitFor();
  await page.locator('#history-table', { hasText: 'FIRE Low · Small' }).waitFor();
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('fire trend: the history chart of a camera incident shows the server trend, "Insufficient data" with one measurement', async () => {
  const page = await dashboard('/incidents');
  await postFrame({ cameraId: 'cam-server', fireSeverity: 'Low', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 60, 60] }] });
  await page.locator('#incident-rows tr', { hasText: 'Server Hall' }).waitFor();
  const [incident] = (await api('/api/incidents?resolved=false')).filter((i) => i.cameraId === 'cam-server');
  await page.selectOption('#history-incident', String(incident.id));
  const trend = page.locator('#history-chart [data-trend]');
  await trend.filter({ hasText: 'Insufficient data' }).waitFor();
  assert.match(await trend.textContent(), /not physical size or a spread forecast/);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('risk warning: a room within 5 m of a fire appears in the Live Event Log and the incident history', async () => {
  const plan = await api('/api/floorplans', { method: 'POST', body: JSON.stringify({ name: 'Risk e2e', building: 'E2E', floorNumber: 0,
    widthMeters: 20, heightMeters: 4, elements: [
      { type: 'Corridor', name: 'Hall', x: 0, y: 0, width: 20, height: 2 },
      { type: 'DangerZone', name: 'Lobby fire', x: 0, y: 0, width: 1, height: 2, zone: 'Ground Floor - Lobby' },
      { type: 'Room', name: 'Near room', x: 5, y: 0, width: 1, height: 2 }] }) });
  try {
    const page = await dashboard('/activity');
    await postFrame({ cameraId: 'cam-lobby', fireSeverity: 'Low', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 60, 60] }] });
    await page.locator('#activity', { hasText: "Proximity-based risk warning (not a fire-spread prediction): room 'Near room' is 5 m" }).waitFor();
    await page.locator('#history-table tr', { hasText: 'RiskWarning' }).first().waitFor();
    assert.deepEqual(page.problems, []);
    await page.context().close();
  } finally {
    await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' }); // keep the other tests' floor plans unaffected
  }
});

test('route comparison and rescue list: every exit with its status, then camera person markers with their nearest safe exit', async () => {
  const plan = await api('/api/floorplans', { method: 'POST', body: JSON.stringify({ name: 'Compare e2e', building: 'E2E', floorNumber: 0,
    widthMeters: 20, heightMeters: 10, elements: [
      { type: 'Corridor', name: 'Hall', x: 0, y: 4, width: 20, height: 2 },
      { type: 'Exit', name: 'East', x: 19, y: 4, width: 1, height: 2 }, { type: 'Exit', name: 'West', x: 0, y: 4, width: 1, height: 2 },
      { type: 'Exit', name: 'North', x: 9, y: 3, width: 2, height: 1, isBlocked: true }] }) });
  // cam-lobby's frame (640 x 480) mapped onto this plan, as ai-service/tools/live_test.py does
  await api('/api/cameras/cam-lobby/calibration', { method: 'PUT', body: JSON.stringify({ floorPlanId: plan.id,
    imagePoints: [[0, 240], [640, 240], [640, 480], [0, 480]], planPoints: [[2, 3], [18, 3], [18, 7], [2, 7]] }) });
  try {
    const page = await dashboard('/map');
    await page.selectOption('#evac-plan', String(plan.id));
    const map = page.locator('#evacuation svg');
    await map.scrollIntoViewIfNeeded();
    const m = await map.boundingBox();
    const s = Math.min(m.width, m.height * 21.8 / 11.8) / 21.8;
    await page.mouse.click(m.x + m.width / 2 + (16 - 10) * s, m.y + m.height / 2 + (5 - 5) * s); // start at (16, 5) m
    const table = page.locator('#evacuation [data-exits]');
    await table.locator('tr', { hasText: 'North' }).filter({ hasText: 'Blocked' }).waitFor();
    assert.match(await table.locator('tbody tr').first().textContent(), /East\s*Available\s*[\d.]+ m\s*~\d+ s/); // cells join without spaces
    assert.match(await table.locator('tr', { hasText: 'North' }).textContent(), /Blocked\s*—\s*—/); // no route: no distance or time
    await table.locator('tr', { hasText: 'West' }).getByRole('button', { name: 'Show' }).click();
    await table.locator('tr', { hasText: 'West' }).getByRole('button', { name: 'Shown' }).waitFor();

    // A person seen by the calibrated camera: feet at (320, 400) px -> about (10, 5.7) m on the plan
    const person = { cameraId: 'cam-lobby', detections: [{ category: 'Human', confidence: 0.9, bbox: [300, 300, 40, 100] }] };
    await postFrame(person);
    await postFrame({ cameraId: 'cam-server', detections: [{ category: 'Human', confidence: 0.9, bbox: [100, 100, 40, 100] }] });
    const rescue = page.locator('#evacuation [data-rescue-list]');
    await rescue.locator('tr', { hasText: 'Person marker 1' }).filter({ hasText: /East|West/ }).waitFor({ timeout: 12_000 });
    assert.match(await rescue.textContent(), /Not in an active danger zone \(plan\)/);
    await page.locator('#evacuation [data-rescue]', { hasText: 'location unknown (camera not calibrated to this plan)' }).waitFor(); // cam-server's frame
    assert.match(await page.locator('#evacuation [data-rescue]').textContent(), /not identified\s+people/);
    await rescue.locator('tr', { hasText: 'Location stale' }).waitFor({ timeout: 6000 }); // no new frame: the marker ages
    await page.locator('#evacuation [data-rescue]', { hasText: 'No person markers from cameras calibrated to this plan right now.' })
      .waitFor({ timeout: 6000 }); // expired markers are not listed as current people
    assert.deepEqual(page.problems, []);
    await page.context().close();
  } finally {
    await api('/api/cameras/cam-lobby/calibration', { method: 'DELETE' });
    await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
  }
});

test('camera tile shows live AI counts and the overlay canvas, then "NO AI DATA" when frames stop', async () => {
  const page = await dashboard();
  const tile = page.locator('.tile', { hasText: 'Main Lobby' });
  await postFrame({ cameraId: 'cam-lobby', detections: [{ category: 'Human', confidence: 0.9, bbox: [100, 100, 50, 150], trackId: 'p7' },
    { category: 'Human', confidence: 0.8, bbox: [300, 100, 50, 150], trackId: 'p8' }] });
  await tile.locator('.chips', { hasText: /HUM\s*2/ }).waitFor(); // counts are refreshed 4 times a second
  await tile.getByText('LIVE', { exact: true }).waitFor();
  assert.equal(await tile.locator('canvas').count(), 1);
  await tile.getByText('NO AI DATA').waitFor({ timeout: 8000 }); // stale after 4 s without frames
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('devices: validation errors, add, edit and delete a camera (with confirmation)', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/devices`);
  await page.getByRole('link', { name: '+ Camera' }).click();
  await page.fill('input[name=id]', 'cam-e2e');
  await page.fill('input[name=name]', 'E2E Gate');
  await page.fill('input[name=location_zone]', 'Gate');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.getByText('Zone must be "Floor - Area"').waitFor(); // server-side validation, value kept
  assert.equal(await page.inputValue('input[name=name]'), 'E2E Gate');
  await page.fill('input[name=location_zone]', 'Ground Floor - Gate');
  await page.fill('input[name=stream_url]', 'rtsp://user:secret@10.0.0.20/live');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.locator('.toast', { hasText: 'Camera saved: E2E Gate' }).waitFor();
  const row = page.locator('tr', { hasText: 'cam-e2e' });
  assert.match(await row.textContent(), /rtsp:\/\/\*\*\*@10\.0\.0\.20\/live/); // the password never reaches the page

  await row.getByRole('link', { name: 'Edit' }).click();
  assert.equal(await page.inputValue('input[name=stream_url]'), 'rtsp://***@10.0.0.20/live');
  await page.fill('input[name=name]', 'E2E Front Gate');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.locator('tr', { hasText: 'E2E Front Gate' }).waitFor();
  assert.equal((await api('/api/cameras')).find((c) => c.id === 'cam-e2e').streamUrl, 'rtsp://***@10.0.0.20/live');

  await page.locator('tr', { hasText: 'cam-e2e' }).getByRole('button', { name: 'Delete' }).click();
  await page.locator('#confirm-dialog').getByRole('button', { name: 'Delete' }).click();
  await page.locator('.toast', { hasText: 'Camera deleted: E2E Front Gate' }).waitFor();
  assert.equal(await page.locator('tr', { hasText: 'cam-e2e' }).count(), 0);
  assert.equal((await postFrame({ cameraId: 'cam-e2e' })).status, 404); // a deleted camera's late frames are refused
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('AI page: status from the service, a wrong file is refused in the browser, a failed analysis shows the server error', async () => {
  await setAiMode('ok');
  const page = await newPage();
  await page.goto(`${BASE}/ai`);
  await page.getByText('ONLINE · all models loaded').waitFor();
  let posted = false;
  page.on('request', (r) => { if (r.method() === 'POST' && r.url().endsWith('/ai')) posted = true; });
  await page.setInputFiles('input[name=file]', { name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('not an image') });
  await page.getByRole('button', { name: 'Analyze' }).click();
  assert.equal(await page.locator('input[name=file]').evaluate((i) => i.validationMessage), 'Choose a PNG or JPG image.');
  assert.equal(posted, false);
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');
  await page.setInputFiles('input[name=file]', { name: 'test.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('button', { name: 'Analyze' }).click();
  await page.getByRole('alert').filter({ hasText: 'AI model not available' }).waitFor();
  await setAiMode('offline');
  await page.goto(`${BASE}/ai`);
  await page.getByText('OFFLINE · Health check returned HTTP 503.').waitFor();
  await setAiMode('ok');
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('floor plans: draw a corridor and an exit, save, then the dashboard routes a clicked person to the exit', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/floor-plans`);
  await page.getByRole('button', { name: '+ New' }).click();
  await page.getByText('Floor plan created').waitFor();
  const svg = page.locator('#editor-map svg');
  await svg.waitFor();
  const box = await svg.boundingBox();
  // The 30 x 20 m plan is fitted into the SVG with 3 % padding: plan meters -> screen pixels.
  const scale = Math.min(box.width, box.height * 32 / 21.8) / 31.8;
  const toScreen = (x, y) => [box.x + box.width / 2 + (x - 15) * scale, box.y + box.height / 2 + (y - 10) * scale];
  const drag = async (tool, [x0, y0], [x1, y1]) => {
    await page.locator('#tools').getByRole('button', { name: tool }).click();
    await page.mouse.move(...toScreen(x0, y0));
    await page.mouse.down();
    await page.mouse.move(...toScreen(x1, y1), { steps: 5 });
    await page.mouse.up();
  };
  await drag('+ Corridor', [2, 8], [28, 12]);
  await drag('+ Exit', [27, 8], [28, 12]);
  assert.equal(await page.locator('#editor-title').textContent(), 'New floor plan · unsaved');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.locator('.toast', { hasText: 'Floor plan saved' }).waitFor();
  const plans = await api('/api/floorplans');
  const plan = plans.find((p) => p.name === 'New floor plan');
  assert.deepEqual(plan.elements.map((e) => e.type), ['Corridor', 'Exit']);

  await page.goto(`${BASE}/map`);
  await page.selectOption('#evac-plan', String(plan.id));
  const map = page.locator('#evacuation svg');
  await map.scrollIntoViewIfNeeded();
  const m = await map.boundingBox();
  const s = Math.min(m.width, m.height * 31.8 / 21.8) / 31.8;
  await page.mouse.click(m.x + m.width / 2 + (5 - 15) * s, m.y + m.height / 2 + (10 - 10) * s);
  // The route is recalculated, through "Calculating…", whenever the set of incident zones changes (e.g. a "camera lost" incident
  // for an earlier test's camera, 10 s after its last frame): wait for the settled result in one step, never read it twice.
  const settled = page.locator('#evacuation [data-result]', { hasText: 'Estimated walking time',
    has: page.locator('p:first-child', { hasText: /^Route to exit 'Exit 1'\.$/ }) });
  await settled.waitFor();
  assert.equal(await page.locator('#evacuation polyline').count(), 1);
  // Regression: an incident in a new zone while the route is shown recalculates it and it settles on the same route again.
  const recalculated = page.waitForResponse((r) => r.url().endsWith(`/api/floorplans/${plan.id}/route`));
  const other = await api('/api/incidents/dispatch', { method: 'POST', body: JSON.stringify({ zone: 'E2E Route Zone', severity: 'Medium' }) });
  await recalculated;
  await settled.waitFor();
  await api(`/api/incidents/${other.id}/resolve`, { method: 'POST' });
  await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('overview: building status, device counts, fire and smoke apart, camera state and health cards follow the server', async () => {
  const page = await dashboard();
  const card = (label) => page.locator('#kpis .kpi', { hasText: label });
  const [cameras, sensors, sprinklers] = await Promise.all(['/api/cameras', '/api/iot/sensors', '/api/iot/sprinklers'].map((p) => api(p)));
  for (const [label, list] of [['Cameras', cameras], ['Sensors', sensors]]) { // "X of Y working"
    assert.match(await card(label).locator('.kpi-hint').textContent(), new RegExp(`^of ${list.length} working`), label);
  }
  assert.equal(Number(await card('Sprinklers').locator('.kpi-value').textContent()), sprinklers.length);
  const status = page.locator('#building-status');
  const agrees = () => page.waitForFunction(() => { // Safe / Warning / Emergency always say what the status banner says
    const pill = document.getElementById('status-pill').textContent.split(' ')[0];
    return { ALL: 'Building is Safe', MONITORING: 'Warning', ELEVATED: 'Warning', CRITICAL: 'Emergency' }[pill] === document.querySelector('#building-status [data-title]').textContent;
  }, null, POLL);
  await agrees();
  if (await status.locator('[data-title]').textContent() === 'Building is Safe') assert.equal(await status.locator('[data-text]').textContent(), 'No fire or smoke anywhere.');

  for (let i = 0; i < 3; i++) { // simulated data: no sprinkler is switched
    await postFrame({ cameraId: 'cam-warehouse', simulated: true, fireSeverity: 'High', detections: [
      { category: 'Fire', confidence: 0.91, bbox: [200, 150, 160, 140] }, { category: 'Smoke', confidence: 0.77, bbox: [180, 60, 220, 100] }] });
    await postFrame({ cameraId: 'cam-lobby', simulated: true, detections: [{ category: 'Smoke', confidence: 0.66, bbox: [100, 80, 200, 120] }] });
  }
  await page.locator('.tile', { hasText: 'Warehouse Floor' }).locator('.tile-state', { hasText: 'Fire Detected' }).waitFor();
  await page.locator('.tile', { hasText: 'Main Lobby' }).locator('.tile-state', { hasText: 'Smoke Detected' }).waitFor();
  await status.locator('[data-title]', { hasText: /^(Emergency|Warning)$/ }).waitFor();
  await page.locator('#attention .att', { hasText: 'Warehouse' }).first().waitFor(); // the problem is listed under "Needs your attention"
  await agrees();
  assert.match(await status.locator('[data-text]').textContent(), /Warehouse/);

  const list = await newPage(); // the fire and smoke cards on the incidents page, while the frames are still current
  await list.goto(`${BASE}/incidents`);
  for (let i = 0; i < 2; i++) {
    await postFrame({ cameraId: 'cam-warehouse', simulated: true, fireSeverity: 'High', detections: [
      { category: 'Fire', confidence: 0.91, bbox: [200, 150, 160, 140] }, { category: 'Smoke', confidence: 0.77, bbox: [180, 60, 220, 100] }] });
    await postFrame({ cameraId: 'cam-lobby', simulated: true, detections: [{ category: 'Smoke', confidence: 0.66, bbox: [100, 80, 200, 120] }] });
  }
  const fire = list.locator('#fire-smoke .fs-fire'), smoke = list.locator('#fire-smoke .fs-smoke');
  await fire.filter({ hasText: 'Warehouse Floor' }).filter({ hasText: 'confidence 91%' }).waitFor();
  assert.doesNotMatch(await fire.locator('.fs-list li', { hasText: 'AI camera' }).allTextContents().then((t) => t.join('|')), /Main Lobby/); // smoke alone is never a fire
  await smoke.filter({ hasText: 'Main Lobby' }).filter({ hasText: 'confidence 66%' }).waitFor();
  assert.equal((await api('/api/iot/sprinklers')).filter((s) => s.isActive).length, 0);

  await list.goto(`${BASE}/notifications`); // the health cards
  await list.locator('#health-strip .hs', { hasText: 'Database' }).filter({ hasText: 'Healthy' }).waitFor(POLL);
  await list.locator('#health-strip .hs', { hasText: 'AI Models' }).filter({ hasText: 'Healthy' }).waitFor(POLL);
  for (const [name, path] of [['Live Cameras', '/cameras'], ['Floor Plan', '/map'], ['Occupancy & Heatmap', '/occupancy'], ['Incidents', '/incidents'],
    ['Sensors', '/sensors'], ['Sprinklers', '/sprinklers'], ['Activity Log', '/activity'], ['Reports & Analytics', '/reports'], ['AI Settings', '/ai'],
    ['Notifications', '/notifications'], ['Settings', '/devices'], ['Overview', '/']]) {
    await list.locator('.sidebar .nav-link', { has: list.getByText(name, { exact: true }) }).click(); // every menu item opens its page
    await list.waitForURL(`${BASE}${path}`);
    assert.equal(await list.locator('.sidebar .nav-link[aria-current="page"] .nav-label').textContent(), name); // and is the highlighted one
  }
  assert.deepEqual([...page.problems, ...list.problems], []);
  await resolveAll();
  await list.context().close();
  await page.context().close();
});

test('accessibility basics and no broken routes on every page, also at phone width', async () => {
  for (const width of [1600, 375]) {
    const page = await newPage();
    await page.setViewportSize({ width, height: 900 });
    for (const path of ['/', '/cameras', '/sensors', '/sprinklers', '/incidents', '/activity', '/map', '/occupancy', '/floor-plans', '/ai',
      '/notifications', '/devices', '/devices?add', '/reports']) {
      const response = await page.goto(`${BASE}${path}`);
      assert.equal(response.status(), 200, path);
      await page.waitForLoadState('networkidle').catch(() => undefined); // the live stream keeps one request open
      const unnamed = await page.evaluate(() => [...document.querySelectorAll('input:not([type=hidden]), select, textarea, button, a[href]')]
        .filter((el) => el.offsetParent !== null || el.classList.contains('sr-only'))
        .filter((el) => !(el.labels?.length || el.getAttribute('aria-label') || el.title || el.textContent.trim()))
        .map((el) => el.outerHTML.slice(0, 120)));
      assert.deepEqual(unnamed, [], `${path}: controls without an accessible name`);
      assert.equal(await page.locator('main').count(), 1, path);
      assert.equal(await page.locator('#status-pill[role=status]').count(), 1, path);
      assert.deepEqual(await page.evaluate(() => [...document.images].filter((i) => !i.hasAttribute('alt')).length), 0, path);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 1, `${path} at ${width}px scrolls sideways by ${overflow}px`);
    }
    assert.deepEqual(page.problems, []);
    await page.context().close();
  }
});

test('fusion: a camera fire and a smoke warning in the same zone mark the incident "+ SMOKE SENSOR" and log it once', async () => {
  const page = await dashboard('/incidents');
  const { body } = await postFrame({ cameraId: 'cam-server', fireSeverity: 'Medium', detections: [{ category: 'Fire', confidence: 0.9, bbox: [100, 100, 200, 200] }] });
  const row = page.locator('#incident-rows tr', { hasText: 'First Floor - Server Hall (Server Hall)' });
  await row.waitFor();
  assert.doesNotMatch(await row.textContent(), /SMOKE SENSOR/); // a camera alone is not corroborated
  const smoke = (value) => fetch(`${BASE}/api/iot/telemetry`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Device-Key': process.env.SENSOR_KEY },
    body: JSON.stringify({ readings: [{ sensorId: 'smoke-c1', value }], simulated: true }) });
  await smoke(250); // Warning (limit 300 ppm)
  await row.filter({ hasText: '+ SMOKE SENSOR' }).waitFor();
  await page.locator('#history-table', { hasText: 'Corroborated' }).waitFor();
  await smoke(260);
  const logs = await api(`/api/incidents/logs?incidentId=${body.incidentId}&type=Corroborated`);
  assert.equal(logs.length, 1);
  assert.match(logs[0].details, /^Smoke sensor smoke-c1 in the same zone read 250ppm/);
  assert.equal((await api(`/api/incidents/${body.incidentId}`)).severity, 'Medium'); // a record only, never a severity change
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('reports: totals, false-alarm rate and breakdowns for the last 7 days; a custom empty range says so, an invalid one is refused', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/`);
  await page.getByRole('link', { name: 'Reports' }).click();
  await page.waitForURL(`${BASE}/reports`);
  const kpi = async (label) => Number(await page.locator('#report-kpis .kpi').filter({ has: page.getByText(label, { exact: true }) }).locator('.kpi-value').textContent());
  const row = async (name) => (await page.locator('#report-empty').count()) ? 0 // no incidents yet (test run alone): no tables
    : Number(await page.locator('table.data tr').filter({ has: page.getByRole('cell', { name, exact: true }) }).locator('td.num').textContent());
  // The watchdog opens CameraLost/SensorLost incidents for earlier tests' cameras and sensors at any moment (10 s / 5 s after their
  // last frame or reading), so the total is compared without them, all values from one page render.
  const counts = async () => ({ total: await kpi('Total incidents') - await row('CameraLost') - await row('SensorLost'),
    falseAlarms: await kpi('False alarms'), critical: await row('Critical'), manual: await row('Manual') });
  const before = await counts();
  const created = await api('/api/incidents/dispatch', { method: 'POST', body: JSON.stringify({ zone: 'Report Zone', severity: 'Critical', activateSprinklers: false }) });
  await api(`/api/incidents/${created.id}/false-alarm`, { method: 'POST' });
  await page.reload();
  assert.deepEqual(await counts(), { total: before.total + 1, falseAlarms: before.falseAlarms + 1, critical: before.critical + 1, manual: before.manual + 1 });
  assert.match(await page.locator('#false-alarm-rate').textContent(), /^\d+(\.\d)?% of resolved$/);
  assert.equal(await page.locator('.day-bar').count(), 7);

  await page.fill('input[name=start]', '2026-01-01');
  await page.fill('input[name=end]', '2026-01-31');
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.waitForURL(`${BASE}/reports?start=2026-01-01&end=2026-01-31`);
  await page.locator('#report-empty', { hasText: 'No incidents were opened in this period.' }).waitFor();
  assert.equal(await kpi('Total incidents'), 0);
  assert.equal(await page.locator('#false-alarm-rate').textContent(), 'no rate: nothing resolved');

  await page.fill('input[name=start]', '2026-02-01');
  await page.getByRole('button', { name: 'Apply' }).click();
  await page.getByRole('alert').filter({ hasText: 'Start date must be on or before the end date.' }).waitFor();
  assert.equal(await page.locator('#report-kpis').count(), 0);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

// Last on purpose: a sensor read here goes Stale again 5 s later (Alerts__SensorStaleSeconds=5 in e2e/run.py).
test('sensor staleness: a silent sensor turns Stale with its last reading time and a "Sensor lost" incident; a reading restores it', async () => {
  const page = await dashboard('/sensors');
  const card = page.locator('.sensor', { hasText: 'temp-c1' });
  const read = (value) => fetch(`${BASE}/api/iot/sensors/temp-c1/readings`, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Device-Key': process.env.SENSOR_KEY }, body: JSON.stringify({ value }) });
  const relays = async () => (await api('/api/iot/sprinklers')).map((s) => `${s.id}:${s.isActive}`);
  const before = await relays();
  assert.equal(await card.locator('.state-badge').textContent(), 'Not reporting'); // never reported: not shown as Normal
  assert.match(await card.textContent(), /no reading yet/);
  await read(21);
  await card.locator('.state-badge', { hasText: 'Normal' }).waitFor();
  await card.locator('.state-badge', { hasText: 'Not reporting' }).waitFor(POLL); // pushed by the server, no page reload
  assert.match(await card.textContent(), /last reading /);
  const list = await newPage();
  await list.goto(`${BASE}/incidents`);
  const row = list.locator('#incident-rows tr', { hasText: '(sensor temp-c1)' });
  await row.filter({ hasText: 'Sensor lost' }).waitFor();
  const [episode] = (await api('/api/incidents?resolved=false')).filter((i) => i.source === 'SensorLost' && i.sensorId === 'temp-c1');
  assert.match(episode.message, /^Sensor unavailable: no valid reading from Temperature sensor temp-c1 since /);
  await read(22);
  await card.locator('.state-badge', { hasText: 'Normal' }).waitFor();
  assert.equal((await api(`/api/incidents/logs?incidentId=${episode.id}&type=SensorRecovered`)).length, 1);
  assert.deepEqual(await relays(), before); // staleness and recovery never switch a relay
  assert.deepEqual([...page.problems, ...list.problems], []);
  await list.context().close();
  await page.context().close();
});

test('building map: people per room from the server (live, stale, unknown) as their own layer, sensors at their plan position with live status', async () => {
  const room = (name, x) => ({ type: 'Room', name, x, y: 0, width: 10, height: 10 });
  const plan = await api('/api/floorplans', { method: 'POST', body: JSON.stringify({ name: 'Rooms e2e', building: 'E2E', floorNumber: 0,
    widthMeters: 30, heightMeters: 10, elements: [room('Lobby', 0), room('Office', 10), room('Store', 20),
      { type: 'Sensor', name: 'temp-a1', x: 4, y: 4, width: 0.6, height: 0.6 }] }) });
  await api('/api/cameras/cam-lobby/calibration', { method: 'PUT', body: JSON.stringify({ floorPlanId: plan.id,
    imagePoints: [[0, 0], [640, 0], [640, 480], [0, 480]], planPoints: [[0.5, 0.5], [9.5, 0.5], [9.5, 9.5], [0.5, 9.5]] }) });
  const read = (value) => fetch(`${BASE}/api/iot/sensors/temp-a1/readings`, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Device-Key': process.env.SENSOR_KEY }, body: JSON.stringify({ value }) });
  try {
    const page = await dashboard('/occupancy');
    await page.selectOption('#evac-plan', String(plan.id));
    const rows = page.locator('#evacuation [data-room-counts]');
    await rows.locator('tr', { hasText: 'Lobby' }).filter({ hasText: 'Unknown' }).waitFor(); // no frame yet: unknown, not 0
    assert.match(await rows.locator('tr', { hasText: 'Store' }).textContent(), /\?\s*Unknown\s*No calibrated camera sees this room/);

    const people = (...hits) => postFrame({ cameraId: 'cam-lobby', detections: hits.map((h, i) =>
      ({ category: 'Human', confidence: 0.9, trackId: `r${i}`, hits: h, bbox: [100 + i * 200, 250, 40, 150] })) });
    await people(1, 1); // seen once: not yet tracked people
    await rows.locator('tr', { hasText: 'Lobby' }).filter({ hasText: /0\s*Live/ }).waitFor();
    await people(3, 3);
    await rows.locator('tr', { hasText: 'Lobby' }).filter({ hasText: /2\s*Live/ }).waitFor(POLL);
    const layer = page.locator('#evacuation svg [data-room][data-state="live"]');
    assert.match(await layer.textContent(), /2 people/);
    assert.equal(await page.locator('#evacuation svg [data-room][data-state="unknown"]').count(), 2); // Office, Store: "?", no fill
    await page.uncheck('#layer-people'); // a separate layer: hiding it leaves the plan and the fire-risk layer alone
    assert.equal(await page.locator('#evacuation svg [data-room]').count(), 0);
    await page.check('#layer-people');
    await rows.locator('tr', { hasText: 'Lobby' }).filter({ hasText: /2\s*Stale/ }).waitFor(POLL); // no frame for 10 s: last count, stale
    assert.match(await page.locator('#occ-kpis').textContent(), /Rooms with live data\s*0 \/ 3/); // the totals follow: stale is not live

    await page.goto(`${BASE}/map`); // sensors are shown on the Floor Plan, where a click opens their details
    await page.selectOption('#evac-plan', String(plan.id));
    const sensor = page.locator('#evacuation svg [data-sensor="temp-a1"]');
    await sensor.locator('title', { hasText: 'temp-a1 · Offline · never reported' }).waitFor({ state: 'attached' }); // once this page has the live data
    await read(50); // Warning (limit 57 °C)
    await sensor.locator('title', { hasText: 'Warning · 50 °C' }).waitFor({ state: 'attached' }); // an SVG title is never "visible"
    await sensor.locator('title', { hasText: 'Stale · last reading' }).waitFor({ ...POLL, state: 'attached' }); // silent for 5 s (e2e setting)
    await sensor.click();
    await page.locator('#map-detail', { hasText: 'Sensor temp-a1' }).waitFor();
    assert.deepEqual(page.problems, []);
    await page.context().close();
  } finally {
    await api('/api/cameras/cam-lobby/calibration', { method: 'DELETE' });
    await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
    await resolveAll();
  }
});

test('floor plan editor: "+ Sensor" places a sensor marker, the panel picks the sensor, and the position is saved with the plan', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/floor-plans`);
  await page.getByRole('button', { name: '+ New' }).click();
  await page.getByText('Floor plan created').waitFor();
  const svg = page.locator('#editor-map svg');
  const box = await svg.boundingBox();
  const scale = Math.min(box.width, box.height * 32 / 21.8) / 31.8; // 30 x 20 m plan with 3 % padding
  await page.locator('#tools').getByRole('button', { name: '+ Sensor' }).click();
  await page.mouse.click(box.x + box.width / 2 + (6 - 15) * scale, box.y + box.height / 2 + (5 - 10) * scale); // at (6, 5) m
  await page.locator('#element-panel select[data-text="name"]').selectOption('smoke-b1');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.locator('.toast', { hasText: 'Floor plan saved' }).waitFor();
  const plan = (await api('/api/floorplans')).find((p) => p.name === 'New floor plan' && p.elements.some((e) => e.type === 'Sensor'));
  const [s] = plan.elements;
  assert.equal(s.name, 'smoke-b1');
  assert.ok(Math.abs(s.x + s.width / 2 - 6) < 0.3 && Math.abs(s.y + s.height / 2 - 5) < 0.3, `placed at ${s.x}, ${s.y}`);
  await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('smoke: a smoke-only camera alert is its own incident, marked SMOKE (no fire size) in the list and its detail', async () => {
  const page = await dashboard('/incidents');
  await postFrame({ cameraId: 'cam-server', detections: [{ category: 'Smoke', confidence: 0.81, bbox: [100, 50, 300, 150] }] });
  const row = page.locator('#incident-rows tr', { hasText: 'Smoke detected on Server Hall' });
  await row.locator('.tag-smoke').waitFor();
  assert.match(await row.textContent(), /Medium/);
  await row.locator('[data-pick-incident]').click();
  await page.locator('#incident-detail', { hasText: 'no fire' }).filter({ hasText: 'seen by the AI' }).waitFor();
  await page.locator('#history-table', { hasText: 'SmokeMeasurement' }).waitFor();
  assert.match(await page.locator('#history-table').textContent(), /top confidence 0\.81/);
  assert.deepEqual(page.problems, []);
  await page.context().close();
  await resolveAll();
});

test('reports: model accuracy from the recorded offline evaluations, per class, with synthetic runs kept apart', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/reports`);
  const table = page.locator('#model-accuracy [data-evaluations]').first();
  const fire = table.locator('tr[data-run="2026-10-19-fire_smoke-yolox_tiny-dfire-test"][data-class="Fire"]');
  assert.match(await fire.textContent(), /4306[\s\S]*0\.731[\s\S]*0\.649[\s\S]*0\.685[\s\S]*1039 \/ 19 \/ 76 \/ 3172/);
  assert.equal(await table.locator('tr[data-run="2026-10-19-person-yolox_tiny-pennfudan"][data-class="Human"]').count(), 1);
  assert.match(await page.locator('#model-accuracy details summary').textContent(), /Synthetic stress runs \(16\)/);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('AI sensitivity: the slider stays within 0.25-0.55, shows its value, applies what the AI service confirms, and keeps it after a reload', async () => {
  await setAiMode('ok');
  const page = await newPage();
  await page.goto(`${BASE}/ai`);
  const form = page.locator('form[data-sensitivity="fire_smoke"]');
  const slider = form.locator('input[type="range"][name="threshold"]');
  const shown = form.locator('[data-sensitivity-value]'), status = form.locator('[data-sensitivity-status]');
  assert.match(await status.textContent(), /Active in the AI service: 0\.4\s+\(default 0\.4\)/);
  assert.deepEqual([await slider.getAttribute('min'), await slider.getAttribute('max'), await slider.inputValue(), await shown.textContent()],
    ['0.25', '0.55', '0.4', '0.40']);
  const drag = (v) => slider.evaluate((el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }, v); // as a hand moves it
  const sent = [];
  page.on('request', (r) => { if (r.url().endsWith('/api/ai/sensitivity') && r.method() === 'PUT') sent.push(r.postDataJSON().threshold); });
  try {
    await drag('0.1'); // below the range: the slider stops at its minimum
    assert.deepEqual([await slider.inputValue(), await shown.textContent()], ['0.25', '0.25']);
    await drag('0.9'); // above it: the maximum
    assert.deepEqual([await slider.inputValue(), await shown.textContent()], ['0.55', '0.55']);
    await drag('0.33'); // a value in between, shown as it moves
    assert.equal(await shown.textContent(), '0.33');
    await form.getByRole('button', { name: 'Apply' }).click();
    await status.filter({ hasText: 'Active in the AI service: 0.33 (confirmed; default 0.4).' }).waitFor();
    assert.equal(await form.locator('[data-sensitivity-current]').textContent(), '(current 0.33)');
    assert.deepEqual(sent, [0.33]); // only the applied value was ever sent, never 0.1 or 0.9
    await page.reload(); // the AI service keeps it: the page shows it again
    assert.deepEqual([await slider.inputValue(), await shown.textContent()], ['0.33', '0.33']);
    assert.match(await status.textContent(), /Active in the AI service: 0\.33/);
    assert.equal((await api('/api/ai/sensitivity')).models.find((m) => m.name === 'fire_smoke').threshold, 0.33);
    const logs = await api('/api/incidents/logs?type=SettingChanged');
    assert.match(logs[0].details, /set the fire_smoke confidence threshold to 0\.33 \(was 0\.4\); applied by the AI service/);
    assert.deepEqual(page.problems, []);
  } finally {
    await api('/api/ai/sensitivity', { method: 'PUT', body: JSON.stringify({ model: 'fire_smoke', threshold: 0.4 }) });
    await page.context().close();
  }
});

test('crowd density: an overcrowded exit shows the safe alternative and opens one Crowd alert, which closes when it clears', async () => {
  const plan = await api('/api/floorplans', { method: 'POST', body: JSON.stringify({ name: 'Crowd e2e', building: 'E2E', floorNumber: 0,
    widthMeters: 20, heightMeters: 10, elements: [{ type: 'Corridor', name: 'Hall', x: 0, y: 0, width: 20, height: 10 },
      { type: 'Exit', name: 'East', x: 19, y: 4, width: 1, height: 2 }, { type: 'Exit', name: 'West', x: 0, y: 4, width: 1, height: 2 }] }) });
  await api(`/api/floorplans/${plan.id}/capacities`, { method: 'PUT', body: JSON.stringify({ 'Exit:East': 3 }) });
  await api('/api/cameras/cam-lobby/calibration', { method: 'PUT', body: JSON.stringify({ floorPlanId: plan.id,
    imagePoints: [[0, 0], [640, 0], [640, 480], [0, 480]], planPoints: [[0.5, 0.5], [19.5, 0.5], [19.5, 9.5], [0.5, 9.5]] }) });
  const people = (n) => postFrame({ cameraId: 'cam-lobby', detections: Array.from({ length: n }, (_, i) =>
    ({ category: 'Human', confidence: 0.9, trackId: `c${i}`, hits: 5, bbox: [470 + 3 * i, 250, 20, 150] })) });
  try {
    const page = await dashboard('/occupancy');
    await page.selectOption('#evac-plan', String(plan.id));
    await people(4);
    const east = page.locator('#evacuation [data-exit-crowd] tr[data-exit-row="East"]');
    await east.filter({ hasText: 'Overcrowded' }).filter({ hasText: "Send people to exit 'West'" }).waitFor(POLL);
    assert.match(await east.textContent(), /Available\s*4\s*3 · Overcrowded/);
    const open = (await api('/api/incidents?resolved=false')).filter((i) => i.source === 'Crowd');
    assert.deepEqual(open.map((i) => i.zone), ["Crowd e2e - Exit 'East'"]);
    await new Promise((ok) => setTimeout(ok, 2100)); // past the 2 s crowd check throttle
    await people(1);
    await east.filter({ hasText: /1\s*3 · OK/ }).waitFor(POLL);
    assert.equal((await api(`/api/incidents/${open[0].id}`)).isResolved, true);
    assert.deepEqual(page.problems, []);
    await page.context().close();
  } finally {
    await api('/api/cameras/cam-lobby/calibration', { method: 'DELETE' });
    await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
    await resolveAll();
  }
});

test('notifications: every alert channel with its configuration status, and nothing claims success without a provider', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/notifications`);
  const table = page.locator('#notification-channels');
  for (const name of ['SMS (Twilio, internet)', 'SMS fallback (GSM modem)', 'WhatsApp (Twilio)', 'Telegram', 'Firebase Cloud Messaging']) {
    assert.match(await table.locator(`tr[data-channel="${name}"]`).textContent(), /Not configured/);
  }
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

// ---------------------------------------------------------------- Firebase push in the browser (the Firebase SDK is faked)
const FIREBASE = 'https://www.gstatic.com/firebasejs/10.12.2/';
const FAKE_SDK = {
  'firebase-app.js': 'export function initializeApp(config) { window.__fcm.config = config; return { config }; }',
  'firebase-messaging.js': `export async function isSupported() { return true; }
    export function getMessaging(app) { return { app }; }
    export async function getToken(m, options) {
      window.__fcm.options = { vapidKey: options.vapidKey, worker: Boolean(options.serviceWorkerRegistration) };
      if (window.__fcm.tokenError) throw new Error(window.__fcm.tokenError);
      return window.__fcm.token;
    }
    export function onMessage(m, callback) { window.__fcm.onMessage = callback; }
    export async function deleteToken() { window.__fcm.deleted = true; return true; }`,
};

/** Settings page with the fake Firebase SDK; `setup` runs in the page before any script (e.g. to fake permission answers). */
async function pushPage({ permission = 'granted', setup = '' } = {}) {
  const page = await newPage();
  const context = page.context();
  if (permission === 'granted') await context.grantPermissions(['notifications'], { origin: BASE });
  await context.addInitScript(`window.__fcm = { token: 'ZTJlLWJyb3dzZXItdG9rZW4tMTIzNDU2Nzg5MA', asked: 0 };
    if ('Notification' in window) { const ask = Notification.requestPermission.bind(Notification);
      Notification.requestPermission = (...a) => { window.__fcm.asked++; return ask(...a); }; }
    ${setup}`);
  await page.route(`${FIREBASE}**`, (route) => route.fulfill({ status: 200, contentType: 'application/javascript',
    headers: { 'Access-Control-Allow-Origin': '*' }, body: FAKE_SDK[route.request().url().slice(FIREBASE.length)] }));
  await page.goto(`${BASE}/notifications`);
  await page.locator('#fcm-panel [data-fcm-facts]').waitFor();
  return page;
}
const fcmDevices = async () => (await api('/api/push/fcm-token')).devices.filter((d) => d.active);

test('Firebase push: permission only on the click, the browser token is registered, foreground alerts show, Disable unregisters', async () => {
  const page = await pushPage();
  const panel = page.locator('#fcm-panel');
  assert.match(await panel.locator('[data-fcm-status]').textContent(), /Not configured/); // no service-account file on this server
  assert.match(await panel.textContent(), /This browser\s*Not registered/);
  assert.equal(await page.evaluate(() => window.__fcm.asked), 0); // nothing asked before the click
  await panel.getByRole('button', { name: 'Enable notifications on this browser' }).click();
  await panel.locator('[data-fcm-device]', { hasText: 'Registered' }).waitFor();
  const fcm = await page.evaluate(() => window.__fcm);
  assert.equal(fcm.asked, 1);
  assert.deepEqual(fcm.config, { apiKey: 'e2e-public-api-key', projectId: 'sentinel-e2e', messagingSenderId: '0', appId: '1:0:web:e2e' });
  assert.deepEqual(fcm.options, { vapidKey: 'e2e-vapid-key', worker: true }); // the token is tied to /sw.js
  assert.deepEqual((await fcmDevices()).map((d) => d.tokenEnd), ['U2Nzg5MA']); // only the token's end is ever shown
  await page.evaluate(() => window.__fcm.onMessage({ notification: { title: 'FIRE · Critical', body: 'Ground Floor - Lobby' }, data: { incidentId: '7' } }));
  await page.locator('.toast', { hasText: 'FIRE · Critical' }).filter({ hasText: 'open Incidents for #7' }).waitFor();
  await panel.getByRole('button', { name: 'Disable on this browser' }).click();
  await panel.locator('[data-fcm-device]', { hasText: 'Not registered' }).waitFor();
  assert.deepEqual(await fcmDevices(), []);
  assert.equal(await page.evaluate(() => window.__fcm.deleted), true);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('Firebase push: a denied permission, an unsupported browser and a token error are explained and register nothing', async () => {
  const denied = await pushPage({ permission: 'default', setup: "Notification.requestPermission = async () => { window.__fcm.asked++; return 'denied'; };" });
  await denied.locator('#fcm-panel').getByRole('button', { name: 'Enable notifications on this browser' }).click();
  await denied.locator('#fcm-panel [data-fcm-error]', { hasText: 'Notifications are blocked' }).waitFor();
  await denied.context().close();

  const old = await pushPage({ setup: 'delete window.PushManager;' });
  assert.match(await old.locator('#fcm-panel [data-fcm-permission]').textContent(), /Not supported by this browser/);
  assert.equal(await old.locator('#fcm-panel [data-fcm-enable]').isDisabled(), true);
  await old.context().close();

  const broken = await pushPage({ setup: "window.__fcm.tokenError = 'messaging/token-subscribe-failed';" });
  await broken.locator('#fcm-panel').getByRole('button', { name: 'Enable notifications on this browser' }).click();
  await broken.locator('#fcm-panel [data-fcm-error]', { hasText: 'Could not enable notifications: messaging/token-subscribe-failed' }).waitFor();
  await broken.context().close();
  assert.deepEqual(await fcmDevices(), []);
});

test('Firebase push: a click on the notification opens that incident', async () => {
  const page = await newPage();
  await page.goto(`${BASE}/sw.js`);
  const shown = await page.evaluate(async () => { // the service worker's own mapping from an FCM push to the notification
    const code = document.body.innerText;
    const fromPush = new Function('self', `${code}; return fromPush;`)({ addEventListener() {} });
    return fromPush({ notification: { title: 'SMOKE · Medium', body: 'First Floor - Server Hall' },
      data: { incidentId: '42', severity: 'Medium', url: 'http://127.0.0.1:5399/incidents?incident=42' } });
  });
  assert.equal(shown[0], 'SMOKE · Medium');
  assert.deepEqual([shown[1].tag, shown[1].data.url, shown[1].requireInteraction], ['incident-42', 'http://127.0.0.1:5399/incidents?incident=42', false]);
  const incident = await api('/api/incidents/dispatch', { method: 'POST', body: JSON.stringify({ zone: 'E2E Push Zone', severity: 'Medium' }) });
  await page.goto(`${BASE}/incidents?incident=${incident.id}`); // where that click lands
  await page.locator('#incident-detail', { hasText: `#${incident.id}` }).filter({ hasText: 'E2E Push Zone' }).waitFor();
  assert.deepEqual(page.problems, []);
  await page.context().close();
  await resolveAll();
});
