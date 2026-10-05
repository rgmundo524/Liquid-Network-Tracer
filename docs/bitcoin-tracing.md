# Bitcoin tracing in the same workspace

`liquid-web` supports Bitcoin and Liquid investigations in the same local service.
The saved network controls transaction lookup, collection, address links, amounts,
and compatible shared evidence. A transaction hash or address alone does not
change that selection.

## Create a Bitcoin investigation

1. Start the existing service with `devenv shell`, then `liquid-web`.
2. Choose New investigation and select Bitcoin Network before loading outputs.
3. Paste the Bitcoin transaction hashes and choose Load outputs. Select the exact
   output indexes relevant to the investigation. Alternatively, enter known
   `TRANSACTION_HASH:VOUT` references. Indexes start at zero.
4. Create the investigation. Its network badge remains Bitcoin. Changing networks
   requires another investigation. Starting outputs can still be edited within
   the investigation's network; saved runs and previews keep their original seeds.
5. Add reviewed attribution and explicit stop rules, then collect the desired
   depth in Collect data. Use Plots & Miro to generate figures from that evidence.

Switching the network on the creation form clears loaded outputs and selections
so a Liquid lookup cannot become a Bitcoin seed selection accidentally. Loading
outputs inspects the transactions; collection is the separate step that follows
spending transactions.

The examples below use the browser and CLI. The existing Textual menu is not
required to create a Bitcoin case.

## Choose a plot

| Plot | Purpose |
| --- | --- |
| Full trace | Show reachable transactions and their inputs and outputs within the selected display scope. |
| Starter connections | Show saved directed connections among the selected starting transactions. Shortest connections retains one minimum-hop route for each connected pair and keeps disconnected starters visible. |
| Paths to endpoints | Show qualifying paths from the selected starting outputs to the endpoint types enabled for the plot. |

Paths to endpoints is the shared name for the former Paths to peg-outs goal.
The internal goal and compatible CLI option remain `pegouts`. Existing Liquid
previews retain their saved query and selected endpoint flags.

New browser endpoint plots enable these optional types by default:

- Observed unspent outputs: the explorer recorded the particular UTXO as unspent.
  This describes the saved observation, not a current address balance or a minimum
  period of inactivity.
- Unspendable outputs: for example, OP_RETURN outputs. They are terminal outputs,
  not spendable assets available for recovery.
- Attributed stops: an enabled investigation rule explicitly stops tracing through
  that address. The endpoint retains the attribution and its supporting metadata.

Liquid also includes protocol peg-out requests. Bitcoin has no Liquid peg-out
endpoint and does not offer the cumulative L-BTC peg-out limit. A Bitcoin deposit
into a service or bridge is not recognized as a service endpoint merely from
its transaction format or amount. Record that assessment and enable Stop tracing
when it is justified.

A hop cutoff, missing spend evidence, or interrupted collection is an open
boundary. It is not automatically an unspent or service endpoint. Endpoint plots
can omit unfinished branches that have not reached a selected endpoint type;
use Full trace or Analysis to review those boundaries.

## Attribution and stopping rules

An attribution name describes an assessment. It does not, by itself, establish
ownership or stop collection. Enable the assessment and explicitly enable Stop
tracing through this address when it should be a boundary. New address reviews
and imports default to continuing unless a stop is selected. Existing saved
stop settings retain their meaning.

For an attributed Coinbase deposit, for example, record the address, name,
confidence, source, and supporting notes, then enable Stop tracing. Enable
Include attributed stops in Paths to endpoints to include its path in the
figure and endpoint export. Disabling that endpoint option does not remove the
underlying collection stop rule.

Full trace and endpoint plots respect explicit stop rules. Starter connections
searches the selected saved evidence for connections and deliberately ignores
those stop rules. It cannot discover a connection through transactions that were
never collected.

Shared attribution provides names and supporting information across opted-in
investigations on the same chain. It does not propagate tracing stops or display
hop caps. Set a local investigation rule to make a shared label a stopping
boundary. Local assessments override inherited shared attribution.

Attribution from another research tool can be entered or imported using the
existing reviewed CSV workflow. No TRM Labs API connection or automatic service
attribution is included.

## Collection depth and figure scope

Collection and plotting have independent hop limits. A 15-hop collection can
support a 10-hop figure and a later deeper figure without discarding saved data.
Starting transactions are hop 0; each subsequent spending transaction adds one
hop. Choose Original starting transactions for consistent seed-based comparisons.

Use Analysis to compare saved scopes and inspect open branches before running
layout. A smaller figure limits presentation; it does not establish that the
trace ended there. Extending the display cannot fetch a branch absent from the
collection. Collect additional data first when needed.

Shared collections are separated by blockchain. A Bitcoin investigation uses the
Bitcoin dataset and a Liquid investigation uses the Liquid dataset. Start shared
collection with the investigations on the relevant chain; the collection captures
its selected members and the focused investigation's policy. Each investigation
then plots its own seeds and rules against a compatible saved shared snapshot.

The default workspace paths are `cases/.shared-collection` for Liquid and
`cases/.shared-collection-bitcoin` for Bitcoin. Liquid's shared attribution file
remains `cases/.shared-attributions.json`; Bitcoin uses
`cases/.bitcoin/.shared-attributions.json`. The workspace Shared attribution
panel has a network selector. Its investigation panel uses that case's fixed
network. Shared import approval and export operate on the selected library.

