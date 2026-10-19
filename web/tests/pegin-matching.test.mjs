import assert from 'node:assert/strict';
import {beforeEach,test} from 'node:test';
import {matchingRequest,peginAction,peginInput,peginPanel,peginComplete,peginOutputsComplete,peginFile,peginToggle,parsePeginCsv,reviewPeginCsv,forgetPegin} from '../src/scripts/pegin-matching.ts';
const btc='a'.repeat(64),liquid='b'.repeat(64),id='c'.repeat(32),other='d'.repeat(64);
const settings={action:'pegin-match',bridge:'pegin',network:'bitcoin',mode:'single',max_pages:100,start_minutes:-5,end_minutes:4320,public:true};
const draft={txid:btc,vout:'',pages:'100',start_minutes:'-5',end_minutes:'4320',public:true};
const report={id,created_at:'2026-10-05T00:00:00Z',query:{bitcoin_txid:btc,bitcoin_vout:null,liquid_txid:null,max_pages:100},
  status:'matched',stop_reason:'exact_reference_found',match_count:1,
  matches:[{bitcoin_outpoint:btc+':1',bitcoin_value_sats:'60000000',liquid_txid:liquid,liquid_vin:0,verification:'confirmed_reference',liquid_status:{confirmed:true,block_time:3600}}],
  bitcoin:{txid:btc,status:{confirmed:true,block_time:0},outputs:[{vout:1,address:'bc1-deposit',value:'60000000'}]},
  liquid_outputs:{[liquid]:[{vout:0,outpoint:liquid+':0',address:'liquid-output',value:null,asset:null,selectable:true,reason:null}]},
  coverage:{pages_this_task:1},notice:'Individual input-to-output allocation is not established.',
  downloads:[{name:'matches.csv',url:'/files/pegin-matches/'+id+'/matches.csv'}]};
function context(overrides={}) {return {busy:false,render(){},get:async()=>structuredClone(report),start:async()=> 'match-job',
  loadOutputs:async()=> 'outputs-job',download(){},openLiquid(){},...overrides};}
const action=(name,ctx=context(),element)=>peginAction('pegin-'+name,element,ctx);
const edit=(field,value)=>peginInput({id:'pegin-field-'+field,value});
const check=(vout,checked=true)=>peginInput({id:'pegin-output-'+vout,value:String(vout),checked});
async function upload(text,name='transactions.csv') {await peginFile({files:[{name,size:new TextEncoder().encode(text).length,text:async()=>text}]},()=>{});}
async function show(saved=report,overrides={}) {
  const ctx=context({get:async()=>structuredClone(saved),...overrides});edit('txid',btc);await action('start',ctx);await peginComplete('match-job',{id:saved.id},undefined,ctx);return ctx;
}
function inspection(network='bitcoin',txid=btc) {return {blockchain:network,transactions:[{txid,outputs:[
  {vout:0,outpoint:txid+':0',address:'deposit-address',value:60000000,value_text:'60000001',selectable:true,reason:null,script_type:'v0_p2wsh'},
  {vout:1,outpoint:txid+':1',address:'change-address',value:1,value_text:'1',selectable:true,reason:null,script_type:'v0_p2wpkh'},
  {vout:2,outpoint:txid+':2',address:null,value:0,value_text:'0',selectable:false,reason:'OP_RETURN output',script_type:'op_return'}]}]};}
function batchReport(rows,status='partial') {return {...structuredClone(report),query:{...report.query,mode:'batch',bitcoin_txid:null,bitcoin_txids:[btc,other]},
  status,stop_reason:'batch_complete',results:rows};}
const completedRows=[{bitcoin_txid:btc,status:'matched',stop_reason:'exact_reference_found',match_count:1},{bitcoin_txid:other,status:'no_match',stop_reason:'window_range_complete',match_count:0}];
async function startBatch(ctx) {await action('mode-batch',ctx);await upload('Bitcoin txid\n'+btc+'\n'+other+'\n');await action('start',ctx);}
beforeEach(()=>forgetPegin());

