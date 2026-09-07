/* Contract tests with a minimal DOM, no browser/network/trading access.
 * These exercise event/rendering safety, not CSS layout or Telegram WebView.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../webapp/static', name), 'utf8');
const unescape = value => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

class Element {
  constructor(id = '') {
    this.id = id; this.dataset = {}; this.events = {}; this.attributes = {}; this.content = ''; this.controls = []; this.isConnected = true;
    const classes = new Set();
    this.classList = {contains: name => classes.has(name), toggle: (name, value) => value ? classes.add(name) : classes.delete(name)};
  }
  set textContent(value) { this.controls.forEach(control => { control.isConnected = false; }); this.content = String(value); this.controls = []; }
  get textContent() { return this.content; }
  set innerHTML(value) {
    this.controls.forEach(control => { control.isConnected = false; });
    this.content = String(value); this.controls = [];
    if (this.content.includes('id="aiRefresh"')) this.controls.push(new Element('aiRefresh'));
    if (this.content.includes('id="aiSlotToggle"')) this.controls.push(new Element('aiSlotToggle'));
    if (this.id !== 'aiModes' && this.content.includes('id="aiModes"')) {
      const child = new Element('aiModes'); child.innerHTML = this.content; this.controls.push(child);
    }
    for (const match of this.content.matchAll(/<button[^>]+data-ai-mode="([^"]*)"[^>]*>/g)) {
      const button = new Element(); button.dataset.aiMode = match[1];
      button.disabled = match[0].includes('disabled'); this.controls.push(button);
    }
    for (const match of this.content.matchAll(/data-resume="([^"]*)"/g)) {
      const button = new Element(); button.dataset.resume = unescape(match[1]); this.controls.push(button);
    }
  }
  get innerHTML() { return this.content; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    if (selector === '.card') return this.cards || [];
    if (selector === '.card p.muted') return this.description ? [this.description] : [];
    if (selector === 'button') return this.controls.filter(control => control.id !== 'newWallet');
    if (selector.startsWith('#')) return this.controls.filter(control => control.id === selector.slice(1));
    if (selector === '[data-resume]') return this.controls.filter(control => Object.hasOwn(control.dataset, 'resume'));
    if (selector === '[data-ai-mode]') return this.controls.filter(control => Object.hasOwn(control.dataset, 'aiMode'));
    return [];
  }
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  async click() { if (this.disabled) return; await this.onclick?.(); for (const callback of this.events.click || []) await callback(); }
  remove() { this.removed = true; }
}

function fixture() {
  const pages = ['home', 'wallets', 'analysis', 'chart', 'ai', 'settings'].map(id => new Element(id));
  const title = new Element('title');
  const buttons = pages.map(page => { const button = new Element(); button.dataset.page = page.id; return button; });
  const events = {}, requests = [], scripts = [], timers = new Map();
  let timerId = 0, decision = false;
  const document = {
    getElementById: id => id === 'title' ? title : pages.find(page => page.id === id),
    querySelectorAll: selector => selector === '.page' ? pages : selector === 'nav button' ? buttons : [],
    querySelector: selector => buttons.find(button => selector === `nav button[data-page="${button.dataset.page}"]`),
    createElement: () => new Element(), body: {appendChild: script => scripts.push(script)}
  };
  const context = {
    document, AbortController, console, Date, Number, String, Object, JSON, Promise,
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id),
    confirm: () => decision,
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    walletHunterLanguage: 'ru', walletHunterPrivacy: false,
    Telegram: {WebApp: {initData: 'TEST-INIT-DATA'}},
    addEventListener: (name, callback) => (events[name] ||= []).push(callback)
  };
  context.window = context;
  vm.createContext(context);
  pages[0].classList.toggle('active', true);
  buttons.forEach(button => button.onclick = () => {
    pages.forEach(page => page.classList.toggle('active', page.id === button.dataset.page));
    title.textContent = button.dataset.page;
  });
  return {
    context, pages, title, requests, scripts, timers, root: pages.find(page => page.id === 'ai'),
    run: name => vm.runInContext(read(name), context, {filename: name}),
    event: name => (events[name] || []).forEach(callback => callback()),
    button: page => buttons.find(button => button.dataset.page === page),
    allow: value => { decision = value; },
    resolve: (index, payload, status = 200) => requests[index].resolve({ok: status < 400, status, json: async () => payload})
  };
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const row = (coin = 'BTC') => ({created: 1788714000, status: 'INFORMATION', payload: {
  position: {coin, side: 'SHORT', size: 123456789, margin_used: 91234567}, roe: -50.25,
  notional_usdc: 87654321, extra_margin_usdc: 76543210, remaining_size: 65432109,
  gate: {allowed: false, probability: null, source_budget_usdc: 54321098},
  factors: {trend_ema20_50: {ema20: 101, ema50: 102}, rsi14: 34, macd_hist: -2,
    atr14_pct: 3, volume_ratio20: 1.1, levels20: {support: 90, resistance: 110},
    funding_bps_hour: .12, open_interest: 9000, candle_close_ms: 1788713900000}
}});
const modeResponse = (enabled = false) => ({reviews:[row()], holds:{},
  learning:{status:'WAITING_DATA',reason:'insufficient_matured_history',model:null,
    counts:{candles:0,examples:0},collector:{status:'NOT_STARTED'}},modes:{
  trader:{enabled,execution_mode:'PAPER',slot_selected:true,blocked_reason:enabled ? null : 'disabled',
    universe:['BTC','ETH'],limits:{entry_pct:10,max_leverage:40,max_loss_pct:10},
    last_tick:{status:'OK',asof_ms:1788714000000},
    paper:{status:'ACTIVE',reason:'rules_paper_only',budget_usdc:43210.98,equity_usdc:43210.98,
      realized_pnl_usdc:0,positions:[],events:[]}},
  rescue:{enabled:true,execution_mode:'CONFIRMATION_REQUIRED',real_execution_available:false,
    trigger_roe_pct:-40,max_extra_slot_fraction:.5,max_additions:4}
}});

test('combined review and modes render bind toggle authorized POST and reload without losing reviews or privacy', async () => {
  const f = fixture(); f.run('ai-modes.js'); f.run('ai-learning.js'); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, modeResponse(false)); await opening;
  assert.match(f.root.innerHTML, /Трейдер/); assert.match(f.root.innerHTML, /Помощник позиций/);
  assert.match(f.root.innerHTML, /История проверок/);
  assert.match(f.root.innerHTML, /Обучение модели/);
  assert.ok(f.root.innerHTML.indexOf('aiModes') < f.root.innerHTML.indexOf('aiLearningCard'));
  assert.ok(f.root.innerHTML.indexOf('aiLearningCard') < f.root.innerHTML.indexOf('Виртуальное исследование'));
  const modes = f.root.querySelector('#aiModes'); assert.ok(modes);
  const trader = modes.querySelectorAll('[data-ai-mode]')[0];
  assert.equal(trader.events.click.length, 1);
  const click = trader.click(); await flush();
  assert.equal(f.requests.length, 2); assert.equal(f.requests[1].url, '/api/ai/modes');
  assert.equal(f.requests[1].options.method, 'POST');
  assert.equal(f.requests[1].options.headers['X-Telegram-Init-Data'], 'TEST-INIT-DATA');
  assert.equal(f.requests[1].options.body, '{"mode":"trader","enabled":true}');
  assert.ok(f.requests[1].options.signal); assert.equal(f.requests[1].options.signal.aborted, false);
  f.resolve(1, {ok:true}); await flush();
  assert.equal(f.requests.length, 3); assert.equal(f.requests[2].url, '/api/ai');
  assert.equal(f.requests[2].options.method, 'GET');
  f.resolve(2, modeResponse(true)); await click;
  assert.equal(modes.isConnected, false); assert.equal(f.timers.size, 0);
  assert.equal(f.root.querySelector('#aiModes').querySelectorAll('[data-ai-mode]')[0].disabled, false);
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.match(f.root.innerHTML, /Review history/); assert.match(f.root.innerHTML, /Position assistant/);
  assert.match(f.root.innerHTML, /Model learning/);
  assert.doesNotMatch(f.root.innerHTML, /[А-Яа-яЁё]/); assert.match(f.root.innerHTML, /43,210\.98/);
  f.context.walletHunterPrivacy = true; f.event('whprivacy');
  assert.doesNotMatch(f.root.innerHTML, /43,210\.98/); assert.match(f.root.innerHTML, /Amount hidden/);
  assert.equal(f.requests.length, 3);
});

test('combined mode timeout aborts fetch and late success cannot retry or override a manual refresh', async () => {
  const f = fixture(); f.run('ai-modes.js'); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open(); f.resolve(0, modeResponse(false)); await opening;
  const click = f.root.querySelector('#aiModes').querySelectorAll('[data-ai-mode]')[0].click();
  await flush();
  assert.equal(f.timers.size, 1); [...f.timers.values()][0](); await click;
  assert.equal(f.requests[1].options.signal.aborted, true); assert.equal(f.requests.length, 2);
  let modes = f.root.querySelector('#aiModes');
  assert.match(modes.innerHTML, /Изменение не подтверждено/);
  assert.equal(modes.querySelectorAll('[data-ai-mode]')[0].disabled, true);
  await modes.querySelectorAll('[data-ai-mode]')[0].click(); assert.equal(f.requests.length, 2);
  // Server might have applied the timed-out preference; only this new GET decides.
  const refresh = f.root.querySelector('#aiRefresh').click(); await flush();
  assert.equal(f.requests.length, 3); assert.equal(f.requests[2].options.method, 'GET');
  f.resolve(2, modeResponse(true)); await refresh;
  f.resolve(1, {ok:true}); await flush();
  assert.equal(f.requests.length, 3); assert.equal(f.timers.size, 0);
  modes = f.root.querySelector('#aiModes');
  assert.doesNotMatch(modes.innerHTML, /Изменение не подтверждено/);
  assert.equal(modes.querySelectorAll('[data-ai-mode]')[0].disabled, false);
  assert.match(modes.innerHTML, /aria-checked="true" data-ai-mode="trader"/);
});

test('fully bilingual render lists all eight factors without inventing probability or exposing own money', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, {reviews: [row()], holds: {}}); await opening;
  assert.match(f.root.innerHTML, /Порог: ROE ≤−40%/);
  assert.equal((f.root.innerHTML.match(/<dt>/g) || []).length, 8);
  assert.match(f.root.innerHTML, /Вероятность успеха: <b>—<\/b>/);
  for (const amount of ['123456789', '91234567', '87654321', '76543210', '65432109', '54321098']) assert.ok(!f.root.innerHTML.includes(amount));
  f.context.walletHunterPrivacy = true; f.event('whprivacy');
  assert.match(f.root.innerHTML, /ROE -50,25%/);
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.ok(!/[А-Яа-яЁё]/.test(f.root.innerHTML));
  assert.match(f.root.innerHTML, /Real autonomous AI trading is not enabled/);
  assert.match(f.root.innerHTML, /Success probability: <b>—<\/b>/);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].options.headers['X-Telegram-Init-Data'], 'TEST-INIT-DATA');
});

test('untrusted markets, statuses, errors and sparse factors cannot inject markup or crash all cards', async () => {
  const f = fixture(); f.run('ai-review.js');
  const attack = row('<img src=x onerror="alert(1)">'); attack.status = '__proto__';
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, {reviews: [attack, null, {payload: {factors: {rsi14: 'Infinity'}}}], holds: {}}); await opening;
  assert.match(f.root.innerHTML, /&lt;img/); assert.ok(!f.root.innerHTML.includes('<img'));
  assert.ok(!f.root.innerHTML.includes('NaN')); assert.ok(!f.root.innerHTML.includes('Infinity'));
  assert.equal((f.root.innerHTML.match(/<article /g) || []).length, 3);
  assert.equal(f.root.getAttribute('aria-busy'), 'false');
});

test('a stale response cannot overwrite a newer request', async () => {
  const f = fixture(); f.run('ai-review.js');
  const first = f.context.walletHunterAiReview.open(), second = f.context.walletHunterAiReview.open();
  assert.equal(f.requests[0].options.signal.aborted, true);
  f.resolve(1, {reviews: [row('NEW')], holds: {}}); await second;
  f.resolve(0, {reviews: [row('OLD')], holds: {}}); await first;
  assert.match(f.root.innerHTML, /NEW/); assert.ok(!f.root.innerHTML.includes('OLD'));
});

test('a completed request cannot navigate back after leaving AI', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.context.walletHunterAiReview.hide(); await f.button('home').click();
  f.resolve(0, {reviews: [row()], holds: {}}); await opening;
  assert.equal(f.root.classList.contains('active'), false); assert.equal(f.title.textContent, 'home');
  assert.ok(!f.root.innerHTML.includes('BTC'));
});

test('authentication error is localised, never prints server detail and offers refresh', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, {detail: '<script>private-token</script>'}, 401); await opening;
  assert.match(f.root.innerHTML, /Откройте приложение/); assert.ok(!f.root.innerHTML.includes('private-token'));
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.match(f.root.innerHTML, /Reopen the app through Telegram/); assert.ok(!/[А-Яа-яЁё]/.test(f.root.innerHTML));
  assert.ok(f.root.querySelector('#aiRefresh'));
});

test('read-only gate never exposes a trade confirmation even for a forged executable proposal', async () => {
  const f = fixture(); f.run('ai-review.js');
  const forged = row(); forged.status = 'PENDING'; forged.payload.action = 'REDUCE'; forged.payload.gate = {allowed: true, probability: .99};
  const opening = f.context.walletHunterAiReview.open(); f.resolve(0, {reviews: [forged], holds: {}}); await opening;
  assert.equal(f.root.controls.length, 1); assert.equal(f.requests.length, 1);
  assert.match(f.root.innerHTML, /этот расчёт не исполняется/);
  assert.ok(!f.root.innerHTML.includes('99%'));
});

test('resume copying requires an explicit confirmation and does not retry a conflict', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open(); f.resolve(0, {reviews: [], holds: {'BTC|': {}}}); await opening;
  await f.root.querySelectorAll('[data-resume]')[0].click(); assert.equal(f.requests.length, 1);
  f.allow(true);
  const click = f.root.querySelectorAll('[data-resume]')[0].click();
  await flush(); assert.equal(f.requests.length, 2); assert.equal(f.requests[1].options.method, 'POST');
  assert.equal(f.requests[1].options.body, '{"market":"BTC|"}');
  await f.root.querySelectorAll('[data-resume]')[0].click(); assert.equal(f.requests.length, 2);
  f.resolve(1, {detail: 'private server information'}, 409); await click;
  assert.equal(f.requests.length, 2); assert.match(f.root.innerHTML, /Действие недоступно/);
  assert.ok(!f.root.innerHTML.includes('private server information'));
});

test('missing review array is rendered as unknown history, not proof of no risk', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open(); f.resolve(0, {}); await opening;
  assert.match(f.root.innerHTML, /Это не подтверждает отсутствие риска/);
});

test('loader queues an early click, loads once and uses the module, not a legacy fallback', async () => {
  const f = fixture(); f.run('ai.js');
  const first = f.context.openAi(), second = f.context.openAi();
  assert.equal(f.scripts.length, 1); assert.equal(f.requests.length, 0);
  assert.ok(!f.root.innerHTML.includes('Обучение активно'));
  f.run('ai-review.js'); f.scripts[0].onload(); await flush();
  assert.equal(f.requests.length, 1); f.resolve(0, {reviews: [], holds: {}}); await Promise.all([first, second]);
  assert.match(f.root.innerHTML, /ИИ · два режима/);
});

test('failed script load can be retried and loader error follows language selection', async () => {
  const f = fixture(); f.run('ai.js');
  const first = f.context.openAi(); f.scripts[0].onerror(); await first;
  assert.match(f.root.textContent, /Не удалось загрузить AI/); assert.equal(f.scripts[0].removed, true);
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage'); assert.match(f.root.textContent, /AI could not load/);
  const second = f.context.openAi(); assert.equal(f.scripts.length, 2);
  f.run('ai-review.js'); f.scripts[1].onload(); await flush(); f.resolve(0, {reviews: [], holds: {}}); await second;
  assert.match(f.root.innerHTML, /AI · two modes/);
});

test('late script completion after navigation away does not fetch or steal the page', async () => {
  const f = fixture(); f.run('ai.js');
  const opening = f.context.openAi(); await f.button('wallets').click();
  f.run('ai-review.js'); f.scripts[0].onload(); await opening;
  assert.equal(f.requests.length, 0); assert.equal(f.root.classList.contains('active'), false);
  assert.equal(f.title.textContent, 'wallets');
});

test('loading the scripts twice does not duplicate navigation handlers', async () => {
  const f = fixture(); f.run('ai.js'); f.run('ai.js'); f.run('ai-review.js'); f.run('ai-review.js');
  await f.button('ai').click(); await flush(); assert.equal(f.requests.length, 1);
  f.resolve(0, {reviews: [], holds: {}}); await flush();
});

function slotFixture() {
  const f = fixture(), page = f.pages.find(page => page.id === 'wallets');
  const form = new Element(), input = new Element('newWallet'), button = new Element('addWallet');
  form.controls = [input, button];
  const slot = new Element(); slot.textContent = '⚪ Свободный слот · Кошелёк 3';
  page.cards = [slot, form]; page.description = new Element();
  f.run('ai-slot.js');
  return {...f, page, form, input, add: button, slot};
}
const wallets = count => [1, 2, 3].map(slot => ({slot, configured: slot <= count}));

test('selected AI occupies only slot three; after deleting a copy wallet another can be added', async () => {
  const f = slotFixture(); const refresh = f.button('wallets').click(); await flush();
  f.resolve(0, {wallets: wallets(1), ai_slot_selected: true}); await refresh;
  assert.equal(f.input.disabled, false); assert.equal(f.add.disabled, false);
  assert.match(f.slot.innerHTML, /AI · слот 3/); assert.match(f.slot.innerHTML, /Каждый реальный ордер требует отдельного подтверждения/);
  assert.match(f.slot.innerHTML, /Автоторговля не включается/);
  assert.match(f.page.description.textContent, /Каждому из 3 слотов выделяется 1\/3/);
  assert.ok(!f.slot.innerHTML.includes('обучение активно'));
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.ok(!/[А-Яа-яЁё]/.test(f.slot.innerHTML + f.add.textContent + f.page.description.textContent));
  assert.equal(f.requests.length, 1);
});

test('two wallets plus an AI reservation disable adding a fourth occupant', async () => {
  const f = slotFixture(); const refresh = f.button('wallets').click(); await flush();
  f.resolve(0, {wallets: wallets(2), ai_slot_selected: true}); await refresh;
  assert.equal(f.input.disabled, true); assert.equal(f.add.disabled, true);
  assert.equal(f.form.classList.contains('slotFull'), true);
  assert.match(f.add.textContent, /Третий слот занят AI/);
});

test('three copied wallets do not display an AI replacement button', async () => {
  const f = slotFixture(); const refresh = f.button('wallets').click(); await flush();
  f.resolve(0, {wallets: wallets(3), ai_slot_selected: false}); await refresh;
  assert.equal(f.add.disabled, true); assert.equal(f.slot.querySelector('#aiSlotToggle'), null);
});

test('a stale dashboard cannot incorrectly mark a freshly freed slot as full', async () => {
  const f = slotFixture(); const first = f.button('wallets').click(), second = f.button('wallets').click(); await flush();
  f.resolve(1, {wallets: wallets(1), ai_slot_selected: true}); await second;
  f.resolve(0, {wallets: wallets(2), ai_slot_selected: true}); await first;
  assert.equal(f.add.disabled, false);
});

test('research policy and status counts are bilingual, shared by source and never a trading readiness signal', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, {reviews: [], holds: {}, research: {
    method: 'delayed-next-15m-open-HOLD-v1', studies: 7, probability: .99, ready_for_live_trading: true,
    groups: [{status: 'WAITING_START', reason: 'private budget $99999', n: 2},
      {status: 'COMPLETE', reason: 'target', n: 3}, {status: 'UNAVAILABLE', reason: '<img src=x>', n: 2}],
    outcomes: {scenarios: 3, status_counts: {TARGET: 2, LOSS: 1}}
  }}); await opening;
  assert.match(f.root.innerHTML, /\+3% \/ −120%/);
  assert.match(f.root.innerHTML, /до 50% доли кошелька, до 4 пополнений суммарно/);
  assert.match(f.root.innerHTML, /Все его позиции делят этот лимит/);
  assert.match(f.root.innerHTML, /Исследований: 7/);
  assert.match(f.root.innerHTML, /Завершены<b>3<\/b>/);
  assert.ok(!f.root.innerHTML.includes('99999')); assert.ok(!f.root.innerHTML.includes('<img'));
  assert.ok(!f.root.innerHTML.includes('99%'));
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.ok(!/[А-Яа-яЁё]/.test(f.root.innerHTML));
  assert.match(f.root.innerHTML, /All positions from that source share this limit/);
  assert.match(f.root.innerHTML, /The waiting interval is excluded/);
  assert.match(f.root.innerHTML, /not exchange stop orders/);
  assert.equal(f.root.controls.length, 1);
});

test('missing or malformed research counts do not appear as an invented zero', async () => {
  const f = fixture(); f.run('ai-review.js');
  const opening = f.context.walletHunterAiReview.open();
  f.resolve(0, {reviews: [], holds: {}, research: {studies: null, groups: [{status: '__proto__', n: -1}, {status: 'COMPLETE', n: true}], outcomes: {status_counts: {TARGET: 'Infinity'}}}}); await opening;
  assert.match(f.root.innerHTML, /Исследований: —/);
  assert.ok(!f.root.innerHTML.includes('NaN')); assert.ok(!f.root.innerHTML.includes('Infinity'));
  assert.ok(!f.root.innerHTML.includes('__proto__'));
});

test('candidate alternatives reveal no private money and create no order buttons', async () => {
  const f = fixture(); f.run('ai-review.js');
  const review = row(); review.payload.candidates = [
    {action: 'HOLD', variant: 'unchanged', available_for_research: true},
    {action: 'REDUCE', variant: '25pct', available_for_research: true},
    {action: 'AVERAGE', variant: '0.25', available_for_research: true, order_notional_usdc: 88776655},
    {action: 'LOWER_LEVERAGE', variant: '5', available_for_research: false, reason: 'private balance 77665544'},
    {action: 'ADD_MARGIN', variant: '0.50', available_for_research: false, modelled_margin_change_usdc: 66554433},
    {action: '<script>bad()</script>', variant: 'private $55443322', available_for_research: true}
  ];
  const opening = f.context.walletHunterAiReview.open(); f.resolve(0, {reviews: [review], holds: {}}); await opening;
  assert.match(f.root.innerHTML, /Усреднить · 25% доступного резерва/);
  assert.match(f.root.innerHTML, /Снизить плечо · 5×/);
  assert.ok(!f.root.innerHTML.includes('<script>')); assert.ok(!f.root.innerHTML.includes('bad()'));
  for (const secret of ['88776655', '77665544', '66554433', '55443322']) assert.ok(!f.root.innerHTML.includes(secret));
  assert.equal(f.root.controls.length, 1);
  f.context.walletHunterPrivacy = true; f.event('whprivacy');
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.ok(!/[А-Яа-яЁё]/.test(f.root.innerHTML));
  assert.match(f.root.innerHTML, /archived research calculation, not permission to submit an order/);
  assert.equal(f.requests.length, 1);
});
