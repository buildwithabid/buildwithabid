"""Google Trends client.

Google Trends has no official API. The web app calls an `explore` endpoint
that returns one signed token per widget, then fetches each widget's data with
that token. Most Trends scrapers fail because Google rate-limits hard (HTTP
429) and will not answer without a session cookie. This client:

  * warms every session up on the Trends home page to get the NID cookie,
  * retries with exponential backoff and full jitter,
  * throws away a session (and its proxy IP) the moment Google blocks it,
  * escalates through proxy tiers (e.g. datacenter, then residential) only
    after repeated blocks, so cheap IPs are used whenever they work,
  * paces requests per session so one IP is never hammered.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

BASE = "https://trends.google.com"
HT_NS = "{https://trends.google.com/trending/rss}"

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15",
)

ProxyUrlFn = Callable[[str], Awaitable[str | None]]


class TrendsError(Exception):
    """Base class for everything this client raises."""


class TrendsBlocked(TrendsError):
    """Google kept refusing even after retries and fresh IPs."""


class TrendsBadRequest(TrendsError):
    """Google rejected the query itself (e.g. an invalid geo or time range)."""


def parse_json(text: str) -> Any:
    """Trends prefixes JSON with `)]}'` to defeat JSON hijacking."""
    start = text.find("{")
    if start < 0:
        raise ValueError("response is not JSON")
    return json.loads(text[start:])


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def explore_url(terms: list[str], geo: str, time_range: str, category: int = 0, prop: str = "") -> str:
    q: dict[str, Any] = {"q": ",".join(terms), "date": time_range}
    if geo:
        q["geo"] = geo
    if category:
        q["cat"] = category
    if prop:
        q["gprop"] = prop
    return f"{BASE}/trends/explore?{urlencode(q)}"


# --------------------------------------------------------------------------
# Sessions and proxies
# --------------------------------------------------------------------------

@dataclass
class Session:
    id: str
    client: httpx.AsyncClient
    tier: int
    warmed: bool = False
    last_request: float = 0.0


class SessionPool:
    """Sessions bound to proxy IPs, escalating proxy tiers on repeated blocks.

    `tiers` is a list of functions that turn a session id into a proxy URL (or
    None for a direct connection). With an empty list every session connects
    directly.
    """

    def __init__(self, tiers: list[ProxyUrlFn] | None = None, *, escalate_after: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None, language: str = "en-US",
                 timeout_s: float = 30.0, rng: random.Random | None = None):
        self.tiers = tiers or []
        self.escalate_after = escalate_after
        self.transport = transport
        self.language = language
        self.timeout_s = timeout_s
        self.rng = rng or random.Random()
        self.tier = 0
        self._idle: list[Session] = []
        self._blocks_on_tier = 0
        self.created = 0
        self.burned = 0

    @property
    def can_escalate(self) -> bool:
        return self.tier + 1 < len(self.tiers)

    def escalate(self) -> bool:
        if not self.can_escalate:
            return False
        self.tier += 1
        self._blocks_on_tier = 0
        return True

    async def acquire(self) -> Session:
        while self._idle:
            s = self._idle.pop()
            if s.tier == self.tier:
                return s
            await s.client.aclose()      # left over from a cheaper tier
        return await self._new()

    def release(self, s: Session) -> None:
        self._idle.append(s)

    def mark_ok(self) -> None:
        self._blocks_on_tier = 0

    async def burn(self, s: Session, blocked: bool = True) -> None:
        """Discard a session and its IP. Repeated blocks move to the next tier."""
        self.burned += 1
        await s.client.aclose()
        if blocked and s.tier == self.tier:
            self._blocks_on_tier += 1
            if self._blocks_on_tier >= self.escalate_after:
                self.escalate()

    async def _new(self) -> Session:
        sid = "gt" + uuid.uuid4().hex[:12]
        proxy = await self.tiers[self.tier](sid) if self.tiers else None
        headers = {
            "User-Agent": self.rng.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self.language},{self.language.split('-')[0]};q=0.9",
            "Referer": f"{BASE}/trends/explore",
        }
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self.timeout_s, "follow_redirects": True}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        elif proxy:
            kwargs["proxy"] = proxy
        self.created += 1
        return Session(sid, httpx.AsyncClient(**kwargs), self.tier)

    async def close(self) -> None:
        for s in self._idle:
            await s.client.aclose()
        self._idle.clear()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class TrendsClient:
    def __init__(self, pool: SessionPool, *, language: str = "en-US", tz_offset_min: int = 0,
                 max_retries: int = 6, backoff_base_s: float = 1.5, backoff_cap_s: float = 30.0,
                 min_interval_s: float = 0.8, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None,
                 log: Any = None):
        self.pool = pool
        self.language = language
        self.tz = str(tz_offset_min)
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self.rng = rng or random.Random()
        self.log = log
        self.requests = 0

    # -- transport -------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_cap_s, self.backoff_base_s * 2 ** (attempt - 1))
        return ceiling * self.rng.uniform(0.5, 1.0)

    @staticmethod
    def _blocked(r: httpx.Response) -> bool:
        if r.status_code == 429 or "/sorry/" in str(r.url):
            return True
        return r.status_code == 200 and "unusual traffic" in r.text[:4000].lower()

    async def _pace(self, s: Session) -> None:
        wait = s.last_request + self.min_interval_s - self._clock()
        if wait > 0:
            await self._sleep(wait)

    async def _get(self, path: str, params: dict[str, Any], *, expect_json: bool = True,
                   probe: bool = False) -> Any:
        """`probe`: two attempts, and a 429 is not treated as a block. Google answers
        some invalid-but-well-formed requests (e.g. an unsupported region level)
        with 429, so probing must not burn IPs or escalate proxies."""
        last = "no attempt made"
        attempts = 2 if probe else self.max_retries + 1
        for attempt in range(attempts):
            if attempt:
                await self._sleep(self._backoff(attempt))
            s = await self.pool.acquire()
            keep, blocked = True, False
            try:
                if not s.warmed:
                    w = await s.client.get(f"{BASE}/trends/?geo=")
                    s.last_request = self._clock()
                    if self._blocked(w) or w.status_code >= 400:
                        keep, blocked = False, True
                        last = f"session warm-up refused (HTTP {w.status_code})"
                        continue
                    s.warmed = True
                await self._pace(s)
                self.requests += 1
                r = await s.client.get(BASE + path, params=params)
                s.last_request = self._clock()
                if self._blocked(r):
                    if probe:
                        last = f"HTTP {r.status_code} for {path}"
                        if attempt + 1 >= attempts:
                            raise TrendsBlocked(last)
                        continue            # keep the session: this 429 may just mean "unsupported"
                    keep, blocked = False, True
                    last = f"rate limited by Google (HTTP {r.status_code})"
                    continue
                if r.status_code in (400, 404):
                    raise TrendsBadRequest(f"HTTP {r.status_code} for {path}")
                if r.status_code != 200:
                    # 5xx: Google's problem, the session is fine. 403: the IP is blocked.
                    # Anything else: start a clean session without blaming the IP.
                    keep = r.status_code >= 500
                    blocked = r.status_code == 403
                    last = f"HTTP {r.status_code}"
                    continue
                if not expect_json:
                    self.pool.mark_ok()
                    return r.text
                try:
                    data = parse_json(r.text)
                except ValueError:
                    keep, blocked = False, True     # consent or captcha page instead of data
                    last = "unexpected non-JSON response"
                    continue
                self.pool.mark_ok()
                return data
            except httpx.HTTPError as e:
                keep = False
                last = f"network error: {type(e).__name__}"
                continue
            finally:
                if keep:
                    self.pool.release(s)
                else:
                    await self.pool.burn(s, blocked=blocked)
                if not keep and self.log:
                    self.log.debug(f"attempt {attempt + 1} failed: {last}")
        raise TrendsBlocked(f"gave up after {self.max_retries + 1} attempts: {last}")

    # -- endpoints -------------------------------------------------------------
    async def explore(self, terms: list[str], geo: str, time_range: str, category: int = 0,
                      prop: str = "") -> list[dict[str, Any]]:
        req = {"comparisonItem": [{"keyword": t, "geo": geo, "time": time_range} for t in terms],
               "category": category, "property": prop}
        data = await self._get("/trends/api/explore", {"hl": self.language, "tz": self.tz, "req": _dumps(req)})
        return [w for w in data.get("widgets", []) if "request" in w and "token" in w]

    async def widget(self, endpoint: str, widget: dict[str, Any], overrides: dict[str, Any] | None = None,
                     *, probe: bool = False) -> Any:
        req = dict(widget["request"])
        req.update(overrides or {})
        params = {"hl": self.language, "tz": self.tz, "req": _dumps(req), "token": widget["token"]}
        return await self._get(f"/trends/api/widgetdata/{endpoint}", params, probe=probe)

    async def trending_now(self, geo: str) -> list[dict[str, Any]]:
        text = await self._get("/trending/rss", {"geo": geo}, expect_json=False)
        return parse_trending_rss(text, geo)

    # -- high level --------------------------------------------------------------
    async def fetch(self, terms: list[str], geo: str, time_range: str, *, category: int = 0, prop: str = "",
                    sections: tuple[str, ...] = ("interestOverTime", "interestByRegion", "relatedQueries",
                                                 "relatedTopics"),
                    resolution: str | None = None, include_low_volume: bool = False) -> dict[str, Any]:
        """Everything Trends shows for one search term, or for up to five compared terms."""
        widgets = await self.explore(terms, geo, time_range, category, prop)
        flagged = any((w["request"].get("userConfig") or {}).get("userType") == "USER_TYPE_SCRAPER"
                      for w in widgets)
        by_id = {w["id"]: w for w in widgets}
        out: dict[str, Any] = {}
        errors: list[str] = []
        has_data = False

        if "interestOverTime" in sections and "TIMESERIES" in by_id:
            try:
                rows, averages, ok = parse_timeline(await self.widget("multiline", by_id["TIMESERIES"]), terms)
                out["interestOverTime"] = rows
                if averages:
                    out["averages"] = averages
                has_data |= ok
            except TrendsError as e:
                errors.append(f"interestOverTime: {e}")

        if "interestByRegion" in sections and "GEO_MAP" in by_id:
            w = by_id["GEO_MAP"]
            base = {"includeLowSearchVolumeGeos": include_low_volume}
            default_res = w["request"].get("resolution")
            try:
                data = None
                if resolution and resolution != default_res:
                    # Not every level exists for every place; try once, then use Google's default.
                    try:
                        data = await self.widget("comparedgeo", w, {**base, "resolution": resolution}, probe=True)
                        out["regionResolutionUsed"] = resolution
                    except (TrendsBadRequest, TrendsBlocked):
                        data = None
                if data is None:
                    data = await self.widget("comparedgeo", w, base)
                    out["regionResolutionUsed"] = default_res
                regions = parse_regions(data, terms, include_low_volume)
                out["interestByRegion"] = regions
                has_data |= bool(regions)
            except TrendsError as e:
                errors.append(f"interestByRegion: {e}")

        for section, prefix, kind in (("relatedQueries", "RELATED_QUERIES", "queries"),
                                      ("relatedTopics", "RELATED_TOPICS", "topics")):
            if section not in sections:
                continue
            related_widgets = [w for w in widgets if w["id"] == prefix or w["id"].startswith(prefix + "_")]
            per_term: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for w in related_widgets:
                term = _widget_term(w, terms)
                try:
                    per_term[term] = parse_related(await self.widget("relatedsearches", w), kind)
                except TrendsError as e:
                    errors.append(f"{section} ({term}): {e}")
            if kind == "topics" and flagged and not any(v["top"] or v["rising"] for v in per_term.values()) \
                    and self.pool.escalate():
                # Google answered as it does for suspected bots: topics withheld. Try once more
                # from the next proxy tier, which is usually a residential IP.
                try:
                    return await self.fetch(terms, geo, time_range, category=category, prop=prop,
                                            sections=sections, resolution=resolution,
                                            include_low_volume=include_low_volume)
                except TrendsError:
                    pass
            if len(terms) == 1:
                value = per_term.get(terms[0], {"top": [], "rising": []})
                has_data |= bool(value["top"] or value["rising"])
            else:
                value = {t: per_term.get(t, {"top": [], "rising": []}) for t in terms}
                has_data |= any(v["top"] or v["rising"] for v in value.values())
            out[section] = value

        if not has_data and any(
                (r.get("value") or any((r.get("values") or {}).values())) for r in out.get("interestOverTime", [])):
            out["lowSearchVolume"] = True     # only noise-level interest; reported, not charged
        if errors:
            out["partialErrors"] = errors
        out["hasData"] = has_data
        return out


def _widget_term(w: dict[str, Any], terms: list[str]) -> str:
    if w.get("keywordName"):
        return str(w["keywordName"])
    kws = ((w["request"].get("restriction") or {}).get("complexKeywordsRestriction") or {}).get("keyword") or []
    if kws and kws[0].get("value"):
        return str(kws[0]["value"])
    suffix = w["id"].rsplit("_", 1)[-1]
    return terms[int(suffix)] if suffix.isdigit() and int(suffix) < len(terms) else terms[0]


# --------------------------------------------------------------------------
# Parsers (pure functions, tested against recorded Google responses)
# --------------------------------------------------------------------------

def parse_timeline(data: dict[str, Any], terms: list[str]) -> tuple[list[dict[str, Any]], dict[str, int] | None, bool]:
    """Rows, comparison averages, and whether there is a real signal (2+ non-zero points)."""
    default = data.get("default") or {}
    rows: list[dict[str, Any]] = []
    nonzero = [0] * len(terms)
    for p in default.get("timelineData", []):
        ts = int(p["time"])
        values = p.get("value") or []
        for i, v in enumerate(values[:len(terms)]):
            nonzero[i] += bool(v)
        row: dict[str, Any] = {"date": _iso(ts)[:10], "dateTime": _iso(ts), "formattedTime": p.get("formattedTime"),
                               "isPartial": bool(p.get("isPartial", False))}
        if len(terms) == 1:
            row["value"] = values[0] if values else None
        else:
            row["values"] = dict(zip(terms, values))
        rows.append(row)
    averages = default.get("averages")
    avg = dict(zip(terms, averages)) if averages and len(terms) > 1 else None
    # Google scales the busiest point to 100 even when it is a single stray search,
    # so one non-zero point on its own is noise, not a trend.
    has_data = any(n >= 2 for n in nonzero)
    return rows, avg, has_data


def parse_regions(data: dict[str, Any], terms: list[str], include_empty: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in (data.get("default") or {}).get("geoMapData", []):
        if not include_empty and not any(g.get("hasData") or []):
            continue
        row: dict[str, Any] = {"geoName": g.get("geoName")}
        if g.get("geoCode"):
            row["geoCode"] = g["geoCode"]
        if g.get("coordinates"):
            row["coordinates"] = g["coordinates"]
        values = g.get("value") or []
        if len(terms) == 1:
            row["value"] = values[0] if values else None
        else:
            row["values"] = dict(zip(terms, values))     # share of searches between the terms, in %
        out.append(row)
    return out


def parse_related(data: dict[str, Any], kind: str) -> dict[str, list[dict[str, Any]]]:
    """`top` = most popular; `rising` = biggest growth ("Breakout" = over 5000%)."""
    out: dict[str, list[dict[str, Any]]] = {"top": [], "rising": []}
    lists = (data.get("default") or {}).get("rankedList") or []
    for name, ranked in zip(("top", "rising"), lists):
        for k in ranked.get("rankedKeyword", []):
            if kind == "queries":
                item: dict[str, Any] = {"query": k.get("query")}
            else:
                topic = k.get("topic") or {}
                item = {"title": topic.get("title"), "type": topic.get("type"), "mid": topic.get("mid")}
            item["value"] = k.get("value")
            item["formattedValue"] = k.get("formattedValue")
            if k.get("link"):
                item["link"] = BASE + k["link"]
            out[name].append(item)
    return out


def _traffic_min(text: str | None) -> int | None:
    if not text:
        return None
    digits = text.replace(",", "").replace("+", "").strip().upper()
    mult = 1
    if digits.endswith("K"):
        mult, digits = 1_000, digits[:-1]
    elif digits.endswith("M"):
        mult, digits = 1_000_000, digits[:-1]
    try:
        return int(float(digits) * mult)
    except ValueError:
        return None


def parse_trending_rss(text: str, geo: str) -> list[dict[str, Any]]:
    try:
        root = safe_fromstring(text)        # proxies sit in the path, so never trust the XML blindly
    except (ET.ParseError, DefusedXmlException) as e:
        raise TrendsError(f"trending feed was not valid XML: {e}") from e
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(root.iter("item"), start=1):
        title = (item.findtext("title") or "").strip()
        traffic = item.findtext(f"{HT_NS}approx_traffic")
        started = item.findtext("pubDate")
        started_iso = None
        if started:
            try:
                started_iso = parsedate_to_datetime(started).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError):
                started_iso = None
        news = [{
            "title": n.findtext(f"{HT_NS}news_item_title"),
            "url": n.findtext(f"{HT_NS}news_item_url"),
            "source": n.findtext(f"{HT_NS}news_item_source"),
            "picture": n.findtext(f"{HT_NS}news_item_picture"),
            "snippet": n.findtext(f"{HT_NS}news_item_snippet") or None,
        } for n in item.findall(f"{HT_NS}news_item")]
        out.append({
            "geo": geo, "rank": rank, "title": title, "approxTraffic": traffic,
            "approxTrafficMin": _traffic_min(traffic), "startedAt": started_iso,
            "picture": item.findtext(f"{HT_NS}picture"), "pictureSource": item.findtext(f"{HT_NS}picture_source"),
            "news": news, "exploreUrl": explore_url([title], geo, "now 1-d"),
        })
    return out