test('transfer type and source network are independent; default is Bitcoin-input peg-in with collapsed settings',()=>{
  let html=peginPanel(false);
  assert.match(html,/aria-label="Transfer type"/);assert.match(html,/data-action="pegin-bridge-pegin" aria-pressed="true"/);
  assert.match(html,/aria-label="Transaction provided"/);assert.match(html,/data-action="pegin-network-bitcoin" aria-pressed="true"/);
  assert.match(html,/Any Bitcoin output/);assert.match(html,/Load Bitcoin outputs/);
  assert.doesNotMatch(html,/pegin-reload|pegin-select|Load saved|No saved searches|id="pegin-field-candidate"|id="pegin-field-direction"/);
  assert.doesNotMatch(html,/<details id="pegin-advanced"[^>]* open/);
  peginToggle({id:'pegin-advanced',open:true});edit('bridge','pegout');edit('network','liquid');html=peginPanel(false);
  assert.match(html,/Load Liquid outputs/);assert.match(html,/Find Bitcoin payout candidates/);assert.match(html,/<details id="pegin-advanced"[^>]* open/);
});
test('all four bridge/source combinations have explicit requests without requiring two hashes',()=>{
  for(const bridge of ['pegin','pegout'])for(const network of ['bitcoin','liquid']) {
    assert.deepEqual(matchingRequest({...draft,bridge,network}),{...settings,bridge,network,txid:btc});
  }
  assert.deepEqual(matchingRequest({...draft,txid:btc.toUpperCase(),vout:'0'}),{...settings,txid:btc,vout:0});
  assert.deepEqual(matchingRequest({...draft,bridge:'pegout',network:'liquid',vouts:[2,0,2]}),{...settings,bridge:'pegout',network:'liquid',txid:btc,vouts:[0,2]});
  assert.equal('vout' in matchingRequest({...draft,network:'liquid',vout:'0'}),false);
});
test('invalid hashes, output indices and bounds fail before task submission',()=>{
  for(const change of [{txid:'bad'},{vout:'-1'},{vout:'1.5'},{vout:'1073741824'},{vouts:[NaN]},{pages:'0'},{pages:'1001'},
    {start_minutes:''},{end_minutes:'Infinity'},{start_minutes:'-5256000.1'},{end_minutes:'-5'},{bridge:'other'},{network:'other'}])
    assert.throws(()=>matchingRequest({...draft,...change}));
  assert.deepEqual(matchingRequest({...draft,start_minutes:'-1.5',end_minutes:'+2.25',public:false}),{...settings,txid:btc,start_minutes:-1.5,end_minutes:2.25,public:false});
});
test('default peg-out window follows the input chain without overwriting investigator settings',()=>{
  edit('bridge','pegout');let html=peginPanel(false);
  assert.match(html,/id="pegin-field-start_minutes" value="-4320"/);assert.match(html,/id="pegin-field-end_minutes" value="5"/);
  assert.match(html,/Window start \(minutes from Bitcoin/);
  edit('network','liquid');html=peginPanel(false);assert.match(html,/id="pegin-field-start_minutes" value="-5"/);assert.match(html,/value="4320"/);
  edit('start_minutes','-12');edit('end_minutes','65');edit('network','bitcoin');html=peginPanel(false);
  assert.match(html,/id="pegin-field-start_minutes" value="-12"/);assert.match(html,/id="pegin-field-end_minutes" value="65"/);
});
test('submission uses selected bridge and source, does not create an investigation and blocks a second task',async()=>{
  const starts=[];const ctx=context({start:async body=>{starts.push(body);return 'match-job';},openLiquid(){throw Error('not requested');}});
  edit('bridge','pegout');edit('network','liquid');edit('txid',liquid);await action('start',ctx);await action('start',ctx);edit('txid',other);
  assert.deepEqual(starts,[{...settings,bridge:'pegout',network:'liquid',txid:liquid}]);
  assert.match(peginPanel(false),new RegExp('id="pegin-field-txid" value="'+liquid+'"'));assert.match(peginPanel(false),/Matching in progress/);
});
test('pending admission prevents duplicates and fast result applies only to eventual admitted job',async()=>{
  let release,starts=0;const gate=new Promise(resolve=>{release=resolve;});
  const ctx=context({start:async()=>{starts++;return gate;}});edit('txid',btc);const pending=action('start',ctx);
  await action('start',ctx);assert.equal(starts,1);
  assert.equal(await peginComplete('unrelated',{id},'Other task failed',ctx),false);
  assert.equal(await peginComplete('fast',{id},undefined,ctx),false);
  release('fast');await pending;assert.match(peginPanel(false),/Confirmed exact reference/);assert.doesNotMatch(peginPanel(false),/Other task failed/);
});
test('only session-admitted results are fetched; historical tasks cannot populate or download',async()=>{
  const paths=[],downloads=[];const ctx=context({get:async path=>{paths.push(path);return structuredClone(report);},download:url=>downloads.push(url)});
  assert.equal(await peginComplete('old-job',{id},undefined,ctx),false);
  await action('reload',ctx);await action('select',ctx,{dataset:{report:id}});assert.deepEqual(paths,[]);
  edit('txid',btc);await action('start',ctx);await peginComplete('match-job',{id},undefined,ctx);
  assert.deepEqual(paths,['/api/tools/pegin-matches/'+id]);assert.deepEqual(downloads,[]);
  forgetPegin();assert.equal(await peginComplete('match-job',{id},undefined,ctx),false);assert.doesNotMatch(peginPanel(false),/Confirmed exact reference/);
});
test('paired peg-in cards show exact outpoint, value, UTC block dates and allocation limits',async()=>{
  await show();const html=peginPanel(false);
  assert.match(html,/<h4>Bitcoin deposit/);assert.match(html,/<h4>Liquid claim/);assert.match(html,/vout 1/);assert.match(html,/vin 0/);
  assert.match(html,/0\.60000000 BTC \(60000000 sats\)/);assert.match(html,/bc1-deposit/);
  assert.match(html,/1970-01-01 00:00:00 UTC/);assert.match(html,/1970-01-01 01:00:00 UTC/);
  assert.match(html,/does not establish ownership or allocate/);assert.match(html,/Confidential \/ unavailable/);assert.match(html,/Download matches.csv/);
});
test('peg-out cards label even unique confirmed-chain matches as candidates and keep ambiguity visible',async()=>{
  const saved=structuredClone(report);saved.query={...saved.query,bridge:'pegout',network:'liquid',txid:liquid,mode:'single'};
  saved.matches=[{...saved.matches[0],liquid_vin:undefined,liquid_vout:2,status:'ambiguous',verification:'script_amount_time_candidate'}];
  await show(saved);const html=peginPanel(false);
  assert.match(html,/Ambiguous payout candidate/);assert.match(html,/Liquid peg-out request/);assert.match(html,/Bitcoin payout candidate/);assert.match(html,/vout 2/);
  assert.match(html,/do not establish an exact cross-chain payout reference/);assert.doesNotMatch(html,/Confirmed exact reference|Unconfirmed exact reference|Claim input/);
});
test('only a Liquid transaction present in current result opens investigation setup',async()=>{
  const opened=[],ctx=await show(report,{openLiquid:txid=>opened.push(txid)});
  await action('open-liquid',ctx,{dataset:{txid:other}});await action('open-liquid',ctx,{dataset:{txid:liquid}});assert.deepEqual(opened,[liquid]);
});
test('result values are escaped and unsafe download paths are neither linked nor downloaded',async()=>{
  const saved=structuredClone(report);saved.notice='<script>bad</script>';saved.liquid_outputs[liquid][0].address='<img src=x>';
  saved.downloads=[{name:'matches.csv',url:'https://other.invalid/matches.csv'},{name:'matches.csv',url:'/files/pegin-matches/../secrets'}];
  await show(saved);const html=peginPanel(false);assert.doesNotMatch(html,/<script|<img|https:\/\/other|\/\.\.\/secrets/);assert.match(html,/&lt;script&gt;/);
});
test('failed result fetch can retry current task directly without a search library',async()=>{
  let fails=true;const paths=[],ctx=context({get:async path=>{paths.push(path);if(fails)throw Error('Connection unavailable');return structuredClone(report);}});
  edit('txid',btc);await action('start',ctx);await peginComplete('match-job',{id},undefined,ctx);
  assert.match(peginPanel(false),/Retry loading current result/);assert.match(peginPanel(false),/Connection unavailable/);
  fails=false;await action('result',ctx);assert.match(peginPanel(false),/Confirmed exact reference/);assert.deepEqual(paths,Array(2).fill('/api/tools/pegin-matches/'+id));
});
test('an old in-flight completion cannot populate a reset session',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;}),ctx=context({get:async()=>gate});
  edit('txid',btc);await action('start',ctx);const completion=peginComplete('match-job',{id},undefined,ctx);
  forgetPegin();release(structuredClone(report));assert.equal(await completion,false);assert.doesNotMatch(peginPanel(false),/Confirmed exact reference/);
});
test('completion without a report releases the form and exposes failure without fetching history',async()=>{
  const ctx=context({get:async()=>{throw Error('must not fetch');}});edit('txid',btc);await action('start',ctx);
  await peginComplete('match-job',undefined,'Task cancelled.',ctx);assert.match(peginPanel(false),/Task cancelled/);
  assert.doesNotMatch(peginPanel(false),/data-action="pegin-start" disabled/);
});
test('current paused result can resume with its original network, window and batch identity',async()=>{
  const saved=batchReport([{...completedRows[0]}, {...completedRows[1],status:'unprocessed'}],'paused');saved.query={...saved.query,start_minutes:-12,end_minutes:120,public:false};
  const starts=[],ctx=await show(saved,{start:async body=>{starts.push(body);return 'match-job';}});
  edit('bridge','pegout');edit('network','liquid');await action('continue',ctx);
  assert.deepEqual(starts.at(-1),{...settings,mode:'batch',txids:[btc,other],start_minutes:-12,end_minutes:120,public:false,resume_report:id});
});
test('CSV results auto-download exactly once after every row is processed, including unmatched and row errors',async()=>{
  const rows=[...completedRows,{bitcoin_txid:liquid,status:'error',stop_reason:'lookup_failed',match_count:0}],saved=batchReport(rows),downloads=[],paths=[];
  const ctx=context({get:async path=>{paths.push(path);return saved;},download:url=>downloads.push(url)});
  await action('mode-batch',ctx);await upload('Bitcoin txid\n'+btc+'\n'+other+'\n'+liquid+'\n');await action('start',ctx);
  await peginComplete('match-job',{id},undefined,ctx);await peginComplete('match-job',{id},undefined,ctx);
  peginPanel(false);peginPanel(false);assert.deepEqual(downloads,[report.downloads[0].url]);assert.equal(paths.length,1);
  const html=peginPanel(false);assert.match(html,/CSV batch results/);assert.match(html,/no_match/);assert.match(html,/lookup failed/);assert.match(html,/Download matches.csv/);
});
test('concurrent duplicate completion signals cannot trigger a second download',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;}),downloads=[];
  const ctx=context({get:async()=>gate,download:url=>downloads.push(url)});await startBatch(ctx);
  const first=peginComplete('match-job',{id},undefined,ctx),second=peginComplete('match-job',{id},undefined,ctx);
  release(batchReport(completedRows));await Promise.all([first,second]);assert.equal(downloads.length,1);
});
test('paused, unprocessed or cancelled batches never auto-download and still expose partial CSV',async()=>{
  for(const kind of ['unprocessed','paused','cancelled']) {
    forgetPegin();const downloads=[],saved=batchReport([{...completedRows[0]},{...completedRows[1],status:kind}],kind==='unprocessed'?'partial':kind);
    const ctx=context({get:async()=>saved,download:url=>downloads.push(url)});await startBatch(ctx);
    await peginComplete('match-job',{id},kind==='cancelled'?'Task cancelled.':undefined,ctx);
    assert.equal(downloads.length,0);assert.match(peginPanel(false),/Download matches.csv/);
  }
});
test('browser download failure shows fallback and a re-render does not repeat the attempt',async()=>{
  let attempts=0;const ctx=context({get:async()=>batchReport(completedRows),download(){attempts++;throw Error('Blocked');}});
  await startBatch(ctx);await peginComplete('match-job',{id},undefined,ctx);peginPanel(false);
  assert.equal(attempts,1);assert.match(peginPanel(false),/browser blocked the automatic download/);assert.match(peginPanel(false),/Download matches.csv/);
});
test('batch result summary derives counterpart links and preserves per-row search coverage',async()=>{
  const saved=batchReport(completedRows.map(row=>({...row})));saved.results[1].coverage={window:{start_time:0,end_time:3600,complete:true,start_height:10,end_height:20}};
  await show(saved);const html=peginPanel(false),table=html.match(/<table>[\s\S]*?<\/table>/)[0];
  assert.match(table,new RegExp('https://blockstream.info/liquid/tx/'+liquid));assert.match(table,/no_match/);
  assert.match(html,/1970-01-01T00:00:00.000Z to 1970-01-01T01:00:00.000Z/);assert.match(html,/Complete bounded coverage/);
});

