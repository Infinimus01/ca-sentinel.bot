"""Parser tests against real captures of suppoman.com (2026-09-16)."""

from pathlib import Path

import pytest

from sentinel.models import FetchResult
from sentinel.sites import load_all
from sentinel.sites.suppoman import SuppomanParser

FIXTURES = Path(__file__).parent / "fixtures"
HOME_EVM = "0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777"
HOME_SOL = "8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8"
SECRET_EVM = "0x008Df4b3E857D06c4603Aeb11F267ccD32ce2005"


def _result(fixture: str, url: str) -> FetchResult:
    return FetchResult(
        url=url,
        status=200,
        changed=True,
        body=(FIXTURES / fixture).read_text(encoding="utf-8"),
        body_sha256="x" * 64,
    )


@pytest.fixture
def parser():
    return SuppomanParser("https://suppoman.com")


class TestSecretArea:
    def test_extracts_the_gem_contract(self, parser):
        cands = parser.parse(_result("suppoman_secretarea.html",
                                     "https://suppoman.com/secretarea/"))
        assert [c.address for c in cands] == [SECRET_EVM]

    def test_anchored_selector_gives_top_confidence(self, parser):
        cand = parser.parse(_result("suppoman_secretarea.html",
                                    "https://suppoman.com/secretarea/"))[0]
        assert cand.context == "selector:#ca"
        assert cand.confidence >= 0.95
        assert cand.chain == "evm"

    def test_labels_with_the_gem_title(self, parser):
        cand = parser.parse(_result("suppoman_secretarea.html",
                                    "https://suppoman.com/secretarea/"))[0]
        assert cand.label == "New Secret Pick"


class TestHomepage:
    def test_extracts_both_published_surfaces(self, parser):
        cands = parser.parse(_result("suppoman_home.html", "https://suppoman.com/"))
        alertable = {c.address for c in cands if c.confidence >= 0.6}
        assert alertable == {HOME_EVM, HOME_SOL}

    def test_ca_box_beats_swap_link(self, parser):
        cands = parser.parse(_result("suppoman_home.html", "https://suppoman.com/"))
        by_address = {c.address: c for c in cands}
        assert by_address[HOME_EVM].context == "selector:#contractText"
        assert by_address[HOME_EVM].confidence > by_address[HOME_SOL].confidence

    def test_wrapped_sol_is_not_reported(self, parser):
        """jup.ag's sell= leg is wSOL and must never become an alert."""
        cands = parser.parse(_result("suppoman_home.html", "https://suppoman.com/"))
        assert all("So1111" not in c.address for c in cands)

    def test_mangled_dexscreener_link_stays_below_threshold(self, parser):
        cands = parser.parse(_result("suppoman_home.html", "https://suppoman.com/"))
        mangled = [c for c in cands if "casing lost" in c.context]
        assert len(mangled) == 1
        assert mangled[0].confidence < 0.6


class TestDiscovery:
    def test_finds_no_offsite_links_as_targets(self, parser):
        links = parser.discover_links(
            _result("suppoman_home.html", "https://suppoman.com/")
        )
        assert all("suppoman.com" in link for link in links)

    def test_skips_asset_urls(self, parser):
        assert not parser.should_watch("https://suppoman.com/icon.png")
        assert not parser.should_watch("https://suppoman.com/script.js")
        assert parser.should_watch("https://suppoman.com/secretarea/")


class TestRegistry:
    def test_registered_under_its_site_id(self):
        from sentinel.sites import get_parser

        load_all()
        assert isinstance(get_parser("suppoman", "https://suppoman.com"), SuppomanParser)

    def test_unknown_id_falls_back_to_generic(self):
        from sentinel.sites import get_parser
        from sentinel.sites.generic import GenericParser

        load_all()
        assert isinstance(get_parser("nope", "https://example.com"), GenericParser)
