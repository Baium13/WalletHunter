/* Offline real-browser layout check. All API requests use synthetic fixtures.
 * No Telegram login, real wallet data, or exchange request is used.
 * NODE_PATH should point to an existing Playwright installation.
 */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const assets = path.resolve(__dirname, '../webapp/static');

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const output = path.resolve(process.argv[2] || 'ui-audit');
  fs.mkdirSync(output, {recursive: true});
  try {
    for (const width of [320, 390]) for (const language of ['ru', 'en']) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      const errors = [], posts = [];
      page.on('pageerror', error => errors.push(error.message));
      const modes = {
        trader: {enabled: true, slot_selected: true, execution_mode: 'PAPER', real_execution_available: false,
          limits: {entry_pct: 10, max_leverage: 40, max_loss_pct: 10}, universe: ['BTC', 'ETH'],
          last_tick: {status: 'OK', asof_ms: Date.now()},
          paper: {status: 'ACTIVE', reason: 'rules_paper_only', budget_usdc: 300, equity_usdc: 302,
            realized_pnl_usdc: 1, positions: [{coin: 'BTC', side: 'LONG', leverage: 20, margin_usdc: 3,
              notional_usdc: 60, unrealized_pnl_usdc: 1, roe_pct: 33.3}],
            events: [{action: 'OPEN', coin: 'BTC', time_ms: Date.now(), reason: 'trend_macd_rsi'}]}},
        rescue: {enabled: true, execution_mode: 'CONFIRMATION_REQUIRED', real_execution_available: false,
          trigger_roe_pct: -50, max_extra_slot_fraction: .5, max_additions: 4}
      };
      const learning = {status:'RESEARCH_MODEL', reason:'public_rules_features_logistic_research_only', real_execution_available:false,
        counts:{candles:3000,examples:1400,train_groups:280,test_groups:70,forward_predictions:4,forward_matured:0},
        model:{version:1,created_ms:Date.now(),trained_through_ms:Date.now()-3600000,features:['return1'],train_samples:1000},
        validation:{kind:'RETROSPECTIVE_BOOTSTRAP',model_version:1,groups:70,samples:280,brier:.24,log_loss:.65,baseline_brier:.23,baseline_log_loss:.63},
        forward:{kind:'FORWARD_PREQUENTIAL',samples:0},predictions:[],collector:{status:'OK',last_success_ms:Date.now()}};
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/ai/modes') {
          const body = route.request().postDataJSON(); posts.push(body);
          assert.deepEqual(Object.keys(body).sort(), ['enabled', 'mode']);
          modes[body.mode].enabled = body.enabled;
          return route.fulfill({json: {ok: true, modes}});
        }
        if (url.pathname === '/api/ai') return route.fulfill({json: {modes, learning, reviews: [], holds: {}, research: {studies: 0, groups: []},
          user_orders:{status:'IDLE',reason:'no_signal',pending:[],history:[],execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false}}});
        if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body:
          '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main><header><h1 id="title">AI</h1></header><section class="page active" id="ai"></section></main><nav><button data-page="ai">AI</button></nav></body></html>'});
        throw new Error('Unexpected network request: ' + url.pathname);
      });
      await page.goto('https://offline-wallet.test/');
      await page.addStyleTag({path: path.join(assets, 'style.css')});
      await page.addStyleTag({path: path.join(assets, 'ai.css')});
      await page.evaluate(lang => {window.walletHunterLanguage = lang; window.walletHunterPrivacy = false;}, language);
      await page.addScriptTag({path: path.join(assets, 'ai-modes.js')});
      await page.addScriptTag({path: path.join(assets, 'ai-learning.js')});
      await page.addScriptTag({path: path.join(assets, 'ai-user-orders.js')});
      await page.addScriptTag({path: path.join(assets, 'ai-review.js')});
      await page.evaluate(() => window.walletHunterAiReview.open());
      assert.equal(await page.locator('[data-ai-mode]').count(), 2);
      assert.equal(await page.locator('.aiUserOrdersCard').count(), 1);
      for (const button of await page.locator('[data-ai-mode]').all()) {
        // Animated translateY may give a 47.999999px bounding rect in Chromium.
        assert.ok(await button.evaluate(el=>parseFloat(getComputedStyle(el).minHeight)>=48));
        const bounds=await button.boundingBox();
        assert.ok(bounds.height>=47.99, JSON.stringify(bounds));
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow');
      await page.screenshot({path: path.join(output, `ai-modes-${width}-${language}.png`), fullPage: true});
      assert.equal(await page.locator('.aiLearningCard').count(), 1);
      await page.locator('.aiLearningCard details summary').click();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'expanded learning metrics overflow');
      await page.locator('.aiLearningCard').screenshot({path: path.join(output, `learning-${width}-${language}.png`)});
      await page.locator('[data-ai-mode="trader"]').click();
      await page.waitForFunction(() => document.querySelector('[data-ai-mode="trader"]')?.getAttribute('aria-checked') === 'false');
      assert.equal(await page.locator('[data-ai-mode="rescue"]').getAttribute('aria-checked'), 'true');
      await page.evaluate(() => {window.walletHunterPrivacy = true; window.dispatchEvent(new Event('whprivacy'));});
      assert.ok(await page.locator('#aiModes .maskedValue').count() >= 3);
      assert.deepEqual(posts, [{mode: 'trader', enabled: false}]);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log(JSON.stringify({offline_browser_layouts: 4, widths: [320,390], languages: ['ru','en'],
      overflow: false, switches_min_height: 48, independent_switch_and_privacy: 'passed', real_requests: 0, screenshots: output}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
