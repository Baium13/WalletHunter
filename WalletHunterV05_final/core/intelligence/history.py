"""Durable incremental public history, preserving incomplete coverage.

Only a complete trailing retrieval establishes coverage. Partial pages are
checkpointed but never returned as a complete analysis. Old completed evidence
is retained when an exchange retention ceiling prevents a newer gap proof.
"""
import json
from core.fill_history import HistoryIncomplete,_key,RESPONSE_CAP,RETENTION_CAP


class IncrementalHistory:
    def __init__(self,store,network):
        self.store,self.network=store,network
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS public_history(network TEXT,wallet TEXT,kind TEXT,body TEXT,PRIMARY KEY(network,wallet,kind))')

    def _save(self,address,kind,state):
        with self.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO public_history VALUES(?,?,?,?)',
                (self.network,address,kind,json.dumps(state,sort_keys=True,allow_nan=False)))

    def fetch(self,info,address,start,now,max_requests,kind):
        with self.store.transaction() as db:
            row=db.execute('SELECT body FROM public_history WHERE network=? AND wallet=? AND kind=?',(self.network,address,kind)).fetchone()
        state=json.loads(row[0]) if row else {'complete':None,'pending':None}
        completed=state['complete'];pending=state['pending']
        if completed and now<completed['end']:raise HistoryIncomplete('CLOCK_REGRESSION')
        if pending is None:
            lo=max(start,completed['end']-60000) if completed and completed['start']<=start else start
            pending={'start':lo,'end':now,'work':[[lo,now]],'fills':{},'gaps':[]}
            state['pending']=pending
        elif now>pending['end']:
            pending['work'].append([pending['end']+1,now]);pending['end']=now
        state.update(reason='GAP_PENDING',updated_ms=now)
        self._save(address,kind,state)
        used=0
        try:
            while pending['work']:
                if used>=max_requests:raise HistoryIncomplete('GAP_PENDING:REQUEST_BUDGET')
                lo,hi=pending['work'][-1];used+=1
                try:rows=info({'type':'userFillsByTime','user':address,'startTime':lo,'endTime':hi,'aggregateByTime':False})
                except Exception as exc:
                    from core.hl_budget import BudgetUnavailable
                    raise HistoryIncomplete('BUDGET_DEFERRED' if isinstance(exc,BudgetUnavailable) or str(exc)=='RESOURCE_BUDGET' else 'TRANSPORT_FAILURE') from exc
                if not isinstance(rows,list) or len(rows)>RESPONSE_CAP:raise HistoryIncomplete('INVALID_RESPONSE')
                valid=[(_key(f,lo,hi),f) for f in rows]
                if len(rows)==RESPONSE_CAP:
                    if lo==hi:raise HistoryIncomplete('RESPONSE_CAP:SAME_TIMESTAMP')
                    mid=(lo+hi)//2
                    pending['work'].pop();pending['work'].extend([[mid+1,hi],[lo,mid]])
                else:
                    for key,f in valid:
                        stable=str((f.get('tid',f.get('id',f.get('hash'))),f['coin'],f['side']))
                        old=pending['fills'].get(stable)
                        if old and _key(old,0,now)!=key:raise HistoryIncomplete('FILL_IDENTITY_CONFLICT')
                        pending['fills'][stable]=f
                    if len(pending['fills'])>=RETENTION_CAP:raise HistoryIncomplete('RETENTION_LIMIT')
                    pending['work'].pop()
                    pending['gaps']=[list(x) for x in pending['work']]
                self._save(address,kind,state)
        except HistoryIncomplete as exc:
            state['reason']=str(exc)
            self._save(address,kind,state)
            raise
        combined=dict(completed['fills']) if completed and completed['start']<=start else {}
        for stable,f in pending['fills'].items():
            old=combined.get(stable)
            if old and _key(old,0,now)!=_key(f,0,now):raise HistoryIncomplete('IMMUTABLE_HISTORY_CONFLICT')
            combined[stable]=f
        # This is a public rolling evidence cache, not execution/audit history.
        combined={k:f for k,f in combined.items() if f['time']>=start}
        state={'complete':{'start':start,'end':now,'fills':combined},'pending':None,'reason':None,'updated_ms':now}
        self._save(address,kind,state)
        return sorted(combined.values(),key=lambda f:(f['time'],str(f.get('tid',f.get('hash')))))
