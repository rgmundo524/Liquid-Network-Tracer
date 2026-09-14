"""Explicit address names, independent stop flags, and reviewable evidence notes."""
import html
from .common import digest
from .services import confidence_value, notes_for


def display_name(assessment):
    name = assessment.get("entity") or assessment.get("name") or "Unnamed address"
    if assessment.get("classification") == "suspected_service" and name == "Suspected service":
        name = "service"  # Historical generated fallback, not a new classification.
    return ("" if confidence_value(assessment.get("confidence")) == "confirmed" else "Suspected ") + name


def attribution_reference(node_id):
    return "A-" + digest(node_id.encode())[:12]


def attribution_lines(node):
    """One record per address node; retain complete names, sources and notes."""
    assessments = node.get("details", {}).get("address_attributions", [])
    if not assessments:
        return []
    details = node["details"]
    lines = [attribution_reference(node["id"]), "Address: " + (details.get("address") or node["id"]),
             "Network: " + details.get("network", "liquid")]
    for assessment in assessments:
        lines.extend(["Name: " + display_name(assessment),
                      "Confidence: " + str(confidence_value(assessment.get("confidence"))),
                      "Stop tracing: " + ("Yes" if assessment.get("stop") is True else "No"),
                      "Source: " + str(assessment.get("source", "")),
                      "Notes: " + str(notes_for(assessment)),
                      "Observation: " + str(assessment.get("observed_at", ""))])
    if details.get("name_colors"):
        lines.append("Assigned name colors: " + ", ".join(name + " = " + color for name, color in details["name_colors"].items()))
        if node.get("role") == "seed":
            if node.get("color_source") == "role_palette":
                lines.append("Display: selected seed color " + node["color"] + " takes priority over assigned colors.")
            else:
                lines.append("Display: selected seed red takes priority over assigned colors.")
        elif details.get("name_color_conflict"):
            lines.append("Display: conflicting name colors; normal trace-role color retained.")
    return lines


def register_html(graph):
    entries = []
    for node in graph["nodes"]:
        lines = attribution_lines(node)
        if lines:
            entries.append('<details id="' + html.escape(lines[0], quote=True) + '"><summary>'
                           + html.escape(lines[0] + " · " + display_name(node["details"]["address_attributions"][0]))
                           + '</summary><pre>' + html.escape("\n".join(lines)) + '</pre></details>')
    return '<section class="attribution-register"><h2>Address attribution register</h2>' + ''.join(entries) + '</section>' if entries else ''
