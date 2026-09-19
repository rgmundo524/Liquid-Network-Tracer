"""Offline, printable activity atlases of existing display geometry.

The overview and detail pages share the exact drawing. Views never relayout,
aggregate evidence or discard a connection crossing a page boundary. Sparse
tiling omits genuinely empty space, including between unrelated activities.
"""

import math
import re
from collections import defaultdict

from .edge_labels import caption_box, caption_text
from .graph_markers import node_border
from .miro_frames import activity_frames


PAGE_WIDTH = 1152.0
PAGE_HEIGHT = 704.0
OVERLAP = 200.0
SCALE = 1.25
PADDING = 24.0
# Printing more pages is not useful and can exhaust an offline browser. This
# guard affects only the optional atlas, never graph/layout/export completeness.
MAX_DETAIL_PAGES = 4096


class _AtlasTooLarge(Exception):
    pass


def _intersects(first, second):
    return first[0] <= second[2] and second[0] <= first[2] and first[1] <= second[3] and second[1] <= first[3]


def _inside(inner, outer):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _node_box(node):
    pad = node_border(node)[1] / 2
    return (node["x"] - node["width"] / 2 - pad, node["y"] - node["height"] / 2 - pad,
            node["x"] + node["width"] / 2 + pad, node["y"] + node["height"] / 2 + pad)


def _segment_hits(start, end, box):
    """Slab intersection works for elbows, diagonals and zero-length lines."""
    lower, upper = 0.0, 1.0
    for axis in (0, 1):
        delta = end[axis] - start[axis]
        if not delta:
            if not box[axis] <= start[axis] <= box[axis + 2]:
                return False
            continue
        first = (box[axis] - start[axis]) / delta
        last = (box[axis + 2] - start[axis]) / delta
        lower, upper = max(lower, min(first, last)), min(upper, max(first, last))
        if lower > upper:
            return False
    return True


def _axis_range(first, last, origin, extent, step):
    low = max(0, math.ceil((first - origin - extent) / step))
    high = max(0, math.floor((last - origin) / step))
    if high - low > MAX_DETAIL_PAGES:
        raise _AtlasTooLarge
    return range(low, high + 1)


def _box_cells(box, origin):
    columns = _axis_range(box[0], box[2], origin[0], PAGE_WIDTH, PAGE_WIDTH - OVERLAP)
    rows = _axis_range(box[1], box[3], origin[1], PAGE_HEIGHT, PAGE_HEIGHT - OVERLAP)
    if len(columns) * len(rows) > MAX_DETAIL_PAGES:
        raise _AtlasTooLarge
    return ((column, row) for row in rows for column in columns)


def _viewport(cell, origin):
    x = origin[0] + cell[0] * (PAGE_WIDTH - OVERLAP)
    y = origin[1] + cell[1] * (PAGE_HEIGHT - OVERLAP)
    return x, y, x + PAGE_WIDTH, y + PAGE_HEIGHT


def _segment_cells(start, end, origin):
    """Enumerate the occupied strips, not a diagonal's entire bounding box."""
    pad = 14.0  # Arrowheads and the rounded-elbow control hull.
    for column in _axis_range(min(start[0], end[0]) - pad, max(start[0], end[0]) + pad,
                              origin[0], PAGE_WIDTH, PAGE_WIDTH - OVERLAP):
        x = origin[0] + column * (PAGE_WIDTH - OVERLAP)
        low_y, high_y = sorted((start[1], end[1]))
        delta = end[0] - start[0]
        if delta:
            fractions = sorted(((x - pad - start[0]) / delta, (x + PAGE_WIDTH + pad - start[0]) / delta))
            first, last = max(0, fractions[0]), min(1, fractions[1])
            if first > last:
                continue
            low_y, high_y = sorted((start[1] + first * (end[1] - start[1]),
                                   start[1] + last * (end[1] - start[1])))
        for row in _axis_range(low_y - pad, high_y + pad, origin[1], PAGE_HEIGHT, PAGE_HEIGHT - OVERLAP):
            box = _viewport((column, row), origin)
            if _segment_hits(start, end, (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)):
                yield column, row


