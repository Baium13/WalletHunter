/* Wallet Hunter product shell.
 * Read-only presentation of canonical backend state.  It deliberately uses
 * N/A for missing evidence and never invents balances, leaders or activity.
 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { page: 'home', dashboard: null, intelligence: null, health: null, manual: null, report: null,
    loading: true, error: null, analyticsMode: 'PAPER', cursor: 0, selectedLeader: null, selectedPosition: null,
    chartRequest: 0, manualAnalysis: null, manualAnalysisSlot: null };
  const navItems = [
    ['home','⌂',['Главная','Home']], ['positions','◈',['Позиции','Positions']],
    ['manual','◎',['Manual Copy','Manual Copy']], ['leaders','◉',['Лидеры','Leaders']],
    ['agents','✦',['Агенты','Agents']], ['analytics','▥',['Аналитика','Analytics']],
    ['health','⌁',['Система','System']], ['settings','⚙',['Настройки','Settings']]
  ];
  const agentNames = ['Market Structure','Momentum','Volatility','Liquidity','Order Flow','Leader Intelligence','Risk Context'];
  const ru = s => s[0], en = s => s[1];
  const isEn = () => (window.walletHunterLanguage || localStorage.getItem('wh_lang') || 'ru') === 'en';
  const t = (r,e) => isEn() ? e : r;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => { const n = Number(value); return Number.isFinite(n) ? n : null; };
  const number = (value, digits=2) => { const n = finite(value); return n === null ? 'N/A' : n.toLocaleString(isEn()?'en-US':'ru-RU',{maximumFractionDigits:digits}); };
  const money = (value, own=false) => {
    if (own && (window.walletHunterPrivacy || localStorage.getItem('wh_privacy') === 'on')) return '••••••';
    const n = finite(value); return n === null ? 'N/A' : new Intl.NumberFormat(isEn()?'en-US':'ru-RU',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
  };
  const pct = (value, digits=1) => { const n = finite(value); return n === null ? 'N/A' : `${n>=0?'+':''}${n.toFixed(digits)}%`; };
  const time = value => { const n = finite(value); if (n === null || n <= 0) return 'N/A'; return new Date(n).toLocaleString(isEn()?'en-GB':'ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}); };
  const shortWallet = value => { const s=String(value||''); return s.length>13 ? `${s.slice(0,6)}…${s.slice(-4)}` : (s || 'N/A'); };
  const statusClass = value => { const s=String(value||'').toUpperCase(); return s.includes('UNHEALTH')||s.includes('ERROR')||s.includes('BLOCK')?'bad':s.includes('DEGRADED')||s.includes('WAIT')||s.includes('UNKNOWN')?'warn':'good'; };
  const headers = () => ({'Content-Type':'application/json','X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''});
  async function read(url, options={}) {
    const response = await fetch(url,{...options,headers:{...headers(),...(options.headers||{})},cache:'no-store'});
    const body = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(typeof body.detail==='string'?body.detail:(body.detail?.code||'UNAVAILABLE'));
    return body;
  }
  const modeValue = () => {
    const d=state.dashboard||{}, ai=state.ai||{};
    const explicit=d.autonomous_mode||d.mode||ai.mode;
    if (explicit) return String(explicit).toUpperCase().replace('_',' ');
    if (ai.modes?.trader?.enabled && ai.modes.trader.execution_mode === 'PAPER') return 'PAPER_AUTO';
    return 'OBSERVE';
  };
  const modeMeta = mode => {
    const m=String(mode||'OBSERVE').toUpperCase();
    if(m.includes('PAPER')) return {cls:'paper',title:'PAPER AUTO',note:t('Реальные средства не используются','NO REAL FUNDS AT RISK')};
    if(m.includes('SHADOW')) return {cls:'shadow',title:'SHADOW',note:t('Ордера не отправляются','NO ORDERS SUBMITTED')};
    if(m.includes('LIVE')) return {cls:'live',title:'LIVE CONFIRM',note:t('Реальное исполнение требует подтверждения','REAL EXECUTION REQUIRES CONFIRMATION')};
    return {cls:'observe',title:'OBSERVE',note:t('Только наблюдение и аналитика','ANALYSIS ONLY')};
  };
  function metric(label,value,cls='') { return `<div class="product-metric"><small>${esc(label)}</small><b class="${cls}">${esc(value)}</b></div>`; }
  function card(title, body, extra='') { return `<article class="product-card ${extra}"><h3>${title}</h3>${body}</article>`; }
  function unavailable(label) { return `<div class="product-empty">${esc(label||t('Подтверждённые данные недоступны','Verified data unavailable'))}</div>`; }
  function pageHeader(title, subtitle='') { return `<div class="product-section-head"><div><h2>${esc(title)}</h2>${subtitle?`<p>${esc(subtitle)}</p>`:''}</div><span class="product-fresh">${esc(time(Date.now()))}</span></div>`; }
  function positionRow(p) {
    const side=String(p.side||'').toUpperCase(), pnl=finite(p.unrealized_pnl), roe=finite(p.roe), own=true;
    const cls=(roe??pnl??0)>=0?'product-good':'product-bad';
    const source=p.origin?.strategy||p.origin?.controller||p.origin?.source||t('Позиция','Position');
    return `<div class="product-row" data-coin="${esc(p.coin)}" data-dex="${esc(p.dex||'')}"><div class="product-row-main"><b>${side==='SHORT'?'🔴':'🟢'} ${esc(p.coin||'N/A')} <span class="product-badge ${side==='SHORT'?'bad':'good'}">${esc(side||'N/A')}</span></b><small>${esc(source)} · ${esc(p.origin?.leader||p.origin?.strategy||t('источник неизвестен','source unavailable'))}</small></div><div class="product-row-end"><b class="${cls}">${money(pnl,own)}</b><small class="${cls}">${roe===null?'N/A':pct(roe)}</small></div><button class="product-btn" data-action="chart" aria-label="${esc(t('Открыть график','Open chart'))}">↗</button></div>`;
  }
  function leaderRow(leader,index) {
    const score=finite(leader.score), conf=finite(leader.confidence), status=String(leader.status||'N/A').toUpperCase();
    return `<tr data-leader="${esc(leader.wallet||'')}"><td><b>${index+1}</b></td><td><b>${esc(shortWallet(leader.wallet))}</b><small class="product-sub">${esc(leader.wallet||'N/A')}</small></td><td class="product-score">${score===null?'N/A':number(score,0)}</td><td>${conf===null?'N/A':pct(conf*100,0)}</td><td>${esc(status)}</td><td>${esc(time(leader.last_seen))}</td></tr>`;
  }
  function latestDecision() {
    const events=state.intelligence?.events||[];
    return events.find(e=>e.kind==='DECISION')?.body||null;
  }
  function agentsFromDecision() {
    const body=latestDecision(), rows=Array.isArray(body?.agents)?body.agents:[];
    return agentNames.map(name=>rows.find(a=>String(a.agent_id||a.agent||'').toLowerCase().includes(name.toLowerCase().split(' ')[0]))||null).map((row,i)=>({name:agentNames[i],row}));
  }
  function agentCard(item) {
    const a=item.row, confidence=finite(a?.confidence), score=finite(a?.score), direction=a?.direction||a?.state||'WAIT', status=a?String(a.status||a.freshness||'ONLINE').toUpperCase():'WAIT';
    const cls=statusClass(status); return `<article class="agent-card ${cls==='warn'?'wait':cls==='bad'?'degraded':''}"><div class="agent-state">${esc(status)}</div><h4>${esc(item.name)}</h4><div class="agent-value">${score===null?'N/A':number(score,0)} <small>${confidence===null?'':pct(confidence*100,0)}</small></div><p class="agent-evidence">${esc(direction)} · ${esc(a?.evidence?.summary||a?.evidence?.reason||a?.invalidation||t('Нет свежего результата','No fresh result'))}</p><small class="product-fresh">${esc(time(a?.timestamp_ms||a?.created_ms||a?.freshness_ms))}</small></article>`;
  }
  function networkSvg() {
    const labels=['Market','Leader','Structure','Momentum','Volatility','Liquidity','Order Flow','Leader Intel','Risk Context','Consensus','Risk','Execution'];
    const pts=[[9,50],[9,78],[28,18],[28,38],[28,58],[28,78],[28,94],[51,38],[51,72],[72,55],[87,55],[97,55]];
    const edges=pts.slice(0,-1).map((p,i)=>`<line class="product-edge ${latestDecision()?'active':''}" x1="${p[0]}%" y1="${p[1]}%" x2="${pts[i+1][0]}%" y2="${pts[i+1][1]}%"/>`).join('');
    const nodes=pts.map((p,i)=>`<g transform="translate(${p[0]} ${p[1]})"><circle class="product-node ${i>=7&&i<=9?'agent':''} ${i===9?'consensus':''}" r="${i===9?7:5}"/><text y="14">${esc(labels[i])}</text></g>`).join('');
    return `<div class="product-network" aria-label="${esc(t('Поток событий архитектуры','Architecture activity flow'))}"><svg viewBox="0 0 100 112" preserveAspectRatio="none">${edges}${nodes}</svg></div>`;
  }
  function eventRow(e) {
    const body=e.body||e, kind=e.kind||body.action||'EVENT', label={DECISION:t('Решение консенсуса','Consensus decision'),LEADER_ANALYZED:t('Лидер проанализирован','Leader analysed'),OPEN:t('Открытие позиции','Position opened'),ADD:t('Добор','Position increased'),REDUCE:t('Сокращение','Position reduced'),CLOSE:t('Закрытие позиции','Position closed'),POSITION_CLOSED:t('Позиция закрыта','Position closed'),RISK_APPROVED:t('Риск одобрен','Risk approved'),RISK_REJECTED:t('Риск отклонён','Risk rejected')}[kind]||kind;
    const detail=body.consensus?.decision||body.event?.action||body.wallet||body.event?.wallet||'';
    return `<div class="product-row"><div class="product-row-main"><b>${esc(label)}</b><small>${esc(detail)} · ${esc(body.correlation_id||body.event?.event_id||'')}</small></div><div class="product-row-end"><small>${esc(time(e.created||body.created_ms))}</small></div></div>`;
  }
  function home() {
    const d=state.dashboard||{}, intel=state.intelligence||{}, mode=modeMeta(modeValue()), positions=Array.isArray(d.positions)?d.positions:[], leaders=Array.isArray(intel.leaders)?intel.leaders:[], decision=latestDecision(), counts=intel.counts||{};
    const healthy=intel.health==='HEALTHY' && !d.balance_error;
    const top=leaders.slice(0,5), events=[...(d.events||[]).map(e=>({body:e,kind:e.action,created:e.time})),...(intel.events||[])].sort((a,b)=>(Number(b.created)||0)-(Number(a.created)||0)).slice(0,8);
    const decisionText=decision?.consensus?.decision||t('Нет актуального решения','No current decision');
    const summary=`<section class="product-section"><div class="product-grid">${metric(t('Баланс','Total balance'),money(d.balance,true))}${metric(t('Реализованный PnL','Realized PnL'),'N/A')}${metric(t('Нереализованный PnL','Unrealized PnL'),positions.length?money(positions.reduce((s,p)=>s+(finite(p.unrealized_pnl)||0),0),true):'N/A')}${metric(t('Открытые позиции','Open positions'),number(positions.length,0))}${metric(t('Активные лидеры','Active leaders'),number(counts.ACTIVE||counts.active,0))}${metric(t('Агенты онлайн','Agents online'),agentsFromDecision().filter(x=>x.row).length+'/'+agentNames.length)}${metric(t('Автономный бюджет','Autonomous budget'),'N/A')}${metric(t('Состояние риска','Risk status'),decision?.consensus?.decision||'N/A',statusClass(decision?.consensus?.decision)==='bad'?'product-bad':'')}</div></section>`;
    const consensusCard=card(t('Текущий консенсус','Current consensus'),`<div class="product-kicker">${esc(decision?.event?.instrument?.symbol||decision?.event?.instrument||'N/A')}</div><div class="product-value">${esc(decisionText)}</div><p class="product-sub">${esc(t('Решение отображается только при наличии свежих результатов агентов.','Displayed only when fresh agent results are available.'))}</p><div class="product-metrics">${metric(t('Уверенность','Confidence'),decision?.consensus?.confidence===undefined?'N/A':pct(Number(decision.consensus.confidence)*100,0))}${metric(t('Поддержка','Supporting agents'),number(decision?.consensus?.supporting?.length,0))}${metric(t('Против','Opposing agents'),number(decision?.consensus?.opposing?.length,0))}${metric(t('Версия политики','Policy version'),decision?.consensus?.policy_version||'N/A')}</div><button class="product-btn" data-page="agents">${esc(t('Открыть матрицу агентов','Open agent matrix'))}</button>`);
    const activityCard=card(t('Система следит за рынком','System activity'),networkSvg()+`<p class="product-sub">${esc(t('Пульсация отражает только реальные события потока.','Activity reflects real event flow only.'))}</p>`);
    const positionsCard=card(t('Открытые позиции','Open positions'),positions.length?`<div class="product-list">${positions.slice(0,5).map(positionRow).join('')}</div><div class="product-actions"><button class="product-btn" data-page="positions">${esc(t('Все позиции','All positions'))}</button></div>`:unavailable(t('Подтверждённых открытых позиций нет','No verified open positions')));
    const leadersCard=card(t('Топ лидеры','Top leaders'),top.length?`<div class="product-list">${top.map((l,i)=>`<div class="product-row" data-leader="${esc(l.wallet||'')}"><div class="product-row-main"><b>#${i+1} · ${esc(shortWallet(l.wallet))}</b><small>${esc(String(l.status||'N/A'))} · ${esc(t('последняя активность','last activity'))} ${esc(time(l.last_seen))}</small></div><div class="product-row-end"><b class="product-score">${finite(l.score)===null?'N/A':number(l.score,0)}</b><small>${finite(l.confidence)===null?'N/A':pct(Number(l.confidence)*100,0)}</small></div></div>`).join('')}</div><button class="product-btn" data-page="leaders">${esc(t('Открыть рейтинг','Open ranking'))}</button>`:unavailable(t('Рейтинг пока не заполнен','Ranking is not populated yet')));
    const activity=card(t('Последняя активность','Recent activity'),events.length?`<div class="product-list">${events.map(eventRow).join('')}</div>`:unavailable(t('Событий пока нет','No events yet')));
    return `<div class="product-banner ${mode.cls}"><span><strong>${esc(mode.title)}</strong> · ${esc(mode.note)}</span><span class="product-badge ${healthy?'good':'warn'}">● ${esc(healthy?t('СИСТЕМА В НОРМЕ','SYSTEM HEALTHY'):t('СОСТОЯНИЕ ПРОВЕРЯЕТСЯ','STATUS CHECK'))}</span></div>${summary}<section class="product-section"><div class="product-detail">${consensusCard}${activityCard}</div></section><section class="product-section"><div class="product-detail">${positionsCard}${leadersCard}</div></section><section class="product-section">${activity}</section>`;
  }
  function positionsPage() {
    const d=state.dashboard||{}, rows=Array.isArray(d.positions)?d.positions:[];
    const selected=state.selectedPosition&&rows.find(p=>String(p.coin)===String(state.selectedPosition.coin)&&String(p.dex||'')===String(state.selectedPosition.dex||''));
    const detail=selected?`<section class="product-section"><div class="product-card"><div class="product-section-head"><div><h3>${esc(selected.coin||'N/A')} · ${esc(selected.side||'N/A')}</h3><p>${esc(selected.origin?.strategy||t('Источник не подтверждён','Source unavailable'))}</p></div><button class="product-btn" data-action="close-detail">${esc(t('Назад','Back'))}</button></div><div class="product-chart" id="productPositionChart"><div class="product-empty">${esc(t('Загружаю свечи…','Loading candles…'))}</div></div><div class="product-metrics">${metric(t('Вход','Entry'),number(selected.entry_price))}${metric(t('Текущая цена','Current price'),number(selected.mark_price))}${metric(t('Маржа','Margin'),money(selected.margin_used,true))}${metric(t('Плечо','Leverage'),number(selected.leverage,0)+'x')}${metric(t('PnL','PnL'),money(selected.unrealized_pnl,true),((finite(selected.unrealized_pnl)||0)>=0?'product-good':'product-bad'))}${metric(t('PnL %','PnL %'),finite(selected.roe)===null?'N/A':pct(selected.roe),((finite(selected.roe)||0)>=0?'product-good':'product-bad'))}</div><p class="product-sub">${esc(t('Маркеры жизненного цикла строятся по durable events. Стопы и защиты показаны только при подтверждённом backend state.','Lifecycle markers use durable events. Stops and protection are shown only when confirmed by backend state.'))}</p></div></section>`:'';
    return `${pageHeader(t('Позиции','Positions'),t('Проверенное состояние счёта и жизненный цикл сделок','Verified account state and trade lifecycle'))}<div class="product-tabs"><button class="product-tab active">${esc(t('ВСЕ','ALL'))}</button><button class="product-tab">MANUAL LEADER</button><button class="product-tab">AUTONOMOUS</button><button class="product-tab">PAPER</button><button class="product-tab">SHADOW</button><button class="product-tab">LIVE</button></div>${detail}${rows.length?`<div class="product-list">${rows.map(positionRow).join('')}</div>`:unavailable(t('Открытых позиций нет или данные недоступны','No open positions or data unavailable'))}`;
  }
  function manualPage() {
    const d=state.dashboard||{}, wallets=(d.wallets||[]).filter(w=>w.configured), config=state.manual, leader=config?.configured?config:wallets[0], pctSaved=Number(config?.allocation_pct||localStorage.getItem('wh_manual_pct')||80), active=Boolean(config?.enabled);
    const capital=finite(d.balance), allocation=capital===null?null:capital*pctSaved/100;
    const leaderBody=leader?`<div class="product-kicker">${esc(t('РУЧНОЙ ЛИДЕР','MANUAL LEADER'))}</div><div class="product-value">${esc(config?.leader?shortWallet(config.leader):t('Слот','Slot')+' '+(leader.slot||'N/A'))}</div><p class="product-sub">${esc(config?.leader||t('Адрес скрыт в защищённом dashboard payload. Анализ доступен через экран Анализ.','Address is kept out of the dashboard payload. Use Analysis for public research.'))}</p><span class="product-badge ${active?'good':'warn'}">● ${esc(active?t('АКТИВЕН','ACTIVE'):t('ПАУЗА','PAUSED'))}</span>`:unavailable(t('Лидер не выбран','No leader selected'));
    const leaderCard=card(t('Выбранный лидер','Selected leader'),leaderBody+`<label class="product-label"><span>${esc(t('Адрес Hyperliquid лидера','Hyperliquid leader address'))}</span><input class="product-input" id="productManualLeader" placeholder="0x…" autocomplete="off"></label><div class="product-actions"><button class="product-btn primary" data-action="manual-add">${esc(t('Выбрать лидера','Select leader'))}</button></div><p class="product-note">${esc(t('Ручной лидер отделён от автоматического Discovery. Добавление не переносит владение уже открытыми позициями.','Manual Copy is separate from automatic Discovery. Adding never transfers provenance of existing positions.'))}</p>`);
    const allocationCard=card(t('Аллокация Manual Copy','Manual Copy allocation'),`<label class="product-label"><span>${esc(t('Доля капитала','Capital allocation'))}: <b id="manualPctValue">${number(pctSaved,0)}%</b></span><input id="manualPct" class="product-range" type="range" min="1" max="100" step="1" value="${Math.min(100,Math.max(1,pctSaved))}"></label><div class="product-metrics">${metric(t('Капитал счёта','Account capital'),money(capital,true))}${metric(t('Выделено','Allocated'),money(allocation,true))}${metric(t('Резерв / прочая ёмкость','Reserve / other capacity'),capital===null?'N/A':money(Math.max(0,capital-(allocation||0)),true))}${metric(t('Статус','Status'),active?t('АКТИВЕН','ACTIVE'):t('ПАУЗА','PAUSED'))}</div><p class="product-sub">${esc(t('100% означает 100% доступного торгового капитала, а не отказ от операционного резерва. Фактическая маржа и комиссии проходят через Risk Gateway.','100% means 100% of allocatable trading capital, not zero operational reserve. Margin and fees remain subject to the Risk Gateway.'))}<br>${esc(t('Позиции остановленного лидера остаются HOLD и продолжают занимать committed capital.','Stopped leader positions remain HOLD and continue consuming committed capital.'))}</p><div class="product-actions"><button class="product-btn ${active?'danger':'primary'}" data-action="manual-toggle">${esc(active?t('Остановить копирование','Stop copying'):t('Начать копирование','Start copying'))}</button></div>`);
    const a=state.manualAnalysisSlot===leader?.slot?state.manualAnalysis:null;
    const analysisBody=a?`<div class="product-kicker">${esc(t('ИСТОРИЧЕСКИЕ ДАННЫЕ','HISTORICAL DATA'))}</div><div class="product-value">${finite(a.rating)===null?'N/A':number(a.rating,0)}/100</div><p class="product-sub">${esc(t('Оценка качества истории, не прогноз прибыли.','Historical quality score, not a profit forecast.'))}</p><div class="product-metrics">${metric('PnL',money(a.net_pnl))}${metric('Profit Factor',a.profit_factor===undefined?'N/A':String(a.profit_factor))}${metric('Win rate',finite(a.win_rate)===null?'N/A':pct(a.win_rate,1))}${metric(t('Сделки','Trades'),finite(a.trades)===null?'N/A':number(a.trades,0))}${metric(t('Ожидание','Expectancy'),money(a.expectancy))}${metric(t('Просадка (proxy)','Drawdown (proxy)'),finite(a.drawdown)===null?'N/A':money(a.drawdown))}</div>`:unavailable(t('Подробные метрики появятся после свежего анализа лидера','Detailed metrics appear after a fresh leader analysis'));
    const analysisCard=card(t('Анализ выбранного лидера','Selected leader analysis'),analysisBody);
    return `${pageHeader(t('Manual Copy','Manual Copy'),t('Один выбранный лидер · отдельная аллокация · пропорциональное копирование','One selected leader · separate allocation · proportional copying'))}<div class="product-detail">${leaderCard}${allocationCard}</div><section class="product-section">${analysisCard}</section>`;
  }
  function leadersPage() {
    const intel=state.intelligence||{}, leaders=Array.isArray(intel.leaders)?intel.leaders:[], counts=intel.counts||{};
    const counter=(label,key)=>`<div class="product-metric"><small>${esc(label)}</small><b>${esc(number(counts[key]||counts[key.toLowerCase()],0))}</b></div>`;
    return `${pageHeader(t('Лидеры · Discovery','Leaders · Discovery'),t('Публичные данные, ранжирование и активный watchlist','Public evidence, ranking and active watchlist'))}<div class="product-grid three">${counter(t('Наблюдались','Observed'),'OBSERVED')}${counter(t('Кандидаты','Candidates'),'CANDIDATE')}${counter(t('Квалифицированы','Qualified'),'QUALIFIED')}${counter(t('Активны','Active'),'ACTIVE')}${counter(t('Пробация','Probation'),'PROBATION')}${counter(t('Сняты','Retired'),'RETIRED')}</div><section class="product-section"><div class="product-card"><div class="product-section-head"><div><h3>${esc(t('Рейтинг лидеров','Leader ranking'))}</h3><p>${esc(t('Score — качество исторических данных, не гарантия прибыли.','Score is historical quality evidence, not a profit guarantee.'))}</p></div><span class="product-badge ${statusClass(intel.health)}">${esc(intel.health||'N/A')}</span></div>${leaders.length?`<div class="product-table-wrap"><table class="product-table"><thead><tr><th>#</th><th>Wallet</th><th>Score</th><th>${esc(t('Уверенность','Confidence'))}</th><th>${esc(t('Статус','Status'))}</th><th>${esc(t('Последняя активность','Last activity'))}</th></tr></thead><tbody>${leaders.map(leaderRow).join('')}</tbody></table></div>`:unavailable(t('Discovery ещё не опубликовал лидеров','Discovery has not published leaders yet'))}</div></section>`;
  }
  function agentsPage() {
    const body=latestDecision(), consensus=body?.consensus;
    return `${pageHeader(t('Агенты и консенсус','Agents & consensus'),t('Реальный поток аналитических результатов · без доступа к исполнению','Real analytical outputs · no execution access'))}<div class="product-detail">${card(t('Архитектура активности','Activity architecture'),networkSvg()+`<p class="product-sub">${esc(t('Это визуализация потока данных, а не биологическая нейросеть.','This is a data-flow visualization, not a biological neural network.'))}`)}${card(t('Консенсус','Consensus'),`<div class="product-kicker">${esc(body?.event?.instrument?.symbol||'N/A')}</div><div class="product-value">${esc(consensus?.decision||t('ОЖИДАНИЕ','WAIT'))}</div><div class="product-metrics">${metric(t('Score','Score'),consensus?.score===undefined?'N/A':number(Number(consensus.score)*100,0)+'%')}${metric(t('Уверенность','Confidence'),consensus?.confidence===undefined?'N/A':pct(Number(consensus.confidence)*100,0))}${metric(t('Поддержка','Support'),number(consensus?.supporting?.length,0))}${metric(t('Блокеры','Blockers'),number(consensus?.blocking_conditions?.length,0))}</div><p class="product-sub">${esc(consensus?.policy_version||t('Версия политики недоступна','Policy version unavailable'))}</p>`)}</div><section class="product-section"><div class="agent-grid">${agentsFromDecision().map(agentCard).join('')}</div></section>`;
  }
  function analyticsPage() {
    const r=state.report||{}, mode=state.analyticsMode;
    const metrics=[['PnL',r.pnl], [t('Сделки','Trades'),r.trades], ['Win rate',r.win_rate===undefined?null:`${number(r.win_rate,1)}%`], ['Profit Factor','profit_factor' in r?r.profit_factor:null], ['Max DD',r.drawdown], [t('Комиссии','Fees'),r.fees], [t('Просадка','Exposure'),r.exposure]];
    return `${pageHeader(t('Аналитика · Performance Lab','Analytics · Performance Lab'),t('Режимы не смешиваются: каждое значение имеет собственную среду','Modes stay separate: every value belongs to one environment'))}<div class="product-tabs">${['PAPER','SHADOW','LIVE'].map(x=>`<button class="product-tab ${x===mode?'active':''}" data-analytics="${x}">${x}</button>`).join('')}</div><div class="product-banner ${mode.toLowerCase()}"><span><strong>${esc(mode)}</strong></span><span>${esc(mode==='PAPER'?t('Симулированное исполнение','Simulated execution'):mode==='SHADOW'?t('Гипотетический результат · ордера не отправляются','Hypothetical result · no orders submitted'):t('Реальные данные только при подтверждённом исполнении','Real data only for confirmed execution'))}</span></div><section class="product-section"><div class="product-detail">${card(t('Ключевые метрики','Key metrics'),`<div class="product-grid two">${metrics.map(x=>metric(x[0],x[1]===null||x[1]===undefined?'N/A':(typeof x[1]==='number'&&x[0]!=='Trades'&&x[0]!==t('Сделки','Trades')?money(x[1]):String(x[1])))).join('')}</div>`)}${card(t('Equity / cumulative PnL','Equity / cumulative PnL'),r.equity_curve?.length?`<div class="product-chart"><svg viewBox="0 0 500 220" preserveAspectRatio="none"><defs><linearGradient id="productArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#53e4e1" stop-opacity=".5"/><stop offset="1" stop-color="#53e4e1" stop-opacity="0"/></linearGradient></defs><polyline class="line" points="${r.equity_curve.map((v,i)=>`${(i/Math.max(1,r.equity_curve.length-1))*500},${200-(Math.max(0,Number(v)||0)/Math.max(1,...r.equity_curve.map(x=>Number(x)||0))*170)}`).join(' ')}"/></svg></div>`:unavailable(t('Кривая капитала недоступна','Equity curve unavailable')))}</div></section>`;
  }
  function healthPage() {
    const intel=state.intelligence||{}, d=state.dashboard||{}, status=(name,ok,detail)=>`<div class="health-item"><div><b>${esc(name)}</b><small>${esc(detail||'N/A')}</small></div><span class="health-status ${ok?'healthy':'degraded'}">${esc(ok?'HEALTHY':'DEGRADED')}</span></div>`;
    const okIntel=intel.health==='HEALTHY', okAccount=!!d.account&&!d.balance_error;
    return `${pageHeader(t('Система и здоровье','System health'),t('Приоритет: account data → risk → execution → reconciliation','Priority: account data → risk → execution → reconciliation'))}<div class="product-banner ${okIntel&&okAccount?'':'shadow'}"><strong>${esc(okIntel&&okAccount?t('Система работает штатно','System operating normally'):t('Требуется внимание','Attention required'))}</strong><span>${esc(intel.reason||d.balance_error||'N/A')}</span></div><section class="product-section"><div class="health-list">${status('Hyperliquid public data',okIntel,intel.last_success?time(intel.last_success):intel.reason)}${status('Private account data',okAccount,d.account||t('Аккаунт не подключён','Account not connected'))}${status('Wallet Discovery',okIntel,number(intel.leaders?.length,0)+' leaders')}${status('Active Watchlist',okIntel,number(intel.counts?.ACTIVE,0)+' active')}${status('Agents',!!latestDecision(),latestDecision()?t('Есть свежий результат','Fresh result available'):t('Нет свежего результата','No fresh result'))}${status('Consensus',!!latestDecision()?.consensus,latestDecision()?.consensus?.decision||'N/A')}${status('Risk / execution',true,t('Канонический gateway','Canonical gateway'))}${status('Event stream',okIntel,intel.cursor===undefined?'N/A':`cursor ${intel.cursor}`)}${status('Database',okIntel,t('Последняя проверка','Last check')+' '+time(Date.now()))}${status('Web/API',!!state.health?.ok,state.health?.ok?'HTTP OK':'N/A')}</div></section><p class="product-note">${esc(t('Интерфейс не считает HTTP-ответ сервера доказательством здоровья торговли. Критические состояния остаются видимыми при отключении UI.','A successful HTTP response alone does not prove trading health. Critical states remain visible when the UI disconnects.'))}</p>`;
  }
  function settingsPage() {
    const d=state.dashboard||{}, mode=modeValue();
    return `${pageHeader(t('Настройки','Settings'),t('Режим, сеть, приватность и уведомления','Mode, network, privacy and notifications'))}<div class="product-detail">${card(t('Operating mode','Operating mode'),`<div class="product-list">${[['OBSERVE',t('Только наблюдение','Analysis only')],['PAPER_AUTO',t('Автоматический PAPER без реальных средств','Autonomous PAPER without real funds')],['SHADOW',t('Гипотетический результат · без отправки','Hypothetical · no submission')],['LIVE_CONFIRM',t('LIVE только после подтверждения','LIVE only after confirmation')]].map(x=>`<div class="product-row"><div class="product-row-main"><b>${esc(x[0].replace('_',' '))}</b><small>${esc(x[1])}</small></div><span class="product-badge ${mode.replace(' ','_')===x[0]?'good':'warn'}">${mode.replace(' ','_')===x[0]?esc(t('ВКЛЮЧЁН','ACTIVE')):esc(t('ДОСТУПЕН ЧЕРЕЗ BACKEND','BACKEND CONTROL'))}</span></div>`).join('')}</div><p class="product-note">${esc(t('LIVE_AUTO заблокирован и не доступен как обычный режим.','LIVE_AUTO is locked and unavailable as a normal mode.'))}</p>`)}${card(t('Параметры счёта','Account settings'),`<div class="product-metrics">${metric(t('Сеть','Network'),d.execution_scope?.network||'N/A')}${metric(t('Счёт','Account'),d.account||'N/A')}${metric(t('Копирование','Copy'),d.copy_enabled?t('ВКЛ','ON'):t('ПАУЗА','PAUSED'))}${metric(t('Уведомления','Notifications'),d.notifications?t('ВКЛ','ON'):t('ВЫКЛ','OFF'))}</div><div class="product-actions"><button class="product-btn" data-action="privacy">${esc(t('Переключить приватность сумм','Toggle amount privacy'))}</button></div>`)}</div>`;
  }
  function candleSvg(candles) {
    const rows=candles.slice(-70).filter(c=>[c.o,c.h,c.l,c.c].every(v=>finite(v)!==null));
    if(!rows.length)return unavailable(t('Свечи недоступны','Candles unavailable'));
    const lo=Math.min(...rows.map(c=>Number(c.l))), hi=Math.max(...rows.map(c=>Number(c.h))), span=hi-lo||1, w=500, step=w/rows.length;
    const glyphs=rows.map((c,i)=>{const x=i*step+step/2, y=v=>12+(hi-v)/span*190, green=Number(c.c)>=Number(c.o), top=Math.min(y(c.o),y(c.c)), bottom=Math.max(y(c.o),y(c.c));return `<line x1="${x.toFixed(2)}" y1="${y(c.h).toFixed(2)}" x2="${x.toFixed(2)}" y2="${y(c.l).toFixed(2)}" stroke="${green?'#58e6a0':'#ff768d'}"/><rect x="${(x-step*.28).toFixed(2)}" y="${top.toFixed(2)}" width="${(step*.56).toFixed(2)}" height="${Math.max(1,bottom-top).toFixed(2)}" fill="${green?'#58e6a0':'#ff768d'}" opacity=".85"/>`;}).join('');
    return `<svg viewBox="0 0 ${w} 215" preserveAspectRatio="none" aria-label="${esc(t('Свечной график','Candlestick chart'))}">${glyphs}</svg>`;
  }
  async function loadPositionChart() {
    const selected=state.selectedPosition, target=$('productPositionChart'), request=++state.chartRequest;
    if(!selected||!target)return;
    try{const q=new URLSearchParams({coin:String(selected.coin||''),dex:String(selected.dex||''),interval:'15m',hours:'24'});const data=await read('/api/chart?'+q);if(request!==state.chartRequest||!$('productPositionChart'))return;target.innerHTML=candleSvg(Array.isArray(data.candles)?data.candles:[]);}catch(e){if(request===state.chartRequest)target.innerHTML=unavailable(t('Рынок временно недоступен','Market temporarily unavailable'));}
  }
  function shell() {
    document.body.classList.add('product-mode');
    const main=document.querySelector('main'); if(!main)return;
    document.querySelector('body > nav')?.remove();
    main.innerHTML=`<div class="product-main"><header class="product-header"><div class="product-brand"><img src="/static/assets/skull-trader.png" alt="Wallet Hunter"><div><small>WALLET HUNTER</small><h1>${esc(t('Командный центр','Command center'))}</h1></div></div><div class="product-header-spacer"></div><div id="productMode" class="product-pill observe"><i></i><span>OBSERVE</span></div><button id="productLanguage" class="product-icon" type="button" aria-label="Language">RU</button><button id="productPrivacy" class="product-icon" type="button" aria-label="${esc(t('Скрыть суммы','Hide amounts'))}">◉</button><button id="productSound" class="product-icon" type="button" aria-label="Sound">◖</button></header><div id="productBanner"></div><div id="productContent" class="product-content"></div><p class="product-footer-note">${esc(t('Wallet Hunter · данные только из подтверждённого backend state','Wallet Hunter · data from verified backend state only'))}</p></div><nav id="productNav" class="product-nav">${navItems.map(x=>`<button data-product-page="${x[0]}"><span>${x[1]}</span>${esc(t(ru(x[2]),en(x[2])))}</button>`).join('')}</nav>`;
    bind();
  }
  function render() {
    const content=$('productContent'); if(!content)return;
    const pages={home,positions:positionsPage,manual:manualPage,leaders:leadersPage,agents:agentsPage,analytics:analyticsPage,health:healthPage,settings:settingsPage};
    content.innerHTML=`<section class="product-page active">${(pages[state.page]||home)()}</section>`;
    document.querySelectorAll('[data-product-page]').forEach(b=>b.classList.toggle('active',b.dataset.productPage===state.page));
    const mode=modeMeta(modeValue()), pill=$('productMode'); if(pill){pill.className=`product-pill ${mode.cls}`;pill.innerHTML=`<i></i><span>${esc(mode.title)}</span>`;}
    const languageButton=$('productLanguage'); if(languageButton)languageButton.textContent=isEn()?'EN':'RU';
    const soundButton=$('productSound'); if(soundButton)soundButton.textContent=localStorage.getItem('wh_sound')==='off'?'◌':'◖';
    document.querySelectorAll('[data-analytics]').forEach(b=>b.onclick=()=>{state.analyticsMode=b.dataset.analytics;loadReport().then(render);});
    document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{state.page=b.dataset.page;render();});
    document.querySelectorAll('[data-action="chart"]').forEach(b=>b.onclick=()=>{const row=b.closest('[data-coin]');state.selectedPosition={coin:row.dataset.coin,dex:row.dataset.dex};state.page='positions';render();loadPositionChart();});
    document.querySelectorAll('[data-action="close-detail"]').forEach(b=>b.onclick=()=>{state.selectedPosition=null;render();});
    const range=$('manualPct'), rangeValue=$('manualPctValue'); if(range){range.oninput=()=>{localStorage.setItem('wh_manual_pct',range.value);if(rangeValue)rangeValue.textContent=`${range.value}%`;};}
    document.querySelectorAll('[data-leader]').forEach(row=>row.onclick=()=>{state.selectedLeader=row.dataset.leader;});
    if(state.page==='positions'&&state.selectedPosition)loadPositionChart();
    const manualLeader=(state.dashboard?.wallets||[]).find(w=>w.configured);
    if(state.page==='manual'&&manualLeader&&state.manualAnalysisSlot!==manualLeader.slot)loadManualAnalysis(manualLeader.slot);
  }
  async function loadReport(){ try{state.report=await read(`/api/account/report?days=${state.analyticsMode==='PAPER'?30:90}`);}catch(_){state.report=null;} }
  async function loadManualAnalysis(slot){
    state.manualAnalysisSlot=slot; state.manualAnalysis=null;
    try{state.manualAnalysis=await read(`/api/analyse/${encodeURIComponent(slot)}`);}catch(_){state.manualAnalysis=null;}
    if(state.page==='manual')render();
  }
  async function refresh() {
    state.loading=true; state.error=null; render();
    const [dashboard,intel,health,manual]=await Promise.allSettled([read('/api/dashboard'),read(`/api/intelligence?after=${state.cursor}&limit=50`),read('/health'),read('/api/manual-copy')]);
    if(dashboard.status==='fulfilled')state.dashboard=dashboard.value;
    if(intel.status==='fulfilled'){state.intelligence=intel.value;state.cursor=Number(intel.value.cursor)||state.cursor;}
    if(health.status==='fulfilled')state.health=health.value;
    if(manual.status==='fulfilled')state.manual=manual.value;
    try{state.ai=await read('/api/ai');}catch(_){state.ai=null;}
    if(state.page==='analytics')await loadReport();
    state.loading=false; if(dashboard.status==='rejected'&&intel.status==='rejected')state.error=t('Данные backend недоступны','Backend data unavailable'); render();
  }
  async function manualAdd(){const input=$('productManualLeader'),address=input?.value.trim();if(!address)return alert(t('Введите адрес лидера','Enter a leader address'));try{await read('/api/manual-copy',{method:'PUT',body:JSON.stringify({leader:address,allocation_pct:Number($('manualPct')?.value||80)})});await refresh();}catch(e){alert(e.message);}}
  async function manualToggle(){const enabled=Boolean(state.manual?.enabled);if(enabled&&!confirm(t('Остановить новые сделки? Открытые позиции останутся HOLD.','Stop new trades? Open positions remain HOLD.')))return;try{await read('/api/manual-copy',{method:'PUT',body:JSON.stringify({action:enabled?'stop':'start'})});await refresh();}catch(e){alert(e.message);}}
  function bind(){
    document.querySelectorAll('[data-product-page]').forEach(b=>b.onclick=()=>{state.page=b.dataset.productPage;render();});
    $('productLanguage')?.addEventListener('click',()=>window.toggleLanguage?.());
    $('productPrivacy')?.addEventListener('click',()=>{const next=!(window.walletHunterPrivacy||localStorage.getItem('wh_privacy')==='on');window.walletHunterPrivacy=next;localStorage.setItem('wh_privacy',next?'on':'off');render();});
    $('productSound')?.addEventListener('click',()=>{const muted=localStorage.getItem('wh_sound')==='off';localStorage.setItem('wh_sound',muted?'on':'off');$('productSound').textContent=muted?'◖':'◌';});
    if(!document.body.dataset.productEvents){document.addEventListener('click',e=>{const action=e.target.closest('[data-action]')?.dataset.action;if(action==='manual-add')manualAdd();if(action==='manual-toggle')manualToggle();if(action==='privacy'){const b=$('productPrivacy');b?.click();}});document.body.dataset.productEvents='1';}
  }
  window.addEventListener('whlanguage',()=>{if(document.body.classList.contains('product-mode')){shell();render();}});
  window.whProduct={refresh,render,state};
  const boot=()=>{shell();refresh();setInterval(()=>{if(!document.hidden)refresh();},10000);};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
