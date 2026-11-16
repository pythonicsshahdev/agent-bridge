'use strict';
/* a2a-bridge control plane. Plain JavaScript, no dependencies, no network except this server's /admin/api.
   Server data is only ever written with textContent / setAttribute, never as HTML. */

const SVG = 'http://www.w3.org/2000/svg';
const POLL_MS = 3000;
const KEY_STORE = 'a2a-admin-credential';

const state = {
  credential: readStored(),
  overview: null,
  graph: null,
  view: 'flow',
  selected: null,
  paused: false,
  seen: new Set(),
  timer: null,
  filters: { agent: '', status: '' },
};

function readStored() {
  try { return JSON.parse(sessionStorage.getItem(KEY_STORE) || 'null'); } catch { return null; }
}
function store(credential) {
  try {
    if (credential) sessionStorage.setItem(KEY_STORE, JSON.stringify(credential));
    else sessionStorage.removeItem(KEY_STORE);
  } catch { /* private mode: the credential just lasts for this page */ }
}

/* ---- tiny DOM helpers ---- */
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return node;
}
function svg(tag, attrs = {}, ...kids) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const kid of kids.flat()) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return node;
}
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

/* ---- API ---- */
class AuthProblem extends Error {
  constructor(kind) { super(kind); this.kind = kind; }
}
async function api(path, init = {}) {
  const c = state.credential;
  const headers = { Accept: 'application/json', ...(init.headers || {}) };
  if (c) {
    if (c.mode === 'bearer') headers.Authorization = 'Bearer ' + c.value;
    else headers[c.header || 'X-API-Key'] = c.value;
  }
  const res = await fetch('/admin/api/' + path, { ...init, headers, cache: 'no-store' });
  if (res.status === 401) throw new AuthProblem('rejected');
  if (res.status === 403) throw new AuthProblem('no-admin-scope');
  if (!res.ok) throw new Error('The server answered ' + res.status + '.');
  return res.json();
}

/* ---- sign in ---- */
function showSignIn(problem) {
  stopPolling();
  const form = el('form', { class: 'signin', novalidate: true });
  const key = el('input', { class: 'input input--code', type: 'password', id: 'cred', autocomplete: 'off', required: true });
  const header = el('input', { class: 'input input--code', id: 'hdr', value: 'X-API-Key' });
  const mode = el('select', { class: 'input', id: 'mode' },
    el('option', { value: 'key', text: 'Access key' }),
    el('option', { value: 'bearer', text: 'Bearer token from your identity provider' }));
  const notice = el('div', { 'aria-live': 'polite' });
  if (problem) notice.append(el('p', { class: 'problem', text: problem }));
  form.append(
    el('h1', { text: 'Sign in to the control plane' }),
    el('p', { class: 'signin__lead', text: 'Use a key or token that has the admin scope. It stays in this tab and is cleared when you close it.' }),
    notice,
    el('div', { class: 'field' }, el('label', { class: 'field__label', for: 'mode', text: 'Sign in with' }), mode),
    el('div', { class: 'field' }, el('label', { class: 'field__label', for: 'cred', text: 'Key or token' }), key),
    el('details', { class: 'details' }, el('summary', { text: 'Key header name' }),
      el('div', { class: 'details__body field' },
        el('label', { class: 'field__label', for: 'hdr', text: 'Header' }), header,
        el('span', { class: 'field__hint', text: 'Only change this if your configuration sets server.inbound_auth.header_name.' }))),
    el('button', { class: 'button', type: 'submit', text: 'Sign in' }));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!key.value.trim()) { showSignIn('Enter a key or token.'); return; }
    state.credential = { mode: mode.value, value: key.value.trim(), header: header.value.trim() || 'X-API-Key' };
    try { await load(); store(state.credential); start(); } catch (e) { signInFailed(e); }
  });
  mount(form);
  key.focus();
}
function signInFailed(e) {
  state.credential = null;
  if (e instanceof AuthProblem && e.kind === 'no-admin-scope') {
    showSignIn('That credential works, but it does not have the admin scope (admin:*). Ask for a client with that scope.');
  } else if (e instanceof AuthProblem) {
    showSignIn('That credential was not accepted. Check the key and the header name.');
  } else {
    showSignIn('Could not reach the control plane. ' + e.message);
  }
}

