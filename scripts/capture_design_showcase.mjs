// Capture the standalone design specimen with a real browser renderer.
// Set PLAYWRIGHT_PACKAGE to an installed Playwright module if it is external.
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright'));
} catch {
  throw new Error('Install Playwright or set PLAYWRIGHT_PACKAGE to its module path.');
}

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const specimen = path.join(root, 'design/component-showcase.html');
const output = path.join(root, 'docs/assets/theme-showcase');
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: 'light',
    locale: 'en-GB',
  });
  for (const theme of ['harbour', 'ember', 'field']) {
    for (const mode of ['light', 'dark']) {
      const url = new URL(pathToFileURL(specimen));
      url.searchParams.set('theme', theme);
      url.searchParams.set('mode', mode);
      await page.goto(url.href);
      await page.evaluate(() => document.fonts.ready);
      const actual = await page.locator('html').evaluate((element) => ({
        theme: element.dataset.themeFamily,
        mode: element.dataset.mode,
      }));
      if (actual.theme !== theme || actual.mode !== mode) {
        throw new Error(`Unexpected specimen state: ${JSON.stringify(actual)}`);
      }
      await page.screenshot({ path: path.join(output, `${theme}-${mode}.png`), fullPage: true });
      if (mode === 'light') {
        await page.locator('#components').screenshot({ path: path.join(output, `${theme}-detail.png`) });
      }
    }
  }
  await page.close();
} finally {
  await browser.close();
}
