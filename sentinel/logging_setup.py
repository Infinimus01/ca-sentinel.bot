"""Logging: human-readable console plus a rotating file log."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FMT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO") -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        directory / "sentinel.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(logging.Formatter(FMT, DATEFMT))
    root.addHandler(rotating)

    # A dedicated, never-rotated-away record of every hit.
    hits = logging.getLogger("sentinel.hits")
    hits_handler = logging.handlers.RotatingFileHandler(
        directory / "hits.log", maxBytes=5 * 1024 * 1024, backupCount=20,
        encoding="utf-8",
    )
    hits_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", DATEFMT))
    hits.addHandler(hits_handler)
    hits.propagate = True

    for noisy in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
