"""Small display-only investigation views, separate from evidence validation.

These summaries are not permission to publish or export. The existing selected
artifact and action handlers still validate their immutable source evidence.
"""
from copy import deepcopy

from .common import TraceError
from .investigations import validate_blockchain, validate_settings

RUN_FIELDS = {"id", "status", "stop_reason", "created_at", "transaction_count", "frontier_count",
              "seeds", "max_hops", "collected_hops", "hop_reference_name", "performance"}
SECTIONS = ("collection", "workflow", "boards", "history", "shared")


def _public_run(summary):
    return {key: deepcopy(value) for key, value in summary.items() if key in RUN_FIELDS}


def _run(case, metadata, run_id, *, schedule=True, priority=True):
    from .run_summaries import get_run_summary
    from .web import RUN_ID

    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return None, "unavailable"
    # get_run_summary rejects symlinks as unavailable without hiding metadata.
    archive = case / "runs" / run_id
    return get_run_summary(archive, metadata["case_id"], schedule=schedule, priority=priority)


def overview(case, metadata, *, detail=True):
    """Read case settings and one small cached summary; never open a trace."""
    seeds = list(metadata["seeds"]) if isinstance(metadata.get("seeds"), list) else []
    result = {"id": metadata["case_id"], "name": metadata.get("name") or case.name,
              "blockchain": validate_blockchain(metadata.get("blockchain", "liquid")),
              "created_at": metadata.get("created_at"), "latest_run": metadata.get("latest_run"),
              "fixture": bool(metadata.get("fixture")), "miro_board": metadata.get("miro_board"),
              "run_defaults": validate_settings(metadata.get("run_defaults", {})),
              "seed_count": len(seeds), "status": "Not started"}
    if detail:
        result.update(seeds=seeds, sections={key: "unloaded" for key in SECTIONS})
    latest = metadata.get("latest_run")
    if latest:
        summary, status = _run(case, metadata, latest, schedule=detail)
        if summary is not None and summary.get("collection_source", {}).get("kind") != "shared":
            item = _public_run(summary)
            if not detail:
                item.pop("seeds", None)
            result.update(latest=item, status=item.get("status"))
            if detail:
                result["runs"] = [item]
        elif status == "loading":
            result["status"] = "Loading saved run summary"
            if detail:
                result["runs"] = [{"id": latest, "status": "Loading saved run summary", "summary_pending": True}]
        else:
            result["status"] = "Saved run unavailable"
    return result


def collection(case, metadata, selected_run=None, *, history=False):
    """Enumerate only cached run summaries; unknown archives load separately."""
    from .web import RUN_ID, safe_path

    directory = safe_path(case, ["runs"])
    result = {"runs": [], "latest_run": metadata.get("latest_run"), "status": "Not started"}
    selected_run = selected_run or metadata.get("latest_run")
    if selected_run is not None and (not isinstance(selected_run, str) or not RUN_ID.fullmatch(selected_run)):
        raise TraceError("Choose a saved collection run")
    loading = False
    for archive in directory.iterdir() if directory.is_dir() else ():
        if not RUN_ID.fullmatch(archive.name) or archive.is_symlink() or not archive.is_dir():
            continue
        selected = archive.name == selected_run
        summary, status = _run(case, metadata, archive.name, schedule=history or selected, priority=selected)
        if summary is not None:
            if summary.get("collection_source", {}).get("kind") == "shared":
                continue
            item = _public_run(summary)
            result["runs"].append(item)
            if archive.name == metadata.get("latest_run"):
                result.update(latest=item, status=item.get("status"))
        elif status == "loading":
            loading = loading or history or selected
            result["runs"].append({"id": archive.name, "status": "Loading saved run summary", "summary_pending": True})
            if archive.name == metadata.get("latest_run"):
                result["status"] = "Loading saved run summary"
    result["runs"].sort(key=lambda run: (run.get("created_at") or "", run["id"]), reverse=True)
    if result["latest_run"] and "latest" not in result and result["status"] == "Not started":
        result["status"] = "Saved run unavailable"
    result["sections"] = {"collection": "loading" if loading else "ready"}
    return result


