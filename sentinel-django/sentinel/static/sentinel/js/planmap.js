// SVG floor-plan renderer shared by the editor and the dashboard evacuation panel (port of floor-plan-map.component.ts).
// Units are meters, origin top-left. Pointer events are reported in plan meters with the element under the pointer.
import { esc } from './core.js';

const STYLE = {
  Room: { fill: '#172131', stroke: '#263246', label: '#e6ebf2' }, // the design system's floor-plan colours
  Corridor: { fill: '#141e2f', stroke: '#263246', dash: '4 3', label: '#b7c1d1' },
  Wall: { fill: '#4e5a6e', stroke: '#4e5a6e' },
  Door: { fill: '#eac54f', stroke: '#eac54f' },
  Exit: { fill: '#0e2e22', stroke: '#12b76a', label: '#5fd69a' },
  SafeZone: { fill: 'rgba(18,183,106,0.12)', stroke: '#12b76a', label: '#5fd69a' },
  DangerZone: { fill: 'rgba(240,68,56,0.08)', stroke: '#f04438', dash: '5 3', label: '#ff8a80' },
  Sensor: { fill: 'rgba(14,165,233,0.25)', stroke: '#38bdf8' },
};
const DRAW_ORDER = ['Room', 'Corridor', 'SafeZone', 'DangerZone', 'Wall', 'Door', 'Exit', 'Sensor'];
const CAMERA_TONE = { ok: '#10b981', warn: '#f59e0b', danger: '#ef4444', off: '#64748b' };
// People per room (purple, so it never reads as the red/orange fire-risk layer); stale and unknown rooms are never filled as 0.
const peopleFill = (n) => `rgba(168,85,247,${Math.min(0.6, 0.12 + 0.12 * n).toFixed(2)})`;
let nextUid = 0;

