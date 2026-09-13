"""Reviewed, resumable conversion of a managed Miro graph to shared addresses.

Only presentation identities change. Tracing archives are read and verified,
never rewritten. One existing circle is retained per full network/address pair;
connectors are redirected before redundant managed circles can be removed.
Normal sync is blocked while this journaled conversion is unfinished.
"""

import copy
import fcntl
import math
import os
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, urlencode

from .api import http
from .common import TraceError, canonical, digest, now, read_json, save_json
from .export import build_graph
from .investigations import read_case
from .miro import (_load_sync_state, _namespace, _response, _remote_url, _editable,
                   _fields, _get, _same, _SyncProgress)
from .miro_http import MiroHTTP
from .miro_reads import preflight
from .miro_requests import MiroRequests
from .miro_quota import SharedMiroQuota
from .miro_state import SyncState, load_state
from .layout import NODE_SIZE


VERSION = 1
NOTICE = ("Retains one existing circle per full address per network, reconnects managed edges, "
          "and removes redundant managed circles. UTXOs and archived runs are unchanged. "
          "Do not edit the board during conversion. Comments attached to removed circles are "
          "not exposed by the REST item snapshot; preserve those separately before approval.")


def _fingerprint(state):
    return digest(canonical({k: v for k, v in state.items() if k != "address_migration"}))


def _signed(value):
    return {**value, "sha256": digest(canonical(value))}


def _verified_plan(plan):
    if (not isinstance(plan, dict) or plan.get("schema_version") != VERSION
            or _signed({k: v for k, v in plan.items() if k != "sha256"}) != plan):
        raise TraceError("Invalid address conversion plan; preserve the Miro mapping and inspect its journal")
    return plan


def _paths(case, board):
    from .cli import board_id
    case = Path(case).resolve()
    metadata = read_case(case)
    target = board_id(board or metadata.get("miro_board") or "")
    path = case / "miro" / (digest(target.encode())[:24] + ".json")
    if any(p.is_symlink() for p in (path, path.parent, case / "miro" / "migrations")):
        raise TraceError("Address conversion requires ordinary local Miro mapping files")
    if not path.is_file():
        raise TraceError("This board has no saved mapping to convert; use ordinary Sync for a new board")
    return case, metadata, target, path


def _plan(case, state, target, metadata):
    from .cli import run_path, verify_export
    namespace = _namespace(state)
    if namespace["case_id"] != metadata["case_id"] or state.get("board_id") != target:
        raise TraceError("Address conversion mapping belongs to another case or board")
    if namespace["address_mode"] != "outpoint_occurrences":
        raise TraceError("This board already uses shared address objects; no conversion is required")
    if state.get("active_run_id") or any(state.get(k) for k in (
            "pending", "pending_creations", "pending_updates", "pending_deletions",
            "pending_frame_deletions", "pending_creation_detaches")):
        raise TraceError("Finish or reconcile the previous Miro sync before converting address objects")
    selected = state.get("latest_run_id")
    if not selected:
        raise TraceError("A completed Miro sync is required before address conversion")
    archive = run_path(case, selected)
    if any(p.is_symlink() for p in (case / "runs", archive)):
        raise TraceError("Address conversion requires ordinary archived run files")
    verify_export(archive)
    trace = read_json(archive / "trace.json")
    if (trace.get("case_id") != metadata["case_id"] or trace.get("run_id") != selected
            or trace.get("source") != namespace["source"]):
        raise TraceError("Address conversion archive does not match the mapped graph")
    old, new = (build_graph(trace, merge_addresses=merged, include_fees=True) for merged in (False, True))
    old_nodes = {n["id"]: n for n in old["nodes"]}
    old_edges = {e["id"]: e for e in old["edges"]}
    new_edges = {e["id"]: e for e in new["edges"]}
    items = state["items"]
    fee_ids = set(old["fee_items"])
    required = (set(old_nodes) | set(old_edges)) - fee_ids
    if not required <= items.keys():
        raise TraceError("The last synced graph is incomplete in the mapping; repair it before conversion")
    groups, aliases = defaultdict(list), {}
    for key, record in sorted(items.items()):
        if record["endpoint"] == "shapes" and key in old_nodes:
            node = old_nodes[key]
            if node["kind"] == "address":
                details = node["details"]
                addr = details["address"]
                canonical_key = (details["network"] + ":address:" + addr) if addr else key
                aliases[key] = canonical_key
                groups[canonical_key].append(key)
        elif record["endpoint"] == "connectors":
            edge = old_edges.get(key)
            if not edge or any(record.get(f) != edge[f] for f in ("source", "target")):
                raise TraceError("A mapped connector is not proven by the archived UTXO graph")
        elif record["endpoint"] == "shapes" and key != "legend" and not key.startswith("run:"):
            raise TraceError("An unrecognized managed shape prevents safe address conversion")
    retained = {new_key: min(keys) for new_key, keys in sorted(groups.items())}
    if any(new_key in items and new_key not in groups[new_key] for new_key in retained):
        raise TraceError("A canonical address key is already mapped to another object")
    removed = sorted(key for key, new_key in aliases.items() if key != retained[new_key])
    rewires = {}
    for key, record in sorted(items.items()):
        if record["endpoint"] != "connectors":
            continue
        edge = new_edges[key]
        logical = {f: aliases.get(record[f], record[f]) for f in ("source", "target")}
        if logical != {f: edge[f] for f in logical} or old_edges[key]["outpoint"] != edge["outpoint"]:
            raise TraceError("Address conversion would alter the underlying UTXO relationships")
        before = {f: items[record[f]]["id"] for f in logical}
        after = {f: items[retained.get(logical[f], logical[f])]["id"] for f in logical}
        rewires[key] = {"logical": logical, "before": before, "after": after}
    return _signed({"schema_version": VERSION, "case_id": metadata["case_id"], "board_id": target,
                    "run_id": selected, "namespace": namespace,
                    "archive_sha256": digest((archive / "SHA256SUMS").read_bytes()),
                    "mapping_sha256": _fingerprint(state), "aliases": aliases,
                    "retained": retained, "removed": removed, "rewires": rewires})


