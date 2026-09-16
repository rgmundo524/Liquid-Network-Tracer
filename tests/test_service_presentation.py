"""Investigator service designations stay tentative across graph products."""

import copy
import csv
import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import save_json
from liquid_tracer.csv_export import export_csv
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import (COLORS, NODE_CSV_FIELDS, PRESENTATION_VERSION,
                                  build_graph, export_run, legend_lines, svg_graph)
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, X, fixture


ROOT = Path(__file__).resolve().parents[1]
HAS_ELK = bool(shutil.which("node") and (ROOT / "layout/node_modules/elkjs/package.json").is_file())
ADDRESS = "SYNTHETIC-branch-A"


def designation(address=ADDRESS, **values):
    return {"kind": "address", "value": address, "entity": "Synthetic exchange",
            "classification": "suspected_service", "source": "Investigator designation",
            "confidence": "candidate", "managed_by": "case_service_rules", "stop": True,
            "observed_at": "2026-01-01T00:00:00+00:00",
            "rationale": "Synthetic consolidation pattern; review required", **values}


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class ServicePresentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "case")
        self.addCleanup(self.store.close)
        fixture_path = self.root / "fixture.json"
        save_json(fixture_path, fixture())
        limits = Limits(max_hops=3)
        with Esplora(self.store, "pending", limits, fixture=fixture_path, min_interval=0) as api:
            state = new_state([A + ":0"], api.base, limits, [], case_id="synthetic-services")
            api.run_id = state["run_id"]
            self.state = trace(api, state, limits, self.root / "checkpoint.json")

    @staticmethod
    def node(graph, outpoint=B + ":0"):
        return next(node for node in graph["nodes"] if node["kind"] == "address"
                    and node["details"].get("network") == "liquid"
                    and any(item["outpoint"] == outpoint for item in node["details"]["occurrences"]))

    def test_designation_reaches_every_occurrence_without_rewriting_evidence_or_topology(self):
        baseline = build_graph(self.state)
        label = designation()
        self.state["labels"] = [label]
        original = copy.deepcopy(self.state)
        with patch.object(Esplora, "get", side_effect=AssertionError("Rendering cannot fetch address history")):
            graph = build_graph(self.state)
        self.assertEqual(self.state, original)
        self.assertEqual({node["id"] for node in baseline["nodes"]}, {node["id"] for node in graph["nodes"]})
        self.assertEqual(baseline["edges"], graph["edges"])
        service_nodes = [node for node in graph["nodes"] if node["details"].get("address_attributions")]
        self.assertEqual({node["id"] for node in service_nodes},
                         {"liquid:address:" + ADDRESS})
        for node in service_nodes:
            self.assertEqual(node["color"], COLORS["candidate"])
            self.assertIn("Suspected Synthetic exchange", node["label"].splitlines())
            self.assertEqual(node["details"]["address_attributions"], [label])
            self.assertNotIn(label["rationale"], node["label"])
        self.assertEqual(graph["presentation_version"], PRESENTATION_VERSION)

    def test_default_service_name_is_not_repeated(self):
        self.state["labels"] = [designation(entity="Suspected service")]
        node = self.node(build_graph(self.state))
        self.assertEqual(node["label"].count("Suspected service"), 1)

    def test_presentation_records_a_separate_current_service_control_snapshot(self):
        controls = {"schema_version": 1, "case_id": "synthetic-services", "revision": 3,
                    "rules": {ADDRESS: {"enabled": True, "rationale": "Synthetic review"}}}
        self.state["service_controls"] = copy.deepcopy(controls)
        graph = build_graph(self.state)
        self.assertEqual(graph["service_controls"], controls)
        graph["service_controls"]["rules"][ADDRESS]["rationale"] = "Changed presentation copy"
        self.assertEqual(self.state["service_controls"], controls)

    def test_unassigned_names_preserve_seed_context_and_unspent_colors(self):
        for outpoint, address in ((A + ":0", "SYNTHETIC-victim-deposit"),
                                  (X + ":0", "SYNTHETIC-funding-context"),
                                  (C + ":0", ADDRESS)):
            with self.subTest(outpoint=outpoint):
                state = copy.deepcopy(self.state)
                state["labels"] = [designation(address)]
                if outpoint == C + ":0":
                    state["links"].pop(outpoint, None)
                    state["transactions"].pop(D, None)
                    state["outputs"][outpoint].update(status="unspent_at_observation",
                        observed_spend={"spent": False}, spend_observation_id=123)
                node = self.node(build_graph(state), outpoint)
                role = "seed" if outpoint == A + ":0" else "address" if outpoint == X + ":0" else "unspent_endpoint"
                self.assertEqual(node["role"], role)
                self.assertEqual(node["color"], COLORS[role])
                if outpoint == C + ":0":
                    self.assertIn("Unspent endpoint", node["label"].splitlines())

    def test_service_stop_and_held_statuses_do_not_claim_an_unspent_endpoint(self):
        for status in ("suspected_service_stop", "held_behind_service"):
            for labelled in (False, True):
                with self.subTest(status=status, labelled=labelled):
                    state = copy.deepcopy(self.state)
                    state["links"].pop(C + ":0", None)
                    state["transactions"].pop(D, None)
                    state["outputs"][C + ":0"].update(status=status, observed_spend={"spent": False},
                                                    spend_observation_id=123)
                    state["labels"] = [designation()] if labelled else []
                    node = self.node(build_graph(state), C + ":0")
                    self.assertEqual(node["role"], "candidate")
                    self.assertNotIn("Unspent endpoint", node["label"])
                    self.assertNotIn("unspent_endpoints", node["details"])

    def test_confidence_controls_display_without_hiding_alternative_assessment(self):
        for confidence in ("candidate", "corroborated", "confirmed"):
            with self.subTest(confidence=confidence):
                manual = {"kind": "outpoint", "value": B + ":0", "entity": "Synthetic custodian",
                          "source": "fixture://manual", "confidence": confidence, "observed_at": "2026-01-02"}
                self.state["labels"] = [designation(), manual]
                node = self.node(build_graph(self.state))
                role = "candidate"  # Confidence changes the name, never the color.
                self.assertEqual(node["role"], role)
                self.assertEqual(node["color"], COLORS[role])
                self.assertIn("Suspected Synthetic exchange", node["label"].splitlines())
                prefix = "" if confidence == "confirmed" else "Suspected "
                self.assertIn(prefix + "Synthetic custodian", node["label"].splitlines())
                self.assertIn(designation(), node["details"]["address_attributions"])
                self.assertIn(manual, node["details"]["address_attributions"])

    def test_merged_color_and_service_labels_are_deterministic(self):
        labels = [designation(entity="Synthetic Z"), designation(entity="Synthetic A")]
        signatures = []
        for reversed_order in (False, True):
            state = copy.deepcopy(self.state)
            state["labels"] = list(reversed(labels)) if reversed_order else labels
            if reversed_order:
                state["transactions"] = dict(reversed(list(state["transactions"].items())))
            graph = build_graph(state, merge_addresses=True)
            node = next(node for node in graph["nodes"] if node["id"] == "liquid:address:" + ADDRESS)
            signatures.append((node["role"], node["color"], node["label"], node["details"]["address_attributions"]))
            self.assertEqual(node["role"], "candidate")
            self.assertEqual({item["outpoint"] for item in node["details"]["occurrences"]}, {B + ":0", C + ":0"})
        self.assertEqual(signatures[0], signatures[1])

    def test_bitcoin_input_does_not_inherit_liquid_service_designation(self):
        self.state["transactions"][B]["data"]["vin"][0]["is_pegin"] = True
        self.state["labels"] = [designation("SYNTHETIC-victim-deposit")]
        graph = build_graph(self.state)
        bitcoin = next(node for node in graph["nodes"] if node["kind"] == "address" and node["details"]["network"] == "bitcoin")
        self.assertEqual(bitcoin["role"], "address")
        self.assertNotIn("address_attributions", bitcoin["details"])

    def test_all_graph_products_preserve_unassigned_color_labels_and_explorer(self):
        self.state["labels"] = [designation()]
        self.state["source"] = "https://blockstream.info/liquid/api"
        graph = build_graph(self.state)
        node = self.node(graph)
        self.assertEqual(node["url"], "https://blockstream.info/liquid/address/" + ADDRESS)
        plan = make_plan(graph)
        shape = next(item for item in plan["shapes"] if item["key"] == node["id"])
        self.assertEqual(shape["body"]["style"]["fillColor"], COLORS["candidate"])
        self.assertIn("Suspected Synthetic exchange", shape["body"]["data"]["content"])
        self.assertIn(node["url"], shape["body"]["data"]["content"])
        source = mermaid_source(graph)
        self.assertIn("Suspected Synthetic exchange", source)
        self.assertIn("fill:" + COLORS["candidate"], source)
        for svg, attribute in ((svg_graph(graph), "data-key"), (render_svg(graph), "data-node-id")):
            document = ET.fromstring(svg)
            group = next(element for element in document.iter() if element.get(attribute) == node["id"])
            self.assertTrue(any(child.get("fill") == COLORS["candidate"] for child in group.iter()))
            self.assertIn("Suspected Synthetic exchange", " ".join(group.itertext()))
        legend = " ".join(legend_lines())
        self.assertIn("confidence never selects a color", legend)
        self.assertIn("selected seed red > assigned name color", legend)

    @unittest.skipUnless(HAS_ELK, "local Node and pinned ELK dependency are required")
    def test_real_elk_preserves_service_evidence_and_role(self):
        self.state["labels"] = [designation()]
        graph = build_graph(self.state)
        node = self.node(optimize_graph(graph))
        for field in ("role", "color", "label", "details", "url"):
            self.assertEqual(node[field], self.node(graph)[field])

    def test_transaction_csv_preserves_current_legacy_labels_and_original_archive(self):
        from liquid_tracer.transaction_csv import TRANSACTION_CSV_FIELDS
        archive = self.root / "saved-run"
        export_run(self.store, self.state, archive)
        before = {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}
        label = designation(entity="=Synthetic service", rationale="@Manual hypothesis, not attribution")
        self.state["labels"] = [label]
        # Historical graph metadata was not always a complete modern settings
        # document. Exact occurrence labels, not rebuilt rules, are authoritative.
        self.state["service_controls"] = {"revision": 3, "rules": {ADDRESS: {"enabled": True}}}
        destination = self.root / "export"
        export_csv(build_graph(self.state), archive, destination)
        rows = [row for row in read_csv(destination / "transactions.csv") if row["Address Hash"] == ADDRESS]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(tuple(row), TRANSACTION_CSV_FIELDS)
            self.assertEqual(row["Address Label"], "Suspected " + label["entity"])
            self.assertEqual(row["Address Entities"], "'" + label["entity"])
            self.assertIn("STOP TRACING", row["Address Flags"])
        info = json.loads((destination / "export.json").read_text())
        self.assertEqual(info["service_controls"], self.state["service_controls"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()})
        self.assertEqual({p.name for p in destination.glob("*.csv")}, {"transactions.csv"})

    def test_transaction_csv_multiple_designations_are_occurrence_scoped(self):
        archive = self.root / "saved-run"
        export_run(self.store, self.state, archive)
        self.state["labels"] = [designation(entity="Synthetic Z", rationale="Z rationale"),
                                designation(entity="Synthetic A", rationale="A rationale")]
        destination = self.root / "export"
        export_csv(build_graph(self.state, merge_addresses=True), archive, destination)
        rows = [row for row in read_csv(destination / "transactions.csv") if row["Address Hash"] == ADDRESS]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["Address Entities"], "Synthetic A; Synthetic Z")
            self.assertEqual(row["Address Label"], "Suspected Synthetic A; Suspected Synthetic Z")
        other = [row for row in read_csv(destination / "transactions.csv") if row["Address Hash"] != ADDRESS]
        self.assertTrue(all(not row["Address Label"] for row in other))


if __name__ == "__main__":
    unittest.main()
