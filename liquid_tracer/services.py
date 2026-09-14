"""Investigator-designated service boundaries, separate from immutable run evidence."""

import copy
import fcntl
import heapq
from collections import defaultdict
from pathlib import Path

from .address_activity import validate_address
from .common import TraceError, match_labels, now, output_kind, read_json, save_json
from .investigations import read_case

MANAGED_BY = "case_service_rules"
SERVICE_STATUSES = {"suspected_service_stop", "held_behind_service"}
CONFIDENCES = {"suspected", "confirmed"}


def confidence_value(value):
    """Read legacy confidence conservatively, without upgrading old assessments."""
    return "suspected" if value in (None, "candidate", "corroborated") else value


def notes_for(rule):
    return rule.get("notes", rule.get("rationale", ""))


def rule_fields(rule):
    """Read old records without modifying their archived values or audit history."""
    return {"confidence": confidence_value(rule.get("confidence")),
            "source": rule.get("source", "Investigator designation"),
            "observed_at": rule.get("observed_at", ""),
            "stop_tracing": rule.get("stop_tracing", rule.get("classification") != "label")}


def validate_rule_fields(fields):
    if fields["confidence"] not in CONFIDENCES:
        raise TraceError("Confidence must be suspected or confirmed")
    if type(fields["stop_tracing"]) is not bool:
        raise TraceError("Stop tracing must be true or false")
    _text(fields["source"], "Source", 1000, required=True)
    value = _text(fields["observed_at"], "Observation date", 80)
    if value:
        from datetime import datetime
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise TraceError("Observation date must be an ISO date or timestamp") from None
    return fields


def _text(value, name, maximum, *, required=False, multiline=False):
    if not isinstance(value, str):
        raise TraceError(name + " must be text")
    value = value.replace("\r\n", "\n").strip() if multiline else value.strip()
    if len(value) > maximum or (required and not value):
        raise TraceError(f"{name} must contain {'1' if required else '0'} to {maximum} characters")
    if any((ord(char) < 32 and not (multiline and char in "\n\t")) or ord(char) == 127 for char in value):
        raise TraceError(name + " cannot contain control characters")
    return value


def _validate(data, identity):
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") not in (1, 2)
            or data.get("case_id") != identity or type(data.get("revision")) is not int
            or data["revision"] < 0 or not isinstance(data.get("rules"), dict)
            or not isinstance(data.get("history"), list)):
        raise TraceError("Invalid address-assessment settings; restore services.json")
    for address, rule in data["rules"].items():
        if (validate_address(address) != address or not isinstance(rule, dict)
                or rule.get("address") != address
                or type(rule.get("enabled")) is not bool):
            raise TraceError("Invalid address-assessment rule")
        if "classification" in rule and rule["classification"] not in ("suspected_service", "service", "label"):
            raise TraceError("Invalid historical address classification")
        validate_rule_fields(rule_fields(rule))
        for field, maximum in (("name", 120), ("created_at", 80), ("updated_at", 80)):
            if _text(rule.get(field), field, maximum, required=field.endswith("_at"),
                     multiline=False) != rule[field]:
                raise TraceError("Invalid address-assessment rule " + field)
        _text(notes_for(rule), "Notes", 4000, multiline=True)
    from .name_colors import validate_name_colors
    validate_name_colors(data.get("name_colors", {}))
    return data


def load_services(case):
    """Read one atomic settings version; caller holds trace.lock for a run snapshot."""
    case = Path(case)
    identity = read_case(case)["case_id"]
    path = case / "services.json"
    if not path.exists():
        return {"schema_version": 2, "case_id": identity, "revision": 0, "rules": {}, "history": []}
    try:
        return _validate(read_json(path), identity)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise TraceError("Unable to read address-assessment settings; restore services.json") from error


def set_service(case, address, *, name=None, notes=None, rationale=None, enabled=True,
                confidence=None, source=None, observed_at=None, stop_tracing=None,
                classification=None):
    """Save an independent attribution and stop flag.

    ``rationale`` and ``classification`` are Python compatibility arguments only.
    Classification is ignored and never controls a new rule. Legacy callers may
    pass old confidence spellings with their obsolete classification argument.
    New UI/CSV entry points accept only suspected/confirmed and notes.
    """
    if notes is not None and rationale is not None:
        raise TraceError("Supply notes, not both notes and rationale")
    if classification is not None and confidence is not None:
        confidence = confidence_value(confidence)
    return _update_service(case, address, name=name,
                           rationale=notes if notes is not None else rationale, enabled=enabled,
                           fields={"confidence": confidence, "source": source,
                                   "observed_at": observed_at, "stop_tracing": stop_tracing})


def _update_service(case, address, *, name=None, rationale=None, enabled=True, preserve_text=False, fields=None):
    case, address = Path(case), validate_address(address)
    name = _text(name, "Name", 120) if name is not None else None
    rationale = _text(rationale, "Notes", 4000, multiline=True) if rationale is not None else None
    if type(enabled) is not bool:
        raise TraceError("Service stop enabled must be true or false")
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace is running in this case; change service stops after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            data = load_services(case)
            stamp = now()
            previous = data["rules"].get(address)
            if preserve_text:
                if previous is None:
                    raise TraceError("This address has no saved assessment")
                name, rationale = previous["name"], notes_for(previous)
            name = name if name is not None else (previous or {}).get("name", "")
            rationale = rationale if rationale is not None else notes_for(previous or {})
            metadata = rule_fields(previous or {})
            metadata.update({key: value for key, value in (fields or {}).items() if value is not None})
            metadata["source"] = _text(metadata["source"], "Source", 1000, required=True)
            metadata["observed_at"] = _text(metadata["observed_at"], "Observation date", 80)
            validate_rule_fields(metadata)
            rule = {"address": address, **metadata, "name": name,
                    "notes": rationale, "enabled": enabled,
                    "created_at": previous["created_at"] if previous else stamp, "updated_at": stamp}
            data["schema_version"] = 2
            data["rules"][address] = rule
            data["revision"] += 1
            data["history"].append({"revision": data["revision"], "changed_at": stamp,
                                    "address": address, "previous": previous, "rule": copy.deepcopy(rule)})
            save_json(case / "services.json", data)
    return data


