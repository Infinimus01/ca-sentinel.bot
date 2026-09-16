"""Fallback parser.

Works on any HTML page without site-specific knowledge: swap links, labelled
text, code blocks and loose body matches. Good enough that many new sites need
no plugin at all — just a config entry with `parser: generic`.
"""

from __future__ import annotations

from .base import SiteParser, register


@register
class GenericParser(SiteParser):
    site_id = "generic"
    ca_selectors = (
        "#ca",
        "#contract",
        "#contractAddress",
        "#contractText",
        "#token-address",
        ".contract-address",
        ".contract",
        ".ca-box code",
        "[data-contract]",
        "[data-ca]",
    )
