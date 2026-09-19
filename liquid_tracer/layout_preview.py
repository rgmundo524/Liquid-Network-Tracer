"""Portable, offline SVG previews of an already calculated display layout.

This renderer consumes display geometry only. It never traces, contacts Miro,
or changes an archived graph. Each physical edge remains a separate SVG path.
"""

import html
import math
import os
import re
from pathlib import Path

from .name_colors import color_text
from .graph_markers import node_border
from .connector_styles import stroke_width
from .common import TraceError, save_json
from .export import COLORS, edge_color, legend_lines
from .edge_labels import (FONT_SIZE, LINE_HEIGHT, PADDING_Y, caption_box, caption_text,
                          validate_label_layout)


LAYOUT_NOTICE = "ELK layout; Miro routes may differ. Crossing counts are estimates."
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_PERCENT = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)%\Z")
_EXPLORER_URL = re.compile(
    r"https://blockstream\.info/(?:liquid|liquidtestnet)/"
    r"(?:tx/[0-9a-fA-F]{64}|address/[a-zA-Z0-9]{1,200})\Z"
)


def _explorer_url(graph, node):
    # Display data is not trusted HTML. Permit only the exact public explorer
    # routes emitted by build_graph, with no credentials, query, or fragments.
    # Fixtures and synthetic event nodes never link to unrelated live evidence.
    url = node.get("url")
    if (graph.get("simulated") or node["kind"] not in ("transaction", "address")
            or not isinstance(url, str) or not _EXPLORER_URL.fullmatch(url)):
        return None
    expected = "/tx/" if node["kind"] == "transaction" else "/address/"
    return url if expected in url else None


def layout_title(graph):
    layout = graph.get("layout", {})
    if layout.get("algorithm") == "dependency_layers_v1":
        return "Dependency layout fallback" if layout.get("fallback_reason") else "Dependency layout"
    return "ELK layout"


def layout_notice(graph):
    layout = graph.get("layout", {})
    if layout.get("algorithm") == "dependency_layers_v1":
        return (str(layout.get("fallback_notice") or "Dependency layout; ELK optimization was not applied.")
                + " Miro routes may differ. Crossing counts are estimates.")
    notice = LAYOUT_NOTICE
    changes = layout.get("change_outputs")
    if changes:
        notice += f" Change rows: {len(changes.get('applied', []))} aligned; {len(changes.get('skipped', []))} skipped."
    return notice


def _number(value, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (positive and value <= 0)):
        raise TraceError("Local SVG preview contains invalid geometry")
    try:
        result = float(value)
    except OverflowError as error:
        raise TraceError("Local SVG preview contains invalid geometry") from error
    if not math.isfinite(result):
        raise TraceError("Local SVG preview contains invalid geometry")
    return result


def _text(value):
    # XML 1.0 does not permit most control characters or unpaired surrogates.
    value = str(value)
    return "".join(char if char in "\n\t" or 0x20 <= ord(char) <= 0xD7FF
                   or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
                   else " " for char in value)


def _escape(value):
    return html.escape(_text(value), quote=True)


def _fmt(value):
    # Check derived values too: finite input coordinates can still overflow
    # when a bounding box, attachment or curved segment is calculated.
    return format(_number(value), ".3f").rstrip("0").rstrip(".") or "0"


def _point(value):
    if not isinstance(value, dict):
        raise TraceError("ELK preview contains an invalid connector route")
    return (_number(value.get("x")), _number(value.get("y")))


def _fraction(value):
    if not isinstance(value, str) or not _PERCENT.fullmatch(value):
        raise TraceError("ELK preview contains an invalid connection attachment")
    result = float(value[:-1]) / 100
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise TraceError("ELK preview contains an invalid connection attachment")
    return result


