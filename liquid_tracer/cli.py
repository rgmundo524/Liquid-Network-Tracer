import argparse
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .api import ENTERPRISE, Esplora, Limits
from .common import TraceError, digest, load_labels, parse_outpoint, read_json, save_json
from .export import export_run
from .miro import publish, resolve, sync
from .store import Store
from .trace import new_state, trace


def parser():
    case_default = os.environ.get("LIQUID_CASE_DIR") or None
    board_default = os.environ.get("LIQUID_MIRO_BOARD") or None
    root = argparse.ArgumentParser(description="Bounded Liquid UTXO reachability with saved evidence and Miro export")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("trace", help="Start or extend a bounded run")
    run.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                     help="Case directory (default: LIQUID_CASE_DIR)")
    run.add_argument("--seed", action="append", default=[], help="Liquid txid:vout; may be repeated")
    run.add_argument("--seeds-file", type=Path, help="One txid:vout per line; # comments allowed")
    run.add_argument("--resume", help="Prior run ID or latest in this case; extends its saved frontier")
    run.add_argument("--only", action="append", help="Resume only these frontier outpoints; may be repeated")
    run.add_argument("--hops", type=int, help="Absolute maximum hop depth (default: 3)")
    run.add_argument("--additional-hops", type=int, help="Increase the resumed run's hop ceiling by this many")
    run.add_argument("--max-transactions", type=int, default=250, help="New unique transactions per run, including seed funding transactions")
    run.add_argument("--max-outpoints", type=int, default=2000)
    run.add_argument("--max-requests", type=int, default=600, help="All HTTP attempts, including OAuth and retries")
    run.add_argument("--max-seconds", type=float, default=300)
    run.add_argument("--base-url", default=ENTERPRISE)
    run.add_argument("--auth", choices=["blockstream", "none"], default="blockstream")
    run.add_argument("--fixture", type=Path, help="Offline synthetic API responses; no network calls")
    run.add_argument("--labels", type=Path)
    run.add_argument("--include-unconfirmed", action="store_true")
    run.add_argument("--tx-cache-seconds", type=float, default=86400)
    run.add_argument("--min-interval", type=float, default=.25)
    run.add_argument("--merge-addresses", action="store_true", default=None, help="Merge circles by address; continuation otherwise inherits its parent's mode")
    run.add_argument("--offline-preview", action="store_true", help="Also save optional HTML/SVG inspection files")
    run.add_argument("--miro-board", help="Sync the saved run to this Miro board URL or ID")
    run.add_argument("--max-new-items", type=int, default=750, help="Maximum new Miro shapes plus connectors")
    export = commands.add_parser("export", help="Regenerate a run export without network calls")
    export.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                        help="Case directory (default: LIQUID_CASE_DIR)")
    export.add_argument("--run", required=True, help="Saved run ID or latest")
    export.add_argument("--out", type=Path, required=True, help="New export directory")
    export.add_argument("--merge-addresses", action="store_true", default=None)
    export.add_argument("--offline-preview", action="store_true")
    update = commands.add_parser("miro-sync", help="Add a saved run to the existing case graph, preserving manual edits")
    update.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                        help="Case directory (default: LIQUID_CASE_DIR)")
    update.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    update.add_argument("--board", default=board_default, required=board_default is None,
                        help="Miro board URL or board ID (default: LIQUID_MIRO_BOARD)")
    update.add_argument("--plan", type=Path, help="Use a regenerated miro-plan.json for this run")
    update.add_argument("--dry-run", action="store_true", help="Preview local new/mapped counts without network access or writes")
    update.add_argument("--max-new-items", type=int, default=750)
    miro = commands.add_parser("miro-publish", help="Legacy: create a separate snapshot; use miro-sync for cumulative graphs")
    miro.add_argument("--plan", type=Path, required=True)
    miro.add_argument("--board-id", required=True)
    miro.add_argument("--state", type=Path, required=True, help="Persistent local publication state")
    miro.add_argument("--max-items", type=int, default=750)
    reconcile = commands.add_parser("miro-resolve", help="Resolve a POST with uncertain outcome after inspecting the board")
    reconcile.add_argument("--state", type=Path, required=True)
    group = reconcile.add_mutually_exclusive_group(required=True)
    group.add_argument("--item-id")
    group.add_argument("--absent", action="store_true", help="You verified that the pending item is absent")
    return root


def run_path(case, run_id):
    if not isinstance(run_id, str) or not run_id.isalnum() or len(run_id) != 16:
        raise TraceError("Invalid run ID")
    return case / "runs" / run_id


