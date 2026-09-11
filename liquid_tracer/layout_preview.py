"""Portable, offline SVG previews of an already calculated display layout.

This renderer consumes display geometry only. It never traces, contacts Miro,
or changes an archived graph. Each physical edge remains a separate SVG path.
"""

import base64
import html
import math
import os
import re
from pathlib import Path

from .common import TraceError, save_json
from .export import COLORS, edge_color, legend_lines


LAYOUT_NOTICE = "ELK layout; Miro routes may differ. Crossing counts are estimates."
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_PERCENT = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)%\Z")
_MAX_ITEMS = 50000
_MAX_COORDINATE = 10000000
_MAX_ROUTE_POINTS = 1000
_MAX_TOTAL_ROUTE_POINTS = 250000


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
    return LAYOUT_NOTICE


def _number(value, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (positive and value <= 0)):
        raise TraceError("Local SVG preview contains invalid geometry")
    if abs(value) > _MAX_COORDINATE:
        raise TraceError(f"Local SVG preview exceeds the coordinate limit of {_MAX_COORDINATE:,}; "
                         "export CSV for the complete graph or select an earlier saved run for a smaller preview")
    if not math.isfinite(value):
        raise TraceError("Local SVG preview contains invalid geometry")
    return float(value)


def _text(value):
    # XML 1.0 does not permit most control characters or unpaired surrogates.
    value = str(value)
    return "".join(char if char in "\n\t" or 0x20 <= ord(char) <= 0xD7FF
                   or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
                   else " " for char in value)


def _escape(value):
    return html.escape(_text(value), quote=True)


def _fmt(value):
    return format(value, ".3f").rstrip("0").rstrip(".") or "0"


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
    if node["kind"] == "transaction":
        return (node["x"] + node["width"] / 2 * (1 if source else -1), node["y"])
    dx, dy = other["x"] - node["x"], other["y"] - node["y"]
    if dx == dy == 0:
        dx = 1 if source else -1
    rx, ry = node["width"] / 2, node["height"] / 2
    scale = (1 / (abs(dx) / rx + abs(dy) / ry) if node["kind"] == "event"
             else 1 / math.sqrt((dx / rx) ** 2 + (dy / ry) ** 2))
    return (node["x"] + dx * scale, node["y"] + dy * scale)


