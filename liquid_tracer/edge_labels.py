"""Shared, deterministic connector-caption geometry.

Miro chooses its own font metrics and connector routes. These padded estimates
reserve space using the actual full caption and font size, without requiring a
browser or sending investigation text to a font/layout service.
"""

import hashlib
import json
import math
import unicodedata

from .common import TraceError


FONT_SIZE = 11
LINE_HEIGHT = 14
PADDING_X = 6
PADDING_Y = 5
LABEL_LAYOUT_VERSION = 1


def caption_text(edge, *, display=True):
    # Dense context summaries keep each original label/quantity in evidence
    # and hover details, without reserving hundreds of on-chart text boxes.
    if display and edge.get("caption_display") == "details_only":
        return ""
    label, quantity = str(edge.get("label") or ""), str(edge.get("quantity") or "")
    # Synthetic geometry-only edges have no caption. Miro's separator remains
    # present for real captions even when the quantity is empty.
    return label + " · " + quantity if "label" in edge or quantity else ""


def caption_size(edge):
    text = caption_text(edge).replace("\t", "    ")
    def advance(char):
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in ("W", "F"):
            return 1.2
        return 1.0 if char in "MWmw@%" else .8
    lines = text.splitlines() or [""]
    return {"width": round(max(PADDING_X * 2, max(sum(advance(char) for char in line) for line in lines)
                              * FONT_SIZE + PADDING_X * 2), 4),
            "height": len(lines) * LINE_HEIGHT + PADDING_Y * 2}


def route_signature(points):
    # Four decimals match saved ELK coordinates; tuples and lists hash equally.
    normalized = []
    for point in points:
        current = [round(float(value), 4) or 0.0 for value in point]
        if not normalized or normalized[-1] != current:
            normalized.append(current)
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def midpoint(points):
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    longest = max(lengths, default=0)
    if not longest:
        return points[0]
    weights = [length / longest for length in lengths]
    remaining = sum(weights) / 2
    for a, b, weight in zip(points, points[1:], weights):
        if weight and remaining <= weight:
            fraction = remaining / weight
            return tuple(a[axis] + (b[axis] - a[axis]) * fraction for axis in (0, 1))
        remaining -= weight
    return points[-1]


def validate_label_layout(edge):
    value = edge.get("label_layout")
    if value is None:
        return
    try:
        valid = (isinstance(value, dict)
                 and all(not isinstance(value.get(key), bool) and isinstance(value.get(key), (int, float))
                         and math.isfinite(value[key]) for key in ("x", "y", "width", "height"))
                 and value["width"] > 0 and value["height"] > 0
                 and isinstance(value.get("route_signature"), str))
    except OverflowError:
        valid = False
    if not valid:
        raise TraceError("Invalid connector label geometry; generate a new ELK preview")


def caption_box(edge, points, *, use_layout=True):
    """Return top-left/bottom-right bounds for the caption actually drawn.

An ELK label position only applies while its route remains unchanged. Changed
or straightened routes use the midpoint placement rendered by the preview.
"""
    size = caption_size(edge)
    value = edge.get("label_layout")
    if use_layout and value is not None:
        validate_label_layout(edge)
        if (value["route_signature"] == route_signature(points)
                and value["width"] == size["width"] and value["height"] == size["height"]):
            return value["x"], value["y"], value["x"] + value["width"], value["y"] + value["height"]
    x, y = midpoint(points)
    # Keep the historical baseline seven units above the connector. The box
    # includes font descent and the SVG's white halo.
    return x - size["width"] / 2, y - 7 - FONT_SIZE - PADDING_Y, x + size["width"] / 2, y - 7 - FONT_SIZE - PADDING_Y + size["height"]


def translate_label(edge, dx, dy):
    """Move a saved label together with an already translated stored route."""
    if edge.get("label_layout") is not None:
        validate_label_layout(edge)
        original = [(p["x"] - dx, p["y"] - dy) for p in edge["route"]]
        if edge["label_layout"]["route_signature"] != route_signature(original):
            edge.pop("label_layout")
            return
        edge["label_layout"]["x"] += dx
        edge["label_layout"]["y"] += dy
        edge["label_layout"]["route_signature"] = route_signature([(p["x"], p["y"]) for p in edge["route"]])
