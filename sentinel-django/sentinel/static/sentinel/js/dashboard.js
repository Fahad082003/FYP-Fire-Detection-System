// The command-center dashboard panels (ports of the Angular dashboard components).
import { SEVERITY_RANK, api, confirmAsk, dateTimeOf, errorMessage, esc, relativeTime, timeOf, toast } from './core.js';
import { HISTORY_POINTS, live } from './shell.js';
import { OverlayRenderer, register, unregister } from './overlay.js';
import { PlanMap } from './planmap.js';

const $ = (id) => document.getElementById(id);

// ================================================================ video wall
const MJPEG_RETRY_MS = 5000;
const wall = { filter: 'all', heatmap: true, focused: null, tiles: new Map() };
let hlsLoading = null;

function loadHls() { // vendored hls.js (Apache-2.0), fetched only when an HLS feed exists
  hlsLoading ??= new Promise((resolve, reject) => {
    const s = Object.assign(document.createElement('script'), { src: '/static/sentinel/vendor/hls.min.js', onload: () => resolve(window.Hls), onerror: reject });
    document.head.append(s);
  });
  return hlsLoading;
}

class Tile {
  constructor(camera) {
    this.el = document.createElement('article');
    this.el.className = 'tile';
    this.el.tabIndex = 0;
    this.el.setAttribute('role', 'button');
    this.el.innerHTML = `<div class="stage"><canvas aria-hidden="true"></canvas><div class="tile-top"></div><div class="chips"></div></div>`;
    this.stage = this.el.firstElementChild;
    this.renderer = new OverlayRenderer(this.stage, this.stage.querySelector('canvas'), (stale) => { this.stale = stale; this.renderInfo(); });
    this.stale = true;
    register(this.renderer);
    const toggle = () => { wall.focused = wall.focused === this.camera.id ? null : this.camera.id; renderWall(); };
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
    const alert = !s ? '' : s.dangerCount > 0 || s.fireSeverity === 'Critical' || (s.fireDetected && s.humanCount > 0) ? 'critical' : s.fireDetected ? 'elevated' : '';
    this.el.classList.toggle('tile-critical', alert === 'critical');
    this.el.classList.toggle('tile-elevated', alert === 'elevated');
    this.stage.querySelector('.tile-top').innerHTML = `<span class="feed-type ${c.feedType === 'Drone' ? 'drone' : ''}">${esc(c.feedType.toUpperCase())}</span>
      <span class="tile-name">${esc(c.name)} <span>(${esc(c.locationZone)})</span></span>
      <span class="live-flag ${this.stale ? 'stale' : ''}"><span class="dot ${this.stale ? 'dot-slate' : 'dot-red blink'}" style="width:6px;height:6px"></span>${this.stale ? 'NO AI DATA' : 'LIVE'}</span>`;
    const model = s?.model;
    this.stage.querySelector('.chips').innerHTML = `<span class="chip">HUM <b class="hum">${s?.humanCount ?? 0}</b></span>
      <span class="chip ${(s?.dangerCount ?? 0) > 0 ? 'chip-danger' : ''}">DANGER <b>${s?.dangerCount ?? 0}</b></span>
      ${s?.simulated ? '<span class="chip" title="Producer marked this data as simulated">SIM DATA</span>' : ''}
      ${model ? `<span class="chip" title="Detections from AI model: ${esc(model)}">AI</span>` : ''}
      ${model?.includes(':missing') ? `<span class="chip text-amber" title="Model not loaded: ${esc(model)}">MODEL MISSING</span>` : ''}
      ${s?.smokeDetected ? '<span class="chip chip-fire">SMOKE</span>' : ''}
      <span class="chip ${s?.fireDetected ? 'chip-fire' : ''}" title="Fire severity = share of the image covered by fire boxes, not a calibrated physical size">FIRE <b>${s?.fireDetected ? esc(s.fireSeverity ?? 'YES') : '—'}</b></span>`;
  }

