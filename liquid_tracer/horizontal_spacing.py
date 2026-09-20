"""Remove redundant horizontal gaps without changing ELK's organization.

Center captions occupy extra ELK layers. The configured layer clearance is
then charged on each side of a caption. Protected horizontal intervals let us
remove some of that duplicate padding while keeping nodes, captions and route
channels rigid. Every y coordinate, dimension and port offset stays unchanged.
"""

from bisect import bisect_right
import math

from .common import TraceError
from .layout import HORIZONTAL_NODE_GAP


HORIZONTAL_SPACING_VERSION = 2
_NODE_GUARD = HORIZONTAL_NODE_GAP / 2  # Two nodes retain the configured clearance.
_LABEL_GUARD = 16.0
_ROUTE_GUARD = 11.0  # Two vertical route channels retain 22 units.
_EPS = 1e-6


def _number(value):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value))
    except OverflowError:
        valid = False
    if not valid:
        raise TraceError("ELK returned invalid geometry for horizontal spacing")
    return value


def compact_candidate(candidate):
    """Compact an ephemeral worker candidate in place and return it.

Only completely vacant x bands are removed. A node or caption's entire width
is inside one protected interval, so it translates rigidly. All bend points
are protected too: horizontal sections shorten and vertical sections retain
their shape and relative order. Existing gaps tighter than the guards remain
unchanged because their intervals overlap. No investigation data is inspected.

The sweep is O(N log N), including route points and labels, and does not compare
every pair of objects. Validate all geometry before making any changes.
"""
    if (not isinstance(candidate, dict) or not isinstance(candidate.get("nodes"), list)
            or not isinstance(candidate.get("edges"), list)):
        raise TraceError("ELK returned invalid geometry for horizontal spacing")
    intervals, positions, bounds = [], [], []

    def point(item):
        if not isinstance(item, dict):
            raise TraceError("ELK returned invalid geometry for horizontal spacing")
        x = _number(item.get("x"))
        _number(item.get("y"))
        positions.append(item)
        return x

    def rectangle(item, guard):
        x = point(item)
        width, height = _number(item.get("width")), _number(item.get("height"))
        if width <= 0 or height <= 0:
            raise TraceError("ELK returned invalid geometry for horizontal spacing")
        end = _number(x + width)
        intervals.append((_number(x - guard), _number(end + guard)))
        bounds.extend((x, end))

    for node in candidate["nodes"]:
        rectangle(node, _NODE_GUARD)
    for edge in candidate["edges"]:
        if not isinstance(edge, dict):
            raise TraceError("ELK returned invalid geometry for horizontal spacing")
        labels, sections = edge.get("labels", []), edge.get("sections")
        if not isinstance(labels, list) or not isinstance(sections, list):
            raise TraceError("ELK returned invalid geometry for horizontal spacing")
        for label in labels:
            rectangle(label, _LABEL_GUARD)
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("bendPoints", []), list):
                raise TraceError("ELK returned invalid geometry for horizontal spacing")
            for item in [section.get("startPoint"), *section.get("bendPoints", []), section.get("endPoint")]:
                x = point(item)
                intervals.append((_number(x - _ROUTE_GUARD), _number(x + _ROUTE_GUARD)))
                bounds.append(x)

    ends, removed_prefix = [], [0.0]
    covered = None
    for start, end in sorted(intervals):
        if covered is not None and start > covered + _EPS:
            ends.append(start)
            removed_prefix.append(removed_prefix[-1] + start - covered)
        covered = end if covered is None else max(covered, end)

    def translated(x):
        return x - removed_prefix[bisect_right(ends, x)]

    # All coordinates referenced above lie in protected intervals. Relative
    # ports and other worker metadata are intentionally not transformed.
    for item in positions:
        item["x"] = translated(item["x"])
    before = max(bounds) - min(bounds) if bounds else 0.0
    after = translated(max(bounds)) - translated(min(bounds)) if bounds else 0.0
    candidate["horizontal_spacing"] = {
        "version": HORIZONTAL_SPACING_VERSION,
        "removed_width": round(before - after, 4),
        "removed_bands": len(ends),
    }
    return candidate
