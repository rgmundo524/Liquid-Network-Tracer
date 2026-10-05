"""One workspace evidence collection, with an explicit frozen collection policy.

The shared store owns its observations and immutable runs. Investigations keep
their own seeds, annotations, plots and boards; no private run is merged here.
"""
from copy import deepcopy
import fcntl
from pathlib import Path
import re
import uuid

from .common import TraceError, canonical, digest, now, parse_outpoint, read_json, save_json
from .investigations import effective_run_settings, list_investigations, read_case, validate_settings
from .progress import report_progress
from .networks import blockchain, default_api

DIRECTORY = ".shared-collection"
IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
RUN_ID = re.compile(r"[0-9a-f]{16}\Z")
_SOURCE_HINTS = {}
_RUN_SUMMARIES = {}


def _signature(path):
    info = path.stat()
    return info.st_mtime_ns, info.st_size


def _remember(cache, key, value):
    cache[key] = value
    if len(cache) > 256:
        cache.pop(next(iter(cache)), None)


def _safe(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Shared collection paths cannot contain symbolic links")
    return path


def dataset_directory(chain="liquid"):
    return DIRECTORY if blockchain(chain) == "liquid" else DIRECTORY + "-bitcoin"


def dataset_path(case):
    return _safe(_safe(case).parent / dataset_directory(blockchain(read_case(case))))


def _source(case, metadata=None, *, verify=True, progress=None):
    """Determine source identity without contacting an API."""
    from .cli import run_path, verify_export
    metadata = metadata or read_case(case)
    fixture = metadata.get("fixture")
    configured = "fixture://" + digest(canonical(read_json(_safe(fixture)))) if fixture else None
    parent = None
    if metadata.get("latest_run"):
        archive = _safe(run_path(case, metadata["latest_run"]))
        trace_file, manifest = _safe(archive / "trace.json"), _safe(archive / "SHA256SUMS")
        signature = (_signature(trace_file), _signature(manifest))
        cached = _SOURCE_HINTS.get(trace_file)
        if not verify and cached is not None and cached[0] == signature:
            parent = cached[1]
        else:
            if verify:
                verify_export(archive, progress=progress)
            report_progress(progress, "loading_collection", 0, 1)
            parent = read_json(trace_file)
            report_progress(progress, "loading_collection", 1, 1)
            _remember(_SOURCE_HINTS, trace_file, (signature, {**{key: parent.get(key) for key in ("case_id", "run_id", "source")}, "blockchain": blockchain(parent)}))
        if parent.get("case_id") != metadata["case_id"] or parent.get("run_id") != metadata["latest_run"]:
            raise TraceError("The selected collection does not belong to its investigation")
        if blockchain(parent) != blockchain(metadata):
            raise TraceError("The selected collection belongs to a different blockchain")
        source = parent.get("source")
        if not isinstance(source, str) or not source:
            raise TraceError("Saved collection has no API source identity")
        if configured and source != configured:
            raise TraceError("The fixture no longer matches the investigation's saved API source")
        if source.startswith("fixture://") and not configured:
            raise TraceError("The investigation's original fixture is required for shared collection")
    else:
        source = configured or default_api(metadata)
    return source, parent


def _dataset_metadata(path):
    _safe(Path(path) / "case.json")
    metadata = read_case(path)
    if metadata.get("shared_dataset") is not True or not isinstance(metadata.get("source"), str):
        raise TraceError("Invalid shared collection identity; restore its metadata")
    return metadata


def _compatible(case, dataset, *, verify=True, progress=None):
    metadata = read_case(case)
    source, _ = _source(case, metadata, verify=verify, progress=progress)
    if metadata["blockchain"] != dataset["blockchain"] or source != dataset["source"]:
        raise TraceError("Shared collection requires the same blockchain and API source or identical fixture")
    return source


def pin_shared_run(case, run_id="latest", dataset_id=None):
    """Select an immutable run for admission without loading or trusting evidence.

    The worker must still validate source compatibility and archive identity,
    either through load_shared_run or the verified snapshot index, before use.
    """
    from .cli import resolve_latest, run_path
    path = dataset_path(case)
    metadata = _dataset_metadata(path)
    if dataset_id is not None and dataset_id != metadata["case_id"]:
        raise TraceError("The shared dataset identity changed; select its current saved collection")
    selected = resolve_latest(path, run_id)
    if not isinstance(selected, str) or not RUN_ID.fullmatch(selected):
        raise TraceError("Choose a saved shared collection run")
    if not _safe(run_path(path, selected)).is_dir():
        raise TraceError("Choose a saved shared collection run")
    return selected, path


def load_shared_run(case, run_id="latest", *, dataset_id=None, progress=None):
    """Verify a pinned dataset archive and compatibility with a focused case."""
    from .cli import run_path, resolve_latest, verify_export
    path = dataset_path(case)
    metadata = _dataset_metadata(path)
    if dataset_id is not None and dataset_id != metadata["case_id"]:
        raise TraceError("The shared dataset identity changed; select its current saved collection")
    _compatible(_safe(case), metadata, progress=progress)
    run_id = resolve_latest(path, run_id)
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise TraceError("Choose a saved shared collection run")
    archive = _safe(run_path(path, run_id))
    manifest = _safe(archive / "SHA256SUMS")
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2:
            _safe(archive / parts[1])
    verify_export(archive, progress=progress)
    report_progress(progress, "loading_collection", 0, 1)
    state = read_json(archive / "trace.json")
    report_progress(progress, "loading_collection", 1, 1)
    if (state.get("case_id") != metadata["case_id"] or state.get("run_id") != run_id
            or state.get("source") != metadata["source"]
            or blockchain(state) != blockchain(metadata)
            or state.get("shared_collection", {}).get("dataset_id") != metadata["case_id"]):
        raise TraceError("Shared run does not match the dataset identity")
    return path, state, archive


def resolve_shared_run(case, run_id="latest", dataset_id=None):
    path, state, _ = load_shared_run(case, run_id, dataset_id=dataset_id)
    return state["run_id"], path


def _create_dataset(root, focused, source):
    root = _safe(root)
    path = _safe(root / dataset_directory(focused["blockchain"]))
    # The workspace lock covers first creation only. Collectors use the normal
    # dataset trace.lock, independently of every private investigation.
    with _safe(root / (dataset_directory(focused["blockchain"]) + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            path.mkdir()
        if not (path / "case.json").exists():
            save_json(path / "case.json", {
                "schema_version": 1, "case_id": uuid.uuid4().hex, "name": "Shared collection",
                "blockchain": focused["blockchain"], "source": source, "shared_dataset": True,
                "created_at": now(), "miro_board": None, "fixture": focused.get("fixture"),
                "seeds": [], "run_defaults": validate_settings({}),
            })
        metadata = _dataset_metadata(path)
        if metadata["blockchain"] != focused["blockchain"] or metadata["source"] != source:
            raise TraceError("Shared collection requires the same blockchain and API source or identical fixture")
    return path, metadata


def prepare_collection(case, members=None, *, hops, resume=None, hop_reference_name=None, settings=None):
    """Freeze membership and focused-case policy before queuing a worker.

    Continuation keeps its parent's exact seeds/member snapshot. Fresh collect
    captures the selected open investigations, not future browser tab changes.
    """
    from .group_hops import normalize_reference_name, reference_addresses, reference_name
    from .services import apply_service_labels, effective_services
    case = _safe(case)
    if read_case(case).get("shared_dataset") is True:
        raise TraceError("Choose an investigation to supply shared collection policy")
    if type(hops) is not int or not 0 <= hops <= 2147483647:
        raise TraceError("Collection hops must be a whole number from 0 to 2147483647")
    with _safe(case / "case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        focused = read_case(case)
        source, private = _source(case, focused)
        controls = {key: value for key, value in effective_services(case).items() if key != "history"}
        values = effective_run_settings(settings if settings is not None else focused.get("run_defaults", {}))
        labels = apply_service_labels(private.get("labels", []) if private else [], controls)
    reference = normalize_reference_name(values["hop_reference_name"] if hop_reference_name is None else hop_reference_name)
    if reference and not reference_addresses({"labels": labels, "hop_reference_name": reference}):
        raise TraceError("The hop reference name has no enabled address attributions in the policy investigation")
    path = dataset_path(case)
    metadata = (_dataset_metadata(path) if (path / "case.json").exists() else
                {"source": source, "blockchain": focused["blockchain"]})
    if metadata["blockchain"] != focused["blockchain"] or metadata["source"] != source:
        raise TraceError("Shared collection requires the same blockchain and API source or identical fixture")
    parent = None
    if resume is not None:
        if members:
            raise TraceError("Continue shared collection keeps its saved members; do not select new investigations")
        if not isinstance(resume, str) or not RUN_ID.fullmatch(resume):
            raise TraceError("Continue shared collection requires a pinned saved run ID")
        if "case_id" not in metadata:
            raise TraceError("Collect shared evidence before continuing a saved run")
        _, parent, _ = load_shared_run(case, resume, dataset_id=metadata["case_id"])
        if read_case(path).get("latest_run") != resume:
            raise TraceError("Shared collection advanced; continue from its latest saved run")
        captured_members = deepcopy(parent["shared_collection"]["members"])
        seeds = parent["seeds"][:]
        maximum = (parent["limits"]["max_hops"] + hops
                   if reference.casefold() == reference_name(parent).casefold() else hops)
    else:
        if (not isinstance(members, list) or not 1 <= len(members) <= 100
                or any(not isinstance(identity, str) or not IDENTITY.fullmatch(identity) for identity in members)
                or len(set(members)) != len(members)):
            raise TraceError("Choose 1 to 100 distinct open investigation IDs")
        catalog = {data.get("case_id"): (member_case, data) for member_case, data in list_investigations(case.parent)}
        captured_members, seed_set = [], set()
        for identity in members:
            if identity not in catalog:
                raise TraceError("An open investigation is unavailable; refresh the selected investigations")
            member_case, member = catalog[identity]
            member_case = _safe(member_case)
            _compatible(member_case, metadata)
            selected = member.get("seeds")
            if not isinstance(selected, list) or not selected:
                raise TraceError("Every shared collection member needs saved starting outputs")
            selected = sorted({f"{txid}:{index}" for txid, index in map(parse_outpoint, selected)})
            captured_members.append({"id": identity, "name": member["name"], "seeds": selected})
            seed_set.update(selected)
        seeds, maximum = sorted(seed_set), hops
    if maximum > 2147483647:
        raise TraceError("The shared collection hop ceiling exceeds 2147483647")
    path, metadata = _create_dataset(case.parent, focused, source)
    values.update(hops=maximum, hop_reference_name=reference)
    request_id = uuid.uuid4().hex
    request = {
        "schema_version": 1, "request_id": request_id, "dataset_id": metadata["case_id"],
        "captured_at": now(), "policy_case_id": focused["case_id"], "policy_case_name": focused["name"],
        "source": source, "blockchain": focused["blockchain"], "fixture": focused.get("fixture"),
        "members": captured_members, "seeds": seeds, "resume": resume, "settings": values,
        "labels": labels, "service_controls": controls,
        "include_unconfirmed": bool(private and private.get("include_unconfirmed", False)),
    }
    request["sha256"] = digest(canonical(request))
    save_json(_safe(path / "requests" / (request_id + ".json")), request)
    return {"request_id": request_id, "dataset_id": metadata["case_id"], "dataset_case": str(path),
            "resume": resume, "members": captured_members, "seeds": seeds, "policy": {
                "case_id": focused["case_id"], "name": focused["name"], "settings": deepcopy(values)},
            "live": not bool(request["fixture"])}


def _request(case, request_id):
    if not isinstance(request_id, str) or not IDENTITY.fullmatch(request_id):
        raise TraceError("Choose a prepared shared collection request")
    path = dataset_path(case)
    metadata = _dataset_metadata(path)
    request = read_json(_safe(path / "requests" / (request_id + ".json")))
    if (not isinstance(request, dict) or request.get("schema_version") != 1
            or request.get("request_id") != request_id or request.get("dataset_id") != metadata["case_id"]
            or request.get("policy_case_id") != read_case(case)["case_id"]
            or request.get("source") != metadata["source"]
            or blockchain(request) != blockchain(metadata)
            or digest(canonical({key: value for key, value in request.items() if key != "sha256"})) != request.get("sha256")):
        raise TraceError("Prepared shared collection changed; prepare a new request")
    fixture = request.get("fixture")
    if fixture and "fixture://" + digest(canonical(read_json(_safe(fixture)))) != request["source"]:
        raise TraceError("Shared collection fixture changed after this request was prepared")
    validate_settings(request["settings"])
    return path, request


def collect_prepared(case, request_id, *, progress=None):
    """Run the ordinary bounded collector under the shared dataset's lock."""
    from .cli import parser, run_trace
    path, request = _request(_safe(case), request_id)
    values = request["settings"]
    arguments = ["trace", "--case", str(path), "--hops", str(values["hops"]),
                 "--hop-reference-name", values["hop_reference_name"]]
    if request["resume"]:
        arguments.extend(["--resume", request["resume"]])
    else:
        for seed in request["seeds"]:
            arguments.extend(["--seed", seed])
    for key in ("max_transactions", "max_outpoints", "max_requests", "max_seconds"):
        arguments.extend(["--" + key.replace("_", "-"), str(values[key])])
    if request["fixture"]:
        arguments.extend(["--fixture", request["fixture"]])
    else:
        arguments.extend(["--base-url", request["source"], "--auth",
                          "blockstream" if request["source"].startswith("https://enterprise.blockstream.info/") else "none"])
    if request["include_unconfirmed"]:
        arguments.append("--include-unconfirmed")
    args = parser().parse_args(arguments)
    args._shared_request = request
    return run_trace(args, progress=progress)


def read_summary(root, case=None):
    """Small read-only UI description; reading never initializes a dataset."""
    path = dataset_path(case) if case is not None else _safe(_safe(root) / DIRECTORY)
    result = {"name": "Shared collection", "compatible": True, "seeds": [], "seed_count": 0,
              "members": [], "runs": []}
    if not (path / "case.json").exists():
        return result
    try:
        metadata = _dataset_metadata(path)
        result.update(dataset_id=metadata["case_id"], latest_run=metadata.get("latest_run"),
                      blockchain=metadata["blockchain"])
        if case is not None:
            _compatible(_safe(case), metadata, verify=False)
        for directory in sorted((path / "runs").glob("*"), reverse=True):
            if not RUN_ID.fullmatch(directory.name) or not (directory / "SHA256SUMS").is_file():
                continue
            trace_file = _safe(directory / "trace.json")
            signature = (_signature(trace_file), _signature(_safe(directory / "SHA256SUMS")))
            cached = _RUN_SUMMARIES.get(trace_file)
            if cached is not None and cached[0] == signature and cached[1] == metadata["case_id"]:
                item, provenance = deepcopy(cached[2]), cached[3]
                result["runs"].append(item)
                if directory.name == metadata.get("latest_run"):
                    result.update(seeds=item["seeds"][:], seed_count=item["seed_count"],
                                  members=deepcopy(provenance["members"]),
                                  policy_case_name=provenance["policy_case_name"], latest=item)
                continue
            state = read_json(trace_file)
            if state.get("case_id") != metadata["case_id"] or state.get("run_id") != directory.name:
                continue
            provenance = state.get("shared_collection", {})
            if provenance.get("dataset_id") != metadata["case_id"]:
                continue
            from .web import collected_hops
            from .performance import public_performance
            item = {key: deepcopy(state.get(key)) for key in ("status", "stop_reason")}
            item.update(id=state["run_id"], created_at=state.get("started_at"),
                        transaction_count=len(state.get("transactions", {})),
                        frontier_count=state.get("stats", {}).get("frontier_count", 0),
                        collected_hops=collected_hops(state))
            item.update(max_hops=state.get("limits", {}).get("max_hops"),
                        hop_reference_name=state.get("hop_reference_name", ""),
                        seed_count=len(state["seeds"]), seeds=deepcopy(state["seeds"]),
                        policy_case_name=provenance.get("policy_case_name"))
            performance = public_performance(state.get("performance"))
            if performance:
                item["performance"] = performance
            public_provenance = {"members": [{key: member[key] for key in ("id", "name")} for member in provenance["members"]],
                                 "policy_case_name": provenance["policy_case_name"]}
            _remember(_RUN_SUMMARIES, trace_file, (signature, metadata["case_id"], deepcopy(item), public_provenance))
            result["runs"].append(item)
            if directory.name == metadata.get("latest_run"):
                result.update(seeds=deepcopy(state["seeds"]), seed_count=len(state["seeds"]),
                              members=[{key: member[key] for key in ("id", "name")} for member in provenance["members"]],
                              policy_case_name=provenance["policy_case_name"], latest=item)
        result["runs"].sort(key=lambda item: (item.get("created_at") or "", item["id"]), reverse=True)
        result["runs"] = result["runs"][:100]
        if result.get("latest_run") and "latest" not in result:
            raise TraceError("The latest shared collection is unavailable")
    except (TraceError, OSError, ValueError, KeyError, TypeError):
        result.update(compatible=False, reason="Shared collection is unavailable or uses a different blockchain or API source.")
    return result
