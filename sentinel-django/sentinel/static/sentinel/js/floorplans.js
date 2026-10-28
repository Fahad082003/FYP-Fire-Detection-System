// Floor-plan manager (port of floor-plans.component.ts): list, create and delete plans, upload a reference image, draw and edit
// rooms, corridors, walls, doors, exits, safe and danger zones, and calibrate cameras. Saving sends the whole element list.
import { api, confirmAsk, errorMessage, esc, toast } from './core.js';
import { live } from './shell.js';
import { PlanMap } from './planmap.js';

const $ = (id) => document.getElementById(id);
const TYPES = { Room: 'Room', Corridor: 'Corridor', Wall: 'Wall', Door: 'Door', Exit: 'Exit', SafeZone: 'Safe zone', DangerZone: 'Danger zone' };
const SNAP = 0.1, MIN_SIZE = 0.2, MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const THICKNESS = { Wall: 0.2, Door: 0.4 }; // walls and doors are drawn as lines; doors overlap the 0.2 m wall
const snap = (v) => Math.round(v / SNAP) * SNAP;
const round = (v) => Math.round(v * 100) / 100;

/** Rectangle dragged from a to b, snapped, clamped to the plan; walls and doors become thin bars along the drag direction. */
export function drawnRect(type, a, b, w, h) {
  const clamp = (v, max) => Math.min(max, Math.max(0, snap(v)));
  let x = Math.min(clamp(a.x, w), clamp(b.x, w)), y = Math.min(clamp(a.y, h), clamp(b.y, h));
  let width = Math.abs(clamp(b.x, w) - clamp(a.x, w)), height = Math.abs(clamp(b.y, h) - clamp(a.y, h));
  const t = THICKNESS[type];
  if (t) {
    if (width >= height) { height = t; y = Math.min(h - t, Math.max(0, clamp(a.y, h) - t / 2)); }
    else { width = t; x = Math.min(w - t, Math.max(0, clamp(a.x, w) - t / 2)); }
  }
  if (width < MIN_SIZE || height < MIN_SIZE) return null;
  return { x: round(x), y: round(y), width: round(width), height: round(height) };
}

const ed = { plans: [], current: null, elements: [], selected: null, tool: 'select', preview: null, imageUrl: null, dirty: false, busy: false, drag: null,
  calib: { camera: null, image: [], plan: [], pick: null } };
const form = $('plan-form');
const map = new PlanMap($('editor-map'), onPointer);

const planWidth = () => Math.max(1, Number(form.elements.widthMeters.value) || 1);
const planHeight = () => Math.max(1, Number(form.elements.heightMeters.value) || 1);

function setDirty(dirty) {
  ed.dirty = dirty;
  $('save-plan').disabled = ed.busy || !dirty;
  $('editor-title').innerHTML = `${esc(ed.current?.name ?? 'Editor')}${dirty ? ' <span class="text-amber" style="text-transform:none;letter-spacing:0">· unsaved</span>' : ''}`;
  renderCalibration();
}

function renderMap() {
  map.set({ width: planWidth(), height: planHeight(), elements: ed.elements, imageUrl: ed.imageUrl, selected: ed.selected, preview: ed.preview, panEnabled: ed.tool === 'pan' });
}

function renderList() {
  const box = $('plan-list');
  if (!ed.plans.length) { box.innerHTML = '<p class="empty">No floor plans yet. Click <b>+ New</b> to start.</p>'; return; }
  const groups = new Map();
  for (const p of ed.plans) groups.set(p.building || 'No building', [...(groups.get(p.building || 'No building') ?? []), p]);
  box.innerHTML = [...groups].map(([building, plans]) => `<div class="group-title">${esc(building)}</div><ul class="plan-list" style="padding:6px">${
    plans.sort((a, b) => a.floorNumber - b.floorNumber).map((p) => `<li><button type="button" data-plan="${p.id}" class="${p.id === ed.current?.id ? 'current' : ''}">
      <div class="strong">${esc(p.name)}</div><div class="tiny muted">Floor ${p.floorNumber} · ${p.widthMeters}×${p.heightMeters} m · ${p.elements.length} elements${p.hasImage ? ' · image' : ''}</div>
      </button></li>`).join('')}</ul>`).join('');
}

