"""Run orchestration, independent of the Apify SDK so it can be tested directly.

Billing rules (pay per event):
  * `trend-result`    one per search term (or comparison group) that returned data
  * `trending-search` one per trending search returned
  * Results with no data and terms that failed are pushed for transparency but
    never charged.
  * Work stops as soon as the user's spending limit is reached.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .config import RunConfig
from .trends import TrendsBadRequest, TrendsClient, TrendsError, explore_url

EVENT_TERM = "trend-result"
EVENT_TRENDING = "trending-search"


class Charger(Protocol):
    async def push_data(self, data: dict[str, Any] | list[dict[str, Any]],
                        charged_event_name: str | None = None) -> Any: ...
    def limit_reached(self, event_name: str) -> bool: ...


@dataclass
class Summary:
    ok: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_by_limit: bool = False

    def message(self) -> str:
        parts = [f"{self.ok} result{'s' if self.ok != 1 else ''} with data"]
        if self.empty:
            parts.append(f"{self.empty} with no Google data (not charged)")
        if self.failed:
            parts.append(f"{self.failed} failed (not charged)")
        if self.stopped_by_limit:
            parts.append(f"stopped at your spending limit, {self.skipped} not processed")
        return "Done: " + ", ".join(parts) + "."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def fetch_item(client: TrendsClient, cfg: RunConfig, terms: list[str]) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if len(terms) == 1:
        item["searchTerm"] = terms[0]
    item.update({
        "searchTerms": terms, "geo": cfg.geo or "worldwide", "timeRange": cfg.time_range,
        "category": cfg.category, "searchType": cfg.property_name,
        "exploreUrl": explore_url(terms, cfg.geo, cfg.time_range, cfg.category, cfg.prop),
    })
    try:
        data = await client.fetch(terms, cfg.geo, cfg.time_range, category=cfg.category, prop=cfg.prop,
                                  sections=cfg.sections, resolution=cfg.resolution,
                                  include_low_volume=cfg.include_low_volume)
    except TrendsBadRequest as e:
        return {**item, "hasData": False, "error": f"Google rejected this query ({e}). Check geo, dates and category.",
                "scrapedAt": _now()}
    except TrendsError as e:
        return {**item, "hasData": False, "error": str(e), "scrapedAt": _now()}
    has_data = data.pop("hasData")
    return {**item, "hasData": has_data, **data, "scrapedAt": _now()}


async def run(cfg: RunConfig, client: TrendsClient, charger: Charger, log: Any = None) -> Summary:
    summary = Summary()

    if cfg.mode == "trendingNow":
        for i, geo in enumerate(cfg.trending_geos):
            if charger.limit_reached(EVENT_TRENDING):
                summary.stopped_by_limit = True
                summary.skipped = len(cfg.trending_geos) - i
                break
            try:
                items = await client.trending_now(geo)
            except TrendsError as e:
                summary.failed += 1
                await charger.push_data({"geo": geo, "error": str(e), "scrapedAt": _now()})
                continue
            if not items:
                summary.empty += 1
                continue
            stamp = _now()
            result = await charger.push_data([{**x, "scrapedAt": stamp} for x in items],
                                             charged_event_name=EVENT_TRENDING)
            summary.ok += len(items)
            if getattr(result, "event_charge_limit_reached", False):
                summary.stopped_by_limit = True
                summary.skipped = len(cfg.trending_geos) - i - 1
                break
        return summary

    groups = cfg.groups()
    sem = asyncio.Semaphore(cfg.max_concurrency)
    stop = asyncio.Event()

    async def work(terms: list[str]) -> None:
        async with sem:
            if stop.is_set() or charger.limit_reached(EVENT_TERM):
                stop.set()
                summary.skipped += 1
                return
            item = await fetch_item(client, cfg, terms)
            result = await charger.push_data(item, charged_event_name=EVENT_TERM if item["hasData"] else None)
            if item.get("error"):
                summary.failed += 1
                if log:
                    log.warning(f"{', '.join(terms)}: {item['error']}")
            elif item["hasData"]:
                summary.ok += 1
            else:
                summary.empty += 1
            if getattr(result, "event_charge_limit_reached", False):
                stop.set()

    await asyncio.gather(*(work(g) for g in groups))
    summary.stopped_by_limit = stop.is_set() and summary.skipped > 0
    return summary
