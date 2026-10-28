// Shared helpers for every page: REST calls, error text, toasts, the confirm dialog and formatting.

export const SEVERITY_RANK = { Low: 0, Medium: 1, High: 2, Critical: 3 };

/** Display-only size word for an AI fire severity (fireSeverity, not the incident severity); the AI thresholds are unchanged. */
export const FIRE_SIZE = { Low: 'Small', Medium: 'Medium', High: 'Large', Critical: 'Large' };
/** "High · Large"; an unknown or missing value is returned as it is (no guessed size). */
export const fireSeverityText = (severity) => (FIRE_SIZE[severity] ? `${severity} · ${FIRE_SIZE[severity]}` : severity);

/** HTML-escape a value for use inside template strings. */
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function csrfToken() {
  return document.cookie.split('; ').find((c) => c.startsWith('csrftoken='))?.slice(10) ?? '';
}

/** Error carrying the server's ProblemDetails body, so errorMessage() can show the real reason. */
export class ApiError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

/** fetch() for the JSON API. `body` is JSON unless it is FormData. A 401 means the session ended: back to sign-in. */
export async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRFToken'] = csrfToken();
  if (body !== undefined && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method, headers, credentials: 'same-origin',
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });
  if (response.status === 401) {
    location.assign(`/login?next=${encodeURIComponent(location.pathname)}`);
    throw new ApiError(401, { title: 'Signed out' });
  }
  if (response.status === 204) return null;
  const type = response.headers.get('Content-Type') ?? '';
  const data = type.includes('json') ? await response.json() : await response.blob();
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

/** Human-readable message from a failed api() call (ProblemDetails aware). */
export function errorMessage(err) {
  if (err instanceof ApiError) {
    const b = err.body;
    if (b?.errors) return Object.entries(b.errors).map(([k, v]) => `${k}: ${v.join(' ')}`).join('; ');
    return b?.detail || b?.title || err.message;
  }
  if (err instanceof TypeError) return 'Cannot reach the server. Is it running?';
  return err?.message ?? String(err);
}

// ---------------------------------------------------------------- toasts
const toasts = document.getElementById('toasts');

export function toast(title, message = '', tone = 'info', timeoutMs = 6000) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = `toast toast-${tone}`;
  el.setAttribute('role', tone === 'critical' ? 'alert' : 'status');
  el.innerHTML = `<strong>${esc(title)}</strong>${message ? `<span>${esc(message)}</span>` : ''}`;
  toasts.prepend(el);
  while (toasts.children.length > 5) toasts.lastElementChild.remove();
  if (timeoutMs) setTimeout(() => el.remove(), timeoutMs);
}

toasts.addEventListener('click', (e) => e.target.closest('.toast')?.remove());
for (const el of toasts.children) setTimeout(() => el.remove(), 6000); // server messages (e.g. "Camera saved")

// ---------------------------------------------------------------- confirm dialog
const dialog = document.getElementById('confirm-dialog');

/** Promise<boolean>: used before dangerous actions (master sprinkler switch, deletes). */
export function confirmAsk(title, text, confirmLabel = 'Confirm') {
  dialog.querySelector('[data-title]').textContent = title;
  dialog.querySelector('[data-text]').textContent = text;
  dialog.querySelector('[data-confirm-label]').textContent = confirmLabel;
  dialog.showModal();
  return new Promise((resolve) => {
    const done = (ok) => { dialog.removeEventListener('close', onClose); dialog.close(); resolve(ok); };
    const onClose = () => done(false);
    dialog.addEventListener('close', onClose);
    dialog.querySelectorAll('[data-answer]').forEach((b) => { b.onclick = () => done(b.dataset.answer === 'yes'); });
  });
}

// Server-rendered forms that need a confirmation first (e.g. deleting a camera).
document.addEventListener('submit', async (e) => {
  const form = e.target;
  if (!form.dataset.confirm || form.dataset.confirmed) return;
  e.preventDefault();
  if (await confirmAsk(form.dataset.confirm, form.dataset.confirmText ?? '', form.dataset.confirmLabel)) {
    form.dataset.confirmed = '1';
    form.requestSubmit(e.submitter);
  }
});

// The AI test-image form: refuse a wrong file in the browser before uploading it, then show that inference is running.
document.getElementById('analyze-form')?.addEventListener('submit', (e) => {
  const input = e.target.elements.file;
  const file = input.files[0];
  const busy = document.getElementById('analyze-busy');
  let problem = null;
  if (file && !['image/png', 'image/jpeg'].includes(file.type)) problem = 'Choose a PNG or JPG image.';
  else if (file && file.size > Number(input.dataset.maxMb) * 1024 * 1024) problem = `Image too large: maximum size is ${input.dataset.maxMb} MB.`;
  if (problem) {
    e.preventDefault();
    input.setCustomValidity(problem);
    input.reportValidity();
    input.addEventListener('change', () => input.setCustomValidity(''), { once: true });
    return;
  }
  busy.classList.remove('hidden');
  e.submitter && (e.submitter.disabled = true);
});

// ---------------------------------------------------------------- formatting
export const timeOf = (iso) => new Date(iso).toLocaleTimeString([], { hour12: false });
export const dateTimeOf = (iso) => new Date(iso).toLocaleString();

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return 'never';
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}

/** Same rule as the server's snapshot: any High+ incident or sensor alarm = critical. */
export function systemLevel(openSeverities, sensorStates) {
  const top = Math.max(-1, ...openSeverities.map((s) => SEVERITY_RANK[s]));
  if (top >= SEVERITY_RANK.High || sensorStates.some((s) => s === 'Alarm' || s === 'Critical')) return 'critical';
  if (top >= 0 || sensorStates.includes('Warning')) return 'elevated';
  return 'nominal';
}
