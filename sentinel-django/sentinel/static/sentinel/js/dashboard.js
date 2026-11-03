// The live panels of every page (dashboard, cameras, sensors, sprinklers, incidents, activity, building map, AI, settings).
// Each section runs only where its element is on the page.
import { SEVERITY_RANK, api, confirmAsk, dateTimeOf, errorMessage, esc, fireSeverityText, relativeTime, timeOf, toast } from './core.js';
import { FRAMES_STALE_MS, HEALTH_STALE_MS, HISTORY_POINTS, degradedReason, live } from './shell.js';
import { OverlayRenderer, register, unregister } from './overlay.js';
import { PlanMap } from './planmap.js';

const $ = (id) => document.getElementById(id);

// ================================================================ video wall
const MJPEG_RETRY_MS = 5000;
const wall = { filter: 'all', heatmap: true, focused: null, selected: new URLSearchParams(location.search).get('camera'), tiles: new Map() };
let hlsLoading = null;

function loadHls() { // vendored hls.js (Apache-2.0), fetched only when an HLS feed exists
  hlsLoading ??= new Promise((resolve, reject) => {
    const s = Object.assign(document.createElement('script'), { src: '/static/sentinel/vendor/hls.min.js', onload: () => resolve(window.Hls), onerror: reject });
    document.head.append(s);
  });
  return hlsLoading;
}

/** A camera's state from the central health check (GET /api/system/health, polled every 10 s): the page snapshot's status is
 * only the state at page load. [label, tone, explanation]; unknown while there is no current health answer. */
const CAMERA_STATE = { connected: ['Online', 'on'], recovering: ['Recovering', 'warn'], stale: ['Stale', 'warn'], disconnected: ['Offline', 'off'],
  'not-monitored': ['Simulated', 'muted'] };
function cameraState(camera) {
  const current = live.health && live.now - live.healthAt <= HEALTH_STALE_MS;
  const entry = current ? live.health.components.cameras?.details?.cameras?.find((x) => x.id === camera.id) : null;
  const last = live.frameStats[camera.id]?.receivedAt; // between polls this page sees first that frames started or stopped
  if (last && live.now - last <= FRAMES_STALE_MS && last > live.healthAt) return ['Online', 'on', 'AI frames arriving now.'];
  if (!entry || !CAMERA_STATE[entry.state]) return ['Status unknown', 'muted', 'No current health check.'];
  if (entry.state === 'connected' && last && live.now - last > FRAMES_STALE_MS) return ['Stale', 'warn', `No AI frame for ${Math.round((live.now - last) / 1000)} s.`];
  return [...CAMERA_STATE[entry.state], entry.message];
}

class Tile {
  constructor(camera) {
    this.el = document.createElement('article');
    this.el.className = 'tile';
    this.el.tabIndex = 0;
    this.el.setAttribute('role', 'button');
    this.el.innerHTML = `<div class="stage"><canvas aria-hidden="true"></canvas><div class="tile-top"></div><div class="chips"></div><div class="tile-state"></div></div>`;
    this.stage = this.el.firstElementChild;
    this.renderer = new OverlayRenderer(this.stage, this.stage.querySelector('canvas'), (stale) => { this.stale = stale; this.renderInfo(); });
    this.stale = true;
    register(this.renderer);
    const toggle = () => { // cameras page: select it (details beside the grid); dashboard: open it on the cameras page
      if (!$('camera-detail')) { location.href = `/cameras?camera=${encodeURIComponent(this.camera.id)}`; return; }
      wall.selected = this.camera.id;
      renderWall();
    };
    this.el.addEventListener('click', toggle);
    this.el.addEventListener('keydown', (e) => { if (e.key === 'Enter') toggle(); });
    this.setCamera(camera);
  }

  setCamera(camera) {
    const old = this.camera;
    this.camera = camera;
    const format = camera.streamUrl ? camera.streamFormat : 'Simulated'; // no link: no real stream, the AI overlay is still drawn
    if (!old || old.streamUrl !== camera.streamUrl || old.streamFormat !== camera.streamFormat || old.status !== camera.status) this.attachMedia(format);
    this.el.setAttribute('aria-label', `${camera.name}, ${camera.feedType} feed, ${camera.locationZone}`);
    this.renderInfo();
  }

  attachMedia(format) {
    this.hls?.destroy();
    this.hls = null;
    clearTimeout(this.retryTimer);
    this.stage.querySelectorAll('img, video, .sim-backdrop, .no-signal').forEach((n) => n.remove());
    const canvas = this.stage.querySelector('canvas');
    let media = null;
    if (format === 'Mjpeg') {
      media = Object.assign(document.createElement('img'), { alt: '', decoding: 'async' });
      const load = (attempt) => { media.src = `/api/cameras/${encodeURIComponent(this.camera.id)}/stream${attempt ? `?r=${attempt}` : ''}`; };
      let attempt = 0;
      media.onload = () => this.setOffline(false);
      media.onerror = () => { this.setOffline(true); this.retryTimer = setTimeout(() => load(++attempt), MJPEG_RETRY_MS); };
      load(0);
    } else if (format === 'Hls' || format === 'Mp4') {
      media = Object.assign(document.createElement('video'), { muted: true, autoplay: true, playsInline: true, loop: format === 'Mp4' });
      media.onplaying = () => this.setOffline(false);
      if (format === 'Mp4') { media.src = this.camera.streamUrl; media.onerror = () => this.setOffline(true); } else this.attachHls(media);
    } else {
      const backdrop = document.createElement('div');
      backdrop.className = 'sim-backdrop';
      backdrop.innerHTML = `<span class="sim-label">${this.camera.status === 'Offline' ? 'FEED OFFLINE' : 'SIMULATED FEED · AI OVERLAY'}</span>`;
      this.stage.insertBefore(backdrop, canvas);
    }
    if (media) this.stage.insertBefore(media, canvas);
    this.renderer.media = media;
  }

  async attachHls(video) {
    const url = this.camera.streamUrl;
    if (video.canPlayType('application/vnd.apple.mpegurl')) { video.src = url; video.play().catch(() => undefined); return; } // Safari: native
    const Hls = await loadHls().catch(() => null);
    if (!Hls?.isSupported()) { this.setOffline(true); return; }
    const hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 2, maxBufferLength: 10 });
    this.hls = hls;
    hls.on(Hls.Events.ERROR, (_e, data) => {
      if (!data.fatal) return;
      this.setOffline(true);
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) setTimeout(() => hls.startLoad(), 3000);
      else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else { hls.destroy(); this.hls = null; }
    });
    hls.on(Hls.Events.MANIFEST_PARSED, () => { this.setOffline(false); video.play().catch(() => undefined); });
    hls.loadSource(url);
    hls.attachMedia(video);
  }

  setOffline(offline) {
    this.stage.querySelector('.no-signal')?.remove();
    if (offline) this.stage.querySelector('canvas').insertAdjacentHTML('beforebegin', '<div class="no-signal">NO SIGNAL — RECONNECTING</div>');
  }

  renderInfo() {
    const c = this.camera;
    const s = this.stale ? null : live.frameStats[c.id] ?? null;
    const camState = cameraState(c);
    const alert = !s ? '' : s.dangerCount > 0 || s.fireSeverity === 'Critical' || (s.fireDetected && s.humanCount > 0) ? 'critical' : s.fireDetected ? 'elevated' : '';
    this.el.classList.toggle('tile-critical', alert === 'critical');
    this.el.classList.toggle('tile-elevated', alert === 'elevated');
    this.el.classList.toggle('tile-selected', wall.selected === c.id && !!$('camera-detail'));
    this.stage.querySelector('.tile-top').innerHTML = `<span class="tile-name" title="${esc(`${c.feedType} · ${c.locationZone}`)}">${esc(c.name)}</span>
      <span class="cam-status ${camState[1]}" title="${esc(camState[2])}">${camState[0]}</span>
      <span class="live-flag ${this.stale ? 'stale' : ''}"><span class="dot ${this.stale ? 'dot-slate' : 'dot-red blink'}" style="width:6px;height:6px"></span>${this.stale ? 'NO AI DATA' : 'LIVE'}</span>`;
    const model = s?.model;
    this.stage.querySelector('.chips').innerHTML = s ? `${s.humanCount ? `<span class="chip">HUM <b class="hum">${s.humanCount}</b></span>` : ''}
      ${s.dangerCount ? `<span class="chip chip-danger">DANGER <b>${s.dangerCount}</b></span>` : ''}
      ${model?.includes(':missing') ? `<span class="chip text-amber" title="Model not loaded: ${esc(model)}">MODEL MISSING</span>` : ''}
      ${s.smokeDetected ? '<span class="chip chip-fire">SMOKE</span>' : ''}
      ${s.fireDetected ? `<span class="chip chip-fire" title="Fire severity = share of the image covered by fire boxes, not a calibrated physical size">FIRE <b>${esc(fireSeverityText(s.fireSeverity) ?? 'YES')}</b></span>` : ''}` : '';
    // A camera without a stream link only gets data from the simulator (start.ps1 -Simulator): say so instead of "waiting".
    const [state, tone] = camState[1] === 'off' ? ['Camera Offline', 'danger'] : s?.fireDetected ? ['Fire Detected', 'danger']
      : s?.smokeDetected ? ['Smoke Detected', 'warn'] : s ? ['Normal', 'ok']
        : !c.streamUrl ? ['No camera connected', 'muted'] : ['Waiting for AI', 'muted'];
    this.stage.querySelector('.tile-state').innerHTML = `<span class="state-pill sp-${tone}" title="${esc(!c.streamUrl && !s
      ? 'This is a demo camera without a stream link. Start with .\\start.ps1 -Simulator for demo data, or add the real link under Settings.' : '')}">${state}</span>`;
  }

  destroy() {
    clearTimeout(this.retryTimer);
    this.hls?.destroy();
    unregister(this.renderer);
    this.renderer.destroy();
    this.el.remove();
  }
}