  destroy() {
    clearTimeout(this.retryTimer);
    this.hls?.destroy();
    unregister(this.renderer);
    this.renderer.destroy();
    this.el.remove();
  }
}

function renderWall() {
  const container = $('video-wall');
  if (!live.loaded) return;
  const visible = live.cameras.filter((c) => (wall.filter === 'all' || c.feedType === wall.filter) && (!wall.focused || c.id === wall.focused));
  for (const [id, tile] of wall.tiles) if (!live.cameras.some((c) => c.id === id)) { tile.destroy(); wall.tiles.delete(id); }
  if (!visible.length) {
    container.innerHTML = '<p class="empty">No camera feeds registered.</p>';
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
}

$('feed-filter').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  wall.filter = b.dataset.filter;
  for (const x of $('feed-filter').children) x.setAttribute('aria-selected', String(x === b));
  renderWall();
});
$('heatmap-toggle').addEventListener('change', (e) => { wall.heatmap = e.target.checked; renderWall(); });
live.on('detection', (f) => wall.tiles.get(f.cameraId)?.renderer.update(f));
live.on('stats', () => wall.tiles.forEach((t) => t.renderInfo()));
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
  const open = live.openIncidents();
  const critical = open.filter((i) => i.severity === 'Critical').length;
  const top = Math.max(-1, ...open.map((i) => SEVERITY_RANK[i.severity]));
  const stats = live.liveStats();
  const { humans, danger } = peopleCount(stats, live.cameras);
  const peak = (type) => live.sensors.filter((s) => s.type === type).reduce((best, s) => (!best || s.currentValue > best.currentValue ? s : best), null);
  const tone = (s) => (!s ? 'neutral' : s.state === 'Normal' ? 'ok' : s.state === 'Warning' ? 'warn' : 'danger');
  const temp = peak('Temperature'), smoke = peak('Smoke');
  const kpis = [
    ['Open Incidents', String(open.length), critical ? `${critical} critical` : open.length ? 'monitoring' : 'none open', top >= SEVERITY_RANK.High ? 'danger' : top >= 0 ? 'warn' : 'ok'],
    ['Humans Tracked', String(humans), `${danger} in danger · ${stats.length} live feed(s)`, danger > 0 ? 'danger' : humans > 0 ? 'ok' : 'neutral'],
    ['Peak Temperature', temp ? `${temp.currentValue.toFixed(1)}°C` : '—', temp ? `${temp.locationZone} · ${temp.id}` : 'no sensors', tone(temp)],
    ['Peak Smoke', smoke ? `${Math.round(smoke.currentValue)} ppm` : '—', smoke ? `${smoke.locationZone} · ${smoke.id}` : 'no sensors', tone(smoke)],
  ];
  $('kpis').innerHTML = kpis.map(([label, value, hint, t]) => `<article class="kpi kpi-${t}"><span class="kpi-label">${label}</span>
    <span class="kpi-value">${esc(value)}</span><span class="kpi-hint">${esc(hint)}</span></article>`).join('');
}
for (const name of ['incidents', 'stats', 'sensors', 'cameras', 'tick']) live.on(name, renderKpis);

// ================================================================ sensors and sprinklers
const STATE_COLORS = { Normal: '#10b981', Warning: '#f59e0b', Alarm: '#ef4444', Critical: '#ef4444' };
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