export class PlanMap {
  /** onPointer({ kind: 'down'|'move'|'up', x, y, index, handle }) in plan meters. */
  constructor(container, onPointer) {
    this.uid = `fp${++nextUid}`;
    this.onPointer = onPointer;
    this.props = { width: 1, height: 1, elements: [], imageUrl: null, riskLayer: null, route: null, start: null, people: [], selected: null,
      preview: null, activeDanger: [], panEnabled: false, cameras: [], // cameras: [{ id, name, points (4 plan points), tone }]
      occupancy: null, sensorStatus: null }; // occupancy: GET …/occupancy rooms; sensorStatus: { sensor id: { tone, text } } (view only)
    this.view = { x: 0, y: 0, w: 1, h: 1 };
    this.panFrom = null;
    container.innerHTML = `<div class="map"><svg preserveAspectRatio="xMidYMid meet" role="img" aria-label="Floor plan"></svg>
      <div class="map-zoom"><button type="button" class="btn btn-sm" data-zoom="0.8" title="Zoom in">+</button>
      <button type="button" class="btn btn-sm" data-zoom="1.25" title="Zoom out">−</button>
      <button type="button" class="btn btn-sm" data-fit title="Fit to screen">Fit</button></div></div>`;
    this.root = container.firstElementChild;
    this.svg = this.root.querySelector('svg');
    this.root.querySelector('.map-zoom').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b?.dataset.zoom) this.zoom(Number(b.dataset.zoom));
      if (b?.hasAttribute('data-fit')) this.fit();
    });
    this.svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = this.toPlan(e);
      this.zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, p.x, p.y);
    }, { passive: false });
    this.svg.addEventListener('pointerdown', (e) => {
      this.svg.setPointerCapture(e.pointerId);
      if (this.props.panEnabled || e.button === 1) { this.panFrom = { cx: e.clientX, cy: e.clientY, vx: this.view.x, vy: this.view.y }; return; }
      this.emit('down', e);
    });
    this.svg.addEventListener('pointermove', (e) => {
      if (this.panFrom) {
        const scale = this.view.w / this.svg.clientWidth;
        this.view = { ...this.view, x: this.panFrom.vx - (e.clientX - this.panFrom.cx) * scale, y: this.panFrom.vy - (e.clientY - this.panFrom.cy) * scale };
        return this.applyView();
      }
      this.emit('move', e);
    });
    this.svg.addEventListener('pointerup', (e) => {
      if (this.panFrom) { this.panFrom = null; return; }
      this.emit('up', e);
    });
  }

  /** Update any props and redraw. The view is refitted when the plan size changes. */
  set(props) {
    const resized = (props.width ?? this.props.width) !== this.props.width || (props.height ?? this.props.height) !== this.props.height;
    Object.assign(this.props, props);
    this.root.classList.toggle('pan', this.props.panEnabled);
    if (resized || !this.drawn) this.fit(); else this.render();
    this.drawn = true;
  }

  fit() {
    const { width, height } = this.props;
    const pad = Math.max(width, height) * 0.03;
    this.view = { x: -pad, y: -pad, w: width + 2 * pad, h: height + 2 * pad };
    this.render();
  }

  zoom(factor, cx, cy) {
    const v = this.view;
    const w = Math.min(Math.max(this.props.width, this.props.height) * 4, Math.max(1, v.w * factor));
    const f = w / v.w;
    const x0 = cx ?? v.x + v.w / 2, y0 = cy ?? v.y + v.h / 2;
    this.view = { x: x0 - (x0 - v.x) * f, y: y0 - (y0 - v.y) * f, w, h: v.h * f };
    this.render();
  }

  applyView() {
    const v = this.view;
    this.svg.setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
  }

  render() {
    this.applyView();
    const p = this.props;
    const label = Math.max(p.width, p.height) / 60;
    const handle = this.view.w / 60;
    const isBlockedOpening = (e) => e.isBlocked && (e.type === 'Door' || e.type === 'Exit');
    const fill = (e) => (e.type === 'DangerZone' && (e.isActive || p.activeDanger.includes(e.name?.trim() ? e.name : 'Danger zone')) ? '#3a1416'
      : isBlockedOpening(e) ? 'rgba(240,68,56,0.45)' : STYLE[e.type].fill);
    const sorted = p.elements.map((e, i) => ({ e, i })).sort((a, b) => DRAW_ORDER.indexOf(a.e.type) - DRAW_ORDER.indexOf(b.e.type));
    const parts = [`<defs><pattern id="${this.uid}-grid" width="1" height="1" patternUnits="userSpaceOnUse">
        <path d="M 1 0 L 0 0 0 1" fill="none" stroke="#1c2636" stroke-width="0.03"/></pattern>
      <marker id="${this.uid}-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#22c55e"/></marker>
      <pattern id="${this.uid}-hatch" width="0.6" height="0.6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="0.6" height="0.6" fill="rgba(148,163,184,0.08)"/><path d="M 0 0 L 0 0.6" stroke="rgba(148,163,184,0.35)" stroke-width="0.12"/></pattern></defs>
      <rect width="${p.width}" height="${p.height}" fill="#0d1422"/>`];
    if (p.imageUrl) parts.push(`<image href="${esc(p.imageUrl)}" x="0" y="0" width="${p.width}" height="${p.height}" preserveAspectRatio="none" opacity="0.35"/>`);
    parts.push(`<rect width="${p.width}" height="${p.height}" fill="url(#${this.uid}-grid)" stroke="#263246" stroke-width="1" vector-effect="non-scaling-stroke"/>`);
    for (const { e, i } of sorted) {
      const s = STYLE[e.type];
      if (e.type === 'Sensor') continue; // drawn above the people and fire-risk layers
      parts.push(`<g data-index="${i}" style="cursor:pointer"><rect x="${e.x}" y="${e.y}" width="${e.width}" height="${e.height}" fill="${fill(e)}"
        stroke="${isBlockedOpening(e) ? '#ef4444' : s.stroke}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''} stroke-width="1.5" vector-effect="non-scaling-stroke"/>`);
      if (isBlockedOpening(e)) {
        parts.push(`<path d="M ${e.x} ${e.y} L ${e.x + e.width} ${e.y + e.height} M ${e.x + e.width} ${e.y} L ${e.x} ${e.y + e.height}"
          stroke="#ef4444" stroke-width="2" vector-effect="non-scaling-stroke"/>`);
      }
      if (e.name && e.type !== 'Wall') {
        parts.push(`<text x="${e.x + e.width / 2}" y="${e.y + e.height / 2}" font-size="${label}" text-anchor="middle" dominant-baseline="middle"
          fill="${s.label ?? s.stroke}" font-weight="600" pointer-events="none">${esc(e.type === 'Exit' ? `EXIT ${e.name}` : e.name)}</text>`);
      }
      parts.push('</g>');
    }
    for (const r of p.occupancy ?? []) parts.push(this.roomCount(r, label));
    if (p.riskLayer) parts.push(`<image href="${p.riskLayer.url}" x="0" y="0" width="${p.riskLayer.width}" height="${p.riskLayer.height}" preserveAspectRatio="none" pointer-events="none"/>`);
    for (const { e, i } of sorted) if (e.type === 'Sensor') parts.push(this.sensorMark(e, i, label));
    const sel = p.selected === null ? null : p.elements[p.selected];
    if (sel) {
      parts.push(`<rect x="${sel.x}" y="${sel.y}" width="${sel.width}" height="${sel.height}" fill="none" stroke="#22d3ee" stroke-width="2" vector-effect="non-scaling-stroke" pointer-events="none"/>
        <rect data-handle="1" x="${sel.x + sel.width - handle / 2}" y="${sel.y + sel.height - handle / 2}" width="${handle}" height="${handle}" fill="#22d3ee" style="cursor:nwse-resize"/>`);
    }
    if (p.preview) {
      const r = p.preview;
      parts.push(`<rect x="${r.x}" y="${r.y}" width="${r.width}" height="${r.height}" fill="rgba(34,211,238,0.12)" stroke="#22d3ee" stroke-dasharray="4 3"
        stroke-width="1.5" vector-effect="non-scaling-stroke" pointer-events="none"/>`);
    }
    if (p.route?.length) {
      parts.push(`<polyline points="${p.route.map((q) => q.join(',')).join(' ')}" fill="none" stroke="#22c55e" stroke-width="${label * 0.6}"
        stroke-linejoin="round" stroke-linecap="round" marker-end="url(#${this.uid}-arrow)" pointer-events="none"/>`);
    }
    for (const c of p.cameras) { // the floor area a calibrated camera sees (its calibration points), and a mark to click
      const [cx, cy] = [0, 1].map((k) => c.points.reduce((sum, q) => sum + q[k], 0) / c.points.length);
      parts.push(`<polygon points="${c.points.map((q) => q.join(',')).join(' ')}" fill="rgba(59,130,246,0.08)" stroke="#3b82f6" stroke-dasharray="3 3"
        stroke-width="1" vector-effect="non-scaling-stroke" pointer-events="none"/>
        <g data-camera="${esc(c.id)}" style="cursor:pointer"><title>${esc(c.name)}</title><circle cx="${cx}" cy="${cy}" r="${label * 1.1}"
        fill="${CAMERA_TONE[c.tone] ?? CAMERA_TONE.ok}" stroke="#fff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
        <rect x="${cx - label * 0.55}" y="${cy - label * 0.35}" width="${label * 0.75}" height="${label * 0.7}" rx="${label * 0.1}" fill="#fff" pointer-events="none"/>
        <path d="M ${cx + label * 0.2} ${cy} l ${label * 0.4} ${-label * 0.3} v ${label * 0.6} z" fill="#fff" pointer-events="none"/></g>`);
    }
    for (const q of p.people) {
      parts.push(`<circle cx="${q.x}" cy="${q.y}" r="${label * 0.6}" fill="#f59e0b" stroke="#000" stroke-width="1" vector-effect="non-scaling-stroke" pointer-events="none"/>`);
    }
    if (p.start) {
      parts.push(`<circle cx="${p.start[0]}" cy="${p.start[1]}" r="${label * 0.8}" fill="#22d3ee" stroke="#fff" stroke-width="1.5" vector-effect="non-scaling-stroke" pointer-events="none"/>`);
    }
    this.svg.innerHTML = parts.join('');
  }

  /** A sensor at its plan position: the marker (the element's square) and, on the Building Map, its live status colour. */
  sensorMark(e, i, label) {
    const st = this.props.sensorStatus?.[e.name];
    const [cx, cy, r] = [e.x + e.width / 2, e.y + e.height / 2, Math.max(e.width, e.height) / 2];
    return `<g data-index="${i}" data-sensor="${esc(e.name)}" style="cursor:pointer"><title>${esc(e.name)}${st ? ` · ${esc(st.text)}` : ''}</title>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="${st ? CAMERA_TONE[st.tone] ?? CAMERA_TONE.off : STYLE.Sensor.fill}" stroke="${STYLE.Sensor.stroke}"
        stroke-width="1.5" vector-effect="non-scaling-stroke"/>
      <text x="${cx}" y="${cy + r + label * 0.9}" font-size="${label * 0.8}" text-anchor="middle" fill="${STYLE.Sensor.stroke}" pointer-events="none">${esc(e.name)}</text></g>`;
  }

  /** One room of the people layer: live count (0 included), stale (last count, hatched) or unknown ("?", no fill). */
  roomCount(r, label) {
    const [x, y, w, h] = r.bounds;
    const n = r.count === r.maxPossible ? `${r.count}` : `${r.count}–${r.maxPossible}`;
    const text = r.state === 'live' ? `${n} ${r.maxPossible === 1 ? 'person' : 'people'}` : r.state === 'stale' ? `${n}? stale` : '? unknown';
    const fill = r.state === 'live' ? (r.count ? peopleFill(r.count) : 'rgba(168,85,247,0.05)') : r.state === 'stale' ? `url(#${this.uid}-hatch)` : 'none';
    return `<g data-room="${r.elementId}" data-state="${r.state}" pointer-events="none"><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}"/>
      <text x="${x + label * 0.4}" y="${y + label * 1.2}" font-size="${label * 0.9}" font-weight="700"
        fill="${r.state === 'live' ? '#e9d5ff' : '#94a3b8'}">${esc(text)}</text></g>`;
  }

  emit(kind, e) {
    const p = this.toPlan(e);
    const index = e.target.closest?.('[data-index]')?.getAttribute('data-index');
    this.onPointer({ kind, x: p.x, y: p.y, index: index == null ? null : Number(index), handle: !!e.target.closest?.('[data-handle]'),
      camera: e.target.closest?.('[data-camera]')?.getAttribute('data-camera') ?? null,
      sensor: e.target.closest?.('[data-sensor]')?.getAttribute('data-sensor') ?? null });
  }

  toPlan(e) {
    const m = this.svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }
}
