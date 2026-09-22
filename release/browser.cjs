// Drive the installed frontend against its real API (CI tooling only).
const { chromium } = require('../apps/web/node_modules/@playwright/test');
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = page.waitForResponse(r => r.url().endsWith('/api/config'));
    await page.goto('http://127.0.0.1:8000');
    const config = await response;
    if (!config.ok() || !(await config.json()).workflows.length) {
      throw new Error('Frontend did not receive the bundled workflow from the API');
    }
    await page.waitForFunction(() => document.querySelector('#root')?.innerText.trim());
    if (errors.length) throw new Error(errors.join('\n'));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
