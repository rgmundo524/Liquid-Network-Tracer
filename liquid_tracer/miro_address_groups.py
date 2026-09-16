"""Native Miro groups pairing an address with its generated transaction count.

Groups are presentation relationships, never transaction graph nodes. We only
create pairs of our mapped items; user groups are never replaced or ungrouped.
A lost POST response is reconciled by exact live membership, never blind replay.

REST: https://developers.miro.com/reference/creategroup
      https://developers.miro.com/reference/get-all-groups
      https://developers.miro.com/reference/getitemsbygroupid
"""
import copy
import json
from urllib.parse import urlencode

from .common import TraceError, digest
from .miro_requests import MiroRequestNotSent
from .presentation_items import proof, validate_items

GROUPS = 'address_count_groups'
PENDING = 'pending_address_count_groups'
PREFIX = 'group:address_count:'


def _id(value):
    return (isinstance(value, str) and bool(value) and len(value) <= 200
            and not any(c.isspace() or c in '/?#' for c in value))


def _item_id(value):
    return value.get('id') if isinstance(value, dict) else value


def _json(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError):
        raise TraceError('Miro group response is malformed; no group outcome was assumed') from None


def _pages(requests, base, headers, path, params=None):
    cursor, seen = None, set()
    while True:
        query = {'limit': 50, **(params or {})}
        if cursor:
            query['cursor'] = cursor
        status, _, raw = requests.request('GET', base + path + '?' + urlencode(query), headers)
        if not 200 <= status < 300:
            raise TraceError(f'Miro group preflight returned HTTP {status}; grouping cannot be verified')
        body = _json(raw)
        data, cursor = body.get('data'), body.get('cursor')
        if (not isinstance(data, list) or (cursor is not None and not isinstance(cursor, str))
                or (cursor and (cursor in seen or not data))):
            raise TraceError('Miro group pagination is incomplete or malformed; grouping cannot be verified')
        # Never follow response links to another endpoint or host.
        if cursor:
            seen.add(cursor)
        yield from data
        if not cursor:
            break


def inventory(requests, base, headers):
    groups = {}
    for group in _pages(requests, base, headers, '/groups'):
        if not isinstance(group, dict) or not _id(group.get('id')):
            raise TraceError('Miro group inventory contains an invalid group ID')
        group_id = group['id']
        # REST group data is an array of item IDs. Generic summaries may omit
        # membership; use the documented paginated member endpoint in that case.
        members = group.get('items')
        if members is None and isinstance(group.get('data'), dict):
            members = group['data'].get('items')
        if members is None:
            members = list(_pages(requests, base, headers, '/groups/items', {'group_item_id': group_id}))
        if not isinstance(members, list):
            raise TraceError('Miro group inventory has invalid membership')
        ids = [_item_id(member) for member in members]
        if any(not _id(value) for value in ids) or len(ids) != len(set(ids)):
            raise TraceError('Miro group inventory has invalid or duplicated members')
        members = frozenset(ids)
        if group_id in groups and groups[group_id] != members:
            raise TraceError('Miro group membership changed between pages; retry sync')
        groups[group_id] = members
    return groups


def _validate_saved(state):
    for field in (GROUPS, PENDING):
        values = state.get(field, {})
        if not isinstance(values, dict):
            raise TraceError('Invalid address-count grouping journal; preserve the Miro mapping')
        for key, entry in values.items():
            if not isinstance(entry, dict):
                raise TraceError('Invalid address-count grouping journal entry')
            host, label, members = entry.get('host'), entry.get('label'), entry.get('members')
            if (not isinstance(host, str) or not host or not isinstance(label, str)
                    or label != proof('address_count', host)['key']
                    or key != PREFIX + digest(host.encode())
                    or not isinstance(members, list) or len(members) != 2
                    or any(not _id(value) for value in members) or len(set(members)) != 2
                    or (field == GROUPS and not _id(entry.get('id')))):
                raise TraceError('Invalid address-count grouping identity; preserve the Miro mapping')
            if field == PENDING and (members != [_item_id(state['items'].get(host)),
                                                _item_id(state['items'].get(label))]):
                raise TraceError('Pending address-count group no longer matches its mapped items')


