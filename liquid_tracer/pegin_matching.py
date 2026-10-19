"""Exact Bitcoin outpoint -> Liquid peg-in input matching, with resumable discovery.

Discovery uses Esplora's native-asset history, not amount/address heuristics.
The shared index is only a hint: every returned claim is fetched and checked
again. It never asserts allocation from a claim input to an individual output.
"""
from contextlib import contextmanager, nullcontext
import csv
import fcntl
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from .api import Esplora, Limits
from .common import HEX64, LBTC, StopRun, TraceError, canonical, digest, now, read_json, save_json
from .inspection import transaction_outputs
from .investigation_deletion import operation_guard
from .investigations import read_case
from .networks import blockchain, default_api
from .progress import report_progress
from .store import Store

REPORT_ID = re.compile(r"[0-9a-f]{32}\Z")
FILES = frozenset({"match.json", "matches.csv", "liquid-outputs.csv", "evidence.json", "SHA256SUMS"})
HISTORY = "/asset/" + LBTC + "/txs/chain"
NOTICE = ("An exact peg-in input reference links a Bitcoin output to a Liquid claim transaction. "
          "The listed Liquid outputs belong to that transaction; individual input-to-output value allocation "
          "and ownership are not established. No match in searched data does not prove that a deposit "
          "was never claimed. Only confirmed Liquid native-asset history is searched; mempool discovery "
          "requires a known Liquid transaction ID.")


def _hash(value, label):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise TraceError(label + " must be a 64-character transaction hash")
    return value.lower()


def query(txid, vout=None, liquid_txid=None, max_pages=100, *, start_minutes=None, end_minutes=None,
          start_hours=None, end_hours=None, vouts=None):
    txid = _hash(txid, "Bitcoin transaction")
    if vout is not None and (type(vout) is not int or not 0 <= vout < 0x40000000):
        raise TraceError("Bitcoin vout must be a non-negative output index")
    if vouts is not None:
        if vout is not None:
            raise TraceError("Choose a Bitcoin vout or selected vouts, not both")
        if (not isinstance(vouts, list) or len(vouts) > 10000
                or any(type(value) is not int or not 0 <= value < 0x40000000 for value in vouts)):
            raise TraceError("Selected Bitcoin outputs must be a list of non-negative output indexes")
        vouts = sorted(set(vouts))
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise TraceError("Choose from 1 to 1000 history pages per task")
    window = {}
    minutes = start_minutes is not None or end_minutes is not None
    hours = start_hours is not None or end_hours is not None
    if minutes and hours:
        raise TraceError("Choose search window minutes or legacy hours, not both")
    if minutes:
        if any(type(v) not in (int, float) or not -5256000 <= v <= 5256000 or not math.isfinite(v)
               for v in (start_minutes, end_minutes)) or end_minutes <= start_minutes:
            raise TraceError("Search window minutes must be finite, within -5256000 to 5256000, with end after start")
        window = {"start_minutes": start_minutes, "end_minutes": end_minutes}
    elif hours:
        if any(type(v) not in (int, float) or not -87600 <= v <= 87600 or not math.isfinite(v)
               for v in (start_hours, end_hours)) or end_hours <= start_hours:
            raise TraceError("Search window hours must be finite, within -87600 to 87600, with end after start")
        window = {"start_hours": start_hours, "end_hours": end_hours}
    return {**window, "bitcoin_txid": txid, "bitcoin_vout": vout,
            "bitcoin_vouts": vouts if vouts is not None else ([] if vout is None else [vout]),
            "liquid_txid": _hash(liquid_txid, "Liquid claim") if liquid_txid is not None else None,
            "max_pages": max_pages}


