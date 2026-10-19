"""Session bridge workflows using bounded shared clients and explicit report owners."""
from pathlib import Path
from tempfile import TemporaryDirectory

from .api import Esplora, Limits
from .common import StopRun, TraceError, now, read_json
from .inspection import transaction_outputs
from .pegin_matching import REPORT_ID, _hash, query, match_pegin, read_report, verified_file
from .pegin_batch import completed_status, result_row, save_progress, workspace_session
from .pegin_reverse import match_liquid_pegin
from .progress import report_progress
from .store import Store


def bridge_query(*, bridge="pegin", network="bitcoin", mode="single", txid=None, txids=None,
                 vout=None, vouts=None, max_pages=100, start_minutes=None, end_minutes=None, resume_report=None):
    """Only inexpensive in-memory validation belongs in HTTP admission."""
    if bridge not in ("pegin", "pegout") or network not in ("bitcoin", "liquid"):
        raise TraceError("Choose peg-in or peg-out and a Bitcoin or Liquid source")
    if mode not in ("single", "batch"):
        raise TraceError("Choose single transaction or CSV batch matching")
    if mode == "single":
        if txids is not None:
            raise TraceError("Choose one transaction hash for a single search")
        hashes = [_hash(txid, "Source transaction")]
    else:
        if txid is not None or not isinstance(txids, (list, tuple)) or not 1 <= len(txids) <= 500:
            raise TraceError("Choose from 1 to 500 transaction hashes for a batch")
        hashes = list(dict.fromkeys(_hash(value, "Source transaction") for value in txids))
    defaults = (-4320, 5) if bridge == "pegout" and network == "bitcoin" else (-5, 4320)
    start_minutes = defaults[0] if start_minutes is None else start_minutes
    end_minutes = defaults[1] if end_minutes is None else end_minutes
    checked = query(hashes[0], vout=vout, vouts=vouts, max_pages=max_pages,
                    start_minutes=start_minutes, end_minutes=end_minutes)
    if mode == "batch" and checked["bitcoin_vouts"]:
        raise TraceError("Choose individual outputs only for a single transaction")
    if resume_report is not None and (not isinstance(resume_report, str) or not REPORT_ID.fullmatch(resume_report)):
        raise TraceError("Choose the current session report to continue")
    if bridge == "pegin" and network == "liquid" and checked["bitcoin_vouts"]:
        raise TraceError("Liquid claim outputs do not identify particular Bitcoin deposit inputs")
    return {"bridge": bridge, "network": network, "mode": mode,
            **({"txid": hashes[0]} if mode == "single" else {"txids": hashes}),
            "vouts": checked["bitcoin_vouts"], "max_pages": max_pages,
            "start_minutes": start_minutes, "end_minutes": end_minutes,
            **({"resume_report": resume_report} if resume_report is not None else {})}


def carry_bridge_completed(owner, identity, result, evidence):
    """Continue only the same session query, with explicitly dated prior rows."""
    previous = read_report(None, identity, report_root=owner)
    first, second = previous["query"], result["query"]
    keys = ("bridge", "network", "mode", "txid", "txids", "vouts", "public", "start_minutes", "end_minutes")
    if (previous["sources"] != result["sources"]
            or any(first.get(key) != second.get(key) for key in keys)):
        raise TraceError("Continue with the original bridge, source hashes, outputs, API mode, and time window")
    completed = {row["source_txid"]: row for row in previous["results"]
        if row["status"] in ("matched", "candidate", "ambiguous", "no_match")
        and row["stop_reason"] != "window_not_yet_elapsed"}
    for row in result["results"]:
        if row["source_txid"] in completed:
            row.update(completed[row["source_txid"]], carried_from_report=identity)
            row.setdefault("checked_at", previous.get("finished_at", previous["created_at"]))
    source_key = first["network"] + "_txid"
    result["matches"] = [match for match in previous["matches"] if match[source_key] in completed]
    # Extra historical observations remain available for provenance. Newly
    # retried rows replace their inspections; each match keeps its observation ID.
    for key in ("bitcoins", "liquids", "liquid_outputs", "liquid_output_observation_ids"):
        if key in previous:
            result[key] = previous[key]
    old_evidence = read_json(verified_file(None, identity, "evidence.json", report_root=owner))
    for key, value in old_evidence.items():
        if isinstance(value, dict):
            evidence.setdefault(key, {}).update(value)
    result["continued_from_report"] = identity


