"""suppoman.com parser.

Built from a live inspection of the site (2026-09-16):

* Plain hand-written static HTML on Hostinger. No framework, no hydration, no
  JSON API — `script.js` is a canvas background plus a clipboard helper and
  makes no network calls. Contract addresses are literal text in the markup,
  so plain HTTP fetching sees everything a browser would.
* `/` holds the CA in `<code id="contractText">` inside `.ca-box`, under a
  "CONTRACT ADDRESS" heading.
* `/secretarea/` holds it in `<div class="contract" id="ca">`, under a "Latest
  Gem" card with an `<h2>` title and a NEW/updated status pill.
* The chain is not stated anywhere and has been inconsistent: the homepage CA
  box has held an EVM address while the buy buttons pointed at a Solana mint
  via `jup.ag?buy=` and a `dexscreener.com/solana/<pair>` chart. Both surfaces
  are therefore extracted, and the alert reports whichever chain each address
  actually belongs to rather than assuming one.
* Pages are served with `Last-Modified` and a size+mtime `ETag`, and HTML
  responses are CDN pass-through (`x-hcdn-cache-status: DYNAMIC`), so a
  conditional GET is both cheap and never stale.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from .. import extract
from ..models import Candidate, FetchResult
from .base import SiteParser, register


@register
class SuppomanParser(SiteParser):
    site_id = "suppoman"

    ca_selectors = (
        "#ca",              # /secretarea/
        "#contractText",    # /
        ".ca-box code",
        ".contract",
    )

    #: Probed in addition to the global wordlist. The operator runs a
    #: "secret area" naming scheme, so sibling names are the likeliest place a
    #: second drop page appears.
    route_hints = (
        "secretarea2", "secretarea/2", "secret-area", "secretarea/new",
        "secretgem", "secretgems", "gem", "gems", "gemdrop", "gemdrops",
        "drop", "drops", "pick", "picks", "alpha", "alphaarea",
        "vip", "viparea", "insider", "insiders", "private", "privatearea",
        "members", "membersarea", "next", "new", "latest", "call", "calls",
    )

    def parse(self, result: FetchResult) -> list[Candidate]:
        tree = HTMLParser(result.body)
        found: list[Candidate] = []

        # Anchored selectors first — these are the boxes the site is built to
        # display a CA in, so a hit here is as certain as it gets.
        for selector in self.ca_selectors:
            found += extract.from_selector(tree, selector, result.url)

        # Buy/chart links carry the real tradeable mint, which has at times
        # differed from the address printed in the CA box.
        found += extract.from_links(tree, result.url)

        # Anything the site adds in a shape we have not seen before.
        found += extract.from_labelled_text(tree, result.url)
        found += extract.from_code_blocks(tree, result.url)
        found += extract.from_body(tree, result.url)

        candidates = extract.dedupe_candidates(found)

        gem_title = self._gem_title(tree)
        if gem_title:
            for cand in candidates:
                if not cand.label or cand.label == cand.address:
                    cand.label = gem_title
        return candidates

    @staticmethod
    def _gem_title(tree: HTMLParser) -> str:
        """The card heading next to the CA, e.g. 'New Secret Pick'."""
        for selector in (".card .top h2", ".card h2", ".info-box h3", "h2"):
            node = tree.css_first(selector)
            if node:
                text = (node.text() or "").strip()
                if text:
                    return text[:120]
        return ""
