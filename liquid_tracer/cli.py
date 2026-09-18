import argparse
import fcntl
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .api import ENTERPRISE, Esplora, Limits
from .hop_limits import UNSET
from .address_counts import apply_saved_counts, ensure_counts, ensure_graph_counts
from .boards import create_board
from .common import HEX64, TraceError, digest, load_labels, output_kind, parse_outpoint, read_json, save_json
from .export import build_graph, export_run
from .investigations import read_case, update_case
from .inspection import inspect_transaction, inspect_transactions, parse_transaction_hashes
from .miro import _load_sync_state, _namespace, make_plan, publish, resolve, sync, validate_plan
from .progress import ProgressReporter
from .store import Store
from .trace import new_state, trace
from .services import apply_service_labels, disable_service, load_services, set_service
from .compaction_preview import (compaction_apply_lock, compaction_preview_metadata, latest_compaction_preview,
                                 verified_compaction_preview)


def fee_arguments(command):
    choices = command.add_mutually_exclusive_group()
    choices.add_argument("--include-fees", dest="include_fees", action="store_true", default=None,
                         help="Show transaction fee flows for this action (default: investigation setting, otherwise hidden)")
    choices.add_argument("--exclude-fees", dest="include_fees", action="store_false",
                         help="Hide transaction fee flows for this action; retain their evidence")


