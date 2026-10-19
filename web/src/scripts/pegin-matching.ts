/** Session-only bridge matching. Peg-in references and peg-out candidates remain distinct. */
type Network = 'bitcoin'|'liquid';
type Bridge = 'pegin'|'pegout';
type Query = {bridge?: Bridge; network?: Network; mode?: 'single'|'batch'|'reverse'; txid?: string; txids?: string[]; vouts?: number[];
  bitcoin_txid?: string | null; bitcoin_txids?: string[]; bitcoin_vout?: number | null; bitcoin_vouts?: number[]; liquid_txid?: string | null;
  max_pages: number; start_minutes?: number; end_minutes?: number; start_hours?: number; end_hours?: number; public?: boolean};
type Output = {vout: number; outpoint: string; address: string | null; value: number | string | null;
  value_text?: string; asset: string | null; selectable: boolean; reason: string | null; script_type?: string; is_pegout?: boolean; pegout_address?: string; pegout_scriptpubkey?: string};
type ChainStatus = {confirmed: boolean; block_height?: number; block_time?: number};
type Transaction = {txid: string; status: ChainStatus; outputs: Output[]};
type Match = {bitcoin_outpoint: string; bitcoin_txid?: string; bitcoin_vout?: number; bitcoin_address?: string;
  bitcoin_value_sats: number | string; bitcoin_status?: ChainStatus; liquid_txid: string; liquid_vout?: number;
  liquid_vin?: number; verification: string; status?: string; elapsed_seconds?: number | null; liquid_status: ChainStatus};
type WindowCoverage = {start_time?: number | null; end_time?: number | null; start_utc?: string | null;
  end_utc?: string | null; complete: boolean; covered_ranges?: {start_height: number; end_height: number}[];
  start_height?: number | null; end_height?: number | null; boundary_note?: string; pages_this_task?: number; network?: Network};
type ResultRow = {source_txid?: string; bitcoin_txid?: string | null; bitcoin_vout?: number | null; liquid_txid?: string | null;
  status: string; stop_reason: string; match_count: number; coverage?: {window?: WindowCoverage}};
type Report = {id: string; created_at: string; query: Query; status: string; stop_reason: string; match_count: number;
  matches: Match[]; liquid_outputs: Record<string, Output[]>; notice: string; bitcoin?: Transaction | null;
  bitcoins?: Record<string,Transaction>; liquids?: Record<string,Transaction>; results?: ResultRow[];
  coverage?: {pages_this_task?: number; history_entries_processed?: number; window?: WindowCoverage}; downloads: {name: string; url: string}[]};
type TextField = 'txid'|'vout'|'candidate'|'pages'|'start_minutes'|'end_minutes';
type Completion = {result: unknown; error?: string};
type OutputRequest = {txid: string; public: boolean; network: Network; bridge: Bridge; jobId: string | null;
  earlyCompletions: Map<string, Completion>};
type MatchRequest = {jobId: string | null; body: Record<string,unknown>; earlyCompletions: Map<string,Completion>;
  processing?: Promise<boolean>; reportId?: string; completion?: Completion; downloaded: boolean};
type Draft = Record<TextField, string> & {public: boolean; pending: boolean; mode: 'single'|'batch'; network: Network; bridge: Bridge;
  vouts: number[]; advanced: boolean; outputsOpen: boolean; csv: PeginCsv | null; csvFilename: string; csvReading: boolean;
  csvVersion: number; csvError: string; outputs: Output[] | null; outputRequest: OutputRequest | null; outputError: string;
  selected: Report | null; error: string; matchRequest: MatchRequest | null; retryRequest: MatchRequest | null};
type Context = {busy: boolean; render: () => void; get: <T>(path: string) => Promise<T>;
  start: (body: Record<string, unknown>) => Promise<string | null>;
  loadOutputs: (body: {txid: string; public: boolean; network: Network; bridge: Bridge}) => Promise<string | null>;
  download: (url: string) => void; openLiquid: (txid: string) => void};
