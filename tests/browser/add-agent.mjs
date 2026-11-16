import fs from 'node:fs';
import puppeteer from 'puppeteer-core';
const SHOTS = process.env.SHOTS_DIR || 'shots';
fs.mkdirSync(SHOTS, { recursive: true });
const BASE = process.env.BASE_URL || 'http://localhost:8013';  // the demo: demo/run.sh
const results = [];
const ok = (name, cond, extra = '') => results.push([!!cond, name + (extra ? ` (${extra})` : '')]);
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: 'new', args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 1000 });
const problems = [];
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(m.text()); });
page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
await page.evaluateOnNewDocument(() => { window.__csp = []; document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective)); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const set = async (sel, text) => { await page.$eval(sel, (e) => { e.value = ''; e.dispatchEvent(new Event('input', { bubbles: true })); }); if (text) await page.type(sel, text); };
const pick = async (sel, value) => { try { await page.select(sel, value); } catch (e) { console.log('PICK FAILED', sel, '| url:', page.url(), '| text:', (await page.evaluate(() => document.body.innerText)).slice(0, 300).replace(/\n/g, ' / '), '| selects:', await page.$$eval('select', (x) => x.map((y) => y.id))); throw e; } await sleep(50); };
const yamlText = () => page.$eval('#pv-yaml', (e) => e.textContent);
const status = () => page.$eval('#pv-status', (e) => e.textContent);
const settle = () => sleep(1100);
const safeText = (sel) => page.$eval(sel, (e) => e.textContent).catch(() => null);

await page.goto(BASE + '/admin', { waitUntil: 'networkidle0' });
await page.type('#cred', 'demo-admin-key'); await page.click('button[type=submit]'); await sleep(900);
await page.click('[data-view=add]'); await sleep(400);
ok('the Add an agent page opens', (await page.$eval('h1', (e) => e.textContent)) === 'Add an agent');

// ---- REST agent
await page.type('#f-name', 'Invoice lookup');
ok('the slug follows the name', (await page.$eval('#f-slug', (e) => e.value)) === 'invoice-lookup');
await page.type('#f-s-url', 'https://erp.example.com/invoices');
await page.type('.skill-row input', 'Look up an invoice');
await settle();
ok('a valid REST agent is confirmed', (await status()).startsWith('Valid'), await status());
let y = await yamlText();
ok('the yaml has the slug, adapter and skill', y.includes('slug: invoice-lookup') && y.includes('type: http_sync') && y.includes('id: look-up-an-invoice'), y.split('\n').slice(0, 4).join(' | '));
ok('copy and save are enabled when valid', !(await page.$eval('.builder__out .button', (e) => e.disabled)));
await page.screenshot({ path: `${SHOTS}/add-1-rest.png`, fullPage: true });

// ---- error state
await set('#f-s-url', 'ftp://nope'); await settle();
ok('a bad address lists the problem', (await status()).includes('problem') && (await page.$$eval('.result__list li', (l) => l.some((x) => x.textContent.includes('http')))));
ok('copy is disabled while there are problems', await page.$eval('.builder__out .button', (e) => e.disabled));
ok('no yaml is shown for an invalid agent', (await yamlText()) === '');
await set('#f-s-url', 'https://erp.example.com/invoices'); await settle();

// ---- slug already used
await set('#f-slug', 'orders'); await settle();
const taken = await safeText('.problem');
ok('a slug already in use is called out', taken && taken.includes('already exists'), taken === null ? 'no .problem; status=' + (await status()) + ' details=' + (await safeText('#pv-details')) : '');
await set('#f-slug', 'invoice-lookup'); await settle();

// ---- long-running job
await page.click('input[name=f-type][value=http_async]'); await sleep(200);
ok('REST fields hide and job fields show', !(await page.$eval('#f-s-url', (e) => e.offsetParent !== null)) && (await page.$eval('#f-a-status', (e) => e.offsetParent !== null)));
await page.type('#f-a-url', 'https://boomi.example.com/ws/rest/export/start');
await page.type('#f-a-status', 'https://boomi.example.com/ws/rest/export/status/{job_id}');
await pick('#f-a-auth', 'static_header');
ok('the env var field appears for a fixed header', await page.$eval('#f-a-env', (e) => e.offsetParent !== null));
await page.type('#f-a-env', 'EXPORT_KEY'); await settle();
y = await yamlText();
ok('the job config references the env var, not a secret', (await status()).startsWith('Valid') && y.includes('${EXPORT_KEY}') && y.includes('type: http_async'), await status());
const envLine = await page.$$eval('#pv-details li', (l) => l.map((x) => x.textContent).join(' | '));
ok('it says whether the variable is set here', envLine.includes('EXPORT_KEY') && envLine.includes('not set'), envLine);
await set('#f-a-env', 'not a valid name!'); await settle();
ok('an invalid variable name is rejected, not written', !(await yamlText()).includes('not a valid'));
await set('#f-a-env', 'EXPORT_KEY'); await settle();
await page.screenshot({ path: `${SHOTS}/add-2-job.png`, fullPage: true });

