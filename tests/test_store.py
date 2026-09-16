"""Dedup and outbox durability — the two properties that must not regress."""

import json

import pytest

from sentinel.models import Candidate
from sentinel.store import Store

EVM = "0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _cand(address=EVM, chain="evm", conf=0.98, url="https://x/"):
    return Candidate(address, chain, url, "selector:#ca", conf, "label")


class TestDeduplication:
    def test_first_sighting_returns_an_alert(self, store):
        assert store.record_candidate("s", _cand(), suppress_alert=False) is not None

    def test_second_sighting_is_silent(self, store):
        store.record_candidate("s", _cand(), suppress_alert=False)
        assert store.record_candidate("s", _cand(), suppress_alert=False) is None

    def test_evm_dedup_is_case_insensitive(self, store):
        store.record_candidate("s", _cand(EVM), suppress_alert=False)
        assert store.record_candidate("s", _cand(EVM.lower()), suppress_alert=False) is None

    def test_same_address_on_another_site_alerts_again(self, store):
        store.record_candidate("site-a", _cand(), suppress_alert=False)
        assert store.record_candidate("site-b", _cand(), suppress_alert=False) is not None

    def test_repeat_sightings_are_counted(self, store):
        for _ in range(3):
            store.record_candidate("s", _cand(), suppress_alert=False)
        row = store._conn.execute(
            "SELECT sightings FROM seen_addresses WHERE site_id='s'"
        ).fetchone()
        assert row["sightings"] == 3

    def test_confidence_only_ratchets_up(self, store):
        store.record_candidate("s", _cand(conf=0.5), suppress_alert=False)
        store.record_candidate("s", _cand(conf=0.9), suppress_alert=False)
        store.record_candidate("s", _cand(conf=0.7), suppress_alert=False)
        row = store._conn.execute(
            "SELECT confidence FROM seen_addresses WHERE site_id='s'"
        ).fetchone()
        assert row["confidence"] == pytest.approx(0.9)


class TestBootstrapSeeding:
    def test_seeding_records_without_alerting(self, store):
        assert store.record_candidate("s", _cand(), suppress_alert=True) is None
        assert store.stats()["addresses"] == 1
        assert store.stats()["outbox_pending"] == 0

    def test_seeded_address_never_alerts_later(self, store):
        store.record_candidate("s", _cand(), suppress_alert=True)
        assert store.record_candidate("s", _cand(), suppress_alert=False) is None

    def test_a_different_address_after_seeding_does_alert(self, store):
        store.record_candidate("s", _cand(), suppress_alert=True)
        other = "0x008Df4b3E857D06c4603Aeb11F267ccD32ce2005"
        assert store.record_candidate("s", _cand(other), suppress_alert=False) is not None


class TestOutbox:
    def test_alert_is_enqueued_atomically_with_the_seen_marker(self, store):
        store.record_candidate("s", _cand(), suppress_alert=False)
        pending = store.pending_alerts()
        assert len(pending) == 1
        assert json.loads(pending[0]["payload"])["address"] == EVM

    def test_marking_sent_clears_it(self, store):
        store.record_candidate("s", _cand(), suppress_alert=False)
        store.mark_sent(store.pending_alerts()[0]["id"])
        assert store.pending_alerts() == []

    def test_failure_defers_the_retry(self, store):
        store.record_candidate("s", _cand(), suppress_alert=False)
        store.mark_failed(store.pending_alerts()[0]["id"], retry_in=60)
        assert store.pending_alerts() == []          # not due yet
        assert store.stats()["outbox_pending"] == 1  # still owed

    def test_pending_alerts_survive_a_restart(self, tmp_path):
        path = tmp_path / "persist.db"
        first = Store(path)
        first.record_candidate("s", _cand(), suppress_alert=False)
        first.close()

        second = Store(path)
        assert len(second.pending_alerts()) == 1
        assert second.record_candidate("s", _cand(), suppress_alert=False) is None
        second.close()


class TestPageState:
    def test_validators_round_trip(self, store):
        store.save_page_state("k", "s", "https://x/", 'W/"abc"', "Wed, 01 Jan 2025",
                              "hash1", 200, changed=True)
        row = store.get_page_state("k")
        assert row["etag"] == 'W/"abc"' and row["body_sha256"] == "hash1"

    def test_counters_accumulate(self, store):
        store.save_page_state("k", "s", "u", None, None, "h1", 200, changed=True)
        store.save_page_state("k", "s", "u", None, None, "h1", 304, changed=False)
        store.save_page_state("k", "s", "u", None, None, "h2", 200, changed=True)
        row = store.get_page_state("k")
        assert row["checks"] == 3 and row["changes"] == 2


class TestRoutes:
    def test_new_route_reported_once(self, store):
        assert store.add_route("s", "https://x/a", 200, "probe") is True
        assert store.add_route("s", "https://x/a", 200, "probe") is False
