// Firebase Cloud Messaging for this browser (Settings page): permission only on a click, the FCM registration token sent to
// the server, unregister, and alerts shown while this page is open. Closed tabs get them from /sw.js (it shows FCM pushes).
import { api, errorMessage, esc, toast } from './core.js';

const SDK = 'https://www.gstatic.com/firebasejs/10.12.2/'; // pinned Firebase Web SDK (ES modules)
const TOKEN_KEY = 'sentinel-fcm-token';
const $ = (id) => document.getElementById(id);
const box = $('fcm-panel');
const state = { info: null, error: null, busy: false, messaging: null };

const stored = () => { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } };
const remember = (token) => { try { if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ } };
const supported = () => 'serviceWorker' in navigator && 'Notification' in window && 'PushManager' in window;
const PERMISSION = { granted: ['Allowed', 'text-green'], denied: ['Blocked in the browser settings', 'text-red'], default: ['Not asked yet', 'muted'] };
const FIREBASE_TONE = { Configured: 'text-green', 'Not configured': 'muted', 'Missing dependency': 'text-amber', 'Invalid credentials': 'text-red',
  'Firebase unavailable': 'text-red' };

/** This browser's device entry: the stored token's end matched against the user's registered devices. */
function thisDevice() {
  const token = stored();
  return token ? state.info?.devices.find((d) => d.active && token.endsWith(d.tokenEnd)) ?? null : null;
}

function render() {
  if (!box) return;
  const i = state.info, fb = i?.firebase, device = thisDevice();
  const [perm, permClass] = supported() ? PERMISSION[Notification.permission] : ['Not supported by this browser', 'text-red'];
  const canEnable = i && supported() && i.webConfig && Notification.permission !== 'denied' && !device && !state.busy;
  box.innerHTML = !i ? `<p class="${state.error ? 'text-red' : 'empty'}">${esc(state.error ?? 'Loading…')}</p>` : `
    <dl class="facts" data-fcm-facts>
      <div><dt>Firebase (server)</dt><dd class="${FIREBASE_TONE[fb.status] ?? 'muted'}" data-fcm-status>${esc(fb.status)}${fb.reason ? ` <span class="tiny muted">${esc(fb.reason)}</span>` : ''}</dd></div>
      <div><dt>Browser push setup</dt><dd>${i.webConfig ? '<span class="text-green">Configured</span>' : '<span class="muted">Not configured (Firebase web app keys missing on the server)</span>'}</dd></div>
      <div><dt>Notification permission</dt><dd class="${permClass}" data-fcm-permission>${esc(perm)}</dd></div>
      <div><dt>This browser</dt><dd data-fcm-device>${device ? `<span class="text-green">Registered</span> <span class="tiny muted">(device ${device.id})</span>` : '<span class="muted">Not registered</span>'}</dd></div>
      <div><dt>Your devices</dt><dd>${i.devices.filter((d) => d.active).length} active</dd></div>
      <div><dt>Latest Firebase delivery</dt><dd class="small" data-fcm-last>${esc(fb.lastDelivery ?? 'none yet')}</dd></div>
    </dl>
    <div class="row" style="margin-top:8px">
      <button type="button" class="btn btn-sm btn-success" data-fcm-enable ${canEnable ? '' : 'disabled'}>Enable notifications on this browser</button>
      <button type="button" class="btn btn-sm btn-ghost" data-fcm-disable ${device && !state.busy ? '' : 'disabled'}>Disable on this browser</button>
    </div>
    ${state.error ? `<p class="small text-red" role="alert" data-fcm-error>${esc(state.error)}</p>` : ''}
    <p class="tiny muted">"Sent" in the delivery log means Firebase accepted the alert; whether the device showed it is only known on the device.</p>`;
}

async function load() {
  try {
    state.info = await api('/api/push/fcm-token');
  } catch (err) {
    state.error = errorMessage(err);
  }
  render();
  if (thisDevice()) listen().catch(() => undefined); // foreground alerts while this page is open
}

/** Firebase messaging on this page (the SDK comes from Google's CDN the first time it is needed). */
async function messaging() {
  if (state.messaging) return state.messaging;
  const [{ initializeApp }, fm] = await Promise.all([import(`${SDK}firebase-app.js`), import(`${SDK}firebase-messaging.js`)]);
  if (!(await fm.isSupported())) throw new Error('This browser does not support Firebase push notifications.');
  const { vapidKey, ...config } = state.info.webConfig;
  state.messaging = { fm, vapidKey, instance: fm.getMessaging(initializeApp(config)) };
  return state.messaging;
}

async function listen() {
  const m = await messaging();
  m.fm.onMessage(m.instance, (message) => { // a push while this page is visible: Firebase hands it here, not to the worker
    const d = message.data ?? {};
    toast(message.notification?.title ?? 'Sentinel alert', `${message.notification?.body ?? ''} — open Incidents for #${d.incidentId}`, 'critical');
  });
}

async function enable() {
  state.error = null;
  if (!supported()) { state.error = 'This browser cannot receive push notifications.'; return render(); }
  state.busy = true;
  render();
  try {
    if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications are blocked: allow them for this site in the browser settings.');
    const registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    const m = await messaging();
    const token = await m.fm.getToken(m.instance, { vapidKey: m.vapidKey, serviceWorkerRegistration: registration });
    if (!token) throw new Error('Firebase gave no registration token.');
    const previous = stored();
    await api('/api/push/fcm-token', { method: 'POST', body: { token, ...(previous && previous !== token ? { replaces: previous } : {}) } });
    remember(token);
    await listen();
    toast('Notifications enabled', 'This browser now receives fire and smoke alerts, also when Sentinel is closed.', 'success');
  } catch (err) {
    state.error = `Could not enable notifications: ${errorMessage(err)}`;
  } finally {
    state.busy = false;
  }
  await load();
}

async function disable() {
  const token = stored();
  state.error = null;
  try {
    if (token) await api('/api/push/fcm-token', { method: 'DELETE', body: { token } });
    if (state.messaging) await state.messaging.fm.deleteToken(state.messaging.instance).catch(() => undefined);
    remember(null);
    toast('Notifications disabled', 'This browser no longer receives Sentinel alerts.', 'info', 3000);
  } catch (err) {
    state.error = `Could not disable notifications: ${errorMessage(err)}`;
  }
  await load();
}

box?.addEventListener('click', (e) => {
  if (e.target.closest('[data-fcm-enable]')) enable();
  if (e.target.closest('[data-fcm-disable]')) disable();
});
load();
