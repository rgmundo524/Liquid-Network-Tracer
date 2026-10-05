"""One auditable endpoint table for explicitly selected investigation snapshots."""
import csv
import io
from pathlib import Path
import re

from .common import TraceError, read_json
from .investigations import read_case
from .pegout_csv import ENDPOINT_TABLE_FIELDS, endpoint_table_rows
from .plot_csv import _saved_source
from .plots import GOALS, PREVIEW_ID, _locked, _ordinary
from .transaction_csv import _text

MAX_INVESTIGATIONS = 100
MAX_CSV_BYTES = 64 * 1024 * 1024
PROVENANCE_FIELDS = (
    "Investigation ID", "Investigation Name", "Run ID", "Plot ID", "Plot Created At",
    "Minimum Hops", "Maximum Hops", "Include Unspent", "Include Unspendable",
    "Data Source", "Shared Dataset ID", "Shared Run ID", "Include Attributed Stops",
)
FIELDS = (*ENDPOINT_TABLE_FIELDS, *PROVENANCE_FIELDS)


class _BoundedCsv(io.StringIO):
    def __init__(self):
        super().__init__(newline="")
        self.bytes_written = 0

    def write(self, value):
        self.bytes_written += len(value.encode("utf-8"))
        if self.bytes_written > MAX_CSV_BYTES:
            raise TraceError("Combined endpoint CSV exceeds 64 MiB; export fewer open investigations at once")
        return super().write(value)