let workspaceDraft: Draft | undefined;
const esc = (v: unknown): string => String(v ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const disabled = (v: boolean): string => v ? ' disabled' : '';
const hash = /^[0-9a-f]{64}$/;
const chainName = (network: Network): string => network === 'bitcoin' ? 'Bitcoin' : 'Liquid';
function draftFor(): Draft {
  return workspaceDraft ??= {txid:'',vout:'',candidate:'',pages:'100',start_minutes:'-5',end_minutes:'4320',public:true,pending:false,
    mode:'single',network:'bitcoin',bridge:'pegin',vouts:[],advanced:false,outputsOpen:false,csv:null,csvFilename:'',csvReading:false,
    csvVersion:0,csvError:'',outputs:null,outputRequest:null,outputError:'',selected:null,error:'',matchRequest:null,retryRequest:null};
}
export function forgetPegin(): void {workspaceDraft=undefined;}
function clearOutputs(d: Draft): void {d.outputs=null;d.outputRequest=null;d.outputError='';d.vout='';d.vouts=[];d.outputsOpen=false;}
function selectableOutput(output: Output, d: Pick<Draft,'bridge'|'network'>): boolean {
  if (d.bridge === 'pegin' && d.network === 'liquid') return false;
  if (d.bridge === 'pegout' && d.network === 'liquid') return output.is_pegout === true && output.selectable === true;
  return output.selectable === true && !/^(?:op_return|nulldata)$/i.test(output.script_type || '');
}
function defaultWindow(bridge: Bridge, network: Network): [string,string] {
  return bridge === 'pegout' && network === 'bitcoin' ? ['-4320','5'] : ['-5','4320'];
}
function setChoice(d: Draft, field: 'bridge'|'network', value: string): void {
  const previous=defaultWindow(d.bridge,d.network), auto=d.start_minutes === previous[0] && d.end_minutes === previous[1];
  if (field === 'network') {d.network=value as Network;d.txid='';d.csv=null;d.csvFilename='';d.csvVersion++;d.csvReading=false;}
  else d.bridge=value as Bridge;
  if (auto) [d.start_minutes,d.end_minutes]=defaultWindow(d.bridge,d.network);
  clearOutputs(d);
}
export function peginInput(element: HTMLInputElement | HTMLSelectElement, render?: () => void): boolean {
  const d=draftFor(), locked=d.pending || d.matchRequest !== null;
  if (element.id?.startsWith('pegin-output-')) {
    if (!locked && !d.outputRequest && d.outputs) {
      const vout=Number(element.value);
      if (d.outputs.some(output => output.vout === vout && selectableOutput(output,d))) {
        d.vouts=d.vouts.filter(value => value !== vout);
        if ((element as HTMLInputElement).checked) d.vouts.push(vout);
        d.vouts.sort((a,b) => a-b);d.vout='';render?.();
      }
    }
    return true;
  }
  if (!element.id?.startsWith('pegin-field-')) return false;
  const field=element.id.slice('pegin-field-'.length);
  if (field === 'network' || field === 'bridge' || field === 'csv-column') {
    if (!locked) {
      if (field === 'csv-column' && d.csv) d.csv=reviewPeginCsv(d.csv,element.value === '' ? null : Number(element.value));
      else if ((field === 'network' && ['bitcoin','liquid'].includes(element.value)) || (field === 'bridge' && ['pegin','pegout'].includes(element.value))) {
        if (d[field] !== element.value) {
          setChoice(d,field,element.value);
        }
      }
      d.error='';render?.();
    }
    return true;
  }
  if (!['txid','vout','candidate','pages','start_minutes','end_minutes','public'].includes(field)) return false;
  if (!locked) {
    const changedSource=field === 'public' && d.public !== (element as HTMLInputElement).checked;
    const changedHash=field === 'txid' && d.txid.trim().toLowerCase() !== element.value.trim().toLowerCase();
    if (changedSource || changedHash) clearOutputs(d);
    if (field === 'public') d.public=(element as HTMLInputElement).checked;
    else if (field === 'vout' && d.outputs && element.value && !d.outputs.some(output => String(output.vout) === element.value && selectableOutput(output,d))) return true;
    else {d[field as TextField]=element.value;if (field === 'vout') d.vouts=[];}
    d.error='';if (changedSource || changedHash) render?.();
  }
  return true;
}
export function peginToggle(element: HTMLDetailsElement): boolean {
  if (element.id !== 'pegin-advanced' && element.id !== 'pegin-output-picker') return false;
  const d=draftFor();if (element.id === 'pegin-advanced') d.advanced=element.open;else d.outputsOpen=element.open;return true;
}
export async function peginFile(input: HTMLInputElement, render: () => void, busy=false): Promise<void> {
  const d=draftFor(),file=input.files?.[0];if (!file || busy || d.pending || d.matchRequest) return;
  const version=++d.csvVersion;d.csv=null;d.csvError='';d.csvFilename=file.name;d.csvReading=true;render();
  try {
    if (file.size > MAX_PEGIN_CSV_BYTES) throw Error('CSV files must be 1 MiB or smaller.');
    const text=await file.text();if (workspaceDraft !== d || d.csvVersion !== version) return;
    d.csv=parsePeginCsv(text,d.network);
  } catch(error) {if (workspaceDraft === d && d.csvVersion === version) d.csvError=error instanceof Error ? error.message : 'Could not read this CSV.';}
  finally {if (workspaceDraft === d && d.csvVersion === version) {d.csvReading=false;render();}}
}
export function matchingRequest(draft: Pick<Draft,'txid'|'vout'|'pages'> & Partial<Pick<Draft,'candidate'|'start_minutes'|'end_minutes'|'public'|'mode'|'network'|'bridge'|'vouts'|'csv'>>): Record<string,unknown> {
  const txid=draft.txid.trim().toLowerCase(),bridge=draft.bridge ?? 'pegin',network=draft.network ?? 'bitcoin';
  if (!['pegin','pegout'].includes(bridge) || !['bitcoin','liquid'].includes(network)) throw Error('Choose a transfer type and source network.');
  if (!/^\d+$/.test(draft.pages.trim()) || Number(draft.pages) < 1 || Number(draft.pages) > 1000) throw Error('Choose 1 to 1000 pages per task.');
  const defaults=defaultWindow(bridge,network),start=draft.start_minutes ?? defaults[0],end=draft.end_minutes ?? defaults[1];
  const startMinutes=Number(start),endMinutes=Number(end);
  if (!start.trim() || !end.trim() || !Number.isFinite(startMinutes) || !Number.isFinite(endMinutes) || Math.abs(startMinutes)>5256000 || Math.abs(endMinutes)>5256000)
    throw Error('Enter finite start and end minutes between -5256000 and 5256000.');
  if (endMinutes <= startMinutes) throw Error('End minutes must be greater than start minutes.');
  const settings={action:'pegin-match',bridge,network,mode:draft.mode ?? 'single',max_pages:Number(draft.pages),start_minutes:startMinutes,end_minutes:endMinutes,public:draft.public ?? true};
  if (draft.mode === 'batch') {
    if (!draft.csv || draft.csv.selectedColumn === null || draft.csv.errors.length || !draft.csv.txids.length) throw Error('Upload a CSV, choose its transaction-hash column, and fix the reported rows before searching.');
    return {...settings,txids:draft.csv.txids};
  }
  if (!hash.test(txid)) throw Error('Enter a full 64-character transaction hash.');
  if (draft.vout.trim() && (!/^\d+$/.test(draft.vout.trim()) || Number(draft.vout)>=0x40000000)) throw Error('Vout must be a non-negative output index.');
  const vouts=[...new Set(draft.vouts?.length ? draft.vouts : draft.vout.trim() ? [Number(draft.vout)] : [])].sort((a,b)=>a-b);
  if (vouts.some(value => !Number.isInteger(value) || value<0 || value>=0x40000000)) throw Error('Choose valid output indices.');
  return {...settings,txid,...(bridge === 'pegin' && network === 'liquid' ? {} : vouts.length>1 ? {vouts} : vouts.length ? {vout:vouts[0]} : {})};
}
/** Only the session that submitted this task can show its result or download a CSV. */
export async function peginComplete(jobId: string, result: unknown, error: string | undefined, context: Context): Promise<boolean> {
  const d=workspaceDraft,request=d?.matchRequest;
  if (!d || !request) return false;
  if (request.jobId === null) {request.earlyCompletions.set(jobId,{result,error});return false;}
  if (request.jobId !== jobId) return false;
  if (request.processing) return request.processing;
  request.completion={result,error};
  const processing=(async () => {
    const value=result as {id?: unknown;match?: {id?: unknown}} | null;
    const identity=value?.id ?? value?.match?.id;
    request.reportId=typeof identity === 'string' && /^[0-9a-f]{32}$/.test(identity) ? identity : undefined;
    try {
      if (!request.reportId) {d.error=error || 'The matching task did not return a result. Run the search again.';return true;}
      const report=await context.get<Report>('/api/tools/pegin-matches/'+request.reportId);
      if (workspaceDraft !== d || d.matchRequest !== request) return false;
      if (report.id !== request.reportId || !Array.isArray(report.matches) || !Array.isArray(report.downloads)) throw Error('The task returned an invalid matching result.');
      d.selected=report;d.retryRequest=null;d.error=error || '';
      const expected=Array.isArray(request.body.txids) ? request.body.txids : [];
      const rows=report.results || [], source=request.body.network === 'liquid' ? 'liquid' : 'bitcoin';
      const processed=new Set(rows.map(row=>sourceHash(row,source)));
      const completedBatch=request.body.mode === 'batch' && !error && report.stop_reason === 'batch_complete'
        && expected.length > 0 && rows.length === expected.length && processed.size === expected.length
        && expected.every(txid=>processed.has(txid))
        && rows.every(row=>!['unprocessed','paused','cancelled','running','pending'].includes(row.status));
      const csv=report.downloads.find(file=>file.name === 'matches.csv' && safeDownload(file.url));
      if (completedBatch && csv && !request.downloaded) {
        request.downloaded=true;
        try {context.download(csv.url);} catch {d.error='Results are ready. Use Download matches.csv below if your browser blocked the automatic download.';}
      }
      return true;
    } catch(loadError) {
      if (workspaceDraft === d) {d.retryRequest=request;d.error=loadError instanceof Error ? loadError.message : 'Could not load the current result.';}
      return true;
    } finally {
      if (workspaceDraft === d && d.matchRequest === request) {d.matchRequest=null;d.pending=false;context.render();}
    }
  })();
  request.processing=processing;
  return processing;
}
/** A finished lookup may only populate the unchanged source that requested it. */
export function peginOutputsComplete(jobId: string, result: unknown, error?: string): boolean {
  const d=workspaceDraft,request=d?.outputRequest;
  if (!d || !request || request.txid!==d.txid.trim().toLowerCase() || request.public!==d.public || request.network!==d.network || request.bridge!==d.bridge) return false;
  if (request.jobId === null) {request.earlyCompletions.set(jobId,{result,error});return false;}
  if (request.jobId !== jobId) return false;
  d.outputRequest=null;if (error) {d.outputError=error;return true;}
  const inspection=result as {blockchain?: string;transactions?: {txid?: string;outputs?: Output[]}[]} | null;
  const transaction=inspection?.blockchain === request.network && Array.isArray(inspection.transactions) ? inspection.transactions.find(tx=>tx?.txid===request.txid) : undefined;
  if (!transaction || !Array.isArray(transaction.outputs) || transaction.outputs.some(output=>!output || !Number.isInteger(output.vout) || output.vout<0 || output.vout>=0x40000000)
      || new Set(transaction.outputs.map(output=>output.vout)).size !== transaction.outputs.length) {
    d.outputError='The output lookup did not return valid outputs for this '+chainName(d.network)+' transaction. Load the outputs again.';return true;
  }
  d.outputs=transaction.outputs;d.outputsOpen=true;
  d.vouts=d.vouts.filter(vout=>d.outputs!.some(output=>output.vout===vout && selectableOutput(output,d)));
  if (d.vout && d.outputs.some(output=>String(output.vout)===d.vout && selectableOutput(output,d))) d.vouts=[Number(d.vout)];
  d.vout='';d.outputError='';return true;
}
async function loadOutputs(d: Draft,context: Context): Promise<void> {
  if (context.busy || d.matchRequest || d.outputRequest || d.mode!=='single') return;
  const txid=d.txid.trim().toLowerCase();
  if (!hash.test(txid)) {d.outputError='Enter a full 64-character '+chainName(d.network)+' transaction hash to load its outputs.';context.render();return;}
  const request: OutputRequest={txid,public:d.public,network:d.network,bridge:d.bridge,jobId:null,earlyCompletions:new Map()};
  d.outputs=null;d.outputError='';d.outputRequest=request;context.render();
  try {
    const jobId=await context.loadOutputs({txid,public:request.public,network:request.network,bridge:request.bridge});
    if (workspaceDraft!==d || d.outputRequest!==request) return;
    if (!jobId) {d.outputRequest=null;d.outputError='Output lookup could not start. Try again when the current task finishes.';}
    else {request.jobId=jobId;const completion=request.earlyCompletions.get(jobId);request.earlyCompletions.clear();if (completion) peginOutputsComplete(jobId,completion.result,completion.error);}
  } catch(error) {if (workspaceDraft===d && d.outputRequest===request) {d.outputRequest=null;d.outputError=error instanceof Error ? error.message : 'Could not load outputs.';}}
  finally {if (workspaceDraft===d) context.render();}
}
function bitcoinAmount(output: Pick<Output,'value'|'value_text'>): string {
  const value=typeof output.value_text==='string' ? output.value_text : typeof output.value==='string' ? output.value : typeof output.value==='number' && Number.isSafeInteger(output.value) ? String(output.value) : '';
  if (!/^\d+$/.test(value)) return 'Amount unavailable';
  const sats=BigInt(value),unit=100000000n;return String(sats/unit)+'.'+String(sats%unit).padStart(8,'0')+' BTC ('+String(sats)+' sats)';
}
function outputsHtml(d: Draft,locked: boolean): string {
  const information=d.bridge==='pegin' && d.network==='liquid',selected=d.vouts.length ? d.vouts : d.vout ? [Number(d.vout)] : [];
  const label=information ? 'Liquid claim outputs (informational)' : selected.length ? selected.length+' selected output'+(selected.length===1 ? '' : 's')+' · vout '+selected.join(', ') : 'Any '+chainName(d.network)+' output';
  const note=information ? 'Peg-in references are transaction inputs. Individual Liquid outputs cannot identify which Bitcoin deposit funded them, so this lookup checks all claim references.'
    : d.bridge==='pegout' && d.network==='liquid' ? 'Select peg-out request outputs, or leave all unchecked to inspect every request in this transaction.'
    : 'Nothing checked searches any output. Select one or more to check those outpoints.';
  if (!d.outputs) return '<p class="selection-count">'+esc(label)+'. '+esc(information ? note : 'Load outputs to choose specific outputs.')+'</p>';
  return '<details id="pegin-output-picker" class="pegin-output-picker"'+(d.outputsOpen?' open':'')+'><summary>'+esc(label)+' · '+d.outputs.length+' outputs loaded</summary>'
    +'<div class="output-group"><header><span>'+esc(information?'Liquid outputs': 'Choose '+chainName(d.network)+' outputs')+'</span>'
    +(information?'':'<button type="button" class="btn small" data-action="pegin-any-output"'+disabled(locked)+'>Clear selection / any output</button>')+'</header>'
    +d.outputs.map(output=>'<'+(information?'div':'label')+' class="output-row'+(!information && !selectableOutput(output,d)?' disabled':'')+'">'
      +(information?'':'<input type="checkbox" id="pegin-output-'+esc(output.vout)+'" value="'+esc(output.vout)+'"'+(selected.includes(output.vout)?' checked':'')+disabled(locked||!selectableOutput(output,d))+'/>')
      +'<span class="output-detail"><span class="output-top"><strong>vout '+esc(output.vout)+'</strong><span>'
      +esc(d.network==='bitcoin'?bitcoinAmount(output):output.value_text??output.value??'Confidential / unavailable')+(d.network==='liquid' && (output.value_text!=null||output.value!=null)?' base units':'')
      +'</span></span><span class="mono">'+esc(output.is_pegout ? 'Bitcoin destination: '+(output.pegout_address||output.pegout_scriptpubkey||'Unavailable') : output.address||output.script_type||'Address unavailable')+'</span>'
      +(d.network==='liquid'?'<small>Asset: '+esc(output.asset||'Confidential / unavailable')+'</small>':'')
      +(output.is_pegout?'<span class="badge">Peg-out request</span>': !information && !selectableOutput(output,d)?'<span class="badge gray">'+esc(output.reason||'Not available for this search')+'</span>':'')
      +'</span></'+(information?'div':'label')+'>').join('')+'</div><p class="selection-count">'+esc(note)+'</p></details>';
}
function table(headings: string[],rows: string): string {return '<div class="table-wrap"><table><thead><tr>'+headings.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div>';}
function txLink(txid: string,chain: Network): string {return hash.test(txid)?'<a class="mono" target="_blank" rel="noopener noreferrer" href="https://blockstream.info/'+(chain==='liquid'?'liquid/':'')+'tx/'+txid+'">'+esc(txid)+'</a>':'<span class="mono">'+esc(txid||'Unavailable')+'</span>';}
function dateLabel(status?: ChainStatus): string {
  if (status?.block_time!=null && Number.isFinite(status.block_time)) {const date=new Date(status.block_time*1000);if(Number.isFinite(date.getTime()))return date.toISOString().replace('T',' ').replace('.000Z',' UTC');}
  return status?.confirmed?'Block timestamp unavailable':'Unconfirmed / timestamp unavailable';
}
function reportNetwork(query: Query): Network {return query.network??(query.mode==='reverse'?'liquid':'bitcoin');}
function sourceHash(row: ResultRow,network: Network): string {return row.source_txid||(network==='bitcoin'?row.bitcoin_txid:row.liquid_txid)||'';}
function matchCards(report: Report,busy: boolean): string {
  const pegout=report.query.bridge==='pegout';
  return report.matches.slice(0,100).map(match=>{
    const [outpointHash,outpointIndex]=(match.bitcoin_outpoint||'').split(':'),txid=match.bitcoin_txid||outpointHash,vout=match.bitcoin_vout??Number(outpointIndex);
    const bitcoin=report.bitcoins?.[txid]||(report.bitcoin?.txid===txid?report.bitcoin:undefined),output=bitcoin?.outputs.find(item=>item.vout===vout);
    const liquidOutputs=report.liquid_outputs?.[match.liquid_txid]||[];
    const heading=pegout?(match.status==='ambiguous'?'Ambiguous payout candidate':'Candidate payout'):(match.verification==='confirmed_reference'?'Confirmed exact reference':'Unconfirmed exact reference');
    const btcCard='<section class="pegin-transaction"><h4>'+(pegout?'Bitcoin payout candidate':'Bitcoin deposit')+'</h4>'+txLink(txid,'bitcoin')
      +'<dl><dt>Output</dt><dd>vout '+esc(vout)+'</dd><dt>'+(pegout?'Output value':'Deposit value')+'</dt><dd>'+esc(bitcoinAmount({value:match.bitcoin_value_sats}))
      +'</dd><dt>Block date</dt><dd>'+esc(dateLabel(bitcoin?.status||match.bitcoin_status))+'</dd><dt>'+(pegout?'Destination address':'Deposit address')+'</dt><dd class="mono">'
      +esc(match.bitcoin_address||output?.address||'Address unavailable')+'</dd></dl></section>';
    const liquidCard='<section class="pegin-transaction"><h4>'+(pegout?'Liquid peg-out request':'Liquid claim')+'</h4>'+txLink(match.liquid_txid,'liquid')
      +'<dl><dt>'+(pegout?'Request output':'Claim input')+'</dt><dd>'+(pegout?'vout '+esc(match.liquid_vout):'vin '+esc(match.liquid_vin))+'</dd>'
      +(pegout?'':'<dt>Referenced Bitcoin output</dt><dd class="mono">'+esc(match.bitcoin_outpoint)+'</dd>')+'<dt>Block date</dt><dd>'+esc(dateLabel(match.liquid_status))+'</dd></dl>'
      +(!pegout?'<details><summary>'+liquidOutputs.length+' Liquid outputs</summary>'+liquidOutputs.slice(0,100).map(value=>'<div class="pegin-claim-output"><strong>vout '+esc(value.vout)+'</strong><span>'+esc(value.value_text??value.value??'Confidential / unavailable')+(value.value_text!=null||value.value!=null?' base units':'')+'</span><span class="mono">'+esc(value.address||'Address unavailable')+'</span><small>Asset: '+esc(value.asset||'Confidential / unavailable')+'</small></div>').join('')+'</details>':'')
      +'<button type="button" class="btn small" data-action="pegin-open-liquid" data-txid="'+esc(match.liquid_txid)+'"'+disabled(busy||!hash.test(match.liquid_txid))+'>Start Liquid investigation</button></section>';
    return '<article class="pegin-match"><div class="pegin-match-heading"><strong>'+heading+'</strong>'
      +(match.elapsed_seconds!=null&&Number.isFinite(match.elapsed_seconds)?'<span>Block-time difference: '+esc((match.elapsed_seconds/60).toFixed(1))+' minutes</span>':'')+'</div><div class="pegin-pair">'+(pegout?liquidCard+btcCard:btcCard+liquidCard)+'</div>'
      +'<p class="artifact-note">'+(pegout?'Destination script, amount, and time identify a candidate. They do not establish an exact cross-chain payout reference. Review ambiguous or reused destinations before drawing a conclusion.':'An exact peg-in reference links these transactions. It does not establish ownership or allocate the Bitcoin deposit across individual Liquid outputs.')+'</p></article>';
  }).join('')+(report.matches.length>100?'<p>Showing the first 100 matched pairs. The current CSV contains all results.</p>':'');
}
function safeDownload(url: string): boolean {return /^\/files\/(?:[0-9a-f]{32}\/)?pegin-matches\/[0-9a-f]{32}\/[a-zA-Z0-9.-]+$/.test(url);}
function windowHtml(window?: WindowCoverage): string {
  if (!window) return '<p>No bounded timestamp coverage was recorded for this result.</p>';
  const utc=(label: string|null|undefined,seconds: number|null|undefined): string=>{
    if(label)return esc(label);if(seconds!=null&&Number.isFinite(seconds)){const date=new Date(seconds*1000);if(Number.isFinite(date.getTime()))return esc(date.toISOString());}return 'Unavailable';};
  return '<p>Selected timestamp window (UTC): '+utc(window.start_utc,window.start_time)+' to '+utc(window.end_utc,window.end_time)+'.</p>'
    +'<p>'+(window.complete?'Complete':'Incomplete')+' bounded coverage'+(window.start_height!=null&&window.end_height!=null?' · Blocks '+esc(window.start_height)+' to '+esc(window.end_height):'')+'.</p>'
    +(window.boundary_note?'<p>'+esc(window.boundary_note)+'</p>':'');
}
function reportHtml(report: Report,busy: boolean): string {
  const pegout=report.query.bridge==='pegout',network=reportNetwork(report.query),rows=report.results||[],csv=report.downloads.find(file=>file.name==='matches.csv'&&safeDownload(file.url));
  const rowLinks=(row: ResultRow,chain: Network): string=>{
    const related=report.matches.filter(match=>(network==='bitcoin'?(match.bitcoin_txid||match.bitcoin_outpoint.split(':')[0]):match.liquid_txid)===sourceHash(row,network));
    const values=related.map(match=>chain==='bitcoin'?(match.bitcoin_txid||match.bitcoin_outpoint.split(':')[0]):match.liquid_txid);
    const original=chain==='bitcoin'?row.bitcoin_txid:row.liquid_txid;if(original)values.push(original);
    return [...new Set(values)].map(txid=>txLink(txid,chain)).join('<br/>')||'None found';};
  return '<section aria-label="Current matching result" class="pegin-results"><h3>'+(report.query.mode==='batch'?'CSV batch results':'Matching results')+'</h3>'
    +'<p>'+report.matches.length+' '+(pegout?'payout candidate(s)':'exact claim reference(s)')+' · Status: '+esc(report.status)+'. '+esc((report.stop_reason||'').replaceAll('_',' '))+'.</p>'
    +(csv?'<div class="artifact-actions"><a class="btn" href="'+esc(csv.url)+'" download>Download matches.csv</a></div>':'')
    +(report.query.mode==='batch'?'<p>The CSV downloads automatically after every transaction has been processed. If your browser blocks it, use the download button above.</p>':'')
    +(rows.length?table(['Bitcoin transaction','Liquid transaction','Status','Matches'],rows.map(row=>'<tr><td>'+rowLinks(row,'bitcoin')+'</td><td>'+rowLinks(row,'liquid')+'</td><td>'+esc(row.status)+'<br/><small>'+esc((row.stop_reason||'').replaceAll('_',' '))+'</small></td><td>'+esc(row.match_count)+'</td></tr>').join('')):'')
    +(report.matches.length?matchCards(report,busy):'<p>No match in this search is not proof that no bridge transfer exists. A corresponding transaction may be outside the window or in data not yet searched.</p>')
    +'<p class="artifact-note">'+esc(report.notice)+'</p><details class="pegin-coverage"><summary>Search coverage</summary>'
    +(rows.length?rows.map(row=>'<section class="pegin-row-coverage"><h4 class="mono">'+esc(sourceHash(row,network))+'</h4><p>'+esc(row.status)+' · '+esc((row.stop_reason||'').replaceAll('_',' '))+'</p>'+windowHtml(row.coverage?.window)+'</section>').join(''):windowHtml(report.coverage?.window))+'</details>'
    +(['paused','cancelled','error','partial'].includes(report.status)?'<button type="button" class="btn" data-action="pegin-continue"'+disabled(busy)+'>Continue / refresh same window</button>':'')+'</section>';
}
function restoreQuery(d: Draft,query: Query): void {
  clearOutputs(d);d.mode=query.mode==='batch'?'batch':'single';d.bridge=query.bridge??'pegin';d.network=reportNetwork(query);
  d.txid=query.txid||(d.network==='bitcoin'?query.bitcoin_txid:query.liquid_txid)||'';
  d.vouts=query.vouts?.length?[...query.vouts]:query.bitcoin_vouts?.length?[...query.bitcoin_vouts]:query.bitcoin_vout==null?[]:[query.bitcoin_vout];
  d.candidate='';d.pages=String(query.max_pages);d.start_minutes=String(query.start_minutes??(query.start_hours??0)*60);d.end_minutes=String(query.end_minutes??(query.end_hours??72)*60);d.public=query.public??true;
  if(d.mode==='batch'){const txids=query.txids||query.bitcoin_txids||[];d.csv=reviewPeginCsv({columns:[chainName(d.network)+' transaction hash'],rows:txids.map((txid,index)=>({row:index+2,cells:[txid]}))},0);d.csvFilename='Current batch';}
}
function csvHtml(d: Draft,locked: boolean): string {
  const csv=d.csv,network=chainName(d.network);
  return '<label class="field"><span>CSV of '+network+' transaction hashes</span><input id="pegin-csv-file" type="file" accept=".csv,text/csv"'+disabled(locked)+'/>'
    +'<small>Up to 1 MiB and 500 unique hashes. Duplicate hashes run once. Results download after the whole batch finishes.</small></label>'
    +(d.csvFilename?'<p>'+esc(d.csvFilename)+(d.csvReading?' · Reading CSV…':'')+'</p>':'')+(d.csvError?'<p role="alert">'+esc(d.csvError)+'</p>':'')
    +(csv?'<label class="field"><span>'+network+' transaction-hash column</span><select id="pegin-field-csv-column"'+disabled(locked)+'><option value=""'+(csv.selectedColumn===null?' selected':'')+'>Choose a column</option>'
      +csv.columns.map((name,index)=>'<option value="'+index+'"'+(csv.selectedColumn===index?' selected':'')+'>'+esc(name)+' (column '+(index+1)+')</option>').join('')+'</select></label>'
      +'<p class="selection-count">'+csv.rows.length+' data rows · '+csv.txids.length+' unique hashes · '+csv.duplicates.length+' duplicate rows · '+csv.errors.length+' errors.</p>'
      +(csv.errors.length?'<div role="alert">'+csv.errors.slice(0,20).map(error=>'<p>'+(error.row?'Row '+error.row+': ':'')+esc(error.message)+'</p>').join('')+(csv.errors.length>20?'<p>Showing the first 20 errors.</p>':'')+'</div>':'')
      +(csv.duplicates.length?'<details><summary>Review duplicate rows</summary>'+csv.duplicates.slice(0,50).map(item=>'<p>Row '+item.row+' repeats row '+item.firstRow+' · <span class="mono">'+esc(item.txid)+'</span></p>').join('')+'</details>':'')
      +(csv.selectedColumn!==null&&csv.txids.length?table([network+' transaction hash'],csv.txids.slice(0,5).map(txid=>'<tr><td class="mono">'+esc(txid)+'</td></tr>').join(''))+'<p>Previewing '+Math.min(5,csv.txids.length)+' of '+csv.txids.length+' unique hashes. Other CSV columns stay local.</p>':''):'');
}
export function peginPanel(busy: boolean): string {
  const d=draftFor(),locked=busy||d.pending||d.matchRequest!==null,lookupPending=d.outputRequest!==null,network=chainName(d.network);
  const field=(name: TextField,label: string,placeholder: string)=>'<label class="field"><span>'+label+'</span><input id="pegin-field-'+name+'" value="'+esc(d[name])+'" placeholder="'+placeholder+'" autocomplete="off"'+(['start_minutes','end_minutes'].includes(name)?' type="number" step="any" min="-5256000" max="5256000"':'')+disabled(locked)+'/></label>';
  const ready=d.mode!=='batch'||!!(d.csv&&d.csv.selectedColumn!==null&&!d.csv.errors.length&&d.csv.txids.length);
  const label=d.bridge==='pegout'?(d.network==='liquid'?'Find Bitcoin payout candidates':'Find Liquid peg-out candidates'):(d.network==='liquid'?'Find Bitcoin deposit':'Find Liquid claim');
  return '<section class="panel" id="pegin-matching-panel"><div class="panel-head"><div><h2>Liquid Network</h2><p>Match Bitcoin and Liquid peg-ins or peg-outs.</p></div></div><div class="panel-body">'
    +'<div class="field"><span>Transfer type</span><div class="pegin-mode-switch" role="group" aria-label="Transfer type">'+(['pegin','pegout'] as Bridge[]).map(value=>'<button type="button" class="btn'+(d.bridge===value?' primary':'')+'" data-action="pegin-bridge-'+value+'" aria-pressed="'+(d.bridge===value)+'"'+disabled(locked)+'>'+(value==='pegin'?'Peg-in':'Peg-out')+'</button>').join('')+'</div></div>'
    +'<div class="field"><span>Transaction provided</span><div class="pegin-mode-switch" role="group" aria-label="Transaction provided">'+(['bitcoin','liquid'] as Network[]).map(value=>'<button type="button" class="btn'+(d.network===value?' primary':'')+'" data-action="pegin-network-'+value+'" aria-pressed="'+(d.network===value)+'"'+disabled(locked)+'>'+(value==='bitcoin'?'Bitcoin':'Liquid')+'</button>').join('')+'</div></div>'
    +'<div class="pegin-mode-switch" role="group" aria-label="Matching input"><button type="button" class="btn'+(d.mode==='single'?' primary':'')+'" data-action="pegin-mode-single" aria-pressed="'+(d.mode==='single')+'"'+disabled(locked)+'>Single transaction</button><button type="button" class="btn'+(d.mode==='batch'?' primary':'')+'" data-action="pegin-mode-batch" aria-pressed="'+(d.mode==='batch')+'"'+disabled(locked)+'>Upload CSV</button></div>'
    +(d.mode==='batch'?csvHtml(d,locked):field('txid',network+' transaction hash','64-character '+network+' hash')+'<button type="button" class="btn" data-action="pegin-load-outputs"'+disabled(locked||lookupPending)+'>'+(lookupPending?'Loading '+network+' outputs…':'Load '+network+' outputs')+'</button>'+(d.outputError||lookupPending?'<p role="'+(d.outputError?'alert':'status')+'">'+esc(d.outputError||'Fetching transaction outputs.')+'</p>':'')+outputsHtml(d,locked||lookupPending))
    +(d.bridge==='pegout'?'<p class="artifact-note">Peg-out results are candidates matched by destination script, amount, and time. A Bitcoin transaction does not contain the Liquid request hash, so this search does not establish an exact cross-chain reference.</p>':'')
    +'<details id="pegin-advanced" class="pegin-advanced"'+(d.advanced?' open':'')+'><summary>Advanced settings</summary><label class="check-line"><input id="pegin-field-public" type="checkbox"'+(d.public?' checked':'')+disabled(locked)+'/><span>Use public Blockstream APIs</span></label>'
    +'<p>Both API modes read the same public on-chain data. Public access needs no API token and may be rate-limited. Uncheck to use the configured authenticated connection.</p>'+field('pages','Maximum pages per task','100')
    +'<div class="field-row">'+field('start_minutes','Window start (minutes from '+network+' block timestamp)',defaultWindow(d.bridge,d.network)[0])+field('end_minutes','Window end (minutes from '+network+' block timestamp)',defaultWindow(d.bridge,d.network)[1])+'</div>'
    +'<p>Negative minutes search before the provided transaction. Offsets use block timestamps, not API clocks. They limit the searched data, not when a transfer must occur.</p>'
    +(d.bridge==='pegin'?'<p>A peg-in uses a Bitcoin deposit and a Liquid claim. Your wallet or service must submit the claim, which can be delayed or remain unclaimed. Time narrows the search; an exact Bitcoin outpoint reference establishes the match. Direct Liquid lookups inspect claim inputs without a history window.</p>':'<p>Bitcoin-source searches look backwards for Liquid requests by default. Liquid-source searches look forwards for Bitcoin payouts. Reused destinations or equal amounts can produce multiple candidates.</p>')
    +'<p>Mainnet only. Searches are cancellable. Shared search data can be reused; these results are available only during this session.</p></details>'
    +'<div class="task-actions"><button type="button" class="btn primary" data-action="pegin-start"'+disabled(locked||lookupPending||d.csvReading||!ready)+'>'+label+'</button>'
    +(d.retryRequest?'<button type="button" class="btn" data-action="pegin-result"'+disabled(locked)+'>Retry loading current result</button>':'')+'</div>'
    +'<p role="'+(d.error?'alert':'status')+'">'+esc(d.error||(d.pending?'Submitting matching task…':d.matchRequest?'Matching in progress. Results will appear here.':'A no-match result describes only the searched data and window.'))+'</p>'
    +(d.selected?reportHtml(d.selected,locked||lookupPending):'')+'</div></section>';
}
export async function peginAction(action: string,element: {dataset: Record<string,string|undefined>} | undefined,context: Context): Promise<boolean> {
  if(!action.startsWith('pegin-'))return false;
  const d=draftFor();if(d.pending||d.matchRequest)return true;
  const choice=/^pegin-(bridge|network)-(pegin|pegout|bitcoin|liquid)$/.exec(action);
  if(choice) {
    const field=choice[1] as 'bridge'|'network',value=choice[2];
    if(!context.busy && (field==='bridge'?['pegin','pegout']:['bitcoin','liquid']).includes(value)) {
      if(d[field]!==value)setChoice(d,field,value);
      d.error='';context.render();
    }
    return true;
  }
  if(action==='pegin-mode-single'||action==='pegin-mode-batch') {
    if(!context.busy){d.mode=action==='pegin-mode-batch'?'batch':'single';clearOutputs(d);d.error='';d.csvVersion++;d.csvReading=false;context.render();}return true;
  }
  if(action==='pegin-any-output'){if(!context.busy&&!d.outputRequest){d.vouts=[];d.vout='';context.render();}return true;}
  if(action==='pegin-load-outputs'){await loadOutputs(d,context);return true;}
  if(action==='pegin-result'&&d.retryRequest){const request=d.retryRequest;request.processing=undefined;d.matchRequest=request;await peginComplete(request.jobId!,request.completion?.result,request.completion?.error,context);return true;}
  try {
    d.error='';
    if(action==='pegin-open-liquid'){const txid=element?.dataset.txid||'';if(!context.busy&&hash.test(txid)&&d.selected?.matches.some(match=>match.liquid_txid===txid))context.openLiquid(txid);return true;}
    if(action!=='pegin-start'&&action!=='pegin-continue')return true;
    if(context.busy||d.outputRequest||d.csvReading)return true;
    if(action==='pegin-continue'&&d.selected)restoreQuery(d,d.selected.query);
    const body=matchingRequest(d);
    if(action==='pegin-continue'&&d.selected&&(d.selected.query.bridge||d.selected.query.mode==='batch'))body.resume_report=d.selected.id;
    const request: MatchRequest={jobId:null,body,earlyCompletions:new Map(),downloaded:false};
    d.matchRequest=request;d.retryRequest=null;d.pending=true;context.render();
    const jobId=await context.start(body);
    if(workspaceDraft!==d||d.matchRequest!==request)return true;
    if(!jobId){d.matchRequest=null;d.error='Matching could not start. Try again when the current task finishes.';}
    else {request.jobId=jobId;d.selected=null;const completion=request.earlyCompletions.get(jobId);request.earlyCompletions.clear();if(completion)await peginComplete(jobId,completion.result,completion.error,context);}
  } catch(error){if(workspaceDraft===d){d.matchRequest=null;d.error=error instanceof Error?error.message:'Bridge matching could not be completed.';}}
  finally {if(workspaceDraft===d){d.pending=false;context.render();}}
  return true;
}

/** Local-only CSV review. Nothing is sent until the investigator submits the batch. */
export const MAX_PEGIN_CSV_BYTES = 1024 * 1024;
export const MAX_PEGIN_TXIDS = 500;
export type CsvRow = {row: number; cells: string[]};
export type PeginCsv = {columns: string[]; rows: CsvRow[]; selectedColumn: number | null; txids: string[];
  duplicates: {row: number; firstRow: number; txid: string}[]; errors: {row: number; message: string}[]};
const csvHash = /^[0-9a-f]{64}$/i;

function records(text: string): CsvRow[] {
  const rows: CsvRow[] = [];
  let cells: string[] = [], value = '', quoted = false, closed = false, line = 1, firstLine = 1;
  const field = () => {cells.push(value); value=''; closed=false;};
  const record = () => {field(); if (cells.some(cell => cell.trim())) rows.push({row:firstLine, cells}); cells=[]; firstLine=line;};
  for (let i=0; i<text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i+1] === '"') {value+='"'; i++;} else {quoted=false; closed=true;}
      } else if (char === '\r' || char === '\n') {
        if (char === '\r' && text[i+1] === '\n') i++;
        value+='\n'; line++;
      } else value+=char;
    } else if (char === ',') field();
    else if (char === '\r' || char === '\n') {
      if (char === '\r' && text[i+1] === '\n') i++;
      line++; record();
    } else if (char === '"') {
      if (value.trim() || closed) throw Error('Row '+line+': unexpected quote in an unquoted CSV field.');
      value=''; quoted=true;
    } else if (closed) {
      if (!/\s/.test(char)) throw Error('Row '+line+': unexpected text after a closing quote.');
    } else value+=char;
  }
  if (quoted) throw Error('Row '+firstLine+': quoted CSV field was not closed.');
  if (value || cells.length || closed) record();
  return rows;
}

