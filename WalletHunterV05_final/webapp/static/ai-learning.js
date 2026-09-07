/* Shared public-market research model. Rendering only: no training/order API. */
(() => {
  if (window.whAiLearning) return;
  const en = () => window.walletHunterLanguage === 'en';
  const t = (ru, eng) => en() ? eng : ru;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const n = value => count(value) === null ? '—' : value.toLocaleString(en() ? 'en-GB' : 'ru-RU');
  const modelVersion = value => count(value) !== null && value > 0 ? String(value)
    : typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,80}$/.test(value) ? esc(value) : '—';
  const fixed = (value, max = Infinity) => finite(value) && value >= 0 && value <= max ? value.toLocaleString(en() ? 'en-GB' : 'ru-RU', {maximumFractionDigits: 4}) : '—';
  const when = value => finite(value) && value > 0 && Number.isFinite(new Date(value).getTime())
    ? esc(new Date(value).toLocaleString(en() ? 'en-GB' : 'ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'})) : '—';
  const key = value => typeof value === 'string' ? value.toUpperCase() : '';
  const statuses = {
    COLLECTING:['Сбор данных','Collecting data'], WAITING_DATA:['Ожидает данные','Awaiting data'],
    INSUFFICIENT_DATA:['Недостаточно данных','Insufficient data'], UNTRAINED:['Модель ещё не обучена','Model not trained yet'],
    TRAINING:['Обучение модели','Training model'], TRAINED:['Модель обучена · исследование','Model trained · research'],
    READY:['Модель для исследования','Research model available'], RESEARCH_ONLY:['Только исследование','Research only'],
    RESEARCH_MODEL:['Обученная модель · исследование','Trained model · research'],
    ACTIVE:['Исследование продолжается','Research in progress'], OK:['Данные обновлены','Data updated'],
    FETCHING:['Получение публичных свечей','Fetching public candles'], BUSY:['Обработка данных','Processing data'],
    NOT_STARTED:['Ещё не запущено','Not started'], WAITING_MODEL:['Ожидает модель','Awaiting model'],
    UNAVAILABLE:['Данные недоступны','Data unavailable'], ERROR:['Ошибка получения данных','Data retrieval error'],
    STALE:['Данные устарели','Data is stale'], PARTIAL_DATA:['Данные получены частично','Partial data'],
    PAUSED:['На паузе','Paused']
  };
  const reasons = {
    NOT_STARTED:['Сбор и обучение ещё не начались.','Collection and training have not started yet.'],
    INSUFFICIENT_DATA:['Пока недостаточно подготовленных примеров для обучения.','There are not enough prepared examples for training yet.'],
    INSUFFICIENT_MATURED_HISTORY:['Недостаточно исторических примеров с уже известным исходом.','Not enough historical examples with known outcomes yet.'],
    PUBLIC_RULES_FEATURES_LOGISTIC_RESEARCH_ONLY:['Логистическая модель обучается на публичных рыночных признаках. Только исследование.','The logistic model learns from public market features. Research only.'],
    NO_MODEL:['Обученной версии пока нет.','No trained version is available yet.'],
    WAITING_DATA:['Ожидаются подтверждённые рыночные данные.','Awaiting verified market data.'],
    MARKET_DATA_UNAVAILABLE:['Свежие рыночные данные недоступны. Старые результаты сохранены, но не подтверждают текущую работу.','Fresh market data is unavailable. Earlier results are retained but do not confirm current operation.'],
    PUBLIC_DATA_UNAVAILABLE:['Источник публичных данных временно недоступен.','The public data source is temporarily unavailable.'],
    STALE_DATA:['Нет свежей подтверждённой проверки.','No fresh verified check is available.'],
    COLLECTOR_ERROR:['Ошибка сборщика данных. Личные данные и текст системной ошибки не отображаются.','Data collector error. Personal data and raw system errors are not displayed.'],
    PUBLIC_HISTORY_UNAVAILABLE_OR_INVALID:['Публичная история недоступна или не прошла проверку. Исторические метрики ниже сохранены от предыдущих проверок.','Public history is unavailable or failed validation. Historical metrics below are retained from earlier evaluations.'],
    TRAINING_FAILED:['Последнее обучение не завершилось. Сохранённая версия и исторические метрики не означают успешное обновление.','The latest training run did not complete. A saved version and historical metrics do not mean a successful update.'],
    UNVALIDATED_MODEL:['Модель ещё не подтверждена для реального исполнения.','The model is not validated for real execution.']
  };
  const label = (map, value, fallback) => t(...(Object.hasOwn(map, key(value)) ? map[key(value)] : fallback));
  const status = value => label(statuses, value, ['Состояние не подтверждено','State unconfirmed']);
  const reason = value => label(reasons, value, ['Подробности пока недоступны.','Details are not available yet.']);

  function metricTable(raw, expected) {
    const data = obj(raw), validKind = data.kind === expected;
    const retrospective = expected === 'RETROSPECTIVE_BOOTSTRAP';
    const populated = validKind && count(data.groups) > 0 && count(data.samples) > 0;
    const value = (field, max = Infinity) => populated ? fixed(data[field], max) : '—';
    const accuracy = populated && finite(data.accuracy) && data.accuracy >= 0 && data.accuracy <= 1
      ? `${fixed(data.accuracy * 100, 100)}%` : '—';
    return `<div class="aiLearningTest"><h4>${retrospective ? t('Проверка на истории','Historical test') : t('Проверка новых прогнозов','Forward test')}</h4>` +
      `<p class="muted">${retrospective ? t('Проверка начальной модели на отложенном историческом периоде. Это не результат будущих прогнозов и не доходность торговли.', 'Initial-model evaluation on a held-out historical period. Not the result of future predictions or trading returns.') : t('Только прогнозы, записанные до появления результата. Нужны завершённые наблюдения.', 'Only predictions recorded before their outcome was known. Completed observations are required.')}</p>` +
      (retrospective ? `<small>${t('Проверенная версия','Evaluated version')}: ${modelVersion(data.model_version)}</small>` : '') +
      (!retrospective && data.across_model_versions === true ? `<p class="muted">${t('Общий результат прогнозов разных версий модели, не только последней.', 'Aggregated results from predictions made by different model versions, not only the latest.')}</p>` : '') +
      (!retrospective && count(data.sample_window_limit) > 0 ? `<small>${t('Метрики по последним примерам, не более','Metrics use the most recent samples, at most')} ${n(data.sample_window_limit)}.</small>` : '') +
      `<div class="aiLearningCounts"><span>${t('Группы','Groups')} <b>${validKind ? n(data.groups) : '—'}</b></span>` +
      `<span>${t('Примеры','Samples')} <b>${validKind ? n(data.samples) : '—'}</b></span></div>` +
      `<table class="aiLearningMetrics"><thead><tr><th scope="col">${t('Метрика','Metric')}</th><th scope="col">${t('Модель','Model')}</th><th scope="col">${t('База','Baseline')}</th></tr></thead><tbody>` +
      `<tr><th scope="row">Brier ↓</th><td>${value('brier', 1)}</td><td>${value('baseline_brier', 1)}</td></tr>` +
      `<tr><th scope="row">Log loss ↓</th><td>${value('log_loss')}</td><td>${value('baseline_log_loss')}</td></tr></tbody></table>` +
      `<p class="muted">${t('Точность знака','Sign accuracy')}: <b>${accuracy}</b> · ${t('не доля прибыльных сделок','not a profitable-trade rate')}</p>` +
      (!populated ? `<small>${t('Проверяемых результатов пока недостаточно. Прочерк не означает нулевую ошибку.', 'There are not enough verifiable outcomes yet. A dash does not mean zero error.')}</small>` : '') + '</div>';
  }

  function render(raw) {
    const data = obj(raw), counts = obj(data.counts), model = obj(data.model), collector = obj(data.collector);
    const collectorProblem = ['UNAVAILABLE','ERROR','STALE','PARTIAL_DATA'].includes(key(collector.status));
    const currentStatus = collectorProblem ? collector.status : data.status;
    const collectorError = (Array.isArray(collector.errors) ? collector.errors : []).map(value => obj(value).reason)
      .find(value => ['PUBLIC_HISTORY_UNAVAILABLE_OR_INVALID','TRAINING_FAILED'].includes(key(value)));
    const currentReason = collectorProblem ? (collector.error_code || collectorError || (key(collector.status) === 'STALE' ? 'stale_data' : 'market_data_unavailable')) : data.reason;
    const version = modelVersion(model.version);
    const features = Array.isArray(model.features) ? model.features.length : count(model.features);
    return `<section class="card aiLearningCard" data-self-localized="true" aria-labelledby="aiLearningTitle">` +
      `<div class="aiLearningHeading"><h3 id="aiLearningTitle">${t('Обучение модели','Model learning')}</h3><span class="aiModeBadge">${t('ИССЛЕДОВАНИЕ','RESEARCH')}</span></div>` +
      `<p>${t('Общая логистическая модель по публичным данным BTC и ETH. Отдельно от правил виртуального трейдера.', 'Shared logistic model using public BTC and ETH data. Separate from the virtual trader’s rules.')}</p>` +
      `<p class="aiLearningState"><b>${status(currentStatus)}</b>` +
      (currentReason ? `<small>${reason(currentReason)}</small>` : '') + '</p>' +
      `<div class="aiModeMetrics"><span>${t('Свечи / примеры','Candles / examples')}<b>${n(counts.candles)} / ${n(counts.examples)}</b></span>` +
      `<span>${t('Группы обучения / теста','Train / test groups')}<b>${n(counts.train_groups)} / ${n(counts.test_groups)}</b></span>` +
      `<span>${t('Прогнозов вперёд','Forward predictions')}<b>${n(counts.forward_predictions)}</b></span>` +
      `<span>${t('Проверено вперёд','Forward outcomes checked')}<b>${n(counts.forward_matured)}</b></span></div>` +
      `<p class="aiLearningVersion">${t('Версия модели','Model version')}: <b>${version}</b><br>` +
      `${t('Обучена на данных до','Trained on data through')}: ${when(model.trained_through_ms)}<br>` +
      `${t('Сбор данных','Data collection')}: ${status(collector.status)}<br>` +
      `${t('Последние свежие данные','Last successful data check')}: ${when(collector.last_success_ms)}</p>` +
      `<details class="aiModeHistory"><summary>${t('Качество модели и ограничения','Model quality and limitations')}</summary>` +
      `<p class="muted">${t('Создана','Created')}: ${when(model.created_ms)} · ${t('Признаков','Features')}: ${n(features)} · ${t('Примеров обучения','Training samples')}: ${n(model.train_samples)}</p>` +
      metricTable(data.validation, 'RETROSPECTIVE_BOOTSTRAP') + metricTable(data.forward, 'FORWARD_PREQUENTIAL') +
      `<p class="muted">${t('Brier и Log loss измеряют ошибку вероятностного прогноза: ниже — лучше. Сравнивайте с простой базовой моделью. Исторический тест и будущая проверка показаны отдельно.', 'Brier and Log loss measure probability-prediction error: lower is better. Compare against the simple baseline. Historical and forward evaluation are shown separately.')}</p>` +
      `<p class="muted">${t('Выход модели пока не откалиброван как надёжная вероятность. Он не означает «60% выигрышных сделок». Прогнозы не гарантируют прибыль.', 'Model output is not yet calibrated as a reliable probability. It does not mean “60% winning trades”. Predictions do not guarantee profit.')}</p></details>` +
      `<p class="aiModeWarning">${t('Только исследование. Модель не отправляет ордера автоматически. Счётчики и хорошая метрика не включают торговлю автоматически. Личные средства и API-ключи здесь не используются.', 'Research only. The model does not send orders automatically. Counts or a good metric never enable trading automatically. Personal funds and API keys are not used here.')}</p></section>`;
  }
  window.whAiLearning = {render};
})();