def _activity_pages(nodes, edges, bounds, sequence):
    origin = bounds[0] - PADDING, bounds[1] - PADDING
    cells = defaultdict(lambda: {"node_ids": set(), "edge_ids": set(), "caption_ids": set()})

    def add(cell, field, identity):
        cells[cell][field].add(identity)
        if len(cells) > MAX_DETAIL_PAGES:
            raise _AtlasTooLarge

    for node in nodes:
        for cell in _box_cells(_node_box(node), origin):
            add(cell, "node_ids", node["id"])
    for edge in edges:
        for start, end in zip(edge["points"], edge["points"][1:]):
            for cell in _segment_cells(start, end, origin):
                add(cell, "edge_ids", edge["id"])
        if caption_text(edge):
            left, top, right, bottom = caption_box(edge, edge["points"])
            for cell in _box_cells((left - 3, top - 3, right + 3, bottom + 3), origin):
                add(cell, "caption_ids", edge["id"])
                add(cell, "edge_ids", edge["id"])
    lookup = {node["id"]: node for node in nodes}
    edge_lookup = {edge["id"]: edge for edge in edges}
    ordered = sorted(cells, key=lambda cell: (cell[1], cell[0]))
    labels = {cell: f"A{sequence}-{index}" for index, cell in enumerate(ordered, 1)}
    pages = []
    for cell in ordered:
        box = _viewport(cell, origin)
        entry = {key: sorted(value) for key, value in cells[cell].items()}
        boundary_edges = []
        for identity in entry["edge_ids"]:
            edge = edge_lookup[identity]
            if (not _inside(_node_box(lookup[edge["source"]]), box)
                    or not _inside(_node_box(lookup[edge["target"]]), box)
                    or any(not _inside((x, y, x, y), box) for x, y in edge["points"])):
                boundary_edges.append(identity)
        entry.update(id=labels[cell], column=cell[0] + 1, row=cell[1] + 1, bounds=list(box),
                     continued_node_ids=[key for key in entry["node_ids"] if not _inside(_node_box(lookup[key]), box)],
                     boundary_edge_ids=boundary_edges,
                     neighbors={direction: labels[neighbor] for direction, neighbor in (
                         ("left", (cell[0] - 1, cell[1])), ("right", (cell[0] + 1, cell[1])),
                         ("above", (cell[0], cell[1] - 1)), ("below", (cell[0], cell[1] + 1))) if neighbor in labels})
        pages.append(entry)
    return pages


def _inline(svg, prefix):
    # One definition per drawing. Detail pages use <use>, avoiding graph-sized
    # copies for every sheet and preserving original separate edge paths.
    value = svg.decode("utf-8").split(">", 1)[1].rsplit("</svg>", 1)[0]
    value = re.sub(r'(?<![\w-])id="([^"]+)"', lambda match: 'id="' + prefix + match[1] + '"', value)
    return re.sub(r'url\(#([^)]+)\)', lambda match: 'url(#' + prefix + match[1] + ')', value)


def _svg_view(identity, bounds, *, markup="", overview=False):
    from .layout_preview import _fmt
    left, top, right, bottom = bounds
    box = " ".join(map(_fmt, (left, top, right - left, bottom - top)))
    classes = "overview-map" if overview else "detail-map"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'class="{classes}" viewBox="{box}" role="img" aria-label="Graph view">'
            f'<use href="#{identity}" xlink:href="#{identity}"/>{markup}</svg>')


