const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const js=fs.readFileSync(path.join(__dirname,'../webapp/static/product.js'),'utf8');
test('preview distinguishes exact safe errors without exposing raw exception',()=>{
 const fn=js.slice(js.indexOf('function manualEvidenceMessage('),js.indexOf('function manualAnalysisMarkup('));
 const context={t:(ru,en)=>en};vm.createContext(context);vm.runInContext(fn,context);
 assert.match(context.manualEvidenceMessage('API_BUDGET_CONSTRAINED'),/budget temporarily constrained/);
 assert.match(context.manualEvidenceMessage('NETWORK_MISMATCH'),/network/);
 assert.doesNotMatch(context.manualEvidenceMessage('SECRET_RAW_EXCEPTION'),/SECRET/);
});
test('wallet form only saves; historical analysis is an explicit separate action',()=>{
 assert.match(js,/e.target.id==='manual-form'.*void saveManualDraft\(\)/);
 assert.match(js,/b.dataset.manualAnalysis.*await loadManualAnalysis\(leader\)/);
 assert.doesNotMatch(js,/function manualPreview\(/);
 const analysis=js.slice(js.indexOf('async function loadManualAnalysis('),js.indexOf('function manualConfirm('));
 assert.match(analysis,/HISTORY_INCOMPLETE/);
 assert.doesNotMatch(analysis,/saveManual|method:'PUT'/);
});

function harness(responses){
 const button={disabled:false},status={textContent:''},sheet={closed:false,close(){this.closed=true;}};
 const nodes={'manual-wallet':{value:'0x'+'b'.repeat(40)},allocation:{value:'85'},'manual-form-status':status,'manual-action-status':status,sheet};
 const calls=[],config={leader:'0x'+'b'.repeat(40),allocation_pct:85,enabled:false,generation_id:null};
 const c={S:{snapshot:{manual_copy:{}},manualRequest:{...config,action:'start'}},document:{querySelector:()=>button},
  $:id=>nodes[id],t:(ru,en)=>en,render(){},refresh:async()=>{},manualEvidenceMessage:code=>code||'Safe error',
  read:async(url,options)=>{calls.push({url,options});const next=responses.shift();if(next instanceof Error)throw next;return next;}};
 vm.createContext(c);vm.runInContext(js.slice(js.indexOf('function acceptManualConfig('),js.indexOf("document.addEventListener('visibilitychange'")),c);
 return {c,button,status,sheet,calls,config};
}
test('adding wallet never calls preview, analysis or START',async()=>{
 const h=harness([{leader:'0x'+'b'.repeat(40),enabled:false,generation_id:null,allocation_pct:85}]);
 await h.c.saveManualDraft();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/manual-copy');
 assert.equal(JSON.parse(h.calls[0].options.body).action,undefined);assert.equal(h.button.disabled,false);
 assert.equal(h.c.S.snapshot.manual_copy.ui_state,'READY');assert.match(h.status.textContent,/separate Start/);
});
for(const code of [400,409,503])test('START '+code+' restores button with inline error and no retry',async()=>{
 const error=new Error('HTTP_'+code);error.detail={code:'API_BUDGET_CONSTRAINED'};
 const h=harness([error]);await h.c.saveManual('start');
 assert.equal(h.calls.length,1);assert.equal(h.button.disabled,false);assert.equal(h.sheet.closed,false);
 assert.equal(h.status.textContent,'API_BUDGET_CONSTRAINED');
});
test('successful START closes only on authoritative enabled response',async()=>{
 const h=harness([{leader:'0x'+'b'.repeat(40),enabled:true,generation_id:'fresh',allocation_pct:85}]);
 await h.c.saveManual('start');assert.equal(h.sheet.closed,true);assert.equal(h.calls.length,1);
 assert.equal(h.c.S.snapshot.manual_copy.generation_id,'fresh');assert.equal(h.c.S.manualRequest,null);
});
test('lost START response is query-only and resolves persisted enabled state',async()=>{
 const h=harness([new Error('network'),{leader:'0x'+'b'.repeat(40),enabled:true,generation_id:'fresh',allocation_pct:85}]);
 await h.c.saveManual('start');assert.equal(h.calls.length,2);assert.equal(h.calls[1].options,undefined);
 assert.equal(h.sheet.closed,true);assert.equal(h.c.S.snapshot.manual_copy.generation_id,'fresh');
});
test('lost response with unknown state never enables blind retry',async()=>{
 const h=harness([new Error('network'),new Error('network')]);await h.c.saveManual('start');
 assert.equal(h.button.disabled,true);assert.equal(h.sheet.closed,false);assert.match(h.status.textContent,/No automatic retry/);
 assert.equal(h.calls.filter(x=>x.options?.method==='PUT').length,1);
});
test('in-flight double click creates only one request',async()=>{
 const h=harness([]);let finish;h.c.read=async()=>{h.calls.push(1);return new Promise(resolve=>finish=resolve);};
 const first=h.c.saveManual('start');await h.c.saveManual('start');
 finish({...h.config,enabled:true,generation_id:'fresh'});await first;assert.equal(h.calls.length,1);
});
test('saved draft has separate enabled START; no automatic historical analysis',()=>{
 const manual=js.slice(js.indexOf('function manual(){'),js.indexOf('function ',js.indexOf('function manual(){')+10));
 assert.match(manual,/data-manual-start/);assert.match(manual,/data-manual-analysis/);
 assert.doesNotMatch(manual,/loadManualAnalysis|\/analysis/);
 assert.match(js,/id="manual-action-status" role="alert"/);
});
test('historical quarantine blocks START but still permits adding a new leader draft',()=>{
 const manual=js.slice(js.indexOf('function manual(){'),js.indexOf('function ',js.indexOf('function manual(){')+10));
 assert.match(manual,/formBlocked=\['PENDING','UNKNOWN','ERROR'\]/);
 assert.match(manual,/startBlocked=!!m\.quarantine\?\.operator_review_required\|\|formBlocked/);
 assert.match(manual,/type="submit" \$\{formBlocked\?'disabled':''\}/);
 assert.match(manual,/data-manual-start \$\{startBlocked\?'disabled':''\}/);
});
