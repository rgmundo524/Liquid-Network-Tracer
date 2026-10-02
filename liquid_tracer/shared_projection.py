"""Immutable investigation-owned projections of a sealed shared collection.

The source's acquisition policy never becomes the recipient's presentation
policy. Exact saved spend evidence selects descendants; complete transaction
payloads and observation identities are preserved within this source revision.
"""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import shutil
import tempfile

from .common import TraceError, canonical, digest, output_kind, parse_outpoint, read_json, save_json
from .group_hops import normalize_reference_name
from .investigations import read_case
from .progress import report_progress


def _project(state, seeds, case_id, hop_reference_name):
    from .connections import _saved_connection_evidence
    from .trace import COLLECTION_POLICY

    result = _saved_connection_evidence(state)
    transactions, outputs, links = result["transactions"], result["outputs"], result["links"]
    from .layout import transaction_ranks
    if transaction_ranks(transactions)[1]:
        raise TraceError("Shared saved spend evidence contains a cycle")
    for key in seeds:
        txid, index = parse_outpoint(key)
        if txid not in transactions:
            raise TraceError("Shared collection is missing a selected starting transaction; collect shared data for these seeds first")
        if index >= len(transactions[txid]["data"]["vout"]):
            raise TraceError("A selected starting output does not exist in the shared transaction evidence")

    depths, tx_depths, selected_links = {}, {}, {}
    pending = deque((key, 0) for key in seeds)
    while pending:
        key, depth = pending.popleft()
        if depth >= depths.get(key, float("inf")):
            continue
        txid, index = parse_outpoint(key)
        depths[key] = depth
        tx_depths[txid] = min(depth, tx_depths.get(txid, depth))
        if key not in links:
            continue
        link = links[key]
        selected_links[key] = deepcopy(link)
        child = link["spending_txid"]
        pending.extend((f"{child}:{number}", depth + 1)
                       for number in range(len(transactions[child]["data"]["vout"])))

    selected_outputs = {}
    for key, depth in depths.items():
        txid, index = parse_outpoint(key)
        item = deepcopy(outputs.get(key, {"outpoint": key, "txid": txid, "vout": index,
                                          "status": "not_observed", "origin": "saved_transaction_input"}))
        if (item.get("outpoint", key) != key or item.get("txid") != txid
                or type(item.get("vout")) is not int or item["vout"] != index):
            raise TraceError("Shared output bookkeeping disagrees with its exact saved output")
        control = item.pop("trace_control", None)
        if isinstance(control, dict) and item.get("status") in {
                "suspected_service_stop", "analyst_stop", "held_behind_service",
                "attribution_hop_limit", "named_group_hop_limit"}:
            item["status"] = control.get("previous_status", "not_observed")
        for name in ("labels", "trace_scope_depth", "reference_hops", "seed_depth"):
            item.pop(name, None)
        if item.get("status") in {"suspected_service_stop", "analyst_stop", "held_behind_service",
                                  "attribution_hop_limit", "named_group_hop_limit"}:
            item["status"] = "not_observed"
        kind = output_kind(transactions[txid]["data"]["vout"][index])
        if kind != "spendable":
            item["status"] = kind
        elif key in selected_links and item.get("status") != "spent":
            item["status"] = "spent_in_saved_evidence"
        item.update(outpoint=key, depth=depth)
        if key in seeds:
            item["origin"] = "analyst_seed"
        selected_outputs[key] = item

    result["transactions"] = {key: record for key, record in transactions.items() if key in tx_depths}
    for key, record in result["transactions"].items():
        record["depth"] = tx_depths[key]
        record.pop("reference_hops", None)
    for key, link in selected_links.items():
        link["hop"] = depths[key] + 1
    result.update(case_id=case_id, seeds=seeds, outputs=selected_outputs, links=selected_links,
                  labels=[], collection_policy=dict(COLLECTION_POLICY), parent_run=None, ancestor_runs=[])
    for name in ("service_controls", "shared_collection", "selected_frontier", "graph_options", "performance"):
        result.pop(name, None)
    if hop_reference_name:
        result["hop_reference_name"] = hop_reference_name
    else:
        result.pop("hop_reference_name", None)
    from .saved_inputs import input_evidence, saved_input_output
    input_transactions = input_evidence(result)
    addresses = {output.get("scriptpubkey_address") for record in result["transactions"].values()
                 for output in [*record["data"]["vout"],
                                *(saved_input_output(input_transactions, vin) for vin in record["data"]["vin"])]}
    result["address_tx_counts"] = {key: value for key, value in result.get("address_tx_counts", {}).items()
                                   if key in addresses}
    result["stats"] = {"requests_this_run": 0, "outpoints_examined_this_run": 0,
                       "new_transactions_this_run": 0, "transactions_cumulative": len(tx_depths),
                       "outputs_cumulative": len(depths),
                       "frontier_count": sum(key not in selected_links and
                                             output_kind(transactions[item["txid"]]["data"]["vout"][item["vout"]]) == "spendable"
                                             for key, item in selected_outputs.items())}
    return result


