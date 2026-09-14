"""Import capitalization is cosmetic, never a change to address identity or evidence."""

import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_import import apply_import, parse_import, preview_import
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, service_labels, set_service

A = "SYNTHETIC-capitalization-A"
B = "SYNTHETIC-capitalization-B"


def csv_text(*rows):
    output = io.StringIO(newline="")
    fields = ("Address", "Name", "confidence", "stop_tracing", "source", "notes")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def row(**overrides):
    return {"Address": A, "Name": "BTSE", "confidence": "suspected",
            "stop_tracing": "true", "source": "Evidence/Record-A",
            "notes": "Do NOT infer ownership.", **overrides}


class ImportCapitalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Capitalization tests")

    def apply(self, text, **options):
        plan = preview_import(self.case, text, **options)
        self.assertTrue(plan["valid"], plan["errors"])
        return apply_import(self.case, text, approval_sha256=plan["approval_sha256"], **options)

    def test_confidence_case_and_whitespace_are_normalized(self):
        for value in ("confirmed", "Confirmed", "CONFIRMED", "cOnFiRmEd",
                      "suspected", "Suspected", "SUSPECTED", "sUsPeCtEd", "  Confirmed  "):
            with self.subTest(value=value):
                parsed = parse_import(csv_text(row(confidence=value)))
                self.assertEqual(parsed["errors"], [])
                self.assertEqual(parsed["rows"][0]["rule"]["confidence"], value.strip().lower())
                self.assertEqual(parsed["rows"][0]["rule"]["name"], "BTSE")

    def test_headers_are_case_insensitive(self):
        for header in ("ADDRESS,NAME,CONFIDENCE", "aDdReSs,nAmE,cOnFiDeNcE",
                       " Address , Name , Confidence "):
            with self.subTest(header=header):
                parsed = parse_import(header + "\n" + A + ",BTSE,Confirmed\n")
                self.assertEqual(parsed["errors"], [])
                self.assertEqual(parsed["rows"][0]["rule"]["confidence"], "confirmed")
                self.assertEqual(parsed["rows"][0]["rule"]["name"], "BTSE")

    def test_alias_headers_and_json_use_the_same_normalization(self):
        for text in ("ADDRESS,ENTITY,CONFIDENCE,RATIONALE\n" + A + ",BTSE,Suspected,Keep CASE\n",
                     json.dumps([{"ADDRESS": A, "ENTITY": "BTSE", "CONFIDENCE": "Suspected",
                                  "RATIONALE": "Keep CASE"}])):
            with self.subTest(text=text):
                parsed = parse_import(text)
                self.assertEqual(parsed["errors"], [])
                self.assertEqual(parsed["rows"][0]["rule"]["confidence"], "suspected")
                self.assertEqual(parsed["rows"][0]["rule"]["notes"], "Keep CASE")

    def test_duplicate_names_ignore_case_and_keep_first_display_spelling(self):
        parsed = parse_import(csv_text(row(), row(Name="btse", confidence="SUSPECTED"),
                                       row(Name="BtSe", confidence="Suspected")))
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["duplicates"], 2)
        self.assertEqual(len(parsed["rows"]), 1)
        self.assertEqual(parsed["rows"][0]["rule"]["name"], "BTSE")

    def test_unicode_names_casefold_without_rewriting_the_display(self):
        parsed = parse_import(csv_text(row(Name="Straße"), row(Name="STRASSE")))
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["duplicates"], 1)
        self.assertEqual(parsed["rows"][0]["rule"]["name"], "Straße")

    def test_case_only_reimports_are_noops_even_with_replace_policy(self):
        self.apply(csv_text(row(confidence="Confirmed")))
        path = self.case / "services.json"
        before = path.read_bytes()
        for policy in ("keep", "replace"):
            with self.subTest(policy=policy):
                text = csv_text(row(Name="btse", confidence="CONFIRMED"))
                plan = preview_import(self.case, text, policy=policy)
                self.assertEqual(plan["counts"]["unchanged"], 1)
                self.assertEqual(plan["changes"][0]["previous"]["name"], "BTSE")
                with patch("liquid_tracer.address_import.save_json", side_effect=AssertionError("No write")):
                    self.assertEqual(self.apply(text, policy=policy)["changed"], 0)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_case_only_reimport_preserves_original_audit_records(self):
        settings = set_service(self.case, A, name="BTSE", confidence="suspected",
                               source="Evidence/Record-A", notes="Do NOT infer ownership.")
        settings["schema_version"] = 1
        rule_value = settings["rules"][A]
        rule_value["confidence"] = "corroborated"
        rule_value["classification"] = "suspected_service"
        rule_value["rationale"] = rule_value.pop("notes")
        save_json(self.case / "services.json", settings)
        before = (self.case / "services.json").read_bytes()
        self.assertEqual(self.apply(csv_text(row(Name="btse", confidence="SUSPECTED")))["changed"], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), before)

    def test_substantive_conflicts_are_not_hidden_by_name_casefold(self):
        for change in ({"Name": "Different Exchange"}, {"confidence": "confirmed"},
                       {"stop_tracing": "false"}, {"source": "evidence/record-a"},
                       {"notes": "do not infer ownership."}):
            with self.subTest(change=change):
                changed = row(Name="btse") | change
                plan = preview_import(self.case, csv_text(row(), changed))
                self.assertFalse(plan["valid"])
                self.assertIn("Conflicting entries", plan["errors"][0]["message"])
                self.assertFalse((self.case / "services.json").exists())

    def test_real_existing_change_still_requires_replace_and_preserves_history(self):
        self.apply(csv_text(row()))
        previous = copy.deepcopy(load_services(self.case)["rules"][A])
        text = csv_text(row(Name="btse", confidence="CONFIRMED", stop_tracing="false"))
        self.assertEqual(self.apply(text)["counts"]["keep"], 1)
        self.assertEqual(load_services(self.case)["rules"][A], previous)
        self.assertEqual(self.apply(text, policy="replace")["changed"], 1)
        settings = load_services(self.case)
        self.assertEqual(settings["history"][-1]["previous"], previous)
        self.assertEqual(settings["rules"][A]["confidence"], "confirmed")
        self.assertFalse(settings["rules"][A]["stop_tracing"])

    def test_same_names_never_merge_distinct_addresses(self):
        addresses = ("VJLqUdAXaKRDFZRnzBaYLBQ8NSYnNn69Qz", "vjLqUdAXaKRDFZRnzBaYLBQ8NSYnNn69Qz")
        parsed = parse_import(csv_text(row(Address=addresses[0]), row(Address=addresses[1], Name="btse")))
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["duplicates"], 0)
        self.assertEqual([entry["rule"]["address"] for entry in parsed["rows"]], list(addresses))

    def test_saved_labels_keep_name_source_notes_and_independent_stop(self):
        self.apply(csv_text(row(confidence="CONFIRMED", stop_tracing="FALSE"),
                            row(Address=B, Name="Example Exchange", confidence="Suspected")))
        labels = {label["value"]: label for label in service_labels(load_services(self.case))}
        self.assertEqual(labels[A]["entity"], "BTSE")
        self.assertEqual(labels[A]["confidence"], "confirmed")
        self.assertFalse(labels[A]["stop"])
        self.assertEqual(labels[B]["confidence"], "suspected")
        self.assertTrue(labels[B]["stop"])
        self.assertEqual(labels[A]["source"], "Evidence/Record-A")
        self.assertEqual(labels[A]["notes"], "Do NOT infer ownership.")

    def test_changed_input_casing_still_invalidates_an_old_approval(self):
        original = csv_text(row())
        plan = preview_import(self.case, original)
        for changed in (csv_text(row(Name="btse")), csv_text(row(confidence="SUSPECTED"))):
            with self.subTest(changed=changed), self.assertRaisesRegex(TraceError, "changed"):
                apply_import(self.case, changed, approval_sha256=plan["approval_sha256"])
        self.assertFalse((self.case / "services.json").exists())

    def test_unsupported_confidences_still_reject_the_whole_batch(self):
        for value in ("Candidate", "CORROBORATED", "Maybe", "sus pected", 1, True, [], {}):
            with self.subTest(value=value):
                text = json.dumps([{"address": A}, {"address": B, "confidence": value}])
                plan = preview_import(self.case, text)
                self.assertFalse(plan["valid"])
                self.assertIsNone(plan["approval_sha256"])
                self.assertFalse((self.case / "services.json").exists())

    def test_duplicate_case_variant_headers_remain_invalid(self):
        for header in ("Address,Name,NAME", "Address,Confidence,CONFIDENCE", "Address,Name,ENTITY"):
            with self.subTest(header=header), self.assertRaises(TraceError):
                parse_import(header + "\n" + A + ",x,x\n")


if __name__ == "__main__":
    unittest.main()
