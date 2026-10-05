"""Offline publication of a finished trace interrupted in its ancillary phase.

Recovery never resumes traversal, fetches counts, changes trace completion, or
edits an already sealed archive. It verifies the checkpoint against its local
response evidence, overlays durable counts, and makes that snapshot resumable.
"""
from contextlib import ExitStack
from datetime import datetime
from functools import lru_cache
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .networks import blockchain
from .common import HEX64, TraceError, canonical, digest, now, output_kind, parse_outpoint, read_json, save_json
from .investigations import read_case
from .trace import TERMINAL, validate_transaction

RUN_ID = re.compile(r"[0-9a-f]{16}\Z")


def _ordinary(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise TraceError("Collection recovery paths cannot contain symbolic links")
    return path


def _lock(stack, path):
    handle = stack.enter_context(_ordinary(path).open("a"))
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise TraceError("Collection or count lookup is still active; wait for its worker to exit before recovery") from None


def _finished(state):
    if not isinstance(state, dict) or state.get("status") not in {"bounded_complete", "paused", "error"}:
        return False
    try:
        start = datetime.fromisoformat(state["started_at"])
        finish = datetime.fromisoformat(state["finished_at"])
        return start.tzinfo is not None and finish.tzinfo is not None and finish >= start
    except (KeyError, ValueError, TypeError):
        return False


def _checkpoint(archive):
    # A previous interrupted installation always retains the original bytes.
    backup = _ordinary(archive / "recovery-original-trace.json")
    return backup if backup.is_file() else _ordinary(archive / "trace.json")


def _select(case, run_id):
    if run_id is not None:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise TraceError("Choose an explicit 16-character collection run ID")
        return run_id
    candidates = []
    latest = read_case(case).get("latest_run")
    for archive in _ordinary(case / "runs").glob("*"):
        if not RUN_ID.fullmatch(archive.name):
            continue
        _ordinary(archive)
        if _ordinary(archive / "SHA256SUMS").exists():
            # Installation may have finished just before the latest pointer
            # failed. Only our own pending recovery is an automatic candidate.
            if not _ordinary(archive / "collection-recovery.json").is_file():
                continue
            state = read_json(_ordinary(archive / "trace.json"))
            audit = state.get("collection_recovery", {})
            if (latest != archive.name and latest == state.get("parent_run")
                    and isinstance(audit, dict) and audit.get("run_id") == archive.name):
                candidates.append(archive.name)
            continue
        checkpoint = _checkpoint(archive)
        if checkpoint.is_file() and _finished(read_json(checkpoint)):
            candidates.append(archive.name)
    if len(candidates) != 1:
        message = ("No finished unsealed collection was found" if not candidates else
                   "Choose --run from these finished unsealed collections: " + ", ".join(sorted(candidates)))
        raise TraceError(message)
    return candidates[0]


def _finish_sealed_recovery(case, archive):
    """Finish our own validated publication without editing any archived byte."""
    from .cli import verify_export
    verify_export(archive)
    names = {line.split("  ", 1)[1] for line in (archive / "SHA256SUMS").read_text().splitlines()}
    required = {"collection-recovery.json", "recovery-original-trace.json"}
    if not required.issubset(names):
        raise TraceError("Collection is already sealed; use its saved run instead of recovery")
    state = read_json(_ordinary(archive / "trace.json"))
    audit = read_json(_ordinary(archive / "collection-recovery.json"))
    original = _ordinary(archive / "recovery-original-trace.json").read_bytes()
    original_state = json.loads(original)
    excluded = {"address_tx_counts", "collection_recovery"}
    if (audit.get("schema_version") != 1 or audit.get("run_id") != archive.name
            or audit.get("original_checkpoint_sha256") != digest(original)
            or audit.get("trace_status") != original_state.get("status")
            or state.get("collection_recovery") != audit
            or {key: value for key, value in state.items() if key not in excluded}
               != {key: value for key, value in original_state.items() if key not in excluded}):
        raise TraceError("Sealed recovery does not match its original checkpoint and audit")
    with _ordinary(case / "case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = read_case(case)
        if metadata.get("latest_run") == archive.name:
            raise TraceError("Collection is already sealed; use its saved run instead of recovery")
        evidence = _Evidence(case)
        try:
            _verify(case, archive, original_state, metadata, evidence)
        finally:
            evidence.close()
        metadata["latest_run"] = archive.name
        save_json(case / "case.json", metadata)
    return {**audit, "directory": str(archive), "shared_collection": metadata.get("shared_dataset") is True,
            "notice": "Recovered archive was already complete; its latest pointer is now saved. "
                      "Continue with zero additional hops to fetch missing counts."}


class _Evidence:
    """Read-only evidence access with a bounded cache, independent of API code."""

    def __init__(self, case):
        path = _ordinary(case / "evidence.sqlite")
        for suffix in ("-wal", "-shm", "-journal"):
            _ordinary(path.with_name(path.name + suffix))
        try:
            self.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise TraceError("Unable to open saved collection evidence for offline recovery") from error
        self.connection.row_factory = sqlite3.Row

    def close(self):
        self.row.cache_clear()
        self.decoded.cache_clear()
        self.connection.close()

    @lru_cache(maxsize=64)
    def row(self, identity):
        if type(identity) is not int or identity < 1:
            raise TraceError("Collection checkpoint has an invalid evidence reference")
        row = self.connection.execute("SELECT * FROM observations WHERE id=?", (identity,)).fetchone()
        if row is None or digest(row["body"]) != row["sha256"]:
            raise TraceError("Missing or altered collection evidence observation " + str(identity))
        return dict(row)

    @lru_cache(maxsize=64)
    def decoded(self, identity):
        return json.loads(self.row(identity)["body"])

    def observations(self, identities):
        for identity in sorted(set(identities)):
            yield dict(self.row(identity))


def _verify(case, archive, state, metadata, evidence):
    from .api import Limits
    from .cli import verify_export
    if (state.get("schema_version") != 1 or blockchain(state) != blockchain(metadata)
            or state.get("case_id") != metadata["case_id"]
            or state.get("run_id") != archive.name or not _finished(state)
            or state.get("collection_source") or state.get("pegout_query")
            or state.get("address_mode") not in {"merged", "outpoint_occurrences"}
            or not isinstance(state.get("source"), str) or not state["source"]):
        raise TraceError("Checkpoint is not a finished collection for this investigation")
    if metadata.get("latest_run") != state.get("parent_run"):
        raise TraceError("Investigation advanced or the checkpoint has a different parent; recovery will not replace its latest run")
    Limits(**state["limits"]).validate()
    parent_id = state.get("parent_run")
    if parent_id is not None:
        if not isinstance(parent_id, str) or not RUN_ID.fullmatch(parent_id):
            raise TraceError("Checkpoint has an invalid parent")
        parent_archive = _ordinary(case / "runs" / parent_id)
        verify_export(parent_archive)
        parent = read_json(_ordinary(parent_archive / "trace.json"))
        if (parent.get("case_id") != state["case_id"] or parent.get("source") != state["source"]
                or parent.get("seeds") != state.get("seeds")
                or state.get("ancestor_runs") != [*parent.get("ancestor_runs", []), parent_id]):
            raise TraceError("Checkpoint does not match its verified parent collection")
    elif state.get("ancestor_runs") != []:
        raise TraceError("Initial collection has unexpected ancestors")
    if metadata.get("shared_dataset") is True:
        provenance = state.get("shared_collection", {})
        request_id = provenance.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise TraceError("Shared checkpoint has no valid frozen request")
        request = read_json(_ordinary(case / "requests" / (request_id + ".json")))
        if (digest(canonical({k: v for k, v in request.items() if k != "sha256"})) != request.get("sha256")
                or request.get("dataset_id") != metadata["case_id"]
                or request.get("source") != state["source"] or metadata.get("source") != state["source"]
                or request.get("resume") != parent_id or request.get("seeds") != state.get("seeds")
                or any(provenance.get(key) != request.get(key) for key in (
                    "schema_version", "request_id", "dataset_id", "captured_at", "policy_case_id", "policy_case_name",
                    "members", "settings", "service_controls"))
                or state.get("service_controls") != request.get("service_controls")
                or state.get("labels") != request.get("labels")
                or state.get("include_unconfirmed") != request.get("include_unconfirmed")
                or state.get("hop_reference_name", "") != request["settings"].get("hop_reference_name", "")):
            raise TraceError("Shared checkpoint does not match its frozen collection request")
        settings = request["settings"]
        expected_limits = {"max_hops": settings["hops"], **{key: settings[key] for key in (
            "max_transactions", "max_outpoints", "max_requests", "max_seconds")}}
        if state["limits"] != expected_limits:
            raise TraceError("Shared checkpoint changed its frozen hop or request limits")
    elif state.get("shared_collection"):
        raise TraceError("Shared checkpoint cannot be recovered into a private investigation")
    seeds = state.get("seeds")
    if not isinstance(seeds, list) or not seeds or len(set(seeds)) != len(seeds):
        raise TraceError("Checkpoint has invalid starting outputs")
    for seed in seeds:
        parse_outpoint(seed)
    identities = state.get("observations")
    if (not isinstance(identities, list) or any(type(value) is not int or value < 1 for value in identities)
            or len(set(identities)) != len(identities)):
        raise TraceError("Checkpoint has invalid evidence references")
    identity_set = set(identities)
    for row in evidence.observations(identities):
        # Retry responses remain part of the observation history. Only evidence
        # referenced as a transaction or spend must be a successful response.
        if (row["source"] != state["source"] or type(row["status"]) is not int
                or not 100 <= row["status"] <= 599):
            raise TraceError("Collection evidence has a different API source or invalid response status")

    def body(identity, endpoint):
        if identity not in identity_set:
            raise TraceError("Collection object refers to evidence outside its checkpoint")
        row = evidence.row(identity)
        if row["endpoint"] != endpoint or row["status"] != 200:
            raise TraceError("Collection evidence endpoint or response status does not match its object")
        return evidence.decoded(identity)

    transactions, outputs, links = (state[key] for key in ("transactions", "outputs", "links"))
    if not all(isinstance(value, dict) for value in (transactions, outputs, links)):
        raise TraceError("Malformed collection objects")
    for txid, record in transactions.items():
        if not HEX64.fullmatch(txid) or type(record.get("depth")) is not int or record["depth"] < 0:
            raise TraceError("Malformed collection transaction")
        validate_transaction(record["data"], txid)
        if body(record["observation_id"], "/tx/" + txid) != record["data"]:
            raise TraceError("Collection transaction disagrees with saved evidence")
    for key, output in outputs.items():
        txid, index = parse_outpoint(key)
        if (output.get("outpoint") != key or output.get("txid") != txid or output.get("vout") != index
                or txid not in transactions or index >= len(transactions[txid]["data"]["vout"])
                or type(output.get("depth")) is not int or output["depth"] < 0
                or not isinstance(output.get("status"), str) or output["status"] == "pending"):
            raise TraceError("Collection output is incomplete or disagrees with its transaction")
        status = output["status"]
        kind = output_kind(transactions[txid]["data"]["vout"][index], blockchain(state))
        if status in TERMINAL - {"spent"} and status != kind:
            raise TraceError("Collection terminal output disagrees with saved transaction")
        if (status == "spent") != (key in links):
            raise TraceError("Collection spent output and link bookkeeping disagree")
        if "spend_observation_id" in output:
            spends = body(output["spend_observation_id"], "/tx/" + txid + "/outspends")
            if (not isinstance(spends, list) or len(spends) != len(transactions[txid]["data"]["vout"])
                    or spends[index] != output.get("observed_spend")):
                raise TraceError("Collection output spend disagrees with saved evidence")
            if status == "unspent_at_observation" and spends[index].get("spent") is not False:
                raise TraceError("Collection unspent output disagrees with saved evidence")
        elif status in {"spent", "unspent_at_observation"} or "observed_spend" in output:
            raise TraceError("Collection spend is missing its evidence reference")
    if any(seed not in outputs for seed in seeds):
        raise TraceError("Collection is missing a starting output")
    for key, link in links.items():
        txid, index = parse_outpoint(key)
        output = outputs.get(key, {})
        child = transactions.get(link.get("spending_txid"), {})
        vin = link.get("vin")
        if (link.get("outpoint") != key or type(vin) is not int or vin < 0 or not child
                or vin >= len(child["data"]["vin"])
                or link.get("observation_id") != output.get("spend_observation_id")
                or link.get("spending_tx_observation_id") != child.get("observation_id")):
            raise TraceError("Collection link has invalid evidence references")
        spend, actual = output["observed_spend"], child["data"]["vin"][vin]
        if (spend.get("spent") is not True or spend.get("txid") != link["spending_txid"]
                or spend.get("vin") != vin or actual.get("txid") != txid or actual.get("vout") != index
                or actual.get("is_pegin") or link.get("relationship") != "observed_utxo_spend"):
            raise TraceError("Collection link disagrees with its saved transaction input")
    stats = state.get("stats", {})
    if (stats.get("transactions_cumulative") != len(transactions)
            or stats.get("outputs_cumulative") != len(outputs)
            or stats.get("frontier_count") != sum(row["status"] not in TERMINAL for row in outputs.values())):
        raise TraceError("Collection statistics disagree with its saved objects")


def recover_collection(case, run_id=None, *, progress=None):
    """Publish exactly one finished unsealed checkpoint, using local evidence only."""
    from .address_counts import addresses, apply_saved_counts
    from .cli import verify_export
    from .export import export_run
    from .progress import report_progress
    case = _ordinary(case)
    _ordinary(case / "case.json")
    with ExitStack() as stack:
        _lock(stack, case / "trace.lock")
        _lock(stack, case / "address-counts.lock")
        run_id = _select(case, run_id)
        archive = _ordinary(case / "runs" / run_id)
        if _ordinary(archive / "SHA256SUMS").exists():
            try:
                return _finish_sealed_recovery(case, archive)
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as error:
                raise TraceError("Sealed recovery validation failed: " + type(error).__name__) from error
        original = _checkpoint(archive).read_bytes()
        state = json.loads(original)
        metadata = read_case(case)
        evidence = _Evidence(case)
        stack.callback(evidence.close)
        try:
            _verify(case, archive, state, metadata, evidence)
            counts = apply_saved_counts(case, state)
            wanted = addresses(state)
            known = sum(address in counts for address in wanted)
            recovered = {"schema_version": 1, "recovered_at": now(), "run_id": run_id,
                "original_checkpoint_sha256": digest(original), "trace_status": state["status"],
                "address_counts": {"known": known, "total": len(wanted), "remaining": len(wanted) - known,
                                   "complete": known == len(wanted)},
                "note": "Offline recovery verified trace response evidence and merged the validated count cache. "
                        "Ancillary count responses retain the existing cache/evidence-store provenance. "
                        "No additional tracing or count requests were performed."}
            state["collection_recovery"] = recovered
            report_progress(progress, "exporting_collection", 0, 1)
            with tempfile.TemporaryDirectory(prefix=".collection-recovery-", dir=case) as temporary:
                staging = Path(temporary)
                (staging / "recovery-original-trace.json").write_bytes(original)
                save_json(staging / "collection-recovery.json", recovered)
                export_run(evidence, state, staging, state["address_mode"] == "merged")
                verify_export(staging)
                with _ordinary(case / "case.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    latest = read_case(case)
                    if latest.get("latest_run") != metadata.get("latest_run"):
                        raise TraceError("Investigation advanced during recovery; no latest pointer was changed")
                    # Preserve the original before replacing any partial export.
                    files = sorted(staging.rglob("*"), key=lambda path: (
                        path.name != "recovery-original-trace.json", str(path)))
                    for source in files:
                        if not source.is_file() or source.name == "SHA256SUMS":
                            continue
                        destination = _ordinary(archive / source.relative_to(staging))
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, destination)
                    os.replace(staging / "SHA256SUMS", _ordinary(archive / "SHA256SUMS"))
                    latest["latest_run"] = run_id
                    save_json(case / "case.json", latest)
            report_progress(progress, "exporting_collection", 1, 1)
            return {**recovered, "directory": str(archive), "shared_collection": metadata.get("shared_dataset") is True,
                    "notice": "Saved collection is available. Continue with zero additional hops to fetch missing counts."}
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as error:
            raise TraceError("Collection recovery validation failed: " + type(error).__name__) from error
