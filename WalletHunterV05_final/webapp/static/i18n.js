(() => {
  const pairs = {
    "Главная":"Home", "Кошельки":"Wallets", "Анализ":"Analyse", "График":"Chart", "Настройки":"Settings", "Мой баланс":"My balance",
    "Открытые позиции":"Open positions", "Нет открытых позиций.":"No open positions.", "МОЯ СТАТИСТИКА":"MY STATS", "События":"Events",
    "Сегодня":"Today", "дней":"days", "Контроль рынков":"Market control", "Уведомления":"Notifications", "Исполнение":"Execution",
    "Риск":"Risk", "Стратегия":"Strategy", "Максимальное плечо":"Max leverage", "Сохранить параметры":"Save settings",
    "Мой Hyperliquid":"My Hyperliquid", "Сохранить аккаунт":"Save account", "Остановить":"Stop", "Запустить":"Start",
    "копирование":"copy", "Активен":"Active", "Пауза":"Paused", "Удалить":"Delete", "Добавить кошелёк":"Add wallet",
    "Анализ любого кошелька":"Analyse any wallet", "Проверить за 90 дней":"Check 90 days", "ДОБАВЛЕННЫЕ КОШЕЛЬКИ":"SAVED WALLETS",
    "Ввести адрес кошелька Hyperliquid":"Enter Hyperliquid wallet address", "Сделок":"Trades", "Баланс":"Balance", "Открытых позиций нет.":"No open positions.",
    "ТЕКУЩАЯ СТРАТЕГИЯ":"CURRENT STRATEGY", "Распределение":"Allocation", "Не удалось загрузить":"Could not load", "Повторить":"Retry",
    "Загружаю статистику…":"Loading statistics…", "Экспозиция":"Exposure", "Макс. плечо":"Max leverage", "Кошелёк":"Wallet",
    "Найти BTC, NVDA, HYPE…":"Search BTC, NVDA, HYPE…", "Точка входа":"Entry", "Размер свечи":"Candle size", "Период просмотра":"View range",
    "Текущая цена":"Live price", "Последняя цена":"Last price", "свечей":"candles", "Инструмент не найден.":"Market not found.",
    "ПРОВЕРКА · 90 ДНЕЙ":"CHECK · 90 DAYS", "допущен":"eligible", "не допущен":"not eligible", "Follower backtest":"Follower backtest",
    "Мой баланс":"My balance", "МОЙ БАЛАНС":"MY BALANCE", "Аккаунт не подключён":"Account not connected", "Проверка":"Check",
    "КОШЕЛЬКИ ДЛЯ КОПИРОВАНИЯ":"COPY WALLETS", "Бюджет распределяется между активными кошельками. Кошелёк на паузе не получает новые сделки.":"Budget is split across active wallets. Paused wallets receive no new trades.",
    "Свободный слот":"Free slot", "Добавьте публичный 0x-адрес ниже.":"Add a public 0x address below.", "Добавить в свободный слот":"Add to free slot",
    "Адрес не добавляется в отслеживание.":"This address is not added to tracking.", "Открытые позиции (":"Open positions (",
    "Crypto":"Crypto", "Уведомления":"Alerts", "Консервативный":"Conservative", "Стандартный":"Standard", "Агрессивный":"Aggressive",
    "Ключ хранится в зашифрованном виде. После сохранения копирование будет на паузе.":"The key is encrypted. Copying pauses after saving.",
    "Название аккаунта":"Account name", "Название":"Name", "Неверный адрес кошелька.":"Invalid wallet address.", "Введите адрес кошелька.":"Enter a wallet address.",
    "Собираю сделки и позиции…":"Loading trades and positions…", "Введите адрес.":"Enter an address.", "Ошибка запроса":"Request failed",
    "Закрыть только позиции, открытые ботом? Это действие нельзя отменить.":"Close bot-managed positions only? This cannot be undone.",
    "Остановить копирование и отменить активные ордера? Открытые позиции сохранятся.":"Stop copying and cancel active orders? Open positions remain.",
    "Отключить Hyperliquid-аккаунт? Копирование будет остановлено, ключ удалён с сервера.":"Disconnect Hyperliquid? Copying stops and the key is deleted from the server.",
    "Отключить аккаунт и удалить ключ":"Disconnect account & delete key", "AI‑ассистент":"AI Assistant", "Обучение активно":"Learning active",
    "AI ведёт виртуальные сценарии и пока не открывает реальные ордера.":"AI runs virtual scenarios and cannot place real orders yet.",
    "Загружаю историю…":"Loading history…", "наблюдений":"observations", "Виртуальных предложений":"Virtual proposals",
    "Накопление данных активно. Реальные сделки AI пока недоступны.":"Data collection is active. AI real trading is not available.",
    "Предложенные ордера":"Suggested orders", "уверенность":"confidence", "Статус":"Status", "Виртуально отслеживается":"Tracked virtually",
    "Пока нет виртуальных сигналов.":"No virtual signals yet.", "Ошибка AI":"AI error", "Все 3 слота заняты":"All 3 slots are used",
    "Третий слот занят AI":"Third slot is reserved by AI", "Доля 1/3 зарезервирована":"1/3 share reserved",
    "Бюджет делится поровну между занятыми слотами. Пауза не перераспределяет долю и только прекращает новые копирования.":"Budget is split equally across used slots. Pause only stops new copies.",
    "⚪ Свободный слот · Кошелёк 3":"⚪ Free slot · Wallet 3", "✦ AI · слот 3":"✦ AI · slot 3",
    "Этот слот можно использовать для копируемого кошелька или AI.":"Use this slot for a copy wallet or AI.",
    "Убрать AI из слота":"Remove AI", "Выбрать AI":"Select AI", "«Активен» недоступен: AI не открывает реальные сделки.":"Active is unavailable: AI does not place real trades.",
    "Не удалось изменить AI-слот.":"Could not update AI slot.", "Ошибка AI-слота.":"AI slot error.",
    "Вход ":"Entry ", "РЫНОК":"MARKET", "Загружаю свечи…":"Loading candles…", "1ч":"1h", "1д":"1d", "1н":"1w", "1м":"1mo",
    "Скрыть суммы":"Hide amounts", "Выключить звук":"Mute sound", "Включить звук":"Enable sound",
    "Событие":"Event", "0x адрес кошелька Hyperliquid":"0x Hyperliquid wallet address", "Добавить кошелёк":"Add wallet"
  };
  const reverse = Object.fromEntries(Object.entries(pairs).map(([ru,en])=>[en,ru]));
  let applying = false;
  const language = () => window.walletHunterLanguage || localStorage.getItem("wh_lang") || "ru";
  const translateText = value => {
    const table = language() === "en" ? pairs : reverse;
    return Object.entries(table).sort((a,b)=>b[0].length-a[0].length).reduce((text,[from,to])=>text.split(from).join(to), value);
  };
  function apply(root=document.body) {
    if (applying || !root) return;
    applying=true;
    document.body.dataset.lang=language();
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:n=>n.parentElement?.closest("script,style,canvas,[data-self-localized]")?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
    const nodes=[]; while(walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(node=>{const translated=translateText(node.nodeValue);if(translated!==node.nodeValue)node.nodeValue=translated});
    document.querySelectorAll("input[placeholder],button[title],button[aria-label]").forEach(el=>{if(el.closest('[data-self-localized]'))return;["placeholder","title","aria-label"].forEach(attr=>{if(el.hasAttribute(attr))el.setAttribute(attr,translateText(el.getAttribute(attr)))})});
    const button=document.getElementById("languageToggle"), label=language()==="en"?"EN":"RU";
    // Do not recreate the label if it did not change: that would re-trigger
    // the page-wide mutation observer forever and freeze navigation.
    if(button && button.textContent!==label) button.textContent=label;
    applying=false;
  }
  async function toggleLanguage() {
    const next=language()==="en"?"ru":"en";
    const headers={"Content-Type":"application/json","X-Telegram-Init-Data":window.Telegram?.WebApp?.initData||""};
    const response=await fetch("/api/settings",{method:"PUT",headers,body:JSON.stringify({language:next})});
    if(!response.ok) return alert("Language setting could not be saved.");
    window.walletHunterLanguage=next; localStorage.setItem("wh_lang",next); apply(); window.dispatchEvent(new Event("whlanguage"));
  }
  window.toggleLanguage=toggleLanguage;
  window.addEventListener("whlanguage",()=>apply());
  new MutationObserver(records=>{if(!applying&&records.some(r=>r.addedNodes.length))apply()}).observe(document.documentElement,{childList:true,subtree:true});
  setTimeout(apply,100);
})();
