// Drives the real page in headless Chromium: loads each sample, checks the
// findings on screen, clicks Cancel, downloads the CSV, and records every
// network request the page tries to make (there should be none).
//
//   NODE_PATH=$(npm root -g) node tests/run-browser.mjs [screenshot-dir]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');
const pageUrl = pathToFileURL(path.join(root, 'index.html')).href;
const shots = process.argv[2];
const key = JSON.parse(fs.readFileSync(path.join(here, 'answer-key.json'), 'utf8'));

let failures = 0;
const check = (ok, msg) => { if (!ok) failures++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); };

const browser = await chromium.launch(fs.existsSync('/opt/pw-browsers/chromium') ? {} : {});
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1100, height: 900 } });
const requests = [];
context.on('request', r => { if (!r.url().startsWith('file:') && !r.url().startsWith('blob:') && !r.url().startsWith('data:')) requests.push(r.url()); });
const errors = [];

for (const [file, exp] of Object.entries(key)) {
  if (file.startsWith('_')) continue;
  console.log(`\n=== ${file} ===`);
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(`${file}: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`${file}: ${m.text()}`); });
  await page.goto(pageUrl);
  await page.setInputFiles('#file', path.join(root, 'samples', file));

  await page.waitForSelector('#columns:not([hidden]), #results:not([hidden]), #error:not([hidden])');
  const onColumns = await page.isVisible('#columns');
  check(onColumns === exp.expectColumnStep, `column check step ${onColumns ? 'shown' : 'not shown'} (expected ${exp.expectColumnStep ? 'shown' : 'not shown'})`);
  if (onColumns) {
    const preview = await page.locator('#preview tbody tr').count();
    check(preview > 0, `preview shows ${preview} rows before confirming`);
    if (shots) await page.screenshot({ path: path.join(shots, 'columns-step.png'), fullPage: true });
    await page.click('#columns-go');
  }
  await page.waitForSelector('#results:not([hidden])');

  const cards = await page.$$eval('#findings > li', lis => lis.map(li => ({
    id: li.dataset.id,
    name: li.querySelector('h3').textContent,
    chips: [...li.querySelectorAll('.chip')].map(c => c.textContent),
    cost: li.querySelector('.f-cost strong').textContent
  })));
  const stopped = await page.$$eval('#stopped li', lis => lis.map(li => li.textContent));
  const expectedActive = exp.findings.filter(f => f.status !== 'stopped');
  const expectedStopped = exp.findings.filter(f => f.status === 'stopped');
  check(cards.length === expectedActive.length, `${cards.length} findings on screen (expected ${expectedActive.length})`);
  check(stopped.length === expectedStopped.length, `${stopped.length} stopped charges listed (expected ${expectedStopped.length})`);
  for (const x of expectedActive) {
    const hit = cards.find(c => c.name.toLowerCase().includes(x.merchant) && (x.type === 'duplicate') === c.chips.some(ch => ch.startsWith('Charged')));
    check(!!hit, `card for "${x.merchant}" (${x.type})${hit ? ': ' + hit.chips.join(', ') + ', ' + hit.cost : ''}`);
    if (hit && x.priceRise) check(hit.chips.includes('Price went up'), `"${x.merchant}" shows the price rise`);
  }
  const costs = cards.map(c => parseFloat(c.cost.replace(/[^\d.]/g, '')));
  check(costs.every((v, i) => i === 0 || v <= costs[i - 1]), 'findings sorted by yearly cost, highest first');

  // Mark the two biggest items Cancel and check the live savings total
  const save0 = await page.textContent('#save-total');
  await page.locator('#findings > li').nth(0).locator('button.cancel').click();
  await page.locator('#findings > li').nth(1).locator('button.cancel').click();
  const save2 = await page.textContent('#save-total');
  const want = costs[0] + costs[1];
  const got = parseFloat(save2.replace(/[^\d.]/g, ''));
  check(Math.abs(got - want) < 0.02, `savings total goes from ${save0} to ${save2} after 2 cancels (expected ${want.toFixed(2)})`);
  await page.locator('#findings > li').nth(1).locator('button.keep').click();
  const save1 = parseFloat((await page.textContent('#save-total')).replace(/[^\d.]/g, ''));
  check(Math.abs(save1 - costs[0]) < 0.02, `switching one to Keep drops the total to ${save1.toFixed(2)}`);

  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#download')]);
  const csv = fs.readFileSync(await dl.path(), 'utf8');
  const lines = csv.trim().split(/\r?\n/);
  check(lines[0].includes('Merchant') && lines.length >= cards.length + 5, `CSV download "${dl.suggestedFilename()}" has ${lines.length} lines`);
  check(csv.includes('Cancel') && csv.includes('Keep'), 'CSV includes the Keep/Cancel choices');

  if (shots && file === 'uk-bank-statement.csv') {
    await page.screenshot({ path: path.join(shots, 'results-desktop.png'), fullPage: false });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(shots, 'results-phone.png'), fullPage: false });
  }
  await page.close();
}

// The example button and an unreadable file
{
  console.log('\n=== Example and error handling ===');
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(`example: ${e.message}`));
  await page.goto(pageUrl);
  if (shots) await page.screenshot({ path: path.join(shots, 'start-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  if (shots) await page.screenshot({ path: path.join(shots, 'start-phone.png'), fullPage: false });
  await page.click('#example');
  await page.waitForSelector('#results:not([hidden])');
  check(await page.isVisible('#example-banner'), 'example loads and says it is made-up data');
  const w = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  check(w, 'no sideways scrolling at phone width');
  await page.click('#restart');
  await page.setInputFiles('#file', { name: 'statement.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 fake') });
  await page.waitForSelector('#error:not([hidden])');
  check((await page.textContent('#error')).includes('PDF'), 'a PDF gets a plain explanation');
  await page.setInputFiles('#file', { name: 'old.xls', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]) });
  await page.waitForFunction(() => document.getElementById('error').textContent.includes('.xls'));
  check(true, 'an old .xls file gets a plain explanation');
  await page.close();
}

check(errors.length === 0, `no script errors${errors.length ? ': ' + errors.join(' | ') : ''}`);
check(requests.length === 0, `network requests made by the page: ${requests.length}${requests.length ? ' ' + requests.join(', ') : ''}`);
await browser.close();
console.log(`\n${failures === 0 ? 'ALL BROWSER CHECKS PASSED' : failures + ' BROWSER CHECK(S) FAILED'}`);
process.exit(failures ? 1 : 0);