test('output checkbox cards retain exact values and selecting none searches any output',async()=>{
  const loads=[],starts=[],ctx=context({loadOutputs:async body=>{loads.push(body);return 'outputs-job';},start:async body=>{starts.push(body);return 'match-job';}});
  edit('txid',btc.toUpperCase());await action('load-outputs',ctx);assert.deepEqual(loads,[{txid:btc,public:true,network:'bitcoin',bridge:'pegin'}]);
  assert.equal(peginOutputsComplete('other',inspection()),false);assert.equal(peginOutputsComplete('outputs-job',inspection()),true);
  const html=peginPanel(false);assert.match(html,/Any Bitcoin output/);assert.match(html,/0\.60000001 BTC \(60000001 sats\)/);assert.match(html,/id="pegin-output-2" value="2" disabled/);
  assert.doesNotMatch(html,/id="pegin-output-\d+"[^>]* checked/);await action('start',ctx);assert.equal(starts[0].vout,undefined);
});
test('output multi-selection filters invalid and unspendable choices and clears to any',async()=>{
  const starts=[],ctx=context({start:async body=>{starts.push(body);return 'match-job';}});edit('txid',btc);await action('load-outputs',ctx);peginOutputsComplete('outputs-job',inspection());
  check(1);check(0);check(2);check(99);await action('start',ctx);assert.deepEqual(starts[0].vouts,[0,1]);
  await peginComplete('match-job',{id},undefined,ctx);check(0,false);await action('start',ctx);assert.equal(starts[1].vout,1);
  await peginComplete('match-job',{id},undefined,ctx);await action('any-output',ctx);await action('start',ctx);assert.equal(starts[2].vout,undefined);
});
test('Liquid peg-out OP_RETURN requests can be checked while ordinary outputs cannot',async()=>{
  edit('bridge','pegout');edit('network','liquid');edit('txid',liquid);await action('load-outputs');
  const data=inspection('liquid',liquid);data.transactions[0].outputs[2]={...data.transactions[0].outputs[2],selectable:true,is_pegout:true,reason:null,pegout_address:'bc1-payout-destination'};
  peginOutputsComplete('outputs-job',data);let html=peginPanel(false);assert.match(html,/id="pegin-output-2" value="2"\/>/);assert.match(html,/id="pegin-output-0" value="0" disabled/);assert.match(html,/Bitcoin destination: bc1-payout-destination/);
  check(2);check(0);const starts=[];await action('start',context({start:async body=>{starts.push(body);return 'match-job';}}));assert.equal(starts[0].vout,2);
});
test('Liquid peg-in outputs are informational and cannot allocate a Bitcoin claim to an output',async()=>{
  edit('network','liquid');edit('txid',liquid);await action('load-outputs');peginOutputsComplete('outputs-job',inspection('liquid',liquid));
  const html=peginPanel(false);assert.match(html,/Liquid claim outputs \(informational\)/);assert.match(html,/Peg-in references are transaction inputs/);assert.doesNotMatch(html,/id="pegin-output-\d+"/);
  const starts=[];check(0);await action('start',context({start:async body=>{starts.push(body);return 'match-job';}}));assert.deepEqual(starts,[{...settings,network:'liquid',txid:liquid}]);
});
test('changing source, transfer type, API or hash invalidates outstanding output results',async()=>{
  for(const change of [['network','liquid'],['bridge','pegout'],['txid',other],['public',null]]) {
    forgetPegin();edit('txid',btc);await action('load-outputs');if(change[0]==='public')peginInput({id:'pegin-field-public',checked:false});else edit(...change);
    assert.equal(peginOutputsComplete('outputs-job',inspection()),false);assert.doesNotMatch(peginPanel(false),/id="pegin-output-picker"/);
  }
});
test('lookup admission waits for exact job id and discards stale hash results',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;}),ctx=context({loadOutputs:async()=>gate});edit('txid',btc);const loading=action('load-outputs',ctx);
  peginOutputsComplete('wrong',undefined,'Other failure');peginOutputsComplete('fast',inspection());release('fast');await loading;
  assert.match(peginPanel(false),/id="pegin-output-picker"/);assert.doesNotMatch(peginPanel(false),/Other failure/);
  await action('load-outputs');edit('txid',other);edit('txid',btc);assert.equal(peginOutputsComplete('outputs-job',inspection()),false);
});
test('malformed output responses are rejected and lookup errors are retryable',async()=>{
  edit('txid',btc);
  for(const result of [inspection('liquid'),inspection('bitcoin',other),{blockchain:'bitcoin',transactions:[{txid:btc,outputs:[{vout:-1}]}]}]) {
    await action('load-outputs');peginOutputsComplete('outputs-job',result);assert.match(peginPanel(false),/did not return valid outputs/);
  }
  await action('load-outputs');peginOutputsComplete('outputs-job',undefined,'Lookup cancelled.');assert.match(peginPanel(false),/Lookup cancelled/);
  assert.doesNotMatch(peginPanel(false),/data-action="pegin-load-outputs" disabled/);
  await action('load-outputs',context({loadOutputs:async()=>null}));assert.match(peginPanel(false),/could not start/);
});
test('matching and output tasks mutually block additional work without hiding current results',async()=>{
  let starts=0,loads=0;edit('txid',btc);await action('load-outputs');await action('start',context({start:async()=>{starts++;return 'x';}}));assert.equal(starts,0);
  peginOutputsComplete('outputs-job',inspection());await action('start');await action('load-outputs',context({loadOutputs:async()=>{loads++;return 'x';}}));assert.equal(loads,0);
});