class AddressCountGroups:
    def __init__(self, plan, state):
        self.state = state
        self.pairs = {PREFIX + digest(proof['host'].encode()): (proof['host'], key)
                      for key, proof in validate_items(plan).items() if proof['kind'] == 'address_count'}
        _validate_saved(state)
        self.groups = {}
        self.memberships = {}

    def prepare(self, requests, base, headers):
        """Read complete live membership before any shape/connector mutations."""
        if not self.pairs and not self.state.get(PENDING):
            return
        self.groups = inventory(requests, base, headers)
        for group_id, members in self.groups.items():
            for member in members:
                self.memberships.setdefault(member, set()).add(group_id)
        for key, entry in self.state.get(PENDING, {}).items():
            found = self._exact(entry['members'])
            if found is None:
                raise TraceError('Prior Miro group POST outcome is uncertain for ' + key +
                    '; inspect the board and use miro-resolve --key with the group ID or --absent before retrying')

    def _exact(self, members):
        memberships = [self.memberships.get(member, set()) for member in members]
        if memberships[0] == memberships[1] and len(memberships[0]) == 1:
            group_id = next(iter(memberships[0]))
            if self.groups[group_id] == frozenset(members):
                return group_id
        return None

    def apply(self, requests, base, headers, journal, progress):
        report = {'created': 0, 'reused': 0, 'preserved': 0, 'conflicts': []}
        if not self.pairs and not self.state.get(PENDING):
            return report
        # Recover successful writes even when the current plan no longer needs
        # that pair. The group and its objects are never automatically deleted.
        for key, entry in list(self.state.get(PENDING, {}).items()):
            journal.commit(sets=[((GROUPS, key), {**entry, 'id': self._exact(entry['members'])})],
                           deletes=[(PENDING, key)])
        mapped_ids = {_item_id(value) for value in self.state['items'].values()}
        for index, (key, (host, label)) in enumerate(sorted(self.pairs.items()), 1):
            progress.emit('grouping', index - 1, len(self.pairs), 'Grouping addresses with transaction counts')
            members = [_item_id(self.state['items'].get(item)) for item in (host, label)]
            if any(not _id(value) for value in members) or members[0] == members[1]:
                raise TraceError('Cannot group an address until both mapped shapes exist')
            record = {'host': host, 'label': label, 'members': members}
            group_id = self._exact(members)
            if group_id is not None:
                saved = {**record, 'id': group_id}
                if self.state.get(GROUPS, {}).get(key) != saved:
                    journal.commit(sets=[((GROUPS, key), saved)])
                report['reused'] += 1
                continue
            previous = self.state.get(GROUPS, {}).get(key)
            if any(self.memberships.get(member) for member in members) or previous is not None:
                # Preserve larger user groups, regrouped items and intentional
                # ungrouping of a pair we had previously acknowledged.
                report['preserved'] += 1
                report['conflicts'].append({'key': key, 'reason': 'Existing or manually changed grouping preserved'})
                continue
            journal.commit(sets=[((PENDING, key), record)])
            try:
                status, _, raw = requests.request('POST', base + '/groups', headers,
                                                  {'data': {'items': members}})
            except MiroRequestNotSent:
                journal.commit(deletes=[(PENDING, key)])
                raise
            except Exception as error:
                raise TraceError('Miro group request outcome is uncertain; progress is saved. '
                                 'Retry sync to check live membership before any new group POST') from error
            if not 200 <= status < 300:
                if 400 <= status < 500 and status != 408:
                    journal.commit(deletes=[(PENDING, key)])
                raise TraceError(f'Miro grouping returned HTTP {status}; acknowledged graph items are saved')
            body = _json(raw)
            group_id = body.get('id')
            if (not _id(group_id) or group_id in self.groups
                    or group_id in mapped_ids):
                raise TraceError('Miro accepted grouping but returned an invalid or reused group ID; '
                                 'retry sync to reconcile live membership')
            journal.commit(sets=[((GROUPS, key), {**record, 'id': group_id})], deletes=[(PENDING, key)])
            self.groups[group_id] = frozenset(members)
            for member in members:
                self.memberships[member] = {group_id}
            report['created'] += 1
        if self.pairs:
            progress.emit('grouping', len(self.pairs), len(self.pairs), 'Address-count grouping complete')
        return report


def resolve_pending(state, journal, key, item_id=None):
    """Explicit reconciliation after the analyst inspects an uncertain group."""
    _validate_saved(state)
    entry = state.get(PENDING, {}).get(key)
    if entry is None:
        raise TraceError('That key is not a pending address-count group')
    sets = []
    if item_id is not None:
        occupied = {_item_id(value) for value in state['items'].values()}
        occupied.update(value['id'] for value in state.get(GROUPS, {}).values())
        if not _id(item_id) or item_id in occupied:
            raise TraceError('Provide an unmapped Miro group ID, not an item ID or URL')
        sets = [((GROUPS, key), {**copy.deepcopy(entry), 'id': item_id})]
    journal.commit(sets=sets, deletes=[(PENDING, key)])
