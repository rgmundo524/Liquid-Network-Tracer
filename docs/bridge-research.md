# Liquid exit routes and attribution leads

Checked 9 September 2026. This is a focused starting list, not an exhaustive catalog. No case-specific Liquid addresses, transaction IDs, or service records were supplied. No Liquid service address has been verified by this research.

| Lead | Documented route | What to look for |
| --- | --- | --- |
| SideShift | Liquid BTC to Avalanche AVAX; Liquid USDT to Avalanche USDT | A service deposit and its order/settlement record. A visible Liquid transfer may be a service payment without a protocol peg-out. |
| SideSwap | L-BTC/BTC conversion; Liquid asset swaps | Order-linked funding outpoint, receiving address and payout transaction. Its Bitcoin liquidity can satisfy a customer before a corresponding protocol peg-out. |
| Boltz | Bitcoin/Liquid chain swaps and Lightning-related swaps | Swap records, lockup/claim transactions, refund state, and matched chain-side evidence. |
| Bitcoin route into Avalanche | Exit Liquid to Bitcoin, then use the applicable BTC-to-Avalanche infrastructure | Establish the Bitcoin leg first. Bridge infrastructure and attribution must match the historical period. |

## SideShift: a concrete direct-route lead

SideShift advertises [Liquid BTC to AVAX](https://sideshift.ai/liquid/avaxc) and [Liquid USDT to Avalanche USDT](https://sideshift.ai/usdtla/usdtavax). This supports service capability at the time checked. It does not establish historical availability for your transaction date or identify your case deposit.

The [shift-details API](https://docs.sideshift.ai/endpoints/v2/shift/) can return deposit and settlement transactions, networks, addresses, status, and amounts for a **known shift ID**. For multiple deposits, examine each deposit entry. This is not a documented arbitrary-address-to-order lookup. The documentation also says deposit addresses can be reassigned and later removed from old order responses, which makes historical records and dates important.

Check status before interpreting a settlement hash: SideShift documents that a refund can also populate that field. See [refund semantics](https://docs.sideshift.ai/api-intro/refunds/). An identified transaction hash alone does not establish which asset/network was paid to the intended recipient.

## SideSwap: order records may be stronger than a static address list

The [SideSwap API documentation](https://sideswap.io/docs/) describes a peg order and `peg_status` response that includes the payment transaction/outpoint, receiving address and payout txid when completed. It also describes use of hot-wallet liquidity. That supports requesting the order's records if attribution points to SideSwap; it does not justify automatically matching any nearby Bitcoin payment to the Liquid transaction.

The API documentation explicitly labels its example values as regtest/testnet. **Do not add those sample addresses to a Liquid-mainnet attribution list.** No common production deposit address was established from this source.

## Boltz: an indirect route to consider

Boltz describes [chain swaps between Bitcoin and Liquid](https://api.docs.boltz.exchange/lifecycle.html). It can therefore be relevant to an intervening Bitcoin leg. This source does not establish a direct Liquid-to-Avalanche product or an address attribution for your case. A script pattern may be a candidate indicator; script resemblance alone is insufficient to name a service or select a recipient output.

## Avalanche: date the infrastructure

[Avalanche's support notice](https://support.avax.network/en/articles/6349640-where-can-i-find-support-for-the-avalanche-bitcoin-bridge), dated 2 February 2026, says its original Bitcoin bridge infrastructure has been wound down and replaced by Lombard's architecture. It names these retired addresses:

| Network | Historical address |
| --- | --- |
| Bitcoin | `bc1q2f0tczgrukdxjrhhadpft2fehzpcrwrz549u90` |
| Avalanche | `0xF5163f69F97B221d50347Dd79382F11c6401f1a1` |

These are historical research references, **not Liquid addresses or current deposit instructions**. The notice does not give a complete validity interval or prove any link to this case. Machine-readable copies are in `examples/historical-bridge-addresses.csv`. [Lombard's BTC.b documentation](https://docs.lombard.finance/use/btcb) is the starting point for reviewing current BTC.b activity. Lombard's LBTC product is distinct from Liquid L-BTC; do not merge them based on similar ticker names.

## Evidence to record for a proposed cross-chain link

Use `examples/handoffs.csv` as a manual ledger. Record the Liquid outpoint, service attribution, destination network, destination transaction/address, source, date, reviewer, and status. Use statuses such as `candidate`, `corroborated`, `confirmed`, or `rejected`. Keep supporting service responses with their original files and timestamps.

Confirm the source-side payment, destination-side transaction, service order state, and applicable network/asset. Timestamp proximity and plausible amounts can guide review but cannot establish a confidential Liquid cross-chain correspondence alone. A VASP deposit establishes receipt by that service when attribution is sound; it does not by itself identify a downstream withdrawal belonging to the same customer.

Once a handoff is supported, it can become the starting evidence for a separate Bitcoin or Avalanche trace. This version deliberately ends automatic traversal at the Liquid boundary. No service was contacted and no swap order was created during this work.
