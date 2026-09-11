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
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .common import TraceError, save_json
from .export import COLORS, edge_color, legend_lines
from .processes import defer_cancellation_during_spawn
from .render_runtime import renderer_failure, renderer_heap_mb

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_DIAGNOSTIC_BYTES = 64 * 1024


@dataclass(frozen=True)
class _RenderResult:
    returncode: int
    stderr: str
    heap_mb: int


def _renderer_environment(heap_mb):
    # Keep the pinned browser, local fonts and normal Linux runtime paths.
    # SecretSpec credentials and arbitrary Node/preload options are unnecessary.
    names = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT",
             "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR",
             "FONTCONFIG_FILE", "FONTCONFIG_PATH", "PUPPETEER_EXECUTABLE_PATH", "PUPPETEER_CACHE_DIR",
             "PUPPETEER_TMP_DIR", "PUPPETEER_SKIP_DOWNLOAD", "DISPLAY", "WAYLAND_DISPLAY")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment["NODE_OPTIONS"] = f"--max-old-space-size={heap_mb}"
    return environment


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
    heap_mb = renderer_heap_mb()
    browser_config = Path(directory) / "puppeteer-config.json"
    # A single CDP call includes the complete Mermaid layout calculation.
    # Puppeteer's default 180-second protocol deadline is a render deadline too.
    # Browser startup retains its normal bounded timeout and sandbox defaults.
    save_json(browser_config, {"protocolTimeout": 0,
                              "args": [f"--js-flags=--max-old-space-size={heap_mb}"]})
    command = [*command, "--puppeteerConfigFile", str(browser_config)]
    process = None
    reader = None
    tail = bytearray()

    def collect_stderr():
        try:
            while chunk := process.stderr.read1(8192):
                tail.extend(chunk)
                if len(tail) > _DIAGNOSTIC_BYTES:
                    del tail[:-_DIAGNOSTIC_BYTES]
        except (OSError, ValueError):
            pass  # Cleanup may close the pipe after killing the process group.

    try:
        with defer_cancellation_during_spawn():
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       cwd=directory, env=_renderer_environment(heap_mb), start_new_session=True)
        reader = threading.Thread(target=collect_stderr, daemon=True)
        reader.start()
        process.wait()
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            finally:
                if reader is not None:
                    reader.join(timeout=5)
                process.stderr.close()
    stderr = tail.decode("utf-8", errors="replace")
    if process.returncode and stderr:
        # This bounded local diagnostic can contain investigation labels.
        # It is deliberately absent from the web download allowlist.
        try:
            descriptor = os.open(Path(directory) / "mermaid-renderer.log",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(tail)
        except OSError:
            pass  # Diagnostic persistence must not mask the renderer failure.
    return _RenderResult(process.returncode, stderr, heap_mb)


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
        result = _render(command, directory)
    except OSError as error:
        raise TraceError(f"Cannot start Mermaid renderer. Enter the project's devenv shell and retry. {retained}") from error
    returncode = result if isinstance(result, int) else result.returncode
    if returncode:
        paths["svg"].unlink(missing_ok=True)
        # Do not echo arbitrary renderer output, which can contain complete
        # investigation labels. The source and config support local diagnosis.
        stderr = "" if isinstance(result, int) else result.stderr
        heap_mb = renderer_heap_mb() if isinstance(result, int) else result.heap_mb
        detail = renderer_failure(stderr, returncode, "Mermaid", heap_mb)
        diagnostic = (f" Local renderer details saved at {directory / 'mermaid-renderer.log'}."
                      if (directory / "mermaid-renderer.log").is_file() else "")
        raise TraceError(f"{detail}{diagnostic} {retained}")
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
