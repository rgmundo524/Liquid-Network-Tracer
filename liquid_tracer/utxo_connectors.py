"""Counted display connections with exact, independently retained UTXOs."""
from collections import Counter, defaultdict

from .common import TraceError
from .networks import is_primary, blockchain


def _index(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and str(int(value)) == value


def _quantity(members, direction, chain):
    from .export import graph_quantity
    outputs = [(edge['details'].get('vin', {}).get('prevout') or {}) if direction == 'input'
               else edge['details'] for edge in members]
    # No partial sums. Every member needs an explicit value in the same asset.
    # Integer arithmetic preserves all satoshis.
    if any(type(output.get('value')) is not int or output['value'] < 0 for output in outputs):
        return ''
    asset = None
    if chain == 'liquid':
        if any(not isinstance(output.get('asset'), str) or not output['asset'] for output in outputs):
            return ''
        assets = {output['asset'].lower() for output in outputs}
        if len(assets) != 1:
            return ''
        asset = next(iter(assets))
    return graph_quantity({'value': sum(output['value'] for output in outputs), 'asset': asset}, chain)


def derive(graph):
    from .context_connectors import _inventory, parallel_identity, _ERROR
    nodes, edges = _inventory(graph)
    candidates = defaultdict(list)
    for edge in edges.values():
        source, target = nodes[edge['source']], nodes[edge['target']]
        direction = ('input' if source['kind'] == 'address' and target['kind'] == 'transaction'
                     else 'output' if source['kind'] == 'transaction' and target['kind'] == 'address' else None)
        if not direction:
            continue
        address = source if direction == 'input' else target
        transaction = target if direction == 'input' else source
        if (not is_primary(address, graph) or not address.get('details', {}).get('address')
                or edge.get('original_source') is not None):
            continue
        txid = transaction['id'].removeprefix('tx:')
        outpoint = edge.get('outpoint')
        if not isinstance(outpoint, str) or not outpoint.rpartition(':')[0] or not _index(outpoint.rpartition(':')[2]):
            continue
        if direction == 'input':
            vin = edge.get('details', {}).get('vin', {})
            prefix = 'in:' + txid + ':'
            if (edge.get('role') not in ('context_input', 'traced_input')
                    or vin.get('is_pegin') or vin.get('is_coinbase')
                    or not edge['id'].startswith(prefix) or not _index(edge['id'][len(prefix):])
                    or type(vin.get('vout')) is not int or vin['vout'] < 0
                    or not isinstance(vin.get('txid'), str) or not vin['txid']
                    or outpoint != f"{vin['txid']}:{vin['vout']}"):
                continue
        elif (edge.get('role') not in ('context_output', 'candidate_output', 'seed_output')
              or edge['id'] != 'out:' + outpoint or outpoint.rpartition(':')[0] != txid):
            continue
        candidates[edge['source'], edge['target'], direction].append(edge)
    summaries = []
    for (source, target, direction), members in sorted(candidates.items()):
        if len(members) < 2:
            continue
        # Duplicate outpoints are not multiple UTXOs. Never double count one.
        if len({edge['outpoint'] for edge in members}) != len(members):
            continue
        identity = parallel_identity(source, target)
        if identity in nodes or identity in edges:
            raise TraceError(_ERROR)
        counts = dict(sorted(Counter(edge['role'] for edge in members).items()))
        priorities = ('seed_output', 'candidate_output', 'traced_input', 'context_output', 'context_input')
        role = next(role for role in priorities if role in counts)
        label = f'{len(members)} {direction}s'
        context = sum(count for role, count in counts.items() if role.startswith('context'))
        if context and context < len(members):
            label += f' · {len(members) - context} traced, {context} context'
        changes = sum(bool(edge.get('change_output')) for edge in members)
        if changes:
            label += f' · {changes} designated change'
        summary = {'id': identity, 'source': source, 'target': target, 'role': role,
            'outpoint': None, 'label': label, 'quantity': _quantity(members, direction, blockchain(graph)),
            'details': {'context_summary': {'version': 3, 'kind': 'parallel', 'direction': direction,
                'input_count': len(members), 'address_count': 1,
                'member_edge_ids': sorted(edge['id'] for edge in members), 'role_counts': counts}}}
        colors = {edge.get('color') for edge in members}
        if len(colors) == 1 and next(iter(colors)) is not None:
            summary['color'] = next(iter(colors))
        summaries.append(summary)
    return summaries