export function reviewPeginCsv(source: Pick<PeginCsv,'columns'|'rows'>, column: number | null): PeginCsv {
  const result: PeginCsv = {...source, selectedColumn:column, txids:[], duplicates:[], errors:[]};
  if (column !== null && (!Number.isInteger(column) || column < 0 || column >= source.columns.length))
    throw Error('Choose a transaction-hash column from this CSV.');
  const seen = new Map<string, number>();
  for (const item of source.rows) {
    if (item.cells.length !== source.columns.length) {
      result.errors.push({row:item.row, message:'Expected '+source.columns.length+' columns; found '+item.cells.length+'.'});
      continue;
    }
    if (column === null) continue;
    const txid = item.cells[column].trim().toLowerCase();
    if (!csvHash.test(txid)) {result.errors.push({row:item.row, message:'Enter a full 64-character transaction hash.'}); continue;}
    if (seen.has(txid)) result.duplicates.push({row:item.row, firstRow:seen.get(txid)!, txid});
    else {seen.set(txid,item.row); result.txids.push(txid);}
  }
  if (!source.rows.length) result.errors.push({row:0,message:'The CSV has no transaction rows.'});
  if (result.txids.length > MAX_PEGIN_TXIDS)
    result.errors.push({row:0,message:'Choose at most '+MAX_PEGIN_TXIDS+' unique transaction hashes per batch.'});
  return result;
}

