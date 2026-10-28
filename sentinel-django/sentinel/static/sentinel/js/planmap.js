// SVG floor-plan renderer shared by the editor and the dashboard evacuation panel (port of floor-plan-map.component.ts).
// Units are meters, origin top-left. Pointer events are reported in plan meters with the element under the pointer.
import { esc } from './core.js';

const STYLE = {
  Room: { fill: 'rgba(148,163,184,0.10)', stroke: '#94a3b8' },
  Corridor: { fill: 'rgba(96,165,250,0.10)', stroke: '#60a5fa', dash: '4 3' },
  Wall: { fill: '#cbd5e1', stroke: '#cbd5e1' },
  Door: { fill: '#f59e0b', stroke: '#f59e0b' },
  Exit: { fill: 'rgba(34,197,94,0.55)', stroke: '#22c55e' },
  SafeZone: { fill: 'rgba(34,197,94,0.20)', stroke: '#22c55e' },
  DangerZone: { fill: 'rgba(239,68,68,0.10)', stroke: '#ef4444', dash: '5 3' },
};
const DRAW_ORDER = ['Room', 'Corridor', 'SafeZone', 'DangerZone', 'Wall', 'Door', 'Exit'];
let nextUid = 0;

export class PlanMap {
  /** onPointer({ kind: 'down'|'move'|'up', x, y, index, handle }) in plan meters. */
  constructor(container, onPointer) {
    this.uid = `fp${++nextUid}`;
    this.onPointer = onPointer;
    this.props = { width: 1, height: 1, elements: [], imageUrl: null, riskLayer: null, route: null, start: null, people: [], selected: null,
      preview: null, activeDanger: [], panEnabled: false };
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
    const fill = (e) => (e.type === 'DangerZone' && (e.isActive || p.activeDanger.includes(e.name?.trim() ? e.name : 'Danger zone')) ? 'rgba(239,68,68,0.38)'
      : isBlockedOpening(e) ? 'rgba(239,68,68,0.45)' : STYLE[e.type].fill);
    const sorted = p.elements.map((e, i) => ({ e, i })).sort((a, b) => DRAW_ORDER.indexOf(a.e.type) - DRAW_ORDER.indexOf(b.e.type));
    const parts = [`<defs><pattern id="${this.uid}-grid" width="1" height="1" patternUnits="userSpaceOnUse">
        <path d="M 1 0 L 0 0 0 1" fill="none" stroke="#1c2739" stroke-width="0.03"/></pattern>
      <marker id="${this.uid}-arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#22c55e"/></marker></defs>
      <rect width="${p.width}" height="${p.height}" fill="#0a0f1a"/>`];
    if (p.imageUrl) parts.push(`<image href="${esc(p.imageUrl)}" x="0" y="0" width="${p.width}" height="${p.height}" preserveAspectRatio="none" opacity="0.35"/>`);
    parts.push(`<rect width="${p.width}" height="${p.height}" fill="url(#${this.uid}-grid)" stroke="#2a3850" stroke-width="1" vector-effect="non-scaling-stroke"/>`);
    for (const { e, i } of sorted) {
      const s = STYLE[e.type];
      parts.push(`<g data-index="${i}" style="cursor:pointer"><rect x="${e.x}" y="${e.y}" width="${e.width}" height="${e.height}" fill="${fill(e)}"
        stroke="${isBlockedOpening(e) ? '#ef4444' : s.stroke}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''} stroke-width="1.5" vector-effect="non-scaling-stroke"/>`);
      if (isBlockedOpening(e)) {
        parts.push(`<path d="M ${e.x} ${e.y} L ${e.x + e.width} ${e.y + e.height} M ${e.x + e.width} ${e.y} L ${e.x} ${e.y + e.height}"
          stroke="#ef4444" stroke-width="2" vector-effect="non-scaling-stroke"/>`);
      }
      if (e.name && e.type !== 'Wall') {
        parts.push(`<text x="${e.x + e.width / 2}" y="${e.y + e.height / 2}" font-size="${label}" text-anchor="middle" dominant-baseline="middle"
          fill="${s.stroke}" font-weight="600" pointer-events="none">${esc(e.type === 'Exit' ? `EXIT ${e.name}` : e.name)}</text>`);
      }
      parts.push('</g>');
    }
    if (p.riskLayer) parts.push(`<image href="${p.riskLayer.url}" x="0" y="0" width="${p.riskLayer.width}" height="${p.riskLayer.height}" preserveAspectRatio="none" pointer-events="none"/>`);
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
    for (const q of p.people) {
      parts.push(`<circle cx="${q.x}" cy="${q.y}" r="${label * 0.6}" fill="#f59e0b" stroke="#000" stroke-width="1" vector-effect="non-scaling-stroke" pointer-events="none"/>`);
    }
    if (p.start) {
      parts.push(`<circle cx="${p.start[0]}" cy="${p.start[1]}" r="${label * 0.8}" fill="#22d3ee" stroke="#fff" stroke-width="1.5" vector-effect="non-scaling-stroke" pointer-events="none"/>`);
    }
    this.svg.innerHTML = parts.join('');
  }

  emit(kind, e) {
    const p = this.toPlan(e);
    const index = e.target.closest?.('[data-index]')?.getAttribute('data-index');
    this.onPointer({ kind, x: p.x, y: p.y, index: index == null ? null : Number(index), handle: !!e.target.closest?.('[data-handle]') });
  }

  toPlan(e) {
    const m = this.svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }
}