/* ---- data ---- */
async function load() {
  const [overview, graph] = await Promise.all([api('overview'), api('diagram')]);
  state.overview = overview;
  state.graph = graph;
}

/* ---- shell ---- */
const VIEWS = [
  ['flow', 'Flow'],
  ['agents', 'Agents'],
  ['access', 'Access'],
  ['traffic', 'Traffic'],
  ['check', 'Check a config'],
];

function mount(node) { document.getElementById('app').replaceChildren(node); }

function start() {
  const nav = el('nav', { class: 'rail__nav', 'aria-label': 'Control plane' },
    VIEWS.map(([id, label]) => el('button', { class: 'rail__link', type: 'button', 'data-view': id, text: label, onclick: () => go(id) })));
  const rail = el('aside', { class: 'rail' },
    el('div', { class: 'rail__brand' }, el('div', { class: 'rail__name', text: 'a2a-bridge' }), el('div', { class: 'rail__sub', text: 'Control plane, read only' })),
    nav,
    el('div', { class: 'rail__foot' }, el('div', { text: 'Version ' + state.overview.version }),
      el('button', { type: 'button', text: 'Sign out', onclick: () => { store(null); state.credential = null; showSignIn(); } })));
  mount(el('div', { class: 'shell' }, rail, el('main', { class: 'main', id: 'main', tabindex: '-1' })));
  go(state.view);
  startPolling();
}

function go(view) {
  state.view = view;
  for (const b of document.querySelectorAll('.rail__link')) {
    if (b.dataset.view === view) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  const main = document.getElementById('main');
  const render = { flow: viewFlow, agents: viewAgents, access: viewAccess, traffic: viewTraffic, check: viewCheck }[view];
  main.replaceChildren(render());
  if (view === 'traffic' || view === 'flow') pollOnce();
}

function head(title, sub) {
  return el('div', { class: 'main__head' }, el('h1', { text: title }), sub ? el('p', { class: 'main__sub', text: sub }) : null);
}
function badge(text, tone) { return el('span', { class: 'badge badge--' + tone, text }); }
function copyButton(text) {
  const b = el('button', { class: 'copy', type: 'button', text: 'Copy', 'aria-label': 'Copy ' + clip(text, 40) });
  b.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; } catch { b.textContent = 'Select and copy'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1500);
  });
  return b;
}
function findingsList(findings) {
  if (!findings.length) return el('p', { class: 'calm', text: 'Nothing to flag in this configuration.' });
  return el('ul', { class: 'findings' }, findings.map((f) => el('li', { class: 'finding finding--' + f.level },
    el('span', { class: 'finding__level', text: f.level === 'warn' ? 'Fix' : 'Note' }), f.message)));
}
function flatten(value) {
  if (value == null) return '–';
  if (Array.isArray(value)) return value.length ? value.map(flatten).join(', ') : 'none';
  if (typeof value === 'object') return Object.entries(value).filter(([, v]) => v != null).map(([k, v]) => k.replace(/_/g, ' ') + ': ' + flatten(v)).join('; ');
  return String(value);
}

/* ---- Flow ---- */
const COLUMNS = [
  ['caller', 'Callers'],
  ['proxy', 'Proxy'],
  ['agent', 'Agents'],
  ['backend', 'Backends and remote agents'],
];
const NODE_W = 210;
const NODE_H = 58;
const GAP_Y = 22;
const COL_X = [24, 364, 704, 1044];

