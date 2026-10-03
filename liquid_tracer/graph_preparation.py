"""Request-scoped graph preparation; no persistent evidence caches."""

from collections import defaultdict
from copy import deepcopy


class LabelLookup:
    """Preserve match_labels ordering and duplicate rows with indexed matches."""

    def __init__(self, labels):
        self._by_kind = {kind: defaultdict(list) for kind in ("outpoint", "address", "script")}
        for index, label in enumerate(labels):
            self._by_kind[label["kind"]][label["value"]].append((index, label))

    def __call__(self, outpoint, output):
        matches = []
        for kind, value in (("outpoint", outpoint), ("address", output.get("scriptpubkey_address")),
                            ("script", output.get("scriptpubkey"))):
            matches.extend(self._by_kind[kind].get(value, ()))
        return [label for _, label in sorted(matches, key=lambda item: item[0])]


def copy_for_geometry(graph):
    """Copy a private candidate for coordinate-only trial changes.

    The caller must already own the candidate (independent of caller evidence).
    Node/edge details remain read-only and are shared with that private candidate;
    all mutable presentation fields are deep-copied. This helper is deliberately
    not used to make the initial candidate or to return caller-owned input data.
    """
    evidence = {id(item["details"]): item["details"]
                for field in ("nodes", "edges") for item in graph[field] if "details" in item}
    return deepcopy(graph, evidence)
