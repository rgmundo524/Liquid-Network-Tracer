import argparse
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .api import ENTERPRISE, Esplora, Limits
from .boards import create_board
from .common import HEX64, TraceError, digest, load_labels, parse_outpoint, read_json, save_json
from .export import build_graph, export_run
from .investigations import read_case, update_case
from .inspection import inspect_transaction, inspect_transactions, parse_transaction_hashes
from .miro import _namespace, make_plan, publish, resolve, sync, validate_plan
from .store import Store
from .trace import new_state, trace


def parser():
    case_default = os.environ.get("LIQUID_CASE_DIR") or None
    root = argparse.ArgumentParser(description="Bounded Liquid UTXO reachability with saved evidence and Miro export")
    commands = root.add_subparsers(dest="command", required=True)
    menu = commands.add_parser("menu", help="Open the interactive investigation menu")
    menu.add_argument("--investigations-dir", type=Path, help="Directory containing saved investigations")
    credentials = commands.add_parser("credentials-check", help="Check injected credential presence without contacting services")
    credentials.add_argument("--service", choices=["blockstream", "miro", "all"], default="blockstream",
                             help="Service credentials to check (default: blockstream)")
    inspect = commands.add_parser("inspect-tx", help="Look up one transaction's outputs before choosing seeds")
    inspect.add_argument("--txid", required=True, help="64-character Liquid transaction hash")
    inspect.add_argument("--output", type=Path, help="Write output JSON to a new file instead of stdout")
    inspect.add_argument("--fixture", type=Path, help="Offline synthetic API response; no network calls")
    inspect.add_argument("--max-requests", type=int, default=5, help="All HTTP attempts, including OAuth and retries (default: 5)")
    inspect.add_argument("--max-seconds", type=float, default=30, help="Maximum lookup duration (default: 30)")
    inspect.add_argument("--base-url", default=ENTERPRISE)
    inspect.add_argument("--auth", choices=["blockstream", "none"], default="blockstream")
    batch = commands.add_parser("inspect-txs", help="Look up comma-separated transaction hashes before choosing seeds")
    batch.add_argument("--txids", required=True, help="Liquid transaction hashes separated by commas")
    batch.add_argument("--output", type=Path, help="Write the complete output report to a new file instead of stdout")
    batch.add_argument("--fixture", type=Path, help="Offline synthetic API responses; no network calls")
    batch.add_argument("--max-requests", type=int, help="Shared HTTP attempt limit (default: 5 per distinct transaction)")
    batch.add_argument("--max-seconds", type=float, help="Shared lookup duration limit (default: 30 seconds per distinct transaction)")
    batch.add_argument("--base-url", default=ENTERPRISE)
    batch.add_argument("--auth", choices=["blockstream", "none"], default="blockstream")
    run = commands.add_parser("trace", help="Start or extend a bounded run")
    run.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                     help="Case directory (default: LIQUID_CASE_DIR)")
    run.add_argument("--seed", action="append", default=[], help="Liquid HASH:NUMBER, using the numeric output index starting at 0; may be repeated")
    run.add_argument("--seeds-file", type=Path, help="One HASH:NUMBER per line, using the numeric output index starting at 0; # comments allowed")
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
    board = commands.add_parser("miro-create-board", help="Create and save a Miro board for this investigation")
    board.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                       help="Case directory (default: LIQUID_CASE_DIR)")
    board.add_argument("--name", help="Board name (default: investigation name, at most 60 characters)")
    board.add_argument("--team-id", help="Optional destination Miro team ID")
    board.add_argument("--visibility", choices=["private", "team"], default="private",
                       help="Private or editable by the destination team (default: private)")
    update = commands.add_parser("miro-sync", help="Add a saved run to the existing case graph, preserving manual edits")
    update.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                        help="Case directory (default: LIQUID_CASE_DIR)")
    update.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    update.add_argument("--board", help="Miro board URL or ID (default: saved case board, then LIQUID_MIRO_BOARD, or a prompt)")
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


def check_credentials(service):
    """Report presence only; credential retrieval and authentication are separate."""
    names = []
    if service in ("blockstream", "all"):
        names.extend(("BLOCKSTREAM_CLIENT_ID", "BLOCKSTREAM_CLIENT_SECRET"))
    if service in ("miro", "all"):
        names.append("MIRO_ACCESS_TOKEN")
    missing = False
    for name in names:
        present = bool(os.environ.get(name))
        print(name + ": " + ("present" if present else "MISSING"))
        missing |= not present
    return int(missing)


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
    if not isinstance(value, str):
        raise TraceError("Invalid Miro board ID")
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


def resolve_board(metadata, explicit=None):
    """Choose a board without writing settings or consulting a credential provider."""
    value = explicit if explicit is not None else metadata.get("miro_board") or os.environ.get("LIQUID_MIRO_BOARD")
    if value is None or (explicit is None and value == ""):
        if not sys.stdin.isatty():
            raise TraceError("No Miro board is saved for this case; supply --board with a board URL or ID, or configure the board in the investigation menu")
        print("Miro board URL or ID: ", end="", file=sys.stderr, flush=True)
        try:
            value = input()
        except EOFError:
            raise TraceError("No Miro board was provided; supply --board with a board URL or ID") from None
    return board_id(value)


