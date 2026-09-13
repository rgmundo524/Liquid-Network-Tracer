"""Non-evidence Miro items: corner badges and an on-board attribution register.

These objects never enter ELK, UTXO traversal or activity connectivity. Logical
identities and dedicated proofs let sync retire only its own obsolete items.
"""
import html
import math
import textwrap

from .common import TraceError, digest
from .attribution_presentation import attribution_lines

PREFIX = "annotation:"
BADGE_SIZE = 24.0


def proof(kind, host, page=0):
    if kind not in ("convergence", "attribution") or not isinstance(host, str) or not host or type(page) is not int or page < 0:
        raise TraceError("Invalid presentation annotation identity")
    key = PREFIX + kind + ":" + digest((host + ":" + str(page)).encode())
    return {"schema_version": 1, "key": key, "kind": kind, "host": host, "page": page}


def corner(body, position=None):
    width, height = (float(body["geometry"][k]) for k in ("width", "height"))
    if min(width, height) < 48:
        raise TraceError("Transaction is too small for its convergence badge; restore its size before syncing")
    x, y = position if position is not None else (float(body["position"][k]) for k in ("x", "y"))
    angle = math.radians(float(body.get("rotation", 0)))
    dx, dy = width / 2 - 18, -height / 2 + 18
    return x + dx * math.cos(angle) - dy * math.sin(angle), y + dx * math.sin(angle) + dy * math.cos(angle)


def make_items(graph, existing_bounds=()):
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


def validate_items(plan):
    catalog = plan.get("presentation_items", {})
    shapes = {item["key"]: item for item in plan["shapes"]}
    if not isinstance(catalog, dict):
        raise TraceError("Invalid presentation annotation catalog")
    annotation_keys = {key for key in shapes if key.startswith(PREFIX)}
    if annotation_keys != set(catalog):
        raise TraceError("Presentation annotation catalog disagrees with its shapes")
    for key, item in catalog.items():
        try:
            if (type(item.get("schema_version")) is not int or item != proof(item["kind"], item["host"], item["page"]) or key != item["key"]
                    or item["host"] not in shapes or item["host"].startswith(PREFIX)
                    or shapes[key]["body"]["data"]["shape"] != "rectangle"):
                raise ValueError
            if item["kind"] == "convergence":
                if not item["host"].startswith("tx:") or item["page"] != 0 or shapes[key]["body"]["data"]["content"] != "<p>★</p>":
                    raise ValueError
                body = shapes[key]["body"]
                if body["geometry"] != {"width": BADGE_SIZE, "height": BADGE_SIZE}:
                    raise ValueError
                expected = corner(shapes[item["host"]]["body"])
                if any(not math.isclose(float(body["position"][axis]), value, abs_tol=1e-7)
                       for axis, value in zip(("x", "y"), expected)):
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TraceError("Invalid presentation annotation proof") from None
    if any(e[side] in catalog for e in plan["connectors"] for side in ("source", "target")):
        raise TraceError("Presentation annotations cannot be transaction connector endpoints")
    return catalog


def removals(plan, state):
    if "presentation_items" not in plan:
        return {}  # Historical explicit plans never authorize new removals.
    desired = validate_items(plan)
    result = {}
    for key, record in state["items"].items():
        if not key.startswith(PREFIX):
            continue
        item = record.get("presentation_proof")
        try:
            if record["endpoint"] != "shapes" or item != proof(item["kind"], item["host"], item["page"]) or key != item["key"]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TraceError("Mapped annotation has no valid creation proof; preserve the mapping") from None
        if key not in desired:
            result[key] = item
    return result


def place_badges(plan, state, remote, removed, reorganize, placement):
    catalog = validate_items(plan)
    old_items = {key for key, item in state["items"].items() if item.get("presentation_proof")}
    if not catalog and not old_items:
        return placement(plan, state, remote, removed, reorganize)
    ordinary = {**plan, "shapes": [item for item in plan["shapes"] if item["key"] not in catalog]}
    plain_state = {**state, "items": {key: item for key, item in state["items"].items() if key not in old_items}}
    positions, shift = placement(ordinary, plain_state, remote, removed, reorganize)
    shapes = {item["key"]: item["body"] for item in plan["shapes"]}
    # The register is a generated annotation column, not part of the ELK graph.
    # Refit it around actual managed graph positions, including moved hosts.
    boxes = []
    for key in (set(shapes) | set(remote)) - catalog.keys() - old_items - set(removed):
        if key in remote and state["items"].get(key, {}).get("endpoint") != "shapes":
            continue
        body = remote.get(key, shapes.get(key))
        if body is None:
            continue
        from .miro import _bounds
        x, y, w, h = _bounds(body, key)
        x, y = positions.get(key, (x, y))
        boxes.append((x + w / 2, y - h / 2))
    right = max((box[0] for box in boxes), default=200)
    top = min((box[1] for box in boxes), default=0)
    for key in sorted(catalog, key=lambda key: (catalog[key]["kind"], catalog[key]["host"], catalog[key]["page"])):
        item = catalog[key]
        host = item["host"]
        if item["kind"] == "convergence":
            body = remote.get(host, shapes[host])
            if key in remote and any(float(remote[key]["geometry"][axis]) != BADGE_SIZE for axis in ("width", "height")):
                raise TraceError("A convergence badge was resized; restore its 24 by 24 size before syncing")
            positions[key] = corner(body, positions.get(host))
        else:
            geometry = note_geometry(shapes[key], remote.get(key))
            from .miro import _bounds
            rendered = _bounds({**remote.get(key, shapes[key]), "geometry": geometry}, key)
            positions[key] = (right + 120 + rendered[2] / 2, top + rendered[3] / 2)
            top += rendered[3] + 80
    return positions, shift


def note_geometry(planned, actual=None):
    before = (actual or planned)["geometry"]
    return {axis: max(float(before[axis]), float(planned["geometry"][axis])) for axis in ("width", "height")}


def svg_badge(node):
    if not node.get("convergence"):
        return ""
    width, height = node["width"], node["height"]
    cx, cy = node["x"] + width / 2 - 18, node["y"] - height / 2 + 18
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = 11 if index % 2 == 0 else 5
        points.append(f"{cx + radius * math.cos(angle):.3f},{cy + radius * math.sin(angle):.3f}")
    numbers = ", ".join(map(str, node["convergence"]["starting_transaction_indices"]))
    return ('<g class="convergence-badge"><title>' + html.escape("Starting branches " + numbers + " meet here; UTXO connectivity only")
            + '</title><polygon points="' + " ".join(points) + '" fill="#fbbf24" stroke="#92400e" stroke-width="1"/></g>')
