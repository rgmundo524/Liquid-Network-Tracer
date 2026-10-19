"""Small loopback API adapter for worker-owned peg-in matching."""
import math
from pathlib import Path
import uuid
from urllib.parse import quote

from .common import HEX64, TraceError
from .networks import blockchain
from .pegin_matching import FILES, REPORT_ID, _ordinary, query, batch_query, reverse_query, read_report, list_reports, verified_file
from .investigations import read_case


def public_report(case, identity, *, workspace=None):
    report = read_report(case, identity, workspace=workspace)
    # Preserve integer base units through JavaScript, even beyond 2**53.
    for outputs in report["liquid_outputs"].values():
        for output in outputs:
            if type(output.get("value")) is int:
                output["value"] = str(output["value"])
    for match in report["matches"]:
        match["bitcoin_value_sats"] = str(match["bitcoin_value_sats"])
    bitcoins = list(report.get("bitcoins", {}).values())
    bitcoins.extend(report.get("liquids", {}).values())
    if report.get("bitcoin") is not None:
        bitcoins.append(report["bitcoin"])
    for transaction in bitcoins:
        for output in transaction.get("outputs", []):
            if type(output.get("value")) is int:
                output["value"] = str(output["value"])
    prefix = ("/files/" + report["case_id"] + "/pegin-matches/" if case is not None
              else "/files/pegin-matches/") + identity + "/"
    return {**report, "downloads": [{"name": name, "url": prefix + quote(name)}
                                   for name in sorted(FILES)]}


def _match_query(body):
    if not isinstance(body, dict):
        raise TraceError("Choose a matching mode and transaction hashes")
    mode = body.get("mode", "single")
    if not isinstance(mode, str) or mode not in ("single", "batch", "reverse"):
        raise TraceError("Choose single, batch, or reverse matching")
    identifiers = {"single": {"txid", "vout", "vouts", "liquid_txid"},
                   "batch": {"txids", "resume_report"}, "reverse": {"liquid_txid"}}[mode]
    required = {"action"} | ({"txid", "max_pages"} if mode == "single" else
                              {"txids"} if mode == "batch" else {"liquid_txid"})
    common = {"mode", "max_pages", "max_requests", "max_seconds", "public",
              "start_minutes", "end_minutes", "start_hours", "end_hours"}
    if (not required <= set(body) or set(body) - identifiers - common - {"action"}
            or body["action"] != "pegin-match"):
        raise TraceError("Choose only the transaction identifiers and bounds for this matching mode")
    if "vout" in body and "vouts" in body:
        raise TraceError("Choose one output index or a list of output indexes, not both")
    if "vouts" in body and not isinstance(body["vouts"], list):
        raise TraceError("Bitcoin output indexes must be a list")
    if mode == "batch" and not isinstance(body["txids"], list):
        raise TraceError("Batch Bitcoin transaction hashes must be a list")
    if "resume_report" in body and body["resume_report"] is None:
        raise TraceError("Choose a saved batch report identifier")
    public = body.get("public", True)
    if type(public) is not bool:
        raise TraceError("Public API selection must be true or false")
    minutes = bool({"start_minutes", "end_minutes"} & set(body))
    hours = bool({"start_hours", "end_hours"} & set(body))
    if minutes and hours:
        raise TraceError("Choose search window minutes or legacy hours, not both")
    unit, defaults = ("hours", (0, 72)) if hours else ("minutes", (-5, 4320))
    window = {"start_" + unit: body.get("start_" + unit, defaults[0]),
              "end_" + unit: body.get("end_" + unit, defaults[1])}
    if any(value is None for value in window.values()):
        raise TraceError("Choose numeric start and end " + unit + " for the search window")
    if mode == "batch":
        spec = batch_query(body["txids"], body.get("max_pages", 100),
                           resume_report=body.get("resume_report"), **window)
    elif mode == "reverse":
        spec = reverse_query(body["liquid_txid"], body.get("max_pages", 100), **window)
    else:
        spec = query(body["txid"], body.get("vout"), body.get("liquid_txid"), body["max_pages"],
                     vouts=body.get("vouts"), **window)
    if "max_requests" in body:
        requests = body["max_requests"]
        if type(requests) is not int or not 1 <= requests <= 10000:
            raise TraceError("Peg-in matching requires a request limit from 1 to 10000")
        spec["max_requests"] = requests
    if "max_seconds" in body:
        seconds = body["max_seconds"]
        if type(seconds) not in (int, float) or not 0 < seconds <= 3600 or not math.isfinite(seconds):
            raise TraceError("Peg-in matching requires a time limit up to 3600 seconds")
        spec["max_seconds"] = seconds
    return spec, public