function renderSensors() {
  $('sensor-count').textContent = `${live.sensors.length} nodes`;
  if (!live.loaded) return;
  if (!live.sensors.length) { $('sensors').innerHTML = '<p class="empty">No sensors registered.</p>'; return; }
  $('sensors').innerHTML = `<div class="sensor-grid">${live.sensors.map((s) => {
    const color = STATE_COLORS[s.state];
    const value = s.type === 'Temperature' ? s.currentValue.toFixed(1) : String(Math.round(s.currentValue));
    const pct = Math.max(0, Math.min(100, (s.currentValue / (s.criticalThreshold * 1.1)) * 100)); // scale ends 10 % above critical
    return `<article class="sensor ${s.state}" aria-label="${esc(`${s.type} ${s.id} in ${s.locationZone}: ${value} ${s.unit}, ${s.state}`)}">
      <div class="gauge" style="--pct:${pct};--tone:${color}"><div class="gauge-ring"></div><div class="gauge-value"><b>${value}</b><span>${esc(s.unit)}</span></div></div>
      <div class="grow"><div class="sensor-head">${s.type}<span class="state-badge" style="color:${color};background:${color}22">${s.state}</span></div>
        <div class="tiny muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.locationZone)} · ${esc(s.id)}</div>
        <canvas class="sparkline" data-sensor="${esc(s.id)}" aria-hidden="true"></canvas>
        <div class="tiny muted">Limit <b class="mono" style="color:var(--text-2)">${s.threshold}${esc(s.unit)}</b></div></div></article>`;
  }).join('')}</div>`;
  for (const s of live.sensors) {
    const canvas = $('sensors').querySelector(`[data-sensor="${CSS.escape(s.id)}"]`);
    if (canvas) drawSparkline(canvas, s.history ?? [], s.threshold, STATE_COLORS[s.state]);
  }
}

const relayTime = (s) => (s.isActive ? `spraying · ${relativeTime(s.lastTriggeredAt, live.now)}` : `last ${relativeTime(s.lastTriggeredAt, live.now)}`);

