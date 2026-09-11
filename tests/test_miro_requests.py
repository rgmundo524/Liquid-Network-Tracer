import threading
import time
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical
from liquid_tracer import miro_requests
from liquid_tracer.miro_requests import MiroRequestNotSent, MiroRequests


class Progress:
    def __init__(self):
        self.current = {"phase": "updating", "completed": 0, "total": 8}
        self.events = []

    def _send(self, event):
        self.events.append((threading.get_ident(), dict(event)))


class MiroRequestsTests(unittest.TestCase):
    def test_bounded_jobs_and_coordinator_acknowledgments(self):
        coordinator = threading.get_ident()
        barrier = threading.Barrier(4)
        active, maximum, consumed = 0, 0, []
        lock = threading.Lock()
        accepted = []

        def jobs():
            for value in range(8):
                consumed.append(value)
                yield value

        def worker(value):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            if value < 4:
                barrier.wait(timeout=2)
                self.assertEqual(len(consumed), 4)
            time.sleep(.02)
            with lock:
                active -= 1
            return value * 2

        with MiroRequests(None, interval=0, workers=4) as requests:
            requests.map(jobs(), worker, lambda job, result: accepted.append((threading.get_ident(), job, result)))
        self.assertEqual(maximum, 4)
        self.assertEqual(sorted((job, result) for _, job, result in accepted), [(x, x * 2) for x in range(8)])
        self.assertTrue(all(identity == coordinator for identity, _, _ in accepted))

    def test_failure_stops_new_work_and_drains_successful_sent_writes(self):
        barrier = threading.Barrier(4)
        called, accepted = [], []
        failure = TraceError("synthetic failed edit")

        def worker(value):
            called.append(value)
            barrier.wait(timeout=2)
            if value == 0:
                raise failure
            time.sleep(.04)
            return value

        with MiroRequests(None, interval=0, workers=4) as requests:
            with self.assertRaises(TraceError) as caught:
                requests.map(range(20), worker, lambda job, result: accepted.append(result))
            self.assertIs(caught.exception, failure)
            self.assertEqual(sorted(called), [0, 1, 2, 3])
            self.assertEqual(sorted(accepted), [1, 2, 3])
            # Cancellation belongs to the batch, not the entire session.
            requests.map([21], lambda value: value, lambda job, result: accepted.append(result))
        self.assertIn(21, accepted)

    def test_accept_failure_also_drains_other_acknowledgments(self):
        barrier = threading.Barrier(3)
        accepted = []
        error = OSError("checkpoint failed")

        def worker(value):
            barrier.wait(timeout=2)
            if value:
                time.sleep(.03)
            return value

        def accept(job, result):
            if job == 0:
                raise error
            accepted.append(result)

        with MiroRequests(None, interval=0, workers=3) as requests:
            with self.assertRaises(OSError) as caught:
                requests.map(range(20), worker, accept)
        self.assertIs(caught.exception, error)
        self.assertEqual(sorted(accepted), [1, 2])

    def test_interrupt_drains_already_sent_writes(self):
        barrier = threading.Barrier(3)
        accepted = []

        def worker(value):
            barrier.wait(timeout=2)
            if value == 0:
                raise KeyboardInterrupt()
            time.sleep(.03)
            return value

        with MiroRequests(None, interval=0, workers=3) as requests:
            with self.assertRaises(KeyboardInterrupt):
                requests.map(range(20), worker, lambda job, result: accepted.append(result))
        self.assertEqual(sorted(accepted), [1, 2])

    def test_coordinator_interrupts_still_drain_sent_write_results(self):
        accepted = []
        begun = threading.Barrier(4)
        original_wait = miro_requests.wait
        interrupt = KeyboardInterrupt()
        waits = []

        def worker(value):
            begun.wait(timeout=2)
            time.sleep(.03)
            return value

        def interrupted_wait(*args, **kwargs):
            waits.append(1)
            if len(waits) <= 2:
                raise interrupt
            return original_wait(*args, **kwargs)

        with MiroRequests(None, interval=0, workers=4) as requests, \
                patch("liquid_tracer.miro_requests.wait", side_effect=interrupted_wait):
            with self.assertRaises(KeyboardInterrupt) as caught:
                requests.map(range(20), worker, lambda job, result: accepted.append(result))
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(sorted(accepted), [0, 1, 2, 3])

    def test_waiting_requests_cancel_before_transport_after_failure(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            raise TraceError("failed first request")

        with MiroRequests(transport, interval=.2, workers=4) as requests:
            with self.assertRaises(TraceError):
                requests.map(range(20), lambda value: requests.request("PATCH", str(value), {}, {}), lambda *_: None)
        self.assertEqual(len(calls), 1)

    def test_request_starts_share_one_pacing_gate(self):
        starts = []

        def transport(*_):
            starts.append(time.monotonic())
            time.sleep(.025)
            return 200, {}, b"{}"

        with MiroRequests(transport, interval=.02, workers=4) as requests:
            requests.map(range(6), lambda _: requests.request("PATCH", "url", {}, {}), lambda *_: None)
        starts.sort()
        self.assertTrue(all(right - left >= .017 for left, right in zip(starts, starts[1:])), starts)

    def test_429_installs_shared_cooldown_and_serializes_progress(self):
        progress = Progress()
        coordinator = threading.get_ident()
        starts = []

        def transport(method, url, headers, body, timeout):
            starts.append(time.monotonic())
            if len(starts) == 1:
                return 429, {"Retry-After": "1"}, b"{}"
            return 200, {}, b"{}"

        with MiroRequests(transport, interval=0, workers=3, progress=progress) as requests:
            requests.map(range(3), lambda _: requests.request("PATCH", "url", {}, {}), lambda *_: None)
        self.assertEqual(len(starts), 4)
        self.assertGreaterEqual(min(starts[1:]) - starts[0], .95)
        self.assertTrue(progress.events)
        self.assertTrue(all(identity == coordinator for identity, _ in progress.events))
        self.assertTrue(any(event["phase"] == "waiting" for _, event in progress.events))

    def test_waiting_progress_stays_visible_until_deadline(self):
        progress = Progress()
        clock = [100.]
        with MiroRequests(None, interval=0, progress=progress) as requests, \
                patch("liquid_tracer.miro_requests.time.monotonic", side_effect=lambda: clock[0]):
            requests._notify(3, "Waiting for the Miro rate limit to reset")
            requests._flush_progress()
            self.assertEqual([event["phase"] for _, event in progress.events], ["waiting"])
            clock[0] += 2.9
            requests._flush_progress()
            self.assertEqual([event["phase"] for _, event in progress.events], ["waiting"])
            clock[0] += .1
            requests._flush_progress()
            self.assertEqual([event["phase"] for _, event in progress.events], ["waiting", "updating"])
            self.assertEqual(progress.events[-1][1]["completed"], 0)

    def test_exhausted_response_budget_defers_next_request(self):
        starts = []

        def transport(*_):
            starts.append(time.monotonic())
            if len(starts) == 1:
                return 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(time.time() + .12)}, b"{}"
            return 200, {}, b"{}"

        with MiroRequests(transport, interval=0) as requests:
            requests.request("GET", "url", {})
            requests.request("PATCH", "url", {}, {})
        self.assertGreaterEqual(starts[1] - starts[0], .1)

    def test_late_quota_responses_cannot_restore_spent_budget(self):
        with MiroRequests(lambda *_: (200, {}, b"{}"), interval=0) as requests:
            reset = str(time.time() + 10)
            requests._inflight_credits = 200
            requests._observe({"X-RateLimit-Remaining": "200", "X-RateLimit-Reset": reset}, 100, 200)
            self.assertEqual(requests._remaining, 100)
            requests._observe({"X-RateLimit-Remaining": "400", "X-RateLimit-Reset": reset}, 100, 200)
            self.assertEqual(requests._remaining, 100)

    def test_overlong_rate_limit_is_reported_without_retrying(self):
        calls = []

        def transport(*_):
            calls.append(1)
            return 429, {"Retry-After": "31"}, b"{}"

        with MiroRequests(transport, interval=0) as requests:
            with self.assertRaisesRegex(TraceError, "more than 30 seconds"):
                requests.request("PATCH", "url", {}, {})
        self.assertEqual(len(calls), 1)

    def test_raw_send_preserves_known_rejection_and_caller_owns_progress(self):
        calls = []
        progress = Progress()

        def transport(*_):
            calls.append(1)
            return 429, {"Retry-After": "31"}, b"{}"

        with MiroRequests(transport, interval=0, progress=progress) as requests:
            self.assertEqual(requests.send("POST", "url", {}, b"{}")[0], 429)
            self.assertEqual(progress.events, [])
            with self.assertRaisesRegex(TraceError, "more than 30 seconds"):
                requests.send("POST", "url", {}, b"{}")
        self.assertEqual(len(calls), 1)

    def test_exhausted_long_window_is_distinguished_as_not_sent(self):
        calls = []

        def transport(*_):
            calls.append(1)
            return 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(time.time() + 60)}, b"{}"

        with MiroRequests(transport, interval=0) as requests:
            requests.send("GET", "url", {})
            with self.assertRaises(MiroRequestNotSent):
                requests.send("POST", "url", {}, b"{}")
        self.assertEqual(calls, [1])

    def test_workers_persist_across_read_and_edit_phases(self):
        identities = []
        with MiroRequests(None, interval=0, workers=1) as requests:
            for _ in range(2):
                requests.map([0], lambda _: threading.get_ident(), lambda job, identity: identities.append(identity))
        self.assertEqual(identities[0], identities[1])
        self.assertNotEqual(identities[0], threading.get_ident())

    def test_no_write_retry_for_server_error_or_lost_response(self):
        for outcome in ((503, {}, b"{}"), TraceError("lost response")):
            calls = []

            def transport(*_):
                calls.append(1)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with MiroRequests(transport, interval=0) as requests:
                if isinstance(outcome, Exception):
                    with self.assertRaises(TraceError):
                        requests.request("PATCH", "url", {}, {"position": {"x": 1}})
                else:
                    self.assertEqual(requests.request("PATCH", "url", {}, {})[0], 503)
            self.assertEqual(len(calls), 1)

    def test_read_retry_is_bounded_and_request_body_is_canonical(self):
        bodies = []

        def transport(method, url, headers, body, timeout):
            bodies.append(body)
            return 503, {}, b"{}"

        with MiroRequests(transport, interval=0) as requests, patch.object(requests, "_wait") as pause:
            result = requests.request("GET", "url", {}, {"z": 2, "a": 1})
        self.assertEqual(result[0], 503)
        self.assertEqual(bodies, [canonical({"a": 1, "z": 2})] * 4)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [1, 2, 4])

    def test_invalid_concurrency_and_pacing_rejected(self):
        for workers in (0, 5, True, 1.5):
            with self.assertRaises(TraceError):
                MiroRequests(None, workers=workers)
        for interval in (-1, float("nan"), float("inf"), True, "0"):
            with self.assertRaises(TraceError):
                MiroRequests(None, interval=interval)


if __name__ == "__main__":
    unittest.main()
