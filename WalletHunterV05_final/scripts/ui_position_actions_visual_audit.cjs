/* Offline browser QA: synthetic fixtures only; every HTTP request is intercepted.
 * No Telegram session, real account, signer, server or exchange is accessed.
 */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const assets=path.resolve(__dirname,'../webapp/static');

function proposal(now,action='REDUCE'){
  const average=action==='AVERAGE',size=average?.0046:.0047,limit=average?2497.4:2522.6;
  const before={coin:'ETH',dex:'',side:'SHORT',size:.0188,entry_price:2444.6,leverage:20,margin_mode:'cross',roe:-53,
    margin_used:2.3594,unrealized_pnl:-1.22952};
  const afterSize=before.size+(average?size:-size),fee=size*2522.6*.0005;
  return{id:average?'synthetic-average':'synthetic-reduce',status:'PENDING',created_ms:now,expires_ms:now+90000,
    payload:{action,coin:'ETH',dex:'',direction:'SHORT',side:'SHORT',network:'MAINNET',margin_mode:'cross',
      size,size_text:String(size),sz_decimals:4,limit_price:limit,limit_price_text:String(limit),reference_price:2510,leverage:20,
      order_type:'LIMIT_IOC',reduce_only:!average,is_buy:!average,position_before:before,
      expected_after:{size:afterSize,entry_price:average?(before.size*before.entry_price+size*limit)/afterSize:before.entry_price,
        margin_estimate_usdc:average?before.margin_used+size*2522.6/20:before.margin_used*.75},
      action_notional_usdc:size*2510,margin_change_estimate_usdc:average?size*2522.6/20:-before.margin_used*.25,
      realized_pnl_estimate_usdc:(average?0:(before.entry_price-limit)*size)-fee,estimated_fee_usdc:fee,assumed_fee_bps:5,
      source_wallet:'0x'+'a'.repeat(40),source_slot_usdc:30.347981,source_reserved_usdc:2.3594,extra_used_usdc:0,
      extra_remaining_usdc:15.1739905,additions_used:0,additions_remaining:4,sizing_mode:'unifiedAccount',
      factors:{trend_ema20_50:{ema20:2491.456,ema50:2525.009},rsi14:45.78,macd_hist:-1.5123,atr14_pct:1.255,
        volume_ratio20:1.244,levels20:{support:2420,resistance:2560},funding_bps_hour:.08,open_interest:123456789,
        candle_close_ms:Math.floor(now/900000)*900000-1,asof_ms:now},
      hypothesis:{classification:average?'EXPERIMENTAL_REBOUND':'RISK_REDUCTION',probability:null,probability_label:'success_not_guaranteed'},
      alternatives:[{action:'LOWER_LEVERAGE',available:false,reason:'cross_or_unimplemented'},
        {action:'ADD_MARGIN',available:false,reason:'cross_or_unimplemented'}],
      stop_impact:{open_orders_count:0,stop_orders_count:0,policy:'no_automatic_stop_changes',reason:'no_orders'},
      policy:{trigger_roe_pct:-40,target_roe_pct:3,failure_roe_pct:-120,max_extra_source_fraction:.5,max_additions:4}}};
}
function fixture(){
  const now=Date.now();
  return{position_actions:{status:'PENDING',reason:'confirmation_required',execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false,
      pending:[proposal(now),proposal(now,'AVERAGE')],history:[],holds:[],availability:[]},
    modes:{trader:{enabled:true,slot_selected:true,execution_mode:'PAPER',real_execution_available:false,
      limits:{entry_pct:1,max_leverage:20,max_loss_pct:10},universe:['BTC','ETH'],last_tick:{status:'OK',asof_ms:now},
      paper:{status:'ACTIVE',reason:'rules_paper_only',budget_usdc:30.347981,equity_usdc:30.64,realized_pnl_usdc:.3,positions:[],events:[]}},
      rescue:{enabled:true,execution_mode:'CONFIRMATION_REQUIRED',real_execution_available:false,trigger_roe_pct:-40,max_extra_slot_fraction:.5,max_additions:4}},
    learning:{status:'RESEARCH_MODEL',reason:'public_rules_features_logistic_research_only',real_execution_available:false,
      counts:{candles:3000,examples:1400,train_groups:280,test_groups:70,forward_predictions:4,forward_matured:0},
      model:{version:1,created_ms:now,trained_through_ms:now-3600000,features:['return1'],train_samples:1000},
      validation:{kind:'RETROSPECTIVE_BOOTSTRAP',model_version:1,groups:70,samples:280,brier:.24,log_loss:.65,baseline_brier:.23,baseline_log_loss:.63},
      forward:{kind:'FORWARD_PREQUENTIAL',samples:0},predictions:[],collector:{status:'OK',last_success_ms:now}},
    reviews:[],holds:{},research:{studies:0,groups:[]},
    user_orders:{status:'IDLE',reason:'no_signal',pending:[],history:[],execution_mode:'USER_CONFIRMATION_ONLY',automatic_execution:false}};
}
async function assertLayout(page,label){
  const bad=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
    outside:[...document.querySelectorAll('.aiPositionActionsCard button,.aiPositionBeforeAfter,.aiPositionFactors')]
      .filter(el=>el.getBoundingClientRect().width&&((el.getBoundingClientRect().right>innerWidth+.5)||(el.getBoundingClientRect().left<-.5)))
      .map(el=>({tag:el.tagName,class:el.className,rect:el.getBoundingClientRect().toJSON()}))}));
  assert.ok(bad.scroll<=bad.width,`${label}: horizontal document overflow ${JSON.stringify(bad)}`);
  assert.deepEqual(bad.outside,[],`${label}: clipped important controls`);
  for(const button of await page.locator('.aiPositionButtons button,.aiPositionPrepare,.aiPositionRelease').all()){
    assert.ok(await button.evaluate(el=>parseFloat(getComputedStyle(el).minHeight)>=52),`${label}: CSS touch target`);
    assert.ok((await button.boundingBox()).height>=51.99,`${label}: actual touch target`);
  }
}

