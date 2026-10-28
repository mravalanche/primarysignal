// Real, isolated browser phases for the disposable publication exercise.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { setTimeout as delay } from 'node:timers/promises';

const require = createRequire(import.meta.url);
const PHASES = new Set(['publish', 'published', 'suppress', 'suppressed']);
const COOKIE = '__Host-primary_signal_admin';

function configuration() {
  const input = readFileSync(0);
  assert(input.length <= 8192);
  const config = JSON.parse(input.toString('utf8'));
  assert(PHASES.has(config.phase));
  const publicURL = new URL(config.publicOrigin);
  const adminURL = new URL(config.adminOrigin);
  assert.equal(publicURL.protocol, 'http:');
  assert.equal(publicURL.hostname, '127.0.0.1');
  assert.equal(adminURL.protocol, 'https:');
  assert.equal(adminURL.hostname, 'localhost');
  for (const [url, origin] of [[publicURL, config.publicOrigin], [adminURL, config.adminOrigin]]) {
    assert.equal(url.origin, origin);
    assert(url.port && !url.username && !url.password);
  }
  assert.equal(config.slug, 'synthetic-security-notice');
  assert.equal(config.headline, 'Synthetic security notice');
  for (const key of ['adminPassword', 'rawMarker', 'publishReason', 'suppressReason']) {
    assert.equal(typeof config[key], 'string');
    assert(config[key].length > 0 && config[key].length <= 1024);
  }
  return config;
}

async function visit(page, url, expectedStatus = 200) {
  const deadline = Date.now() + 30000;
  do {
    try {
      const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 5000 });
      assert.equal(response?.status(), expectedStatus);
      return response;
    } catch {
      if (Date.now() >= deadline) throw new Error('Local publication endpoint unavailable');
      await delay(250);
    }
  } while (true);
}

async function login(context, config) {
  const page = await context.newPage();
  const loginPage = await visit(page, `${config.adminOrigin}/admin/login`);
  assert.equal(loginPage.headers()['referrer-policy'], 'same-origin');
  await page.getByLabel('Password', { exact: true }).fill(config.adminPassword);
  await Promise.all([
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === config.adminOrigin && url.pathname === '/admin/login'
        && response.request().method() === 'POST';
    }).then((response) => {
      assert.equal(response.status(), 303);
      assert.equal(response.request().headers().origin, config.adminOrigin);
    }),
    page.waitForURL((url) => url.origin === config.adminOrigin && url.pathname === '/admin/desk'),
    page.getByRole('button', { name: 'Sign in', exact: true }).click(),
  ]);
  const cookies = await context.cookies(config.adminOrigin);
  const session = cookies.find((cookie) => cookie.name === COOKIE);
  assert(session?.secure && session.httpOnly && session.sameSite === 'Strict');
  assert.equal(session.path, '/');
  assert.equal(session.domain, 'localhost');
  assert(!(await context.cookies(config.publicOrigin)).some((cookie) => cookie.name === COOKIE));
  await visit(page, `${config.adminOrigin}/admin/desk/stories/${config.slug}`);
  await page.locator('#desk-story-title').waitFor();
  assert.equal(await page.locator('#desk-story-title').innerText(), config.headline);
  const recordedLineage = page.locator('.ps-desk-lineage');
  await recordedLineage.locator('summary').click();
  const lineage = await recordedLineage.innerText();
  assert(lineage.includes('Article') && lineage.includes('Content version'));
  assert(!lineage.includes('Not recorded'));
  return page;
}

