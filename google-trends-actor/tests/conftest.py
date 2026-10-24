"""Test helpers: a fake Google Trends server built from real recorded responses."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeGoogle:
    """Routes requests like trends.google.com does, from recorded responses.

    `script` lets a test inject failures: a list of (path_substring, status, body)
    consumed in order before normal routing.
    """

    def __init__(self, compare: bool = False):
        self.compare = compare
        self.script: list[tuple[str, int, str]] = []
        self.calls: list[httpx.Request] = []
        self.topics_body = fixture("RELATED_TOPICS.txt")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        for i, (needle, status, body) in enumerate(self.script):
            if needle in path:
                self.script.pop(i)
                return httpx.Response(status, text=body)
        if path in ("/trends/", "/trends"):
            return httpx.Response(200, text="<html>Google Trends</html>")
        if path == "/trends/api/explore":
            req = json.loads(request.url.params["req"])
            many = len(req["comparisonItem"]) > 1
            return httpx.Response(200, text=fixture("explore_cmp.txt" if many else "explore.txt"))
        sfx = "_cmp" if self.compare else ""
        if path.endswith("/multiline"):
            return httpx.Response(200, text=fixture(f"TIMESERIES{sfx}.txt"))
        if path.endswith("/comparedgeo"):
            req = json.loads(request.url.params["req"])
            if req.get("resolution") == "CITY":
                return httpx.Response(200, text=fixture("GEO_MAP_city.txt"))
            return httpx.Response(200, text=fixture(f"GEO_MAP{sfx}.txt"))
        if path.endswith("/relatedsearches"):
            req = json.loads(request.url.params["req"])
            if req.get("keywordType") == "ENTITY":
                return httpx.Response(200, text=self.topics_body)
            return httpx.Response(200, text=fixture(f"RELATED_QUERIES{sfx}.txt"))
        if path == "/trending/rss":
            return httpx.Response(200, text=fixture("rss.xml"))
        return httpx.Response(404, text="not found")

    def count(self, needle: str) -> int:
        return sum(needle in r.url.path for r in self.calls)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def make_client(google: FakeGoogle) -> Callable[..., Any]:
    from src.trends import SessionPool, TrendsClient

    def build(tiers: list[Any] | None = None, **kw: Any) -> Any:
        pool = SessionPool(tiers, transport=httpx.MockTransport(google.handler),
                           escalate_after=kw.pop("escalate_after", 3))
        sleeps = Sleeps()
        client = TrendsClient(pool, sleep=sleeps, min_interval_s=0, **kw)
        client.sleeps = sleeps  # type: ignore[attr-defined]
        return client

    return build
