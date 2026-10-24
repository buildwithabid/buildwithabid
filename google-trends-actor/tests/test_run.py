"""Input validation and billing rules."""
from dataclasses import dataclass

import pytest

from src.config import parse_input
from src.run import EVENT_TERM, EVENT_TRENDING, run


@dataclass
class Result:
    event_charge_limit_reached: bool


class FakeCharger:
    """Mimics Apify pay-per-event: charges per pushed item until the budget runs out."""

    def __init__(self, budget: int | None = None):
        self.budget = budget
        self.items: list[dict] = []
        self.charged: dict[str, int] = {}

    def limit_reached(self, event: str) -> bool:
        return self.budget is not None and self.budget <= 0

    async def push_data(self, data, charged_event_name=None):
        batch = data if isinstance(data, list) else [data]
        if charged_event_name:
            if self.budget is not None:
                batch = batch[: max(0, self.budget)]
                self.budget -= len(batch)
            self.charged[charged_event_name] = self.charged.get(charged_event_name, 0) + len(batch)
        self.items.extend(batch)
        return Result(self.budget is not None and self.budget <= 0)


# -- config --------------------------------------------------------------------

def test_defaults_and_term_cleanup():
    cfg = parse_input({"searchTerms": ["  Python ", "python", "", "rust   lang"]})
    assert cfg.search_terms == ["Python", "rust lang"]
    assert cfg.time_range == "today 12-m" and cfg.geo == "" and cfg.prop == ""
    assert cfg.groups() == [["Python"], ["rust lang"]]
    assert cfg.proxy == {"useApifyProxy": True}


def test_comparison_groups_of_five():
    cfg = parse_input({"searchTerms": [f"t{i}" for i in range(7)], "compareTerms": True})
    assert cfg.groups() == [["t0", "t1", "t2", "t3", "t4"], ["t5", "t6"]]


def test_custom_dates_and_options():
    cfg = parse_input({"searchTerms": ["x"], "startDate": "2024-01-01", "endDate": "2024-06-30", "geo": "us-ca",
                       "searchType": "youtube", "outputs": ["relatedQueries", "interestOverTime"],
                       "regionResolution": "city", "category": 7})
    assert cfg.time_range == "2024-01-01 2024-06-30"
    assert cfg.geo == "US-CA" and cfg.prop == "youtube" and cfg.category == 7
    assert cfg.sections == ("interestOverTime", "relatedQueries")
    assert cfg.resolution == "CITY"


@pytest.mark.parametrize("raw, message", [
    ({}, "at least one search term"),
    ({"searchTerms": ["x"], "geo": "United States"}, "not a valid location code"),
    ({"searchTerms": ["x"], "timeRange": "yesterday"}, "timeRange must be one of"),
    ({"searchTerms": ["x"], "startDate": "2024-01-01"}, "both startDate and endDate"),
    ({"searchTerms": ["x"], "startDate": "2003-01-01", "endDate": "2004-06-01"}, "before 2004-01-01"),
    ({"searchTerms": ["x"], "startDate": "2024-05-01", "endDate": "2024-01-01"}, "must be before endDate"),
    ({"searchTerms": ["x"], "outputs": ["everything"]}, "unknown outputs"),
    ({"searchTerms": ["x" * 101]}, "longer than 100"),
    ({"mode": "trendingNow", "trendingGeos": [""]}, "at least one country code"),
    ({"mode": "nope"}, "mode must be"),
])
def test_validation_messages(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_input(raw)


# -- billing -------------------------------------------------------------------

async def test_charges_only_results_with_data(make_client, google):
    google.script = [("/explore", 429, "")] * 20         # second term will fail completely
    cfg = parse_input({"searchTerms": ["python"], "maxConcurrency": 1, "maxRetries": 1})
    charger = FakeCharger()
    summary = await run(cfg, make_client(max_retries=1), charger)
    assert summary.failed == 1 and summary.ok == 0
    assert charger.charged == {}                          # failures are never charged
    assert charger.items[0]["error"].startswith("gave up after 2 attempts")


async def test_successful_terms_are_charged_once_each(make_client):
    cfg = parse_input({"searchTerms": ["a", "b", "c"]})
    charger = FakeCharger()
    summary = await run(cfg, make_client(), charger)
    assert summary.ok == 3 and charger.charged == {EVENT_TERM: 3}
    assert {i["searchTerm"] for i in charger.items} == {"a", "b", "c"}
    assert all(i["exploreUrl"].startswith("https://trends.google.com/") for i in charger.items)
    assert summary.message() == "Done: 3 results with data."


async def test_stops_at_spending_limit(make_client, google):
    cfg = parse_input({"searchTerms": [f"t{i}" for i in range(6)], "maxConcurrency": 1})
    charger = FakeCharger(budget=2)
    summary = await run(cfg, make_client(), charger)
    assert charger.charged == {EVENT_TERM: 2}
    assert summary.stopped_by_limit and summary.skipped == 4
    assert google.count("/explore") == 2                  # no work done that can't be billed
    assert "stopped at your spending limit" in summary.message()


async def test_trending_mode_charges_per_trend(make_client):
    cfg = parse_input({"mode": "trendingNow", "trendingGeos": ["US", "GB"]})
    charger = FakeCharger()
    summary = await run(cfg, make_client(), charger)
    assert charger.charged == {EVENT_TRENDING: 6} and summary.ok == 6
    assert {i["geo"] for i in charger.items} == {"US", "GB"}


# -- at-a-glance summary ----------------------------------------------------------

async def test_items_carry_an_at_a_glance_summary(make_client):
    cfg = parse_input({"searchTerms": ["python"]})
    charger = FakeCharger()
    await run(cfg, make_client(), charger)
    item = charger.items[0]
    values = [r["value"] for r in item["interestOverTime"] if not r["isPartial"]]
    assert item["latestValue"] == values[-1]
    assert item["peakValue"] == max(values)
    assert item["peakDate"] == next(r["date"] for r in item["interestOverTime"] if r["value"] == max(values))
    assert item["averageValue"] == round(sum(values) / len(values), 1)
    assert isinstance(item["trendChangePercent"], int)
    assert item["topRegion"] == "Wyoming"
    assert item["topQuery"] == "what is python"
    assert item["topRisingQuery"] == item["relatedQueries"]["rising"][0]["query"]


async def test_comparison_summary_names_the_leader(make_client, google):
    google.compare = True
    cfg = parse_input({"searchTerms": ["chatgpt", "gemini"], "compareTerms": True})
    charger = FakeCharger()
    await run(cfg, make_client(), charger)
    item = charger.items[0]
    assert item["leader"] == "chatgpt" and "latestValue" not in item


def test_summary_handles_flat_start():
    from src.run import summarize
    rows = [{"date": f"2026-01-0{i}", "value": v, "isPartial": False} for i, v in enumerate([0, 0, 10, 20], 1)]
    s = summarize(["x"], {"interestOverTime": rows})
    assert s["trendChangePercent"] is None and s["peakDate"] == "2026-01-04"
