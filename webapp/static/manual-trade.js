/* Deliberate manual controls for the user's own open positions only. */
let manualPosition=null;
const originalOpenChart=window.openChart;

function manualError(payload, fallback){return payload?.detail||fallback}
async function manualRequest(url, method, body){
  const response=await fetch(url,{method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw Error(manualError(data,'Не удалось выполнить действие'));
  return data;
}
function drawManualStop(){
  if(!manualPosition?.stop||!lastCandles?.length)return;
  const canvas=document.getElementById('priceChart');if(!canvas)return;
  const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1,w=Math.max(280,rect.width),h=300,axis=62;
  const values=lastCandles.flatMap(x=>[x.h,x.l,chartState.entry||x.l,manualPosition.stop]);
  let lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;
  const y=Math.max(12,Math.min(h-25,h-25-(manualPosition.stop-lo)/(hi-lo)*(h-42))),c=canvas.getContext('2d');
  c.save();c.setTransform(ratio,0,0,ratio,0,0);c.setLineDash([5,3]);c.lineWidth=2;c.strokeStyle='#ff7182';c.beginPath();c.moveTo(0,y);c.lineTo(w-axis,y);c.stroke();c.setLineDash([]);c.fillStyle='#ff7182';c.font='bold 12px system-ui';c.fillText('SL '+chartMoney(manualPosition.stop),10,Math.max(14,y-7));c.restore();
}
function mountManualControls(){
  const host=document.getElementById('chart');if(!host||!manualPosition?.own)return;
  host.querySelector('.manualTrade')?.remove();
  const panel=document.createElement('div');panel.className='card manualTrade';
  panel.innerHTML=`<b>РУЧНОЕ УПРАВЛЕНИЕ</b><div class="actions"><button class="danger" onclick="closePositionMarket()">Закрыть по рынку</button></div><label>Стоп-лосс<input id="stopLossPrice" type="number" inputmode="decimal" step="any" value="${manualPosition.stop||''}" placeholder="Цена стопа"></label><div class="actions"><button class="primary" onclick="saveStopLoss()">${manualPosition.stop?'Изменить SL':'Установить SL'}</button>${manualPosition.stop?'<button class="danger" onclick="removeStopLoss()">Удалить SL</button>':''}</div><small class="muted">Стоп — биржевой reduce-only ордер Hyperliquid.</small>`;
  host.append(panel);drawManualStop();
}
window.openChart=async function(coin,dex,entry,side,size=0,leverage=1,own=false,stop=0){
  manualPosition={coin,dex,entry:Number(entry)||0,side,size:Number(size)||0,leverage:Number(leverage)||1,own:Boolean(own),stop:Number(stop)||0};
  await originalOpenChart(coin,dex,entry,side,size,leverage,own);
  setTimeout(mountManualControls,0);
};
async function closePositionMarket(){
  if(!manualPosition?.own||!confirm(`Закрыть ${manualPosition.coin} ${manualPosition.side} по рынку? Стоп-лосс также будет удалён.`))return;
  try{await manualRequest('/api/position/close','POST',{coin:manualPosition.coin,dex:manualPosition.dex||''});manualPosition.stop=0;await window.load();await window.openChart(manualPosition.coin,manualPosition.dex,0,'MARKET')}catch(error){alert(error.message)}
}
async function saveStopLoss(){
  const price=Number(document.getElementById('stopLossPrice')?.value);if(!price)return alert('Введите цену стоп-лосса.');
  try{await manualRequest('/api/position/stop-loss','POST',{coin:manualPosition.coin,dex:manualPosition.dex||'',price});manualPosition.stop=price;await window.load();mountManualControls()}catch(error){alert(error.message)}
}
async function removeStopLoss(){
  if(!manualPosition?.stop||!confirm('Удалить стоп-лосс?'))return;
  try{await manualRequest('/api/position/stop-loss','DELETE',{coin:manualPosition.coin,dex:manualPosition.dex||''});manualPosition.stop=0;await window.load();mountManualControls()}catch(error){alert(error.message)}
}
Object.assign(window,{closePositionMarket,saveStopLoss,removeStopLoss,drawManualStop});
