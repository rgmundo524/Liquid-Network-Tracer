"""Portable CSV tables from a verified saved run, without live API access."""

import json
from pathlib import Path

from .common import HEX64, TraceError, digest, save_json
from .export import NODE_CSV_FIELDS, node_csv_rows, write_csv

_DETAIL_FILES = ("inputs.csv", "outputs.csv", "spends.csv", "events.csv", "frontier.csv")
_NODE_FIELDS = NODE_CSV_FIELDS
_EDGE_FIELDS = ("id", "source", "target", "role", "outpoint", "label", "quantity", "details")


def _source_files(archive):
    """Capture only bytes checked against explicit saved-manifest entries.

    The CLI verifies the whole archive first. Rechecking these particular files
    prevents a missing manifest entry or a later edit from becoming trusted CSV
    evidence. The bytes we check are the bytes we subsequently copy.
    """
    try:
        lines = (archive / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise TraceError("Cannot read saved-run SHA256SUMS for CSV export") from error
    checksums = {}
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not HEX64.fullmatch(parts[0]) or parts[1] in checksums:
            raise TraceError("Malformed or duplicated entry in saved-run checksum manifest")
        checksums[parts[1]] = parts[0].lower()
    required = ("trace.json", *_DETAIL_FILES)
    for name in required:
        if name not in checksums:
            raise TraceError("Saved-run manifest is missing required CSV source: " + name)
    captured = {}
    for name in required:
        path = archive / name
        if path.is_symlink() or not path.is_file():
            raise TraceError("Missing or invalid saved CSV source: " + name)
        try:
            content = path.read_bytes()
        except OSError as error:
            raise TraceError("Cannot read saved CSV source: " + name) from error
        if digest(content) != checksums[name]:
            raise TraceError("Saved-run checksum mismatch: " + name + "; restore the original evidence")
        captured[name] = content
    return captured, checksums


def export_csv(graph, archive, directory):
    """Create a new CSV bundle, preserving all archived detailed table bytes.

    Graph tables reflect the selected presentation, including fee visibility.
    The detailed tables retain every saved output and event, including fees.
    """
    archive, directory = Path(archive), Path(directory)
    if directory.exists() or directory.is_symlink():
        raise TraceError("CSV export directory already exists; choose a new directory")
    if directory.resolve().is_relative_to(archive.resolve()):
        raise TraceError("Save CSV exports outside the saved run to preserve archived evidence")
    captured, checksums = _source_files(archive)
    try:
        state = json.loads(captured["trace.json"])
    except (ValueError, UnicodeError) as error:
        raise TraceError("Saved trace is invalid; cannot create CSV export") from error
    if not isinstance(state, dict) or state.get("run_id") != graph.get("run_id"):
        raise TraceError("CSV graph does not match the saved run")
    if state.get("case_id") != graph.get("namespace", {}).get("case_id"):
        raise TraceError("CSV graph does not match the saved case")
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise TraceError("CSV export directory already exists; choose a new directory") from error
    write_csv(directory / "nodes.csv", node_csv_rows(graph), _NODE_FIELDS)
    write_csv(directory / "edges.csv", graph["edges"], _EDGE_FIELDS)
    for name in _DETAIL_FILES:
        (directory / name).write_bytes(captured[name])
    save_json(directory / "export.json", {
        "schema_version": 1,
        "run_id": graph["run_id"],
        "case_id": state["case_id"],
        "source_archive": str(archive.resolve()),
        "source_trace_sha256": checksums["trace.json"],
        "source_files": {name: checksums[name] for name in _DETAIL_FILES},
        "address_mode": graph.get("address_mode"),
        "presentation_version": graph.get("presentation_version"),
        "graph_options": graph.get("graph_options", {}),
        **({"service_controls": graph["service_controls"]} if "service_controls" in graph else {}),
        "notice": "Graph tables use the selected presentation. Detailed tables retain all saved fees. "
                  "These checksums detect byte changes; they are not signatures or independent timestamps.",
    })
    files = ("nodes.csv", "edges.csv", *_DETAIL_FILES)
    # The manifest marks a complete bundle for local download discovery. Its
    # final name must never expose a partial write after interruption.
    temporary = directory / "SHA256SUMS.tmp"
    temporary.write_text(
        "".join(digest((directory / name).read_bytes()) + "  " + name + "\n"
                for name in sorted((*files, "export.json"))), encoding="utf-8")
    temporary.replace(directory / "SHA256SUMS")
    return {"directory": str(directory.resolve()),
            "files": [str((directory / name).resolve()) for name in files]}