function renderRelays() {
  const relays = live.sprinklers;
  const active = relays.filter((s) => s.isActive).length;
  $('active-relays').textContent = active;
  $('master-switch').checked = relays.length > 0 && active === relays.length;
  $('relays').innerHTML = relays.length ? relays.map((s) => {
    const busy = relayBusy.has(s.id);
    return `<li class="relay ${s.isActive ? 'on' : ''} ${busy ? 'busy' : ''}" data-relay="${esc(s.id)}">
      <div class="relay-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v5M8 8h8l-1 3H9zM7 15l-1 2M12 15v3M17 15l1 2M9.5 14l-.5 1M14.5 14l.5 1"/></svg></div>
      <div style="min-width:0"><div class="mono strong">${esc(s.id)}</div><div class="tiny muted">${esc(s.zone)} · <span data-rel>${relayTime(s)}</span></div></div>
      <div class="seg" role="group" aria-label="Control mode">${['Auto', 'Manual'].map((m) => `<button type="button" class="seg-btn" style="padding:4px 8px;font-size:11px"
        data-mode="${m}" aria-pressed="${s.mode === m}" ${busy ? 'disabled' : ''}>${m}</button>`).join('')}</div>
      <label class="switch-label"><input type="checkbox" role="switch" class="sr-only" data-toggle ${s.isActive ? 'checked' : ''} ${busy ? 'disabled' : ''}
        aria-label="${esc(s.id)} relay ${s.isActive ? 'on' : 'off'}"><span class="switch"></span></label></li>`;
  }).join('') : '<li class="empty">No sprinkler relays registered.</li>';
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

$('relays').addEventListener('click', (e) => {
  const mode = e.target.closest('[data-mode]');
  const id = e.target.closest('[data-relay]')?.dataset.relay;
  if (mode && id && mode.getAttribute('aria-pressed') !== 'true') sendRelay(id, { mode: mode.dataset.mode });
});
$('relays').addEventListener('change', (e) => {
  if (e.target.matches('[data-toggle]')) sendRelay(e.target.closest('[data-relay]').dataset.relay, { isActive: e.target.checked });
});
$('master-switch').addEventListener('change', async (e) => {
  const activate = e.target.checked;
  const ok = await confirmAsk(activate ? 'Activate ALL sprinklers?' : 'Deactivate all sprinklers?',
    activate ? 'Every relay in every zone will open and discharge water, including relays in Manual mode.'
      : 'All relays will close. Only do this once the area is confirmed safe.', activate ? 'Activate all' : 'Deactivate all');
  if (!ok) { renderRelays(); return; }
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
live.on('tick', () => $('relays').querySelectorAll('[data-relay]').forEach((li) => {
  const s = live.sprinklers.find((x) => x.id === li.dataset.relay);
  if (s) li.querySelector('[data-rel]').textContent = relayTime(s);
}));

// ================================================================ alert logs
const SOURCE_LABEL = { Vision: 'AI Vision', Sensor: 'Sensor', Manual: 'Manual', CameraLost: 'Camera lost' };
let incidentFilter = 'open';
const resolving = new Set();
const locationOf = (i) => (i.cameraName ? `${i.zone} (${i.cameraName})` : i.sensorId ? `${i.zone} (sensor ${i.sensorId})` : i.zone);

function renderIncidents() {
  $('open-count').textContent = live.openIncidents().length;
  const body = $('incident-rows');
  if (!live.loaded) {
    body.innerHTML = `<tr><td colspan="10" class="empty">${live.state === 'disconnected' ? 'Cannot reach the realtime server — retrying…' : 'Loading incidents…'}</td></tr>`;
    return;
  }
  const rows = incidentFilter === 'open' ? live.openIncidents() : live.incidents;
  body.innerHTML = rows.length ? rows.map((i) => {
    const critical = i.severity === 'Critical' && !i.isResolved;
    return `<tr class="${critical ? 'row-critical' : ''} ${i.isResolved ? 'row-resolved' : ''}">
      <td class="mono">${i.id}</td><td class="nowrap" title="${esc(dateTimeOf(i.timestamp))}">${timeOf(i.timestamp)}</td>
      <td><span class="sev sev-${i.severity}">${i.severity}</span></td>
      <td style="min-width:180px;color:var(--text-2)" title="${esc(i.message)}">${esc(locationOf(i))}</td>
      <td class="nowrap">${SOURCE_LABEL[i.source] ?? esc(i.source)}</td>
      <td class="mono">${i.fireDetected ? esc(i.fireSeverity ?? 'Yes') : '—'}</td>
      <td class="num">${i.humanCount}</td><td class="num ${i.dangerCount > 0 ? 'text-amber' : ''}">${i.dangerCount}</td>
      <td class="tiny strong ${i.isResolved ? 'text-green' : 'text-amber'}">${i.isResolved ? 'Resolved' : 'Open'}</td>
      <td>${i.isResolved ? '' : `<button type="button" class="btn btn-sm btn-success" data-resolve="${i.id}" ${resolving.has(i.id) ? 'disabled' : ''}>Resolve</button>`}</td></tr>`;
  }).join('') : `<tr><td colspan="10" class="empty">${incidentFilter === 'open' ? 'No open incidents.' : 'No incidents recorded.'}</td></tr>`;
}

$('incident-filter').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  incidentFilter = b.dataset.filter;
  for (const x of $('incident-filter').children) x.setAttribute('aria-selected', String(x === b));
  renderIncidents();
});
$('incident-rows').addEventListener('click', async (e) => {
  const id = Number(e.target.closest('[data-resolve]')?.dataset.resolve);
  if (!id) return;
  resolving.add(id);
  renderIncidents();
  try {
    live.upsertIncident(await api(`/api/incidents/${id}/resolve`, { method: 'POST' }));
  } catch (err) {
    toast('Could not resolve incident', errorMessage(err), 'error');
  } finally {
    resolving.delete(id);
    renderIncidents();
  }
});
live.on('incidents', renderIncidents);
live.on('state', renderIncidents);

// ================================================================ incident history
const CHART_W = 600, CHART_H = 140;
const history = { incidentId: null, type: '', logs: [], sensor: null, loading: false, error: null, request: 0 };

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
  Object.assign(history, { loading: true, sensor: null });
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
  renderHistory();
}

