// Shared browser-test helpers. Started by e2e/run.py, which provides the server, the stub AI service and these variables.
import { chromium } from 'playwright-core';

export const BASE = process.env.BASE_URL;
const DEVICE_KEY = process.env.DEVICE_KEY;
if (!BASE) throw new Error('Run the browser tests through e2e/run.py (it starts a throwaway server and database).');

let browser;
let session; // storage state of one signed-in admin: sign-in is rate limited (10 per minute)
let token;

export async function closeBrowser() { await browser?.close(); browser = null; }

/** A fresh page that records JavaScript errors, console errors and failed requests (asserted with page.problems). */
export async function newPage({ signedIn = true } = {}) {
  browser ??= await chromium.launch({ channel: 'msedge', headless: true });
  if (signedIn && !session) {
    const page = await newPage({ signedIn: false });
    await page.goto(`${BASE}/login`);
    await page.fill('input[name=email]', process.env.ADMIN_EMAIL);
    await page.fill('input[name=password]', process.env.ADMIN_PASSWORD);
    await page.click('button[type=submit]');
    await page.waitForURL(`${BASE}/`);
    session = await page.context().storageState();
    await page.context().close();
  }
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, storageState: signedIn ? session : undefined });
  const page = await context.newPage();
  page.problems = [];
  page.on('pageerror', (e) => page.problems.push(`page error: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) page.problems.push(`console: ${m.text()}`); });
  page.on('response', (r) => { if (r.status() >= 500 || r.status() === 404) page.problems.push(`HTTP ${r.status()} ${r.url()}`); });
  return page;
}

export async function setAiMode(mode) {
  await fetch(process.env.AI_CONTROL, { method: 'POST', body: JSON.stringify({ mode }) });
}

/** POST /api/detections as the AI service does (device key). */
export async function postFrame(frame) {
  const r = await fetch(`${BASE}/api/detections`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Device-Key': DEVICE_KEY },
    body: JSON.stringify({ frameWidth: 640, frameHeight: 480, detections: [], ...frame }),
  });
  return { status: r.status, body: await r.json() };
}

/** Operator API call with a bearer token (one sign-in for the whole run). */
export async function api(path, init = {}) {
  if (!token) {
    const r = await fetch(`${BASE}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: process.env.ADMIN_EMAIL, password: process.env.ADMIN_PASSWORD }) });
    token = (await r.json()).token;
  }
  const r = await fetch(`${BASE}${path}`, { ...init, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...init.headers } });
  return r.status === 204 ? null : r.json();
}

/** Resolve every open incident, so a test starts from a quiet state. */
export async function resolveAll() {
  for (const i of await api('/api/incidents?resolved=false')) await api(`/api/incidents/${i.id}/resolve`, { method: 'POST' });
}

export const banner = (page) => page.locator('#status-pill');
