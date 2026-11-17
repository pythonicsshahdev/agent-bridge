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

const REDUCED_MOTION = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
function motionOn() {
  if (REDUCED_MOTION) return false;
  try { return sessionStorage.getItem('a2a-motion') !== 'off'; } catch { return true; }
}
function setMotion(on) {
  try { sessionStorage.setItem('a2a-motion', on ? 'on' : 'off'); } catch { /* the choice just lasts for this page */ }
}

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
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;  // an absent optional child draws nothing, not the word "null"
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
function svg(tag, attrs = {}, ...kids) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;  // an absent optional child draws nothing, not the word "null"
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

/* ---- icons: stroke glyphs on a 24x24 grid, built as DOM (never as HTML strings) ---- */
const circ = (cx, cy, r) => `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;
const ICONS = {
  flow: [circ(5, 12, 2.3), circ(19, 6, 2.3), circ(19, 18, 2.3), 'M7.2 11.1L16.8 6.9', 'M7.2 12.9L16.8 17.1'],
  agents: ['M12 3l9 5-9 5-9-5 9-5z', 'M3 12.5l9 5 9-5', 'M3 17l9 5 9-5'],
  add: [circ(12, 12, 9), 'M12 8v8', 'M8 12h8'],
  access: ['M12 3l8 3v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6z', 'M9 12l2 2 4-4'],
  traffic: ['M3 12h4l3-8 4 16 3-8h4'],
  check: ['M9 4h6a1 1 0 0 1 1 1v1H8V5a1 1 0 0 1 1-1z', 'M8 6H6a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-2', 'M9 14l2 2 4-4'],
  user: [circ(12, 8, 3.6), 'M5 20c0-3.9 3.1-6 7-6s7 2.1 7 6'],
  hub: [circ(12, 12, 3), 'M12 3v4', 'M12 17v4', 'M3.5 7.5l3.6 2', 'M16.9 14.5l3.6 2', 'M3.5 16.5l3.6-2', 'M16.9 9.5l3.6-2'],
  bolt: ['M13 2L4 14h7l-1 8 9-12h-7z'],
  database: ['M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z', 'M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6', 'M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6'],
  globe: [circ(12, 12, 9), 'M3 12h18', 'M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9', 'M12 3c-2.5 2.7-3.8 5.7-3.8 9s1.3 6.3 3.8 9'],
  warn: ['M12 3l10 18H2z', 'M12 10v5', 'M12 18.2v.1'],
  info: [circ(12, 12, 9), 'M12 11v6', 'M12 7.3v.1'],
};
function glyph(name, props = {}) {
  return svg('g', props, ICONS[name].map((d) => svg('path', { d, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })));
}
function icon(name, size = 18) { return svg('svg', { viewBox: '0 0 24 24', width: size, height: size, 'aria-hidden': 'true', focusable: 'false' }, glyph(name)); }
function logo(size = 30, extra = '') {
  const lines = ['M7 7.5L16.5 9.5', 'M16.5 9.5L12.5 20', 'M7 7.5L12.5 20'];
  return svg('svg', { class: extra, viewBox: '0 0 26 26', width: size, height: size, 'aria-hidden': 'true', focusable: 'false' },
    svg('defs', {}, svg('linearGradient', { id: 'logo-g', x1: 0, y1: 0, x2: 1, y2: 1 }, svg('stop', { offset: '0%', 'stop-color': '#ff9a4d' }), svg('stop', { offset: '100%', 'stop-color': '#ff5a0a' }))),
    lines.map((d) => svg('path', { d, fill: 'none', stroke: '#22d3c5', 'stroke-width': '1.4', 'stroke-opacity': '0.8' })),
    svg('circle', { cx: 7, cy: 7.5, r: 4, fill: 'url(#logo-g)' }),
    svg('circle', { cx: 17, cy: 9.5, r: 3.2, fill: '#22d3c5' }),
    svg('circle', { cx: 12.5, cy: 20, r: 3.6, fill: '#60a5fa' }));
}

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
    el('div', { class: 'signin__brand' }, logo(36), el('span', { text: 'a2a-bridge' })),
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
  mount(el('div', { class: 'signin__wrap' }, form));
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
  ['flow', 'Flow', 'flow'],
  ['agents', 'Agents', 'agents'],
  ['add', 'Add an agent', 'add'],
  ['access', 'Access', 'access'],
  ['traffic', 'Traffic', 'traffic'],
  ['check', 'Check a config', 'check'],
];

function mount(node) { document.getElementById('app').replaceChildren(node); }

function start() {
  const nav = el('nav', { class: 'rail__nav', 'aria-label': 'Control plane' },
    VIEWS.map(([id, label, ic]) => el('button', { class: 'rail__link', type: 'button', 'data-view': id, onclick: () => go(id) }, icon(ic, 18), el('span', { text: label }))));
  const rail = el('aside', { class: 'rail' },
    el('div', { class: 'rail__brand' }, logo(32, 'rail__logo'), el('div', {}, el('div', { class: 'rail__name', text: 'a2a-bridge' }), el('div', { class: 'rail__sub', text: 'Control plane, read only' }))),
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
  const render = { flow: viewFlow, agents: viewAgents, add: viewAdd, access: viewAccess, traffic: viewTraffic, check: viewCheck }[view];
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
    el('span', { class: 'finding__icon' }, icon(f.level === 'warn' ? 'warn' : 'info', 18)),
    el('span', {}, el('span', { class: 'finding__level', text: f.level === 'warn' ? 'Fix' : 'Note' }), f.message))));
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
const NODE_W = 224;
const NODE_H = 66;
const GAP_Y = 22;
const COL_X = [20, 338, 656, 974];
const KIND_ICON = { caller: 'user', proxy: 'hub', agent: 'bolt', backend: 'database', remote: 'globe' };

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

function uptimeText(seconds) {
  if (seconds < 90) return `${seconds} seconds`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 90) return `${minutes} minutes`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} hours` : `${Math.floor(hours / 24)} days`;
}
function statusStrip() {
  const o = state.overview;
  const live = o.agents.filter((a) => a.live).length;
  const pill = (...kids) => el('span', { class: 'pill' }, kids);
  return el('div', { class: 'strip', role: 'group', 'aria-label': 'Status' },
    pill(el('span', { class: 'dot', 'aria-hidden': 'true' }), el('strong', { text: 'Running' }), ' for ' + uptimeText(o.uptime_seconds)),
    pill(el('strong', { text: String(live) }), live === 1 ? ' agent live' : ' agents live'),
    o.remote_agents.length ? pill(el('strong', { text: String(o.remote_agents.length) }), o.remote_agents.length === 1 ? ' remote agent' : ' remote agents') : null,
    pill(el('strong', { id: 'pill-requests', text: state.stats ? String(state.stats.window) : '–' }), ' recent requests'));
}