def connector_arguments(command):
    command.add_argument("--connector-style", choices=("straight", "curved", "elbowed"),
                         help="Connector appearance (default: investigation setting, otherwise straight)")


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
    bulk = commands.add_parser("address-import", help="Preview/apply bulk address attributions before or between runs; offline")
    bulk.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    bulk.add_argument("--file", type=Path, required=True, help="UTF-8 CSV, JSON array, or plain address list")
    bulk.add_argument("--format", choices=("auto", "csv", "json", "text"), default="auto")
    bulk.add_argument("--on-conflict", choices=("keep", "replace"), default="keep")
    approval = bulk.add_mutually_exclusive_group()
    approval.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    approval.add_argument("--approve-plan", help="Apply the exact approval_sha256 from a reviewed preview")
    colors = commands.add_parser("name-color-import", help="Preview/apply name-group colors from CSV or JSON; offline")
    colors.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    colors.add_argument("--file", type=Path, required=True, help="UTF-8 CSV with Name,Color columns or JSON array")
    colors.add_argument("--format", choices=("auto", "csv", "json"), default="auto")
    colors.add_argument("--on-conflict", choices=("keep", "replace"), default="keep")
    color_approval = colors.add_mutually_exclusive_group()
    color_approval.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    color_approval.add_argument("--approve-plan", help="Apply the exact approval_sha256 from a reviewed preview")
    activity = commands.add_parser("address-inspect", help="Save a bounded address activity lookup using the investigation's API source")
    activity.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    activity.add_argument("--address", required=True)
    activity.add_argument("--run", default="latest", help="Saved run whose API source to inspect (default: latest)")
    activity.add_argument("--max-pages", type=int, default=5, help="Maximum confirmed history pages, 25 transactions per page")
    activity.add_argument("--max-requests", type=int, default=10, help="Maximum HTTP attempts including authentication and retries")
    activity.add_argument("--max-seconds", type=float, default=60)
    addresses = commands.add_parser("address-list", help="Review saved addresses and cached activity without API requests")
    addresses.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    addresses.add_argument("--run", default="latest")
    addresses.add_argument("--query", default="")
    addresses.add_argument("--offset", type=int, default=0)
    addresses.add_argument("--limit", type=int, default=25)
    addresses.add_argument("--suspected-only", action="store_true")
    service = commands.add_parser("service-set", help="Save or remove an investigator-designated suspected-service stop")
    service.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    service.add_argument("--address", required=True)
    service.add_argument("--name", default=None, help="Optional attribution name")
    service.add_argument("--notes", "--rationale", dest="notes", default=None, help="Notes supporting the attribution")
    service.add_argument("--confidence", choices=("suspected", "confirmed"), default=None)
    service.add_argument("--source", default=None)
    service.add_argument("--hop-limit", default=UNSET, help="Additional hops after this address; blank clears the cap")
    service.add_argument("--stop-tracing", choices=("true", "false"), default=None)
    service.add_argument("--disable", action="store_true", help="Disable this designation so future continuation can resume its branches")
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
    run.add_argument("--api-workers", type=int, default=8, help="Concurrent explorer requests, from 1 to 8 (default: 8)")
    run.add_argument("--api-rate-limit", type=float,
                     help="Verified account requests/second; use 95%% of this limit (default: LIQUID_BLOCKSTREAM_API_RPS, otherwise the enterprise target of 49 requests/second or 4 for other endpoints)")
    run.add_argument("--min-interval", type=float,
                     help="Additional minimum seconds between requests; cannot exceed the configured rate ceiling")
    address_display = run.add_mutually_exclusive_group()
    address_display.add_argument("--merge-addresses", action="store_true", default=True,
                     help="One circle per full address (default); UTXO tracing remains unchanged")
    address_display.add_argument("--separate-outpoints", dest="merge_addresses", action="store_false",
                     help="Explicit legacy display: one circle per outpoint")
    fee_arguments(run)
    run.add_argument("--offline-preview", action="store_true", help="Also save optional HTML/SVG inspection files")
    run.add_argument("--miro-board", help="Sync the saved run to this Miro board URL or ID")
    run.add_argument("--max-new-items", type=int, default=750, help="Maximum new Miro shapes plus connectors")
    export = commands.add_parser("export", help="Regenerate a run export, fetching missing address transaction counts")
    export.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                        help="Case directory (default: LIQUID_CASE_DIR)")
    export.add_argument("--run", required=True, help="Saved run ID or latest")
    export.add_argument("--out", type=Path, required=True, help="New export directory")
    address_display = export.add_mutually_exclusive_group()
    address_display.add_argument("--merge-addresses", action="store_true", default=True)
    address_display.add_argument("--separate-outpoints", dest="merge_addresses", action="store_false",
                        help="Explicit legacy display: one circle per outpoint")
    fee_arguments(export)
    export.add_argument("--offline-preview", action="store_true")
    mermaid = commands.add_parser("mermaid", help="Create a local Mermaid chart, fetching missing address transaction counts")
    mermaid.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                         help="Case directory (default: LIQUID_CASE_DIR)")
    mermaid.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    mermaid.add_argument("--out", type=Path, help="New preview directory (default: automatically saved under the case's previews/)")
    mermaid.add_argument("--open", dest="open_browser", action="store_true", help="Open the completed local HTML chart in your browser")
    fee_arguments(mermaid)
    layout = commands.add_parser("layout-preview", help="Fetch missing address counts, then optimize locally with ELK and export HTML/SVG")
    layout.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    layout.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    layout.add_argument("--out", type=Path, help="New preview directory (default: saved under the case's previews/)")
    layout.add_argument("--open", dest="open_browser", action="store_true", help="Open the completed local layout in your browser")
    fee_arguments(layout)
    connector_arguments(layout)
    connections = commands.add_parser("connections", help="Plot only saved directed paths between starting transactions")
    connections.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    connections.add_argument("--run", default="latest")
    connections.add_argument("--hops", type=int, default=10, help="Maximum transaction hops per starter-to-starter path (default: 10)")
    connections.add_argument("--open", dest="open_browser", action="store_true")
    connection_publish = commands.add_parser("connections-publish", help="Publish a reviewed connection-only snapshot to a separate Miro board")
    connection_publish.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    connection_publish.add_argument("--preview", required=True)
    connection_publish.add_argument("--board", required=True)
    connection_publish.add_argument("--max-items", type=int, default=750)
    compact = commands.add_parser("compact-preview", help="Compact an ELK layout locally and save a before/after comparison for review")
    compact.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    compact.add_argument("--run", default="latest")
    compact.add_argument("--open", dest="open_browser", action="store_true")
    fee_arguments(compact)
    connector_arguments(compact)
    counts = commands.add_parser("address-counts", help="Fetch missing address transaction counts without tracing or layout")
    counts.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    counts.add_argument("--run", default="latest")
    counts.add_argument("--max-requests", type=int, default=1000)
    counts.add_argument("--max-seconds", type=int, default=300)
    counts.add_argument("--refresh", action="store_true", help="Refresh already cached counts too")
    csv = commands.add_parser("csv-export", help="Export displayed transaction input/output rows without API calls")
    csv.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                     help="Case directory (default: LIQUID_CASE_DIR)")
    csv.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    csv.add_argument("--out", type=Path, help="New export directory (default: automatically saved under the case's exports/)")
    fee_arguments(csv)
    board = commands.add_parser("miro-create-board", help="Create and save a Miro board for this investigation")
    board.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                       help="Case directory (default: LIQUID_CASE_DIR)")
    board.add_argument("--name", help="Board name (default: investigation name, at most 60 characters)")
    board.add_argument("--team-id", help="Optional destination Miro team ID")
    board.add_argument("--visibility", choices=["private", "team"], default="private",
                       help="Private or editable by the destination team (default: private)")
    migration = commands.add_parser("miro-merge-addresses", help="Review or resume in-place conversion to shared address circles")
    migration.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    migration.add_argument("--board", help="Existing mapped board (default: saved case board)")
    approval = migration.add_mutually_exclusive_group(required=True)
    approval.add_argument("--dry-run", action="store_true", help="Local-only conversion plan; no network or writes")
    approval.add_argument("--approve-plan", help="Exact approval_sha256 from the reviewed local plan")
    update = commands.add_parser("miro-sync", help="Add a saved run to the existing case graph, preserving manual edits")
    update.add_argument("--case", type=Path, default=case_default, required=case_default is None,
                        help="Case directory (default: LIQUID_CASE_DIR)")
    update.add_argument("--run", default="latest", help="Saved run ID (default: latest)")
    update.add_argument("--board", help="Miro board URL or ID (default: saved case board, then LIQUID_MIRO_BOARD, or a prompt)")
    update.add_argument("--plan", type=Path, help="Use a regenerated miro-plan.json for this run")
    update.add_argument("--compact-preview", help="Apply this exact saved compact preview ID; requires --reorganize")
    update.add_argument("--dry-run", action="store_true", help="Preview local new/mapped counts without network access or writes")
    fee_arguments(update)
    connector_arguments(update)
    update.add_argument("--reorganize", action="store_true",
                        help="Apply the current automatic layout to managed graph items, replacing their manual positions")
    update.add_argument("--max-new-items", type=int, default=750)
    miro = commands.add_parser("miro-publish", help="Legacy: create a separate snapshot; use miro-sync for cumulative graphs")
    miro.add_argument("--plan", type=Path, required=True)
    miro.add_argument("--board-id", required=True)
    miro.add_argument("--state", type=Path, required=True, help="Persistent local publication state")
    miro.add_argument("--max-items", type=int, default=750)
    reconcile = commands.add_parser("miro-resolve", help="Resolve a POST with uncertain outcome after inspecting the board")
    reconcile.add_argument("--state", type=Path, required=True)
    reconcile.add_argument("--key", help="Logical item key to reconcile when multiple creations have uncertain outcomes")
    group = reconcile.add_mutually_exclusive_group(required=True)
    group.add_argument("--item-id")
    group.add_argument("--absent", action="store_true", help="You verified that the pending item is absent")
    recover = commands.add_parser("miro-recover", help="Recover an uncertain initial publication after inspecting an empty board")
    recover.add_argument("--case", type=Path, default=case_default, required=case_default is None)
    recover.add_argument("--confirm-empty", action="store_true", required=True,
                         help="You inspected the linked board after the failed sync and confirmed it is empty; verify by API before clearing pending items")
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


