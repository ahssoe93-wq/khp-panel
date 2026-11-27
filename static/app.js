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
async function loadDashboard() {
  try {
    const j = await api('/api/servers');
    SERVERS = j.servers;
    renderServers();
    const h = await api('/api/health');
    HEALTH = {};
    h.health.forEach(x => HEALTH[x.id] = x);
    renderServers();
    document.getElementById('stat-servers').textContent =
      h.health.filter(x => x.online).length + ' / ' + h.health.length;
    document.getElementById('stat-keys').textContent =
      h.health.reduce((a, x) => a + (x.clients || 0), 0);
  } catch (e) {
    document.getElementById('server-list').innerHTML =
      '<div class="error">Could not load servers: ' + esc(e.message) + '</div>';
  }
  try {
    const j = await api('/api/expiring?days=7');
    document.getElementById('stat-expiring').textContent = j.results.length;
    renderExpiring(j.results);
  } catch (e) {
    document.getElementById('expiring').innerHTML =
      '<div class="error">Could not load expiring keys.</div>';
  }
}
function renderServers() {
  const el = document.getElementById('server-list');
  const f = (document.getElementById('server-filter').value || '').toLowerCase();
  const list = SERVERS.filter(s => s.name.toLowerCase().includes(f) ||
    (s.country || '').toLowerCase().includes(f));
  if (!list.length) { el.innerHTML = '<div class="empty">No servers yet — add one in Settings.</div>'; return; }
  el.innerHTML = list.map(s => {
    const h = HEALTH[s.id];
    const dot = h ? (h.online ? 'on' : 'off') : '';
    const sub = h
      ? (h.online ? (s.country + ' • ' + h.clients + ' clients') : ('Offline — ' + esc(h.error || 'unreachable')))
      : (s.country + ' • checking…');
    const CC = {singapore: 'SG', thailand: 'TH', japan: 'JP'};
    const flag = CC[(s.country || '').toLowerCase()] || (s.country || '??').slice(0, 2).toUpperCase();
    return '<div class="server" onclick="location.href=\'/server/' + s.id + '\'">' +
      '<div class="flag">' + esc(flag) + '</div>' +
      '<div class="grow"><b style="font-size:18px">' + esc(s.name) +
      (s.is_free ? '<span class="badge-free">FREE</span>' : '') + '</b>' +
      '<div class="muted small">' + esc(sub) + '</div></div>' +
      '<span class="dot ' + dot + '"></span></div>';
  }).join('');
}
function filterServers() { renderServers(); }
function renderExpiring(results) {
  const el = document.getElementById('expiring');
  if (!results.length) { el.innerHTML = '<div class="empty">Nothing expiring in the next 7 days.</div>'; return; }
  el.innerHTML = results.map(c =>
    '<div class="card client"><div class="row"><div class="grow">' +
    '<div class="name">' + esc(c.name) + '</div>' +
    '<div class="muted small">' + esc(c.server_name) + ' • expires ' + esc(c.expiry_h) +
    ' • ' + c.days_left + ' day' + (c.days_left === 1 ? '' : 's') + ' left</div></div>' +
    '<span class="status ' + c.status + '">' + c.status + '</span></div></div>'
  ).join('');
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
async function loadServerPage() {
  try {
    const h = await api('/api/health');
    const me = h.health.find(x => x.id === SERVER_ID);
    document.getElementById('srv-status').textContent =
      me ? (me.online ? me.clients + ' clients' : 'offline — ' + (me.error || '')) : 'unknown';
    document.getElementById('srv-dot').className = 'dot ' + (me && me.online ? 'on' : 'off');
  } catch (e) { /* health is best-effort */ }
  try {
    const j = await api('/api/server/' + SERVER_ID + '/clients');
    ALL_CLIENTS = j.clients;
    renderClients();
  } catch (e) {
    document.getElementById('client-list').innerHTML =
      '<div class="error">Could not load keys: ' + esc(e.message) + '</div>';
  }
}
function renderClients() {
  const el = document.getElementById('client-list');
  const f = (document.getElementById('client-filter').value || '').toLowerCase();
  const list = ALL_CLIENTS.filter(c => c.name.toLowerCase().includes(f));
  if (!list.length) { el.innerHTML = '<div class="empty">No keys found.</div>'; return; }
  el.innerHTML = list.map(c =>
    '<div class="card client">' +
    '<div class="row"><div class="grow"><div class="name">' + esc(c.name) + '</div>' +
    '<div class="muted small">' + esc(c.used_h) + ' / ' + esc(c.limit_h) + ' • exp ' + esc(c.expiry_h) + '</div>' +
    '<div class="bar"><i style="width:' + c.pct + '%"></i></div></div>' +
    '<span class="status ' + c.status + '">' + c.status + '</span></div>' +
    '<div class="row" style="margin-top:12px">' +
    '<button class="btn ghost" onclick="copyLink(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\', this)">Copy link</button>' +
    '<button class="btn ghost" style="color:var(--red);border-color:rgba(239,68,68,.5)" onclick="deleteKey(' + c.server_id + ', \'' + esc(c.name).replace(/'/g, "\\'") + '\')">Delete</button>' +
    '</div></div>'
  ).join('');
}
function filterClients() { renderClients(); }
async function copyLink(serverId, name, btn) {
  btn.disabled = true;
  try {
    const j = await api('/api/server/' + serverId + '/link/' + encodeURIComponent(name));
    await copyText(j.link, 'Link copied');
  } catch (e) { toast('Failed: ' + e.message); }
  btn.disabled = false;
}
async function deleteKey(serverId, name) {
  if (!confirm('Delete key ' + name + '?\nThis cannot be undone. The customer loses access at once.')) return;
  try {
    await api('/api/server/' + serverId + '/delete-key', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})});
    toast('Deleted ' + name);
    loadServerPage();
  } catch (e) { toast('Delete failed: ' + e.message); }
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
  const name = document.getElementById('add-name').value.trim();
  const months = parseInt(document.querySelector('#add-months button.sel').dataset.m, 10);
  const btn = document.getElementById('add-submit');
  if (!name) { toast('Enter a customer name'); return; }
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    const j = await api('/api/server/' + SERVER_ID + '/add-key', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, months})});
    document.getElementById('add-result-name').textContent =
      j.name + '  •  ' + j.months + ' mo • ' + j.gb + ' GB • exp ' + j.expiry;
    document.getElementById('add-result-link').textContent = j.link;
    document.getElementById('add-form').style.display = 'none';
    document.getElementById('add-result').style.display = '';
    loadServerPage();
  } catch (e) { toast('Create failed: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Create key';
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  try {
    const j = await api('/api/servers');
    SERVERS = j.servers; // editServer() reads from this global
    const el = document.getElementById('settings-list');
    if (!j.servers.length) { el.innerHTML = '<div class="empty">No servers yet.</div>'; return; }
    el.innerHTML = j.servers.map(s =>
      '<div class="card"><div class="row"><div class="grow">' +
      '<b style="font-size:17px">' + esc(s.name) + '</b> ' +
      '<span class="muted small">' + esc(s.type === 'xui' ? '3X-UI' : 'Marzban') + ' • ' + esc(s.country || '') + '</span>' +
      '<div class="muted small">' + esc(s.base_url) + '</div></div></div>' +
      '<div class="row" style="margin-top:12px">' +
      '<button class="btn ghost" onclick="editServer(' + s.id + ')">Edit</button>' +
      '<button class="btn ghost" onclick="testServer(' + s.id + ', this)">Test connection</button>' +
      '<button class="btn ghost" style="color:var(--red);border-color:rgba(239,68,68,.5)" onclick="removeServer(' + s.id + ', \'' + esc(s.name) + '\', this)">Remove</button>' +
      '</div><div class="muted small" id="test-' + s.id + '" style="margin-top:8px"></div></div>'
    ).join('');
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
