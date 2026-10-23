import pytest

from src.trends import (TrendsError, explore_url, parse_json, parse_regions, parse_related, parse_timeline,
                        parse_trending_rss)

from conftest import fixture


def test_parse_json_strips_anti_hijacking_prefix():
    assert parse_json(")]}',\n{\"a\": 1}") == {"a": 1}
    assert parse_json(")]}'\n{\"b\": [1]}") == {"b": [1]}
    with pytest.raises(ValueError):
        parse_json("<html>consent page</html>")


def test_timeline_single_term():
    rows, avg, has_data = parse_timeline(parse_json(fixture("TIMESERIES.txt")), ["python"])
    assert len(rows) == 53 and has_data and avg is None
    assert rows[0] == {"date": "2025-10-19", "dateTime": "2025-10-19T00:00:00Z",
                       "formattedTime": rows[0]["formattedTime"], "isPartial": False, "value": 59}
    assert rows[-1]["isPartial"] is True       # the current week is incomplete


def test_timeline_comparison_maps_values_to_terms():
    rows, avg, has_data = parse_timeline(parse_json(fixture("TIMESERIES_cmp.txt")), ["chatgpt", "gemini"])
    assert has_data
    assert avg == {"chatgpt": 16, "gemini": 10}
    assert rows[0]["values"] == {"chatgpt": 9, "gemini": 9}
    assert "value" not in rows[0]


def test_single_noise_spike_is_not_data():
    data = {"default": {"timelineData": [
        {"time": "1700000000", "value": [0], "hasData": [True]},
        {"time": "1700604800", "value": [100], "hasData": [True]},
        {"time": "1701209600", "value": [0], "hasData": [True]},
    ]}}
    assert parse_timeline(data, ["zzqx"])[2] is False
    data["default"]["timelineData"][0]["value"] = [40]
    assert parse_timeline(data, ["zzqx"])[2] is True


def test_regions_single_and_comparison():
    single = parse_regions(parse_json(fixture("GEO_MAP.txt")), ["python"])
    assert len(single) == 51
    assert single[0] == {"geoName": "Wyoming", "geoCode": "US-WY", "value": 100}
    cmp = parse_regions(parse_json(fixture("GEO_MAP_cmp.txt")), ["chatgpt", "gemini"])
    assert cmp[0] == {"geoName": "China", "geoCode": "CN", "values": {"chatgpt": 78, "gemini": 22}}
    assert all(r["geoCode"] != "AD" for r in cmp)     # Andorra had no data and is dropped


def test_city_regions_drop_empty_rows_and_keep_coordinates():
    data = parse_json(fixture("GEO_MAP_city.txt"))
    raw = data["default"]["geoMapData"]
    kept = parse_regions(data, ["pizza"])
    assert 0 < len(kept) < len(raw)
    assert all("coordinates" in r and "geoCode" not in r for r in kept)
    assert len(parse_regions(data, ["pizza"], include_empty=True)) == len(raw)


def test_related_queries_top_and_rising():
    rel = parse_related(parse_json(fixture("RELATED_QUERIES.txt")), "queries")
    assert len(rel["top"]) == 25 and len(rel["rising"]) == 25
    assert rel["top"][0]["query"] == "what is python" and rel["top"][0]["value"] == 100
    assert rel["top"][0]["link"].startswith("https://trends.google.com/trends/explore?q=")
    breakout = [r for r in rel["rising"] if r["formattedValue"] == "Breakout"]
    assert breakout and breakout[0]["value"] >= 5000


def test_related_topics_shape_and_empty_response():
    assert parse_related(parse_json(fixture("RELATED_TOPICS.txt")), "topics") == {"top": [], "rising": []}
    data = {"default": {"rankedList": [
        {"rankedKeyword": [{"topic": {"mid": "/m/05z1_", "title": "Python", "type": "Programming language"},
                            "value": 100, "formattedValue": "100", "link": "/trends/explore?q=/m/05z1_"}]},
        {"rankedKeyword": []},
    ]}}
    rel = parse_related(data, "topics")
    assert rel["top"][0] == {"title": "Python", "type": "Programming language", "mid": "/m/05z1_", "value": 100,
                             "formattedValue": "100", "link": "https://trends.google.com/trends/explore?q=/m/05z1_"}


def test_trending_rss():
    items = parse_trending_rss(fixture("rss.xml"), "US")
    assert len(items) == 3
    first = items[0]
    assert first["rank"] == 1 and first["geo"] == "US" and first["title"]
    assert first["approxTraffic"].endswith("+") and isinstance(first["approxTrafficMin"], int)
    assert first["startedAt"].endswith("Z")
    assert first["news"] and first["news"][0]["url"].startswith("http")


def test_trending_rss_rejects_garbage_and_entity_tricks():
    with pytest.raises(TrendsError):
        parse_trending_rss("<html>not xml", "US")
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            "<rss><channel><item><title>&b;</title></item></channel></rss>")
    with pytest.raises(TrendsError):
        parse_trending_rss(bomb, "US")


def test_explore_url():
    assert explore_url(["a b", "c"], "US", "today 12-m", 7, "youtube") == (
        "https://trends.google.com/trends/explore?q=a+b%2Cc&date=today+12-m&geo=US&cat=7&gprop=youtube")
    assert explore_url(["x"], "", "all") == "https://trends.google.com/trends/explore?q=x&date=all"