def include_fee_flows(metadata, explicit=None):
    """Resolve a display preference without changing case settings."""
    defaults = metadata.get("run_defaults", {})
    if not isinstance(defaults, dict):
        raise TraceError("Invalid investigation run defaults; restore case.json")
    value = explicit if explicit is not None else defaults.get("include_fees", False)
    if not isinstance(value, bool):
        raise TraceError("include_fees must be true or false")
    return value


def connector_appearance(metadata, explicit=None):
    defaults = metadata.get("run_defaults", {})
    if not isinstance(defaults, dict):
        raise TraceError("Invalid investigation run defaults; restore case.json")
    value = explicit if explicit is not None else defaults.get("connector_style", "straight")
    if not isinstance(value, str) or value not in ("straight", "curved", "elbowed"):
        raise TraceError("connector_style must be straight, curved, or elbowed")
    return value


def refresh_presentation(plan, trace_path, include_fees=False, connector_style="straight", progress=None,
                         service_settings=None, preview_directory=None, fetch_address_counts=False, count_report=None):
    """Verify historical topology, then create a current shared-address view."""
    namespace = _namespace(plan)
    state = read_json(trace_path)
    if (not isinstance(state, dict) or state.get("run_id") != plan["run_id"]
            or state.get("case_id") != namespace["case_id"]
            or state.get("source") != namespace["source"]):
        raise TraceError("Saved trace does not match the Miro plan's run, case, or API source")
    if service_settings is not None:
        state["labels"] = apply_service_labels(state["labels"], service_settings)
        state["service_controls"] = {key: value for key, value in service_settings.items() if key != "history"}
    try:
        count_case = Path(trace_path).resolve().parents[2]
        if (count_case / "case.json").is_file():
            apply_saved_counts(count_case, state)
        merged = namespace["address_mode"] == "merged"
        full_graph = build_graph(state, merged, include_fees=True)
        full_plan = make_plan(full_graph)
        fee_outpoints = {f"{txid}:{index}" for txid, record in state["transactions"].items()
                         for index, output in enumerate(record["data"]["vout"])
                         if output_kind(output) == "fee"}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError("Saved trace cannot be rendered; restore the original evidence") from None
    validate_plan(full_plan)
    if _namespace(full_plan) != namespace or full_plan["run_id"] != plan["run_id"]:
        raise TraceError("Presentation refresh changed the saved graph identity")

    def topology(value):
        return (
            {(item["key"], item["body"]["data"]["shape"]) for item in value["shapes"]
             if item["key"] not in value.get("presentation_items", {})},
            {(item["key"], item["source"], item["target"]) for item in value["connectors"]},
        )

    full_topology, archived_topology = topology(full_plan), topology(plan)
    fee_keys = ({"event:" + key for key in fee_outpoints}, {"out:" + key for key in fee_outpoints})
    for complete, archived, keys in zip(full_topology, archived_topology, fee_keys):
        ordinary = {item for item in complete if item[0] not in keys}
        if ({item for item in archived if item[0] not in keys} != ordinary
                or not {item for item in archived if item[0] in keys}.issubset(complete)):
            raise TraceError("Presentation refresh would change saved graph topology beyond fee flows; use an explicit verified --plan or regenerate an export for review")
    # Layout is a derivative of verified evidence, never a rewrite of the archive.
    from .elk_layout import optimize_graph
    graph = build_graph(state, merge_addresses=True, include_fees=include_fees)
    if fetch_address_counts:
        report = ensure_counts(count_case, state, graph=graph, progress=progress)
        if count_report is not None:
            count_report.update(report)
    from .layout_reuse import reusable_elk_preview, report_phase
    laid_out = reusable_elk_preview(graph, preview_directory, connector_style, progress)
    if laid_out is None:
        laid_out = optimize_graph(graph, connector_style=connector_style, progress=progress)
    report_phase(progress, "building_plan")
    refreshed = make_plan(laid_out)
    validate_plan(refreshed)
    expected = topology(make_plan(graph))
    if (_namespace(refreshed) != {**namespace, "address_mode": "merged"}
            or refreshed["run_id"] != plan["run_id"] or topology(refreshed) != expected):
        raise TraceError("Presentation refresh would change saved graph topology; use an explicit verified --plan or regenerate an export for review")
    return refreshed