test('CSV supports Bitcoin and Liquid headers independently and generic or ambiguous headers require a choice',()=>{
  assert.deepEqual(parsePeginCsv('Liquid txid\n'+liquid+'\n','liquid').txids,[liquid]);
  assert.equal(parsePeginCsv('Bitcoin txid,Liquid txid\n'+btc+','+liquid+'\n','liquid').selectedColumn,1);
  assert.equal(parsePeginCsv('Liquid txid\n'+liquid+'\n').selectedColumn,null);
  const generic=parsePeginCsv('Transaction hash,Reference\n'+btc+','+liquid+'\n');assert.equal(generic.selectedColumn,null);assert.deepEqual(reviewPeginCsv(generic,0).txids,[btc]);
  assert.equal(parsePeginCsv('Bitcoin txid,BTC hash\n'+btc+','+liquid+'\n').selectedColumn,null);assert.throws(()=>reviewPeginCsv(generic,9));
  assert.deepEqual(parsePeginCsv(btc+'\r\n'+liquid+'\r\n').txids,[btc,liquid]);
});
test('CSV handles BOM, CRLF, quotes and multiline fields with physical row numbers and deduplication',()=>{
  const csv=parsePeginCsv('\uFEFF"Bitcoin transaction hash","Note"\r\n"'+btc.toUpperCase()+'","line one\r\nline two"\r\n"'+liquid+'","a ""quote"", comma"\r\n"'+btc+'","again"\r\n');
  assert.equal(csv.selectedColumn,0);assert.deepEqual(csv.txids,[btc,liquid]);assert.deepEqual(csv.rows.map(row=>row.row),[2,4,5]);
  assert.equal(csv.rows[1].cells[1],'a "quote", comma');assert.deepEqual(csv.duplicates,[{row:5,firstRow:2,txid:btc}]);assert.deepEqual(csv.errors,[]);
});
test('malformed CSV rows and quote syntax fail locally before any task',async()=>{
  const csv=parsePeginCsv('Bitcoin txid,Note\n'+btc+',ok\ninvalid,bad\n'+liquid+',extra,value\n');assert.deepEqual(csv.errors.map(error=>error.row),[3,4]);
  for(const text of ['Bitcoin txid,Note\n'+btc+',"unclosed','Bitcoin txid,Note\n'+btc+',bad"quote','Bitcoin txid,Note\n'+btc+',"closed"x'])assert.throws(()=>parsePeginCsv(text),/Row 2:/);
  let starts=0;await action('mode-batch');await upload('Bitcoin txid\ninvalid\n');await action('start',context({start:async()=>{starts++;return 'x';}}));
  assert.equal(starts,0);assert.match(peginPanel(false),/Row 2:/);assert.match(peginPanel(false),/data-action="pegin-start" disabled/);
});
test('CSV limits count UTF-8 bytes and unique hashes, not duplicate rows',()=>{
  assert.throws(()=>parsePeginCsv('é'.repeat(524289)),/1 MiB/);const hashes=Array.from({length:501},(_,index)=>index.toString(16).padStart(64,'0'));
  assert.equal(parsePeginCsv('Bitcoin txid\n'+hashes.slice(0,500).join('\n')).errors.length,0);assert.match(parsePeginCsv('Bitcoin txid\n'+hashes.join('\n')).errors[0].message,/at most 500/);
  const repeated=parsePeginCsv('Bitcoin txid\n'+Array(510).fill(btc).join('\n'));assert.equal(repeated.errors.length,0);assert.equal(repeated.duplicates.length,509);
});
test('CSV submits only selected unique hashes for chosen bridge and source',async()=>{
  const starts=[],ctx=context({start:async body=>{starts.push(body);return 'match-job';}});edit('bridge','pegout');edit('network','liquid');await action('mode-batch',ctx);
  await upload('Liquid transaction,Private note\n'+liquid+',local note\n'+other+',local note\n'+liquid+',duplicate\n');assert.equal(starts.length,0);
  assert.match(peginPanel(false),/2 unique hashes · 1 duplicate rows · 0 errors/);await action('start',ctx);
  assert.deepEqual(starts,[{...settings,bridge:'pegout',network:'liquid',mode:'batch',txids:[liquid,other]}]);assert.equal(JSON.stringify(starts).includes('local note'),false);
});
test('CSV labels are escaped; column selection enables generic headers',async()=>{
  const starts=[];await action('mode-batch');await upload('Txid,"<img src=x>"\n'+btc+',note\n');assert.match(peginPanel(false),/Choose a column/);assert.match(peginPanel(false),/&lt;img/);assert.doesNotMatch(peginPanel(false),/<img/);
  edit('csv-column','0');await action('start',context({start:async body=>{starts.push(body);return 'match-job';}}));assert.deepEqual(starts[0].txids,[btc]);
});
test('stale file reads, mode changes and source changes cannot replace a newer CSV',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});await action('mode-batch');const old=peginFile({files:[{name:'old.csv',size:10,text:async()=>gate}]},()=>{});
  await upload('Bitcoin txid\n'+other+'\n','new.csv');release('Bitcoin txid\n'+btc+'\n');await old;assert.match(peginPanel(false),/new.csv/);assert.doesNotMatch(peginPanel(false),/old.csv/);
  edit('network','liquid');assert.doesNotMatch(peginPanel(false),/new.csv/);assert.match(peginPanel(false),/data-action="pegin-start" disabled/);
  let reads=0;await peginFile({files:[{name:'large.csv',size:1048577,text:async()=>{reads++;return '';}}]},()=>{});assert.equal(reads,0);assert.match(peginPanel(false),/1 MiB/);
});

