"""Count lookups share bounded explorer workers and retain completed observations."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_counts import _collect_counts, addresses, apply_saved_counts, fetch_counts
from liquid_tracer.common import TraceError, canonical, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


SOURCE = "https://blockstream.info/liquid/api"


def statistics(address, confirmed=19):
    return {"address": address, "chain_stats": {"tx_count": confirmed},
            "mempool_stats": {"tx_count": 2}}


class CountTransport:
    """A fake provider whose barrier proves actual request overlap."""

    def __init__(self, workers=1, *, outcomes=None, confirmed=19):
        self.barrier = threading.Barrier(workers)
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.maximum = 0
        self.outcomes = outcomes or {}
        self.confirmed = confirmed

    def __call__(self, method, url, headers, body, timeout):
        if method != "GET" or not url.startswith(SOURCE + "/address/"):
            raise AssertionError("Count lookup requested a non-statistics endpoint")
        address = url.removeprefix(SOURCE + "/address/")
        with self.lock:
            self.calls.append(address)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            self.barrier.wait(timeout=5)
            result = self.outcomes.get(address)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
            return 200, {}, canonical(statistics(address, self.confirmed))
        finally:
            with self.lock:
                self.active -= 1


class AddressCountConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Parallel counts")
        self.state = {"case_id": read_case(self.case)["case_id"],
                      "run_id": "0123456789abcdef", "source": SOURCE,
                      "transactions": {}, "fetch_options": {"workers": 3}}
        # Test transport is local and instant. Keep the real shared limiter but
        # use a tiny floor so barriers do not depend on public-provider pacing.
        pacing = patch("liquid_tracer.api.default_min_interval", return_value=0.000001)
        pacing.start()
        self.addCleanup(pacing.stop)

    def wanted(self, count):
        return [f"SYNTHETIC-count-{index:02d}" for index in range(count)]

    def collect(self, wanted, transport, **options):
        settings = {"max_requests": 100, "max_seconds": 30, "best_effort": True}
        settings.update(options)
        return _collect_counts(self.case, self.state, wanted, transport=transport, **settings)

    def cached(self):
        return read_json(self.case / "address-counts.json")["counts"]

    def test_saved_worker_limit_overlaps_requests_and_cache_writes_stay_serial(self):
        wanted = self.wanted(6)
        transport = CountTransport(workers=3)
        writing_threads, events = [], []
        caller = threading.get_ident()

        def save(path, data):
            writing_threads.append(threading.get_ident())
            save_json(path, data)

        with patch("liquid_tracer.address_counts.save_json", side_effect=save):
            report = self.collect(wanted, transport, progress=events.append)

        self.assertEqual(transport.maximum, 3)
        self.assertCountEqual(transport.calls, wanted)
        self.assertEqual(writing_threads, [caller] * len(wanted))
        self.assertEqual(report["requests_this_lookup"], len(wanted))
        self.assertEqual((report["fetched"], report["remaining"]), (6, 0))
        self.assertEqual(events[-1], {"phase": "address_counts_ready", "completed": 6, "total": 6})
        self.assertEqual(set(apply_saved_counts(self.case, self.state)), set(wanted))
        self.assertEqual(len({record["observation_ids"][0] for record in self.cached().values()}), 6)

    def test_explicit_single_worker_remains_serial(self):
        self.state["fetch_options"]["workers"] = 1
        transport = CountTransport()
        report = self.collect(self.wanted(4), transport)
        self.assertEqual(transport.maximum, 1)
        self.assertEqual((report["fetched"], report["remaining"]), (4, 0))

    def test_legacy_run_without_fetch_options_defaults_to_eight_workers(self):
        self.state.pop("fetch_options")
        transport = CountTransport(workers=8)
        report = self.collect(self.wanted(16), transport)
        self.assertEqual(transport.maximum, 8)
        self.assertEqual((report["fetched"], report["remaining"]), (16, 0))

    def test_repeated_addresses_and_saved_counts_do_not_issue_duplicate_requests(self):
        wanted = self.wanted(6)
        self.collect(wanted[:3], CountTransport(workers=3))
        original = copy.deepcopy(self.cached())
        transport = CountTransport(workers=3)
        report = self.collect(wanted + wanted + wanted[:2], transport)
        self.assertCountEqual(transport.calls, wanted[3:])
        self.assertEqual((report["total"], report["fetched"], report["known"]), (6, 3, 6))
        self.assertEqual({address: self.cached()[address] for address in wanted[:3]}, original)
        with patch("liquid_tracer.api.Esplora", side_effect=AssertionError("Cache-only lookup")):
            again = self.collect(wanted, transport)
        self.assertEqual(again["requests_this_lookup"], 0)
        self.assertEqual(again["remaining"], 0)

    def test_request_budget_retains_every_successful_inflight_count(self):
        self.state["fetch_options"]["workers"] = 4
        wanted = self.wanted(8)
        transport = CountTransport(workers=2)
        report = self.collect(wanted, transport, max_requests=2)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(report["requests_this_lookup"], 2)
        self.assertEqual(report["stop_reason"], "request_limit")
        self.assertEqual((report["fetched"], report["known"], report["remaining"]), (2, 2, 6))
        self.assertEqual(set(self.cached()), set(transport.calls))

    def test_global_failure_stops_next_batch_but_keeps_other_batch_successes(self):
        wanted = self.wanted(6)
        failures = ((TraceError("Network request failed: TimeoutError"), "network_failed"),
                    ((401, {}, b"unauthorized"), "authentication_failed"))
        for failure, reason in failures:
            with self.subTest(reason=reason):
                transport = CountTransport(workers=3, outcomes={wanted[0]: failure})
                report = self.collect(wanted, transport, refresh=True)
                self.assertCountEqual(transport.calls, wanted[:3])
                self.assertEqual(report["stop_reason"], reason)
                self.assertEqual((report["fetched"], report["remaining"], report["failed"]), (2, 4, 1))
                self.assertEqual(set(self.cached()), set(wanted[1:3]))
                self.assertEqual(report["errors"], [{"address": wanted[0], "reason": reason}])

    def test_explicit_refresh_saves_other_batch_successes_before_raising(self):
        state = graph_state(seeds=tuple(name + ":0" for name in "abcdef"))
        state.update(source=SOURCE, fetch_options={"workers": 3})
        self.state, _ = saved_case(self.case, state)
        wanted = addresses(self.state)
        self.assertEqual(len(wanted), 6)
        failures = (TraceError("Network request failed: TimeoutError"),
                    (401, {}, b"unauthorized"))
        for failure in failures:
            with self.subTest(failure=failure):
                self.collect(wanted, CountTransport(workers=3), refresh=True)
                original = copy.deepcopy(self.cached())
                transport = CountTransport(workers=3, outcomes={wanted[0]: failure}, confirmed=42)
                with self.assertRaises(TraceError):
                    fetch_counts(self.case, refresh=True, transport=transport)
                self.assertCountEqual(transport.calls, wanted[:3])
                cached = self.cached()
                for address in wanted[1:3]:
                    self.assertEqual(cached[address]["confirmed_tx_count"], 42)
                    self.assertNotEqual(cached[address]["observation_ids"], original[address]["observation_ids"])
                for address in [wanted[0], *wanted[3:]]:
                    self.assertEqual(cached[address], original[address])

    def test_invalid_response_keeps_batch_successes_and_continues_next_batch(self):
        wanted = self.wanted(6)
        malformed = (200, {}, canonical(statistics("SYNTHETIC-other")))
        transport = CountTransport(workers=3, outcomes={wanted[0]: malformed})
        report = self.collect(wanted, transport)
        self.assertCountEqual(transport.calls, wanted)
        self.assertEqual(report["stop_reason"], "lookup_failed")
        self.assertEqual((report["fetched"], report["remaining"], report["failed"]), (5, 1, 1))
        self.assertEqual(set(self.cached()), set(wanted[1:]))


if __name__ == "__main__":
    unittest.main()
