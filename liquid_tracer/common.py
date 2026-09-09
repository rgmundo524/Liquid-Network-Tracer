import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

LBTC = "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d"
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class TraceError(Exception):
    pass


class StopRun(TraceError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_outpoint(value):
    txid, sep, index = value.strip().rpartition(":")
    if not sep or not HEX64.fullmatch(txid):
        raise TraceError("Seed must contain a 64-character transaction hash, a colon, and an output number, "
                         "for example HASH:0. Do not include a backslash before the colon.")
    if not index.isascii() or not index.isdigit():
        raise TraceError("Seed output number must be a nonnegative integer: 0, 1, 2, etc. "
                         "Replace the word 'vout' with the actual output number.")
    if int(index) > 0xffffffff:
        raise TraceError("Output index exceeds uint32")
    return txid.lower(), int(index)


def output_kind(output):
    if output.get("pegout"):
        return "pegout"
    if output.get("scriptpubkey_type") == "fee" or output.get("scriptpubkey") == "":
        return "fee"
    if output.get("scriptpubkey_type") == "op_return" or output.get("scriptpubkey", "").startswith("6a"):
        return "provably_unspendable"
    return "spendable"


def public_fields(output):
    return {key: output.get(key) for key in (
        "scriptpubkey", "scriptpubkey_type", "scriptpubkey_address", "value",
        "valuecommitment", "asset", "assetcommitment", "pegout")}


def quantity(output):
    value = output.get("value")
    if value is None:
        amount = "amount confidential" if output.get("valuecommitment") else "amount unavailable"
    else:
        amount = str(value) + " base units"
    asset = output.get("asset")
    name = "L-BTC" if asset == LBTC else ((asset[:10] + "…") if asset else "asset unknown")
    return amount + "; " + name


def load_labels(path=None):
    labels = read_json(path) if path else []
    if not isinstance(labels, list):
        raise TraceError("Labels must be a JSON array")
    for label in labels:
        if label.get("kind") not in ("address", "script", "outpoint"):
            raise TraceError("Label kind must be address, script, or outpoint")
        for field in ("value", "entity", "source", "confidence", "observed_at"):
            if not label.get(field):
                raise TraceError("Each label requires " + field)
        if label["confidence"] not in ("candidate", "corroborated", "confirmed"):
            raise TraceError("Invalid label confidence")
        if "stop" in label and not isinstance(label["stop"], bool):
            raise TraceError("Label stop must be a JSON boolean")
    return labels


def match_labels(labels, outpoint, output):
    targets = {"outpoint": outpoint, "address": output.get("scriptpubkey_address"),
               "script": output.get("scriptpubkey")}
    return [label for label in labels if targets[label["kind"]] == label["value"]]
