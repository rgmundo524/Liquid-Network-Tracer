import hashlib

from liquid_tracer.common import LBTC

A, B, C, D, X = [hashlib.sha256(("SYNTHETIC-" + s).encode()).hexdigest() for s in "ABCDX"]
CONFIRMED = {"confirmed": True, "block_height": 12345, "block_time": 1700000000,
             "block_hash": hashlib.sha256(b"SYNTHETIC-BLOCK").hexdigest()}


def output(address, value=None, asset=None, script="0014" + "aa" * 20):
    result = {"scriptpubkey": script, "scriptpubkey_type": "v0_p2wpkh", "scriptpubkey_address": address}
    if value is None:
        result["valuecommitment"] = "08" + "ab" * 32
    else:
        result["value"] = value
    if asset is None:
        result["assetcommitment"] = "0a" + "cd" * 32
    else:
        result["asset"] = asset
    return result


def fixture():
    fee = {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 100}
    a0 = output("SYNTHETIC-victim-deposit", value=1_000_000, asset=LBTC)
    a1 = output("SYNTHETIC-unrelated-seed-sibling")
    b0 = output("SYNTHETIC-branch-A")
    b1 = output("SYNTHETIC-branch-B")
    # Same address reused with a DIFFERENT outpoint. Do not merge its UTXOs.
    c0 = output("SYNTHETIC-branch-A")
    c1 = {"scriptpubkey": "6a04deadbeef", "scriptpubkey_type": "op_return",
          "value": 0, "asset": LBTC}
    d0 = {"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "value": 1234,
          "asset": LBTC, "pegout": {"genesis_hash": "00" * 32,
              "scriptpubkey_address": "SYNTHETIC-bitcoin-payout-request", "scriptpubkey": "0014" + "bb" * 20}}
    def vin(txid, index, out):
        return {"txid": txid, "vout": index, "prevout": out, "is_coinbase": False, "is_pegin": False}
    def tx(txid, ins, outs):
        return {"txid": txid, "vin": ins, "vout": outs, "status": dict(CONFIRMED), "fee": 100, "version": 2}
    def spent(txid, index):
        return {"spent": True, "txid": txid, "vin": index, "status": dict(CONFIRMED)}
    return {
        "/tx/" + A: tx(A, [vin(X, 0, output("SYNTHETIC-funding-context"))], [a0, a1, fee]),
        "/tx/" + A + "/outspends": [spent(B, 0), spent(X, 0), {"spent": False}],
        "/tx/" + B: tx(B, [vin(A, 0, a0)], [b0, b1, fee]),
        "/tx/" + B + "/outspends": [spent(C, 0), spent(C, 1), {"spent": False}],
        "/tx/" + C: tx(C, [vin(B, 0, b0), vin(B, 1, b1), vin(X, 1, output("SYNTHETIC-external-coinput"))], [c0, c1, fee]),
        "/tx/" + C + "/outspends": [spent(D, 0), {"spent": False}, {"spent": False}],
        "/tx/" + D: tx(D, [vin(C, 0, c0)], [d0, fee]),
    }
