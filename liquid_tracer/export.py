import csv
import html
import json
from pathlib import Path

from .common import (LBTC, TraceError, canonical, digest, match_labels, output_kind,
                     public_fields, save_json)
from .trace import TERMINAL
from .layout import arrange, fee_date, transaction_ranks

PRESENTATION_VERSION = 3
# Both renderers and their legends use this palette. Node colors describe the
# displayed role, not ownership of an address or allocation of stolen value.
PALETTE = {
    "transaction": ("Blue", "#a6ccf5"),
    "address": ("Light gray", "#f5f6f8"),
    "seed": ("Red", "#f16c7f"),
    "candidate": ("Yellow", "#fff9b1"),
    "event": ("Pink", "#ea94bb"),
    "attributed": ("Green", "#d5f692"),
    "traced_edge": ("Dark teal", "#155e75"),
    "context_edge": ("Gray", "#9ca3af"),
}
COLORS = {key: value[1] for key, value in PALETTE.items()}
_ADDRESS_PRIORITY = {"address": 0, "candidate": 1, "seed": 2, "attributed": 3}


def legend_lines():
    def name(key):
        return PALETTE[key][0]
    return [
        f"{name('transaction')} squares: transactions, including starting transactions. {name('event')} diamonds: events.",
        f"Circles: {name('seed').lower()} = selected seed outputs; {name('candidate').lower()} = reachable candidate outputs.",
        f"Circles: {name('address').lower()} = context; {name('attributed').lower()} = analyst attribution (read confidence).",
        f"Arrows: {name('traced_edge').lower()} = traced UTXO links; {name('context_edge').lower()} = context only.",
        "Captions: vin/vout number · amount asset. ?? = not publicly available. Known amounts are in base units.",
        "Overlapping circle roles: attribution > seed > candidate > context. Arrows do not allocate stolen value.",
    ]


def edge_color(role):
    return COLORS["context_edge" if role.startswith("context") else "traced_edge"]


def graph_quantity(output):
    """Compact public quantity without inferring hidden assets or values."""
    value, asset = output.get("value"), output.get("asset")
    amount = "??" if value is None else str(value) + " base units"
    name = "L-BTC" if asset == LBTC else (short(asset) if asset else "??")
    return amount + " " + name


def short(value):
    return value if len(value) <= 20 else value[:10] + "…" + value[-7:]