def resolve_latest(case, run_id):
    """Resolve the explicitly saved pointer, never directory ordering or timestamps."""
    if run_id != "latest":
        return run_id
    path = case / "case.json"
    metadata = read_json(path) if path.exists() else {}
    if not isinstance(metadata, dict):
        raise TraceError("Invalid case metadata; restore the original case.json")
    selected = metadata.get("latest_run")
    if selected is None:
        raise TraceError("No latest run is saved for this case; use an explicit run ID or create a new run")
    try:
        run_path(case, selected)
    except TraceError:
        raise TraceError("Invalid latest_run in case.json; use an explicit run ID or restore the saved pointer") from None
    return selected


def save_latest(case, run_id):
    with (case / "case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = case / "case.json"
        metadata = read_json(path)
        metadata["latest_run"] = run_id
        save_json(path, metadata)


def verify_export(directory):
    """Verify archived bytes before using a completed run as new evidence input."""
    directory = Path(directory)
    manifest = directory / "SHA256SUMS"
    if not manifest.is_file():
        raise TraceError("Saved run has no SHA256SUMS manifest; use an intact completed export")
    seen = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or any(c not in "0123456789abcdef" for c in parts[0]):
            raise TraceError("Malformed saved-run checksum manifest")
        checksum, name = parts
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or name in seen or not path.is_file():
            raise TraceError("Missing, duplicated, or invalid file in saved-run manifest: " + name)
        if digest(path.read_bytes()) != checksum:
            raise TraceError("Saved-run checksum mismatch: " + name + "; restore the original evidence")
        seen.add(name)
    if not {"trace.json", "graph.json", "miro-plan.json"}.issubset(seen):
        raise TraceError("Saved-run manifest is missing required trace or graph files")


def board_id(value):
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme != "https" or parsed.netloc not in ("miro.com", "www.miro.com")
                or len(parts) != 3 or parts[:2] != ["app", "board"]):
            raise TraceError("Use a Miro board URL such as https://miro.com/app/board/BOARD_ID/ or a board ID")
        value = unquote(parts[2])
    if not value or len(value) > 200 or any(not (c.isascii() and (c.isalnum() or c in "_=-")) for c in value):
        raise TraceError("Invalid Miro board ID")
    return value