def _attachment(edge, key, node, other, source):
    attachments = edge.get("attachment")
    if attachments is not None:
        try:
            position = attachments[key]["position"]
            px, py = _fraction(position["x"]), _fraction(position["y"])
        except (KeyError, TypeError) as error:
            raise TraceError("ELK preview contains an invalid connection attachment") from error
        return (node["x"] + (px - .5) * node["width"],
                node["y"] + (py - .5) * node["height"])
    # A valid legacy graph can still be previewed. Transactions use fixed sides;
    # address/event fallback points intersect the actual ellipse/diamond.
    if node["kind"] in ("transaction", "context_group"):
        return (node["x"] + node["width"] / 2 * (1 if source else -1), node["y"])
    dx, dy = _number(other["x"] - node["x"]), _number(other["y"] - node["y"])
    if dx == dy == 0:
        dx = 1 if source else -1
    direction_scale = max(abs(dx), abs(dy))
    dx, dy = dx / direction_scale, dy / direction_scale
    rx, ry = _number(node["width"] / 2, positive=True), _number(node["height"] / 2, positive=True)
    scale = (1 / (abs(dx) / rx + abs(dy) / ry) if node["kind"] == "event"
             else 1 / math.hypot(dx / rx, dy / ry))
    return (node["x"] + dx * scale, node["y"] + dy * scale)


def _geometry(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise TraceError("ELK preview requires graph nodes and edges")
    if not graph["nodes"]:
        raise TraceError("Local SVG preview requires a nonempty graph")
    nodes = {}
    for item in graph["nodes"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in nodes
                or item.get("kind") not in ("transaction", "address", "event", "context_group")):
            raise TraceError("ELK preview contains an invalid or duplicate node")
        node = dict(item)
        for key in ("x", "y", "width", "height"):
            node[key] = _number(node.get(key), positive=key in ("width", "height"))
        node["color"] = node.get("color", COLORS.get(node["kind"], COLORS["address"]))
        if not isinstance(node["color"], str) or not _COLOR.fullmatch(node["color"]):
            raise TraceError("ELK preview contains an invalid node color")
        nodes[node["id"]] = node
    edges, seen = [], set()
    for item in graph["edges"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in seen
                or not isinstance(item.get("source"), str) or item["source"] not in nodes
                or not isinstance(item.get("target"), str) or item["target"] not in nodes
                or not isinstance(item.get("role", ""), str)):
            raise TraceError("ELK preview contains an invalid edge or missing endpoint")
        seen.add(item["id"])
        edge = dict(item)
        validate_label_layout(edge)
        edge["connector_shape"] = edge.get("connector_shape", "straight")
        if edge["connector_shape"] not in ("straight", "curved", "elbowed"):
            raise TraceError("ELK preview contains an unsupported connector appearance")
        route = edge.get("route")
        if route is not None:
            if not isinstance(route, list) or len(route) < 2:
                raise TraceError("ELK preview contains an invalid connector route")
            route = [_point(point) for point in route]
        start, end = nodes[edge["source"]], nodes[edge["target"]]
        first = _attachment(edge, "startItem", start, end, True)
        last = _attachment(edge, "endItem", end, start, False)
        # Keep the ELK bends for routed exceptions and anchor the path to the
        # same explicit ports sent to Miro. Straight mode ignores bendpoints.
        points = [first, *(route[1:-1] if route and edge["connector_shape"] != "straight" else []), last]
        edge["points"] = [point for index, point in enumerate(points) if not index or point != points[index - 1]]
        if len(edge["points"]) < 2:
            edge["points"] = [first, last]
        edges.append(edge)
    return sorted(nodes.values(), key=lambda node: node["id"]), sorted(edges, key=lambda edge: edge["id"])


def _path(points, curved=False):
    def pair(point):
        return _fmt(point[0]) + " " + _fmt(point[1])
    parts = ["M " + pair(points[0])]
    for index, point in enumerate(points[1:-1], 1):
        if not curved:
            parts.append("L " + pair(point))
            continue
        previous, following = points[index - 1], points[index + 1]
        first = math.dist(previous, point)
        second = math.dist(point, following)
        radius = min(14, first / 3, second / 3)
        if not radius:
            parts.append("L " + pair(point))
            continue
        before = tuple(point[axis] + (previous[axis] - point[axis]) * (radius / first) for axis in (0, 1))
        after = tuple(point[axis] + (following[axis] - point[axis]) * (radius / second) for axis in (0, 1))
        parts.extend(["L " + pair(before), "Q " + pair(point) + " " + pair(after)])
    parts.append("L " + pair(points[-1]))
    return " ".join(parts)


def _midpoint(points):
    lengths = [_number(math.dist(start, end)) for start, end in zip(points, points[1:])]
    longest = max(lengths, default=0)
    if not longest:
        return points[0]
    # Relative lengths avoid overflow in the total for long routes. Compute
    # the fraction first so interpolation also works at large coordinates.
    weights = [length / longest for length in lengths]
    remaining = sum(weights) / 2
    for start, end, weight in zip(points, points[1:], weights):
        if remaining <= weight and weight:
            fraction = remaining / weight
            return tuple(start[axis] + (end[axis] - start[axis]) * fraction for axis in (0, 1))
        remaining -= weight
    return points[0]


def _short_lines(value, width, height, kind, reserved_lines=0):
    # Shorten visible labels instead of changing their forensic source values.
    # Titles and graph.json retain the complete original text.
    font = 12
    usable = width * (.58 if kind == "event" else .72 if kind == "address" else .85)
    chars = max(1, min(120, int(usable / (font * .63))))
    vertical = .45 if kind == "event" else .56 if kind == "address" else .68
    limit = max(1, min(12, int(height * vertical / 16)) - reserved_lines)
    original = _text(value).replace("\t", " ").splitlines() or [""]
    lines = [line if len(line) <= chars else line[:max(0, chars - 1)] + "…" for line in original[:limit]]
    if len(original) > limit:
        lines[-1] = lines[-1][:max(0, chars - 1)] + "…"
    return lines


def drawing_bounds(nodes, edges):
    """Bounds of everything drawn, including markers, borders and caption halos."""
    boxes = []
    for node in nodes:
        padding = node_border(node)[1] / 2
        boxes.append((node["x"] - node["width"] / 2 - padding,
                      node["y"] - node["height"] / 2 - padding,
                      node["x"] + node["width"] / 2 + padding,
                      node["y"] + node["height"] / 2 + padding))
    for edge in edges:
        # The SVG arrow is nine units long, independent of line thickness.
        padding = max(9, stroke_width(edge.get("role", "")) / 2)
        for x, y in edge["points"]:
            boxes.append((x - padding, y - padding, x + padding, y + padding))
        if caption_text(edge):
            left, top, right, bottom = caption_box(edge, edge["points"])
            boxes.append((left - 3, top - 3, right + 3, bottom + 3))
    return tuple(_number(value) for value in (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes)))