async function decide(page, config, kind) {
  let form;
  if (kind === 'publish') {
    const details = page.locator('details.ps-desk-decision-fallback.is-publish');
    await details.locator('summary').click();
    form = details.locator('form');
    assert.match(await form.locator('[name=evidence_fingerprint]').inputValue(), /^[0-9a-f]{64}$/);
    assert(await form.locator('[name=candidate_revision_id]').inputValue());
  } else {
    await page.locator('[data-decision-dialog=decision-suppress-dialog]').click();
    const dialog = page.locator('#decision-suppress-dialog');
    await dialog.waitFor({ state: 'visible' });
    form = dialog.locator('form');
    assert(await form.locator('[name=expected_current_revision_id]').inputValue());
  }
  assert(await form.locator('[name=csrf_token]').inputValue());
  await form.locator('[name=reason]').fill(kind === 'publish' ? config.publishReason : config.suppressReason);
  const endpoint = `/admin/desk/stories/${config.slug}/${kind}`;
  const [response] = await Promise.all([
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === config.adminOrigin && url.pathname === endpoint
        && response.request().method() === 'POST';
    }),
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    form.getByRole('button', {
      name: kind === 'publish' ? 'Confirm publication' : 'Confirm suppression', exact: true,
    }).click(),
  ]);
  assert.equal(response.status(), 303);
  assert.equal(response.request().headers().origin, config.adminOrigin);
  assert(new URLSearchParams(response.request().postData()).get('csrf_token'));
}

async function verifyPublic(context, config, published) {
  const page = await context.newPage();
  await visit(page, `${config.publicOrigin}/`);
  assert.equal(await page.locator(`a[href="/stories/${config.slug}"]`).count() > 0, published);
  assert(!(await page.content()).includes(config.rawMarker));
  await visit(page, `${config.publicOrigin}/stories/${config.slug}`, published ? 200 : 404);
  const html = await page.content();
  for (const forbidden of [config.rawMarker, config.publishReason, config.suppressReason]) {
    assert(!html.includes(forbidden));
  }
  if (published) {
    await page.getByRole('heading', { name: config.headline, exact: true }).waitFor();
    const source = page.locator('a[href="https://public.example/synthetic-security-notice"]');
    assert.equal(await source.count(), 1);
    assert((await source.innerText()).includes('Synthetic security notice'));
    const relation = await source.getAttribute('rel');
    assert(relation?.includes('noopener') && relation.includes('noreferrer'));
  }
  for (const hidden of ['/admin/login', '/admin/desk', '/docs', '/openapi.json', '/v1/ready']) {
    await visit(page, `${config.publicOrigin}${hidden}`, 404);
  }
  await page.close();
}

async function main() {
  const config = configuration();
  assert.equal(require('playwright/package.json').version, '1.64.0');
  const { chromium } = require('playwright');
  const browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath() });
  let context;
  let timeout;
  try {
    context = await browser.newContext({
      ignoreHTTPSErrors: true,
      javaScriptEnabled: config.phase !== 'publish',
      serviceWorkers: 'block',
      acceptDownloads: false,
    });
    context.setDefaultTimeout(10000);
    context.setDefaultNavigationTimeout(15000);
    const allowed = new Set([config.publicOrigin, config.adminOrigin]);
    let blocked = 0;
    await context.route('**/*', async (route) => {
      if (allowed.has(new URL(route.request().url()).origin)) await route.continue();
      else { blocked += 1; await route.abort('blockedbyclient'); }
    });
    await context.routeWebSocket('**/*', (socket) => { blocked += 1; socket.close(); });
    await Promise.race([
      (async () => {
        if (config.phase === 'publish') await verifyPublic(context, config, false);
        const admin = await login(context, config);
        if (config.phase === 'publish') await decide(admin, config, 'publish');
        if (config.phase === 'suppress') await decide(admin, config, 'suppress');
        const suppressed = config.phase === 'suppress' || config.phase === 'suppressed';
        const expectedState = suppressed ? 'Suppressed' : 'Published';
        assert.equal((await admin.locator('.ps-desk-detail-head .ps-desk-state').innerText()).trim(), expectedState);
        const decisions = await admin.locator('section[aria-labelledby=desk-events-title]').innerText();
        assert(decisions.includes('Published') && decisions.includes(config.publishReason));
        if (suppressed) assert(decisions.includes('Suppressed') && decisions.includes(config.suppressReason));
        await verifyPublic(context, config, !suppressed);
        assert.equal(blocked, 0);
      })(),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Browser phase timeout')), 60000); }),
    ]);
  } finally {
    clearTimeout(timeout);
    if (context) await context.close();
    await browser.close();
  }
  process.stdout.write(JSON.stringify({ phase: config.phase, passed: true }) + '\n');
}

main().catch(() => {
  process.stderr.write('Synthetic publication browser phase failed.\n');
  process.exitCode = 1;
});
