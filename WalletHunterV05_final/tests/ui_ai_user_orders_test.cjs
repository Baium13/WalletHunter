/* All requests mocked. These tests never create an exchange order. */
const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../webapp/static/ai-user-orders.js'),'utf8');
class Element{
  constructor(){this.isConnected=true;this.buttons=[];this.content='';}
  set innerHTML(value){this.content=value;this.buttons=[];
    for(const match of value.matchAll(/<button[^>]*data-ai-order-(prepare|confirm|decline)(?:="([^"]*)")?[^>]*>/g)){
      const listeners=[],prop='aiOrder'+match[1][0].toUpperCase()+match[1].slice(1);
      this.buttons.push({dataset:{[prop]:match[2]||''},kind:match[1],disabled:match[0].includes('disabled'),
        addEventListener:(name,callback)=>listeners.push(callback),async click(){if(!this.disabled)for(const callback of listeners)await callback();}});
    }
  }
  get innerHTML(){return this.content;}
  querySelectorAll(selector){return this.buttons.filter(button=>selector.includes(`-${button.kind}]`));}
}
function fixture(){
  let now=1788800000000,timerId=0;const timers=new Map(),events={};
  class TestDate extends Date{static now(){return now;}}
  const context={Date:TestDate,AbortController,console,walletHunterLanguage:'ru',walletHunterPrivacy:false,
    setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:key=>timers.delete(key),
    addEventListener:(name,fn)=>(events[name]||=[]).push(fn)};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);
  return {context,api:context.whAiUserOrders,timers,now:()=>now,advance:ms=>{now+=ms;},event:name=>(events[name]||[]).forEach(fn=>fn())};
}
function proposal(f){return {id:'proposal-1',status:'PENDING',created_ms:f.now(),expires_ms:f.now()+120000,payload:{
  action:'OPEN',coin:'BTC',dex:'',side:'LONG',direction:'LONG',order_type:'LIMIT_IOC',size:.00022,size_text:'0.0002200',
  limit_price:50000.01,limit_price_text:'50000.0100',leverage:40,margin_mode:'cross',network:'MAINNET',
  share_usdc:100,share_fraction:1/3,entry_pct_of_share:10,margin_cap_usdc:10,margin_estimate_usdc:.275,
  maximum_expected_margin_usdc:.27501,notional_usdc:11,estimated_fee_usdc:.005501,
  source:{probability_positive_net:.61,calibrated:false,model_version:3}},result:null};}
function data(f,withPending=true){return {status:withPending?'PENDING':'IDLE',reason:withPending?'confirmation_required':'no_signal',
  execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false,pending:withPending?[proposal(f)]:[],history:[]};}
function mount(f,value,options={}){const node=new Element();node.innerHTML=f.api.render(value);f.api.bind(node,options);return node;}
const button=(node,kind)=>node.buttons.find(row=>row.kind===kind);

test('frozen exact quantity price margin fees network and cross risk are visible before one-click confirmation',()=>{
  const f=fixture(),node=mount(f,data(f));const html=node.innerHTML;
  assert.match(html,/0.0002200/);assert.match(html,/50000.0100/);
  assert.match(html,/Основная сеть · реальные средства/);assert.match(html,/40×/);
  assert.match(html,/На вход · 10% своей доли/);assert.match(html,/не выше лимита биржи/);
  assert.match(html,/Маржа · оценка/);assert.match(html,/Комиссия входа · оценка/);
  assert.match(html,/CROSS: расчётная маржа — не максимальный убыток/);
  assert.match(html,/Доля 1\/3 — лимит расчёта, не изоляция денег/);
  assert.match(html,/Не калиброванная вероятность прибыли или исполнения/);
  assert.match(html,/не на этот немедленный IOC/);assert.doesNotMatch(html,/61%/);
  assert.match(html,/ни усреднения/);assert.match(html,/не разрешение на автономную торговлю/);
  assert.equal(button(node,'confirm').disabled,false);
});

test('complete order screen and known blocked reasons are English when selected',()=>{
  const f=fixture();f.context.walletHunterLanguage='en';
  for(const reason of ['confirmation_required','minimum_notional','position_conflict','open_order_conflict','instrument_owned_or_held',
    'account_missing','ai_slot_unavailable','too_many_wallets','model_unavailable','signal_already_handled','unsupported_capital_mode']){
    const value=data(f);value.reason=reason;const html=f.api.render(value);
    assert.doesNotMatch(html,/[А-Яа-яЁё]/);assert.match(html,/maximum loss/);
    assert.match(html,/Confirm · send order/);assert.match(html,/not permission for autonomous trading/);
  }
});

test('preparation sends only the read-only prepare request and never a decision',async()=>{
  const f=fixture(),calls=[];let reloads=0;
  const node=mount(f,data(f,false),{api:async(url,body)=>calls.push({url,body}),reload:async()=>{reloads++;}});
  await button(node,'prepare').click();
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/ai/orders/prepare');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{});assert.equal(reloads,1);
});

test('one confirm sends only proposal ID and strict true; stale pending reload cannot allow another send',async()=>{
  const f=fixture(),value=data(f),calls=[];let finish;
  const node=mount(f,value,{api:(url,body)=>{calls.push({url,body});return new Promise(resolve=>{finish=resolve;});},
    reload:async()=>{node.innerHTML=f.api.render(data(f));f.api.bind(node,{api:async()=>calls.push('unexpected')});}});
  const confirm=button(node,'confirm'),pending=confirm.click();
  await confirm.click();assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/ai/orders/proposal-1/decision');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{confirm:true});
  finish({});await pending;
  assert.equal(button(node,'confirm').disabled,true);await button(node,'confirm').click();assert.equal(calls.length,1);
  assert.match(node.innerHTML,/Повторная отправка этого предложения заблокирована/);
});