function layout(graph) {
  const columnOf = { caller: 0, proxy: 1, agent: 2, backend: 3, remote: 3 };
  const lists = [[], [], [], []];
  for (const n of graph.nodes) lists[columnOf[n.kind]].push(n);
  const tallest = Math.max(...lists.map((l) => l.length));
  const height = Math.max(tallest * (NODE_H + GAP_Y) + 40, 220);
  const pos = {};
  lists.forEach((list, col) => {
    const used = list.length * (NODE_H + GAP_Y) - GAP_Y;
    list.forEach((n, i) => { pos[n.id] = { x: COL_X[col], y: 44 + (height - 44 - used) / 2 + i * (NODE_H + GAP_Y) }; });
  });
  return { pos, height };
}

function viewFlow() {
  const { overview, graph } = state;
  const { pos, height } = layout(graph);
  const width = COL_X[3] + NODE_W + 24;
  const canvas = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': 'How requests flow from callers through the proxy to agents and their backends' });
  COLUMNS.forEach(([, title], i) => canvas.append(svg('text', { class: 'col-title', x: COL_X[i], y: 24 }, title)));

  const wires = svg('g');
  for (const e of graph.edges) {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, mx = (x1 + x2) / 2;
    wires.append(svg('path', { class: 'wire' + (e.style === 'dashed' ? ' wire--dashed' : ''), d: `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`, 'data-edge': `${e.from}>${e.to}` }));
  }
  canvas.append(wires);

  const nodes = svg('g');
  for (const n of graph.nodes) {
    const p = pos[n.id];
    const down = n.kind === 'agent' && n.detail.live === false;
    const g = svg('g', {
      class: `node node--${n.kind}${down ? ' node--down' : ''}`, transform: `translate(${p.x} ${p.y})`, tabindex: '0', role: 'button',
      'aria-pressed': state.selected === n.id ? 'true' : 'false', 'aria-label': `${n.label}. ${n.sub}${down ? '. Not running' : ''}`, 'data-node': n.id,
    },
    svg('rect', { class: 'node__body', width: NODE_W, height: NODE_H, rx: 6 }),
    svg('rect', { class: 'node__edge', width: 6, height: NODE_H, rx: 3 }),
    svg('text', { class: 'node__label', x: 18, y: 24 }, clip(n.label, 23)),
    svg('text', { class: 'node__sub', x: 18, y: 42 }, clip(down ? 'not running: adapter unavailable' : n.sub, 31)),
    svg('title', {}, n.label + (n.sub ? ' - ' + n.sub : '')));
    const choose = () => { state.selected = n.id; go('flow'); };
    g.addEventListener('click', choose);
    g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); choose(); } });
    nodes.append(g);
  }
  canvas.append(nodes);

  const detail = state.selected ? graph.nodes.find((n) => n.id === state.selected) : null;
  const sorted = [...overview.findings].sort((a, b) => (a.level === b.level ? 0 : a.level === 'warn' ? -1 : 1));
  const warns = sorted.filter((f) => f.level === 'warn');
  return el('div', {},
    head('How requests flow', 'Callers on the left, the agents this proxy serves, and where each one sends the work. A wire lights up when a request goes through it.'),
    el('section', { 'aria-label': 'Flow map' }, el('div', { class: 'map' }, canvas),
      el('p', { class: 'map__legend', text: 'Solid wires are requests this proxy serves. Dashed wires are remote agents it reaches through the REST facade.' }),
      el('p', { class: 'map__hint', text: 'Swipe sideways to see the whole map. The Agents page lists the same information as a table.' })),
    detail ? factsPanel(detail) : el('p', { class: 'status-line', text: 'Select a box to see its details.' }),
    el('section', { class: 'section', 'aria-labelledby': 'find-h' },
      el('h2', { id: 'find-h', text: warns.length ? `${warns.length} ${warns.length === 1 ? 'thing needs' : 'things need'} attention` : 'Configuration checks' }),
      findingsList(sorted)));
}
const FACT_LABELS = { live: 'Running', slug: 'Slug', base_url: 'Public address', sign_in: 'Sign-in', facade: 'REST facade', skills: 'Skills', adapter: 'Adapter', scopes: 'May call', keys: 'Keys', target: 'Host', auth: 'Sends credentials as', type: 'Adapter type' };
const KIND_LABELS = { caller: 'Caller', proxy: 'Proxy', agent: 'Agent', backend: 'Backend', remote: 'Remote agent' };
const sentence = (k) => { const t = k.replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
function factValue(v) { return v === true ? 'Yes' : v === false ? 'No' : flatten(v); }
function factsPanel(node) {
  const rows = [['Kind', KIND_LABELS[node.kind] || node.kind], ...Object.entries(node.detail).map(([k, v]) => [FACT_LABELS[k] || sentence(k), factValue(v)])];
  return el('section', { class: 'panel', 'aria-label': 'Details for ' + node.label },
    el('h2', { class: 'panel__title', text: node.label }),
    el('dl', { class: 'facts' }, rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])));
}

