"""Reuse completed full-graph ELK previews without treating them as evidence.

The current graph is rebuilt from verified run evidence before this lookup.
Only an exact non-geometric match may supply coordinates. Filtered connection
snapshots, compact layouts, other runs and stale presentation data never match.
"""

import hashlib
import re
from pathlib import Path

from .common import TraceError, canonical, read_json
from .elk_layout import ALGORITHM, ELK_VERSION, _validate_graph
from .layout_preview import _geometry
from .edge_labels import LABEL_LAYOUT_VERSION
from .input_order import INPUT_ORDER_VERSION

_NODE_GEOMETRY = frozenset({"x", "y"})
_EDGE_GEOMETRY = frozenset({"attachment", "route", "connector_shape", "routing_exception", "label_layout"})
_FILES = ("graph.json", "layout-report.json", "graph.svg", "graph.html")


def _fingerprint(graph):
    """Hash semantic records one at a time instead of copying the whole graph."""
    result = hashlib.sha256()
    metadata = {key: value for key, value in graph.items()
                if key not in {"nodes", "edges", "layout", "connector_attachment", "graph_options"}}
    metadata["graph_options"] = {key: value for key, value in graph.get("graph_options", {}).items()
                                 if key != "connector_style"}
    result.update(canonical(metadata))
    for field, excluded in (("nodes", _NODE_GEOMETRY), ("edges", _EDGE_GEOMETRY)):
        result.update(b"\0" + field.encode() + b"\0")
        for item in sorted(graph[field], key=lambda value: value["id"]):
            result.update(canonical({key: value for key, value in item.items() if key not in excluded}))
            result.update(b"\0")
    return result.digest()


def _signature(directory):
    values = []
    for name in _FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise TraceError("Incomplete ELK preview")
        stat = path.stat()
        values.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return values


def report_phase(progress, phase):
    if progress is not None:
        try:
            progress({"phase": phase, "completed": 0, "total": 0})
        except Exception:
            pass  # Advisory UI output never changes publication behavior.


def reusable_elk_preview(graph, directory, connector_style="straight", progress=None):
    """Return an exact, validated full-graph layout or None, without any writes.

    Old ELK previews have no checksum manifest. Revalidate their entire semantic
    content against the freshly built graph, then check geometry and the Miro
    plan. A preview cannot add/drop evidence, change labels, or cross namespaces.
    Invalid/incomplete optional previews are ignored, not used as a fallback
    layout. The caller runs the selected ELK engine normally when none matches.
    """
    run_id = graph.get("run_id")
    if (directory is None or not isinstance(run_id, str)
            or not re.fullmatch(r"[0-9a-f]{16}", run_id)
            or graph.get("graph_options", {}).get("view") is not None):
        return None
    directory = Path(directory)
    if any(path.is_symlink() for path in (directory, *directory.parents)) or not directory.is_dir():
        return None
    pattern = re.compile(re.escape(run_id) + r"-elk-[0-9a-f]{8}\Z")
    report_phase(progress, "checking_layout")
    try:
        candidates = [(path.stat().st_mtime_ns, path.name, path)
                      for path in directory.iterdir()
                      if pattern.fullmatch(path.name) and not path.is_symlink() and path.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    expected = _fingerprint(graph)
    from .miro import make_plan, validate_plan
    for _, _, path in sorted(candidates, reverse=True):
        try:
            before = _signature(path)
            saved = read_json(path / "graph.json")
            layout = saved.get("layout", {})
            if (layout.get("algorithm") != ALGORITHM or layout.get("version") != ELK_VERSION
                    or layout.get("edge_labels", {}).get("version") != LABEL_LAYOUT_VERSION
                    or layout.get("input_order", {}).get("version") != INPUT_ORDER_VERSION
                    or "compaction" in layout or "fallback_reason" in layout
                    or saved.get("connector_attachment") != "transaction_ports_v2"
                    or saved.get("graph_options", {}).get("connector_style") != connector_style
                    or _fingerprint(saved) != expected):
                continue
            report = read_json(path / "layout-report.json")
            if (report.get("run_id") != run_id or report.get("layout") != layout
                    or report.get("node_count") != len(saved["nodes"])
                    or report.get("edge_count") != len(saved["edges"])):
                continue
            _validate_graph(saved, connector_style)
            _geometry(saved)
            validate_plan(make_plan(saved))
            if _signature(path) != before:
                continue
        except (TraceError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
        report_phase(progress, "reusing_layout")
        return saved
    return None
