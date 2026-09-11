/* Pure product-v1 presentation. No execution authority or legacy fallback. */
(function(root){
 'use strict';
 const number=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
 const modes=['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM'];
 const runtime=(s,m)=>(s?.runtimes||[]).find(r=>r.mode===m)||null;
 const analysis=(s,m)=>{const own=runtime(s,m==='LIVE'?'LIVE_CONFIRM':m);return own?.agents?.length?own:s?.analysis||null;};
 const scopeKey=s=>JSON.stringify([s?.tenant,s?.account,s?.network]);
 const liveId=(s,p)=>JSON.stringify([scopeKey(s),p.instrument,p.side,[...(p.order_ids||[])].sort()]);
 function positions(s,m){
  if(m==='LIVE')return (s?.account?.portfolio?.positions||[]).map(p=>{
   const actions=(s.account.actions||[]).filter(a=>(a.receipt?.order_ids||[]).some(id=>(p.order_ids||[]).includes(id)));
   // The account snapshot is authoritative for the position itself, while
   // current_price/pnl are optional live marks refreshed by the read-only UI
   // price feed. Preserve them when available instead of resetting them on
   // every product snapshot/render.
   const current=number(p.current_price??p.mark_price),entry=number(p.entry_price??p.entry),size=number(p.size);
   const markedPnl=number(p.pnl)??(current!==null&&entry!==null&&size!==null&&['LONG','SHORT'].includes(p.side)?(current-entry)*size*(p.side==='LONG'?1:-1):null);
   // Exchange-observed exposure is an open position even when provenance is
   // still UNKNOWN. Keep provenance/evidence separate from the lifecycle
   // badge so a missing ownership link cannot hide a real live position.
   // A row in the LIVE account portfolio is already proof that exposure exists.
   // Ownership/provenance may still be UNKNOWN (for example after a restart),
   // and the mark feed may be temporarily unavailable.  Do not turn that
   // known exchange exposure into an UNKNOWN lifecycle badge.  Keep the
   // evidence field untouched so the audit state remains truthful.
   // Every row here came from the authoritative LIVE account portfolio, so
   // its lifecycle is OPEN even when ownership/provenance and the mark feed
   // are temporarily unavailable.  Keep those evidence fields untouched.
   const lifecycle=(p.state&&p.state!=='UNKNOWN')?p.state:'OPEN';
   return {...p,id:liveId(s.scope,p),mode:'LIVE',entry:entry,current_price:current,margin:p.margin??p.margin_used,notional:p.notional??p.position_value,pnl:markedPnl,state:lifecycle,actions,
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
 const api={number,modes,runtime,analysis,scopeKey,liveId,positions,isOpen,timeline,curve,mergeEvents,proposalValid,candles,markers,activityEvent};if(typeof module==='object')module.exports=api;else root.WHModel=api;
})(typeof window==='object'?window:globalThis);