def _copy_observations(state, archive, destination, manifest_names, *, snapshot=None):
    """Copy only referenced observations, keeping IDs in their source namespace."""
    from .plots import _ordinary

    if "evidence-index.json" not in manifest_names:
        raise TraceError("Shared collection has no sealed observation index; collect shared data again")
    if snapshot is None:
        rows = read_json(_ordinary(archive / "evidence-index.json"))
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int
                                            or row["id"] <= 0 for row in rows)):
            raise TraceError("Shared collection has an invalid observation index")
        index = {row["id"]: row for row in rows}
        if len(index) != len(rows):
            raise TraceError("Shared collection has duplicate observation identities")
    else:
        index = {}
    referenced = set()
    expectations = {}
    evidence = {**state.get("saved_transactions", {}), **state["transactions"]}
    for txid, record in evidence.items():
        oid = record.get("observation_id")
        if type(oid) is not int or oid <= 0 or oid in expectations:
            raise TraceError("Shared transactions require distinct original observation identities")
        expectations[oid] = ("/tx/" + txid, record["data"])
        referenced.add(oid)
    for item in state["outputs"].values():
        oid = item.get("spend_observation_id")
        if oid is not None:
            referenced.add(oid)
    for link in state["links"].values():
        referenced.update(link[name] for name in ("observation_id", "spending_tx_observation_id")
                          if link.get(name) is not None)
    if snapshot is not None:
        index = {oid: snapshot.observation(oid) for oid in referenced if type(oid) is int and oid > 0}
    if any(type(oid) is not int or oid <= 0 or not index.get(oid) for oid in referenced):
        raise TraceError("Shared projection requires every referenced original observation")
    copied, payloads = [], {}
    for oid in sorted(referenced):
        row = index[oid]
        filename = row.get("file")
        if (not isinstance(filename, str) or filename not in manifest_names
                or Path(filename).parts[:1] != ("evidence",)
                or row.get("source") != state["source"] or row.get("status") != 200):
            raise TraceError("Shared observation disagrees with its sealed source archive")
        source = _ordinary(archive / filename)
        raw = source.read_bytes()
        if digest(raw) != row.get("sha256"):
            raise TraceError("Shared observation checksum disagrees with its index")
        data = json.loads(raw)
        if oid in expectations:
            endpoint, expected = expectations[oid]
            if row.get("endpoint") != endpoint or canonical(data) != canonical(expected):
                raise TraceError("Shared transaction disagrees with its original observation")
        payloads[oid] = data
        output_file = f"evidence/{oid:08d}.response"
        target = destination / output_file
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
        copied.append({**row, "file": output_file})
    for item in state["outputs"].values():
        oid = item.get("spend_observation_id")
        if oid is None:
            continue
        rows = payloads[oid]
        if (index[oid].get("endpoint") != "/tx/" + item["txid"] + "/outspends"
                or not isinstance(rows, list) or item["vout"] >= len(rows)
                or canonical(rows[item["vout"]]) != canonical(item.get("observed_spend"))):
            raise TraceError("Shared outspend disagrees with its original observation")
    state["observations"] = sorted(referenced)
    save_json(destination / "evidence-index.json", copied)