def build_graph(state, merge_addresses=False, include_fees=False):
    nodes, edges, fee_items = {}, [], {}
    ranks, cycle_groups = transaction_ranks(state["transactions"])
    source = state["source"]
    explorer = "https://blockstream.info/" + ("liquidtestnet" if "liquidtestnet" in source else "liquid")
    simulated = source.startswith("fixture://")

    def add_node(key, kind, label, column, details, url=None, color=None):
        if key not in nodes:
            nodes[key] = {"id": key, "kind": kind, "label": label, "column": column,
                          "details": details, "url": url, "color": color or COLORS[kind]}
        else:
            nodes[key]["column"] = min(nodes[key]["column"], column)
        return key

    def address(key, output, column, network="liquid"):
        addr = output.get("scriptpubkey_address")
        kind = output_kind(output)
        tracked = state["outputs"].get(key) if network == "liquid" else None
        matches = match_labels(state["labels"], key, output) if network == "liquid" else []
        if kind != "spendable":
            label = "PEG-OUT REQUEST" if kind == "pegout" else ("FEE" if kind == "fee" else "UNSPENDABLE")
            peg = output.get("pegout") or {}
            destination = peg.get("scriptpubkey_address")
            label += "\n" + (short(destination) if destination else "vout " + key.rsplit(":", 1)[-1])
            return add_node("event:" + key, "event", label, column,
                            {"outpoint": key, "output": output, "trace": tracked})
        node_key = (network + ":address:" + (addr or output.get("scriptpubkey") or key)
                    if merge_addresses else network + ":outpoint:" + key)
        label = short(addr) if addr else "Address ??"
        if network != "liquid":
            label = network.upper() + "\n" + label
        role = "address"
        if network == "liquid":
            if key in state["outputs"]:
                role = "candidate"
            if key in state["seeds"]:
                role = "seed"
            if matches:
                role = "attributed"
        url = None if simulated or not addr or network != "liquid" else explorer + "/address/" + addr
        node_id = add_node(node_key, "address", label, column,
            {"address": addr, "network": network, "occurrences": []}, url, COLORS[role])
        node = nodes[node_id]
        # One merged circle can occur first as context and later as a seed or
        # attributed output. Its role and label must not depend on visit order.
        if _ADDRESS_PRIORITY[role] >= _ADDRESS_PRIORITY[node.get("role", "address")]:
            node["role"], node["color"] = role, COLORS[role]
        occurrence = {"outpoint": key, "output": public_fields(output), "trace": tracked, "labels": matches}
        if occurrence not in node["details"]["occurrences"]:
            node["details"]["occurrences"].append(occurrence)
        attributions = sorted({m["entity"] + " (" + m["confidence"] + ")"
                               for item in node["details"]["occurrences"] for m in item["labels"]})
        node["label"] = label + ("\n" + ", ".join(attributions) if attributions else "")
        return node_id

    for txid, record in sorted(state["transactions"].items(), key=lambda item: (ranks[item[0]], item[0])):
        tx = record["data"]
        column = 2 * ranks[txid] + 1
        txnode = add_node("tx:" + txid, "transaction", "TX\n" + short(txid) + "\nhop " + str(record["depth"]),
                          column, {"transaction": tx, "observation_id": record["observation_id"]},
                          None if simulated else explorer + "/tx/" + txid)
        for index, vin in enumerate(tx["vin"]):
            key = f"{vin.get('txid', txid)}:{vin.get('vout', index)}"
            network = "bitcoin" if vin.get("is_pegin") else "liquid"
            prevout = vin.get("prevout") or {}
            if vin.get("is_coinbase"):
                input_node = add_node("coinbase:" + txid + ":" + str(index), "event", "COINBASE", column - 1, vin)
            else:
                input_node = address(key, prevout, column - 1, network)
            link = state["links"].get(key)
            traced = bool(link and link["spending_txid"] == txid and link["vin"] == index and network == "liquid")
            edges.append({"id": f"in:{txid}:{index}", "source": input_node, "target": txnode,
                          "role": "traced_input" if traced else "context_input", "outpoint": key,
                          "label": f"vin {index}", "quantity": graph_quantity(prevout),
                          "details": {"vin": vin, "validated_trace_link": link if traced else None}})
        for index, output in enumerate(tx["vout"]):
            key = f"{txid}:{index}"
            if output_kind(output) == "fee":
                fee_items["event:" + key] = {"endpoint": "shapes", "txid": txid, "vout": index}
                fee_items["out:" + key] = {"endpoint": "connectors", "source": txnode, "target": "event:" + key}
                if not include_fees:
                    continue
            output_node = address(key, output, column + 1)
            if output_kind(output) == "fee":
                nodes[output_node]["label"] += "\n" + fee_date(tx)
            role = "seed_output" if key in state["seeds"] else ("candidate_output" if key in state["outputs"] else "context_output")
            edges.append({"id": "out:" + key, "source": txnode, "target": output_node,
                          "role": role, "outpoint": key, "label": "vout " + str(index),
                          "quantity": graph_quantity(output), "details": public_fields(output)})

    layout = arrange(nodes, edges, state["transactions"], fee_items)
    layout["cycle_groups"] = cycle_groups
    mode = "merged" if merge_addresses else "outpoint_occurrences"
    return {"schema_version": 2, "presentation_version": PRESENTATION_VERSION,
            "run_id": state["run_id"], "simulated": simulated,
            "namespace": {"case_id": state["case_id"], "source": source, "address_mode": mode},
            "run": {key: state.get(key) for key in ("run_id", "parent_run", "ancestor_runs", "seeds", "started_at", "finished_at",
                    "status", "stop_reason", "limits", "stats")},
            "address_mode": mode,
            "include_fees": bool(include_fees),
            "graph_options": {"include_fees": bool(include_fees)},
            "fee_items": fee_items, "layout": layout,
            "notice": "UTXO reachability, not allocation of stolen value. Gray arrows and light gray circles are context. "
                      "?? marks amounts or assets not available from public data. "
                      "Repeated addresses are separate outpoint occurrences by default.",
            "nodes": list(nodes.values()), "edges": edges}


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
            # Keep spreadsheet programs from interpreting externally supplied labels as formulas.
            values = {k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in values.items()}
            writer.writerow(values)


