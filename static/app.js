/* KHP Panel v1 — frontend helpers (no framework, plain fetch). */
let toastTimer = null;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
async function copyText(text, msg) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
  }
  toast(msg || 'Copied');
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ---------------- dashboard ---------------- */
let SERVERS = [], HEALTH = {};
let EXPIRING = [], EXPIRING_ALL = false, EXP_COUNTS = {};
let EXP_LOADED = false;
// v4: "Expiring soon" starts COLLAPSED (tap the header to show). The
// choice is remembered on this device.
let EXP_COLLAPSED = localStorage.getItem('khp_exp_collapsed') !== '0';
let COLLAPSED = new Set();
let CHECKED_AT = 0;
let DASH_TIMER = null; // 60s quiet auto-refresh timer (cleared on tab switch)
let NAV_CACHE = {}; // prefetched tab HTML, for instant tab switching
let lastServersHtml = null, lastExpiringHtml = null;
const COUNTRY_ORDER = ['Singapore', 'Japan', 'Thailand'];
/* Inline SVG country flags (v3) — drawn shapes, NO emoji font needed.
   Emoji flags rendered as plain "SG"/"TH"/"JP" letters on the user's
   phone, so flags are real graphics now. Same three SVGs as in
   templates/flags.html — keep them in sync. */