/* ---- Agents ---- */
function viewAgents() {
  const { overview } = state;
  const rows = overview.agents.map((a) => el('tr', {},
    el('td', {}, el('div', { class: 'stack' }, el('strong', { text: a.name }), el('code', { text: a.slug }))),
    el('td', {}, badge(a.live ? 'Running' : 'Not running', a.live ? 'ok' : 'err')),
    el('td', {}, el('div', { class: 'stack' }, el('span', { text: a.adapter.type + (a.adapter.mode ? ' (' + a.adapter.mode + ')' : '') }),
      a.adapter.identity ? el('span', { class: 'status-line', text: a.adapter.identity === 'caller' ? 'Callers use their own Boomi token' : 'Shared service account' }) : null)),
    el('td', {}, el('code', { text: a.adapter.target || '–' })),
    el('td', {}, el('div', { class: 'chips' }, a.skills.map((s) => el('span', { class: 'chip', text: s.name })))),
    el('td', {}, el('div', { class: 'stack' },
      el('span', {}, el('code', { text: '/agents/' + a.slug + '/invoke' }), copyButton(a.card_url)),
      el('span', { class: 'status-line', text: 'Copy puts the agent card URL on your clipboard.' })))));
  const remotes = overview.remote_agents.map((r) => el('tr', {}, el('td', {}, el('code', { text: r.slug })), el('td', {}, el('code', { text: r.target || '–' })), el('td', { text: r.auth })));
  const toolHelp = 'a2a-bridge tool-spec --config agents.yaml --base-url ' + overview.public_base_url + ' --out tools/';
  return el('div', {},
    head('Agents', 'What this proxy serves, what runs it, and where the work goes.'),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('caption', { text: 'Agents served by this proxy' }),
      el('thead', {}, el('tr', {}, ['Agent', 'State', 'Runs on', 'Backend host', 'Skills', 'Entry point'].map((h) => el('th', { scope: 'col', text: h })))),
      el('tbody', {}, rows))),
    overview.remote_agents.length ? el('section', { class: 'section' }, el('h2', { text: 'Remote agents' }),
      el('div', { class: 'table-wrap' }, el('table', {}, el('caption', { text: 'External A2A agents reached through this proxy' }),
        el('thead', {}, el('tr', {}, ['Slug', 'Host', 'Sends'].map((h) => el('th', { scope: 'col', text: h })))), el('tbody', {}, remotes)))) : null,
    el('section', { class: 'section' }, el('h2', { text: 'Use an agent as a Boomi tool' }),
      el('p', { class: 'main__sub', text: 'Generate Agentstudio tool definitions. The files contain no credentials.' }),
      el('p', {}, el('code', { text: toolHelp }), copyButton(toolHelp))));
}

