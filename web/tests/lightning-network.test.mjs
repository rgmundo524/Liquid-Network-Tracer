import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {forgetLightning, lightningAction, lightningComplete, lightningFile, lightningInput, lightningPanel} from '../src/scripts/lightning-network.ts';

const btc = 'a'.repeat(64), fundingTxid = 'b'.repeat(64), other = 'c'.repeat(64);
const node1 = '02'+'d'.repeat(64), node2 = '03'+'e'.repeat(64);
let downloads;
const report = {
  schema_version:1, kind:'lightning_lookup', query:{bitcoin_txid:btc}, status:'complete',
  classifications:['indexed_channel_funding'],
  bitcoin:{txid:btc, status:{confirmed:true, block_height:123, block_time:3600}},
  matches:[{
    role:'funding', classification:'indexed_channel_funding', channel_id:'135239930216448',
    short_channel_id:'123x4x0', funding_txid:btc, funding_vout:0, capacity_sat:60000001,
    funding_status:{confirmed:true}, nodes:[{public_key:node1, alias:'First node'}, {public_key:node2, alias:'Second node'}],
    closing_txid:null, bitcoin_input_index:null,
  }],
  sources:[{provider:'mempool.space', endpoint:'/api/v1/lightning/channels/txids', observed_at:'2026-10-06T00:00:00Z'}],
  notice:'Only public indexed channel references are checked.',
};
function context(overrides = {}) {
  return {busy:false, render(){}, start:async () => 'lightning-job', download(name,text){downloads.push({name,text});}, ...overrides};
}
const action = (ctx = context()) => lightningAction('lightning-start', ctx);
const edit = value => lightningInput({id:'lightning-field-txid', value});
async function show(saved = report, overrides = {}) {
  const ctx = context(overrides);
  edit(btc);
  await action(ctx);
  assert.equal(await lightningComplete('lightning-job', structuredClone(saved), undefined, ctx), true);
  return ctx;
}
function noIndex() {
  return {...structuredClone(report), classifications:['no_indexed_channel_found'], matches:[]};
}
function closing() {
  const saved = structuredClone(report);
  saved.classifications = ['indexed_channel_closure'];
  Object.assign(saved.matches[0], {role:'closure', classification:'indexed_channel_closure', funding_txid:fundingTxid,
    funding_vout:2, closing_txid:btc, bitcoin_input_index:3});
  return saved;
}
beforeEach(() => {forgetLightning();downloads=[];});

test('the optional panel offers one transaction or CSV and opening or rerendering it never starts a lookup', () => {
  const html = lightningPanel(false);
  assert.match(html, /<h2>Lightning Network<\/h2>/);
  assert.match(html, /Bitcoin transaction hash/);
  assert.match(html, /id="lightning-field-txid"/);
  assert.match(html, /data-action="lightning-start"/);
  assert.match(html, /Check Lightning Network/);
  assert.match(html, /Upload CSV/);
  assert.doesNotMatch(html, /saved searches|Load history|data-action="lightning-(reload|select)"/);
  edit(btc);
  assert.equal(lightningPanel(false), lightningPanel(false));
  assert.match(lightningPanel(false), new RegExp('value="'+btc+'"'));
  assert.match(html, /available transaction evidence/);
});

test('CSV mode reviews hashes locally, submits one batch and downloads the consolidated result', async () => {
  const starts=[],ctx=context({start:async body=>{starts.push(body);return 'batch-job';}});
  await lightningAction('lightning-mode-batch',ctx);
  assert.match(lightningPanel(false),/id="lightning-csv-file"/);
  const csv='bitcoin_transaction\n'+btc+'\n'+other+'\n'+btc+'\n';
  await lightningFile({id:'lightning-csv-file',files:[{name:'checks.csv',size:csv.length,text:async()=>csv}]},ctx.render);
  let html=lightningPanel(false);
  assert.match(html,/2 unique valid transactions/);
  assert.match(html,/1 duplicate row was ignored/);
  await action(ctx);
  assert.deepEqual(starts,[{action:'lightning-lookup',txids:[btc,other]}]);
  const first=noIndex(),second=structuredClone(noIndex());
  second.query.bitcoin_txid=other;second.bitcoin.txid=other;
  const batch={schema_version:1,kind:'lightning_batch',query:{bitcoin_txids:[btc,other]},status:'complete',
    reports:[first,second],summary:{total:2,matched:0,no_matches:2,incomplete:0}};
  assert.equal(await lightningComplete('batch-job',batch,undefined,ctx),true);
  html=lightningPanel(false);
  assert.match(html,/CSV checks complete/);assert.match(html,/2 transactions checked/);
  assert.equal(downloads.length,1);assert.equal(downloads[0].name,'lightning-network-checks.csv');
  assert.match(downloads[0].text,new RegExp(btc+',no_matches'));
  assert.match(downloads[0].text,new RegExp(other+',no_matches'));
  await lightningAction('lightning-download-csv',ctx);
  assert.equal(downloads.length,2);
});

