/* AI occupies a source slot only for shadow research; it cannot send orders. */
(() => {
  const headers = () => ({"X-Telegram-Init-Data": window.Telegram?.WebApp?.initData || ""});
  async function dashboard() {
    const response = await fetch("/api/dashboard", {headers: headers()});
    return response.ok ? response.json() : null;
  }
  async function renderAiSlot() {
    const page = document.getElementById("wallets");
    if (!page) return;
    document.getElementById("aiSlotCard")?.remove();
    const data = await dashboard();
    if (!data || data.wallets.filter(wallet => wallet.configured).length !== 2) return;
    const selected = Boolean(data.ai_slot_selected);
    const card = document.createElement("div");
    card.id = "aiSlotCard";
    card.className = "card aiSlot";
    card.innerHTML = `<b>✦ AI · слот 3</b><p class="muted">${selected ? "⚪ Неактивен · обучение и виртуальные сценарии" : "Свободный третий слот можно отдать AI."}</p><button class="${selected ? "danger" : "primary"}" id="aiSlotToggle">${selected ? "Убрать AI из слота" : "Выбрать AI"}</button><small>«Активен» недоступен: AI не открывает реальные сделки.</small>`;
    const add = page.querySelector(".card:last-child");
    (add || page).before(card);
    card.querySelector("#aiSlotToggle").addEventListener("click", async () => {
      try {
        const response = await fetch("/api/ai/slot", {method: "POST", headers: {...headers(), "Content-Type": "application/json"}, body: JSON.stringify({value: !selected})});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || "Не удалось изменить AI-слот.");
        await window.load?.();
      } catch (error) { alert(error.message || "Ошибка AI-слота."); }
    });
  }
  const originalLoad = window.load;
  if (originalLoad) window.load = async () => { await originalLoad(); await renderAiSlot(); };
  window.addEventListener("DOMContentLoaded", () => setTimeout(renderAiSlot, 250));
})();
