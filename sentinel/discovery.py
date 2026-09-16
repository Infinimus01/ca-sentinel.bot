"""Finding pages that are not linked from anywhere yet.

Three independent channels, cheapest and highest-signal first:

1. Link crawling — handled inline by the watcher, since a new page usually
   gets linked the moment it goes live.
2. Route probing — a paced wordlist sweep for unlinked pages. Calibrated
   against a random path first so soft-404 sites (which answer 200 for
   everything) are handled correctly.
3. Certificate Transparency — public CT logs surface a new subdomain as soon
   as its TLS certificate is issued, which is typically before it is linked.

All three use ordinary unauthenticated requests to public endpoints. Nothing
here attempts to get past authentication, a CAPTCHA, or any access control.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import string
from urllib.parse import urljoin, urlparse

from .config import AppConfig, SiteConfig
from .http import HttpClient
from .store import Store
from .watcher import SiteRuntime

log = logging.getLogger(__name__)

CRT_SH = "https://crt.sh/?q=%25.{domain}&output=json"


def _random_path() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=22))


class RouteProber:
    """Wordlist sweep with soft-404 calibration."""

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/") + "/"
        self.soft_404 = False
        self.miss_fingerprint: str | None = None

    async def calibrate(self) -> None:
        """Learn what a miss looks like on this host."""
        url = urljoin(self.base_url, _random_path())
        result = await self.http.conditional_get(url)
        if result.status == 200:
            self.soft_404 = True
            self.miss_fingerprint = self._fingerprint(result.body)
            log.info("%s uses soft 404s; comparing bodies instead of status", self.base_url)
        else:
            log.info("%s returns hard %s for unknown paths", self.base_url, result.status or "error")

    @staticmethod
    def _fingerprint(body: str) -> str:
        # Length-bucketed hash: tolerates a 404 page that echoes the path.
        normalized = " ".join(body.split())
        return hashlib.sha256(f"{len(normalized) // 256}:{normalized[:2000]}".encode()).hexdigest()

    async def probe(self, path: str) -> tuple[int, str]:
        """Return (status, resolved_url) if `path` is a real page, else (0, url).

        The resolved URL is the one after redirects, so a hit on `/secretarea`
        is added as `/secretarea/` and does not become a second target for a
        page already being watched.
        """
        url = urljoin(self.base_url, path.lstrip("/"))

        if not self.soft_404:
            status, final_url = await self.http.head_status(url)
            return (status, final_url) if 200 <= status < 400 else (0, url)

        result = await self.http.conditional_get(url)
        if result.status != 200 or not result.body:
            return 0, url
        if self._fingerprint(result.body) == self.miss_fingerprint:
            return 0, url
        return 200, result.url


async def probe_routes_loop(
    runtime: SiteRuntime, cfg: AppConfig, http: HttpClient, store: Store,
    stop: asyncio.Event,
) -> None:
    site: SiteConfig = runtime.site
    disc = site.discovery
    if not disc.probe_routes:
        return

    words = list(dict.fromkeys(list(disc.route_wordlist) + list(runtime.parser.route_hints)))
    prober = RouteProber(http, site.base_url)
    log.info("route probing enabled for %s: %d candidates every %.0fs",
             site.id, len(words), disc.probe_interval)

    first_run = True
    while not stop.is_set():
        try:
            if first_run:
                await prober.calibrate()
                first_run = False

            for word in words:
                if stop.is_set():
                    return
                # A static host usually serves a page as a directory, so check
                # the trailing-slash form too.
                for candidate in (word, f"{word}/"):
                    status, url = await prober.probe(candidate)
                    if status:
                        added = await runtime.add_target(
                            url, disc.discovered_interval,
                            discovered=True, source="route_probe",
                        )
                        if added:
                            log.warning("route probe found %s (%d)", url, status)
                        break
                    await asyncio.sleep(disc.probe_delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("route probe cycle failed for %s", site.id)

        try:
            await asyncio.wait_for(stop.wait(), timeout=disc.probe_interval)
            return
        except asyncio.TimeoutError:
            pass


async def ct_logs_loop(
    runtime: SiteRuntime, cfg: AppConfig, http: HttpClient, store: Store,
    stop: asyncio.Event,
) -> None:
    """Watch public CT logs for new subdomains on the site's apex domain."""
    site = runtime.site
    disc = site.discovery
    if not disc.ct_logs:
        return

    host = (urlparse(site.base_url).hostname or "").lower().removeprefix("www.")
    if not host:
        return

    log.info("CT log monitoring enabled for *.%s every %.0fs", host, disc.ct_interval)
    meta_key = f"ct_seen:{site.id}"

    while not stop.is_set():
        try:
            records = await http.get_json(CRT_SH.format(domain=host), timeout=45.0)
            found: set[str] = set()
            for record in records or []:
                for name in str(record.get("name_value", "")).split("\n"):
                    name = name.strip().lower().lstrip("*.")
                    if name.endswith(host) and name:
                        found.add(name)

            known = set(filter(None, (store.get_meta(meta_key) or "").split(",")))
            fresh = found - known
            if known:
                for name in sorted(fresh):
                    url = f"https://{name}/"
                    added = await runtime.add_target(
                        url, disc.discovered_interval, discovered=True,
                        source="ct_log", resolve=True,
                    )
                    if added:
                        log.warning("CT log surfaced new subdomain: %s", name)
            store.set_meta(meta_key, ",".join(sorted(found | known)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CT log check failed for %s: %s", host, exc)

        try:
            await asyncio.wait_for(stop.wait(), timeout=disc.ct_interval)
            return
        except asyncio.TimeoutError:
            pass
