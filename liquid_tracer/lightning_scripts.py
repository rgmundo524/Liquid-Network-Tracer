"""Conservative recognition of revealed Bitcoin swap-contract templates.

Template sources: BoltzExchange/boltz-core lib/swap/{SwapScript,
ReverseSwapScript,SwapTree,ReverseSwapTree}.ts; lightninglabs/loop swap/htlc.go.
A template match does not identify the service, swap direction, or settlement.
Commitments follow BIP141/BIP341. This is not a consensus script/signature VM.
"""
import hashlib
import re

from .common import TraceError

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
TEMPLATES = {'boltz_legacy_swap', 'boltz_legacy_reverse', 'loop_htlc_v2',
             'boltz_taproot_claim', 'boltz_taproot_reverse_claim', 'loop_htlc_v3_claim'}


def hash160(data):
    return hashlib.new('ripemd160', hashlib.sha256(data).digest()).digest()


def _hex(value, limit=10000):
    if not isinstance(value, str) or len(value) > limit * 2 or not re.fullmatch(r'(?:[0-9a-fA-F]{2})*', value):
        raise TraceError('Invalid or oversized Bitcoin script/witness hex')
    return bytes.fromhex(value)


def _lift(x):
    if not 0 <= x < P:
        raise TraceError('Invalid secp256k1 public key')
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise TraceError('Invalid secp256k1 public key')
    return x, y if y % 2 == 0 else P - y


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x, y = a
    u, v = b
    if x == u and (y != v or y == 0):
        return None
    slope = ((3*x*x) * pow(2*y, -1, P) if a == b else (v-y)*pow(u-x, -1, P)) % P
    z = (slope*slope-x-u) % P
    return z, (slope*(x-z)-y) % P


def _mul(n):
    point, result = G, None
    while n:
        if n & 1:
            result = _add(result, point)
        point = _add(point, point)
        n >>= 1
    return result


def _tagged(tag, value):
    digest = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(digest + digest + value).digest()


def _compact(value):
    return bytes([value]) if value < 253 else b'\xfd' + value.to_bytes(2, 'little')


def taproot_commitment(program, script, control):
    """Check a script/control-block commitment and parity, without signatures."""
    if len(control) < 33 or len(control) > 33 + 32*128 or (len(control)-33) % 32:
        raise TraceError('Invalid Taproot control block')
    internal = _lift(int.from_bytes(control[1:33], 'big'))
    root = _tagged('TapLeaf', bytes([control[0] & 0xfe]) + _compact(len(script)) + script)
    for offset in range(33, len(control), 32):
        sibling = control[offset:offset+32]
        root = _tagged('TapBranch', min(root, sibling) + max(root, sibling))
    tweak = int.from_bytes(_tagged('TapTweak', control[1:33] + root), 'big')
    if tweak >= N:
        raise TraceError('Invalid Taproot tweak')
    output = _add(internal, _mul(tweak))
    if output is None or output[0].to_bytes(32, 'big') != program or output[1] % 2 != control[0] & 1:
        raise TraceError('Taproot witness does not commit to the previous output')
    return control[0] & 0xfe


def _tokens(script):
    tokens, position = [], 0
    while position < len(script):
        op = script[position]
        position += 1
        if 1 <= op <= 75:
            if position + op > len(script):
                raise TraceError('Truncated script push')
            tokens.append(script[position:position+op])
            position += op
        elif op in (76, 77, 78):
            # Templates use only direct minimal pushes; other scripts are unsupported.
            return []
        else:
            tokens.append(op)
    return tokens


def _key(value, size):
    if not isinstance(value, bytes) or len(value) != size or size == 33 and value[0] not in (2, 3):
        return False
    try:
        _lift(int.from_bytes(value[-32:], 'big'))
        return True
    except TraceError:
        return False


def _timeout(value):
    if isinstance(value, int):
        return 0x51 <= value <= 0x60
    if not isinstance(value, bytes) or not 1 <= len(value) <= 5 or value[-1] & 0x80:
        return False
    if value[-1] == 0 and (len(value) == 1 or not value[-2] & 0x80):
        return False
    return 0 < int.from_bytes(value, 'little') <= 0xffffffff


def _template(script, taproot):
    t = _tokens(script)
    if not taproot:
        if (len(t) == 12 and t[0] == 0xa9 and isinstance(t[1], bytes) and len(t[1]) == 20
                and t[2:4] == [0x87, 0x63] and _key(t[4], 33) and t[5] == 0x67
                and _timeout(t[6]) and t[7:9] == [0xb1, 0x75] and _key(t[9], 33)
                and t[10:] == [0x68, 0xac]):
            return 'boltz_legacy_swap', t[1], None
        if (len(t) == 16 and t[:4] == [0x82, b'\x20', 0x87, 0x63]
                and t[4] == 0xa9 and isinstance(t[5], bytes) and len(t[5]) == 20
                and t[6] == 0x88 and _key(t[7], 33) and t[8:10] == [0x67, 0x75]
                and _timeout(t[10]) and t[11:13] == [0xb1, 0x75] and _key(t[13], 33)
                and t[14:] == [0x68, 0xac]):
            return 'boltz_legacy_reverse', t[5], None
        if (len(t) == 20 and _key(t[0], 33) and t[1:5] == [0xac, 0x64, 0x76, 0xa9]
                and isinstance(t[5], bytes) and len(t[5]) == 20 and t[6:8] == [0x88, 0xad]
                and _timeout(t[8]) and t[9:15] == [0xb1, 0x67, 0x82, b'\x20', 0x88, 0xa9]
                and isinstance(t[15], bytes) and len(t[15]) == 20 and t[16:] == [0x88, 0x51, 0xb2, 0x68]):
            return 'loop_htlc_v2', t[15], t[5]
    else:
        if (len(t) == 5 and t[0] == 0xa9 and isinstance(t[1], bytes) and len(t[1]) == 20
                and t[2] == 0x88 and _key(t[3], 32) and t[4] == 0xac):
            return 'boltz_taproot_claim', t[1], None
        if (len(t) == 8 and t[:4] == [0x82, b'\x20', 0x88, 0xa9]
                and isinstance(t[4], bytes) and len(t[4]) == 20 and t[5] == 0x88
                and _key(t[6], 32) and t[7] == 0xac):
            return 'boltz_taproot_reverse_claim', t[4], None
        if (len(t) == 10 and _key(t[0], 32) and t[1:6] == [0xad, 0x82, b'\x20', 0x88, 0xa9]
                and isinstance(t[6], bytes) and len(t[6]) == 20 and t[7:] == [0x88, 0x51, 0xb2]):
            return 'loop_htlc_v3_claim', t[6], None
    return None


