"""Shared output columns for a board update's newly staged objects.

Columns follow the graph's transaction dependencies, including explicit hub
resets. Collection hop budgets are not a safe ordering when seeds overlap.
The full graph supplies ownership before retained board objects are filtered.
"""

from collections import defaultdict

from .common import TraceError
from .hub_layout import hub_layout_view


def update_columns(graph, new_ids):
    view = hub_layout_view(graph)
    nodes = {node["id"]: node for node in view["nodes"]}
    # Legacy synthetic/preview graphs without dependency columns retain the
    # existing validation path in the layout engine.
    if any(type(node.get("column")) is not int for node in nodes.values()):
        return None
    fees = set(graph.get("fee_items", {}))
    columns = {key: 2 * node["column"] for key, node in nodes.items() if key in new_ids and key not in fees}
    producers = defaultdict(set)
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        if (source["kind"] == "transaction" and target["kind"] in ("address", "event")
                and target["id"] in columns):
            producers[target["id"]].add(source["column"])
    outputs, shared = [], []
    for key, stages in producers.items():
        if len(stages) == 1 and not nodes[key].get("layout_hub"):
            columns[key] = 2 * (next(iter(stages)) + 1)
            outputs.append(key)
        else:
            # One reused address cannot occupy several output columns. Keep
            # its established compromise/hub column and every connection.
            shared.append(key)
    # A reused address can have the same original column as an adjacent
    # transaction. Give such a join an intermediate slot: an in-layer edge
    # would otherwise force ELK to push only part of an output column forward.
    # These temporary slots do not change the saved dependency columns.
    shifts = {}
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        if (source["id"] in columns and source["kind"] != "transaction"
                and target["kind"] == "transaction"
                and columns[source["id"]] == 2 * target["column"]):
            shifts[source["id"]] = -1
        elif (target["id"] in columns and target["kind"] != "transaction"
                and source["kind"] == "transaction"
                and columns[target["id"]] == 2 * source["column"]):
            shifts.setdefault(target["id"], 1)
    output_ids = set(outputs)
    for key, shift in shifts.items():
        if key not in output_ids:
            columns[key] += shift
    return {"version": 1, "columns": columns, "outputs": sorted(outputs),
            "shared_outputs": sorted(shared), "basis": "transaction_dependencies"}


def constrain_request(request, alignment):
    """Reserve native ELK layers for real columns and intermediate captions.

    Center captions create dummy layers in ELK. A tiny temporary spacer in each
    gap gives them a layer without pushing some outputs into later columns.
    Spacers have no connections and are stripped from worker results, never
    becoming graph evidence or Miro items. ELK still calculates all real routes.
    """
    columns = alignment["columns"]
    children = request["children"]
    if set(columns) != {node["id"] for node in children} or any(type(value) is not int for value in columns.values()):
        raise TraceError("Invalid output alignment columns; no Miro changes were made")
    levels = {column: index for index, column in enumerate(sorted(set(columns.values())))}
    stride = max([1000.] + [4 * node["width"] for node in children]
                 + [4 * label["width"] for edge in request["edges"] for label in edge.get("labels", [])])
    for node in children:
        node["x"] = stride * (levels[columns[node["id"]]] + 1) - node["width"] / 2
        node["y"] = 0
        node["layoutOptions"]["elk.alignment"] = "CENTER"
    ids = set(columns)
    spacers = []
    for index in range(max(0, len(levels) - 1)):
        key = "output-column-spacer:" + str(index)
        while key in ids:
            key += ":"
        ids.add(key)
        spacers.append(key)
        children.append({"id": key, "x": stride * (index + 1.5), "y": 0,
                         "width": 1, "height": 1, "ports": [],
                         "layoutOptions": {"elk.alignment": "CENTER"}})
    for field in ("branchNodeOrder", "centerNodeOrder"):
        if field in request:
            request[field] = [*request[field], *spacers]
    request["outputAlignmentSpacers"] = spacers
    request["outputAlignmentPositions"] = {node["id"]: node["x"] for node in children}
    request["layoutOptions"].update({"elk.separateConnectedComponents": "false",
                                     "elk.partitioning.activate": "false",
                                     "elk.layered.cycleBreaking.strategy": "INTERACTIVE",
                                     "elk.layered.layering.strategy": "INTERACTIVE"})


def validate_alignment(graph):
    alignment = graph.get("layout", {}).get("output_alignment")
    if not alignment:
        return
    nodes = {node["id"]: node for node in graph["nodes"]}
    centers = {}
    for key in alignment["outputs"]:
        node = nodes[key]
        column = alignment["columns"][key]
        expected = centers.setdefault(column, node["x"])
        if abs(node["x"] - expected) > .001:
            raise TraceError("ELK could not align the new hop outputs; no Miro changes were made")
    ordered = [centers[column] for column in sorted(centers)]
    if any(right <= left for left, right in zip(ordered, ordered[1:])):
        raise TraceError("ELK could not preserve new hop output order; no Miro changes were made")