def svg_graph(graph):
    lookup = {n["id"]: n for n in graph["nodes"]}
    node_top = min((n["y"] - n["height"] / 2 for n in lookup.values()), default=160)
    header_top = node_top - 170
    min_x = min(0, min((n["x"] - n["width"] / 2 for n in lookup.values()), default=0) - 30)
    min_y = min(0, header_top - 30)
    width = max(1100, max((n["x"] + n["width"] / 2 for n in lookup.values()), default=500) + 80) - min_x
    height = max((n["y"] + n["height"] / 2 for n in lookup.values()), default=300) + 50 - min_y
    chunks = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x} {min_y} {width} {height}" width="{width}" height="{height}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>',
        f'<rect x="{min_x}" y="{min_y}" width="{width}" height="{height}" fill="#fff"/>',
        '<g font-family="Arial, sans-serif">',
        f'<text x="40" y="{header_top}" font-size="24" font-weight="bold">Liquid UTXO trace' + (' · SYNTHETIC DEMO' if graph["simulated"] else '') + '</text>']
    chunks.extend(f'<text x="40" y="{header_top + 24 + index * 18}" font-size="13">{html.escape(line)}</text>'
                  for index, line in enumerate(legend_lines()))
    for edge in graph["edges"]:
        start, end = lookup[edge["source"]], lookup[edge["target"]]
        fee = graph.get("fee_items", {}).get(edge["id"], {}).get("endpoint") == "connectors"
        vertical = fee or start["x"] == end["x"]
        if vertical:
            direction = 1 if end["y"] > start["y"] else -1
            x1, y1, x2, y2 = start["x"], start["y"] + 80 * direction, end["x"], end["y"] - 80 * direction
        else:
            direction = 1 if end["x"] > start["x"] else -1
            x1, y1, x2, y2 = start["x"] + 80 * direction, start["y"], end["x"] - 80 * direction, end["y"]
        context = edge["role"].startswith("context")
        color = edge_color(edge["role"])
        mid = (x1 + x2) / 2
        controls = (f'{x1} {(y1+y2)/2}, {x2} {(y1+y2)/2}' if vertical
                    else f'{mid} {y1}, {mid} {y2}')
        chunks.append(f'<g class="edge {"context" if context else "tracked"}"><title>{html.escape(edge["outpoint"] + " | " + edge["quantity"])}</title>'
            f'<path d="M {x1} {y1} C {controls}, {x2} {y2}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#arrow)"/>'
            f'<text x="{mid}" y="{(y1+y2)/2-10}" text-anchor="middle" font-size="11" fill="{color}">{html.escape(edge["label"])}</text></g>')
    for node in graph["nodes"]:
        x, y, fill = node["x"], node["y"], node["color"]
        chunks.append(f'<g class="node" data-key="{html.escape(node["id"], quote=True)}" tabindex="0" style="cursor:pointer"><title>{html.escape(json.dumps(node["details"], ensure_ascii=False))}</title>')
        if node["kind"] == "address":
            shape = f'<circle cx="{x}" cy="{y}" r="80"'
        elif node["kind"] == "transaction":
            shape = f'<rect x="{x-80}" y="{y-80}" width="160" height="160"'
        else:
            shape = f'<polygon points="{x},{y-80} {x+80},{y} {x},{y+80} {x-80},{y}"'
        chunks.append(shape + f' fill="{fill}" stroke="#334155" stroke-width="2"/>')
        lines = node["label"].splitlines()
        for i, line in enumerate(lines):
            chunks.append(f'<text x="{x}" y="{y + (i-(len(lines)-1)/2)*18}" text-anchor="middle" dominant-baseline="middle" font-size="12">{html.escape(line)}</text>')
        chunks.append('</g>')
    chunks.append('</g></svg>')
    return "".join(chunks)


