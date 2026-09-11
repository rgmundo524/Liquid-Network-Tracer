"""Small, non-content diagnostics for failed Miro creation requests.

Miro describes error code/message/context separately in its official client:
https://github.com/miroapp/api-clients/blob/main/packages/miro-api/model/bulkOperationError.ts
Messages and arbitrary context can echo submitted board text, so neither is
included here. Unknown codes and unrecognized correlation formats are omitted.
"""

import json
import re


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


def _request_error(method, status, endpoint, count, response_headers, raw, *, request_headers=None):
    """Describe the operation without printing headers, request data, or errors.

Only recognized error codes and UUID/128-bit hexadecimal request identifiers
are displayed. In particular, transaction hashes, URLs, HTML, arbitrary server
messages, credentials, and oversized/malformed response bodies are not logged.
"""
    operation = endpoint if endpoint in _ENDPOINTS else "items"
    details = [operation]
    if count is not None:
        number = count if type(count) is int and 1 <= count <= 20 else 1
        details.append(f"{number} item" + ("s" if number != 1 else ""))
    secrets = []
    for key, value in (request_headers or {}).items():
        if str(key).lower() == "authorization" and isinstance(value, str):
            secrets.extend([value, value.removeprefix("Bearer ")])

    def safe(value, pattern):
        return (isinstance(value, str) and pattern.fullmatch(value) is not None
                and not any(secret and secret in value for secret in secrets))

    data = {}
    if isinstance(raw, (bytes, str)) and len(raw) <= 16384:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, TypeError, UnicodeError, RecursionError):
            pass
    code = data.get("code")
    if safe(code, _CODE) and code.replace("_", "").lower() in _ERROR_CODES:
        details.append("code=" + code)
    identifiers = {}
    for key, value in (response_headers or {}).items():
        name = _ID_HEADERS.get(str(key).lower())
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