def miro_recovery_status(case):
    """Read-only status for the local UI, without exposing journal keys or paths."""
    from .miro_recovery import initial_pending_batch
    from .miro_state import load_state

    result = {"pending_count": 0, "can_confirm_empty": False}
    try:
        case = Path(case)
        metadata = read_case(case)
        if not metadata.get("miro_board"):
            return result
        target = board_id(metadata["miro_board"])
        path = case / "miro" / (digest(target.encode())[:24] + ".json")
        if not path.exists():
            return result
        state = load_state(path)
        namespace = _namespace(state)
        if namespace["case_id"] != metadata["case_id"]:
            raise TraceError("Miro mapping belongs to another investigation")
        state = _load_sync_state(path, target, namespace, allow_pending=True)
        result["pending_count"] = len(state.get("pending_creations", {})) + bool(state.get("pending"))
        if result["pending_count"]:
            try:
                initial_pending_batch(state)
                result["can_confirm_empty"] = True
            except TraceError:
                pass
        return result
    except (TraceError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**result, "can_confirm_empty": False, "unavailable": True}


def recover_miro_run(case, confirmed_empty=False, progress=None):
    """Recover only the linked board, using the saved investigation namespace."""
    from .miro_recovery import recover_empty_board

    if confirmed_empty is not True:
        raise TraceError("Inspect the linked Miro board after the failed sync and confirm it is empty first")
    case = Path(case)
    metadata = read_case(case)
    if not metadata.get("miro_board"):
        raise TraceError("This investigation has no linked Miro board to recover")
    target = board_id(metadata["miro_board"])
    run_id = resolve_latest(case, "latest")
    archive = run_path(case, run_id)
    verify_export(archive)
    plan = read_json(archive / "miro-plan.json")
    validate_plan(plan)
    namespace = _namespace(plan)
    if plan["run_id"] != run_id or namespace["case_id"] != metadata["case_id"]:
        raise TraceError("Saved plan does not match this investigation")
    path = case / "miro" / (digest(target.encode())[:24] + ".json")
    report = recover_empty_board(path, target, namespace, confirmed_empty=True, progress=progress)
    return {**report, "board_url": "https://miro.com/app/board/" + quote(target, safe="") + "/"}


