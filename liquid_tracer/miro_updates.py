"""Reconcile uncertain existing-item edits before any bounded replay.

This helper never creates or deletes items. Its caller journals the intended
edit first, and checkpoints only the verified response returned here.
"""

import copy
import json
import math

from .common import TraceError, canonical
from .miro_errors import update_error
from .miro_requests import MiroRequestNotSent


_TRANSIENT = {409, 500, 502, 503, 504}
_TYPES = {"shapes": "shape", "connectors": "connector", "frames": "frame"}
_NUMERIC = {"x", "y", "width", "height", "rotation", "fontSize", "borderWidth",
            "borderOpacity", "fillOpacity", "strokeWidth", "strokeOpacity"}
_MISSING = object()


def _matches(actual, expected, path=()):
    """Compare every submitted leaf, ignoring extra response-only fields."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict)
                and all(key in actual and _matches(actual[key], value, path + (key,))
                        for key, value in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_matches(a, b, path + (index,))
                        for index, (a, b) in enumerate(zip(actual, expected))))
    if path and path[-1] in _NUMERIC:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        try:
            a, b = float(actual), float(expected)
            return math.isfinite(a) and math.isfinite(b) and a == b
        except (TypeError, ValueError, OverflowError):
            return False
    if path and isinstance(path[-1], str) and path[-1].endswith("Color"):
        return (isinstance(actual, str) and isinstance(expected, str)
                and actual.lower() == expected.lower())
    return type(actual) is type(expected) and actual == expected


def _unchanged(actual, baseline, submitted, path=()):
    if isinstance(submitted, dict):
        return (isinstance(actual, dict) and isinstance(baseline, dict)
                and all(key in actual and key in baseline
                        and _unchanged(actual[key], baseline[key], value, path + (key,))
                        for key, value in submitted.items()))
    return _matches(actual, baseline, path)


def _response(raw, endpoint, item_id):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if (not isinstance(value, dict) or value.get("id") != item_id
            or value.get("type") != _TYPES.get(endpoint)):
        return None
    return value


def _parent(item):
    parent = item.get("parent")
    if parent is None:
        return None
    if not isinstance(parent, dict):
        return _MISSING
    identity = parent.get("id")
    return identity if identity is None or isinstance(identity, str) and identity else _MISSING


def _observable(patch, baseline, current, endpoint):
    # REST cannot prove whether a returned attachment percentage was fixed or
    # calculated automatically. Frame/parent edits can move other board items.
    if any(key in patch for key in ("startItem", "endItem", "parent")):
        return False
    if baseline.get("id") != current.get("id") or baseline.get("type") != _TYPES.get(endpoint):
        return False
    if endpoint == "frames" and any(key in patch for key in ("position", "geometry")):
        return False
    allowed = {"data", "style", "position", "geometry"} if endpoint == "shapes" else (
        {"captions", "style", "shape"} if endpoint == "connectors" else {"data", "style"})
    if set(patch) - allowed:
        return False
    before_parent, after_parent = _parent(baseline), _parent(current)
    if before_parent is _MISSING or after_parent is _MISSING or before_parent != after_parent:
        return False
    if endpoint == "shapes" and any(key in patch for key in ("position", "geometry")):
        if before_parent or after_parent:
            return False
        for item in (baseline, current, patch):
            position = item.get("position", {})
            if (not isinstance(position, dict)
                    or position.get("relativeTo") not in (None, "canvas_center")
                    or position.get("origin") not in (None, "center")):
                return False
    if endpoint == "connectors":
        for name in ("startItem", "endItem"):
            before, after = baseline.get(name), current.get(name)
            if (not isinstance(before, dict) or not isinstance(after, dict)
                    or not isinstance(before.get("id"), str) or not before["id"]
                    or before["id"] != after.get("id")):
                return False
    return True


def patch_item(requests, url, headers, patch, baseline, endpoint, item_id, *, retry_action="sync"):
    """Return a matching item acknowledgment, or stop with its journal intact.

Three transient attempts are allowed, in addition to the request coordinator's
existing bounded retries for known rate-limit rejections. Every replay requires
a full resource read proving the submitted fields still match the preflight.
"""
    # Own immutable copies across transport callbacks and concurrent callers.
    submitted = json.loads(canonical(patch))
    before = copy.deepcopy(baseline)
    action = retry_action if retry_action == "Create / update Miro frames" else "sync"
    suffix = "; acknowledged progress is saved; rerun " + action + " to reconcile the saved update journal"
    for attempt in range(3):
        try:
            status, response_headers, raw = requests.request("PATCH", url, headers, copy.deepcopy(submitted))
        except MiroRequestNotSent:
            raise
        except TraceError:
            failure = "Miro PATCH response was lost or could not be read"
        else:
            if 200 <= status < 300:
                response = _response(raw, endpoint, item_id)
                if response is None:
                    raise TraceError("Miro PATCH returned an invalid response or the wrong item ID/type" + suffix)
                return response
            failure = update_error(status, endpoint, response_headers, raw, patch=submitted,
                                   request_headers=headers, item_id=item_id, retry_action=action)
            if status not in _TRANSIENT:
                raise TraceError(failure)

        requests._notify(.01, "Checking a temporary Miro update failure before retrying", "update_retry")
        try:
            status, _, raw = requests.request("GET", url, headers)
        except MiroRequestNotSent:
            raise
        except TraceError:
            raise TraceError(failure + "; the current item could not be verified" + suffix) from None
        current = _response(raw, endpoint, item_id) if 200 <= status < 300 else None
        if current is None:
            raise TraceError(failure + "; the current item could not be verified" + suffix)
        if not _observable(submitted, before, current, endpoint):
            raise TraceError(failure + "; attachment, parent, or frame state cannot be safely verified for automatic recovery" + suffix)
        if _matches(current, submitted):
            return current
        if not _unchanged(current, before, submitted):
            raise TraceError(failure + "; item fields changed or were only partly applied; automatic retry stopped to preserve manual edits" + suffix)
        if attempt == 2:
            raise TraceError(failure + "; temporary update failure persisted after 3 attempts" + suffix)
        delay = 2 ** attempt
        requests._notify(delay, "Checking a temporary Miro update failure before retrying", "update_retry")
        requests._wait(delay)
