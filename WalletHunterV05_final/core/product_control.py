"""Exact authenticated confirmation control. Reads never invoke the factory."""
import json
from pathlib import Path
from core.product_read import reader
from core.foundation.contracts import OrderIntent
from core.foundation.store import digest,scope_key
from core.foundation.authorization import AuthorizationDecision


class ProductConfirmations:
    def __init__(self,scope,binding,root,factory,clock):
        self.scope,self.binding,self.root,self.factory,self.clock=scope,binding,Path(root),factory,clock
        if binding.scope!=scope or binding.mode!='LIVE_CONFIRM':raise ValueError('CONFIRMATION_SCOPE')
        self.path=Path(binding.state_path)
        if not self.path.is_absolute():self.path=self.root/self.path

    def proposals(self):
        with reader(self.path) as db:
            owner=db.execute('SELECT tenant FROM owners WHERE network=? AND account=?',(self.scope.network,self.scope.account)).fetchone()
            if not owner or owner[0]!=self.scope.tenant:raise ValueError('CONFIRMATION_STORE_SCOPE')
            records=db.execute('SELECT * FROM autonomous_decisions WHERE scope=? ORDER BY rowid DESC LIMIT 100',(scope_key(self.scope),)).fetchall()
            result=[]
            for row in records:
                if not row['intent']:continue
                intent=OrderIntent.model_validate_json(row['intent']);data=json.loads(row['body'])
                if intent.scope!=self.scope or intent.intent_id!=row['id']:raise ValueError('PROPOSAL_SCOPE_MISMATCH')
                request=db.execute('SELECT body FROM authorization_requests WHERE id=? AND scope=?',(intent.intent_id,scope_key(self.scope))).fetchone()
                original=json.loads(request[0]) if request else {}
                if request and AuthorizationDecision.model_validate(original).scope!=self.scope:raise ValueError('AUTHORIZATION_SCOPE_MISMATCH')
                if data['mode']!='LIVE_CONFIRM' or original.get('outcome')!='CONFIRMATION_REQUIRED':continue
                rejected=False
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='authorization_rejections'").fetchone():
                    rejected=bool(db.execute('SELECT 1 FROM authorization_rejections WHERE id=? AND scope=?',(intent.intent_id,scope_key(self.scope))).fetchone())
                execution=db.execute('SELECT status FROM intents WHERE id=? AND scope=?',(intent.intent_id,scope_key(self.scope))).fetchone()
                status=execution[0] if execution else 'REJECTED' if rejected else 'EXPIRED' if self.clock()>=intent.expires_ms else 'PENDING'
                result.append({'id':intent.intent_id,'proposal_hash':digest(intent),'intent':intent.model_dump(mode='json'),'status':status,
                    'episode_id':data.get('episode',{}).get('episode_id'),'authorization':original})
            return result

    def act(self,proposal_id,proposal_hash,user,approve):
        if str(user)!=self.scope.tenant:raise ValueError('TENANT_MISMATCH')
        from core.ai_review import account_guard
        with account_guard(self.path.parent,self.scope.account):
            proposal=next((p for p in self.proposals() if p['id']==proposal_id),None)
            if not proposal or proposal['proposal_hash']!=proposal_hash:raise ValueError('PROPOSAL_MISMATCH')
            if proposal['status'] in {'FILLED','REJECTED','CONFIGURED'}:return proposal
            if proposal['status']=='EXPIRED':raise ValueError('PROPOSAL_EXPIRED')
            if approve:
                backend=self.factory(self.binding)
                if backend.auth_policy.scope!=self.scope or backend.auth_policy.mode!='LIVE_CONFIRM':raise ValueError('FACTORY_SCOPE')
                backend.confirm(proposal_id,authenticated_user=user)
            else:
                from core.foundation.store import Store
                from core.foundation.authorization import AuthorizationService
                from core.position_episodes import EpisodeService,PositionEpisode
                store=Store(self.path);episodes=EpisodeService(store);authorization=AuthorizationService(store)
                def projection(db):
                    row=db.execute('SELECT body FROM autonomous_decisions WHERE id=? AND scope=?',(proposal_id,scope_key(self.scope))).fetchone()
                    data=json.loads(row[0]);data['status']='USER_REJECTED'
                    db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(data),proposal_id))
                    e=db.execute('SELECT p.body FROM position_episodes p JOIN episode_actions a ON a.episode=p.id WHERE a.id=? AND p.scope=?',(proposal_id,scope_key(self.scope))).fetchone()
                    if e:episodes.transition_in(db,PositionEpisode.model_validate_json(e[0]),proposal_id,'REJECTED',{'reason':'USER_REJECTED','no_submission':True})
                    db.execute("UPDATE autonomous_jobs SET stage='COMPLETED',updated_ms=? WHERE scope=? AND event_id=?",(self.clock(),scope_key(self.scope),data['event']['event_id']))
                authorization.reject(proposal_id,self.scope,self.clock(),authenticated_user=user,projection=projection)
            return next(p for p in self.proposals() if p['id']==proposal_id)