function renderElement() {
  const panel = $('element-panel');
  const e = ed.selected === null ? null : ed.elements[ed.selected];
  if (!e) {
    panel.innerHTML = '<h3 class="panel-title">Selected element</h3><p class="small muted">Nothing selected. Use <b>Select / Move</b> and click an element.</p>';
    return;
  }
  const num = (field, label) => `<label class="field"><span>${label}</span><input class="input" type="number" step="0.1" data-num="${field}" value="${e[field]}"></label>`;
  panel.innerHTML = `<h3 class="panel-title">Selected element</h3><div class="strong small">${TYPES[e.type]}</div>
    <label class="field"><span>Name</span><input class="input" data-text="name" value="${esc(e.name)}" maxlength="80"></label>
    <div class="grid-2">${num('x', 'X (m)')}${num('y', 'Y (m)')}${num('width', 'Width (m)')}${num('height', 'Height (m)')}</div>
    ${e.type === 'Door' || e.type === 'Exit' ? `<label class="row small"><input type="checkbox" data-check="isBlocked" ${e.isBlocked ? 'checked' : ''}> Blocked / closed</label>` : ''}
    ${e.type === 'DangerZone' ? `<label class="row small"><input type="checkbox" data-check="isActive" ${e.isActive ? 'checked' : ''}> Active now (manual)</label>
      <label class="field"><span>Linked camera/sensor zone</span><input class="input" list="plan-zone-list" data-text="zone" value="${esc(e.zone ?? '')}"
        placeholder="e.g. Ground Floor - Warehouse" maxlength="64"><datalist id="plan-zone-list">${live.zones().map((z) => `<option value="${esc(z)}"></option>`).join('')}</datalist></label>
      <p class="tiny muted">Becomes active automatically while that zone has an open incident.</p>` : ''}
    <button type="button" class="btn btn-sm btn-ghost btn-text-red" data-delete-element>Delete element</button>`;
}

function render() {
  const editing = !!ed.current;
  document.querySelectorAll('.editing-only').forEach((el) => el.classList.toggle('hidden', !editing));
  $('no-plan').classList.toggle('hidden', editing);
  for (const b of $('tools').children) b.classList.toggle('active', b.dataset.tool === ed.tool);
  renderList();
  if (editing) { renderMap(); renderElement(); }
  setDirty(ed.dirty);
}

function update(patch) {
  if (ed.selected === null) return;
  ed.elements = ed.elements.map((e, k) => (k === ed.selected ? { ...e, ...patch } : e));
  setDirty(true);
  renderMap();
}

function setImage(url) {
  if (ed.imageUrl) URL.revokeObjectURL(ed.imageUrl);
  ed.imageUrl = url;
}

async function open(plan, reloadImage) {
  if (ed.current?.id !== plan.id) { // another plan: never keep the previous plan's calibration points
    ed.calib = { camera: null, image: [], plan: [], pick: null };
    $('calib-camera').value = '';
  }
  ed.current = plan;
  for (const k of ['name', 'building', 'floorNumber', 'widthMeters', 'heightMeters']) form.elements[k].value = plan[k];
  ed.elements = plan.elements.map((e) => ({ ...e }));
  ed.selected = null;
  ed.dirty = false;
  if (reloadImage) setImage(null);
  render();
  if (!reloadImage || !plan.hasImage) return;
  try {
    const blob = await api(`/api/floorplans/${plan.id}/image`);
    if (ed.current?.id === plan.id) { setImage(URL.createObjectURL(blob)); renderMap(); } // drop a late image of a plan already left
  } catch { /* the plan still works without its background */ }
}

async function load(openId) {
  try {
    ed.plans = await api('/api/floorplans');
  } catch (err) {
    $('plan-list').innerHTML = `<p class="panel-body small text-red" role="alert">${esc(errorMessage(err))} <button type="button" class="btn btn-sm" data-retry>Retry</button></p>`;
    return;
  }
  const target = ed.plans.find((p) => p.id === (openId ?? ed.current?.id)) ?? ed.plans[0];
  if (target) open(target, true); else { ed.current = null; render(); }
}

