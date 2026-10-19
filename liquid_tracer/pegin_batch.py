"""Workspace peg-in batches sharing one explorer budget and verified index."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import math
import uuid

from .api import Esplora, Limits
from .common import StopRun, TraceError, canonical, digest, now, read_json
from .networks import default_api
from .pegin_matching import (NOTICE, REPORT_ID, _index, _ordinary, _report_owner, _save,
                             batch_query, match_pegin, read_report, verified_file)
from .progress import report_progress
from .store import Store


@contextmanager
def workspace_session(workspace, spec, *, max_requests, max_seconds, public,
                      bitcoin_fixture, liquid_fixture, report_root=None, report_id=None, _context=None):
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise TraceError("Peg-in matching requires a request limit from 1 to 10000")
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 3600):
        raise TraceError("Peg-in matching requires a time limit up to 3600 seconds")
    if type(public) is not bool:
        raise TraceError("Public API selection must be true or false")
    if bool(bitcoin_fixture) != bool(liquid_fixture):
        raise TraceError("Provide both Bitcoin and Liquid fixtures")
    owner = _ordinary(Path(report_root)) if report_root is not None else _report_owner(None, workspace)
    if report_id is not None and (not isinstance(report_id, str) or not REPORT_ID.fullmatch(report_id)):
        raise TraceError("Invalid bridge report identifier")
    identity = report_id or uuid.uuid4().hex
    if report_id is not None and _ordinary(owner / "pegin-matches" / identity).exists():
        raise TraceError("Bridge report already exists; choose a new report identifier")
    workspace = _ordinary(Path(workspace).expanduser()).resolve()
    bases = [default_api(chain).replace("enterprise.blockstream.info", "blockstream.info")
             if public else default_api(chain) for chain in ("bitcoin", "liquid")]
    sources = [("fixture://" + digest(canonical(read_json(path)))) if path else base
               for path, base in zip((bitcoin_fixture, liquid_fixture), bases)]
    index_root = _ordinary(workspace / ".pegin-index" / digest(canonical(sources))[:24])
    result = {"schema_version": 1, "id": identity, "case_id": None, "scope": "workspace",
              "created_at": now(), "query": {**spec, "public": public},
              "networks": "bitcoin-mainnet/liquid-mainnet", "sources": dict(zip(("bitcoin", "liquid"), sources)),
              "status": "searching", "stop_reason": None, "matches": [], "liquid_outputs": {},
              "liquid_output_observation_ids": {},
              "bitcoin": None, "bitcoins": {}, "results": [], "coverage": {}, "notice": NOTICE}
    evidence = {"bitcoin": {}, "liquid": {}, "liquid_checks": {}}
    limits = Limits(max_hops=0, max_requests=max_requests, max_seconds=max_seconds)
    limits.validate()
    with (nullcontext(_context["index"]) if _context else _index(index_root)) as index:
        store = _context["store"] if _context else Store(index_root)
        try:
            with (nullcontext(_context["bitcoin"]) if _context else
                  Esplora(store, result["id"], limits, base=bases[0], auth="none" if public else "blockstream",
                         fixture=bitcoin_fixture, blockchain="bitcoin", tx_cache_seconds=0)) as btc, \
                 (nullcontext(_context["liquid"]) if _context else
                  Esplora(store, result["id"], limits, base=bases[1], auth="none" if public else "blockstream",
                         fixture=liquid_fixture, blockchain="liquid", tx_cache_seconds=0)) as liquid:
                liquid.budget = btc.budget
                yield owner, result, evidence, (_context if _context is not None else
                    {"index": index, "store": store, "bitcoin": btc, "liquid": liquid})
        finally:
            if _context is None:
                store.close()


def result_row(bitcoin_txid=None, *, bitcoin_vout=None, liquid_txid=None, liquid_vin=None):
    return {"bitcoin_txid": bitcoin_txid, "bitcoin_vout": bitcoin_vout,
            "liquid_txid": liquid_txid, "liquid_vin": liquid_vin, "status": "unprocessed",
            "stop_reason": "not_processed", "match_count": 0, "coverage": {}}


def save_progress(owner, result, evidence, context, pages=0, *, persist=True):
    result["coverage"] = {"pages_this_task": pages, "requests_this_task": context["bitcoin"].budget.requests,
                          "processed_transactions": sum(row["status"] != "unprocessed" for row in result["results"]),
                          "total_transactions": len(result["results"]), "searched_mempool": False}
    result["finished_at"] = now()
    if persist:
        _save(owner, result, evidence)
    else:
        context["last_result"], context["last_evidence"] = result, evidence


def completed_status(result):
    statuses = {row["status"] for row in result["results"]}
    if statuses == {"matched"}:
        return "matched"
    if result["matches"]:
        return "partial"
    return "error" if "error" in statuses else "no_match"


def carry_completed(workspace, identity, result, evidence):
    """Carry dated verified evidence, never silently rerun completed batch rows."""
    previous = read_report(None, identity, workspace=workspace)
    old, new = previous["query"], result["query"]
    fields = ("bitcoin_txids", "public")
    def offsets(spec):
        if "start_minutes" in spec:
            return spec["start_minutes"], spec["end_minutes"]
        if "start_hours" in spec:
            return spec["start_hours"] * 60, spec["end_hours"] * 60
        return None
    first, second = offsets(old), offsets(new)
    same_window = first == second or (first is not None and second is not None
        and all(math.isclose(a, b, rel_tol=1e-15, abs_tol=1e-9) for a, b in zip(first, second)))
    if (old.get("mode") != "batch" or previous["sources"] != result["sources"]
            or not same_window or any(old.get(key) != new.get(key) for key in fields)):
        raise TraceError("Continue a batch with its original transactions, API mode, and search window")
    old_evidence = read_json(verified_file(None, identity, "evidence.json", workspace=workspace))
    completed = {row["bitcoin_txid"]: row for row in previous.get("results", [])
                 if row["status"] == "matched" or (row["status"] == "no_match"
                     and row["stop_reason"] in ("window_range_complete", "history_exhausted"))}
    for row in result["results"]:
        if row["bitcoin_txid"] in completed:
            row.update(completed[row["bitcoin_txid"]], carried_from_report=identity)
            row.setdefault("checked_at", previous.get("finished_at", previous["created_at"]))
    result["matches"] = [item for item in previous["matches"] if item["bitcoin_txid"] in completed]
    liquid_hashes = {item["liquid_txid"] for item in result["matches"]}
    result["bitcoins"] = {key: value for key, value in previous.get("bitcoins", {}).items() if key in completed}
    result["liquid_outputs"] = {key: value for key, value in previous["liquid_outputs"].items() if key in liquid_hashes}
    result["liquid_output_observation_ids"] = {key: value for key, value in
        previous.get("liquid_output_observation_ids", {}).items() if key in liquid_hashes}
    evidence["bitcoin"] = {key: value for key, value in old_evidence.get("bitcoin", {}).items() if key in completed}
    evidence["liquid"] = {key: value for key, value in old_evidence.get("liquid", {}).items() if key in liquid_hashes}
    # One immutable transaction can be checked in several tasks, with distinct
    # confirmation observations. Keep every match's dated verification body.
    required = {str(item["observation_id"]) for item in result["matches"]}
    required.update(str(value) for value in result["liquid_output_observation_ids"].values())
    checks = {str(value["observation_id"]): value for value in old_evidence.get("liquid", {}).values()}
    checks.update(old_evidence.get("liquid_checks", {}))
    if not required <= checks.keys():
        raise TraceError("Saved batch is missing a completed match's verification evidence")
    evidence["liquid_checks"] = {key: value for key, value in checks.items() if key in required}
    result["continued_from_report"] = identity


def match_pegin_batch(workspace, txids, *, max_pages=100, max_requests=1200, max_seconds=300,
                      public=False, bitcoin_fixture=None, liquid_fixture=None, progress=None,
                      start_minutes=None, end_minutes=None, start_hours=None, end_hours=None, resume_report=None):
    window = dict(start_minutes=start_minutes, end_minutes=end_minutes, start_hours=start_hours, end_hours=end_hours)
    spec = batch_query(txids, max_pages, resume_report=resume_report, **window)
    options = dict(max_pages=max_pages, max_requests=max_requests, max_seconds=max_seconds, public=public,
                   bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture, progress=progress, **window)
    with workspace_session(workspace, spec, max_requests=max_requests, max_seconds=max_seconds, public=public,
                           bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture) as (owner, result, evidence, context):
        result["results"] = [result_row(txid) for txid in spec["bitcoin_txids"]]
        if resume_report is not None:
            carry_completed(workspace, resume_report, result, evidence)
        pages = 0
        save_progress(owner, result, evidence, context)
        try:
            for row in result["results"]:
                if row.get("carried_from_report"):
                    continue
                context["bitcoin"].budget.check_request()
                report_progress(progress, "pegin_batch",
                                sum(item["status"] != "unprocessed" for item in result["results"]), len(result["results"]))
                context.pop("last_result", None)
                context.pop("last_evidence", None)
                cancelled = False
                try:
                    child = match_pegin(None, row["bitcoin_txid"], workspace=workspace, _context=context,
                                        _persist=False, _page_limit=max_pages - pages, **options)
                except KeyboardInterrupt:
                    cancelled = True
                    child = context.get("last_result", {"status": "cancelled", "stop_reason": "cancelled"})
                except Exception:
                    child = context.get("last_result", {"status": "error", "stop_reason": "lookup_failed"})
                child_evidence = context.get("last_evidence", {})
                row.update(status=child["status"], stop_reason=child["stop_reason"],
                           match_count=len(child.get("matches", [])), coverage=child.get("coverage", {}), checked_at=now())
                pages += row["coverage"].get("pages_this_task", 0)
                if child.get("bitcoin"):
                    result["bitcoins"][row["bitcoin_txid"]] = child["bitcoin"]
                result["matches"].extend(child.get("matches", []))
                result["liquid_outputs"].update(child.get("liquid_outputs", {}))
                if "bitcoin" in child_evidence:
                    evidence["bitcoin"][row["bitcoin_txid"]] = child_evidence["bitcoin"]
                evidence["liquid"].update({key: value for key, value in child_evidence.items() if key != "bitcoin"})
                evidence["liquid_checks"].update({str(value["observation_id"]): value
                    for key, value in child_evidence.items() if key != "bitcoin"})
                result["liquid_output_observation_ids"].update({key: child_evidence[key]["observation_id"]
                    for key in child.get("liquid_outputs", {}) if key in child_evidence})
                save_progress(owner, result, evidence, context, pages)
                if cancelled:
                    raise KeyboardInterrupt
                if row["status"] == "paused":
                    raise StopRun(row["stop_reason"])
            result.update(status=completed_status(result), stop_reason="batch_complete")
        except StopRun as error:
            result.update(status="paused", stop_reason=str(error))
            for pending in result["results"]:
                if pending["status"] == "unprocessed":
                    pending["stop_reason"] = "not_processed_after_" + str(error)
        except KeyboardInterrupt:
            result.update(status="cancelled", stop_reason="cancelled")
            for pending in result["results"]:
                if pending["status"] == "unprocessed":
                    pending["stop_reason"] = "not_processed_after_cancellation"
            raise
        finally:
            save_progress(owner, result, evidence, context, pages)
    report_progress(progress, "pegin_saved")
    return result
