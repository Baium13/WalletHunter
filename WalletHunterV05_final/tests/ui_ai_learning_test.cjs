/* Public research renderer only. No network or private trading data required. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../webapp/static/ai-learning.js'),'utf8');
function fixture() {
  const context = {walletHunterLanguage:'ru',walletHunterPrivacy:false}; context.window = context;
  vm.createContext(context); vm.runInContext(source,context);
  return {context,render:raw=>context.whAiLearning.render(raw)};
}
function data() {
  return {status:'RESEARCH_MODEL',reason:'public_rules_features_logistic_research_only',real_execution_available:false,
    counts:{candles:1000,examples:200,train_groups:70,test_groups:30,forward_predictions:20,forward_matured:10,model_versions:2},
    model:{version:'logistic-2',created_ms:1788796800000,trained_through_ms:1788795000000,
      features:['ema','macd','rsi','atr','volume','levels','funding','oi'],train_samples:140},
    validation:{kind:'RETROSPECTIVE_BOOTSTRAP',groups:30,samples:60,brier:.2345,log_loss:.621,
      accuracy:.55,baseline_brier:.25,baseline_log_loss:.693},
    forward:{kind:'FORWARD_PREQUENTIAL',groups:5,samples:10,brier:.2456,log_loss:.631,accuracy:.6,
      baseline_brier:.25,baseline_log_loss:.693,across_model_versions:true},
    collector:{status:'OK',last_success_ms:1788796800000},
    predictions:[{coin:'BTC',direction:'LONG',probability_positive_net:.91,status:'PENDING'}]};
}

test('learning card explains real model fitting separately from rule trader and trading readiness',()=>{
  const f=fixture(),html=f.render(data());
  assert.match(html,/Обучение модели/); assert.match(html,/Общая логистическая модель/);
  assert.match(html,/Отдельно от правил виртуального трейдера/);
  assert.match(html,/Обученная модель · исследование/); assert.match(html,/logistic-2/);
  assert.match(html,/Модель не отправляет ордера автоматически/);
  assert.match(html,/не включают торговлю автоматически/);
  assert.doesNotMatch(html,/91%|data-decision|data-ai-mode|<button|<input|<form/);
});

test('all new blocks and available/error labels are English when selected',()=>{
  const f=fixture();f.context.walletHunterLanguage='en';
  for(const value of [data(),null,{status:'WAITING_DATA',reason:'insufficient_matured_history'},
    {...data(),collector:{status:'UNAVAILABLE',error_code:'market_data_unavailable'}}]){
    const html=f.render(value);
    assert.doesNotMatch(html,/[А-Яа-яЁё]/);assert.match(html,/Model learning/);
    assert.match(html,/Research only/);assert.match(html,/not yet calibrated/);
  }
});

test('empty untrained or malformed data does not invent zero samples metrics or profits',()=>{
  const f=fixture();
  for(const value of [null,{},[],{counts:{candles:false,examples:'0'},model:null}]){
    const html=f.render(value);
    assert.match(html,/Версия модели: <b>—/);assert.match(html,/<td>—<\/td>/);
    assert.doesNotMatch(html,/<td>0(?:[.,]0+)?<\/td>|>0%<|undefined|NaN|Infinity|USDC|\$/);
  }
  const waiting={status:'WAITING_DATA',reason:'insufficient_matured_history',counts:{candles:0,examples:0},model:null};
  assert.match(f.render(waiting),/Недостаточно исторических примеров/);
  assert.match(f.render(waiting),/Свечи \/ примеры<b>0 \/ 0/); // real reported zero, not fabricated
});

test('historical and forward metrics are explicitly separate and compared against baseline',()=>{
  const f=fixture(),value=data();
  value.model.version=2;value.validation.model_version=1;value.forward.sample_window_limit=10000;
  const html=f.render(value);
  assert.match(html,/Проверка на истории/);assert.match(html,/Проверка новых прогнозов/);
  assert.match(html,/отложенном историческом периоде/);assert.match(html,/до появления результата/);
  assert.match(html,/разных версий модели/);assert.match(html,/ниже — лучше/);
  assert.match(html,/0,2345/);assert.match(html,/0,2456/);assert.match(html,/0,693/);
  assert.equal((html.match(/<table class="aiLearningMetrics">/g)||[]).length,2);
  assert.match(html,/не доля прибыльных сделок/);
  assert.match(html,/Версия модели: <b>2<\/b>/);assert.match(html,/Проверенная версия: 1/);
  assert.match(html,/Метрики по последним примерам, не более/);
});

test('no observed outcomes means no score even when misleading zero metrics are supplied',()=>{
  const f=fixture(),value=data();
  for(const validation of [
    {kind:'RETROSPECTIVE_BOOTSTRAP',groups:0,samples:0,brier:0,log_loss:0,accuracy:1},
    {kind:'FAKE',groups:30,samples:60,brier:0,log_loss:0,accuracy:1},
    {kind:'RETROSPECTIVE_BOOTSTRAP',groups:true,samples:60,brier:0,log_loss:0,accuracy:1}
  ]){
    value.validation=validation;value.forward=null;
    const html=f.render(value);assert.doesNotMatch(html,/<td>0<\/td>|100%/);
    assert.match(html,/Прочерк не означает нулевую ошибку/);
  }
});

test('untrusted metadata and unexpected private fields never become markup or visible secrets',()=>{
  const f=fixture(),value=data();
  value.status='<script>bad()</script>';value.reason='PRIVATE_ERROR';
  value.model.version='<img src=x onerror="bad()">';value.model.features=['SECRET_FEATURE'];
  value.account={private_key:'SECRET_KEY',balance:99112233};value.private_key='TOP_SECRET';
  value.predictions=[{coin:'<img src=x>',probability_positive_net:1,account:'PRIVATE_ADDRESS'}];
  value.validation.brier='<script>bad()</script>';value.collector={status:'ERROR',error_code:'SECRET_CODE',error:'SECRET_DETAIL'};
  const html=f.render(value);
  assert.doesNotMatch(html,/<script|<img|PRIVATE|SECRET|99112233|onerror/);
  assert.match(html,/Ошибка получения данных/);assert.match(html,/Подробности пока недоступны/);
});

test('collector unavailable or stale status takes precedence over a trained model while preserving history',()=>{
  const f=fixture(),value=data();
  for(const status of ['UNAVAILABLE','STALE','PARTIAL_DATA','ERROR']){
    value.collector={status,last_success_ms:1788796800000,error_code:'market_data_unavailable'};
    const html=f.render(value);
    assert.doesNotMatch(html,/aiLearningState"><b>Обученная модель/);
    assert.match(html,/Старые результаты сохранены/);assert.match(html,/logistic-2/);
    assert.match(html,/0,2345/); // historical evaluation stays identifiable as history
  }
  value.collector={status:'PARTIAL_DATA',errors:[{coin:'BTC',reason:'training_failed',detail:'SECRET'}]};
  assert.match(f.render(value),/Последнее обучение не завершилось/);
  value.collector={status:'UNAVAILABLE',errors:[{reason:'public_history_unavailable_or_invalid'}]};
  assert.match(f.render(value),/Исторические метрики ниже сохранены/);
  value.collector={status:'STALE'};assert.match(f.render(value),/Нет свежей подтверждённой проверки/);
  f.context.walletHunterLanguage='en';value.collector={status:'FETCHING'};
  assert.match(f.render(value),/Fetching public candles/);assert.doesNotMatch(f.render(value),/[А-Яа-яЁё]/);
});

test('privacy does not change shared public learning metrics or expose account information',()=>{
  const f=fixture(),value=data(),visible=f.render(value);
  f.context.walletHunterPrivacy=true;
  assert.equal(f.render(value),visible);
  assert.doesNotMatch(visible,/maskedValue|USDC|private_key|margin_usdc|notional_usdc/);
});

test('renderer has no request actions and is inserted between modes and research',()=>{
  const f=fixture();assert.deepEqual(Object.keys(f.context.whAiLearning),['render']);
  assert.doesNotMatch(source,/fetch\(|\/api\/|XMLHttpRequest|\.api\(|addEventListener|data-decision/);
  const review=fs.readFileSync(path.join(__dirname,'../webapp/static/ai-review.js'),'utf8');
  assert.ok(review.indexOf('window.whAiModes.render(data)')<review.indexOf('window.whAiLearning.render(data.learning)'));
  assert.ok(review.indexOf('window.whAiLearning.render(data.learning)')<review.indexOf('body += researchCard(data.research)'));
});
