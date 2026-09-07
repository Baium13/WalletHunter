/* Actual index.html / static modules startup smoke. Fully offline synthetic API.
 * Every request is fulfilled locally; no auth, production data or trading calls.
 */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const assets=path.resolve(__dirname,'../webapp/static');
const build='20260906-11';
function dashboard(language){return{language,balance:91.043944,account:'Synthetic account · 111139',copy_enabled:true,
  crypto_enabled:true,stocks_enabled:true,notifications:true,risk_mode:'standard',strategy_mode:'swing',max_leverage:20,
  platform_leverage_choices:[1,2,3,5,10,20,40],ai_slot_selected:true,position_actions_pending:1,ai_entries_pending:0,
  wallets:[{slot:1,configured:true,enabled:true},{slot:2,configured:true,enabled:true},{slot:3,configured:false,enabled:false}],
  positions:[{coin:'ETH',dex:'',side:'SHORT',size:.0188,entry_price:2444.6,mark_price:2510,leverage:20,
    margin_used:2.3594,position_value:47.188,unrealized_pnl:-1.22952,roe:-53,origin:{managed:true,source_slots:[1]}}],
  events:[{action:'AI_POSITION_REVIEW',coin:'ETH',time:Date.now()}]};}
function ai(){const now=Date.now();return{reviews:[],holds:{},research:{studies:0,groups:[]},
  position_actions:{status:'IDLE',reason:'no_eligible_scenario',pending:[],history:[],holds:[],availability:[],
    execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false},
  user_orders:{status:'IDLE',reason:'no_signal',pending:[],history:[],execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false},
  modes:{trader:{enabled:true,slot_selected:true,execution_mode:'PAPER',real_execution_available:false,
    limits:{entry_pct:10,max_leverage:40,max_loss_pct:10},universe:['BTC','ETH'],last_tick:{status:'OK',asof_ms:now},
    paper:{status:'ACTIVE',reason:'rules_paper_only',budget_usdc:30.34,equity_usdc:30.34,realized_pnl_usdc:0,positions:[],events:[]}},
    rescue:{enabled:true,execution_mode:'CONFIRMATION_REQUIRED',real_execution_available:false,trigger_roe_pct:-40,max_extra_slot_fraction:.5,max_additions:4}},
  learning:{status:'WAITING_DATA',reason:'insufficient_training_data',real_execution_available:false,counts:{candles:0,examples:0,train_groups:0,test_groups:0,
    forward_predictions:0,forward_matured:0},model:null,validation:null,forward:{samples:0},predictions:[],collector:{status:'WAITING_DATA'}}};}
function candles(){const end=Math.floor(Date.now()/900000)*900000;return Array.from({length:96},(_,i)=>({t:end-(96-i)*900000,
  T:end-(95-i)*900000-1,o:60000+i*12,h:60100+i*12,l:59900+i*12,c:60030+i*12,v:120+i}));}
async function layout(page,name){return page.evaluate(name=>({page:name,viewport:innerWidth,scroll:document.documentElement.scrollWidth,
  outside_controls:[...document.querySelectorAll('header button,nav button')].filter(node=>node.getBoundingClientRect().right>innerWidth+.5||node.getBoundingClientRect().left<-.5)
    .map(node=>({id:node.id,page:node.dataset.page,left:node.getBoundingClientRect().left,right:node.getBoundingClientRect().right}))}),name);}

