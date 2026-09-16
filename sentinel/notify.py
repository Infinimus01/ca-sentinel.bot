"""Telegram delivery.

Send-only, so this talks to the Bot API over httpx directly rather than
pulling in a full bot framework. A background task drains the SQLite outbox,
which is what makes delivery survive restarts and network failures.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time

import httpx

from .chains import explorer_links
from .models import Alert

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def format_alert(alert: Alert) -> str:
    """Build the HTML-formatted Telegram message."""
    if alert.kind == "new_route":
        return (
            "🛰 <b>NEW ROUTE DISCOVERED</b>\n"
            f"<b>Site:</b> {html.escape(alert.site_id)}\n"
            f"<b>URL:</b> {html.escape(alert.source_url)}\n"
            f"<i>{html.escape(alert.extra or 'now being monitored')}</i>"
        )

    chain_icon = {"solana": "◎", "evm": "⬢"}.get(alert.chain, "•")
    confidence_bar = "🟢" if alert.confidence >= 0.9 else "🟡" if alert.confidence >= 0.7 else "🟠"

    lines = [
        "🚨 <b>NEW CONTRACT ADDRESS</b>",
        "",
        f"<b>Site:</b> {html.escape(alert.site_id)}",
        f"<b>Chain:</b> {chain_icon} {html.escape(alert.chain.upper())}",
        f"<b>Page:</b> {html.escape(alert.source_url)}",
        "",
        f"<code>{html.escape(alert.address)}</code>",
        "",
        f"{confidence_bar} <b>Confidence:</b> {alert.confidence:.0%}  "
        f"<b>Source:</b> {html.escape(alert.context)}",
    ]

    if alert.label:
        snippet = alert.label.strip()
        if len(snippet) > 160:
            snippet = snippet[:157] + "…"
        lines.append(f"<i>{html.escape(snippet)}</i>")

    links = " · ".join(
        f'<a href="{html.escape(url)}">{html.escape(name)}</a>'
        for name, url in explorer_links(alert.address, alert.chain)
    )
    if links:
        lines += ["", links]

    lines += ["", "<i>Unverified. Confirm the contract yourself before acting.</i>"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 15.0,
        min_interval: float = 1.2,
        disable_preview: bool = True,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.min_interval = min_interval
        self.disable_preview = disable_preview
        self._last_sent = 0.0
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify(self) -> str:
        """Confirm the token works; returns the bot username."""
        resp = await self._client.get(f"{API}/bot{self.token}/getMe")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getMe failed: {data}")
        return data["result"].get("username", "?")

    async def send(self, text: str) -> None:
        """Send one message. Raises on failure so the outbox can retry.

        Telegram's per-chat limit is roughly 20 messages/minute; `min_interval`
        paces sends below that, and an explicit 429 `retry_after` is honoured.
        """
        wait = self.min_interval - (time.monotonic() - self._last_sent)
        if wait > 0:
            await asyncio.sleep(wait)

        resp = await self._client.post(
            f"{API}/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": self.disable_preview,
            },
        )
        self._last_sent = time.monotonic()

        if resp.status_code == 429:
            retry_after = 5
            try:
                retry_after = int(resp.json()["parameters"]["retry_after"])
            except Exception:
                pass
            raise RateLimited(retry_after)

        if resp.status_code >= 400:
            raise RuntimeError(f"telegram {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram error: {payload}")


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited, retry in {retry_after}s")
        self.retry_after = retry_after


async def outbox_worker(store, notifier: TelegramNotifier, stop: asyncio.Event) -> None:
    """Drain pending alerts until stopped. Never raises out of the loop."""
    log.info("outbox worker started")
    while not stop.is_set():
        try:
            pending = store.pending_alerts(limit=10)
            if not pending:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue

            for row in pending:
                if stop.is_set():
                    break
                alert = Alert(**json.loads(row["payload"]))
                try:
                    await notifier.send(format_alert(alert))
                    store.mark_sent(row["id"])
                    log.info(
                        "alert delivered site=%s chain=%s address=%s",
                        alert.site_id, alert.chain, alert.address,
                    )
                except RateLimited as exc:
                    store.mark_failed(row["id"], exc.retry_after + 1)
                    log.warning("telegram rate limited; backing off %ss", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                except Exception as exc:
                    attempts = row["attempts"] + 1
                    delay = min(600, 5 * (2 ** min(attempts, 7)))
                    store.mark_failed(row["id"], delay)
                    log.error(
                        "alert delivery failed (attempt %d, retry in %ds): %s",
                        attempts, delay, exc,
                    )
        except Exception:
            log.exception("outbox worker iteration failed")
            await asyncio.sleep(2)

    log.info("outbox worker stopped")