const discardOk = async () => !ed.dirty || confirmAsk('Discard unsaved changes?', `Changes to "${ed.current?.name}" are not saved.`, 'Discard');

$('plan-list').addEventListener('click', async (e) => {
  if (e.target.closest('[data-retry]')) return load();
  const id = Number(e.target.closest('[data-plan]')?.dataset.plan);
  if (!id || id === ed.current?.id || !(await discardOk())) return;
  open(ed.plans.find((p) => p.id === id), true);
});

$('new-plan').addEventListener('click', async () => {
  if (!(await discardOk())) return;
  try {
    const p = await api('/api/floorplans', { method: 'POST', body: { name: 'New floor plan', building: '', floorNumber: 0, widthMeters: 30, heightMeters: 20, elements: [] } });
    toast('Floor plan created', p.name, 'success', 3000);
    load(p.id);
  } catch (err) {
    toast('Could not create floor plan', errorMessage(err), 'error');
  }
});

$('save-plan').addEventListener('click', async () => {
  const f = form.elements;
  const nameOk = f.name.value.trim().length > 0, sizeOk = [f.widthMeters, f.heightMeters].every((x) => Number(x.value) >= 1 && Number(x.value) <= 1000);
  form.querySelector('[data-error="name"]').classList.toggle('hidden', nameOk);
  form.querySelector('[data-error="size"]').classList.toggle('hidden', sizeOk);
  if (!nameOk || !sizeOk || !ed.current) return;
  ed.busy = true;
  $('save-plan').textContent = 'Saving…';
  setDirty(ed.dirty);
  try {
    const saved = await api(`/api/floorplans/${ed.current.id}`, { method: 'PUT', body: {
      name: f.name.value, building: f.building.value, floorNumber: Number(f.floorNumber.value), widthMeters: Number(f.widthMeters.value),
      heightMeters: Number(f.heightMeters.value), elements: ed.elements } });
    ed.plans = ed.plans.map((p) => (p.id === saved.id ? saved : p));
    open(saved, false);
    toast('Floor plan saved', `${saved.elements.length} element(s)`, 'success', 3000);
  } catch (err) {
    toast('Save failed', errorMessage(err), 'error');
  } finally {
    ed.busy = false;
    $('save-plan').textContent = 'Save';
    setDirty(ed.dirty);
  }
});

$('delete-plan').addEventListener('click', async () => {
  const plan = ed.current;
  if (!plan || !(await confirmAsk(`Delete "${plan.name}"?`, 'The floor plan, its elements and its image are deleted permanently.', 'Delete'))) return;
  try {
    await api(`/api/floorplans/${plan.id}`, { method: 'DELETE' });
    ed.current = null;
    toast('Floor plan deleted', plan.name, 'success', 3000);
    load();
  } catch (err) {
    toast('Delete failed', errorMessage(err), 'error');
  }
});

$('plan-image').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  const plan = ed.current;
  if (!file || !plan) return;
  if (!['image/png', 'image/jpeg'].includes(file.type)) return toast('Unsupported file', 'Choose a PNG or JPG image.', 'warning');
  if (file.size > MAX_IMAGE_BYTES) return toast('Image too large', 'Maximum size is 5 MB.', 'warning');
  const body = new FormData();
  body.append('file', file);
  try {
    await api(`/api/floorplans/${plan.id}/image`, { method: 'PUT', body });
    plan.hasImage = true;
    setImage(URL.createObjectURL(file));
    render();
    toast('Image uploaded', 'Trace walls, rooms and exits on top of it. The image itself is not analysed.', 'success');
  } catch (err) {
    toast('Upload failed', errorMessage(err), 'error');
  }
});

form.addEventListener('input', () => { setDirty(true); renderMap(); });
form.addEventListener('submit', (e) => e.preventDefault());

$('tools').addEventListener('click', (e) => {
  const tool = e.target.closest('[data-tool]')?.dataset.tool;
  if (!tool) return;
  ed.tool = tool;
  render();
});