def _branch(template, stack, digest, refund_key_hash):
    if template.startswith('loop_'):
        if len(stack) == 2 and stack[1] and len(stack[0]) == 32 and hash160(stack[0]) == digest:
            return 'claim'
        if (template == 'loop_htlc_v2' and len(stack) == 3 and stack[0] and stack[2] == b''
                and _key(stack[1], 33) and hash160(stack[1]) == refund_key_hash):
            return 'refund'
    elif len(stack) == 2 and stack[0]:
        preimage = stack[1]
        if hash160(preimage) == digest and (len(preimage) == 32 or template in ('boltz_legacy_swap', 'boltz_taproot_claim')):
            return 'claim'
        if template == 'boltz_legacy_swap' and hash160(preimage) != digest:
            return 'refund'
        if template == 'boltz_legacy_reverse' and len(preimage) != 32:
            return 'refund'
    return 'unknown'


def inspect_input(entry):
    previous = entry.get('prevout') if isinstance(entry, dict) else None
    if not isinstance(previous, dict):
        raise TraceError('Missing previous output for witness inspection')
    program = _hex(previous.get('scriptpubkey'))
    result = dict(status='unsupported', script_type='other', template=None, branch='unknown',
                  payment_hash=None, hash_algorithm=None,
                  summary='No supported swap contract was identified. This does not exclude Lightning use.', evidence=[])
    raw = entry.get('witness', [])
    if not isinstance(raw, list) or len(raw) > 1000 or any(not isinstance(v, str) for v in raw) or sum(map(len, raw)) > 800000:
        raise TraceError('Invalid or oversized Bitcoin witness')
    witness = [_hex(value, 400000) for value in raw]
    taproot = program[:2] == b'\x51\x20' and len(program) == 34
    if len(program) == 23 and program[:2] == b'\xa9\x14' and program[-1] == 0x87:
        redeem = _hex(entry.get('scriptsig', ''))
        if len(redeem) != 35 or redeem[:3] != b'\x22\x00\x20':
            return result
        if hash160(redeem[1:]) != program[2:22]:
            raise TraceError('Nested witness script does not commit to the previous output')
        program = redeem[1:]
        result['script_type'] = 'P2SH-P2WSH'
    if taproot:
        result['script_type'] = 'P2TR'
        if len(witness) >= 2 and witness[-1][:1] == b'\x50':
            witness = witness[:-1]  # BIP341 annex is excluded from stack/script parsing.
        if not witness:
            raise TraceError('Missing Taproot spending witness')
        if len(witness) == 1:
            if len(witness[0]) not in (64, 65):
                raise TraceError('Invalid Taproot key-path signature length')
            result.update(status='hidden', summary='Taproot key-path spend. No contract script is revealed, so a swap cannot be determined from this witness.')
            return result
        script, control, stack = witness[-2], witness[-1], witness[:-2]
        if len(script) > 10000:
            raise TraceError('Script inspection size limit exceeded')
        version = taproot_commitment(program[2:], script, control)
        result['evidence'].append('Taproot script/control-block commitment matches the previous output.')
        if version != 0xc0:
            return result
    elif len(program) == 34 and program[:2] == b'\x00\x20':
        if not witness:
            raise TraceError('Missing P2WSH spending witness')
        script, stack = witness[-1], witness[:-1]
        if len(script) > 10000 or hashlib.sha256(script).digest() != program[2:]:
            raise TraceError('Witness script does not commit to the previous output')
        result['script_type'] = 'P2WSH' if result['script_type'] == 'other' else result['script_type']
        result['evidence'].append('Witness script SHA256 matches the previous output.')
    else:
        return result
    matched = _template(script, taproot)
    if matched:
        template, digest, refund_key_hash = matched
        branch = _branch(template, stack, digest, refund_key_hash)
        result.update(status='candidate', template=template, branch=branch, payment_hash=digest.hex(), hash_algorithm='hash160',
                      summary='Recognized swap-contract candidate. Template compatibility does not establish the provider, swap direction, or a completed Lightning payment.')
        result['evidence'] += ['Exact supported template: ' + template + '.',
            'Contract HASH160 commitment: ' + digest.hex() + '.',
            'Witness branch: ' + branch + '. Bitcoin signatures and consensus execution are not independently revalidated.']
    return result
