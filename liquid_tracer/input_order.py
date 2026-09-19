"""Display input continuations before unrelated transaction context.

Priority belongs to an exact displayed outpoint, never to an address label,
ownership attribution, or the order of the transaction's evidence records.
"""

from collections import defaultdict


INPUT_ORDER_VERSION = 3


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


def input_order_metadata(graph, policy="traced_first"):
    return {"version": INPUT_ORDER_VERSION, "rule": "displayed_child_outputs_preferred",
            "policy": policy, "crossing_avoidance_first": True,
            "transaction_count": len(input_orders(graph))}


def centered_input_positions(graph, aligned):
    """Preserve ordered WEST inputs around an explicitly centered change path.

    One continuation keeps its 50% attachment. Other inputs occupy distinct
    positions above or below it, including any additional change continuations.
    Their address objects still keep their explicitly aligned change rows.
    """
    incoming = defaultdict(list)
    nodes = {node["id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        if nodes[edge["target"]]["kind"] == "transaction":
            incoming[edge["target"]].append(edge)
    geometry = graph.get("layout", {}).get("input_order", {}).get("policy") == "geometry"
    semantic = input_orders(graph)
    input_ranks = {key: {edge: i for i, edge in enumerate(order)} for key, order in semantic.items()}
    orders = {} if geometry else semantic
    result = {}
    for key, edges in incoming.items():
        if not any(edge["id"] in aligned for edge in edges):
            continue
        # Re-evaluate physical source rows after change alignment for the
        # geometry alternative. The traced-first alternative retains the
        # semantic order; candidate scoring decides whether it is safe.
        def current_position(edge):
            if geometry:
                source = nodes[edge["source"]]
                position = edge.get("attachment", {}).get("startItem", {}).get("position", {})
                fraction = float(str(position.get("y", "50%")).removesuffix("%")) / 100
                y = source["y"] if edge["id"] in aligned else source["y"] + (fraction - .5) * source["height"]
                preferred = input_ranks.get(key, {}).get(edge["id"], float("inf"))
                return y, preferred, _vin_index(edge), edge["id"]
            position = edge.get("attachment", {}).get("endItem", {}).get("position", {})
            try:
                y = float(str(position.get("y", "50%")).removesuffix("%"))
            except ValueError:
                y = 50.0
            return y, 0, _vin_index(edge), edge["id"]

        order = orders.get(key) or [edge["id"] for edge in sorted(edges, key=current_position)]
        centers = [i for i, key in enumerate(order) if key in aligned]
        center = centers[0]
        for index, key in enumerate(order):
            if index < center:
                y = 50 * (index + 1) / (center + 1)
            elif index > center:
                y = 50 + 50 * (index - center) / (len(order) - center)
            else:
                y = 50
            result[key] = y
    return result