// ---- Agentstudio
await page.click('input[name=f-type][value=agentstudio]'); await sleep(200);
await page.type('#f-g-account', 'acct-123'); await page.type('#f-g-deploy', '11111111-1111-1111-1111-111111111111'); await settle();
y = await yamlText();
ok('Agentstudio defaults to each caller using their own token', (await status()).startsWith('Valid') && y.includes('type: agentstudio') && !y.includes('service_account') && y.includes('c01-usa-east'), await status());
await pick('#f-g-region', 'https://c01-gbr.ai-agent-garden.boomi.com'); await settle();
ok('choosing a region changes the address', (await yamlText()).includes('c01-gbr'));
await pick('#f-g-identity', 'service_account');
ok('service account asks for env var names', await page.$eval('#f-g-token', (e) => e.offsetParent !== null));
await page.type('#f-g-user', 'BOOMI_USERNAME'); await page.type('#f-g-token', 'BOOMI_API_TOKEN'); await settle();
y = await yamlText();
ok('the shared account is referenced by variable name only', y.includes('identity: service_account') && y.includes('${BOOMI_API_TOKEN}') && y.includes('${BOOMI_USERNAME}'));
await pick('#f-g-identity', 'caller'); await settle();
ok('switching back removes the service account', !(await yamlText()).includes('service_account'));
await page.screenshot({ path: `${SHOTS}/add-3-agentstudio.png`, fullPage: true });

// ---- skills
await page.click('.builder__form .button--quiet:not([aria-label])'); await sleep(200);
ok('a second skill row can be added', (await page.$$('.skill-row')).length === 2);
await page.click('.skill-row .button--quiet'); await sleep(200);
ok('a skill row can be removed', (await page.$$('.skill-row')).length === 1);
await page.click('.skill-row .button--quiet'); await sleep(200);
ok('the last skill row cannot be removed', (await page.$$('.skill-row')).length === 1);

// ---- hostile input stays text
await set('#f-name', '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'); await settle();
ok('markup in a name never executes', (await page.evaluate(() => window.__xss)) === undefined && (await page.$$('#pv-yaml img, #pv-details img, #pv-yaml script, #pv-details script')).length === 0);

// ---- accessibility basics
const unlabeled = await page.$$eval('.builder__form input:not([type=radio]), .builder__form select', (els) => els.filter((e) => !e.labels.length && !e.getAttribute('aria-label')).map((e) => e.id || e.name));
ok('every field has a label', unlabeled.length === 0, unlabeled.join(','));
ok('the radios are in a labelled group', await page.$eval('fieldset.choice legend', (e) => e.textContent.length > 5));
ok('the live status is announced politely', (await page.$eval('#pv-status', (e) => e.getAttribute('aria-live'))) === 'polite');

// ---- no stray placeholder text on any page
const stray = [];
for (const view of ['flow', 'agents', 'add', 'access', 'traffic', 'check']) {
  await page.click(`[data-view=${view}]`); await sleep(500);
  const bad = await page.evaluate(() => { const t = document.querySelector('main').innerText; return ['null', 'undefined', '[object', 'NaN'].filter((w) => new RegExp('\\b' + w.replace('[', '\\[') + '\\b|' + w.replace('[', '\\[')).test(t)); });
  if (bad.length) stray.push(view + ':' + bad.join('/'));
}
ok('no page shows stray null, undefined, [object or NaN text', stray.length === 0, stray.join(' ; '));

// ---- phone
const phone = await browser.newPage(); await phone.setViewport({ width: 390, height: 800 });
await phone.goto(BASE + '/admin', { waitUntil: 'networkidle0' });
await phone.evaluate(() => sessionStorage.setItem('a2a-admin-credential', JSON.stringify({ mode: 'key', value: 'demo-admin-key', header: 'X-API-Key' })));
await phone.reload({ waitUntil: 'networkidle0' }); await sleep(500);
await phone.evaluate(() => document.querySelector('[data-view=add]').click()); await sleep(500);
ok('the phone layout has no sideways scroll', await phone.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth + 1), await phone.evaluate(() => document.scrollingElement.scrollWidth + ' vs ' + window.innerWidth));
await phone.screenshot({ path: `${SHOTS}/add-4-phone.png`, fullPage: true });

const csp = await page.evaluate(() => window.__csp);
ok('no CSP violations', csp.length === 0, csp.join(','));
ok('no unexpected console errors', problems.filter((p) => !/status of 40[13]/.test(p)).length === 0, problems.slice(0, 2).join(' | '));
await browser.close();
let failed = 0;
for (const [pass, name] of results) { console.log((pass ? 'PASS ' : 'FAIL ') + name); if (!pass) failed++; }
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