test('No sends strict false and durable declined history replaces the proposal',async()=>{
  const f=fixture(),value=data(f),calls=[];
  const node=mount(f,value,{api:async(url,body)=>calls.push({url,body}),reload:async()=>{
    const refreshed=data(f,false),declined=proposal(f);declined.status='DECLINED';refreshed.history=[declined];
    node.innerHTML=f.api.render(refreshed);f.api.bind(node,{});
  }});
  await button(node,'decline').click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{confirm:false});
  assert.equal(button(node,'confirm'),undefined);assert.match(node.innerHTML,/Отклонено вами/);
  assert.equal(calls.length,1);
});

test('privacy masks own quantity and financial amounts and disables confirmation but permits decline',async()=>{
  const f=fixture(),calls=[];f.context.walletHunterPrivacy=true;
  const node=mount(f,data(f),{api:async(url,body)=>calls.push({url,body})});
  assert.match(node.innerHTML,/Сумма скрыта/);assert.doesNotMatch(node.innerHTML,/0.0002200|100,00 USDC|11,00 USDC|0,55 USDC/);
  assert.match(node.innerHTML,/50000.0100/);assert.match(node.innerHTML,/Откройте суммы глазиком/);
  assert.equal(button(node,'confirm').disabled,true);assert.equal(button(node,'decline').disabled,false);
  await button(node,'confirm').click();assert.equal(calls.length,0);
  f.context.walletHunterPrivacy=false;f.event('whprivacy');assert.equal(button(node,'confirm').disabled,false);
});

test('expiry disables confirmation without waiting for another API response',async()=>{
  const f=fixture(),calls=[],value=data(f),node=mount(f,value,{api:async()=>calls.push(1)});
  const expiry=[...f.timers.values()].find(row=>row.delay>15000);assert.ok(expiry);
  f.advance(120010);expiry.callback();
  assert.match(node.innerHTML,/Срок предложения истёк/);assert.equal(button(node,'confirm').disabled,true);
  await button(node,'confirm').click();assert.equal(calls.length,0);
});

test('malformed unsupported below-minimum or changed frozen terms cannot be confirmed',async()=>{
  const cases=[p=>{p.coin='xyz:NVDA';},p=>{p.action='AVERAGE';},p=>{p.notional_usdc=9.99;},
    p=>{p.size_text='0.0022';},p=>{p.network='UNKNOWN';},p=>{p.leverage=41;},p=>{p.margin_cap_usdc=2;},
    p=>{p.entry_pct_of_share=1;},p=>{p.maximum_expected_margin_usdc=10.01;},
    p=>{p.margin_mode='isolated';},p=>{p.estimated_fee_usdc=null;},p=>{p.share_fraction=.5;}];
  for(const change of cases){const f=fixture(),value=data(f);change(value.pending[0].payload);let calls=0;
    const node=mount(f,value,{api:async()=>{calls++;}});assert.equal(button(node,'confirm').disabled,true);
    await button(node,'confirm').click();assert.equal(calls,0);
  }
});

test('timeout aborts after fifteen seconds then only rereads status, never retries the order',async()=>{
  const f=fixture(),calls=[];let reloads=0,signal;
  const node=mount(f,data(f),{api:(url,body,s)=>{calls.push({url,body});signal=s;return new Promise(()=>{});},reload:async()=>{reloads++;}});
  const pending=button(node,'confirm').click();
  const deadline=[...f.timers.values()].find(row=>row.delay===15000);assert.ok(deadline);deadline.callback();await pending;
  assert.equal(signal.aborted,true);assert.equal(calls.length,1);assert.equal(reloads,1);
  assert.match(node.innerHTML,/Ответ не подтверждён/);assert.equal(button(node,'confirm').disabled,true);
  await button(node,'confirm').click();assert.equal(calls.length,1);
});

test('minimum and occupied market explanations do not suggest automatic upsizing or another entry',()=>{
  const f=fixture(),value=data(f,false);value.reason='minimum_notional';
  assert.match(f.api.render(value),/ниже 10 USDC/);assert.match(f.api.render(value),/автоматически не увеличивается/);
  value.reason='position_conflict';assert.match(f.api.render(value),/Повторный вход заблокирован/);
});

test('untrusted identifiers and fields are escaped or rejected, private raw server errors are omitted',()=>{
  const f=fixture(),value=data(f);value.pending[0].id='../bad';value.pending[0].payload.coin='<img src=x onerror="bad()">';
  value.reason='SECRET_RAW_ERROR';value.pending[0].payload.source.private_key='SECRET_KEY';
  const html=f.api.render(value);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img|SECRET|\.\.\/bad/);
  assert.match(html,/data-ai-order-confirm="" disabled/);
});

test('language and privacy rerenders preserve frozen proposal and do not issue requests',()=>{
  const f=fixture();let calls=0;const node=mount(f,data(f),{api:async()=>{calls++;}});
  f.context.walletHunterLanguage='en';f.event('whlanguage');assert.doesNotMatch(node.innerHTML,/[А-Яа-яЁё]/);
  assert.match(node.innerHTML,/0.0002200/);f.context.walletHunterPrivacy=true;f.event('whprivacy');
  assert.match(node.innerHTML,/Amount hidden/);assert.equal(calls,0);
});
