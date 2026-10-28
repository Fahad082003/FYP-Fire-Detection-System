// Live dashboard state shared by every page: the event stream, the status banner, the audible alarm, web push and the
// emergency-dispatch dialog. Port of the Angular SignalRService, ShellComponent and AlarmService.
import { SEVERITY_RANK, api, errorMessage, esc, systemLevel, toast } from './core.js';

/** AI health (models + per-camera stream state) is a cheap status call: 10 s keeps the banner within ~10 s of a camera failure. */
export const AI_POLL_MS = 10_000;
/** A camera whose AI frames stopped 8-120 s ago marks monitoring degraded before the next poll or the 10 s camera-lost alarm
 * (normal gaps measured up to 5.4 s at 3-4 cameras). Longer silences are left to the AI poll and the alarm. */
export const FRAMES_STALE_MS = 8_000;
const FRAMES_FORGET_MS = 120_000;
/** A tile with no frame for this long shows "NO AI DATA" (the AI sends ~0.6-2 frames/s per camera on a busy CPU). */
export const FRAME_STALE_MS = 4000;
export const HISTORY_POINTS = 60;
const MAX_INCIDENTS = 200;
const MAX_ACTIVITY = 250;

const listeners = new Map();
const pendingStats = new Map();

export const live = {
  state: 'connecting', // connecting | connected | reconnecting | disconnected
  loaded: false,
  cameras: [], sensors: [], sprinklers: [], incidents: [], activity: [],
  frameStats: {}, // cameraId -> { humanCount, dangerCount, fireDetected, fireSeverity, simulated, smokeDetected, model, receivedAt }
  ai: null, // GET /api/ai/status, null = could not be read
  now: Date.now(),

  on(name, fn) { (listeners.get(name) ?? listeners.set(name, []).get(name)).push(fn); },
  emit(name, value) { for (const fn of listeners.get(name) ?? []) fn(value); },

  openIncidents() { return this.incidents.filter((i) => !i.isResolved); },
  level() { return systemLevel(this.openIncidents().map((i) => i.severity), this.sensors.map((s) => s.state)); },
  zones() {
    return [...new Set([...this.cameras.map((c) => c.locationZone), ...this.sensors.map((s) => s.locationZone), ...this.sprinklers.map((s) => s.zone)])].sort();
  },
  liveStats() { return Object.values(this.frameStats).filter((s) => this.now - s.receivedAt < FRAME_STALE_MS); },

  upsertIncident(incident) {
    const list = this.incidents.some((i) => i.id === incident.id) ? this.incidents.map((i) => (i.id === incident.id ? incident : i)) : [incident, ...this.incidents];
    list.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    while (list.length > MAX_INCIDENTS) {
      const idx = list.map((i) => i.isResolved).lastIndexOf(true);
      if (idx < 0) break;
      list.splice(idx, 1);
    }
    this.incidents = list;
    this.emit('incidents');
  },
  upsertSprinkler(s) {
    this.sprinklers = this.sprinklers.some((x) => x.id === s.id) ? this.sprinklers.map((x) => (x.id === s.id ? s : x)) : [...this.sprinklers, s];
    this.emit('sprinklers');
  },
  updateCamera(camera) {
    this.cameras = this.cameras.map((c) => (c.id === camera.id ? camera : c));
    this.emit('cameras');
  },
  addActivity(entry) {
    this.activity = [entry, ...this.activity].slice(0, MAX_ACTIVITY);
    this.emit('activity');
  },
};

// ---------------------------------------------------------------- event stream (replaces the SignalR hub connection)
let source = null;
let retry = 0;
let everConnected = false;

function setState(state) {
  live.state = state;
  live.emit('state');
  renderBanner();
}

function connect() {
  source = new EventSource('/api/events');
  source.onopen = () => {
    retry = 0;
    setState('connected');
    live.addActivity({ level: 'success', message: everConnected ? 'Realtime link re-established' : 'Realtime link established', zone: null, timestamp: new Date().toISOString() });
    everConnected = true;
  };
  source.onerror = async () => {
    if (source.readyState === EventSource.CONNECTING) { setState('reconnecting'); return; } // the browser retries by itself
    setState('disconnected');
    source.close();
    try { await api('/api/auth/me'); } catch { /* api() sends a signed-out user to the login page */ }
    setTimeout(connect, Math.min(30000, 1000 * 2 ** retry++) + Math.random() * 500);
  };
  const on = (name, fn) => source.addEventListener(name, (e) => fn(JSON.parse(e.data)));
  on('snapshot', applySnapshot);
  on('detection', onDetection);
  on('telemetry', upsertSensor);
  on('sprinkler', (s) => live.upsertSprinkler(s));
  on('incident', onIncident);
  on('activity', (a) => live.addActivity(a));
  on('notification', onNotification);
  on('incidentLog', (l) => live.emit('incidentLog', l));
}

