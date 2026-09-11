import multiprocessing
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.miro_quota import SharedMiroQuota, TARGET_CREDITS_PER_MINUTE
from liquid_tracer.miro_requests import MiroRequestNotSent, MiroRequests, request_credits


def _reserve_rounds(directory, barrier, output):
    clock = [100.]
    quota = SharedMiroQuota("synthetic-shared-token", directory=directory, clock=lambda: clock[0])
    results = []
    for tick in range(12):
        clock[0] = 100 + tick * (6000 / TARGET_CREDITS_PER_MINUTE + .000001)
        barrier.wait(timeout=10)
        reservation, delay = quota.reserve(100)
        results.append((tick, bool(reservation), delay))
        if reservation is not None:
            quota.finish(reservation)
        barrier.wait(timeout=10)
    output.put(results)


def _reserve_once(directory, now, output):
    quota = SharedMiroQuota("synthetic-shared-token", directory=directory, clock=lambda: now)
    output.put(quota.reserve(100))


class MiroQuotaTests(unittest.TestCase):
    def test_independent_processes_share_one_credit_pacing_gate(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            barrier = context.Barrier(4)
            output = context.Queue()
            processes = [context.Process(target=_reserve_rounds, args=(directory, barrier, output)) for _ in range(4)]
            try:
                for process in processes:
                    process.start()
                results = [output.get(timeout=20) for _ in processes]
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
                for tick in range(12):
                    self.assertEqual(sum(bool(group[tick][1]) for group in results), 1)
                    self.assertTrue(all(group[tick][2] >= 0 for group in results))
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                output.close()

    def test_rate_limit_cooldown_survives_in_another_process(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            quota = SharedMiroQuota("synthetic-shared-token", directory=directory, clock=lambda: 100.)
            reservation, _ = quota.reserve(100)
            quota.finish(reservation, {"Retry-After": "10"}, 429)
            output = context.Queue()
            process = context.Process(target=_reserve_once, args=(directory, 104., output))
            try:
                process.start()
                self.assertEqual(output.get(timeout=10), (None, 6.))
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
                output.close()

    def test_bulk_cost_and_explicit_slower_spacing_are_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.]
            first = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            second = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            reservation, delay = first.reserve(2000)
            self.assertIsNotNone(reservation)
            self.assertEqual(delay, 0)
            self.assertAlmostEqual(second.reserve(100)[1], 120000 / 95000)
            now[0] += 2
            self.assertIsNotNone(second.reserve(100, spacing=3)[0])
            self.assertEqual(first.reserve(50)[1], 3)

    def test_different_tokens_do_not_share_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            first = SharedMiroQuota("first", directory=directory, clock=lambda: 100.)
            second = SharedMiroQuota("second", directory=directory, clock=lambda: 100.)
            self.assertNotEqual(first.path, second.path)
            self.assertIsNotNone(first.reserve(2000)[0])
            self.assertIsNotNone(second.reserve(2000)[0])

    def test_headers_account_for_other_processes_inflight_and_do_not_restore_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.]
            first = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            second = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            first_id, _ = first.reserve(100)
            now[0] += .1
            second_id, _ = second.reserve(2000)
            first.finish(first_id, {"X-RateLimit-Remaining": "1000", "X-RateLimit-Reset": "160"}, 200)
            second.finish(second_id, {"X-RateLimit-Remaining": "2000", "X-RateLimit-Reset": "160"}, 200)
            now[0] = 102.
            self.assertEqual(first.reserve(100), (None, 58.))
            now[0] = 160.
            self.assertIsNotNone(first.reserve(100)[0])

    def test_duplicate_completion_cannot_apply_stale_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.]
            quota = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            reservation, _ = quota.reserve(100)
            quota.finish(reservation, {"X-RateLimit-Remaining": "800", "X-RateLimit-Reset": "160"}, 200)
            quota.finish(reservation, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "160"}, 429)
            now[0] += .1
            self.assertIsNotNone(quota.reserve(100)[0])

    def test_smaller_advertised_allowance_slows_future_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.]
            quota = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            reservation, _ = quota.reserve(100)
            quota.finish(reservation, {"X-RateLimit-Limit": "10000"}, 200)
            now[0] += .1
            self.assertIsNotNone(quota.reserve(100)[0])
            self.assertAlmostEqual(quota.reserve(100)[1], 6000 / 9500)

    def test_cache_is_private_and_never_contains_token_or_unrelated_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "synthetic-private-token-with-sensitive-looking-text"
            quota = SharedMiroQuota(token, directory=directory, clock=lambda: 100.)
            reservation, _ = quota.reserve(100)
            quota.finish(reservation, {"Authorization": token, "X-Unrelated": token}, 200)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(quota.path.stat().st_mode), 0o600)
            self.assertNotIn(token, str(quota.path))
            self.assertNotIn(token.encode(), quota.path.read_bytes())

    def test_cache_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            quota = SharedMiroQuota("token", directory=directory)
            quota.path.unlink()
            target = Path(directory) / "unrelated.txt"
            target.write_text("untouched")
            quota.path.symlink_to(target)
            with self.assertRaises(TraceError):
                SharedMiroQuota("token", directory=directory)
            self.assertEqual(target.read_text(), "untouched")

    def test_xdg_cache_directory_is_used_without_persisting_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
            quota = SharedMiroQuota("token")
            self.assertEqual(quota.path.parent, Path(directory) / "liquid-network-tracer" / "miro-quota")

    def test_crashed_reservations_expire_without_refunding_spent_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.]
            quota = SharedMiroQuota("token", directory=directory, clock=lambda: now[0])
            abandoned, _ = quota.reserve(100)
            now[0] = 221.
            active, _ = quota.reserve(100)
            quota.finish(abandoned, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "300"}, 200)
            quota.finish(active)
            with sqlite3.connect(quota.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
            now[0] += .1
            self.assertIsNotNone(quota.reserve(100)[0])

    def test_endpoint_credit_costs_include_collections_and_each_bulk_item(self):
        for path, expected in (("items?limit=50", 100), ("connectors?limit=50", 100),
                               ("shapes/item-id", 50), ("connectors/item-id", 50)):
            self.assertEqual(request_credits("GET", "https://api.miro.com/v2/boards/id/" + path), expected)
        self.assertEqual(request_credits("POST", "https://api.miro.com/v2/boards/id/items/bulk", b'[{}, {}, {}]'), 300)
        self.assertEqual(request_credits("PATCH", "url"), 100)
        for body in (b"{}", b"[]", b"broken", b"[" + b",".join([b"{}"] * 21) + b"]"):
            with self.assertRaises(MiroRequestNotSent):
                request_credits("POST", "https://api.miro.com/v2/boards/id/items/bulk", body)

    def test_invalid_credits_fail_before_transport(self):
        for cost in (0, -1, True, 1, 1.5, float("inf"), 95_001):
            calls = []
            with MiroRequests(lambda *_: calls.append(1), interval=0) as requests:
                with self.assertRaises(MiroRequestNotSent):
                    requests.send("POST", "url", credits=cost)
            self.assertEqual(calls, [])

    def test_explicit_credit_cost_cannot_undercharge_bulk_items(self):
        calls = []
        with MiroRequests(lambda *_: calls.append(1), interval=0) as requests:
            with self.assertRaises(MiroRequestNotSent):
                requests.send("POST", "https://api.miro.com/v2/boards/id/items/bulk", body=b"[{},{}]", credits=100)
        self.assertEqual(calls, [])

    def test_post_acknowledgment_survives_cache_failure_and_blocks_next_send(self):
        class Quota:
            def reserve(self, *_):
                return "reservation", 0

            def finish(self, *_):
                raise TraceError("synthetic cache failure")

        calls = []
        def transport(*_):
            calls.append(1)
            return 201, {}, b'{"id":"created"}'

        with MiroRequests(transport, interval=0, quota=Quota()) as requests:
            self.assertEqual(requests.send("POST", "url")[0], 201)
            with self.assertRaises(MiroRequestNotSent):
                requests.send("POST", "url")
        self.assertEqual(calls, [1])

    def test_cache_reservation_failure_is_not_sent(self):
        class Quota:
            def reserve(self, *_):
                raise TraceError("synthetic cache failure")

        calls = []
        with MiroRequests(lambda *_: calls.append(1), interval=0, quota=Quota()) as requests:
            with self.assertRaises(MiroRequestNotSent):
                requests.send("POST", "url")
        self.assertEqual(calls, [])

    def test_exhausted_shared_minute_budget_waits_in_another_request_coordinator(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.]
            first = SharedMiroQuota("token", directory=directory, clock=lambda: clock[0])
            reservation, _ = first.reserve(100)
            first.finish(reservation, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "160"}, 200)
            second = SharedMiroQuota("token", directory=directory, clock=lambda: clock[0])
            calls = []

            def transport(*_):
                calls.append(clock[0])
                return 201, {}, b"{}"

            with MiroRequests(transport, interval=0, quota=second) as requests, \
                    patch("liquid_tracer.miro_requests.time.monotonic", side_effect=lambda: clock[0]), \
                    patch.object(requests, "_wait", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)) as pause:
                self.assertEqual(requests.send("POST", "url")[0], 201)
            self.assertEqual(calls, [160.])
            self.assertEqual(pause.call_args.args, (60.,))


if __name__ == "__main__":
    unittest.main()
