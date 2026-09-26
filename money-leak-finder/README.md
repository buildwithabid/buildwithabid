# Money Leak Finder

A free, single-file web page that reads a bank or card statement and shows:

- **Recurring charges**: weekly, every 2 weeks, every 4 weeks, monthly, every 2 or 3 or 6 months, yearly
- **Forgotten subscriptions**, with the cost per month and per year
- **Double payments**: same company, same amount, within 3 days (refunded ones are skipped)
- **Price rises**: old price, new price, and what it adds up to each year

Each finding is labelled **Likely** or **Possible**. You mark each one Keep or Cancel and see a live
"Money you could save per year" total, then download the results as CSV.

## Use it

Open `index.html` in any modern browser (Chrome, Edge, Firefox, Safari). No install, no account.
Drop in a CSV or Excel (.xlsx) export from your bank. Several files at once is fine.

**Your data never leaves your device.** Everything runs in the page. The page carries a
Content-Security-Policy that blocks all network connections, so it can't send anything anywhere,
and it works offline.

## Tests

```bash
pip install openpyxl                               # only needed to regenerate the samples
python3 tests/make-samples.py                      # writes the fake statements in samples/
node tests/run-detection.mjs                       # detection vs tests/answer-key.json
bash tests/stress.sh 30                            # same checks with 30 random sets of everyday spending
NODE_PATH=$(npm root -g) node tests/run-browser.mjs  # the real page in headless Chromium (needs Playwright)
```

| Sample | What makes it tricky |
|---|---|
| `uk-bank-statement.csv` | DD/MM/YYYY, separate Paid out / Paid in columns, £ and commas, junk rows above the header, a price rise, a double charge, a cancelled subscription, a coffee bought at the same price twice a week |
| `us-credit-card.csv` | MM/DD/YYYY, charges are positive numbers, $ and commas, city names and phone numbers in descriptions, weekly, quarterly and yearly charges, a refunded double charge, one-off purchases at subscription companies |
| `eu-bank-export.xlsx` | Real Excel dates, amounts always positive with a separate Debit/credit column, title rows, a second sheet, a bill that varies, a gym that charges every 4 weeks |
| `no-header-semicolon.csv` | No column names, semicolons, decimal commas (1.299,00). The page asks the user to confirm the columns |

All names and numbers in the samples are made up.
