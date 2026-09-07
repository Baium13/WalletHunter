/* All requests are mocks; no exchange actions are performed. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../webapp/static/ai-position-actions.js'),'utf8');
class Element{constructor(){this.isConnected=true;this.content='';this.buttons=[];}
  set innerHTML(value){this.content=value;this.buttons=[];for(const m of value.matchAll(/<button[^>]*data-position-(prepare|confirm|decline|release)(?:="([^"]*)")?[^>]*>/g)){
    const listeners=[],prop='position'+m[1][0].toUpperCase()+m[1].slice(1);this.buttons.push({kind:m[1],dataset:{[prop]:m[2]||''},disabled:m[0].includes('disabled'),
      addEventListener:(name,fn)=>listeners.push(fn),async click(){if(!this.disabled)for(const fn of listeners)await fn();}});}}
  get innerHTML(){return this.content;}querySelectorAll(selector){return this.buttons.filter(b=>selector.includes(`-${b.kind}]`));}}
function fixture(){let now=1788800000000,seq=0;const timers=new Map(),events={};class TestDate extends Date{static now(){return now;}}
  const context={Date:TestDate,AbortController,console,walletHunterLanguage:'ru',walletHunterPrivacy:false,
    setTimeout:(callback,delay)=>{timers.set(++seq,{callback,delay});return seq;},clearTimeout:id=>timers.delete(id),addEventListener:(name,fn)=>(events[name]||=[]).push(fn)};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);return{context,api:context.whAiPositionActions,timers,now:()=>now,advance:n=>now+=n,event:name=>(events[name]||[]).forEach(fn=>fn())};}
function proposal(f,action='REDUCE',id='position-1'){return{id,status:'PENDING',created_ms:f.now(),expires_ms:f.now()+90000,payload:{action,coin:'BTC',dex:'',direction:'LONG',side:'LONG',network:'MAINNET',margin_mode:'cross',
  size:.25,size_text:'0.2500',limit_price:74.5,limit_price_text:'74.500',reference_price:75,leverage:2,order_type:'LIMIT_IOC',reduce_only:action==='REDUCE',is_buy:action==='AVERAGE',
  position_before:{coin:'BTC',dex:'',side:'LONG',size:1,entry_price:100,leverage:2,margin_mode:'cross',roe:-50,margin_used:50,unrealized_pnl:-25},
  expected_after:{size:action==='REDUCE'?.75:1.25,entry_price:action==='REDUCE'?100:95,margin_estimate_usdc:action==='REDUCE'?28.125:46.875},
  action_notional_usdc:18.75,margin_change_estimate_usdc:action==='REDUCE'?-9.375:9.375,realized_pnl_estimate_usdc:action==='REDUCE'?-6.25:0,estimated_fee_usdc:.009375,
  source_wallet:'0x'+'a'.repeat(40),source_slot_usdc:100,source_reserved_usdc:50,extra_used_usdc:0,extra_remaining_usdc:50,additions_used:0,additions_remaining:4,
  factors:{trend_ema20_50:{ema20:80,ema50:70},rsi14:40,macd_hist:1,atr14_pct:2,volume_ratio20:1.2,levels20:{support:74,resistance:90},funding_bps_hour:.1,open_interest:123000,candle_close_ms:f.now()-1000},
  hypothesis:{classification:action==='REDUCE'?'RISK_REDUCTION':'EXPERIMENTAL_REBOUND',probability:null,probability_label:'success_not_guaranteed'},
  alternatives:[{action:'LOWER_LEVERAGE',available:false,reason:'cross_or_unimplemented'}],
  stop_impact:{open_orders_count:0,stop_orders_count:0,policy:'no_automatic_stop_changes',reason:'no_orders'}}};}
function data(f,pending=true){return{status:pending?'PENDING':'IDLE',reason:pending?'confirmation_required':'no_triggered_positions',execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false,
  pending:pending?[proposal(f)]:[],history:[],availability:[]};}
function mount(f,value,options={}){const node=new Element();node.innerHTML=f.api.render(value);f.api.bind(node,options);return node;}
const buttons=(node,kind)=>node.buttons.filter(b=>b.kind===kind),button=(node,kind)=>buttons(node,kind)[0];

test('deep-loss reduction shows eight factors exact frozen action and before-after financial consequences',()=>{
  const f=fixture(),node=mount(f,data(f)),html=node.innerHTML;
  assert.match(html,/ROE -50%/);assert.match(html,/Сократить позицию/);assert.match(html,/0.2500/);assert.match(html,/74.500/);
  assert.match(html,/Сейчас/);assert.match(html,/После · оценка/);assert.match(html,/Результат после комиссии · оценка/);
  assert.match(html,/-6,25 USDC/);assert.match(html,/До 50% доли и до 4 доборов/);
  assert.match(html,/Вероятность восстановления: неизвестна/);assert.match(html,/не гарантируется/);
  assert.match(html,/CROSS: общим обеспечением/);assert.match(html,/не устанавливает стоп/);
  assert.match(html,/копирование этого инструмента приостанавливается/);assert.match(html,/−120% не является биржевым стопом/);
  assert.equal((html.match(/<dt>/g)||[]).length,8);assert.equal(button(node,'confirm').disabled,false);
});
test('new position card is fully English and never promotes fake calibrated probabilities',()=>{
  const f=fixture();f.context.walletHunterLanguage='en';const value=data(f);value.pending[0].payload.hypothesis.probability=.99;
  const html=f.api.render(value);assert.doesNotMatch(html,/[А-Яа-яЁё]/);assert.doesNotMatch(html,/99%|0.99/);
  assert.match(html,/Recovery probability: unknown/);assert.match(html,/does not permit automatic additions/);
});
test('AVERAGE shows additional risk and source reserve but unsupported margin and leverage variants have no action buttons',()=>{
  const f=fixture(),value=data(f);value.pending=[proposal(f,'AVERAGE')];const node=mount(f,value),html=node.innerHTML;
  assert.match(html,/увеличивает объём риска/);assert.match(html,/Добавить к позиции/);assert.match(html,/9,375%/);
  assert.equal(button(node,'confirm').disabled,false);assert.equal(buttons(node,'confirm').length,1);
  assert.match(html,/Снизить плечо/);assert.match(html,/здесь пока не поддерживаются/);
});
test('prepare is read-only and no decision is sent just by rendering refreshing or changing language',async()=>{
  const f=fixture(),calls=[];let reloads=0;const node=mount(f,data(f,false),{api:async(url,body)=>calls.push({url,body}),reload:async()=>{reloads++;}});
  f.context.walletHunterLanguage='en';f.event('whlanguage');assert.equal(calls.length,0);
  await button(node,'prepare').click();assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/ai/positions/prepare');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{});assert.equal(reloads,1);
});
test('confirmation sends only exact proposal ID and strict boolean and blocks stale alternative for the same position',async()=>{
  const f=fixture(),value=data(f),calls=[];value.pending.push(proposal(f,'AVERAGE','position-2'));let finish;
  const node=mount(f,value,{api:(url,body)=>{calls.push({url,body});return new Promise(resolve=>{finish=resolve;});},reload:async()=>{}});
  const send=button(node,'confirm').click();await buttons(node,'confirm')[1].click();assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/ai/positions/position-1/decision');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{confirm:true});
  finish({});await send;assert.ok(buttons(node,'confirm').every(b=>b.disabled));
  await buttons(node,'confirm')[1].click();assert.equal(calls.length,1);
});
test('No persists decline through authoritative reload without any confirmation request',async()=>{
  const f=fixture(),calls=[],value=data(f);const node=mount(f,value,{api:async(url,body)=>calls.push({url,body}),reload:async()=>{
    const response=data(f,false),old=proposal(f);old.status='DECLINED';response.history=[old];node.innerHTML=f.api.render(response);f.api.bind(node,{});}});
  await button(node,'decline').click();assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{confirm:false});
  assert.equal(buttons(node,'confirm').length,0);assert.match(node.innerHTML,/Отклонено вами/);
});
test('privacy hides quantity margin PnL and source money, preserves percentages, and blocks confirm not decline',async()=>{
  const f=fixture();let calls=0;f.context.walletHunterPrivacy=true;const node=mount(f,data(f),{api:async()=>{calls++;}});
  assert.match(node.innerHTML,/Сумма скрыта/);assert.doesNotMatch(node.innerHTML,/0.2500|-6,25 USDC|100,00 USDC|50,00 USDC/);
  assert.match(node.innerHTML,/ROE -50%/);assert.match(node.innerHTML,/74.500/);assert.equal(button(node,'confirm').disabled,true);
  assert.equal(button(node,'decline').disabled,false);await button(node,'confirm').click();assert.equal(calls,0);
  f.context.walletHunterPrivacy=false;f.event('whprivacy');assert.equal(button(node,'confirm').disabled,false);
});
test('expired incomplete protected small or inconsistent scenarios cannot expose a working Confirm',async()=>{
  const changes=[p=>{p.position_before.roe=-39.99;},p=>{p.action_notional_usdc=9.99;},p=>{p.stop_impact.stop_orders_count=1;},
    p=>{p.factors.rsi14=null;},p=>{p.size_text='0.5';},p=>{p.expected_after.size=.99;},p=>{p.network='unknown';}];
  for(const change of changes){const f=fixture(),value=data(f);change(value.pending[0].payload);let calls=0;
    const node=mount(f,value,{api:async()=>{calls++;}});assert.equal(button(node,'confirm').disabled,true);await button(node,'confirm').click();assert.equal(calls,0);}
  const f=fixture(),node=mount(f,data(f));f.advance(90010);[...f.timers.values()][0].callback();assert.equal(button(node,'confirm').disabled,true);
  assert.match(node.innerHTML,/Предложение истекло/);
});
test('extra budget and addition count violations block averaging',()=>{
  for(const change of [p=>p.additions_remaining=0,p=>p.additions_used=4,p=>p.extra_remaining_usdc=1,p=>p.extra_used_usdc=49]){
    const f=fixture(),value=data(f);value.pending=[proposal(f,'AVERAGE')];change(value.pending[0].payload);
    assert.equal(button(mount(f,value),'confirm').disabled,true);
  }
});

test('rescue starts at ROE minus40 inclusive and never at minus39.99 in either language',async()=>{
  for(const language of ['ru','en'])for(const action of ['REDUCE','AVERAGE']){
    const f=fixture(),value=data(f);f.context.walletHunterLanguage=language;value.pending=[proposal(f,action)];
    value.pending[0].payload.position_before.roe=-40;let requests=0;
    const node=mount(f,value,{api:async()=>{requests++;}});
    assert.equal(button(node,'confirm').disabled,false);assert.match(node.innerHTML,/ROE ≤−40%/);
    assert.doesNotMatch(node.innerHTML,/≤−50%/);
    value.pending[0].payload.position_before.roe=-39.99;node.innerHTML=f.api.render(value);f.api.bind(node,{api:async()=>{requests++;}});
    assert.equal(button(node,'confirm').disabled,true);await button(node,'confirm').click();assert.equal(requests,0);
    const empty=data(f,false);assert.match(f.api.render(empty),/ROE ≤−40%/);
  }
});

test('all displayed source budget and market fields must be verified before confirmation',async()=>{
  const mutations=[p=>delete p.reference_price,p=>p.reference_price=0,p=>delete p.source_wallet,p=>p.source_wallet='0x123',
    p=>delete p.source_reserved_usdc,p=>p.source_reserved_usdc=-1,p=>delete p.extra_used_usdc,p=>delete p.extra_remaining_usdc,
    p=>delete p.additions_used,p=>p.additions_remaining=5,p=>p.additions_used=.5,p=>p.position_before.coin='ETH',
    p=>p.position_before.dex='xyz',p=>p.position_before.margin_mode='isolated',p=>delete p.factors.candle_close_ms,
    p=>{p.coin=p.position_before.coin='SOL';},p=>{p.coin=p.position_before.coin='xyz:INTC';},
    p=>{p.size=.3;p.size_text='0.3000';p.expected_after.size=.7;},p=>p.margin_change_estimate_usdc=1];
  for(const change of mutations){const f=fixture(),value=data(f);change(value.pending[0].payload);let requests=0;
    const node=mount(f,value,{api:async()=>{requests++;}});assert.equal(button(node,'confirm').disabled,true);
    await button(node,'confirm').click();assert.equal(requests,0);}
});

test('rescue quarter limits stay separate from independent AI policy and reduction remains available when its share is overallocated',()=>{
  for(const change of [p=>p.margin_change_estimate_usdc=12.5001,p=>{p.extra_remaining_usdc=36;p.margin_change_estimate_usdc=9.001;}]){
    const f=fixture(),value=data(f);value.pending=[proposal(f,'AVERAGE')];change(value.pending[0].payload);
    assert.equal(button(mount(f,value),'confirm').disabled,true);
  }
  const f=fixture(),value=data(f),p=value.pending[0].payload;p.source_reserved_usdc=150;p.additions_used=4;p.additions_remaining=0;
  p.leverage=p.position_before.leverage=40;
  assert.equal(button(mount(f,value),'confirm').disabled,false);
  p.coin=p.position_before.coin='xyz:INTC';p.dex=p.position_before.dex='xyz';
  assert.equal(button(mount(f,value),'confirm').disabled,false);
});

test('derived quantity float tails are compact but frozen execution strings remain exact',()=>{
  const f=fixture(),value=data(f),p=value.pending[0].payload;p.expected_after.size=.014100000000000001;
  const html=f.api.render(value);assert.doesNotMatch(html,/014100000000000001/);assert.match(html,/0,0141/);
  assert.match(html,/0.2500/);assert.match(html,/74.500/);
});

const held=(status='FILLED',can_release=true)=>({market:'BTC|',proposal_id:'position-held',status,can_release});
test('resume appears only for a strict releasable verified hold and never for unresolved or malformed data',()=>{
  for(const hold of [held('UNKNOWN',true),held('SUBMITTING',true),held('FILLED',false),held('FILLED','true'),
    {...held(),market:'BTC|xyz'},{...held(),proposal_id:'../bad'},{...held(),market:'<img src=x>|'}]){
    const f=fixture(),value=data(f,false);value.holds=[hold];const node=mount(f,value);
    assert.equal(buttons(node,'release').length,0);assert.match(node.innerHTML,/копирование остаётся на паузе/);assert.doesNotMatch(node.innerHTML,/<img/);
  }
  const f=fixture(),value=data(f,false);value.holds=[held('PARTIAL')];assert.equal(button(mount(f,value),'release').disabled,false);
});

test('manual resume requires the separate synchronization warning and sends only the market once',async()=>{
  const f=fixture(),value=data(f,false),calls=[],warnings=[];value.holds=[held()];let accepted=false,reloads=0;
  f.context.confirm=text=>{warnings.push(text);return accepted;};
  const node=mount(f,value,{api:async(url,body)=>calls.push({url,body}),reload:async()=>{reloads++;}});
  assert.equal(calls.length,0);await button(node,'release').click();assert.equal(calls.length,0);
  assert.match(warnings[0],/синхронизирует позицию с кошельком-источником/);assert.match(warnings[0],/увеличить или закрыть/);
  accepted=true;await button(node,'release').click();assert.equal(calls.length,1);assert.equal(reloads,1);
  assert.equal(calls[0].url,'/api/ai/positions/resume');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{market:'BTC|'});
  assert.equal(button(node,'release').disabled,true);await button(node,'release').click();assert.equal(calls.length,1);
  assert.match(node.innerHTML,/Запрос возобновления уже отправлен/);
});

test('resume timeout aborts after fifteen seconds and rereads but never repeats the POST',async()=>{
  const f=fixture(),value=data(f,false);value.holds=[held()];f.context.confirm=()=>true;let count=0,reloads=0,signal;
  const node=mount(f,value,{api:(url,body,s)=>{count++;signal=s;return new Promise(()=>{});},reload:async()=>{reloads++;}});
  const sending=button(node,'release').click();[...f.timers.values()].find(timer=>timer.delay===15000).callback();await sending;
  assert.equal(signal.aborted,true);assert.equal(count,1);assert.equal(reloads,1);assert.equal(button(node,'release').disabled,true);
  await button(node,'release').click();assert.equal(count,1);
});

test('resume re-renders in English and respects privacy without hidden network requests',async()=>{
  const f=fixture(),value=data(f,false);value.holds=[held()];f.context.walletHunterPrivacy=true;let count=0;f.context.confirm=()=>true;
  const node=mount(f,value,{api:async()=>{count++;}});assert.equal(button(node,'release').disabled,true);
  f.context.walletHunterLanguage='en';f.event('whlanguage');assert.doesNotMatch(node.innerHTML,/[А-Яа-яЁё]/);
  assert.match(node.innerHTML,/may increase or close the position/);await button(node,'release').click();assert.equal(count,0);
  f.context.walletHunterPrivacy=false;f.event('whprivacy');assert.equal(button(node,'release').disabled,false);assert.equal(count,0);
});

test('authoritative removal hides a released hold while disabled assistance cannot prepare new proposals',async()=>{
  const f=fixture(),value=data(f,false);value.holds=[held()];f.context.confirm=()=>true;let count=0;
  const node=mount(f,value,{api:async()=>{count++;},reload:async()=>{node.innerHTML=f.api.render({...value,holds:[]});f.api.bind(node,{});}});
  await button(node,'release').click();assert.equal(count,1);assert.equal(buttons(node,'release').length,0);
  const disabled=data(f,false);disabled.reason='rescue_disabled';assert.equal(button(mount(f,disabled),'prepare').disabled,true);
});
test('timeout aborts once and rereads status without retrying either action or stale alternative',async()=>{
  const f=fixture(),value=data(f),calls=[];value.pending.push(proposal(f,'AVERAGE','position-2'));let signal,reloads=0;
  const node=mount(f,value,{api:(url,body,s)=>{signal=s;calls.push({url,body});return new Promise(()=>{});},reload:async()=>{reloads++;}});
  const send=button(node,'confirm').click();[...f.timers.values()].find(row=>row.delay===15000).callback();await send;
  assert.equal(signal.aborted,true);assert.equal(calls.length,1);assert.equal(reloads,1);assert.ok(buttons(node,'confirm').every(b=>b.disabled));
});
test('information-only unavailable scenarios cannot create hidden financial actions',()=>{
  const f=fixture(),value=data(f,false);value.availability=[{coin:'xyz:NVDA',action:'AVERAGE',available:false,reason:'stop_review_required'},
    {coin:'BTC',action:'REDUCE',available:false,reason:'minimum_notional'}];const node=mount(f,value);
  assert.match(node.innerHTML,/Требуется ручная проверка/);assert.match(node.innerHTML,/10 USDC/);assert.equal(buttons(node,'confirm').length,0);
});
test('untrusted identifiers raw errors factors and probabilities remain inert and do not disclose private data',()=>{
  const f=fixture(),value=data(f);value.pending[0].id='../bad';value.pending[0].payload.coin='<img src=x onerror="bad()">';
  value.reason='PRIVATE_RAW_ERROR';value.pending[0].payload.factors.rsi14='<script>bad()</script>';value.pending[0].payload.private_key='SECRET';
  const html=f.api.render(value);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<script|<img|PRIVATE_RAW_ERROR|SECRET|\.\.\/bad/);
  assert.match(html,/data-position-confirm="" disabled/);
});
