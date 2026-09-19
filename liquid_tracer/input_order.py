"""Display input continuations before unrelated transaction context.

Priority belongs to an exact displayed outpoint, never to an address label,
ownership attribution, or the order of the transaction's evidence records.
"""

from collections import defaultdict


INPUT_ORDER_VERSION = 1


def _vin_index(edge):
    # build_graph's input IDs retain the original vin index. Lexical sorting
    # would incorrectly put vin 10 before vin 2.
    suffix = edge["id"].rsplit(":", 1)[-1]
    return int(suffix) if suffix.isascii() and suffix.isdecimal() else float("inf")


def input_orders(graph):
    """Return top-to-bottom input edge IDs for mixed continuation/context nodes.

    All-continuation and all-context transactions retain ordinary ELK ordering.
    A shared address cannot transfer priority between its different UTXOs.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    outputs, incoming = defaultdict(list), defaultdict(list)
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        if (source["kind"] == "transaction" and target["kind"] == "address"
                and isinstance(edge.get("outpoint"), str) and edge["outpoint"]):
            outputs[edge["outpoint"], edge["target"]].append(source)
        if target["kind"] == "transaction":
            incoming[edge["target"]].append(edge)
    result = {}
    for key, edges in incoming.items():
        ranked = []
        for edge in edges:
            vin = edge.get("details", {}).get("vin", {})
            source = nodes[edge["source"]]
            continuing = (source["kind"] == "address"
                          and source.get("details", {}).get("network", "liquid") == "liquid"
                          and not vin.get("is_pegin") and not vin.get("is_coinbase")
                          and any(parent["id"] != key and parent["column"] < nodes[key]["column"]
                                  for parent in outputs.get((edge.get("outpoint"), edge["source"]), [])))
            ranked.append((not continuing, _vin_index(edge), edge["id"]))
        if any(not row[0] for row in ranked) and any(row[0] for row in ranked):
            result[key] = [row[2] for row in sorted(ranked)]
    return result


def input_order_metadata(graph):
    return {"version": INPUT_ORDER_VERSION, "rule": "displayed_child_outputs_first",
            "transaction_count": len(input_orders(graph))}


def centered_input_positions(graph, aligned):
    """Preserve ordered WEST inputs around an explicitly centered change path.

    Change continuations keep their 50% attachment. Inputs before/after that
    continuation occupy the corresponding half of the transaction's left side.
    Several aligned continuations may share the same center attachment, as
    required by the existing change-row constraints.
    """
    result = {}
    for order in input_orders(graph).values():
        centers = [i for i, key in enumerate(order) if key in aligned]
        if not centers:
            continue
        first, last = centers[0], centers[-1]
        for index, key in enumerate(order):
            if index < first:
                y = 50 * (index + 1) / (first + 1)
            elif index > last:
                y = 50 + 50 * (index - last) / (len(order) - last)
            else:
                y = 50
            result[key] = y
    return result