(async()=>{
  const output=path.resolve(process.argv[2]||'../ui-audit-20260906-10');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const results=[];
  try{
    for(const width of [320,390])for(const language of ['ru','en']){
      const page=await browser.newPage({viewport:{width,height:844},reducedMotion:'reduce'});
      const data=fixture(),posts=[],errors=[],unexpected=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',async route=>{
        const request=route.request(),url=new URL(request.url());
        if(url.origin!=='https://offline-wallet.test'){
          unexpected.push(request.url());return route.abort();
        }
        if(url.pathname==='/api/ai'&&request.method()==='GET')return route.fulfill({json:data});
        if(url.pathname==='/api/ai/positions/synthetic-reduce/decision'&&request.method()==='POST'){
          const body=request.postDataJSON();posts.push({path:url.pathname,body});assert.deepEqual(body,{confirm:true});
          const row=data.position_actions.pending.find(row=>row.id==='synthetic-reduce');
          data.position_actions.pending=[];data.position_actions.status='IDLE';data.position_actions.reason='no_eligible_scenario';
          data.position_actions.history=[{...row,status:'FILLED',result:{filled_size:.0047,origin:'user_confirmed_position_intervention'}}];
          data.position_actions.holds=[{market:'ETH|',proposal_id:row.id,status:'FILLED',can_release:true}];
          return route.fulfill({json:{ok:true,status:'FILLED'}});
        }
        if(url.pathname==='/api/ai/positions/resume'&&request.method()==='POST'){
          const body=request.postDataJSON();posts.push({path:url.pathname,body});assert.deepEqual(body,{market:'ETH|'});
          data.position_actions.holds=[];
          return route.fulfill({json:{released:true,market:'ETH|'}});
        }
        if(url.pathname==='/'&&request.method()==='GET')return route.fulfill({contentType:'text/html',body:
          '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main><header><h1>WALLET HUNTER · AI</h1></header><section class="page active" id="ai"></section></main><nav><button data-page="ai">AI</button></nav></body></html>'});
        unexpected.push(`${request.method()} ${url.pathname}`);return route.fulfill({status:404,body:'Offline fixture missing'});
      });
      await page.goto('https://offline-wallet.test/');
      for(const file of ['style.css','ai.css'])await page.addStyleTag({path:path.join(assets,file)});
      await page.evaluate(lang=>{window.walletHunterLanguage=lang;window.walletHunterPrivacy=false;},language);
      for(const file of ['ai-modes.js','ai-learning.js','ai-user-orders.js','ai-position-actions.js','ai-review.js'])
        await page.addScriptTag({path:path.join(assets,file)});
      await page.evaluate(()=>window.walletHunterAiReview.open());
      const card=page.locator('.aiPositionActionsCard'),proposals=card.locator('.aiPositionProposal');
      assert.equal(await card.count(),1);assert.equal(await proposals.count(),2);
      assert.deepEqual(posts,[],'Opening the AI page must not send a decision');
      assert.equal(await card.locator('[data-position-confirm]:enabled').count(),2,'Both frozen synthetic forms must validate');
      assert.doesNotMatch(await proposals.first().locator('.aiPositionBeforeAfter').innerText(),/014100000000/,'Derived quantity must not expose binary float noise');
      assert.ok(await page.evaluate(()=>document.getElementById('aiPositionActions').compareDocumentPosition(document.getElementById('aiModes'))&Node.DOCUMENT_POSITION_FOLLOWING));
      await assertLayout(page,`${width}-${language}-closed`);
      if(language==='en')assert.doesNotMatch(await card.innerText(),/[А-Яа-яЁё]/,'Untranslated position text');
      await page.screenshot({path:path.join(output,`positions-full-${width}-${language}.png`),fullPage:true});
      await card.scrollIntoViewIfNeeded();
      await page.evaluate(()=>window.scrollTo(0,document.querySelector('.aiPositionActionsCard').getBoundingClientRect().top+scrollY-12));
      await page.screenshot({path:path.join(output,`positions-top-${width}-${language}.png`)});
      const details=proposals.first().locator('details').first();
      await details.locator('summary').click();
      assert.equal(await details.locator('dt').count(),8);
      assert.equal(await details.getAttribute('open'),'');
      await assertLayout(page,`${width}-${language}-expanded`);
      await details.screenshot({path:path.join(output,`positions-factors-${width}-${language}.png`)});
      assert.deepEqual(posts,[],'Expanding analysis must not send a decision');
      await page.evaluate(()=>{window.walletHunterPrivacy=true;window.dispatchEvent(new Event('whprivacy'));});
      assert.equal(await card.locator('[data-position-confirm]:enabled').count(),0);
      assert.equal(await card.locator('[data-position-decline]:enabled').count(),2);
      assert.ok(await card.locator('.maskedValue').count()>=20,'Quantity, margin, money and source allocation must be masked');
      assert.match(await card.innerText(),/ROE -53%/,'ROE percentage remains visible');
      assert.doesNotMatch(await card.locator('.aiPositionBeforeAfter').first().innerText(),/0[.,]0188|2[.,]36\s*USDC/);
      await assertLayout(page,`${width}-${language}-privacy`);
      await card.scrollIntoViewIfNeeded();
      await page.evaluate(()=>window.scrollTo(0,document.querySelector('.aiPositionBeforeAfter').getBoundingClientRect().top+scrollY-24));
      await page.screenshot({path:path.join(output,`positions-private-${width}-${language}.png`)});
      assert.deepEqual(posts,[],'Privacy changes must not send a decision');
      await page.evaluate(()=>{window.walletHunterPrivacy=false;window.dispatchEvent(new Event('whprivacy'));});
      await card.locator('[data-position-confirm="synthetic-reduce"]').click();
      await page.waitForFunction(()=>!document.querySelector('[data-position-confirm]'));
      assert.deepEqual(posts,[{path:'/api/ai/positions/synthetic-reduce/decision',body:{confirm:true}}]);
      await assertLayout(page,`${width}-${language}-confirmed`);
      assert.deepEqual(unexpected,[]);assert.deepEqual(errors,[]);
      await card.screenshot({path:path.join(output,`positions-result-${width}-${language}.png`)});
      const release=card.locator('[data-position-release="ETH|"]');
      assert.equal(await release.count(),1,'Verified HOLD must offer explicit reconciliation/resume');
      page.once('dialog',async dialog=>{assert.equal(dialog.type(),'confirm');assert.match(dialog.message(),/ETH/);await dialog.dismiss();});
      await release.click();
      assert.equal(posts.length,1,'Declining the resume warning must not submit');
      page.once('dialog',async dialog=>{assert.equal(dialog.type(),'confirm');await dialog.accept();});
      await release.click();
      await page.waitForFunction(()=>!document.querySelector('[data-position-release]'));
      assert.deepEqual(posts,[{path:'/api/ai/positions/synthetic-reduce/decision',body:{confirm:true}},
        {path:'/api/ai/positions/resume',body:{market:'ETH|'}}]);
      assert.deepEqual(unexpected,[]);assert.deepEqual(errors,[]);
      results.push({width,language,proposals:2,factors:8,touch_min_px:52,privacy:'passed',fake_decisions:1,fake_explicit_resumes:1,real_requests:0});
      await page.close();
    }
    const report={offline:true,results,overflow:false,page_errors:0,screenshots:output};
    fs.writeFileSync(path.join(output,'position-actions-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