/** A camera with something to look at: not working, or fire or smoke seen now. */
function cameraProblem(c) {
  const s = live.liveStats().find((x) => x.cameraId === c.id);
  return ['off', 'warn'].includes(cameraState(c)[1]) || !!s?.fireDetected || !!s?.smokeDetected;
}

function renderWall() {
  const container = $('video-wall');
  if (!container || !live.loaded) return;
  const shown = (c) => (wall.filter === 'all' || (wall.filter === 'problems' ? cameraProblem(c) : c.feedType === wall.filter));
  const visible = live.cameras.filter((c) => shown(c) && (!wall.focused || c.id === wall.focused));
  for (const [id, tile] of wall.tiles) if (!live.cameras.some((c) => c.id === id)) { tile.destroy(); wall.tiles.delete(id); }
  if (!visible.length) {
    container.innerHTML = `<p class="empty">${live.cameras.length ? 'No camera matches this filter.' : 'No camera added yet. Add one under Settings.'}</p>`;
    renderCameraDetail();
    return;
  }
  let grid = container.querySelector('.video-grid');
  if (!grid) { container.innerHTML = '<div class="video-grid"></div>'; grid = container.firstElementChild; }
  grid.classList.toggle('focused', !!wall.focused);
  for (const camera of live.cameras) {
    let tile = wall.tiles.get(camera.id);
    if (tile) tile.setCamera(camera); else wall.tiles.set(camera.id, tile = new Tile(camera));
    tile.renderer.showHeatmap = wall.heatmap;
  }
  grid.replaceChildren(...visible.map((c) => wall.tiles.get(c.id).el));
  wall.tiles.forEach((t) => t.renderInfo());
  renderCameraDetail();
}

/** Cameras page: the selected camera, in plain words. */
function renderCameraDetail() {
  const box = $('camera-detail');
  if ($('camera-count')) {
    const working = live.cameras.filter((c) => cameraState(c)[0] === 'Online').length;
    $('camera-count').textContent = live.loaded ? `${working} of ${live.cameras.length} working` : '';
  }
  if (!box) return;
  const c = live.cameras.find((x) => x.id === wall.selected);
  if (!c) { if (!box.dataset.empty) { box.innerHTML = '<p class="muted">Click a camera to see its details.</p>'; box.dataset.empty = '1'; } return; }
  delete box.dataset.empty;
  const s = live.liveStats().find((x) => x.cameraId === c.id);
  const [label, tone, why] = cameraState(c);
  const seen = !s ? 'no AI data right now' : [`${s.humanCount} people`, s.fireDetected ? 'FIRE' : 'no fire', s.smokeDetected ? 'SMOKE' : 'no smoke'].join(' · ');
  const last = live.frameStats[c.id]?.receivedAt;
  const html = `<h2 class="section-title">${esc(c.name)}</h2>
    <dl class="facts">
      <div><dt>Status</dt><dd class="${{ on: 'text-green', off: 'text-red', warn: 'text-amber' }[tone] ?? 'dim'}" title="${esc(why)}">${label}</dd></div>
      <div><dt>AI sees</dt><dd>${esc(seen)}</dd></div>
      <div><dt>Area</dt><dd>${esc(c.locationZone)}</dd></div>
      <div><dt>Type</dt><dd>${esc(c.feedType)}${c.streamUrl ? '' : ' · demo camera (no stream link)'}</dd></div>
      <div><dt>Last picture</dt><dd>${last ? relativeTime(new Date(last).toISOString(), live.now) : 'none yet'}</dd></div>
    </dl>
    <div class="row">
      <button type="button" class="btn btn-primary" data-full>${wall.focused === c.id ? 'Show all cameras' : 'Full screen'}</button>
      <a class="btn btn-ghost" href="/map">Show on map</a>
    </div>`;
  if (html !== box.dataset.html) { box.innerHTML = html; box.dataset.html = html; }
}
$('camera-detail')?.addEventListener('click', (e) => {
  if (!e.target.closest('[data-full]')) return;
  wall.focused = wall.focused === wall.selected ? null : wall.selected;
  renderWall();
});

$('feed-filter')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  wall.filter = b.dataset.filter;
  for (const x of $('feed-filter').children) x.setAttribute('aria-selected', String(x === b));
  renderWall();
});
$('heatmap-toggle')?.addEventListener('change', (e) => { wall.heatmap = e.target.checked; renderWall(); });
live.on('detection', (f) => wall.tiles.get(f.cameraId)?.renderer.update(f));
for (const name of ['stats', 'health', 'tick']) live.on(name, () => { wall.tiles.forEach((t) => t.renderInfo()); renderCameraDetail(); });
live.on('cameras', renderWall);

// ================================================================ KPIs
/** People across feeds: cameras in one zone usually see the same people, so take the largest count per zone, then add zones. */
export function peopleCount(stats, cameras) {
  const zoneOf = new Map(cameras.map((c) => [c.id, c.locationZone]));
  const perZone = new Map();
  for (const s of stats) {
    const key = zoneOf.get(s.cameraId) ?? `camera:${s.cameraId}`;
    const z = perZone.get(key) ?? { humans: 0, danger: 0 };
    perZone.set(key, { humans: Math.max(z.humans, s.humanCount), danger: Math.max(z.danger, s.dangerCount) });
  }
  let humans = 0, danger = 0;
  for (const z of perZone.values()) { humans += z.humans; danger += z.danger; }
  return { humans, danger };
}

function renderKpis() {
  if (!$('kpis')) return;
  const open = live.openIncidents();
  const critical = open.filter((i) => i.severity === 'Critical').length;
  const top = Math.max(-1, ...open.map((i) => SEVERITY_RANK[i.severity]));
  const { humans, danger } = peopleCount(live.liveStats(), live.cameras);
  const cams = live.cameras.length, camsOnline = live.cameras.filter((c) => cameraState(c)[0] === 'Online').length;
  const demo = live.cameras.filter((c) => cameraState(c)[0] === 'Simulated').length; // no stream link: only the simulator feeds them
  const sensors = live.sensors.length, reporting = live.sensors.filter((s) => s.state !== 'Stale').length;
  const overLimit = live.sensors.filter((s) => s.state === 'Alarm' || s.state === 'Critical').length;
  const relays = live.sprinklers.length, spraying = live.sprinklers.filter((s) => s.isActive).length;
  const kpis = [ // [label, value, hint, tone]
    ['Cameras', camsOnline, `of ${cams} working${demo ? ` · ${demo} demo` : ''}${humans ? ` · ${humans} people` : ''}${danger ? ` · ${danger} in danger` : ''}`,
      danger > 0 ? 'danger' : !cams ? 'neutral' : camsOnline < cams ? 'warn' : 'ok'],
    ['Sensors', reporting, `of ${sensors} working${overLimit ? ` · ${overLimit} over the limit` : ''}`,
      overLimit ? 'danger' : !sensors ? 'neutral' : reporting < sensors ? 'warn' : 'ok'],
    ['Sprinklers', relays, spraying ? `${spraying} spraying now` : relays ? 'all ready' : 'none registered',
      spraying ? 'info' : relays ? 'ok' : 'neutral'],
    ['Open Incidents', open.length, critical ? `${critical} critical` : open.length ? 'monitoring' : 'none open',
      top >= SEVERITY_RANK.High ? 'danger' : top >= 0 ? 'warn' : 'ok'],
  ];
  $('kpis').innerHTML = kpis.map(([label, value, hint, t]) => `<article class="kpi kpi-${t}"><span class="kpi-label">${label}</span>
    <span class="kpi-value">${esc(String(value))}</span><span class="kpi-hint">${esc(hint)}</span></article>`).join('');
  renderStatus();
}
for (const name of ['incidents', 'stats', 'sensors', 'cameras', 'sprinklers', 'health', 'tick']) live.on(name, renderKpis);

/** The status banner's reasons, said simply. */
const PLAIN_REASON = {
  'LIVE LINK DOWN': 'Server se live connection nahi hai', 'AI STATUS UNKNOWN': 'AI Service ka haal maloom nahi',
  'AI OFFLINE': 'AI Service Band Hai', 'AI UNRESPONSIVE': 'AI Service jawab nahi de rahi', 'AI RESTARTING': 'AI Service restart ho rahi hai',
  'AI RECOVERING': 'AI Service dobara shuru ho rahi hai', 'AI RECOVERY FAILED': 'AI Service shuru nahi ho saki',
  'AI LOADING': 'AI Models load ho rahe hain', 'AI MODEL MISSING': 'AI Model Load Nahi Hua', 'CAMERA OFFLINE': 'Camera Offline',
  'AI DATA STALE': 'Camera se AI data aana ruk gaya hai',
};