def _geometry(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise TraceError("ELK preview requires graph nodes and edges")
    if not graph["nodes"]:
        raise TraceError("Local SVG preview requires a nonempty graph")
    if len(graph["nodes"]) + len(graph["edges"]) > _MAX_ITEMS:
        raise TraceError(f"Local SVG preview has {len(graph['nodes']):,} objects and {len(graph['edges']):,} connections; "
                         f"its display limit is {_MAX_ITEMS:,} combined. Export CSV for the complete graph "
                         "or select an earlier saved run for a smaller preview; no objects were omitted")
    nodes = {}
    for item in graph["nodes"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in nodes
                or item.get("kind") not in ("transaction", "address", "event")):
            raise TraceError("ELK preview contains an invalid or duplicate node")
        node = dict(item)
        for key in ("x", "y", "width", "height"):
            node[key] = _number(node.get(key), positive=key in ("width", "height"))
        node["color"] = node.get("color", COLORS[node["kind"]])
        if not isinstance(node["color"], str) or not _COLOR.fullmatch(node["color"]):
            raise TraceError("ELK preview contains an invalid node color")
        nodes[node["id"]] = node
    edges, seen, route_points = [], set(), 0
    for item in graph["edges"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in seen
                or not isinstance(item.get("source"), str) or item["source"] not in nodes
                or not isinstance(item.get("target"), str) or item["target"] not in nodes
                or not isinstance(item.get("role", ""), str)):
            raise TraceError("ELK preview contains an invalid edge or missing endpoint")
        seen.add(item["id"])
        edge = dict(item)
        edge["connector_shape"] = edge.get("connector_shape", "straight")
        if edge["connector_shape"] not in ("straight", "curved", "elbowed"):
            raise TraceError("ELK preview contains an unsupported connector appearance")
        route = edge.get("route")
        if route is not None:
            if not isinstance(route, list) or not 2 <= len(route) <= _MAX_ROUTE_POINTS:
                raise TraceError("ELK preview contains an invalid connector route")
            route_points += len(route)
            if route_points > _MAX_TOTAL_ROUTE_POINTS:
                raise TraceError("ELK preview connector routes exceed the display limit")
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
        before = tuple(point[axis] + (previous[axis] - point[axis]) * radius / first for axis in (0, 1))
        after = tuple(point[axis] + (following[axis] - point[axis]) * radius / second for axis in (0, 1))
        parts.extend(["L " + pair(before), "Q " + pair(point) + " " + pair(after)])
    parts.append("L " + pair(points[-1]))
    return " ".join(parts)


def _midpoint(points):
    remaining = sum(math.dist(start, end) for start, end in zip(points, points[1:])) / 2
    for start, end in zip(points, points[1:]):
        length = math.dist(start, end)
        if remaining <= length and length:
            return tuple(start[axis] + (end[axis] - start[axis]) * remaining / length for axis in (0, 1))
        remaining -= length
    return points[0]


def _short_lines(value, width, height, kind):
    # Shorten visible labels instead of changing their forensic source values.
    # Titles and graph.json retain the complete original text.
    font = 12
    usable = width * (.58 if kind == "event" else .72 if kind == "address" else .85)
    chars = max(1, min(120, int(usable / (font * .63))))
    limit = max(1, min(12, int(height * (.45 if kind == "event" else .68) / 16)))
    original = _text(value).replace("\t", " ").splitlines() or [""]
    lines = [line if len(line) <= chars else line[:max(0, chars - 1)] + "…" for line in original[:limit]]
    if len(original) > limit:
        lines[-1] = lines[-1][:max(0, chars - 1)] + "…"
    return lines


def _svg(graph, nodes, edges):
    banner = ("Dependency layout fallback · Full graph retained; crossing optimization skipped."
              if graph.get("layout", {}).get("fallback_reason") else layout_notice(graph))
    left = min(node["x"] - node["width"] / 2 for node in nodes)
    right = max(node["x"] + node["width"] / 2 for node in nodes)
    top = min(node["y"] - node["height"] / 2 for node in nodes)
    bottom = max(node["y"] + node["height"] / 2 for node in nodes)
    points = [point for edge in edges for point in edge["points"]]
    if points:
        left, right = min(left, min(p[0] for p in points)), max(right, max(p[0] for p in points))
        top, bottom = min(top, min(p[1] for p in points)), max(bottom, max(p[1] for p in points))
    margin = 180  # Includes abbreviated line captions at the outermost edges.
    x, y = left - margin, top - margin
    width, height = max(800, right - left + margin * 2), bottom - top + margin * 2
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc" '
             f'width="{_fmt(width)}" height="{_fmt(height)}" viewBox="{_fmt(x)} {_fmt(y)} {_fmt(width)} {_fmt(height)}">',
             '<title id="title">Liquid trace · ' + _escape(layout_title(graph)) + '</title>',
             '<desc id="desc">' + _escape(layout_notice(graph) + " " + str(graph.get("notice", ""))) + '</desc>',
             '<defs>']
    for key, color in (("traced", COLORS["traced_edge"]), ("context", COLORS["context_edge"])):
        lines.append(f'<marker id="arrow-{key}" markerWidth="9" markerHeight="7" refX="8" refY="3.5" '
                     f'orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 0 L 9 3.5 L 0 7 Z" fill="{color}"/></marker>')
    lines.extend(['</defs>', f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(width)}" height="{_fmt(height)}" fill="white"/>',
                  f'<text x="{_fmt(left)}" y="{_fmt(top - 100)}" font-family="sans-serif" font-size="15" fill="#475569">'
                  + _escape(banner) + '</text>', '<g id="edges" fill="none" stroke-width="2">'])
    for index, edge in enumerate(edges):
        marker = "context" if edge.get("role", "").startswith("context") else "traced"
        caption = _text(edge.get("label", "")) + (" · " + _text(edge["quantity"]) if edge.get("quantity") else "")
        lines.append(f'<path id="edge-{index}" data-edge-id="{_escape(edge["id"])}" '
                     f'data-source="{_escape(edge["source"])}" data-target="{_escape(edge["target"])}" '
                     f'data-appearance="{edge["connector_shape"]}" d="{_path(edge["points"], edge["connector_shape"] == "curved")}" '
                     f'stroke="{edge_color(edge.get("role", ""))}" marker-end="url(#arrow-{marker})">'
                     f'<title>{_escape(caption)}</title></path>')
    lines.append('</g><g id="nodes" font-family="sans-serif" text-anchor="middle" fill="#172033">')
    for index, node in enumerate(nodes):
        cx, cy, width, height = node["x"], node["y"], node["width"], node["height"]
        style = f'fill="{node["color"]}" stroke="#334155" stroke-width="2"'
        lines.append(f'<g id="node-{index}" data-node-id="{_escape(node["id"])}"><title>{_escape(node.get("label", ""))}</title>')
        if node["kind"] == "transaction":
            shape = f'<rect x="{_fmt(cx - width / 2)}" y="{_fmt(cy - height / 2)}" width="{_fmt(width)}" height="{_fmt(height)}" {style}/>'
        elif node["kind"] == "address":
            shape = f'<ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt(width / 2)}" ry="{_fmt(height / 2)}" {style}/>'
        else:
            polygon = [(cx, cy - height / 2), (cx + width / 2, cy), (cx, cy + height / 2), (cx - width / 2, cy)]
            shape = '<polygon points="' + ' '.join(_fmt(a) + ',' + _fmt(b) for a, b in polygon) + f'" {style}/>'
        lines.append(shape)
        inset_x, inset_y = width * .22, height * .22
        if node["kind"] == "transaction":
            inset_x, inset_y = width * .06, height * .06
        lines.append(f'<clipPath id="label-clip-{index}"><rect x="{_fmt(cx - width / 2 + inset_x)}" '
                     f'y="{_fmt(cy - height / 2 + inset_y)}" width="{_fmt(width - inset_x * 2)}" '
                     f'height="{_fmt(height - inset_y * 2)}"/></clipPath><g clip-path="url(#label-clip-{index})">')
        labels = _short_lines(node.get("label", ""), width, height, node["kind"])
        for offset, label in enumerate(labels):
            baseline = cy - (len(labels) - 1) * 8 + offset * 16 + 4
            lines.append(f'<text x="{_fmt(cx)}" y="{_fmt(baseline)}" font-size="12">{_escape(label)}</text>')
        lines.append('</g></g>')
    lines.append('</g><g id="captions" font-family="sans-serif" font-size="11" text-anchor="middle" fill="#334155">')
    for edge in edges:
        caption = _text(edge.get("label", "")) + (" · " + _text(edge["quantity"]) if edge.get("quantity") else "")
        if len(caption) > 48:
            caption = caption[:47] + "…"
        cx, cy = _midpoint(edge["points"])
        lines.append(f'<text x="{_fmt(cx)}" y="{_fmt(cy - 7)}" stroke="white" stroke-width="5" '
                     f'stroke-linejoin="round" paint-order="stroke">{_escape(caption)}</text>')
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


