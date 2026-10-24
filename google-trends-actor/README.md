# Google Trends Scraper (Reliable)

Get Google Trends data without failed runs: **interest over time, interest by region, related queries, related topics, comparisons of up to 5 terms, and today's trending searches**, for any country, region or worldwide.

Built for reliability. Google rate-limits Trends hard, which is why many Trends scrapers fail often. This one warms up a session, rotates to a fresh IP whenever Google blocks, and backs off before retrying. It moves from cheap datacenter IPs to residential IPs only when it has to. One problem keyword never fails your whole run, and **you only pay for results that contain data**.

## What you get

| Data | Description |
|---|---|
| **Interest over time** | Search interest from 0 to 100 for every hour, day or week in your time range. The current period is marked `isPartial` |
| **Interest by region** | Countries, regions or states, cities, or US metro areas, ranked by interest |
| **Related queries** | `top`: the most searched related queries. `rising`: the fastest-growing ones, where "Breakout" means more than 5000% growth |
| **Related topics** | Topics (people, companies, concepts) linked to your term, also top and rising |
| **Comparisons** | Up to 5 terms on one shared scale, with averages |
| **Trending now** | Today's trending searches per country, with approximate search volume and news links |

Filters: location (country or region), time range (past hour to 2004, or custom dates), category, and search type (web, images, news, Google Shopping, YouTube).

## Why this one

- **Doesn't give up easily.** Every Google block gets a fresh IP and an exponential backoff. Up to 6 retries per request by default (configurable).
- **Partial failures stay partial.** If one keyword or one section fails, you still get everything else, plus a clear `error` or `partialErrors` field.
- **Fair billing.** No charge for failed terms, for terms where Google has no data, or for single-spike noise (flagged `lowSearchVolume`).
- **Respects your budget.** It stops as soon as your run's spending limit is reached, so it never does work you won't get.
- **Handles Google's quirks.** If Google doesn't offer the region level you asked for (for example cities inside some states), it falls back to Google's default level and tells you which one it used in `regionResolutionUsed`.
- **Cost-aware proxies.** Datacenter IPs first, residential only after repeated blocks. That keeps runs fast and cheap.

## Quick start

Look up search interest for a few keywords in the US over the past 12 months:

```json
{
  "searchTerms": ["artificial intelligence", "chatgpt"],
  "geo": "US",
  "timeRange": "past-12-months"
}
```

Compare terms on one scale, worldwide, for a custom date range, YouTube searches only:

```json
{
  "searchTerms": ["chatgpt", "gemini", "claude"],
  "compareTerms": true,
  "startDate": "2025-01-01",
  "endDate": "2025-06-30",
  "searchType": "youtube"
}
```

Today's trending searches in three countries:

```json
{
  "mode": "trendingNow",
  "trendingGeos": ["US", "GB", "PK"]
}
```

## Output

One dataset item per search term, or per comparison group. A real result, shortened:

```json
{
  "searchTerm": "bitcoin",
  "geo": "worldwide",
  "timeRange": "today 3-m",
  "exploreUrl": "https://trends.google.com/trends/explore?q=bitcoin&date=today+3-m",
  "hasData": true,
  "latestValue": 25,
  "averageValue": 31.4,
  "peakValue": 100,
  "peakDate": "2026-08-14",
  "trendChangePercent": -22,
  "topRegion": "Switzerland",
  "topQuery": "bitcoin price",
  "topRisingQuery": "mua bitcoin binance",
  "interestOverTime": [
    { "date": "2026-10-22", "dateTime": "2026-10-22T00:00:00Z", "formattedTime": "Oct 22, 2026", "isPartial": false, "value": 25 },
    { "date": "2026-10-23", "dateTime": "2026-10-23T00:00:00Z", "formattedTime": "Oct 23, 2026", "isPartial": true, "value": 24 }
  ],
  "regionResolutionUsed": "COUNTRY",
  "interestByRegion": [
    { "geoName": "Switzerland", "geoCode": "CH", "value": 100 },
    { "geoName": "Netherlands", "geoCode": "NL", "value": 89 }
  ],
  "relatedQueries": {
    "top": [{ "query": "bitcoin price", "value": 100, "formattedValue": "100", "link": "https://trends.google.com/trends/explore?q=bitcoin+price&date=today+3-m" }],
    "rising": [{ "query": "mua bitcoin binance", "value": 25350, "formattedValue": "Breakout", "link": "https://trends.google.com/trends/explore?q=mua+bitcoin+binance&date=today+3-m" }]
  },
  "relatedTopics": { "top": [], "rising": [] },
  "scrapedAt": "2026-10-23T12:53:11Z"
}
```

The summary fields (`latestValue`, `averageValue`, `peakValue`, `peakDate`, `trendChangePercent`, `topRegion`, `topQuery`, `topRisingQuery`) give you the headline numbers in the results table without opening each item. `trendChangePercent` compares the last quarter of the time range with the first quarter.

In comparison mode, `value` becomes `values` (one number per term), and related queries and topics are grouped per term. Trending-now items contain `geo`, `rank`, `title`, `approxTraffic` (e.g. "2000+"), `approxTrafficMin`, `startedAt`, `picture` and `news`.

Download results as JSON, CSV, Excel or HTML, or connect them to Google Sheets, Make, Zapier, n8n and more.

## Pricing

Pay per result. See the **Pricing** tab for current prices.

| Charged | Not charged |
|---|---|
| Each search term (or comparison group) that returned data | Terms that failed, terms with no Google data, single-spike noise |
| Each trending search returned | Proxy retries and blocked attempts |

## Use it from code or AI agents

Run it through the Apify API and get the results in one call:

```bash
curl -X POST "https://api.apify.com/v2/acts/<username>~google-trends-scraper-reliable/run-sync-get-dataset-items?token=<YOUR_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"searchTerms": ["ai agents"], "geo": "US", "timeRange": "past-90-days"}'
```

AI agents (Claude, ChatGPT, Cursor and others) can call it as a tool through the [Apify MCP server](https://mcp.apify.com).

## Good to know

- **Values are relative, not search counts.** 100 is the busiest point for that term, place and time; 50 means half as popular. They aren't comparable across separate runs unless you use comparison mode.
- **Google samples its data.** The same query can return slightly different numbers on different days. That's how Google Trends works, not an error.
- **Comparisons are limited to 5 terms.** Longer lists are split into groups of 5, and values are only comparable inside a group. Comparison items name the most searched term in `leader`.
- **Related topics are sometimes empty.** Google withholds them for some queries and some IPs. The scraper retries once from a residential IP, then returns empty lists rather than failing.

## FAQ

**Is scraping Google Trends legal?**
This scraper collects publicly available, aggregated data that contains no personal information. You are responsible for using it in line with Google's terms and the laws that apply to you.

**Why didn't I get data for a keyword?**
Google only shows terms with enough search volume. Those items come back with `hasData: false` (and `lowSearchVolume: true` when it's just noise), and you aren't charged for them.

**Can I schedule it?**
Yes. Use Apify schedules to run it daily or weekly and get alerts or integrations on every run.

**Something broken?**
Open an issue on the Issues tab with your input, and it will be looked at quickly.
