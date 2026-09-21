"""Shared, presentation-only color key for Miro and local graph previews."""

import html

from .name_colors import color_value, validate_name_colors
from .role_colors import validate_role_colors


_ROLES = (
    ("starting_transaction", "Starting transaction", "A transaction provided as a starting point."),
    ("transaction", "Subsequent transaction", "A transaction reached in later hops."),
    ("seed", "Selected seed", "An output selected to begin tracing."),
    ("candidate", "Reachable address", "An address reached by the traced outputs."),
    ("unspent_endpoint", "Unspent endpoint", "A traced output observed unspent at its last check."),
    ("address", "Context address", "An address shown for transaction context."),
    ("event", "Event", "Peg-out requests, fees, and other events."),
    ("traced_edge", "Traced arrow", "A thicker arrow showing a traced UTXO link."),
    ("context_edge", "Context arrow", "A thinner arrow showing transaction context."),
)

LEGEND_CSS = """
.trace-legend { margin:20px 0 8px; color:#172033; }
.trace-legend h2 { font-size:18px; margin:0 0 12px; }
.trace-legend-rows { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr)); gap:12px 28px; padding:0; margin:0; list-style:none; }
.trace-legend-row { display:flex; gap:12px; align-items:flex-start; margin:0; min-width:0; }
.trace-legend-swatch { display:block; flex:0 0 20px; width:20px; height:20px; margin-top:2px; border:1px solid #64748b; border-radius:50%; box-sizing:border-box; }
.trace-legend-label { display:block; font-weight:650; overflow-wrap:anywhere; }
.trace-legend-description { display:block; margin-top:2px; color:#475569; overflow-wrap:anywhere; }
.trace-legend-notes { margin:14px 0 0; padding-left:18px; color:#475569; font-size:13px; }
.trace-legend-notes li { margin:5px 0; }
"""


def legend_rows(graph=None):
    """Return one swatch and definition per role or referenced attribution name.

    Colors come from the graph's presentation snapshot. Unreferenced case names
    stay out of the key. Names differing only in capitalization share a row.
    Neither saved evidence nor the supplied graph is modified.
    """
    from .export import PALETTE

    graph = graph or {}
    settings = graph.get("service_controls", {})
    colors = validate_role_colors(settings.get("role_colors", {}))
    rows = [{"key": "role:" + role, "color": colors.get(role, PALETTE[role][1]),
             "label": label, "description": description}
            for role, label, description in _ROLES]
    palette = validate_name_colors(settings.get("name_colors", {}))
    names = {}
    for node in graph.get("nodes", []):
        details = node.get("details", {})
        if node.get("kind") != "address" or details.get("network") != "liquid":
            continue
        assigned = validate_name_colors(details.get("name_colors", {}))
        for assessment in details.get("address_attributions", []):
            name = assessment.get("entity") or assessment.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            key = name.casefold()
            color = assigned.get(key, palette.get(key))
            if color is not None:
                names.setdefault(key, {"variants": set(), "color": color})["variants"].add(name)
    arrows = graph.get("graph_options", {}).get("color_attribution_arrows", False)
    description = ("Assigned to this name and arrows directly touching its addresses."
                   if arrows else "Assigned to addresses with this attribution name.")
    for key, record in sorted(names.items()):
        rows.append({"key": "name:" + key, "color": color_value(record["color"]),
                     "label": sorted(record["variants"])[0], "description": description})
    return rows


def legend_notes(graph=None):
    """Short interpretation notes, separate from the scan-friendly color key."""
    arrows = (graph or {}).get("graph_options", {}).get("color_attribution_arrows", False)
    return [
        "Squares = transactions; circles = addresses; diamonds = events.",
        "Selected seeds keep their seed color. Assigned name colors override other address colors.",
        ("Named arrows color only links directly entering or leaving that address; other arrows use the defaults."
         if arrows else "Arrows use the traced and context colors shown above."),
        "Thick red borders mark branch convergence. Colors and links do not prove ownership or allocate value.",
        "?? = not publicly available. STOP TRACING = an explicit address boundary.",
    ]


def legend_html(graph=None):
    """Escaped HTML key; include LEGEND_CSS in the enclosing document."""
    rows = []
    for row in legend_rows(graph):
        rows.append('<li class="trace-legend-row" data-legend-key="'
                    + html.escape(row["key"], quote=True) + '">'
                    '<span class="trace-legend-swatch" aria-hidden="true" style="background-color:'
                    + row["color"] + '"></span><span><span class="trace-legend-label">'
                    + html.escape(row["label"]) + '</span><span class="trace-legend-description">'
                    + html.escape(row["description"]) + '</span></span></li>')
    notes = "".join("<li>" + html.escape(note) + "</li>" for note in legend_notes(graph))
    return ('<section class="trace-legend" aria-label="Graph legend"><h2>Graph legend</h2>'
            '<ul class="trace-legend-rows">' + "".join(rows) + '</ul>'
            '<ul class="trace-legend-notes">' + notes + '</ul></section>')
