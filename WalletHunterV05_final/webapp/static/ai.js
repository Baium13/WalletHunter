/* One entry point, including clicks made before the review module has loaded. */
(() => {
  if (window.walletHunterAiLoader) return;
  window.walletHunterAiLoader = true;
  let pendingScript = null;
  let navigation = 0;
  const text = (ru, en) => window.walletHunterLanguage === 'en' ? en : ru;

  function renderLoader(kind) {
    const root = document.getElementById('ai');
    if (!root || !root.classList.contains('active')) return;
    root.dataset.aiLoaderState = kind;
    root.textContent = kind === 'loading' ? text('Загрузка анализа…', 'Loading review…') :
      text('Не удалось загрузить AI. Нажмите вкладку AI, чтобы повторить.', 'AI could not load. Tap the AI tab to retry.');
    root.setAttribute('aria-busy', String(kind === 'loading'));
    const title = document.getElementById('title');
    if (title) title.textContent = text('AI‑ассистент', 'AI assistant');
  }

  function activate() {
    const root = document.getElementById('ai');
    if (!root) return null;
    root.dataset.selfLocalized = 'true';
    document.querySelectorAll('.page').forEach(page => page.classList.toggle('active', page.id === 'ai'));
    document.querySelectorAll('nav button').forEach(button => button.classList.toggle('active', button.dataset.page === 'ai'));
    const title = document.getElementById('title');
    if (title) title.textContent = text('AI‑ассистент', 'AI assistant');
    return root;
  }

  function loadModule() {
    if (window.walletHunterAiReview) return Promise.resolve(window.walletHunterAiReview);
    if (pendingScript) return pendingScript;
    pendingScript = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/ai-review.js?v=20260906-11';
      let done = false;
      const finish = success => {
        if (done) return;
        done = true;
        clearTimeout(timeout);
        script.onload = script.onerror = null;
        if (success && window.walletHunterAiReview) resolve(window.walletHunterAiReview);
        else { script.remove(); reject(new Error('AI_MODULE_UNAVAILABLE')); }
      };
      const timeout = setTimeout(() => finish(false), 15000);
      script.onload = () => finish(true);
      script.onerror = () => finish(false);
      document.body.appendChild(script);
    }).catch(error => { pendingScript = null; throw error; });
    return pendingScript;
  }

  async function openAi() {
    const request = ++navigation;
    const root = activate();
    if (!root) return;
    renderLoader('loading');
    try {
      const module = await loadModule();
      if (request !== navigation || !root.classList.contains('active')) return;
      delete root.dataset.aiLoaderState;
      await module.open();
    } catch (_) {
      if (request !== navigation || !root.classList.contains('active')) return;
      renderLoader('error');
    }
  }
  window.openAi = openAi;
  window.addEventListener('whlanguage', () => {
    const kind = document.getElementById('ai')?.dataset.aiLoaderState;
    if (kind) renderLoader(kind);
  });
  document.querySelectorAll('nav button').forEach(button => button.addEventListener('click', () => {
    if (button.dataset.page === 'ai') openAi();
    else { ++navigation; window.walletHunterAiReview?.hide(); }
  }));
  if (window.location?.hash === '#ai-position') openAi();
})();
