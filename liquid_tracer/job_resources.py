"""Admission scopes for jobs whose immutable inputs support overlap.

This is UI-server admission only. Engine file locks still protect collectors
and board writers across separate servers and CLI invocations.
"""

from pathlib import Path

PARALLEL_ACTIONS = frozenset({"trace", "shared-trace", "address-counts", "plot", "plot-sync",
                              "board-create", "board-create-sync", "board-link", "board-sync"})
VALUE_OPTIONS = frozenset({"--case", "--run", "--resume", "--goal", "--min-hops", "--max-hops",
                           "--layout-mode", "--board-record-id", "--record", "--preview", "--board",
                           "--name", "--max-items", "--layout-settings-json", "--seed", "--seeds-file",
                           "--hops", "--additional-hops", "--hop-reference-name", "--fixture",
                           "--max-transactions", "--max-outpoints", "--max-requests", "--max-seconds",
                           "--request", "--data-source", "--dataset-id", "--members-json"})


def option(arguments, flag):
    # Values such as board names may themselves resemble option names. Skip
    # each validated value instead of searching the raw argument list.
    index = 1
    while index < len(arguments):
        candidate = arguments[index]
        if candidate in VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                return None
            if candidate == flag:
                return arguments[index + 1]
            index += 2
        else:
            index += 1
    return None


def job_resources(arguments, action, case=None):
    """Derive trusted resource identities from validated CLI arguments."""
    case = Path(case) if case is not None else None
    resource = {"resource_kind": "exclusive"}
    source = option(arguments, "--run") or option(arguments, "--resume")
    if source and source != "latest":
        resource["source_run_id"] = source
    preview_id = option(arguments, "--preview")
    if action in ("board-sync", "board-create-sync") and preview_id and case is not None:
        from .common import read_json
        report = read_json(case / "previews" / preview_id / "plot.json")
        if type(report.get("input_snapshot_version")) is not int or report["input_snapshot_version"] != 1:
            resource["source_run_id"] = preview_id[:16]
            return resource
    if action == "shared-trace":
        from .common import digest
        if case is None:
            return resource
        resource.update(resource_kind="shared_collection",
                        resource_key=digest(str(case.parent.resolve()).encode("utf-8")))
    elif action in ("trace", "address-counts"):
        resource["resource_kind"] = "collection"
    elif action == "plot" and option(arguments, "--layout-mode") != "update":
        resource["resource_kind"] = "plot"
    elif action == "plot-sync" and option(arguments, "--layout-mode") != "update":
        # A new board cannot target an existing mapping. Avoid parsing every
        # saved board's item state just to reserve this known resource.
        resource.update(resource_kind="board", resource_key="new-board")
    elif action in PARALLEL_ACTIONS:
        from .investigation_boards import list_boards

        records = list_boards(case) if case is not None else []
        record_id = option(arguments, "--record") or option(arguments, "--board-record-id")
        record = next((item for item in records if record_id and item["id"] == record_id), None)
        if record is None and action == "board-create-sync":
            record = next((item for item in records if preview_id
                           and item.get("creation_preview_id") == preview_id), None)
        target = (record or {}).get("board_id") or option(arguments, "--board")
        resource.update(resource_kind="board", resource_key=target or "new-board")
        if not source and preview_id:
            resource["source_run_id"] = preview_id[:16]
    return resource


def conflicts(first, second):
    """Unknown/legacy operations retain the original exclusive case scope."""
    same_case = first.get("case_id") is not None and first.get("case_id") == second.get("case_id")
    left, right = first.get("resource_kind", "exclusive"), second.get("resource_kind", "exclusive")
    if "shared_collection" in (left, right):
        return (left == right and first.get("resource_key") is not None
                and first.get("resource_key") == second.get("resource_key"))
    if same_case and ("exclusive" in (left, right) or left == right == "collection"):
        return True
    if left == right == "board":
        key = first.get("resource_key")
        return bool(key and key == second.get("resource_key") and (key != "new-board" or same_case))
    return False