def _svg(graph, nodes, edges, *, banner=True):
    notice = ("Dependency layout fallback · Full graph retained; crossing optimization skipped."
              if graph.get("layout", {}).get("fallback_reason") else layout_notice(graph))
    left, top, right, bottom = drawing_bounds(nodes, edges)
    margin = 180
    x, y = left - margin, top - margin
    width, height = max(800, right - left + margin * 2), bottom - top + margin * 2
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" role="group" aria-labelledby="title desc" '
             f'width="{_fmt(width)}" height="{_fmt(height)}" viewBox="{_fmt(x)} {_fmt(y)} {_fmt(width)} {_fmt(height)}">',
             '<title id="title">Liquid trace · ' + _escape(layout_title(graph)) + '</title>',
             '<desc id="desc">' + _escape(layout_notice(graph) + " " + str(graph.get("notice", ""))) + '</desc>',
             '<style>.explorer-link { cursor:pointer; } '
             '.explorer-link:focus-visible > g '
             '{ filter:drop-shadow(0 0 5px #0f766e); }</style>',
             '<defs>']
    for key, color in (("traced", COLORS["traced_edge"]), ("context", COLORS["context_edge"])):
        lines.append(f'<marker id="arrow-{key}" markerWidth="9" markerHeight="7" refX="8" refY="3.5" '
                     f'orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 0 L 9 3.5 L 0 7 Z" fill="{color}"/></marker>')
    lines.extend(['</defs>', f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(width)}" height="{_fmt(height)}" fill="white"/>',
                  *([f'<text x="{_fmt(left)}" y="{_fmt(top - 100)}" font-family="sans-serif" font-size="15" fill="#475569">'
                     + _escape(notice) + '</text>'] if banner else []), '<g id="edges" fill="none">'])
    for index, edge in enumerate(edges):
        marker = "context" if edge.get("role", "").startswith("context") else "traced"
        caption = _text(caption_text(edge))
        lines.append(f'<path id="edge-{index}" data-edge-id="{_escape(edge["id"])}" '
                     f'data-source="{_escape(edge["source"])}" data-target="{_escape(edge["target"])}" '
                     f'data-appearance="{edge["connector_shape"]}" d="{_path(edge["points"], edge["connector_shape"] == "curved")}" '
                     f'stroke="{edge_color(edge.get("role", ""))}" stroke-width="{stroke_width(edge.get("role", ""))}" marker-end="url(#arrow-{marker})">'
                     f'<title>{_escape(caption)}</title></path>')
    lines.append('</g><g id="nodes" font-family="sans-serif" text-anchor="middle" fill="#172033">')
    for index, node in enumerate(nodes):
        cx, cy, width, height = node["x"], node["y"], node["width"], node["height"]
        border, thickness = node_border(node)
        style = f'fill="{node["color"]}" stroke="{border}" stroke-width="{thickness}"'
        url = _explorer_url(graph, node)
        title = str(node.get("label", ""))
        if url:
            title += "\nOpen Blockstream explorer in a new tab"
            lines.append(f'<a class="explorer-link" href="{_escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer" referrerpolicy="no-referrer" tabindex="0" '
                         f'aria-label="{_escape(title)}">')
        lines.append(f'<g id="node-{index}" data-node-id="{_escape(node["id"])}"><title>{_escape(title)}</title>')
        if node["kind"] in ("transaction", "context_group"):
            shape = f'<rect x="{_fmt(cx - width / 2)}" y="{_fmt(cy - height / 2)}" width="{_fmt(width)}" height="{_fmt(height)}" {style}/>'
        elif node["kind"] == "address":
            shape = f'<ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt(width / 2)}" ry="{_fmt(height / 2)}" {style}/>'
        else:
            polygon = [(cx, cy - height / 2), (cx + width / 2, cy), (cx, cy + height / 2), (cx - width / 2, cy)]
            shape = '<polygon points="' + ' '.join(_fmt(a) + ',' + _fmt(b) for a, b in polygon) + f'" {style}/>'
        lines.append(shape)
        inset_x, inset_y = width * .22, height * .22
        if node["kind"] == "address":
            inset_x = width * .10  # Match the label width; keep the Suspected prefix visible.
        if node["kind"] in ("transaction", "context_group"):
            inset_x, inset_y = width * .06, height * .06
        lines.append(f'<clipPath id="label-clip-{index}"><rect x="{_fmt(cx - width / 2 + inset_x)}" '
                     f'y="{_fmt(cy - height / 2 + inset_y)}" width="{_fmt(width - inset_x * 2)}" '
                     f'height="{_fmt(height - inset_y * 2)}"/></clipPath><g clip-path="url(#label-clip-{index})">')
        # Reserve a label row inside the existing shape rather than changing
        # ELK geometry or putting link text over a neighboring connector.
        show_link_label = bool(url and width - inset_x * 2 >= 64 and height - inset_y * 2 >= 36)
        from .address_counts import caption as count_caption
        count = count_caption(node)
        reserved = int(show_link_label) + int(count is not None)
        labels = _short_lines(node.get("label", ""), width, height, node["kind"], reserved)
        row_count = len(labels) + reserved
        for offset, label in enumerate(labels):
            baseline = cy - (row_count - 1) * 8 + offset * 16 + 4
            lines.append(f'<text x="{_fmt(cx)}" y="{_fmt(baseline)}" font-size="12" fill="{color_text(node["color"])}">{_escape(label)}</text>')
        if show_link_label:
            baseline = cy - (row_count - 1) * 8 + len(labels) * 16 + 4
            lines.append(f'<text x="{_fmt(cx)}" y="{_fmt(baseline)}" font-size="11" '
                         f'fill="{color_text(node["color"])}" text-decoration="underline">Explorer</text>')
        if count is not None:
            baseline = cy + (row_count - 1) * 8 + 4
            lines.append(f'<text class="address-tx-count" x="{_fmt(cx)}" y="{_fmt(baseline)}" '
                         f'font-size="11" fill="{color_text(node["color"])}">{_escape(count)}</text>')
        lines.append('</g></g>')
        if url:
            lines.append('</a>')
    lines.append(f'</g><g id="captions" font-family="sans-serif" font-size="{FONT_SIZE}" text-anchor="middle" fill="#334155">')
    for edge in edges:
        caption = _text(caption_text(edge)).replace("\t", "    ")
        if not caption:
            continue
        left, top, right, _ = caption_box(edge, edge["points"])
        cx = (left + right) / 2
        for row, text in enumerate(caption.splitlines()):
            baseline = top + PADDING_Y + FONT_SIZE + row * LINE_HEIGHT
            lines.append(f'<text x="{_fmt(cx)}" y="{_fmt(baseline)}" stroke="white" stroke-width="5" '
                         f'stroke-linejoin="round" paint-order="stroke">{_escape(text)}</text>')
    lines.append('</g></svg>\n')
    return "\n".join(lines).encode("utf-8")