def render_details(graph, nodes, edges, overview_svg):
    """Return a complete offline HTML atlas and a machine-readable page index."""
    from .layout_preview import _escape, _fmt, _svg, drawing_bounds, layout_notice
    descriptors = activity_frames(graph)["activities"]
    node_lookup = {node["id"]: node for node in nodes}
    edge_lookup = {edge["id"]: edge for edge in edges}
    groups = []
    reason = None
    try:
        for sequence, descriptor in enumerate(descriptors, 1):
            members = [node_lookup[key] for key in descriptor["shape_keys"]]
            connections = [edge_lookup[key] for key in descriptor["connector_keys"]]
            bounds = drawing_bounds(members, connections)
            pages = _activity_pages(members, connections, bounds, sequence)
            groups.append({"id": f"activity-{sequence}", "title": descriptor["title"],
                           "node_ids": descriptor["shape_keys"], "edge_ids": descriptor["connector_keys"],
                           "bounds": list(bounds), "pages": pages})
            if sum(len(group["pages"]) for group in groups) > MAX_DETAIL_PAGES:
                raise _AtlasTooLarge
    except (OverflowError, _AtlasTooLarge):
        groups = []
        reason = ("This drawing would require more than 4,096 fixed-scale detail pages. "
                  "The complete overview and SVG remain available. Create a smaller branch or connection view for printable details.")
    edge_pages = defaultdict(list)
    for group in groups:
        for page in group["pages"]:
            for identity in page["edge_ids"]:
                edge_pages[identity].append(page["id"])
    index = {"schema_version": 1, "run_id": graph.get("run_id"), "paper": "A3 landscape",
             "scale": SCALE, "page_width": PAGE_WIDTH, "page_height": PAGE_HEIGHT,
             "overlap": OVERLAP, "page_count": sum(len(group["pages"]) for group in groups),
             "activities": groups, "edge_pages": dict(sorted(edge_pages.items())), "unavailable_reason": reason}
    definitions = '<g id="complete-drawing">' + _inline(overview_svg, "overview-") + '</g>'
    contents = []
    links = " · ".join(f'<a href="#{group["id"]}">{_escape(group["title"])}</a>' for group in groups)
    if reason:
        links = '<p role="status">' + _escape(reason) + '</p>'
    full_bounds = drawing_bounds(nodes, edges)
    full_bounds = (full_bounds[0] - PADDING, full_bounds[1] - PADDING,
                   full_bounds[2] + PADDING, full_bounds[3] + PADDING)
    contents.append('<section class="sheet overview" id="overview"><h1>Complete graph overview</h1>'
                    '<p>' + _escape(graph.get("run_id", "")) + f' · {len(nodes)} nodes · {len(edges)} connections</p>'
                    + _svg_view("complete-drawing", full_bounds, overview=True)
                    + '<p>Overview is scaled to fit. Activity detail pages keep one readable scale; '
                    'their overlap repeats evidence for navigation.</p></section>')
    for group in groups:
        identity = group["id"]
        members = [node_lookup[key] for key in group["node_ids"]]
        connections = [edge_lookup[key] for key in group["edge_ids"]]
        definitions += f'<g id="{identity}-drawing">' + _inline(_svg(graph, members, connections, banner=False), identity + "-") + '</g>'
        bounds = group["bounds"]
        overview_bounds = (bounds[0] - PADDING, bounds[1] - PADDING, bounds[2] + PADDING, bounds[3] + PADDING)
        overlays = []
        # Labels scale with the activity overview, rather than disappearing on
        # enormous graphs. Transparent cells remain clickable screen targets.
        font = max(24, (bounds[2] - bounds[0]) / 75, (bounds[3] - bounds[1]) / 45)
        for page in group["pages"]:
            left, top, right, bottom = page["bounds"]
            overlays.append(f'<a href="#{page["id"]}"><rect x="{_fmt(left)}" y="{_fmt(top)}" '
                            f'width="{_fmt(right-left)}" height="{_fmt(bottom-top)}" fill="#dbeafe" fill-opacity="0.06" '
                            f'stroke="#1d4ed8" stroke-width="{_fmt(font/16)}" stroke-dasharray="{_fmt(font/3)}"/>'
                            f'<text x="{_fmt(left+font/3)}" y="{_fmt(top+font)}" font-size="{_fmt(font)}" '
                            f'fill="#1e40af" stroke="white" stroke-width="{_fmt(font/8)}" paint-order="stroke">'
                            f'{page["id"]}</text></a>')
        # Page rectangles, including the overlapping outer strips, must fit
        # the page map. Their drawing itself always remains inside this union.
        overview_bounds = (min(overview_bounds[0], min(p["bounds"][0] for p in group["pages"])),
                           min(overview_bounds[1], min(p["bounds"][1] for p in group["pages"])),
                           max(overview_bounds[2], max(p["bounds"][2] for p in group["pages"])),
                           max(overview_bounds[3], max(p["bounds"][3] for p in group["pages"])))
        contents.append(f'<section class="sheet overview" id="{identity}"><h2>{_escape(group["title"])}</h2>'
                        f'<p>{len(members)} nodes · {len(connections)} connections · {len(group["pages"])} detail pages. '
                        'Select a blue page number or continue below.</p>'
                        + _svg_view(identity + "-drawing", overview_bounds, markup="".join(overlays), overview=True)
                        + '<p>Activity means displayed connectivity, not common ownership. '
                        '<a href="#overview">Complete overview</a></p></section>')
        for page in group["pages"]:
            navigation = " · ".join(f'<a href="#{target}">{direction.capitalize()}: {target}</a>'
                                    for direction, target in page["neighbors"].items())
            boundary = f'{len(page["boundary_edge_ids"])} connections continue beyond this page.'
            contents.append(f'<section class="sheet detail" id="{page["id"]}"><h2>{page["id"]} · {_escape(group["title"])}</h2>'
                            f'<p>Row {page["row"]}, column {page["column"]} · '
                            f'<a href="#{identity}">Activity map</a> · {navigation}</p>'
                            + _svg_view(identity + "-drawing", page["bounds"])
                            + '<p class="continuation">' + boundary + ' Overlap: 200 layout units. '
                            'See the connection index for destination pages.</p></section>')
    rows = []
    for edge in edges:
        page_links = " ".join(f'<a href="#{page}">{page}</a>' for page in edge_pages.get(edge["id"], []))
        rows.append('<tr><td>' + _escape(edge["id"]) + '</td><td>' + _escape(caption_text(edge))
                    + '</td><td>' + _escape(edge["source"]) + '<br>→ ' + _escape(edge["target"])
                    + '</td><td>' + page_links + '</td></tr>')
    members_html = []
    for node in nodes:
        if node["kind"] != "context_group":
            continue
        records = node.get("details", {}).get("members", [])
        members_html.append('<details><summary>' + _escape(node.get("label", "Context group")) + '</summary><ul>'
                            + ''.join('<li><strong>' + _escape(member.get("id", "")) + '</strong><pre>'
                                      + _escape(member.get("label", "")) + '</pre></li>' for member in records)
                            + '</ul><p>Full member records are preserved in graph.json.</p></details>')
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Liquid trace · Overview and detail pages</title><style>
body{margin:0;background:#eef1f5;color:#172033;font:14px system-ui,sans-serif}header{padding:18px 24px;background:white}
h1{font-size:22px}h2{font-size:18px;margin:0 0 6px}p{margin:5px 0 10px;line-height:1.4}a{color:#155e75}
.sheet{width:381mm;margin:18px auto;padding:8mm;background:white;break-after:page;page-break-after:always}
.overview-map{display:block;width:381mm;height:230mm}.detail-map{display:block;width:381mm;height:232.833mm;overflow:hidden;border:1px solid #cbd5e1}
.continuation{font-size:11px}.index{padding:24px;background:white;overflow-wrap:anywhere}.index table{width:100%;border-collapse:collapse}
th,td{text-align:left;vertical-align:top;border-bottom:1px solid #d5dbe3;padding:6px}th{font-weight:600}pre{white-space:pre-wrap}
.drawing-definitions{position:absolute;width:0;height:0;overflow:hidden}.index th:nth-child(1){width:25%}.index td:last-child a{white-space:nowrap}
@page{size:A3 landscape;margin:10mm}@media print{body{background:white}header{display:none}.sheet{padding:0;margin:0;width:381mm}
.index{padding:0;font-size:10px}.index details{display:block}a{color:inherit;text-decoration:none}}
</style></head><body><header><h1>Overview and readable detail pages</h1>
<p>Print at A3 landscape and 100% scale, with browser headers and footers off. Node labels are approximately 11.25 pt;
connector captions are approximately 10.3 pt. The overview is scaled separately. Details preserve the exact drawing,
including connections to other branches. Empty tiles are omitted; tiles containing only connectors remain.</p>
<p><a href="graph.html">Return to graph</a> · <a href="graph.svg" download>Complete SVG</a> ·
<a href="details.json" download>Page index JSON</a> · <a href="#connection-index">Connection index</a></p>'''
    page += '<p>' + _escape(layout_notice(graph)) + '</p><nav>' + links + '</nav></header>'
    page += '<svg xmlns="http://www.w3.org/2000/svg" class="drawing-definitions" aria-hidden="true"><defs>' + definitions + '</defs></svg>'
    page += ''.join(contents)
    page += '<section class="index" id="connection-index"><h2>Connection index</h2><p>Each original connection remains separate. '
    page += 'Page links include its route and caption. Shared boundary connections are repeated across overlapping pages.</p>'
    page += '<table><thead><tr><th>Connection</th><th>Caption</th><th>From → To</th><th>Detail pages</th></tr></thead><tbody>'
    page += ''.join(rows) + '</tbody></table>' + ''.join(members_html) + '</section></body></html>\n'
    return page, index