/** Safe / Warning / Emergency, from the same rules as the status banner (open incidents, sensor states, monitoring gaps). */
function renderStatus() {
  const box = $('building-status');
  const level = live.level(), reason = live.loaded ? degradedReason() : 'LIVE LINK DOWN';
  const worst = [...live.openIncidents()].sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity] || b.timestamp.localeCompare(a.timestamp))[0];
  const alarming = live.sensors.filter((s) => s.state === 'Alarm' || s.state === 'Critical');
  const [tone, title] = level === 'critical' ? ['danger', 'Emergency'] : level === 'elevated' || reason ? ['warn', 'Warning'] : ['ok', 'Building is Safe'];
  const text = worst ? `${worst.message} · ${locationOf(worst)} · since ${timeOf(worst.timestamp)}`
    : alarming.length ? `${alarming.map((s) => `${s.type} sensor ${s.id} (${s.locationZone}) ${s.state}`).join('; ')}`
      : reason ? `${PLAIN_REASON[reason] ?? reason}. Fire detection may not be working right now.`
        : 'No fire or smoke anywhere.';
  box.className = `building-status bs-${tone}`;
  if (box.dataset.tone !== tone) { // the mark says the same as the words: tick, exclamation, warning sign
    box.dataset.tone = tone;
    box.querySelector('.bs-icon').innerHTML = `<svg viewBox="0 0 24 24">${{ ok: '<path d="m5 12 4.5 4.5L19 7"/>', warn: '<path d="M12 6v8m0 4v.01"/>',
      danger: '<path d="M12 3 2 21h20L12 3zm0 7v5m0 3v.01"/>' }[tone]}</svg>`;
  }
  box.querySelector('[data-title]').textContent = title;
  box.querySelector('[data-text]').textContent = text;
  box.title = reason && (worst || alarming.length) ? `Also: ${PLAIN_REASON[reason] ?? reason}` : '';
  const actions = !worst ? '' : `<a class="btn btn-danger" href="/incidents">Open incident</a>${worst.cameraId
    ? `<button type="button" class="btn btn-ghost" data-show-camera="${esc(worst.cameraId)}">Show camera</button>` : ''}`;
  if (actions !== statusActions) box.querySelector('[data-actions]').innerHTML = statusActions = actions; // rebuilt every second otherwise
  const now = new Date(live.now);
  $('today').textContent = `${now.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} · ${now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  renderAttention();
}
let statusActions = '';
$('building-status')?.addEventListener('click', (e) => {
  const id = e.target.closest('[data-show-camera]')?.dataset.showCamera;
  if (id) location.href = `/cameras?camera=${encodeURIComponent(id)}`;
});

/** Only what is wrong right now, each in one plain line with where and since when; nothing else. */
function renderAttention() {
  const items = []; // [tone, text, where, link]
  for (const i of [...live.openIncidents()].sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity])) {
    items.push([SEVERITY_RANK[i.severity] >= SEVERITY_RANK.High ? 'danger' : 'warn', `${i.severity}: ${i.message}`,
      `${locationOf(i)} · since ${timeOf(i.timestamp)}`, '/incidents']);
  }
  for (const c of live.cameras) {
    const [label, tone, why] = cameraState(c);
    if (tone === 'off' || tone === 'warn') items.push([tone === 'off' ? 'danger' : 'warn', `Camera ${c.name}: ${label}`, `${c.locationZone} · ${why}`, `/cameras?camera=${encodeURIComponent(c.id)}`]);
  }
  const stale = live.sensors.filter((s) => s.state === 'Stale');
  if (stale.length) {
    items.push(['warn', `${stale.length} sensor(s) not reporting`, stale.map((s) => `${s.type} ${s.id} (${s.locationZone})`).join(', '), '/sensors']);
  }
  for (const s of live.sensors.filter((x) => ['Warning', 'Alarm', 'Critical'].includes(x.state))) {
    items.push([s.state === 'Warning' ? 'warn' : 'danger', `${s.type} sensor ${s.id}: ${s.state}`,
      `${s.locationZone} · ${s.type === 'Temperature' ? s.currentValue.toFixed(1) : Math.round(s.currentValue)} ${s.unit}`, '/sensors']);
  }
  const h = live.health;
  if (live.healthError && !h) items.push(['danger', 'Server jawab nahi de raha', live.healthError, '/ai']);
  for (const [key, c] of Object.entries(h?.components ?? {})) {
    if (c.optional || c.status === 'healthy' || key === 'cameras' || key === 'sensors') continue; // cameras and sensors: listed above
    items.push([c.status === 'offline' ? 'danger' : 'warn', `${c.name}: ${HEALTH_SPECIFIC[key]?.[c.status] ?? HEALTH_PLAIN[c.status] ?? c.status}`, c.message, '/ai']);
  }
  const html = items.length ? items.map(([tone, text, where, link]) => `<li class="att att-${tone}"><span class="att-dot" aria-hidden="true"></span>
      <div class="grow"><b>${esc(text)}</b><span>${esc(where)}</span></div><a href="${link}">Open</a></li>`).join('')
    : `<li class="attention-ok">${live.loaded ? 'Everything is working. Problems appear here as soon as they happen.' : 'Checking…'}</li>`;
  if (html !== attentionHtml) $('attention').innerHTML = attentionHtml = html;
}
let attentionHtml = '';

// ================================================================ sensors and sprinklers
const STATE_COLORS = { Normal: '#10b981', Warning: '#f59e0b', Alarm: '#ef4444', Critical: '#ef4444', Stale: '#94a3b8' };
const relayBusy = new Set();

function drawSparkline(canvas, data, threshold, color) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 160, h = canvas.clientHeight || 28;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const max = Math.max(threshold * 1.05, ...data), min = Math.min(0, ...data), range = max - min || 1;
  const offset = HISTORY_POINTS - data.length;
  const x = (i) => ((i + offset) / (HISTORY_POINTS - 1)) * w;
  const y = (v) => h - 2 - ((v - min) / range) * (h - 4);
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = 'rgba(239,68,68,.5)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, y(threshold)); ctx.lineTo(w, y(threshold)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.stroke();
  ctx.lineTo(x(data.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, `${color}55`);
  grad.addColorStop(1, `${color}00`);
  ctx.fillStyle = grad;
  ctx.fill();
}

const SENSOR_LABEL = { Stale: 'Not reporting' };

function renderSensors() {
  if (!$('sensors')) return;
  if (!live.loaded) return;
  $('sensor-count').textContent = `${live.sensors.filter((s) => s.state !== 'Stale').length} of ${live.sensors.length} working · limits in the table`;
  if (!live.sensors.length) { $('sensors').innerHTML = '<tr><td colspan="6" class="empty">No sensor added yet.</td></tr>'; return; }
  $('sensors').innerHTML = live.sensors.map((s) => {
    const color = STATE_COLORS[s.state];
    const value = s.lastReadingAt ? `${s.type === 'Temperature' ? s.currentValue.toFixed(1) : Math.round(s.currentValue)} ${s.unit}` : 'no reading';
    return `<tr class="sensor ${s.state}">
      <td><button type="button" class="link-btn" data-sensor-history="${esc(s.id)}"><b>${esc(s.type)}</b> <span class="muted">${esc(s.id)}</span></button></td>
      <td>${esc(s.locationZone)}</td>
      <td class="strong">${esc(value)} <span class="tiny muted">limit ${s.threshold} ${esc(s.unit)}</span></td>
      <td><span class="state-badge" style="color:${color};background:${color}22">${SENSOR_LABEL[s.state] ?? s.state}</span></td>
      <td class="${s.state === 'Stale' ? 'text-amber' : 'dim'}" data-age="${esc(s.id)}">${sensorAge(s)}</td>
      <td><canvas class="sparkline" data-sensor="${esc(s.id)}" aria-hidden="true"></canvas></td></tr>`;
  }).join('');
  for (const s of live.sensors) {
    const canvas = $('sensors').querySelector(`[data-sensor="${CSS.escape(s.id)}"]`);
    if (canvas) drawSparkline(canvas, s.history ?? [], s.threshold, STATE_COLORS[s.state]);
  }
}

const sensorAge = (s) => (s.lastReadingAt ? `${s.state === 'Stale' ? 'last reading' : 'updated'} ${relativeTime(s.lastReadingAt, live.now)}` : 'no reading yet');
$('sensors')?.addEventListener('click', async (e) => { // a sensor's saved readings (GET …/history), drawn as one line
  const id = e.target.closest('[data-sensor-history]')?.dataset.sensorHistory;
  const box = $('sensor-history');
  if (!id || !box) return;
  box.classList.remove('hidden');
  box.innerHTML = `<h2 class="section-title">History · ${esc(id)}</h2><p class="muted">Loading…</p>`;
  try {
    const h = await api(`/api/iot/sensors/${encodeURIComponent(id)}/history?limit=500`);
    const path = chartPath(h.readings.map((r) => ({ t: Date.parse(r.recordedAt), v: r.value })));
    box.innerHTML = `<div class="section-head"><h2 class="section-title">History · ${esc(id)} (${esc(h.unit)})</h2>
      <button type="button" class="btn btn-ghost btn-sm" data-close-history>Close</button></div>${path
      ? `<svg class="chart" viewBox="0 0 ${CHART_W} ${CHART_H}" preserveAspectRatio="none" role="img" aria-label="Readings of ${esc(id)}">
          <polyline points="${path.line}" fill="none" stroke="#3b82f6" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
        <div class="chart-axis"><span>${dateTimeOf(path.from)}</span><span>max scale ${path.max.toFixed(1)} ${esc(h.unit)}</span><span>${dateTimeOf(path.to)}</span></div>`
      : '<p class="muted">No saved readings yet.</p>'}`;
  } catch (err) {
    box.innerHTML = `<p class="text-red" role="alert">${esc(errorMessage(err))}</p>`;
  }
});
$('sensor-history')?.addEventListener('click', (e) => { if (e.target.closest('[data-close-history]')) $('sensor-history').classList.add('hidden'); });
live.on('tick', () => $('sensors')?.querySelectorAll('[data-age]').forEach((el) => {
  const s = live.sensors.find((x) => x.id === el.dataset.age);
  if (s) el.textContent = sensorAge(s);
}));
/** Ready = Auto and closed (opens by itself in an emergency); Manual = closed, only a person opens it; Active = spraying. */
const relayState = (s) => (s.isActive ? ['Active', 'info'] : s.mode === 'Auto' ? ['Ready', 'ok'] : ['Manual', 'muted']);
const relayTime = (s) => (s.isActive ? `spraying since ${relativeTime(s.lastTriggeredAt, live.now)}`
  : s.lastTriggeredAt ? `last used ${relativeTime(s.lastTriggeredAt, live.now)}` : 'never used');
let selectedRelay = null;

function renderRelays() {
  if (!$('relays')) return;
  const relays = live.sprinklers;
  const active = relays.filter((s) => s.isActive).length;
  const all = relays.length > 0 && active === relays.length;
  $('relay-summary').textContent = `${relays.filter((s) => !s.isActive && s.mode === 'Auto').length} ready · ${active ? `${active} spraying` : 'none spraying'}`;
  $('master-switch').textContent = all ? 'Turn ALL off…' : 'Turn ALL on…';
  $('master-switch').dataset.activate = String(!all);
  const zones = [...new Set(relays.map((s) => s.zone))].sort();
  $('relays').innerHTML = relays.length ? zones.map((zone) => `<li class="panel relay-area"><h2 class="section-title">${esc(zone)}</h2><ul class="stack">${
    relays.filter((s) => s.zone === zone).map((s) => `<li class="relay ${s.isActive ? 'on' : ''} ${relayBusy.has(s.id) ? 'busy' : ''}" data-relay="${esc(s.id)}">
      <button type="button" class="relay-pick ${selectedRelay === s.id ? 'selected' : ''}" data-pick="${esc(s.id)}" aria-pressed="${selectedRelay === s.id}">
        <span><b class="mono">${esc(s.id)}</b><span class="tiny muted">${s.mode} · <span data-rel>${relayTime(s)}</span></span></span>
        <span class="state-pill sp-${relayState(s)[1]}">${relayState(s)[0]}</span></button></li>`).join('')}</ul></li>`).join('')
    : '<li class="empty">No sprinkler added yet.</li>';
  renderRelayDetail();
}

function renderRelayDetail() {
  const box = $('relay-detail');
  const s = live.sprinklers.find((x) => x.id === selectedRelay);
  if (!box || !s) return;
  const busy = relayBusy.has(s.id) ? 'disabled' : '';
  box.innerHTML = `<h2 class="section-title grow">${esc(s.id)} · ${esc(s.zone)} <span class="state-pill sp-${relayState(s)[1]}">${relayState(s)[0]}</span></h2>
    <div class="seg" role="group" aria-label="Mode">${['Auto', 'Manual'].map((m) => `<button type="button" class="seg-btn" data-mode="${m}" aria-pressed="${s.mode === m}" ${busy}>${m}</button>`).join('')}</div>
    <button type="button" class="btn ${s.isActive ? 'btn-ghost' : 'btn-primary'}" data-toggle-relay ${busy}>${s.isActive ? 'Turn off' : 'Turn on'}</button>`;
}

async function sendRelay(id, body) {
  relayBusy.add(id);
  renderRelays();
  try {
    live.upsertSprinkler(await api(`/api/iot/sprinklers/${encodeURIComponent(id)}/toggle`, { method: 'POST', body }));
  } catch (err) {
    toast(`Sprinkler ${id} command failed`, errorMessage(err), 'error');
  } finally {
    relayBusy.delete(id);
    renderRelays();
  }
}

$('relays')?.addEventListener('click', (e) => {
  const id = e.target.closest('[data-pick]')?.dataset.pick;
  if (id) { selectedRelay = id; renderRelays(); }
});
$('relay-detail')?.addEventListener('click', async (e) => {
  const s = live.sprinklers.find((x) => x.id === selectedRelay);
  if (!s) return;
  const mode = e.target.closest('[data-mode]');
  if (mode && mode.getAttribute('aria-pressed') !== 'true') sendRelay(s.id, { mode: mode.dataset.mode });
  if (e.target.closest('[data-toggle-relay]') && await confirmAsk(s.isActive ? `Turn ${s.id} off?` : `Turn ${s.id} on?`,
    s.isActive ? 'Water stops in this area.' : `Water comes out in ${s.zone}.`, s.isActive ? 'Turn off' : 'Turn on')) {
    sendRelay(s.id, { isActive: !s.isActive });
  }
});
$('master-switch')?.addEventListener('click', async (e) => {
  const activate = e.target.dataset.activate !== 'false';
  const ok = await confirmAsk(activate ? 'Activate ALL sprinklers?' : 'Deactivate all sprinklers?',
    activate ? 'Every relay in every zone will open and discharge water, including relays in Manual mode.'
      : 'All relays will close. Only do this once the area is confirmed safe.', activate ? 'Activate all' : 'Deactivate all');
  if (!ok) return;
  e.target.disabled = true;
  try {
    const res = await api('/api/iot/sprinklers/master', { method: 'POST', body: { isActive: activate } });
    res.sprinklers.forEach((s) => live.upsertSprinkler(s));
    toast(activate ? 'All sprinklers activated' : 'All sprinklers deactivated', `${res.changed.length} relay(s) changed`, activate ? 'critical' : 'success');
  } catch (err) {
    toast('Master switch failed', errorMessage(err), 'error');
  } finally {
    e.target.disabled = false;
    renderRelays();
  }
});
live.on('sensors', renderSensors);
live.on('sprinklers', renderRelays);
live.on('tick', () => $('relays')?.querySelectorAll('[data-relay]').forEach((li) => {
  const s = live.sprinklers.find((x) => x.id === li.dataset.relay);
  if (s) li.querySelector('[data-rel]').textContent = relayTime(s);
}));

// ================================================================ alert logs
const SOURCE_LABEL = { Vision: 'AI Vision', Sensor: 'Sensor', Manual: 'Manual', CameraLost: 'Camera lost', SensorLost: 'Sensor lost' };
let incidentFilter = 'open', selectedIncident = null;
const resolving = new Set();
const locationOf = (i) => (i.cameraName ? `${i.zone} (${i.cameraName})` : i.sensorId ? `${i.zone} (sensor ${i.sensorId})` : i.zone);

function renderIncidents() {
  const body = $('incident-rows');
  if (!body) return;
  $('open-count').textContent = live.openIncidents().length;
  if (!live.loaded) {
    body.innerHTML = `<tr><td colspan="10" class="empty">${live.state === 'disconnected' ? 'Cannot reach the realtime server — retrying…' : 'Loading incidents…'}</td></tr>`;
    return;
  }
  const rows = incidentFilter === 'open' ? live.openIncidents() : live.incidents;
  body.innerHTML = rows.length ? rows.map((i) => {
    const critical = i.severity === 'Critical' && !i.isResolved;
    return `<tr class="inc ${critical ? 'row-critical' : ''} ${i.isResolved ? 'row-resolved' : ''} ${selectedIncident === i.id ? 'selected' : ''}">
      <td><span class="sev sev-${i.severity}">${i.severity}</span></td>
      <td class="inc-main"><button type="button" class="link-btn" data-pick-incident="${i.id}">${esc(i.message)}</button>
        <div class="tiny muted" title="${esc(dateTimeOf(i.timestamp))}">${esc(locationOf(i))} · #${i.id} · since ${timeOf(i.timestamp)}</div></td>
      <td class="nowrap small">${SOURCE_LABEL[i.source] ?? esc(i.source)}${i.corroborated
        ? ' <span class="tag tag-fire" title="A smoke sensor in the same zone read its warning level within the fusion window">+ SMOKE SENSOR</span>' : ''}</td>
      <td class="small">${i.fireDetected ? esc(fireSeverityText(i.fireSeverity) ?? 'Yes') : '—'}</td>
      <td class="small nowrap">${i.humanCount} people${i.dangerCount > 0 ? ` · <span class="text-amber">${i.dangerCount} in danger</span>` : ''}</td>
      <td class="small strong ${i.falseAlarm ? 'muted' : i.isResolved ? 'text-green' : 'text-amber'}">${i.falseAlarm ? 'False alarm' : i.isResolved ? 'Resolved' : 'Open'}</td>
      <td class="nowrap">${i.isResolved ? '' : resolveButtons(i)}</td></tr>`;
  }).join('') : `<tr><td colspan="7" class="empty">${incidentFilter === 'open' ? 'No open incidents.' : 'No incidents recorded.'}</td></tr>`;
  renderIncidentDetail();
}

const resolveButtons = (i) => `<button type="button" class="btn btn-sm btn-success" data-resolve="${i.id}" ${resolving.has(i.id) ? 'disabled' : ''}>Resolve</button>
  <button type="button" class="btn btn-sm btn-ghost" data-resolve="${i.id}" data-false-alarm ${resolving.has(i.id) ? 'disabled' : ''}>False alarm</button>`;

/** The selected incident in plain words; its history is the "What happened" panel below it. */
function renderIncidentDetail() {
  const box = $('incident-detail');
  const i = live.incidents.find((x) => x.id === selectedIncident);
  if (!box || !i) return;
  const by = i.cameraName ? `${i.cameraName} camera` : i.sensorId ? `sensor ${i.sensorId}` : SOURCE_LABEL[i.source] ?? i.source;
  box.innerHTML = `<p class="tiny strong ${i.isResolved ? 'text-green' : 'text-red'}">${i.severity.toUpperCase()} · ${i.falseAlarm ? 'FALSE ALARM' : i.isResolved ? 'RESOLVED' : 'OPEN'} · #${i.id}</p>
    <h2 class="detail-title">${esc(i.message)}</h2>
    <dl class="facts">
      <div><dt>Where</dt><dd>${esc(i.zone)}</dd></div>
      <div><dt>Seen by</dt><dd>${esc(by)}${i.corroborated ? ' · confirmed by a smoke sensor' : ''}</dd></div>
      <div><dt>Fire size</dt><dd>${i.fireDetected ? esc(fireSeverityText(i.fireSeverity) ?? 'yes') : 'no fire'}</dd></div>
      <div><dt>People</dt><dd>${i.humanCount} seen · ${i.dangerCount} in danger</dd></div>
      <div><dt>Opened</dt><dd>${esc(dateTimeOf(i.timestamp))}</dd></div>
      ${i.resolvedAt ? `<div><dt>Resolved</dt><dd>${esc(dateTimeOf(i.resolvedAt))}</dd></div>` : ''}
    </dl>
    ${i.isResolved ? '' : `<div class="row">${resolveButtons(i)}</div>`}`;
}

$('incident-filter')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  incidentFilter = b.dataset.filter;
  for (const x of $('incident-filter').children) x.setAttribute('aria-selected', String(x === b));
  renderIncidents();
});
async function onIncidentClick(e) {
  const picked = Number(e.target.closest('[data-pick-incident]')?.dataset.pickIncident);
  if (picked) { // select it: its details and its history
    selectedIncident = picked;
    renderIncidents();
    if ($('history-incident')) { $('history-incident').value = String(picked); history.incidentId = picked; loadHistory(); }
    return;
  }
  const button = e.target.closest('[data-resolve]');
  const id = Number(button?.dataset.resolve);
  if (!id || resolving.has(id)) return;
  const falseAlarm = 'falseAlarm' in button.dataset;
  if (falseAlarm && !await confirmAsk(`Mark incident #${id} as a false alarm?`,
    'The incident is resolved and kept in the history as a false alarm. Sprinklers are not changed.', 'False alarm')) return;
  resolving.add(id);
  renderIncidents();
  try {
    live.upsertIncident(await api(`/api/incidents/${id}/${falseAlarm ? 'false-alarm' : 'resolve'}`, { method: 'POST' }));
  } catch (err) {
    toast('Could not resolve incident', errorMessage(err), 'error');
  } finally {
    resolving.delete(id);
    renderIncidents();
  }
}
$('incident-rows')?.addEventListener('click', onIncidentClick);
$('incident-detail')?.addEventListener('click', onIncidentClick);
live.on('incidents', renderIncidents);
live.on('state', renderIncidents);

