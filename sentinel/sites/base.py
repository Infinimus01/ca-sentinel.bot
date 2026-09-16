"""The site-parser contract.

A site plugin is one class with a `site_id` and a `parse()` method. Everything
else — fetching, change detection, validation, dedup, delivery, persistence —
is handled by the core and is identical for every site.
"""

from __future__ import annotations

from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from .. import extract
from ..models import Candidate, FetchResult


class SiteParser:
    #: Matches the `parser:` key in config.yaml.
    site_id: str = "generic"

    #: CSS selectors known to hold a contract address on this site. Hits here
    #: get the highest confidence score.
    ca_selectors: tuple[str, ...] = ()

    #: Extra hostnames (beyond the configured base_url host) whose pages may be
    #: crawled for this site.
    extra_hosts: tuple[str, ...] = ()

    #: Route names to probe when discovery is enabled, in addition to the
    #: global wordlist.
    route_hints: tuple[str, ...] = ()

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()
        self.allowed_hosts = {host, f"www.{host}".replace("www.www.", "www.")} | {
            h.lower() for h in self.extra_hosts
        }
        self.allowed_hosts.discard("")

    # ------------------------------------------------------------------
    # Hooks a plugin may override
    # ------------------------------------------------------------------
    def parse(self, result: FetchResult) -> list[Candidate]:
        """Return every contract-address candidate on the page.

        The default walks the generic pipeline and then upgrades anything found
        in `ca_selectors` to anchored confidence.
        """
        tree = HTMLParser(result.body)
        found: list[Candidate] = []

        for selector in self.ca_selectors:
            found += extract.from_selector(tree, selector, result.url)

        found += extract.from_links(tree, result.url)
        found += extract.from_labelled_text(tree, result.url)
        found += extract.from_code_blocks(tree, result.url)
        found += extract.from_body(tree, result.url)
        return extract.dedupe_candidates(found)

    def discover_links(self, result: FetchResult) -> set[str]:
        """Same-site URLs to add to the watch list."""
        return extract.internal_links(result.body, result.url, self.allowed_hosts)

    def should_watch(self, url: str) -> bool:
        """Filter for discovered URLs. Override to skip noisy sections."""
        path = urlparse(url).path.lower()
        return not path.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
             ".css", ".js", ".woff", ".woff2", ".ttf", ".mp4", ".pdf", ".zip")
        )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
_REGISTRY: dict[str, type[SiteParser]] = {}


def register(cls: type[SiteParser]) -> type[SiteParser]:
    """Class decorator that makes a parser available to config.yaml."""
    if not getattr(cls, "site_id", None):
        raise ValueError(f"{cls.__name__} must define site_id")
    _REGISTRY[cls.site_id] = cls
    return cls


def get_parser(site_id: str, base_url: str) -> SiteParser:
    """Look up a parser, falling back to the generic one."""
    cls = _REGISTRY.get(site_id)
    if cls is None:
        from .generic import GenericParser

        cls = GenericParser
    return cls(base_url)


def registered() -> list[str]:
    return sorted(_REGISTRY)