function viewFlow() {
  const { overview, graph } = state;
  const { pos, height } = layout(graph);
  const width = COL_X[3] + NODE_W + 24;
  const canvas = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': 'How requests flow from callers through the proxy to agents and their backends' });
  canvas.append(svg('defs', {},
    svg('linearGradient', { id: 'nodefill', x1: 0, y1: 0, x2: 0, y2: 1 }, svg('stop', { offset: '0%', 'stop-color': '#1b2c50' }), svg('stop', { offset: '100%', 'stop-color': '#101b34' })),
    svg('linearGradient', { id: 'wire-grad', gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0, x2: width, y2: 0 }, svg('stop', { offset: '0%', 'stop-color': '#4f9dff' }), svg('stop', { offset: '100%', 'stop-color': '#2ee6c5' })),
    svg('linearGradient', { id: 'wire-grad-remote', gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0, x2: width, y2: 0 }, svg('stop', { offset: '0%', 'stop-color': '#a78bfa' }), svg('stop', { offset: '100%', 'stop-color': '#f0abfc' }))));
  COLUMNS.forEach(([, title], i) => canvas.append(svg('text', { class: 'col-title', x: COL_X[i], y: 24 }, title)));

  const wires = svg('g');
  for (const e of graph.edges) {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, mx = (x1 + x2) / 2;
    wires.append(svg('path', { class: 'wire' + (e.style === 'dashed' ? ' wire--dashed' : ''), d: `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`, stroke: e.style === 'dashed' ? 'url(#wire-grad-remote)' : 'url(#wire-grad)', 'data-edge': `${e.from}>${e.to}` }));
  }
  canvas.append(wires);

  const nodes = svg('g');
  for (const n of graph.nodes) {
    const p = pos[n.id];
    const down = n.kind === 'agent' && n.detail.live === false;
    const running = n.kind === 'proxy' || (n.kind === 'agent' && !down);  // we know these are up; callers and backends we do not
    const g = svg('g', {
      class: `node node--${n.kind}${down ? ' node--down' : ''}`, transform: `translate(${p.x} ${p.y})`, tabindex: '0', role: 'button',
      'aria-pressed': state.selected === n.id ? 'true' : 'false', 'aria-label': `${n.label}. ${n.sub}${down ? '. Not running' : ''}`, 'data-node': n.id,
    },
    running ? svg('rect', { class: `node__halo halo-d${nodes.childNodes.length % 5}`, width: NODE_W, height: NODE_H, rx: 12, 'aria-hidden': 'true' }) : null,
    svg('rect', { class: 'node__body', width: NODE_W, height: NODE_H, rx: 12, fill: 'url(#nodefill)' }),
    svg('circle', { class: 'node__badge', cx: 32, cy: NODE_H / 2, r: 17 }),
    glyph(KIND_ICON[n.kind], { class: 'node__glyph', transform: `translate(${32 - 10} ${NODE_H / 2 - 10}) scale(0.83)` }),
    svg('text', { class: 'node__label', x: 58, y: 28 }, clip(n.label, 19)),
    svg('text', { class: 'node__sub', x: 58, y: 47 }, clip(down ? 'not running: adapter unavailable' : n.sub, 24)),
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
    statusStrip(),
    el('section', { 'aria-label': 'Flow map' }, el('div', { class: 'map', 'data-motion': motionOn() ? 'on' : 'off', id: 'flow-map' }, canvas),
      el('div', { class: 'map__bar' },
        el('p', { class: 'map__legend', text: 'A pulsing ring means running here. Moving dashes show the direction of requests, and a bright flash means a request just went through. Dashed wires reach remote agents through the REST facade.' }),
        motionButton()),
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
function motionButton() {
  const label = (on) => (on ? 'Pause animation' : 'Play animation');
  const b = el('button', { class: 'button button--quiet', type: 'button', text: label(motionOn()), 'aria-pressed': String(motionOn()), disabled: REDUCED_MOTION });
  if (REDUCED_MOTION) {
    b.textContent = 'Animation off';
    b.title = 'Your system asks for reduced motion.';
    return b;
  }
  b.addEventListener('click', () => {
    const on = !motionOn();
    setMotion(on);
    document.getElementById('flow-map').dataset.motion = on ? 'on' : 'off';
    b.textContent = label(on);
    b.setAttribute('aria-pressed', String(on));
  });
  return b;
}

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

/* ---- Add an agent: a form that produces the exact YAML to deploy ---- */
const REGIONS = [
  ['US', 'https://c01-usa-east.ai-agent-garden.boomi.com'],
  ['UK', 'https://c01-gbr.ai-agent-garden.boomi.com'],
  ['Australia and New Zealand', 'https://c01-aus.ai-agent-garden.boomi.com'],
  ['Japan', 'https://c01-jp.ai-agent-garden.boomi.com'],
  ['Another address', ''],
];
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const slugify = (t) => t.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);

function viewAdd() {
  const form = el('form', { class: 'builder__form', novalidate: true, 'aria-label': 'New agent' });
  let slugTouched = false;
  const id = (n) => 'f-' + n;
  const input = (n, props = {}) => el('input', { class: 'input', id: id(n), autocomplete: 'off', ...props });
  const select = (n, options, value) => { const s = el('select', { class: 'input', id: id(n) }, options.map(([v, t]) => el('option', { value: v, text: t }))); if (value) s.value = value; return s; };
  const field = (n, label, control, hint) => el('div', { class: 'field' }, el('label', { class: 'field__label', for: id(n), text: label }), hint ? el('span', { class: 'field__hint', id: id(n) + '-hint', text: hint }) : null, control);
  const group = (key, ...kids) => el('div', { class: 'group', 'data-for': key, hidden: true }, kids);
  const authFields = (p) => [
    field(p + '-auth', 'Credentials sent to the backend', select(p + '-auth', [['none', 'None'], ['bearer_passthrough', "Forward the caller's bearer token"], ['static_header', 'A fixed header from an environment variable']], 'bearer_passthrough')),
    el('div', { class: 'group', 'data-auth': p, hidden: true },
      field(p + '-header', 'Header name', input(p + '-header', { value: 'X-API-Key' })),
      field(p + '-env', 'Environment variable holding the value', input(p + '-env', { placeholder: 'BACKEND_API_KEY', class: 'input input--code' }), 'Only the variable name goes in the file, never the secret.')),
  ];

  const skills = el('div', { class: 'skills' });
  const addSkill = (name = '', description = '') => {
    const row = el('div', { class: 'skill-row' },
      el('input', { class: 'input', 'aria-label': 'Skill name', placeholder: 'What it can do, e.g. Look up an invoice', value: name }),
      el('input', { class: 'input', 'aria-label': 'Skill description (optional)', placeholder: 'Description (optional)', value: description }),
      el('button', { class: 'button button--quiet', type: 'button', text: 'Remove', 'aria-label': 'Remove this skill', onclick: () => { if (skills.children.length > 1) { row.remove(); schedule(); } } }));
    skills.append(row);
  };
  addSkill();

  const typeRadios = el('fieldset', { class: 'choice' }, el('legend', { text: 'What does this agent run on?' }),
    [['http_sync', 'A REST call', 'One HTTP request, answered straight away.'], ['http_async', 'A long-running job', 'Start it, then check on it until it finishes.'], ['agentstudio', 'A Boomi Agentstudio agent', 'A deployed Boomi agent, including multi-turn conversations.'], ['remote', 'An agent that already speaks A2A', 'Google ADK, LangGraph, CrewAI, another a2a-bridge, or any A2A server. Reached through the REST facade.']]
      .map(([v, t, h], i) => el('label', { class: 'choice__item' }, el('input', { type: 'radio', name: 'f-type', value: v, checked: i === 0 }), el('span', {}, el('strong', { text: t }), el('span', { class: 'field__hint', text: h })))));

  form.append(
    field('name', 'Name', input('name', { placeholder: 'Invoice lookup', required: true })),
    field('slug', 'Address name (slug)', input('slug', { placeholder: 'invoice-lookup', class: 'input input--code' }), 'Used in the address: /agents/<slug>. Lowercase letters, numbers and hyphens.'),
    el('div', { 'data-hide-for': 'remote' }, field('desc', 'Description', input('desc', { placeholder: 'What this agent does, in one sentence' }))),
    typeRadios,
    group('http_sync',
      field('s-url', 'Backend address', input('s-url', { placeholder: 'https://erp.example.com/invoices', type: 'url' })),
      field('s-method', 'HTTP method', select('s-method', ['POST', 'GET', 'PUT', 'PATCH', 'DELETE'].map((m) => [m, m]), 'POST')),
      authFields('s'),
      el('details', { class: 'details' }, el('summary', { text: 'My service expects a specific body (LangServe, CrewAI, n8n, …)' }), el('div', { class: 'details__body' },
        field('s-template', 'Request body', el('textarea', { class: 'input input--code', id: id('s-template'), rows: '5', spellcheck: 'false', placeholder: '{"input": "{{text}}", "config": {"configurable": {"thread_id": "{{context_id}}"}}}' }), 'JSON. Fill in {{text}}, {{context_id}}, {{task_id}} or {{data}}. Leave empty to send the message as it is.'),
        field('s-textpath', 'Where the reply text is in the response', input('s-textpath', { placeholder: 'output', class: 'input input--code' }), 'A dotted path such as output.content. Leave empty to return the whole response.')))),
    group('remote',
      field('r-card', 'Agent card address', input('r-card', { placeholder: 'https://agent.example.com/.well-known/agent-card.json', type: 'url' }), 'Must be https unless you are testing locally.'),
      field('r-auth', 'Credentials sent to that agent', select('r-auth', [['none', 'None'], ['bearer_passthrough', "Forward the caller's bearer token"], ['static_header', 'A fixed header from an environment variable'], ['oauth_client_credentials', 'OAuth client credentials']], 'none')),
      el('div', { class: 'group', 'data-rauth': 'static_header', hidden: true },
        field('r-header', 'Header name', input('r-header', { value: 'Authorization' })),
        field('r-env', 'Environment variable holding the value', input('r-env', { placeholder: 'REMOTE_AGENT_KEY', class: 'input input--code' }), 'Only the variable name goes in the file, never the secret.')),
      el('div', { class: 'group', 'data-rauth': 'oauth_client_credentials', hidden: true },
        field('r-token', 'Token address', input('r-token', { placeholder: 'https://login.example.com/oauth/token', type: 'url' })),
        field('r-cid', 'Client id', input('r-cid', { class: 'input input--code' })),
        field('r-secret', 'Environment variable holding the client secret', input('r-secret', { placeholder: 'REMOTE_CLIENT_SECRET', class: 'input input--code' }), 'Only the variable name goes in the file.'),
        field('r-scope', 'Scope (optional)', input('r-scope')))),
    group('http_async',
      field('a-url', 'Address that starts the job', input('a-url', { placeholder: 'https://boomi.example.com/ws/rest/export/start', type: 'url' })),
      field('a-status', 'Address that reports status', input('a-status', { placeholder: 'https://boomi.example.com/ws/rest/export/status/{job_id}', class: 'input input--code' }), 'Must contain {job_id}.'),
      field('a-jobpath', 'Where the job id is in the start reply', input('a-jobpath', { value: 'executionId', class: 'input input--code' })),
      field('a-statuspath', 'Where the status is in each status reply', input('a-statuspath', { value: 'status', class: 'input input--code' })),
      field('a-done', 'Statuses that mean finished', input('a-done', { value: 'COMPLETE' }), 'Comma separated.'),
      field('a-fail', 'Statuses that mean failed', input('a-fail', { value: 'ERROR, ABORTED' }), 'Comma separated.'),
      authFields('a'),
      el('details', { class: 'details' }, el('summary', { text: 'More options' }), el('div', { class: 'details__body' },
        field('a-result', 'Where the result is in the final reply', input('a-result', { placeholder: 'result', class: 'input input--code' }), 'Leave empty to return the whole reply.'),
        field('a-cancel', 'Address that stops the job', input('a-cancel', { placeholder: 'https://boomi.example.com/ws/rest/export/cancel/{job_id}', class: 'input input--code' }), 'Called when a caller cancels or the job takes too long.'),
        field('a-poll', 'Check every (seconds)', input('a-poll', { type: 'number', value: '2', min: '0.1', step: '0.1' })),
        field('a-max', 'Give up after (seconds)', input('a-max', { type: 'number', value: '300', min: '1' }))))),
    group('agentstudio',
      field('g-region', 'Boomi region', select('g-region', REGIONS.map(([t, u]) => [u || 'custom', t]), REGIONS[0][1])),
      el('div', { class: 'group', 'data-region': 'custom', hidden: true }, field('g-url', 'Execute address', input('g-url', { placeholder: 'https://…', type: 'url' }))),
      field('g-account', 'Boomi account id', input('g-account')),
      field('g-deploy', 'Deployment id', input('g-deploy', { class: 'input input--code' }), 'From a production deployment of the agent.'),
      field('g-mode', 'The agent takes', select('g-mode', [['conversational', 'Free text (a conversation)'], ['structured', 'One JSON object']], 'conversational')),
      field('g-identity', 'Who calls Boomi', select('g-identity', [['caller', 'Each caller uses their own Boomi token'], ['caller_fallback', 'Each caller, with a shared account as a fallback'], ['service_account', 'One shared service account']], 'caller'), 'Using each caller\'s own token keeps Boomi access control and audit per person.'),
      el('div', { class: 'group', 'data-identity': 'service', hidden: true },
        field('g-user', 'Environment variable holding the Boomi username', input('g-user', { placeholder: 'BOOMI_USERNAME', class: 'input input--code' })),
        field('g-token', 'Environment variable holding the API token', input('g-token', { placeholder: 'BOOMI_API_TOKEN', class: 'input input--code' }), 'Only the variable name goes in the file.')),
      field('g-blocked', "The agent's guardrail message (optional)", input('g-blocked', { placeholder: "I can't help with that request." }), 'When the agent replies with exactly this, the task is reported as rejected.')),
    el('div', { class: 'field', 'data-hide-for': 'remote' }, el('span', { class: 'field__label', id: 'skills-label', text: 'Skills' }), el('span', { class: 'field__hint', text: 'What callers can ask this agent for. Add at least one.' }), skills,
      el('button', { class: 'button button--quiet', type: 'button', text: 'Add a skill', onclick: () => { addSkill(); schedule(); } })));

  const val = (n) => (document.getElementById(id(n)) || { value: '' }).value.trim();
  const authOf = (p) => {
    const mode = val(p + '-auth');
    if (mode === 'static_header') return { mode, header_name: val(p + '-header') || 'X-API-Key', value: ENV_NAME.test(val(p + '-env')) ? '${' + val(p + '-env') + '}' : '' };
    return { mode };
  };
  let localProblem = '';
  const list = (t) => t.split(',').map((x) => x.trim()).filter(Boolean);
  function collect() {
    const type = form.querySelector('input[name=f-type]:checked').value;
    localProblem = '';
    if (type === 'remote') {
      const mode = val('r-auth');
      const auth = { mode };
      if (mode === 'static_header') { auth.header_name = val('r-header') || 'Authorization'; auth.value = ENV_NAME.test(val('r-env')) ? '${' + val('r-env') + '}' : ''; }
      if (mode === 'oauth_client_credentials') {
        Object.assign(auth, { token_url: val('r-token'), client_id: val('r-cid'), client_secret: ENV_NAME.test(val('r-secret')) ? '${' + val('r-secret') + '}' : '' });
        if (val('r-scope')) auth.scope = val('r-scope');
      }
      const remote = { slug: val('slug'), card_url: val('r-card'), auth };
      if (remote.card_url.startsWith('http://')) remote.allow_insecure_http = true;
      return { remote };
    }
    const agent = { slug: val('slug'), name: val('name') };
    if (val('desc')) agent.description = val('desc');
    let adapter;
    if (type === 'http_sync') {
      adapter = { type, url: val('s-url'), method: val('s-method'), auth: authOf('s') };
      if (val('s-template')) {
        try { adapter.request_template = JSON.parse(val('s-template')); } catch { localProblem = 'The request body is not valid JSON.'; }
      }
      if (val('s-textpath')) adapter.response_text_path = val('s-textpath');
    } else if (type === 'http_async') {
      adapter = { type, url: val('a-url'), status_url: val('a-status'), job_id_path: val('a-jobpath') || 'executionId', status_path: val('a-statuspath') || 'status', completed_values: list(val('a-done')), failed_values: list(val('a-fail')), poll_interval_seconds: Number(val('a-poll')) || 2, max_poll_seconds: Number(val('a-max')) || 300, auth: authOf('a') };
      if (val('a-result')) adapter.result_path = val('a-result');
      if (val('a-cancel')) adapter.cancel_url = val('a-cancel');
    } else {
      const region = val('g-region');
      adapter = { type, deployment_id: val('g-deploy'), execute_url: region === 'custom' ? val('g-url') : region, account_id: val('g-account'), mode: val('g-mode') };
      const who = val('g-identity');
      if (who === 'service_account') adapter.identity = 'service_account';
      if (who === 'caller_fallback') adapter.allow_service_account_fallback = true;
      if (who !== 'caller') {
        if (ENV_NAME.test(val('g-user'))) adapter.username = '${' + val('g-user') + '}';
        if (ENV_NAME.test(val('g-token'))) adapter.api_token = '${' + val('g-token') + '}';
      }
      if (val('g-blocked')) adapter.blocked_message = val('g-blocked');
    }
    agent.adapter = adapter;
    const used = new Set();
    agent.skills = [...skills.querySelectorAll('.skill-row')].map((row, i) => {
      const [n, d] = row.querySelectorAll('input');
      let sid = slugify(n.value) || 'skill-' + (i + 1);
      while (used.has(sid)) sid += '-' + (i + 1);
      used.add(sid);
      const skill = { id: sid, name: n.value.trim() };
      if (d.value.trim()) skill.description = d.value.trim();
      return skill;
    }).filter((sk) => sk.name);
    return { agent };
  }

  const status = el('p', { class: 'status-line', id: 'pv-status', 'aria-live': 'polite', text: 'Fill in the form and the configuration appears here.' });
  const yamlBox = el('pre', { class: 'code', id: 'pv-yaml', tabindex: '0', 'aria-label': 'Generated configuration' });
  const details = el('div', { id: 'pv-details', class: 'result' });
  const copy = el('button', { class: 'button', type: 'button', text: 'Copy configuration', disabled: true });
  const save = el('button', { class: 'button button--quiet', type: 'button', text: 'Save as file', disabled: true });
  let latest = null;
  copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(latest.yaml); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select the text and copy it'; } setTimeout(() => { copy.textContent = 'Copy configuration'; }, 1800); });
  save.addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([latest.yaml], { type: 'text/yaml' }));
    const a = el('a', { href: url, download: `agent-${latest.slug}.yaml` }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  });

  let ticket = 0;
  let timer = null;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(check, 350); };
  async function check() {
    const mine = ++ticket;
    const { agent, remote } = collect();
    const named = remote ? remote.slug : (agent.name || agent.slug);
    if (!named) { status.textContent = 'Fill in the form and the configuration appears here.'; yamlBox.textContent = ''; details.replaceChildren(); latest = null; copy.disabled = save.disabled = true; return; }
    if (localProblem) { status.textContent = localProblem; yamlBox.textContent = ''; details.replaceChildren(); latest = null; copy.disabled = save.disabled = true; return; }
    status.textContent = 'Checking…';
    let r;
    try { r = await api('agent/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(remote ? { remote_agent: remote } : { agent }) }); }
    catch (e) { if (mine === ticket) status.textContent = e instanceof AuthProblem ? 'Your session ended. Sign in again.' : 'Could not check: ' + e.message; return; }
    if (mine !== ticket) return;  // a newer edit is already being checked
    const parts = [];
    latest = r.ok ? r : null;
    copy.disabled = save.disabled = !r.ok;
    if (!r.ok) {
      status.textContent = `${r.errors.length} ${r.errors.length === 1 ? 'problem' : 'problems'} to fix before this can be used.`;
      yamlBox.textContent = '';
      parts.push(el('section', {}, el('ul', { class: 'result__list' }, r.errors.map((m) => el('li', { text: m })))));
    } else {
      status.textContent = r.slug_taken ? 'This is valid, but the address name is already in use on this proxy.' : 'Valid. This is the configuration to add.';
      yamlBox.textContent = r.yaml;
      if (r.slug_taken) parts.push(el('p', { class: 'problem', text: `An agent or remote agent named "${r.slug}" already exists here. Pick a different slug, or the configuration will be rejected.` }));
      if (r.env.length) parts.push(el('section', {}, el('h3', { text: 'Environment variables this needs' }), el('ul', { class: 'result__list' }, r.env.map((e) => el('li', {}, el('code', { text: e.name }), e.set ? ' is set on this proxy.' : ' is not set on this proxy. Set it wherever the agent will run.')))));
      if (r.findings.length) parts.push(el('section', {}, el('h3', { text: 'Review' }), findingsList(r.findings)));
      const cmd = `a2a-bridge tool-spec --config agents.yaml --agent ${r.slug} --out tools/`;
      parts.push(el('section', {}, el('h3', { text: 'To use it' }),
        el('ol', { class: 'steps' },
          el('li', { text: (r.kind === 'remote_agent' ? 'Add the configuration above under remote_agents: in your agents.yaml.' : 'Add the configuration above under agents: in your agents.yaml.') }),
          el('li', {}, 'Give each client that should call it the scope ', el('code', { text: 'agents:' + r.slug }), '.'),
          el('li', { text: 'Restart or redeploy the proxy.' }),
          el('li', {}, 'Optional: make it a tool for Boomi agents with ', el('code', { text: cmd }), '.'))));
    }
    details.replaceChildren(...parts);
  }

  const syncVisibility = () => {
    const type = form.querySelector('input[name=f-type]:checked').value;
    for (const g of form.querySelectorAll('[data-for]')) g.hidden = g.dataset.for !== type;
    for (const g of form.querySelectorAll('[data-auth]')) g.hidden = val(g.dataset.auth + '-auth') !== 'static_header';
    const region = document.getElementById(id('g-region'));
    for (const g of form.querySelectorAll('[data-region]')) g.hidden = !(region && region.value === 'custom');
    for (const g of form.querySelectorAll('[data-hide-for]')) g.hidden = g.dataset.hideFor === type;
    for (const g of form.querySelectorAll('[data-rauth]')) g.hidden = val('r-auth') !== g.dataset.rauth;
    for (const g of form.querySelectorAll('[data-identity]')) g.hidden = val('g-identity') === 'caller';
  };
  form.addEventListener('input', (e) => {
    if (e.target.id === id('slug')) slugTouched = true;
    if (e.target.id === id('name') && !slugTouched) document.getElementById(id('slug')).value = slugify(e.target.value);
    syncVisibility(); schedule();
  });
  form.addEventListener('change', () => { syncVisibility(); schedule(); });
  form.addEventListener('submit', (e) => e.preventDefault());
  queueMicrotask(syncVisibility);

  return el('div', {},
    head('Add an agent', 'Describe the agent and get the exact configuration to deploy. Nothing is changed on this proxy: you add the result to your configuration and redeploy.'),
    el('div', { class: 'builder' }, form,
      el('section', { class: 'builder__out', 'aria-label': 'Configuration' }, el('h2', { text: 'Configuration' }), status, yamlBox, el('div', { class: 'toolbar' }, copy, save), details)));
}

/* ---- live updates ---- */
async function pollOnce() {
  if (!state.credential) return;
  try {
    const [{ events }, stats] = await Promise.all([api('events?limit=200'), api('stats')]);
    state.stats = stats;
    const requests = document.getElementById('pill-requests');
    if (requests) requests.textContent = String(stats.window);
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
      path.addEventListener('animationend', (ev) => { if (ev.animationName.startsWith('pulse-')) path.classList.remove('wire--ok', 'wire--err'); }, { once: true });
    }
    for (const id of [`agent:${e.agent}`, `backend:${e.agent}`]) {
      const box = document.querySelector(`[data-node="${CSS.escape(id)}"]`);
      if (!box) continue;
      box.classList.remove('node--hit', 'node--hit-err');
      void box.getBoundingClientRect();
      box.classList.add(e.status >= 400 ? 'node--hit-err' : 'node--hit');
      box.addEventListener('animationend', (ev) => { if (ev.animationName.startsWith('hit-')) box.classList.remove('node--hit', 'node--hit-err'); }, { once: true, capture: true });
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