def sync_run(case, run_id, board=None, max_new_items=750, dry_run=False, plan_path=None,
             include_fees=None, reorganize=False, progress=None, connector_style=None, compact_preview=None):
    if compact_preview is not None and (not reorganize or plan_path is not None):
        raise TraceError("--compact-preview requires --reorganize and cannot be combined with --plan")
    if (plan_path is not None or compact_preview is not None) and (include_fees is not None or connector_style is not None):
        raise TraceError("--plan cannot be combined with --include-fees, --exclude-fees, or --connector-style; select an explicit plan with the desired presentation")
    run_id = resolve_latest(case, run_id)
    default_plan = run_path(case, run_id) / "miro-plan.json"
    compaction_meta = None
    count_report = {}
    if compact_preview is not None:
        plan, compaction_meta = verified_compaction_preview(case, run_id, compact_preview)
    else:
        verify_export((plan_path or default_plan).parent)
        plan = read_json(plan_path or default_plan)
    validate_plan(plan)
    namespace = _namespace(plan)
    if plan.get("run_id") != run_id:
        raise TraceError("Miro plan does not match the selected run")
    metadata = read_case(case)
    if namespace["case_id"] != metadata["case_id"]:
        raise TraceError("Saved plan has no matching case identity; regenerate it with export or create a continuation")
    archived_plan_sha256 = (read_json(default_plan)["sha256"] if compaction_meta is not None else plan["sha256"])
    if not isinstance(max_new_items, int) or max_new_items < 0:
        raise TraceError("--max-new-items must be a nonnegative integer")
    target = resolve_board(metadata, board)
    state_path = case / "miro" / (digest(target.encode())[:24] + ".json")
    # Pending creation outcomes block both preview and publication. Check the
    # durable mapping before running ELK; sync repeats this under its own lock.
    expected_namespace = ({**namespace, "address_mode": "merged"}
                          if plan_path is None and compact_preview is None else namespace)
    _load_sync_state(state_path, target, expected_namespace)
    if plan_path is None and compact_preview is None:
        plan = refresh_presentation(plan, default_plan.parent / "trace.json", include_fee_flows(metadata, include_fees),
                                    connector_appearance(metadata, connector_style), progress=progress,
                                    service_settings=load_services(case), preview_directory=Path(case) / "previews",
                                    fetch_address_counts=not dry_run, count_report=count_report)
    # Validate the mapping, lineage, and item budget locally before saving a selection.
    options = {"reorganize": True} if reorganize else {}
    if progress is not None:
        options["progress"] = progress
    result = sync(plan, target, state_path, max_items=max_new_items, dry_run=True, **options)
    if not dry_run:
        with compaction_apply_lock(case, compaction_meta):
            update_case(case, {"miro_board": target})
            result = sync(plan, target, state_path, max_items=max_new_items, dry_run=False, **options)
    report = {**result, "run_id": run_id, "board_id": target,
              "board_url": "https://miro.com/app/board/" + quote(target, safe="") + "/",
              "state_file": str(state_path.resolve()),
              "plan_sha256": plan["sha256"], "archived_plan_sha256": archived_plan_sha256,
              "presentation_version": plan.get("presentation_version", 1),
              "include_fees": plan.get("include_fees", True),
              "reorganize": bool(reorganize),
              "presentation_refreshed": plan["sha256"] != archived_plan_sha256}
    if count_report:
        report["address_counts"] = count_report
    if compaction_meta is not None:
        report.update(compact_preview=compact_preview, compaction=compaction_meta["compaction"],
                      connector_style=compaction_meta["connector_style"])
    if not dry_run:
        report_path = case / "miro" / "reports" / (run_id + "-" + uuid.uuid4().hex[:12] + ".json")
        report["report_file"] = str(report_path.resolve())
        save_json(report_path, report)
    return report