def _merge_child(result, evidence, child, child_evidence, source, network):
    observations = ({source: child_evidence["bitcoin"]} if network == "bitcoin" and "bitcoin" in child_evidence
                    else child_evidence.get("bitcoin", {}) if network == "liquid" else {})
    for match in child.get("matches", []):
        observation = observations.get(match["bitcoin_txid"])
        if observation:
            match["bitcoin_observation_id"] = observation["observation_id"]
            match["bitcoin_status"] = dict(observation["response"].get("status", {}))
    evidence.setdefault("bitcoin_checks", {}).update({str(value["observation_id"]): value
                                                     for value in observations.values()})
    if child.get("bitcoin"):
        result["bitcoins"][source] = child["bitcoin"]
        if result["query"]["mode"] == "single":
            result["bitcoin"] = child["bitcoin"]
    result["bitcoins"].update(child.get("bitcoins", {}))
    result["matches"].extend(child.get("matches", []))
    result["liquid_outputs"].update(child.get("liquid_outputs", {}))
    if network == "bitcoin":
        if "bitcoin" in child_evidence:
            evidence["bitcoin"][source] = child_evidence["bitcoin"]
        checks = {key: value for key, value in child_evidence.items() if key != "bitcoin"}
        evidence["liquid"].update(checks)
        evidence["liquid_checks"].update({str(value["observation_id"]): value for value in checks.values()})
        result["liquid_output_observation_ids"].update({key: checks[key]["observation_id"]
            for key in child.get("liquid_outputs", {}) if key in checks})
    else:
        for key in ("bitcoin", "liquid", "liquid_checks"):
            evidence[key].update(child_evidence.get(key, {}))
        result["liquid_output_observation_ids"].update(child.get("liquid_output_observation_ids", {}))


