import {MAX_PEGIN_CSV_BYTES, parsePeginCsv, reviewPeginCsv, type PeginCsv} from './pegin-matching.ts';

/** A workspace lookup draft and its current result live only in this browser session. */
type ChainStatus = {confirmed: boolean; block_height?: number; block_time?: number};
type Node = {public_key: string; alias?: string | null; explorer_url?: string};
type Match = {role: 'funding' | 'closure'; classification: string; channel_id: string;
  short_channel_id?: string | null; funding_txid: string; funding_vout: number;
  capacity_sat: number | string | null; funding_status?: ChainStatus | null;
  nodes: Node[]; closing_txid: string | null; bitcoin_input_index: number | null;
  channel_url?: string; observed_at?: string};
type Source = {provider?: string; endpoint?: string; url?: string; observed_at?: string};
type Finding = {role:'input'|'output'; index:number; funding_txid:string; funding_vout:number;
  spending_txid:string|null; spending_input_index:number|null; value_sat:number|null;
  status:'candidate'|'hidden'|'unsupported'|'unspent'|'unavailable'; script_type:string;
  template:string|null; branch:'claim'|'refund'|'unknown'; payment_hash:string|null; hash_algorithm:string|null;
  summary:string; evidence:string[]};
type Inspection = {status:'complete'|'partial'|'error'; findings:Finding[]; checked_inputs:number;
  checked_outputs:number; total_inputs:number; total_outputs:number; reason:string|null};
type Verification = {status:'verified'|'unmatched'|'unavailable'; provider:'Boltz'; swap_id:string;
  swap_type:'submarine'; transaction_id:string|null; provider_status:string|null; summary:string; evidence:string[]};
type IndexObservations = {attempts:number; positive:number; empty:number; consistent:boolean;
  samples:{result:'channels'|'empty'; channel_count:number; observed_at:string}[]};
type Report = {schema_version: 1|2; kind: 'lightning_lookup'; query: {bitcoin_txid: string};
  status: 'complete' | 'error' | 'paused' | 'cancelled'; classifications: string[];
  bitcoin: {txid: string; status: ChainStatus; explorer_url?: string} | null;
  inspection?:Inspection|null; provider_verification?:Verification|null; index_observations?:IndexObservations;
  matches: Match[]; sources: Source[]; notice: string; stop_reason?: string | null; error?: string};
type BatchReport = {schema_version:1; kind:'lightning_batch'; query:{bitcoin_txids:string[]}; status:'complete';
  reports:Report[]; summary:{total:number; matched:number; no_matches:number; incomplete:number}};
type Completion = {result: unknown; error?: string};
type Request = {jobId: string | null; mode:'single'|'batch'; txids:string[]; earlyCompletions: Map<string, Completion>};
type Draft = {mode:'single'|'batch'; txid: string; boltzSwapId:string; advancedOpen:boolean; csv:PeginCsv|null;
  csvFilename:string; pending: boolean; request: Request | null; selected: Report | BatchReport | null; error: string};
type Context = {busy: boolean; render: () => void; start: (body: Record<string, unknown>) => Promise<string | null>;
  download:(name:string, text:string)=>void};
let workspaceDraft: Draft | undefined;
const MAX_LIGHTNING_CSV_TXIDS = 100;
const hash = /^[0-9a-f]{64}$/;
const pubkey = /^(?:02|03)[0-9a-f]{64}$/;
const classifications = ['indexed_channel_funding', 'indexed_channel_closure', 'no_indexed_channel_found'];
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  character => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]!));
const disabled = (value: boolean): string => value ? ' disabled' : '';
const index = (value: unknown): value is number => Number.isInteger(value) && Number(value) >= 0 && Number(value) < 0x100000000;
function draftFor(): Draft {
  return workspaceDraft ??= {mode:'single',txid:'',boltzSwapId:'',advancedOpen:false,csv:null,csvFilename:'',pending:false,request:null,selected:null,error:''};
}
/** Used when explicitly resetting the session; closing a tab must preserve this draft. */
export function forgetLightning(): void {workspaceDraft = undefined;}
export function lightningInput(element: HTMLInputElement, render?: () => void): boolean {
  if (!['lightning-field-txid','lightning-field-boltz_swap_id','lightning-field-csv-column'].includes(element.id)) return false;
  const draft = draftFor();
  if (!draft.pending && !draft.request) {
    const clearError = !!draft.error;
    if (element.id === 'lightning-field-txid') draft.txid = element.value;
    else if (element.id === 'lightning-field-boltz_swap_id') draft.boltzSwapId = element.value;
    else if (draft.csv) draft.csv = lightningCsv(reviewPeginCsv(draft.csv, element.value === '' ? null : Number(element.value)));
    draft.error = '';
    if (clearError) render?.();
  }
  return true;
}

