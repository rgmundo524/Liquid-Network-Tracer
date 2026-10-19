"""Verify the explicit Bitcoin references of one Liquid claim without discovery."""
from .common import StopRun, TraceError
from .inspection import transaction_outputs
from .pegin_batch import completed_status, result_row, save_progress, workspace_session
from .pegin_matching import _claims, _hash, _inputs, _observation, _status, query, reverse_query
from .progress import report_progress


def match_liquid_pegin(workspace, liquid_txid, *, max_pages=100, max_requests=1200, max_seconds=300,
                       public=False, bitcoin_fixture=None, liquid_fixture=None, progress=None,
                       start_minutes=None, end_minutes=None, start_hours=None, end_hours=None,
                       _context=None, _persist=True):
    spec = reverse_query(liquid_txid, max_pages, start_minutes=start_minutes, end_minutes=end_minutes,
                         start_hours=start_hours, end_hours=end_hours)
    with workspace_session(workspace, spec, max_requests=max_requests, max_seconds=max_seconds, public=public,
                           bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture, _context=_context) as (owner, result, evidence, context):
        btc_api, liquid_api, store = context["bitcoin"], context["liquid"], context["store"]
        result["results"] = [result_row(liquid_txid=spec["liquid_txid"])]
        save_progress(owner, result, evidence, context, persist=_persist)
        row = result["results"][0]
        try:
            report_progress(progress, "pegin_reverse")
            transaction, oid = liquid_api.get("/tx/" + spec["liquid_txid"])
            if not isinstance(transaction, dict) or _hash(transaction.get("txid"), "Liquid transaction") != spec["liquid_txid"]:
                raise TraceError("Liquid transaction response does not match the requested hash")
            inputs = _inputs(transaction)
            _status(transaction)
            result["liquid_outputs"][spec["liquid_txid"]] = transaction_outputs(
                spec["liquid_txid"], transaction, blockchain="liquid")["outputs"]
            result["liquid_output_observation_ids"][spec["liquid_txid"]] = oid
            evidence["liquid"][spec["liquid_txid"]] = _observation(store, oid)
            evidence["liquid_checks"] = {str(oid): evidence["liquid"][spec["liquid_txid"]]}
            result["query"]["bitcoin_txids"] = list(dict.fromkeys(item[0] for item in inputs))
            if not inputs:
                row.update(status="no_match", stop_reason="liquid_transaction_has_no_pegin_inputs")
                result.update(status="no_match", stop_reason="liquid_transaction_has_no_pegin_inputs")
            else:
                result["results"] = [result_row(btc, bitcoin_vout=vout, liquid_txid=liquid, liquid_vin=vin)
                                     for btc, vout, liquid, vin in inputs]
                save_progress(owner, result, evidence, context, persist=_persist)
                for row in result["results"]:
                    btc_api.budget.check()
                    collected = {}
                    try:
                        target = row["bitcoin_txid"]
                        if target not in result["bitcoins"]:
                            report_progress(progress, "pegin_bitcoin")
                            bitcoin, bid = btc_api.get("/tx/" + target)
                            report = transaction_outputs(target, bitcoin, blockchain="bitcoin")
                            report["status"] = _status(bitcoin)
                            result["bitcoins"][target] = report
                            evidence["bitcoin"][target] = _observation(store, bid)
                        report = result["bitcoins"][target]
                        one = query(target, row["bitcoin_vout"], spec["liquid_txid"], max_pages)
                        for matches, outputs, _checked in _claims(liquid_api, store, report, one,
                                                                 [spec["liquid_txid"]], collected):
                            # Duplicate inputs referencing one outpoint still retain their distinct vin.
                            matches = [match for match in matches if match["liquid_vin"] == row["liquid_vin"]]
                            result["matches"].extend(matches)
                            result["liquid_outputs"].update(outputs)
                            result["liquid_output_observation_ids"].update({key: collected[key]["observation_id"]
                                for key in outputs if key in collected})
                            row.update(status="matched" if matches else "no_match",
                                       stop_reason="exact_reference_found" if matches else "liquid_input_no_longer_references_outpoint",
                                       match_count=len(matches))
                            db, _state = context["index"]
                            with db:
                                db.executemany("INSERT OR REPLACE INTO claims VALUES(?,?,?,?)", [
                                    (item["bitcoin_txid"], item["bitcoin_vout"], item["liquid_txid"], item["liquid_vin"])
                                    for item in matches])
                    except StopRun:
                        raise
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        row.update(status="error", stop_reason="bitcoin_outpoint_verification_failed")
                    finally:
                        evidence["liquid"].update(collected)
                        evidence["liquid_checks"].update({str(item["observation_id"]): item for item in collected.values()})
                    save_progress(owner, result, evidence, context, persist=_persist)
                result.update(status=completed_status(result), stop_reason="reverse_verification_complete")
        except StopRun as error:
            row.update(status="paused", stop_reason=str(error))
            result.update(status="paused", stop_reason=str(error))
            for pending in result["results"]:
                if pending["status"] == "unprocessed":
                    pending["stop_reason"] = "not_processed_after_" + str(error)
        except KeyboardInterrupt:
            row.update(status="cancelled", stop_reason="cancelled")
            result.update(status="cancelled", stop_reason="cancelled")
            for pending in result["results"]:
                if pending["status"] == "unprocessed":
                    pending["stop_reason"] = "not_processed_after_cancellation"
            raise
        except Exception:
            row.update(status="error", stop_reason="liquid_claim_lookup_failed")
            result.update(status="error", stop_reason="liquid_claim_lookup_failed")
        finally:
            save_progress(owner, result, evidence, context, persist=_persist)
    report_progress(progress, "pegin_saved")
    return result