$('element-panel').addEventListener('change', (e) => {
  const t = e.target;
  if (t.dataset.num) {
    const v = Number(t.value);
    if (Number.isFinite(v)) update({ [t.dataset.num]: round(Math.max(t.dataset.num === 'width' || t.dataset.num === 'height' ? MIN_SIZE : 0, v)) });
  } else if (t.dataset.text) {
    update(t.dataset.text === 'zone' ? { zone: t.value.trim() || null } : { name: t.value });
  } else if (t.dataset.check) {
    update({ [t.dataset.check]: t.checked });
  }
});
$('element-panel').addEventListener('click', (e) => { if (e.target.closest('[data-delete-element]')) deleteSelected(); });

function deleteSelected() {
  if (ed.selected === null) return;
  ed.elements = ed.elements.filter((_, k) => k !== ed.selected);
  ed.selected = null;
  setDirty(true);
  renderMap();
  renderElement();
}

window.addEventListener('keydown', (e) => {
  const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement;
  if (typing || !ed.current) return;
  if (e.key === 'Delete' || e.key === 'Backspace') deleteSelected();
  if (e.key === 'Escape') { ed.selected = null; ed.tool = 'select'; render(); }
});

// ---------------------------------------------------------------- drawing and editing on the plan
function onPointer(p) {
  if (ed.calib.pick !== null) {
    if (p.kind === 'down') { ed.calib.plan[ed.calib.pick] = [round(p.x), round(p.y)]; ed.calib.pick = null; renderCalibration(); }
    return;
  }
  // The cyan corner resizes with any tool: right after drawing, the draw tool is still active.
  if (ed.tool === 'select' || (p.kind === 'down' && p.handle) || ed.drag?.mode === 'resize') return onSelectPointer(p);
  if (ed.tool === 'pan') return;
  if (p.kind === 'down') { ed.drag = { mode: 'draw', start: p }; return; }
  if (ed.drag?.mode !== 'draw') return;
  const rect = drawnRect(ed.tool, ed.drag.start, p, planWidth(), planHeight());
  if (p.kind === 'move') { ed.preview = rect; renderMap(); return; }
  ed.drag = null;
  ed.preview = null;
  if (p.kind === 'up' && rect) {
    const count = ed.elements.filter((e) => e.type === ed.tool).length + 1;
    ed.elements = [...ed.elements, { type: ed.tool, name: ed.tool === 'Wall' ? '' : `${TYPES[ed.tool]} ${count}`, ...rect, isBlocked: false, isActive: false, zone: null }];
    ed.selected = ed.elements.length - 1;
    setDirty(true);
    renderElement();
  }
  renderMap();
}

function onSelectPointer(p) {
  if (p.kind === 'down') {
    if (p.handle && ed.selected !== null) { ed.drag = { mode: 'resize' }; return; }
    ed.selected = p.index;
    const e = p.index === null ? null : ed.elements[p.index];
    ed.drag = e ? { mode: 'move', offset: { x: p.x - e.x, y: p.y - e.y } } : null;
    renderMap();
    renderElement();
    return;
  }
  if (!ed.drag || ed.selected === null) return;
  if (p.kind === 'up') { ed.drag = null; renderElement(); return; }
  const e = ed.elements[ed.selected];
  if (ed.drag.mode === 'move') {
    update({ x: round(Math.min(planWidth() - e.width, Math.max(0, snap(p.x - ed.drag.offset.x)))),
      y: round(Math.min(planHeight() - e.height, Math.max(0, snap(p.y - ed.drag.offset.y)))) });
  } else {
    update({ width: round(Math.max(MIN_SIZE, Math.min(planWidth() - e.x, snap(p.x - e.x)))),
      height: round(Math.max(MIN_SIZE, Math.min(planHeight() - e.y, snap(p.y - e.y)))) });
  }
}

