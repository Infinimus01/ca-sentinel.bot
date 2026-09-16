"""Site plugins.

Every module in this package is imported at startup, so dropping a new file in
here that uses `@register` is all it takes to make a parser selectable from
config.yaml. No core file needs editing.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import SiteParser, get_parser, register, registered

log = logging.getLogger(__name__)

__all__ = ["SiteParser", "get_parser", "register", "registered", "load_all"]


def load_all() -> list[str]:
    """Import every sibling module so its @register decorators run."""
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_") or module.name == "base":
            continue
        try:
            importlib.import_module(f"{__name__}.{module.name}")
        except Exception:
            log.exception("failed to load site plugin %r", module.name)
    names = registered()
    log.info("loaded %d site parsers: %s", len(names), ", ".join(names))
    return names