test('CSV mode blocks malformed files and batches larger than 100 hashes', async () => {
  const ctx=context();await lightningAction('lightning-mode-batch',ctx);
  const invalid='bitcoin_transaction\nnot-a-hash\n';
  await lightningFile({id:'lightning-csv-file',files:[{name:'bad.csv',size:invalid.length,text:async()=>invalid}]},ctx.render);
  await action(ctx);assert.match(lightningPanel(false),/Fix the CSV errors/);
  const rows=Array.from({length:101},(_,index)=>index.toString(16).padStart(64,'0')).join('\n');
  const large='bitcoin_transaction\n'+rows+'\n';
  await lightningFile({id:'lightning-csv-file',files:[{name:'large.csv',size:large.length,text:async()=>large}]},ctx.render);
  assert.match(lightningPanel(false),/at most 100 unique transaction hashes/);
});

test('only the Lightning field and action are handled', async () => {
  assert.equal(lightningInput({id:'case-field-txid', value:btc}), false);
  assert.equal(await lightningAction('pegin-start', context()), false);
  assert.equal(await lightningAction('lightning-reload', context()), false);
});

test('hash validation rejects invalid input locally and normalizes the single Bitcoin request', async () => {
  const starts = [], ctx = context({start:async body => {starts.push(body); return 'lightning-job';}});
  for (const value of ['', 'not-a-hash', 'a'.repeat(63), 'g'.repeat(64), btc+'\n'+other]) {
    edit(value);
    await action(ctx);
    assert.match(lightningPanel(false), /full 64-character Bitcoin transaction hash/);
  }
  assert.deepEqual(starts, []);
  edit('  '+btc.toUpperCase()+'\n');
  await action(ctx);
  assert.deepEqual(starts, [{action:'lightning-lookup', txid:btc}]);
});

test('current and submitting jobs block duplicate work and protect the input draft', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;}), starts = [];
  const ctx = context({start:async body => {starts.push(body); return gate;}});
  edit(btc);
  const pending = action(ctx);
  await action(ctx);
  edit(other);
  assert.equal(starts.length, 1);
  assert.match(lightningPanel(false), /Submitting lookup/);
  assert.match(lightningPanel(false), new RegExp('value="'+btc+'"[^>]* disabled'));
  release('lightning-job');
  await pending;
  await action(ctx);
  assert.equal(starts.length, 1);
  assert.match(lightningPanel(false), /Checking Lightning index/);
  assert.match(lightningPanel(false), /cancel this task from the task controls/);
});

test('shared busy state blocks admission without erasing the draft or latest result', async () => {
  await show();
  let starts = 0;
  edit(other);
  await action(context({busy:true, start:async () => {starts++; return 'new-job';}}));
  const html = lightningPanel(true);
  assert.equal(starts, 0);
  assert.match(html, /Matches found/);
  assert.match(html, new RegExp('value="'+other+'"[^>]* disabled'));
});

test('a fast completion can populate only the eventually admitted exact job', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;}), ctx = context({start:async () => gate});
  edit(btc);
  const pending = action(ctx);
  assert.equal(await lightningComplete('unrelated', undefined, 'Other task failed', ctx), false);
  assert.equal(await lightningComplete('fast-job', report, undefined, ctx), false);
  assert.doesNotMatch(lightningPanel(false), /Matches found/);
  release('fast-job');
  await pending;
  assert.match(lightningPanel(false), /Matches found/);
  assert.doesNotMatch(lightningPanel(false), /Other task failed/);
});

