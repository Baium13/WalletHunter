/* Every real order requires a separate explicit user decision. Never auto-trade. */
(() => {
  if (window.whAiUserOrders) return;
  const en = () => window.walletHunterLanguage === 'en';
  const t = (ru, eng) => en() ? eng : ru;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const key = value => typeof value === 'string' ? value.toUpperCase() : '';
  const number = value => finite(value) ? value.toLocaleString(en() ? 'en-GB' : 'ru-RU', {maximumFractionDigits: 8}) : '—';
  const mask = () => `<span class="maskedValue" aria-label="${t('Сумма скрыта','Amount hidden')}">••••••</span>`;
  const money = value => window.walletHunterPrivacy ? mask() : finite(value)
    ? `${value.toLocaleString(en() ? 'en-GB' : 'ru-RU', {minimumFractionDigits: 2,maximumFractionDigits: 4})} USDC` : '—';
  const decimal = value => typeof value === 'string' && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value))
    ? esc(value) : finite(value) && value >= 0 ? esc(String(value)) : '—';
  const when = value => finite(value) && value > 0 && Number.isFinite(new Date(value).getTime())
    ? esc(new Date(value).toLocaleString(en() ? 'en-GB' : 'ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'})) : '—';
  const id = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(value) ? value
    : Number.isSafeInteger(value) && value > 0 ? String(value) : null;
  const statuses = {
    READY:['Можно подготовить предложение','Ready to prepare a proposal'], IDLE:['Нет ожидающего ордера','No pending order'], AVAILABLE:['Доступно с подтверждением','Available with confirmation'],
    BLOCKED:['Подготовка недоступна','Preparation unavailable'], UNAVAILABLE:['Данные недоступны','Data unavailable'],
    PENDING:['Ожидает вашего решения','Awaiting your decision'], PREPARED:['Ожидает вашего решения','Awaiting your decision'],
    DECLINED:['Отклонено вами','Declined by you'], EXPIRED:['Срок предложения истёк','Proposal expired'],
    INVALIDATED:['Условия изменились','Conditions changed'], EXECUTING:['Проверяется исполнение','Checking execution'], SUBMITTING:['Отправка и проверка исполнения','Submission and execution check'],
    EXECUTED:['Исполнение подтверждено','Execution verified'], FILLED:['Исполнение подтверждено','Execution verified'],
    PARTIAL:['Частичное исполнение','Partial fill'], UNKNOWN:['Исполнение не подтверждено','Execution unconfirmed'],
    NO_FILL:['Не исполнено','Not filled'], REJECTED:['Отклонено биржей','Rejected by exchange'], ERROR:['Нужна проверка состояния','State check needed']
  };
  const reasons = {
    ACCOUNT_MISSING:['Подключите Hyperliquid-аккаунт в настройках.','Connect your Hyperliquid account in Settings.'],
    AI_SLOT_UNAVAILABLE:['Выберите ИИ в третьем слоте раздела «Кошельки».','Select AI in the third slot in Wallets.'],
    SLOT_NOT_SELECTED:['Выберите ИИ в третьем слоте раздела «Кошельки».','Select AI in the third slot in Wallets.'],
    TOO_MANY_WALLETS:['Три слота заняты кошельками. Для ИИ нужен третий слот.','Three slots are occupied by wallets. AI requires the third slot.'],
    BELOW_MINIMUM:['Объём ниже 10 USDC. Ордер не отправляется и автоматически не увеличивается.','Notional is below 10 USDC. No order is sent or automatically increased.'],
    BELOW_MIN_NOTIONAL:['Объём ниже 10 USDC. Ордер не отправляется и автоматически не увеличивается.','Notional is below 10 USDC. No order is sent or automatically increased.'],
    MARKET_OCCUPIED:['По инструменту уже есть позиция или ордер. Повторный вход заблокирован.','This market already has a position or order. Another entry is blocked.'],
    NO_MODEL:['Нет готовой исследовательской модели для предложения.','No research model is available to prepare a proposal.'],
    NO_SIGNAL:['Сейчас нет подходящего исследовательского сигнала.','No eligible research signal is available now.'],
    STALE_DATA:['Нужны свежие подтверждённые данные.','Fresh verified data is required.'],
    PRICE_CHANGED:['Цена изменилась. Подготовьте новое предложение: старый ордер не будет переоценён автоматически.','Price changed. Prepare a new proposal: the old order will not be repriced automatically.'],
    BUDGET_CHANGED:['Бюджет изменился. Старое предложение недействительно.','Budget changed. The previous proposal is no longer valid.'],
    INSUFFICIENT_BUDGET:['Недостаточно выделенного бюджета или свободной маржи.','The allocated budget or free margin is insufficient.'],
    EXPIRED:['Срок предложения истёк. Подготовьте новое.','The proposal expired. Prepare a new one.'],
    PUBLIC_DATA_UNAVAILABLE:['Рыночные данные недоступны. Ордер не подготовлен.','Market data is unavailable. No order was prepared.'],
    EXECUTION_UNRESOLVED:['Предыдущее исполнение ещё не сверено. Повторная отправка запрещена.','Previous execution is not reconciled yet. Do not submit again.']
  };
  const label = (map,value,fallback) => t(...(Object.hasOwn(map,key(value)) ? map[key(value)] : fallback));
  const status = value => label(statuses,value,['Статус не подтверждён','Status unconfirmed']);
  const reason = value => label(reasons,value,['Действие недоступно. Обновите состояние.','Action unavailable. Refresh the status.']);
  Object.assign(reasons, {
    MODEL_UNAVAILABLE:reasons.NO_MODEL, MINIMUM_NOTIONAL:reasons.BELOW_MINIMUM,
    POSITION_CONFLICT:reasons.MARKET_OCCUPIED, OPEN_ORDER_CONFLICT:reasons.MARKET_OCCUPIED,
    INSTRUMENT_OWNED_OR_HELD:reasons.MARKET_OCCUPIED, INSUFFICIENT_CAPACITY:reasons.INSUFFICIENT_BUDGET,
    AI_POSITION_RECONCILIATION_REQUIRED:reasons.EXECUTION_UNRESOLVED,
    AI_SLOT_BUDGET_EXHAUSTED:['Выделенная доля ИИ уже занята. Новый ордер не увеличит бюджет автоматически.','The allocated AI share is already reserved. A new order will not increase its budget automatically.'],
    CRYPTO_DISABLED:['Крипторынки отключены в настройках. Подготовка ордера недоступна.','Crypto markets are disabled in Settings. Order preparation is unavailable.'],
    EXECUTION_PENDING:reasons.EXECUTION_UNRESOLVED, MARKET_DATA_UNAVAILABLE:reasons.PUBLIC_DATA_UNAVAILABLE,
    SIGNAL_ALREADY_HANDLED:['Этот сигнал уже обработан. Повторной отправки по нему не будет.','This signal was already handled. It will not be submitted again.'],
    UNSUPPORTED_CAPITAL_MODE:['Режим обеспечения аккаунта не поддерживается для этого ордера.','The account capital mode is not supported for this order.'],
    MARKET_MODE_UNSUPPORTED:['Маржинальный режим инструмента не поддерживается.','The market margin mode is not supported.'],
    CONFIRMATION_REQUIRED:['Проверьте замороженные условия. Отправка — только по кнопке «Подтвердить».','Review the frozen terms. Submission requires the Confirm button.'],
    PROPOSAL_EXPIRED:['Срок предложения истёк; оно не будет отправлено.','The proposal expired and will not be submitted.'],
    EXCHANGE_REJECTED_VERIFIED_FLAT:['Биржа отклонила ордер; отсутствие открытой позиции подтверждено.','The exchange rejected the order; no open position was verified.'],
    FRESH_CHECKS_FAILED_PREPARE_NEW_FORM:['Свежая проверка не пройдена. Старый ордер не отправлен; подготовьте новое предложение.','Fresh checks failed. The previous order was not sent; prepare a new proposal.'],
    SUBMISSION_OR_VERIFICATION_UNCONFIRMED_NO_RETRY:['Результат отправки не подтверждён. Не отправляйте повторно; обновите статус и проверьте биржу.','Submission outcome is unconfirmed. Do not submit again; refresh status and check the exchange.']
  });
  let latest = null, container = null, callbacks = null, busy = false, failure = null, expiryTimer = null;
  const uncertain = new Set();
  const rows = () => Array.isArray(obj(latest).pending) ? latest.pending.map(obj) : [];
  const expired = row => !finite(row.expires_ms) || row.expires_ms <= Date.now();
  const pending = row => row.status === 'PENDING' && !expired(row);
  const ownSize = value => window.walletHunterPrivacy ? mask() : decimal(value);
  function validOrder(row) {
    const p = obj(row.payload), side = p.side || p.direction;
    return id(row.id) !== null && pending(row) && p.action === 'OPEN' && ['BTC','ETH'].includes(p.coin) && p.dex === '' &&
      ['LONG','SHORT'].includes(side) && (!p.direction || p.direction === side) && p.order_type === 'LIMIT_IOC' && p.margin_mode === 'cross' &&
      ['MAINNET','TESTNET'].includes(p.network) && Number.isSafeInteger(p.leverage) && p.leverage >= 1 && p.leverage <= 40 &&
      finite(p.size) && p.size > 0 && typeof p.size_text === 'string' && decimal(p.size_text) !== '—' && Number(p.size_text) === p.size &&
      finite(p.limit_price) && p.limit_price > 0 && typeof p.limit_price_text === 'string' && decimal(p.limit_price_text) !== '—' && Number(p.limit_price_text) === p.limit_price &&
      finite(p.share_fraction) && Math.abs(p.share_fraction - 1/3) < 1e-12 && p.entry_pct_of_share === 10 &&
      finite(p.share_usdc) && p.share_usdc > 0 && finite(p.margin_estimate_usdc) && p.margin_estimate_usdc > 0 &&
      finite(p.margin_cap_usdc) && p.margin_cap_usdc > 0 && Math.abs(p.margin_cap_usdc-p.share_usdc*.10) < 1e-8 &&
      finite(p.maximum_expected_margin_usdc) && p.maximum_expected_margin_usdc >= p.margin_estimate_usdc &&
      p.maximum_expected_margin_usdc <= p.margin_cap_usdc + 1e-12 &&
      finite(p.notional_usdc) && p.notional_usdc >= 10 && finite(p.estimated_fee_usdc) && p.estimated_fee_usdc >= 0;
  }
  const contract = () => obj(latest).execution_mode === 'USER_CONFIRMATION_ONLY' && obj(latest).automatic_execution === false;
  function prepareAllowed() {
    return contract() && !busy && !failure && !rows().some(pending) &&
      !['ACCOUNT_MISSING','AI_SLOT_UNAVAILABLE','TOO_MANY_WALLETS','EXECUTION_PENDING','CRYPTO_DISABLED','AI_POSITION_RECONCILIATION_REQUIRED'].includes(key(obj(latest).reason));
  }
  function orderCard(row) {
    const p = obj(row.payload), source = obj(p.source), identifier = id(row.id), isExpired = row.status === 'PENDING' && expired(row);
    const side = (p.side || p.direction) === 'LONG' ? t('ЛОНГ','LONG') : (p.side || p.direction) === 'SHORT' ? t('ШОРТ','SHORT') : '—';
    const score = finite(source.probability_positive_net) && source.probability_positive_net >= 0 && source.probability_positive_net <= 1
      ? number(source.probability_positive_net) : '—';
    const network = p.network === 'MAINNET' ? t('Основная сеть · реальные средства','Mainnet · real funds') : p.network === 'TESTNET' ? t('Тестовая сеть · тестовые средства','Testnet · test funds') : t('Сеть не подтверждена','Network unconfirmed');
    const disabled = busy || failure || uncertain.has(identifier) || !contract() || !validOrder(row) || window.walletHunterPrivacy;
    return `<article class="aiUserOrder"><div class="aiUserOrderHeading"><b>${esc(p.coin || '—')} · ${side} · ${number(p.leverage)}×</b><span>${t('ОТКРЫТИЕ','OPEN')}</span></div>` +
      `<p class="aiUserNetwork">${network}</p><p><b>${status(isExpired ? 'EXPIRED' : row.status)}</b></p>` +
      `<div class="aiModeMetrics"><span>${t('Доля ИИ · фиксированная 1/3','AI share · fixed 1/3')}<b>${money(p.share_usdc)}</b></span>` +
      `<span>${t('На вход · 10% своей доли','Entry · 10% of own share')}<b>${money(p.margin_cap_usdc)}</b></span>` +
      `<span>${t('Точное количество','Exact quantity')}<b>${ownSize(p.size_text)}</b></span>` +
      `<span>${t('Точный лимит IOC','Exact IOC limit')}<b>${decimal(p.limit_price_text)}</b></span>` +
      `<span>${t('Маржа · оценка','Margin · estimate')}<b>${money(p.margin_estimate_usdc)}</b></span>` +
      `<span>${t('Оценка маржи у лимита','Margin estimate at limit')}<b>${money(p.maximum_expected_margin_usdc)}</b></span>` +
      `<span>${t('Объём позиции · оценка','Position notional · estimate')}<b>${money(p.notional_usdc)}</b></span>` +
      `<span>${t('Комиссия входа · оценка','Entry fee · estimate')}<b>${money(p.estimated_fee_usdc)}</b></span></div>` +
      `<p class="aiUserExpiry">${t('Действует до','Valid until')}: ${when(row.expires_ms)}</p>` +
      `<p class="aiModeWarning">${t('CROSS: расчётная маржа — не максимальный убыток. Общим обеспечением могут служить средства всего аккаунта. Доля 1/3 — лимит расчёта, не изоляция денег.', 'CROSS: estimated margin is not the maximum loss. The whole account’s collateral may be used. The 1/3 share is a sizing limit, not segregated funds.')}</p>` +
      `<p class="muted">${t('IOC: исполнение сразу по указанному лимиту или лучше; неисполненный остаток снимается. Возможен частичный вход. При изменении цены или бюджета предложение отменяется, а не меняется скрыто.', 'IOC: immediate execution at the stated limit or better; any unfilled remainder is cancelled. A partial fill is possible. Price or budget changes invalidate the proposal rather than silently alter it.')}</p>` +
      `<p class="muted">${t('Экспериментальная оценка модели (0–1)','Experimental model score (0–1)')}: <b>${score}</b>. ` +
      `${t('Не калиброванная вероятность прибыли или исполнения. Исследовательский прогноз рассчитан на другой момент входа, не на этот немедленный IOC.', 'Not a calibrated profit or execution probability. The research forecast assumes a different entry time, not this immediate IOC.')}</p>` +
      (window.walletHunterPrivacy ? `<p class="aiUserNotice">${t('Откройте суммы глазиком, чтобы проверить условия перед подтверждением.', 'Reveal amounts with the eye icon to review the terms before confirming.')}</p>` : '') +
      (uncertain.has(identifier) && !busy ? `<p class="loss" role="status">${t('Результат не подтверждён. Повторная отправка этого предложения заблокирована; проверьте статус и биржу.', 'Outcome unconfirmed. Resubmission of this proposal is blocked; check the status and exchange.')}</p>` : '') +
      (!validOrder(row) && !isExpired ? `<p class="loss">${t('Условия не прошли проверку. Подтверждение недоступно; обновите данные.', 'Terms did not pass validation. Confirmation is unavailable; refresh data.')}</p>` : '') +
      `<div class="aiUserActions"><button type="button" class="aiUserConfirm" data-ai-order-confirm="${esc(identifier || '')}" ${disabled ? 'disabled' : ''}>${t('Подтвердить · отправить ордер','Confirm · send order')}</button>` +
      `<button type="button" class="aiUserDecline" data-ai-order-decline="${esc(identifier || '')}" ${busy || failure || !identifier || !pending(row) || uncertain.has(identifier) ? 'disabled' : ''}>${t('Нет · отклонить','No · decline')}</button></div>` +
      `<small>${t('Одно нажатие «Подтвердить» отправляет именно этот ордер после свежих проверок. Это не разрешение на автономную торговлю.', 'One Confirm tap submits this exact order after fresh checks. It is not permission for autonomous trading.')}</small></article>`;
  }
  function historyCard(row) {
    const p = obj(row.payload), result = obj(row.result), identifier = id(row.id);
    return `<div class="aiUserHistoryRow"><b>${esc(p.coin || '—')} · ${status(row.status)}</b><small>${when(row.created_ms)}</small>` +
      (finite(result.filled_size) ? `<p>${t('Исполненное количество','Filled quantity')}: ${ownSize(result.filled_size)} · ${t('Средняя цена','Average price')}: ${decimal(result.average_price)}</p>` : '') +
      (['UNKNOWN','SUBMITTING'].includes(row.status) || uncertain.has(identifier) ? `<p class="loss">${reason('execution_pending')}</p>` : '') +
      (result.reason && !['UNKNOWN','SUBMITTING','FILLED','PARTIAL'].includes(row.status) ? `<p class="muted">${reason(result.reason)}</p>` : '') + '</div>';
  }
  function render(data) {
    if (data !== latest) failure = null;
    latest = data;
    const summary = obj(data), active = rows(), history = Array.isArray(summary.history) ? summary.history.map(obj) : [];
    history.filter(row => !['UNKNOWN','SUBMITTING','PENDING'].includes(row.status)).forEach(row => uncertain.delete(id(row.id)));
    return `<section class="card aiUserOrdersCard" data-self-localized="true"><h3>${t('Ордер ИИ · ваше подтверждение','AI order · your confirmation')}</h3>` +
      `<p class="aiUserIntro">${t('Отдельное открытие BTC или ETH: только после вашего решения. Подготовка ничего не отправляет на биржу.', 'A separate BTC or ETH entry, only after your decision. Preparing a proposal sends nothing to the exchange.')}</p>` +
      `<p class="muted">${t('Бюджет: фиксированная 1/3. Маржа входа: до 10% этой доли, плечо до 40×, но не выше лимита биржи для инструмента. Ни автоматического закрытия, ни усреднения. После входа позицию контролируете вы.', 'Budget: fixed 1/3. Entry margin: up to 10% of that share, leverage up to 40×, capped by the exchange limit for the instrument. No automatic closing or averaging. You manage the position after entry.')}</p>` +
      `<p><b>${status(summary.status)}</b></p>` + (summary.reason ? `<p class="muted">${reason(summary.reason)}</p>` : '') +
      active.map(orderCard).join('') +
      `<button type="button" class="aiUserPrepare" data-ai-order-prepare ${prepareAllowed() ? '' : 'disabled'}>${busy ? t('Проверка…','Checking…') : t('Подготовить предложение','Prepare proposal')}</button>` +
      (failure ? `<p class="loss" role="alert">${failure === 401 || failure === 403 ? t('Откройте приложение заново через Telegram.','Reopen the app through Telegram.') : t('Ответ не подтверждён. Запрос не повторяется; обновите статус перед следующим действием.','Response unconfirmed. The request is not retried; refresh the status before the next action.')}</p>` : '') +
      `<details class="aiModeHistory"><summary>${t('История решений и исполнения','Decision and execution history')}</summary>${history.map(historyCard).join('') || `<p class="muted">${t('Истории пока нет.','No history yet.')}</p>`}</details></section>`;
  }
  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    const times = rows().filter(pending).map(row => row.expires_ms);
    if (times.length && container?.isConnected !== false) expiryTimer = setTimeout(paint, Math.max(1, Math.min(...times) - Date.now() + 5));
  }
  function paint() {
    if (!container || container.isConnected === false) return;
    container.innerHTML = render(latest); attach(); scheduleExpiry();
  }
  async function submit(kind, identifier = null) {
    if (busy || failure || !contract() || typeof callbacks?.api !== 'function') return;
    const row = rows().find(row => id(row.id) === identifier);
    if (kind === 'prepare' ? !prepareAllowed() : !row || !pending(row) || uncertain.has(identifier)) return;
    if (kind === 'confirm' && (window.walletHunterPrivacy || !validOrder(row))) return;
    busy = true; failure = null; paint();
    let timer;
    try {
      const controller = new AbortController();
      const timeout = new Promise((resolve,reject) => {timer = setTimeout(() => {controller.abort();reject(new Error('UNCONFIRMED'));},15000);});
      const url = kind === 'prepare' ? '/api/ai/orders/prepare' : `/api/ai/orders/${encodeURIComponent(identifier)}/decision`;
      const body = kind === 'prepare' ? {} : {confirm:kind === 'confirm'};
      if(kind !== 'prepare')uncertain.add(identifier);
      await Promise.race([callbacks.api(url,body,controller.signal),timeout]);
      clearTimeout(timer);
      await callbacks.reload?.();
    } catch (error) {
      failure = Number(error?.status) || 1;
      if (kind !== 'prepare') uncertain.add(identifier);
      // Only a GET refresh is allowed after ambiguity; never repeat the POST.
      try { await callbacks.reload?.(); } catch (_) { /* Keep the unconfirmed state. */ }
    } finally { clearTimeout(timer);busy = false;paint(); }
  }
  function attach() {
    container?.querySelectorAll('[data-ai-order-prepare]').forEach(button => button.addEventListener('click',()=>submit('prepare')));
    container?.querySelectorAll('[data-ai-order-confirm]').forEach(button => button.addEventListener('click',()=>submit('confirm',button.dataset.aiOrderConfirm)));
    container?.querySelectorAll('[data-ai-order-decline]').forEach(button => button.addEventListener('click',()=>submit('decline',button.dataset.aiOrderDecline)));
  }
  function bind(target,options) {container=target;callbacks=options;attach();scheduleExpiry();}
  window.whAiUserOrders={render,bind};
  window.addEventListener('whlanguage',paint);
  window.addEventListener('whprivacy',paint);
})();