// ================================================================ fire & smoke tracking
const percent = (v) => (v === null || v === undefined ? '' : ` · confidence ${Math.round(v * 100)}%`);

function renderFireSmoke() {
  if (!$('fire-smoke') || !live.loaded) return;
  const stats = live.liveStats();
  const where = (s) => { const c = live.cameras.find((x) => x.id === s.cameraId); return c ? `${c.name} · ${c.locationZone}` : s.cameraId; };
  const item = (title, text) => `<li><b>${esc(title)}</b><span>${esc(text)}</span></li>`;
  const fireNow = stats.filter((s) => s.fireDetected);
  const fire = [...fireNow.map((s) => item(where(s), `AI camera, now${percent(s.fireConfidence)}${s.fireSeverity ? ` · ${fireSeverityText(s.fireSeverity)}` : ''}`)),
    ...live.openIncidents().filter((i) => i.fireDetected).map((i) => item(`Incident #${i.id} · ${locationOf(i)}`,
      `${i.severity}, open since ${timeOf(i.timestamp)} (${relativeTime(i.timestamp, live.now)})`))];
  const smokeNow = stats.filter((s) => s.smokeDetected);
  const smokeSensors = live.sensors.filter((s) => s.type === 'Smoke' && ['Warning', 'Alarm', 'Critical'].includes(s.state));
  const smoke = [...smokeNow.map((s) => item(where(s), `AI camera, now${percent(s.smokeConfidence)}`)),
    ...smokeSensors.map((s) => item(`Smoke sensor ${s.id} · ${s.locationZone}`, `${Math.round(s.currentValue)} ${s.unit}, ${s.state} · ${relativeTime(s.lastReadingAt, live.now)}`))];
  const card = (kind, label, count, items, clear) => `<article class="fs-card fs-${kind} ${items.length ? 'active' : ''}">
    <div class="fs-head"><span>${label}</span><b>${count}</b></div>
    <p class="tiny">${count ? `${count} camera(s) see it now` : items.length ? 'not seen by a camera now' : clear}</p>
    ${items.length ? `<ul class="fs-list">${items.join('')}</ul>` : ''}</article>`;
  $('fire-smoke').innerHTML = `<div class="fs-grid">${card('fire', 'Fire', fireNow.length, fire, 'No fire detected right now.')}
    ${card('smoke', 'Smoke', smokeNow.length, smoke, 'No smoke detected right now.')}</div>`;
}
for (const name of ['stats', 'incidents', 'sensors', 'cameras', 'snapshot', 'tick']) live.on(name, renderFireSmoke);