test('historical, unrelated, duplicate and reset-session completions cannot fill this workspace', async () => {
  const ctx = context();
  assert.equal(await lightningComplete('old-job', report, undefined, ctx), false);
  edit(btc);
  await action(ctx);
  assert.equal(await lightningComplete('unrelated', report, undefined, ctx), false);
  assert.equal(await lightningComplete('lightning-job', report, undefined, ctx), true);
  assert.equal(await lightningComplete('lightning-job', noIndex(), undefined, ctx), false);
  assert.match(lightningPanel(false), /Matches found/);
  forgetLightning();
  assert.equal(await lightningComplete('lightning-job', report, undefined, ctx), false);
  assert.doesNotMatch(lightningPanel(false), /Matches found/);
});

test('tab hiding does not lose the draft or a completion received while hidden', async () => {
  const ctx = context({render(){}});
  edit(btc);
  await action(ctx);
  await lightningComplete('lightning-job', report, undefined, ctx);
  const html = lightningPanel(false);
  assert.match(html, new RegExp('value="'+btc+'"'));
  assert.match(html, /Matches found/);
});

test('an explicitly reset draft rejects a submission still awaiting admission', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;}), ctx = context({start:async () => gate});
  edit(btc);
  const pending = action(ctx);
  await lightningComplete('fast-job', report, undefined, ctx);
  forgetLightning();
  edit(other);
  release('fast-job');
  await pending;
  assert.match(lightningPanel(false), new RegExp('value="'+other+'"'));
  assert.doesNotMatch(lightningPanel(false), /Matches found|Checking Lightning index/);
});

test('funding cards show exact output, capacity, nodes and safe explorer links', async () => {
  await show();
  const html = lightningPanel(false);
  assert.match(html, /Indexed channel funding/);
  assert.match(html, /Bitcoin funding output/);
  assert.match(html, /vout 0/);
  assert.match(html, /0\.60000001 BTC \(60000001 sats\)/);
  assert.match(html, /Confirmed · Block 123 · 1970-01-01 01:00:00 UTC/);
  assert.match(html, /135239930216448/);
  assert.match(html, /123x4x0/);
  assert.match(html, /First node/);
  assert.match(html, new RegExp('https://mempool.space/tx/'+btc));
  assert.match(html, /https:\/\/mempool.space\/lightning\/channel\/135239930216448/);
  assert.match(html, new RegExp('https://mempool.space/lightning/node/'+node1));
  assert.match(html, /Lookup sources/);
});

test('closing cards show the actual closing input and its exact spent funding reference', async () => {
  await show(closing());
  const html = lightningPanel(false);
  assert.match(html, /Indexed channel funding spend/);
  assert.match(html, /Bitcoin spending input/);
  assert.match(html, /Input 3 \(vin 3\)/);
  assert.match(html, new RegExp(fundingTxid+':2'));
  assert.match(html, /exact input spends the indexed channel funding outpoint/);
});

test('one transaction can show both indexed funding and indexed closing', async () => {
  const saved = structuredClone(report);
  saved.matches.push(closing().matches[0]);
  saved.classifications.push('indexed_channel_closure');
  await show(saved);
  const html = lightningPanel(false);
  assert.match(html, /2 indexed channel matches/);
  assert.match(html, /Indexed channel funding/);
  assert.match(html, /Indexed channel funding spend/);
});

test('a completed lookup without evidence is concise', async () => {
  await show(noIndex());
  const html = lightningPanel(false);
  assert.match(html, /No matches found/);
  assert.doesNotMatch(html, /private or unindexed|Lightning swaps|Witness and direct spend inspection|Lookup sources|Bitcoin funding output/);
});

test('inconsistent channel-index observations preserve and explain a verified positive', async () => {
  const saved={...structuredClone(report),schema_version:2,inspection:{status:'complete',findings:[],checked_inputs:0,
    checked_outputs:0,total_inputs:0,total_outputs:0,reason:null},provider_verification:null,
    index_observations:{attempts:3,positive:1,empty:2,consistent:false,samples:[
      {result:'empty',channel_count:0,observed_at:'2026-10-06T01:00:00Z'},
      {result:'channels',channel_count:1,observed_at:'2026-10-06T01:00:01Z'},
      {result:'empty',channel_count:0,observed_at:'2026-10-06T01:00:02Z'}]}};
  await show(saved);
  const html=lightningPanel(false);
  assert.match(html,/Channel-index responses were inconsistent/);
  assert.match(html,/channel in 1 of 3 checks and an empty result in 2/);
  assert.match(html,/passed independent Bitcoin funding-output verification/);
  assert.match(html,/Matches found/);
});