def _selection(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_INVESTIGATIONS):
        raise TraceError("Choose between 1 and 100 open investigations to export")
    selected, seen = [], set()
    for item in value:
        if (not isinstance(item, dict) or set(item) - {"case_id", "run_id", "data_source", "dataset_id"}
                or not isinstance(item.get("case_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", item["case_id"])):
            raise TraceError("Endpoint export requires investigation IDs and saved collection selections only")
        mode = item.get("data_source", "investigation")
        if mode not in ("investigation", "shared"):
            raise TraceError("Choose investigation or shared data for each endpoint export")
        run = item.get("run_id")
        source = None
        if mode == "shared":
            source = _shared_source({"kind": "shared", "dataset_id": item.get("dataset_id"), "run_id": run})
        elif "dataset_id" in item:
            raise TraceError("A shared dataset ID requires the shared data source")
        elif (run is not None and run != "latest"
                and (not isinstance(run, str) or not re.fullmatch(r"[a-zA-Z0-9]{16}", run))):
            raise TraceError("Choose a saved run or latest for each investigation")
        if item["case_id"] in seen:
            raise TraceError("Choose each open investigation only once")
        seen.add(item["case_id"])
        selected.append((item["case_id"], run, source))
    return selected


def _shared_source(value):
    """Match one explicit shared revision while retaining richer saved provenance."""
    if (not isinstance(value, dict) or value.get("kind") != "shared"
            or not isinstance(value.get("dataset_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["dataset_id"])
            or not isinstance(value.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", value["run_id"])):
        raise TraceError("Shared endpoint export requires a dataset ID and a pinned shared run ID")
    return {key: value[key] for key in ("kind", "dataset_id", "run_id")}


def _pin_shared(case, metadata, source):
    """Select a completed case-owned plot, never fall back to private evidence."""
    info = {"case_id": metadata["case_id"], "name": metadata.get("name") or case.name,
            "run_id": source["run_id"], "collection_source": source}
    candidates = []
    from .seed_settings import normalize_seeds
    current_seeds = normalize_seeds(metadata["seeds"]) if metadata.get("seeds") else None
    different_seeds = False
    for directory in _ordinary(case / "previews").glob("*-plots-*"):
        if not PREVIEW_ID.fullmatch(directory.name):
            continue
        _ordinary(directory)
        if not _ordinary(directory / "SHA256SUMS").is_file():
            continue
        report = read_json(_ordinary(directory / "plot.json"))
        if not isinstance(report, dict):
            raise TraceError("A saved plot has invalid metadata; restore or regenerate it")
        selected_source = report.get("collection_source")
        if selected_source is None or _shared_source(selected_source) != source:
            continue
        run = report.get("run_id")
        if (report.get("case_id") != metadata["case_id"] or report.get("goal") not in GOALS
                or not isinstance(report.get("created_at"), str)
                or not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{16}", run)
                or not directory.name.startswith(run + "-plots-")):
            raise TraceError("A saved plot for the selected shared run has invalid metadata; restore or regenerate it")
        if report["goal"] == "pegouts":
            # This selection asks for the current investigation over a shared
            # snapshot. A historical preview is still downloadable explicitly,
            # but must not silently stand in for newly selected starting roots.
            if current_seeds is not None and normalize_seeds(report.get("query", {}).get("seeds", [])) != current_seeds:
                different_seeds = True
                continue
            candidates.append((report["created_at"], directory.name, run))
    if not candidates:
        if different_seeds:
            return info, None, "Saved endpoint previews use different starting outputs. Generate a preview for the current starting outputs before combining endpoints."
        return info, None, "No completed Paths to endpoints plot exists for the selected shared snapshot."
    _, preview_id, run = max(candidates)
    archive = _ordinary(case / "runs" / run)
    if not archive.is_dir() or not _ordinary(archive / "SHA256SUMS").is_file():
        raise TraceError("Selected shared plot's investigation evidence is unavailable; restore or regenerate it")
    return {**info, "run_id": run}, preview_id, None


def _pin(case, identity, requested_run, source=None):
    """Resolve one selection without silently falling back from damaged exports."""
    case = _ordinary(Path(case))
    with _locked(case):
        metadata = read_case(case)
        if metadata["case_id"] != identity:
            raise TraceError("Saved investigation identity changed; refresh the open investigations")
        name = metadata.get("name") or case.name
        if not isinstance(name, str):
            raise TraceError("Saved investigation name must be text")
        if source is not None:
            return _pin_shared(case, metadata, source)
        run = metadata.get("latest_run") if requested_run in (None, "latest") else requested_run
        info = {"case_id": identity, "name": name, "run_id": run}
        if run is None:
            return info, None, "No collected run is available."
        if not isinstance(run, str) or not re.fullmatch(r"[a-zA-Z0-9]{16}", run):
            raise TraceError("Saved investigation has an invalid latest run; select a saved run")
        archive = _ordinary(case / "runs" / run)
        if not archive.is_dir() or not _ordinary(archive / "SHA256SUMS").is_file():
            raise TraceError("Selected collected run is unavailable; refresh the investigation or restore it")
        previews = _ordinary(case / "previews")
        candidates = []
        for directory in previews.glob(run + "-plots-*"):
            if not PREVIEW_ID.fullmatch(directory.name):
                continue
            _ordinary(directory)
            # The manifest is atomically installed only once a plot is complete.
            if not _ordinary(directory / "SHA256SUMS").is_file():
                continue
            report = read_json(_ordinary(directory / "plot.json"))
            if (not isinstance(report, dict) or report.get("case_id") != identity
                    or report.get("run_id") != run or report.get("goal") not in GOALS
                    or not isinstance(report.get("created_at"), str)):
                raise TraceError("A saved plot for the selected run has invalid metadata; restore or regenerate it")
            if report["goal"] == "pegouts":
                candidates.append((report["created_at"], directory.name))
        if not candidates:
            return info, None, "No completed Paths to endpoints plot exists for the selected run."
        _, preview_id = max(candidates)
        return info, preview_id, None


def build_combined_endpoint_csv(investigations, resolve_case):
    """Pin every run and latest completed endpoint plot before rebuilding rows.

    A repeated outpoint in different investigations remains one row per case.
    The existing endpoint exporter validates archived evidence and attribution
    snapshots; current collection or attribution edits never rewrite this scope.
    """
    selections = _selection(investigations)
    # Resolve every client ID through the server's trusted investigation catalog.
    cases = [(identity, run, source, resolve_case(identity)[0]) for identity, run, source in selections]
    pinned, skipped = [], []
    for identity, run, source, case in cases:
        try:
            info, preview_id, reason = _pin(case, identity, run, source)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise TraceError(f"Saved endpoint export for investigation {identity} is unavailable; "
                             "restore its saved run and plot before exporting") from error
        if reason:
            skipped.append({**info, "reason": reason})
        else:
            pinned.append((case, info, preview_id))

    stream = _BoundedCsv()
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    included, total = [], 0
    for case, info, preview_id in pinned:
        try:
            # Every chosen artifact is immutable. Metadata saves and collectors
            # remain free to proceed throughout archive checks and path searches.
            graph, state, observations = _saved_source(case, preview_id)
            report = graph["plot"]
            source = _shared_source(report["collection_source"]) if "collection_source" in report else None
            if (report.get("run_id") != info["run_id"]
                    or info.get("collection_source") is not None and source != info["collection_source"]
                    or source is not None and _shared_source(state.get("collection_source")) != source):
                raise TraceError("Saved endpoint plot does not match the selected collection source")
            query = report["query"]
            rows = endpoint_table_rows(graph, state, observations=observations)
            provenance = {
                "Investigation ID": info["case_id"], "Investigation Name": info["name"],
                "Run ID": info["run_id"], "Plot ID": preview_id,
                "Plot Created At": report["created_at"], "Minimum Hops": query["min_hops"],
                "Maximum Hops": query["max_hops"],
                "Include Unspent": str(query.get("include_unspent", False)).lower(),
                "Include Unspendable": str(query.get("include_unspendable", False)).lower(),
                "Include Attributed Stops": str(query.get("include_attributed_stops", False)).lower(),
                "Data Source": "shared" if source else "investigation",
                "Shared Dataset ID": source["dataset_id"] if source else "",
                "Shared Run ID": source["run_id"] if source else "",
            }
            for row in rows:
                writer.writerow({key: _text(value) for key, value in {**row, **provenance}.items()})
            count = len(rows)
            # Release each archive before reading the next, including on success.
            del graph, state, observations, rows
        except TraceError as error:
            raise TraceError(f'Cannot export endpoints for investigation "{info["name"]}": {error}') from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise TraceError(f'Cannot export endpoints for investigation "{info["name"]}": '
                             "saved evidence or plot files are unavailable; restore or regenerate the plot") from error
        included.append({**info, "plot_id": preview_id, "endpoint_count": count})
        total += count
    return {"filename": "open-investigation-endpoints.csv", "content_type": "text/csv; charset=utf-8",
            "csv": stream.getvalue(), "included": included, "skipped": skipped, "endpoint_count": total}
