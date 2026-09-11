/* Legacy explainable research history, not a trained profit-prediction model.
 * Separate frozen position-action forms own all explicit user decisions.
 * These historical cards never send a trade decision or invent a probability.
 */
(() => {
  if (window.walletHunterAiReview) return;
  const en = () => window.walletHunterLanguage === 'en';
  const text = (ru, eng) => en() ? eng : ru;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const finite = value => ['number', 'string'].includes(typeof value) && value !== '' && Number.isFinite(Number(value));
  const number = value => finite(value) ? Number(value).toLocaleString(en() ? 'en-GB' : 'ru-RU', {maximumSignificantDigits: 6}) : '—';
  const date = value => finite(value) && Number(value) > 0 && Number.isFinite(new Date(Number(value)).getTime())
    ? new Date(Number(value)).toLocaleString(en() ? 'en-GB' : 'ru-RU') : '—';
  let generation = 0;
  let controller = null;
  let data = null;
  let state = 'idle';
  let problem = null;
  let mutation = false;

  const root = () => document.getElementById('ai');
  const visible = () => Boolean(root()?.classList.contains('active'));
  function errorMessage(error) {
    if (error.status === 401 || error.status === 403) return text('Откройте приложение заново через Telegram.', 'Reopen the app through Telegram.');
    if (error.status === 409) return text('Действие недоступно: состояние изменилось или исполнение ещё не сверено. Обновите анализ.', 'Action unavailable: the state changed or execution is not reconciled yet. Refresh the review.');
    return text('Не удалось получить данные. Попробуйте ещё раз.', 'Could not load data. Please try again.');
  }
  async function api(url, body, signal) {
    const response = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST', signal, cache: 'no-store',
      headers: {'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || '', 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    if (!response.ok) { const error = new Error('AI_REQUEST_FAILED'); error.status = response.status; throw error; }
    const payload = await response.json();
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('AI_INVALID_RESPONSE');
    return payload;
  }

  function statusLabel(status) {
    const labels = {
      INFORMATION: ['Наблюдение · без сделки', 'Observation · no trade'],
      PENDING: ['Ожидает проверки', 'Awaiting verification'],
      DECLINED: ['Отклонено', 'Declined'], EXPIRED: ['Срок истёк', 'Expired'],
      EXECUTING: ['Исполнение проверяется', 'Execution being checked'],
      EXECUTED: ['Исполнено', 'Executed'], UNKNOWN: ['Результат не подтверждён', 'Result unconfirmed'],
      PARTIAL: ['Требуется сверка исполнения', 'Execution reconciliation required'],
      INVALIDATED: ['Предложение недействительно', 'Proposal invalidated']
    };
    return text(...(Object.hasOwn(labels, status) ? labels[status] : ['Статус неизвестен', 'Unknown status']));
  }

  const count = value => finite(value) && !['boolean', 'object'].includes(typeof value) && Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
  function researchCard(research) {
    research = object(research);
    const studies = count(research.studies);
    const knownGroups = {
      WAITING_START: ['Ожидают старта', 'Awaiting start'], WAITING_DATA: ['Ожидают данные', 'Awaiting data'],
      OBSERVING: ['Наблюдение', 'Observing'], COMPLETE: ['Завершены', 'Complete'],
      UNAVAILABLE: ['Недостаточно данных', 'Data unavailable']
    };
    const grouped = new Map();
    (Array.isArray(research.groups) ? research.groups : []).forEach(group => {
      group = object(group);
      const n = count(group.n), key = Object.hasOwn(knownGroups, group.status) ? group.status : 'OTHER';
      if (n !== null) grouped.set(key, (grouped.get(key) || 0) + n);
    });
    const groups = [...grouped].map(([status, n]) => `<span>${text(...(knownGroups[status] || ['Прочее', 'Other']))}<b>${number(n)}</b></span>`).join('');
    const outcomes = object(object(research.outcomes).status_counts);
    const outcomeNames = {
      TARGET: ['Цель', 'Target'], LOSS: ['Лимит убытка', 'Loss barrier'], LIQUIDATION: ['Ликвидация', 'Liquidation'],
      HORIZON: ['Срок истёк', 'Time limit'], AMBIGUOUS: ['Неоднозначно', 'Ambiguous']
    };
    const outcomeRows = Object.keys(outcomeNames).filter(key => count(outcomes[key]) !== null).map(key =>
      `<span>${text(...outcomeNames[key])}<b>${number(outcomes[key])}</b></span>`).join('');
    return `<div class="card"><h3>${text('Виртуальное исследование', 'Virtual research')}</h3>` +
      `<div class="metrics"><span>${text('Цель / предел ROE', 'ROE target / barrier')}<b>+3% / −120%</b></span>` +
      `<span>${text('Горизонт', 'Time horizon')}<b>${text('24ч', '24h')}</b></span></div>` +
      `<p class="muted">${text('Доп. средства: до 50% доли кошелька, до 4 пополнений суммарно. Все его позиции делят этот лимит.', 'Extra capital: up to 50% of the source share, up to 4 injections in total. All positions from that source share this limit.')}</p>` +
      `<p><b>${text('Исследований', 'Studies')}: ${studies === null ? '—' : number(studies)}</b></p>` +
      (groups ? `<div class="metrics">${groups}</div>` : `<small>${text('Статистика исследования пока недоступна.', 'Research statistics are not available yet.')}</small>`) +
      `<details><summary>${text('Что означают результаты', 'What the results mean')}</summary>` +
      (outcomeRows ? `<div class="metrics">${outcomeRows}</div>` : '') +
      `<p class="muted">${research.method === 'delayed-next-15m-open-HOLD-v1' ? text('Пока проверяется сценарий без изменения позиции: старт с открытия следующей 15м свечи. Интервал ожидания не учитывается.', 'Currently studying an unchanged position, starting at the next 15m candle open. The waiting interval is excluded.') : text('Это виртуальные наблюдения, не результаты исполненных ордеров.', 'These are virtual observations, not the results of executed orders.')}</p>` +
      `<p class="muted">${text('Это условия модели, не биржевые стопы. Ликвидация может наступить раньше. Счётчик успешных исходов не означает вероятность успеха 60%.', 'These are modelling conditions, not exchange stop orders. Liquidation may occur sooner. Successful-outcome counts do not imply a 60% success probability.')}</p></details></div>`;
  }

  function candidateCards(candidates) {
    if (!Array.isArray(candidates) || !candidates.length) return '';
    const labels = {HOLD: ['Без изменений', 'Hold unchanged'], REDUCE: ['Сократить', 'Reduce'],
      AVERAGE: ['Усреднить', 'Average'], ADD_MARGIN: ['Добавить маржу', 'Add margin'], LOWER_LEVERAGE: ['Снизить плечо', 'Lower leverage']};
    const rows = candidates.slice(0, 30).map(candidate => {
      candidate = object(candidate);
      const known = Object.hasOwn(labels, candidate.action);
      let label = text(...(known ? labels[candidate.action] : ['Неизвестный сценарий', 'Unknown scenario']));
      const variant = candidate.variant;
      if (candidate.action === 'REDUCE' && variant === '25pct') label += ' · 25%';
      if (candidate.action === 'LOWER_LEVERAGE' && count(variant) !== null && Number(variant) >= 1 && Number(variant) <= 100) label += ` · ${number(variant)}×`;
      if (['AVERAGE', 'ADD_MARGIN'].includes(candidate.action) && finite(variant) && typeof variant !== 'boolean' && Number(variant) > 0 && Number(variant) <= 1) {
        label += ` · ${number(Number(variant) * 100)}% ` + text('доступного резерва', 'of available reserve');
      }
      return `<p><b>${escape(label)}</b><br><small>${known && candidate.available_for_research === true ? text('Доступен для виртуальной проверки', 'Available for virtual evaluation') : text('Для проверки недостаточно данных', 'Insufficient data for evaluation')}</small></p>`;
    });
    return `<details><summary>${text('Варианты для исследования', 'Research alternatives')} · ${candidates.length}</summary>` + rows.join('') +
      `<small>${text('Это архивный исследовательский расчёт, не разрешение отправить ордер. Подтверждаемые действия находятся в отдельной карточке позиций.', 'This is an archived research calculation, not permission to submit an order. Confirmable actions are in the separate position card.')}</small></details>`;
  }
  function card(row) {
    row = object(row);
    const payload = object(row.payload), factors = object(payload.factors), position = object(payload.position);
    const trend = object(factors.trend_ema20_50), levels = object(factors.levels20);
    const side = position.side === 'LONG' ? text('Лонг', 'Long') : position.side === 'SHORT' ? text('Шорт', 'Short') : '—';
    const factorRows = [
      ['EMA20 / EMA50', `${number(trend.ema20)} / ${number(trend.ema50)}`],
      ['RSI14', number(factors.rsi14)], ['MACD · '+text('гистограмма', 'histogram'), number(factors.macd_hist)],
      ['ATR14', `${number(factors.atr14_pct)}%`],
      [text('Относительный объём', 'Relative volume'), number(factors.volume_ratio20)],
      [text('Поддержка / сопротивление', 'Support / resistance'), `${number(levels.support)} / ${number(levels.resistance)}`],
      [text('Фандинг · б.п./ч', 'Funding · bps/h'), number(factors.funding_bps_hour)],
      [text('Открытый интерес · снимок', 'Open interest · snapshot'), number(factors.open_interest)]
    ];
    return `<article class="card aiSignal"><h3>${escape(position.coin || '—')} · ${side}</h3>` +
      `<p class="${finite(payload.roe) && Number(payload.roe) < 0 ? 'loss' : 'muted'}">ROE ${number(payload.roe)}%</p>` +
      `<p>${escape(statusLabel(row.status))}</p>` +
      `<p>${text('Вероятность успеха', 'Success probability')}: <b>—</b></p>` +
      `<p class="muted">${text('Архивная проверка: вероятность 60% не подтверждена, этот расчёт не исполняется. Конкретные изменения позиции доступны только в отдельной карточке с вашим подтверждением и явным риском.', 'Archived review: a 60% probability is unverified, and this calculation is not executed. Specific position changes require the separate confirmation card with explicit risks.')}</p>` +
      candidateCards(payload.candidates) +
      `<details><summary>${text('8 факторов · показать анализ', '8 factors · show analysis')}</summary><dl>` +
      factorRows.map(([name, value], index) => `<dt>${index + 1}. ${escape(name)}</dt><dd>${escape(value)}</dd>`).join('') +
      `</dl><p class="muted">${text('Закрытые свечи 15м. Открытый интерес — снимок, не тренд. Это индикаторы, не гарантия восстановления позиции.', 'Closed 15m candles. Open interest is a snapshot, not a trend. These are indicators, not a guarantee of recovery.')}</p>` +
      `<small>${text('Последняя закрытая свеча', 'Last closed candle')}: ${date(factors.candle_close_ms)}</small></details>` +
      `<small>${text('Проверка', 'Review')}: ${date(finite(row.created) ? Number(row.created) * 1000 : null)}</small></article>`;
  }

  function render() {
    const target = root();
    if (!target || !visible()) return;
    target.dataset.selfLocalized = 'true';
    target.setAttribute('aria-busy', String(state === 'loading'));
    const title = document.getElementById('title');
    if (title) title.textContent = text('AI‑ассистент', 'AI assistant');
    const intro = `<div class="card aiHero"><h2>${text('ИИ · два режима', 'AI · two modes')}</h2>` +
      `<p>${text('Виртуальный трейдер и помощник позиций работают независимо.', 'The virtual trader and position assistant operate independently.')}</p>` +
      `<small>${text('Порог: ROE ≤−40% отдельной позиции для помощника.', 'Trigger: ROE ≤−40% of an individual position for the assistant.')}</small>` +
      `<p class="muted">${text('Правила виртуального трейдера и отдельное экспериментальное обучение. Автоматической реальной торговли нет. Отдельный реальный ордер — только после вашего подтверждения.', 'Virtual trader rules and separate experimental learning. Real autonomous AI trading is not enabled. A separate real order requires your confirmation.')}</p>` +
      `<button type="button" id="aiRefresh" ${state === 'loading' || mutation ? 'disabled' : ''}>${state === 'loading' ? text('Загрузка…', 'Loading…') : text('Обновить', 'Refresh')}</button></div>`;
    const error = problem ? `<p class="loss" role="alert">${escape(errorMessage(problem))}</p>` : '';
    let body = '';
    if (data) {
      body += window.whAiPositionActions ? `<div id="aiPositionActions">${window.whAiPositionActions.render(data.position_actions)}</div>` : '';
      body += window.whAiModes ? `<div id="aiModes">${window.whAiModes.render(data)}</div>` :
        `<p class="loss">${text('Панель режимов недоступна. Откройте приложение заново.', 'Mode controls unavailable. Reopen the app.')}</p>`;
      body += window.whAiUserOrders ? `<div id="aiUserOrders">${window.whAiUserOrders.render(data.user_orders)}</div>` : '';
      body += window.whAiLearning ? window.whAiLearning.render(data.learning) :
        `<p class="muted">${text('Панель обучения пока недоступна. Откройте приложение заново.', 'The model-learning panel is unavailable. Reopen the app.')}</p>`;
      body += researchCard(data.research);
      const holds = Object.keys(object(data.holds));
      if (holds.length) body += `<h3>${text('Пауза по инструментам', 'Market holds')}</h3>` + holds.map(key =>
        `<div class="card"><b>${escape(key)}</b><p>${text('Изменения копирования приостановлены. Возобновление может изменить размер позиции и плечо.', 'Copying changes are on hold. Resuming may change the position size and leverage.')}</p>` +
        `<button type="button" data-resume="${escape(key)}" ${mutation ? 'disabled' : ''}>${text('Вернуть копирование', 'Resume copying')}</button></div>`).join('');
      const reviews = Array.isArray(data.reviews) ? data.reviews : [];
      body += `<h3>${text('История проверок', 'Review history')}</h3>` + (reviews.map(card).join('') ||
        `<p class="muted">${text('Проверок пока нет. Это не подтверждает отсутствие риска или открытых позиций.', 'No reviews yet. This does not confirm the absence of risk or open positions.')}</p>`);
    }
    target.innerHTML = intro + error + body;
    if(data&&window.whAiPositionActions)window.whAiPositionActions.bind(target.querySelector('#aiPositionActions'),{reload:async()=>{if(visible())await open()},api});
    if(data&&window.whAiModes)window.whAiModes.bind(target.querySelector('#aiModes'),{reload:async()=>{if(visible())await open()},api});
    if(data&&window.whAiUserOrders)window.whAiUserOrders.bind(target.querySelector('#aiUserOrders'),{reload:async()=>{if(visible())await open()},api});
    target.querySelector('#aiRefresh')?.addEventListener('click', open);
    target.querySelectorAll('[data-resume]').forEach(button => button.addEventListener('click', () => resume(button.dataset.resume)));
  }

  async function open() {
    const target = root();
    if (!target) return;
    document.querySelectorAll('.page').forEach(page => page.classList.toggle('active', page.id === 'ai'));
    document.querySelectorAll('nav button').forEach(button => button.classList.toggle('active', button.dataset.page === 'ai'));
    const request = ++generation;
    controller?.abort();
    const activeController = new AbortController();
    controller = activeController;
    // Never retain another account's last response during a fresh fetch.
    data = null; state = 'loading'; problem = null; render();
    const timeout = setTimeout(() => activeController.abort(), 15000);
    try {
      const response = await api('/api/ai', undefined, activeController.signal);
      if (request !== generation || !visible()) return;
      data = response; state = 'ready'; render();
    } catch (error) {
      if (request !== generation || !visible()) return;
      problem = error; state = 'error'; render();
    } finally {
      clearTimeout(timeout);
      if (controller === activeController) controller = null;
    }
  }

  async function resume(market) {
    if (mutation || !visible() || !Object.hasOwn(object(data?.holds), market)) return;
    if (!confirm(text('Возобновить изменения вслед за кошельком? Размер и плечо могут измениться. Открытые позиции не закроются только от нажатия этой кнопки.', 'Resume following the source? Size and leverage may change. This button does not itself close open positions.'))) return;
    mutation = true; problem = null; render();
    try {
      // A POST is never automatically retried after an uncertain response.
      await api('/api/ai/resume-copy', {market});
      if (visible()) await open();
      else data = null;
    } catch (error) {
      problem = error;
    } finally { mutation = false; render(); }
  }

  function hide() { ++generation; controller?.abort(); controller = null; data = null; state = 'idle'; }
  window.walletHunterAiReview = {open, hide};
  window.addEventListener('whlanguage', render);
  // No own size, margin, PnL or source budget is embedded in this view. Public
  // market indicators and ROE remain visible when the privacy eye is closed.
  window.addEventListener('whprivacy', render);
})();
