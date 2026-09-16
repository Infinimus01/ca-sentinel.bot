from sentinel.extract import (
    CONF_ANCHORED,
    CONF_LINK,
    candidates_from_url,
    dedupe_candidates,
    extract_all,
    internal_links,
    scan_text,
)
from sentinel.models import Candidate

EVM = "0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777"
SOL = "8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8"


class TestScanText:
    def test_finds_both_chains(self):
        found = dict((token, chain) for token, chain in scan_text(f"buy {EVM} or {SOL}"))
        assert found[EVM] == "evm"
        assert found[SOL] == "solana"

    def test_skips_bare_hex_that_looks_base58(self):
        bare = EVM[2:]  # 40 hex chars, no 0x
        assert scan_text(bare) == []

    def test_denylisted_solana_skipped(self):
        assert scan_text("So11111111111111111111111111111111111111112") == []


class TestUrlExtraction:
    def test_jupiter_buy_param(self):
        cands = candidates_from_url(
            f"https://jup.ag/?sell=So11111111111111111111111111111111111111112&buy={SOL}",
            "https://example.com/",
        )
        assert [c.address for c in cands] == [SOL]
        assert cands[0].confidence == CONF_LINK

    def test_dexscreener_path(self):
        cands = candidates_from_url(
            f"https://dexscreener.com/solana/{SOL}", "https://example.com/"
        )
        assert [c.address for c in cands] == [SOL]

    def test_lowercased_dexscreener_flagged_not_dropped(self):
        mangled = "2zrmpeat65m8p3hzy4epmxvwduvupq1qlr9oq6epins3"
        cands = candidates_from_url(
            f"https://dexscreener.com/solana/{mangled}", "https://example.com/"
        )
        assert len(cands) == 1
        assert cands[0].address == mangled
        assert "casing lost" in cands[0].context
        assert cands[0].confidence < 0.6  # stored, never alerted by default

    def test_unrelated_host_ignored(self):
        assert candidates_from_url(f"https://t.me/{SOL}", "https://example.com/") == []


class TestExtractAll:
    HTML = f"""
    <html><body>
      <h3>CONTRACT ADDRESS</h3>
      <div class="ca-box"><code id="contractText">{EVM}</code></div>
      <a href="https://jup.ag/?buy={SOL}">BUY</a>
      <a href="/secretarea/">secret</a>
      <a href="https://t.me/somechannel">telegram</a>
    </body></html>
    """

    def test_finds_both_addresses(self):
        found = {c.address for c in extract_all(self.HTML, "https://example.com/")}
        assert found == {EVM, SOL}

    def test_highest_confidence_wins_after_dedupe(self):
        cands = extract_all(self.HTML, "https://example.com/")
        evm = next(c for c in cands if c.address == EVM)
        # Present in a code block, a labelled heading and body text at once.
        assert evm.confidence >= 0.85

    def test_internal_links_only(self):
        links = internal_links(self.HTML, "https://example.com/", {"example.com"})
        assert links == {"https://example.com/secretarea/"}


class TestDedupe:
    def test_keeps_highest_confidence(self):
        low = Candidate(EVM, "evm", "u", "body_text", 0.4)
        high = Candidate(EVM, "evm", "u", "selector:#ca", CONF_ANCHORED)
        assert dedupe_candidates([low, high])[0].confidence == CONF_ANCHORED

    def test_evm_case_insensitive(self):
        a = Candidate(EVM, "evm", "u", "x", 0.9)
        b = Candidate(EVM.lower(), "evm", "u", "y", 0.5)
        assert len(dedupe_candidates([a, b])) == 1

    def test_sorted_by_confidence(self):
        cands = dedupe_candidates([
            Candidate(EVM, "evm", "u", "x", 0.4),
            Candidate(SOL, "solana", "u", "y", 0.9),
        ])
        assert [c.confidence for c in cands] == [0.9, 0.4]
