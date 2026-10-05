from .networks import blockchain
"""Saved-evidence scope comparisons before graph construction or layout.

The index proves exact transaction spends globally. Only reached transaction
bodies are read here; lookup of a boundary's spend does not load its descendants.
Analyses are immutable, content-addressed selections, independent of previews.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
import fcntl
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

from .common import StopRun, TraceError, canonical, digest, now, output_kind, parse_outpoint
from .connections import validate_hops
from .hop_limits import hop_limit_value
from .pegout_limit import lbtc_units
from .progress import report_progress
from .transaction_csv import _amount, _asset, _text

SCHEMA_VERSION = 1
ALGORITHM_VERSION = 1
ANALYSIS_ID = re.compile(r"[0-9a-f]{16}-analysis\Z")
FILES = frozenset({"analysis.json", "inputs.json", "frontiers.csv", "SHA256SUMS"})
SUMMARY_BYTES = 4 * 1024 * 1024
FRONTIER_PREVIEW_LIMIT = 100
FRONTIER_FIELDS = ("outpoint", "txid", "vout", "hop", "reason", "address", "saved_continuation",
                   "spending_txid", "observation_time", "spend_observation_id", "observation_status")
NOTICE = (
    "Scope baseline over saved exact transaction spends, with the original starting outputs at hop 0. "
    "Named address groups do not reset this distance. Explicit stops and attribution hop allowances "
    "are respected, as in Full trace. Peg-out and starter-connection plots may use different policies. "
    "Counts include reached outputs, exclude fees and unrelated context, and are not rendered-object counts. "
    "Peg-out amounts are gross disclosed L-BTC request values, not allocated stolen funds or proof of payout. "
    "A boundary is not an endpoint; an unspent output is unspent only at its recorded observation. "
    "A collection hop limit does not establish complete coverage at that depth. No network data was fetched."
)


def _ordinary(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Analysis files cannot contain symbolic links")
    return path


def _open(path):
    path = _ordinary(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise TraceError("Analysis files must be ordinary files")
    return os.fdopen(fd, "rb")


def _checkpoint(progress=None, cancel=None, completed=0, total=0):
    if cancel is not None and cancel():
        raise StopRun("Scope analysis cancelled")
    report_progress(progress, "scope_analysis", completed, total)


def _hash(path, *, progress=None, cancel=None):
    result = hashlib.sha256()
    with _open(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _checkpoint(progress, cancel)
            result.update(chunk)
    return result.hexdigest()


def _write_json(path, data):
    with path.open("wb") as stream:
        stream.write(canonical(data))
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _manifest(directory):
    with _open(directory / "SHA256SUMS") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise TraceError("Analysis manifest is too large")
    try:
        entries = {}
        for line in raw.decode("utf-8").splitlines():
            checksum, name = line.split("  ", 1)
            if (name not in FILES - {"SHA256SUMS"} or name in entries
                    or not re.fullmatch("[0-9a-f]{64}", checksum)):
                raise ValueError
            entries[name] = checksum
        if set(entries) != FILES - {"SHA256SUMS"}:
            raise ValueError
    except (UnicodeError, ValueError) as error:
        raise TraceError("Analysis manifest is invalid") from error
    return entries


def _directory(case, analysis_id):
    if not isinstance(analysis_id, str) or not ANALYSIS_ID.fullmatch(analysis_id):
        raise TraceError("Choose a saved scope analysis")
    return _ordinary(Path(case) / "analyses" / analysis_id)


def _summary(directory, case_id, expected_sha256=None):
    with _open(directory / "analysis.json") as stream:
        raw = stream.read(SUMMARY_BYTES + 1)
    if len(raw) > SUMMARY_BYTES:
        raise TraceError("Analysis summary is too large")
    if expected_sha256 is not None and digest(raw) != expected_sha256:
        raise TraceError("Analysis summary checksum mismatch")
    try:
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION
                or type(result.get("schema_version")) is not int
                or result.get("case_id") != case_id or result.get("analysis_id") != directory.name
                or not isinstance(result.get("created_at"), str) or len(result["created_at"]) > 80
                or not isinstance(result.get("name"), str) or len(result["name"]) > 200
                or not isinstance(result.get("notice"), str) or len(result["notice"]) > 8192
                or type(result.get("analysis_number")) is not int or result["analysis_number"] < 1
                or result.get("hop_basis") != "original_seeds"
                or not isinstance(result.get("input_sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", result["input_sha256"])
                or result["input_sha256"][:16] + "-analysis" != directory.name):
            raise ValueError
        validate_hops(result["max_hops"])
        expected = sorted({max(0, result["max_hops"] - offset) for offset in range(3)})
        if [row.get("max_hops") for row in result["comparisons"]] != expected:
            raise ValueError
        for row in result["comparisons"]:
            for field in ("transaction_count", "address_count", "output_count", "pegout_count", "unspent_count",
                          "unspendable_count", "frontier_count", "data_gap_count", "stopped_count"):
                if type(row.get(field)) is not int or not 0 <= row[field] < 2 ** 53:
                    raise ValueError
            if (not isinstance(row.get("pegout_base_units"), str)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,199}", row["pegout_base_units"])
                    or row.get("pegout_lbtc") != lbtc_units(int(row["pegout_base_units"]))):
                raise ValueError
        if (not isinstance(result.get("source"), dict)
                or result["source"].get("data_source") not in ("investigation", "shared")
                or not isinstance(result.get("frontier"), list)
                or len(result["frontier"]) > FRONTIER_PREVIEW_LIMIT
                or type(result.get("frontier_count")) is not int
                or result["frontier_count"] < len(result["frontier"])
                or result["frontier_count"] != result["comparisons"][-1]["frontier_count"]):
            raise ValueError
        source = result["source"]
        if set(source) - {"data_source", "run_id", "archive_sha256", "source_case_id", "dataset_id",
                          "collection_max_hops", "collected_depth", "collection_status", "collection_stop_reason",
                          "source_hop_reference_name", "investigation_hop_reference_name", "seed_basis", "seed_count",
                          "indexed_source"}:
            raise ValueError
        for field, pattern in (("run_id", r"[0-9a-f]{16}"), ("archive_sha256", r"[0-9a-f]{64}"),
                               ("source_case_id", r"[0-9a-f]{32}")):
            if not isinstance(source.get(field), str) or not re.fullmatch(pattern, source[field]):
                raise ValueError
        if result.get("run_id") != source["run_id"]:
            raise ValueError
        if source["data_source"] == "shared" and source.get("dataset_id") != source["source_case_id"]:
            raise ValueError
        if source.get("seed_basis") not in ("saved_run", "investigation_settings"):
            raise ValueError
        if type(source.get("seed_count")) is not int or source["seed_count"] < 1:
            raise ValueError
        if type(source.get("indexed_source")) is not bool:
            raise ValueError
        for field in ("collection_max_hops", "collected_depth"):
            if source.get(field) is not None and (type(source[field]) is not int or source[field] < 0):
                raise ValueError
        for field in ("collection_status", "collection_stop_reason", "source_hop_reference_name",
                      "investigation_hop_reference_name"):
            if source.get(field) is not None and (not isinstance(source[field], str) or len(source[field]) > 4096):
                raise ValueError
        for row in result["frontier"]:
            txid, number = parse_outpoint(row["outpoint"])
            if (row["outpoint"] != f"{txid}:{number}" or row.get("txid") != txid or row.get("vout") != number
                    or type(row.get("hop")) is not int or not 0 <= row["hop"] <= result["max_hops"]
                    or row.get("reason") not in {"explicit_stop", "attribution_hop_limit", "analysis_hop_limit",
                                                 "missing_spending_transaction", "unobserved_outspend"}
                    or type(row.get("saved_continuation")) is not bool):
                raise ValueError
            for field in ("address", "spending_txid", "observation_time", "observation_status"):
                if not isinstance(row.get(field), str) or len(row[field]) > 4096:
                    raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, TraceError) as error:
        raise TraceError("Analysis summary is invalid") from error
    return result


def read_analysis(case, analysis_id):
    """Authenticate bounded saved metadata; never read the potentially large CSV."""
    from .investigations import read_case
    directory = _directory(case, analysis_id)
    manifest = _manifest(directory)
    return _summary(directory, read_case(case)["case_id"], manifest["analysis.json"])


def list_analyses(case, limit=20):
    """List bounded summary files only; do not load evidence or frontier exports."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise TraceError("Analysis listing limit must be from 1 to 100")
    from .investigations import read_case
    directory = _ordinary(Path(case) / "analyses")
    if not directory.exists():
        return []
    identity = read_case(case)["case_id"]
    # Only stat directory entries when selecting the newest few summaries.
    candidates = heapq.nlargest(limit, ((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
        for entry in os.scandir(directory) if ANALYSIS_ID.fullmatch(entry.name)
        and entry.is_dir(follow_symlinks=False)))
    result = []
    for _, name in candidates:
        try:
            item = _summary(_ordinary(directory / name), identity)
        except (OSError, TraceError):
            continue
        result.append({key: value for key, value in item.items() if key != "frontier"})
    return sorted(result, key=lambda item: item["analysis_number"], reverse=True)


def verified_analysis_file(case, analysis_id, name):
    if name not in FILES:
        raise TraceError("Analysis file not found")
    directory = _directory(case, analysis_id)
    read_analysis(case, analysis_id)
    manifest = _manifest(directory)
    if name != "SHA256SUMS" and _hash(directory / name) != manifest[name]:
        raise TraceError("Analysis file checksum mismatch")
    return directory / name


def _analyse_index(index, seeds, labels, max_hops, *, progress=None, cancel=None):
    """One bounded Pareto walk, then three cheap summaries of its reached outputs.

    Keep depth and remaining allowance paired: a longer unrestricted arrival
    must not lend its allowance to a shorter exhausted path through a join.
    """
    indexed_labels = defaultdict(list)
    for label in labels:
        if label.get("hop_limit") is not None:
            hop_limit_value(label["hop_limit"])
        indexed_labels[label["kind"], label["value"]].append(label)
    transactions, outputs, links, matches, paths, tx_depths = {}, {}, {}, {}, defaultdict(list), {}
    queue = [(0, key, math.inf) for key in seeds]
    heapq.heapify(queue)
    examined = 0

    def transaction(txid):
        if txid not in transactions:
            record = index.transaction(txid)
            if record is None:
                raise TraceError("Saved scope is missing a reached transaction")
            transactions[txid] = record
        return transactions[txid]

    while queue:
        depth, key, remaining = heapq.heappop(queue)
        if depth > max_hops:
            continue
        examined += 1
        if examined % 128 == 1:
            _checkpoint(progress, cancel, examined, examined + len(queue))
        txid, number = parse_outpoint(key)
        raw = transaction(txid)["data"]["vout"]
        if number >= len(raw):
            raise TraceError("A selected starting output does not exist in saved evidence")
        tx_depths[txid] = min(depth, tx_depths.get(txid, depth))
        output = raw[number]
        if output_kind(output, blockchain(index.metadata)) == "fee":
            continue
        if key not in outputs:
            outputs[key] = (output, index.output(key) or {})
            links[key] = index.link(key)
            matches[key] = [label for target in (("outpoint", key), ("script", output.get("scriptpubkey")),
                            ("address", output.get("scriptpubkey_address"))) if target[1] is not None
                            for label in indexed_labels[target]]
        annotations = matches[key]
        cap = 0 if any(label.get("stop") is True for label in annotations) else min(
            (hop_limit_value(label["hop_limit"]) for label in annotations if label.get("hop_limit") is not None),
            default=math.inf)
        remaining = min(remaining, cap)
        if any(old_depth <= depth and old_remaining >= remaining for old_depth, old_remaining in paths[key]):
            continue
        paths[key] = [(d, r) for d, r in paths[key] if not (depth <= d and remaining >= r)] + [(depth, remaining)]
        link = links[key]
        if output_kind(output, blockchain(index.metadata)) == "spendable" and remaining > 0 and depth < max_hops and link:
            child = link["spending_txid"]
            rows = transaction(child)["data"]["vout"]
            tx_depths[child] = min(depth + 1, tx_depths.get(child, depth + 1))
            for number in range(len(rows)):
                heapq.heappush(queue, (depth + 1, f"{child}:{number}", remaining - 1))

    observations = {}

    def observation_time(item):
        oid = item.get("spend_observation_id")
        if oid is not None and oid not in observations:
            observations[oid] = index.observation(oid) or {}
        record = observations.get(oid, {})
        return record.get("fetched_at") or item.get("observed_at") or ""

    comparisons, final_frontier = [], []
    for limit in sorted({max(0, max_hops - offset) for offset in range(3)}):
        counts = Counter()
        addresses, frontier = set(), []
        total = 0
        for number, key in enumerate(sorted(paths), 1):
            if number % 128 == 1:
                _checkpoint(progress, cancel, number, len(paths))
            arrivals = [(d, r) for d, r in paths[key] if d <= limit]
            if not arrivals:
                continue
            output, item = outputs[key]
            kind = output_kind(output, blockchain(index.metadata))
            counts["output_count"] += 1
            address = output.get("scriptpubkey_address")
            if address:
                addresses.add(address)
            if kind == "pegout":
                counts["pegout_count"] += 1
                asset, amount = _asset(output), _amount(output)
                if not asset:
                    counts["unknown_pegout_asset_count"] += 1
                elif asset != "L-BTC":
                    counts["non_lbtc_pegout_count"] += 1
                elif amount == "":
                    counts["unknown_pegout_amount_count"] += 1
                else:
                    counts["valued_lbtc_pegout_count"] += 1
                    total += amount
                continue
            if kind == "provably_unspendable":
                counts["unspendable_count"] += 1
                continue
            link = links[key]
            observed = item.get("observed_spend") or {}
            status = item.get("status", "not_observed")
            control = item.get("trace_control")
            if isinstance(control, dict):
                status = control.get("previous_status", status)
            oid = item.get("spend_observation_id")
            verified_unspent = (not link and status == "unspent_at_observation"
                                and observed.get("spent") is False
                                and ((type(oid) is int and oid > 0) or (isinstance(oid, str) and bool(oid.strip()))))
            if verified_unspent:
                counts["unspent_count"] += 1
                continue
            permitted = any(remaining > 0 for _, remaining in arrivals)
            if not permitted:
                reason = "explicit_stop" if any(label.get("stop") is True for label in matches[key]) else "attribution_hop_limit"
                counts["stopped_count"] += 1
            elif link and any(depth < limit and remaining > 0 for depth, remaining in arrivals):
                continue
            elif link:
                reason = "analysis_hop_limit"
            elif observed.get("spent") is True or status in ("spent", "spent_in_saved_evidence"):
                reason = "missing_spending_transaction"
                counts["data_gap_count"] += 1
            else:
                reason = "unobserved_outspend"
                counts["data_gap_count"] += 1
            txid, vout = parse_outpoint(key)
            frontier.append({"outpoint": key, "txid": txid, "vout": vout, "hop": min(d for d, _ in arrivals),
                "reason": reason, "address": address or "", "saved_continuation": bool(link),
                "spending_txid": link["spending_txid"] if link else observed.get("txid", ""),
                "observation_time": observation_time(item), "spend_observation_id": oid or "",
                "observation_status": status})
        fields = ("output_count", "pegout_count", "unspent_count", "unspendable_count", "data_gap_count",
                  "stopped_count", "unknown_pegout_amount_count", "unknown_pegout_asset_count",
                  "non_lbtc_pegout_count", "valued_lbtc_pegout_count")
        comparisons.append({"max_hops": limit, "transaction_count": sum(d <= limit for d in tx_depths.values()),
            "address_count": len(addresses), "pegout_lbtc": lbtc_units(total), "pegout_base_units": str(total),
            "frontier_count": len(frontier), **{field: counts[field] for field in fields}})
        final_frontier = sorted(frontier, key=lambda row: (row["hop"], row["outpoint"]))
    _checkpoint(progress, cancel, 1, 1)
    return comparisons, final_frontier


@contextmanager
def _index(case, metadata, run_id, data_source, dataset_id, progress):
    if data_source == "shared":
        from .shared_projection import _shared_index
        with _shared_index(case, metadata, run_id, dataset_id, progress) as index:
            yield index
    else:
        from .cli import run_path
        from .snapshot_index import open_snapshot_index
        archive = _ordinary(run_path(case, run_id))
        if not _ordinary(archive / "evidence-index.json").exists():
            # Historical private runs predate sealed observation indexes. They
            # remain usable, with a full verification/read explicitly disclosed.
            from .plots import _archive_source
            state, checksum = _archive_source(case, run_id, metadata, progress=progress)
            yield _LegacyIndex(state, checksum)
            return
        with open_snapshot_index(archive, case_id=metadata["case_id"],
                                 run_id=run_id, progress=progress) as index:
            yield index


class _LegacyIndex:
    """The same lookup contract for already verified, unindexed private runs."""
    indexed_source = False

    def __init__(self, state, checksum):
        from .connections import _saved_connection_evidence
        from .pegout_paths import _evidence
        self.state = _saved_connection_evidence(state, copy_state=False)
        _evidence(self.state, respect_attribution_hops=False)
        self.manifest_sha256 = checksum
        self.metadata = {key: value for key, value in state.items()
                         if key not in {"transactions", "outputs", "links", "observations", "address_tx_counts"}}
        self.metadata["_snapshot_collected_depth"] = max((row.get("depth", 0)
            for row in state["outputs"].values()), default=0)

    def transaction(self, txid):
        return self.state["transactions"].get(txid)

    def output(self, key):
        return self.state["outputs"].get(key)

    def link(self, key):
        return self.state["links"].get(key)

    def observation(self, identity):
        return None


@contextmanager
def _publication_lock(directory, progress, cancel):
    with _ordinary(directory / ".publish.lock").open("a") as stream:
        while True:
            _checkpoint(progress, cancel)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def analyze_scope(case, run_id="latest", *, data_source="investigation", dataset_id=None,
                  max_hops=10, progress=None, cancel=None):
    """Persist/reuse a scope baseline; never construct a graph or invoke ELK."""
    from .cli import resolve_latest
    from .investigations import read_case
    from .plots import _locked
    from .services import apply_service_labels, effective_services
    validate_hops(max_hops)
    if data_source not in ("investigation", "shared"):
        raise TraceError("Choose investigation or shared saved data")
    if data_source != "shared" and dataset_id is not None:
        raise TraceError("A shared dataset applies only to shared data")
    case = _ordinary(Path(case))
    _checkpoint(progress, cancel)
    with _locked(case):
        metadata = read_case(case)
        controls = {key: value for key, value in effective_services(case).items() if key != "history"}
        selected_run = resolve_latest(case, run_id) if data_source == "investigation" else run_id
    with _index(case, metadata, selected_run, data_source, dataset_id, progress) as index:
        _checkpoint(progress, cancel)
        original = index.metadata
        raw_seeds = metadata.get("seeds") if data_source == "shared" else original.get("seeds")
        if not isinstance(raw_seeds, list) or not raw_seeds:
            raise TraceError("Choose starting outputs before analyzing saved data")
        seeds = sorted({f"{txid}:{number}" for txid, number in map(parse_outpoint, raw_seeds)})
        labels = apply_service_labels(original.get("labels", []) if data_source == "investigation" else [], controls)
        source = {"data_source": data_source, "run_id": original["run_id"],
            "archive_sha256": index.manifest_sha256, "source_case_id": original["case_id"],
            "indexed_source": getattr(index, "indexed_source", True),
            "seed_basis": "investigation_settings" if data_source == "shared" else "saved_run",
            "seed_count": len(seeds),
            "collection_max_hops": original.get("limits", {}).get("max_hops"),
            "collected_depth": original.get("_snapshot_collected_depth"),
            "collection_status": original.get("status"), "collection_stop_reason": original.get("stop_reason"),
            "source_hop_reference_name": original.get("hop_reference_name", ""),
            "investigation_hop_reference_name": metadata.get("run_defaults", {}).get("hop_reference_name", "")}
        if data_source == "shared":
            source["dataset_id"] = original["case_id"]
        inputs = {"schema_version": SCHEMA_VERSION, "algorithm_version": ALGORITHM_VERSION,
            "case_id": metadata["case_id"], "source": source, "seeds": seeds, "labels": labels,
            "service_controls": controls, "max_hops": max_hops, "hop_basis": "original_seeds",
            "respect_stops": True, "respect_attribution_hops": True, "exclude_fees": True}
        fingerprint = digest(canonical(inputs))
        analysis_id = fingerprint[:16] + "-analysis"
        root = _ordinary(case / "analyses")
        root.mkdir(exist_ok=True)
        target = _directory(case, analysis_id)

        def cached():
            result = read_analysis(case, analysis_id)
            if result["input_sha256"] != fingerprint:
                raise TraceError("Analysis identity conflicts with saved inputs")
            manifest = _manifest(target)
            for name in FILES - {"SHA256SUMS", "analysis.json"}:
                if _hash(target / name, progress=progress, cancel=cancel) != manifest[name]:
                    raise TraceError("Analysis file checksum mismatch")
            if digest(canonical(json.loads((target / "inputs.json").read_bytes()))) != fingerprint:
                raise TraceError("Analysis inputs disagree with their identity")
            return {**result, "cache_hit": True}

        if target.exists():
            return cached()
        comparisons, frontier = _analyse_index(index, seeds, labels, max_hops, progress=progress, cancel=cancel)
    _checkpoint(progress, cancel)
    with _publication_lock(root, progress, cancel):
        if target.exists():
            return cached()
        existing = list_analyses(case, limit=100)
        sequence = max((item["analysis_number"] for item in existing), default=0) + 1
        result = {"schema_version": SCHEMA_VERSION, "analysis_id": analysis_id, "analysis_number": sequence,
            "name": f"Analysis {sequence} · {max_hops} hops", "created_at": now(), "case_id": metadata["case_id"],
            "run_id": source["run_id"], "source": source, "max_hops": max_hops, "hop_basis": "original_seeds",
            "input_sha256": fingerprint, "comparisons": comparisons, "frontier_count": len(frontier),
            "frontier": frontier[:FRONTIER_PREVIEW_LIMIT], "notice": NOTICE}
        temporary = Path(tempfile.mkdtemp(prefix=".analysis-", dir=root))
        try:
            _write_json(temporary / "analysis.json", result)
            _write_json(temporary / "inputs.json", inputs)
            with (temporary / "frontiers.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FRONTIER_FIELDS)
                writer.writeheader()
                for number, row in enumerate(frontier, 1):
                    if number % 128 == 1:
                        _checkpoint(progress, cancel, number, len(frontier))
                    writer.writerow({key: _text(value) for key, value in row.items()})
                stream.flush()
                os.fsync(stream.fileno())
            checksums = "".join(f"{_hash(temporary / name, progress=progress, cancel=cancel)}  {name}\n"
                                for name in sorted(FILES - {"SHA256SUMS"}))
            with (temporary / "SHA256SUMS").open("w", encoding="utf-8") as stream:
                stream.write(checksums)
                stream.flush()
                os.fsync(stream.fileno())
            _checkpoint(progress, cancel, 1, 1)
            temporary.rename(target)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {**result, "cache_hit": False}
