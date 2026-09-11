const statHeaders={'X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''};
async function showAccountStats(days){
  const r=await fetch(`/api/account/report?days=${days}`,{headers:statHeaders});
  const data=await r.json(); if(!r.ok){alert(data.detail||'Ошибка статистики');return}
  const money=x=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(x||0);
  const pf=data.profit_factor===Infinity?'∞':Number(data.profit_factor||0).toFixed(2);
  document.getElementById('accountStats').innerHTML=`<div class="card"><small>МОЯ СТАТИСТИКА · ${data.days} дн.</small><h3>${money(data.pnl)}</h3><p>Баланс ${money(data.balance)} · Экспозиция ${money(data.exposure)}</p><p>Сделок ${data.trades} · Win rate ${Number(data.win_rate||0).toFixed(1)}% · PF ${pf}</p><p>Max DD ${money(data.drawdown)} · Позиций ${data.positions.length}</p></div>`;
}
setInterval(()=>{const home=document.getElementById('home');if(!home||document.getElementById('accountStats'))return;home.insertAdjacentHTML('beforeend','<div id="accountStats"><div class="controls"><button class="toggle" onclick="showAccountStats(1)">📊 Сегодня</button><button class="toggle" onclick="showAccountStats(7)">📈 7 дней</button></div></div>')},300);
window.showAccountStats=showAccountStats;