def _search_arguments(spec, public):
    unit = "minutes" if "start_minutes" in spec else "hours"
    mode = spec.get("mode", "single")
    arguments = (["--txids", ",".join(spec["bitcoin_txids"])] if mode == "batch" else
                 ["--from-liquid", spec["liquid_txid"]] if mode == "reverse" else
                 ["--txid", spec["bitcoin_txid"]])
    arguments.extend(["--max-pages", str(spec["max_pages"]),
                      "--start-" + unit, str(spec["start_" + unit]),
                      "--end-" + unit, str(spec["end_" + unit])])
    if public:
        arguments.append("--public")
    if spec["bitcoin_vout"] is not None:
        arguments.extend(["--vout", str(spec["bitcoin_vout"])])
    elif spec.get("bitcoin_vouts"):
        arguments.extend(["--vouts", ",".join(map(str, spec["bitcoin_vouts"]))])
    if mode == "single" and spec["liquid_txid"]:
        arguments.extend(["--liquid-txid", spec["liquid_txid"]])
    if spec.get("resume_report"):
        arguments.extend(["--resume-report", spec["resume_report"]])
    for limit in ("max_requests", "max_seconds"):
        if limit in spec:
            arguments.extend(["--" + limit.replace("_", "-"), str(spec[limit])])
    return arguments


def start_match(server, case, metadata, body):
    if blockchain(metadata) != "bitcoin":
        raise TraceError("Choose a Bitcoin investigation for peg-in matching")
    spec, public = _match_query(body)
    if spec.get("mode", "single") != "single":
        raise TraceError("Batch and reverse matching are workspace tools; choose a single transaction for an investigation")
    arguments = ["pegin-match", "--case", str(case), *_search_arguments(spec, public)]
    # Admission is fast. All database, history, and output verification runs
    # after the visible job exists, outside the server's job lock.
    return server.start_job(arguments, action="pegin-match", live=not bool(metadata.get("fixture")), case=case)


def workspace_reports(server):
    """Read older case reports in place without moving or rewriting evidence."""
    reports = list_reports(workspace=server.root)
    for case in server.case_paths():
        if not (case / "pegin-matches").is_dir():
            continue
        try:
            metadata = read_case(case)
            reports.extend({**item, "source_case_id": metadata["case_id"],
                            "source_case_name": metadata.get("name") or case.name}
                           for item in list_reports(case))
        except (TraceError, OSError, ValueError):
            continue
    return sorted(reports, key=lambda row: (row["created_at"], row["id"]), reverse=True)[:50]


def start_workspace_match(server, body):
    if isinstance(body, dict) and ("bridge" in body or "network" in body):
        return start_bridge_match(server, body)
    spec, public = _match_query(body)
    arguments = ["pegin-match", "--investigations-dir", str(server.root),
                 *_search_arguments(spec, public)]
    return server.start_job(arguments, action="pegin-match", live=True)