// ---------------------------------------------------------------- camera calibration: 4 floor points in the frame (px) and on the plan (m)
function renderCameraChoices() {
  const select = $('calib-camera');
  const chosen = ed.calib.camera?.id ?? '';
  select.innerHTML = '<option value="">Choose a camera…</option>' + live.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name)} (${esc(c.id)})${
    c.calibration ? (c.calibration.floorPlanId === ed.current?.id ? ' · calibrated here' : ' · calibrated to another plan') : ''}</option>`).join('');
  select.value = chosen;
}

function renderCalibration() {
  const box = $('calib-rows');
  const { camera, image, plan, pick } = ed.calib;
  if (!camera) { box.innerHTML = ''; return; }
  const input = (kind, row, axis, value, step) => `<input class="input" type="number" min="0" ${step ? `step="${step}"` : ''} value="${value}" data-calib="${kind}"
    data-row="${row}" data-axis="${axis}" aria-label="Point ${row + 1} ${kind === 'image' ? 'camera' : 'plan'} ${axis ? 'y' : 'x'}">`;
  box.innerHTML = `<ol class="stack" style="gap:8px">${[0, 1, 2, 3].map((row) => `<li class="calib-row"><span class="dim">${row + 1} · Camera px</span>
      ${input('image', row, 0, image[row][0])}${input('image', row, 1, image[row][1])}
      <button type="button" class="btn btn-sm ${pick === row ? 'btn-success' : ''}" style="padding:0 6px" data-pick="${row}" aria-label="Pick point ${row + 1} on the plan">Plan m ⌖</button>
      ${input('plan', row, 0, plan[row][0], 0.1)}${input('plan', row, 1, plan[row][1], 0.1)}</li>`).join('')}</ol>
    ${pick !== null ? `<p class="tiny text-cyan">Click point ${pick + 1} on the plan.</p>` : ''}
    <div class="row"><button type="button" class="btn btn-sm btn-success" data-calib-save ${ed.dirty ? 'disabled' : ''}>Save calibration</button>
      ${camera.calibration ? '<button type="button" class="btn btn-sm btn-ghost btn-text-red" data-calib-clear>Remove</button>' : ''}</div>
    ${ed.dirty ? '<p class="tiny text-amber">Save the plan first.</p>' : ''}`;
}

$('calib-camera').addEventListener('change', (e) => {
  const camera = live.cameras.find((c) => c.id === e.target.value) ?? null;
  const cal = camera?.calibration?.floorPlanId === ed.current?.id ? camera.calibration : null;
  const zeros = () => [[0, 0], [0, 0], [0, 0], [0, 0]];
  ed.calib = { camera, image: cal ? cal.imagePoints.map((p) => [...p]) : zeros(), plan: cal ? cal.planPoints.map((p) => [...p]) : zeros(), pick: null };
  renderCalibration();
});
$('calib-rows').addEventListener('change', (e) => {
  const t = e.target.closest('[data-calib]');
  if (t) ed.calib[t.dataset.calib][Number(t.dataset.row)][Number(t.dataset.axis)] = Number(t.value);
});
$('calib-rows').addEventListener('click', async (e) => {
  const pick = e.target.closest('[data-pick]');
  if (pick) { const row = Number(pick.dataset.pick); ed.calib.pick = ed.calib.pick === row ? null : row; renderCalibration(); return; }
  const { camera } = ed.calib;
  const plan = ed.current;
  if (!camera || !plan) return;
  const url = `/api/cameras/${encodeURIComponent(camera.id)}/calibration`;
  try {
    if (e.target.closest('[data-calib-save]')) {
      const saved = await api(url, { method: 'PUT', body: { floorPlanId: plan.id, imagePoints: ed.calib.image, planPoints: ed.calib.plan } });
      calibrated(saved);
      toast('Camera calibrated', `${saved.name} now places people on "${plan.name}".`, 'success', 3000);
    } else if (e.target.closest('[data-calib-clear]')) {
      calibrated(await api(url, { method: 'DELETE' }));
      toast('Calibration removed', camera.name, 'success', 3000);
    }
  } catch (err) {
    toast('Calibration not saved', errorMessage(err), 'error');
  }
});

// shortcut: only this browser's camera list is updated; other open dashboards see the change after their next snapshot (reconnect or reload).
function calibrated(camera) {
  live.updateCamera(camera);
  ed.calib.camera = camera;
  renderCameraChoices();
  renderCalibration();
}

live.on('cameras', renderCameraChoices);
load();