def match_bridge(workspace, *, bridge="pegin", network="bitcoin", mode="single", txid=None, txids=None,
                 vout=None, vouts=None, max_pages=100, max_requests=1200, max_seconds=300, public=False,
                 start_minutes=None, end_minutes=None, bitcoin_fixture=None, liquid_fixture=None,
                 progress=None, report_root=None, report_id=None, resume_report=None):
    spec = bridge_query(bridge=bridge, network=network, mode=mode, txid=txid, txids=txids,
                        vout=vout, vouts=vouts, max_pages=max_pages,
                        start_minutes=start_minutes, end_minutes=end_minutes, resume_report=resume_report)
    hashes = [spec["txid"]] if mode == "single" else spec["txids"]
    options = dict(max_pages=max_pages, max_requests=max_requests, max_seconds=max_seconds,
                   public=public, start_minutes=spec["start_minutes"], end_minutes=spec["end_minutes"],
                   bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture, progress=progress)
    if bridge == "pegout":
        from .pegout_matching import match_pegout
        return match_pegout(workspace, hashes[0] if mode == "single" else hashes,
                            source_network=network, vouts=spec["vouts"],
                            report_root=report_root, report_id=report_id, resume_report=resume_report, **options)
    # Keep the established report fields available to legacy readers while the
    # active UI uses the explicit bridge/network/source identifiers above.
    spec.update(bitcoin_txid=hashes[0] if mode == "single" and network == "bitcoin" else None,
                bitcoin_txids=hashes if network == "bitcoin" else [],
                bitcoin_vout=spec["vouts"][0] if len(spec["vouts"]) == 1 else None,
                bitcoin_vouts=spec["vouts"], liquid_txid=hashes[0] if mode == "single" and network == "liquid" else None)
    with workspace_session(workspace, spec, max_requests=max_requests, max_seconds=max_seconds,
                           public=public, bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture,
                           report_root=report_root, report_id=report_id) as (owner, result, evidence, context):
        result["results"] = [{**result_row(source if network == "bitcoin" else None,
            liquid_txid=source if network == "liquid" else None), "source_txid": source} for source in hashes]
        if resume_report is not None:
            carry_bridge_completed(owner, resume_report, result, evidence)
        row = result["results"][0]
        pages = 0
        save_progress(owner, result, evidence, context)
        try:
            for row in result["results"]:
                if row.get("carried_from_report"):
                    continue
                context["bitcoin"].budget.check_request()
                report_progress(progress, "bridge_batch", sum(item["status"] != "unprocessed" for item in result["results"]), len(hashes))
                context.pop("last_result", None)
                context.pop("last_evidence", None)
                interrupted = False
                try:
                    if network == "bitcoin":
                        child = match_pegin(None, row["source_txid"], workspace=workspace, vouts=spec["vouts"],
                            _context=context, _persist=False, _page_limit=max_pages-pages, **options)
                    else:
                        child = match_liquid_pegin(workspace, row["source_txid"], _context=context, _persist=False, **options)
                except KeyboardInterrupt:
                    interrupted = True
                    child = context.get("last_result", {"status": "cancelled", "stop_reason": "cancelled"})
                except Exception:
                    child = context.get("last_result", {"status": "error", "stop_reason": "lookup_failed"})
                row.update(status=child["status"], stop_reason=child["stop_reason"],
                           match_count=len(child.get("matches", [])), coverage=child.get("coverage", {}), checked_at=now())
                pages += row["coverage"].get("pages_this_task", 0)
                _merge_child(result, evidence, child, context.get("last_evidence", {}), row["source_txid"], network)
                save_progress(owner, result, evidence, context, pages)
                if interrupted:
                    raise KeyboardInterrupt
                if row["status"] == "paused":
                    raise StopRun(row["stop_reason"])
            result.update(status=completed_status(result), stop_reason="batch_complete" if mode == "batch" else "bridge_search_complete")
        except StopRun as error:
            if row["status"] == "unprocessed":
                row.update(status="paused", stop_reason=str(error))
            result.update(status="paused", stop_reason=str(error))
        except KeyboardInterrupt:
            if row["status"] == "unprocessed":
                row.update(status="cancelled", stop_reason="cancelled")
            result.update(status="cancelled", stop_reason="cancelled")
            raise
        finally:
            for pending in result["results"]:
                if pending["status"] == "unprocessed":
                    pending["stop_reason"] = "not_processed_after_" + (result.get("stop_reason") or "interruption")
            if network == "liquid":
                result["query"]["bitcoin_txids"] = list(result["bitcoins"])
            save_progress(owner, result, evidence, context, pages)
    report_progress(progress, "bridge_ready")
    return result


def inspect_bridge_outputs(txid, *, bridge="pegin", network="bitcoin", public=False, fixture=None,
                           max_requests=5, max_seconds=30):
    spec = bridge_query(bridge=bridge, network=network, txid=txid)
    txid = spec["txid"]
    from .networks import default_api
    base = default_api(network)
    if public:
        base = base.replace("enterprise.blockstream.info", "blockstream.info")
    limits = Limits(max_hops=0, max_transactions=1, max_requests=max_requests, max_seconds=max_seconds)
    limits.validate()
    with TemporaryDirectory(prefix="liquid-bridge-output-") as directory:
        store = Store(directory)
        try:
            with Esplora(store, "bridge-output", limits, base=base, auth="none" if public else "blockstream",
                         fixture=fixture, tx_cache_seconds=0, blockchain=network) as api:
                transaction, _oid = api.get("/tx/" + txid)
                report = transaction_outputs(txid, transaction, blockchain=network)
                if network == "liquid":
                    if bridge == "pegout":
                        from .pegout_matching import pegout_outputs
                        eligible = pegout_outputs(transaction)
                        for output in report["outputs"]:
                            details = eligible.get(output["vout"])
                            output.update(selectable=details is not None, is_pegout=details is not None,
                                          reason="pegout" if details is not None else "not_a_valid_pegout")
                            if details is not None:
                                output.update(pegout_scriptpubkey=details["scriptpubkey"],
                                              pegout_address=details.get("address"))
                    else:
                        for output in report["outputs"]:
                            output.update(selectable=False, reason="Claim outputs do not allocate Bitcoin peg-in inputs")
                return {"bridge": bridge, "blockchain": network, "transactions": [report]}
        finally:
            store.close()