function applySnapshot(s) {
  live.loaded = true;
  live.cameras = s.cameras;
  live.sensors = s.sensors.map((x) => ({ ...x, history: (x.history ?? []).slice(-HISTORY_POINTS) }));
  live.sprinklers = s.sprinklers;
  live.incidents = s.incidents;
  for (const name of ['snapshot', 'cameras', 'sensors', 'sprinklers', 'incidents']) live.emit(name);
  renderBanner();
}

function upsertSensor(sensor) {
  const prev = live.sensors.find((s) => s.id === sensor.id);
  const history = [...(prev?.history ?? [])];
  if (!prev || prev.lastReadingAt !== sensor.lastReadingAt) history.push(sensor.currentValue);
  const next = { ...sensor, history: history.slice(-HISTORY_POINTS) };
  live.sensors = prev ? live.sensors.map((s) => (s.id === sensor.id ? next : s)) : [...live.sensors, next];
  live.emit('sensors');
  renderBanner();
}

function onDetection(frame) {
  live.emit('detection', frame); // stream rate: canvas overlays and the evacuation panel
  pendingStats.set(frame.cameraId, {
    cameraId: frame.cameraId, humanCount: frame.humanCount, dangerCount: frame.dangerCount, fireDetected: frame.fireDetected,
    fireSeverity: frame.fireSeverity, simulated: frame.simulated, smokeDetected: frame.smokeDetected, model: frame.model, receivedAt: Date.now(),
  });
}

// Frames arrive many times a second in total; tiles and KPIs only need ~4 updates a second.
setInterval(() => {
  if (!pendingStats.size) return;
  live.frameStats = { ...live.frameStats, ...Object.fromEntries(pendingStats) };
  pendingStats.clear();
  live.emit('stats');
}, 250);

function onIncident({ action, incident }) {
  live.upsertIncident(incident);
  renderBanner();
  if (action !== 'created' && action !== 'escalated') return;
  const title = `${incident.severity.toUpperCase()} — Incident #${incident.id}`;
  if (SEVERITY_RANK[incident.severity] >= SEVERITY_RANK.High) {
    toast(title, incident.message, 'critical', 10000);
    notifyLocal(incident);
  } else {
    toast(title, incident.message, 'warning');
  }
}

function onNotification(n) {
  const channel = n.channel.toUpperCase();
  const message = n.skipped ? `${channel} skipped for incident #${n.incidentId} (${n.detail})`
    : n.ok ? `${channel} alert for incident #${n.incidentId} delivered to ${n.delivered}${n.detail ? ` — ${n.detail}` : ''}`
      : `${channel} alert for incident #${n.incidentId} failed${n.detail ? `: ${n.detail}` : ''}`;
  live.addActivity({ level: n.skipped ? 'info' : n.ok ? 'notify' : 'warning', message, zone: null, timestamp: n.timestamp });
}

// ---------------------------------------------------------------- status banner
/** Why "all clear" cannot be trusted right now (no AI = no detections, which is not the same as no fire), or null. */
export function degradedReason() {
  if (live.state !== 'connected' || !live.loaded) return 'LIVE LINK DOWN'; // before the first snapshot, "no incidents" is unknown
  const s = live.ai;
  if (!s) return 'AI STATUS UNKNOWN';
  if (!s.online) return 'AI OFFLINE';
  if (s.service?.status !== 'ok') return 'AI MODEL MISSING';
  // The server's "camera lost" alarm needs 10 s of silence; the AI service knows at once which stream it cannot read.
  if ((s.service.cameras ?? []).some((c) => c.state === 'error' || c.state === 'reconnecting')) return 'CAMERA OFFLINE';
  // Between polls: AI frames that stopped arriving (e.g. the AI service just died) are known here first.
  const cameras = new Set(live.cameras.map((c) => c.id));
  return Object.values(live.frameStats).some((f) => cameras.has(f.cameraId)
    && live.now - f.receivedAt > FRAMES_STALE_MS && live.now - f.receivedAt < FRAMES_FORGET_MS) ? 'AI DATA STALE' : null;
}

const pill = document.getElementById('status-pill');

