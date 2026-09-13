"""Reviewable compaction products, independent of immutable tracing archives."""

import html
import fcntl
import math
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .common import TraceError, canonical, digest, read_json, save_json
from .investigations import read_case
from .layout_preview import _preview_html, export_layout, render_svg
from .miro import make_plan, validate_plan
from .services import load_services

PREVIEW_ID = re.compile(r"[0-9a-f]{16}-compact-[0-9a-f]{8}\Z")
FILES = frozenset({"graph.html", "graph.svg", "graph.json", "before.html", "before.svg", "before.json",
                   "layout-report.json", "miro-plan.json", "compaction.json"})


def _hash_file(path):
    import hashlib
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def service_fingerprint(case):
    settings = load_services(case)
    return digest(canonical({key: value for key, value in settings.items() if key != "history"}))


@contextmanager
def compaction_apply_lock(case, meta):
    """Keep reviewed service decisions stable through live preflight and writes.

    Service edits and tracing hold the same lock exclusively. The live compact
    application needs only a shared lock; ordinary sync keeps its existing
    concurrency behavior. Recheck after acquiring it because local validation
    can finish well before a large board is ready to receive changes.
    """
    if meta is None:
        yield
        return
    with (Path(case) / "trace.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("This investigation has an active trace or address review; apply the compact layout after it finishes") from None
        if meta.get("service_sha256") != service_fingerprint(case):
            raise TraceError("Service assessments changed since this preview; create a new compact preview before applying it")
        yield


def _stat_signature(paths):
    result = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise TraceError("The compact preview is incomplete or contains a symbolic link; create it again")
        stat = path.stat()
        result.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(result)


def _directory(case, preview_id):
    if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
        raise TraceError("Choose a saved compact preview from this investigation")
    directory = Path(case) / "previews" / preview_id
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise TraceError("Compact previews cannot contain symbolic links")
    return directory


@lru_cache(maxsize=8)
def _verified_files(directory_text, signature):
    """Cache only small metadata after checking all immutable product bytes."""
    directory = Path(directory_text)
    seen = set()
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if (len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
                or parts[1] not in FILES or parts[1] in seen):
            raise TraceError("Invalid compact-preview checksum manifest; create the preview again")
        checksum, name = parts
        if _hash_file(directory / name) != checksum:
            raise TraceError("Compact-preview checksum mismatch; create the preview again")
        seen.add(name)
    if seen != FILES:
        raise TraceError("Compact preview has missing files; create the preview again")
    meta = read_json(directory / "compaction.json")
    if (not isinstance(meta, dict) or meta.get("schema_version") != 1
            or meta.get("preview_id") != directory.name or not isinstance(meta.get("compaction"), dict)
            or meta["compaction"].get("algorithm") != "local_address_components_v1"
            or meta["compaction"].get("version") != 1
            or meta.get("connector_style") not in ("straight", "curved", "elbowed")
            or type(meta.get("include_fees")) is not bool):
        raise TraceError("Invalid compact-preview metadata; create the preview again")
    plan = read_json(directory / "miro-plan.json")
    validate_plan(plan)
    if (plan.get("sha256") != meta.get("plan_sha256") or plan.get("namespace") != meta.get("namespace")
            or plan.get("run_id") != meta.get("run_id")
            or plan.get("layout", {}).get("compaction") != meta["compaction"]):
        raise TraceError("Compact preview and Miro plan disagree; create the preview again")
    return meta


@lru_cache(maxsize=4)
def _verified_archive(archive_text, signature):
    from .cli import verify_export
    archive = Path(archive_text)
    verify_export(archive)
    plan = read_json(archive / "miro-plan.json")
    validate_plan(plan)
    return _hash_file(archive / "SHA256SUMS"), plan.get("namespace"), plan.get("run_id")


def compaction_preview_metadata(case, run_id, preview_id):
    from .cli import resolve_latest, run_path
    case = Path(case)
    selected = resolve_latest(case, run_id)
    directory = _directory(case, preview_id)
    if not preview_id.startswith(selected + "-compact-"):
        raise TraceError("Compact preview belongs to another saved run")
    paths = [directory / name for name in sorted(FILES | {"SHA256SUMS"})]
    meta = _verified_files(str(directory.absolute()), _stat_signature(paths))
    metadata = read_case(case)
    if meta.get("case_id") != metadata["case_id"] or meta.get("run_id") != selected:
        raise TraceError("Compact preview belongs to another investigation")
    archive = run_path(case, selected)
    if any(path.is_symlink() for path in (archive, *archive.parents)):
        raise TraceError("Compact preview requires ordinary saved run files")
    signature = _stat_signature(sorted(path for path in archive.iterdir() if path.is_file() or path.is_symlink()))
    fingerprint, namespace, archived_run = _verified_archive(str(archive.absolute()), signature)
    if (fingerprint != meta.get("archive_sha256") or namespace != meta.get("namespace")
            or archived_run != selected):
        raise TraceError("The compact preview no longer matches its saved evidence; create it again")
    if meta.get("service_sha256") != service_fingerprint(case):
        raise TraceError("Service assessments changed since this preview; create a new compact preview before applying it")
    # Do not let callers mutate cached verification metadata.
    import copy
    return copy.deepcopy(meta)


def verified_compaction_preview(case, run_id, preview_id):
    meta = compaction_preview_metadata(case, run_id, preview_id)
    plan = read_json(_directory(case, preview_id) / "miro-plan.json")
    validate_plan(plan)
    if plan.get("sha256") != meta["plan_sha256"]:
        raise TraceError("Compact preview changed during verification; create it again")
    return plan, meta


def latest_compaction_preview(case, run_id="latest"):
    from .cli import resolve_latest
    if run_id == "latest" and not read_case(case).get("latest_run"):
        return None
    selected = resolve_latest(case, run_id)
    parent = Path(case) / "previews"
    if not parent.is_dir() or parent.is_symlink():
        return None
    candidates = sorted((path for path in parent.glob(selected + "-compact-*")
                         if PREVIEW_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()),
                        key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for directory in candidates:
        try:
            compaction_preview_metadata(case, selected, directory.name)
            return directory.name
        except (TraceError, OSError, ValueError, TypeError, KeyError):
            continue
    return None


def _display(value):
    return f"{value:,.1f}" if type(value) in (int, float) and math.isfinite(value) and value >= 0 else "??"


def _inline(svg, prefix):
    # Both generated SVGs share element IDs. Namespace the first one's IDs and
    # references so marker/clip paths cannot point into the other drawing.
    value = svg.decode("utf-8")
    value = re.sub(r'(?<![\w-])id="([^"]+)"', lambda m: 'id="' + prefix + m[1] + '"', value)
    value = re.sub(r'url\(#([^)]+)\)', lambda m: 'url(#' + prefix + m[1] + ')', value)
    value = value.replace('aria-labelledby="title desc"', 'aria-labelledby="' + prefix + 'title ' + prefix + 'desc"')
    return value


def export_compaction(before, after, directory, *, archive_sha256, service_sha256):
    result = export_layout(after, directory)
    directory = Path(result["directory"])
    try:
        report = after["layout"]["compaction"]
        plan = make_plan(after)
        save_json(directory / "before.json", before)
        before_svg = render_svg(before)
        (directory / "before.svg").write_bytes(before_svg)
        baseline_page = _preview_html(before, before_svg, before["layout"].get("metrics", {}))
        baseline_page = baseline_page.replace('href="graph.svg"', 'href="before.svg"').replace('href="graph.json"', 'href="before.json"')
        (directory / "before.html").write_text(baseline_page, encoding="utf-8")
        save_json(directory / "miro-plan.json", plan)
        meta = {"schema_version": 1, "preview_id": directory.name, "run_id": after["run_id"],
                "case_id": after["namespace"]["case_id"], "namespace": after["namespace"],
                "archive_sha256": archive_sha256, "service_sha256": service_sha256,
                "plan_sha256": plan["sha256"], "include_fees": after["include_fees"],
                "connector_style": after["graph_options"]["connector_style"], "compaction": report}
        save_json(directory / "compaction.json", meta)
        rows = []
        for group, key, title in (("main", "width", "Main graph width"), ("main", "height", "Main graph height"),
                                 ("main", "area", "Main graph area"), ("main", "edge_length", "Connection length"),
                                 ("main", "address_distance", "Address-to-transaction distance"),
                                 ("board", "width", "Full drawing width"), ("board", "height", "Full drawing height")):
            values = [_display(report.get(phase, {}).get(group, {}).get(key)) for phase in ("before", "after")]
            rows.append(f"<tr><th>{title}</th><td>{values[0]}</td><td>{values[1]}</td></tr>")
        summary = ("No safe size or proximity improvement was found; the ELK layout was retained."
                   if report.get("unchanged") else
                   f"Moved {report.get('moved_addresses', 0)} addresses and {report.get('moved_components', 0)} activity components.")
        limit = (" Some optimization checks reached their work budget. Unchecked moves were skipped; the full graph remains present."
                 if report.get("truncated") else "")
        document = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Liquid trace · Compact graph comparison</title><style>
body{margin:0;background:#f5f6f8;color:#172033;font:14px system-ui,sans-serif}header{padding:20px;background:white}
h1{font-size:22px}p{max-width:85em;line-height:1.6}a{color:#155e75}table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 14px;border-bottom:1px solid #d5dbe3;text-align:right}th:first-child{text-align:left}details{padding:12px;background:white}
summary{cursor:pointer;font-size:18px;font-weight:600;margin:8px}.chart{overflow:auto}.chart svg{display:block;max-width:none}
.metrics{overflow:auto}body:has(#chart:target) header,body:has(#chart:target) .baseline{display:none}#chart:target svg{width:100%;height:auto}
</style></head><body><header><h1>Compact graph comparison</h1>'''
        document += "<p>" + html.escape(summary + limit) + "</p>"
        document += "<p>Before is a fresh ELK layout of the selected saved run, not your current Miro arrangement. Node sizes and default clearances are retained. Label bounds and curved paths are estimates; Miro routes may differ.</p>"
        document += '<div class="metrics"><table><thead><tr><th>Measure</th><th>Before</th><th>After</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
        document += '<p><a href="before.html" target="_blank" rel="noopener noreferrer">Open before separately</a> · <a href="graph.svg" download>Download compact SVG</a> · <a href="before.svg" download>Download before SVG</a> · <a href="layout-report.json" download>Layout report</a></p>'
        document += '<p>Review both drawings, then return to the investigation and choose Apply compact layout to Miro. Applying replaces positions of managed graph objects. Calculating this preview makes no board changes.</p></header>'
        # Keep the baseline in its own complete document. A collapsed details
        # element would still instantiate a second huge SVG DOM in the browser.
        document += '<details class="baseline"><summary>Before: ELK layout</summary><p><a href="before.html" target="_blank" rel="noopener noreferrer">Open the full before drawing</a> to compare it with the compact layout below.</p></details>'
        document += '<details open><summary>After: compact layout</summary><main id="chart" class="chart">' + _inline((directory / "graph.svg").read_bytes(), "after-") + '</main></details></body></html>\n'
        temporary = directory / "comparison.tmp"
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(directory / "graph.html")
        # The manifest is the completion marker. Partial products cannot be
        # rediscovered or applied after cancellation or an interrupted write.
        manifest = "".join(_hash_file(directory / name) + "  " + name + "\n" for name in sorted(FILES))
        temporary = directory / "SHA256SUMS.tmp"
        temporary.write_text(manifest, encoding="utf-8")
        temporary.replace(directory / "SHA256SUMS")
    except (OSError, ValueError, KeyError, TypeError) as error:
        (directory / "SHA256SUMS").unlink(missing_ok=True)
        raise TraceError("The compact preview could not be completed; saved investigation evidence is unchanged") from error
    return {**result, "preview_id": directory.name, "compaction": report}