def html_graph(graph, svg):
    # JSON cannot close the script tag; all detail displays use textContent.
    encoded = json.dumps(graph, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Liquid UTXO trace</title><style>body{margin:0;font:14px system-ui;color:#15253b}header{padding:16px;background:#15253b;color:white}button,input{padding:8px;margin:0 5px}main{display:grid;grid-template-columns:1fr 360px;height:calc(100vh - 82px)}#canvas{overflow:auto;background:#f3f5f8}aside{padding:18px;overflow:auto;border-left:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}svg{max-width:none}.selected>*:not(text):not(title){stroke:#e11d48;stroke-width:5}a{color:#0369a1}</style>
<header><strong>Liquid UTXO trace</strong> <input id="search" placeholder="Search address or txid"><button id="find">Find</button><button id="minus">−</button><button id="plus">+</button><button id="fit">Fit</button><label><input id="context" type="checkbox" checked>Context edges</label></header>
<main><div id="canvas">''' + svg + '''</div><aside><p id="notice"></p><p>Click a node to inspect full identifiers and evidence. Hover an edge for its outpoint and public quantity.</p><a id="link" target="_blank" rel="noopener noreferrer" hidden>Open explorer</a><pre id="details"></pre></aside></main>
<script type="application/json" id="graph-data">''' + encoded + '''</script><script>
const graph=JSON.parse(document.getElementById('graph-data').textContent),svg=document.querySelector('svg'),canvas=document.getElementById('canvas');
document.getElementById('notice').textContent=(graph.simulated?'SYNTHETIC DEMO. ':'')+graph.notice;
const original=svg.viewBox.baseVal.width;let scale=1;function zoom(f){scale=Math.max(.1,Math.min(4,f));svg.style.width=(original*scale)+'px';svg.style.height='auto'}
document.getElementById('plus').onclick=()=>zoom(scale*1.25);document.getElementById('minus').onclick=()=>zoom(scale/1.25);document.getElementById('fit').onclick=()=>zoom(canvas.clientWidth/original);
function select(node){document.querySelectorAll('.selected').forEach(x=>x.classList.remove('selected'));const el=[...document.querySelectorAll('.node')].find(x=>x.dataset.key===node.id);el.classList.add('selected');document.getElementById('details').textContent=JSON.stringify(node.details,null,2);const link=document.getElementById('link');link.hidden=!node.url;if(node.url)link.href=node.url;return el}
document.querySelectorAll('.node').forEach(el=>{el.onclick=()=>select(graph.nodes.find(n=>n.id===el.dataset.key));el.onkeydown=e=>{if(e.key==='Enter')el.onclick()}});
document.getElementById('find').onclick=()=>{const q=document.getElementById('search').value.trim().toLowerCase();const node=graph.nodes.find(n=>JSON.stringify(n.details).toLowerCase().includes(q));if(node&&q)select(node).scrollIntoView({block:'center',inline:'center'})};
document.getElementById('context').onchange=e=>document.querySelectorAll('.edge.context').forEach(x=>x.style.display=e.target.checked?'':'none');zoom(Math.min(1,canvas.clientWidth/original));
</script></html>'''


def export_run(store, state, destination, merge_addresses=False, offline_preview=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    save_json(destination / "trace.json", state)
    save_json(destination / "investigation.json", {
        "run_id": state["run_id"], "parent_run": state.get("parent_run"),
        "case_id": state["case_id"],
        "name": state.get("investigation", {}).get("name"),
        "miro_board": state.get("investigation", {}).get("miro_board"),
        "note": "Board selection recorded at trace time. Subsequent publication details are in the case's miro/reports directory.",
    })
    graph = build_graph(state, merge_addresses, state.get("graph_options", {}).get("include_fees", False))
    save_json(destination / "graph.json", graph)
    if offline_preview:
        svg = svg_graph(graph)
        (destination / "graph.svg").write_text(svg, encoding="utf-8")
        (destination / "graph.html").write_text(html_graph(graph, svg), encoding="utf-8")
    write_csv(destination / "nodes.csv", graph["nodes"],
        ["id", "kind", "label", "url", "color", "details"])
    write_csv(destination / "edges.csv", graph["edges"],
        ["id", "source", "target", "role", "outpoint", "label", "quantity", "details"])
    rows, events, inputs = [], [], []
    for txid, record in state["transactions"].items():
        tx = record["data"]
        for index, output in enumerate(tx["vout"]):
            key = f"{txid}:{index}"
            item = state["outputs"].get(key, {})
            rows.append({"outpoint": key, "txid": txid, "vout": index,
                         "depth": item.get("depth"), "trace_status": item.get("status", "context_only"),
                         "kind": output_kind(output), **public_fields(output),
                         "observation_id": record["observation_id"], "labels": match_labels(state["labels"], key, output)})
            if output_kind(output) != "spendable":
                events.append({"txid": txid, "index": index, "kind": output_kind(output), "data": output,
                               "observation_id": record["observation_id"]})
        for index, vin in enumerate(tx["vin"]):
            inputs.append({"txid": txid, "vin": index, "previous_txid": vin.get("txid"), "previous_vout": vin.get("vout"),
                           "is_pegin": vin.get("is_pegin"), "is_coinbase": vin.get("is_coinbase"),
                           **public_fields(vin.get("prevout") or {}), "observation_id": record["observation_id"]})
            for flag, kind in (("is_pegin", "pegin"), ("issuance", "issuance_or_reissuance")):
                if vin.get(flag):
                    events.append({"txid": txid, "index": index, "kind": kind, "data": vin,
                                   "observation_id": record["observation_id"]})
    write_csv(destination / "outputs.csv", rows, ["outpoint", "txid", "vout", "depth", "trace_status", "kind",
        "scriptpubkey_address", "scriptpubkey", "scriptpubkey_type", "value", "valuecommitment", "asset", "assetcommitment", "pegout", "labels", "observation_id"])
    write_csv(destination / "inputs.csv", inputs, ["txid", "vin", "previous_txid", "previous_vout", "is_pegin", "is_coinbase",
        "scriptpubkey_address", "scriptpubkey", "value", "valuecommitment", "asset", "assetcommitment", "observation_id"])
    write_csv(destination / "spends.csv", state["links"].values(), ["outpoint", "spending_txid", "vin", "hop", "relationship", "observation_id", "spending_tx_observation_id"])
    write_csv(destination / "events.csv", events, ["txid", "index", "kind", "data", "observation_id"])
    frontier = [item for item in state["outputs"].values() if item["status"] not in TERMINAL]
    save_json(destination / "frontier.json", frontier)
    write_csv(destination / "frontier.csv", frontier, ["outpoint", "depth", "status", "labels", "spend_observation_id", "observed_spend"])
    evidence = destination / "evidence"
    evidence.mkdir(exist_ok=True)
    metadata = []
    for observation in store.observations(state["observations"]):
        raw = observation.pop("body")
        filename = f"{observation['id']:08d}.response"
        (evidence / filename).write_bytes(raw)
        metadata.append({**observation, "file": "evidence/" + filename})
    save_json(destination / "evidence-index.json", metadata)
    save_json(destination / "labels.json", state["labels"])
    from .miro import make_plan
    save_json(destination / "miro-plan.json", make_plan(graph))
    (destination / "RUN.md").write_text(
        f"# Liquid trace run {state['run_id']}\n\n"
        f"Status: {state['status']}. Parent: {state['parent_run'] or 'none'}.\n\n"
        f"Started: {state['started_at']}. Finished: {state['finished_at']}.\n\n"
        f"Limits: `{json.dumps(state['limits'])}`\n\n"
        f"Statistics: `{json.dumps(state['stats'])}`\n\n"
        "Use miro-sync to add this run to your case's existing Miro graph. nodes.csv and edges.csv retain logical graph IDs. "
        "miro-plan.json is input to this program, not a native Miro import file. An offline preview is optional.\n\n"
        "The frontier lists branches that remain unresolved, including outputs unspent at observation time. "
        "A completed bounded run is not a completed investigation. API request counts include authentication and retries; they are not a count of billable credits.\n\n"
        "Each solid graph edge represents an observed input or output. Expanding all outputs establishes possible UTXO ancestry, "
        "not the amount, asset, owner, beneficiary, or allocation of stolen funds. Co-inputs are context. "
        "A peg-out request does not independently prove a Bitcoin payout or an Avalanche deposit.\n\n"
        "JSON response bytes, retrieval time, endpoint, HTTP status and SHA-256 are archived. These are API observations, "
        "not independently validated consensus proofs or raw wire transactions. Continuation retains prior observations; "
        "run a fresh trace with --tx-cache-seconds 0 to re-observe historical links after a suspected reorg.\n",
        encoding="utf-8")
    # Bundle-local checksums; not a signature or independent timestamp.
    files = sorted(p for p in destination.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (destination / "SHA256SUMS").write_text("".join(digest(p.read_bytes()) + "  " + str(p.relative_to(destination)) + "\n" for p in files), encoding="utf-8")
    return graph
