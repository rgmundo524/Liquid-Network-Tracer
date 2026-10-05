"""Download the two CSV views of an immutable saved endpoint trace."""
import csv
import io
from pathlib import Path

from .common import TraceError, canonical, digest, read_json
from .investigations import read_case
from .networks import blockchain
from .services import apply_service_labels
from .transaction_csv import _text

CSV_NAMES = frozenset({"transactions.csv", "endpoints.csv"})


def csv_links(case_id, preview_id):
    from urllib.parse import quote
    base = "/api/cases/" + quote(case_id, safe="") + "/plot-exports/" + quote(preview_id, safe="") + "/"
    return [{"name": name, "url": base + name} for name in ("transactions.csv", "endpoints.csv")]


def _saved_source(case, preview_id):
    from .cli import verify_export
    from .plots import PREVIEW_ID, _ordinary, _snapshot
    from .pegouts import PREVIEW_ID as LEGACY_ID, saved_pegout_snapshot

    if not isinstance(preview_id, str):
        raise TraceError("Choose a saved endpoint trace")
    if PREVIEW_ID.fullmatch(preview_id):
        graph, _ = _snapshot(case, preview_id)
        report = graph["plot"]
        if report["goal"] != "pegouts":
            raise TraceError("Endpoint tables are available for saved endpoint traces")
        archive = _ordinary(case / "runs" / report["run_id"])
        # Check every manifest path before the shared archive verifier reads it.
        for line in _ordinary(archive / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            parts = line.split("  ", 1)
            if len(parts) == 2:
                _ordinary(archive / parts[1])
        verify_export(archive)
        state = read_json(_ordinary(archive / "trace.json"))
    elif LEGACY_ID.fullmatch(preview_id):
        graph, _, state = saved_pegout_snapshot(case, preview_id)
        report = graph["pegouts"]
        archive = _ordinary(case / "pegouts" / state["run_id"])
    else:
        raise TraceError("Choose a saved endpoint trace")
    metadata = read_case(case)
    if (not isinstance(state, dict) or state.get("case_id") != metadata["case_id"]
            or blockchain(state) != blockchain(graph) or blockchain(state) != blockchain(metadata)
            or state.get("run_id") != graph.get("run_id")
            or state.get("source") != graph.get("namespace", {}).get("source")
            or canonical(state.get("collection_source")) != canonical(report.get("collection_source"))
            or report.get("archive_sha256") != digest((archive / "SHA256SUMS").read_bytes())):
        raise TraceError("Saved endpoint trace does not match its source archive")
    controls = graph.get("service_controls", state.get("service_controls"))
    if not isinstance(controls, dict) or digest(canonical(controls)) != report.get("service_sha256"):
        raise TraceError("Saved endpoint trace is missing its original attribution rules")
    state["labels"] = apply_service_labels(state["labels"], controls)
    state["service_controls"] = controls
    names = {line.split("  ", 1)[1] for line in (archive / "SHA256SUMS").read_text(encoding="utf-8").splitlines()}
    observations = {}
    if "evidence-index.json" in names:
        rows = read_json(_ordinary(archive / "evidence-index.json"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int for row in rows):
            raise TraceError("Malformed saved observation index")
        observations = {row["id"]: row for row in rows}
        if len(observations) != len(rows):
            raise TraceError("Duplicate observation IDs in the saved index")
    return graph, state, observations


def build_plot_csv(case, preview_id, filename):
    """Read a sealed snapshot without recollecting, relayout, or Miro access."""
    from .plots import _locked, _ordinary
    from .pegout_csv import ENDPOINT_TABLE_FIELDS, endpoint_table_rows

    if filename not in CSV_NAMES:
        raise TraceError("Choose the transaction accounting or endpoint table CSV")
    case = _ordinary(Path(case))
    with _locked(case):
        graph, state, observations = _saved_source(case, preview_id)
        if filename == "transactions.csv":
            data = _ordinary(case / "previews" / preview_id / filename).read_bytes()
        else:
            rows = endpoint_table_rows(graph, state, observations=observations)
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=ENDPOINT_TABLE_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _text(value) for key, value in row.items()})
            data = stream.getvalue().encode("utf-8")
    return {"filename": filename, "content_type": "text/csv; charset=utf-8", "data": data}
