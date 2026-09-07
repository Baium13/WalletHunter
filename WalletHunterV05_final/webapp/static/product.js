/* Wallet Hunter product shell.
 * Read-only presentation of canonical backend state. It deliberately uses
 * N/A for missing evidence and never invents balances, leaders or activity.
 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {
    page: 'home', dashboard: null, intelligence: null, health: null, manual: null,
    ai: null, report: null, reportAt: 0, loading: true, error: null,
    analyticsMode: 'PAPER', positionFilter: 'ALL', chartInterval: '15m', chartHours: 24,
    cursor: 0, selectedLeader: null, selectedPosition: null, chartRequest: 0,
    manualAnalysis: null, manualAnalysisSlot: null, eventHistory: [], streamStatus: 'IDLE'
  };
  const navItems = [
    ['home', '⌂', ['Главная', 'Home']], ['positions', '◈', ['Позиции', 'Positions']],
    ['manual', '◎', ['Manual Copy', 'Manual Copy']], ['leaders', '◉', ['Лидеры', 'Leaders']],
    ['agents', '✦', ['Агенты', 'Agents']], ['analytics', '▥', ['Аналитика', 'Analytics']],
    ['health', '⌁', ['Система', 'System']], ['settings', '⚙', ['Настройки', 'Settings']]
  ];
  const agentNames = [
    ['structure', ['Структура рынка', 'Market Structure']], ['momentum', ['Импульс', 'Momentum']],
    ['volatility', ['Волатильность', 'Volatility']], ['liquidity', ['Ликвидность', 'Liquidity']],
    ['order_flow', ['Поток ордеров', 'Order Flow']], ['leader', ['Интеллект лидера', 'Leader Intelligence']],
    ['risk_context', ['Контекст риска', 'Risk Context']]
  ];
  const lifecycleLabels = {
    OPEN: ['Открытие позиции', 'Position opened'], ADD: ['Добор', 'Position increased'],
    REDUCE: ['Сокращение', 'Position reduced'], CLOSE: ['Закрытие позиции', 'Position closed'],
    REVERSE: ['Разворот', 'Position reversed'], POSITION_OPENED: ['Позиция открыта', 'Position opened'],
    POSITION_INCREASED: ['Позиция увеличена', 'Position increased'], POSITION_REDUCED: ['Позиция сокращена', 'Position reduced'],
    POSITION_CLOSED: ['Позиция закрыта', 'Position closed'], LEADER_TRADE: ['Сделка лидера', 'Leader trade'],
    LEADER_PROMOTED: ['Лидер повышен', 'Leader promoted'], LEADER_DEGRADED: ['Лидер понижен', 'Leader degraded'],
    DECISION: ['Решение консенсуса', 'Consensus decision'], RISK_APPROVED: ['Риск одобрен', 'Risk approved'],
    RISK_REJECTED: ['Риск отклонён', 'Risk rejected'], ORDER_SUBMITTED: ['Ордер отправлен', 'Order submitted'],
    EXECUTION_UNKNOWN: ['Исполнение не подтверждено', 'Execution unknown']
  };
  const t = (ru, en) => (isEn() ? en : ru);
  const isEn = () => (window.walletHunterLanguage || storage.get('wh_lang') || 'ru') === 'en';
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
  };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const finite = value => { const n = Number(value); return Number.isFinite(n) ? n : null; };
  const privateMode = () => Boolean(window.walletHunterPrivacy || storage.get('wh_privacy') === 'on');
  const number = (value, digits = 2, own = false) => {
    if (own && privateMode()) return '••••••';
    const n = finite(value);
    return n === null ? 'N/A' : n.toLocaleString(isEn() ? 'en-US' : 'ru-RU', { maximumFractionDigits: digits });
  };
  const money = (value, own = false) => {
    if (own && privateMode()) return '••••••';
    const n = finite(value);
    return n === null ? 'N/A' : new Intl.NumberFormat(isEn() ? 'en-US' : 'ru-RU', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
  };
  const pct = (value, digits = 1) => { const n = finite(value); return n === null ? 'N/A' : `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`; };
  const scorePct = value => { const n = finite(value); return n === null ? 'N/A' : `${Math.round(Math.abs(n) <= 1 ? n * 100 : n)}%`; };
  const time = value => { const n = finite(value); if (n === null || n <= 0) return 'N/A'; return new Date(n).toLocaleString(isEn() ? 'en-GB' : 'ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); };
  const shortWallet = value => { const s = String(value || ''); return s.length > 13 ? `${s.slice(0, 6)}…${s.slice(-4)}` : (s || 'N/A'); };
  const walletDisplay = value => privateMode() ? '••••••' : shortWallet(value);
  const statusClass = value => { const s = String(value || '').toUpperCase(); return s.includes('UNHEALTH') || s.includes('ERROR') || s.includes('BLOCK') || s.includes('FAIL') ? 'bad' : s.includes('DEGRADED') || s.includes('WAIT') || s.includes('UNKNOWN') || s.includes('CAUTION') ? 'warn' : s === 'N/A' || s === 'IDLE' ? 'muted' : 'good'; };
  const headers = () => ({ 'Content-Type': 'application/json', 'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || '' });
  async function read(url, options = {}) {
    const response = await fetch(url, { ...options, headers: { ...headers(), ...(options.headers || {}) }, cache: 'no-store' });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === 'string' ? body.detail : body.detail?.code;
      throw new Error(detail && !/secret|private|key|token/i.test(detail) ? detail : 'UNAVAILABLE');
    }
    return body;
  }
  const modeValue = () => {
    const d = state.dashboard || {}, ai = state.ai || {};
    const explicit = d.autonomous_mode || d.mode || ai.mode;
    if (explicit) return String(explicit).toUpperCase();
    if (ai.modes?.trader?.enabled === true && ai.modes.trader.execution_mode === 'PAPER') return 'PAPER_AUTO';
    return 'OBSERVE';
  };
  const modeMeta = mode => {
    const m = String(mode || 'OBSERVE').toUpperCase();
    if (m.includes('PAPER')) return { cls: 'paper', title: 'PAPER AUTO', note: t('Реальные средства не используются', 'NO REAL FUNDS AT RISK') };
    if (m.includes('SHADOW')) return { cls: 'shadow', title: 'SHADOW', note: t('Ордера не отправляются', 'NO ORDERS SUBMITTED') };
    if (m.includes('LIVE')) return { cls: 'live', title: 'LIVE CONFIRM', note: t('Реальное исполнение требует подтверждения', 'REAL EXECUTION REQUIRES CONFIRMATION') };
    return { cls: 'observe', title: 'OBSERVE', note: t('Только наблюдение и аналитика', 'ANALYSIS ONLY') };
  };
  const eventBody = e => e?.body || e || {};
  const eventKind = e => String(e?.kind || e?.action || eventBody(e).kind || eventBody(e).action || 'EVENT').toUpperCase();
  const eventTime = e => finite(e?.created || e?.time || e?.created_ms || eventBody(e).created_ms || eventBody(e).exchange_ms);
  const eventId = e => String(e?.seq || e?.id || eventBody(e).event_id || eventBody(e).correlation_id || `${eventKind(e)}:${eventTime(e) || ''}`);
  function mergeEvents(rows) {
    const merged = new Map(state.eventHistory.map(e => [eventId(e), e]));
    (rows || []).forEach(e => merged.set(eventId(e), e));
    state.eventHistory = [...merged.values()].sort((a, b) => (eventTime(b) || 0) - (eventTime(a) || 0)).slice(0, 240);
  }
  const recentActivity = () => state.eventHistory.some(e => { const at = eventTime(e); return at !== null && Date.now() - at >= 0 && Date.now() - at < 120000; });
  function metric(label, value, cls = '') { return `<div class="product-metric"><small>${esc(label)}</small><b class="${cls}">${esc(value)}</b></div>`; }
  function card(title, body, extra = '') { return `<article class="product-card ${extra}"><h3>${esc(title)}</h3>${body}</article>`; }
  function unavailable(label) { return `<div class="product-empty">${esc(label || t('Подтверждённые данные недоступны', 'Verified data unavailable'))}</div>`; }
  function pageHeader(title, subtitle = '') { return `<div class="product-section-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div><span class="product-fresh">${esc(time(Date.now()))}</span></div>`; }
  function positionSource(p) {
    const o = p?.origin || {}, raw = String(o.strategy || o.controller || o.source || '').toLowerCase();
    if (raw.includes('manual_leader')) return 'MANUAL LEADER';
    if (raw.includes('autonomous') || raw.includes('intelligence') || raw.includes('ai')) return 'AUTONOMOUS';
    if (raw.includes('copy')) return 'COPY';
    return o.strategy || o.controller || t('Источник неизвестен', 'SOURCE UNKNOWN');
  }
  function positionMode(p) { return String(p?.mode || p?.execution_mode || p?.origin?.mode || p?.origin?.execution_mode || 'N/A').toUpperCase(); }
  function positionLeader(p) { return p?.origin?.leader || p?.origin?.leader_wallet || null; }
  function positionMatches(p) {
    const filter = state.positionFilter, source = positionSource(p).toUpperCase(), mode = positionMode(p);
    if (filter === 'ALL') return true;
    if (filter === 'MANUAL') return source.includes('MANUAL');
    if (filter === 'AUTONOMOUS') return source.includes('AUTONOMOUS');
    if (filter === 'COPY') return source.includes('COPY');
    return mode === filter;
  }
  function positionRow(p) {
    const side = String(p.side || '').toUpperCase(), pnl = finite(p.unrealized_pnl), roe = finite(p.roe), cls = (roe ?? pnl ?? 0) >= 0 ? 'product-good' : 'product-bad';
    const source = positionSource(p), leader = positionLeader(p), mode = positionMode(p);
    return `<div class="product-row position-row" data-coin="${esc(p.coin)}" data-dex="${esc(p.dex || '')}" tabindex="0"><div class="product-row-main"><b>${side === 'SHORT' ? '🔴' : side === 'LONG' ? '🟢' : '⚪'} ${esc(p.coin || 'N/A')} <span class="product-badge ${side === 'SHORT' ? 'bad' : side === 'LONG' ? 'good' : 'warn'}">${esc(side || 'N/A')}</span></b><small>${esc(source)} · ${esc(leader ? walletDisplay(leader) : t('лидер не подтверждён', 'leader unavailable'))} · ${esc(mode)}</small></div><div class="product-row-end"><b class="${cls}">${money(pnl, true)}</b><small class="${cls}">${roe === null ? 'N/A' : pct(roe)}</small></div><button class="product-btn" data-action="chart" aria-label="${esc(t('Открыть детали позиции', 'Open position details'))}">↗</button></div>`;
  }
  function latestDecision() { return state.eventHistory.find(e => eventKind(e) === 'DECISION')?.body || null; }
  function agentLabel(id) { const row = agentNames.find(a => a[0] === id); return row ? t(row[1][0], row[1][1]) : id || t('Агент', 'Agent'); }
  function agentsFromDecision() {
    const rows = Array.isArray(latestDecision()?.agents) ? latestDecision().agents : [];
    return agentNames.map(([id]) => ({ id, row: rows.find(a => String(a.agent_id || a.agent || '').toLowerCase() === id || String(a.agent_id || a.agent || '').toLowerCase().includes(id)) || null }));
  }
  function agentCard(item) {
    const a = item.row, confidence = finite(a?.confidence), score = finite(a?.score), direction = a?.direction || a?.state || 'WAIT', freshness = String(a?.freshness || 'UNKNOWN').toUpperCase(), status = a ? (freshness === 'FRESH' ? 'ONLINE' : freshness) : 'WAIT';
    const cls = statusClass(status);
    return `<article class="agent-card ${cls === 'warn' ? 'wait' : cls === 'bad' ? 'degraded' : ''}" data-agent="${esc(item.id)}"><div class="agent-state ${cls}">${esc(status)}</div><h4>${esc(agentLabel(item.id))}</h4><div class="agent-value">${score === null ? 'N/A' : esc(scorePct(score))} <small>${confidence === null ? 'N/A' : esc(pct(confidence * 100, 0))}</small></div><p class="agent-evidence">${esc(direction)} · ${esc(Array.isArray(a?.evidence) ? a.evidence.join(', ') : a?.evidence?.summary || a?.evidence?.reason || a?.invalidation || t('Нет свежего результата', 'No fresh result'))}</p><small class="product-fresh">${esc(time(a?.created_ms || a?.timestamp_ms))} · ${esc(a?.feature_version || a?.strategy_version || 'N/A')}</small></article>`;
  }
  function networkSvg() {
    const labels = [t('Рынок', 'Market'), t('Лидер', 'Leader'), t('Структура', 'Structure'), t('Импульс', 'Momentum'), t('Волатильность', 'Volatility'), t('Ликвидность', 'Liquidity'), t('Поток ордеров', 'Order Flow'), t('Интеллект лидера', 'Leader Intel'), t('Контекст риска', 'Risk Context'), t('Консенсус', 'Consensus'), t('Риск', 'Risk'), t('Исполнение', 'Execution')];
    const pts = [[9, 50], [9, 78], [28, 18], [28, 38], [28, 58], [28, 78], [28, 94], [51, 38], [51, 72], [72, 55], [87, 55], [97, 55]], active = recentActivity();
    const edges = pts.slice(0, -1).map((p, i) => `<line class="product-edge ${active ? 'active' : ''}" x1="${p[0]}%" y1="${p[1]}%" x2="${pts[i + 1][0]}%" y2="${pts[i + 1][1]}%"/>`).join('');
    const nodes = pts.map((p, i) => `<g transform="translate(${p[0]} ${p[1]})"><circle class="product-node ${i >= 7 && i <= 9 ? 'agent' : ''} ${i === 9 ? 'consensus' : ''}" r="${i === 9 ? 7 : 5}"/><text y="14">${esc(labels[i])}</text></g>`).join('');
    return `<div class="product-network" aria-label="${esc(t('Поток событий архитектуры', 'Architecture activity flow'))}"><svg viewBox="0 0 100 112" preserveAspectRatio="none">${edges}${nodes}</svg></div>`;
  }
  function eventRow(e) {
    const body = eventBody(e), kind = eventKind(e), labels = lifecycleLabels[kind] || [kind, kind], detail = body.consensus?.decision || body.event?.action || body.action || body.wallet || body.event?.wallet || '';
    return `<div class="product-row"><div class="product-row-main"><b>${esc(t(labels[0], labels[1]))}</b><small>${esc(detail)}${eventId(e) ? ` · ${esc(eventId(e).slice(0, 12))}` : ''}</small></div><div class="product-row-end"><small>${esc(time(eventTime(e)))}</small></div></div>`;
  }
  function performanceChart(report) {
    const values = Array.isArray(report?.equity_curve) ? report.equity_curve.map(finite).filter(v => v !== null) : [];
    if (values.length < 2) return unavailable(t('Кривая капитала недоступна для этого режима', 'Equity curve unavailable for this mode'));
    const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
    return `<div class="product-chart"><svg viewBox="0 0 500 220" preserveAspectRatio="none"><defs><linearGradient id="productArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#53e4e1" stop-opacity=".5"/><stop offset="1" stop-color="#53e4e1" stop-opacity="0"/></linearGradient></defs><polyline class="area" points="${values.map((v, i) => `${(i / (values.length - 1)) * 500},${205 - ((v - lo) / span) * 180}`).join(' ')} 500,215 0,215"/><polyline class="line" points="${values.map((v, i) => `${(i / (values.length - 1)) * 500},${205 - ((v - lo) / span) * 180}`).join(' ')}"/></svg></div>`;
  }
  function consensusCard() {
    const body = latestDecision(), c = body?.consensus, decision = c?.decision || t('Нет актуального решения', 'No current decision');
    return card(t('Текущий консенсус', 'Current consensus'), `<div class="product-kicker">${esc(body?.event?.instrument?.symbol || body?.event?.instrument || 'N/A')}</div><div class="product-value ${statusClass(decision) === 'bad' ? 'product-bad' : statusClass(decision) === 'warn' ? 'product-warn' : ''}">${esc(decision)}</div><p class="product-sub">${esc(t('Консенсус — аналитическое решение, не разрешение на исполнение.', 'Consensus is analysis, not execution authorization.'))}</p><div class="product-metrics">${metric(t('Уверенность', 'Confidence'), c?.confidence === undefined ? 'N/A' : pct(Number(c.confidence) * 100, 0))}${metric(t('Поддержка', 'Supporting agents'), number(c?.supporting?.length, 0))}${metric(t('Против', 'Opposing agents'), number(c?.opposing?.length, 0))}${metric(t('Блокеры', 'Blockers'), number(c?.blockers?.length || c?.blocking_conditions?.length, 0))}${metric(t('Версия политики', 'Policy version'), c?.policy_id || c?.policy_version || 'N/A')}</div><button class="product-btn" data-product-page="agents">${esc(t('Открыть матрицу агентов', 'Open agent matrix'))}</button>`);
  }
  function home() {
    const d = state.dashboard || {}, intel = state.intelligence || {}, mode = modeMeta(modeValue()), positions = Array.isArray(d.positions) ? d.positions : [], leaders = Array.isArray(intel.leaders) ? intel.leaders : [], decision = latestDecision(), counts = intel.counts || {}, report = state.report;
    const healthy = intel.health === 'HEALTHY' && !d.balance_error, realized = finite(d.realized_pnl ?? report?.pnl), unrealized = positions.map(p => finite(p.unrealized_pnl)).filter(v => v !== null).reduce((a, b) => a + b, 0);
    const top = leaders.slice(0, 5), events = state.eventHistory.slice(0, 8), agents = agentsFromDecision().filter(x => x.row).length;
    const paper = state.ai?.modes?.trader?.paper, autonomousBudget = finite(paper?.budget_usdc ?? paper?.initial_budget_usdc);
    const summary = `<section class="product-section"><div class="product-grid">${metric(t('Баланс счёта', 'Account balance'), money(d.balance, true))}${metric(t('Реализованный PnL', 'Realized PnL'), realized === null ? 'N/A' : money(realized, true))}${metric(t('Нереализованный PnL', 'Unrealized PnL'), positions.length ? money(unrealized, true) : 'N/A', unrealized >= 0 ? 'product-good' : 'product-bad')}${metric(t('Открытые позиции', 'Open positions'), number(positions.length, 0))}${metric(t('Активные лидеры', 'Active leaders'), number(counts.ACTIVE ?? counts.active, 0))}${metric(t('Агенты онлайн', 'Agents online'), `${agents}/${agentNames.length}`)}${metric(t('Автономный бюджет', 'Autonomous budget'), autonomousBudget === null ? 'N/A' : money(autonomousBudget, true))}${metric(t('Состояние риска', 'Risk status'), decision?.consensus?.decision || 'N/A', statusClass(decision?.consensus?.decision))}</div><p class="product-sub product-data-note">${esc(t('PnL счёта показывается только при наличии подтверждённого account report; PAPER и SHADOW имеют отдельные среды.', 'Account PnL is shown only from a verified account report; PAPER and SHADOW remain separate environments.'))}</p></section>`;
    const positionsCard = card(t('Открытые позиции', 'Open positions'), positions.length ? `<div class="product-list">${positions.slice(0, 5).map(positionRow).join('')}</div><div class="product-actions"><button class="product-btn" data-product-page="positions">${esc(t('Все позиции', 'All positions'))}</button></div>` : unavailable(t('Подтверждённых открытых позиций нет', 'No verified open positions')));
    const leadersCard = card(t('Топ лидеры', 'Top leaders'), top.length ? `<div class="product-list">${top.map((l, i) => `<div class="product-row" data-action="leader-detail" data-leader="${esc(l.wallet || '')}" tabindex="0"><div class="product-row-main"><b>#${i + 1} · ${esc(walletDisplay(l.wallet))}</b><small>${esc(String(l.status || 'N/A'))} · ${esc(t('последняя активность', 'last activity'))} ${esc(time(l.last_seen))}</small></div><div class="product-row-end"><b class="product-score">${esc(scorePct(l.score))}</b><small>${esc(finite(l.confidence) === null ? 'N/A' : pct(Number(l.confidence) * 100, 0))}</small></div></div>`).join('')}</div><button class="product-btn" data-product-page="leaders">${esc(t('Открыть рейтинг', 'Open ranking'))}</button>` : unavailable(t('Рейтинг пока не заполнен', 'Ranking is not populated yet')));
    const activity = card(t('Последняя активность', 'Recent activity'), events.length ? `<div class="product-list">${events.map(eventRow).join('')}</div>` : unavailable(t('Событий пока нет', 'No events yet')));
    const manual = state.manual?.configured ? card(t('Manual Copy', 'Manual Copy'), `<div class="product-kicker">${esc(state.manual.enabled ? t('АКТИВЕН', 'ACTIVE') : t('ПАУЗА', 'PAUSED'))}</div><div class="product-value">${esc(walletDisplay(state.manual.leader))}</div><p class="product-sub">${esc(t('Отдельная аллокация · остановка оставляет позиции HOLD.', 'Separate allocation · stopping leaves positions on HOLD.'))}<br>${esc(number(state.manual.allocation_pct, 0))}%</p><button class="product-btn" data-product-page="manual">${esc(t('Открыть Manual Copy', 'Open Manual Copy'))}</button>`) : card(t('Manual Copy', 'Manual Copy'), unavailable(t('Лидер ещё не выбран', 'No leader selected')) + `<button class="product-btn" data-product-page="manual">${esc(t('Настроить', 'Configure'))}</button>`);
    return `<div class="product-banner ${mode.cls}"><span><strong>${esc(mode.title)}</strong> · ${esc(mode.note)}</span><span class="product-badge ${healthy ? 'good' : 'warn'}">● ${esc(healthy ? t('СИСТЕМА В НОРМЕ', 'SYSTEM HEALTHY') : t('СОСТОЯНИЕ ПРОВЕРЯЕТСЯ', 'STATUS CHECK'))}</span></div>${summary}<section class="product-section"><div class="product-detail">${consensusCard()}${card(t('Поток решений', 'Decision flow'), networkSvg() + `<p class="product-sub">${esc(t('Активность отражает только реальные события; в простое поток спокоен.', 'Activity reflects real events only; idle flow stays calm.'))}</p>`)}</div></section><section class="product-section"><div class="product-detail">${positionsCard}${leadersCard}</div></section><section class="product-section"><div class="product-detail">${manual}${card(t('Performance snapshot', 'Performance snapshot'), performanceChart(report))}</div></section><section class="product-section">${activity}</section>`;
  }
  function markerEvents(selected) {
    const coin = String(selected?.coin || '').split(':').pop().toUpperCase();
    return state.eventHistory.filter(e => { const b = eventBody(e), symbol = String(b.instrument?.symbol || b.instrument || b.coin || b.event?.instrument?.symbol || '').split(':').pop().toUpperCase(); return symbol === coin && ['OPEN', 'ADD', 'REDUCE', 'CLOSE', 'REVERSE', 'POSITION_OPENED', 'POSITION_INCREASED', 'POSITION_REDUCED', 'POSITION_CLOSED', 'LEADER_TRADE'].includes(eventKind(e)); }).slice(0, 20);
  }
  function timelineHtml(selected) {
    const events = markerEvents(selected);
    return card(t('Decision Timeline · почему эта сделка?', 'Decision Timeline · why this trade?'), events.length ? `<div class="product-timeline">${events.map(e => { const kind = eventKind(e), labels = lifecycleLabels[kind] || [kind, kind], b = eventBody(e); return `<div class="timeline-item"><i class="timeline-dot ${statusClass(kind)}"></i><div><b>${esc(t(labels[0], labels[1]))}</b><small>${esc(time(eventTime(e)))}${b.consensus?.decision ? ` · ${esc(b.consensus.decision)}` : ''}</small></div></div>`; }).join('')}</div>` : unavailable(t('Связанные durable events пока недоступны', 'Related durable events are not available yet')));
  }
  function positionDetail(selected) {
    const side = String(selected.side || '').toUpperCase(), pnl = finite(selected.unrealized_pnl), roe = finite(selected.roe), leader = positionLeader(selected);
    const controls = `<div class="chart-controls"><label>${esc(t('Интервал', 'Interval'))}<select class="product-select" data-chart-interval><option value="1m">1m</option><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option><option value="4h">4h</option></select></label><div class="product-actions">${[24, 72, 168].map(h => `<button class="product-tab ${state.chartHours === h ? 'active' : ''}" data-chart-hours="${h}">${h}h</button>`).join('')}</div></div>`;
    const intelligence = latestDecision() ? card(t('Текущая аналитика', 'Current intelligence'), `<div class="agent-grid compact">${agentsFromDecision().map(agentCard).join('')}</div><div class="product-section">${consensusCard()}</div>`) : card(t('Текущая аналитика', 'Current intelligence'), unavailable(t('Ожидаются свежие результаты агентов', 'Awaiting fresh agent results')));
    return `<section class="product-section"><div class="product-card"><div class="product-section-head"><div><h3>${esc(selected.coin || 'N/A')} · ${esc(side || 'N/A')}</h3><p>${esc(positionSource(selected))} · ${esc(leader ? walletDisplay(leader) : t('лидер не подтверждён', 'leader unavailable'))} · ${esc(positionMode(selected))}</p></div><button class="product-btn" data-action="close-detail">${esc(t('Назад', 'Back'))}</button></div>${controls}<div class="product-chart" id="productPositionChart"><div class="product-empty">${esc(t('Загружаю свечи…', 'Loading candles…'))}</div></div><div class="product-metrics">${metric(t('Средний вход', 'Average entry'), number(selected.entry_price))}${metric(t('Текущая цена', 'Current price'), number(selected.mark_price))}${metric(t('Размер', 'Size'), number(selected.size, 6, true))}${metric(t('Маржа', 'Margin'), money(selected.margin_used, true))}${metric(t('Номинал', 'Notional'), money(selected.position_value, true))}${metric(t('Плечо', 'Leverage'), selected.leverage === undefined ? 'N/A' : `${number(selected.leverage, 0)}x`)}${metric(t('PnL', 'PnL'), money(pnl, true), (pnl ?? 0) >= 0 ? 'product-good' : 'product-bad')}${metric(t('PnL %', 'PnL %'), roe === null ? 'N/A' : pct(roe), (roe ?? 0) >= 0 ? 'product-good' : 'product-bad')}</div><div class="product-detail-meta"><span>${esc(t('Открыта', 'Opened'))}: ${esc(time(selected.opened_ms || selected.created_ms))}</span><span>${esc(t('Статус', 'Status'))}: ${esc(selected.status || t('OPEN', 'OPEN'))}</span></div><p class="product-sub">${esc(t('Свечи, линии входа/цены и маркеры показываются только из подтверждённых данных backend.', 'Candles, entry/current lines and markers use verified backend data only.'))}</p></div></section><section class="product-section"><div class="product-detail">${intelligence}${timelineHtml(selected)}</div></section>`;
  }
  function positionsPage() {
    const d = state.dashboard || {}, rows = (Array.isArray(d.positions) ? d.positions : []).filter(positionMatches), selected = state.selectedPosition && rows.find(p => String(p.coin) === String(state.selectedPosition.coin) && String(p.dex || '') === String(state.selectedPosition.dex || ''));
    const filters = [['ALL', ['ВСЕ', 'ALL']], ['MANUAL', ['MANUAL LEADER', 'MANUAL LEADER']], ['AUTONOMOUS', ['АВТОНОМНЫЕ', 'AUTONOMOUS']], ['COPY', ['COPY', 'COPY']], ['PAPER', ['PAPER', 'PAPER']], ['SHADOW', ['SHADOW', 'SHADOW']], ['LIVE', ['LIVE', 'LIVE']]];
    return `${pageHeader(t('Позиции', 'Positions'), t('Проверенное состояние счёта и жизненный цикл сделок', 'Verified account state and trade lifecycle'))}<div class="product-tabs">${filters.map(([key, labels]) => `<button class="product-tab ${state.positionFilter === key ? 'active' : ''}" data-position-filter="${key}">${esc(t(labels[0], labels[1]))}</button>`).join('')}</div>${selected ? positionDetail(selected) : ''}${rows.length ? `<div class="product-list">${rows.map(positionRow).join('')}</div>` : unavailable(t('Открытых позиций нет или данные недоступны', 'No open positions or data unavailable'))}`;
  }
  function manualPage() {
    const d = state.dashboard || {}, config = state.manual, leader = config?.configured ? config : null, pctSaved = finite(config?.allocation_pct) ?? finite(storage.get('wh_manual_pct')) ?? 80, active = Boolean(config?.enabled), capital = finite(d.balance), allocation = capital === null ? null : capital * pctSaved / 100, committed = finite(config?.committed_capital ?? d.manual_copy?.committed_capital), reserved = finite(config?.reserved_capital ?? d.manual_copy?.reserved_capital), available = finite(config?.available_capital ?? d.manual_copy?.available_capital);
    const oldPositions = (d.positions || []).filter(p => String(positionSource(p)).includes('MANUAL'));
    const leaderBody = leader ? `<div class="product-kicker">${esc(t('РУЧНОЙ ЛИДЕР', 'MANUAL LEADER'))}</div><div class="product-value">${esc(walletDisplay(leader.leader))}</div><p class="product-sub">${esc(leader.alias || leader.leader || 'N/A')}</p><span class="product-badge ${active ? 'good' : 'warn'}">● ${esc(active ? t('АКТИВЕН', 'ACTIVE') : t('ПАУЗА', 'PAUSED'))}</span>` : unavailable(t('Лидер не выбран', 'No leader selected'));
    const leaderCard = card(t('Выбранный лидер', 'Selected leader'), leaderBody + `<label class="product-label"><span>${esc(t('Адрес Hyperliquid лидера', 'Hyperliquid leader address'))}</span><input class="product-input" id="productManualLeader" placeholder="0x…" autocomplete="off"></label><div class="product-actions"><button class="product-btn primary" data-action="manual-add">${esc(t('Выбрать лидера', 'Select leader'))}</button></div><p class="product-note">${esc(t('Manual Copy отделён от автоматического Discovery. Смена не переносит происхождение старых позиций.', 'Manual Copy is separate from automatic Discovery. Switching never transfers old provenance.'))}</p>`);
    const allocationCard = card(t('Аллокация Manual Copy', 'Manual Copy allocation'), `<label class="product-label"><span>${esc(t('Доля доступного капитала', 'Allocatable capital'))}: <b id="manualPctValue">${esc(number(pctSaved, 0))}%</b></span><input id="manualPct" class="product-range" type="range" min="1" max="100" step="1" value="${Math.min(100, Math.max(1, pctSaved))}"></label><div class="product-metrics">${metric(t('Капитал счёта', 'Account capital'), money(capital, true))}${metric(t('Выделено', 'Allocated'), money(allocation, true))}${metric(t('Committed', 'Committed'), money(committed, true))}${metric(t('Reserved', 'Reserved'), money(reserved, true))}${metric(t('Доступно', 'Available'), money(available, true))}</div><p class="product-sub">${esc(t('100% означает 100% доступного торгового капитала, а не отказ от операционного резерва. Комиссии и маржа проходят через Risk Gateway.', '100% means 100% of allocatable trading capital, not zero operational reserve. Fees and margin remain subject to Risk Gateway.'))}<br>${esc(t('Остановленные позиции остаются HOLD и продолжают занимать committed capital.', 'Stopped positions remain HOLD and continue consuming committed capital.'))}</p><div class="product-actions"><button class="product-btn" data-action="manual-save">${esc(t('Сохранить аллокацию', 'Save allocation'))}</button><button class="product-btn ${active ? 'danger' : 'primary'}" data-action="manual-toggle">${esc(active ? t('Остановить копирование', 'Stop copying') : t('Начать копирование', 'Start copying'))}</button></div>`);
    const analysis = state.manualAnalysisSlot === leader?.leader ? state.manualAnalysis : null, analysisBody = analysis ? `<div class="product-kicker">${esc(t('ИСТОРИЧЕСКИЕ ДАННЫЕ', 'HISTORICAL DATA'))}</div><div class="product-value">${finite(analysis.rating) === null ? 'N/A' : `${number(analysis.rating, 0)}/100`}</div><p class="product-sub">${esc(t('Оценка качества истории, не прогноз прибыли.', 'Historical quality score, not a profit forecast.'))}</p><div class="product-metrics">${metric('PnL', money(analysis.net_pnl))}${metric('Profit Factor', analysis.profit_factor === undefined ? 'N/A' : String(analysis.profit_factor))}${metric('Win rate', finite(analysis.win_rate) === null ? 'N/A' : pct(analysis.win_rate, 1))}${metric(t('Сделки', 'Trades'), number(analysis.trades, 0))}${metric(t('Ожидание', 'Expectancy'), money(analysis.expectancy))}${metric(t('Просадка (proxy)', 'Drawdown (proxy)'), money(analysis.drawdown))}</div>` : unavailable(t('Подробные метрики появятся после свежего анализа лидера', 'Detailed metrics appear after a fresh leader analysis'));
    return `${pageHeader(t('Manual Copy', 'Manual Copy'), t('Один выбранный лидер · отдельная аллокация · пропорциональное копирование', 'One selected leader · separate allocation · proportional copying'))}<div class="product-detail">${leaderCard}${allocationCard}</div><section class="product-section">${card(t('Лидер и качество истории', 'Leader quality'), analysisBody + (leader ? `<p class="product-sub">${esc(t('Активных позиций Manual Copy', 'Open Manual Copy positions'))}: ${esc(number(oldPositions.length, 0))}</p>` : ''))}</section>`;
  }
  function leaderAnalysisMetrics(leader) {
    const a = leader?.analysis || {}, windows = a.windows || {}, behavior = windows['180']?.deep_analysis || windows['90']?.deep_analysis || windows['30']?.deep_analysis || {};
    return { a, windows, behavior };
  }
  function leaderDetailPage(leader) {
    const { a, windows, behavior } = leaderAnalysisMetrics(leader), score = a.score || {}, reasons = Array.isArray(score.reasons) ? score.reasons.join(', ') : 'N/A';
    const windowCard = (days, row) => row ? card(`${days}D`, `<div class="product-metrics">${metric('Net PnL', money(row.net_pnl))}${metric('Gross Profit', money(row.gross_profit))}${metric('Gross Loss', money(row.gross_loss))}${metric('Profit Factor', row.profit_factor_unbounded ? '∞' : row.profit_factor ?? 'N/A')}${metric(t('Win rate', 'Win rate'), finite(row.win_rate) === null ? 'N/A' : pct(row.win_rate, 1))}${metric(t('Сделки', 'Trades'), number(row.trades, 0))}${metric(t('Expectancy', 'Expectancy'), money(row.expectancy))}${metric(t('Средний выигрыш', 'Average win'), money(row.avg_win))}${metric(t('Средний проигрыш', 'Average loss'), money(row.avg_loss))}</div>`) : card(`${days}D`, unavailable(t('Недостаточно подтверждённой истории', 'Insufficient verified history')));
    const components = [['profit_quality', ['Качество прибыли', 'Profit quality']], ['consistency', ['Стабильность', 'Consistency']], ['drawdown_quality', ['Качество просадки', 'Drawdown quality']], ['sample_quality', ['Качество выборки', 'Sample quality']], ['recent_quality', ['Свежесть результата', 'Recent quality']], ['anomaly', ['Аномальность', 'Behavior anomaly']]];
    return `${pageHeader(t('Детали лидера', 'Leader detail'), t('Историческая оценка и подтверждённые публичные данные', 'Historical quality and verified public evidence'))}<div class="product-actions"><button class="product-btn" data-action="leader-back">${esc(t('Назад к рейтингу', 'Back to ranking'))}</button><button class="product-btn" data-action="copy-wallet" data-wallet="${esc(leader.wallet || '')}">${esc(t('Скопировать адрес', 'Copy address'))}</button></div><section class="product-section"><div class="product-detail">${card(t('LeaderScore', 'LeaderScore'), `<div class="product-value product-score">${esc(scorePct(leader.score))}</div><p class="product-sub">${esc(t('Это оценка качества истории, а не гарантия будущей прибыли.', 'A historical quality score, not a guarantee of future profitability.'))}</p><div class="product-metrics">${metric(t('Уверенность', 'Confidence'), finite(leader.confidence) === null ? 'N/A' : pct(leader.confidence * 100, 0))}${metric(t('Статус', 'Status'), leader.status || 'N/A')}${metric(t('Первое наблюдение', 'First seen'), time(leader.first_seen))}${metric(t('Последняя активность', 'Last activity'), time(leader.last_seen))}</div>`)}${card(t('Компоненты оценки', 'Score components'), `<div class="score-grid">${components.map(([key, labels]) => { const value = finite(score[key]); return `<div class="score-item"><span>${esc(t(labels[0], labels[1]))}</span><b>${value === null ? 'N/A' : esc(scorePct(value))}</b><i style="--score:${value === null ? 0 : Math.max(0, Math.min(100, value * 100))}%"></i></div>`; }).join('')}</div><p class="product-sub">${esc(reasons)}</p>`)}</div></section><section class="product-section"><div class="product-grid three">${windowCard(30, windows['30'])}${windowCard(90, windows['90'])}${windowCard(180, windows['180'])}</div></section><section class="product-section">${card(t('Поведение', 'Behavior'), behavior && Object.keys(behavior).length ? `<div class="product-metrics">${metric(t('Drawdown proxy', 'Drawdown proxy'), behavior.drawdown_proxy?.value_usdc === undefined ? 'N/A' : money(behavior.drawdown_proxy.value_usdc))}${metric(t('Payoff ratio', 'Payoff ratio'), behavior.payoff_ratio ?? 'N/A')}${metric(t('Плечо', 'Leverage'), behavior.leverage_behavior?.reason || 'N/A')}${metric(t('Концентрация', 'Concentration'), behavior.symbol_notional_share ? Object.entries(behavior.symbol_notional_share).map(([k, v]) => `${k}: ${scorePct(v)}`).join(', ') : 'N/A')}${metric(t('Long / Short', 'Long / Short'), behavior.long_short_bias ? `${scorePct(behavior.long_short_bias.long)} / ${scorePct(behavior.long_short_bias.short)}` : 'N/A')}</div><p class="product-note">${esc(t('Просадка — прокси накопленного cashflow по fills; account-equity drawdown не утверждается.', 'Drawdown is a cumulative fill-cashflow proxy; account-equity drawdown is not claimed.'))}</p>` : unavailable(t('Поведенческие метрики недоступны', 'Behavior metrics unavailable')))}</section>`;
  }
  function leadersPage() {
    const intel = state.intelligence || {}, leaders = Array.isArray(intel.leaders) ? intel.leaders : [], counts = intel.counts || {}, selected = state.selectedLeader && leaders.find(l => String(l.wallet).toLowerCase() === String(state.selectedLeader).toLowerCase());
    if (selected) return leaderDetailPage(selected);
    const counter = (label, key) => `<div class="product-metric"><small>${esc(label)}</small><b>${esc(number(counts[key] ?? counts[key.toLowerCase()], 0))}</b></div>`;
    return `${pageHeader(t('Лидеры · Discovery', 'Leaders · Discovery'), t('Публичные данные, ранжирование и активный watchlist', 'Public evidence, ranking and active watchlist'))}<div class="product-grid three">${counter(t('Наблюдались', 'Observed'), 'OBSERVED')}${counter(t('Кандидаты', 'Candidates'), 'CANDIDATE')}${counter(t('Квалифицированы', 'Qualified'), 'QUALIFIED')}${counter(t('Активны', 'Active'), 'ACTIVE')}${counter(t('Пробация', 'Probation'), 'PROBATION')}${counter(t('Сняты', 'Retired'), 'RETIRED')}</div><section class="product-section"><div class="product-card"><div class="product-section-head"><div><h3>${esc(t('Рейтинг лидеров', 'Leader ranking'))}</h3><p>${esc(t('LeaderScore — качество исторических данных, не гарантия прибыли.', 'LeaderScore is historical quality evidence, not a profit guarantee.'))}</p></div><span class="product-badge ${statusClass(intel.health)}">${esc(intel.health || 'N/A')}</span></div>${leaders.length ? `<div class="product-table-wrap"><table class="product-table"><thead><tr><th>#</th><th>Wallet</th><th>Score</th><th>${esc(t('Уверенность', 'Confidence'))}</th><th>${esc(t('Net PnL', 'Net PnL'))}</th><th>PF</th><th>${esc(t('Статус', 'Status'))}</th><th>${esc(t('Последняя активность', 'Last activity'))}</th></tr></thead><tbody>${leaders.map((l, i) => { const h = l.analysis?.windows?.['180'] || l.analysis?.windows?.['90'] || l.analysis?.windows?.['30']; return `<tr data-action="leader-detail" data-leader="${esc(l.wallet || '')}" tabindex="0"><td><b>${i + 1}</b></td><td><b>${esc(walletDisplay(l.wallet))}</b><small class="product-sub wallet-full">${esc(privateMode() ? '••••••' : l.wallet || 'N/A')}</small></td><td class="product-score">${esc(scorePct(l.score))}</td><td>${esc(finite(l.confidence) === null ? 'N/A' : pct(l.confidence * 100, 0))}</td><td>${esc(h ? money(h.net_pnl) : 'N/A')}</td><td>${esc(h?.profit_factor_unbounded ? '∞' : h?.profit_factor ?? 'N/A')}</td><td>${esc(String(l.status || 'N/A').toUpperCase())}</td><td>${esc(time(l.last_seen))}</td></tr>`; }).join('')}</tbody></table></div>` : unavailable(t('Discovery ещё не опубликовал лидеров', 'Discovery has not published leaders yet'))}</div></section>`;
  }
  function agentsPage() {
    const body = latestDecision(), c = body?.consensus;
    return `${pageHeader(t('Агенты и консенсус', 'Agents & consensus'), t('Реальный поток аналитических результатов · без доступа к исполнению', 'Real analytical outputs · no execution access'))}<div class="product-detail">${card(t('Intelligence Flow', 'Intelligence Flow'), networkSvg() + `<p class="product-sub">${esc(t('Это визуализация потока данных, не биологическая нейросеть.', 'This is a data-flow visualization, not a biological neural network.'))}</p>`)}${card(t('Consensus hero', 'Consensus hero'), `<div class="product-kicker">${esc(body?.event?.instrument?.symbol || 'N/A')}</div><div class="product-value ${statusClass(c?.decision)}">${esc(c?.decision || t('ОЖИДАНИЕ', 'WAIT'))}</div><div class="product-metrics">${metric(t('Score', 'Score'), c?.score === undefined ? 'N/A' : scorePct(c.score))}${metric(t('Уверенность', 'Confidence'), c?.confidence === undefined ? 'N/A' : pct(Number(c.confidence) * 100, 0))}${metric(t('Поддержка', 'Support'), number(c?.supporting?.length, 0))}${metric(t('Против', 'Opposing'), number(c?.opposing?.length, 0))}${metric(t('Блокеры', 'Blockers'), number(c?.blockers?.length || c?.blocking_conditions?.length, 0))}</div><p class="product-sub">${esc(c?.policy_id || c?.policy_version || t('Версия политики недоступна', 'Policy version unavailable'))}</p>`)}</div><section class="product-section"><div class="agent-grid">${agentsFromDecision().map(agentCard).join('')}</div></section>`;
  }
  function analyticsReport() {
    if (state.analyticsMode === 'LIVE') return state.report || {};
    if (state.analyticsMode === 'PAPER') {
      const p = state.ai?.modes?.trader?.paper;
      if (!p || typeof p !== 'object') return {};
      return { pnl: p.realized_pnl_usdc, trades: p.counters?.closed, win_rate: p.win_rate, profit_factor: p.profit_factor, drawdown: p.max_drawdown_usdc, fees: p.counters?.fees_usdc, positions: p.positions, equity: p.equity_usdc };
    }
    return {};
  }
  function analyticsPage() {
    const r = analyticsReport(), mode = state.analyticsMode, modeNote = mode === 'PAPER' ? t('Симулированное исполнение · реальные средства не затрагиваются', 'Simulated execution · no real funds at risk') : mode === 'SHADOW' ? t('Гипотетический результат · ордера не отправляются', 'Hypothetical result · no orders submitted') : t('Реальные данные только при подтверждённом исполнении', 'Real data only for confirmed execution');
    const val = (v, moneyValue = true) => v === null || v === undefined || !Number.isFinite(Number(v)) ? 'N/A' : moneyValue ? money(v) : String(v);
    const metrics = [['PnL', val(r.pnl)], [t('Сделки', 'Trades'), val(r.trades, false)], ['Win rate', r.win_rate === undefined ? 'N/A' : pct(Number(r.win_rate), 1)], ['Profit Factor', r.profit_factor === undefined ? 'N/A' : String(r.profit_factor)], ['Max DD', val(r.drawdown)], [t('Комиссии', 'Fees'), val(r.fees)], [t('Открытые позиции', 'Open positions'), val(r.positions ? Object.keys(r.positions).length : undefined, false)]];
    return `${pageHeader(t('Аналитика · Performance Lab', 'Analytics · Performance Lab'), t('PAPER, SHADOW и LIVE разделены и не смешиваются', 'PAPER, SHADOW and LIVE remain separate'))}<div class="product-tabs">${['PAPER', 'SHADOW', 'LIVE'].map(x => `<button class="product-tab ${x === mode ? 'active' : ''}" data-analytics="${x}">${x}</button>`).join('')}</div><div class="product-banner ${mode.toLowerCase()}"><span><strong>${esc(mode)}</strong></span><span>${esc(modeNote)}</span></div><section class="product-section"><div class="product-detail">${card(t('Ключевые метрики', 'Key metrics'), `<div class="product-grid two">${metrics.map(x => metric(x[0], x[1])).join('')}</div><p class="product-sub">${esc(mode === 'LIVE' ? (state.report?.methodology || t('Методика account report недоступна', 'Account report methodology unavailable')) : t('История режима показывается только при наличии его собственных записей.', 'Mode history is shown only when its own records exist.'))}</p>`)}${card(t('Equity / cumulative PnL', 'Equity / cumulative PnL'), performanceChart(r))}</div></section>`;
  }
  function healthComponent(key, fallbackStatus, detail) {
    const raw = state.health?.components?.[key], status = typeof raw === 'string' ? raw : raw?.status || fallbackStatus || 'UNKNOWN', info = typeof raw === 'object' ? raw.detail || raw.reason || raw.last_success || detail : raw || detail;
    return `<div class="health-item"><div><b>${esc(key)}</b><small>${esc(typeof info === 'number' ? time(info) : info || 'N/A')}</small></div><span class="health-status ${statusClass(status)}">${esc(String(status).toUpperCase())}</span></div>`;
  }
  function healthPage() {
    const intel = state.intelligence || {}, d = state.dashboard || {}, accountStatus = d.account && !d.balance_error ? 'HEALTHY' : d.balance_error ? 'DEGRADED' : 'UNKNOWN', overall = state.health?.status || (intel.health === 'HEALTHY' && accountStatus === 'HEALTHY' ? 'HEALTHY' : 'DEGRADED');
    return `${pageHeader(t('Система и здоровье', 'System health'), t('Приоритет: account data → risk → execution → reconciliation', 'Priority: account data → risk → execution → reconciliation'))}<div class="product-banner ${statusClass(overall)}"><strong>${esc(String(overall).toUpperCase())}</strong><span>${esc(state.health?.reason || intel.reason || d.balance_error || t('Проверка состояния', 'Health check'))}</span></div><section class="product-section"><div class="health-list">${healthComponent('Hyperliquid public data', intel.health, intel.reason)}${healthComponent('Private account data', accountStatus, d.account || t('Аккаунт не подключён', 'Account not connected'))}${healthComponent('Discovery', intel.health, `${number(intel.leaders?.length, 0)} leaders`)}${healthComponent('Watchlist', intel.health, `${number(intel.counts?.ACTIVE, 0)} active`)}${healthComponent('Deep analysis', intel.health, intel.last_success)}${healthComponent('Agents', latestDecision() ? 'HEALTHY' : 'UNKNOWN', latestDecision() ? t('Есть свежий результат', 'Fresh result available') : t('Нет свежего результата', 'No fresh result'))}${healthComponent('Consensus', latestDecision()?.consensus ? 'HEALTHY' : 'UNKNOWN', latestDecision()?.consensus?.decision)}${healthComponent('Authorization', 'UNKNOWN', t('Данные backend не опубликованы', 'Backend detail unavailable'))}${healthComponent('Risk / execution', 'UNKNOWN', t('Проверяется каноническим gateway', 'Checked by canonical gateway'))}${healthComponent('Reconciliation', 'UNKNOWN', t('Backlog не опубликован', 'Backlog not published'))}${healthComponent('Event stream', state.streamStatus === 'CONNECTED' ? 'HEALTHY' : state.streamStatus === 'RECONNECTING' ? 'DEGRADED' : 'UNKNOWN', state.streamStatus)}${healthComponent('Database', 'UNKNOWN', t('Детали не опубликованы', 'Detail unavailable'))}${healthComponent('Telegram', 'UNKNOWN', t('Доставка не подтверждена', 'Delivery not confirmed'))}${healthComponent('Web/API', state.health?.ok ? 'HEALTHY' : 'UNKNOWN', state.health?.ok ? 'HTTP OK' : 'N/A')}</div></section><p class="product-note">${esc(t('HTTP-ответ сам по себе не доказывает здоровье торговли. Критические состояния остаются видимыми при отключении UI.', 'An HTTP response alone does not prove trading health. Critical states remain visible when the UI disconnects.'))}</p>`;
  }
  function settingsPage() {
    const d = state.dashboard || {}, mode = modeValue();
    return `${pageHeader(t('Настройки', 'Settings'), t('Счёт, сеть, режим, Manual Copy и приватность', 'Account, network, mode, Manual Copy and privacy'))}<div class="product-detail">${card(t('Operating mode', 'Operating mode'), `<div class="product-list">${[['OBSERVE', t('Только наблюдение', 'Analysis only')], ['PAPER_AUTO', t('Автоматический PAPER без реальных средств', 'Autonomous PAPER without real funds')], ['SHADOW', t('Гипотетический результат · без отправки', 'Hypothetical · no submission')], ['LIVE_CONFIRM', t('LIVE только после подтверждения', 'LIVE only after confirmation')]].map(x => `<div class="product-row"><div class="product-row-main"><b>${esc(x[0].replace('_', ' '))}</b><small>${esc(x[1])}</small></div><span class="product-badge ${mode === x[0] ? 'good' : 'warn'}">${esc(mode === x[0] ? t('ВКЛЮЧЁН', 'ACTIVE') : t('ДОСТУПЕН ЧЕРЕЗ BACKEND', 'BACKEND CONTROL'))}</span></div>`).join('')}</div><p class="product-note">${esc(t('LIVE_AUTO заблокирован и недоступен как обычный режим.', 'LIVE_AUTO is locked and unavailable as a normal mode.'))}</p>`)}${card(t('Счёт и приватность', 'Account & privacy'), `<div class="product-metrics">${metric(t('Сеть', 'Network'), d.execution_scope?.network || 'N/A')}${metric(t('Счёт', 'Account'), privateMode() ? '••••••' : d.account || 'N/A')}${metric(t('Копирование', 'Copy'), d.copy_enabled ? t('ВКЛ', 'ON') : t('ПАУЗА', 'PAUSED'))}${metric(t('Уведомления', 'Notifications'), d.notifications ? t('ВКЛ', 'ON') : t('ВЫКЛ', 'OFF'))}</div><div class="product-actions"><button class="product-btn" data-action="privacy">${esc(t('Переключить приватность', 'Toggle privacy'))}</button><button class="product-btn" data-product-page="manual">${esc(t('Открыть Manual Copy', 'Open Manual Copy'))}</button></div>`)}</div>`;
  }
  function candleSvg(candles, selected) {
    const rows = candles.slice(-90).filter(c => [c.o, c.h, c.l, c.c].every(v => finite(v) !== null));
    if (!rows.length) return unavailable(t('Свечи недоступны', 'Candles unavailable'));
    const lo = Math.min(...rows.map(c => Number(c.l))), hi = Math.max(...rows.map(c => Number(c.h))), span = hi - lo || 1, w = 500, step = w / rows.length, minT = finite(rows[0].t), maxT = finite(rows[rows.length - 1].t) || minT;
    const y = v => 12 + (hi - v) / span * 190;
    const glyphs = rows.map((c, i) => { const x = i * step + step / 2, green = Number(c.c) >= Number(c.o), top = Math.min(y(c.o), y(c.c)), bottom = Math.max(y(c.o), y(c.c)); return `<line x1="${x.toFixed(2)}" y1="${y(c.h).toFixed(2)}" x2="${x.toFixed(2)}" y2="${y(c.l).toFixed(2)}" stroke="${green ? '#58e6a0' : '#ff768d'}"/><rect x="${(x - step * .28).toFixed(2)}" y="${top.toFixed(2)}" width="${(step * .56).toFixed(2)}" height="${Math.max(1, bottom - top).toFixed(2)}" fill="${green ? '#58e6a0' : '#ff768d'}" opacity=".85"/>`; }).join('');
    const lines = [];
    [['ENTRY', selected?.entry_price, '#53e4e1'], ['CURRENT', selected?.mark_price, '#a58bff']].forEach(([label, value, color]) => { const n = finite(value); if (n !== null && n >= lo && n <= hi) lines.push(`<line class="chart-price-line" x1="0" x2="500" y1="${y(n).toFixed(2)}" y2="${y(n).toFixed(2)}" stroke="${color}"/><text class="chart-price-label" x="6" y="${(y(n) - 4).toFixed(2)}" fill="${color}">${label} ${esc(number(n))}</text>`); });
    markerEvents(selected).forEach(e => { const at = eventTime(e); if (at === null || minT === null || maxT === null || maxT <= minT || at < minT || at > maxT) return; const x = ((at - minT) / (maxT - minT)) * w, labels = lifecycleLabels[eventKind(e)] || [eventKind(e), eventKind(e)]; lines.push(`<g class="chart-marker" transform="translate(${x.toFixed(2)} 0)"><line x1="0" x2="0" y1="16" y2="198"/><circle cx="0" cy="18" r="3"/><text x="4" y="28">${esc(t(labels[0], labels[1]))}</text></g>`); });
    return `<svg viewBox="0 0 ${w} 215" preserveAspectRatio="none" aria-label="${esc(t('Свечной график позиции', 'Position candlestick chart'))}">${glyphs}${lines.join('')}</svg><div class="chart-legend"><span><i class="legend-entry"></i>${esc(t('Вход', 'Entry'))}</span><span><i class="legend-current"></i>${esc(t('Текущая цена', 'Current price'))}</span>${eventsForLegend(selected).map(x => `<span>${esc(x)}</span>`).join('')}</div>`;
  }
  function eventsForLegend(selected) { return [...new Set(markerEvents(selected).map(e => { const row = lifecycleLabels[eventKind(e)] || [eventKind(e), eventKind(e)]; return t(row[0], row[1]); }).slice(0, 4))]; }
  async function loadPositionChart() {
    const selected = state.selectedPosition, target = $('productPositionChart'), request = ++state.chartRequest;
    if (!selected || !target) return;
    target.innerHTML = `<div class="product-empty">${esc(t('Загрузка подтверждённых свечей…', 'Loading verified candles…'))}</div>`;
    try { const q = new URLSearchParams({ coin: String(selected.coin || ''), dex: String(selected.dex || ''), interval: state.chartInterval, hours: String(state.chartHours) }); const data = await read('/api/chart?' + q); if (request !== state.chartRequest || !$('productPositionChart')) return; target.innerHTML = candleSvg(Array.isArray(data.candles) ? data.candles : [], selected); } catch (_) { if (request === state.chartRequest) target.innerHTML = unavailable(t('Рынок временно недоступен', 'Market temporarily unavailable')); }
  }
  function shell() {
    document.body.classList.add('product-mode');
    const main = document.querySelector('main'); if (!main) return;
    document.querySelector('body > nav')?.remove();
    main.innerHTML = `<div class="product-main"><header class="product-header"><div class="product-brand"><img src="/static/assets/skull-trader.png" alt="Wallet Hunter"><div><small>WALLET HUNTER</small><h1>${esc(t('Командный центр', 'Command center'))}</h1></div></div><div class="product-header-spacer"></div><div id="productMode" class="product-pill observe"><i></i><span>OBSERVE</span></div><button id="productLanguage" class="product-icon" type="button" aria-label="Language">RU</button><button id="productPrivacy" class="product-icon" type="button" aria-label="${esc(t('Скрыть суммы', 'Hide amounts'))}">◉</button><button id="productSound" class="product-icon" type="button" aria-label="Sound">◖</button></header><div id="productContent" class="product-content"></div><p class="product-footer-note">${esc(t('Wallet Hunter · данные только из подтверждённого backend state', 'Wallet Hunter · data from verified backend state only'))}</p></div><nav id="productNav" class="product-nav">${navItems.map(x => `<button data-product-page="${x[0]}" aria-label="${esc(t(x[2][0], x[2][1]))}"><span>${x[1]}</span>${esc(t(x[2][0], x[2][1]))}</button>`).join('')}</nav>`;
    bind();
  }
  function render() {
    const content = $('productContent'); if (!content) return;
    const pages = { home, positions: positionsPage, manual: manualPage, leaders: leadersPage, agents: agentsPage, analytics: analyticsPage, health: healthPage, settings: settingsPage };
    if (state.loading && !state.dashboard && !state.intelligence) content.innerHTML = `<section class="product-page active">${unavailable(t('Загрузка защищённого состояния…', 'Loading verified state…'))}</section>`;
    else content.innerHTML = `<section class="product-page active">${state.error ? `<div class="product-banner shadow"><strong>${esc(t('Backend недоступен', 'Backend unavailable'))}</strong><span>${esc(state.error)}</span></div>` : ''}${(pages[state.page] || home)()}</section>`;
    document.querySelectorAll('[data-product-page]').forEach(b => b.classList.toggle('active', b.dataset.productPage === state.page));
    const mode = modeMeta(modeValue()), pill = $('productMode'); if (pill) { pill.className = `product-pill ${mode.cls}`; pill.innerHTML = `<i></i><span>${esc(mode.title)}</span>`; }
    const languageButton = $('productLanguage'); if (languageButton) languageButton.textContent = isEn() ? 'EN' : 'RU';
    const soundButton = $('productSound'); if (soundButton) soundButton.textContent = storage.get('wh_sound') === 'off' ? '◌' : '◖';
    document.querySelectorAll('[data-chart-interval]').forEach(s => { s.value = state.chartInterval; s.onchange = () => { state.chartInterval = s.value; loadPositionChart(); }; });
    if (state.page === 'positions' && state.selectedPosition) loadPositionChart();
  }
  async function loadManualAnalysis(leader) {
    if (!leader) return; state.manualAnalysisSlot = leader; state.manualAnalysis = null;
    try { state.manualAnalysis = await read('/api/analyse', { method: 'POST', body: JSON.stringify({ address: leader }) }); } catch (_) { state.manualAnalysis = null; }
    if (state.page === 'manual') render();
  }
  async function refresh() {
    state.loading = true; state.error = null; render();
    const shouldReport = !state.report || Date.now() - state.reportAt > 30000;
    const calls = [read('/api/dashboard'), read(`/api/intelligence?after=${state.cursor}&limit=50`), read('/health'), read('/api/manual-copy'), read('/api/ai')];
    if (shouldReport) calls.push(read('/api/account/report?days=30'));
    const results = await Promise.allSettled(calls), [dashboard, intel, health, manual, ai, report] = results;
    if (dashboard.status === 'fulfilled') state.dashboard = dashboard.value;
    if (intel.status === 'fulfilled') { state.intelligence = intel.value; state.cursor = Number(intel.value.cursor) || state.cursor; mergeEvents(intel.value.events); state.streamStatus = 'CONNECTED'; } else state.streamStatus = 'RECONNECTING';
    if (dashboard.status === 'fulfilled') mergeEvents((dashboard.value.events || []).map(e => ({ body: e, kind: e.action, created: e.time })));
    if (health.status === 'fulfilled') state.health = health.value;
    if (manual.status === 'fulfilled') state.manual = manual.value;
    if (ai.status === 'fulfilled') state.ai = ai.value;
    if (report?.status === 'fulfilled') { state.report = report.value; state.reportAt = Date.now(); }
    state.loading = false; if (dashboard.status === 'rejected' && intel.status === 'rejected') state.error = t('Данные backend недоступны', 'Backend data unavailable'); render();
  }
  async function manualSave() {
    const value = Number($('manualPct')?.value || 80), current = state.manual?.leader, input = $('productManualLeader')?.value.trim();
    if (!current && !input) return alert(t('Сначала укажите адрес лидера', 'Enter a leader address first'));
    try { await read('/api/manual-copy', { method: 'PUT', body: JSON.stringify({ leader: input || current, allocation_pct: value }) }); storage.set('wh_manual_pct', value); await refresh(); } catch (e) { alert(e.message); }
  }
  async function manualAdd() {
    const input = $('productManualLeader'), address = input?.value.trim(); if (!address) return alert(t('Введите адрес лидера', 'Enter a leader address'));
    if (state.manual?.leader && state.manual.leader.toLowerCase() !== address.toLowerCase() && (state.dashboard?.positions || []).some(p => positionSource(p).includes('MANUAL'))) {
      const keep = confirm(t('У старого лидера есть открытые позиции. Сохранить их в HOLD и переключить лидера?', 'The old leader has open positions. Keep them on HOLD and switch leaders?'));
      if (!keep) return;
    }
    try { await read('/api/manual-copy', { method: 'PUT', body: JSON.stringify({ leader: address, allocation_pct: Number($('manualPct')?.value || 80) }) }); await refresh(); } catch (e) { alert(e.message); }
  }
  async function manualToggle() {
    const enabled = Boolean(state.manual?.enabled); if (enabled && !confirm(t('Остановить новые сделки? Открытые позиции останутся HOLD.', 'Stop new trades? Open positions remain HOLD.'))) return;
    try { await read('/api/manual-copy', { method: 'PUT', body: JSON.stringify({ action: enabled ? 'stop' : 'start' }) }); await refresh(); } catch (e) { alert(e.message); }
  }
  function bind() {
    $('productLanguage')?.addEventListener('click', () => window.toggleLanguage?.());
    $('productPrivacy')?.addEventListener('click', () => { const next = !privateMode(); window.walletHunterPrivacy = next; storage.set('wh_privacy', next ? 'on' : 'off'); render(); });
    $('productSound')?.addEventListener('click', () => { const muted = storage.get('wh_sound') === 'off'; storage.set('wh_sound', muted ? 'on' : 'off'); render(); });
    if (!document.body.dataset.productEvents) {
      document.addEventListener('click', e => {
        const target = e.target.closest('[data-action], [data-product-page], [data-position-filter], [data-chart-hours], [data-analytics]'); if (!target) return;
        const action = target.dataset.action;
        if (target.dataset.productPage) { state.page = target.dataset.productPage; state.selectedLeader = null; render(); return; }
        if (target.dataset.positionFilter) { state.positionFilter = target.dataset.positionFilter; render(); return; }
        if (target.dataset.chartHours) { state.chartHours = Number(target.dataset.chartHours); render(); return; }
        if (target.dataset.analytics) { state.analyticsMode = target.dataset.analytics; render(); return; }
        if (action === 'manual-add') manualAdd();
        if (action === 'manual-save') manualSave();
        if (action === 'manual-toggle') manualToggle();
        if (action === 'privacy') $('productPrivacy')?.click();
        if (action === 'chart') { const row = target.closest('[data-coin]'); if (row) { state.selectedPosition = { coin: row.dataset.coin, dex: row.dataset.dex }; state.page = 'positions'; render(); } }
        if (action === 'close-detail') { state.selectedPosition = null; render(); }
        if (action === 'leader-detail') { state.selectedLeader = target.dataset.leader; state.page = 'leaders'; render(); }
        if (action === 'leader-back') { state.selectedLeader = null; render(); }
        if (action === 'copy-wallet') { const wallet = target.dataset.wallet; if (wallet && !privateMode() && navigator.clipboard?.writeText) navigator.clipboard.writeText(wallet).catch(() => {}); }
      });
      document.addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[data-action="leader-detail"], [data-action="chart"]')) e.target.click(); });
      document.body.dataset.productEvents = '1';
    }
    const range = $('manualPct'), rangeValue = $('manualPctValue'); if (range) range.oninput = () => { storage.set('wh_manual_pct', range.value); if (rangeValue) rangeValue.textContent = `${range.value}%`; };
    if (state.page === 'manual' && state.manual?.leader && state.manualAnalysisSlot !== state.manual.leader) loadManualAnalysis(state.manual.leader);
  }
  window.addEventListener('whlanguage', () => { if (document.body.classList.contains('product-mode')) { shell(); render(); } });
  window.addEventListener('whprivacy', () => { if (document.body.classList.contains('product-mode')) render(); });
  window.whProduct = { refresh, render, state };
  const boot = () => { shell(); refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 10000); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})();