// ================================================================ incident history
const CHART_W = 600, CHART_H = 140;
const history = { incidentId: null, type: '', logs: [], sensor: null, trend: null, loading: false, error: null, request: 0 };
const TREND_COLOR = { Increasing: 'text-red', Decreasing: 'text-green', Stable: 'text-amber' };

/** SVG polyline for a time series scaled into a CHART_W x CHART_H box; null when there is nothing to draw. */
export function chartPath(points) {
  if (!points.length) return null;
  const from = points[0].t, to = points[points.length - 1].t;
  const max = Math.max(...points.map((p) => p.v)) * 1.1 || 1;
  const span = to - from || 1;
  return { line: points.map((p) => `${(((p.t - from) / span) * CHART_W).toFixed(1)},${(CHART_H - (p.v / max) * CHART_H).toFixed(1)}`).join(' '), max, from, to };
}

async function loadHistory() {
  const request = ++history.request; // a newer filter wins over a slower older reply
  const { incidentId, type } = history;
  Object.assign(history, { loading: true, sensor: null, trend: null });
  renderHistory();
  const q = new URLSearchParams({ limit: '500' });
  if (incidentId) q.set('incidentId', incidentId);
  if (type) q.set('type', type);
  try {
    const logs = await api(`/api/incidents/logs?${q}`);
    if (request !== history.request) return;
    Object.assign(history, { logs, loading: false, error: null });
  } catch (err) {
    if (request !== history.request) return;
    Object.assign(history, { loading: false, error: errorMessage(err) });
  }
  const incident = live.incidents.find((i) => i.id === incidentId);
  if (incident?.sensorId) {
    api(`/api/iot/sensors/${encodeURIComponent(incident.sensorId)}/history?limit=500&since=${encodeURIComponent(incident.timestamp)}`)
      .then((s) => { if (request === history.request) { history.sensor = s; renderHistory(); } }).catch(() => undefined);
  }
  loadTrend();
  renderHistory();
}

/** The server computes the fire-area trend (one implementation, also used for the FireGrowth warning); reading it never warns. */
function loadTrend() {
  const { incidentId, request } = history;
  if (live.incidents.find((i) => i.id === incidentId)?.source !== 'Vision') return;
  api(`/api/incidents/${incidentId}`)
    .then((i) => { if (request === history.request) { history.trend = i.fireTrend; renderHistory(); } }).catch(() => undefined);
}

function renderHistory() {
  const select = $('history-incident');
  if (!select) return;
  const chosen = select.value;
  select.innerHTML = '<option value="">All incidents</option>' + live.incidents.map((i) =>
    `<option value="${i.id}">#${i.id} · ${i.severity} · ${esc(i.zone)}</option>`).join('');
  select.value = chosen;

  const incident = live.incidents.find((i) => i.id === history.incidentId);
  let chart = '';
  if (incident) {
    let label, unit, path;
    if (incident.sensorId && history.sensor) {
      const s = history.sensor;
      [label, unit, path] = [`Sensor readings: ${s.sensorId} (${s.unit})`, s.unit, chartPath(s.readings.map((r) => ({ t: Date.parse(r.recordedAt), v: r.value })))];
    } else {
      const m = history.logs.filter((l) => l.type === 'FireMeasurement' && l.fireAreaPercent !== null).reverse();
      [label, unit, path] = ['AI fire bounding-box area (% of camera frame) — not a physical fire size', '%',
        chartPath(m.map((l) => ({ t: Date.parse(l.timestamp), v: l.fireAreaPercent })))];
    }
    const t = !incident.sensorId && history.trend;
    const trend = !t ? '' : `<div class="tiny" style="margin-bottom:6px" data-trend>Trend (last ${t.windowSeconds / 60} min):
      <b class="${TREND_COLOR[t.trend] ?? 'muted'}">${esc(t.trend)}</b>${t.spanSeconds !== undefined
        ? ` <span class="dim">· ${t.fromPercent}% → ${t.toPercent}% in ${t.spanSeconds} s</span>`
        : ` <span class="dim">· ${t.measurements} measurement(s); needs at least 4 over 20 s. No detection is no proof the fire is out.</span>`}
      <span class="muted">— share of the camera frame, not physical size or a spread forecast</span></div>`;
    chart = `<div class="panel-body" style="border-bottom:1px solid var(--ink-700);padding:12px"><div class="tiny strong dim" style="margin-bottom:4px">${esc(label)}</div>${trend}
      ${path ? `<svg class="chart" viewBox="0 0 ${CHART_W} ${CHART_H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
        <polyline points="${path.line}" fill="none" stroke="#ef4444" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
        <div class="chart-axis"><span>${timeOf(path.from)}</span><span>max scale ${path.max.toFixed(1)} ${esc(unit)}</span><span>${timeOf(path.to)}</span></div>`
        : `<p class="tiny muted">No measurements recorded for incident #${incident.id} (${incident.source}). Manual dispatches have no measurements;
          for camera incidents choose "All event types" or "FireMeasurement".</p>`}</div>`;
  }
  $('history-chart').innerHTML = chart;
  $('history-table').innerHTML = history.loading ? '<p class="empty">Loading history…</p>'
    : history.error ? `<p class="panel-body text-red" role="alert">${esc(history.error)}</p>`
      : `<table class="data" style="font-size:12px"><thead><tr><th>Time</th><th>Incident</th><th>Event</th><th>Details</th></tr></thead><tbody>${
        history.logs.length ? history.logs.map((l) => `<tr class="${l.type === 'PersonInDanger' ? 'row-person' : ''}">
          <td class="mono nowrap">${timeOf(l.timestamp)}</td><td class="mono">${l.incidentId ? `#${l.incidentId}` : '—'}</td>
          <td class="nowrap">${l.type}${l.simulated ? '<span class="tag tag-sim">SIMULATED</span>' : ''}${l.type === 'EvacuationRoute' ? '<span class="tag tag-est">ESTIMATE</span>' : ''}${l.fireSeverity ? `<span class="tag tag-fire">FIRE ${esc(fireSeverityText(l.fireSeverity))}</span>` : ''}</td>
          <td style="color:var(--text-2)">${esc(l.details)}</td></tr>`).join('')
          : '<tr><td colspan="4" class="empty">No history entries for this filter.</td></tr>'}</tbody></table>`;
}