/* ---- Access ---- */
function viewAccess() {
  const i = state.overview.inbound;
  const when = (c) => (c.expires_at ? new Date(c.expires_at).toLocaleDateString() : 'Never');
  const clientRows = i.clients.map((c) => el('tr', {},
    el('td', {}, el('code', { text: c.id })),
    el('td', {}, el('div', { class: 'chips' }, c.scopes.map((s) => el('span', { class: 'chip', text: s })))),
    el('td', { class: 'num', text: String(c.keys) }),
    el('td', {}, c.expired ? badge('Expired ' + when(c), 'err') : c.expires_at ? badge('Expires ' + when(c), 'quiet') : badge('No expiry', 'warn'))));
  const jwt = i.jwt ? el('dl', { class: 'facts' }, [['Issuer', i.jwt.issuer], ['Audience', flatten(i.jwt.audience)], ['Algorithms', flatten(i.jwt.algorithms)], ['Keys fetched from', i.jwt.jwks_host || '–'], ['Scopes read from', i.jwt.scope_claim]]
    .flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])) : el('p', { class: 'status-line', text: 'Token sign-in is not configured.' });
  return el('div', {},
    head('Access', 'Who can call this proxy and what each caller may reach.'),
    el('dl', { class: 'facts' },
      el('dt', { text: 'Sign-in method' }), el('dd', { text: i.mode.replace(/_/g, ' ') }),
      el('dt', { text: 'Rate limit' }), el('dd', { text: i.rate_limit ? `${i.rate_limit.requests_per_minute} requests a minute per caller, burst of ${i.rate_limit.burst}` : 'Off' }),
      el('dt', { text: 'Largest request' }), el('dd', { text: Math.round(i.max_request_bytes / 1024) + ' KB' }),
      el('dt', { text: 'Public address' }), el('dd', {}, el('code', { text: state.overview.public_base_url }))),
    el('section', { class: 'section' }, el('h2', { text: 'Clients' }),
      i.clients.length ? el('div', { class: 'table-wrap' }, el('table', {}, el('caption', { text: 'Key holders and their scopes. Hashes are never shown.' }),
        el('thead', {}, el('tr', {}, ['Client', 'May call', 'Keys', 'Expiry'].map((h) => el('th', { scope: 'col', text: h })))), el('tbody', {}, clientRows)))
        : el('p', { class: 'empty', text: i.simple_keys ? `${i.simple_keys} shared key${i.simple_keys === 1 ? '' : 's'} with full access. Add scoped clients to limit what each caller can reach.` : 'No key holders are configured.' })),
    el('section', { class: 'section' }, el('h2', { text: 'Token sign-in' }), jwt));
}

