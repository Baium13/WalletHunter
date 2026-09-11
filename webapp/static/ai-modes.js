/* Independent AI modes. Trader is virtual only; no exchange-order controls. */
(() => {
  if (window.whAiModes) return;
  const en = () => window.walletHunterLanguage === 'en';
  const t = (ru, eng) => en() ? eng : ru;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const num = value => finite(value) ? value.toLocaleString(en() ? 'en-GB' : 'ru-RU', {maximumFractionDigits: 2}) : '—';
  const money = value => window.walletHunterPrivacy
    ? `<span class="maskedValue" aria-label="${t('Сумма скрыта', 'Amount hidden')}">••••••</span>`
    : finite(value) ? `${num(value)} USDC` : '—';
  const when = value => finite(value) && value > 0 && Number.isFinite(new Date(value).getTime())
    ? esc(new Date(value).toLocaleString(en() ? 'en-GB' : 'ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'})) : '—';
  const statusNames = {
    READY:['Готов к наблюдению','Ready to observe'], ACTIVE:['Работает виртуально','Running virtually'],
    RUNNING:['Работает виртуально','Running virtually'], PAPER:['Виртуальный режим','Virtual mode'],
    DISABLED:['Выключен','Off'], PAUSED:['На паузе','Paused'], IDLE:['Наблюдение','Observing'],
    WAITING_DATA:['Ожидает данные','Awaiting data'], WAITING_PRICE:['Ожидает цену','Awaiting price'],
    WAIT_PRICE:['Ожидает цену','Awaiting price'], NO_DATA:['Нет свежих данных','No fresh data'],
    NO_SLOT:['Нужен третий слот','Third slot needed'], SLOT_REQUIRED:['Нужен третий слот','Third slot needed'],
    NO_BUDGET:['Нет виртуального бюджета','No virtual budget'], NO_SIGNAL:['Нет сигнала','No signal'],
    OPEN:['Виртуальная позиция открыта','Virtual position open'], OBSERVING:['Наблюдение','Observing'],
    WAITING_BUDGET:['Ожидает бюджет','Awaiting budget'], LOSS_LIMIT:['Лимит виртуального убытка','Virtual loss limit'],
    BUDGET_PAUSED:['Пауза бюджета','Budget paused'], NOT_STARTED:['Ещё не запущен','Not started'],
    UNAVAILABLE:['Рыночные данные недоступны','Market data unavailable'], PARTIAL_DATA:['Данные получены частично','Partial market data'],
    OK:['Данные обновлены','Data updated'],
    WAITING:['Ожидание','Waiting'], ERROR:['Ошибка данных','Data error'], UNKNOWN:['Статус не подтверждён','Status unconfirmed']
  };
  const reasonNames = {
    AI_SLOT_NOT_SELECTED:['Выберите ИИ в третьем слоте кошельков.','Select AI in the third wallet slot.'],
    SLOT_NOT_SELECTED:['Выберите ИИ в третьем слоте кошельков.','Select AI in the third wallet slot.'],
    AI_SLOT_UNAVAILABLE:['Откройте «Кошельки» и выберите ИИ в третьем слоте.','Open Wallets and select AI in the third slot.'],
    ACCOUNT_MISSING:['Подключите свой Hyperliquid-аккаунт в настройках.','Connect your Hyperliquid account in Settings.'],
    TOO_MANY_WALLETS:['Все слоты заняты кошельками. В разделе «Кошельки» нужен третий слот для ИИ.','All slots are occupied by wallets. A third slot for AI is needed in Wallets.'],
    DISABLED:['Виртуальный трейдер выключен.','The virtual trader is off.'],
    MODE_DISABLED:['Этот режим выключен.','This mode is off.'],
    BUDGET_UNAVAILABLE:['Бюджет пока не подтверждён.','The budget is not confirmed yet.'],
    INSUFFICIENT_BUDGET:['Недостаточно выделенного бюджета.','The allocated budget is insufficient.'],
    MINIMUM_NOTIONAL:['Расчётный объём ниже минимума рынка.','Calculated notional is below the market minimum.'],
    BELOW_MINIMUM:['Расчётный объём ниже минимума рынка.','Calculated notional is below the market minimum.'],
    NO_SIGNAL:['Условия виртуального входа пока не выполнены.','Virtual entry conditions are not met yet.'],
    NO_CLOSED_CANDLES:['Ожидаются закрытые свечи.','Awaiting closed candles.'],
    AWAITING_CANDLES:['Ожидаются закрытые свечи.','Awaiting closed candles.'],
    MARKET_DATA_UNAVAILABLE:['Нет подтверждённых свежих рыночных данных.','Verified fresh market data is unavailable.'],
    PUBLIC_DATA_UNAVAILABLE:['Публичные рыночные данные недоступны. Старые результаты не означают работу сейчас.','Public market data is unavailable. Previous results do not imply the mode is running now.'],
    UNVALIDATED_MODEL:['Модель не прошла проверку для реального исполнения.','The model is not validated for real execution.'],
    PAPER_ONLY:['Только виртуальные сделки. Реальные деньги не используются.','Virtual trades only. No real funds are used.']
  };
  Object.assign(reasonNames, {
    RULES_PAPER_ONLY:['Виртуальная стратегия по правилам, не обученная модель.','Rule-based virtual strategy, not a trained model.'],
    NO_MARKET_DATA:['Нет подтверждённых свежих рыночных данных.','Verified fresh market data is unavailable.'],
    NO_ALLOCATED_BUDGET:['Нет выделенного виртуального бюджета.','No virtual budget is allocated.'],
    SLOT_LOSS_LIMIT:['Достигнут виртуальный лимит убытка доли.','The virtual share loss limit was reached.'],
    DAILY_LOSS_LIMIT:['Достигнут виртуальный дневной лимит убытка.','The virtual daily loss limit was reached.'],
    RESERVED_EXCEEDS_BUDGET:['Виртуальная занятая маржа превышает текущий бюджет. Новые входы на паузе.','Virtual reserved margin exceeds the current budget. New entries are paused.'],
    NOT_STARTED:['Виртуальное наблюдение ещё не запущено.','Virtual observation has not started yet.']
  });
  const key = value => typeof value === 'string' ? value.toUpperCase() : '';
  const label = (map, value, fallback) => t(...(Object.hasOwn(map, key(value)) ? map[key(value)] : fallback));
  const status = value => label(statusNames, value, ['Статус не подтверждён','Status unconfirmed']);
  const reason = value => label(reasonNames, value, ['Подробности состояния пока недоступны.','Status details are not available yet.']);
  let latest = null, container = null, callbacks = null, busy = false, failure = null;

  function enableBlocked(mode, data) {
    if (mode !== 'trader' || data.enabled === true) return false;
    const reasons = [data.blocked_reason, obj(data.paper).reason, obj(data.last_tick).reason].map(key);
    return data.slot_selected !== true || reasons.some(value => ['ACCOUNT_MISSING', 'TOO_MANY_WALLETS', 'AI_SLOT_UNAVAILABLE'].includes(value));
  }

  function switchButton(mode, data) {
    const known = typeof data.enabled === 'boolean';
    const title = mode === 'trader' ? t('Виртуальный трейдер','Virtual trader') : t('Помощник позиций','Position assistant');
    return `<button class="aiModeSwitch" type="button" role="switch" aria-label="${title}" aria-checked="${known && data.enabled}" data-ai-mode="${mode}" ${busy || failure || !known || enableBlocked(mode, data) ? 'disabled' : ''}>` +
      `<span aria-hidden="true" class="aiModeSwitchTrack"><i></i></span><b>${!known ? '—' : data.enabled ? t('Вкл.','On') : t('Выкл.','Off')}</b></button>`;
  }

  function positions(rows) {
    if (!Array.isArray(rows) || !rows.length) return `<p class="muted">${t('Виртуальных позиций пока нет.','No virtual positions yet.')}</p>`;
    return rows.slice(0, 30).map(raw => {
      const row = obj(raw), pnl = row.unrealized_pnl_usdc;
      const side = row.side === 'LONG' ? t('ЛОНГ','LONG') : row.side === 'SHORT' ? t('ШОРТ','SHORT') : '—';
      return `<article class="aiPaperPosition"><div class="aiPaperPositionHead"><b>${esc(row.coin || '—')}</b><span>${side} · ${num(row.leverage)}×</span></div>` +
        `<div class="aiModeMetrics"><span>${t('Маржа · виртуальная','Margin · virtual')}<b>${money(row.margin_usdc)}</b></span>` +
        `<span>${t('Объём','Notional')}<b>${money(row.notional_usdc)}</b></span>` +
        `<span>${t('Вход / цена','Entry / price')}<b>${num(row.entry_price)} / ${num(row.mark_price)}</b></span>` +
        `<span>${t('Результат','PnL')}<b class="${finite(pnl) ? pnl >= 0 ? 'pnl' : 'loss' : ''}">${money(pnl)} · ${num(row.roe_pct)}%</b></span></div></article>`;
    }).join('');
  }

  function history(rows) {
    if (!Array.isArray(rows) || !rows.length) return `<p class="muted">${t('Виртуальных событий пока нет.','No virtual events yet.')}</p>`;
    const names = {OPEN:['Виртуальный вход','Virtual entry'], CLOSE:['Виртуальное закрытие','Virtual close'],
      REDUCE:['Виртуальное сокращение','Virtual reduction'], ADD:['Виртуальный добор','Virtual addition'], AVERAGE:['Виртуальный добор','Virtual addition'],
      STOP:['Виртуальный лимит убытка','Virtual loss limit'], TAKE_PROFIT:['Виртуальная цель','Virtual target'],
      SKIP:['Вход пропущен','Entry skipped'], ERROR:['Ошибка данных','Data error']};
    return rows.slice(0, 30).map(raw => {
      const row = obj(raw);
      return `<div class="aiPaperEvent"><b>${label(names, row.action || row.status, ['Виртуальное событие','Virtual event'])} · ${esc(row.coin || '—')}</b>` +
        `<small>${when(row.time_ms ?? row.created_ms)}${finite(row.pnl_usdc) ? ` · ${money(row.pnl_usdc)}` : ''}</small></div>`;
    }).join('');
  }

  function render(data) {
    if (data !== latest) failure = null;
    latest = data;
    const modes = obj(obj(data).modes), trader = obj(modes.trader), rescue = obj(modes.rescue);
    const paper = obj(trader.paper), limits = obj(trader.limits);
    const tick = obj(trader.last_tick), tickProblem = ['UNAVAILABLE', 'PARTIAL_DATA', 'PAUSED'].includes(key(tick.status));
    const prerequisiteReason = [trader.blocked_reason, paper.reason, tick.reason].find(value =>
      ['ACCOUNT_MISSING', 'TOO_MANY_WALLETS', 'AI_SLOT_UNAVAILABLE'].includes(key(value)));
    // A fresh reader failure must not be hidden by the last successful paper run.
    const overridePaper = tickProblem && (!finite(paper.last_tick_ms) || !finite(tick.asof_ms) || tick.asof_ms >= paper.last_tick_ms);
    const universe = [...new Set((Array.isArray(trader.universe) ? trader.universe : []).filter(coin => ['BTC', 'ETH'].includes(coin)))];
    const enabled = trader.enabled === true;
    const unknown = !Object.hasOwn(modes, 'trader') || !Object.hasOwn(modes, 'rescue');
    return `<div class="aiModes" data-self-localized="true">` +
      `<p class="aiModeIntro muted">${t('Два независимых режима: могут работать одновременно.','Two independent modes: both can run at the same time.')}</p>` +
      (unknown ? `<p class="loss" role="status">${t('Настройки режимов ещё не загружены.','Mode settings have not loaded yet.')}</p>` : '') +
      `<section class="card aiModeCard" aria-labelledby="aiTraderTitle"><div class="aiModeHeading"><div><h3 id="aiTraderTitle">${t('Трейдер','Trader')}</h3>` +
      `<span class="aiModeBadge">${t('ТОЛЬКО ВИРТУАЛЬНО','VIRTUAL ONLY')}</span></div>${switchButton('trader', trader)}</div>` +
      `<p>${t('Самостоятельно проверяет виртуальные входы и выходы. Реальные ордера не отправляет.','Independently tests virtual entries and exits. Does not send real orders.')}</p>` +
      `<p class="aiModeState"><b>${enabled ? status(overridePaper ? tick.status : paper.status) : trader.enabled === false ? t('Выключен','Off') : t('Статус неизвестен','Unknown status')}</b>` +
      `<small>${trader.slot_selected === true ? t('Выделен слот 3 · фиксированная 1/3','Slot 3 allocated · fixed 1/3') : t('Слот 3 не выбран для ИИ','Slot 3 is not assigned to AI')}</small></p>` +
      (trader.slot_selected !== true ? `<p class="muted">${reason('ai_slot_unavailable')}</p>` : '') +
      ((prerequisiteReason || trader.blocked_reason || (overridePaper && tick.reason) || paper.reason) ? `<p class="muted">${reason(prerequisiteReason || trader.blocked_reason || (overridePaper && tick.reason) || paper.reason)}</p>` : '') +
      `<p class="muted">${t('Рынки виртуального трейдера','Virtual trader markets')}: <b>${universe.join(' · ') || '—'}</b><br>` +
      `${t('Последняя проверка данных','Latest data check')}: ${status(tick.status)} · ${when(tick.asof_ms)}</p>` +
      `<details class="aiModeHistory"><summary>${t('Стратегия и модель издержек','Strategy and cost model')}</summary>` +
      `<p class="muted">${t('Стратегия по правилам на закрытых 15м свечах: EMA, MACD и RSI. Не обученная модель. Акции пока не входят в виртуальный трейдер.', 'Rule-based strategy on closed 15m candles: EMA, MACD and RSI. Not a trained model. Stocks are not part of the virtual trader yet.')}</p>` +
      `<p class="muted">${t('Условные издержки на каждую сторону: комиссия 0,05% + проскальзывание 0,05%. Фандинг не учтён. Виртуальное исполнение не гарантирует такой же результат на бирже.', 'Assumed costs on each side: 0.05% fee + 0.05% slippage. Funding is excluded. Virtual execution does not guarantee the same result on the exchange.')}</p></details>` +
      `<div class="aiModeMetrics"><span>${t('На виртуальный вход','Per virtual entry')}<b>${num(limits.entry_pct)}% ${t('своей доли','of own share')}</b></span>` +
      `<span>${t('Макс. плечо · с лимитом биржи','Max leverage · exchange capped')}<b>${num(limits.max_leverage)}×</b></span>` +
      `<span>${t('Виртуальный предел убытка','Virtual loss limit')}<b>${num(limits.max_loss_pct)}%</b></span>` +
      `<span>${t('Бюджет · виртуальный','Budget · virtual')}<b>${money(paper.budget_usdc)}</b></span>` +
      `<span>${t('Капитал · виртуальный','Equity · virtual')}<b>${money(paper.equity_usdc)}</b></span>` +
      `<span>${t('Закрытый результат','Realized PnL')}<b>${money(paper.realized_pnl_usdc)}</b></span></div>` +
      `<p class="muted">${t('Общий бюджет не перераспределяется. Эти суммы и результаты виртуальные, не движение средств на бирже.','The shared budget is not reallocated. These amounts and results are virtual, not exchange fund movements.')}</p>` +
      `<h4>${t('Виртуальные позиции','Virtual positions')}</h4>${positions(paper.positions)}` +
      `<details class="aiModeHistory"><summary>${t('История виртуальных действий','Virtual action history')}</summary>${history(paper.events)}</details></section>` +
      `<section class="card aiModeCard aiRescueCard" aria-labelledby="aiRescueTitle"><div class="aiModeHeading"><div><h3 id="aiRescueTitle">${t('Помощник позиций','Position assistant')}</h3>` +
      `<span class="aiModeBadge">${t('АНАЛИЗ · С ПОДТВЕРЖДЕНИЕМ','REVIEW · CONFIRMATION REQUIRED')}</span></div>${switchButton('rescue', rescue)}</div>` +
      `<p>${t('Проверяет открытые позиции копирования. Выключение помощника не закрывает позиции.','Reviews open copy positions. Switching the assistant off does not close positions.')}</p>` +
      `<div class="aiModeMetrics"><span>${t('Порог отдельной позиции','Per-position trigger')}<b>ROE ${num(rescue.trigger_roe_pct)}%</b></span>` +
      `<span>${t('Доп. бюджет кошелька','Extra source budget')}<b>${finite(rescue.max_extra_slot_fraction) ? num(rescue.max_extra_slot_fraction * 100) : '—'}%</b></span>` +
      `<span>${t('Макс. доборов','Max additions')}<b>${num(rescue.max_additions)}</b></span>` +
      `<span>${t('Автоисполнение','Automatic execution')}<b>${t('Выключено','Off')}</b></span></div>` +
      `<p class="muted">${t('До 50% доли и 4 добора суммарно на исходный кошелёк: лимит общий для всех его позиций. Средства других кошельков не используются.','Up to 50% of the share and 4 additions in total per source wallet: all its positions share the limit. Other wallets’ funds are not used.')}</p>` +
      `<p class="aiModeWarning">${t('Вероятность успеха 60% не доказана. Конкретные изменения — только в карточке «Позиции · решения с ИИ», после проверки риска и вашего подтверждения. Этот переключатель не разрешает автоматические ордера.','A 60% success probability is not established. Specific changes require the Positions · decisions with AI card, a risk review and your confirmation. This switch does not permit automatic orders.')}</p></section>` +
      (failure ? `<p class="loss" role="alert">${failure === 401 || failure === 403 ? t('Откройте приложение заново через Telegram.','Reopen the app through Telegram.') : t('Изменение не подтверждено. Обновите страницу перед повторной попыткой.','Change not confirmed. Refresh before trying again.')}</p>` : '') + '</div>';
  }

  function paint() {
    if (!container || container.isConnected === false) return;
    const data = latest;
    container.innerHTML = render(data);
    attach();
  }
  function attach() {
    container?.querySelectorAll('[data-ai-mode]').forEach(button => button.addEventListener('click', async () => {
      const mode = button.dataset.aiMode;
      const current = obj(obj(obj(latest).modes)[mode]);
      if (busy || failure || !['trader', 'rescue'].includes(mode) || typeof current.enabled !== 'boolean' || enableBlocked(mode, current) || typeof callbacks?.api !== 'function') return;
      busy = true; failure = null; paint();
      let timeout;
      try {
        // Only changes a mode preference. Never an exchange/decision endpoint.
        const controller = new AbortController();
        const deadline = new Promise((resolve, reject) => {
          timeout = setTimeout(() => {
            controller.abort();
            reject(new Error('AI_MODE_CHANGE_UNCONFIRMED'));
          }, 15000);
        });
        // A local timeout cannot prove whether the server applied a preference.
        // Never retry this POST. A fresh GET must reconcile before another click.
        await Promise.race([callbacks.api('/api/ai/modes', {mode, enabled: !current.enabled}, controller.signal), deadline]);
        clearTimeout(timeout);
        if (typeof callbacks.reload === 'function') await callbacks.reload();
      } catch (error) {
        failure = Number(error?.status) || 1;
      } finally { clearTimeout(timeout); busy = false; paint(); }
    }));
  }
  function bind(target, options) {
    container = target;
    callbacks = options;
    attach();
  }
  window.whAiModes = {render, bind};
  window.addEventListener('whlanguage', paint);
  window.addEventListener('whprivacy', paint);
})();
