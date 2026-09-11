"""Read-only Miro preflight with complete connector pages and safe fallbacks.

Miro's /items collection returns GenericItem summaries without style, so shape
reads deliberately use /shapes/{id}. The connector collection returns the same
ConnectorWithLinks model as the individual endpoint. A partial collection body
must never be mistaken for a user deleting a managed field.
"""

import json
import urllib.parse

from .common import TraceError


PAGE_SIZE = 50


def _response(raw, operation):
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (TypeError, ValueError):
        raise TraceError("Miro returned invalid JSON for " + operation + "; no board writes made") from None


def _complete_connector(body, record, pending_update):
    """Only use list bodies that contain every field needed for safe merging."""
    if body.get("type") not in (None, "connector") or not isinstance(body.get("shape"), str):
        return False
    if body.get("isSupported") is False or not isinstance(body.get("style"), dict):
        return False
    for field in ("startItem", "endItem"):
        connection = body.get(field)
        if not isinstance(connection, dict) or not isinstance(connection.get("id"), str):
            return False
    captions = body.get("captions")
    if not isinstance(captions, list):
        return False
    if any(not isinstance(caption, dict) or not isinstance(caption.get("content"), str)
           or "position" not in caption for caption in captions):
        return False
    expected_style = set()
    for original in (record.get("managed", {}), record.get("intent", {}), pending_update.get("patch", {})):
        expected_style.update(original.get("style", {}))
    return expected_style <= body["style"].keys()


def preflight(requests, base, headers, state, removals, progress=None):
    """Return mapped, live bodies before the caller performs any board writes.

    Paginate when more than one page of connectors is mapped. Missing or partial
    list results are checked individually, including permission/deletion cases.
    Page links are never followed: only an encoded cursor is sent to the same
    authenticated Miro endpoint. Shapes and the first connector page overlap.
    All acceptance and progress callbacks run on the request coordinator.
    """
    items = state["items"]
    total = len(items)
    checked = 0
    remote, missing = {}, []

    def emit():
        if progress is not None:
            progress.emit("preflight", checked, total, "Checking existing board items")

    emit()
    connectors = {record["id"]: (key, record) for key, record in items.items()
                  if record["endpoint"] == "connectors"}
    paginate = len(connectors) > PAGE_SIZE
    unseen = set(connectors) if paginate else set()
    seen_bodies = {}
    seen_cursors = set()
    next_cursor = None

    def request_page(cursor):
        query = {"limit": PAGE_SIZE}
        if cursor is not None:
            query["cursor"] = cursor
        url = base + "/connectors?" + urllib.parse.urlencode(query)
        return requests.request("GET", url, headers)

    def accept_page(result):
        nonlocal checked, next_cursor
        status, _, raw = result
        if not 200 <= status < 300:
            raise TraceError("Miro preflight connector list returned HTTP " + str(status) + "; no board writes made")
        body = _response(raw, "preflight connector list")
        data = body.get("data")
        if not isinstance(data, list):
            raise TraceError("Miro preflight connector list is malformed; no board writes made")
        cursor = body.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise TraceError("Miro preflight connector cursor is malformed; no board writes made")
        cursor = cursor or None
        if cursor is not None:
            if cursor in seen_cursors:
                raise TraceError("Miro preflight connector pagination repeated a cursor; no board writes made")
            seen_cursors.add(cursor)
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise TraceError("Miro preflight connector list contains an invalid item; no board writes made")
            item_id = item["id"]
            if item_id in seen_bodies:
                if item != seen_bodies[item_id]:
                    raise TraceError("Miro preflight connector changed between pages; no board writes made. Retry sync.")
                continue
            # Only mapped items affect the preflight. Retain their bodies to
            # detect inconsistent repeated IDs without retaining unrelated work.
            if item_id not in connectors:
                continue
            if item.get("isSupported") is False:
                raise TraceError("Miro preflight found an unsupported mapped connector; no board writes made")
            seen_bodies[item_id] = item
            unseen.discard(item_id)
            key, record = connectors[item_id]
            if _complete_connector(item, record, state.get("pending_updates", {}).get(key, {})):
                remote[key] = item
                checked += 1
        # An empty page cannot advance a useful snapshot. Verify outstanding
        # IDs directly instead of trusting an unbounded sequence of empty pages.
        next_cursor = cursor if unseen and data else None
        emit()

    def read_item(job):
        key, record = job
        if key is None:
            return request_page(None)
        url = base + "/" + record["endpoint"] + "/" + urllib.parse.quote(record["id"], safe="")
        return requests.request("GET", url, headers)

    def accept_read(job, result):
        nonlocal checked
        key, record = job
        if key is None:
            accept_page(result)
            return
        status, _, raw = result
        checked += 1
        if status == 404:
            if not (key in removals and state.get("pending_deletions", {}).get(key, {}).get("attempted")):
                missing.append(key + " (" + record["id"] + ")")
        elif not 200 <= status < 300:
            raise TraceError("Miro preflight GET returned HTTP " + str(status) + "; no board writes made")
        else:
            item = _response(raw, "preflight GET")
            if item.get("id") != record["id"]:
                raise TraceError("Miro preflight returned the wrong item ID; no board writes made")
            if record["endpoint"] == "connectors" and item.get("isSupported") is False:
                raise TraceError("Miro preflight found an unsupported mapped connector; no board writes made")
            remote[key] = item
        emit()

    def initial_jobs():
        if paginate:
            yield (None, None)
        yield from ((key, record) for key, record in items.items()
                    if not paginate or record["endpoint"] != "connectors")

    requests.map(initial_jobs(), read_item, accept_read)
    while next_cursor is not None:
        accept_page(request_page(next_cursor))
    if paginate:
        requests.map(((key, record) for key, record in connectors.values() if key not in remote),
                     read_item, accept_read)
    if missing:
        raise TraceError("Miro preflight found missing or inaccessible mapped items: " + ", ".join(missing) +
                         ". No board writes made. Restore the items/access or repair the mapping; they will not be recreated automatically.")
    return remote
