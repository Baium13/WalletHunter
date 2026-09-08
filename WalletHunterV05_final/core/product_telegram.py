"""Thin notification/status consumer; failure cannot propagate to financial workers."""
import asyncio
from pathlib import Path
from core.product_runtime import view_for
from core.product_events import ProductEvents


def status_text(snapshot,en=True):
    account=(snapshot.get('account') or {}).get('portfolio') or {}
    equity=account.get('equity');modes=snapshot['runtimes']
    unavailable='Unavailable' if en else 'Недоступно'
    return '\n'.join([
        'Wallet Hunter · '+snapshot['health']['status'],
        ('Mode: ' if en else 'Режим: ')+(', '.join(m.get('runtime_mode') or unavailable for m in modes) or unavailable),
        ('Account equity: ' if en else 'Капитал аккаунта: ')+(str(equity) if equity is not None else unavailable),
        ('Manual Copy: ' if en else 'Копирование: ')+snapshot['manual_copy']['status'],
        ('Positions: ' if en else 'Позиции: ')+str(sum(e['state'] not in {'CLOSED','REJECTED'} for m in modes for e in m.get('episodes',[]))),
        ('Active leaders: ' if en else 'Активные лидеры: ')+str((snapshot['discovery']['counts'] or {}).get('ACTIVE',unavailable)),
        ('Consensus: ' if en else 'Консенсус: ')+', '.join((m.get('consensus') or {}).get('decision',unavailable) for m in modes)])


def notice_text(event,identity,en=True):
    labels={'AUTHORIZATION_REQUIRED':('Confirmation required','Требуется подтверждение'),
        'EXECUTION_UPDATED':('Execution update','Обновление исполнения'),'OUTCOME_UPDATED':('Position outcome','Результат позиции'),
        'LEADER_PROMOTED':('Leader promoted','Лидер выбран'),'LEADER_DEGRADED':('Leader downgraded','Рейтинг лидера понижен'),
        'HEALTH_UPDATED':('System warning','Предупреждение системы')}
    kind=event['type'];title=labels.get(kind,(kind,kind))[0 if en else 1];data=event['data']
    if isinstance(data.get('evidence'),dict):data={**data,**data['evidence']}
    if isinstance(data.get('payload'),dict):data={**data,**data['payload']}
    fields=('mode','status','wallet','intent_id','decision_id','net_pnl')
    return title+'\n'+'\n'.join(f'{k}: {data[k]}' for k in fields if k in data and data[k] is not None)+'\nID: '+identity[:12]


async def product_notifications(root,storage,settings,client):
    service=ProductEvents(Path(root)/'data/product.sqlite3');offset=0
    while True:
        try:
            users=list(storage.load().get('profiles',{}))
            selected=(users[offset:offset+8] if offset<len(users) else users[:8]);offset=(offset+8)%max(1,len(users))
            for uid in selected:
                try:
                    _,profile=storage.profile(int(uid))
                    if not profile.get('account'):continue
                    view=view_for(root,uid,profile,settings.hl_mode)
                    await asyncio.to_thread(service.ingest,view)
                    snapshot=await asyncio.to_thread(view.snapshot)
                    await asyncio.to_thread(service.collect,snapshot)
                    if not profile.get('notifications',True):continue
                    async def send(event,identity):
                        await client.send_message(int(uid),notice_text(event,identity,profile.get('language')=='en'),parse_mode=None)
                    await service.deliver(view.scope,send,limit=1)
                except Exception:pass # Durable delivery errors remain in the outbox; no secrets logged.
        except Exception:pass
        await asyncio.sleep(10)
