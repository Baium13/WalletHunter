/* Slot three may be reserved for AI research. A reservation is not live trading. */
(() => {
  if (window.walletHunterAiSlot) return;
  window.walletHunterAiSlot = true;
  const text = (ru, en) => window.walletHunterLanguage === 'en' ? en : ru;
  const headers = () => ({'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || ''});
  let generation = 0, latest = null, changing = false;

  function render() {
    const page = document.getElementById('wallets');
    if (!page || !latest || !Array.isArray(latest.wallets)) return;
    const configured = latest.wallets.filter(wallet => wallet.configured).length;
    const selected = Boolean(latest.ai_slot_selected);
    const full = configured + (selected ? 1 : 0) >= 3;
    const cards = [...page.querySelectorAll('.card')];
    const form = cards.find(card => card.querySelector('#newWallet'));
    const input = form?.querySelector('#newWallet'), addButton = form?.querySelector('button');
    if (input && addButton) {
      input.disabled = full; addButton.disabled = full;
      addButton.dataset.selfLocalized = 'true';
      addButton.textContent = configured >= 3 ? text('Все 3 слота заняты', 'All 3 slots are used') : full ?
        text('Третий слот занят AI', 'Third slot is reserved by AI') : text('Добавить в свободный слот', 'Add to free slot');
      form.classList.toggle('slotFull', full);
    }
    const description = page.querySelector('.card p.muted');
    if (description) {
      description.dataset.selfLocalized = 'true';
      description.textContent = text('Каждому из 3 слотов выделяется 1/3 бюджета. Свободная доля и доля кошелька на паузе не передаются другим.', 'Each of the 3 slots has 1/3 of the budget. An empty or paused slot does not give its share to others.');
    }
    if (configured > 2) return;
    const card = cards.find(item => item.classList.contains('aiSlot')) || cards.find(item =>
      (item.textContent.includes('Свободный слот') || item.textContent.includes('Free slot')) &&
      (item.textContent.includes('Кошелёк 3') || item.textContent.includes('Wallet 3')));
    if (!card) return;
    card.classList.toggle('aiSlot', true);
    card.dataset.selfLocalized = 'true';
    card.innerHTML = `<b>${selected ? text('✦ AI · слот 3', '✦ AI · slot 3') : text('⚪ Свободный слот · Кошелёк 3', '⚪ Free slot · Wallet 3')}</b>` +
      `<p class="muted">${selected ? text('Доля 1/3 зарезервирована. Анализ и подготовка ордера — во вкладке AI.', 'A 1/3 share is reserved. Analysis and order preparation are in the AI tab.') : text('Этот слот можно зарезервировать для AI или добавить в него копируемый кошелёк.', 'Reserve this slot for AI or add a wallet to copy.')}</p>` +
      `<button type="button" class="${selected ? 'danger' : 'primary'}" id="aiSlotToggle" ${changing ? 'disabled' : ''}>${selected ? text('Убрать AI из слота', 'Remove AI from slot') : text('Выбрать AI', 'Select AI')}</button>` +
      (selected ? `<small>${text('Автоторговля не включается. Каждый реальный ордер требует отдельного подтверждения. Удаление слота не закрывает позиции.', 'No automatic trading. Every real order needs separate confirmation. Removing the slot does not close positions.')}</small>` : '');
    card.querySelector('#aiSlotToggle')?.addEventListener('click', () => change(!selected));
  }

  async function refresh() {
    const request = ++generation;
    try {
      const response = await fetch('/api/dashboard', {headers: headers(), cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if (request !== generation || !Array.isArray(data?.wallets)) return;
      latest = data; render();
    } catch (_) { /* Main dashboard already provides a retry/offline status. */ }
  }

  async function change(value) {
    if (changing) return;
    changing = true; render();
    try {
      const response = await fetch('/api/ai/slot', {method: 'POST', headers: {...headers(), 'Content-Type': 'application/json'}, body: JSON.stringify({value})});
      if (!response.ok) throw new Error('AI_SLOT_UNAVAILABLE');
      ++generation; latest = null;
      if (window.load) await window.load(); else await refresh();
    } catch (_) {
      alert(text('Не удалось изменить AI-слот. Обновите приложение и проверьте состояние перед повтором.', 'Could not update the AI slot. Refresh the app and check its state before retrying.'));
    } finally { changing = false; render(); }
  }
  const originalLoad = window.load;
  if (originalLoad) window.load = async () => { await originalLoad(); await refresh(); };
  document.querySelector('nav button[data-page="wallets"]')?.addEventListener('click', refresh);
  window.addEventListener('whlanguage', render);
  setTimeout(refresh, 350);
})();
