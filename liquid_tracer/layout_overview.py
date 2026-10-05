"""Bounded offline navigation views of a complete saved drawing.

Sections are navigation containers, never ownership claims or altered evidence.
Every display object has one section; cross-section connections are explicit
links in both sections. Canonical inputs hidden by display bundles stay listed.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import stat

from .common import TraceError

MIN_NODES = 2000
MIN_EDGES = 5000
SECTION_NODES = 128
ROWS_PER_PAGE = 200
SECTIONS_PER_PAGE = 60
MAX_PAGES = 200000
INDEX_BYTES = 16 * 1024 * 1024
NAVIGATION_NAME = re.compile(r'(?:overview-[0-9]{6}|section-[0-9]{6}-[0-9]{6})\.html\Z')


def enabled(nodes, edges):
    return len(nodes) >= MIN_NODES or len(edges) >= MIN_EDGES


def _ordinary(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError('Preview navigation cannot contain symbolic links')
    return path


def navigation_files(directory):
    """Read only the small existing details index, never the graph or plan."""
    path = _ordinary(Path(directory) / 'details.json')
    if not path.is_file():
        return frozenset()
    if path.stat().st_size > INDEX_BYTES:
        # Legacy printable atlases may have a large index and no navigation.
        return frozenset()
    try:
        info = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, RecursionError) as error:
        raise TraceError('Invalid preview navigation index') from error
    navigation = info.get('navigation') if isinstance(info, dict) else None
    if navigation is None:
        return frozenset()
    if (not isinstance(navigation, dict) or type(navigation.get('version')) is not int
            or navigation['version'] != 1 or not isinstance(navigation.get('sections'), list)
            or not 0 < len(navigation['sections']) <= MAX_PAGES):
        raise TraceError('Invalid preview navigation index')
    files = set()
    for number, section in enumerate(navigation['sections'], 1):
        count = section.get('pages') if isinstance(section, dict) else None
        if type(count) is not int or not 1 <= count <= MAX_PAGES or len(files) + count > MAX_PAGES:
            raise TraceError('Invalid preview navigation page count')
        files.update(_section_file(number, page) for page in range(1, count + 1))
    count = (len(navigation['sections']) + SECTIONS_PER_PAGE - 1) // SECTIONS_PER_PAGE
    files.update(_overview_file(page) for page in range(2, count + 1))
    if len(files) > MAX_PAGES:
        raise TraceError('Preview navigation exceeds its page limit')
    return frozenset(files)


def verified_navigation_file(directory, name):
    """Verify just viewed HTML and its index; this never approves publication."""
    directory = _ordinary(directory)
    files = navigation_files(directory)
    if not files or name not in files | {'graph.html', 'details.html', 'details.json'}:
        return None
    manifest = _ordinary(directory / 'SHA256SUMS')
    if not manifest.is_file() or manifest.stat().st_size > 32 * 1024 * 1024:
        raise TraceError('Saved preview is unfinished or has an invalid manifest')
    wanted, found = {name, 'details.json'}, {}
    with manifest.open(encoding='utf-8') as stream:
        for line in stream:
            parts = line.rstrip('\n').split('  ', 1)
            if len(parts) == 2 and parts[1] in wanted:
                if parts[1] in found or not re.fullmatch('[0-9a-f]{64}', parts[0]):
                    raise TraceError('Invalid preview navigation checksum')
                found[parts[1]] = parts[0]
    if set(found) != wanted:
        raise TraceError('Saved preview navigation is incomplete')
    for item, expected in found.items():
        checksum = hashlib.sha256()
        path = _ordinary(directory / item)
        if not stat.S_ISREG(path.stat().st_mode):
            raise TraceError('Preview navigation requires ordinary files')
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(chunk)
        if checksum.hexdigest() != expected:
            raise TraceError('Saved preview navigation checksum mismatch')
    return _ordinary(directory / name)


def _section_file(number, page=1):
    return f'section-{number:06d}-{page:06d}.html'


def _overview_file(page):
    return 'graph.html' if page == 1 else f'overview-{page:06d}.html'


def _groups(graph, nodes):
    by_id = {node['id']: node for node in nodes}
    stored = graph.get('layout', {}).get('section_geometry', {}).get('sections', [])
    groups, assigned = [], set()
    # Geometry metadata is only a hint: never let stale/subset metadata hide an
    # object or assign a shared address to multiple navigation sections.
    for section in stored if isinstance(stored, list) else []:
        keys = section.get('node_ids', []) if isinstance(section, dict) else []
        keys = sorted({key for key in keys if isinstance(key, str) and key in by_id} - assigned)
        for start in range(0, len(keys), SECTION_NODES):
            groups.append(keys[start:start + SECTION_NODES])
        assigned.update(keys)
    remaining = sorted((key for key in by_id if key not in assigned),
                       key=lambda key: (by_id[key]['x'], by_id[key]['y'], key))
    groups.extend(remaining[start:start + SECTION_NODES] for start in range(0, len(remaining), SECTION_NODES))
    return sorted(groups, key=lambda keys: (min(by_id[key]['x'] for key in keys),
                                            min(by_id[key]['y'] for key in keys), min(keys)))


def _shell(title, body):
    from .layout_preview import _escape
    from .legend import LEGEND_CSS
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>''' + _escape(title) + '''</title><style>
body{margin:0;color:#172033;background:#f5f6f8;font:15px system-ui,sans-serif}header,main{padding:20px 28px}header{background:white;border-bottom:1px solid #d5dbe3}h1{font-size:24px;margin:0 0 8px}h2{font-size:19px}a{color:#155e75}p{line-height:1.5}nav{display:flex;gap:20px;margin:16px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:white;padding:18px;border:1px solid #cbd5e1;border-radius:8px;overflow-wrap:anywhere}.muted{color:#475569}.drawing{overflow:auto;background:white}.drawing svg{display:block;max-width:none}.fit svg{width:100%;height:auto}table{width:100%;border-collapse:collapse;background:white}th,td{padding:9px;border:1px solid #d5dbe3;text-align:left;overflow-wrap:anywhere;vertical-align:top}code{overflow-wrap:anywhere}summary{cursor:pointer}button{font:inherit}@media print{nav{display:none}.drawing{overflow:visible}}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
''' + LEGEND_CSS + '</style></head><body>' + body + '</body></html>\n'


def export_overview(graph, nodes, edges, directory):
    """Write bounded static pages; no scripts, network, new layout or full DOM."""
    from .layout_preview import _escape, _svg, layout_notice
    from .attribution_presentation import register_html
    from .legend import legend_html
    from .context_connectors import evidence_graph
    directory = Path(directory)
    groups = _groups(graph, nodes)
    owner = {key: number for number, keys in enumerate(groups, 1) for key in keys}
    by_id = {node['id']: node for node in nodes}
    canonical = {edge['id']: edge for edge in evidence_graph(graph)['edges']}
    internal, records, neighbors = defaultdict(list), defaultdict(list), defaultdict(set)
    for edge in edges:
        a, b = owner[edge['source']], owner[edge['target']]
        if a == b:
            internal[a].append(edge)
        else:
            neighbors[a].add(b); neighbors[b].add(a)
        members = edge.get('details', {}).get('context_summary', {}).get('member_edge_ids') or [edge['id']]
        for key in members:
            original = canonical.get(key, edge)
            row = (edge, original, a, b)
            records[a].append(row)
            if a != b:
                records[b].append(row)
    entries = []
    overview_count = (len(groups) + SECTIONS_PER_PAGE - 1) // SECTIONS_PER_PAGE
    page_total = max(0, overview_count - 1)
    for number, keys in enumerate(groups, 1):
        count = max(1, (len(records[number]) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
        page_total += count
        if page_total > MAX_PAGES:
            raise TraceError('Preview navigation exceeds its page limit')
        entries.append({'number': number, 'node_count': len(keys), 'connection_count': len(records[number]),
                        'pages': count, 'neighbor_count': len(neighbors[number])})
    index = {'schema_version': 1, 'page_count': 0, 'activities': [], 'edge_pages': {},
             'unavailable_reason': 'Large drawing: use the bounded section browser or download the complete SVG.',
             'navigation': {'version': 1, 'sections': entries, 'node_count': len(nodes),
                            'display_edge_count': len(edges), 'rows_per_page': ROWS_PER_PAGE}}
    if len(json.dumps(index, indent=2, ensure_ascii=False).encode('utf-8')) + 1 > INDEX_BYTES:
        raise TraceError('Preview navigation index exceeds its size limit')
    common = ('<p><a href="graph.html">Section overview</a> · <a href="graph.svg" target="_blank" rel="noopener noreferrer">Open complete SVG</a> · '
              '<a href="graph.json" download>Complete graph data</a> · <a href="layout-report.json" download>Layout report</a></p>')
    note = ('<p class="muted">Sections organize this drawing for navigation; they do not identify owners. '
            'Every original object and connection remains in the complete graph and exports. '
            'Connections leaving a section are listed with links to their destination sections.</p>')
    # Compute scope/role notes once, without a graph-wide address register or
    # name palette on every page. Attribution evidence is local to its section
    # below, so large drawings remain bounded while preserving review context.
    interpretation = ('<details><summary>Graph legend and interpretation</summary><p>'
                      + _escape(graph.get('notice', '')) + '</p><p>' + _escape(layout_notice(graph))
                      + '</p>' + legend_html({**graph, 'nodes': []}) + '</details>')
    note += interpretation
    first_document = ''
    for page in range(1, overview_count + 1):
        selected = entries[(page - 1) * SECTIONS_PER_PAGE:page * SECTIONS_PER_PAGE]
        body = f'<header><h1>Trace section overview</h1><p>{len(nodes):,} objects · {len(edges):,} drawn connections · {len(groups):,} sections</p>' + common + note + '</header><main>'
        body += _pager(page, overview_count, _overview_file)
        body += '<div class="cards">'
        for entry in selected:
            number = entry['number']; keys = groups[number - 1]
            title = next((by_id[key].get('label', key) for key in keys if by_id[key]['kind'] == 'transaction'), by_id[keys[0]].get('label', keys[0]))
            body += (f'<article class="card"><h2><a href="{_section_file(number)}">Section {number}</a></h2>'
                     f'<p>{entry["node_count"]} objects · {entry["connection_count"]} input/output records · '
                     f'{entry["neighbor_count"]} neighboring sections</p><p>{_escape(str(title)[:240])}</p></article>')
        body += '</div>' + _pager(page, overview_count, _overview_file) + '</main>'
        document = _shell('Trace section overview', body)
        if page == 1:
            first_document = document
        else:
            (directory / _overview_file(page)).write_text(document, encoding='utf-8')
    for entry, keys in zip(entries, groups):
        number, pages = entry['number'], entry['pages']
        selected_nodes = [by_id[key] for key in keys]
        for page in range(1, pages + 1):
            batch = records[number][(page - 1) * ROWS_PER_PAGE:page * ROWS_PER_PAGE]
            body = f'<header><h1>Section {number}</h1><p>{len(keys)} objects · {len(records[number])} input/output records</p>' + common + note + '</header><main>'
            body += _pager(page, pages, lambda p: _section_file(number, p))
            if page == 1:
                # Bound SVG work even for high-degree sections. All remaining
                # connections are present in paginated exact records below.
                shown = internal[number][:ROWS_PER_PAGE]
                body += '<p>Local objects and up to 200 internal connections in their saved positions. Cross-section connections are in the table below.</p>'
                body += '<details open><summary>Fit section to window</summary><div class="drawing fit">' + _svg(graph, selected_nodes, shown, banner=False).decode() + '</div></details>'
                body += '<details><summary>Object identities</summary><ul>'
                body += ''.join('<li><code>' + _escape(key) + '</code><br>' + _escape(by_id[key].get('label', '')) + '</li>' for key in keys)
                body += '</ul></details>'
                body += register_html({'nodes': selected_nodes})
            else:
                body += f'<p><a href="{_section_file(number)}">Return to this section’s drawing</a></p>'
            body += '<h2>Exact input/output records</h2><p>Bundled inputs are expanded here. Amounts are individual recorded values, never inferred totals.</p>'
            body += '<table><thead><tr><th>Connection / input</th><th>From → To</th><th>Outpoint / value</th><th>Follow</th></tr></thead><tbody>'
            for edge, original, a, b in batch:
                links = ' · '.join(f'<a href="{_section_file(target)}">Section {target}</a>' for target in sorted({a, b} - {number})) or 'Within section'
                body += ('<tr><td><code>' + _escape(original['id']) + '</code><br>' + _escape(original.get('label', ''))
                         + '</td><td>' + _escape(original.get('original_source', original['source'])) + '<br>→ ' + _escape(original['target'])
                         + '</td><td>' + _escape(original.get('outpoint') or '') + '<br>' + _escape(original.get('quantity', ''))
                         + '</td><td>' + links + '</td></tr>')
            body += '</tbody></table>' + _pager(page, pages, lambda p: _section_file(number, p)) + '</main>'
            (directory / _section_file(number, page)).write_text(_shell(f'Trace · Section {number}', body), encoding='utf-8')
    details = _shell('Trace details', '<header><h1>Trace details</h1>' + common + note + '</header><main><p>This large drawing uses section pages. Open the overview, choose a section, and follow its connections to neighboring sections. Complete input records are available on each section’s numbered pages.</p></main>')
    return first_document, details, index


def _pager(page, count, filename):
    before = f'<a href="{filename(page - 1)}">Previous</a>' if page > 1 else ''
    after = f'<a href="{filename(page + 1)}">Next</a>' if page < count else ''
    return f'<nav aria-label="Pages">{before}<span>Page {page} of {count}</span>{after}</nav>'