@contextmanager
def _shared_index(case, metadata, run_id, dataset_id, progress):
    """Validate source compatibility without reloading a saved private trace."""
    from .api import ENTERPRISE
    from .cli import run_path
    from .shared_collection import pin_shared_run, _dataset_metadata, _safe
    from .snapshot_index import open_snapshot_index, verified_source_identity

    selected, dataset = pin_shared_run(case, run_id, dataset_id)
    shared = _dataset_metadata(dataset)
    fixture = metadata.get("fixture")
    configured = "fixture://" + digest(canonical(read_json(_safe(fixture)))) if fixture else None
    source = configured or ENTERPRISE
    if metadata.get("latest_run"):
        private_run = metadata["latest_run"]
        private = verified_source_identity(run_path(case, private_run), case_id=metadata["case_id"],
                                           run_id=private_run, progress=progress)
        source = private.get("source")
        if not isinstance(source, str) or not source:
            raise TraceError("Saved collection has no API source identity")
        if configured and source != configured:
            raise TraceError("The fixture no longer matches the investigation's saved API source")
        if source.startswith("fixture://") and not configured:
            raise TraceError("The investigation's original fixture is required for shared collection")
    if metadata["blockchain"] != shared["blockchain"] or source != shared["source"]:
        raise TraceError("Shared collection requires the same blockchain and API source or identical fixture")
    with open_snapshot_index(run_path(dataset, selected), case_id=shared["case_id"],
                             run_id=selected, source=shared["source"], progress=progress) as snapshot:
        if snapshot.metadata.get("shared_collection", {}).get("dataset_id") != shared["case_id"]:
            raise TraceError("Shared run does not match the dataset identity")
        yield snapshot


def materialize_shared_run(case, run_id="latest", dataset_id=None, *, progress=None,
                           max_hops=None, connection_scope=None):
    """Return a sealed local source run; never update private latest/history."""
    return materialize_shared_source(case, run_id, dataset_id, progress=progress,
                                     max_hops=max_hops, connection_scope=connection_scope)[0]


def materialize_shared_source(case, run_id="latest", dataset_id=None, *, progress=None,
                              max_hops=None, connection_scope=None):
    """Return (run ID, verified state, manifest digest) for reuse within a job."""
    from .plots import _locked, _ordinary
    case = _ordinary(Path(case))
    with _locked(case):
        metadata = read_case(case)
        raw_seeds = metadata.get("seeds")
        if not isinstance(raw_seeds, list) or not raw_seeds:
            raise TraceError("Choose this investigation's starting outputs before plotting shared data")
        seeds = sorted({f"{txid}:{index}" for txid, index in map(parse_outpoint, raw_seeds)})
        reference = normalize_reference_name(metadata.get("run_defaults", {}).get("hop_reference_name", ""))
    # Named-group distance can reset far beyond an ordinary seed-hop limit.
    if reference and connection_scope is None:
        max_hops = None
    with _shared_index(case, metadata, run_id, dataset_id, progress) as snapshot:
        return _materialize(case, metadata, seeds, reference, snapshot, max_hops, connection_scope, progress)


