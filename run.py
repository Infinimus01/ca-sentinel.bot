#!/usr/bin/env python3
"""CA Sentinel — CLI entrypoint.

    python run.py run              start monitoring (default)
    python run.py check <url>      one-shot parse of a URL, no DB, no alerts
    python run.py test-telegram    send a test message
    python run.py stats            print stored counters
    python run.py parsers          list registered site parsers
    python run.py recent [N]       show the most recently seen addresses
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from sentinel.logging_setup import setup_logging


def _cfg(args):
    from sentinel.config import load_config

    return load_config(args.config, args.env)


# --------------------------------------------------------------------------
async def cmd_run(args) -> int:
    from sentinel.app import main

    cfg = _cfg(args)
    setup_logging(cfg.log_dir, cfg.log_level)
    await main(cfg)
    return 0


async def cmd_check(args) -> int:
    """Fetch one URL and show exactly what the parser would extract."""
    from sentinel.config import DEFAULT_UA
    from sentinel.http import HttpClient
    from sentinel.sites import get_parser, load_all

    setup_logging(os.environ.get("LOG_DIR", "logs"), args.log_level)
    load_all()

    http = HttpClient(user_agent=DEFAULT_UA)
    try:
        started = time.perf_counter()
        result = await http.conditional_get(args.url)
        elapsed = (time.perf_counter() - started) * 1000

        if result.error:
            print(f"FETCH FAILED: {result.error}")
            return 1

        print(f"URL            {result.url}")
        print(f"Status         {result.status}")
        print(f"Bytes          {len(result.body)}")
        print(f"ETag           {result.etag}")
        print(f"Last-Modified  {result.last_modified}")
        print(f"SHA-256        {result.body_sha256[:32]}…")
        print(f"Round trip     {elapsed:.0f} ms")
        print()

        parser = get_parser(args.parser, args.url)
        print(f"Parser         {type(parser).__name__} (site_id={parser.site_id})")
        candidates = parser.parse(result)

        if not candidates:
            print("\nNo contract addresses found.")
        else:
            print(f"\n{len(candidates)} candidate(s):\n")
            for cand in candidates:
                flag = "ALERT" if cand.confidence >= args.min_confidence else "below"
                print(f"  [{flag:5}] {cand.confidence:5.0%}  {cand.chain:7}  {cand.address}")
                print(f"           via {cand.context}")
                if cand.label:
                    label = cand.label if len(cand.label) <= 90 else cand.label[:87] + "…"
                    print(f"           label: {label}")
                print()

        links = parser.discover_links(result)
        if links:
            print(f"{len(links)} internal link(s) for the crawl frontier:")
            for link in sorted(links):
                print(f"  {link}")
        return 0
    finally:
        await http.aclose()


async def cmd_test_telegram(args) -> int:
    from sentinel.notify import TelegramNotifier

    cfg = _cfg(args)
    setup_logging(cfg.log_dir, cfg.log_level)
    notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
    try:
        username = await notifier.verify()
        print(f"Authenticated as @{username}")
        await notifier.send(
            "✅ <b>CA Sentinel</b> is wired up correctly.\n"
            "<i>This is a test message; monitoring has not started.</i>"
        )
        print(f"Test message delivered to chat {cfg.telegram_chat_id}")
        return 0
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        await notifier.aclose()


async def cmd_stats(args) -> int:
    from sentinel.store import Store

    cfg = _cfg(args)
    store = Store(cfg.db_path)
    for key, value in store.stats().items():
        print(f"{key:16} {value}")
    store.close()
    return 0


async def cmd_recent(args) -> int:
    from sentinel.store import Store

    cfg = _cfg(args)
    store = Store(cfg.db_path)
    rows = store._conn.execute(  # noqa: SLF001 - read-only CLI helper
        """SELECT site_id, chain, address, first_url, context, confidence,
                  first_seen, sightings, alerted
           FROM seen_addresses ORDER BY first_seen DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("No addresses recorded yet.")
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["first_seen"]))
        mark = "ALERTED" if row["alerted"] else "seeded "
        print(f"{stamp}  {mark}  {row['chain']:7} {row['address']}")
        print(f"{'':21}  {row['site_id']} · {row['context']} · "
              f"{row['confidence']:.0%} · {row['sightings']} sighting(s)")
        print(f"{'':21}  {row['first_url']}")
    store.close()
    return 0


async def cmd_parsers(args) -> int:
    from sentinel.sites import load_all, registered

    load_all()
    print("Registered site parsers:")
    for name in registered():
        print(f"  {name}")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ca-sentinel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("run", help="start monitoring")

    check = sub.add_parser("check", help="one-shot parse of a URL")
    check.add_argument("url")
    check.add_argument("--parser", default="generic", help="parser site_id to use")
    check.add_argument("--min-confidence", type=float, default=0.6, dest="min_confidence")
    check.add_argument("--log-level", default="WARNING", dest="log_level")

    sub.add_parser("test-telegram", help="verify the bot token and chat id")
    sub.add_parser("stats", help="print stored counters")
    sub.add_parser("parsers", help="list registered site parsers")

    recent = sub.add_parser("recent", help="show recently seen addresses")
    recent.add_argument("limit", nargs="?", type=int, default=20)

    return ap


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "test-telegram": cmd_test_telegram,
    "stats": cmd_stats,
    "parsers": cmd_parsers,
    "recent": cmd_recent,
}


def main() -> int:
    args = build_parser().parse_args()
    handler = COMMANDS[args.command or "run"]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
