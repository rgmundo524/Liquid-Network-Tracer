# Lightning Network lookup

Open **Lightning Network** in the workspace sidebar, paste a Bitcoin transaction
hash, and select **Check Lightning Network**. To check several transactions, choose
**Upload CSV** and select a file containing a Bitcoin transaction-hash column. A
headerless single-column list is also accepted. No investigation or seed is needed.
Results stay in the browser session. Opening the tab does not fetch anything.

CSV review occurs locally before submission. Duplicate hashes are ignored, malformed
rows must be corrected, and one batch can contain up to 100 unique transactions.
The batch runs as one cancellable task. When every row has been checked, the browser
automatically downloads `lightning-network-checks.csv`; the result panel also offers
a manual download button for the current session.

The lookup performs independent channel-index and script inspections. A missing
index entry, hidden contract, unsupported script or incomplete request is never
reported as proof that the transaction is unrelated to Lightning.

The channel index is sampled three times because Mempool's public index can return
different snapshots for the same historical transaction. Any returned channel must
still pass exact Bitcoin funding-output verification. One verified positive result
prevails over empty samples, and the interface discloses the disagreement. A negative
index result is shown only when all three completed samples are empty.

## Reading the results

When none of the checks produces a channel, supported contract candidate, or verified
provider association, the interface reports only **No matches found** and the Bitcoin
transaction. This states the lookup outcome without claiming that the transaction is
unrelated to Lightning. Supporting cards and source details are shown for positive
results.

- **Indexed channel funding** verifies the exact funding output, capacity and
  confirmation block against Mempool's channel record and Blockstream Bitcoin data.
- **Indexed channel funding spend** identifies the actual input spending an indexed
  channel output. It may be a closure or a splice. This lookup does not verify splice
  continuity or determine which closing outputs belong to which participant.
- **Recognized swap-contract candidate** means a revealed script matches one of the
  supported templates and its commitment matches the Bitcoin output. The witness
  may identify a claim or refund branch. It does not establish service ownership,
  swap direction, a Lightning recipient, or invoice settlement.
- Hidden, unspent, unsupported, and unavailable scripts do not count as matches and
  are not expanded in the empty interface state.

Candidate cards show the funding outpoint, output value, spending transaction and
input, compatible template, observed branch and contract hash. HASH160 is the
20-byte script commitment, not a BOLT11 invoice's 32-byte payment hash. The checker
verifies script commitments and branch conditions where available; it relies on
explorer transaction data and does not rerun Bitcoin consensus/signature validation.

Coverage is reported independently for the channel index and witness inspection.
Valid script evidence can still appear if the public channel index is unavailable.
Partial results retain checked evidence and state which limits or data failures
prevented further inspection. Inconclusive details are collapsed by default.

## Optional known swap verification

Expand **Optional provider verification** and enter a known **Boltz submarine swap
ID** from a receipt. The ID is sent to Boltz's public read-only API. This checks
whether its reported Bitcoin funding transaction equals the supplied transaction.
If raw transaction hex is returned, its non-witness txid is recomputed as well.

A match is labeled **Provider-reported swap association**. A returned status such
as `invoice.settled` remains a provider statement, not independent evidence of
Lightning settlement. No generic Bitcoin transaction-to-swap search API is assumed.
Leave the field blank when no swap ID is available. Private keys, invoices, API
credentials and recovery keys are not required.

## Scope and bounds

The lookup checks up to 64 query inputs, 32 query outputs and 8 distinct direct
spending transactions. Several outputs spent by the same transaction share one
fetch. It does not recursively trace descendants or scan blocks. The shared budget
is 12 request attempts and 30 seconds, including three index observations, retries, channel verification and
optional Boltz requests. Source endpoints and observation timestamps accompany the
report. Temporary API evidence is discarded when the task finishes.

Supported templates are Boltz legacy swap/reverse swap and Loop HTLC v2 in native
or nested P2WSH, plus Boltz Taproot claim leaves and Loop HTLC v3 claim leaves with
verified BIP341 control blocks. A revealed timeout-only Taproot leaf is insufficient
to recover the hidden hashlock branch and stays unsupported. Template names describe
compatibility with public source code, not proof that the named service handled it.

Private-channel discovery and verified splice continuity are not implemented.
Mempool's txid endpoint may report only one channel spend in a transaction with
several. Node aliases come from the index and are not verified legal identities;
the tool does not independently verify channel-announcement signatures.

## References

- [Blockstream transaction and outspend API](https://github.com/Blockstream/esplora/blob/master/API.md#transactions)
- [Mempool channel txid API](https://mempool.space/docs/api/rest#get-channels-from-txid)
- [BIP141 witness commitments](https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki)
- [BIP341 Taproot commitments](https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki)
- [Boltz script templates](https://github.com/BoltzExchange/boltz-core/tree/master/lib/swap)
- [Loop HTLC v2/v3 templates](https://github.com/lightninglabs/loop/blob/master/swap/htlc.go)
- [Boltz API schema reviewed](https://github.com/BoltzExchange/boltz-backend/blob/4d131ef8562eea25ab687bcc75a17ce899110b66/swagger-spec.json)

For a terminal lookup:

```sh
liquid-trace lightning-lookup --txid BITCOIN_TRANSACTION_HASH
liquid-trace lightning-lookup --txid BITCOIN_TRANSACTION_HASH --boltz-swap-id KNOWN_SWAP_ID
liquid-trace lightning-lookup --txids HASH_1,HASH_2,HASH_3
```

Schema version 2 retains the version 1 `matches` and `classifications` as channel
index results, and adds `inspection` and `provider_verification`. The legacy machine
classification `indexed_channel_closure` means an indexed funding-output spend;
its display wording explicitly leaves closure versus splice undetermined.
