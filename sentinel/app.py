"""Orchestrator: wires config, store, HTTP, parsers, watchers and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from .config import AppConfig
from .discovery import ct_logs_loop, probe_routes_loop
from .http import HttpClient
from .notify import TelegramNotifier, outbox_worker
from .sites import load_all
from .store import Store
from .watcher import SiteRuntime

log = logging.getLogger(__name__)


class Sentinel:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.stop = asyncio.Event()
        self.store = Store(cfg.db_path)
        self.http = HttpClient(
            user_agent=cfg.user_agent,
            timeout=cfg.http_timeout,
            max_connections=cfg.max_connections,
        )
        self.notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
        self.runtimes: list[SiteRuntime] = []
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        load_all()

        try:
            username = await self.notifier.verify()
            log.info("telegram connected as @%s -> chat %s", username, self.cfg.telegram_chat_id)
        except Exception as exc:
            log.error("telegram verification failed: %s", exc)
            raise SystemExit(1) from exc

        if not self.cfg.sites:
            raise SystemExit("no enabled sites in config.yaml")

        self.tasks.append(
            asyncio.create_task(
                outbox_worker(self.store, self.notifier, self.stop), name="outbox"
            )
        )

        for site in self.cfg.sites:
            runtime = SiteRuntime(site, self.cfg, self.http, self.store, self.stop)
            self.runtimes.append(runtime)

            for target in site.targets:
                await runtime.add_target(
                    site.url_for(target.path),
                    target.interval,
                    priority=target.priority,
                    source="config",
                )

            if site.discovery.probe_routes:
                self.tasks.append(
                    asyncio.create_task(
                        probe_routes_loop(runtime, self.cfg, self.http, self.store, self.stop),
                        name=f"probe:{site.id}",
                    )
                )
            if site.discovery.ct_logs:
                self.tasks.append(
                    asyncio.create_task(
                        ct_logs_loop(runtime, self.cfg, self.http, self.store, self.stop),
                        name=f"ct:{site.id}",
                    )
                )

        self.tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))

        total_targets = sum(len(r.targets) for r in self.runtimes)
        log.info(
            "sentinel up: %d site(s), %d target(s), bootstrap=%s, min_confidence=%.2f",
            len(self.runtimes), total_targets, self.cfg.bootstrap, self.cfg.min_confidence,
        )

        await self.stop.wait()
        await self.shutdown()

    # ------------------------------------------------------------------
    async def _heartbeat(self) -> None:
        """Periodic health line, so a silent bot is visibly a healthy one."""
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=300)
                return
            except asyncio.TimeoutError:
                pass

            stats = self.store.stats()
            uptime = int(time.time() - self.started_at)
            log.info(
                "heartbeat uptime=%ds targets=%d checks=%d changes=%d "
                "addresses=%d alerted=%d pending=%d",
                uptime, sum(len(r.targets) for r in self.runtimes),
                stats["checks"], stats["changes"], stats["addresses"],
                stats["alerted"], stats["outbox_pending"],
            )

    # ------------------------------------------------------------------
    def request_stop(self, *_: object) -> None:
        if not self.stop.is_set():
            log.info("shutdown requested")
            self.stop.set()

    async def shutdown(self) -> None:
        log.info("shutting down...")
        for runtime in self.runtimes:
            await runtime.shutdown()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        await self.http.aclose()
        await self.notifier.aclose()

        stats = self.store.stats()
        log.info(
            "final stats: checks=%d changes=%d addresses=%d alerted=%d pending=%d",
            stats["checks"], stats["changes"], stats["addresses"],
            stats["alerted"], stats["outbox_pending"],
        )
        self.store.close()
        log.info("stopped cleanly")


async def main(cfg: AppConfig) -> None:
    sentinel = Sentinel(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, sentinel.request_stop)
    await sentinel.run()