def case_identity(case):
    case.mkdir(parents=True, exist_ok=True)
    with (case / "case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = case / "case.json"
        if path.exists():
            identity = read_json(path)["case_id"]
            if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
                raise TraceError("Invalid case identity; restore the original case.json")
            return identity
        identity = uuid.uuid4().hex
        save_json(path, {"schema_version": 1, "case_id": identity})
        return identity


def sync_run(case, run_id, board, max_new_items=750, dry_run=False, plan_path=None):
    run_id = resolve_latest(case, run_id)
    target = board_id(board)
    default_plan = run_path(case, run_id) / "miro-plan.json"
    verify_export((plan_path or default_plan).parent)
    plan = read_json(plan_path or default_plan)
    if plan.get("run_id") != run_id:
        raise TraceError("Miro plan does not match the selected run")
    identity = read_json(case / "case.json")["case_id"]
    if plan.get("namespace", {}).get("case_id") != identity:
        raise TraceError("Saved plan has no matching case identity; regenerate it with export or create a continuation")
    state_path = case / "miro" / (digest(target.encode())[:24] + ".json")
    result = sync(plan, target, state_path, max_items=max_new_items, dry_run=dry_run)
    report = {**result, "run_id": run_id, "state_file": str(state_path.resolve())}
    if not dry_run:
        report_path = case / "miro" / "reports" / (run_id + "-" + uuid.uuid4().hex[:12] + ".json")
        report["report_file"] = str(report_path.resolve())
        save_json(report_path, report)
    return report


def run_trace(args):
    if args.miro_board:
        args.miro_board = board_id(args.miro_board)
        if args.max_new_items < 0:
            raise TraceError("--max-new-items must be nonnegative")
        if not os.environ.get("MIRO_ACCESS_TOKEN"):
            raise TraceError("Set MIRO_ACCESS_TOKEN locally before tracing with --miro-board")
    seeds = list(args.seed)
    if args.seeds_file:
        seeds.extend(line.split("#", 1)[0].strip() for line in args.seeds_file.read_text().splitlines())
    seeds = sorted(set(f"{txid}:{index}" for txid, index in (parse_outpoint(s) for s in seeds if s)))
    if bool(seeds) == bool(args.resume):
        raise TraceError("Provide seed outpoints OR --resume")
    if args.only and not args.resume:
        raise TraceError("--only requires --resume")
    if args.additional_hops is not None and (not args.resume or args.hops is not None or args.additional_hops < 0):
        raise TraceError("--additional-hops requires --resume, must be nonnegative, and cannot be combined with --hops")
    args.case.mkdir(parents=True, exist_ok=True)
    with (args.case / "trace.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another trace is running in this case") from None
        if args.resume:
            args.resume = resolve_latest(args.case, args.resume)
            verify_export(run_path(args.case, args.resume))
        parent = read_json(run_path(args.case, args.resume) / "trace.json") if args.resume else None
        identity = case_identity(args.case)
        if parent and parent.get("case_id", identity) != identity:
            raise TraceError("The resumed run belongs to a different case")
        if parent and parent.get("run_id") != args.resume:
            raise TraceError("Saved trace does not match the selected run")
        hops = args.hops if args.hops is not None else (parent["limits"]["max_hops"] if parent else 3)
        if args.additional_hops is not None:
            hops += args.additional_hops
        if parent and hops < parent["limits"]["max_hops"]:
            raise TraceError("Continuation cannot lower its parent's hop ceiling; start a fresh run to narrow scope")
        limits = Limits(hops, args.max_transactions, args.max_outpoints, args.max_requests, args.max_seconds)
        limits.validate()
        labels = load_labels(args.labels) if args.labels else (parent["labels"] if parent else [])
        merge_addresses = bool(args.merge_addresses or (parent and parent.get("address_mode") == "merged"))
        store = Store(args.case)
        try:
            api = Esplora(store, "pending", limits, args.base_url, args.auth, args.fixture,
                          args.tx_cache_seconds, args.min_interval)
            state = new_state(seeds, api.base, limits, labels, parent, case_id=identity)
            state["address_mode"] = "merged" if merge_addresses else "outpoint_occurrences"
            api.run_id = state["run_id"]
            destination = run_path(args.case, state["run_id"])
            only = {f"{t}:{i}" for t, i in map(parse_outpoint, args.only)} if args.only else None
            state = trace(api, state, limits, destination / "trace.json", args.include_unconfirmed, only)
            export_run(store, state, destination, merge_addresses, args.offline_preview)
            save_latest(args.case, state["run_id"])
            summary = {"run_id": state["run_id"], "status": state["status"],
                "stop_reason": state.get("stop_reason"), "stats": state["stats"], "errors": state["errors"],
                "directory": str(destination.resolve())}
            failed = state["status"] == "error"
            if args.miro_board:
                try:
                    summary["miro"] = sync_run(args.case, state["run_id"], args.miro_board, args.max_new_items)
                except (TraceError, OSError, ValueError, KeyError) as error:
                    summary["miro_error"] = str(error)
                    summary["miro_retry"] = {"command": "miro-sync", "case": str(args.case.resolve()),
                        "run": state["run_id"], "board": args.miro_board}
                    failed = True
            print(json.dumps(summary, indent=2))
            return 1 if failed else 0
        finally:
            store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "trace":
            return run_trace(args)
        if args.command == "export":
            if args.out.exists():
                raise TraceError("Choose a new export directory to preserve earlier evidence")
            args.run = resolve_latest(args.case, args.run)
            verify_export(run_path(args.case, args.run))
            store = Store(args.case)
            try:
                state = read_json(run_path(args.case, args.run) / "trace.json")
                if state.get("run_id") != args.run:
                    raise TraceError("Saved trace does not match the selected run")
                identity = case_identity(args.case)
                if state.get("case_id", identity) != identity:
                    raise TraceError("The saved run belongs to a different case")
                state["case_id"] = identity
                merged = bool(args.merge_addresses or state.get("address_mode") == "merged")
                export_run(store, state, args.out, merged, args.offline_preview)
            finally:
                store.close()
            print(args.out.resolve())
        elif args.command == "miro-sync":
            print(json.dumps(sync_run(args.case, args.run, args.board, args.max_new_items, args.dry_run, args.plan), indent=2))
        elif args.command == "miro-publish":
            print(json.dumps(publish(read_json(args.plan), board_id(args.board_id), args.state, args.max_items), indent=2))
        elif args.command == "miro-resolve":
            resolve(args.state, args.item_id, args.absent)
            print("Pending publication reconciled.")
        return 0
    except (TraceError, OSError, ValueError, KeyError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