test('errors, cancellation and incomplete reports never become a negative index conclusion', async () => {
  for (const status of ['error', 'paused', 'cancelled']) {
    forgetLightning();
    const saved = {...noIndex(), bitcoin:null, classifications:[], status, error:'Index unavailable'};
    await show(saved);
    const html = lightningPanel(false);
    assert.match(html, /Lookup incomplete/);
    assert.match(html, /Index unavailable/);
    assert.doesNotMatch(html, /No matches found/);
    assert.doesNotMatch(html, /data-action="lightning-start" disabled/);
  }
  forgetLightning();
  edit(btc);
  const ctx = context();
  await action(ctx);
  await lightningComplete('lightning-job', noIndex(), 'Task cancelled.', ctx);
  assert.match(lightningPanel(false), /Task cancelled/);
  assert.match(lightningPanel(false), /Lookup incomplete/);
  assert.doesNotMatch(lightningPanel(false), /No matches found/);
});

test('malformed and wrong-transaction reports release the form and remain retryable errors', async () => {
  const cases = [undefined, {...report, query:{bitcoin_txid:other}}, {...report, kind:'other'},
    {...report, classifications:['no_indexed_channel_found']}, {...report, bitcoin:null},
    {...noIndex(), status:'error'}, {...report, matches:[{...report.matches[0], funding_vout:-1}]},
    {...closing(), matches:[{...closing().matches[0], bitcoin_input_index:null}]},
    {...report, matches:[{...report.matches[0], funding_txid:other}]},
    {...report, bitcoin:{txid:other, status:{confirmed:true}}}];
  const ctx = context();
  edit(btc);
  for (const result of cases) {
    await action(ctx);
    assert.equal(await lightningComplete('lightning-job', result, undefined, ctx), true);
    const html = lightningPanel(false);
    assert.match(html, /did not return a valid Lightning lookup/);
    assert.doesNotMatch(html, /No matches found|Matches found/);
    assert.doesNotMatch(html, /data-action="lightning-start" disabled/);
  }
  await action(ctx);
  await lightningComplete('lightning-job', report, undefined, ctx);
  assert.match(lightningPanel(false), /Matches found/);
});

test('admission failures retain the input and can be retried', async () => {
  edit(btc);
  await action(context({start:async () => null}));
  assert.match(lightningPanel(false), /lookup could not start/);
  await action(context({start:async () => {throw Error('Connection unavailable');}}));
  assert.match(lightningPanel(false), /Connection unavailable/);
  assert.match(lightningPanel(false), new RegExp('value="'+btc+'"'));
  await action();
  await lightningComplete('lightning-job', undefined, 'Task cancelled.', context());
  assert.match(lightningPanel(false), /Task cancelled/);
  assert.doesNotMatch(lightningPanel(false), /No matches found/);
});

test('result strings are escaped and remote supplied URLs cannot become links', async () => {
  const saved = structuredClone(report);
  saved.notice = '<script>bad</script>';
  saved.matches[0].nodes[0].alias = '<img src=x onerror=bad>';
  saved.matches[0].channel_id = '<svg onload=bad>';
  saved.matches[0].short_channel_id = null;
  saved.matches[0].channel_url = 'javascript:bad';
  saved.matches[0].nodes[0].explorer_url = 'https://other.invalid/node';
  saved.bitcoin.explorer_url = 'javascript:bad';
  await show(saved);
  const html = lightningPanel(false);
  assert.doesNotMatch(html, /<script|<img|<svg|javascript:|other\.invalid/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;svg/);
});

test('capacity strings preserve all satoshis without floating-point rounding', async () => {
  const saved = structuredClone(report);
  saved.matches[0].capacity_sat = '9007199254740993';
  await show(saved);
  assert.match(lightningPanel(false), /90071992\.54740993 BTC \(9007199254740993 sats\)/);
});