test('a truncated terminal report cannot auto-download before every submitted hash is accounted for',async()=>{
  const downloads=[],ctx=context({get:async()=>batchReport([completedRows[0]]),download:url=>downloads.push(url)});
  await startBatch(ctx);await peginComplete('match-job',{id},undefined,ctx);assert.deepEqual(downloads,[]);assert.match(peginPanel(false),/Download matches.csv/);
});

for(const bridge of ['pegin','pegout'])for(const network of ['bitcoin','liquid']) {
  test('current single '+bridge+' '+network+' Continue resumes its exact report and query',async()=>{
    const txid=network==='bitcoin'?btc:liquid,vouts=bridge==='pegin'&&network==='liquid'?[]:[2];
    const saved={...structuredClone(report),status:'paused',query:{bridge,network,mode:'single',txid,vouts,
      max_pages:12,start_minutes:-8,end_minutes:1440,public:false}};
    const starts=[],ctx=await show(saved,{start:async body=>{starts.push(body);return 'match-job';}});
    edit('start_minutes','-300');edit('end_minutes','600');edit('txid',other);await action('continue',ctx);
    assert.deepEqual(starts.at(-1),{...settings,bridge,network,txid,max_pages:12,start_minutes:-8,end_minutes:1440,public:false,
      ...(vouts.length?{vout:2}:{}),resume_report:id});
    await peginComplete('match-job',{id},undefined,ctx);await action('start',ctx);assert.equal(starts.at(-1).resume_report,undefined);
  });
}

test('bridge and network toggle buttons preserve source transitions and busy guards',async()=>{
  const ctx=context();edit('txid',btc);
  await action('bridge-pegout',ctx);
  assert.match(peginPanel(false),/data-action="pegin-bridge-pegout" aria-pressed="true"/);
  assert.match(peginPanel(false),/value="-4320"/);
  await action('network-liquid',ctx);
  assert.match(peginPanel(false),/data-action="pegin-network-liquid" aria-pressed="true"/);
  assert.match(peginPanel(false),/Liquid transaction hash/);
  assert.doesNotMatch(peginPanel(false),new RegExp('value="'+btc+'"'));
  await action('network-bitcoin',{...ctx,busy:true});
  assert.match(peginPanel(true),/data-action="pegin-network-liquid" aria-pressed="true" disabled/);
});
