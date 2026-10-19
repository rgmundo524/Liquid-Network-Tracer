"""Build saved v2 presentations for backward-compatibility regression tests."""
from liquid_tracer.export import build_graph as _build
from liquid_tracer.context_groups import group_context_inputs as _group
from liquid_tracer.context_connectors import prepare as _prepare


def prepare(graph):
    return _prepare(graph, version=2)


def build_graph(*args, **kwargs):
    graph = _build(*args, **kwargs)
    graph.pop("context_connectors", None)
    return prepare(graph) if kwargs.get("group_context_inputs") else graph


def group_context_inputs(graph, *, enabled=False):
    if not enabled or graph.get("context_groups", {}).get("enabled"):
        return graph
    graph = _group(graph, enabled=True)
    graph.pop("context_connectors", None)
    return prepare(graph)