def render_svg(graph):
    """Render validated geometry directly, without Node, Chromium, or layout work."""
    return _svg(graph, *_geometry(graph))


def _metrics_table(metrics, title="ELK layout"):
    rows = []
    for key, label in (("crossings", "Line crossings"), ("node_overlaps", "Object overlaps"),
                       ("node_intersections", "Lines through objects")):
        values = []
        for phase in ("before", "after"):
            group = metrics.get(phase, {})
            value = group.get(key) if isinstance(group, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values.append(("≥ " if group.get("truncated") is True else "") + str(value))
            else:
                values.append("??")
        rows.append(f'<tr><th scope="row">{label}</th><td>{values[0]}</td><td>{values[1]}</td></tr>')
    return '<table><caption>Estimated layout quality</caption><thead><tr><th>Measure</th><th>Baseline layout</th><th>' + _escape(title) + '</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'


def _preview_html(graph, svg, metrics, *, detail_pages=False):
    # SVG anchors are disabled inside an <img>. Inline only our escaped,
    # allowlisted renderer output so links work offline and in the local UI.
    from .attribution_presentation import register_html
    inline_svg = svg.decode("utf-8")
    detail_link = ' · <a href="details.html">Open printable overview and detail pages</a>' if detail_pages else ""
    legend = "".join("<li>" + _escape(line) + "</li>" for line in legend_lines(graph))
    simulated = " · Synthetic data" if graph.get("simulated") else ""
    fees = "included" if graph.get("include_fees") else "hidden"
    change_report = graph.get("layout", {}).get("change_outputs", {})
    skipped_changes = change_report.get("skipped", [])
    change_details = ("<details><summary>Skipped change rows</summary><ul>"
                      + "".join("<li>" + _escape(item.get("outpoint", "")) + ": " + _escape(item.get("reason", "")) + "</li>"
                                for item in skipped_changes) + "</ul></details>") if skipped_changes else ""
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Liquid trace · {_escape(layout_title(graph))}</title><style>
body {{ margin:0; color:#172033; background:#f5f6f8; font:14px system-ui,sans-serif; }}
header {{ padding:16px 24px; background:white; border-bottom:1px solid #d5dbe3; }}
h1 {{ font-size:22px; margin:0 0 8px; }} p {{ margin:6px 0; }} a {{ color:#155e75; }}
.summary {{ display:flex; gap:24px; flex-wrap:wrap; align-items:start; }} .summary>div {{ flex:1; min-width:240px; }}
table {{ border-collapse:collapse; font-size:13px; }} caption {{ text-align:left; font-weight:600; margin-bottom:4px; }}
th,td {{ padding:3px 12px 3px 0; text-align:left; }} td {{ text-align:right; }} tbody th {{ font-weight:400; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
details {{ margin-top:8px; }} summary {{ cursor:pointer; }} li {{ margin:4px 0; }}
.chart {{ overflow:auto; background:white; }} .chart svg {{ display:block; max-width:none; }}
body:has(#chart:target) header {{ display:none; }}
#chart:target svg {{ width:100%; height:auto; }}
</style></head><body><header><div class="summary"><div><h1>Liquid trace · {_escape(layout_title(graph))}</h1>
<p>Run {_escape(graph.get('run_id', ''))} · {len(graph['nodes'])} nodes · {len(graph['edges'])} links · Fees {fees}{simulated}</p>
<p>{_escape(layout_notice(graph))}</p><p>Scroll to explore; use your browser zoom to adjust the scale. Select Explorer on a transaction or address to open Blockstream in a new tab.</p>
<p><a href="graph.svg" download>Download SVG</a> · <a href="graph.json" download>Graph details</a> ·
<a href="layout-report.json" download>Layout report</a>{detail_link}</p></div>{_metrics_table(metrics, layout_title(graph))}</div>
{register_html(graph)}
{change_details}
<details><summary>Legend and evidence notes</summary><p>{_escape(graph.get('notice', ''))}</p>
<p>Before uses the saved graph's baseline layout, not live Miro positions. Collision counts exclude label boxes and Miro's automatic curves.
Counts prefixed with ≥ are lower bounds because the comparison limit was reached.</p><ul>{legend}</ul></details></header>
<main id="chart" class="chart">{inline_svg}</main>
</body></html>\n'''


def export_layout(graph, directory):
    """Write a new preview, publishing graph.html only after every file succeeds."""
    nodes, edges = _geometry(graph)
    svg = _svg(graph, nodes, edges)
    layout = graph.get("layout", {})
    if not isinstance(layout, dict) or not isinstance(layout.get("metrics", {}), dict):
        raise TraceError("ELK preview contains invalid layout metadata")
    metrics = layout.get("metrics", {})
    from .layout_details import render_details
    details_document, details_index = render_details(graph, nodes, edges, svg)
    document = _preview_html(graph, svg, metrics, detail_pages=True)
    directory = Path(os.path.abspath(directory))
    # A preview must never silently replace an archived run or follow a link
    # into another investigation. The caller normally supplies a unique path.
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        raise TraceError("ELK preview output directory cannot contain symbolic links")
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise TraceError("ELK preview output directory already exists; choose a new directory") from error
    except OSError as error:
        raise TraceError("Cannot create the ELK preview output directory") from error
    paths = {"directory": directory, "svg": directory / "graph.svg", "html": directory / "graph.html",
             "graph": directory / "graph.json", "report": directory / "layout-report.json",
             "details": directory / "details.html", "details_index": directory / "details.json"}
    temporary = paths["html"].with_name("graph.html.tmp")
    try:
        save_json(paths["graph"], graph)
        save_json(paths["report"], {"run_id": graph.get("run_id"), "layout": layout,
                                  "metrics": metrics, "notice": layout_notice(graph),
                                  "node_count": len(nodes), "edge_count": len(edges)})
        paths["svg"].write_bytes(svg)
        save_json(paths["details_index"], details_index)
        paths["details"].write_text(details_document, encoding="utf-8")
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(paths["html"])
    except (OSError, TypeError, ValueError) as error:
        paths["html"].unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise TraceError("Cannot finish the ELK preview; any graph and report files were retained for diagnosis") from error
    return {**{key: str(path) for key, path in paths.items()}, "layout_metrics": metrics}
