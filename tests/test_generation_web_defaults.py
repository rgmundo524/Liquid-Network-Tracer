"""Browser generation preferences preserve explicit layout choices."""

import unittest
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.investigations import read_case
from tests import test_web


class GenerationWebDefaultsTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def test_browser_created_cases_default_to_trace_and_preserve_explicit_standard(self):
        for style in (None, "standard"):
            for grouping in (False, True):
                with self.subTest(style=style, grouping=grouping):
                    settings = {"group_context_inputs": grouping}
                    if style is not None:
                        settings["layout_style"] = style
                    result = self.success("/api/cases", {"name": "Browser generation",
                        "seeds": [test_web.synthetic_txid() + ":0"], "settings": settings}, 201)
                    case, _ = self.server.case(result["id"])
                    for actual in (result["run_defaults"], read_case(case)["run_defaults"]):
                        self.assertEqual(actual["layout_style"], style or "trace")
                        self.assertIs(actual["group_context_inputs"], grouping)

    def test_case_summaries_offer_trace_for_missing_style_without_rewriting_metadata(self):
        _, result = self.create()
        case, metadata = self.server.case(result["id"])
        for style in (None, "standard"):
            with self.subTest(style=style):
                metadata["run_defaults"] = {"group_context_inputs": False}
                if style is not None:
                    metadata["run_defaults"]["layout_style"] = style
                save_json(case / "case.json", metadata)
                before = (case / "case.json").read_bytes()
                summaries = (self.server.case_summary(case, metadata),
                             self.success("/api/cases/" + result["id"] + "/overview"))
                for summary in summaries:
                    self.assertEqual(summary["run_defaults"]["layout_style"], style or "trace")
                    self.assertFalse(summary["run_defaults"]["group_context_inputs"])
                self.assertEqual((case / "case.json").read_bytes(), before)

    def test_collection_preference_updates_do_not_insert_standard_into_missing_style(self):
        _, result = self.create()
        case, _ = self.server.case(result["id"])
        route = "/api/cases/" + result["id"] + "/actions"
        for style in (None, "standard"):
            for grouping in (False, True):
                with self.subTest(style=style, grouping=grouping):
                    settings = {"hops": 2, "group_context_inputs": grouping}
                    if style is not None:
                        settings["layout_style"] = style
                    with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
                        self.success(route, {"action": "trace", "settings": settings}, 202)
                    start.assert_called_once()
                    actual = read_case(case)["run_defaults"]
                    self.assertEqual(actual["layout_style"], style or "trace")
                    self.assertIs(actual["group_context_inputs"], grouping)


if __name__ == "__main__":
    unittest.main()
