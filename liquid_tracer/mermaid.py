"""Local Mermaid previews of already verified trace graphs.

Only the renderer subprocess is invoked here. No credentials, explorer requests,
Miro requests, or changes to the saved run are needed to create a preview.
"""

import base64
import html
import os
import re
import shutil
import signal
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import TraceError, save_json
from .export import COLORS, edge_color, legend_lines
from .processes import defer_cancellation_during_spawn

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def _label(value):
    """Quote literal text without allowing Mermaid syntax or HTML injection.

    Mermaid's decimal entities are decoded after parsing. Encode the entity
    introducer too, so externally supplied entities cannot become syntax. Only
    generated line breaks use markup; htmlLabels=False renders SVG text spans.
    """
    parts = []
    for char in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            parts.append("<br/>")
        elif char in '"#&<>|`\\{}[];%':
            parts.append(f"#{ord(char)};")
        elif ord(char) < 32 or ord(char) == 127:
            parts.append(" ")
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _items(graph):
    nodes = sorted(graph["nodes"], key=lambda node: node["id"])
    edges = sorted(graph["edges"], key=lambda edge: edge["id"])
    if not nodes:
        raise TraceError("The saved run has no graph nodes to render")
    ids = {node["id"]: f"n{index}" for index, node in enumerate(nodes)}
    if len(ids) != len(nodes):
        raise TraceError("The graph has duplicate node identifiers")
    if any(edge["source"] not in ids or edge["target"] not in ids for edge in edges):
        raise TraceError("The graph contains an edge without both endpoints")
    return nodes, edges, ids


def mermaid_source(graph):
    """Return deterministic, standalone Mermaid text, including every edge."""
    nodes, edges, ids = _items(graph)
    lines = ["flowchart LR", "  %% Local Liquid Network trace; IDs map to mermaid-node-map.json."]
    shapes = {"transaction": ("[", "]"), "address": ("((", "))"), "event": ("{", "}")}
    for node in nodes:
        if node["kind"] not in shapes:
            raise TraceError("The graph contains an unsupported node kind")
        start, end = shapes[node["kind"]]
        color = node.get("color", COLORS[node["kind"]])
        if not isinstance(color, str) or not _COLOR.fullmatch(color):
            raise TraceError("The graph contains an invalid node color")
        identifier = ids[node["id"]]
        lines.append(f"  {identifier}{start}{_label(node['label'])}{end}")
        lines.append(f"  style {identifier} fill:{color},stroke:#334155,stroke-width:2px,color:#172033")
    for edge in edges:
        caption = edge["label"] + (" · " + edge["quantity"] if edge.get("quantity") else "")
        lines.append(f"  {ids[edge['source']]} -->|{_label(caption)}| {ids[edge['target']]}")
    for index, edge in enumerate(edges):
        lines.append(f"  linkStyle {index} stroke:{edge_color(edge['role'])},stroke-width:2px,color:#334155")
    return "\n".join(lines) + "\n"


