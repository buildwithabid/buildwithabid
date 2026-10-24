# Publishing checklist (for the owner, not the Store page)

## 1. Push to Apify

```bash
npm install -g apify-cli
apify login                      # paste your API token from Apify Console → Settings → Integrations
cd google-trends-actor
apify push                       # uploads and builds the Actor on Apify
```

If the name `google-trends-scraper-reliable` is taken in your account, change `name` in `.actor/actor.json`.

## 2. Test on the platform

In Apify Console, open the Actor and run it with the default input, then with 50+ terms.

In the log, check the last line:

- `blocked sessions replaced`: how often Google blocked.
- `final proxy tier`: 1 means it had to switch to residential IPs.
- `Done:`: results with data, empty and failed.

Before going public, aim for at least 98% of terms with data.

## 3. Turn on pay-per-event pricing

Actor → **Publishing → Monetization → Set up monetization → Pay per event**.

1. **Delete** the preset `apify-default-dataset-item` event. It would charge for every item, including failed and empty ones.
2. **Delete** the preset `apify-actor-start` event (or keep it at a tiny price and mention it in the README).
3. Add exactly these event names (the code charges them):

| Event name | Title shown to users | Suggested price |
|---|---|---|
| `trend-result` | Search term result | $0.0025 (leader charges $0.003, another strong competitor $0.002) |
| `trending-search` | Trending search | $0.0008 (competitors charge $0.001–$0.0012) |

4. **Primary event:** `trend-result`.
5. Don't transfer platform costs to users; they're already covered by the price.

Set prices before the first paying users arrive. Increases take 14 days to apply after that, and only once a month.

**Your costs** come out of your revenue, so check your plan's prices:

- **Revenue share:** you get about 80% of the event revenue.
- **Compute:** measured locally at 20 terms in 52 s with 3 parallel requests. At 256 MB memory that's a tiny fraction of a compute unit per term.
- **Proxy:** about 5 requests per term. Datacenter proxies are cheap or included. Residential proxies are charged per GB and only used after repeated blocks. Watch `final proxy tier` in early runs, and raise the price if residential is used often.

## 4. Store listing

- **Categories:** SEO tools, Marketing, Developer tools.
- **Title and description** are already in `.actor/actor.json`. `README.md` is the Store page.
- Add an icon and two example inputs (single term, comparison).
- Default memory is already 256 MB (`defaultMemoryMbytes` in `.actor/actor.json`; measured peak is 85 MB for 10 terms).

## 5. After launch (what the agent team should watch)

- **Daily:** failure rate and blocked-session counts across runs (Apify API: runs + logs). Alert if more than 2% of terms fail.
- **Issues tab:** answer within 24 hours. Ratings drive Store ranking.
- **Weekly:** run the test suite against fresh live responses (`python -m pytest`), because Google changes its endpoints without notice.
- **Monthly:** compare your price and rating with the top Google Trends actors in the Store.
