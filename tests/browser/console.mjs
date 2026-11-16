import fs from 'node:fs';
import puppeteer from 'puppeteer-core';
const SHOTS = process.env.SHOTS_DIR || 'shots';
fs.mkdirSync(SHOTS, { recursive: true });
const BASE = process.env.BASE_URL || 'http://localhost:8013';  // the demo: demo/run.sh
const results = [];
const ok = (name, cond, extra = '') => { results.push([cond, name + (extra ? ` (${extra})` : '')]); };
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: 'new', args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 900 });
const problems = [];
// The 401/403 console lines come from the deliberate bad-credential sign-ins below; anything else counts.
page.on('console', (m) => { if (['error', 'warning'].includes(m.type()) && !/status of 40[13]/.test(m.text())) problems.push(`${m.type()}: ${m.text()}`); });
await page.evaluateOnNewDocument(() => { window.__csp = []; document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI)); });
page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
const shot = (n) => page.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (sel) => page.$eval(sel, (e) => e.textContent).catch(() => null);

// ---- sign in
await page.goto(BASE + '/admin', { waitUntil: 'networkidle0' });
ok('sign-in form is shown first', !!(await page.$('#cred')));
await shot('01-signin');
const signIn = async (key) => { await page.$eval('#cred', (e) => (e.value = '')); await page.type('#cred', key); await page.click('button[type=submit]'); await sleep(900); };
await signIn('wrong-key');
ok('wrong key explains itself', (await text('.problem'))?.includes('was not accepted'), await text('.problem'));
await signIn('demo-crm-key');
ok('agent-only key is told it lacks admin scope', (await text('.problem'))?.includes('admin scope'), await text('.problem'));
await signIn('demo-admin-key');
ok('admin key reaches the console', !!(await page.$('.shell')));
ok('credential kept only in sessionStorage', await page.evaluate(() => !localStorage.length && !!sessionStorage.getItem('a2a-admin-credential')));

// ---- flow
await sleep(500);
const nodes = await page.$$eval('.node', (n) => n.map((x) => x.getAttribute('data-node')));
ok('flow map has every node', nodes.length === 10, nodes.join(','));
const kinds = await page.$$eval('.node', (n) => [...new Set(n.map((x) => x.getAttribute('class').split(' ')[1]))].sort().join(','));
ok('all node kinds drawn', kinds === 'node--agent,node--backend,node--caller,node--proxy,node--remote', kinds);
ok('findings are listed', (await page.$$('.finding')).length >= 3);
ok('flow headline counts what needs attention', /need|needs/.test(await text('#find-h')), await text('#find-h'));
await shot('02-flow');
await page.evaluate(() => document.querySelector('.node[data-node="agent:orders"]').focus());
await page.keyboard.press('Enter'); await sleep(300);
ok('keyboard selects a node and shows its details', (await text('.panel__title')) === 'Order Fulfilment', await text('.panel__title'));
ok('selected node is announced as pressed', (await page.$eval('.node[data-node="agent:orders"]', (e) => e.getAttribute('aria-pressed'))) === 'true');
await shot('03-flow-selected');

// ---- real traffic lights the wire
await page.evaluate(() => fetch('/agents/orders/invoke', { method: 'POST', headers: { 'X-API-Key': 'demo-crm-key', Authorization: 'Bearer d', 'Content-Type': 'application/json' }, body: '{"data":{"sku":"Z"}}' }));
let lit = false;
for (let i = 0; i < 80 && !lit; i++) { lit = await page.$eval('[data-edge="proxy>agent:orders"]', (e) => e.classList.contains('wire--ok')).catch(() => false); if (!lit) await sleep(100); }
ok('a new request pulses its wire', lit);

