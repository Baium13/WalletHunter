/* Local real-browser QA. Synthetic cards + in-memory requests, no exchange. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const assets = path.resolve(__dirname, '../webapp/static');
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const output = path.resolve(process.argv[2] || 'ui-user-orders-audit');
  fs.mkdirSync(output, {recursive:true});
  try {
    for (const width of [320,390]) for (const language of ['ru','en']) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors=[];
      page.on('pageerror', e=>errors.push(e.message));
      await page.route('**/*', route=>route.abort());
      await page.setContent('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main><section id="ai" class="page active"><div id="forms"></div></section></main></body></html>');
      for (const name of ['style.css','ai.css']) await page.addStyleTag({path:path.join(assets,name)});
      await page.evaluate(lang=>{window.walletHunterLanguage=lang;window.walletHunterPrivacy=false;},language);
      await page.addScriptTag({path:path.join(assets,'ai-user-orders.js')});
      await page.evaluate(()=>{
        const row={id:'offline-form-1',status:'PENDING',created_ms:Date.now(),expires_ms:Date.now()+90000,
          payload:{coin:'BTC',dex:'',side:'LONG',direction:'LONG',action:'OPEN',order_type:'LIMIT_IOC',
            margin_mode:'cross',network:'MAINNET',leverage:40,size:.00497,size_text:'0.00497',
            limit_price:80400,limit_price_text:'80400',share_fraction:1/3,entry_pct_of_share:10,entry_policy_version:2,
            share_usdc:100,margin_estimate_usdc:9.94,margin_cap_usdc:10,maximum_expected_margin_usdc:9.9897,
            notional_usdc:397.6,estimated_fee_usdc:.199794,source:{probability_positive_net:.65}}};
        window.fixture={status:'PENDING',reason:'confirmation_required',pending:[row],history:[],
          execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false};
        window.fakePosts=[];
        const container=document.getElementById('forms');
        function paint(){container.innerHTML=whAiUserOrders.render(window.fixture);whAiUserOrders.bind(container,{reload:async()=>paint(),
          api:async(url,body)=>{window.fakePosts.push({url,body});window.fixture={...window.fixture,status:'IDLE',reason:'signal_already_handled',pending:[],
              history:[{...row,status:body.confirm?'FILLED':'DECLINED',result:body.confirm?{filled_size:.00497,average_price:80000}:null}]};return {};}});}
        window.paintFixture=paint;paint();
      });
      const confirm=page.locator('[data-ai-order-confirm]');
      assert.equal(await confirm.isEnabled(),true);
      for (const button of await page.locator('.aiUserActions button').all()) assert.ok((await button.boundingBox()).height>=48);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'horizontal overflow');
      await page.screenshot({path:path.join(output,`order-${width}-${language}.png`),fullPage:true});
      await page.evaluate(()=>{window.walletHunterPrivacy=true;window.dispatchEvent(new Event('whprivacy'));});
      assert.equal(await confirm.isEnabled(),false);
      assert.ok(await page.locator('.maskedValue').count()>=5);
      await page.evaluate(()=>{window.walletHunterPrivacy=false;window.dispatchEvent(new Event('whprivacy'));});
      if(width===320)await page.locator('[data-ai-order-decline]').click();else await confirm.click();
      await page.waitForFunction(()=>document.querySelectorAll('[data-ai-order-confirm]').length===0);
      assert.equal(await page.evaluate(()=>fakePosts.length),1);
      assert.deepEqual(await page.evaluate(()=>Object.keys(fakePosts[0].body)),['confirm']);
      await page.locator('details summary').click();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log(JSON.stringify({layouts:4,widths:[320,390],languages:['ru','en'],overflow:false,
      touch_height_min:48,privacy_guard:'passed',confirm_decline:'mocked only',real_orders:0,screenshots:output}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
