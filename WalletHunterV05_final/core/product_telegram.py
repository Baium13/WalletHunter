"""Thin notification/status consumer; failure cannot propagate to financial workers."""
import asyncio
from pathlib import Path
from core.product_runtime import view_for
from core.product_events import ProductEvents
from core.product_notifications import notice_text


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


def confirmation_notifications_enabled(root,uid,profile,network):
    if not profile.get('notifications',True):return False
    try:
        view=view_for(root,uid,profile,network)
        return ProductEvents(Path(root)/'data/product.sqlite3').preferences(view.scope)['preferences']['LIVE_CONFIRM']
    except (ValueError,OSError):return False


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
