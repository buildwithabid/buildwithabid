"""Client behaviour: retries, IP rotation, proxy escalation, fallbacks."""
import json

import pytest

from src.trends import TrendsBadRequest, TrendsBlocked

TIER_CALLS: list[tuple[int, str]] = []


def tier(n):
    async def proxy_url(session_id: str) -> str:
        TIER_CALLS.append((n, session_id))
        return f"http://tier{n}.proxy:8000"
    return proxy_url


@pytest.fixture(autouse=True)
def _reset():
    TIER_CALLS.clear()


async def test_full_fetch_single_term(make_client, google):
    c = make_client()
    r = await c.fetch(["python"], "US", "today 12-m")
    assert r["hasData"] is True
    assert len(r["interestOverTime"]) == 53
    assert len(r["interestByRegion"]) == 51 and r["regionResolutionUsed"] == "REGION"
    assert len(r["relatedQueries"]["top"]) == 25
    assert r["relatedTopics"] == {"top": [], "rising": []}
    assert "partialErrors" not in r
    assert google.count("/trends/api/explore") == 1
    assert google.count("/trends/api/widgetdata/") == 4
    assert c.pool.created == 1 and c.pool.burned == 0


async def test_comparison_maps_related_widgets_to_their_terms(make_client, google):
    google.compare = True
    r = await make_client().fetch(["chatgpt", "gemini"], "", "2025-01-01 2025-06-30")
    assert r["averages"] == {"chatgpt": 16, "gemini": 10}
    assert set(r["relatedQueries"]) == {"chatgpt", "gemini"}
    assert r["interestByRegion"][0]["values"] == {"chatgpt": 78, "gemini": 22}


async def test_sections_can_be_limited(make_client, google):
    r = await make_client().fetch(["python"], "US", "today 12-m", sections=("interestOverTime",))
    assert set(r) == {"interestOverTime", "hasData"}
    assert google.count("/widgetdata/") == 1


async def test_rate_limit_rotates_to_a_fresh_session(make_client, google):
    google.script = [("/explore", 429, "Too Many Requests"), ("/explore", 429, "Too Many Requests")]
    c = make_client(max_retries=4)
    r = await c.fetch(["python"], "US", "today 12-m", sections=("interestOverTime",))
    assert r["hasData"]
    assert c.pool.burned == 2 and c.pool.created == 3     # each block discarded its session/IP
    assert len(c.sleeps.calls) == 2                       # backed off before each retry
    assert c.sleeps.calls[0] <= 1.5 and c.sleeps.calls[1] <= 3.0


async def test_captcha_page_counts_as_a_block(make_client, google):
    google.script = [("/explore", 200, "<html>Our systems have detected unusual traffic</html>")]
    c = make_client()
    await c.fetch(["python"], "US", "today 12-m", sections=("interestOverTime",))
    assert c.pool.burned == 1


async def test_escalates_to_next_proxy_tier_after_repeated_blocks(make_client, google):
    google.script = [("/explore", 429, "")] * 3
    c = make_client([tier(0), tier(1)], max_retries=5, escalate_after=3)
    r = await c.fetch(["python"], "US", "today 12-m", sections=("interestOverTime",))
    assert r["hasData"]
    assert c.pool.tier == 1
    assert [t for t, _ in TIER_CALLS] == [0, 0, 0, 1]
    assert len({sid for _, sid in TIER_CALLS}) == 4       # a new proxy session (IP) every time


async def test_gives_up_with_clear_error(make_client, google):
    google.script = [("/explore", 429, "")] * 10
    c = make_client(max_retries=2)
    with pytest.raises(TrendsBlocked, match="gave up after 3 attempts: rate limited by Google"):
        await c.fetch(["python"], "US", "today 12-m")


async def test_bad_request_is_not_retried(make_client, google):
    google.script = [("/explore", 400, "<html>Error 400</html>")]
    c = make_client()
    with pytest.raises(TrendsBadRequest):
        await c.fetch(["python"], "US", "today 12-m")
    assert google.count("/explore") == 1 and c.pool.burned == 0


async def test_server_error_retries_on_same_session(make_client, google):
    google.script = [("/explore", 503, "busy")]
    c = make_client()
    await c.fetch(["python"], "US", "today 12-m", sections=("interestOverTime",))
    assert c.pool.burned == 0 and c.pool.created == 1


async def test_one_failed_section_keeps_the_rest(make_client, google):
    google.script = [("/comparedgeo", 429, "")] * 10
    c = make_client(max_retries=1)
    r = await c.fetch(["python"], "US", "today 12-m")
    assert r["hasData"] and len(r["interestOverTime"]) == 53
    assert "interestByRegion" not in r
    assert any(e.startswith("interestByRegion") for e in r["partialErrors"])


async def test_unsupported_region_level_falls_back_without_burning_ips(make_client, google):
    # Google answers an unsupported resolution with 429, not 400.
    google.script = [("/comparedgeo", 429, ""), ("/comparedgeo", 429, "")]
    c = make_client([tier(0), tier(1)], escalate_after=1)
    r = await c.fetch(["pizza"], "US-NY", "today 3-m", sections=("interestByRegion",), resolution="CITY")
    assert r["regionResolutionUsed"] == "REGION"          # the widget's default from the fixture
    assert len(r["interestByRegion"]) == 51
    assert c.pool.burned == 0 and c.pool.tier == 0         # no IPs wasted, no residential escalation


async def test_supported_region_level_is_used(make_client, google):
    r = await make_client().fetch(["pizza"], "US", "today 3-m", sections=("interestByRegion",), resolution="CITY")
    assert r["regionResolutionUsed"] == "CITY"
    assert "coordinates" in r["interestByRegion"][0]


async def test_withheld_topics_retry_once_on_the_next_tier(make_client, google):
    c = make_client([tier(0), tier(1)])
    real_topics = json.dumps({"default": {"rankedList": [
        {"rankedKeyword": [{"topic": {"mid": "/m/1", "title": "Python", "type": "Language"}, "value": 100,
                            "formattedValue": "100"}]}, {"rankedKeyword": []}]}})

    original = google.handler

    def handler(request):
        if c.pool.tier == 1 and "relatedsearches" in request.url.path and "ENTITY" in request.url.params["req"]:
            google.topics_body = ")]}',\n" + real_topics
        return original(request)

    google.handler = handler
    c.pool.transport = __import__("httpx").MockTransport(handler)
    r = await c.fetch(["python"], "US", "today 12-m", sections=("relatedTopics",))
    assert c.pool.tier == 1                                # explore fixture is flagged USER_TYPE_SCRAPER
    assert r["relatedTopics"]["top"][0]["title"] == "Python"


async def test_withheld_topics_without_more_tiers_returns_empty(make_client, google):
    r = await make_client([tier(0)]).fetch(["python"], "US", "today 12-m", sections=("relatedTopics",))
    assert r["relatedTopics"] == {"top": [], "rising": []} and r["hasData"] is False


async def test_trending_now(make_client):
    items = await make_client().trending_now("US")
    assert len(items) == 3 and items[0]["rank"] == 1