def preview_merge(case, board=None):
    """Read-only review; does not call Miro, ELK, Blockstream, or a secret provider."""
    case, metadata, target, path = _paths(case, board)
    state = load_state(path)
    pending = state.get("address_migration")
    if pending:
        plan = _verified_plan(pending.get("plan"))
        # Rebuild against the unchanged old mapping and immutable source. A
        # journal must not authorize arbitrary identifiers after local tampering.
        if _plan(case, state, target, metadata) != plan:
            raise TraceError("The pending conversion no longer matches its source; preserve its journal")
    else:
        _load_sync_state(path, target, _namespace(state))
        plan = _plan(case, state, target, metadata)
    return {"plan": plan, "approval_sha256": plan["sha256"], "board_id": target,
            "run_id": plan["run_id"], "address_objects_before": len(plan["aliases"]),
            "address_objects_after": len(plan["retained"]), "duplicates_to_remove": len(plan["removed"]),
            "connectors_to_redirect": sum(e["before"] != e["after"] for e in plan["rewires"].values()),
            "resume": bool(pending), "notice": NOTICE, "remote_preflight_required": True}


def _inventory(requests, base, headers):
    """Read the ENTIRE board connector inventory, including unmanaged edges.

    Never use the ordinary sync's early-stop pagination for deletion safety.
    https://developers.miro.com/reference/get-connectors-1
    """
    cursor, cursors, result = None, set(), {}
    for _ in range(10000):
        query = {"limit": 50, **({"cursor": cursor} if cursor else {})}
        status, _, raw = requests.request("GET", base + "/connectors?" + urlencode(query), headers)
        if status != 200:
            raise TraceError("Cannot verify the complete Miro connector inventory; conversion stopped")
        page = _response(raw, "address conversion connector inventory")
        data, cursor = page.get("data"), page.get("cursor")
        if (not isinstance(data, list) or len(data) > 50
                or (cursor is not None and not isinstance(cursor, str))):
            raise TraceError("Malformed Miro connector inventory; conversion stopped")
        for item in data:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                    or item["id"] in result or item.get("isSupported") is False):
                raise TraceError("Incomplete or changing Miro connector inventory; conversion stopped")
            result[item["id"]] = item
        if not cursor:
            if "total" in page and page["total"] != len(result):
                raise TraceError("Miro connector inventory did not reconcile; conversion stopped")
            return result
        if cursor in cursors or not data:
            raise TraceError("Miro connector pagination did not complete; conversion stopped")
        cursors.add(cursor)
    raise TraceError("Miro connector inventory exceeded its safety budget; no partial inventory is accepted")


def _ends(body):
    if any(body.get(field) is not None and not isinstance(body[field], dict)
           for field in ("startItem", "endItem")):
        raise TraceError("Malformed connector endpoints; conversion stopped")
    return {logical: (body.get(field) or {}).get("id")
            for field, logical in (("startItem", "source"), ("endItem", "target"))}


