"""Input validation. Every message here is shown to the user, so keep it plain."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

TIME_RANGES = {
    "past-hour": "now 1-H",
    "past-4-hours": "now 4-H",
    "past-day": "now 1-d",
    "past-7-days": "now 7-d",
    "past-30-days": "today 1-m",
    "past-90-days": "today 3-m",
    "past-12-months": "today 12-m",
    "past-5-years": "today 5-y",
    "since-2004": "all",
}
PROPERTIES = {"web": "", "images": "images", "news": "news", "shopping": "froogle", "youtube": "youtube"}
RESOLUTIONS = {"auto": None, "country": "COUNTRY", "region": "REGION", "city": "CITY", "metro": "DMA"}
SECTIONS = ("interestOverTime", "interestByRegion", "relatedQueries", "relatedTopics")
MAX_COMPARE = 5     # Google Trends compares at most five terms at once

_GEO = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")


@dataclass
class RunConfig:
    mode: str = "explore"
    search_terms: list[str] = field(default_factory=list)
    compare: bool = False
    geo: str = ""
    time_range: str = "today 12-m"
    category: int = 0
    property_name: str = "web"
    sections: tuple[str, ...] = SECTIONS
    resolution: str | None = None
    include_low_volume: bool = False
    trending_geos: list[str] = field(default_factory=lambda: ["US"])
    language: str = "en-US"
    timezone_offset: int = 0
    max_concurrency: int = 3
    max_retries: int = 6
    proxy: dict[str, Any] | None = None
    escalate_to_residential: bool = True

    @property
    def prop(self) -> str:
        return PROPERTIES[self.property_name]

    def groups(self) -> list[list[str]]:
        if not self.compare:
            return [[t] for t in self.search_terms]
        return [self.search_terms[i:i + MAX_COMPARE] for i in range(0, len(self.search_terms), MAX_COMPARE)]


def _geo(value: Any, name: str) -> str:
    g = str(value or "").strip().upper()
    if g in ("", "WORLDWIDE", "WORLD", "GLOBAL"):
        return ""
    if not _GEO.match(g):
        raise ValueError(f'{name} "{value}" is not a valid location code. Use a 2-letter country code such as '
                         '"US" or "GB", a region such as "US-CA", or leave it empty for worldwide.')
    return g


def _date(value: str, name: str) -> date:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date like 2025-01-31") from None
    if d < date(2004, 1, 1):
        raise ValueError(f"{name} cannot be before 2004-01-01, when Google Trends data starts")
    return d


def parse_input(raw: dict[str, Any]) -> RunConfig:
    cfg = RunConfig()
    cfg.mode = raw.get("mode", "explore")
    if cfg.mode not in ("explore", "trendingNow"):
        raise ValueError('mode must be "explore" or "trendingNow"')

    if cfg.mode == "trendingNow":
        geos = raw.get("trendingGeos") or ["US"]
        cfg.trending_geos = [g for g in (_geo(x, "trendingGeos") for x in geos) if g]
        if not cfg.trending_geos:
            raise ValueError("trendingGeos needs at least one country code, e.g. US")
    else:
        seen: set[str] = set()
        for term in raw.get("searchTerms") or []:
            t = " ".join(str(term).split())
            if not t or t.lower() in seen:
                continue
            if len(t) > 100:
                raise ValueError(f'search term "{t[:40]}..." is longer than 100 characters')
            seen.add(t.lower())
            cfg.search_terms.append(t)
        if not cfg.search_terms:
            raise ValueError("add at least one search term")
        cfg.compare = bool(raw.get("compareTerms", False))
        cfg.geo = _geo(raw.get("geo"), "geo")

        start, end = raw.get("startDate"), raw.get("endDate")
        if start or end:
            if not (start and end):
                raise ValueError("set both startDate and endDate, or neither")
            s, e = _date(start, "startDate"), _date(end, "endDate")
            if s >= e:
                raise ValueError("startDate must be before endDate")
            cfg.time_range = f"{s.isoformat()} {e.isoformat()}"
        else:
            key = raw.get("timeRange", "past-12-months")
            if key not in TIME_RANGES:
                raise ValueError(f"timeRange must be one of: {', '.join(TIME_RANGES)}")
            cfg.time_range = TIME_RANGES[key]

        try:
            cfg.category = int(raw.get("category", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError("category must be a number (0 = all categories)") from None
        if cfg.category < 0:
            raise ValueError("category must be 0 or a positive Google Trends category id")

        cfg.property_name = raw.get("searchType", "web")
        if cfg.property_name not in PROPERTIES:
            raise ValueError(f"searchType must be one of: {', '.join(PROPERTIES)}")

        sections = raw.get("outputs") or list(SECTIONS)
        bad = [s for s in sections if s not in SECTIONS]
        if bad:
            raise ValueError(f"unknown outputs: {', '.join(bad)}. Choose from: {', '.join(SECTIONS)}")
        cfg.sections = tuple(s for s in SECTIONS if s in sections)

        res = raw.get("regionResolution", "auto")
        if res not in RESOLUTIONS:
            raise ValueError(f"regionResolution must be one of: {', '.join(RESOLUTIONS)}")
        cfg.resolution = RESOLUTIONS[res]
        cfg.include_low_volume = bool(raw.get("includeLowSearchVolume", False))

    lang = str(raw.get("language", "en-US"))
    if not _LANG.match(lang):
        raise ValueError('language must look like "en-US" or "de"')
    cfg.language = lang
    cfg.timezone_offset = int(raw.get("timezoneOffsetMinutes", 0) or 0)
    cfg.max_concurrency = max(1, min(10, int(raw.get("maxConcurrency", 3) or 3)))
    cfg.max_retries = max(0, min(15, int(raw.get("maxRetries", 6) if raw.get("maxRetries") is not None else 6)))
    cfg.proxy = raw.get("proxyConfiguration") or {"useApifyProxy": True}
    cfg.escalate_to_residential = bool(raw.get("escalateToResidential", True))
    return cfg
