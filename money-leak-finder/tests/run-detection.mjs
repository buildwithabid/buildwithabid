// Runs the detection engine from index.html on the sample statements and
// compares what it finds with tests/answer-key.json.
//
//   node tests/run-detection.mjs
//
// Exit code 0 means every check passed.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const engineSrc = html.match(/<script id="engine">([\s\S]*?)<\/script>/)[1];
(0, eval)(engineSrc);
const E = globalThis.MoneyLeakEngine;
const key = JSON.parse(fs.readFileSync(path.join(here, 'answer-key.json'), 'utf8'));

let failures = 0;
const pad = (s, n) => String(s).padEnd(n).slice(0, n);
const money = v => (v == null ? '' : v.toFixed(2));

function describe(f) {
  if (f.type === 'duplicate') return `double charge ${money(f.amount)} x${f.count} (${f.confidence})`;
  let s = `${f.period} ${f.varies ? '~' + money(f.amount) + ' varies' : money(f.amount)} (${f.confidence}, ${f.status})`;
  if (f.priceChange) s += ` price ${f.priceChange.dir} ${money(f.priceChange.from)}->${money(f.priceChange.to)}`;
  return s;
}
function describeExpected(x) {
  if (x.type === 'duplicate') return `double charge ${money(x.amount)} (${x.confidence})`;
  let s = `${x.every} ${x.varies ? 'varies' : money(x.amount)} (${x.confidence || 'any'}, ${x.status})`;
  if (x.priceRise) s += ` price up ${money(x.priceRise.from)}->${money(x.priceRise.to)}`;
  return s;
}
function problems(x, f) {
  const p = [];
  if (x.type === 'recurring') {
    if (x.every && f.period !== x.every) p.push(`schedule ${f.period}, expected ${x.every}`);
    if (x.status && f.status !== x.status) p.push(`status ${f.status}, expected ${x.status}`);
    if (x.varies && !f.varies) p.push('should be marked as varying');
    if (x.priceRise) {
      if (!f.priceChange || f.priceChange.dir !== 'up') p.push('price rise missed');
      else if (Math.abs(f.priceChange.from - x.priceRise.from) > 0.005 || Math.abs(f.priceChange.to - x.priceRise.to) > 0.005)
        p.push(`price ${f.priceChange.from}->${f.priceChange.to}`);
    } else if (f.priceChange && f.priceChange.dir === 'up') p.push('false price rise');
  }
  if (x.amount != null && !f.varies && Math.abs(f.amount - x.amount) > 0.005) p.push(`amount ${f.amount}, expected ${x.amount}`);
  if (x.confidence && f.confidence !== x.confidence) p.push(`${f.confidence}, expected ${x.confidence}`);
  return p;
}

async function runSample(file, exp) {
  const buf = fs.readFileSync(path.join(process.env.SAMPLES_DIR || path.join(root, 'samples'), file));
  const table = await E.readTable(file, new Uint8Array(buf));
  const layout = E.detectLayout(table.rows);
  const { txns, skipped } = E.extract(table.rows, layout);
  const res = E.analyze(txns);
  const all = [...res.findings, ...res.stopped];

  console.log(`\n=== ${file} ===`);
  console.log(exp.about);
  const cols = layout.mode === 'split'
    ? `date="${layout.columns[layout.date]}", description="${layout.columns[layout.desc]}", out="${layout.columns[layout.out]}", in="${layout.columns[layout.in]}"`
    : `date="${layout.columns[layout.date]}", description="${layout.columns[layout.desc]}", amount="${layout.columns[layout.amount]}"` +
      (layout.dir >= 0 ? `, in/out="${layout.columns[layout.dir]}"` : `, money out is ${layout.outSign}`);
  console.log(`Read ${txns.length} payments (${skipped} rows skipped). Header row ${layout.headerIndex >= 0 ? layout.headerIndex + 1 : 'none'}. ${cols}. Dates ${layout.dateOrder}. Currency ${layout.currency || 'none'}.`);

  const stepShown = !layout.confident;
  const stepOk = stepShown === exp.expectColumnStep;
  if (!stepOk) failures++;
  console.log(`${stepOk ? 'PASS' : 'FAIL'}  Column check step shown: ${stepShown ? 'yes' : 'no'} (expected ${exp.expectColumnStep ? 'yes' : 'no'})${layout.issues.length ? ' - ' + layout.issues.join(' ') : ''}`);

  console.log(`\n${pad('Result', 6)}  ${pad('Merchant', 22)}  ${pad('Should find', 52)}  Tool found`);
  console.log('-'.repeat(140));
  const matched = new Set();
  for (const x of exp.findings) {
    const f = all.find(f => !matched.has(f) && f.type === x.type && f.merchant.toLowerCase().includes(x.merchant)
      && (x.type !== 'recurring' || !x.status || f.status === x.status))
      || all.find(f => !matched.has(f) && f.type === x.type && f.merchant.toLowerCase().includes(x.merchant));
    if (!f) {
      failures++;
      console.log(`${pad('MISSED', 6)}  ${pad(x.merchant, 22)}  ${pad(describeExpected(x), 52)}  (nothing)`);
      continue;
    }
    matched.add(f);
    const p = problems(x, f);
    if (p.length) failures++;
    console.log(`${pad(p.length ? 'WRONG' : 'PASS', 6)}  ${pad(f.merchant, 22)}  ${pad(describeExpected(x), 52)}  ${describe(f)}${p.length ? '  <- ' + p.join('; ') : ''}`);
  }
  for (const f of all) {
    if (matched.has(f)) continue;
    failures++;
    const banned = exp.mustNotFlag.find(m => f.merchant.toLowerCase().includes(m));
    console.log(`${pad('EXTRA', 6)}  ${pad(f.merchant, 22)}  ${pad(banned ? 'must NOT be flagged' : '(not expected)', 52)}  ${describe(f)}`);
  }
  const clean = exp.mustNotFlag.filter(m => !all.some(f => f.merchant.toLowerCase().includes(m)));
  console.log(`\nNot flagged, as expected: ${clean.join(', ')}`);
  const s = res.summary;
  console.log(`Totals: ${money(s.monthly)} a month, ${money(s.yearly)} a year from ${s.recurring} recurring charges; ${s.priceRises} price rises; ${s.duplicates} double charges; ${s.stopped} stopped.`);
}