def shared_collection(root, case, metadata, selected_run=None):
    """Compatibility hints and cached shared runs, without reading evidence."""
    from .api import ENTERPRISE
    from .run_summaries import get_run_summary
    from .shared_collection import DIRECTORY, RUN_ID, _dataset_metadata, _safe

    result = {"name": "Shared collection", "compatible": True, "seeds": [], "seed_count": 0,
              "members": [], "runs": []}
    loading = False
    try:
        path = _safe(_safe(root) / DIRECTORY)
        if not (path / "case.json").exists():
            return {"shared_collection": result, "sections": {"shared": "ready"}}
        dataset = _dataset_metadata(path)
        result.update(dataset_id=dataset["case_id"], latest_run=dataset.get("latest_run"),
                      blockchain=dataset["blockchain"])
        selected_run = selected_run or dataset.get("latest_run")
        if selected_run is not None and (not isinstance(selected_run, str) or not RUN_ID.fullmatch(selected_run)):
            raise TraceError("Choose a saved shared collection run")
        if metadata.get("blockchain", "liquid") != dataset["blockchain"]:
            raise TraceError("Different blockchain")
        if metadata.get("latest_run"):
            selected, status = _run(case, metadata, metadata["latest_run"])
            if status == "loading":
                loading = True
                result.update(compatible=False, reason="Checking the selected run's shared collection compatibility.")
            elif selected is None or selected.get("source") != dataset["source"]:
                raise TraceError("Different collection source")
        elif metadata.get("fixture"):
            # Exact fixture contents are checked by the worker before use.
            if not dataset["source"].startswith("fixture://"):
                raise TraceError("Different collection source")
        elif dataset["source"] != ENTERPRISE:
            raise TraceError("Different collection source")
        for archive in sorted((path / "runs").glob("*"), reverse=True):
            if not RUN_ID.fullmatch(archive.name) or archive.is_symlink() or not archive.is_dir():
                continue
            selected = archive.name in (selected_run, dataset.get("latest_run"))
            item, status = get_run_summary(archive, dataset["case_id"], schedule=selected, priority=selected)
            if status == "loading":
                loading = loading or selected
                result["runs"].append({"id": archive.name, "status": "Loading saved run summary", "summary_pending": True})
                continue
            if item is None:
                continue
            provenance = item.get("shared_collection", {})
            if provenance.get("dataset_id") != dataset["case_id"]:
                continue
            run = _public_run(item)
            seeds = run.get("seeds", [])
            run.update(seed_count=len(seeds), policy_case_name=provenance.get("policy_case_name"))
            result["runs"].append(run)
            if archive.name == dataset.get("latest_run"):
                result.update(seeds=seeds[:], seed_count=len(seeds), latest=run,
                              members=[{key: member[key] for key in ("id", "name")} for member in provenance.get("members", [])],
                              policy_case_name=provenance.get("policy_case_name"))
        result["runs"].sort(key=lambda item: (item.get("created_at") or "", item["id"]), reverse=True)
        # Keep the selected/latest snapshot available even when history is capped.
        recent = result["runs"][:100]
        for item in result["runs"]:
            if item["id"] in (selected_run, dataset.get("latest_run")) and item not in recent:
                recent.append(item)
        result["runs"] = recent
        if dataset.get("latest_run") and "latest" not in result:
            if loading:
                result.update(compatible=False, reason="Loading shared collection summaries.")
            else:
                raise TraceError("Latest shared run unavailable")
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        result.update(compatible=False, reason="Shared collection is unavailable or uses a different blockchain or API source.")
    return {"shared_collection": result, "sections": {"shared": "loading" if loading else "ready"}}


def boards(case):
    from .workflow_api import case_boards
    from .cli import miro_recovery_status
    from .board_rebuild import rebuild_status

    result = case_boards(case)
    result["miro_recovery"] = miro_recovery_status(case)
    try:
        rebuild = rebuild_status(case)
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        rebuild = {"status": "unavailable", "notice": "The saved board rebuild receipt is unavailable. "
                   "Restore it before rebuilding again. Your saved investigation remains available."}
    result["miro_rebuild"] = ({key: value for key, value in rebuild.items()
                              if key in {"status", "previous_board_id", "board_id", "run_id", "name", "notice"}
                              and isinstance(value, str)} if rebuild else None)
    result["sections"] = {"boards": "ready"}
    return result