function renderBanner() {
  const level = live.level();
  const reason = degradedReason();
  const open = live.openIncidents().length;
  const also = reason ? ` · ${reason}` : ''; // open incidents stay first; the monitoring gap is still shown
  const text = { nominal: reason ? `MONITORING DEGRADED · ${reason}` : 'ALL SYSTEMS NOMINAL',
    elevated: `ELEVATED RISK · ${open} OPEN${also}`, critical: `CRITICAL ALERT · ${open} OPEN${also}` }[level];
  pill.className = `status-pill level-${level === 'nominal' && reason ? 'elevated' : level}`;
  if (pill.textContent !== text) pill.textContent = text;
  document.getElementById('alarm-frame').classList.toggle('hidden', level !== 'critical');

  const link = { connected: ['Live', 'dot dot-live'], connecting: ['Connecting', 'dot dot-amber blink'],
    reconnecting: ['Reconnecting', 'dot dot-amber blink'], disconnected: ['Offline', 'dot dot-red'] }[live.state];
  document.getElementById('link-label').textContent = link[0];
  document.getElementById('link-dot').className = link[1];
  alarmFollow();
}

let aiPoll = 0;
async function pollAi() {
  const poll = ++aiPoll; // a slow older reply must not overwrite a newer one
  let status = null;
  try { status = await api('/api/ai/status'); } catch { /* unknown */ }
  if (poll !== aiPoll) return;
  live.ai = status;
  const s = status;
  const missing = s?.service?.models?.filter((m) => !m.loaded).map((m) => m.name) ?? [];
  document.getElementById('ai-dot').className = `dot ${!s?.online ? 'dot-red' : s.service?.status === 'ok' ? 'dot-green' : 'dot-amber'}`;
  document.getElementById('ai-link').title = !s ? 'AI service status unknown' : !s.online ? `AI service offline: ${s.error ?? ''}`
    : missing.length ? `AI online, missing model(s): ${missing.join(', ')}` : 'AI online, all models loaded';
  live.emit('ai');
  renderBanner();
}

// ---------------------------------------------------------------- audible alarm (Web Audio; unlocked by the first click)
let audio = null;
let alarmLoop = null;
let alarmCritical = false;
const MUTE_KEY = 'sentinel.muted';
let muted = (() => { try { return localStorage.getItem(MUTE_KEY) === '1'; } catch { return false; } })();

function audioContext() {
  if (typeof AudioContext === 'undefined') return null;
  audio ??= new AudioContext();
  if (audio.state === 'suspended') audio.resume().catch(() => undefined);
  return audio;
}
window.addEventListener('pointerdown', () => audioContext(), { once: true });

function beep() {
  const ctx = muted ? null : audioContext();
  if (!ctx || ctx.state !== 'running') return;
  const [hi, lo, beeps] = alarmCritical ? [988, 740, 4] : [784, 587, 2];
  const t0 = ctx.currentTime + 0.02;
  for (let i = 0; i < beeps; i++) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const t = t0 + i * 0.22;
    osc.type = 'square';
    osc.frequency.setValueAtTime(i % 2 ? lo : hi, t);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.4, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.2);
  }
}

/** Rings every 2.5 s while any incident is open: an open incident stays audible until someone responds to it. */
function alarmFollow() {
  const open = live.openIncidents();
  alarmCritical = open.some((i) => i.severity === 'Critical');
  if (open.length && !alarmLoop) { beep(); alarmLoop = setInterval(beep, 2500); }
  if (!open.length && alarmLoop) { clearInterval(alarmLoop); alarmLoop = null; }
}

const muteButton = document.getElementById('mute');
function renderMute() {
  muteButton.setAttribute('aria-pressed', String(muted));
  muteButton.title = muted ? 'Audible alarm muted' : 'Audible alarm on';
  muteButton.querySelector('[data-on]').classList.toggle('hidden', muted);
  muteButton.querySelector('[data-off]').classList.toggle('hidden', !muted);
}
muteButton.addEventListener('click', () => {
  muted = !muted;
  try { localStorage.setItem(MUTE_KEY, muted ? '1' : '0'); } catch { /* storage unavailable */ }
  renderMute();
});

// ---------------------------------------------------------------- web push (server push with VAPID keys, else local notifications)
const push = { registration: null, vapidKey: null, subscription: null, mode: 'off' };
const pushButton = document.getElementById('push-toggle');
const pushSupported = 'serviceWorker' in navigator && 'Notification' in window;