/* ---- Traffic ---- */
const agentLabel = (a) => a || 'other paths';
function statusTone(code) { return code >= 500 ? 'err' : code >= 400 ? 'warn' : 'ok'; }
function viewTraffic() {
  const agentFilter = el('select', { class: 'input', id: 'f-agent', 'aria-label': 'Filter by agent' }, el('option', { value: '', text: 'All agents' }),
    ...state.overview.agents.map((a) => el('option', { value: a.slug, text: a.slug })), el('option', { value: 'admin', text: 'Control plane (admin)' }));
  const statusFilter = el('select', { class: 'input', id: 'f-status', 'aria-label': 'Filter by result' },
    el('option', { value: '', text: 'All results' }), el('option', { value: 'ok', text: 'Succeeded' }), el('option', { value: 'bad', text: 'Refused or failed' }));
  agentFilter.value = state.filters.agent; statusFilter.value = state.filters.status;
  const apply = () => { state.filters = { agent: agentFilter.value, status: statusFilter.value }; pollOnce(); };
  agentFilter.addEventListener('change', apply); statusFilter.addEventListener('change', apply);
  const pause = el('button', { class: 'button button--quiet', type: 'button', text: state.paused ? 'Resume updates' : 'Pause updates', onclick: (e) => {
    state.paused = !state.paused; e.target.textContent = state.paused ? 'Resume updates' : 'Pause updates'; if (!state.paused) pollOnce();
  } });
  return el('div', {},
    head('Traffic', 'The most recent requests this proxy has handled since it started. Credentials are never recorded.'),
    el('div', { class: 'toolbar' }, agentFilter, statusFilter, pause, el('span', { class: 'status-line', id: 'traffic-status', 'aria-live': 'polite', text: 'Loading…' })),
    el('div', { class: 'table-wrap', id: 'agent-stats' }),
    el('div', { class: 'section table-wrap', id: 'events' }));
}
function renderTraffic(events, stats) {
  const agentBox = document.getElementById('agent-stats');
  if (agentBox) {
    const rows = Object.entries(stats.agents).sort((a, b) => b[1].requests - a[1].requests).map(([slug, s]) => el('tr', {},
      el('td', {}, el('code', { text: slug === '-' ? 'other paths' : slug })), el('td', { class: 'num', text: String(s.requests) }),
      el('td', { class: 'num', text: String(s.errors) }), el('td', { class: 'num', text: s.avg_ms + ' ms' })));
    agentBox.replaceChildren(rows.length ? el('table', {}, el('caption', { text: `Last ${stats.window} requests, by agent` }),
      el('thead', {}, el('tr', {}, ['Agent', 'Requests', 'Refused or failed', 'Average time'].map((h, i) => el('th', { scope: 'col', class: i ? 'num' : null, text: h })))), el('tbody', {}, rows))
      : el('p', { class: 'empty', text: 'No requests yet. They appear here as callers use the proxy.' }));
  }
  const box = document.getElementById('events');
  if (!box) return;
  const shown = events.filter((e) => (state.filters.agent ? e.agent === state.filters.agent : e.agent !== 'admin')
    && (!state.filters.status || (state.filters.status === 'ok' ? e.status < 400 : e.status >= 400)));
  box.replaceChildren(shown.length ? el('table', {}, el('caption', { text: 'Recent requests, newest first' }),
    el('thead', {}, el('tr', {}, ['Time', 'Caller', 'Agent', 'Result', 'Time taken', 'From', 'Note'].map((h) => el('th', { scope: 'col', text: h })))),
    el('tbody', {}, shown.map((e) => el('tr', {},
      el('td', { text: new Date(e.ts).toLocaleTimeString() }), el('td', {}, el('code', { text: e.principal || 'not signed in' })),
      el('td', {}, el('code', { text: agentLabel(e.agent) })), el('td', {}, badge(String(e.status), statusTone(e.status))),
      el('td', { class: 'num', text: e.ms + ' ms' }), el('td', {}, el('code', { text: e.client || '–' })), el('td', { text: e.note || '' })))))
    : el('p', { class: 'empty', text: events.some((e) => e.agent !== 'admin') ? 'No requests match these filters.' : 'No requests yet. They appear here as callers use the proxy.' }));
  const note = document.getElementById('traffic-status');
  if (note) note.textContent = state.paused ? 'Updates paused' : 'Updated ' + new Date().toLocaleTimeString();
}

