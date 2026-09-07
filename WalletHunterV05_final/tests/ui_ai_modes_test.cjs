/* Renderer/API contract tests; no real browser, network or trading access. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webapp/static/ai-modes.js'), 'utf8');
class Element {
  constructor() { this.isConnected = true; this.buttons = []; this.content = ''; }
  set innerHTML(value) {
    this.content = value;
    this.buttons = [...value.matchAll(/<button[^>]+data-ai-mode="([^"]*)"[^>]*>/g)].map(match => {
      const listeners = [];
      return {dataset: {aiMode: match[1]}, disabled: match[0].includes('disabled'),
        addEventListener: (type, listener) => listeners.push(listener),
        click: async function () { if (!this.disabled) for (const listener of listeners) await listener(); }};
    });
  }
  get innerHTML() { return this.content; }
  querySelectorAll() { return this.buttons; }
}
function fixture() {
  let timerId = 0;
  const timers = new Map();
  const events = {}, context = {walletHunterLanguage:'ru', walletHunterPrivacy:false, console, AbortController,
    setTimeout:(callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;},
    clearTimeout:id => timers.delete(id),
    addEventListener: (name, fn) => (events[name] ||= []).push(fn)};
  context.window = context;
  vm.createContext(context); vm.runInContext(source, context);
  return {context, timers, api:context.whAiModes, event:name => (events[name] || []).forEach(fn => fn())};
}
function payload() {
  return {modes:{trader:{enabled:true, execution_mode:'PAPER', slot_selected:true,
    universe:['BTC', 'ETH'], last_tick:{status:'OK',asof_ms:1788728400000},
    blocked_reason:null, limits:{entry_pct:10,max_leverage:40,max_loss_pct:10},
    paper:{status:'ACTIVE', reason:'rules_paper_only', budget_usdc:12345.67, equity_usdc:12350.67,
      realized_pnl_usdc:5, positions:[{coin:'BTC',side:'LONG',leverage:5,entry_price:100,mark_price:110,
        margin_usdc:87654.32,notional_usdc:98765.43,unrealized_pnl_usdc:45678.91,roe_pct:50}],
      events:[{action:'OPEN',status:'PAPER',coin:'BTC',time_ms:1788728400000,pnl_usdc:23456.78}]}},
    rescue:{enabled:true,execution_mode:'CONFIRMATION_REQUIRED',real_execution_available:false,
      trigger_roe_pct:-40,max_extra_slot_fraction:.5,max_additions:4}}};
}

test('both modes are separate, virtual trader and gated rescue are explicit in Russian', () => {
  const f = fixture(), html = f.api.render(payload());
  assert.match(html, /Трейдер/); assert.match(html, /Помощник позиций/);
  assert.match(html, /ТОЛЬКО ВИРТУАЛЬНО/); assert.match(html, /Реальные ордера не отправляет/);
  assert.match(html, /60% не доказана/); assert.match(html, /ROE -40%/);
  assert.match(html, /Виртуальный добор|Макс. доборов/);
  assert.equal((html.match(/role="switch"/g)||[]).length, 2);
  assert.equal((html.match(/aria-checked="true"/g)||[]).length, 2);
  assert.doesNotMatch(html, /data-resume|data-decision|Подтвердить сделку/);
});

test('new panels are entirely English when selected, with backend reasons mapped', () => {
  const f = fixture(); f.context.walletHunterLanguage = 'en';
  const html = f.api.render(payload());
  assert.doesNotMatch(html, /[А-Яа-яЁё]/);
  assert.match(html, /VIRTUAL ONLY/); assert.match(html, /Rule-based virtual strategy, not a trained model/);
  assert.match(html, /Automatic execution/); assert.match(html, /This switch does not permit automatic orders/);
  assert.match(html, /40×/); assert.match(html, /10% of own share/); assert.match(html, /exchange capped/);
});

test('eye privacy removes all own virtual amounts, but public prices and ROE remain', () => {
  const f = fixture(); f.context.walletHunterLanguage = 'en';
  const visible = f.api.render(payload());
  assert.match(visible, /12,345\.67/); assert.match(visible, /98,765\.43/);
  f.context.walletHunterPrivacy = true;
  const hidden = f.api.render(payload());
  for (const amount of ['12,345.67','12,350.67','87,654.32','98,765.43','45,678.91','23,456.78']) {
    assert.ok(!hidden.includes(amount), amount);
  }
  assert.match(hidden, /Amount hidden/); assert.match(hidden, /100 \/ 110/); assert.match(hidden, /50%/);
});

test('untrusted labels are escaped, unknown statuses and reasons do not leak raw payloads', () => {
  const f = fixture(), data = payload();
  data.modes.trader.paper.positions[0].coin = '<img src=x onerror="boom()">';
  data.modes.trader.paper.positions[0].side = '<script>boom()</script>';
  data.modes.trader.paper.status = '<script>privateBalance</script>';
  data.modes.trader.paper.reason = 'privateAmount-secret';
  data.modes.trader.paper.events[0].action = '<script>order()</script>';
  const html = f.api.render(data);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|<script|privateBalance|privateAmount-secret/);
  assert.match(html, /Статус не подтверждён/);
});

test('missing or malformed mode state never shows enabled actionable switches', () => {
  const f = fixture();
  for (const data of [null, {}, {modes:[]}, {modes:{trader:{enabled:'true'},rescue:{enabled:1}}}]) {
    const html = f.api.render(data);
    assert.equal((html.match(/aria-checked="true"/g)||[]).length, 0);
    assert.equal((html.match(/data-ai-mode="[^"]+" disabled/g)||[]).length, 2);
  }
});

test('independent toggle sends only mode preference, then reloads authoritative state', async () => {
  const f = fixture(), node = new Element(), data = payload(), calls = [];
  data.modes.trader.enabled = false;
  node.innerHTML = f.api.render(data);
  f.api.bind(node, {api: async (url, body) => calls.push({url, body}), reload: async () => calls.push('reload')});
  await node.buttons[0].click();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/ai/modes');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), {mode:'trader', enabled:true});
  assert.equal(calls[1], 'reload');
  assert.equal(data.modes.trader.enabled, false); // no optimistic or forged response
  assert.equal(data.modes.rescue.enabled, true);
});

test('duplicate clicks while request is pending do not send duplicate mutations', async () => {
  const f = fixture(), node = new Element(); let resolve, count = 0;
  node.innerHTML = f.api.render(payload());
  f.api.bind(node, {api: () => { count++; return new Promise(done => {resolve = done;}); }, reload: async () => {}});
  const button = node.buttons[0], pending = button.click();
  await button.click(); await node.buttons[1].click();
  assert.equal(count, 1); resolve(); await pending;
});

test('uncertain mutation is not retried or displayed as applied, error is localized', async () => {
  const f = fixture(), node = new Element(), data = payload(); let calls = 0;
  node.innerHTML = f.api.render(data);
  f.api.bind(node, {api: async () => {calls++; throw {status:503, message:'secret API body'};}, reload: async () => {throw Error('not expected');}});
  await node.buttons[1].click();
  assert.equal(calls, 1); assert.equal(data.modes.rescue.enabled, true);
  assert.match(node.innerHTML, /Изменение не подтверждено/); assert.doesNotMatch(node.innerHTML, /secret API body/);
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.match(node.innerHTML, /Change not confirmed/); assert.doesNotMatch(node.innerHTML, /[А-Яа-яЁё]/);
});

test('mounted panel updates on language and privacy events without touching other renderers', () => {
  const f = fixture(), node = new Element();
  node.innerHTML = f.api.render(payload()); f.api.bind(node, {});
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.match(node.innerHTML, /Position assistant/); assert.doesNotMatch(node.innerHTML, /[А-Яа-яЁё]/);
  f.context.walletHunterPrivacy = true; f.event('whprivacy');
  assert.match(node.innerHTML, /Amount hidden/); assert.doesNotMatch(node.innerHTML, /12,345\.67/);
});

test('detached prior account panel is not repainted by global preference events', () => {
  const f = fixture(), node = new Element();
  node.innerHTML = f.api.render(payload()); f.api.bind(node, {});
  node.isConnected = false; const before = node.innerHTML;
  f.context.walletHunterLanguage = 'en'; f.event('whlanguage');
  assert.equal(node.innerHTML, before);
});

test('paper loss limits and budget waiting statuses translate in both languages', () => {
  const f = fixture(), data = payload();
  data.modes.trader.paper.status = 'LOSS_LIMIT'; data.modes.trader.paper.reason = 'slot_loss_limit';
  assert.match(f.api.render(data), /Достигнут виртуальный лимит убытка доли/);
  f.context.walletHunterLanguage = 'en';
  assert.match(f.api.render(data), /The virtual share loss limit was reached/);
  data.modes.trader.paper.status = 'WAITING_BUDGET'; data.modes.trader.paper.reason = 'no_allocated_budget';
  assert.match(f.api.render(data), /No virtual budget is allocated/);
});

test('controls have mobile touch targets and no real trading endpoint is present', () => {
  const css = fs.readFileSync(path.join(__dirname, '../webapp/static/ai.css'), 'utf8');
  assert.match(css, /\.aiModeSwitch\{[^}]*min-height:48px/);
  assert.match(css, /\.aiModeHistory summary\{[^}]*min-height:44px/);
  assert.doesNotMatch(source, /\/api\/position|\/api\/ai\/reviews|\/exchange|real_execution_available\s*:\s*true/);
});

test('invalid trader prerequisites block enabling but never prevent switching off', async () => {
  const f = fixture(), node = new Element(); let calls = 0;
  const data = payload(); data.modes.trader.enabled = false;
  for (const blocked of ['account_missing','too_many_wallets','ai_slot_unavailable']) {
    data.modes.trader.blocked_reason = blocked;
    node.innerHTML = f.api.render(data); f.api.bind(node, {api:async () => {calls++;}});
    assert.equal(node.buttons[0].disabled, true);
    await node.buttons[0].click();
    data.modes.trader.enabled = true;
    node.innerHTML = f.api.render(data);
    assert.equal(node.buttons[0].disabled, false);
    data.modes.trader.enabled = false;
  }
  data.modes.trader.blocked_reason = 'disabled'; data.modes.trader.slot_selected = false;
  node.innerHTML = f.api.render(data);
  assert.equal(node.buttons[0].disabled, true); assert.equal(calls, 0);
  assert.match(node.innerHTML, /Откройте «Кошельки» и выберите ИИ/);
  f.context.walletHunterLanguage = 'en';
  assert.match(f.api.render(data), /Open Wallets and select AI/);
  assert.doesNotMatch(f.api.render(data), /[А-Яа-яЁё]/);
});

test('universe is restricted to declared BTC ETH and cost assumptions are explicit', () => {
  const f = fixture(), data = payload();
  data.modes.trader.universe = ['BTC','ETH','<script>bad()</script>','xyz:NVDA','BTC'];
  const ru = f.api.render(data);
  assert.match(ru, /BTC · ETH/); assert.doesNotMatch(ru, /<script>|xyz:NVDA/);
  assert.match(ru, /Не обученная модель/); assert.match(ru, /комиссия 0,05% \+ проскальзывание 0,05%/);
  assert.match(ru, /Фандинг не учтён/);
  f.context.walletHunterLanguage = 'en'; const en = f.api.render(data);
  assert.match(en, /Not a trained model/); assert.match(en, /0.05% fee \+ 0.05% slippage/);
  assert.match(en, /Funding is excluded/); assert.doesNotMatch(en, /[А-Яа-яЁё]/);
});

test('fresh unavailable or partial observer status overrides stale active paper state safely', () => {
  const f = fixture(), data = payload();
  data.modes.trader.paper.last_tick_ms = 1788728300000;
  data.modes.trader.last_tick = {status:'UNAVAILABLE',reason:'public_data_unavailable',asof_ms:1788728400000,
    error:'SECRET KEY=not-display',errors:[{reason:'<script>details()</script>'}]};
  let html = f.api.render(data);
  assert.match(html, /aiModeState"><b>Рыночные данные недоступны/);
  assert.match(html, /Последняя проверка данных/);
  assert.doesNotMatch(html, /SECRET|<script>|Работает виртуально/);
  f.context.walletHunterLanguage = 'en';
  data.modes.trader.last_tick.status = 'PARTIAL_DATA';
  html = f.api.render(data);
  assert.match(html, /aiModeState"><b>Partial market data/);
  assert.match(html, /Latest data check/); assert.doesNotMatch(html, /[А-Яа-яЁё]/);
  data.modes.trader.last_tick.asof_ms = 1788728200000;
  assert.match(f.api.render(data), /aiModeState"><b>Running virtually/);
});

test('mode POST aborts after 15 seconds without retry and waits for a fresh GET before another mutation', async () => {
  const f = fixture(), node = new Element(), data = payload(); let calls = 0, reloads = 0, signal;
  node.innerHTML = f.api.render(data);
  f.api.bind(node, {api: (url, body, passedSignal) => {
    calls++; signal = passedSignal;
    return new Promise(() => {}); // Even an adapter that ignores abort cannot lock the UI forever.
  }, reload:async () => {reloads++;}});
  const pending = node.buttons[0].click();
  assert.equal(f.timers.size, 1);
  const deadline = [...f.timers.values()][0];
  assert.equal(deadline.delay, 15000); assert.equal(signal.aborted, false);
  deadline.callback(); await pending;
  assert.equal(signal.aborted, true); assert.equal(calls, 1); assert.equal(reloads, 0);
  assert.equal(f.timers.size, 0);
  assert.match(node.innerHTML, /Изменение не подтверждено/);
  assert.equal(node.buttons[0].disabled, true); await node.buttons[0].click(); assert.equal(calls, 1);
  assert.equal(data.modes.trader.enabled, true);
  const refreshed = payload(); refreshed.modes.trader.enabled = false;
  node.innerHTML = f.api.render(refreshed); f.api.bind(node, {});
  assert.equal(node.buttons[0].disabled, false);
  assert.doesNotMatch(node.innerHTML, /Изменение не подтверждено/);
});
