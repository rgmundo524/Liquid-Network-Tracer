"""Read-only verification of a user-supplied Boltz submarine swap ID.

API contract: BoltzExchange/boltz-backend swagger-spec.json at
4d131ef8562eea25ab687bcc75a17ce899110b66 (GET submarine transaction and status).
This is provider-reported association, not independent invoice settlement proof.
"""
import hashlib
import re
from .common import HEX64, StopRun, TraceError

BOLTZ_API = 'https://api.boltz.exchange/v2'


def validate_swap_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise TraceError('Boltz swap ID must contain 1 to 128 letters, digits, underscores or hyphens')
    return value


def transaction_id(raw):
    """Compute a Bitcoin txid, stripping witness data from a bounded wire tx."""
    if not isinstance(raw, str) or len(raw) > 8000000 or not re.fullmatch(r'(?:[0-9a-fA-F]{2})+', raw):
        raise TraceError('Invalid Boltz Bitcoin transaction hex')
    data, pos = bytes.fromhex(raw), 0

    def take(n):
        nonlocal pos
        if n > len(data) - pos:
            raise TraceError('Truncated Boltz Bitcoin transaction')
        value = data[pos:pos+n]
        pos += n
        return value

    def count():
        marker = take(1)[0]
        if marker < 253:
            return marker
        length = {253: 2, 254: 4, 255: 8}[marker]
        value = int.from_bytes(take(length), 'little')
        if value < {253:253, 254:65536, 255:4294967296}[marker] or value > 4000000:
            raise TraceError('Invalid Boltz transaction compact integer')
        return value

    version = take(4)
    witness = data[pos:pos+2] == b'\x00\x01'
    if witness:
        take(2)
    body_start = pos
    inputs = count()
    if not 1 <= inputs <= 100000:
        raise TraceError('Invalid Boltz transaction input count')
    for _ in range(inputs):
        take(36); take(count()); take(4)
    outputs = count()
    if not 1 <= outputs <= 100000:
        raise TraceError('Invalid Boltz transaction output count')
    for _ in range(outputs):
        take(8); take(count())
    body = data[body_start:pos]
    if witness:
        for _ in range(inputs):
            items = count()
            if items > 100000:
                raise TraceError('Oversized Boltz transaction witness')
            for _ in range(items):
                take(count())
    locktime = take(4)
    if pos != len(data):
        raise TraceError('Trailing Boltz transaction data')
    return hashlib.sha256(hashlib.sha256(version + body + locktime).digest()).digest()[::-1].hex()


def unavailable(swap_id, reason):
    return dict(status='unavailable', provider='Boltz', swap_id=swap_id, swap_type='submarine',
                transaction_id=None, provider_status=None, summary=reason[:1000], evidence=[])


def verify_boltz_swap(swap_id, query_txid, fetch):
    swap_id = validate_swap_id(swap_id)
    if not isinstance(query_txid, str) or not HEX64.fullmatch(query_txid):
        raise TraceError('Invalid Bitcoin transaction hash')
    data = fetch('/swap/submarine/' + swap_id + '/transaction')
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not HEX64.fullmatch(data['id']):
        raise TraceError('Boltz did not return a valid submarine swap funding transaction')
    identity = data['id'].lower()
    evidence = ['Boltz submarine swap transaction endpoint returned funding transaction ' + identity + '.']
    if data.get('hex') is not None:
        if transaction_id(data['hex']) != identity:
            raise TraceError('Boltz raw transaction does not match its reported hash')
        evidence.append('Returned raw Bitcoin transaction hashes to the reported funding transaction ID.')
    matched = identity == query_txid.lower()
    result = dict(status='verified' if matched else 'unmatched', provider='Boltz', swap_id=swap_id,
                  swap_type='submarine', transaction_id=identity, provider_status=None,
                  summary=('Boltz associates this exact Bitcoin transaction with the supplied submarine swap ID. '
                           'This is provider-reported association; Lightning invoice settlement is not independently verified.'
                           if matched else 'The supplied Boltz swap ID refers to a different funding transaction.'), evidence=evidence)
    try:
        state = fetch('/swap/' + swap_id)
        status = state.get('status') if isinstance(state, dict) else None
        if not isinstance(status, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', status):
            raise TraceError('Invalid Boltz swap status')
        result['provider_status'] = status
        result['evidence'].append('Provider-reported status: ' + status + '. This is not independent settlement proof.')
    except (TraceError, StopRun):
        result['evidence'].append('Optional provider status unavailable; the transaction association was checked separately.')
    return result
