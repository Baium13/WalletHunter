/* Existing-position changes require a separate explicit decision on frozen terms. */
(() => {
  if (window.whAiPositionActions) return;
  const en=()=>window.walletHunterLanguage==='en',t=(ru,eng)=>en()?eng:ru;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const obj=value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
  const finite=value=>typeof value==='number'&&Number.isFinite(value);
  const key=value=>typeof value==='string'?value.toUpperCase():'';
  const num=value=>finite(value)?value.toLocaleString(en()?'en-GB':'ru-RU',{maximumFractionDigits:4}):'—';
  const decimal=value=>typeof value==='string'&&/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)&&Number.isFinite(Number(value))?esc(value):
    finite(value)&&value>=0?value.toLocaleString(en()?'en-GB':'ru-RU',{useGrouping:false,maximumFractionDigits:20}):'—';
  const mask=()=>`<span class="maskedValue" aria-label="${t('Сумма скрыта','Amount hidden')}">••••••</span>`;
  const money=value=>window.walletHunterPrivacy?mask():finite(value)?`${value.toLocaleString(en()?'en-GB':'ru-RU',{minimumFractionDigits:2,maximumFractionDigits:4})} USDC`:'—';
  // Derived quantities are display estimates; frozen decimal strings stay exact.
  const size=value=>window.walletHunterPrivacy?mask():decimal(finite(value)?Number(value.toPrecision(12)):value);
  const when=value=>finite(value)&&value>0&&Number.isFinite(new Date(value).getTime())?esc(new Date(value).toLocaleString(en()?'en-GB':'ru-RU',
    {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'})):'—';
  const identifier=value=>typeof value==='string'&&/^[a-zA-Z0-9_-]{1,100}$/.test(value)?value:Number.isSafeInteger(value)&&value>0?String(value):null;
  const actions={REDUCE:['Сократить позицию','Reduce position'],AVERAGE:['Добавить к позиции','Add to position'],LOWER_LEVERAGE:['Снизить плечо','Lower leverage'],ADD_MARGIN:['Добавить маржу','Add margin']};
  const statuses={PENDING:['Ожидает вашего решения','Awaiting your decision'],IDLE:['Новых предложений нет','No new proposals'],UNAVAILABLE:['Подготовка недоступна','Preparation unavailable'],
    DECLINED:['Отклонено вами','Declined by you'],EXPIRED:['Предложение истекло','Proposal expired'],INVALIDATED:['Условия изменились','Conditions changed'],
    SUBMITTING:['Отправка и проверка','Submitting and verifying'],FILLED:['Исполнение подтверждено','Execution verified'],EXECUTED:['Исполнение подтверждено','Execution verified'],
    PARTIAL:['Частичное исполнение','Partial fill'],UNKNOWN:['Результат не подтверждён','Outcome unconfirmed'],REJECTED:['Ордер отклонён','Order rejected']};
  const reasons={
    ACCOUNT_MISSING:['Подключите свой Hyperliquid-аккаунт.','Connect your Hyperliquid account.'],
    NO_TRIGGERED_POSITIONS:['Нет позиций, достигших порога ROE ≤−40%.','No positions have reached ROE ≤−40%.'],
    NO_ELIGIBLE_POSITIONS:['Нет подходящих позиций или не подтверждён их источник.','No eligible positions, or their source is unverified.'],
    CONFIRMATION_REQUIRED:['Проверьте условия и решите: выполнить конкретное действие или отказаться.','Review the terms and decide whether to execute the specific action or decline.'],
    MINIMUM_NOTIONAL:['Объём изменения меньше минимума 10 USDC. Сумма не увеличивается автоматически.','The change is below the 10 USDC minimum. Its amount is not increased automatically.'],
    STOP_REVIEW_REQUIRED:['По инструменту есть активные ордера, в том числе возможный стоп. Требуется ручная проверка; автоматического изменения стопов нет.','This market has active orders, possibly a stop. Manual review is needed; stops are not changed automatically.'],
    EXECUTION_PENDING:['Предыдущее исполнение не сверено. Не повторяйте отправку.','Previous execution is not reconciled. Do not submit again.'],
    MARKET_DATA_UNAVAILABLE:['Рыночные данные неполные или устарели. Действие не предлагается.','Market data is incomplete or stale. No action is proposed.'],
    SOURCE_UNVERIFIED:['Кошелёк-источник или его доля не подтверждены.','The source wallet or its share is unverified.'],
    INSUFFICIENT_BUDGET:['Недостаточно остатка бюджета этого кошелька. Чужая доля не используется для расчёта.','The source wallet has insufficient remaining budget. Other shares are not used for sizing.'],
    ADDITIONS_EXHAUSTED:['Общий лимит четырёх доборов этого кошелька исчерпан.','The source wallet’s shared limit of four additions is exhausted.'],
    ADVERSE_TREND:['Тренд и импульс не поддерживают дополнительный вход. Усреднение не предлагается.','Trend and momentum do not support another entry. Averaging is not proposed.'],
    CROSS_OR_UNIMPLEMENTED:['Смена плеча или добавление маржи здесь пока не поддерживаются.','Changing leverage or adding margin is not supported here yet.'],
    FRESH_CHECKS_FAILED_PREPARE_NEW_FORM:['Позиция, цена или бюджет изменились. Это предложение не исполнено; требуется новое.','The position, price or budget changed. This proposal was not executed; a new one is required.'],
    PROPOSAL_EXPIRED:['Срок предложения истёк. Подготовьте свежий расчёт.','The proposal expired. Prepare a fresh calculation.'],
    REVIEW_DISABLED:['Помощник позиций выключен.','The position assistant is off.']
  };
  const label=(map,value,fallback)=>t(...(Object.hasOwn(map,key(value))?map[key(value)]:fallback));
  const action=value=>label(actions,value,['Действие не подтверждено','Action unconfirmed']);
  const status=value=>label(statuses,value,['Статус не подтверждён','Status unconfirmed']);
  const reason=value=>label(reasons,value,['Недостаточно подтверждённых условий для этого действия. Обновите анализ.','Not enough verified conditions for this action. Refresh the analysis.']);
  Object.assign(reasons,{
    RESCUE_DISABLED:reasons.REVIEW_DISABLED,NO_ELIGIBLE_SCENARIO:reasons.NO_ELIGIBLE_POSITIONS,
    REBOUND_NOT_SUPPORTED:reasons.ADVERSE_TREND,INSUFFICIENT_CAPACITY:reasons.INSUFFICIENT_BUDGET,
    EXTRA_BUDGET_EXHAUSTED:['Дополнительный резерв или лимит четырёх доборов исчерпан.','The extra reserve or four-addition limit is exhausted.'],
    AVERAGING_LEVERAGE_LIMIT:['Текущее плечо выше разрешённого для добора. Дополнительный вход не предлагается.','Current leverage exceeds the addition limit. No additional entry is proposed.'],
    SOURCE_UNKNOWN_OR_SHARED:reasons.SOURCE_UNVERIFIED,SOURCE_BUDGET_OWNERSHIP_AMBIGUOUS:reasons.SOURCE_UNVERIFIED,
    SOURCE_REMOVED:['Кошелёк-источник удалён. Сначала требуется уточнить источник и его бюджет.','The source wallet was removed. Its origin and budget must be reconciled first.'],
    FILLS_AFTER_VERIFIED_OWNERSHIP:['После сверенной записи были другие исполнения. Принадлежность позиции требует проверки.','Other fills occurred after the verified ownership record. Position attribution needs review.'],
    OWNERSHIP_SNAPSHOT_CHANGED:['Позиция не совпадает с сохранённой записью источника. Требуется сверка.','The position differs from its saved source record. Reconciliation is required.'],
    OTHER_ACTION_HOLD:reasons.EXECUTION_PENDING,ORDERS_UNAVAILABLE:reasons.STOP_REVIEW_REQUIRED,
    REDUCTION_SIZE_UNAVAILABLE:['Размер сокращения слишком мал после округления. Он не увеличивается автоматически.','Reduction size is too small after rounding. It is not increased automatically.'],
    UNSUPPORTED_MARKET:['Этот рынок пока не поддерживается для подтверждаемых корректировок.','This market is not yet supported for confirmed position adjustments.'],
    UNSUPPORTED_CAPITAL_MODE:['Режим обеспечения аккаунта не поддерживается для этого расчёта.','The account capital mode is not supported for this calculation.'],
    MARKET_DISABLED_FOR_INCREASE:['Рынок выключен для новых входов. Сокращение возможно, добор заблокирован.','This market is disabled for entries. Reduction may be available; adding is blocked.'],
    CLOCK_BEFORE_PROPOSAL:['Время сервера не согласовано со временем предложения. Требуется новый расчёт.','Server time is inconsistent with the proposal timestamp. A new calculation is required.'],
    INTERVENTION_RECONCILIATION_REQUIRED:['Возобновление требует сверки позиции и её источника. До сверки копирование остаётся на паузе.','Resumption requires position and source reconciliation. Copying stays paused until verified.']
  });
  ['POSITIONS_UNAVAILABLE','ANALYSIS_UNAVAILABLE','SCENARIO_UNAVAILABLE','MARKET_CONTEXT_UNAVAILABLE','MARKET_CONTEXT_INVALID',
    'CANDLES_UNAVAILABLE','METADATA_UNAVAILABLE','MARKET_LIMITS_INVALID'].forEach(code=>{reasons[code]=reasons.MARKET_DATA_UNAVAILABLE;});
  let latest=null,container=null,callbacks=null,busy=false,failure=null,expiryTimer=null;
  const attempted=new Set(),releaseAttempts=new Set();
  const pendingRows=()=>Array.isArray(obj(latest).pending)?latest.pending.map(obj):[];
  const heldRows=()=>Array.isArray(obj(latest).holds)?latest.holds.map(obj):[];
  const expired=row=>!finite(row.expires_ms)||row.expires_ms<=Date.now();
  const pending=row=>row.status==='PENDING'&&!expired(row);
  const market=row=>`${obj(row.payload).coin}|${obj(row.payload).dex||''}`;
  const contract=()=>obj(latest).execution_mode==='USER_CONFIRMATION_ONLY'&&obj(latest).automatic_execution===false;
  const prepareAllowed=()=>contract()&&!busy&&!failure&&!pendingRows().some(pending)&&!['ACCOUNT_MISSING','REVIEW_DISABLED','RESCUE_DISABLED','EXECUTION_PENDING'].includes(key(obj(latest).reason));
  const supportedMarket=(coin,dex)=>typeof coin==='string'&&((['BTC','ETH'].includes(coin)&&dex==='')||(dex==='xyz'&&/^xyz:[A-Za-z0-9_.-]{1,36}$/.test(coin)));
  const heldMarket=value=>typeof value==='string'&&value.split('|').length===2&&supportedMarket(...value.split('|'))?value:null;
  const releaseKey=row=>`${row.market}:${identifier(row.proposal_id)}`;
  const releasable=row=>row.can_release===true&&['FILLED','PARTIAL'].includes(row.status)&&heldMarket(row.market)!==null&&identifier(row.proposal_id)!==null;
  const releaseWarning=()=>t('Возобновить копирование? Бот снова синхронизирует позицию с кошельком-источником: может изменить объём или плечо, снова увеличить или закрыть позицию. Ваша корректировка может быть отменена этой синхронизацией. Продолжить?',
    'Resume copying? The bot will synchronize the position with its source wallet again: it may change size or leverage, increase or close the position. This synchronization may undo your adjustment. Continue?');
  function completeFactors(raw){const f=obj(raw),trend=obj(f.trend_ema20_50),levels=obj(f.levels20);
    return [trend.ema20,trend.ema50,f.rsi14,f.macd_hist,f.atr14_pct,f.volume_ratio20,levels.support,levels.resistance,f.funding_bps_hour,f.open_interest].every(finite)&&finite(f.candle_close_ms)&&f.candle_close_ms>0;}
  function valid(row){const p=obj(row.payload),before=obj(p.position_before),after=obj(p.expected_after),stop=obj(p.stop_impact);
    const basic=identifier(row.id)!==null&&pending(row)&&['REDUCE','AVERAGE'].includes(p.action)&&p.order_type==='LIMIT_IOC'&&
      p.reduce_only===(p.action==='REDUCE')&&p.is_buy===(p.action==='REDUCE'?p.direction==='SHORT':p.direction==='LONG')&&
      supportedMarket(p.coin,p.dex)&&
      ['LONG','SHORT'].includes(p.direction)&&(!p.side||p.direction===p.side)&&['MAINNET','TESTNET'].includes(p.network)&&['cross','isolated'].includes(p.margin_mode)&&
      Number.isSafeInteger(p.leverage)&&p.leverage>=1&&finite(p.size)&&p.size>0&&typeof p.size_text==='string'&&decimal(p.size_text)!=='—'&&Number(p.size_text)===p.size&&
      finite(p.limit_price)&&p.limit_price>0&&typeof p.limit_price_text==='string'&&decimal(p.limit_price_text)!=='—'&&Number(p.limit_price_text)===p.limit_price&&
      finite(p.reference_price)&&p.reference_price>0&&before.coin===p.coin&&before.dex===p.dex&&before.margin_mode===p.margin_mode&&
      finite(before.size)&&before.size>0&&finite(before.entry_price)&&before.entry_price>0&&finite(before.leverage)&&before.leverage===p.leverage&&before.side===p.direction&&
      finite(before.roe)&&before.roe<=-40&&finite(before.margin_used)&&before.margin_used>=0&&finite(before.unrealized_pnl)&&
      finite(after.size)&&after.size>=0&&finite(after.entry_price)&&after.entry_price>0&&finite(after.margin_estimate_usdc)&&after.margin_estimate_usdc>=0&&
      finite(p.action_notional_usdc)&&p.action_notional_usdc>=10&&finite(p.margin_change_estimate_usdc)&&finite(p.realized_pnl_estimate_usdc)&&
      finite(p.estimated_fee_usdc)&&p.estimated_fee_usdc>=0&&finite(p.source_slot_usdc)&&p.source_slot_usdc>0&&
      typeof p.source_wallet==='string'&&/^0x[0-9a-f]{40}$/i.test(p.source_wallet)&&finite(p.source_reserved_usdc)&&p.source_reserved_usdc>=0&&
      finite(p.extra_used_usdc)&&p.extra_used_usdc>=0&&finite(p.extra_remaining_usdc)&&p.extra_remaining_usdc>=0&&
      Number.isSafeInteger(p.additions_used)&&p.additions_used>=0&&Number.isSafeInteger(p.additions_remaining)&&p.additions_remaining>=0&&p.additions_remaining<=4&&
      stop.open_orders_count===0&&stop.stop_orders_count===0&&stop.policy==='no_automatic_stop_changes'&&completeFactors(p.factors);
    if(!basic)return false;
    const tolerance=Math.max(1e-12,before.size*1e-9);
    if(p.action==='REDUCE')return p.size<=before.size*.25+tolerance&&p.margin_change_estimate_usdc<=0&&Math.abs(after.size-(before.size-p.size))<=tolerance;
    return Math.abs(after.size-(before.size+p.size))<=tolerance&&Number.isSafeInteger(p.additions_remaining)&&p.additions_remaining>0&&
      Number.isSafeInteger(p.additions_used)&&p.additions_used>=0&&p.additions_used<4&&finite(p.extra_remaining_usdc)&&p.extra_remaining_usdc>=p.margin_change_estimate_usdc&&
      finite(p.extra_used_usdc)&&p.extra_used_usdc>=0&&p.extra_used_usdc+p.margin_change_estimate_usdc<=p.source_slot_usdc*.5+1e-8&&p.margin_change_estimate_usdc>0&&
      p.margin_change_estimate_usdc<=Math.min(before.margin_used*.25,p.extra_remaining_usdc*.25)+1e-8;
  }
  function factors(raw){const f=obj(raw),trend=obj(f.trend_ema20_50),levels=obj(f.levels20);
    const rows=[['EMA20 / EMA50',`${num(trend.ema20)} / ${num(trend.ema50)}`],['RSI14',num(f.rsi14)],['MACD · '+t('гистограмма','histogram'),num(f.macd_hist)],
      ['ATR14',`${num(f.atr14_pct)}%`],[t('Относительный объём','Relative volume'),num(f.volume_ratio20)],
      [t('Поддержка / сопротивление','Support / resistance'),`${num(levels.support)} / ${num(levels.resistance)}`],
      [t('Фандинг · б.п./ч','Funding · bps/h'),num(f.funding_bps_hour)],[t('Открытый интерес · снимок','Open interest · snapshot'),num(f.open_interest)]];
    return `<details class="aiModeHistory"><summary>${t('Причины · 8 индикаторов','Reasons · 8 indicators')}</summary><dl class="aiPositionFactors">`+
      rows.map(([name,value],i)=>`<dt>${i+1}. ${esc(name)}</dt><dd>${esc(value)}</dd>`).join('')+`</dl><small>${t('Последняя закрытая свеча','Last closed candle')}: ${when(f.candle_close_ms)}. `+
      `${t('Индикаторы не гарантируют разворот. Открытый интерес — снимок, не его тренд.','Indicators do not guarantee a reversal. Open interest is a snapshot, not its trend.')}</small></details>`;
  }
  function alternatives(rows){if(!Array.isArray(rows)||!rows.length)return '';
    return `<details class="aiModeHistory"><summary>${t('Другие варианты','Other options')}</summary>`+rows.slice(0,8).map(raw=>{const row=obj(raw);return `<p class="muted"><b>${action(row.action)}</b> · ${reason(row.reason)}</p>`;}).join('')+'</details>';}
  function proposal(row){const p=obj(row.payload),before=obj(p.position_before),after=obj(p.expected_after),identifierValue=identifier(row.id);
    const isExpired=row.status==='PENDING'&&expired(row),side=p.direction==='LONG'?t('ЛОНГ','LONG'):p.direction==='SHORT'?t('ШОРТ','SHORT'):'—';
    const beforeNotional=finite(before.size)&&finite(p.reference_price)?before.size*p.reference_price:null;
    const afterNotional=finite(after.size)&&finite(p.reference_price)?after.size*p.reference_price:null;
    const changePct=finite(p.margin_change_estimate_usdc)&&finite(p.source_slot_usdc)&&p.source_slot_usdc>0?p.margin_change_estimate_usdc/p.source_slot_usdc*100:null;
    const source=typeof p.source_wallet==='string'&&/^0x[0-9a-f]{40}$/i.test(p.source_wallet)?'…'+p.source_wallet.slice(-6):'—';
    const disable=busy||failure||attempted.has(identifierValue)||!contract()||!valid(row)||window.walletHunterPrivacy;
    const stopping=obj(p.stop_impact);
    return `<article class="aiPositionProposal"><div class="aiPositionHeading"><b>${esc(p.coin||'—')} · ${side}</b><span class="loss">ROE ${num(before.roe)}%</span></div>`+
      `<p class="aiPositionActionTitle"><b>${action(p.action)}</b> · ${status(isExpired?'EXPIRED':row.status)}</p>`+
      `<p class="aiPositionHypothesis">${p.action==='REDUCE'?t('Уменьшение объёма снижает рыночную экспозицию, но фиксирует часть текущего результата. Остаток может продолжить убывать.','Reducing size lowers market exposure but realizes part of the current result. The remaining position may keep losing value.'):t('Дополнительный вход меняет среднюю цену и увеличивает объём риска. Даже при поддержке индикаторов цена может продолжить движение против позиции.','Adding changes the average entry and increases risk exposure. Even with indicator support, price may continue against the position.')}</p>`+
      `<p class="aiPositionUnknown">${t('Вероятность восстановления: неизвестна. Положительный исход не гарантируется. Вы принимаете решение с этой неопределённостью.','Recovery probability: unknown. A positive outcome is not guaranteed. Your decision accepts this uncertainty.')}</p>`+
      `<p class="muted">${p.network==='MAINNET'?t('Основная сеть · реальные средства','Mainnet · real funds'):p.network==='TESTNET'?t('Тестовая сеть · тестовые средства','Testnet · test funds'):t('Сеть не подтверждена','Network unconfirmed')} · ${t('Источник','Source')}: ${esc(source)}</p>`+
      `<table class="aiPositionBeforeAfter"><thead><tr><th>${t('Параметр','Parameter')}</th><th>${t('Сейчас','Before')}</th><th>${t('После · оценка','After · estimate')}</th></tr></thead><tbody>`+
      `<tr><th>${t('Количество','Quantity')}</th><td>${size(before.size)}</td><td>${size(after.size)}</td></tr>`+
      `<tr><th>${t('Плечо','Leverage')}</th><td>${num(before.leverage)}×</td><td>${num(p.leverage)}×</td></tr>`+
      `<tr><th>${t('Цена входа','Entry price')}</th><td>${decimal(before.entry_price)}</td><td>${decimal(after.entry_price)}</td></tr>`+
      `<tr><th>${t('Маржа','Margin')}</th><td>${money(before.margin_used)}</td><td>${money(after.margin_estimate_usdc)}</td></tr>`+
      `<tr><th>${t('Объём позиции','Notional')}</th><td>${money(beforeNotional)}</td><td>${money(afterNotional)}</td></tr></tbody></table>`+
      `<div class="aiModeMetrics"><span>${t('Точное изменение количества','Exact quantity change')}<b>${size(p.size_text)}</b></span>`+
      `<span>${t('Точный лимит IOC','Exact IOC limit')}<b>${decimal(p.limit_price_text)}</b></span>`+
      `<span>${t('Объём действия','Action notional')}<b>${money(p.action_notional_usdc)}</b></span>`+
      `<span>${t('Изменение маржи · оценка','Margin change · estimate')}<b>${money(p.margin_change_estimate_usdc)} · ${num(changePct)}%</b></span>`+
      `<span>${t('Результат после комиссии · оценка','PnL after fee · estimate')}<b>${money(p.realized_pnl_estimate_usdc)}</b></span>`+
      `<span>${t('Комиссия действия · оценка','Action fee · estimate')}<b>${money(p.estimated_fee_usdc)}</b></span>`+
      `<span>${t('Доля кошелька · 1/3','Source share · 1/3')}<b>${money(p.source_slot_usdc)}</b></span>`+
      `<span>${t('Занято в доле','Reserved in share')}<b>${money(p.source_reserved_usdc)}</b></span>`+
      `<span>${t('Доп. резерв · использовано / остаток','Extra reserve · used / left')}<b>${money(p.extra_used_usdc)} / ${money(p.extra_remaining_usdc)}</b></span>`+
      `<span>${t('Доборы · использовано / осталось','Additions · used / left')}<b>${num(p.additions_used)} / ${num(p.additions_remaining)}</b></span></div>`+
      `<p class="muted">${t('До 50% доли и до 4 доборов суммарно для всех позиций одного исходного кошелька. Процент изменения маржи рассчитан от этой доли.','Up to 50% of the share and 4 additions shared across all positions from one source wallet. Margin-change percentage is relative to that share.')}</p>`+
      `<p class="aiPositionRisk">${p.margin_mode==='cross'?t('CROSS: общим обеспечением могут служить средства всего аккаунта. Расчёт по доле не изолирует деньги и не ограничивает реальный убыток этой суммой.','CROSS: the whole account’s collateral may be used. Share-based sizing does not segregate funds or limit the actual loss to that amount.'):t('Изолированная маржа: ликвидация возможна раньше ожидаемого восстановления. Оценка результата не является защитным ордером.','Isolated margin: liquidation may occur before an expected recovery. The estimated outcome is not a protective order.')}</p>`+
      `<p class="aiPositionRisk">${stopping.open_orders_count===0&&stopping.stop_orders_count===0?t('Активных защитных стопов не обнаружено. Это действие не устанавливает стоп и не гарантирует защиту.','No active protective stops were found. This action does not place a stop or guarantee protection.'):t('Стопы или другие ордера требуют проверки. Автоматически они не изменяются.','Stops or other orders require review. They are not changed automatically.')}</p>`+
      `<p class="muted">${t('IOC исполняется сразу по лимиту или лучше; остаток снимается. Возможны частичное исполнение и иной итог. При изменении позиции, цены или бюджета требуется новое предложение.','IOC executes immediately at the limit or better; the remainder is cancelled. A partial fill and a different outcome are possible. Position, price or budget changes require a new proposal.')}</p>`+
      `<p class="aiPositionRisk">${t('После изменения копирование этого инструмента приостанавливается до отдельной сверки и возобновления. Бот не должен сразу отменить вашу корректировку вслед за кошельком.', 'After the change, copying for this market is paused until separate reconciliation and resumption. The bot should not immediately undo your adjustment by following the source.')}</p>`+
      `<p class="muted">${t('Выход в +3% не обещан. Уровень −120% не является биржевым стопом: ликвидация может наступить раньше.', 'A +3% exit is not promised. The −120% level is not an exchange stop: liquidation may occur sooner.')}</p>`+
      factors(p.factors)+alternatives(p.alternatives)+`<p class="aiPositionExpiry">${t('Действует до','Valid until')}: ${when(row.expires_ms)}</p>`+
      (window.walletHunterPrivacy?`<p class="aiPositionRisk">${t('Откройте суммы глазиком, чтобы проверить условия перед подтверждением.','Reveal amounts with the eye icon to review the terms before confirming.')}</p>`:'')+
      (attempted.has(identifierValue)&&!busy?`<p class="loss">${t('Решение уже отправлено. Не повторяйте действие; проверьте статус и биржу.','The decision was already sent. Do not repeat the action; check the status and exchange.')}</p>`:'')+
      (!valid(row)&&!isExpired?`<p class="loss">${t('Условия не подтверждены. Отправка недоступна; обновите анализ.','Terms are unverified. Submission is unavailable; refresh the analysis.')}</p>`:'')+
      `<div class="aiPositionButtons"><button type="button" class="aiPositionConfirm" data-position-confirm="${esc(identifierValue||'')}" ${disable?'disabled':''}>${t('Подтвердить','Confirm')} · ${action(p.action)}</button>`+
      `<button type="button" class="aiPositionDecline" data-position-decline="${esc(identifierValue||'')}" ${busy||failure||!identifierValue||!pending(row)||attempted.has(identifierValue)?'disabled':''}>${t('Нет · отказаться','No · decline')}</button></div>`+
      `<small>${t('Одно подтверждение — только это действие. Оно не разрешает автоматические доборы, закрытия или «спасение» позиции.','One confirmation authorizes only this action. It does not permit automatic additions, closes or position “rescue”.')}</small></article>`;
  }
  function information(raw){const row=obj(raw);return `<div class="aiPositionInfo"><b>${esc(row.coin||'—')} · ${row.action?action(row.action):t('Проверка недоступна','Review unavailable')}</b><p class="muted">${reason(row.reason||row.position_reason)}</p></div>`;}
  function historyRow(raw){const row=obj(raw),p=obj(row.payload),result=obj(row.result);
    return `<div class="aiPositionHistory"><b>${esc(p.coin||'—')} · ${action(p.action)} · ${status(row.status)}</b><small>${when(row.created_ms)}</small>`+
      (finite(result.filled_size)?`<p>${t('Фактически исполнено','Verified filled quantity')}: ${size(result.filled_size)}</p>`:'')+
      (['UNKNOWN','SUBMITTING'].includes(row.status)?`<p class="loss">${reason('execution_pending')}</p>`:'')+'</div>';}
  function holdRow(row){const marketValue=heldMarket(row.market),allowed=releasable(row),sent=releaseAttempts.has(releaseKey(row));
    return `<div class="aiPositionHold"><b>${esc(marketValue?marketValue.split('|')[0]:'—')} · ${t('Копирование на паузе','Copying paused')}</b><p>${status(row.status)}</p>`+
      `<p class="aiPositionRisk">${t('Возобновление снова синхронизирует позицию с исходным кошельком. Объём и плечо могут измениться; бот может снова увеличить или закрыть позицию, отменив вашу корректировку.','Resuming synchronizes the position with its source wallet again. Size and leverage may change; the bot may increase or close the position again, undoing your adjustment.')}</p>`+
      (!allowed?`<p class="muted">${reason('intervention_reconciliation_required')}</p>`:'')+
      (sent?`<p class="loss">${t('Запрос возобновления уже отправлен. Повтор заблокирован; обновите статус и проверьте результат.','The resume request was already sent. Repetition is blocked; refresh the status and verify the result.')}</p>`:'')+
      (allowed&&window.walletHunterPrivacy?`<p class="muted">${t('Отключите скрытие сумм перед возобновлением копирования.','Reveal amounts before resuming copying.')}</p>`:'')+
      (allowed?`<button type="button" class="aiPositionRelease" data-position-release="${esc(marketValue)}" ${busy||failure||sent||!contract()||window.walletHunterPrivacy?'disabled':''}>${t('Возобновить копирование…','Resume copying…')}</button>`:'')+'</div>';}
  function render(data){if(data!==latest)failure=null;latest=data;const summary=obj(data),rows=pendingRows(),history=Array.isArray(summary.history)?summary.history.map(obj):[];
    history.filter(row=>!['UNKNOWN','SUBMITTING','PENDING'].includes(row.status)).forEach(row=>attempted.delete(identifier(row.id)));
    for(const attemptedKey of releaseAttempts)if(!heldRows().some(row=>releaseKey(row)===attemptedKey))releaseAttempts.delete(attemptedKey);
    return `<section class="card aiPositionActionsCard" data-self-localized="true"><h3>${t('Позиции · решения с ИИ','Positions · decisions with AI')}</h3>`+
      `<p>${t('Порог ROE ≤−40% отдельной позиции. ИИ подготавливает варианты; каждое реальное изменение выполняется только по вашему подтверждению.','Trigger: ROE ≤−40% for an individual position. AI prepares options; each real change requires your confirmation.')}</p>`+
      `<p><b>${status(summary.status)}</b></p>`+(summary.reason?`<p class="muted">${reason(summary.reason)}</p>`:'')+
      rows.map(proposal).join('')+(Array.isArray(summary.availability)?summary.availability.slice(0,12).map(information).join(''):'')+
      heldRows().slice(0,12).map(holdRow).join('')+
      `<button type="button" class="aiPositionPrepare" data-position-prepare ${prepareAllowed()?'':'disabled'}>${busy?t('Проверка…','Checking…'):t('Проанализировать позиции','Analyse positions')}</button>`+
      (failure?`<p class="loss" role="alert">${failure===401||failure===403?t('Откройте приложение заново через Telegram.','Reopen the app through Telegram.'):t('Ответ не подтверждён. Отправка не повторяется; выполнено только повторное чтение статуса.','Response unconfirmed. Submission is not retried; only status is read again.')}</p>`:'')+
      `<details class="aiModeHistory"><summary>${t('История решений по позициям','Position decision history')}</summary>${history.map(historyRow).join('')||`<p class="muted">${t('Решений пока нет.','No decisions yet.')}</p>`}</details></section>`;
  }
  function scheduleExpiry(){clearTimeout(expiryTimer);const times=pendingRows().filter(pending).map(row=>row.expires_ms);if(times.length&&container?.isConnected!==false)expiryTimer=setTimeout(paint,Math.max(1,Math.min(...times)-Date.now()+5));}
  function paint(){if(!container||container.isConnected===false)return;container.innerHTML=render(latest);attach();scheduleExpiry();}
  async function submit(kind,idValue=null){if(busy||failure||!contract()||typeof callbacks?.api!=='function')return;const row=pendingRows().find(row=>identifier(row.id)===idValue),hold=heldRows().find(row=>row.market===idValue);
    if(kind==='release'){
      if(!hold||!releasable(hold)||releaseAttempts.has(releaseKey(hold))||window.walletHunterPrivacy||typeof window.confirm!=='function')return;
      if(window.confirm(`${hold.market.split('|')[0]}\n\n${releaseWarning()}`)!==true)return;
    }else if(kind==='prepare'?!prepareAllowed():!row||!pending(row)||attempted.has(idValue))return;
    if(kind==='confirm'&&(window.walletHunterPrivacy||!valid(row)))return;
    busy=true;failure=null;paint();let timer;
    try{const controller=new AbortController();const deadline=new Promise((resolve,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new Error('UNCONFIRMED'));},15000);});
      if(kind==='confirm')pendingRows().filter(other=>market(other)===market(row)).forEach(other=>attempted.add(identifier(other.id)));
      else if(kind==='decline')attempted.add(idValue);
      else if(kind==='release')releaseAttempts.add(releaseKey(hold));
      const url=kind==='prepare'?'/api/ai/positions/prepare':kind==='release'?'/api/ai/positions/resume':`/api/ai/positions/${encodeURIComponent(idValue)}/decision`;
      const body=kind==='prepare'?{}:kind==='release'?{market:idValue}:{confirm:kind==='confirm'};
      await Promise.race([callbacks.api(url,body,controller.signal),deadline]);clearTimeout(timer);await callbacks.reload?.();
    }catch(error){failure=Number(error?.status)||1;try{await callbacks.reload?.();}catch(_){} }
    finally{clearTimeout(timer);busy=false;paint();}
  }
  function attach(){container?.querySelectorAll('[data-position-prepare]').forEach(button=>button.addEventListener('click',()=>submit('prepare')));
    container?.querySelectorAll('[data-position-confirm]').forEach(button=>button.addEventListener('click',()=>submit('confirm',button.dataset.positionConfirm)));
    container?.querySelectorAll('[data-position-decline]').forEach(button=>button.addEventListener('click',()=>submit('decline',button.dataset.positionDecline)));
    container?.querySelectorAll('[data-position-release]').forEach(button=>button.addEventListener('click',()=>submit('release',button.dataset.positionRelease)));}
  function bind(target,options){container=target;callbacks=options;attach();scheduleExpiry();}
  window.whAiPositionActions={render,bind};window.addEventListener('whlanguage',paint);window.addEventListener('whprivacy',paint);
})();