def run_trace(args, progress=None):
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
        service_settings = load_services(args.case)
        labels = apply_service_labels(labels, service_settings)
        merge_addresses = bool(args.merge_addresses)
        store = Store(args.case)
        api = None
        try:
            api = Esplora(store, "pending", limits, args.base_url, args.auth, args.fixture,
                          args.tx_cache_seconds, args.min_interval, workers=args.api_workers,
                          advertised_rps=args.api_rate_limit)
            state = new_state(seeds, api.base, limits, labels, parent, case_id=identity)
            state["service_controls"] = {key: value for key, value in service_settings.items() if key != "history"}
            state["fetch_options"] = {"workers": api.workers,
                                      "advertised_rps": api.advertised_rps,
                                      "effective_rps": api.effective_rps,
                                      "rate_limit_source": api.rate_limit_source,
                                      "min_interval": api.min_interval,
                                      "fixture": api.fixture is not None}
            metadata = read_case(args.case)
            state["investigation"] = {"case_id": identity, "name": metadata.get("name"),
                                      "miro_board": args.miro_board or metadata.get("miro_board") or None}
            state["address_mode"] = "merged" if merge_addresses else "outpoint_occurrences"
            state["graph_options"] = {**state.get("graph_options", {}),
                                      "include_fees": include_fee_flows(metadata, args.include_fees)}
            api.run_id = state["run_id"]
            destination = run_path(args.case, state["run_id"])
            only = {f"{t}:{i}" for t, i in map(parse_outpoint, args.only)} if args.only else None
            state = trace(api, state, limits, destination / "trace.json", args.include_unconfirmed, only)
            if state["status"] != "error" and state.get("stop_reason") != "interrupted":
                count_report = ensure_counts(args.case, state, fixture=args.fixture, progress=progress)
            else:
                apply_saved_counts(args.case, state)
                count_report = {}
            export_run(store, state, destination, merge_addresses, args.offline_preview)
            save_latest(args.case, state["run_id"])
            summary = {"run_id": state["run_id"], "status": state["status"],
                "stop_reason": state.get("stop_reason"), "stats": state["stats"], "errors": state["errors"],
                "directory": str(destination.resolve()), "address_counts": count_report}
            failed = state["status"] == "error"
            if args.miro_board:
                try:
                    summary["miro"] = sync_run(args.case, state["run_id"], args.miro_board, args.max_new_items,
                                               include_fees=state["graph_options"]["include_fees"], progress=progress)
                except (TraceError, OSError, ValueError, KeyError) as error:
                    summary["miro_error"] = str(error)
                    summary["miro_retry"] = {"command": "miro-sync", "case": str(args.case.resolve()),
                        "run": state["run_id"], "board": args.miro_board}
                    failed = True
            print(json.dumps(summary, indent=2))
            return 1 if failed else 0
        finally:
            try:
                if api is not None:
                    api.close()
            finally:
                store.close()