function starPoints(cx, cy, rO, rI) {
  const pts = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? rO : rI;
    const a = -Math.PI / 2 + i * Math.PI / 5;
    pts.push((cx + r * Math.cos(a)).toFixed(1) + ',' + (cy + r * Math.sin(a)).toFixed(1));
  }
  return pts.join(' ');
}
function flagSVG(country) {
  const c = (country || '').toLowerCase();
  const open = '<svg class="flag-svg" viewBox="0 0 60 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">';
  // Live DB stores country as codes (SG/JP/TH); accept those AND full names.
  if (c === 'singapore' || c === 'sg') {
    const stars = [[24, 4.5], [29.2, 8.3], [27.2, 14.5], [20.8, 14.5], [18.8, 8.3]]
      .map(([x, y]) => '<polygon points="' + starPoints(x, y, 2.3, 0.95) + '" fill="#fff"/>').join('');
    return open + '<rect width="60" height="20" fill="#ef3340"/><rect y="20" width="60" height="20" fill="#fff"/>' +
      '<circle cx="12" cy="10" r="8" fill="#fff"/><circle cx="16" cy="10" r="6.8" fill="#ef3340"/>' + stars + '</svg>';
  }
  if (c === 'thailand' || c === 'th') {
    return open + '<rect width="60" height="40" fill="#ef3340"/>' +
      '<rect y="6.7" width="60" height="6.6" fill="#fff"/>' +
      '<rect y="13.3" width="60" height="13.4" fill="#241d4f"/>' +
      '<rect y="26.7" width="60" height="6.6" fill="#fff"/></svg>';
  }
  if (c === 'japan' || c === 'jp') {
    return open + '<rect width="60" height="40" fill="#fff"/>' +
      '<circle cx="30" cy="20" r="12" fill="#bc002d"/></svg>';
  }
  return (country || '??').slice(0, 2).toUpperCase();
}
function flagFor(country) { return flagSVG(country); }
// Display name for a country value: live DB stores codes (SG/JP/TH).
function countryName(c) {
  const m = {sg: 'Singapore', jp: 'Japan', th: 'Thailand'};
  return m[(c || '').toLowerCase()] || c || 'Other';
}
function fmtGB(n) {
  if (!n) return '0 GB';
  return Math.round(n / 1073741824).toLocaleString('en-US') + ' GB';
}
function fmtTime(ts) {
  if (!ts) return 'never';
  return new Date(ts * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}
/* v4: load the three dashboard feeds IN PARALLEL (was sequential), and
   never show a spinner after the first load — the auto-refresh updates
   in place, silently. Each feed is applied as soon as it arrives: the
   server list must not wait for the (slow) expiring scan. */
async function loadDashboard(force) {
  try {
    const healthP = api('/api/health' + (force ? '?force=1' : '')).catch(() => null);
    const expP = api('/api/expiring?days=7').catch(() => null);
    const sj = await api('/api/servers');
    SERVERS = sj.servers;
    renderServers();
    const h = await healthP;
    if (h) {
      HEALTH = {};
      h.health.forEach(x => HEALTH[x.id] = x);
      CHECKED_AT = h.checked_at || 0;
      renderDownBanner(h.health);
      const online = h.health.filter(x => x.online).length;
      const statEl = document.getElementById('stat-servers');
      statEl.textContent = online + ' / ' + h.health.length;
      statEl.parentElement.classList.toggle('down', online < h.health.length);
      document.getElementById('stat-keys').textContent =
        h.health.reduce((a, x) => a + (x.clients || 0), 0).toLocaleString('en-US');
      document.getElementById('last-checked').textContent =
        'Last checked ' + fmtTime(CHECKED_AT) + ' • auto-refresh every 60s';
      renderServers();
    }
    const ej = await expP;
    if (ej) applyExpiring(ej.results);
    snapshotTab('/dashboard');
  } catch (e) {
    document.getElementById('server-list').innerHTML =
      '<div class="error">Could not load servers: ' + esc(e.message) + '</div>';
  }
}
/* v4: the 60s auto-refresh — quiet. No spinner, no "Loading servers…"
   flash, no resetting the user's See-all choice; the renderers only
   touch the DOM when the HTML actually changed. */
async function refreshDashboardQuiet() {
  if (!document.getElementById('server-list')) return; // not on dashboard
  try {
    const healthP = api('/api/health').catch(() => null);
    const expP = api('/api/expiring?days=7').catch(() => null);
    const h = await healthP;
    if (h) {
      HEALTH = {};
      h.health.forEach(x => HEALTH[x.id] = x);
      CHECKED_AT = h.checked_at || 0;
      renderDownBanner(h.health);
      const online = h.health.filter(x => x.online).length;
      const statEl = document.getElementById('stat-servers');
      statEl.textContent = online + ' / ' + h.health.length;
      statEl.parentElement.classList.toggle('down', online < h.health.length);
      document.getElementById('stat-keys').textContent =
        h.health.reduce((a, x) => a + (x.clients || 0), 0).toLocaleString('en-US');
      document.getElementById('last-checked').textContent =
        'Last checked ' + fmtTime(CHECKED_AT) + ' • auto-refresh every 60s';
      renderServers();
    }
    const ej = await expP;
    if (ej) applyExpiring(ej.results);
    snapshotTab('/dashboard');
  } catch (e) { /* quiet: a failed background tick just tries again next minute */ }
}
function applyExpiring(results) {
  EXPIRING = results;
  EXP_LOADED = true;
  EXP_COUNTS = {};
  results.forEach(c => EXP_COUNTS[c.server_id] = (EXP_COUNTS[c.server_id] || 0) + 1);
  document.getElementById('stat-expiring').textContent = results.length;
  renderExpiring();
  renderServers(); // expiring counts feed the server cards
}
function initDashboard() {
  if (DASH_TIMER) { clearInterval(DASH_TIMER); DASH_TIMER = null; }
  loadDashboard();
  DASH_TIMER = setInterval(refreshDashboardQuiet, 60000);
  bindDashboardInputs();
  setTimeout(() => prefetchTab('/settings'), 800); // other tab, ready instantly
}
function initSettings() {
  if (DASH_TIMER) { clearInterval(DASH_TIMER); DASH_TIMER = null; }
  loadSettings();
  setTimeout(() => prefetchTab('/dashboard'), 800);
}
function renderDownBanner(health) {
  const el = document.getElementById('down-banner');
  const down = health.filter(x => !x.online);
  if (!down.length) { el.innerHTML = ''; return; }
  el.innerHTML = down.map(x => {
    const seen = x.last_seen ? 'last seen ' + fmtTime(x.last_seen) : 'never seen';
    return '<div class="alert-banner"><div class="warn">!</div>' +
      '<div class="grow"><b>' + esc(x.name) + ' is down</b>' +
      '<div class="sub">' + seen + ' • keys on it still work if already connected</div></div>' +
      '<button class="retry" onclick="retryServer(' + x.id + ', this)">Retry</button></div>';
  }).join('');
}
async function retryServer(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await api('/api/server/' + id + '/test', {method: 'POST'});
    toast('Server is back online');
  } catch (e) {
    toast('Still down: ' + e.message);
  }
  btn.disabled = false; btn.textContent = 'Retry';
  loadDashboard(true);
}
function renderServers() {
  const el = document.getElementById('server-list');
  const f = (document.getElementById('server-filter').value || '').toLowerCase();
  const list = SERVERS.filter(s => s.name.toLowerCase().includes(f) ||
    (s.country || '').toLowerCase().includes(f));
  if (!list.length) {
    const html = '<div class="empty">No servers yet — add one in Settings.</div>';
    if (lastServersHtml !== html) { el.innerHTML = html; lastServersHtml = html; }
    return;
  }
  // group by country: Singapore / Japan / Thailand first, then the rest
  const groups = {};
  list.forEach(s => {
    const g = s.country || 'Other';
    (groups[g] = groups[g] || []).push(s);
  });
  const order = Object.keys(groups).sort((a, b) => {
    const ia = COUNTRY_ORDER.indexOf(countryName(a)), ib = COUNTRY_ORDER.indexOf(countryName(b));
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
  const html = order.map(g => {
    const items = groups[g];
    const collapsed = COLLAPSED.has(g);
    const chev = collapsed ? '▼' : '▲';
    let html = '<div class="group-h" onclick="toggleGroup(\'' + esc(g) + '\')">' +
      '<span class="gflag">' + flagFor(g === 'Other' ? '' : g) + '</span>' +
      '<b>' + esc(countryName(g)) + '</b><span class="muted small">• ' + items.length + ' server' +
      (items.length === 1 ? '' : 's') + '</span>' +
      '<span class="chev">' + chev + '</span></div>';
    if (collapsed) return html;
    html += items.map(s => {
      const h = HEALTH[s.id];
      const online = h ? h.online : null;
      const dot = online === null ? '' : (online ? 'on' : 'off');
      const expN = EXP_COUNTS[s.id] || 0;
      const expHtml = expN ? ' <span class="exp-count">• ' + expN + ' expiring</span>' : '';
      const badges = (s.is_free ? '<span class="badge-free">FREE</span>' : '') +
        (s.is_sales ? '<span class="badge-sales">SALES</span>' : '');
      const sub = online === null
        ? (s.country + ' • checking…')
        : online
          ? (s.type === 'xui' ? '3X-UI' : 'Marzban') + ' • ' + h.clients + ' clients' + expHtml
          : ('Offline — ' + esc(h.error || 'unreachable'));
      if (online === false) {
        const seen = h.last_seen ? 'last seen ' + fmtTime(h.last_seen) : 'never seen';
        return '<div class="server down">' +
          '<div class="flag">' + flagFor(s.country) + '</div>' +
          '<div class="grow"><b style="font-size:18px">' + esc(s.name) +
          '<span class="badge-down">DOWN</span></b>' +
          '<div class="muted small">' + (s.type === 'xui' ? '3X-UI' : 'Marzban') + ' • ' + h.clients + ' clients</div></div>' +
          '<div class="used"><span class="dot off"></span><div class="muted small" style="color:var(--red)">' + seen + '</div></div>' +
          '<button class="keybtn" style="background:transparent;color:var(--red);border:1px solid var(--red)" onclick="event.stopPropagation();retryServer(' + s.id + ', this)">Retry</button></div>';
      }
      // usage bar (v3): real numbers — used vs sum of clients' GB limits
      const pct = (h && h.online && h.limit) ? Math.min(100, Math.round(h.used * 100 / h.limit)) : 0;
      const bar = (h && h.online && h.limit)
        ? '<div class="serverbar"><i style="width:' + pct + '%"></i></div>' : '';
      return '<div class="server" onclick="location.href=\'/server/' + s.id + '\'">' +
        '<div class="flag">' + flagFor(s.country) + '</div>' +
        '<div class="grow"><b style="font-size:18px">' + esc(s.name) + badges + '</b>' +
        '<div class="muted small">' + sub + '</div></div>' +
        '<div class="used"><span class="dot ' + dot + '"></span>' +
        (h && h.online ? '<div class="muted small">' + fmtGB(h.used) + '</div>' : '') + '</div>' +
        '<button class="keybtn" onclick="event.stopPropagation();location.href=\'/server/' + s.id + '#add\'">+ Key</button>' + bar + '</div>';
    }).join('');
    return html;
  }).join('');
  // v4: only touch the DOM when the HTML actually changed — replacing
  // identical markup every 60s is what made the list visibly flicker.
  if (lastServersHtml !== html) { el.innerHTML = html; lastServersHtml = html; }
}
function toggleGroup(g) {
  if (COLLAPSED.has(g)) COLLAPSED.delete(g); else COLLAPSED.add(g);
  renderServers();
}
function filterServers() { renderServers(); }
/* v4: "Expiring soon" is collapsible (default collapsed). The header
   always shows the count; the list only renders when expanded. */
function toggleExpiring() {
  EXP_COLLAPSED = !EXP_COLLAPSED;
  localStorage.setItem('khp_exp_collapsed', EXP_COLLAPSED ? '1' : '0');
  renderExpiring();
}
function renderExpiring() {
  const head = document.getElementById('expiring-head');
  const el = document.getElementById('expiring');
  if (!el) return;
  const countEl = document.getElementById('exp-count');
  if (countEl) countEl.textContent = EXP_LOADED ? EXPIRING.length : '';
  const hint = document.getElementById('exp-hint');
  if (hint) hint.textContent = EXP_COLLAPSED ? 'tap to show' : 'tap to hide';
  const chev = document.getElementById('exp-chev');
  if (chev) chev.textContent = EXP_COLLAPSED ? '▼' : '▲';
  if (EXP_COLLAPSED) { el.style.display = 'none'; return; }
  el.style.display = '';
  if (!EXP_LOADED) { el.innerHTML = '<div class="spin">Loading…</div>'; return; }
  if (!EXPIRING.length) {
    const html = '<div class="empty">Nothing expiring in the next 7 days.</div>';
    if (lastExpiringHtml !== html) { el.innerHTML = html; lastExpiringHtml = html; }
    return;
  }
  const shown = EXPIRING_ALL ? EXPIRING : EXPIRING.slice(0, 5);
  const html = shown.map(c =>
    '<div class="card client"><div class="row"><div class="grow">' +
    '<div class="name">' + esc(c.name) + '</div>' +
    '<div class="muted small">' + esc(c.server_name) + ' • expires ' + esc(c.expiry_h) +
    ' • ' + c.days_left + ' day' + (c.days_left === 1 ? '' : 's') + ' left</div></div>' +
    '<span class="status ' + c.status + '">' + c.status + '</span></div>' +
    '<div class="actions">' +
    '<button class="btn ghost sm" onclick="openRenewModal(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\')">Renew</button>' +
    '<button class="btn ghost sm" onclick="swapKey(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\', this)">Swap</button>' +
    '<button class="btn ghost sm" onclick="openEditModal(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\', ' + Math.round((c.limit || 0) / 1073741824) + ', \'' + nameDateISO(c.name) + '\')">Edit</button>' +
    '<button class="btn ghost sm" onclick="copyLink(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\', this)">Copy link</button>' +
    '</div></div>'
  ).join('') + (!EXPIRING_ALL && EXPIRING.length > 5
    ? '<button class="see-all" onclick="EXPIRING_ALL=true;renderExpiring()">See all ' + EXPIRING.length + ' →</button>'
    : (EXPIRING_ALL && EXPIRING.length > 5
      ? '<button class="see-all" onclick="EXPIRING_ALL=false;renderExpiring()">Show less ↑</button>' : ''));
  if (lastExpiringHtml !== html) { el.innerHTML = html; lastExpiringHtml = html; }
}
async function globalSearch() {
  const q = document.getElementById('q').value.trim();
  const el = document.getElementById('search-results');
  if (q.length < 2) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="spin">Searching all servers…</div>';
  try {
    const j = await api('/api/search?q=' + encodeURIComponent(q));
    if (!j.results.length) { el.innerHTML = '<div class="empty">No key found for “' + esc(q) + '” on any server.</div>'; return; }
    el.innerHTML = j.results.map(c =>
      '<div class="card client" onclick="location.href=\'/server/' + c.server_id + '\'">' +
      '<div class="row"><div class="grow"><div class="name">' + esc(c.name) + '</div>' +
      '<div class="muted small">' + esc(c.server_name) + ' • ' + esc(c.used_h) + ' / ' + esc(c.limit_h) +
      ' • exp ' + esc(c.expiry_h) + '</div></div>' +
      '<span class="status ' + c.status + '">' + c.status + '</span></div></div>'
    ).join('');
  } catch (e) {
    el.innerHTML = '<div class="error">Search failed: ' + esc(e.message) + '</div>';
  }
}

/* ---------------- server page ---------------- */
let SORT_BY = 'newest'; // v4: newest keys first (was expiry)
let FILTER_BY = 'all';
const PAGE_SIZE = 20;
let SHOWN = PAGE_SIZE;
const ARMED = {}; // two-click confirm state, keyed by action+name
/* v4 SPEED: after a key action, update ONLY that key's row from the
   action's own response — no full /clients re-fetch, no health re-check
   of all 12 servers, no spinner. The list is then quietly re-synced
   with the panel in the background a couple of seconds later. */
let QUIET_SYNC_TIMER = null;
function scheduleQuietSync(serverId) {
  if (QUIET_SYNC_TIMER) clearTimeout(QUIET_SYNC_TIMER);
  QUIET_SYNC_TIMER = setTimeout(() => silentReloadServer(serverId), 2500);
}
/* v4: an action starting cancels any pending quiet sync, so no background
   fetch can land in the middle of it (see the race note in
   silentReloadServer). The action schedules a fresh sync when it succeeds. */
function clearQuietSync() {
  if (QUIET_SYNC_TIMER) { clearTimeout(QUIET_SYNC_TIMER); QUIET_SYNC_TIMER = null; }
}
async function silentReloadServer(serverId) {
  // No spinner, no error box: a failed background sync just leaves the
  // (already correct) local rows in place.
  // v4 race fix: never reload while an action modal is open — a background
  // fetch that lands mid-rename sees the NEW name with created_at=0 (the
  // panel_keys row is moved only when the rename request finishes), and
  // overwriting the list with that snapshot drops the NEW badge and makes
  // the follow-up patch miss (it looks the key up by its OLD name).
  // The action's own scheduleQuietSync refreshes after it completes.
  if (document.querySelector('.modal.open')) return;
  try {
    if (document.getElementById('client-list') && serverId === SERVER_ID) {
      const j = await api('/api/server/' + serverId + '/clients');
      // v4: a modal may have opened while this fetch was in flight — a
      // snapshot taken mid-rename is stale, so drop it; the action's own
      // quiet sync refreshes once it finishes.
      if (document.querySelector('.modal.open')) return;
      ALL_CLIENTS = j.clients;
      document.getElementById('count-all').textContent = ALL_CLIENTS.length;
      renderClients();
    } else if (document.getElementById('server-list')) {
      refreshDashboardQuiet();
    }
  } catch (e) { /* ignore — local rows already show the result */ }
}
function applyKeyPatch(serverId, oldName, patch) {
  // patch: {name?, expiry?, expiry_h?, gb?, limit?, used?, enable?, status?, created_at?, remove?}
  const now = Date.now() / 1000;
  if (typeof ALL_CLIENTS !== 'undefined' && document.getElementById('client-list')) {
    if (patch.remove) {
      ALL_CLIENTS = ALL_CLIENTS.filter(c => c.name !== oldName);
    } else {
      // v4: match the OLD name first; if the list was already refreshed to
      // the NEW name (rename landed before this patch), update that row
      // instead of silently doing nothing.
      const c = ALL_CLIENTS.find(x => x.name === oldName) ||
        (patch.name ? ALL_CLIENTS.find(x => x.name === patch.name) : undefined);
      if (c) {
        if (patch.name) c.name = patch.name;
        if (patch.expiry != null) { c.expiry = patch.expiry; c.expiry_h = patch.expiry_h || c.expiry_h; }
        if (patch.limit != null) { c.limit = patch.limit; c.limit_h = fmtGB(patch.limit); c.pct = c.limit ? Math.min(100, Math.round(c.used * 100 / c.limit)) : 0; }
        if (patch.used != null) { c.used = patch.used; c.used_h = fmtGB(patch.used); c.pct = c.limit ? Math.min(100, Math.round(c.used * 100 / c.limit)) : 0; }
        if (patch.enable != null) c.enable = patch.enable;
        if (patch.status) c.status = patch.status;
        if (patch.created_at != null) c.created_at = patch.created_at;
      }
    }
    renderClients();
  }
  // dashboard "Expiring soon" rows use the same actions: keep them in sync
  if (document.getElementById('expiring') && EXP_LOADED) {
    if (patch.remove) {
      EXPIRING = EXPIRING.filter(c => !(c.server_id === serverId && c.name === oldName));
    } else {
      const c = EXPIRING.find(x => x.server_id === serverId && x.name === oldName);
      if (c) {
        if (patch.name) c.name = patch.name;
        if (patch.expiry != null) { c.expiry = patch.expiry; c.expiry_h = patch.expiry_h || c.expiry_h; }
        if (patch.limit != null) c.limit = patch.limit;
        if (patch.enable != null) c.enable = patch.enable;
        if (patch.status) c.status = patch.status;
      }
    }
    // a renewed/edited key leaves the 7-day window — drop it from the list
    EXPIRING = EXPIRING.filter(c => c.expiry && c.expiry - now <= 7 * 86400);
    EXP_COUNTS = {};
    EXPIRING.forEach(c => EXP_COUNTS[c.server_id] = (EXP_COUNTS[c.server_id] || 0) + 1);
    document.getElementById('stat-expiring').textContent = EXPIRING.length;
    renderExpiring();
    renderServers();
  }
}
async function loadServerPage() {
  // v4.2: health and keys load IN PARALLEL — the old code awaited
  // /api/health first, so the key list waited behind the slowest of 12
  // panel checks (and behind a queued gunicorn worker). Keys render as
  // soon as they arrive; the header updates when health arrives.
  const healthP = api('/api/health').then(h => {
    const me = h.health.find(x => x.id === SERVER_ID);
    document.getElementById('srv-status').textContent =
      me ? (me.online ? me.clients + ' clients' : 'offline — ' + (me.error || '')) : 'unknown';
    document.getElementById('srv-dot').className = 'dot ' + (me && me.online ? 'on' : 'off');
  }).catch(() => { /* health is best-effort */ });
  try {
    const j = await api('/api/server/' + SERVER_ID + '/clients');
    ALL_CLIENTS = j.clients;
    document.getElementById('count-all').textContent = ALL_CLIENTS.length;
    renderClients();
  } catch (e) {
    document.getElementById('client-list').innerHTML =
      '<div class="error">Could not load keys: ' + esc(e.message) + '</div>';
  }
  await healthP;
}
function filteredClients() {
  const f = (document.getElementById('client-filter').value || '').toLowerCase();
  const now = Date.now() / 1000;
  let list = ALL_CLIENTS.filter(c => c.name.toLowerCase().includes(f));
  if (FILTER_BY === 'expiring')
    list = list.filter(c => c.expiry && c.expiry - now <= 7 * 86400);
  if (FILTER_BY === 'high')
    list = list.filter(c => c.pct >= 70);
  if (SORT_BY === 'newest') {
    // v4 default: keys this panel created, newest first; keys with no
    // recorded creation time (created elsewhere / before v4) come after,
    // soonest-expiring first.
    list.sort((a, b) => {
      const ca = a.created_at || 0, cb = b.created_at || 0;
      if ((ca > 0) !== (cb > 0)) return cb - ca;
      if (ca && cb) return cb - ca;
      return (a.expiry || 1e15) - (b.expiry || 1e15);
    });
  } else if (SORT_BY === 'expiry')
    list.sort((a, b) => (a.expiry || 1e15) - (b.expiry || 1e15));
  else if (SORT_BY === 'usage')
    list.sort((a, b) => (b.used || 0) - (a.used || 0));
  else
    list.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  return list;
}
function isNewKey(c) {
  // v4: NEW badge for keys created through this panel in the last 24h.
  const ca = c.created_at || 0;
  return ca > 0 && (Date.now() / 1000 - ca) < 24 * 3600;
}
function isExpiringToday(c) {
  const now = Date.now() / 1000;
  return c.expiry && c.expiry - now <= 86400;
}
function renderClients() {
  const el = document.getElementById('client-list');
  const list = filteredClients();
  if (!list.length) { el.innerHTML = '<div class="empty">No keys found.</div>'; }
  else {
    el.innerHTML = list.slice(0, SHOWN).map(c => {
      const n = esc(c.name).replace(/'/g, "\\'");
      const toggleLabel = c.enable ? 'Disable' : 'Enable';
      const isNew = isNewKey(c);
      // v4 cleaner card: name (+ NEW badge) and status on one row,
      // usage line below, slim full-width bar, then the buttons.
      return '<div class="card client' + (isExpiringToday(c) ? ' expiring-today' : '') + (isNew ? ' is-new' : '') + '">' +
      '<div class="row"><div class="grow"><div class="name">' + esc(c.name) +
      (isNew ? '<span class="badge-new">NEW</span>' : '') + '</div></div>' +
      '<span class="status ' + c.status + '">' + c.status + '</span></div>' +
      '<div class="muted small" style="margin-top:4px">' + esc(c.used_h) + ' / ' + esc(c.limit_h) + ' • exp ' + esc(c.expiry_h) + '</div>' +
      '<div class="bar"><i style="width:' + c.pct + '%"></i></div>' +
      '<div class="actions">' +
      '<button class="btn ghost sm" onclick="copyLink(' + c.server_id + ', \'' + n + '\', this)">Copy link</button>' +
      '<button class="btn ghost sm" onclick="openRenewModal(' + c.server_id + ', \'' + n + '\')">Renew</button>' +
      '<button class="btn ghost sm" onclick="swapKey(' + c.server_id + ', \'' + n + '\', this)">Swap</button>' +
      '<button class="btn ghost sm" onclick="toggleKey(' + c.server_id + ', \'' + n + '\', ' + (!c.enable) + ', this)">' + toggleLabel + '</button>' +
      '<button class="btn ghost sm" onclick="openEditModal(' + c.server_id + ', \'' + n + '\')">Edit</button>' +
      '<button class="btn ghost sm red" onclick="deleteKey(' + c.server_id + ', \'' + n + '\', this)">Delete</button>' +
      '</div></div>';
    }).join('');
  }
  document.getElementById('page-info').textContent =
    'Showing ' + Math.min(SHOWN, list.length) + ' of ' + list.length + ' keys';
  document.getElementById('load-more').style.display =
    (SHOWN < list.length) ? '' : 'none';
}
function loadMoreClients() { SHOWN += PAGE_SIZE; renderClients(); }
function filterClients() { SHOWN = PAGE_SIZE; renderClients(); }
async function copyLink(serverId, name, btn) {
  btn.disabled = true;
  try {
    const j = await api('/api/server/' + serverId + '/link/' + encodeURIComponent(name));
    await copyText(j.link, 'Link copied');
  } catch (e) { toast('Failed: ' + e.message); }
  btn.disabled = false;
}
// Two-click arm: first click arms the button ("Sure?"), second click (within
// 4s) runs the destructive action. No window.confirm() — automated browsers
// auto-dismiss those dialogs.
function armOrRun(key, btn, armLabel, run) {
  if (!ARMED[key]) {
    ARMED[key] = true;
    const orig = btn.textContent;
    btn.textContent = armLabel;
    setTimeout(() => {
      ARMED[key] = false;
      if (btn.isConnected) btn.textContent = orig;
    }, 4000);
    return;
  }
  ARMED[key] = false;
  run();
}
async function deleteKey(serverId, name, btn) {
  armOrRun('del:' + name, btn, 'Sure?', async () => {
    clearQuietSync();
    try {
      await api('/api/server/' + serverId + '/delete-key', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})});
      toast('Deleted ' + name);
      applyKeyPatch(serverId, name, {remove: true});
      scheduleQuietSync(serverId);
    } catch (e) { toast('Delete failed: ' + e.message); }
  });
}
async function swapKey(serverId, name, btn) {
  clearQuietSync();
  armOrRun('swap:' + name, btn, 'Swap?', async () => {
    btn.disabled = true;
    try {
      const j = await api('/api/server/' + serverId + '/clients/' + encodeURIComponent(name) + '/swap',
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      showActionResult(j.name, j.link, 'Swapped — new password, same name / expiry / GB');
      toast('Swapped ' + name);
      // fresh password => used traffic resets to 0; it is a new key now
      applyKeyPatch(serverId, name, {used: 0, limit: j.limit || undefined,
        created_at: j.created_at || undefined});
      scheduleQuietSync(serverId);
    } catch (e) { toast('Swap failed: ' + e.message); }
    btn.disabled = false;
  });
}
async function toggleKey(serverId, name, enable, btn) {
  clearQuietSync();
  btn.disabled = true;
  try {
    const j = await api('/api/server/' + serverId + '/clients/' + encodeURIComponent(name) + '/toggle',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({enable})});
    toast((j.enable ? 'Enabled ' : 'Disabled ') + name);
    applyKeyPatch(serverId, name, {enable: j.enable, status: j.status});
    scheduleQuietSync(serverId);
  } catch (e) { toast('Failed: ' + e.message); }
  btn.disabled = false;
}
/* ---- shared Renew / Edit-GB modal (base.html #action-modal) ---- */
function openActionModal(title, bodyHtml) {
  document.getElementById('action-title').textContent = title;
  document.getElementById('action-body').innerHTML = bodyHtml;
  document.getElementById('action-body').style.display = '';
  document.getElementById('action-result').style.display = 'none';
  document.getElementById('action-modal').classList.add('open');
}
function closeActionModal() {
  document.getElementById('action-modal').classList.remove('open');
}
function showActionResult(name, link, note) {
  document.getElementById('action-result-name').textContent =
    name + (note ? '  •  ' + note : '');
  const lb = document.getElementById('action-result-link');
  const cb = document.getElementById('action-copy-btn');
  if (link) {
    lb.textContent = link; lb.style.display = '';
    cb.style.display = '';
  } else {
    lb.style.display = 'none'; cb.style.display = 'none';
  }
  document.getElementById('action-body').style.display = 'none';
  document.getElementById('action-result').style.display = '';
}
function openRenewModal(serverId, name) {
  openActionModal('Renew key — ' + name,
    '<p class="muted">New expiry = old expiry + 30 / 60 / 90 days. ' +
    'The key is renamed to the new date. GB stays the same.</p>' +
    '<div style="height:12px"></div>' +
    '<div class="seg">' +
    '<button type="button" onclick="submitRenew(' + serverId + ', \'' + esc(name).replace(/'/g, "\\'") + '\', 1, this)">+1 mo</button>' +
    '<button type="button" onclick="submitRenew(' + serverId + ', \'' + esc(name).replace(/'/g, "\\'") + '\', 2, this)">+2 mo</button>' +
    '<button type="button" onclick="submitRenew(' + serverId + ', \'' + esc(name).replace(/'/g, "\\'") + '\', 3, this)">+3 mo</button>' +
    '</div><div style="height:10px"></div>' +
    '<button class="btn ghost block" onclick="closeActionModal()">Cancel</button>');
}
async function submitRenew(serverId, name, months, btn) {
  clearQuietSync();
  btn.disabled = true;
  try {
    const j = await api('/api/server/' + serverId + '/clients/' + encodeURIComponent(name) + '/renew',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({months})});
    showActionResult(j.name, j.link,
      'renewed +' + months + ' mo • ' + j.gb + ' GB • exp ' + j.expiry_h);
    toast('Renewed → ' + j.name);
    applyKeyPatch(serverId, name, {name: j.name, expiry: j.expiry,
      expiry_h: j.expiry_h, limit: j.gb * 1073741824});
    scheduleQuietSync(serverId);
  } catch (e) { toast('Renew failed: ' + e.message); }
  btn.disabled = false;
}
/* ---------- Edit key (v3): custom GB + custom expiry date ---------- */
function nameDateISO(name) {
  // Name__D_M_YYYY -> YYYY-MM-DD for the date input.
  const m = (name || '').match(/__(\d{1,2})[_-](\d{1,2})[_-](\d{4})$/);
  if (!m) return '';
  return m[3] + '-' + m[2].padStart(2, '0') + '-' + m[1].padStart(2, '0');
}
function openEditModal(serverId, name, gbPrefill, datePrefill) {
  let gbVal = gbPrefill, dateVal = datePrefill;
  if (gbVal == null || !dateVal) {
    const c = (typeof ALL_CLIENTS !== 'undefined')
      ? ALL_CLIENTS.find(x => x.name === name) : null;
    if (c) {
      if (gbVal == null) gbVal = Math.round((c.limit || 0) / 1073741824) || 400;
      if (!dateVal) dateVal = nameDateISO(name);
    }
  }
  if (gbVal == null) gbVal = 400;
  if (!dateVal) dateVal = '';
  openActionModal('Edit key — ' + name,
    '<p class="muted">GB can be any number. A new expiry date renames the key to that date; used traffic is kept.</p>' +
    '<div style="height:12px"></div>' +
    '<label>GB limit</label>' +
    '<input id="edit-gb" type="number" min="1" step="1" value="' + gbVal + '">' +
    '<div class="seg" style="margin:8px 0 14px">' +
    [400, 500, 1000, 1500].map(gb =>
      '<button type="button" onclick="document.getElementById(\'edit-gb\').value=' + gb + '">' + gb + ' GB</button>'
    ).join('') + '</div>' +
    '<label>Expiry date</label>' +
    '<input id="edit-date" type="date" value="' + dateVal + '">' +
    '<div style="height:12px"></div>' +
    '<div class="row"><button class="btn" id="edit-save" style="flex:1" onclick="submitEdit(' + serverId + ', \'' + esc(name).replace(/'/g, "\\'") + '\', this)">Save changes</button>' +
    '<button class="btn ghost" style="flex:1" onclick="closeActionModal()">Cancel</button></div>');
}
async function submitEdit(serverId, name, btn) {
  clearQuietSync();
  const gbRaw = document.getElementById('edit-gb').value;
  const dateRaw = document.getElementById('edit-date').value;
  const gb = parseFloat(gbRaw);
  const body = {};
  if (gbRaw !== '' && !isNaN(gb) && gb > 0) body.gb = gb;
  if (dateRaw && /^\d{4}-\d{2}-\d{2}$/.test(dateRaw)) body.expiry_date = dateRaw;
  if (!body.gb && !body.expiry_date) { toast('Change the GB or the date first'); return; }
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const j = await api('/api/server/' + serverId + '/clients/' + encodeURIComponent(name) + '/edit',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(body)});
    if (j.name && j.name !== name) {
      showActionResult(j.name, j.link, 'edited • ' + j.gb + ' GB • exp ' + j.expiry_h);
    } else {
      toast('Updated • ' + j.gb + ' GB • exp ' + j.expiry_h);
      closeActionModal();
    }
    applyKeyPatch(serverId, name, {name: j.name, expiry: j.expiry,
      expiry_h: j.expiry_h, limit: j.gb * 1073741824});
    scheduleQuietSync(serverId);
  } catch (e) { toast('Failed: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Save changes';
}
function openAddKey() {
  document.getElementById('add-form').style.display = '';
  document.getElementById('add-result').style.display = 'none';
  document.getElementById('add-modal').classList.add('open');
  document.getElementById('add-name').focus();
}
function closeAddKey() {
  document.getElementById('add-modal').classList.remove('open');
}
async function submitAddKey() {
  clearQuietSync();
  const name = document.getElementById('add-name').value.trim();
  const months = parseInt(document.querySelector('#add-months button.sel').dataset.m, 10);
  const btn = document.getElementById('add-submit');
  if (!name) { toast('Enter a customer name'); return; }
  // v4: optional custom GB / custom expiry date override the preset.
  const gbRaw = (document.getElementById('add-gb').value || '').trim();
  const dateRaw = (document.getElementById('add-date').value || '').trim();
  const body = {name, months};
  if (gbRaw !== '') {
    const gb = parseFloat(gbRaw);
    if (isNaN(gb) || gb <= 0) { toast('Custom GB must be a positive number'); return; }
    body.gb = gb;
  }
  if (dateRaw !== '') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateRaw)) { toast('Pick a valid date'); return; }
    const picked = new Date(dateRaw + 'T00:00:00');
    const today = new Date(); today.setHours(0, 0, 0, 0);
    if (picked < today) { toast('Expiry date is in the past'); return; }
    body.expiry_date = dateRaw;
  }
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    const j = await api('/api/server/' + SERVER_ID + '/add-key', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
    document.getElementById('add-result-name').textContent =
      j.name + '  •  ' + j.summary;
    document.getElementById('add-result-link').textContent = j.link;
    document.getElementById('add-form').style.display = 'none';
    document.getElementById('add-result').style.display = '';
    // v4: the new key appears at the top AT ONCE (Newest sort + NEW badge)
    // — no waiting for a full list re-fetch.
    if (typeof ALL_CLIENTS !== 'undefined') {
      ALL_CLIENTS.unshift({
        name: j.name, server_id: SERVER_ID, status: 'active', enable: true,
        used: 0, used_h: '0 GB', limit: j.limit, limit_h: fmtGB(j.limit),
        expiry: j.expiry_ts, expiry_h: j.expiry_h, pct: 0,
        created_at: j.created_at || Math.floor(Date.now() / 1000),
      });
      document.getElementById('count-all').textContent = ALL_CLIENTS.length;
      SHOWN = PAGE_SIZE;
      renderClients();
    }
    scheduleQuietSync(SERVER_ID);
  } catch (e) { toast('Create failed: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Create key';
}

/* ---------------- instant bottom-nav tabs (v4) ----------------
   The three tabs (Servers / Search / Settings) switch WITHOUT a full
   page reload: the target page's <main> is fetched (or taken from a
   prefetch cache) and swapped in, then the page's init runs. Data still
   comes from the 60s cache with a quiet background refresh — so a tab
   switch is instant. Any fetch failure falls back to a normal load. */
function prefetchTab(url) {
  if (NAV_CACHE[url]) return;
  fetch(url, {credentials: 'same-origin'}).then(r => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.text();
  }).then(html => {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const main = doc.querySelector('main');
    if (main) NAV_CACHE[url] = {title: doc.title, html: main.innerHTML};
  }).catch(() => { /* prefetch is best-effort; the click will fetch */ });
}
/* v4.1: snapshot the POPULATED tab HTML after a successful load, so a
   later tab switch restores live numbers instead of the template's
   "Loading servers…" spinners. The template HTML in NAV_CACHE is only
   a first-visit fallback; the snapshot replaces it. */
function snapshotTab(url) {
  const main = document.querySelector('main');
  if (!main) return;
  const q = document.getElementById('q');
  const sf = document.getElementById('server-filter');
  NAV_CACHE[url] = {title: document.title, html: main.innerHTML, populated: true,
    q: q ? q.value : '', sf: sf ? sf.value : ''};
}
function bindDashboardInputs() {
  const sf = document.getElementById('server-filter');
  if (sf) sf.addEventListener('input', filterServers);
  const q = document.getElementById('q');
  if (q) q.addEventListener('input', debounce(globalSearch, 350));
  if (location.hash === '#search') {
    const q2 = document.getElementById('q');
    if (q2) q2.focus();
  }
}
/* Quiet re-init used when a tab is restored from a populated snapshot:
   no full load, no spinner — just restart the 60s timer and refresh
   silently in the background. */
function initDashboardQuiet() {
  if (DASH_TIMER) { clearInterval(DASH_TIMER); DASH_TIMER = null; }
  DASH_TIMER = setInterval(refreshDashboardQuiet, 60000);
  bindDashboardInputs();
  refreshDashboardQuiet();
  setTimeout(() => prefetchTab('/settings'), 800);
}
function swapMain(url, data, push) {
  const main = document.querySelector('main');
  if (!main) return false;
  // v4: the renderers skip DOM writes when the HTML is unchanged — but a
  // tab swap replaces the DOM with the template's spinner HTML, so the
  // "unchanged" cache is stale here and must be reset.
  lastServersHtml = null; lastExpiringHtml = null;
  main.innerHTML = data.html;
  if (data.populated) { // restore what the user had typed before the switch
    const q = document.getElementById('q');
    if (q && data.q) q.value = data.q;
    const sf = document.getElementById('server-filter');
    if (sf && data.sf) sf.value = data.sf;
  }
  if (url === '/settings' && SERVERS.length) {
    // v4.2: the dashboard already fetched the server list — show real
    // rows at once instead of the template's "Loading…" placeholder.
    const sl = document.getElementById('settings-list');
    if (sl) sl.innerHTML = settingsListHtml(SERVERS);
  }
  document.title = data.title;
  const tab = url === '/settings' ? 'settings' : 'servers';
  document.querySelectorAll('nav.bottom a[data-tab]').forEach(a => {
    a.classList.toggle('on', a.dataset.tab === tab);
  });
  if (push) history.pushState({tab: url}, '', url);
  if (url === '/settings') initSettings();
  else if (data.populated) initDashboardQuiet();
  else initDashboard();
  window.scrollTo(0, 0);
  return true;
}
async function goTab(url, push) {
  if (NAV_CACHE[url]) { swapMain(url, NAV_CACHE[url], push !== false); return; }
  try {
    const r = await fetch(url, {credentials: 'same-origin'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const main = doc.querySelector('main');
    if (!main) throw new Error('no main');
    NAV_CACHE[url] = {title: doc.title, html: main.innerHTML};
    swapMain(url, NAV_CACHE[url], push !== false);
  } catch (e) { location.href = url; }
}
function navTab(link, ev) {
  ev.preventDefault();
  const href = link.getAttribute('href');
  if (href.indexOf('#search') !== -1) {
    // Search tab: focus the dashboard search box (load the dashboard
    // first if we are on another tab).
    const q = document.getElementById('q');
    if (q) {
      q.focus();
      q.scrollIntoView({behavior: 'smooth', block: 'start'});
      history.replaceState(null, '', '/dashboard#search');
    } else {
      goTab('/dashboard').then(() => {
        const q2 = document.getElementById('q');
        if (q2) { q2.focus(); q2.scrollIntoView({behavior: 'smooth'}); }
      });
    }
    return false;
  }
  const path = href.split('#')[0] || '/dashboard';
  if (location.pathname === path || (path === '/dashboard' && location.pathname === '/')) {
    window.scrollTo({top: 0, behavior: 'smooth'});
    return false;
  }
  goTab(path);
  return false;
}
window.addEventListener('popstate', () => {
  const path = location.pathname;
  if (path === '/settings' || path === '/dashboard' || path === '/') {
    goTab(path === '/' ? '/dashboard' : path, false);
  }
});

/* ---------------- settings ---------------- */
/* v4.2: settings row HTML, shared by loadSettings() and the tab-switch
   pre-render in swapMain() (the dashboard already holds SERVERS, so the
   first Settings switch shows real rows instantly instead of "Loading…"). */
function settingsListHtml(servers) {
  if (!servers.length) return '<div class="empty">No servers yet.</div>';
  return servers.map(s =>
    // v4: flag tile + FREE/SALES badges on every row, cleaner layout
    '<div class="card"><div class="row">' +
    '<span class="flag-sm">' + flagSVG(s.country) + '</span>' +
    '<div class="grow">' +
    '<b style="font-size:17px">' + esc(s.name) + '</b>' +
    (s.is_free ? '<span class="badge-free">FREE</span>' : '') +
    (s.is_sales ? '<span class="badge-sales">SALES</span>' : '') +
    '<div class="muted small">' + esc(s.type === 'xui' ? '3X-UI' : 'Marzban') + ' • ' + esc(countryName(s.country)) + '</div>' +
    '<div class="muted small">' + esc(s.base_url) + '</div></div></div>' +
    '<div class="row" style="margin-top:12px">' +
    '<button class="btn ghost" onclick="editServer(' + s.id + ')">Edit</button>' +
    '<button class="btn ghost" onclick="testServer(' + s.id + ', this)">Test connection</button>' +
    '<button class="btn ghost" style="color:var(--red);border-color:rgba(239,68,68,.5)" onclick="removeServer(' + s.id + ', \'' + esc(s.name) + '\', this)">Remove</button>' +
    '</div><div class="muted small" id="test-' + s.id + '" style="margin-top:8px"></div></div>'
  ).join('');
}
async function loadSettings() {
  try {
    const j = await api('/api/servers');
    SERVERS = j.servers; // editServer() reads from this global
    const el = document.getElementById('settings-list');
    el.innerHTML = settingsListHtml(j.servers);
    snapshotTab('/settings');
  } catch (e) {
    document.getElementById('settings-list').innerHTML =
      '<div class="error">Could not load servers: ' + esc(e.message) + '</div>';
  }
}
async function testServer(id, btn) {
  btn.disabled = true;
  const out = document.getElementById('test-' + id);
  out.textContent = 'Testing…';
  try {
    const j = await api('/api/server/' + id + '/test', {method: 'POST'});
    out.textContent = 'Online — ' + j.clients + ' clients, ' + j.used_h + ' used.';
    out.style.color = 'var(--emerald)';
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
    out.style.color = 'var(--red)';
  }
  btn.disabled = false;
}
// Two-click remove: window.confirm() dialogs are auto-dismissed by automated
// browsers (and by some mobile browsers), so the first click only arms the
// button and the second click (within 4s) actually removes. Removing a server
// only deletes this panel's own record — the real server is untouched.
const ARMED_REMOVE = {};
async function removeServer(id, name, btn) {
  if (!ARMED_REMOVE[id]) {
    ARMED_REMOVE[id] = true;
    btn.textContent = 'Click again to remove';
    setTimeout(() => {
      ARMED_REMOVE[id] = false;
      if (btn.isConnected) btn.textContent = 'Remove';
    }, 4000);
    return;
  }
  ARMED_REMOVE[id] = false;
  try {
    await api('/api/servers/' + id, {method: 'DELETE'});
    toast('Removed ' + name);
  } catch (e) {
    toast('Remove failed: ' + e.message);
  }
  loadSettings();
}
let EDITING_ID = null;
function editServer(id) {
  const s = SERVERS.find(x => x.id === id);
  if (!s) return;
  EDITING_ID = id;
  document.getElementById('f-name').value = s.name || '';
  document.getElementById('f-country').value = s.country || '';
  document.getElementById('f-type').value = s.type || 'xui';
  document.getElementById('f-base').value = s.base_url || '';
  document.getElementById('f-path').value = s.panel_path || '';
  document.getElementById('f-user').value = s.admin_user || 'kohein';
  document.getElementById('f-pw').value = '';
  document.getElementById('f-pw').placeholder = 'Leave blank to keep the current password';
  document.getElementById('f-host').value = s.share_host || '';
  document.getElementById('f-port').value = s.share_port || 1080;
  document.getElementById('f-free').checked = !!s.is_free;
  document.getElementById('f-sales').checked = !!s.is_sales;
  document.getElementById('form-title').textContent = 'Edit server: ' + s.name;
  document.getElementById('save-btn').textContent = 'Save changes';
  document.getElementById('cancel-edit').style.display = '';
  document.getElementById('form-error').innerHTML = '';
  window.scrollTo({top: document.getElementById('form-title').offsetTop - 10, behavior: 'smooth'});
}
function cancelEdit() {
  EDITING_ID = null;
  ['f-name', 'f-country', 'f-base', 'f-path', 'f-pw', 'f-host'].forEach(i => document.getElementById(i).value = '');
  document.getElementById('f-type').value = 'xui';
  document.getElementById('f-user').value = 'kohein';
  document.getElementById('f-pw').placeholder = 'Panel admin password';
  document.getElementById('f-port').value = 1080;
  document.getElementById('f-free').checked = false;
  document.getElementById('f-sales').checked = false;
  document.getElementById('form-title').textContent = 'Add a server';
  document.getElementById('save-btn').textContent = 'Save server';
  document.getElementById('cancel-edit').style.display = 'none';
  document.getElementById('form-error').innerHTML = '';
}
async function addServer() {
  const body = {
    name: document.getElementById('f-name').value.trim(),
    country: document.getElementById('f-country').value.trim(),
    type: document.getElementById('f-type').value,
    base_url: document.getElementById('f-base').value.trim(),
    panel_path: document.getElementById('f-path').value.trim(),
    admin_user: document.getElementById('f-user').value.trim() || 'kohein',
    admin_pw: document.getElementById('f-pw').value,
    share_host: document.getElementById('f-host').value.trim(),
    share_port: parseInt(document.getElementById('f-port').value, 10) || 1080,
    is_free: document.getElementById('f-free').checked,
    is_sales: document.getElementById('f-sales').checked,
  };
  const err = document.getElementById('form-error');
  try {
    if (EDITING_ID) {
      // Blank password in edit mode keeps the stored one (server ignores it).
      await api('/api/servers/' + EDITING_ID, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)});
      toast('Server updated');
    } else {
      await api('/api/servers', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)});
      toast('Server saved');
    }
    cancelEdit();
    loadSettings();
  } catch (e) {
    err.innerHTML = '<div class="error">' + esc(e.message) + '</div>';
  }
}