def _check_inventory(inventory, plan, state, *, detached=False):
    removed_ids = {state["items"][key]["id"] for key in plan["removed"]}
    mapped = {record["id"]: key for key, record in state["items"].items()
              if record["endpoint"] == "connectors"}
    if not mapped.keys() <= inventory.keys():
        raise TraceError("Mapped connectors are missing from the full board inventory; conversion stopped")
    for item_id, body in inventory.items():
        ends = _ends(body)
        if removed_ids.intersection(ends.values()):
            if detached or item_id not in mapped:
                raise TraceError("A connector still references a redundant circle; no circles were removed. "
                                 "Preserve or relocate unmanaged connectors before retrying")
        if item_id in mapped:
            entry = plan["rewires"][mapped[item_id]]
            attempted = state.get("address_migration", {}).get("patches", {}).get(mapped[item_id])
            if any(ends[f] not in ({entry["before"][f], entry["after"][f]} if attempted
                                  else {entry["before"][f]}) for f in ends):
                raise TraceError("A connector was manually reattached; restore or review it before conversion")


def _check_duplicate(key, body, record):
    # Positions may differ: the reviewer explicitly chose to retire this circle.
    # Edited content/styles, resizing, rotation, or grouping are not discarded.
    if body.get("type") != "shape" or body.get("data", {}).get("shape") != "circle":
        raise TraceError("A redundant address object is no longer a circle; conversion stopped")
    if (body.get("parent") or {}).get("id"):
        raise TraceError("Move redundant address circles out of frames/groups before conversion")
    actual = _editable(body, "shapes")
    if any(not _same(_get(actual, p), v, p) for p, v in _fields(record["managed"])):
        raise TraceError("A redundant address circle has manual text/style edits; preserve them before conversion: " + key)
    geometry = body.get("geometry", {})
    try:
        if any(not math.isfinite(float(geometry[k])) or float(geometry[k]) != NODE_SIZE for k in ("width", "height")):
            raise ValueError
        if float(geometry.get("rotation", 0)) % 360:
            raise ValueError
    except (TypeError, ValueError, KeyError):
        raise TraceError("A redundant address circle was resized/rotated; preserve its changes before conversion") from None


