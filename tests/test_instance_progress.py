"""Shared scheduling progress explains waits without revealing coordination files."""

import json
import unittest

from liquid_tracer.progress import public_progress


class InstanceProgressTests(unittest.TestCase):
    def event(self, **extra):
        return {"phase": "optimizing", "completed": 0, "total": 0,
                "stage": "resource_wait", "active_layouts": 2,
                "machine_heap_mb": 32000, "message": "PRIVATE", **extra}

    def test_layout_wait_and_allocation_survive_repeated_sanitizing(self):
        for stage in ("resource_wait", "resource_allocated", "calculating"):
            with self.subTest(stage=stage):
                value = public_progress(self.event(stage=stage))
                self.assertEqual(value["stage"], stage)
                self.assertEqual(value["active_layouts"], 2)
                self.assertIn("sharing resources across 2 layouts", value["message"])
                self.assertEqual(public_progress(value), value)
                self.assertNotIn("PRIVATE", json.dumps(value))

    def test_shared_api_progress_includes_combined_rate_and_cooldown(self):
        for phase in ("address_counts", "collecting"):
            value = public_progress({"phase": phase, "completed": 0, "total": 3,
                                    "shared_api_active_clients": 2, "shared_api_effective_rps": 49.,
                                    "shared_api_wait_seconds": 3., "shared_api_wait_reason": "server_cooldown",
                                    "token": "PRIVATE", "host": "PRIVATE", "message": "PRIVATE"})
            self.assertIn("API budget shared by 2 clients (49.0 requests/s total)", value["message"])
            self.assertIn("waiting for the shared API cooldown", value["message"])
            self.assertEqual(public_progress(value), value)
            self.assertNotIn("PRIVATE", json.dumps(value))

    def test_invalid_scheduler_numbers_and_untrusted_reasons_are_discarded(self):
        for invalid in (True, -1, "PRIVATE", [], {}, None, float("inf"), float("nan"), 10 ** 400):
            for field in ("active_layouts", "machine_heap_mb"):
                with self.subTest(field=field, invalid=invalid):
                    value = public_progress(self.event(**{field: invalid}))
                    self.assertNotIn(field, value)
                    json.dumps(value, allow_nan=False)
            for field in ("shared_api_active_clients", "shared_api_effective_rps", "shared_api_wait_seconds",
                          "shared_api_wait_reason"):
                with self.subTest(field=field, invalid=invalid):
                    value = public_progress(self.event(phase="address_counts", **{field: invalid}))
                    self.assertNotIn(field, value)
                    self.assertNotIn("PRIVATE", json.dumps(value, allow_nan=False))

    def test_phase_boundaries_prevent_irrelevant_resource_messages(self):
        value = public_progress(self.event(phase="preflight", shared_api_active_clients=2,
                                           shared_api_effective_rps=49, shared_api_wait_reason="server_cooldown"))
        for field in ("active_layouts", "machine_heap_mb", "shared_api_active_clients",
                      "shared_api_effective_rps", "shared_api_wait_reason"):
            self.assertNotIn(field, value)


if __name__ == "__main__":
    unittest.main()