function urlBase64ToUint8Array(base64) {
  const raw = atob((base64 + '='.repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function renderPush() {
  pushButton.classList.toggle('hidden', !pushSupported);
  pushButton.textContent = push.mode === 'off' ? 'Enable Alerts' : 'Alerts On';
  pushButton.classList.toggle('btn-success', push.mode !== 'off');
}

async function initPush() {
  if (!pushSupported) return;
  try {
    push.registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    push.vapidKey = (await api('/api/push/vapid-public-key')).publicKey;
    if (push.vapidKey && 'PushManager' in window) push.subscription = await push.registration.pushManager.getSubscription();
    push.mode = push.subscription ? 'push' : !push.vapidKey && Notification.permission === 'granted' ? 'local' : 'off';
  } catch (err) {
    console.warn('[push] init failed', err);
  }
  renderPush();
}

pushButton.addEventListener('click', async () => {
  if (push.mode !== 'off') {
    if (push.subscription) {
      const endpoint = push.subscription.endpoint;
      await push.subscription.unsubscribe().catch(() => undefined);
      await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint } }).catch(() => undefined);
      push.subscription = null;
    }
    push.mode = 'off';
    toast('Alerts disabled', '', 'info', 2500);
    return renderPush();
  }
  if (await Notification.requestPermission() !== 'granted') {
    return toast('Notifications blocked', 'Allow notifications for this site in the browser settings.', 'warning');
  }
  try {
    if (push.vapidKey && push.registration && 'PushManager' in window) {
      push.subscription = await push.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(push.vapidKey) });
      await api('/api/push/subscribe', { method: 'POST', body: push.subscription.toJSON() });
      push.mode = 'push';
      toast('Push alerts enabled', 'This device receives incident alerts even when the dashboard is closed.', 'success');
    } else {
      push.mode = 'local';
      toast('Desktop alerts enabled', 'Alerts appear while this dashboard is open (server push not configured).', 'success');
    }
  } catch (err) {
    toast('Push subscription failed', errorMessage(err), 'error');
  }
  renderPush();
});

/** Local fallback: a notification for High/Critical incidents while the tab is in the background. */
function notifyLocal(incident) {
  if (push.mode !== 'local' || !document.hidden || !push.registration) return;
  push.registration.showNotification(`${incident.severity.toUpperCase()} · Incident #${incident.id}`, {
    body: `${incident.zone} — humans in danger: ${incident.dangerCount}`, tag: `incident-${incident.id}`,
    requireInteraction: incident.severity === 'Critical', data: { url: location.href },
  });
}

// ---------------------------------------------------------------- emergency dispatch dialog
const dispatchDialog = document.getElementById('dispatch-dialog');
const dispatchForm = document.getElementById('dispatch-form');

document.getElementById('open-dispatch').addEventListener('click', () => {
  const zones = live.zones();
  dispatchForm.reset();
  dispatchForm.elements.zone.innerHTML = zones.map((z) => `<option>${esc(z)}</option>`).join('');
  dispatchForm.querySelector('[data-count]').textContent = '0';
  dispatchForm.querySelector('[data-error="zone"]').classList.add('hidden');
  dispatchDialog.showModal();
});
dispatchForm.querySelector('[data-close]').addEventListener('click', () => dispatchDialog.close());
dispatchForm.elements.message.addEventListener('input', (e) => { dispatchForm.querySelector('[data-count]').textContent = e.target.value.length; });
dispatchForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = dispatchForm.elements;
  if (!f.zone.value) { dispatchForm.querySelector('[data-error="zone"]').classList.remove('hidden'); return; }
  const button = dispatchForm.querySelector('[type=submit]');
  button.disabled = true;
  button.textContent = 'Dispatching…';
  try {
    const incident = await api('/api/incidents/dispatch', { method: 'POST', body: {
      zone: f.zone.value, severity: f.severity.value, message: f.message.value.trim() || undefined, activateSprinklers: f.activateSprinklers.checked } });
    live.upsertIncident(incident);
    toast('Emergency dispatch sent', `Incident #${incident.id} · ${incident.zone}`, 'critical');
    dispatchDialog.close();
  } catch (err) {
    toast('Dispatch failed', errorMessage(err), 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Dispatch Now';
  }
});

// ---------------------------------------------------------------- start
const clock = document.getElementById('clock');
function tick() {
  live.now = Date.now();
  clock.textContent = new Date(live.now).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  renderBanner(); // stale-frame detection depends on the clock
  live.emit('tick');
}
setInterval(tick, 1000);
tick();
renderMute();
connect();
pollAi();
setInterval(pollAi, AI_POLL_MS);
initPush();
