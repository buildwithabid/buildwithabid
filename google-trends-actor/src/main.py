"""Apify entry point: read input, set up proxies, run, report."""
from __future__ import annotations

from typing import Any

from apify import Actor

from .config import parse_input
from .run import run
from .trends import ProxyUrlFn, SessionPool, TrendsClient


class ApifyCharger:
    async def push_data(self, data: Any, charged_event_name: str | None = None) -> Any:
        return await Actor.push_data(data, charged_event_name=charged_event_name)

    def limit_reached(self, event_name: str) -> bool:
        return Actor.get_charging_manager().is_event_charge_limit_reached(event_name)


async def _proxy_tiers(proxy_input: dict[str, Any], escalate: bool) -> list[ProxyUrlFn]:
    tiers: list[ProxyUrlFn] = []
    primary = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
    if primary is not None:
        tiers.append(lambda sid, p=primary: p.new_url(session_id=sid))
    uses_residential = "RESIDENTIAL" in (proxy_input.get("apifyProxyGroups") or [])
    if escalate and proxy_input.get("useApifyProxy") and not uses_residential:
        residential = await Actor.create_proxy_configuration(
            actor_proxy_input={"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"],
                               "apifyProxyCountry": proxy_input.get("apifyProxyCountry")})
        if residential is not None:
            tiers.append(lambda sid, p=residential: p.new_url(session_id=sid))
    return tiers


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        try:
            cfg = parse_input(raw)
        except ValueError as e:
            await Actor.fail(status_message=f"Invalid input: {e}")
            return

        tiers = await _proxy_tiers(cfg.proxy or {}, cfg.escalate_to_residential)
        pool = SessionPool(tiers, language=cfg.language)
        client = TrendsClient(pool, language=cfg.language, tz_offset_min=cfg.timezone_offset,
                              max_retries=cfg.max_retries, log=Actor.log)
        what = (f"{len(cfg.trending_geos)} location(s)" if cfg.mode == "trendingNow"
                else f"{len(cfg.groups())} search term group(s)")
        Actor.log.info(f"Mode {cfg.mode}: {what}, proxy tiers: {len(tiers) or 'direct'}")
        await Actor.set_status_message(f"Fetching Google Trends for {what}...")
        try:
            summary = await run(cfg, client, ApifyCharger(), Actor.log)
        finally:
            await pool.close()
        Actor.log.info(f"{summary.message()} Requests: {client.requests}, sessions: {pool.created}, "
                       f"blocked sessions replaced: {pool.burned}, final proxy tier: {pool.tier}")
        await Actor.set_status_message(summary.message(), is_terminal=True)
