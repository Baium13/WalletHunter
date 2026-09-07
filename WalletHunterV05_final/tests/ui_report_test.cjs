/* Isolated rendering/API contract tests. No DOM boot, live network or orders.
 * This does not validate Telegram WebView, CSS layout, or the browser i18n walker.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webapp/static/app.js'), 'utf8');
const boundary = source.indexOf('\nfunction render()');
assert.ok(boundary > 0, 'Expected pre-boot renderer boundary');

function fixture({language = 'ru', privacy = false, response} = {}) {
  const listeners = new Map();
  const target = {innerHTML: ''};
  const requests = [];
  const window = {
    walletHunterLanguage: language,
    Telegram: {WebApp: {initData: 'test-only-init-data', ready() {}, expand() {}}},
    addEventListener(name, callback) {
      const callbacks = listeners.get(name) || [];
      callbacks.push(callback); listeners.set(name, callbacks);
    },
  };
  const context = vm.createContext({
    window,
    document: {getElementById: id => id === 'analysisResult' ? target : null},
    localStorage: {getItem: key => key === 'wh_privacy' && privacy ? 'on' : null},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (!response) throw new Error('Unexpected test network call');
      return response;
    },
  });
  vm.runInContext(source.slice(0, boundary), context, {filename: 'app-renderers.js'});
  return {
    context, target, requests,
    language(value) {
      window.walletHunterLanguage = value;
      for (const callback of listeners.get('whlanguage') || []) callback();
    },
  };
}

const position = (coin = 'BTC') => ({
  coin, dex: '', side: 'SHORT', entry_price: 100, mark_price: 99,
  size: 2, leverage: 5, margin_used: 40, position_value: 200,
  unrealized_pnl: 2, roe: 5,
});
const analysis = (overrides = {}) => ({
  wallet: '0x1234', rating: 45, balance: 1000, trades: 6, win_rate: 50,
  profit_factor: 1.2, net_pnl: 25, drawdown: 12, score: 37,
  positions: [], ...overrides,
});
const decodeHtml = text => text.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

test('Unavailable capital is unknown, not a fabricated zero balance', () => {
  const {context}=fixture();
  assert.equal(context.money(null,true),'—');
  assert.equal(context.money(undefined,true),'—');
  assert.equal(context.money(NaN,true),'—');
  assert.match(context.money(0,true),/0\.00/);
});

test('Capital-mode warning is bilingual and never inserts an untrusted error', () => {
  const f=fixture();
  vm.runInContext('D={balance_error:"CAPITAL_MODE_UNSUPPORTED"}',f.context);
  assert.match(f.context.capitalWarning(),/Позиции доступны/);
  f.language('en');
  assert.match(f.context.capitalWarning(),/Positions remain visible/);
  assert.doesNotMatch(f.context.capitalWarning(),/[а-яё]/i);
  vm.runInContext('D={balance_error:"<img onerror=alert(1)>"}',f.context);
  assert.doesNotMatch(f.context.capitalWarning(),/onerror/);
});

test('Capital API errors are useful in both languages', async () => {
  for(const language of ['ru','en'])for(const code of ['CAPITAL_MODE_UNSUPPORTED','BALANCE_UNAVAILABLE']){
    const f=fixture({language,response:{ok:false,json:async()=>({detail:{code}})}});
    await assert.rejects(f.context.api('/api/copy'),language==='en'?/Copying cannot start/:/Копирование не запущено/);
  }
});

test('Russian report describes realised history, costs, and limitations', () => {
  const {context} = fixture();
  const html = context.report(analysis());
  assert.match(html, /ИСТОРИЯ · ДО 90 ДНЕЙ/);
  assert.match(html, /Баланс/);
  assert.match(html, /Закрывающих исполнений/);
  assert.match(html, /Просадка закрытий/);
  assert.match(html, /PnL с комиссиями, без фандинга/);
  assert.match(html, /не допуск к торговле/);
  assert.match(html, /Открытых позиций нет/);
  assert.doesNotMatch(html, /ПОДХОДИТ|ДОПУЩЕН|Sharpe|Median ROI/);
});

test('English report is English and makes no copy-profit admission claim', () => {
  const {context} = fixture({language: 'en'});
  const html = context.report(analysis());
  assert.match(html, /HISTORY · UP TO 90 DAYS/);
  assert.match(html, /Balance/);
  assert.match(html, /Closing fills/);
  assert.match(html, /Realised DD/);
  assert.match(html, /PnL includes fees, excludes funding/);
  assert.match(html, /not a profit forecast/);
  assert.match(html, /Not a backtest of our deposit or trading approval/);
  assert.match(html, /No open positions/);
  assert.doesNotMatch(html, /[А-Яа-яЁё]/);
});

test('Existing analysis re-renders on language switch without another API call', () => {
  const fx = fixture();
  fx.target.innerHTML = fx.context.report(analysis());
  assert.match(fx.target.innerHTML, /Открытые позиции/);
  fx.language('en');
  assert.match(fx.target.innerHTML, /Open positions/);
  assert.doesNotMatch(fx.target.innerHTML, /[А-Яа-яЁё]/);
  fx.language('ru');
  assert.match(fx.target.innerHTML, /Открытые позиции/);
  assert.equal(fx.requests.length, 0);
});

test('Language change before any report is harmless', () => {
  const fx = fixture();
  fx.language('en');
  assert.equal(fx.target.innerHTML, '');
});

test('Unbounded PF renders infinity symbol, not a spurious finite ratio', () => {
  const {context} = fixture();
  const html = context.report(analysis({profit_factor: Infinity}));
  assert.match(html, /PF<b>∞<\/b>/);
  assert.doesNotMatch(html, /Infinity|NaN/);
});

test('Every analysed position is explicitly external, never array-index own=true', () => {
  const {context} = fixture({privacy: true});
  const calls = [];
  context.pos = (p, own) => { calls.push({coin: p.coin, own}); return `<p>${p.coin}</p>`; };
  context.report(analysis({positions: [position('BTC'), position('ETH'), position('xyz:NVDA')]}));
  assert.deepEqual(calls, [
    {coin: 'BTC', own: false}, {coin: 'ETH', own: false}, {coin: 'xyz:NVDA', own: false},
  ]);
});

test('Privacy masks own position money but leaves external amounts and ROE visible', () => {
  const {context} = fixture({language: 'en', privacy: true});
  const own = context.pos(position(), true);
  assert.match(own, /maskedValue/);
  assert.doesNotMatch(own, /\$40\.00|\$200\.00|\$2\.00/);
  assert.match(own, /\+5\.00%/);
  const external = context.report(analysis({positions: [position(), position('ETH')]}));
  assert.doesNotMatch(external, /maskedValue|••••••/);
  assert.equal((external.match(/\$40\.00/g) || []).length, 2);
  assert.equal((external.match(/\$200\.00/g) || []).length, 2);
});

test('Untrusted wallet and PF strings are escaped; untrusted recommendation ignored', () => {
  const {context} = fixture();
  const html = context.report(analysis({
    wallet: '<img src=x onerror="attack()">', profit_factor: '<script>attack()</script>',
    recommendation: '<svg onload=attack()>',
  }));
  assert.match(html, /&lt;img src=x onerror=&quot;attack\(\)&quot;&gt;/);
  assert.match(html, /&lt;script&gt;attack\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<img|<script|<svg/);
});

test('Position coin and dex remain inert in text and chart button code', () => {
  const {context} = fixture();
  const coin = 'BTC\" onclick=\"attack()\"><img src=x onerror=attack()>\'\\';
  const dex = '\");attack();//';
  const html = context.pos({...position(coin), dex});
  assert.doesNotMatch(html, /<img|<script/);
  assert.equal((html.match(/ onclick="/g) || []).length, 1);
  const match = html.match(/<button class=chartIcon onclick="([^"]*)">/);
  assert.ok(match, 'One fully escaped onclick attribute');
  let args;
  vm.runInNewContext(decodeHtml(match[1]), {openChart: (...values) => { args = values; }});
  assert.equal(args[0], coin);
  assert.equal(args[1], dex);
  assert.equal(args[6], false);
});

test('Events use selected-language labels and hide raw error payloads', () => {
  const fx = fixture();
  const event = {action: 'WAIT_PRICE', coin: 'xyz:GOOGL', time: 1788505834310, error: 'private-account-secret'};
  assert.match(fx.context.eventCard(event), /Ожидание цены/);
  fx.language('en');
  const html = fx.context.eventCard(event);
  assert.match(html, /Waiting for price/);
  assert.match(html, /xyz:GOOGL/);
  assert.doesNotMatch(html, /private-account-secret|1788505834310|[А-Яа-яЁё]/);
});

test('Unknown event action is neutral; malicious coin cannot create HTML', () => {
  const {context} = fixture({language: 'en'});
  const html = context.eventCard({action: '<script>attack()</script>', coin: '<img src=x onerror="attack()">', time: 'bad-date'});
  assert.match(html, /<b>Event &lt;img/);
  assert.match(html, /<small>—<\/small>/);
  assert.doesNotMatch(html, /<script|<img|Invalid Date/);
});

test('Incomplete history API response has a useful localized error', async () => {
  const fx = fixture({response: {ok: false, json: async () => ({detail: {code: 'HISTORY_INCOMPLETE'}})}});
  await assert.rejects(fx.context.api('/api/analyse'), /История биржи неполная/);
  fx.language('en');
  await assert.rejects(fx.context.api('/api/analyse'), /The exchange history is incomplete/);
});

test('Structured or malformed API errors are not displayed as raw objects', async () => {
  const fx = fixture({language: 'en', response: {ok: false, json: async () => ({detail: {private_field: 'private-secret'}})}});
  await assert.rejects(fx.context.api('/api/analyse'), error => {
    assert.match(error.message, /Action unavailable/);
    assert.doesNotMatch(error.message, /object Object|private-secret/);
    return true;
  });
  const malformed = fixture({response: {ok: false, json: async () => { throw new Error('invalid JSON'); }}});
  await assert.rejects(malformed.context.api('/api/analyse'), /Действие недоступно/);
});

test('API success preserves Telegram authorization and method/body exactly', async () => {
  const payload = {ok: true};
  const fx = fixture({response: {ok: true, json: async () => payload}});
  assert.equal(await fx.context.api('/api/analyse', {method: 'POST', body: '{"address":"test"}'}), payload);
  assert.equal(fx.requests.length, 1);
  assert.equal(fx.requests[0].options.headers['X-Telegram-Init-Data'], 'test-only-init-data');
  assert.equal(fx.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(fx.requests[0].options.method, 'POST');
  assert.equal(fx.requests[0].options.body, '{"address":"test"}');
});
