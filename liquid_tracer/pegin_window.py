"""Bounded historical peg-in discovery using Esplora's filtered asset history.

Dates select a padded range of heights using monotone median block time (MTP).
Coverage is always a claim about those heights, never all possible timestamps.
"""
from datetime import datetime, timezone
import json

from .common import HEX64, LBTC, StopRun, TraceError
from .progress import report_progress

PADDING_SECONDS = 2 * 3600
BOUNDARY_NOTE = (
    "Liquid block bounds are selected using median block time with two hours of padding. "
    "Individual block timestamps can differ from median time. Complete coverage means the "
    "selected block range was searched, not that every possible claim date was excluded.")


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def gaps(low, high, ranges):
    position = low
    missing = []
    for start, end in sorted(ranges):
        if end < position:
            continue
        if start > high:
            break
        if start > position:
            missing.append((position, min(high, start - 1)))
        position = max(position, end + 1)
    if position <= high:
        missing.append((position, high))
    return missing


class WindowDiscovery:
    def __init__(self, api, db, state, history, fetch, inputs, status, progress, max_pages,
                 bitcoin_status, start_hours, end_hours):
        self.api, self.db, self.state = api, db, state
        self.history, self.fetch, self.inputs, self.status = history, fetch, inputs, status
        self.progress, self.max_pages = progress, max_pages
        self.pages = self.reused = self.entries = 0
        self.ready = self.finalized = False
        timestamp = bitcoin_status.get('block_time')
        if not bitcoin_status.get('confirmed') or type(timestamp) is not int or timestamp < 0:
            raise TraceError('A confirmed Bitcoin block timestamp is required for a date-window search; '
                             'provide a known Liquid claim to verify an unconfirmed deposit')
        start = max(0, int(timestamp + start_hours * 3600))
        end = int(timestamp + end_hours * 3600)
        if end <= start:
            raise TraceError('The search window must end after its start and after the Unix epoch')
        self.window = dict(start_time=start, end_time=end, start_utc=utc(start), end_utc=utc(end),
                           start_height=None, end_height=None, complete=False,
                           covered_ranges=[], remaining_ranges=[], boundary_note=BOUNDARY_NOTE,
                           boundary_padding_seconds=PADDING_SECONDS, searched_mempool=False)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS window_meta (id INTEGER PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS window_headers (height INTEGER PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS window_coverage (first INTEGER NOT NULL, last INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS window_cursors (height INTEGER PRIMARY KEY, txid TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS window_cursor_seen (height INTEGER NOT NULL, txid TEXT NOT NULL, PRIMARY KEY(height,txid));
            CREATE TABLE IF NOT EXISTS window_blocks (hash TEXT PRIMARY KEY, next_index INTEGER NOT NULL, anchor TEXT);
            CREATE TABLE IF NOT EXISTS window_pages (endpoint TEXT PRIMARY KEY, observation_id INTEGER NOT NULL);
        ''')

    def _headers(self, height=None, *, fresh=False):
        self.api.budget.check()
        if height is not None and not fresh:
            row = self.db.execute('SELECT document FROM window_headers WHERE height=?', (height,)).fetchone()
            if row:
                return [json.loads(row[0])]
        report_progress(self.progress, 'pegin_dates')
        data, _ = self.fetch(self.api, '/blocks' + (('/' + str(height)) if height is not None else ''))
        if not isinstance(data, list) or not data or len(data) > 10:
            raise TraceError('Liquid API returned an invalid block header page')
        previous = None
        for block in data:
            if (not isinstance(block, dict) or not HEX64.fullmatch(str(block.get('id', '')))
                    or any(type(block.get(key)) is not int or block[key] < 0
                           for key in ('height', 'timestamp', 'mediantime', 'tx_count'))
                    or block['tx_count'] < 1):
                raise TraceError('Liquid block header is missing valid height, time, or transaction count')
            if previous and (block['height'] != previous['height'] - 1
                             or block['mediantime'] > previous['mediantime']):
                raise TraceError('Liquid block headers have inconsistent height or median time order')
            previous = block
        if height is not None and data[0]['height'] != height:
            raise TraceError('Liquid API returned the wrong block height')
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO window_headers VALUES(?,?)',
                                [(b['height'], json.dumps(b)) for b in data])
        return data

    def _first_median(self, timestamp, tip):
        low, high = 0, tip['height']
        if tip['mediantime'] < timestamp:
            return high
        while low < high:
            middle = (low + high) // 2
            if self._headers(middle)[0]['mediantime'] < timestamp:
                low = middle + 1
            else:
                high = middle
        return low

    def prepare(self):
        if self.ready:
            return
        tip = self._headers(fresh=True)[0]
        old = self.db.execute('SELECT document FROM window_meta WHERE id=1').fetchone()
        if old:
            old = json.loads(old[0])
            if (old['height'] > tip['height'] or
                    self._headers(old['height'], fresh=True)[0]['id'] != old['id']):
                # Height-based proofs/cursors are no longer trustworthy. Immutable
                # block-hash responses remain reusable if encountered on this branch.
                with self.db:
                    for table in ('window_headers', 'window_coverage', 'window_cursors', 'window_cursor_seen', 'window_blocks'):
                        self.db.execute('DELETE FROM ' + table)
                self.window['cache_invalidated_by_reorg'] = True
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO window_meta VALUES(1,?)', (json.dumps(tip),))
        low = max(0, self._first_median(max(0, self.window['start_time'] - PADDING_SECONDS), tip) - 11)
        high = self._first_median(self.window['end_time'] + PADDING_SECONDS, tip)
        self.window.update(start_height=low, end_height=high,
                           snapshot_height=tip['height'], snapshot_hash=tip['id'],
                           end_not_yet_covered_by_chain=tip['mediantime'] < self.window['end_time'])
        self.ready = True
        self.coverage()

    def coverage(self):
        if self.ready:
            low, high = self.window['start_height'], self.window['end_height']
            ranges = [(max(low, a), min(high, b)) for a, b in
                      self.db.execute('SELECT first,last FROM window_coverage ORDER BY first')
                      if a <= high and b >= low]
            missing = gaps(low, high, ranges)
            self.window.update(covered_ranges=[dict(start_height=a, end_height=b) for a, b in ranges],
                               remaining_ranges=[dict(start_height=a, end_height=b) for a, b in missing],
                               complete=self.finalized and not missing and not self.window['end_not_yet_covered_by_chain'])
        return dict(self.window, pages_this_task=self.pages, reused_block_pages=self.reused,
                    entries_this_task=self.entries)

    def _cover(self, low, high):
        if low > high:
            return
        rows = self.db.execute('SELECT first,last FROM window_coverage WHERE last>=? AND first<=?',
                               (low - 1, high + 1)).fetchall()
        if rows:
            low, high = min(low, *(a for a, _ in rows)), max(high, *(b for _, b in rows))
        self.db.execute('DELETE FROM window_coverage WHERE last>=? AND first<=?', (low - 1, high + 1))
        self.db.execute('INSERT INTO window_coverage VALUES(?,?)', (low, high))

    def _transactions(self, data, *, block=None, ceiling=None, cursor=None):
        if not isinstance(data, list) or len(data) > 25:
            raise TraceError('Liquid API returned an invalid transaction page')
        ids, rows, heights = [], [], []
        for item in data:
            rows.extend(self.inputs(item))
            status = self.status(item)
            if not status['confirmed'] or type(status.get('block_time')) is not int:
                raise TraceError('Window discovery requires confirmed transactions with block timestamps')
            height = status['block_height']
            if block and (height != block['height'] or status['block_hash'] != block['id']
                          or status['block_time'] != block['timestamp']):
                raise TraceError('Liquid transaction does not belong to the requested block')
            if ceiling is not None and (height > ceiling or heights and height > heights[-1]):
                raise TraceError('Liquid asset history is not ordered by descending block height')
            ids.append(item['txid'])
            heights.append(height)
        if len(ids) != len(set(ids)) or cursor in ids:
            raise TraceError('Liquid API repeated a transaction or did not advance its cursor')
        return ids, rows, heights

    def _count(self, count):
        self.entries += count
        self.state['pages'] += 1
        self.state['transactions'] += count
        self.db.execute('INSERT OR REPLACE INTO scan VALUES(1,?)', (json.dumps(self.state, sort_keys=True),))

    def _page(self, endpoint, *, immutable=False):
        self.api.budget.check()
        if immutable:
            row = self.db.execute('SELECT observation_id FROM window_pages WHERE endpoint=?', (endpoint,)).fetchone()
            if row:
                observation = next(iter(self.api.store.observations([row[0]])), None)
                if observation and observation['source'] == self.api.base:
                    self.reused += 1
                    return json.loads(observation['body']), row[0]
        if self.pages >= self.max_pages:
            raise StopRun('page_limit')
        report_progress(self.progress, 'pegin_window', self.pages, self.max_pages)
        data, oid = self.fetch(self.api, endpoint)
        self.pages += 1
        return data, oid

    def step(self):
        """Commit one page or empty block, leaving resumable coverage on interruption."""
        self.prepare()
        self.api.budget.check()
        missing = self.coverage()['remaining_ranges']
        if not missing:
            if not self.finalized:
                status, _ = self.fetch(self.api, '/block/' + self.window['snapshot_hash'] + '/status')
                if not isinstance(status, dict) or type(status.get('in_best_chain')) is not bool:
                    raise TraceError('Liquid API returned an invalid block chain status')
                if not status['in_best_chain']:
                    with self.db:
                        for table in ('window_headers', 'window_coverage', 'window_cursors',
                                      'window_cursor_seen', 'window_blocks'):
                            self.db.execute('DELETE FROM ' + table)
                    self.window['cache_invalidated_by_reorg'] = True
                    raise StopRun('liquid_chain_changed')
                self.finalized = True
            return False
        low, high = missing[-1]['start_height'], missing[-1]['end_height']
        cursor = self.db.execute('SELECT height,txid FROM window_cursors WHERE height IN (?,?) '
                                 'ORDER BY height LIMIT 1', (high, high + 1)).fetchone()
        if cursor:
            data, _ = self._page(self.history + '/' + cursor[1])
            ids, rows, heights = self._transactions(data, ceiling=cursor[0], cursor=cursor[1])
            # Last block on a full page may have more entries on the next page.
            # A short page is not assumed to mean exhaustion; crossing the lower
            # boundary or an empty page establishes completion.
            with self.db:
                self.db.executemany('INSERT OR REPLACE INTO claims VALUES(?,?,?,?)', rows)
                self._cover(max(low, heights[-1] + 1) if heights else low, high)
                old_cursor = self.db.execute('SELECT txid FROM window_cursors WHERE height=?', (heights[-1],)).fetchone() if ids else None
                # Rejoining a partially searched block must preserve its saved
                # continuation. Starting again at this page's earlier member
                # would replay old cursors and appear to be a cycle.
                if ids and not (old_cursor and heights[-1] < cursor[0]):
                    previous = self.db.execute('SELECT 1 FROM window_cursor_seen WHERE height=? AND txid=?', (heights[-1], ids[-1])).fetchone()
                    if previous and heights[-1] == cursor[0]:
                        raise TraceError('Liquid history cursor stalled')
                    self.db.execute('INSERT OR REPLACE INTO window_cursors VALUES(?,?)', (heights[-1], ids[-1]))
                    self.db.execute('INSERT OR IGNORE INTO window_cursor_seen VALUES(?,?)', (heights[-1], ids[-1]))
                self._count(len(data))
            return True
        block = self._headers(high)[0]
        if block['tx_count'] == 1:
            # A valid block's sole transaction is its coinbase, which cannot claim a peg-in.
            with self.db:
                self._cover(high, high)
            return True
        row = self.db.execute('SELECT next_index,anchor FROM window_blocks WHERE hash=?', (block['id'],)).fetchone()
        offset, anchor = row if row else (0, None)
        endpoint = '/block/' + block['id'] + '/txs' + (('/' + str(offset)) if offset else '')
        data, oid = self._page(endpoint, immutable=True)
        ids, rows, _ = self._transactions(data, block=block)
        if len(data) != min(25, block['tx_count'] - offset):
            raise TraceError('Liquid block transaction page is incomplete')
        for item in data:
            if (any(vin.get('is_pegin') is True for vin in item['vin']) or
                    any(out.get('pegout') and out.get('asset') == LBTC for out in item.get('vout', []))):
                anchor = anchor or item['txid']
        offset += len(data)
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO claims VALUES(?,?,?,?)', rows)
            self.db.execute('INSERT OR REPLACE INTO window_pages VALUES(?,?)', (endpoint, oid))
            self.db.execute('INSERT OR REPLACE INTO window_blocks VALUES(?,?,?)', (block['id'], offset, anchor))
            if offset == block['tx_count']:
                self._cover(high, high)
                if anchor:
                    self.db.execute('INSERT OR REPLACE INTO window_cursors VALUES(?,?)', (high, anchor))
                    self.db.execute('INSERT OR IGNORE INTO window_cursor_seen VALUES(?,?)', (high, anchor))
            self._count(len(data))
        return True
