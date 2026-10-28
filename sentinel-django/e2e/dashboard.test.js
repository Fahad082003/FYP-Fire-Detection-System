// Browser tests for the Django dashboard (ports and extensions of frontend/e2e/sentinel.e2e.ts). Run via e2e/run.py.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { BASE, api, banner, closeBrowser, newPage, postFrame, resolveAll, setAiMode } from './helpers.js';

after(closeBrowser);
const POLL = { timeout: 15_000 }; // the AI status poll runs every 10 s

async function dashboard() {
  await setAiMode('ok');
  await resolveAll();
  const page = await newPage();
  await page.goto(`${BASE}/`);
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
  await page.goto(`${BASE}/`);
  assert.match(await banner(page).textContent(), /MONITORING DEGRADED · LIVE LINK DOWN|AI STATUS UNKNOWN/); // never green before data
  await banner(page).filter({ hasText: 'ALL SYSTEMS NOMINAL' }).waitFor(POLL);
  assert.equal(await page.locator('.tile').count(), 4);
  assert.equal(await page.locator('.sensor').count(), 6);
  assert.equal(await page.locator('.relay').count(), 5);
  assert.equal(await page.locator('#ai-dot').getAttribute('class'), 'dot dot-green');
  assert.match(await page.locator('#kpis').textContent(), /Open Incidents\s*0/);
  assert.match(await page.locator('#activity').textContent(), /Realtime link established/);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('status banner follows AI health: offline, model missing, camera offline, then nominal again', async () => {
  const page = await dashboard();
  for (const [mode, text] of [['offline', 'MONITORING DEGRADED · AI OFFLINE'], ['model-missing', 'MONITORING DEGRADED · AI MODEL MISSING'],
    ['camera-error', 'MONITORING DEGRADED · CAMERA OFFLINE'], ['ok', 'ALL SYSTEMS NOMINAL']]) {
    await setAiMode(mode);
    await banner(page).filter({ hasText: new RegExp(`^${text}$`) }).waitFor(POLL);
    assert.equal(await banner(page).getAttribute('class'), `status-pill level-${mode === 'ok' ? 'nominal' : 'elevated'}`);
  }
  assert.deepEqual(page.problems, []);
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
  const rows = page.locator('#incident-rows');
  assert.match(await rows.locator('tr', { hasText: 'Warehouse Floor' }).textContent(), /Medium.*AI Vision.*Open/s); // still listed while the AI is down
  assert.match(await rows.locator('tr', { hasText: 'Main Lobby' }).textContent(), /Medium.*Camera lost.*Open/s);

  await setAiMode('ok');
  await banner(page).filter({ hasText: /^ELEVATED RISK · \d+ OPEN · AI DATA STALE$/ }).waitFor(POLL); // AI back, incidents still open
  assert.equal(await rows.locator('tr', { hasText: 'Warehouse Floor' }).filter({ hasText: 'AI Vision' }).count(), 1);
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('incidents: a fire alert appears live with a toast, raises the alarm frame, and is resolved from the alert log', async () => {
  const page = await dashboard();
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

  await page.goto(`${BASE}/`);
  await page.selectOption('#evac-plan', String(plan.id));
  const map = page.locator('#evacuation svg');
  await map.scrollIntoViewIfNeeded(); // the panel is below the fold
  const m = await map.boundingBox();
  const s = Math.min(m.width, m.height * 31.8 / 21.8) / 31.8;
  await page.mouse.click(m.x + m.width / 2 + (5 - 15) * s, m.y + m.height / 2 + (10 - 10) * s);
  const result = page.locator('#evacuation [data-result] p').first();
  await result.filter({ hasNotText: /Click the person|Calculating/ }).waitFor();
  assert.equal(await result.textContent(), "Route to exit 'Exit 1'.");
  assert.match(await page.locator('#evacuation').textContent(), /Estimated walking time/);
  assert.equal(await page.locator('#evacuation polyline').count(), 1);
  await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
  assert.deepEqual(page.problems, []);
  await page.context().close();
});

test('accessibility basics and no broken routes on every page, also at phone width', async () => {
  for (const width of [1600, 375]) {
    const page = await newPage();
    await page.setViewportSize({ width, height: 900 });
    for (const path of ['/', '/floor-plans', '/ai', '/devices', '/devices?add']) {
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
