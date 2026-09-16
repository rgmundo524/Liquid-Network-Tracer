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
VERIFIED = 'membership_verified'


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


def _page_data(body, path, group_id):
    """Decode endpoint-specific collections, never arbitrary nested item data.

    /groups has a data array. /groups/items documents a group object in data,
    with ItemPagedResponse objects in that group's data array. Also accept the
    direct member-array form and older flat collections. A wrapper's identity
    must match the requested group before any member IDs are trusted.
    """
    data = body.get('data')
    metadata = [body]
    if path == '/groups/items' and isinstance(data, dict):
        if data.get('id') != group_id or data.get('type', 'group') != 'group':
            raise TraceError('Miro group-members response does not identify the requested group')
        metadata.append(data)
        data = data.get('data')
        # A single ItemPagedResponse may be returned directly or in an array.
        pages = [data] if isinstance(data, dict) else data
        if isinstance(pages, list) and pages and all(
                isinstance(page, dict) and 'id' not in page and 'data' in page for page in pages):
            data = []
            for page in pages:
                values = page['data']
                if not isinstance(values, list):
                    raise TraceError('Miro group-members response has an invalid nested item page')
                size = page.get('size')
                if size is not None and (type(size) is not int or size != len(values)):
                    raise TraceError('Miro group-members page size disagrees with its item list')
                metadata.append(page)
                data.extend(values)
    if not isinstance(data, list):
        collection = 'group-members' if path == '/groups/items' else 'group-inventory'
        raise TraceError('Miro ' + collection + ' response has an invalid collection envelope; '
                         'grouping cannot be verified')
    return data, metadata


def _page_metadata(metadata):
    """One continuation token across supported envelopes; never follow links."""
    cursors, totals = set(), set()
    for container in metadata:
        cursor = container.get('cursor')
        if cursor is not None and not isinstance(cursor, str):
            raise TraceError('Miro group pagination contains an invalid cursor type')
        if cursor:
            cursors.add(cursor)
        total = container.get('total')
        if total is not None:
            if type(total) is not int or total < 0:
                raise TraceError('Miro group pagination contains an invalid total')
            totals.add(total)
    if len(cursors) > 1 or len(totals) > 1:
        raise TraceError('Miro group pagination has conflicting envelope metadata; retry sync')
    return next(iter(cursors), None), next(iter(totals), None)


def _pages(requests, base, headers, path, params=None):
    cursor, seen, received, expected = None, set(), 0, None
    while True:
        query = {'limit': 50, **(params or {})}
        if cursor:
            query['cursor'] = cursor
        status, _, raw = requests.request('GET', base + path + '?' + urlencode(query), headers)
        if not 200 <= status < 300:
            raise TraceError(f'Miro group preflight returned HTTP {status}; grouping cannot be verified')
        data, metadata = _page_data(_json(raw), path, query.get('group_item_id'))
        cursor, total = _page_metadata(metadata)
        if cursor and (cursor in seen or not data):
            raise TraceError('Miro group pagination is incomplete or malformed; '
                             'repeated cursor or empty continuation page')
        # Group-inventory totals can count items rather than groups. Only member
        # collections can safely compare an advertised total with member count.
        received += len(data)
        if path == '/groups/items' and total is not None:
            if expected is not None and expected != total:
                raise TraceError('Miro group membership total changed between pages; retry sync')
            expected = total
        if expected is not None and (received > expected or (not cursor and received != expected)):
            raise TraceError('Miro group-members pagination is incomplete; advertised members '
                             'were not all returned with a usable continuation cursor')
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
        wrapper = group.get('data')
        # GroupResponseShort.data references Group, which itself wraps items
        # inside data. Support both that documented form and direct data.items.
        for _ in range(2):
            if members is not None or not isinstance(wrapper, dict):
                break
            members = wrapper.get('items')
            wrapper = wrapper.get('data')
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


def _members(requests, base, headers, group_id):
    """Read actual members, rather than trusting a successful creation response."""
    values = list(_pages(requests, base, headers, '/groups/items', {'group_item_id': group_id}))
    ids = [_item_id(value) for value in values]
    if any(not _id(value) for value in ids) or len(ids) != len(set(ids)):
        raise TraceError('Miro group verification returned invalid or duplicated members')
    return frozenset(ids)


