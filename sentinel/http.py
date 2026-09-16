"""HTTP layer: one pooled HTTP/2 client doing conditional GETs.

This is where the speed comes from. A conditional GET against an unchanged
page costs one round trip and returns a zero-byte 304, so a 2-second poll
interval is cheap for both sides. Connections are kept alive, so the steady
state is a single TCP+TLS handshake per host for the life of the process.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time

import httpx

from .models import FetchResult

log = logging.getLogger(__name__)


class Backoff:
    """Exponential backoff with full jitter, used per target on failure."""

    def __init__(self, base: float = 2.0, cap: float = 300.0) -> None:
        self.base = base
        self.cap = cap
        self.failures = 0

    def reset(self) -> None:
        self.failures = 0

    def next_delay(self) -> float:
        self.failures += 1
        raw = min(self.cap, self.base * (2 ** (self.failures - 1)))
        return random.uniform(raw / 2, raw)


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 10.0,
        max_connections: int = 32,
        verify_tls: bool = True,
    ) -> None:
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=120.0,
        )
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout, connect=timeout),
            limits=limits,
            follow_redirects=True,
            verify=verify_tls,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                # Accept-Encoding is deliberately left to httpx, which
                # advertises only the codecs it can actually decode. Hardcoding
                # "br" without the brotli package installed yields a body of
                # raw compressed bytes and silently finds nothing.
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def conditional_get(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        prev_sha256: str | None = None,
    ) -> FetchResult:
        """GET `url`, sending validators so an unchanged page answers 304.

        `changed` is True only when the body hash actually differs from
        `prev_sha256`. Some origins ignore validators or rotate weak ETags on
        recompression, so the hash is the authority and the validators are the
        cheap fast path.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        started = time.perf_counter()
        try:
            resp = await self._client.get(url, headers=headers)
        except Exception as exc:
            return FetchResult(
                url=url,
                status=0,
                changed=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        new_etag = resp.headers.get("etag")
        new_lm = resp.headers.get("last-modified")

        if resp.status_code == 304:
            return FetchResult(
                url=str(resp.url),
                status=304,
                changed=False,
                etag=new_etag or etag,
                last_modified=new_lm or last_modified,
                body_sha256=prev_sha256 or "",
                elapsed_ms=elapsed_ms,
            )

        if resp.status_code >= 400:
            return FetchResult(
                url=str(resp.url),
                status=resp.status_code,
                changed=False,
                elapsed_ms=elapsed_ms,
                error=f"http_{resp.status_code}",
            )

        body = resp.text
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        return FetchResult(
            url=str(resp.url),
            status=resp.status_code,
            changed=digest != prev_sha256,
            body=body,
            etag=new_etag,
            last_modified=new_lm,
            body_sha256=digest,
            elapsed_ms=elapsed_ms,
        )

    async def head_status(self, url: str) -> tuple[int, str]:
        """Cheap existence probe used by route discovery.

        Returns (status, final_url). The final URL matters: `/secretarea` and
        `/secretarea/` are one page behind a 301, and watching both would poll
        it twice. Status 0 means unreachable.
        """
        try:
            resp = await self._client.head(url)
            # Some hosts answer HEAD with 405; fall back to a ranged GET.
            if resp.status_code == 405:
                resp = await self._client.get(url, headers={"Range": "bytes=0-256"})
            return resp.status_code, str(resp.url)
        except Exception as exc:
            log.debug("head_status failed for %s: %s", url, exc)
            return 0, url

    async def get_json(self, url: str, timeout: float = 30.0):
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