function lightningCsv(csv:PeginCsv):PeginCsv {
  const errors=csv.errors.filter(row=>row.message !== 'Choose at most '+MAX_LIGHTNING_CSV_TXIDS+' unique transaction hashes per batch.');
  if(csv.txids.length>MAX_LIGHTNING_CSV_TXIDS)errors.push({row:0,message:'Choose at most '+MAX_LIGHTNING_CSV_TXIDS+' unique transaction hashes per batch.'});
  return {...csv,errors};
}

/** Read and review a CSV locally. Its transaction hashes are submitted only after the user starts the check. */
export async function lightningFile(element:HTMLInputElement, render:()=>void, busy=false):Promise<boolean>{
  if(element.id!=='lightning-csv-file')return false;
  const draft=draftFor(),file=element.files?.[0];
  if(busy||draft.pending||draft.request)return true;
  draft.error='';draft.csv=null;draft.csvFilename='';
  if(!file){render();return true;}
  try{
    if(file.size>MAX_PEGIN_CSV_BYTES)throw Error('CSV files must be 1 MiB or smaller.');
    draft.csv=lightningCsv(parsePeginCsv(await file.text(),'bitcoin'));
    draft.csvFilename=file.name;
  }catch(error){draft.error=error instanceof Error?error.message:'The CSV could not be read.';}
  render();return true;
}
export function lightningToggle(element: HTMLDetailsElement): boolean {
  if (element.id !== 'lightning-advanced') return false;
  draftFor().advancedOpen = element.open;
  return true;
}
function inspectionValid(value: unknown, txid:string): value is Inspection {
  if (!value || typeof value !== 'object') return false;
  const v = value as Inspection;
  if (!['complete','partial','error'].includes(v.status) || !Array.isArray(v.findings) || v.findings.length > 96
      || ![v.checked_inputs,v.checked_outputs,v.total_inputs,v.total_outputs].every(index)
      || v.checked_inputs > v.total_inputs || v.checked_outputs > v.total_outputs
      || v.reason !== null && typeof v.reason !== 'string') return false;
  return v.findings.every(f => f && ['input','output'].includes(f.role) && index(f.index) && index(f.funding_vout)
    && typeof f.funding_txid === 'string' && hash.test(f.funding_txid)
    && (f.spending_txid === null || typeof f.spending_txid === 'string' && hash.test(f.spending_txid))
    && (f.spending_input_index === null || index(f.spending_input_index))
    && (f.value_sat === null || Number.isSafeInteger(f.value_sat) && f.value_sat >= 0)
    && ['candidate','hidden','unsupported','unspent','unavailable'].includes(f.status)
    && ['claim','refund','unknown'].includes(f.branch) && typeof f.summary === 'string' && typeof f.script_type === 'string'
    && (f.template === null || typeof f.template === 'string') && (f.payment_hash === null || typeof f.payment_hash === 'string')
    && (f.hash_algorithm === null || typeof f.hash_algorithm === 'string')
    && Array.isArray(f.evidence) && f.evidence.every(x => typeof x === 'string')
    && (f.role === 'input' ? f.spending_txid === txid && f.spending_input_index === f.index
      : f.funding_txid === txid && f.funding_vout === f.index));
}
function verificationValid(value: unknown, txid:string): value is Verification {
  if (!value || typeof value !== 'object') return false;
  const v = value as Verification;
  return ['verified','unmatched','unavailable'].includes(v.status) && v.provider === 'Boltz' && v.swap_type === 'submarine'
    && typeof v.swap_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(v.swap_id)
    && (v.transaction_id === null || typeof v.transaction_id === 'string' && hash.test(v.transaction_id))
    && (v.status !== 'verified' || v.transaction_id === txid)
    && (v.provider_status === null || typeof v.provider_status === 'string') && typeof v.summary === 'string'
    && Array.isArray(v.evidence) && v.evidence.every(x => typeof x === 'string');
}
function indexObservationsValid(value: unknown): value is IndexObservations {
  if (!value || typeof value !== 'object') return false;
  const v=value as IndexObservations;
  if (![v.attempts,v.positive,v.empty].every(index) || v.attempts > 3 || typeof v.consistent !== 'boolean'
      || !Array.isArray(v.samples) || v.samples.length !== v.attempts
      || v.positive+v.empty !== v.attempts || v.consistent !== !(v.positive && v.empty)) return false;
  let positive=0;
  for (const sample of v.samples) {
    if (!sample || !['channels','empty'].includes(sample.result) || !index(sample.channel_count)
        || typeof sample.observed_at !== 'string' || !sample.observed_at) return false;
    if ((sample.result === 'empty') !== (sample.channel_count === 0)) return false;
    if (sample.result === 'channels') positive++;
  }
  return positive === v.positive;
}
function chainStatusValid(value: unknown): value is ChainStatus {
  if (!value || typeof value !== 'object') return false;
  const status = value as ChainStatus;
  return typeof status.confirmed === 'boolean' && (status.block_height == null || index(status.block_height))
    && (status.block_time == null || Number.isFinite(status.block_time));
}
function matchValid(value: unknown, txid: string): value is Match {
  if (!value || typeof value !== 'object') return false;
  const match = value as Match, funding = match.role === 'funding';
  return (funding || match.role === 'closure')
    && match.classification === (funding ? 'indexed_channel_funding' : 'indexed_channel_closure')
    && typeof match.channel_id === 'string' && !!match.channel_id
    && (match.short_channel_id == null || typeof match.short_channel_id === 'string')
    && typeof match.funding_txid === 'string' && hash.test(match.funding_txid) && index(match.funding_vout)
    && (match.capacity_sat == null || typeof match.capacity_sat === 'string' && /^\d+$/.test(match.capacity_sat)
      || typeof match.capacity_sat === 'number' && Number.isSafeInteger(match.capacity_sat) && match.capacity_sat >= 0)
    && (match.funding_status == null || chainStatusValid(match.funding_status))
    && Array.isArray(match.nodes) && match.nodes.every(node => node && typeof node.public_key === 'string'
      && pubkey.test(node.public_key) && (node.alias == null || typeof node.alias === 'string'))
    && (funding ? match.funding_txid === txid && match.bitcoin_input_index == null
      && (match.closing_txid == null || typeof match.closing_txid === 'string' && hash.test(match.closing_txid))
      : match.closing_txid === txid && index(match.bitcoin_input_index));
}
function reportFor(value: unknown, txid: string): Report | null {
  if (!value || typeof value !== 'object') return null;
  const report = value as Report;
  if (![1,2].includes(report.schema_version) || report.kind !== 'lightning_lookup' || report.query?.bitcoin_txid !== txid
      || !['complete','error','paused','cancelled'].includes(report.status)
      || !Array.isArray(report.classifications) || report.classifications.some(value => !classifications.includes(value))
      || !Array.isArray(report.matches) || !report.matches.every(match => matchValid(match, txid))
      || !Array.isArray(report.sources) || report.sources.some(source => !source || typeof source !== 'object')
      || typeof report.notice !== 'string' || (report.error != null && typeof report.error !== 'string')) return null;
  if (report.schema_version === 2 && (!inspectionValid(report.inspection, txid)
      || report.provider_verification != null && !verificationValid(report.provider_verification, txid)
      || report.index_observations != null && !indexObservationsValid(report.index_observations))) return null;
  if (report.bitcoin && (report.bitcoin.txid !== txid || !chainStatusValid(report.bitcoin.status))) return null;
  if (report.status === 'complete') {
    if (!report.bitcoin) return null;
    const expected = new Set(report.matches.length ? report.matches.map(match => match.classification) : ['no_indexed_channel_found']);
    if (new Set(report.classifications).size !== expected.size || report.classifications.some(value => !expected.has(value))) return null;
  } else if (report.classifications.includes('no_indexed_channel_found')) return null;
  return report;
}
function evidenceFound(report:Report):boolean {
  return report.matches.length>0 || !!report.inspection?.findings.some(row=>row.status==='candidate')
    || report.provider_verification?.status==='verified';
}
function outcome(report:Report):'matched'|'no_matches'|'incomplete' {
  return evidenceFound(report)?'matched':report.status==='complete'?'no_matches':'incomplete';
}
function batchFor(value:unknown,txids:string[]):BatchReport|null {
  if(!value||typeof value!=='object')return null;
  const batch=value as BatchReport;
  if(batch.schema_version!==1||batch.kind!=='lightning_batch'||batch.status!=='complete'
      ||!Array.isArray(batch.query?.bitcoin_txids)||batch.query.bitcoin_txids.length!==txids.length
      ||batch.query.bitcoin_txids.some((value,index)=>value!==txids[index])
      ||!Array.isArray(batch.reports)||batch.reports.length!==txids.length)return null;
  const reports:Report[]=[];
  for(let i=0;i<txids.length;i++){const report=reportFor(batch.reports[i],txids[i]);if(!report)return null;reports.push(report);}
  const states=reports.map(outcome), expected={total:reports.length,matched:states.filter(x=>x==='matched').length,
    no_matches:states.filter(x=>x==='no_matches').length,incomplete:states.filter(x=>x==='incomplete').length};
  if(!batch.summary||Object.keys(expected).some(key=>batch.summary[key as keyof typeof expected]!==expected[key as keyof typeof expected]))return null;
  return {...batch,reports,summary:expected};
}
function csvCell(value:unknown):string {
  const text=String(value??'');return /[",\r\n]/.test(text)?'"'+text.replace(/"/g,'""')+'"':text;
}
function lightningBatchCsv(batch:BatchReport):string {
  const columns=['bitcoin_transaction','result','lookup_status','classifications','indexed_channel_matches','channel_roles',
    'channel_ids','short_channel_ids','funding_outpoints','capacities_sats','public_nodes','swap_contract_candidates',
    'boltz_verification','error'];
  const rows=batch.reports.map(report=>{
    const candidates=report.inspection?.findings.filter(row=>row.status==='candidate')||[];
    const nodes=[...new Set(report.matches.flatMap(match=>match.nodes.map(node=>(node.alias?node.alias+' ':'')+node.public_key)))];
    return [report.query.bitcoin_txid,outcome(report),report.status,report.classifications.join('|'),report.matches.length,
      report.matches.map(match=>match.role).join('|'),report.matches.map(match=>match.channel_id).join('|'),
      report.matches.map(match=>match.short_channel_id||'').join('|'),
      report.matches.map(match=>match.funding_txid+':'+match.funding_vout).join('|'),
      report.matches.map(match=>match.capacity_sat??'').join('|'),nodes.join('|'),candidates.length,
      report.provider_verification?.status||'',report.error||report.stop_reason||''];
  });
  return [columns,...rows].map(row=>row.map(csvCell).join(',')).join('\r\n')+'\r\n';
}
/** Completion must belong to the job admitted by this session, including a fast admission race. */
export function lightningComplete(jobId: string, result: unknown, error: string | undefined, context: Context): boolean {
  const draft = workspaceDraft, request = draft?.request;
  if (!draft || !request) return false;
  if (request.jobId === null) {request.earlyCompletions.set(jobId, {result, error}); return false;}
  if (request.jobId !== jobId) return false;
  const selected = request.mode==='batch' ? batchFor(result,request.txids) : reportFor(result,request.txids[0]);
  draft.request = null;
  draft.pending = false;
  if (selected) {
    if(selected.kind==='lightning_batch'){
      draft.selected=selected;draft.error=error||'';
      if(!error)try{context.download('lightning-network-checks.csv',lightningBatchCsv(selected));}
      catch(downloadError){draft.error=downloadError instanceof Error?downloadError.message:'The result CSV could not be downloaded.';}
    } else {
      draft.selected = error && selected.status === 'complete'
        ? {...selected, status:'error', classifications:selected.classifications.filter(value => value !== 'no_indexed_channel_found')}
        : selected;
      draft.error = error || (selected.status === 'complete' ? '' : selected.error || 'The lookup did not finish. Check again to retry.');
    }
  } else {
    draft.selected = null;
    draft.error = error || (request.mode==='single'
      ? 'The task did not return a valid Lightning lookup for this transaction. Check again to retry.'
      : 'The task did not return valid Lightning results for this CSV. Check again to retry.');
  }
  context.render();
  return true;
}
export async function lightningAction(action: string, context: Context): Promise<boolean> {
  const draft = draftFor();
  if(action==='lightning-mode-single'||action==='lightning-mode-batch'){
    if(!context.busy&&!draft.pending&&!draft.request){draft.mode=action.endsWith('batch')?'batch':'single';draft.error='';context.render();}return true;
  }
  if(action==='lightning-download-csv'){
    if(draft.selected?.kind==='lightning_batch')context.download('lightning-network-checks.csv',lightningBatchCsv(draft.selected));return true;
  }
  if (action !== 'lightning-start') return false;
  if (context.busy || draft.pending || draft.request) return true;
  draft.error = '';
  let txids:string[],body:Record<string,unknown>;
  if(draft.mode==='batch'){
    if(!draft.csv){draft.error='Choose a CSV containing Bitcoin transaction hashes.';context.render();return true;}
    if(draft.csv.selectedColumn===null){draft.error='Choose the column containing Bitcoin transaction hashes.';context.render();return true;}
    if(draft.csv.errors.length){draft.error='Fix the CSV errors before starting the checks.';context.render();return true;}
    txids=draft.csv.txids;
    if(!txids.length){draft.error='The CSV does not contain any valid Bitcoin transaction hashes.';context.render();return true;}
    body={action:'lightning-lookup',txids};
  }else{
    const txid = draft.txid.trim().toLowerCase();
    if (!hash.test(txid)) {draft.error = 'Enter a full 64-character Bitcoin transaction hash.'; context.render(); return true;}
    const swapId = draft.boltzSwapId.trim();
    if (swapId && !/^[A-Za-z0-9_-]{1,128}$/.test(swapId)) {draft.error = 'Enter a valid Boltz swap ID.'; context.render(); return true;}
    txids=[txid];body={action:'lightning-lookup',txid,...(swapId?{boltz_swap_id:swapId}:{})};
  }
  const request: Request = {jobId:null,mode:draft.mode,txids,earlyCompletions:new Map()};
  draft.request = request;
  draft.pending = true;
  context.render();
  try {
    const jobId = await context.start(body);
    if (workspaceDraft !== draft || draft.request !== request) return true;
    if (!jobId) {draft.request = null; draft.error = 'The lookup could not start. Try again when the current task finishes.';}
    else {
      request.jobId = jobId;
      draft.selected = null;
      const completion = request.earlyCompletions.get(jobId);
      request.earlyCompletions.clear();
      if (completion) lightningComplete(jobId, completion.result, completion.error, context);
    }
  } catch (error) {
    if (workspaceDraft === draft && draft.request === request) {
      draft.request = null;
      draft.error = error instanceof Error ? error.message : 'The Lightning lookup could not start.';
    }
  } finally {if (workspaceDraft === draft) {draft.pending = false; context.render();}}
  return true;
}
function txLink(txid: string, label = txid): string {
  return hash.test(txid) ? '<a class="mono" href="https://mempool.space/tx/'+esc(txid)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+'</a>' : esc(label);
}
function channelLink(match: Match): string {
  const identifier = /^[0-9]+$/.test(match.channel_id) ? match.channel_id
    : match.short_channel_id && /^\d+(?:x|:)\d+(?:x|:)\d+$/.test(match.short_channel_id) ? match.short_channel_id : '';
  return identifier ? '<a href="https://mempool.space/lightning/channel/'+esc(identifier)+'" target="_blank" rel="noopener noreferrer">'+esc(match.channel_id)+'</a>' : esc(match.channel_id);
}
function amount(value: Match['capacity_sat']): string {
  if (value == null) return 'Unavailable';
  const sats = BigInt(value), unit = 100000000n;
  return String(sats / unit)+'.'+String(sats % unit).padStart(8,'0')+' BTC ('+String(sats)+' sats)';
}
function statusText(status?: ChainStatus | null): string {
  if (!status) return 'Status unavailable';
  let text = status.confirmed ? 'Confirmed' : 'Unconfirmed';
  if (status.block_height != null) text += ' · Block '+status.block_height;
  if (status.block_time != null) {
    const date = new Date(status.block_time * 1000);
    if (Number.isFinite(date.getTime())) text += ' · '+date.toISOString().replace('T',' ').replace('.000Z',' UTC');
  }
  return esc(text);
}
function matchHtml(match: Match, report: Report): string {
  const funding = match.role === 'funding';
  return '<article class="pegin-match"><div class="pegin-match-heading"><h3>'+(funding?'Indexed channel funding':'Indexed channel funding spend')+'</h3></div><div class="pegin-pair">'
    +'<section class="pegin-transaction"><h4>'+(funding?'Bitcoin funding output':'Bitcoin spending input')+'</h4>'+txLink(report.query.bitcoin_txid)
    +'<dl><dt>'+(funding?'Funding output':'Spending input')+'</dt><dd>'+(funding?'vout '+match.funding_vout:'Input '+match.bitcoin_input_index+' (vin '+match.bitcoin_input_index+')')+'</dd>'
    +'<dt>Transaction status</dt><dd>'+statusText(report.bitcoin?.status)+'</dd><dt>Channel funding reference</dt><dd>'+txLink(match.funding_txid, match.funding_txid+':'+match.funding_vout)+'</dd>'
    +(!funding?'<dt>Funding transaction status</dt><dd>'+statusText(match.funding_status)+'</dd>':'')+'</dl>'
    +'<p class="artifact-note">'+(funding?'This exact output is indexed as the channel funding outpoint.':'This exact input spends the indexed channel funding outpoint. It may be a closure or splice; this lookup does not independently distinguish them.')+'</p></section>'
    +'<section class="pegin-transaction"><h4>Channel and public nodes</h4><dl><dt>Channel ID</dt><dd>'+channelLink(match)+'</dd>'
    +(match.short_channel_id?'<dt>Short channel ID</dt><dd class="mono">'+esc(match.short_channel_id)+'</dd>':'')
    +'<dt>Channel capacity</dt><dd>'+esc(amount(match.capacity_sat))+'</dd>'
    +match.nodes.map((node, position)=>'<dt>Public node '+(position+1)+'</dt><dd>'+(node.alias?esc(node.alias)+'<br/>':'')
      +'<a class="mono" href="https://mempool.space/lightning/node/'+esc(node.public_key)+'" target="_blank" rel="noopener noreferrer">'+esc(node.public_key)+'</a></dd>').join('')
    +(!match.nodes.length?'<dt>Public nodes</dt><dd>Unavailable in this index</dd>':'')+'</dl></section></div></article>';
}
function findingHtml(f: Finding): string {
  const label = f.status === 'candidate' ? 'Recognized swap-contract candidate' : f.status === 'hidden' ? 'Contract script hidden'
    : f.status === 'unspent' ? 'Unspent output' : f.status === 'unavailable' ? 'Inspection unavailable' : 'No supported template identified';
  return '<article class="pegin-transaction"><h4>'+label+'</h4><p>'+esc(f.role === 'input' ? 'Input '+f.index+' (vin '+f.index+')' : 'Output '+f.index+' (vout '+f.index+')')+'</p>'
    +'<p>'+esc(f.summary)+'</p><dl><dt>Funding outpoint</dt><dd>'+txLink(f.funding_txid, f.funding_txid+':'+f.funding_vout)+'</dd>'
    +'<dt>Output value</dt><dd>'+esc(amount(f.value_sat))+'</dd>'
    +(f.spending_txid ? '<dt>Spending transaction</dt><dd>'+txLink(f.spending_txid)+' · vin '+f.spending_input_index+'</dd>' : '')
    +'<dt>Script type</dt><dd>'+esc(f.script_type)+'</dd>'
    +(f.template ? '<dt>Compatible template</dt><dd>'+esc(f.template)+'</dd><dt>Witness branch</dt><dd>'+esc(f.branch)+'</dd>' : '')
    +(f.payment_hash ? '<dt>Contract hash ('+esc((f.hash_algorithm || '').toUpperCase())+')</dt><dd class="mono">'+esc(f.payment_hash)+'</dd>' : '')
    +'</dl>'+(f.evidence.length ? '<ul>'+f.evidence.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ul>':'')+'</article>';
}
function inspectionHtml(v?: Inspection|null): string {
  if (!v) return '';
  const candidates = v.findings.filter(f=>f.status === 'candidate');
  if (!candidates.length) return '';
  return '<section aria-label="Witness and direct spend inspection"><h3>Witness and direct spend inspection</h3>'
    +'<p>Checked '+v.checked_inputs+' of '+v.total_inputs+' inputs and '+v.checked_outputs+' of '+v.total_outputs+' outputs.</p>'
    +'<p>'+candidates.length+' contract candidate'+(candidates.length===1?'':'s')+'. Provider, direction, ownership and Lightning payment settlement are not established by these patterns.</p>'
    +candidates.map(findingHtml).join('')+'</section>';
}
function verificationHtml(v?: Verification|null): string {
  if (!v || v.status !== 'verified') return '';
  return '<section class="pegin-transaction"><h3>Provider-reported swap association</h3>'
    +'<p>'+esc(v.summary)+'</p><dl><dt>Provider</dt><dd>Boltz</dd><dt>Supplied submarine swap ID</dt><dd>'+esc(v.swap_id)+'</dd>'
    +(v.transaction_id ? '<dt>Reported funding transaction</dt><dd>'+txLink(v.transaction_id)+'</dd>' : '')
    +(v.provider_status ? '<dt>Provider-reported status</dt><dd>'+esc(v.provider_status)+'</dd>' : '')+'</dl>'
    +'<ul>'+v.evidence.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ul></section>';
}
function reportHtml(report: Report): string {
  const complete = report.status === 'complete';
  const candidates=report.inspection?.findings.filter(f=>f.status === 'candidate') || [];
  const found=report.matches.length > 0 || candidates.length > 0 || report.provider_verification?.status === 'verified';
  if (!found) return '<section class="pegin-results" aria-label="Current Lightning lookup result"><h3>'
    +(complete?'No matches found':'Lookup incomplete')+'</h3><p>Bitcoin transaction: '+txLink(report.query.bitcoin_txid)+'</p></section>';
  const observations=report.index_observations;
  const consistency=observations && !observations.consistent
    ? '<aside class="notice warning"><p>Channel-index responses were inconsistent. Mempool returned a channel in '+observations.positive
      +' of '+observations.attempts+' checks and an empty result in '+observations.empty
      +'. The channel shown below passed independent Bitcoin funding-output verification.</p></aside>'
    : '';
  return '<section class="pegin-results" aria-label="Current Lightning lookup result"><h3>Matches found</h3>'
    +'<p>Bitcoin transaction: '+txLink(report.query.bitcoin_txid)+'</p>'
    +consistency
    +(!complete?'<p>The channel-index portion of this lookup was incomplete.</p>':'')
    +(report.matches.length?'<p>'+report.matches.length+' indexed channel match'+(report.matches.length===1?'':'es')+'.</p>':'')
    +report.matches.map(match=>matchHtml(match,report)).join('')
    +inspectionHtml(report.inspection)+verificationHtml(report.provider_verification)
    +(report.sources.length?'<details class="pegin-coverage"><summary>Lookup sources</summary>'+report.sources.map(source=>'<p>'+esc(source.provider || 'Public Lightning index')
      +(source.endpoint?' · '+esc(source.endpoint):'')+(source.observed_at?' · Checked '+esc(source.observed_at):'')+'</p>').join('')+'</details>':'')+'</section>';
}
function csvReviewHtml(draft:Draft,locked:boolean):string {
  const csv=draft.csv;
  if(!csv)return '<p class="small muted">Upload a CSV with one Bitcoin transaction-hash column. A headerless single-column list is also accepted. Maximum 100 unique transactions.</p>';
  const column=csv.selectedColumn;
  return '<section class="pegin-csv-review"><h3>CSV review</h3><p>'+esc(draft.csvFilename)+' · '+csv.rows.length+' data row'+(csv.rows.length===1?'':'s')
    +' · '+csv.txids.length+' unique valid transaction'+(csv.txids.length===1?'':'s')+'</p>'
    +(csv.columns.length>1||column===null?'<label class="field"><span>Transaction-hash column</span><select id="lightning-field-csv-column"'+disabled(locked)+'>'
      +'<option value=""'+(column===null?' selected':'')+'>Choose a column</option>'+csv.columns.map((name,index)=>'<option value="'+index+'"'+(column===index?' selected':'')+'>'+esc(name)+'</option>').join('')+'</select></label>':'')
    +(csv.duplicates.length?'<p class="small muted">'+csv.duplicates.length+' duplicate row'+(csv.duplicates.length===1?' was':'s were')+' ignored.</p>':'')
    +(csv.errors.length?'<div class="alert error" role="alert"><div><strong>Fix the CSV before checking</strong><ul>'
      +csv.errors.slice(0,12).map(error=>'<li>'+(error.row?'Row '+error.row+': ':'')+esc(error.message)+'</li>').join('')
      +(csv.errors.length>12?'<li>'+esc(csv.errors.length-12)+' additional errors</li>':'')+'</ul></div></div>':'')+'</section>';
}
function batchHtml(batch:BatchReport):string {
  return '<section class="pegin-results" aria-label="Lightning CSV results"><div class="panel-head"><div><h3>CSV checks complete</h3><p>'
    +batch.summary.total+' transactions checked · '+batch.summary.matched+' with matches · '+batch.summary.no_matches+' with no matches'
    +(batch.summary.incomplete?' · '+batch.summary.incomplete+' incomplete':'')+'</p></div><button type="button" class="btn small" data-action="lightning-download-csv">Download results CSV</button></div>'
    +'<div class="table-wrap"><table><thead><tr><th>Bitcoin transaction</th><th>Result</th><th>Indexed channels</th><th>Contract candidates</th></tr></thead><tbody>'
    +batch.reports.map(report=>'<tr><td>'+txLink(report.query.bitcoin_txid,report.query.bitcoin_txid.slice(0,12)+'…')+'</td><td>'+esc(outcome(report).replace('_',' '))+'</td><td>'+report.matches.length+'</td><td>'
      +(report.inspection?.findings.filter(row=>row.status==='candidate').length||0)+'</td></tr>').join('')+'</tbody></table></div></section>';
}
export function lightningPanel(busy: boolean): string {
  const draft = draftFor(), locked = busy || draft.pending || draft.request !== null;
  return '<section class="panel" id="lightning-network-panel"><div class="panel-head"><div><h2>Lightning Network</h2><p>Check indexed Lightning channels, revealed swap contracts and direct output spends.</p></div></div><div class="panel-body">'
    +'<div class="segmented" role="group" aria-label="Lightning check input"><button type="button" data-action="lightning-mode-single" aria-pressed="'+(draft.mode==='single')+'"'+disabled(locked)+'>One transaction</button><button type="button" data-action="lightning-mode-batch" aria-pressed="'+(draft.mode==='batch')+'"'+disabled(locked)+'>Upload CSV</button></div>'
    +(draft.mode==='single'?'<label class="field"><span>Bitcoin transaction hash</span><input id="lightning-field-txid" value="'+esc(draft.txid)+'" placeholder="64-character Bitcoin transaction hash" autocomplete="off" spellcheck="false"'+disabled(locked)+'/>'
      +'<small>One Bitcoin mainnet transaction. Results stay in this session.</small></label>'
      +'<details id="lightning-advanced" class="pegin-coverage"'+(draft.advancedOpen?' open':'')+'><summary>Optional provider verification</summary>'
      +'<label class="field"><span>Known Boltz submarine swap ID</span><input id="lightning-field-boltz_swap_id" value="'+esc(draft.boltzSwapId)+'" autocomplete="off" spellcheck="false"'+disabled(locked)+'/>'
      +'<small>If you already have a swap receipt, this sends its ID to Boltz to check whether it names this Bitcoin funding transaction. Leave blank for blockchain and channel-index inspection only.</small></label></details>'
      :'<label class="field"><span>Bitcoin transaction CSV</span><input id="lightning-csv-file" type="file" accept=".csv,text/csv"'+disabled(locked)+'/></label>'+csvReviewHtml(draft,locked))
    +'<div class="task-actions"><button type="button" class="btn primary" data-action="lightning-start"'+disabled(locked)+'>'+(draft.pending?'Submitting lookup…':draft.request?'Checking Lightning index…':'Check Lightning Network')+'</button></div>'
    +'<p role="'+(draft.error?'alert':'status')+'">'+esc(draft.error || (draft.pending?'Submitting lookup task…':draft.request?'Lookup in progress. You can cancel this task from the task controls.':'Checks the public channel index and available transaction evidence.'))+'</p>'
    +(draft.selected?(draft.selected.kind==='lightning_batch'?batchHtml(draft.selected):reportHtml(draft.selected)):'')+'</div></section>';
}