def _preview_html(graph, svg):
    # An SVG image cannot execute embedded scripts. Embedding the image also
    # makes this page portable and keeps viewing independent of a web server.
    encoded = base64.b64encode(svg).decode("ascii")
    legend = legend_lines()
    legend[1] = "Pink diamonds: events. Arrows show the direction of UTXO links."
    items = "".join("<li>" + html.escape(line) + "</li>" for line in legend)
    run_id = html.escape(str(graph.get("run_id", "")))
    notice = html.escape(str(graph.get("notice", "")))
    fees = "included" if graph.get("include_fees") else "hidden"
    simulated = " · Synthetic demonstration data" if graph.get("simulated") else ""
    fallback = graph.get("preview", {}).get("renderer") == "direct_svg"
    title = "Direct SVG fallback" if fallback else "Mermaid preview"
    if fallback:
        cause = ("Mermaid reached its rendering time limit." if graph["preview"]["reason"] == "timeout"
                 else "This graph exceeds the automatic Mermaid rendering threshold.")
        layout_note = (cause + " This SVG uses the saved dependency layout. All displayed objects and connections "
                       "are retained; ELK and Mermaid optimization were not applied. "
                       "The complete Mermaid source remains available below.")
    else:
        layout_note = ("Mermaid arranges the graph from left to right. Cycles can return left; "
                       "Miro positions and fixed transaction connection sides are not copied.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Liquid trace · {title}</title>
<style>
body {{ margin:0; color:#172033; background:#f5f6f8; font:15px system-ui,sans-serif; }}
header {{ padding:20px 28px; background:white; border-bottom:1px solid #d5dbe3; }}
h1 {{ margin:0 0 10px; font-size:24px; }}
p {{ margin:8px 0; }} a {{ color:#155e75; }}
summary {{ cursor:pointer; }} li {{ margin:6px 0; }}
.chart {{ overflow:auto; padding:24px; background:white; }}
.chart img {{ display:block; max-width:none; }}
</style></head><body>
<header><h1>Liquid trace · {title}</h1>
<p>Run {run_id} · {len(graph['nodes'])} nodes · {len(graph['edges'])} links · Fees {fees}{simulated}</p>
<p>{notice}</p>
<p>{layout_note}</p>
<p>Scroll to explore; use your browser zoom to adjust the scale.</p>
<p><a href="graph.mmd" download>Mermaid source</a> · <a href="graph.svg" download>SVG</a> ·
<a href="graph.json" download>Graph details</a> · <a href="mermaid-node-map.json" download>Node identifiers</a></p>
<details><summary>Legend</summary><ul>{items}</ul></details></header>
<main class="chart"><img alt="Directed Liquid Network transaction graph" src="data:image/svg+xml;base64,{encoded}"></main>
</body></html>
"""


def _render(command, directory):
    # Chromium launches child processes. Give this invocation its own process
    # group so an interruption cannot leave a browser running after
    # the Node entry point exits. The application already requires POSIX.
    process = None
    try:
        with defer_cancellation_during_spawn():
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, cwd=directory, start_new_session=True)
        process.communicate()
        return process.returncode
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            finally:
                process.stdout.close()
                process.stderr.close()


def export_mermaid(graph, directory):
    """Create a new local preview directory and render using installed mmdc.

    The .mmd source and metadata survive renderer failures for troubleshooting
    and use with another Mermaid installation. Existing exports are untouched.
    """
    source = mermaid_source(graph)
    directory = Path(directory).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise TraceError("Mermaid output directory already exists; choose a new directory") from error
    except OSError as error:
        raise TraceError("Cannot create the Mermaid output directory") from error
    paths = {"directory": directory, "source": directory / "graph.mmd", "svg": directory / "graph.svg",
             "html": directory / "graph.html", "graph": directory / "graph.json",
             "node_map": directory / "mermaid-node-map.json"}
    config_path = directory / "mermaid-config.json"
    try:
        paths["source"].write_text(source, encoding="utf-8")
        save_json(paths["graph"], graph)
        _, edges, ids = _items(graph)
        save_json(paths["node_map"], {value: key for key, value in ids.items()})
        save_json(config_path, {
            "securityLevel": "strict", "htmlLabels": False, "theme": "base",
            "deterministicIds": True, "deterministicIDSeed": "liquid-tracer",
            "fontFamily": "sans-serif", "maxEdges": max(500, len(edges) + 1),
            "maxTextSize": max(50000, len(source) * 2),
            "flowchart": {"htmlLabels": False, "useMaxWidth": False, "curve": "basis",
                          "nodeSpacing": 45, "rankSpacing": 100},
        })
    except OSError as error:
        raise TraceError(f"Cannot write Mermaid preview files in {directory}") from error

    retained = f"Mermaid source saved at {paths['source']}."
    executable = os.environ.get("LIQUID_MERMAID_BIN") or shutil.which("mmdc")
    if not executable:
        raise TraceError(f"Mermaid renderer (mmdc) is unavailable. Enter the project's devenv shell and retry. {retained}")
    command = [executable, "--input", str(paths["source"]), "--output", str(paths["svg"]),
               "--configFile", str(config_path), "--backgroundColor", "white", "--quiet"]
    try:
        returncode = _render(command, directory)
    except OSError as error:
        raise TraceError(f"Cannot start Mermaid renderer. Enter the project's devenv shell and retry. {retained}") from error
    if returncode:
        paths["svg"].unlink(missing_ok=True)
        # Do not echo arbitrary renderer output, which can contain complete
        # investigation labels. The source and config support local diagnosis.
        raise TraceError(f"Mermaid rendering failed (exit {returncode}). Check the local mmdc/Chromium installation. {retained}")
    try:
        svg = paths["svg"].read_bytes()
        root = ET.fromstring(svg)
        if (root.tag != "{http://www.w3.org/2000/svg}svg" or not len(root)
                or root.get("aria-roledescription") == "error"):
            raise ValueError("Not a graph SVG")
        # Discovery treats graph.html as the completion marker. Publish its
        # name only after the entire preview is written, so interruption cannot
        # make an incomplete new preview hide a previous complete one.
        temporary = paths["html"].with_name("graph.html.tmp")
        temporary.write_text(_preview_html(graph, svg), encoding="utf-8")
        temporary.replace(paths["html"])
    except (OSError, ET.ParseError, ValueError) as error:
        paths["svg"].unlink(missing_ok=True)
        paths["html"].unlink(missing_ok=True)
        raise TraceError(f"Mermaid did not produce a usable SVG preview. {retained}") from error
    return {key: str(path) for key, path in paths.items()}