export function parsePeginCsv(text: string, network: Network = 'bitcoin'): PeginCsv {
  if (new TextEncoder().encode(text).length > MAX_PEGIN_CSV_BYTES) throw Error('CSV files must be 1 MiB or smaller.');
  const parsed = records(text.replace(/^\uFEFF/,''));
  if (!parsed.length) throw Error('The CSV is empty.');
  // A single-column, headerless list of hashes is unambiguous.
  const headerless = parsed[0].cells.length === 1 && csvHash.test(parsed[0].cells[0].trim());
  const columns = headerless ? [chainName(network)+' transaction hash'] : parsed[0].cells.map((value,index) => value.trim() || 'Column '+(index+1));
  const rows = headerless ? parsed : parsed.slice(1);
  const specific = columns.map((name,index) => ({name:name.toLowerCase().replace(/[^a-z0-9]/g,''),index}))
    .filter(({name}) => (network === 'bitcoin' ? ['bitcointxid','btctxid','bitcointransaction','btctransaction','bitcointransactionhash','btctransactionhash','bitcoinhash','btchash'] : ['liquidtxid','liquidtransaction','liquidtransactionhash','liquidhash']).includes(name));
  return reviewPeginCsv({columns,rows}, headerless ? 0 : specific.length === 1 ? specific[0].index : null);
}
