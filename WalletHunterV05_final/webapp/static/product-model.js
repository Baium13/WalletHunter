/* Pure product-v1 presentation. No execution authority or legacy fallback. */
(function(root){
 'use strict';
 const number=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
 const modes=['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM'];
 const runtime=(s,m)=>(s?.runtimes||[]).find(r=>r.mode===m)||null;
 const scopeKey=s=>JSON.stringify([s?.tenant,s?.account,s?.network]);
 const liveId=(s,p)=>JSON.stringify([scopeKey(s),p.instrument,p.side,[...(p.order_ids||[])].sort()]);
 function positions(s,m){
  if(m==='LIVE')return (s?.account?.portfolio?.positions||[]).map(p=>{
   const actions=(s.account.actions||[]).filter(a=>(a.receipt?.order_ids||[]).some(id=>(p.order_ids||[]).includes(id)));
   return {...p,id:liveId(s.scope,p),mode:'LIVE',entry:p.entry_price,current_price:null,pnl:null,state:p.evidence==='VERIFIED'?'OPEN':'UNKNOWN',actions,
    timeline:actions.flatMap(a=>[
     {type:'ORDER_INTENT',timestamp:a.timestamp,intent_id:a.intent_id,correlation_id:a.correlation_id,evidence:a.intent},
     ...(a.risk?[{type:'RISK',timestamp:a.risk.created_ms,intent_id:a.intent_id,correlation_id:a.correlation_id,evidence:a.risk}]:[]),
     {type:'EXECUTION',timestamp:a.receipt?.received_ms,intent_id:a.intent_id,correlation_id:a.correlation_id,status:a.status,evidence:a.receipt}])};
  });
  return (runtime(s,m)?.episodes||[]).map(p=>({...p,id:p.episode_id}));
 }
 const isOpen=p=>['OPEN','INCREASED','REDUCED','HOLD','PARTIAL','UNKNOWN','RECONCILIATION_REQUIRED'].includes(p.state)||p.unresolved===true;
 const timeline=p=>[...(p.timeline||[])].filter(x=>x&&typeof x==='object').sort((a,b)=>(number(a.timestamp)||0)-(number(b.timestamp)||0));
 function curve(r,period,now){const duration={'24H':86400000,'7D':604800000,'30D':2592000000,ALL:Infinity}[period];let total=0;return (r?.episodes||[]).map(p=>p.outcome).filter(o=>o&&number(o.net_pnl)!==null&&number(o.exit_ms)!==null&&o.exit_ms>=now-duration&&o.exit_ms<=now).sort((a,b)=>a.exit_ms-b.exit_ms).map(o=>({time:o.exit_ms,value:total+=o.net_pnl}));}
 function mergeEvents(previous,incoming,reset=false){const map=new Map((reset?[]:previous).map(e=>[e.id,e]));for(const e of incoming||[])if(e.id&&Number.isSafeInteger(e.seq))map.set(e.id,e);return [...map.values()].sort((a,b)=>b.seq-a.seq).slice(0,200);}
 const proposalValid=(p,now)=>p?.status==='PENDING'&&/^[a-f0-9]{64}$/.test(p.proposal_hash||'')&&number(p.intent?.expires_ms)!==null&&p.intent.expires_ms>now;
 const candles=rows=>(rows||[]).filter(c=>['t','o','h','l','c'].every(k=>number(c[k])!==null)&&c.t>0&&c.l>0&&c.h>=Math.max(c.o,c.c)&&c.l<=Math.min(c.o,c.c)).sort((a,b)=>a.t-b.t).filter((c,i,a)=>i===0||c.t!==a[i-1].t);
 const markers=p=>(p.actions||[]).flatMap(a=>(a.receipt?.fills||[]).map(f=>({intent:a.intent_id,action:a.intent?.action,time:f.exchange_ms??f.timestamp??f.time,price:f.price,size:f.size}))).filter(f=>number(f.time)!==null&&number(f.price)!==null);
 function activityEvent(event){
  const d=event?.data||{},e=d.evidence||d,p=e.payload||e,c=p.consensus||p,trade=p.event||p;
  const kind=event.type||'',type=p.event_type||e.event_type||kind;
  const category=/RISK|AUTHORIZATION/.test(kind)?'RISK':/POSITION|ORDER|EXECUTION|OUTCOME|MANUAL_COPY|LIVE_CONFIRM/.test(kind)?'TRADING':/CONSENSUS|AGENT|ANALYSIS/.test(kind)?'AI':/LEADER|DISCOVER|RANKING|WALLET/.test(kind)?'LEADERS':'SYSTEM';
  const title=/CONSENSUS/.test(kind)?'AI_CONSENSUS':kind==='LEADER_EVENT'?'LEADER_ACTIVITY':
   /POSITION_(OPEN|ADD|REDUCE|REVERSE|CLOSE)$/.test(kind)?kind:
   ['LEADER_PROMOTED','LEADER_DEGRADED','DISCOVERY_UPDATED','WALLET_DISCOVERED','LEADER_ANALYZED','AGENT_UPDATED','RISK_UPDATED','ORDER_INTENT','EXECUTION_UPDATED','POSITION_UPDATED','OUTCOME_UPDATED','HEALTH_UPDATED','AUTHORIZATION_REQUIRED','LIVE_CONFIRM_REQUIRED'].includes(kind)?kind:'ACTIVITY_'+category;
  const wallet=trade.wallet||trade.leader||p.wallet;
  return {category,title,mode:d.mode||p.mode||'',symbol:trade.instrument?.symbol||p.instrument?.symbol||p.symbol||'',
   action:c.decision||trade.action||p.action||p.status||'',side:trade.side||p.side||'',
   confidence:number(c.confidence),wallet:typeof wallet==='string'&&/^0x[0-9a-f]{40}$/i.test(wallet)?wallet:null,
   technical:{event_id:event.id,correlation_id:e.correlation_id||p.correlation_id,intent_id:p.intent_id,type,evidence:d}};
 }
 const api={number,modes,runtime,scopeKey,liveId,positions,isOpen,timeline,curve,mergeEvents,proposalValid,candles,markers,activityEvent};if(typeof module==='object')module.exports=api;else root.WHModel=api;
})(typeof window==='object'?window:globalThis);