function evidenceReport(status = 'candidate') {
  return {...noIndex(), schema_version:2, provider_verification:null, inspection:{
    status:'complete', checked_inputs:1, checked_outputs:1, total_inputs:1, total_outputs:1, reason:null,
    findings:[{role:'output',index:0,funding_txid:btc,funding_vout:0,spending_txid:other,spending_input_index:2,
      value_sat:12345678,status,script_type:'P2WSH',template:status==='candidate'?'boltz_legacy_swap':null,
      branch:status==='candidate'?'claim':'unknown',payment_hash:status==='candidate'?'ab'.repeat(20):null,
      hash_algorithm:status==='candidate'?'hash160':null,summary:'Contract compatibility does not prove Lightning settlement.',
      evidence:['Output commitment verified.']}],
  }};
}
test('script candidates become the positive result without empty-index prose', async () => {
  await show(evidenceReport());
  const html=lightningPanel(false);
  assert.match(html,/Matches found/);
  assert.doesNotMatch(html,/No matches found|No indexed Lightning channel found/);
  assert.match(html,/Recognized swap-contract candidate/);
  assert.match(html,/Witness and direct spend inspection/);
  assert.match(html,/0\.12345678 BTC/);
  assert.match(html,/Contract hash \(HASH160\)/);
  assert.match(html,/boltz_legacy_swap/);
  assert.match(html,/Checked 1 of 1 inputs and 1 of 1 outputs/);
  assert.match(html,/Provider, direction, ownership and Lightning payment settlement are not established/);
});
test('an unavailable inspection without a match stays concise', async () => {
  const saved=evidenceReport('unavailable');saved.inspection.status='partial';saved.inspection.reason='request_limit';saved.inspection.checked_outputs=0;
  saved.inspection.findings[0].value_sat=null;
  await show(saved);
  const html=lightningPanel(false);
  assert.match(html,/No matches found/);
  assert.doesNotMatch(html,/Inspection incomplete|Checked 1 of 1|Inspection unavailable|Witness and direct spend inspection/);
});
test('hidden contracts without a match stay concise', async () => {
  await show(evidenceReport('hidden'));
  const html=lightningPanel(false);
  assert.match(html,/No matches found/);
  assert.doesNotMatch(html,/Inconclusive output|Contract script hidden|Lightning involvement remains undetermined|Recognized swap-contract candidate/);
});
test('optional provider ID is submitted only when provided and cannot inject paths', async () => {
  const starts=[],ctx=context({start:async body=>{starts.push(body);return 'lightning-job';}});
  edit(btc);
  lightningInput({id:'lightning-field-boltz_swap_id',value:'../bad'});
  await action(ctx);assert.equal(starts.length,0);assert.match(lightningPanel(false),/valid Boltz swap ID/);
  lightningInput({id:'lightning-field-boltz_swap_id',value:'known-id'});
  await action(ctx);
  assert.deepEqual(starts,[{action:'lightning-lookup',txid:btc,boltz_swap_id:'known-id'}]);
});
test('provider association and reported status remain distinct from independent proof', async () => {
  const saved=evidenceReport();saved.provider_verification={status:'verified',provider:'Boltz',swap_id:'known-id',
    swap_type:'submarine',transaction_id:btc,provider_status:'invoice.settled',
    summary:'Provider-reported association; invoice settlement is not independently verified.',evidence:['Exact funding transaction matched.']};
  await show(saved);
  const html=lightningPanel(false);
  assert.match(html,/Provider-reported swap association/);assert.match(html,/Provider-reported status/);
  assert.match(html,/invoice settlement is not independently verified/);
});
test('inspection identity tampering is rejected and evidence is escaped', async () => {
  const saved=evidenceReport();saved.inspection.findings[0].summary='<img src=x onerror=bad>';
  saved.inspection.findings[0].evidence=['<script>bad</script>'];
  await show(saved);let html=lightningPanel(false);
  assert.doesNotMatch(html,/<img|<script/);assert.match(html,/&lt;img/);
  forgetLightning();saved.inspection.findings[0].funding_txid=other;await show(saved);
  assert.match(lightningPanel(false),/did not return a valid Lightning lookup/);
});
