"""Synthetic historical annotation plans for upgrade/recovery regression tests.

Frozen generator from presentation version 13; never used by application code.
"""
import copy
import html
import textwrap
from unittest.mock import patch
from liquid_tracer.attribution_presentation import attribution_lines
from liquid_tracer.presentation_items import proof, corner, BADGE_SIZE
from liquid_tracer.miro import make_plan
from liquid_tracer.common import canonical, digest

def legacy_items(graph, existing_bounds=()):
    items, catalog = [], {}
    right = max((n["x"] + n["width"] / 2 for n in graph["nodes"]), default=200)
    top = min((n["y"] - n["height"] / 2 for n in graph["nodes"]), default=0)
    for x, by, width, height in existing_bounds:
        right, top = max(right, x + width / 2), min(top, by - height / 2)
    y = top
    for node in sorted(graph["nodes"], key=lambda n: n["id"]):
        if node.get("convergence"):
            marker = proof("convergence", node["id"])
            body = {"position": {"x": node["x"], "y": node["y"]},
                    "geometry": {"width": node["width"], "height": node["height"]}}
            x, by = corner(body)
            items.append({"key": marker["key"], "body": {
                "data": {"shape": "rectangle", "content": "<p>★</p>"},
                "position": {"x": x, "y": by, "origin": "center"},
                "geometry": {"width": BADGE_SIZE, "height": BADGE_SIZE},
                "style": {"fillOpacity": "0", "borderOpacity": "0", "fontSize": "22", "color": "#b45309",
                          "textAlign": "center", "textAlignVertical": "middle"}}})
            catalog[marker["key"]] = marker
        lines = attribution_lines(node)
        if not lines:
            continue
        # Wrap explicitly so even long URLs/notes cannot overflow a fixed-width
        # register card. Pagination bounds each REST shape's content size.
        wrapped = [part for value in lines for line in (value.splitlines() or [""])
                   for part in (textwrap.wrap(line, 82, replace_whitespace=False) or [""])]
        for page, start in enumerate(range(0, len(wrapped), 48)):
            marker = proof("attribution", node["id"], page)
            chunk = wrapped[start:start + 48]
            title = lines[0] + (" · continued " + str(page + 1) if page else " · Address attribution")
            height = 70 + 18 * len(chunk)
            items.append({"key": marker["key"], "body": {
                "data": {"shape": "rectangle", "content": '<p><strong>' + html.escape(title) + '</strong></p><p>'
                         + '<br>'.join(html.escape(line) for line in chunk) + '</p>'},
                "position": {"x": right + 490, "y": y + height / 2, "origin": "center"},
                "geometry": {"width": 740, "height": height},
                "style": {"fillColor": "#ffffff", "fontSize": "12", "textAlign": "left", "textAlignVertical": "top"}}})
            catalog[marker["key"]] = marker
            y += height + 80
    return items, catalog



def legacy_plan(graph):
    graph = copy.deepcopy(graph)
    graph["presentation_version"] = 13
    with (patch("liquid_tracer.presentation_items.make_items", side_effect=legacy_items),
          patch("liquid_tracer.miro.node_border", return_value=("#334155", 2))):
        plan = make_plan(graph)
    nodes = {node["id"]: node for node in graph["nodes"]}
    for shape in plan["shapes"]:
        if shape["key"] not in nodes:
            continue
        node = nodes[shape["key"]]
        content = "<p>" + "<br>".join(html.escape(line) for line in node["label"].splitlines()) + "</p>"
        if node.get("url"):
            content += '<p><a href="' + html.escape(node["url"], quote=True) + '">Explorer</a></p>'
        shape["body"]["data"]["content"] = content
    plan["sha256"] = digest(canonical({k: v for k, v in plan.items() if k != "sha256"}))
    return plan