def refresh_presentation(plan, trace_path):
    """Render current labels from verified evidence without changing graph identity."""
    namespace = _namespace(plan)
    state = read_json(trace_path)
    if (not isinstance(state, dict) or state.get("run_id") != plan["run_id"]
            or state.get("case_id") != namespace["case_id"]
            or state.get("source") != namespace["source"]):
        raise TraceError("Saved trace does not match the Miro plan's run, case, or API source")
    try:
        refreshed = make_plan(build_graph(state, namespace["address_mode"] == "merged"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError("Saved trace cannot be rendered; restore the original evidence") from None
    validate_plan(refreshed)
    if _namespace(refreshed) != namespace or refreshed["run_id"] != plan["run_id"]:
        raise TraceError("Presentation refresh changed the saved graph identity")

    def topology(value):
        return (
            {(item["key"], item["body"]["data"]["shape"]) for item in value["shapes"]},
            {(item["key"], item["source"], item["target"]) for item in value["connectors"]},
        )

    if topology(refreshed) != topology(plan):
        raise TraceError("Presentation refresh would change saved graph topology; use an explicit verified --plan or regenerate an export for review")
    return refreshed


def sync_run(case, run_id, board=None, max_new_items=750, dry_run=False, plan_path=None):
    run_id = resolve_latest(case, run_id)
    default_plan = run_path(case, run_id) / "miro-plan.json"
    verify_export((plan_path or default_plan).parent)
    plan = read_json(plan_path or default_plan)
    validate_plan(plan)
    namespace = _namespace(plan)
    if plan.get("run_id") != run_id:
        raise TraceError("Miro plan does not match the selected run")
    metadata = read_case(case)
    if namespace["case_id"] != metadata["case_id"]:
        raise TraceError("Saved plan has no matching case identity; regenerate it with export or create a continuation")
    archived_plan_sha256 = plan["sha256"]
    if plan_path is None:
        plan = refresh_presentation(plan, default_plan.parent / "trace.json")
    if not isinstance(max_new_items, int) or max_new_items < 0:
        raise TraceError("--max-new-items must be a nonnegative integer")
    target = resolve_board(metadata, board)
    state_path = case / "miro" / (digest(target.encode())[:24] + ".json")
    # Validate the mapping, lineage, and item budget locally before saving a selection.
    result = sync(plan, target, state_path, max_items=max_new_items, dry_run=True)
    if not dry_run:
        update_case(case, {"miro_board": target})
        result = sync(plan, target, state_path, max_items=max_new_items, dry_run=False)
    report = {**result, "run_id": run_id, "board_id": target,
              "board_url": "https://miro.com/app/board/" + quote(target, safe="") + "/",
              "state_file": str(state_path.resolve()),
              "plan_sha256": plan["sha256"], "archived_plan_sha256": archived_plan_sha256,
              "presentation_version": plan.get("presentation_version", 1),
              "presentation_refreshed": plan["sha256"] != archived_plan_sha256}
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
            metadata = read_case(args.case)
            state["investigation"] = {"case_id": identity, "name": metadata.get("name"),
                                      "miro_board": args.miro_board or metadata.get("miro_board") or None}
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
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv and sys.stdin.isatty():
            from .menu import run_menu
            return run_menu()
        args = parser().parse_args(argv)
        if args.command == "credentials-check":
            return check_credentials(args.service)
        if args.command in ("inspect-tx", "inspect-txs"):
            # Invalid hashes go directly to inspect_transaction's validation,
            # before any filesystem checks. Preflight valid lookups before they
            # can consume API credits, then retain exclusive creation below.
            txids = parse_transaction_hashes(args.txids) if args.command == "inspect-txs" else None
            if args.output is not None and (txids is not None or HEX64.fullmatch(args.txid)):
                if args.output.exists() or args.output.is_symlink():
                    raise TraceError("Output report already exists; choose a new path")
                if not args.output.parent.is_dir():
                    raise TraceError("Output report parent must be an existing directory")
            inspect = inspect_transactions if txids is not None else inspect_transaction
            result = inspect(txids if txids is not None else args.txid, fixture=args.fixture,
                             base_url=args.base_url, auth=args.auth,
                             max_requests=args.max_requests, max_seconds=args.max_seconds)
            if args.output is None:
                print(json.dumps(result, indent=2))
            else:
                try:
                    with args.output.open("x", encoding="utf-8") as report:
                        json.dump(result, report, indent=2)
                        report.write("\n")
                except FileExistsError:
                    raise TraceError("Output report already exists; choose a new path") from None
                print("Transaction outputs saved.")
            return 0
        if args.command == "menu":
            from .menu import run_menu
            return run_menu(args.investigations_dir)
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
        elif args.command == "miro-create-board":
            print(json.dumps(create_board(args.case, args.name, args.team_id, args.visibility), indent=2))
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