(async()=>{
  const output=path.resolve(process.argv[2]||'../ui-audit-20260906-10');fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true}),results=[];
  try{
    for(const width of [320,390])for(const language of ['ru','en'])for(const deepLink of [false,true]){
      const page=await browser.newPage({viewport:{width,height:844},reducedMotion:'reduce'});
      const errors=[],unexpected=[],requests=[],mutations=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.addInitScript(lang=>{localStorage.setItem('wh_audio','off');localStorage.setItem('wh_lang',lang);localStorage.setItem('wh_privacy','off');},language);
      await page.route('**/*',async route=>{
        const request=route.request(),url=new URL(request.url());requests.push({path:url.pathname,search:url.search,method:request.method()});
        if(request.method()!=='GET'){mutations.push(`${request.method()} ${url.pathname}`);return route.abort();}
        if(url.href==='https://telegram.org/js/telegram-web-app.js')return route.fulfill({contentType:'application/javascript',body:
          'window.Telegram={WebApp:{initData:"offline-test-only",ready(){},expand(){},HapticFeedback:{selectionChanged(){},impactOccurred(){},notificationOccurred(){}}}};'});
        if(url.origin!=='https://offline-wallet.test'){unexpected.push(request.url());return route.abort();}
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'index.html'))});
        if(url.pathname.startsWith('/static/')){
          const file=path.resolve(assets,'.'+decodeURIComponent(url.pathname.slice('/static'.length)));
          if(!file.startsWith(assets+path.sep)||!fs.existsSync(file)){unexpected.push(url.pathname);return route.abort();}
          const types={'.js':'application/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.jpg':'image/jpeg'};
          return route.fulfill({contentType:types[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
        }
        if(url.pathname==='/api/dashboard'){
          if(deepLink)await new Promise(resolve=>setTimeout(resolve,180));
          return route.fulfill({json:dashboard(language)});
        }
        if(url.pathname==='/api/ai')return route.fulfill({json:ai()});
        if(url.pathname==='/api/chart')return route.fulfill({json:{candles:candles(),interval:url.searchParams.get('interval')||'15m'}});
        if(url.pathname==='/api/markets')return route.fulfill({json:{markets:['BTC','ETH','xyz:NVDA']}});
        if(url.pathname==='/api/price')return route.fulfill({json:{coin:url.searchParams.get('coin')||'BTC',price:61200}});
        unexpected.push(url.pathname);return route.fulfill({status:404,json:{detail:'Unknown offline fixture'}});
      });
      await page.goto('https://offline-wallet.test/'+(deepLink?'#ai-position':''));
      await page.waitForFunction(()=>document.querySelector('#home .hero')&&window.walletHunterLanguage);
      assert.equal(await page.locator('meta[name="wh-build"]').getAttribute('content'),build);
      assert.equal(await page.locator('#mode').textContent(),'COPY ON');
      if(deepLink){
        await page.waitForSelector('#ai.active .aiPositionActionsCard');
        assert.equal(await page.locator('.page.active').getAttribute('id'),'ai','Delayed dashboard must not reset deep-link navigation');
      }else{
        assert.equal(await page.locator('.page.active').getAttribute('id'),'home');
        await page.locator('nav button[data-page="ai"]').click();
        await page.waitForSelector('#ai.active .aiPositionActionsCard');
      }
      assert.ok(requests.some(row=>row.path==='/static/ai-review.js'&&row.search===`?v=${build}`),'Actual lazy v10 module must load');
      const scriptOrder=await page.locator('script[src]').evaluateAll(nodes=>nodes.map(node=>new URL(node.src).pathname));
      assert.ok(scriptOrder.indexOf('/static/ai-position-actions.js')<scriptOrder.indexOf('/static/ai.js'),'Position actions must initialize before the AI loader');
      assert.equal(await page.locator('.aiPositionActionsCard').count(),1);
      assert.equal(await page.locator('.aiUserOrdersCard').count(),1);
      assert.equal(await page.locator('#aiModes').count(),1);
      const layouts=[await layout(page,'initial-ai')];
      await page.screenshot({path:path.join(output,`startup-ai-${width}-${language}-${deepLink?'deep':'nav'}.png`),animations:'disabled'});
      for(const name of ['home','wallets','analysis','settings','chart','ai','home']){
        await page.locator(`nav button[data-page="${name}"]`).click();
        await page.waitForFunction(id=>document.querySelector('.page.active')?.id===id,name);
        assert.equal(await page.locator('.page.active').count(),1);
        assert.equal(await page.locator(`nav button[data-page="${name}"]`).evaluate(node=>node.classList.contains('active')),true);
        if(name==='wallets')await page.waitForSelector('#wallets #aiSlotToggle');
        if(name==='chart'){
          await page.waitForFunction(()=>document.getElementById('chartMeta')?.textContent.includes('96'));
          assert.equal(await page.locator('#chart .intervals .active').count(),1);
          assert.equal(await page.locator('#chart .chartRanges .active').count(),1);
        }
        if(name==='ai')await page.waitForSelector('#ai.active .aiPositionActionsCard');
        layouts.push(await layout(page,name));
      }
      await page.screenshot({path:path.join(output,`startup-home-${width}-${language}-${deepLink?'deep':'nav'}.png`),animations:'disabled'});
      assert.deepEqual(mutations,[],'Startup and navigation must never mutate');assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
      results.push({width,language,deep_link:deepLink,build,all_six_tabs:'passed',mutations:0,page_errors:0,external_requests:0,layouts});
      await page.close();
    }
    const report={offline:true,results,screenshots:output};fs.writeFileSync(path.join(output,'startup-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
    const overflowing=results.flatMap(result=>result.layouts.filter(row=>row.scroll>row.viewport||row.outside_controls.length).map(row=>({width:result.width,language:result.language,...row})));
    assert.deepEqual(overflowing,[],'Full app header/navigation must fit the viewport on every tab');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