def open_preview(path):
    """Best effort desktop launch; a missing browser cannot block a saved chart."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import sys, webbrowser; sys.exit(0 if webbrowser.open_new_tab(sys.argv[1]) else 1)",
             Path(path).resolve().as_uri()],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def saved_graph(case, run_id="latest", include_fees=None):
    """Build current presentation from a verified snapshot, without case writes."""
    case = Path(case)
    metadata = read_case(case)
    run_id = resolve_latest(case, run_id)
    archive = run_path(case, run_id)
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if state.get("run_id") != run_id:
        raise TraceError("Saved trace does not match the selected run")
    if state.get("case_id") != metadata["case_id"]:
        raise TraceError("The saved run belongs to a different case")
    service_settings = load_services(case)
    state["labels"] = apply_service_labels(state["labels"], service_settings)
    state["service_controls"] = {key: value for key, value in service_settings.items() if key != "history"}
    apply_saved_counts(case, state)
    fees = include_fee_flows(metadata, include_fees)
    graph = build_graph(state, merge_addresses=True, include_fees=fees)
    return run_id, archive, graph


def mermaid_run(case, run_id="latest", out=None, include_fees=None, open_browser=False, progress=None):
    from .mermaid import export_mermaid

    case = Path(case)
    run_id, _, graph = saved_graph(case, run_id, include_fees)
    destination = Path(out) if out is not None else case / "previews" / (run_id + "-mermaid-" + uuid.uuid4().hex[:8])
    if destination.resolve().is_relative_to((case / "runs").resolve()):
        raise TraceError("Save Mermaid previews outside runs/ to preserve archived evidence")
    counts = ensure_graph_counts(case, graph, progress=progress)
    result = export_mermaid(graph, destination)
    result["address_counts"] = counts
    result.update({"run_id": run_id, "include_fees": graph["include_fees"],
                   "browser_opened": open_preview(result["html"]) if open_browser else False})
    return result


def layout_preview_run(case, run_id="latest", out=None, include_fees=None,
                       connector_style=None, open_browser=False, progress=None):
    from .elk_layout import optimize_graph
    from .layout_preview import export_layout

    case = Path(case)
    run_id, _, graph = saved_graph(case, run_id, include_fees)
    style = connector_appearance(read_case(case), connector_style)
    destination = Path(out) if out is not None else case / "previews" / (run_id + "-elk-" + uuid.uuid4().hex[:8])
    if destination.resolve().is_relative_to((case / "runs").resolve()):
        raise TraceError("Save ELK previews outside runs/ to preserve archived evidence")
    counts = ensure_graph_counts(case, graph, progress=progress)
    graph = optimize_graph(graph, connector_style=style, progress=progress)
    result = export_layout(graph, destination)
    result["address_counts"] = counts
    result.update({"run_id": run_id, "include_fees": graph["include_fees"], "connector_style": style,
                   "layout_algorithm": graph["layout"]["algorithm"],
                   "browser_opened": open_preview(result["html"]) if open_browser else False})
    if graph["layout"].get("fallback_reason") in ("size_limit", "timeout", "mermaid_size_limit", "mermaid_timeout"):
        result["fallback_reason"] = graph["layout"]["fallback_reason"]
    return result


def compact_preview_run(case, run_id="latest", include_fees=None, connector_style=None,
                        open_browser=False, progress=None):
    from .compaction import compact_graph
    from .compaction_preview import export_compaction, service_fingerprint
    from .elk_layout import optimize_graph

    case = Path(case)
    # Freeze local presentation decisions for the calculation without holding a
    # case lock through a potentially long layout. A concurrent decision change
    # prevents the completed proposal from becoming an applicable preview.
    services_before = service_fingerprint(case)
    run_id, archive, graph = saved_graph(case, run_id, include_fees)
    archive_sha256 = digest((archive / "SHA256SUMS").read_bytes())
    style = connector_appearance(read_case(case), connector_style)
    counts = ensure_graph_counts(case, graph, progress=progress)
    before = optimize_graph(graph, connector_style=style, progress=progress)
    after = compact_graph(before, progress=progress)
    if services_before != service_fingerprint(case):
        raise TraceError("Service assessments changed during compaction; create the preview again")
    destination = case / "previews" / (run_id + "-compact-" + uuid.uuid4().hex[:8])
    result = export_compaction(before, after, destination, archive_sha256=archive_sha256,
                               service_sha256=services_before)
    result.update(address_counts=counts, run_id=run_id, include_fees=after["include_fees"], connector_style=style,
                  layout_algorithm=after["layout"]["algorithm"],
                  browser_opened=open_preview(result["html"]) if open_browser else False)
    return result


def csv_run(case, run_id="latest", out=None, include_fees=None):
    from .csv_export import export_csv

    case = Path(case)
    run_id, archive, graph = saved_graph(case, run_id, include_fees)
    destination = Path(out) if out is not None else case / "exports" / (run_id + "-csv-" + uuid.uuid4().hex[:8])
    if destination.resolve().is_relative_to((case / "runs").resolve()):
        raise TraceError("Save CSV exports outside runs/ to preserve archived evidence")
    result = export_csv(graph, archive, destination)
    result.update({"run_id": run_id, "include_fees": graph["include_fees"]})
    return result


def main(argv=None, *, progress=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    progress = progress if progress is not None else ProgressReporter()
    try:
        if not argv and sys.stdin.isatty():
            from .menu import run_menu
            return run_menu()
        args = parser().parse_args(argv)
        if args.command == "credentials-check":
            return check_credentials(args.service)
        if args.command == "address-import":
            from .address_import import apply_import, preview_import, read_import
            text = read_import(args.file)
            options = {"format": args.format, "policy": args.on_conflict}
            result = (apply_import(args.case, text, approval_sha256=args.approve_plan, **options)
                      if args.approve_plan else preview_import(args.case, text, **options))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("valid", True) else 1
        if args.command == "name-color-import":
            from .name_color_import import apply_import, preview_import, read_import
            text = read_import(args.file)
            options = {"format": args.format, "policy": args.on_conflict}
            result = (apply_import(args.case, text, approval_sha256=args.approve_plan, **options)
                      if args.approve_plan else preview_import(args.case, text, **options))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("valid", True) else 1
        if args.command == "address-counts":
            from .address_counts import fetch_counts
            result = fetch_counts(args.case, args.run, max_requests=args.max_requests,
                                  max_seconds=args.max_seconds, refresh=args.refresh, progress=progress)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "address-inspect":
            from .address_review import inspect_case_address
            print(json.dumps(inspect_case_address(args.case, args.address, max_pages=args.max_pages,
                                                 run_id=args.run, max_requests=args.max_requests,
                                                 max_seconds=args.max_seconds), indent=2))
            return 0
        if args.command == "address-list":
            from .address_review import list_addresses
            print(json.dumps(list_addresses(args.case, args.run, args.query, args.offset,
                                           args.limit, args.suspected_only), indent=2))
            return 0
        if args.command == "service-set":
            settings = (disable_service(args.case, args.address) if args.disable else
                        set_service(args.case, args.address, name=args.name, notes=args.notes, confidence=args.confidence, source=args.source, stop_tracing=None if args.stop_tracing is None else args.stop_tracing == "true", hop_limit=args.hop_limit))
            from .address_activity import validate_address
            print(json.dumps({"revision": settings["revision"],
                              "service": settings["rules"][validate_address(args.address)]}, indent=2))
            return 0
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
            return run_trace(args, progress=progress)
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
                service_settings = load_services(args.case)
                state["labels"] = apply_service_labels(state["labels"], service_settings)
                state["service_controls"] = {key: value for key, value in service_settings.items() if key != "history"}
                state["graph_options"] = {**state.get("graph_options", {}),
                                          "include_fees": include_fee_flows(read_case(args.case), args.include_fees)}
                merged = bool(args.merge_addresses)
                ensure_counts(args.case, state, progress=progress)
                export_run(store, state, args.out, merged, args.offline_preview)
            finally:
                store.close()
            print(args.out.resolve())
        elif args.command == "mermaid":
            print(json.dumps(mermaid_run(args.case, args.run, args.out, args.include_fees, args.open_browser, progress), indent=2))
        elif args.command == "layout-preview":
            print(json.dumps(layout_preview_run(args.case, args.run, args.out, args.include_fees,
                                                args.connector_style, args.open_browser, progress), indent=2))
        elif args.command == "connections":
            from .connections import preview_connections
            print(json.dumps(preview_connections(args.case, args.run, args.hops,
                                                open_browser=args.open_browser, progress=progress), indent=2))
        elif args.command == "connections-publish":
            from .connections import publish_connections
            print(json.dumps(publish_connections(args.case, args.preview, args.board, max_items=args.max_items), indent=2))
        elif args.command == "compact-preview":
            print(json.dumps(compact_preview_run(args.case, args.run, args.include_fees,
                                                 args.connector_style, args.open_browser, progress), indent=2))
        elif args.command == "csv-export":
            print(json.dumps(csv_run(args.case, args.run, args.out, args.include_fees), indent=2))
        elif args.command == "miro-create-board":
            print(json.dumps(create_board(args.case, args.name, args.team_id, args.visibility), indent=2))
        elif args.command == "miro-merge-addresses":
            from .address_migration import preview_merge, apply_merge
            result = (preview_merge(args.case, args.board) if args.dry_run else
                      apply_merge(args.case, args.approve_plan, args.board, progress=progress))
            print(json.dumps(result, indent=2))
        elif args.command == "miro-sync":
            print(json.dumps(sync_run(args.case, args.run, args.board, args.max_new_items, args.dry_run, args.plan,
                                      args.include_fees, args.reorganize, progress=progress,
                                      connector_style=args.connector_style, compact_preview=args.compact_preview), indent=2))
        elif args.command == "miro-publish":
            print(json.dumps(publish(read_json(args.plan), board_id(args.board_id), args.state, args.max_items), indent=2))
        elif args.command == "miro-resolve":
            resolve(args.state, args.item_id, args.absent, key=args.key)
            print("Pending publication reconciled.")
        elif args.command == "miro-recover":
            print(json.dumps(recover_miro_run(args.case, args.confirm_empty, progress=progress), indent=2))
        return 0
    except KeyboardInterrupt:
        # Renderer and API cleanup unwinds before reaching this boundary.
        print("Action canceled. Saved investigation data remains available.", file=sys.stderr)
        return 130
    except (TraceError, OSError, ValueError, KeyError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