def start_workspace_outputs(server, body):
    """Queue a bounded Bitcoin output lookup without reading credentials or files."""
    if not isinstance(body, dict):
        raise TraceError("Choose a transaction to load its outputs")
    if "bridge" in body or "network" in body:
        if set(body) - {"txid", "public", "bridge", "network"}:
            raise TraceError("Choose only a bridge, source network, transaction, and API mode")
        from .bridge_matching import bridge_query
        spec = bridge_query(bridge=body.get("bridge", "pegin"), network=body.get("network", "bitcoin"), txid=body.get("txid"))
        public = body.get("public", True)
        if type(public) is not bool:
            raise TraceError("Public API selection must be true or false")
        arguments = ["bridge-outputs", "--txid", spec["txid"], "--bridge", spec["bridge"], "--network", spec["network"]]
        if public:
            arguments.append("--public")
        return server.start_job(arguments, action="pegin-outputs", live=True, txids=[spec["txid"]])
    if set(body) - {"txid", "public"} or "txid" not in body:
        raise TraceError("Choose a Bitcoin transaction to load its outputs")
    txid = body["txid"]
    if not isinstance(txid, str) or not HEX64.fullmatch(txid):
        raise TraceError("Bitcoin transaction must be a 64-character transaction hash")
    public = body.get("public", True)
    if type(public) is not bool:
        raise TraceError("Public API selection must be true or false")
    txid = txid.lower()
    arguments = ["inspect-txs", "--txids", txid, "--blockchain", "bitcoin"]
    if public:
        arguments.extend(["--auth", "none", "--base-url", "https://blockstream.info/api"])
    return server.start_job(arguments, action="pegin-outputs", live=True, txids=[txid])


def start_bridge_match(server, body):
    from .bridge_matching import bridge_query
    allowed = {"action", "bridge", "network", "mode", "txid", "txids", "vout", "vouts", "public",
               "max_pages", "max_requests", "max_seconds", "start_minutes", "end_minutes", "resume_report"}
    if set(body) - allowed or body.get("action") != "pegin-match":
        raise TraceError("Choose only bridge matching inputs and bounded search options")
    if any(body.get(key) is None for key in ("start_minutes", "end_minutes") if key in body):
        raise TraceError("Choose numeric search offsets in minutes")
    if "vout" in body and "vouts" in body:
        raise TraceError("Choose one output index or a list of output indexes, not both")
    if "resume_report" in body and body["resume_report"] is None:
        raise TraceError("Choose the current session report to continue")
    public = body.get("public", True)
    if type(public) is not bool:
        raise TraceError("Public API selection must be true or false")
    spec = bridge_query(**{key: value for key, value in body.items() if key in allowed - {
        "action", "public", "max_requests", "max_seconds"}})
    for name, maximum in (("max_requests", 10000), ("max_seconds", 3600)):
        if name not in body:
            continue
        value = body[name]
        types = (int,) if name == "max_requests" else (int, float)
        if type(value) not in types or not 0 < value <= maximum or not math.isfinite(value):
            raise TraceError("Choose a valid bounded bridge " + name.replace("_", " "))
    identity = uuid.uuid4().hex
    arguments = ["bridge-match", "--investigations-dir", str(server.root),
                 "--report-root", str(server.bridge_reports_root), "--report-id", identity,
                 "--bridge", spec["bridge"], "--network", spec["network"], "--mode", spec["mode"],
                 *( ["--txids", ",".join(spec["txids"])] if spec["mode"] == "batch" else ["--txid", spec["txid"]]),
                 "--max-pages", str(spec["max_pages"]), "--start-minutes", str(spec["start_minutes"]),
                 "--end-minutes", str(spec["end_minutes"])]
    if spec["vouts"]:
        arguments += ["--vouts", ",".join(map(str, spec["vouts"]))]
    if public:
        arguments.append("--public")
    if spec.get("resume_report"):
        arguments += ["--resume-report", spec["resume_report"]]
    for name in ("max_requests", "max_seconds"):
        if name in body:
            arguments += ["--" + name.replace("_", "-"), str(body[name])]
    return server.start_job(arguments, action="pegin-match", live=True)


def report_workspace(server, identity):
    if not isinstance(identity, str) or not REPORT_ID.fullmatch(identity):
        raise TraceError("Invalid bridge report identifier")
    session = getattr(server, "bridge_workspace", None)
    if session is not None and _ordinary(Path(session) / ".pegin-matching" / "pegin-matches" / identity).exists():
        return session
    return server.root


def workspace_report(server, identity):
    return public_report(None, identity, workspace=report_workspace(server, identity))


def workspace_download(server, identity, name):
    return verified_file(None, identity, name, workspace=report_workspace(server, identity))
