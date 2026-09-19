"""Reimportable CSVs from one current settings snapshot, without chain queries."""

import csv
import io
import uuid
import zipfile
from pathlib import Path

from . import address_import, change_output_import, name_color_import
from .common import TraceError
from .name_colors import _names
from .services import load_services, notes_for, rule_fields

KINDS = ("attributions", "name-colors", "change-outputs")
HEADERS = {
    "attributions": ("Address", "Name", "confidence", "stop_tracing", "hop_limit",
                     "source", "notes", "observed_at", "enabled"),
    "name-colors": ("Name", "Color"),
    "change-outputs": ("Txid", "ChangeVout", "Notes"),
}
IMPORTERS = {"attributions": address_import, "name-colors": name_color_import,
             "change-outputs": change_output_import}


def _rows(settings, kind):
    if kind == "attributions":
        for address, rule in sorted(settings["rules"].items()):
            fields = rule_fields(rule)
            yield (address, rule["name"], fields["confidence"], str(fields["stop_tracing"]).lower(),
                   fields["hop_limit"], fields["source"], notes_for(rule), fields["observed_at"],
                   str(rule["enabled"]).lower())
    elif kind == "name-colors":
        groups = _names(settings)
        for key, color in sorted(settings.get("name_colors", {}).items()):
            # Include retained assignments even when their name has no current addresses.
            yield (min(groups[key]["variants"]), color)
    else:
        for txid, change in sorted(settings.get("change_outputs", {}).items()):
            yield (txid, change["vout"], change["notes"])


def _csv_row(row):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerow(row)
    return stream.getvalue().encode("utf-8")


def _parts(settings, kind):
    """Split only between records, counting encoded bytes including each header."""
    importer = IMPORTERS[kind]
    header = _csv_row(HEADERS[kind])
    parts, current, count, total = [], bytearray(header), 0, 0
    for row in _rows(settings, kind):
        encoded = _csv_row(row)
        if len(header) + len(encoded) > importer.MAX_BYTES:
            raise TraceError("A saved " + kind + " row exceeds its CSV import size limit")
        if count and (count >= importer.MAX_ROWS or len(current) + len(encoded) > importer.MAX_BYTES):
            parts.append(bytes(current))
            current, count = bytearray(header), 0
        current.extend(encoded)
        count += 1
        total += 1
    parts.append(bytes(current))
    if len(parts) == 1:
        return [(kind + ".csv", parts[0])], total
    return [(f"{kind}-part-{index:03d}.csv", data) for index, data in enumerate(parts, 1)], total


def _zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in files:
            # Fixed timestamps and permissions keep unchanged exports byte-for-byte stable.
            member = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = 0o100600 << 16
            archive.writestr(member, data)
    return stream.getvalue()


def build_input_export(case, kind="all"):
    """Export every saved entry in selected inputs, independent of UI pagination."""
    if not isinstance(kind, str) or kind not in ("all", *KINDS):
        raise TraceError("Choose all, attributions, name-colors, or change-outputs to export")
    settings = load_services(case)
    files, counts = [], {}
    for selected in KINDS if kind == "all" else (kind,):
        parts, count = _parts(settings, selected)
        files.extend(parts)
        counts[selected] = count
    zipped = kind == "all" or len(files) > 1
    filename = ("input-csvs.zip" if kind == "all" else kind + ".zip") if zipped else files[0][0]
    return {"filename": filename, "content_type": "application/zip" if zipped else "text/csv; charset=utf-8",
            "data": _zip(files) if zipped else files[0][1], "rows": counts, "parts": len(files),
            "revision": settings["revision"], "kind": kind}


def save_input_export(case, kind="all", out=None):
    """Save into a new directory, never modifying prior exports or run evidence."""
    case = Path(case).expanduser()
    destination = (Path(out).expanduser() if out is not None else
                   case / "exports" / ("inputs-" + uuid.uuid4().hex))
    if destination.exists() or destination.is_symlink():
        raise TraceError("Input export directory already exists; choose a new directory")
    if destination.resolve().is_relative_to((case / "runs").resolve()):
        raise TraceError("Save input exports outside runs/ to preserve archived evidence")
    result = build_input_export(case, kind)
    try:
        destination.mkdir(parents=True, mode=0o700, exist_ok=False)
        path = destination / result["filename"]
        with path.open("xb") as stream:
            stream.write(result["data"])
    except FileExistsError as error:
        raise TraceError("Input export directory or file already exists; choose a new directory") from error
    return {key: result[key] for key in ("filename", "kind", "rows", "parts", "revision")} | {
        "directory": str(destination.resolve()), "path": str(path.resolve())}
