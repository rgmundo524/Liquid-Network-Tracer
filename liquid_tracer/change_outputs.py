"""Explicit investigator-designated change outputs, independent of trace evidence."""

import copy
import fcntl
from pathlib import Path
from urllib.parse import urlsplit

from .common import HEX64, TraceError, canonical, digest, now, read_json, save_json
from .investigations import read_case
from .services import _text, load_services

NOTICE = ("Change is an investigator designation, not a claim proved by the blockchain. "
          "One output per transaction may be selected. This saves local layout preferences only; "
          "no trace or Miro sync starts. Regenerate a preview or use Sync and reorganize to apply them. "
          "Transactions without a designation keep the normal ELK layout.")
MAX_VOUT = 2**32 - 1


def validate_txid(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value.strip()):
        raise TraceError("Transaction hash must contain exactly 64 hexadecimal characters, without :vout")
    return value.strip().lower()


def vout_value(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_VOUT:
        raise TraceError("Change vout must be a whole number from 0 to 4294967295, or null to clear")
    return value


def notes_value(value):
    return _text(value, "Notes", 4000, multiline=True)


def validate_change_outputs(mapping):
    if not isinstance(mapping, dict):
        raise TraceError("Invalid saved change outputs; restore services.json")
    for txid, record in mapping.items():
        if (validate_txid(txid) != txid or not isinstance(record, dict)
                or set(record) != {"vout", "notes", "updated_at"}
                or record.get("vout") is None or vout_value(record["vout"]) != record["vout"]
                or notes_value(record.get("notes")) != record["notes"]
                or _text(record.get("updated_at"), "Updated at", 80, required=True) != record["updated_at"]):
            raise TraceError("Invalid saved change output; restore services.json")
    return mapping


def _saved_state(case):
    """Read only the explicitly selected, checksum-verified latest snapshot."""
    from .cli import resolve_latest, run_path, verify_export
    case = Path(case)
    metadata = read_case(case)
    if not metadata.get("latest_run"):
        return None
    selected = resolve_latest(case, "latest")
    archive = run_path(case, selected)
    for candidate in (case / "runs", archive, archive / "trace.json", archive / "SHA256SUMS"):
        if candidate.is_symlink():
            raise TraceError("Change output lookup requires ordinary saved run files")
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if (not isinstance(state, dict) or state.get("case_id") != metadata["case_id"]
            or state.get("run_id") != selected or not isinstance(state.get("transactions"), dict)):
        raise TraceError("Saved trace does not match this investigation")
    return state


def _saved_outputs(state, txid):
    if state is None or txid not in state["transactions"]:
        return None
    from .inspection import transaction_outputs
    record = state["transactions"][txid]
    if not isinstance(record, dict):
        raise TraceError("Saved transaction has an invalid record")
    return transaction_outputs(txid, record.get("data"))


def check_known_output(state, txid, vout):
    """Unknown transactions may be designated before they appear in the graph."""
    if vout is None:
        return
    report = _saved_outputs(state, txid)
    if report is None:
        return
    if vout >= len(report["outputs"]):
        raise TraceError("Change vout does not exist in the saved transaction")
    if not report["outputs"][vout]["selectable"]:
        raise TraceError("Change must be a spendable output, not a fee, pegout, or unspendable output")


def catalog(case, query="", offset=0, limit=100):
    if (not isinstance(query, str) or len(query) > 256 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100):
        raise TraceError("Choose a short transaction search and a page of 1 to 100 change outputs")
    settings = load_services(case)
    mapping = settings.get("change_outputs", {})
    query = query.strip().lower()
    keys = sorted(txid for txid in mapping if query in txid)
    return {"revision": settings["revision"], "total": len(keys), "offset": offset, "limit": limit,
            "rows": [{"txid": txid, **mapping[txid]} for txid in keys[offset:offset + limit]], "notice": NOTICE}


def set_change_output(case, txid, vout, notes="", expected_revision=None):
    txid, vout, notes = validate_txid(txid), vout_value(vout), notes_value(notes)
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise TraceError("Refresh change outputs before saving")
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; assign change after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            if expected_revision is not None and settings["revision"] != expected_revision:
                raise TraceError("Investigation settings changed; refresh change outputs before saving")
            check_known_output(_saved_state(case), txid, vout)
            before = settings.get("change_outputs", {})
            previous = before.get(txid)
            changed = int((previous is not None) if vout is None else
                          previous is None or previous["vout"] != vout or previous["notes"] != notes)
            if changed:
                stamp = now()
                after = copy.deepcopy(before)
                if vout is None:
                    after.pop(txid, None)
                else:
                    after[txid] = {"vout": vout, "notes": notes, "updated_at": stamp}
                settings["change_outputs"] = after
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": stamp,
                    "type": "change_outputs", "previous": copy.deepcopy(before), "change_outputs": copy.deepcopy(after)})
                save_json(case / "services.json", settings)
            return {"changed": changed, "revision": settings["revision"], "txid": txid,
                    "vout": vout, "notes": notes if vout is not None else "", "notice": NOTICE}


def _lookup_options(case, state):
    from .api import ENTERPRISE
    metadata = read_case(case)
    source = state.get("source") if state is not None else None
    if state is not None and (not isinstance(source, str) or not source):
        raise TraceError("Saved transaction source is invalid; restore the original evidence")
    fixture = metadata.get("fixture")
    if fixture:
        if not Path(fixture).is_file():
            raise TraceError("The saved synthetic fixture is unavailable")
        fixture_source = "fixture://" + digest(canonical(read_json(fixture)))
        if source and fixture_source != source:
            raise TraceError("Transaction lookup source does not match the investigation's saved source")
    elif source and source.startswith("fixture://"):
        raise TraceError("The original synthetic fixture is required for this transaction lookup")
    base = source if source and not source.startswith("fixture://") else ENTERPRISE
    auth = "blockstream" if urlsplit(base).hostname == "enterprise.blockstream.info" else "none"
    return {"fixture": fixture, "base_url": base, "auth": auth}


def lookup_requires_network(case, txid):
    txid = validate_txid(txid)
    state = _saved_state(case)
    if _saved_outputs(state, txid) is not None:
        return False
    return not bool(_lookup_options(case, state)["fixture"])


def transaction_lookup(case, txid):
    """Inspect local verified evidence first, otherwise one bounded case-source lookup."""
    from .inspection import inspect_transaction
    case, txid = Path(case), validate_txid(txid)
    with (case / "trace.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; look up change after it finishes") from None
        state = _saved_state(case)
        report = _saved_outputs(state, txid)
        if report is None:
            report = inspect_transaction(txid, **_lookup_options(case, state), max_requests=5, max_seconds=30)
        settings = load_services(case)
        current = settings.get("change_outputs", {}).get(txid, {})
        return {**report, "current_vout": current.get("vout"), "current_notes": current.get("notes", ""),
                "revision": settings["revision"], "notice": NOTICE}
