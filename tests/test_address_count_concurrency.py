"""Count lookups share bounded explorer workers and retain completed observations."""
import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from liquid_tracer.address_counts import (_collect_counts, _failure_reason, addresses,
                                           apply_saved_counts, fetch_counts, _saved_min_interval,
                                           public_count_report)
from liquid_tracer.address_count_cache import CountCacheJournal
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

    def __init__(self, workers=1, *, outcomes=None, confirmed=19, failure_seen=None):
        self.barrier = threading.Barrier(workers)
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.maximum = 0
        self.outcomes = outcomes or {}
        self.confirmed = confirmed
        self.failure_seen = failure_seen

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
            if self.failure_seen is not None and not self.failure_seen.wait(timeout=5):
                raise AssertionError("Failure was not observed before draining other results")
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
        for setting in (patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "auto"}),
                        patch("liquid_tracer.count_concurrency._available_bytes", return_value=8 * 1024 ** 3),
                        patch("liquid_tracer.count_concurrency._available_cpu_count", return_value=8)):
            setting.start()
            self.addCleanup(setting.stop)

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
        self.assertTrue(writing_threads)
        self.assertLess(len(writing_threads), len(wanted))
        self.assertEqual(set(writing_threads), {caller})
        self.assertEqual(report["requests_this_lookup"], len(wanted))
        self.assertEqual((report["fetched"], report["remaining"]), (6, 0))
        self.assertEqual({key: events[-1][key] for key in ("phase", "completed", "total")},
                         {"phase": "address_counts_ready", "completed": 6, "total": 6})
        self.assertTrue(any(event.get("worker_count") == 3 and event.get("worker_limit") == 3
                            for event in events))
        self.assertEqual(set(apply_saved_counts(self.case, self.state)), set(wanted))
        self.assertEqual(len({record["observation_ids"][0] for record in self.cached().values()}), 6)
        self.assertEqual(events[-1]["fetched"], 6)
        self.assertEqual(report["rate_limit_responses"], 0)
        self.assertEqual(report["retry_responses"], 0)

    def test_explicit_single_worker_remains_serial(self):
        self.state["fetch_options"]["workers"] = 1
        transport = CountTransport()
        report = self.collect(self.wanted(4), transport)
        self.assertEqual(transport.maximum, 1)
        self.assertEqual((report["fetched"], report["remaining"]), (4, 0))

    def test_legacy_run_without_fetch_options_starts_eight_workers(self):
        self.state.pop("fetch_options")
        transport = CountTransport(workers=8)
        report = self.collect(self.wanted(8), transport)
        self.assertEqual(transport.maximum, 8)
        self.assertEqual((report["fetched"], report["remaining"]), (8, 0))

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

    def test_zero_budgets_fetch_the_complete_address_list(self):
        wanted = self.wanted(102)
        transport = CountTransport()
        report = self.collect(wanted, transport, max_requests=0, max_seconds=0)
        self.assertEqual((report["fetched"], report["remaining"]), (102, 0))
        self.assertEqual(report["requests_this_lookup"], 102)
        self.assertCountEqual(transport.calls, wanted)

    def test_global_failure_stops_new_requests_and_keeps_inflight_successes(self):
        wanted = self.wanted(6)
        failures = ((TraceError("Network request failed: TimeoutError"), "network_failed"),
                    ((401, {}, b"unauthorized"), "authentication_failed"))
        for failure, reason in failures:
            with self.subTest(reason=reason):
                failure_seen = threading.Event()
                transport = CountTransport(workers=3, outcomes={wanted[0]: failure},
                                           failure_seen=failure_seen)
                def observed(error):
                    failure_seen.set()
                    return _failure_reason(error)
                with patch("liquid_tracer.address_counts._failure_reason", side_effect=observed):
                    report = self.collect(wanted, transport, refresh=True)
                self.assertCountEqual(transport.calls, wanted[:3])
                self.assertEqual(report["stop_reason"], reason)
                self.assertEqual((report["fetched"], report["remaining"], report["failed"]), (2, 4, 1))
                self.assertEqual(set(self.cached()), set(wanted[1:3]))
                self.assertEqual(report["errors"], [{"address": wanted[0], "reason": reason}])

    def test_explicit_refresh_saves_inflight_successes_before_raising(self):
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
                failure_seen = threading.Event()
                transport = CountTransport(workers=3, outcomes={wanted[0]: failure}, confirmed=42,
                                           failure_seen=failure_seen)
                def observed(error):
                    failure_seen.set()
                    return _failure_reason(error)
                with patch("liquid_tracer.address_counts._failure_reason", side_effect=observed):
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

    def test_completed_count_progress_streams_before_a_slower_request_finishes(self):
        self.state["fetch_options"]["workers"] = 2
        wanted = self.wanted(3)
        both_started = threading.Barrier(2)
        progress_received = threading.Event()
        slow_finished = threading.Event()
        callback_threads = []

        def transport(method, url, headers, body, timeout):
            address = url.rsplit("/", 1)[1]
            if address in wanted[:2]:
                both_started.wait(timeout=5)
            if address == wanted[0]:
                if not progress_received.wait(timeout=5):
                    raise AssertionError("Fast completion was held behind the slow response")
                slow_finished.set()
            return 200, {}, canonical(statistics(address))

        def progress(event):
            callback_threads.append(threading.get_ident())
            if event["phase"] == "address_counts" and event["completed"] == 1:
                self.assertFalse(slow_finished.is_set())
                progress_received.set()

        report = self.collect(wanted, transport, progress=progress)
        self.assertTrue(progress_received.is_set())
        self.assertEqual(set(callback_threads), {threading.get_ident()})
        self.assertEqual((report["fetched"], report["remaining"]), (3, 0))

    def test_large_lookup_checkpoints_the_cache_instead_of_rewriting_every_response(self):
        self.state["fetch_options"]["workers"] = 1
        wanted = self.wanted(130)
        saved_sizes = []
        batches = []
        write = CountCacheJournal.write

        def checkpoint(journal, records):
            batches.append(len(records))
            write(journal, records)

        def save(path, data):
            saved_sizes.append(len(data["counts"]))
            save_json(path, data)

        # Freeze only the count checkpoint clock. The API keeps its real budget,
        # request metrics, and pacing clock, so this isolates the size threshold.
        clock = SimpleNamespace(monotonic=lambda: 100.)
        with patch("liquid_tracer.address_counts.time", clock), \
                patch("liquid_tracer.address_counts.save_json", side_effect=save), \
                patch.object(CountCacheJournal, "write", checkpoint):
            report = self.collect(wanted, CountTransport(), max_requests=200)

        self.assertEqual(batches, [32, 32, 32, 32, 2])
        self.assertEqual(sum(batches), len(wanted))
        self.assertEqual(saved_sizes, [130])
        self.assertEqual((report["fetched"], report["remaining"]), (130, 0))
        self.assertEqual(set(apply_saved_counts(self.case, self.state)), set(wanted))
        self.assertEqual(len({record["observation_ids"][0] for record in self.cached().values()}), 130)

    def test_checkpoint_time_threshold_saves_progress_before_lookup_finishes(self):
        self.state["fetch_options"]["workers"] = 1
        wanted = self.wanted(3)
        now = [100.]
        saved_sizes = []
        committed = []
        write = CountCacheJournal.write

        def checkpoint(journal, records):
            write(journal, records)
            state = {key: value for key, value in self.state.items() if key != "address_tx_counts"}
            committed.append(len(apply_saved_counts(self.case, state)))

        def save(path, data):
            saved_sizes.append(len(data["counts"]))
            save_json(path, data)

        def progress(event):
            if event["phase"] == "address_counts" and event["completed"] == 1:
                now[0] += 1.1

        with patch("liquid_tracer.address_counts.time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch("liquid_tracer.address_counts.save_json", side_effect=save), \
                patch.object(CountCacheJournal, "write", checkpoint):
            self.collect(wanted, CountTransport(), progress=progress)
        self.assertEqual(committed, [2, 3])
        self.assertEqual(saved_sizes, [3])

    def test_auto_mode_grows_above_eight_from_measured_transport_latency(self):
        self.state["fetch_options"]["workers"] = 8
        wanted = self.wanted(48)
        lock = threading.Lock()
        active = maximum = 0
        events = []

        def transport(method, url, headers, body, timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                # Fake network latency feeds the actual Esplora EWMA. Assertions
                # use observed overlap, not a wall-clock speed threshold.
                time.sleep(.015)
                return 200, {}, canonical(statistics(url.rsplit("/", 1)[1]))
            finally:
                with lock:
                    active -= 1

        report = self.collect(wanted, transport, progress=events.append)
        self.assertGreater(maximum, 8)
        self.assertLessEqual(maximum, 48)
        self.assertEqual(report["concurrency_mode"], "auto")
        self.assertGreater(report["peak_workers"], 8)
        self.assertEqual(report["worker_limit"], 48)
        self.assertGreater(report["observed_rps"], 0)
        self.assertTrue(any(event.get("worker_count", 0) > 8 for event in events))
        self.assertEqual((report["fetched"], report["remaining"]), (48, 0))

    def test_environment_can_select_more_than_eight_count_workers(self):
        transport = CountTransport(workers=12)
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "12"}):
            report = self.collect(self.wanted(12), transport)
        self.assertEqual(transport.maximum, 12)
        self.assertEqual(report["concurrency_mode"], "fixed")
        self.assertEqual(report["worker_limit"], 12)
        self.assertEqual((report["fetched"], report["remaining"]), (12, 0))

    def test_saved_implicit_rate_does_not_freeze_adaptive_count_client(self):
        legacy = {"rate_limit_source": "enterprise_target", "advertised_rps": None,
                  "min_interval": 1 / 49, "effective_rps": 49}
        self.assertIsNone(_saved_min_interval(legacy))
        self.assertEqual(_saved_min_interval({**legacy, "min_interval": .5}), .5)
        self.assertEqual(_saved_min_interval({**legacy, "advertised_rps": 50}), 1 / 49)
        self.assertEqual(_saved_min_interval({**legacy, "rate_limit_source": "conservative_default"}), 1 / 49)
        self.assertEqual(_saved_min_interval({**legacy, "min_interval_explicit": True}), 1 / 49)
        self.assertEqual(_saved_min_interval({**legacy, "min_interval_explicit": False}), 1 / 49)
        self.assertIsNone(_saved_min_interval({"rate_limit_source": "enterprise_adaptive",
            "min_interval": 1 / 128, "min_interval_explicit": False}))

    def test_public_count_report_keeps_pacing_and_retry_counters_but_not_arbitrary_text(self):
        base = {"fetched": 3, "known": 5, "total": 8, "remaining": 3, "failed": 0,
                "requests_this_lookup": 4, "stop_reason": None}
        report = {**base, "api_rate_mode": "adaptive", "api_target_rps": 256,
                  "shared_api_effective_rps": 128, "rate_limit_responses": 1,
                  "retry_responses": 2, "observed_rps": 110}
        self.assertEqual(public_count_report({**report, "message": "PRIVATE"}), report)
        for invalid in (True, -1, "PRIVATE", [], {}, float("nan"), 10 ** 400):
            with self.subTest(invalid=invalid):
                result = public_count_report({**base, "api_rate_mode": invalid,
                    "api_target_rps": invalid, "rate_limit_responses": invalid,
                    "retry_responses": invalid})
                self.assertEqual(result, base)


if __name__ == "__main__":
    unittest.main()
