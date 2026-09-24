"""Optional presentation summaries for isolated external transaction inputs.

Every input connector keeps its vin, outpoint, quantity and evidence. Only its
displayed source changes; full original address nodes remain inside the summary.
This operation never groups a displayed asset-flow continuation or infers common
ownership. Rebuilding the graph with grouping disabled restores original nodes.
"""

from collections import defaultdict
from copy import deepcopy


CONTEXT_GROUP_VERSION = 2
NOTICE = ("Context summaries contain isolated external input addresses, not an "
          "ownership group. Original addresses and input evidence remain in local exports.")


def _protected(node):
    """Keep investigative meaning and explicit designations individually visible."""
    details = node.get("details", {})
    if node.get("role", "address") != "address":
        return True
    fields = ("attribution_reference", "convergence", "address_convergence", "layout_hub",
              "address_interactions", "interaction_types", "name", "notes",
              "source", "confidence", "service_name", "stop_tracing",
              "name_colors", "name_color_conflict", "change_output")
    if any(node.get(field) for field in fields):
        return True
    if details.get("address_attributions") or details.get("unspent_endpoints"):
        return True
    return any(item.get("labels") or item.get("trace")
               for item in details.get("occurrences", []))


def _input_edge(edge, source, target):
    vin = edge.get("details", {}).get("vin", {})
    return (edge["source"] == source and edge["target"] == target
            and edge.get("role") == "context_input"
            and edge["id"].startswith("in:")
            and not vin.get("is_pegin") and not vin.get("is_coinbase")
            and not edge.get("details", {}).get("validated_trace_link")
            and not edge.get("change_output"))


def group_context_inputs(graph, *, enabled=False):
    """Return a grouped presentation copy, or the original graph when disabled.

Call after attribution, count, convergence and change annotations, before the
layout engine. Eligibility is checked by exact address across *all* displayed
occurrences, including legacy separate-outpoint views. At least two distinct
addresses are required; all must connect only as external inputs to one tx.
"""
    if not enabled:
        return graph
    result = deepcopy(graph)
    if result.get("context_groups", {}).get("enabled"):
        return result
    nodes = {node["id"]: node for node in result["nodes"]}
    incident, identities = defaultdict(list), defaultdict(list)
    for edge in result["edges"]:
        incident[edge["source"]].append(edge)
        incident[edge["target"]].append(edge)
    for node in nodes.values():
        details = node.get("details", {})
        address = details.get("address")
        if (node["kind"] == "address" and details.get("network") == "liquid"
                and isinstance(address, str) and address):
            identities[address].append(node)

    # An investigator may designate change from a parent not displayed in this
    # bounded graph. That choice still prevents its address being summarized.
    designated = {f"{txid}:{item['vout']}"
                  for txid, item in result.get("service_controls", {}).get("change_outputs", {}).items()
                  if isinstance(item, dict) and type(item.get("vout")) is int}
    candidates = defaultdict(list)
    for address in sorted(identities):
        members = identities[address]
        if any(_protected(node) for node in members):
            continue
        edges = [edge for node in members for edge in incident[node["id"]]]
        if not edges:
            continue
        targets = {edge["target"] for edge in edges}
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        if nodes.get(target, {}).get("kind") != "transaction":
            continue
        if any(not _input_edge(edge, node["id"], target)
               or edge.get("outpoint") in designated
               for node in members for edge in incident[node["id"]]):
            continue
        candidates[target].append((address, members))

    removed, summaries = set(), []
    for target in sorted(candidates):
        addresses = candidates[target]
        if len(addresses) < 2:
            continue
        members = sorted((node for _, group in addresses for node in group),
                         key=lambda node: node["id"])
        member_ids = {node["id"] for node in members}
        inputs = [edge for node in members for edge in incident[node["id"]]]
        key = "context-group:" + target.removeprefix("tx:")
        input_count = len(inputs)
        dense = input_count > 8
        summary = {
            "id": key, "kind": "context_group", "role": "context_group",
            "label": (f"{len(addresses)} context addresses\n"
                      f"{input_count} transaction inputs\nDetails in local export"),
            "column": nodes[target]["column"] - 1,
            "x": min(node["x"] for node in members),
            "y": sum(node["y"] for node in members) / len(members),
            # The rectangle summarizes input evidence; its size does not need
            # one text row per UTXO. ELK can preserve distinct zero-size ports
            # within this fixed shape, as it already does on transactions.
            "width": 240, "height": 160,
            "color": members[0].get("color", "#f5f6f8"),
            "text_color": members[0].get("text_color", "#15253b"),
            "url": None,
            "details": {"transaction_id": target, "address_count": len(addresses),
                        "input_count": input_count, "members": members,
                        "input_edge_ids": sorted(edge["id"] for edge in inputs),
                        "notice": NOTICE},
        }
        for edge in inputs:
            edge["original_source"] = edge["source"]
            edge["source"] = key
            if dense:
                # Keep every connector and its original evidence. Reserving a
                # separate visible caption for hundreds of parallel inputs
                # creates an enormous layout channel beside a small summary.
                edge["caption_display"] = "details_only"
        removed.update(member_ids)
        summaries.append(summary)
    result["nodes"] = [node for node in result["nodes"] if node["id"] not in removed] + summaries
    result.setdefault("graph_options", {})["group_context_inputs"] = True
    result["context_groups"] = {
        "version": CONTEXT_GROUP_VERSION, "enabled": True,
        "group_count": len(summaries), "grouped_node_count": len(removed),
        "address_count": sum(node["details"]["address_count"] for node in summaries),
        "input_count": sum(node["details"]["input_count"] for node in summaries),
        "notice": NOTICE,
    }
    if summaries:
        result["notice"] = (result.get("notice", "") + " " + NOTICE).strip()
    return result
