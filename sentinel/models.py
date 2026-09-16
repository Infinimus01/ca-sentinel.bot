"""Shared data structures passed between the core and site parsers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class FetchResult:
    """Outcome of one conditional GET."""

    url: str
    status: int
    changed: bool
    body: str = ""
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str = ""
    elapsed_ms: int = 0
    fetched_at: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status in (200, 304)


@dataclass(slots=True)
class Candidate:
    """A possible contract address, before validation and dedup."""

    address: str
    chain: str
    source_url: str
    #: Where in the page it came from, e.g. "selector:#ca" or "link:jup.ag".
    context: str = "body"
    #: 0.0-1.0. Anchored selectors and swap links score high; loose body text
    #: scores low. Alerts below `min_confidence` are stored but not sent.
    confidence: float = 0.5
    #: Short human-readable snippet shown in the alert.
    label: str = ""


@dataclass(slots=True)
class Target:
    """One URL being polled."""

    site_id: str
    url: str
    interval: float
    priority: str = "normal"
    #: True when discovery found it rather than the operator configuring it.
    discovered: bool = False

    @property
    def key(self) -> str:
        return f"{self.site_id}|{self.url}"


@dataclass(slots=True)
class Alert:
    site_id: str
    chain: str
    address: str
    source_url: str
    context: str
    confidence: float
    label: str = ""
    kind: str = "new_ca"  # new_ca | new_route | ca_changed
    extra: str = ""