$('history-incident')?.addEventListener('change', (e) => { history.incidentId = e.target.value ? Number(e.target.value) : null; loadHistory(); });
$('history-type')?.addEventListener('change', (e) => { history.type = e.target.value; loadHistory(); });
live.on('incidentLog', (l) => {
  if ((history.incidentId === null || l.incidentId === history.incidentId) && (!history.type || l.type === history.type)) {
    history.logs = [l, ...history.logs].slice(0, 500);
    renderHistory();
  }
  if (l.incidentId === history.incidentId && (l.type === 'FireMeasurement' || l.type === 'FireGrowth')) loadTrend();
});
live.on('incidents', () => {
  const select = $('history-incident');
  if (select && select.options.length !== live.incidents.length + 1) renderHistory();
});
if ($('history-table')) loadHistory();

// ================================================================ evacuation route
const PEOPLE_EXPIRE_MS = 4500; // as the camera tile: a camera that sent nothing for this long no longer places people
const RISK_RGBA = ['', 'rgba(250,204,21,0.35)', 'rgba(249,115,22,0.5)', 'rgba(239,68,68,0.6)', 'rgba(185,28,28,0.85)'];
const round1 = (v) => Math.round(v * 10) / 10;
const evac = { plans: [], planId: null, imageUrl: null, start: null, result: null, routing: false, error: null, risk: null, seen: new Map(),
  people: [], openZonesKey: '', map: null, compare: null, compareError: null, highlight: null, rescue: null, rescueError: null, rescueBusy: false, rescueAt: 0 };
const RESCUE_EVERY_MS = 5000, MARKER_STALE_MS = 2000; // markers older than this are shown as stale; PEOPLE_EXPIRE_MS removes them
const EXIT_STATUS_CLASS = { Available: 'text-green', Danger: 'text-red', Blocked: 'text-amber', Unreachable: 'muted', Unknown: 'muted' };
const metres = (v) => (v === null || v === undefined ? '—' : `${v.toFixed(1)} m`); // unknown is never shown as 0
const seconds = (v) => (v === null || v === undefined ? '—' : `~${Math.round(v)} s`);

/** Perspective transform that maps 4 camera-frame points onto the same 4 floor-plan points; null when 3 lie on one line. */
export function homography(src, dst) {
  const a = src.flatMap(([x, y], i) => {
    const [u, v] = dst[i];
    return [[x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]];
  });
  for (let c = 0; c < 8; c++) { // Gauss-Jordan elimination with partial pivoting
    let p = c;
    for (let r = c + 1; r < 8; r++) if (Math.abs(a[r][c]) > Math.abs(a[p][c])) p = r;
    if (Math.abs(a[p][c]) < 1e-12) return null;
    [a[c], a[p]] = [a[p], a[c]];
    for (let r = 0; r < 8; r++) {
      if (r === c) continue;
      const f = a[r][c] / a[c][c];
      for (let k = c; k < 9; k++) a[r][k] -= f * a[c][k];
    }
  }
  return [...a.map((row, i) => row[8] / row[i]), 1];
}