def disable_service(case, address):
    return _update_service(case, address, enabled=False, preserve_text=True)


def service_labels(settings):
    labels = []
    for address, rule in sorted(settings["rules"].items()):
        if not rule["enabled"]:
            continue
        fields = rule_fields(rule)
        labels.append({"kind": "address", "value": address,
            "entity": rule["name"] or "Unnamed address",
            "source": fields["source"], "confidence": fields["confidence"],
            "managed_by": MANAGED_BY, "stop": fields["stop_tracing"],
            "observed_at": fields["observed_at"] or rule["updated_at"], "notes": notes_for(rule)})
    return labels


def apply_service_labels(labels, settings):
    """Replace only our previous snapshot; explicit imported labels stay intact."""
    return copy.deepcopy([label for label in labels if label.get("managed_by") != MANAGED_BY]) + service_labels(settings)


def is_service_stop(label):
    return label.get("kind") == "address" and label.get("stop") is True


class ServiceScope:
    """Forward reachability from independent seeds through retained spend evidence.

    A boundary added after expansion must also hold descendant frontier that is
    reachable only across that boundary. Saved links and transactions remain
    untouched; an independent seed or alternate path can make that frontier
    available again. Address reuse cannot manufacture new graph edges.
    """

    def __init__(self, state):
        self.state = state
        self.addresses = {label["value"] for label in state["labels"] if is_service_stop(label)}
        self.active = bool(self.addresses) or any(item.get("trace_control") for item in state["outputs"].values())
        self.by_tx = defaultdict(list)
        self.reachable = set()
        self.depths = {}
        self.expanded_tx_depth = {}
        for item in state["outputs"].values():
            item.pop("trace_scope_depth", None)
        if not self.active:
            return
        for key, item in state["outputs"].items():
            self.by_tx[item["txid"]].append(key)
            self._restore(item)
            self._refresh_labels(item)
        self._walk([(0, seed) for seed in state["seeds"]])
        for key, item in state["outputs"].items():
            if key not in self.reachable:
                self._hold(item, "held_behind_service")

    @staticmethod
    def _restore(item):
        control = item.pop("trace_control", None)
        if control and item["status"] in SERVICE_STATUSES:
            item["status"] = control["previous_status"]

    @staticmethod
    def _hold(item, reason, address=None):
        control = {"reason": reason, "previous_status": item["status"]}
        if address:
            control["address"] = address
        item["trace_control"] = control
        if item["status"] not in {"spent", "fee", "pegout", "provably_unspendable"}:
            item["status"] = reason

    def stop_address(self, item):
        data = self.state["transactions"].get(item["txid"], {}).get("data", {})
        outputs = data.get("vout", [])
        if item["vout"] >= len(outputs):
            return None
        output = outputs[item["vout"]]
        address = output.get("scriptpubkey_address")
        return address if address in self.addresses and output_kind(output) == "spendable" else None

    def _refresh_labels(self, item):
        outputs = self.state["transactions"].get(item["txid"], {}).get("data", {}).get("vout", [])
        if item["vout"] < len(outputs):
            item["labels"] = match_labels(self.state["labels"], item["outpoint"], outputs[item["vout"]])

    def register(self, key):
        if self.active:
            self.by_tx[self.state["outputs"][key]["txid"]].append(key)

    def depth(self, item):
        return self.depths.get(item["outpoint"], item["depth"])

    def admit(self, key, depth):
        """Return newly available outputs when a new unblocked path reaches them."""
        if not self.active:
            return []
        return self._walk([(depth, key)])

    def _walk(self, starts):
        queue, admitted = list(starts), []
        heapq.heapify(queue)
        while queue:
            depth, key = heapq.heappop(queue)
            if key not in self.state["outputs"] or depth >= self.depths.get(key, float("inf")):
                continue
            self.reachable.add(key)
            self.depths[key] = depth
            item = self.state["outputs"][key]
            self._restore(item)
            self._refresh_labels(item)
            # Preserve the historic shortest depth as evidence. Bounds now use
            # the shortest path allowed by this run's current service rules.
            item["trace_scope_depth"] = depth
            address = self.stop_address(item)
            if address:
                self._hold(item, "suspected_service_stop", address)
                continue
            admitted.append(key)
            link = self.state["links"].get(key)
            if link:
                txid, child_depth = link["spending_txid"], depth + 1
                if child_depth < self.expanded_tx_depth.get(txid, float("inf")):
                    self.expanded_tx_depth[txid] = child_depth
                    for child in self.by_tx[txid]:
                        heapq.heappush(queue, (child_depth, child))
        return admitted

    def blocked(self, key):
        return self.active and bool(self.state["outputs"][key].get("trace_control"))

    def mark_stop(self, item, address):
        self._restore(item)
        self._hold(item, "suspected_service_stop", address)