def apply_merge(case, approval, board=None, *, token=None, transport=http, interval=.02, progress=None):
    """Apply only the approved plan; reruns reconcile PATCH/DELETE outcomes.

    Miro does not offer a cross-item transaction or conditional item deletion.
    Stop concurrent board edits. Duplicate comments cannot be captured by the
    REST item API. Both limitations are shown before approval.
    """
    report = preview_merge(case, board)
    if approval != report["approval_sha256"]:
        raise TraceError("Review the current conversion preview and approve its exact SHA-256 before applying")
    case, metadata, target, path = _paths(case, board)
    status_progress = _SyncProgress(progress)
    with (case / "trace.lock").open("a") as trace_lock, path.with_suffix(".lock").open("a") as lock, ExitStack() as resources:
        for handle in (trace_lock, lock):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TraceError("A trace or Miro sync is active; convert addresses after it finishes") from None
        report = preview_merge(case, target)
        if report["approval_sha256"] != approval:
            raise TraceError("The mapping changed after review; create a new conversion preview")
        plan, state = report["plan"], load_state(path)
        token = token or os.environ.get("MIRO_ACCESS_TOKEN")
        if not token:
            raise TraceError("Set MIRO_ACCESS_TOKEN locally for address conversion")
        base = "https://api.miro.com/v2/boards/" + quote(target, safe="")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        quota = None
        if transport is http:
            quota = resources.enter_context(SharedMiroQuota(token))
            transport = resources.enter_context(MiroHTTP())
        requests = resources.enter_context(MiroRequests(transport, interval=interval, workers=1,
                                                       progress=status_progress, quota=quota))
        pending = state.get("address_migration", {})
        # Existing read preflight accepts absence only after this operation has
        # durably journaled a DELETE attempt for that exact mapped shape.
        read_state = {**state, "pending_deletions": {
            key: {"attempted": True} for key in pending.get("deletions", {})}}
        remote = preflight(requests, base, headers, read_state, {k: True for k in plan["removed"]}, status_progress)
        inventory = _inventory(requests, base, headers)
        _check_inventory(inventory, plan, state)
        for key in plan["removed"]:
            if key in remote:
                _check_duplicate(key, remote[key], state["items"][key])
        # Save original accessible item bodies and mapping before any board write.
        directory = case / "miro" / "migrations"
        directory.mkdir(exist_ok=True)
        backup = directory / (approval + ".before.json")
        if backup.is_symlink():
            raise TraceError("Address conversion backup cannot be a symbolic link")
        if pending:
            if not backup.is_file() or digest(backup.read_bytes()) != pending.get("backup_sha256"):
                raise TraceError("Address conversion backup is missing or changed; preserve the journal")
        else:
            if backup.exists():
                previous = read_json(backup)
                if previous.get("plan") != plan or previous.get("mapping") != state:
                    raise TraceError("An incompatible backup occupies this conversion ID; inspect it before retrying")
            else:
                save_json(backup, {"schema_version": VERSION, "plan": plan, "mapping": state,
                                   "remote": remote, "recorded_at": now()})
        journal = resources.enter_context(SyncState(path, state))
        if not pending:
            journal.commit(sets=[(("address_migration",), {"plan": plan,
                "backup_sha256": digest(backup.read_bytes()), "patches": {}, "deletions": {}})])
        done = 0
        for key, entry in plan["rewires"].items():
            if entry["before"] == entry["after"]:
                continue
            record = state["items"][key]
            # Read again immediately before an edit; never replay a lost PATCH
            # if its new endpoints are already present, or overwrite a third ID.
            status, _, raw = requests.request("GET", _remote_url(base, record), headers)
            if status != 200:
                raise TraceError("Cannot recheck connector before conversion; resume this same plan")
            current = _response(raw, "address conversion connector")
            if current.get("id") != record["id"]:
                raise TraceError("Wrong connector returned; conversion stopped")
            ends = _ends(current)
            attempted = state["address_migration"]["patches"].get(key)
            allowed = ({f: {entry["before"][f], entry["after"][f]} for f in ends} if attempted
                       else {f: {entry["before"][f]} for f in ends})
            if any(ends[f] not in allowed[f] for f in ends):
                raise TraceError("A connector changed during conversion; restore it before resuming")
            if ends != entry["after"]:
                patch = {}
                for field, logical in (("startItem", "source"), ("endItem", "target")):
                    if ends[logical] != entry["after"][logical]:
                        original = current[field]
                        attach = ({"position": original["position"]} if "position" in original
                                  else {"snapTo": original.get("snapTo", "auto")})
                        patch[field] = {"id": entry["after"][logical], **attach}
                journal.commit(sets=[(("address_migration", "patches", key), "attempted")])
                status, _, raw = requests.request("PATCH", _remote_url(base, record), headers, patch)
                if status != 200:
                    raise TraceError("Connector conversion was not acknowledged; resume this same plan")
                response = _response(raw, "address conversion PATCH")
                if response.get("id") != record["id"] or _ends(response) != entry["after"]:
                    raise TraceError("Connector conversion returned unexpected endpoints; resume this same plan")
            journal.commit(sets=[(("address_migration", "patches", key), "done")])
            done += 1
            status_progress.emit("updating", done, report["connectors_to_redirect"], "Connecting shared address objects")
        # No destructive operation is permitted while ANY board connector still
        # references a redundant circle, even a connector outside our mapping.
        inventory = _inventory(requests, base, headers)
        _check_inventory(inventory, plan, state, detached=True)
        for number, key in enumerate(plan["removed"], 1):
            record = state["items"][key]
            status, _, raw = requests.request("GET", _remote_url(base, record), headers)
            if status == 404 and key in state["address_migration"]["deletions"]:
                pass  # Reconcile only a previously journaled DELETE.
            elif status == 200:
                current = _response(raw, "address conversion shape")
                if current.get("id") != record["id"]:
                    raise TraceError("Wrong address shape returned; conversion stopped")
                _check_duplicate(key, current, record)
                journal.commit(sets=[(("address_migration", "deletions", key), "attempted")])
                # Shape deletion is Level 3, unlike connector PATCH (Level 2).
                # https://developers.miro.com/reference/delete-shape-item-1
                status, _, _ = requests.request("DELETE", _remote_url(base, record), headers, credits=500)
                if status != 204:
                    raise TraceError("Address deletion was not acknowledged; resume this same plan")
            else:
                raise TraceError("Cannot recheck a redundant address circle; conversion stopped")
            journal.commit(sets=[(("address_migration", "deletions", key), "done")])
            status_progress.emit("removing", number, len(plan["removed"]), "Removing verified redundant address circles")
        items = {}
        removed = set(plan["removed"])
        for key, record in state["items"].items():
            if key in removed:
                continue
            record = copy.deepcopy(record)
            if key in plan["rewires"]:
                record.update(plan["rewires"][key]["logical"])
            items[plan["aliases"].get(key, key)] = record
        history = [*state.get("address_migration_history", []), {
            "plan_sha256": approval, "backup": str(backup.relative_to(case)),
            "backup_sha256": state["address_migration"]["backup_sha256"],
            "run_id": plan["run_id"], "completed_at": now(), "removed": len(removed)}]
        # One atomic journal operation switches all logical endpoints and the
        # namespace. Until here normal sync sees the migration blocker.
        journal.commit(sets=[(("items",), items),
                             (("namespace",), {**plan["namespace"], "address_mode": "merged"}),
                             (("address_migration_history",), history)], deletes=[("address_migration",)])
        status_progress.emit("complete", 1, 1, "Address conversion complete; sync to refresh labels and frames")
    return {k: v for k, v in {**report, "converted": True, "backup": str(backup),
            "sync_required": True, "notice": "Converted. Sync to Miro to refresh labels and frames; "
            "choose Sync and reorganize to apply a fresh shared-address layout."}.items() if k != "plan"}