Older investigations without an explicit network field are treated as Liquid.
Existing evidence, previews, private collections, and Miro mappings are not
converted to Bitcoin or rewritten by creating a Bitcoin investigation.

## Amounts, evidence, and exports

Bitcoin outputs have explicit BTC values. The tool keeps their integer satoshi
amounts and displays BTC using 100,000,000 satoshis per BTC without rounding away
precision. Liquid confidential values remain unknown when the public evidence
omits them; Bitcoin support does not infer missing Liquid assets or amounts.

Exact UTXO links establish reachability. After inputs are combined, following
candidate outputs does not allocate a particular victim's value among them.
Public Bitcoin amounts improve the available evidence but do not supply an
ownership assessment or a taint-accounting method.

Saved previews include the graph and supporting exports. The endpoint and path
CSVs identify the network and source outpoints; Bitcoin endpoint amounts appear
in Value BTC and Value Base Units. Liquid's existing L-BTC columns remain for
compatibility. Attribution confidence, source, and boundary status accompany
attributed endpoints. OP_RETURN status and analyst stops remain distinct from
observed unspent status.

Export endpoints across open investigations selects each investigation's latest
completed endpoint plot for its selected collection snapshot. It does not combine
every historical preview. An endpoint reached from several investigations retains
one row per investigation with its provenance. Deduplicate by network and exact
outpoint before calculating a unique-output total; a combined sum is not an
allocation of stolen assets. The export reports investigations without a matching
completed endpoint plot instead of silently using another snapshot.

## Review a preview and write it to Miro

Generate preview saves a numbered entry in the investigation's Saved previews
library. Open preview opens the full chart in another browser tab. The saved SVG,
CSV files, and supporting evidence remain available in History & downloads.

Choose Use in Miro on the desired saved preview. Review the selected layout and
board destination, then explicitly create or update the Miro board using that
preview. The saved layout is reused without another ELK layout calculation.
A preview prepared to update an existing board is bound to that board's reviewed
state; regenerate the update if that state has changed. Local-only work can stop
after preview and export.

## CLI examples

Run these inside the existing development environment. Replace the placeholder
transaction IDs and output indexes with reviewed Bitcoin references. These
commands use the configured Blockstream credentials. Bitcoin's default endpoint
is `https://enterprise.blockstream.info/api`; Liquid retains its `/liquid/api`
endpoint. A public API can be selected explicitly with `--base-url
https://blockstream.info/api --auth none` for inspection or a new Bitcoin trace.

Inspect one transaction or several before choosing seeds:

```bash
liquid-trace inspect-tx --blockchain bitcoin --txid BITCOIN_TRANSACTION_ID
liquid-trace inspect-txs --blockchain bitcoin --txids BITCOIN_TXID_1,BITCOIN_TXID_2
```

Create a new Bitcoin case and collect from the selected outputs. Omit the second
`--seed` for a single starting output:

```bash
liquid-trace trace --case cases/bitcoin-follow-up --blockchain bitcoin \
  --seed BITCOIN_TXID_1:0 --seed BITCOIN_TXID_2:1 --hops 10
```

Continue its saved frontier by another five hops:

```bash
liquid-trace trace --case cases/bitcoin-follow-up --blockchain bitcoin \
  --resume latest --additional-hops 5
```

`--blockchain bitcoin` initializes a new CLI case as Bitcoin. For an existing
case it must match the saved network; it cannot convert a Liquid case. Subsequent
plot commands derive the network from the case and do not need that option.

Generate previews from the saved evidence, without publishing to Miro:

```bash
liquid-trace plot --case cases/bitcoin-follow-up --goal full \
  --run latest --max-hops 10 --hop-basis original_seeds

liquid-trace plot --case cases/bitcoin-follow-up --goal connections \
  --run latest --connection-scope shortest

liquid-trace plot --case cases/bitcoin-follow-up --goal pegouts \
  --run latest --max-hops 10 --hop-basis original_seeds \
  --include-unspent --include-unspendable --include-attributed-stops
```

The CLI endpoint switches are explicit opt-ins, unlike the new browser form's
initial checked values. Keep `--goal pegouts` for endpoint plots on both chains.
`--pegout-lbtc-limit` is Liquid-only. Use `--open` on `plot` to open the generated
preview in the browser.

Save a reviewed service boundary, or record a label that continues tracing:

```bash
liquid-trace service-set --case cases/bitcoin-follow-up \
  --address BITCOIN_DEPOSIT_ADDRESS --name "Reviewed service" \
  --confidence suspected --source "Analyst records" --stop-tracing true

liquid-trace service-set --case cases/bitcoin-follow-up \
  --address BITCOIN_LABELED_ADDRESS --name "Address under review" \
  --confidence suspected --source "Analyst records" --stop-tracing false
```

Regenerate a preview to apply revised rules. Export a saved run's transaction
input/output table separately when required:

```bash
liquid-trace csv-export --case cases/bitcoin-follow-up --run latest
```

## Cross-chain matching remains a separate step

Bitcoin tracing follows Bitcoin outputs. It does not automatically identify the
Liquid claim corresponding to a Bitcoin peg-in deposit, identify a bridge or
service from an unlabeled address, or match a Liquid peg-out to its Bitcoin payout.
Once a Bitcoin payout has been independently matched, its exact output can seed
this workflow. A future peg-in matcher will require separate evidence and matching
logic; Bitcoin support does not imply that matching is already available.