def _materialize(case, metadata, seeds, reference, snapshot, max_hops, connection_scope, progress):
    from .cli import verify_export
    from .export import build_graph
    from .miro import make_plan
    from .plots import _ordinary
    from .snapshot_query import select_snapshot

    archive, names = snapshot.archive, snapshot.manifest_names
    original = snapshot.metadata
    provenance = original["shared_collection"]
    identity = {"schema_version": 2, "case_id": metadata["case_id"], "dataset_id": original["case_id"],
                "run_id": original["run_id"], "archive_sha256": snapshot.manifest_sha256, "seeds": seeds,
                "hop_reference_name": reference, "max_hops": max_hops, "connection_scope": connection_scope}
    fingerprint = digest(canonical(identity))
    derived_id = fingerprint[:16]
    target = _ordinary(case / "runs" / derived_id)
    locks = _ordinary(case / "shared-projections")
    locks.mkdir(exist_ok=True)
    with _ordinary(locks / (derived_id + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            for line in _ordinary(target / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                parts = line.split("  ", 1)
                if len(parts) == 2:
                    _ordinary(target / parts[1])
            verify_export(target, progress=progress)
            report_progress(progress, "loading_collection", 0, 1)
            saved = read_json(_ordinary(target / "trace.json"))
            report_progress(progress, "loading_collection", 1, 1)
            if (saved.get("projection_sha256") != fingerprint or saved.get("run_id") != derived_id
                    or saved.get("case_id") != metadata["case_id"] or saved.get("seeds") != seeds
                    or saved.get("hop_reference_name", "") != reference
                    or any(saved.get("collection_source", {}).get(key) != identity[key]
                           for key in ("dataset_id", "run_id", "archive_sha256"))):
                raise TraceError("Shared projection identity conflicts with an existing saved run")
            from .run_summaries import remember_run_summary
            remember_run_summary(target, saved)
            return derived_id, saved, digest((target / "SHA256SUMS").read_bytes())
        source = select_snapshot(snapshot, seeds, max_hops=max_hops, connection_scope=connection_scope, progress=progress)
        report_progress(progress, "projecting_collection", 0, 1)
        state = _project(source, seeds, metadata["case_id"], reference)
        state.pop("_snapshot_collected_depth", None)
        state.update(run_id=derived_id, root_run_id=derived_id, projection_sha256=fingerprint,
                     investigation={"case_id": metadata["case_id"], "name": metadata.get("name"), "miro_board": None})
        state["collection_source"] = {
            "schema_version": 1, "kind": "shared", "dataset_id": source["case_id"], "run_id": source["run_id"],
            "archive_sha256": identity["archive_sha256"], "seeds": original["seeds"],
            "selection": {"max_hops": max_hops, "connection_scope": connection_scope},
            "projection_seeds": seeds, "hop_reference_name": reference,
            "source_hop_reference_name": source.get("hop_reference_name", ""),
            "source_max_hops": source.get("limits", {}).get("max_hops"),
            "source_run_status": source.get("status"), "source_stop_reason": source.get("stop_reason"),
            "source_collected_depth": original["_snapshot_collected_depth"],
            "member_count": len(provenance.get("members", [])), "members": deepcopy(provenance.get("members", [])),
            "policy_case_id": provenance.get("policy_case_id"), "policy_case_name": provenance.get("policy_case_name"),
            "collection_policy": deepcopy(source.get("collection_policy", {})),
            "policy_settings": deepcopy(provenance.get("settings", {})), "policy_sha256": digest(canonical(provenance)),
        }
        target.parent.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".shared-projection-", dir=target.parent))
        try:
            _copy_observations(state, archive, temporary, names, snapshot=snapshot)
            save_json(temporary / "trace.json", state)
            display = deepcopy(state)
            # The neutral evidence archive displays ordinary seed distances.
            # Plotting applies the recipient's current named-group definitions.
            display.pop("hop_reference_name", None)
            graph = build_graph(display, resolve_saved_inputs=True)
            save_json(temporary / "graph.json", graph)
            save_json(temporary / "miro-plan.json", make_plan(graph))
            files = sorted(path for path in temporary.rglob("*") if path.is_file())
            (temporary / "SHA256SUMS").write_text("".join(
                digest(path.read_bytes()) + "  " + str(path.relative_to(temporary)) + "\n" for path in files), encoding="utf-8")
            verify_export(temporary, progress=progress)
            temporary.rename(target)
            from .run_summaries import remember_run_summary
            remember_run_summary(target, state)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return derived_id, state, digest((target / "SHA256SUMS").read_bytes())
