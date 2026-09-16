"""Per-target polling loop and the candidate ingest pipeline."""

from __future__ import annotations

import asyncio
import logging
import random
import time

from .config import AppConfig, SiteConfig
from .http import Backoff, HttpClient
from .models import Alert, Candidate, FetchResult, Target
from .sites import get_parser
from .sites.base import SiteParser
from .store import Store

log = logging.getLogger(__name__)
hits_log = logging.getLogger("sentinel.hits")


class SiteRuntime:
    """Owns one site: its parser, its live targets, and their poll tasks."""

    def __init__(
        self,
        site: SiteConfig,
        cfg: AppConfig,
        http: HttpClient,
        store: Store,
        stop: asyncio.Event,
    ) -> None:
        self.site = site
        self.cfg = cfg
        self.http = http
        self.store = store
        self.stop = stop
        self.parser: SiteParser = get_parser(site.parser, site.base_url)
        self.targets: dict[str, Target] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

        log.info(
            "site %s -> parser %s (%s), hosts %s",
            site.id, site.parser, type(self.parser).__name__,
            ", ".join(sorted(self.parser.allowed_hosts)),
        )

    # ------------------------------------------------------------------
    # Target management
    # ------------------------------------------------------------------
    async def add_target(
        self,
        url: str,
        interval: float,
        *,
        priority: str = "normal",
        discovered: bool = False,
        source: str = "config",
        resolve: bool = False,
    ) -> bool:
        """Start polling `url`. Returns True if it was newly added.

        With `resolve`, redirects are followed first so that `/secretarea` and
        `/secretarea/` collapse to the one page the server actually serves,
        instead of becoming two targets polling the same content.
        """
        if f"{self.site.id}|{url}" in self.targets:
            return False

        if resolve:
            status, final_url = await self.http.head_status(url)
            if not (200 <= status < 400):
                log.debug("skipping %s (status %s)", url, status)
                return False
            url = final_url

        target = Target(
            site_id=self.site.id, url=url, interval=interval,
            priority=priority, discovered=discovered,
        )

        async with self._lock:
            if target.key in self.targets:
                return False
            if discovered and len(self.targets) >= self.site.discovery.max_watched:
                log.warning(
                    "site %s at max_watched=%d; not adding %s",
                    self.site.id, self.site.discovery.max_watched, url,
                )
                return False

            self.targets[target.key] = target
            self.tasks[target.key] = asyncio.create_task(
                self._poll_loop(target), name=f"poll:{target.key}"
            )

        is_new_route = self.store.add_route(self.site.id, url, 0, source)
        self.store.mark_watched(self.site.id, url)
        log.info(
            "watching %s every %.1fs (%s%s)",
            url, interval, source, ", NEW ROUTE" if is_new_route and discovered else "",
        )

        if is_new_route and discovered and self.cfg.alert_on_new_route:
            self.store.enqueue_alert(
                Alert(
                    site_id=self.site.id, chain="", address="", source_url=url,
                    context=source, confidence=1.0, kind="new_route",
                    extra=f"found via {source}; now polling every {interval:.0f}s",
                )
            )
        return True

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    async def _poll_loop(self, target: Target) -> None:
        backoff = Backoff(base=2.0, cap=300.0)

        while not self.stop.is_set():
            try:
                delay = await self._poll_once(target, backoff)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unhandled error polling %s", target.url)
                delay = backoff.next_delay()

            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
                return  # stop was set
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, target: Target, backoff: Backoff) -> float:
        """One conditional GET. Returns how long to sleep before the next."""
        state = self.store.get_page_state(target.key)
        first_pass = state is None

        result = await self.http.conditional_get(
            target.url,
            etag=state["etag"] if state else None,
            last_modified=state["last_modified"] if state else None,
            prev_sha256=state["body_sha256"] if state else None,
        )

        if result.error:
            log.warning("fetch failed %s: %s", target.url, result.error)
            return backoff.next_delay()

        backoff.reset()
        self.store.save_page_state(
            target.key, self.site.id, target.url, result.etag, result.last_modified,
            result.body_sha256, result.status, result.changed,
        )

        if not result.changed:
            return self._next_interval(target)

        log.info(
            "CHANGE detected %s (status=%d, %d bytes, %dms)%s",
            target.url, result.status, len(result.body), result.elapsed_ms,
            " [first pass]" if first_pass else "",
        )
        await self._handle_change(target, result, first_pass)
        return self._next_interval(target)

    def _next_interval(self, target: Target) -> float:
        spread = target.interval * self.cfg.jitter
        return max(0.5, target.interval + random.uniform(-spread, spread))

    async def _handle_change(
        self, target: Target, result: FetchResult, first_pass: bool
    ) -> None:
        try:
            candidates = self.parser.parse(result)
        except Exception:
            log.exception("parser %s failed on %s", self.site.parser, target.url)
            return

        # On a target's very first successful fetch, everything present is
        # pre-existing rather than new. Seeding silently is what makes the
        # first alert a real signal instead of a dump of the current page.
        suppress = first_pass and self.cfg.bootstrap == "seed"

        for cand in candidates:
            if cand.confidence < self.cfg.min_confidence:
                log.debug(
                    "below min_confidence (%.2f < %.2f): %s from %s",
                    cand.confidence, self.cfg.min_confidence, cand.address, cand.context,
                )
                continue

            alert = self.store.record_candidate(
                self.site.id, cand, suppress_alert=suppress
            )
            if alert is not None:
                hits_log.info(
                    "NEW site=%s chain=%s address=%s url=%s context=%s conf=%.2f",
                    self.site.id, cand.chain, cand.address, cand.source_url,
                    cand.context, cand.confidence,
                )
                log.warning(
                    "🚨 NEW CA site=%s chain=%s address=%s (%s, conf=%.0f%%)",
                    self.site.id, cand.chain, cand.address, cand.context,
                    cand.confidence * 100,
                )
            elif suppress:
                log.info(
                    "seeded existing %s %s from %s",
                    cand.chain, cand.address, cand.source_url,
                )

        if self.site.discovery.crawl_links:
            await self._crawl_links(result)

    async def _crawl_links(self, result: FetchResult) -> None:
        try:
            links = self.parser.discover_links(result)
        except Exception:
            log.exception("link discovery failed for %s", result.url)
            return

        for url in links:
            if not self.parser.should_watch(url):
                continue
            await self.add_target(
                url,
                self.site.discovery.discovered_interval,
                discovered=True,
                source="link_crawl",
                resolve=True,
            )

    async def shutdown(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