export function project(h, [x, y]) {
  const w = h[6] * x + h[7] * y + h[8];
  return [(h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w];
}

const currentPlan = () => evac.plans.find((p) => p.id === evac.planId) ?? null;

function riskLayer() {
  const r = evac.risk;
  if (!r?.active) return null;
  const canvas = Object.assign(document.createElement('canvas'), { width: r.cols, height: r.rows });
  const ctx = canvas.getContext('2d');
  r.cells.forEach((v, i) => { if (v > 0) { ctx.fillStyle = RISK_RGBA[v]; ctx.fillRect(i % r.cols, Math.floor(i / r.cols), 1, 1); } });
  return { url: canvas.toDataURL(), width: r.cols * r.cellMeters, height: r.rows * r.cellMeters };
}

function renderEvacuation() {
  const box = $('evacuation');
  const plan = currentPlan();
  if (!box || !plan) return;
  if (!evac.map) {
    box.innerHTML = '<div class="map-layout"><div class="panel map-main" data-map></div><div class="panel evac-details"><h2 class="section-title">Way out from the clicked point</h2><p class="small muted" data-hint>Click a point on the plan, or an amber dot (a person a calibrated camera sees).</p><div data-risk></div><div class="small" style="margin-top:12px" aria-live="polite" data-result></div><div data-compare></div><div data-rescue></div><div data-safe></div></div></div>';
    evac.map = new PlanMap(box.querySelector('[data-map]'), onPlanPointer);
    box.addEventListener('click', (e) => { // "Show" in the exit comparison: draw that exit's route
      const b = e.target.closest('[data-exit]');
      if (b) { evac.highlight = Number(b.dataset.exit); renderEvacuation(); }
    });
  }
  const shown = evac.highlight !== null ? evac.compare?.results?.[0]?.exits?.[evac.highlight] : null;
  evac.map.set({ width: plan.widthMeters, height: plan.heightMeters, elements: plan.elements, imageUrl: evac.imageUrl,
    route: shown?.found ? shown.path : evac.result?.path ?? null,
    start: evac.start, people: evac.people, riskLayer: evac.riskLayerCache ?? null, activeDanger: evac.result?.activeDangerZones ?? evac.risk?.activeDangerZones ?? [],
    cameras: cameraMarks(plan) });

  const rk = evac.risk;
  box.querySelector('[data-risk]').innerHTML = rk?.active ? `<div class="risk-box" role="status"><p class="strong text-orange">⚠ Fire risk: ${esc(rk.message)}</p>
    <ul>${rk.areas.filter((a) => a.level !== 'None').map((a) => `<li><span class="swatch risk-${a.level}"></span><b>${esc(a.name)}</b> ${a.level}${a.distanceMeters ? ` · ${a.distanceMeters} m` : ''}</li>`).join('')}</ul>
    <p class="muted" style="margin-top:6px">Rule-based estimate: distance from ${esc(rk.activeDangerZones.join(', '))} through rooms and doors (walls block it).
      High ≤ 5 m, Medium ≤ 10 m, Low ≤ 20 m. Not a fire-spread simulation.</p></div>` : '';

  const r = evac.result;
  let result;
  if (!evac.start) {
    result = '';
  } else if (evac.routing) {
    result = '<p class="muted">Calculating…</p>';
  } else if (evac.error) {
    result = `<p class="text-red" role="alert">${esc(evac.error)}</p>`;
  } else if (r) {
    result = r.found && r.clearanceViolated ? `<p class="strong text-red" role="alert">⚠ ${esc(r.message)}</p>`
      : r.found ? `<p class="strong text-green">${esc(r.message)}</p><p style="margin-top:4px;color:var(--text-2)">Distance <b>${r.distanceMeters.toFixed(1)} m</b> ·
        Estimated walking time <b>~${Math.round(r.estimatedSeconds)} s</b> <span class="muted">(= distance ÷ ${r.walkingSpeedMetersPerSecond} m/s; an estimate, not a guaranteed time)</span></p>`
        : `<p class="strong text-red" role="alert">⚠ ${esc(r.message)}</p>`;
    if (r.activeDangerZones.length) result += `<p class="tiny" style="margin-top:4px;color:#fca5a5">Avoided active danger zones: ${esc(r.activeDangerZones.join(', '))}</p>`;
    result += `<p class="tiny muted" style="margin-top:4px">Start (${evac.start[0]}, ${evac.start[1]}) m · recalculated automatically when incidents open or close.</p>`;
  }
  box.querySelector('[data-result]').innerHTML = result ?? '';
  box.querySelector('[data-hint]').classList.toggle('hidden', !!evac.start);
  box.querySelector('[data-compare]').innerHTML = compareHtml();
  renderRescue();

  const safe = plan.elements.filter((e) => e.type === 'SafeZone');
  box.querySelector('[data-safe]').innerHTML = !safe.length ? '' : `<h3 class="panel-title" style="margin-top:16px">Safe zones on this floor</h3>${safe.length
    ? `<ul class="tiny" style="margin-top:4px;color:var(--text-2)">${safe.map((z) => `<li class="row" style="padding:2px 0"><span class="swatch" style="border:1px solid #22c55e;background:rgb(34 197 94 / .25)"></span>
        <b>${esc(z.name || 'Safe zone')}</b> — x ${z.x} m, y ${z.y} m, ${z.width} × ${z.height} m</li>`).join('')}</ul>`
    : ''}`;
}

/** Every exit from the chosen start point (POST …/routes, read-only). A row without a route shows no distance, time or line. */
function compareHtml() {
  if (!evac.start) return '';
  if (evac.compareError) return `<p class="tiny text-red" role="alert" style="margin-top:12px">Exit comparison failed: ${esc(evac.compareError)}</p>`;
  const c = evac.compare?.results?.[0];
  if (!c) return '<p class="tiny muted" style="margin-top:12px">Comparing exits…</p>';
  if (!c.exits.length) return `<p class="tiny muted" style="margin-top:12px">Exit comparison: ${esc(c.message)}</p>`;
  const rows = c.exits.map((e, i) => `<tr><td class="strong">${esc(e.exitName || 'Exit')}</td>
    <td class="${EXIT_STATUS_CLASS[e.status] ?? 'muted'}">${esc(e.status)}</td><td class="mono">${metres(e.distanceMeters)}</td>
    <td class="mono">${seconds(e.estimatedSeconds)}</td>
    <td>${e.found ? `<button type="button" class="btn btn-ghost btn-sm" data-exit="${i}" aria-pressed="${evac.highlight === i}">${evac.highlight === i ? 'Shown' : 'Show'}</button>` : ''}
      ${e.reason ? `<span class="tiny muted">${esc(e.reason)}</span>` : ''}</td></tr>`).join('');
  return `<h3 class="panel-title" style="margin-top:16px">Exit comparison</h3>
    <div style="overflow-x:auto"><table class="data" data-exits><thead><tr><th>Exit</th><th>Status</th><th>Distance</th><th>Est. time</th><th>Route</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <p class="tiny muted">${esc(c.message)} Times = distance ÷ ${evac.compare.walkingSpeedMetersPerSecond} m/s, an estimate. Danger = the exit is in an
      active danger zone or every route to it passes closer than ${evac.compare.requiredClearanceMeters} m to one.</p>`;
}

/** Rescue list: the current camera person markers on this plan (not identified people), each with its nearest safe exit. */
function renderRescue() {
  const box = $('evacuation')?.querySelector('[data-rescue]');
  if (!box || !currentPlan()) return;
  const now = Date.now();
  const r = evac.rescue;
  const current = (r?.markers ?? []).map((m, i) => ({ ...m, result: r.results[i] })).filter((m) => now - m.at <= PEOPLE_EXPIRE_MS); // expired: gone
  const unknown = live.cameras.filter((c) => { // people seen by cameras not calibrated to this plan: no location on it
    const s = live.frameStats[c.id];
    return s && s.humanCount > 0 && now - s.receivedAt <= PEOPLE_EXPIRE_MS && c.calibration?.floorPlanId !== evac.planId;
  });
  const rows = current.map((m) => {
    const res = m.result, safe = res?.exits.find((e) => e.status === 'Available');
    const stale = now - m.at > MARKER_STALE_MS;
    const where = `(${m.x.toFixed(1)}, ${m.y.toFixed(1)}) m`;
    const danger = stale ? `<span class="text-amber">Location stale · ${Math.round((now - m.at) / 1000)} s old</span>`
      : !res ? '<span class="muted">Unknown</span>'
      : res.startInDangerZone ? '<span class="text-red strong">In active danger zone (plan)</span>'
      : res.exits.every((e) => e.status === 'Unknown' || e.status === 'Blocked') ? `<span class="muted">Unknown: ${esc(res.message)}</span>`
      : '<span class="dim">Not in an active danger zone (plan)</span>';
    const exit = stale || !res ? '—' : safe ? esc(safe.exitName || 'Exit') : '<span class="text-red strong">No safe route available</span>';
    return `<tr><td>Person marker ${m.n} <span class="tiny muted">(${esc(m.cameraName)})</span></td><td class="mono">${where}</td><td>${danger}</td>
      <td>${exit}</td><td class="mono">${stale ? '—' : metres(safe?.distanceMeters)}</td><td class="mono">${stale ? '—' : seconds(safe?.estimatedSeconds)}</td></tr>`;
  }).join('');
  box.innerHTML = `<h3 class="panel-title" style="margin-top:16px">Rescue list (estimate)</h3>
    ${evac.rescueError ? `<p class="tiny text-red" role="alert">Rescue list failed: ${esc(evac.rescueError)}</p>` : ''}
    ${rows ? `<div style="overflow-x:auto"><table class="data" data-rescue-list><thead><tr><th>Marker</th><th>Plan location</th><th>Danger status</th>
      <th>Nearest safe exit</th><th>Distance</th><th>Est. time</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<p class="tiny muted">No person markers from cameras calibrated to this plan right now.</p>'}
    ${unknown.map((c) => `<p class="tiny text-amber">${esc(c.name)}: ${live.frameStats[c.id].humanCount} person(s) seen — location unknown (camera not calibrated to this plan).</p>`).join('')}
    ${rows ? `<p class="tiny muted">Markers are camera estimates of where feet touch the floor, refreshed every ${RESCUE_EVERY_MS / 1000} s. They are not identified
      people: one person seen by two cameras can appear twice, and numbers change between refreshes. Danger status is computed from the plan
      position, not from the camera's own at-risk flag.</p>` : ''}`;
}

async function loadComparison() {
  const id = evac.planId, s = evac.start;
  Object.assign(evac, { compare: null, compareError: null, highlight: null });
  try {
    const c = await api(`/api/floorplans/${id}/routes`, { method: 'POST', body: { starts: [s] } });
    if (evac.planId === id && evac.start === s) evac.compare = c;
  } catch (err) {
    if (evac.planId === id && evac.start === s) evac.compareError = errorMessage(err);
  }
  renderEvacuation();
}

async function loadRescue() {
  const id = evac.planId;
  if (!id || evac.rescueBusy) return;
  evac.rescueAt = Date.now();
  const cameras = new Map(live.cameras.map((c) => [c.id, c.name]));
  const markers = [...evac.seen].flatMap(([cameraId, v]) => v.points.map((p) => ({ cameraName: cameras.get(cameraId) ?? cameraId, at: v.at, x: p.x, y: p.y })))
    .filter((m) => Date.now() - m.at <= PEOPLE_EXPIRE_MS).slice(0, 50).map((m, i) => ({ ...m, n: i + 1 }));
  if (!markers.length) { Object.assign(evac, { rescue: { markers: [], results: [] }, rescueError: null }); renderRescue(); return; }
  evac.rescueBusy = true;
  try {
    const r = await api(`/api/floorplans/${id}/routes`, { method: 'POST', body: { starts: markers.map((m) => [round1(m.x), round1(m.y)]) } });
    if (evac.planId === id) Object.assign(evac, { rescue: { markers, results: r.results }, rescueError: null });
  } catch (err) {
    if (evac.planId === id) evac.rescueError = errorMessage(err);
  } finally {
    evac.rescueBusy = false;
  }
  renderRescue();
}

function setImage(url) {
  if (evac.imageUrl) URL.revokeObjectURL(evac.imageUrl);
  evac.imageUrl = url;
}

async function choosePlan(id) {
  Object.assign(evac, { planId: id, start: null, result: null, routing: false, error: null, risk: null, riskLayerCache: null, people: [],
    compare: null, compareError: null, highlight: null, rescue: null, rescueError: null, rescueAt: 0 });
  evac.seen.clear();
  setImage(null);
  $('evac-plan').value = String(id);
  renderEvacuation();
  loadRisk();
  if (currentPlan()?.hasImage) {
    try {
      const blob = await api(`/api/floorplans/${id}/image`);
      if (evac.planId === id) { setImage(URL.createObjectURL(blob)); renderEvacuation(); } // a late image of a plan already left is dropped
    } catch { /* the plan still works without its background */ }
  }
}

async function loadRisk() {
  const id = evac.planId;
  if (!id) return;
  try {
    const r = await api(`/api/floorplans/${id}/risk`);
    if (evac.planId !== id) return;
    evac.risk = r;
    evac.riskLayerCache = riskLayer();
    renderEvacuation();
  } catch { /* the risk estimate is optional */ }
}

async function calculateRoute() {
  const id = evac.planId, s = evac.start;
  if (!id || !s) return;
  evac.routing = true;
  renderEvacuation();
  loadComparison();
  try {
    const r = await api(`/api/floorplans/${id}/route`, { method: 'POST', body: { x: s[0], y: s[1] } });
    if (evac.planId !== id) return;
    Object.assign(evac, { result: r, routing: false, error: null });
  } catch (err) {
    if (evac.planId !== id) return;
    Object.assign(evac, { result: null, routing: false, error: errorMessage(err) });
  }
  renderEvacuation();
}

/** Cameras calibrated to this plan: the floor area they see (calibration points) and how they are doing. */
function cameraMarks(plan) {
  return live.cameras.filter((c) => c.calibration?.floorPlanId === plan.id && c.calibration.planPoints?.length === 4).map((c) => {
    const s = live.liveStats().find((x) => x.cameraId === c.id);
    const tone = cameraState(c)[1] === 'off' ? 'off' : s?.fireDetected ? 'danger' : s?.smokeDetected ? 'warn' : 'ok';
    return { id: c.id, name: c.name, points: c.calibration.planPoints, tone };
  });
}

function refreshCameraMarks() {
  if (evac.cameraShown) showCamera(evac.cameraShown); // its details follow the camera, as the marks do
  const plan = currentPlan();
  if (!plan || !evac.map) return;
  const cameras = cameraMarks(plan);
  const key = JSON.stringify(cameras.map((c) => [c.id, c.tone]));
  if (key !== evac.camerasKey) { evac.camerasKey = key; evac.map.set({ cameras }); }
}
for (const name of ['cameras', 'stats', 'health', 'tick']) live.on(name, refreshCameraMarks);

function showCamera(id) {
  const c = live.cameras.find((x) => x.id === id);
  evac.cameraShown = c ? id : null;
  if (!c) return;
  const s = live.liveStats().find((x) => x.cameraId === id);
  const [label, tone, why] = cameraState(c);
  const seen = tone === 'off' ? `<b class="text-red">Camera Offline</b> (${esc(why)})` : !s ? '<span class="muted">no AI data right now</span>'
    : `${s.fireDetected ? '<b class="text-red">Fire detected</b> · ' : ''}${s.smokeDetected ? '<b class="text-amber">Smoke detected</b> · ' : ''}${s.humanCount} person(s) in view`;
  const html = `<b>${esc(c.name)}</b> · ${esc(c.locationZone)} · ${label} · ${seen}
    <button type="button" class="btn btn-ghost btn-sm" data-open-camera="${esc(c.id)}">Open camera</button>`;
  if (html !== evac.cameraHtml) $('map-detail').innerHTML = evac.cameraHtml = html; // refreshed every second: rebuilt only on change
}
$('map-detail')?.addEventListener('click', (e) => {
  const id = e.target.closest('[data-open-camera]')?.dataset.openCamera;
  if (id) location.href = `/cameras?camera=${encodeURIComponent(id)}`;
});

function onPlanPointer(p) {
  if (p.kind !== 'down') return;
  if (p.camera) { showCamera(p.camera); return; } // a camera mark: its details, not a route
  const person = evac.people.find((q) => Math.hypot(q.x - p.x, q.y - p.y) < 1); // clicked a live person dot
  evac.start = person ? [round1(person.x), round1(person.y)] : [round1(p.x), round1(p.y)];
  calculateRoute();
}

function placePeople(f) {
  const plan = currentPlan();
  const cal = live.cameras.find((c) => c.id === f.cameraId)?.calibration;
  if (!plan || !cal || cal.floorPlanId !== plan.id) return;
  const h = homography(cal.imagePoints, cal.planPoints);
  if (!h) return;
  const sx = f.normalized ? f.frameWidth : 1, sy = f.normalized ? f.frameHeight : 1;
  const points = f.detections.filter((d) => d.category === 'Human')
    .map((d) => project(h, [(d.bbox[0] + d.bbox[2] / 2) * sx, (d.bbox[1] + d.bbox[3]) * sy])) // the feet: valid on the floor plane
    .filter(([x, y]) => x >= 0 && y >= 0 && x <= plan.widthMeters && y <= plan.heightMeters)
    .map(([x, y]) => ({ x, y }));
  evac.seen.set(f.cameraId, { at: Date.now(), points });
  refreshPeople();
  if (points.length && Date.now() - evac.rescueAt >= RESCUE_EVERY_MS) loadRescue(); // new markers: list them now, at most every 5 s
}

function refreshPeople() {
  const now = Date.now();
  for (const [id, v] of evac.seen) if (now - v.at > PEOPLE_EXPIRE_MS) evac.seen.delete(id);
  const people = [...evac.seen.values()].flatMap((v) => v.points);
  if (people.length || evac.people.length) { evac.people = people; if (evac.map) evac.map.set({ people }); }
}

$('evac-plan')?.addEventListener('change', (e) => choosePlan(Number(e.target.value)));
live.on('detection', placePeople);
live.on('tick', () => { refreshPeople(); renderRescue(); }); // ages and stale labels move on between rescue refreshes
setInterval(loadRescue, RESCUE_EVERY_MS);
live.on('incidents', () => { // open incidents change the active danger zones: refresh the risk and the route
  const key = [...new Set(live.openIncidents().map((i) => i.zone))].sort().join('|');
  if (key === evac.openZonesKey) return;
  evac.openZonesKey = key;
  loadRisk();
  if (evac.start) calculateRoute();
});

if ($('evacuation')) (async () => {
  try {
    evac.plans = await api('/api/floorplans');
  } catch (err) {
    $('evacuation').innerHTML = `<p class="panel-body text-red" role="alert">${esc(errorMessage(err))}</p>`;
    return;
  }
  if (!evac.plans.length) {
    $('evacuation').innerHTML = `<p class="empty">No floor plan yet. Draw or upload one under <a href="/floor-plans">Edit plan</a> to see the building and the way out.</p>`;
    return;
  }
  $('evac-plan').innerHTML = evac.plans.map((p) => `<option value="${p.id}">${esc(p.building ? `${p.building} · ` : '')}${esc(p.name)} (floor ${p.floorNumber})</option>`).join('');
  $('evac-plan').classList.remove('hidden');
  choosePlan(evac.plans[0].id);
})();

// ================================================================ live event log
const ACTIVITY_DOT = { info: 'dot-info', success: 'dot-success', warning: 'dot-warning', critical: 'dot-critical', notify: 'dot-notify' };

let activityFilter = null; // a RegExp on the message, from the Activity Log's filter buttons

function renderActivity() {
  if (!$('activity')) return;
  const entries = activityFilter ? live.activity.filter((a) => activityFilter.test(a.message)) : live.activity;
  $('activity').innerHTML = entries.length ? entries.map((a) => `<li class="${a.level === 'critical' ? 'critical' : ''}">
    <time>${timeOf(a.timestamp)}</time><span class="dot ${ACTIVITY_DOT[a.level] ?? 'dot-info'}"></span>
    <span style="overflow-wrap:anywhere">${esc(a.message)}${a.zone ? `<span class="zone">${esc(a.zone)}</span>` : ''}</span></li>`).join('')
    : `<li class="empty" style="display:block">${live.activity.length ? 'Nothing of this kind yet.' : 'Waiting for events…'}</li>`;
}
$('clear-activity')?.addEventListener('click', () => { live.activity = []; renderActivity(); });
$('activity-filter')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  activityFilter = b.dataset.filter ? new RegExp(b.dataset.filter, 'i') : null;
  for (const x of $('activity-filter').children) x.setAttribute('aria-selected', String(x === b));
  renderActivity();
});
live.on('activity', renderActivity);

// ================================================================ system health (GET /api/system/health, polled by shell.js)
const HEALTH_TONE = { healthy: 'text-green', loading: 'text-amber', degraded: 'text-amber', offline: 'text-red', critical: 'text-red' };
const healthCell = (status, stale) => `<span class="strong ${stale ? 'muted' : HEALTH_TONE[status] ?? 'muted'}">${esc(status)}${stale ? ' (old)' : ''}</span>`;
let healthHtml = '';

function renderHealth() {
  const h = live.health;
  const failed = live.healthError;
  const stale = Boolean(h) && (Boolean(failed) || live.now - live.healthAt > HEALTH_STALE_MS); // never shown as current
  const components = { ...h?.components };
  if (h && live.state !== 'connected') { // the server only sees its own streams; this browser knows its own link
    components.liveUpdates = { ...components.liveUpdates, status: 'offline', message: `This browser's live link is ${live.state}. ${components.liveUpdates.message}` };
  }
  // No answer = Django (or its database) not answering: critical. An old answer without an error (e.g. a sleeping tab): unknown.
  const overall = failed ? 'critical' : !h ? null : stale ? 'unknown'
    : h.status === 'healthy' && components.liveUpdates.status !== 'healthy' ? 'degraded' : h.status;
  const overallEl = $('health-overall');
  if (overallEl) {
    overallEl.textContent = overall ? overall.toUpperCase() : 'CHECKING…';
    overallEl.className = `small strong ${HEALTH_TONE[overall] ?? 'muted'}`;
    overallEl.title = failed ? `Health check failed: ${failed}` : h?.message ?? '';
  }
  if ($('health-checked')) $('health-checked').textContent = !h ? (failed ? `Health check failed: ${failed}` : '')
    : `Last successful check ${timeOf(h.checkedAt)} (${relativeTime(new Date(live.healthAt).toISOString(), live.now)})${failed ? ` · latest check failed: ${failed}` : stale ? ' · out of date' : ''}`;
  const rows = [];
  for (const c of Object.values(components)) {
    rows.push(`<tr><td class="nowrap">${esc(c.name)}${c.optional ? ' <span class="muted">(optional)</span>' : ''}</td><td>${healthCell(c.status, stale)}</td><td>${esc(c.message)}</td></tr>`);
    for (const e of c.details?.cameras ?? c.details?.sensors ?? []) {
      rows.push(`<tr><td class="nowrap dim">↳ ${esc(e.name ?? `${e.type} ${e.id}`)}</td><td>${healthCell(e.state, stale)}</td><td>${esc(e.message)}</td></tr>`);
    }
  }
  const html = h ? `<table class="data"><thead><tr><th>Component</th><th>Status</th><th>Details</th></tr></thead><tbody>${rows.join('')}</tbody></table>`
    : `<p class="empty">${failed ? 'Health check failed.' : 'Checking system health…'}</p>`;
  if ($('health') && html !== healthHtml) $('health').innerHTML = healthHtml = html; // the clock re-renders every second: rebuild only on change
  const strip = failed && !h ? '<div class="hs hs-danger"><span class="hs-icon">×</span><div><div class="hs-name">Django</div><div class="hs-state">Server jawab nahi de raha</div></div></div>'
    : !h ? '<p class="empty">Checking system health…</p>'
      : Object.entries(components).map(([key, c]) => {
        const tone = stale ? 'muted' : c.status === 'healthy' ? 'ok' : c.status === 'offline' ? 'danger' : c.status === 'unknown' ? 'muted' : 'warn';
        const label = HEALTH_SPECIFIC[key]?.[c.status] ?? HEALTH_PLAIN[c.status] ?? c.status;
        return `<div class="hs hs-${tone}" title="${esc(c.message)}"><span class="hs-icon" aria-hidden="true">${{ ok: '✓', danger: '×', warn: '!', muted: '?' }[tone]}</span>
          <div><div class="hs-name">${esc(c.name)}${c.optional ? ' <span class="muted">(optional)</span>' : ''}</div><div class="hs-state">${esc(label)}${stale ? ' (purani khabar)' : ''}</div></div></div>`;
      }).join('');
  if ($('health-strip') && strip !== healthStrip) $('health-strip').innerHTML = healthStrip = strip;
}
let healthStrip = '';
$('health-details-toggle')?.addEventListener('click', (e) => {
  const open = $('health-details').classList.toggle('hidden') === false;
  e.currentTarget.setAttribute('aria-expanded', String(open));
  e.currentTarget.textContent = open ? 'Hide details' : 'Show details';
});

const HEALTH_PLAIN = { healthy: 'Healthy', loading: 'Shuru ho raha hai', degraded: 'Masla hai', offline: 'Band hai', unknown: 'Maloom nahi' };
const HEALTH_SPECIFIC = {
  database: { offline: 'Database Connection Failed' }, ai: { offline: 'AI Service Band Hai', loading: 'AI Models load ho rahe hain' },
  models: { offline: 'AI Model Load Nahi Hua', degraded: 'Kuch AI Models Load Nahi Hue', loading: 'Load ho rahe hain' },
  cameras: { offline: 'Camera Offline', degraded: 'Kuch Cameras Offline', loading: 'Cameras jur rahe hain' },
  sensors: { offline: 'Sensors Offline', degraded: 'Kuch Sensors Offline' }, liveUpdates: { offline: 'Live Updates Band Hain' },
};
for (const name of ['health', 'state', 'tick']) live.on(name, renderHealth);

renderActivity();
renderKpis();
