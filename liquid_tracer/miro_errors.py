"""Small, non-content diagnostics for failed Miro requests.

Miro describes error code/message/context separately in its official client:
https://github.com/miroapp/api-clients/blob/main/packages/miro-api/model/bulkOperationError.ts
Messages and arbitrary context can echo submitted board text, so neither is
included here. Unknown codes and unrecognized correlation formats are omitted.
"""

import hashlib
import json
import re
from collections.abc import Mapping


_ENDPOINTS = {"items", "items/bulk", "shapes", "connectors", "frames"}
_ERROR_CODES = {
    "badrequest", "invalidparameters", "invalidparameter", "validationerror",
    "unauthorized", "accessdenied", "forbidden", "notfound", "conflict",
    "ratelimitexceeded", "toomanyrequests", "internalerror", "internalservererror",
    "servererror", "serviceunavailable", "requesttimeout", "gatewaytimeout",
    "badgateway", "bulkoperationfailed",
}
_CODE = re.compile(r"[A-Za-z][A-Za-z_]{0,63}\Z")
_CORRELATION = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
_ID_HEADERS = {
    "x-request-id": "request_id", "request-id": "request_id",
    "x-miro-request-id": "request_id",
    "x-correlation-id": "correlation_id", "correlation-id": "correlation_id",
    "x-miro-correlation-id": "correlation_id",
}

# These are API property names, never graph labels or user-supplied map keys.
# Unknown paths are intentionally omitted, even when their spelling looks valid.
_UPDATE_FIELDS = {
    "data", "data.content", "data.shape", "data.title", "data.format",
    "position", "position.x", "position.y", "position.origin", "position.relativeTo",
    "geometry", "geometry.width", "geometry.height", "geometry.rotation",
    "style", "style.fillColor", "style.fillOpacity", "style.borderColor",
    "style.borderWidth", "style.borderOpacity", "style.borderStyle",
    "style.fontFamily", "style.fontSize", "style.color", "style.textAlign",
    "style.textAlignVertical", "style.startStrokeCap", "style.endStrokeCap",
    "style.strokeColor", "style.strokeWidth", "style.strokeStyle",
    "style.textOrientation", "style.textColor",
    "parent", "parent.id", "shape",
    "captions", "captions[]", "captions[].content", "captions[].position",
    "captions[].style", "captions[].style.color", "captions[].style.fontSize",
    "captions[].style.textAlign", "captions[].style.fillColor",
}
for _attachment in ("startItem", "endItem"):
    _UPDATE_FIELDS.update({_attachment, _attachment + ".id", _attachment + ".snapTo",
                           _attachment + ".position", _attachment + ".position.x",
                           _attachment + ".position.y"})
_ITEM_ID = re.compile(r"[0-9]{1,30}\Z")


def _header_items(headers):
    return headers.items() if isinstance(headers, Mapping) else ()


def _secrets(request_headers):
    secrets = []
    for key, value in _header_items(request_headers):
        if isinstance(key, str) and key.lower() == "authorization" and isinstance(value, str):
            secrets.append(value)
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                secrets.append(parts[1])
    return secrets


def _response_data(raw):
    if isinstance(raw, (bytes, str)) and len(raw) <= 16384:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError, UnicodeError, RecursionError):
            pass
    return {}


def _request_error(method, status, endpoint, count, response_headers, raw, *, request_headers=None):
    """Describe the operation without printing headers, request data, or errors.

Only recognized error codes and UUID/128-bit hexadecimal request identifiers
are displayed. In particular, transaction hashes, URLs, HTML, arbitrary server
messages, credentials, and oversized/malformed response bodies are not logged.
"""
    operation = endpoint if isinstance(endpoint, str) and endpoint in _ENDPOINTS else "items"
    details = [operation]
    if count is not None:
        number = count if type(count) is int and 1 <= count <= 20 else 1
        details.append(f"{number} item" + ("s" if number != 1 else ""))
    secrets = _secrets(request_headers)

    def safe(value, pattern):
        return (isinstance(value, str) and pattern.fullmatch(value) is not None
                and not any(secret and secret in value for secret in secrets))

    data = _response_data(raw)
    code = data.get("code")
    if safe(code, _CODE) and code.replace("_", "").lower() in _ERROR_CODES:
        details.append("code=" + code)
    identifiers = {}
    for key, value in _header_items(response_headers):
        name = _ID_HEADERS.get(key.lower()) if isinstance(key, str) else None
        if name and safe(value, _CORRELATION):
            identifiers.setdefault(name, value)
    for key, name in (("requestId", "request_id"), ("correlationId", "correlation_id")):
        value = data.get(key)
        if safe(value, _CORRELATION):
            identifiers.setdefault(name, value)
    details.extend(name + "=" + value for name, value in identifiers.items())
    return "Miro " + method + " returned HTTP " + str(status) + " (" + "; ".join(details) + ")"


def creation_error(status, endpoint, count, response_headers, raw, *, request_headers=None):
    return _request_error("POST", status, endpoint, count, response_headers, raw, request_headers=request_headers)