def _preview_html(graph, svg, metrics):
    encoded = base64.b64encode(svg).decode("ascii")
    legend = "".join("<li>" + _escape(line) + "</li>" for line in legend_lines())
    simulated = " · Synthetic demonstration data" if graph.get("simulated") else ""
    fees = "included" if graph.get("include_fees") else "hidden"
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
details {{ margin-top:8px; }} summary {{ cursor:pointer; }} li {{ margin:4px 0; }}
.chart {{ overflow:auto; background:white; }} .chart img {{ display:block; max-width:none; }}
body:has(#chart:target) header {{ display:none; }}
#chart:target img {{ width:100%; height:auto; }}
</style></head><body><header><div class="summary"><div><h1>Liquid trace · {_escape(layout_title(graph))}</h1>
<p>Run {_escape(graph.get('run_id', ''))} · {len(graph['nodes'])} nodes · {len(graph['edges'])} links · Fees {fees}{simulated}</p>
<p>{_escape(layout_notice(graph))}</p><p>Scroll to explore; use your browser zoom to adjust the scale.</p>
<p><a href="graph.svg" download>Download SVG</a> · <a href="graph.json" download>Graph details</a> ·
<a href="layout-report.json" download>Layout report</a></p></div>{_metrics_table(metrics, layout_title(graph))}</div>
<details><summary>Legend and evidence notes</summary><p>{_escape(graph.get('notice', ''))}</p>
<p>Before uses the saved graph's baseline layout, not live Miro positions. Labels and Miro's automatic curves are not measured.
Counts prefixed with ≥ are lower bounds because the comparison limit was reached.</p><ul>{legend}</ul></details></header>
<main id="chart" class="chart"><img alt="Directed Liquid Network transaction graph · {_escape(layout_title(graph))}" src="data:image/svg+xml;base64,{encoded}"></main>
</body></html>\n'''


def export_layout(graph, directory):
    """Write a new preview, publishing graph.html only after every file succeeds."""
    nodes, edges = _geometry(graph)
    svg = _svg(graph, nodes, edges)
    layout = graph.get("layout", {})
    if not isinstance(layout, dict) or not isinstance(layout.get("metrics", {}), dict):
        raise TraceError("ELK preview contains invalid layout metadata")
    metrics = layout.get("metrics", {})
    document = _preview_html(graph, svg, metrics)
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
             "graph": directory / "graph.json", "report": directory / "layout-report.json"}
    temporary = paths["html"].with_name("graph.html.tmp")
    try:
        save_json(paths["graph"], graph)
        save_json(paths["report"], {"run_id": graph.get("run_id"), "layout": layout,
                                  "metrics": metrics, "notice": layout_notice(graph),
                                  "node_count": len(nodes), "edge_count": len(edges)})
        paths["svg"].write_bytes(svg)
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(paths["html"])
    except (OSError, TypeError, ValueError) as error:
        paths["html"].unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise TraceError("Cannot finish the ELK preview; any graph and report files were retained for diagnosis") from error
    return {**{key: str(path) for key, path in paths.items()}, "layout_metrics": metrics}