function unit(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${pad(name, 58)}  ${JSON.stringify(got)}${ok ? '' : '  expected ' + JSON.stringify(want)}`);
}

async function unitChecks() {
  console.log('\n=== Small checks ===');
  const amt = (s, dc) => { const a = E.parseAmount(s, dc); return a && a.v; };
  unit('amount "$1,299.00"', amt('$1,299.00'), 1299);
  unit('amount "-$1,500.00"', amt('-$1,500.00'), -1500);
  unit('amount "(£12.50)"', amt('(£12.50)'), -12.5);
  unit('amount "1.299,00" (decimal comma)', amt('1.299,00'), 1299);
  unit('amount "-13,99"', amt('-13,99'), -13.99);
  unit('amount "12.50 CR"', E.parseAmount('12.50 CR'), { v: 12.5, flag: 'cr' });
  unit('amount "€ 1 234,56"', amt('€ 1 234,56'), 1234.56);
  unit('amount "USD 45.00"', amt('USD 45.00'), 45);
  unit('amount "1,234" in a decimal-comma column', amt('1,234', true), 1.23);
  unit('not an amount: "2025-01-03"', E.parseAmount('2025-01-03'), null);
  const iso = d => { const x = E.fromDayNumber(d); return `${x.y}-${x.m}-${x.d}`; };
  unit('date "03/04/2025" day-first', iso(E.toDay('03/04/2025', 'dmy')), '2025-4-3');
  unit('date "03/04/2025" month-first', iso(E.toDay('03/04/2025', 'mdy')), '2025-3-4');
  unit('date "14.02.2025"', iso(E.toDay('14.02.2025', 'dmy')), '2025-2-14');
  unit('date "3 Jan 2025"', iso(E.toDay('3 Jan 2025')), '2025-1-3');
  unit('date "Jan 3, 2025"', iso(E.toDay('Jan 3, 2025')), '2025-1-3');
  unit('date "2025-01-03 14:22:00"', iso(E.toDay('2025-01-03 14:22:00')), '2025-1-3');
  unit('date "20250103"', iso(E.toDay('20250103')), '2025-1-3');
  // All days are 12 or less, so only the date order in the file can tell.
  const us = ['01/05/2025', '01/19/2025', '02/05/2025', '02/12/2025', '03/05/2025', '03/12/2025', '04/05/2025', '04/12/2025'].map(s => s.replace(/19/, '09'));
  const r = E.analyzeDateColumn(us);
  unit('ambiguous dates, sorted month-first file', [r.order, r.orderSure], ['mdy', true]);
  const clean = s => E.titleCase(E.cleanMerchant(s));
  unit('merchant "CARD PAYMENT TO NETFLIX.COM ON 03/01"', clean('CARD PAYMENT TO NETFLIX.COM ON 03/01'), 'Netflix');
  unit('merchant "DD SPOTIFY P1A2B3C4D5"', clean('DD SPOTIFY P1A2B3C4D5'), 'Spotify');
  unit('merchant "NETFLIX.COM 866-579-7172 CA"', clean('NETFLIX.COM 866-579-7172 CA'), 'Netflix');
  unit('merchant "AMAZON PRIME*RT4K29XL0 AMZN.COM/BILL WA"', clean('AMAZON PRIME*RT4K29XL0 AMZN.COM/BILL WA'), 'Amazon Prime');
  unit('merchant "PAYPAL *DISNEYPLUS 4029357733"', clean('PAYPAL *DISNEYPLUS 4029357733'), 'Disneyplus');
  unit('merchant "POS DEBIT XXXX1234 UBER *TRIP 8JK2L"', clean('POS DEBIT XXXX1234 UBER *TRIP 8JK2L'), 'Uber Trip');
  unit('merchant "SQ *BLUE BOTTLE COFFEE"', clean('SQ *BLUE BOTTLE COFFEE'), 'Blue Bottle Coffee');
  unit('merchant "LASTSCHRIFT TELEKOM DEUTSCHLAND GMBH RECHNUNG 92375"', clean('LASTSCHRIFT TELEKOM DEUTSCHLAND GMBH RECHNUNG 92375'), 'Telekom Deutschland');
}

for (const [file, exp] of Object.entries(key)) {
  if (file.startsWith('_')) continue;
  await runSample(file, exp);
}
await unitChecks();
console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`);
process.exit(failures ? 1 : 0);