def batch_query(txids, max_pages=100, *, resume_report=None, **window):
    if not isinstance(txids, (list, tuple)) or not 1 <= len(txids) <= 500:
        raise TraceError("Choose from 1 to 500 Bitcoin transaction hashes")
    hashes = list(dict.fromkeys(_hash(value, "Bitcoin transaction") for value in txids))
    if resume_report is not None and (not isinstance(resume_report, str) or not REPORT_ID.fullmatch(resume_report)):
        raise TraceError("Invalid peg-in batch report to continue")
    return {**query(hashes[0], max_pages=max_pages, **window), "mode": "batch",
            "bitcoin_txid": None, "bitcoin_txids": hashes,
            **({"resume_report": resume_report} if resume_report is not None else {})}


def reverse_query(liquid_txid, max_pages=100, **window):
    liquid_txid = _hash(liquid_txid, "Liquid claim")
    return {**query("0" * 64, liquid_txid=liquid_txid, max_pages=max_pages, **window), "mode": "reverse",
            "bitcoin_txid": None, "bitcoin_txids": []}


def _ordinary(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise TraceError("Peg-in matching paths cannot contain symbolic links")
    if path.is_file() and path.stat().st_nlink != 1:
        raise TraceError("Peg-in matching files cannot be hard links")
    return path


@contextmanager
def _index(root):
    root = _ordinary(root)
    root.mkdir(parents=True, exist_ok=True)
    with _ordinary(root / "match.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another Bitcoin-to-Liquid search is updating this shared index") from None
        for suffix in ("", "-journal", "-wal", "-shm"):
            _ordinary(root / ("index.sqlite" + suffix))
        db = sqlite3.connect(root / "index.sqlite")
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS claims(
                    bitcoin_txid TEXT NOT NULL, bitcoin_vout INTEGER NOT NULL,
                    liquid_txid TEXT NOT NULL, vin INTEGER NOT NULL,
                    PRIMARY KEY(liquid_txid, vin));
                CREATE INDEX IF NOT EXISTS claims_bitcoin ON claims(bitcoin_txid, bitcoin_vout);
                CREATE TABLE IF NOT EXISTS scan(id INTEGER PRIMARY KEY, document TEXT NOT NULL);
            """)
            row = db.execute("SELECT document FROM scan WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else {
                "head": None, "cursor": None, "exhausted": False, "pages": 0, "transactions": 0,
                "refresh": None, "refresh_ready": False}
            yield db, state
        finally:
            db.close()


def _inputs(transaction):
    txid = _hash(transaction.get("txid"), "Liquid transaction")
    if not isinstance(transaction.get("vin"), list):
        raise TraceError("Liquid explorer response has invalid inputs")
    result = []
    for number, vin in enumerate(transaction["vin"]):
        if not isinstance(vin, dict):
            raise TraceError("Liquid explorer response has an invalid input")
        if "is_pegin" in vin and type(vin["is_pegin"]) is not bool:
            raise TraceError("Liquid input has an invalid peg-in flag")
        if vin.get("is_pegin") is not True:
            continue
        btc = _hash(vin.get("txid"), "Peg-in Bitcoin reference")
        index = vin.get("vout")
        if type(index) is not int or not 0 <= index < 0x40000000 or vin.get("is_coinbase"):
            raise TraceError("Liquid peg-in input has an invalid Bitcoin outpoint")
        result.append((btc, index, txid, number))
    return result


def _status(transaction):
    status = transaction.get("status")
    if not isinstance(status, dict) or type(status.get("confirmed")) is not bool:
        raise TraceError("Explorer response has no valid confirmation status")
    result = {"confirmed": status["confirmed"]}
    if status["confirmed"]:
        if type(status.get("block_height")) is not int or status["block_height"] < 0:
            raise TraceError("Confirmed transaction has no valid block height")
        result.update(block_height=status["block_height"], block_hash=_hash(status.get("block_hash"), "Block"))
        if type(status.get("block_time")) is int:
            result["block_time"] = status["block_time"]
    return result


def _history_page(api, endpoint):
    # Keep memory bounded to one history page, rather than retaining every
    # downloaded transaction body in Esplora's per-run coalescing cache.
    collected = []
    api.prefetch([endpoint], retain_results=False, on_result=lambda _key, value: collected.append(value))
    if len(collected) != 1:
        raise TraceError("Liquid history request did not return a response")
    value = collected[0]
    if isinstance(value, BaseException):
        raise value
    return value


def _page(api, db, state, cursor, refresh=False):
    endpoint = HISTORY + ("/" + _hash(cursor, "History cursor") if cursor else "")
    values, _ = _history_page(api, endpoint)
    if not isinstance(values, list) or len(values) > 25:
        raise TraceError("Liquid asset history returned an invalid page")
    ids, claims = [], []
    for transaction in values:
        if not isinstance(transaction, dict) or not _status(transaction)["confirmed"]:
            raise TraceError("Confirmed Liquid history contains an invalid transaction")
        txid = _hash(transaction.get("txid"), "Liquid transaction")
        if txid in ids or txid == cursor:
            raise TraceError("Liquid history pagination did not advance")
        ids.append(txid)
        claims.extend(_inputs(transaction))
    with db:
        for item in claims:
            db.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?,?)", item)
        state["pages"] += 1
        state["transactions"] += len(values)
        if refresh:
            pending = state["refresh"]
            if pending["head"] is None and ids:
                pending["head"] = ids[0]
            done = not ids or pending["anchor"] in ids
            if done:
                state["head"] = pending["head"] or state["head"]
                # If the old head vanished in a reorg, this refresh walked to
                # the historical end. Fresh claim checks still reject stale hints.
                if not ids:
                    state["exhausted"] = True
                state["refresh"] = None
                state["refresh_ready"] = True
            else:
                pending["cursor"] = ids[-1]
        elif ids:
            state["head"] = state["head"] or ids[0]
            state["cursor"] = ids[-1]
            state["refresh_ready"] = False
        else:
            state["exhausted"] = True
        db.execute("INSERT OR REPLACE INTO scan VALUES(1,?)", (json.dumps(state),))
    return len(values)


def _observation(store, oid):
    row, = store.observations([oid])
    return {"observation_id": oid, "source": row["source"], "endpoint": row["endpoint"],
            "fetched_at": row["fetched_at"], "sha256": row["sha256"], "response": json.loads(row["body"])}


def _claims(api, store, bitcoin, spec, candidates, evidence, *, window=None):
    for candidate in sorted(set(candidates)):
        candidate = _hash(candidate, "Liquid claim")
        transaction, oid = api.get("/tx/" + candidate)
        if not isinstance(transaction, dict) or _hash(transaction.get("txid"), "Liquid transaction") != candidate:
            raise TraceError("Liquid transaction response does not match the requested hash")
        inputs = _inputs(transaction)
        selected_vouts = spec.get("bitcoin_vouts") or ([] if spec["bitcoin_vout"] is None else [spec["bitcoin_vout"]])
        selected = [item for item in inputs if item[0] == spec["bitcoin_txid"]
                    and (not selected_vouts or item[1] in selected_vouts)]
        evidence[candidate] = _observation(store, oid)
        if not selected:
            yield [], {}, {candidate}
            continue
        status = _status(transaction)
        if window is not None and (not status["confirmed"] or type(status.get("block_time")) is not int
                                   or not window[0] <= status["block_time"] <= window[1]):
            yield [], {}, {candidate}
            continue
        report = transaction_outputs(candidate, transaction, blockchain="liquid")
        matches = []
        for btc, vout, liquid, vin in selected:
            if vout >= len(bitcoin["outputs"]):
                raise TraceError("Liquid claim refers to a missing Bitcoin output")
            deposit = bitcoin["outputs"][vout]
            if not deposit["selectable"] or type(deposit["value"]) is not int:
                raise TraceError("Liquid claim refers to an invalid Bitcoin deposit")
            matches.append({"bitcoin_txid": btc, "bitcoin_vout": vout, "bitcoin_outpoint": f"{btc}:{vout}",
                "bitcoin_address": deposit["address"], "bitcoin_value_sats": deposit["value"],
                "liquid_txid": liquid, "liquid_vin": vin, "liquid_status": status,
                "verification": "confirmed_reference" if bitcoin["status"]["confirmed"] and status["confirmed"]
                                else "unconfirmed_reference",
                "observation_id": oid})
            if type(bitcoin["status"].get("block_time")) is int and type(status.get("block_time")) is int:
                matches[-1]["elapsed_seconds"] = status["block_time"] - bitcoin["status"]["block_time"]
        # Commit each verified candidate before requesting the next one, so a
        # later request limit or cancellation cannot discard completed matches.
        yield matches, {candidate: report["outputs"]}, {candidate}


def _csv(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _save(case, result, evidence):
    folder = _ordinary(Path(case) / "pegin-matches" / result["id"])
    folder.mkdir(parents=True, exist_ok=True)
    save_json(_ordinary(folder / "match.json"), result)
    save_json(_ordinary(folder / "evidence.json"), evidence)
    fields = ["Bitcoin transaction", "Bitcoin vout", "Bitcoin address", "Bitcoin deposit value (sats)",
              "Liquid claim transaction", "Liquid vin", "Verification", "Liquid block height",
              "Bitcoin block timestamp (Unix)", "Liquid block timestamp (Unix)", "Block-time delay (seconds)"]
    rows = [dict(zip(fields, [item["bitcoin_txid"], item["bitcoin_vout"], item["bitcoin_address"],
            item["bitcoin_value_sats"], item["liquid_txid"], item["liquid_vin"], item["verification"],
            item["liquid_status"].get("block_height", ""),
            (item.get("bitcoin_status") or (result.get("bitcoins", {}).get(item["bitcoin_txid"])
             or result.get("bitcoin") or {}).get("status", {})).get("block_time", ""),
            item["liquid_status"].get("block_time", ""), item.get("elapsed_seconds", "")])) for item in result["matches"]]
    fields += ["Liquid transaction", "Status", "Reason"]
    for row in rows:
        row.update({"Liquid transaction": row["Liquid claim transaction"], "Status": "matched",
                    "Reason": "exact_reference_found"})
    for item in result.get("results", []):
        if item["status"] == "matched":
            continue
        rows.append({"Bitcoin transaction": item.get("bitcoin_txid") or "",
                     "Bitcoin vout": item.get("bitcoin_vout") if item.get("bitcoin_vout") is not None else "",
                     "Liquid claim transaction": item.get("liquid_txid") or "",
                     "Liquid transaction": item.get("liquid_txid") or "",
                     "Liquid vin": item.get("liquid_vin") if item.get("liquid_vin") is not None else "",
                     "Status": item["status"], "Reason": item.get("stop_reason") or ""})
    _ordinary(folder / "matches.csv").write_text(_csv(rows, fields), encoding="utf-8")
    fields = ["Liquid claim transaction", "Liquid vout", "Liquid outpoint", "Liquid address",
              "Public value (base units)", "Public asset", "Output kind", "Allocation"]
    rows = [dict(zip(fields, [txid, item["vout"], item["outpoint"], item["address"], item["value"],
            item["asset"], item["reason"] or "spendable", "Not determined"]))
            for txid, outputs in result["liquid_outputs"].items() for item in outputs]
    _ordinary(folder / "liquid-outputs.csv").write_text(_csv(rows, fields), encoding="utf-8")
    _ordinary(folder / "SHA256SUMS").write_text("".join(
        digest((folder / name).read_bytes()) + "  " + name + "\n"
        for name in sorted(FILES - {"SHA256SUMS"})), encoding="ascii")


def match_pegin(case, txid, *, workspace=None, vout=None, vouts=None, liquid_txid=None, max_pages=100,
                max_requests=1200, max_seconds=300, public=False,
                bitcoin_fixture=None, liquid_fixture=None, progress=None,
                start_minutes=None, end_minutes=None, start_hours=None, end_hours=None,
                _context=None, _persist=True, _page_limit=None):
    spec = query(txid, vout, liquid_txid, max_pages, start_minutes=start_minutes, end_minutes=end_minutes,
                 start_hours=start_hours, end_hours=end_hours, vouts=vouts)
    selected_vouts = spec["bitcoin_vouts"]
    if _page_limit is not None:
        max_pages = _page_limit
    spec["public"] = public
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise TraceError("Peg-in matching requires a request limit from 1 to 10000")
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
        raise TraceError("Peg-in matching requires a time limit up to 3600 seconds")
    owner = _report_owner(case, workspace)
    case = _ordinary(Path(case).expanduser()).resolve() if case is not None else None
    workspace = _ordinary(Path(workspace).expanduser()).resolve() if workspace is not None else case.parent
    metadata = read_case(case) if case is not None else {}
    if case is not None and blockchain(metadata) != "bitcoin":
        raise TraceError("Legacy investigation matching requires a Bitcoin investigation")
    # Fixture cases must never fall through to live endpoints.
    if metadata.get("fixture") and not (bitcoin_fixture and liquid_fixture):
        raise TraceError("Synthetic matching requires both Bitcoin and Liquid fixtures")
    if bool(bitcoin_fixture) != bool(liquid_fixture):
        raise TraceError("Provide both Bitcoin and Liquid fixtures")
    bases = [default_api(chain).replace("enterprise.blockstream.info", "blockstream.info")
             if public else default_api(chain) for chain in ("bitcoin", "liquid")]
    sources = [("fixture://" + digest(canonical(read_json(path)))) if path else base
               for path, base in zip((bitcoin_fixture, liquid_fixture), bases)]
    index_root = _ordinary(workspace / ".pegin-index" / digest(canonical(sources))[:24])
    result = {"schema_version": 1, "id": uuid.uuid4().hex, "case_id": metadata.get("case_id"),
              "scope": "investigation" if case is not None else "workspace",
              "created_at": now(), "query": spec, "networks": "bitcoin-mainnet/liquid-mainnet",
              "sources": {"bitcoin": sources[0], "liquid": sources[1]},
              "status": "searching", "stop_reason": None, "matches": [], "liquid_outputs": {},
              "bitcoin": None, "coverage": {}, "notice": NOTICE}
    evidence = {}
    with operation_guard(case), (nullcontext(_context["index"]) if _context else _index(index_root)) as (db, state):
        store = _context["store"] if _context else Store(index_root)
        try:
            limits = Limits(max_hops=0, max_requests=max_requests, max_seconds=max_seconds)
            limits.validate()
            with (nullcontext(_context["bitcoin"]) if _context else
                  Esplora(store, result["id"], limits, base=bases[0], auth="none" if public else "blockstream",
                         fixture=bitcoin_fixture, blockchain="bitcoin", tx_cache_seconds=0)) as btc_api, \
                 (nullcontext(_context["liquid"]) if _context else
                  Esplora(store, result["id"], limits, base=bases[1], auth="none" if public else "blockstream",
                         fixture=liquid_fixture, blockchain="liquid", tx_cache_seconds=0)) as liquid_api:
                liquid_api.budget = btc_api.budget  # One finite budget, including auth and retries.
                first_request = btc_api.budget.requests
                pages = 0
                checked = set()
                discovery = None
                try:
                    report_progress(progress, "pegin_bitcoin")
                    transaction, oid = btc_api.get("/tx/" + spec["bitcoin_txid"])
                    bitcoin = transaction_outputs(spec["bitcoin_txid"], transaction, blockchain="bitcoin")
                    bitcoin["status"] = _status(transaction)
                    if any(index >= len(bitcoin["outputs"]) for index in selected_vouts):
                        raise TraceError("Bitcoin vout is outside this transaction")
                    result["bitcoin"] = bitcoin
                    evidence["bitcoin"] = _observation(store, oid)
                    target = spec["bitcoin_txid"]
                    if ("start_minutes" in spec or "start_hours" in spec) and not spec["liquid_txid"]:
                        from .pegin_window import WindowDiscovery
                        start = spec["start_minutes"] / 60 if "start_minutes" in spec else spec["start_hours"]
                        end = spec["end_minutes"] / 60 if "end_minutes" in spec else spec["end_hours"]
                        discovery = WindowDiscovery(liquid_api, db, state, HISTORY, _history_page,
                            _inputs, _status, progress, max_pages, bitcoin["status"], start, end)
                    while True:
                        if spec["liquid_txid"]:
                            candidates = [spec["liquid_txid"]]
                        else:
                            candidates = [row[0] for row in db.execute(
                                "SELECT DISTINCT liquid_txid FROM claims WHERE bitcoin_txid=?"
                                + (" AND bitcoin_vout=?" if vout is not None else ""),
                                (target, vout) if vout is not None else (target,))]
                        pending = set(candidates) - checked
                        if pending:
                            report_progress(progress, "pegin_verify", pages, max_pages)
                            for matches, outputs, verified in _claims(liquid_api, store, bitcoin, spec, pending, evidence,
                                    window=(discovery.window["start_time"], discovery.window["end_time"]) if discovery else None):
                                checked.update(verified)
                                result["matches"].extend(matches)
                                result["liquid_outputs"].update(outputs)
                                with db:
                                    db.executemany("INSERT OR REPLACE INTO claims VALUES(?,?,?,?)",
                                        [(m["bitcoin_txid"], m["bitcoin_vout"], m["liquid_txid"], m["liquid_vin"])
                                         for m in matches])
                        matched_vouts = {item["bitcoin_vout"] for item in result["matches"]}
                        if result["matches"] and (not selected_vouts or set(selected_vouts) <= matched_vouts):
                            result.update(status="matched", stop_reason="exact_reference_found")
                            break
                        if spec["liquid_txid"]:
                            result.update(status="partial" if result["matches"] else "no_match",
                                          stop_reason="candidate_does_not_reference_all_outputs" if result["matches"]
                                          else "candidate_does_not_reference_deposit")
                            break
                        if discovery is not None:
                            if not discovery.step():
                                result.update(status="partial" if result["matches"] else "no_match", stop_reason=("window_range_complete"
                                    if discovery.coverage()["complete"] else "window_not_yet_elapsed"))
                                break
                            pages = discovery.pages
                            continue
                        # Legacy Python callers without a window retain their existing
                        # history checkpoint. CLI and HTTP always specify a date window.
                        # Refresh all pages back to the previous head before
                        # extending the older cursor. A capped refresh resumes.
                        if (pages == 0 and (state["head"] or state["exhausted"]) and not state["refresh"]
                                and (state["exhausted"] or not state.get("refresh_ready"))):
                            state["refresh"] = {"anchor": state["head"], "cursor": None, "head": None}
                        # A one-page budget can finish only the refresh. Persist
                        # that fact so the next task advances into older history.
                        refresh = state["refresh"] is not None
                        if state["exhausted"] and not refresh:
                            result.update(status="partial" if result["matches"] else "no_match", stop_reason="history_exhausted")
                            break
                        if pages >= max_pages:
                            result.update(status="paused", stop_reason="page_limit")
                            break
                        report_progress(progress, "pegin_history", pages, max_pages)
                        cursor = state["refresh"]["cursor"] if refresh else state["cursor"]
                        _page(liquid_api, db, state, cursor, refresh=refresh)
                        pages += 1
                except StopRun as error:
                    result.update(status="paused", stop_reason=str(error))
                except KeyboardInterrupt:
                    result.update(status="cancelled", stop_reason="cancelled")
                    raise
                except Exception:
                    result.update(status="error", stop_reason="lookup_failed")
                    raise
                finally:
                    result["coverage"] = {"pages_this_task": pages, "history_pages_processed": state["pages"],
                        "history_entries_processed": state["transactions"], "history_exhausted": state["exhausted"],
                        "refresh_pending": state["refresh"] is not None, "history_cursor": state["cursor"],
                        "searched_mempool": False, "requests_this_task": btc_api.budget.requests - first_request,
                        "requested_outpoint_matched": bool(selected_vouts) and set(selected_vouts) <=
                            {item["bitcoin_vout"] for item in result["matches"]},
                        "matched_vouts": sorted({item["bitcoin_vout"] for item in result["matches"]}),
                        "unmatched_vouts": sorted(set(selected_vouts) - {item["bitcoin_vout"] for item in result["matches"]})}
                    if discovery is not None:
                        result["coverage"]["window"] = discovery.coverage()
                        result["coverage"]["pages_this_task"] = discovery.pages
                        result["coverage"]["history_exhausted"] = False
                        result["coverage"]["refresh_pending"] = False
                        result["coverage"]["history_cursor"] = None
                    result["finished_at"] = now()
                    if _context is not None:
                        _context["last_result"], _context["last_evidence"] = result, evidence
                    if _persist:
                        _save(owner, result, evidence)
        finally:
            if _context is None:
                store.close()
    report_progress(progress, "pegin_saved")
    return result


def _report_owner(case, workspace=None):
    if (case is None) == (workspace is None):
        raise TraceError("Choose a workspace or a legacy investigation, not both")
    if workspace is not None:
        return _ordinary(Path(workspace).expanduser() / ".pegin-matching")
    return _ordinary(Path(case).expanduser())


def match_pegin_batch(workspace, txids, **options):
    from .pegin_batch import match_pegin_batch as run
    return run(workspace, txids, **options)


def match_liquid_pegin(workspace, liquid_txid, **options):
    from .pegin_reverse import match_liquid_pegin as run
    return run(workspace, liquid_txid, **options)


def report_directory(case, identity, *, workspace=None, report_root=None):
    if not isinstance(identity, str) or not REPORT_ID.fullmatch(identity):
        raise TraceError("Invalid peg-in match identifier")
    if report_root is not None and (case is not None or workspace is not None):
        raise TraceError("Choose one report owner")
    owner = _ordinary(Path(report_root)) if report_root is not None else _report_owner(case, workspace)
    return _ordinary(owner / "pegin-matches" / identity)


def verified_file(case, identity, name, *, workspace=None, report_root=None):
    if name not in FILES:
        raise TraceError("Unknown peg-in matching download")
    folder = report_directory(case, identity, workspace=workspace, report_root=report_root)
    try:
        lines = _ordinary(folder / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        expected = {}
        for line in lines:
            checksum, filename = line.split("  ", 1)
            if filename not in FILES - {"SHA256SUMS"} or filename in expected or not HEX64.fullmatch(checksum):
                raise ValueError
            expected[filename] = checksum
        if set(expected) != FILES - {"SHA256SUMS"}:
            raise ValueError
        for filename, checksum in expected.items():
            if digest(_ordinary(folder / filename).read_bytes()) != checksum:
                raise ValueError
        return _ordinary(folder / name)
    except (OSError, ValueError):
        raise TraceError("Peg-in matching files are incomplete or their checksums changed") from None


def read_report(case, identity, *, workspace=None, report_root=None):
    result = read_json(verified_file(case, identity, "match.json", workspace=workspace, report_root=report_root))
    expected_case = read_case(case)["case_id"] if case is not None else None
    if (result.get("schema_version") != 1 or result.get("id") != identity
            or result.get("case_id") != expected_case
            or ((workspace is not None or report_root is not None) and result.get("scope") != "workspace")):
        raise TraceError("Peg-in report does not belong to the selected workspace or investigation")
    return result


def list_reports(case=None, *, workspace=None):
    root = _ordinary(_report_owner(case, workspace) / "pegin-matches")
    if not root.exists():
        return []
    folders = sorted((p for p in root.iterdir() if REPORT_ID.fullmatch(p.name) and not p.is_symlink()),
                     key=lambda p: p.stat().st_mtime_ns, reverse=True)
    reports = []
    for folder in folders[:50]:
        try:
            value = read_report(case, folder.name, workspace=workspace)
        except TraceError:
            continue
        reports.append({key: value[key] for key in ("id", "created_at", "query", "status", "stop_reason", "coverage")}
                       | {"match_count": len(value["matches"])})
    return reports