/* ---- Check a config ---- */
const STARTER = [
  '# Paste a configuration to check it. Nothing is applied: this only reports problems,',
  '# what would change compared with the running proxy, and anything risky.',
  'agents:',
  '  - slug: example',
  '    name: Example agent',
  '    adapter: {type: http_sync, url: "https://example.com/hook"}',
  '    skills: [{id: ask, name: Ask}]',
  '',
].join('\n');
function viewCheck() {
  const editor = el('textarea', { class: 'input editor', id: 'cfg', spellcheck: 'false', 'aria-describedby': 'cfg-hint' });
  editor.value = STARTER;
  const out = el('div', { class: 'result', 'aria-live': 'polite' });
  const run = el('button', { class: 'button', type: 'button', text: 'Check configuration' });
  run.addEventListener('click', async () => {
    out.replaceChildren(el('p', { class: 'status-line', text: 'Checking…' }));
    try { out.replaceChildren(...resultNodes(await api('validate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ yaml: editor.value }) }))); }
    catch (e) { out.replaceChildren(el('p', { class: 'problem', text: e instanceof AuthProblem ? 'Your session is no longer valid. Sign in again.' : e.message })); }
  });
  const save = el('button', { class: 'button button--quiet', type: 'button', text: 'Save as file' });
  save.addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([editor.value], { type: 'text/yaml' }));
    const a = el('a', { href: url, download: 'agents.yaml' }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  });
  return el('div', {},
    head('Check a configuration', 'Test a configuration before you deploy it. The running proxy is not changed.'),
    el('div', { class: 'field' }, el('label', { class: 'field__label', for: 'cfg', text: 'Configuration (YAML)' }),
      el('span', { class: 'field__hint', id: 'cfg-hint', text: 'Use ${NAME} for anything secret. Literal secrets are rejected.' }), editor),
    el('div', { class: 'toolbar' }, run, save), out);
}
function resultNodes(r) {
  if (!r.ok) {
    return [el('section', {}, el('h2', { text: `${r.errors.length} ${r.errors.length === 1 ? 'problem' : 'problems'} to fix` }),
      el('ul', { class: 'result__list' }, r.errors.map((m) => el('li', { text: m }))))];
  }
  const c = r.changes_vs_running;
  const change = (label, list) => (list.length ? el('li', { text: label + ': ' + list.join(', ') }) : null);
  const changes = [change('New agents', c.added), change('Agents that would disappear', c.removed), change('Agents on a different adapter', c.adapter_changed)].filter(Boolean);
  return [
    el('section', {}, el('h2', { text: 'This configuration is valid' }),
      el('p', { text: `${r.summary.agents.length} agent${r.summary.agents.length === 1 ? '' : 's'}, ${r.summary.remote_agents.length} remote, sign-in method ${r.summary.inbound_mode.replace(/_/g, ' ')}.` })),
    el('section', {}, el('h2', { text: 'Compared with the running proxy' }),
      changes.length ? el('ul', { class: 'result__list' }, changes) : el('p', { class: 'status-line', text: 'The same set of agents.' })),
    el('section', {}, el('h2', { text: 'Review' }), findingsList(r.findings)),
  ];
}

/* ---- live updates ---- */
async function pollOnce() {
  if (!state.credential) return;
  try {
    const [{ events }, stats] = await Promise.all([api('events?limit=200'), api('stats')]);
    pulse(events);
    if (state.view === 'traffic') renderTraffic(events, stats);
  } catch (e) {
    if (e instanceof AuthProblem) { stopPolling(); store(null); showSignIn('Your session ended. Sign in again.'); }
    else { const n = document.getElementById('traffic-status'); if (n) n.textContent = 'Could not refresh: ' + e.message; }
  }
}
function pulse(events) {
  const fresh = events.filter((e) => e.request_id && !state.seen.has(e.request_id));
  const first = state.seen.size === 0;  // do not replay history on the first load
  for (const e of events) if (e.request_id) state.seen.add(e.request_id);
  if (first || state.view !== 'flow') return;
  for (const e of fresh) {
    if (!e.agent) continue;
    const tone = e.status >= 400 ? 'wire--err' : 'wire--ok';
    for (const path of document.querySelectorAll(`[data-edge="proxy>agent:${CSS.escape(e.agent)}"], [data-edge="agent:${CSS.escape(e.agent)}>backend:${CSS.escape(e.agent)}"]`)) {
      path.classList.remove('wire--ok', 'wire--err');
      void path.getBoundingClientRect();  // restart the animation
      path.classList.add(tone);
    }
  }
}
function startPolling() { stopPolling(); state.timer = setInterval(() => { if (!state.paused && !document.hidden) pollOnce(); }, POLL_MS); }
function stopPolling() { if (state.timer) clearInterval(state.timer); state.timer = null; }

/* ---- boot ---- */
(async function boot() {
  if (!state.credential) { showSignIn(); return; }
  try { await load(); start(); } catch (e) { store(null); signInFailed(e); }
}());