def _validate_saved(state):
    for field in (GROUPS, PENDING):
        values = state.get(field, {})
        if not isinstance(values, dict):
            raise TraceError('Invalid address-count grouping journal; preserve the Miro mapping')
        for key, entry in values.items():
            if not isinstance(entry, dict):
                raise TraceError('Invalid address-count grouping journal entry')
            if VERIFIED in entry and entry[VERIFIED] is not True:
                raise TraceError('Invalid address-count membership verification marker')
            if 'response_id' in entry and not _id(entry['response_id']):
                raise TraceError('Invalid pending Miro group response ID')
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
        self.prepared_groups = {}

    def _refresh(self, requests, base, headers):
        self.groups = inventory(requests, base, headers)
        self.memberships = {}
        for group_id, members in self.groups.items():
            for member in members:
                self.memberships.setdefault(member, set()).add(group_id)

    def prepare(self, requests, base, headers):
        """Read complete live membership before any shape/connector mutations."""
        if not self.pairs and not self.state.get(PENDING):
            return
        self._refresh(requests, base, headers)
        self.prepared_groups = dict(self.groups)
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
        report = {'created': 0, 'reused': 0, 'preserved': 0, 'repaired': 0,
                  'verified': 0, 'total': len(self.pairs), 'status': 'complete', 'conflicts': []}
        if not self.pairs and not self.state.get(PENDING):
            return report
        # Shape/parent/frame updates have run since prepare(). Its inventory is
        # no longer evidence that a group still exists. Re-read before deciding
        # whether a pair can be reused or needs repair.
        self._refresh(requests, base, headers)
        # Recover successful writes even when the current plan no longer needs
        # that pair. The group and its objects are never automatically deleted.
        for key, entry in list(self.state.get(PENDING, {}).items()):
            group_id = self._exact(entry['members'])
            if group_id is None or _members(requests, base, headers, group_id) != frozenset(entry['members']):
                raise TraceError('Pending address-count group cannot be verified; preserve the mapping '
                                 'and reconcile with miro-resolve before retrying')
            saved = {name: entry[name] for name in ('host', 'label', 'members')}
            journal.commit(sets=[((GROUPS, key), {**saved, 'id': group_id, VERIFIED: True})],
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
                saved = {**record, 'id': group_id, VERIFIED: True}
                if self.state.get(GROUPS, {}).get(key) != saved:
                    journal.commit(sets=[((GROUPS, key), saved)])
                report['reused'] += 1
                report['verified'] += 1
                continue
            previous = self.state.get(GROUPS, {}).get(key)
            occupied = any(self.memberships.get(member) for member in members)
            # Keep a deliberate removal of a previously *verified* pair, and
            # preserve larger/different user groups. Older releases marked an ID
            # as success without a membership GET, so their missing records must
            # not permanently suppress repair. A pair lost during this sync is
            # also repaired if neither item has joined a different group.
            manually_removed = (previous is not None and previous.get(VERIFIED) is True
                                and previous['id'] not in self.prepared_groups)
            if occupied or manually_removed:
                report['preserved'] += 1
                report['status'] = 'incomplete'
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
            # Retain the returned ID as an unacknowledged intent before GET.
            # A 201 alone must never become proof that the pair moves together.
            journal.commit(sets=[((PENDING, key), {**record, 'response_id': group_id})])
            if _members(requests, base, headers, group_id) != frozenset(members):
                raise TraceError('Miro returned success but the address/count pair was not verified '
                                 'in the created group; grouping is incomplete. The pending intent '
                                 'is saved; retry sync to check live membership without replaying POST')
            journal.commit(sets=[((GROUPS, key), {**record, 'id': group_id, VERIFIED: True})],
                           deletes=[(PENDING, key)])
            self.groups[group_id] = frozenset(members)
            for member in members:
                self.memberships[member] = {group_id}
            report['created'] += 1
            report['verified'] += 1
            report['repaired'] += previous is not None
        if self.pairs:
            phase = 'grouping' if report['status'] == 'complete' else 'grouping_incomplete'
            progress.emit(phase, report['verified'], len(self.pairs),
                          'Address-count group membership checked')
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
