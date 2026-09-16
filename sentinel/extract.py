"""Generic, site-agnostic contract-address extraction.

Site parsers build on these helpers and add their own anchored selectors. The
confidence score is what keeps the signal clean: an address sitting inside an
element that says "contract address", or inside a Jupiter/Dexscreener swap
link, is worth alerting on; a base58-looking token in a random script blob is
not.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

from selectolax.parser import HTMLParser

from .chains import (
    CHAIN_EVM,
    CHAIN_SOLANA,
    EVM_RE,
    SOLANA_RE,
    is_denied,
    validate,
)
from .models import Candidate

log = logging.getLogger(__name__)

# Confidence tiers
CONF_ANCHORED = 0.98  # inside an element a parser explicitly points at
CONF_LINK = 0.92      # inside a known swap/chart URL
CONF_LABELLED = 0.85  # within a few characters of "contract address" / "CA:"
CONF_CODE = 0.70      # inside <code>/<pre>/monospace markup
CONF_MANGLED = 0.55   # aggregator link whose address lost its casing
CONF_BODY = 0.40      # loose text match

#: Base58 is case-sensitive and omits 0/O/I/l, so a lowercased Solana address
#: is unrecoverable. suppoman.com ships exactly this in its Dexscreener link.
#: We cannot validate or repair it, but the presence of a *new* such link is
#: still worth seeing, so it is surfaced below the default alert threshold
#: rather than dropped silently.
_MANGLED_B58_RE = re.compile(r"^[0-9a-zA-Z]{32,44}$")

#: Query parameters on aggregator links that carry a token mint/address.
_TOKEN_QUERY_KEYS = ("buy", "outputmint", "outputcurrency", "token", "address", "mint", "ca")

#: Host -> path segment index that holds a token address.
_TOKEN_PATH_HOSTS = {
    "dexscreener.com": 1,
    "birdeye.so": 1,
    "solscan.io": 1,
    "pump.fun": 0,
    "etherscan.io": 1,
    "bscscan.com": 1,
    "basescan.org": 1,
    "dextools.io": -1,
}

_LABEL_RE = re.compile(
    r"(contract\s*address|contract|token\s*address|\bCA\b|mint\s*address)\s*[:\-–]?\s*",
    re.IGNORECASE,
)


def _classify(token: str) -> str | None:
    if EVM_RE.fullmatch(token):
        return CHAIN_EVM
    if SOLANA_RE.fullmatch(token):
        return CHAIN_SOLANA
    return None


def make_candidate(
    raw: str, chain: str, source_url: str, context: str, confidence: float, label: str = ""
) -> Candidate | None:
    """Validate `raw` and wrap it, or return None if it does not hold up."""
    raw = raw.strip()
    ok, reason = validate(raw, chain)
    if not ok:
        log.debug("rejected %s (%s) from %s: %s", raw, chain, context, reason)
        return None
    return Candidate(
        address=raw,
        chain=chain,
        source_url=source_url,
        context=context,
        confidence=confidence,
        label=label[:200],
    )


def from_selector(
    tree: HTMLParser, selector: str, source_url: str, confidence: float = CONF_ANCHORED
) -> list[Candidate]:
    """Pull addresses out of elements matching a CSS selector."""
    out: list[Candidate] = []
    for node in tree.css(selector):
        text = (node.text() or "").strip()
        for token, chain in scan_text(text):
            cand = make_candidate(
                token, chain, source_url, f"selector:{selector}", confidence, text
            )
            if cand:
                out.append(cand)
    return out


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return (token, chain) pairs found anywhere in `text`."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in EVM_RE.finditer(text):
        tok = match.group(0)
        if tok not in seen:
            seen.add(tok)
            found.append((tok, CHAIN_EVM))
    for match in SOLANA_RE.finditer(text):
        tok = match.group(0)
        if tok in seen or is_denied(tok):
            continue
        # An EVM address without the 0x prefix can look base58-ish; skip those.
        if re.fullmatch(r"[a-fA-F0-9]{40}", tok):
            continue
        seen.add(tok)
        found.append((tok, CHAIN_SOLANA))
    return found


def from_links(tree: HTMLParser, source_url: str) -> list[Candidate]:
    """Extract token addresses embedded in outbound swap / chart URLs."""
    out: list[Candidate] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        out.extend(candidates_from_url(href, source_url, node.text() or ""))
    return out


def candidates_from_url(href: str, source_url: str, label: str = "") -> list[Candidate]:
    out: list[Candidate] = []
    try:
        parsed = urlparse(href)
    except ValueError:
        return out

    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return out

    # Query-string tokens (jup.ag?buy=..., uniswap?outputCurrency=...)
    for key, values in parse_qs(parsed.query).items():
        if key.lower() not in _TOKEN_QUERY_KEYS:
            continue
        for value in values:
            chain = _classify(value)
            if chain:
                cand = make_candidate(
                    value, chain, source_url, f"link:{host}?{key}", CONF_LINK, label
                )
                if cand:
                    out.append(cand)

    # Path tokens (dexscreener.com/solana/<addr>, pump.fun/<mint>)
    if host in _TOKEN_PATH_HOSTS:
        segments = [s for s in parsed.path.split("/") if s]
        idx = _TOKEN_PATH_HOSTS[host]
        for i, segment in enumerate(segments):
            if idx >= 0 and i != idx:
                continue
            chain = _classify(segment)
            if chain:
                cand = make_candidate(
                    segment, chain, source_url, f"link:{host}/path", CONF_LINK, label
                )
                if cand:
                    out.append(cand)
            elif _MANGLED_B58_RE.match(segment) and not is_denied(segment):
                # Shaped like a Solana address but not decodable — almost always
                # a casing problem in the page's own markup.
                out.append(
                    Candidate(
                        address=segment,
                        chain=CHAIN_SOLANA,
                        source_url=source_url,
                        context=f"link:{host}/path (casing lost, unverifiable)",
                        confidence=CONF_MANGLED,
                        label=label[:200],
                    )
                )
    return out


def from_labelled_text(tree: HTMLParser, source_url: str) -> list[Candidate]:
    """Find addresses that sit right after a 'contract address' style label."""
    out: list[Candidate] = []
    text = tree.body.text(separator=" ") if tree.body else ""
    for match in _LABEL_RE.finditer(text):
        window = text[match.end() : match.end() + 120]
        for token, chain in scan_text(window):
            cand = make_candidate(
                token, chain, source_url, "labelled_text", CONF_LABELLED,
                text[max(0, match.start() - 40) : match.end() + 60],
            )
            if cand:
                out.append(cand)
    return out


def from_code_blocks(tree: HTMLParser, source_url: str) -> list[Candidate]:
    out: list[Candidate] = []
    for node in tree.css("code, pre, kbd, samp, .contract, .ca, [class*='contract'], [class*='address']"):
        text = (node.text() or "").strip()
        for token, chain in scan_text(text):
            cand = make_candidate(token, chain, source_url, "code_block", CONF_CODE, text)
            if cand:
                out.append(cand)
    return out


def from_body(tree: HTMLParser, source_url: str) -> list[Candidate]:
    out: list[Candidate] = []
    text = tree.body.text(separator=" ") if tree.body else ""
    for token, chain in scan_text(text):
        cand = make_candidate(token, chain, source_url, "body_text", CONF_BODY, "")
        if cand:
            out.append(cand)
    return out


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse repeats, keeping the highest-confidence sighting of each."""
    best: dict[tuple[str, str], Candidate] = {}
    for cand in candidates:
        key = (cand.chain, cand.address.lower())
        current = best.get(key)
        if current is None or cand.confidence > current.confidence:
            best[key] = cand
    return sorted(best.values(), key=lambda c: -c.confidence)


def extract_all(html: str, source_url: str) -> list[Candidate]:
    """Default extraction pipeline, used by the generic parser."""
    tree = HTMLParser(html)
    found: list[Candidate] = []
    found += from_links(tree, source_url)
    found += from_labelled_text(tree, source_url)
    found += from_code_blocks(tree, source_url)
    found += from_body(tree, source_url)
    return dedupe_candidates(found)


def internal_links(html: str, base_url: str, allowed_hosts: set[str]) -> set[str]:
    """Same-site links, normalised, for the crawl frontier."""
    tree = HTMLParser(html)
    out: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if host not in allowed_hosts:
            continue
        out.add(parsed._replace(fragment="", query="").geturl())
    return out