def read_error(status, endpoint, response_headers, raw, request_headers=None):
    return _request_error("GET", status, endpoint, None, response_headers, raw, request_headers=request_headers)


def _field_path(value):
    """Normalize common validation paths, then require an exact known API path."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    if value.startswith("/"):
        value = value[1:].replace("/", ".")
    for prefix in ("$.", "body."):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = re.sub(r"\[[0-9]{1,6}\]|\.[0-9]{1,6}(?=\.|$)", "[]", value)
    return value if value in _UPDATE_FIELDS else None


def _validation_fields(data):
    """Inspect validation structures only; message/value contents are ignored."""
    found = set()
    remaining = 128

    def add(value):
        path = _field_path(value)
        if path:
            found.add(path)

    def visit(value, depth=0):
        nonlocal remaining
        if depth > 6 or remaining <= 0:
            return
        remaining -= 1
        if isinstance(value, dict):
            for key in ("field", "property", "path", "pointer"):
                add(value.get(key))
            fields = value.get("fields")
            if isinstance(fields, dict):
                for key in list(fields)[:64]:
                    add(key)
            elif isinstance(fields, list):
                for entry in fields[:64]:
                    add(entry)
                    visit(entry, depth + 1)
            else:
                add(fields)
            for key in ("context", "errors", "violations"):
                visit(value.get(key), depth + 1)
        elif isinstance(value, list):
            for entry in value[:64]:
                visit(entry, depth + 1)

    visit(data)
    return found


def _submitted_fields(patch):
    """Report known leaf names, including malformed fields, without values."""
    found = set()

    def visit(value, prefix="", depth=0):
        if depth > 4:
            return
        if isinstance(value, dict):
            for key in list(value)[:64]:
                if not isinstance(key, str):
                    continue
                path = prefix + "." + key if prefix else key
                if path not in _UPDATE_FIELDS:
                    continue
                before = len(found)
                visit(value[key], path, depth + 1)
                if len(found) == before:
                    found.add(path)
        elif isinstance(value, list) and prefix + "[]" in _UPDATE_FIELDS:
            for entry in value[:64]:
                visit(entry, prefix + "[]", depth + 1)
        elif prefix:
            found.add(prefix)

    visit(patch)
    return found


def update_error(status, endpoint, response_headers, raw, *, patch=None,
                 request_headers=None, item_id=None, retry_action="sync"):
    """Describe a rejected PATCH with safe diagnostics and appropriate recovery.

    The response may echo private board content. Only fixed API field names,
    known error codes and strict correlation identifiers are included. A remote
    numeric Miro ID identifies the failed item; nonstandard IDs become a stable
    opaque reference. No content values, arbitrary paths or messages are logged.
    """
    status = status if type(status) is int and 100 <= status <= 599 else "unknown"
    result = _request_error("PATCH", status, endpoint, None, response_headers, raw,
                            request_headers=request_headers)
    secrets = _secrets(request_headers)

    def safe(value):
        return not any(secret and secret in value for secret in secrets)

    details = []
    if isinstance(item_id, str) and item_id:
        if _ITEM_ID.fullmatch(item_id) and safe(item_id):
            details.append("item_id=" + item_id)
        else:
            # Bounded input also prevents a malformed identifier from turning
            # error handling into expensive work. This reference is not a URL.
            reference = hashlib.sha256(item_id[:1024].encode("utf-8", errors="replace")).hexdigest()[:12]
            if safe(reference):
                details.append("item_ref=" + reference)
    for label, fields in (("rejected_fields", _validation_fields(_response_data(raw))),
                          ("submitted_fields", _submitted_fields(patch))):
        names = sorted(field for field in fields if safe(field))
        if names:
            details.append(label + "=" + ",".join(names))
    if details:
        result = result[:-1] + "; " + "; ".join(details) + ")"
    retry_action = retry_action if retry_action == "Create / update Miro frames" else "sync"
    if status in (400, 422):
        advice = ("Miro rejected the update fields; correct the rejected fields or report these diagnostics "
                  "before retrying. Repeating the same request will not fix this validation error")
    elif status == 401:
        advice = "check or refresh MIRO_ACCESS_TOKEN, then rerun " + retry_action
    elif status == 403:
        advice = "check board access and the token's boards:write scope, then rerun " + retry_action
    elif status == 404:
        advice = "the board or item was not found; check the linked board and item before rerunning " + retry_action
    elif status == 409:
        advice = "the update conflicted with the current board state; rerun " + retry_action + " to refresh and reconcile"
    elif status == 429:
        advice = "the Miro rate limit was reached; wait for it to reset, then rerun " + retry_action
    elif status == 408 or (type(status) is int and status >= 500):
        advice = ("Miro could not confirm the update; rerun " + retry_action +
                  " to read the current item and reconcile the saved update")
    else:
        advice = "inspect these diagnostics and check board access before rerunning " + retry_action
    return result + "; acknowledged progress is saved; " + advice
