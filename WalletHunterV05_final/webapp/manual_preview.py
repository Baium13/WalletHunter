"""Read-only on-demand Manual Copy evidence; never constructs an authority."""
from core.foundation.data import copy_account_snapshot
from core.hl_budget import BudgetUnavailable


def failure(exc,stage):
    cause=exc
    while cause is not None:
        if isinstance(cause,BudgetUnavailable):
            return {'code':'API_BUDGET_CONSTRAINED','stage':stage,'retryable':True}
        cause=cause.__cause__
    text=str(exc)
    codes={'LEADER_STALE':'LEADER_DATA_STALE','ACCOUNT_WATERMARK_UNAVAILABLE':'FOLLOWER_TIMESTAMP_UNAVAILABLE',
        'ACCOUNT_NETWORK_MISMATCH':'NETWORK_MISMATCH','LEADER_NETWORK_MISMATCH':'NETWORK_MISMATCH',
        'LEADER_CAPITAL_UNAVAILABLE':'LEADER_CAPITAL_UNAVAILABLE','FOLLOWER_CAPACITY_UNAVAILABLE':'FOLLOWER_CAPACITY_UNAVAILABLE',
        'ACCOUNT_DATA_STALE':'FOLLOWER_DATA_STALE'}
    return {'code':codes.get(text,stage+'_UNAVAILABLE'),'stage':stage,'retryable':True}


class ReadTrace:
    """Retain the exact read error that canonical optional-capacity mapping hides.

    No signer is exposed; this object is used only for a preview snapshot.
    """
    def __init__(self,client):
        self.client=client;self.error=None
        self.network=client.network;self.address=client.address;self.info=client.info
    def positions(self,*args):return self.client.positions(*args)
    def frontend_open_orders(self,*args):return self.client.frontend_open_orders(*args)
    def capital_snapshot(self):return self._call(self.client.capital_snapshot)
    def available_margin(self,*args):return self._call(self.client.available_margin,*args)
    def _call(self,fn,*args):
        try:return fn(*args)
        except Exception as exc:self.error=exc;raise


def current_evidence(worker,account,client,leader):
    scope=worker.service._scope(account,client)
    if worker.reader.network!=scope.network:raise ValueError('LEADER_NETWORK_MISMATCH')
    leader_state=worker.leader_snapshot(leader)
    required={p.get('dex') or '' for p in leader_state['positions'].values()}
    if any(leader_state['capital'].get(pool) is None for pool in required):raise ValueError('LEADER_CAPITAL_UNAVAILABLE')
    traced=ReadTrace(client)
    try:portfolio=copy_account_snapshot(traced,scope,1,worker.clock,'')
    except Exception as exc:
        exc.preview_stage='FOLLOWER_ACCOUNT';raise
    if portfolio.completeness!='COMPLETE':
        exc=traced.error or ValueError('FOLLOWER_CAPACITY_UNAVAILABLE')
        exc.preview_stage='FOLLOWER_ACCOUNT';raise exc
    now=worker.clock()
    if not 0<=now-leader_state['exchange_ms']<=worker.MAX_AGE_MS:raise ValueError('LEADER_STALE')
    if any(type(t) is not int or not 0<=now-t<=worker.MAX_AGE_MS for t in (portfolio.exchange_ms,portfolio.received_ms)):
        exc=ValueError('ACCOUNT_DATA_STALE');exc.preview_stage='FOLLOWER_ACCOUNT';raise exc
    return {'wallet':leader,'leader':leader_state,'account':portfolio.model_dump(mode='json')}