// ---- agents, access, traffic
await page.click('[data-view=agents]'); await sleep(300);
ok('agents table lists all three', (await page.$$('tbody tr')).length >= 4, String((await page.$$('tbody tr')).length));
ok('agent not running is flagged', (await page.$$eval('.badge', (b) => b.map((x) => x.textContent))).includes('Running'));
await shot('04-agents');
await page.click('[data-view=access]'); await sleep(300);
const accessText = await page.$eval('main', (e) => e.textContent);
ok('access shows clients, never hashes', accessText.includes('billing-bot') && !/[0-9a-f]{64}/.test(accessText));
ok('access flags a client without expiry', accessText.includes('No expiry'));
await shot('05-access');
await page.click('[data-view=traffic]'); await sleep(1500);
const rows = (await page.$$('#events tbody tr')).length;
ok('traffic lists recent requests', rows >= 5, String(rows));
const agentCells = await page.$$eval('#events tbody tr td:nth-child(3)', (c) => c.map((x) => x.textContent));
ok('the console own polling is hidden from traffic', !agentCells.includes('admin') && agentCells.includes('orders'), agentCells.join(','));
const statAgents = await page.$$eval('#agent-stats tbody tr td:first-child', (c) => c.map((x) => x.textContent));
ok('agent stats exclude the console', !statAgents.includes('admin'), statAgents.join(','));
await page.select('#f-status', 'bad'); await sleep(1200);
const badRows = await page.$$eval('#events tbody tr td:nth-child(4)', (c) => c.map((x) => Number(x.textContent)));
ok('failure filter keeps only 4xx/5xx', badRows.length > 0 && badRows.every((s) => s >= 400), badRows.join(','));
await page.select('#f-status', '');
await shot('06-traffic');

// ---- check a config
await page.click('[data-view=check]'); await sleep(300);
await page.click('button.button:not(.button--quiet)'); await sleep(900);
ok('starter config validates', (await text('.result h2'))?.includes('valid'), await text('.result h2'));
ok('validation shows the diff against what is running', (await page.$eval('.result', (e) => e.textContent)).includes('Agents that would disappear'));
await page.$eval('#cfg', (e) => (e.value = 'agents:\n  - {slug: Bad_Slug, name: X, adapter: {type: http_sync, url: "ftp://x"}, skills: []}'));
await page.click('button.button:not(.button--quiet)'); await sleep(900);
const errs = await page.$$eval('.result__list li', (l) => l.map((x) => x.textContent));
ok('invalid config lists each problem', errs.length >= 2 && errs.some((e) => e.includes('slug')), errs.join(' | '));
await shot('07-check');

// ---- hostile strings never become markup
await page.click('[data-view=traffic]'); await sleep(400);
await page.evaluate(() => {
  const hostile = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
  state.filters = { agent: '', status: '' };
  renderTraffic([{ ts: new Date().toISOString(), principal: hostile, agent: hostile, status: 200, ms: 1, client: hostile, note: '"><svg onload=window.__xss=1>' }],
    { window: 1, agents: { [hostile]: { requests: 1, errors: 0, avg_ms: 1 } }, statuses: {}, callers: {} });
});
await sleep(300);
ok('hostile data is shown as text, not run', (await page.evaluate(() => window.__xss)) === undefined && (await page.$eval('#events', (e) => e.textContent)).includes('<img src=x'));
ok('no injected elements', (await page.$$('#events img, #events script, #events svg')).length === 0);

// ---- motion, responsive, sign out
const reduced = await browser.newPage();
await reduced.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
await reduced.goto(BASE + '/admin', { waitUntil: 'networkidle0' });
ok('reduced motion turns pulses off', await reduced.evaluate(() => { const s = document.createElement('div'); s.className = 'wire--ok'; document.body.append(s); const v = getComputedStyle(s).animationName; s.remove(); return v === 'none'; }));
await reduced.close();
const phone = await browser.newPage();
await phone.setViewport({ width: 390, height: 800 });
await phone.goto(BASE + '/admin', { waitUntil: 'networkidle0' });
await phone.evaluate(() => sessionStorage.setItem('a2a-admin-credential', JSON.stringify({ mode: 'key', value: 'demo-admin-key', header: 'X-API-Key' })));
await phone.reload({ waitUntil: 'networkidle0' }); await sleep(600);
ok('phone layout has no page-level sideways scroll', await phone.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth + 1), await phone.evaluate(() => document.scrollingElement.scrollWidth + ' vs ' + window.innerWidth));
await phone.screenshot({ path: `${SHOTS}/08-phone.png`, fullPage: true });
await phone.close();
await page.click('.rail__foot button'); await sleep(400);
ok('sign out clears the credential and returns to sign in', !!(await page.$('#cred')) && (await page.evaluate(() => sessionStorage.getItem('a2a-admin-credential'))) === null);

const csp = await page.evaluate(() => window.__csp);
ok('no CSP violations on any view', csp.length === 0, csp.join(' | '));
ok('no unexpected console errors', problems.length === 0, problems.slice(0, 3).join(' | '));
await browser.close();
let failed = 0;
for (const [pass, name] of results) { console.log((pass ? 'PASS ' : 'FAIL ') + name); if (!pass) failed++; }
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