function renderHistory() {
  const select = $('history-incident');
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
    chart = `<div class="panel-body" style="border-bottom:1px solid var(--ink-700);padding:12px"><div class="tiny strong dim" style="margin-bottom:4px">${esc(label)}</div>
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
          <td class="nowrap">${l.type}${l.simulated ? '<span class="tag tag-sim">SIMULATED</span>' : ''}${l.type === 'EvacuationRoute' ? '<span class="tag tag-est">ESTIMATE</span>' : ''}</td>
          <td style="color:var(--text-2)">${esc(l.details)}</td></tr>`).join('')
          : '<tr><td colspan="4" class="empty">No history entries for this filter.</td></tr>'}</tbody></table>`;
}

$('history-incident').addEventListener('change', (e) => { history.incidentId = e.target.value ? Number(e.target.value) : null; loadHistory(); });
$('history-type').addEventListener('change', (e) => { history.type = e.target.value; loadHistory(); });
live.on('incidentLog', (l) => {
  if ((history.incidentId === null || l.incidentId === history.incidentId) && (!history.type || l.type === history.type)) {
    history.logs = [l, ...history.logs].slice(0, 500);
    renderHistory();
  }
});
live.on('incidents', () => {
  const select = $('history-incident');
  if (select.options.length !== live.incidents.length + 1) renderHistory();
});
loadHistory();

// ================================================================ evacuation route
const PEOPLE_EXPIRE_MS = 4500; // as the camera tile: a camera that sent nothing for this long no longer places people
const RISK_RGBA = ['', 'rgba(250,204,21,0.35)', 'rgba(249,115,22,0.5)', 'rgba(239,68,68,0.6)', 'rgba(185,28,28,0.85)'];
const round1 = (v) => Math.round(v * 10) / 10;
const evac = { plans: [], planId: null, imageUrl: null, start: null, result: null, routing: false, error: null, risk: null, seen: new Map(),
  people: [], openZonesKey: '', map: null };

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
  if (!plan) return;
  if (!evac.map) {
    box.innerHTML = '<div class="panel-body" style="padding:12px"><div data-map></div><div data-risk></div><div class="small" style="margin-top:12px" aria-live="polite" data-result></div><div data-safe></div></div>';
    evac.map = new PlanMap(box.querySelector('[data-map]'), onPlanPointer);
  }
  evac.map.set({ width: plan.widthMeters, height: plan.heightMeters, elements: plan.elements, imageUrl: evac.imageUrl, route: evac.result?.path ?? null,
    start: evac.start, people: evac.people, riskLayer: evac.riskLayerCache ?? null, activeDanger: evac.result?.activeDangerZones ?? [] });

  const rk = evac.risk;
  box.querySelector('[data-risk]').innerHTML = rk?.active ? `<div class="risk-box" role="status"><p class="strong text-orange">⚠ Fire risk: ${esc(rk.message)}</p>
    <ul>${rk.areas.filter((a) => a.level !== 'None').map((a) => `<li><span class="swatch risk-${a.level}"></span><b>${esc(a.name)}</b> ${a.level}${a.distanceMeters ? ` · ${a.distanceMeters} m` : ''}</li>`).join('')}</ul>
    <p class="muted" style="margin-top:6px">Rule-based estimate: distance from ${esc(rk.activeDangerZones.join(', '))} through rooms and doors (walls block it).
      High ≤ 5 m, Medium ≤ 10 m, Low ≤ 20 m. Not a fire-spread simulation.</p></div>` : '';

  const r = evac.result;
  let result;
  if (!evac.start) {
    result = `<p class="muted">Click the person's <b>known</b> position on the plan, or an <span class="text-amber">amber dot</span>: a person seen by a camera
      calibrated to this plan (Floor Plans → Camera calibration). The dot is an estimate from the camera.</p>`;
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

  const safe = plan.elements.filter((e) => e.type === 'SafeZone');
  box.querySelector('[data-safe]').innerHTML = `<h3 class="panel-title" style="margin-top:16px">Safe zones on this floor</h3>${safe.length
    ? `<ul class="tiny" style="margin-top:4px;color:var(--text-2)">${safe.map((z) => `<li class="row" style="padding:2px 0"><span class="swatch" style="border:1px solid #22c55e;background:rgb(34 197 94 / .25)"></span>
        <b>${esc(z.name || 'Safe zone')}</b> — x ${z.x} m, y ${z.y} m, ${z.width} × ${z.height} m</li>`).join('')}</ul>`
    : '<p class="tiny muted" style="margin-top:4px">No safe zone marked on this floor plan.</p>'}`;
}

function setImage(url) {
  if (evac.imageUrl) URL.revokeObjectURL(evac.imageUrl);
  evac.imageUrl = url;
}

async function choosePlan(id) {
  Object.assign(evac, { planId: id, start: null, result: null, routing: false, error: null, risk: null, riskLayerCache: null, people: [] });
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

function onPlanPointer(p) {
  if (p.kind !== 'down') return;
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
}

function refreshPeople() {
  const now = Date.now();
  for (const [id, v] of evac.seen) if (now - v.at > PEOPLE_EXPIRE_MS) evac.seen.delete(id);
  const people = [...evac.seen.values()].flatMap((v) => v.points);
  if (people.length || evac.people.length) { evac.people = people; if (evac.map) evac.map.set({ people }); }
}

$('evac-plan').addEventListener('change', (e) => choosePlan(Number(e.target.value)));
live.on('detection', placePeople);
live.on('tick', refreshPeople);
live.on('incidents', () => { // open incidents change the active danger zones: refresh the risk and the route
  const key = [...new Set(live.openIncidents().map((i) => i.zone))].sort().join('|');
  if (key === evac.openZonesKey) return;
  evac.openZonesKey = key;
  loadRisk();
  if (evac.start) calculateRoute();
});

(async () => {
  try {
    evac.plans = await api('/api/floorplans');
  } catch (err) {
    $('evacuation').innerHTML = `<p class="panel-body text-red" role="alert">${esc(errorMessage(err))}</p>`;
    return;
  }
  if (!evac.plans.length) {
    $('evacuation').innerHTML = `<p class="empty">No floor plan yet. Draw or upload one under <a class="text-cyan" href="/floor-plans">Floor Plans</a> to calculate evacuation routes.</p>`;
    return;
  }
  $('evac-plan').innerHTML = evac.plans.map((p) => `<option value="${p.id}">${esc(p.building ? `${p.building} · ` : '')}${esc(p.name)} (floor ${p.floorNumber})</option>`).join('');
  $('evac-plan').classList.remove('hidden');
  choosePlan(evac.plans[0].id);
})();

// ================================================================ live event log
const ACTIVITY_DOT = { info: 'dot-info', success: 'dot-success', warning: 'dot-warning', critical: 'dot-critical', notify: 'dot-notify' };

function renderActivity() {
  $('activity').innerHTML = live.activity.length ? live.activity.map((a) => `<li class="${a.level === 'critical' ? 'critical' : ''}">
    <time>${timeOf(a.timestamp)}</time><span class="dot ${ACTIVITY_DOT[a.level] ?? 'dot-info'}"></span>
    <span style="overflow-wrap:anywhere">${esc(a.message)}${a.zone ? `<span class="zone">${esc(a.zone)}</span>` : ''}</span></li>`).join('')
    : '<li class="empty" style="display:block">Waiting for events…</li>';
}
$('clear-activity').addEventListener('click', () => { live.activity = []; renderActivity(); });
live.on('activity', renderActivity);

renderActivity();
renderKpis();
