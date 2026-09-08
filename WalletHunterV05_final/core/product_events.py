"""Durable bounded product feed and Telegram delivery, separate from execution."""
import asyncio
import hashlib
import json
import time
from core.foundation.store import Store,scope_key
from core.product_read import clean


class ProductEvents:
    RETENTION=2000
    def __init__(self,path,clock=lambda:int(time.time()*1000)):
        self.store=Store(path);self.clock=clock
        with self.store.transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS product_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT,id TEXT,kind TEXT,body TEXT,created INTEGER,UNIQUE(scope,id));
                CREATE TABLE IF NOT EXISTS product_latest(scope TEXT,object TEXT,digest TEXT,PRIMARY KEY(scope,object));
                CREATE TABLE IF NOT EXISTS product_outbox(scope TEXT,id TEXT,body TEXT,status TEXT,attempts INTEGER,
                due INTEGER,critical INTEGER,last_error TEXT,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS product_delivery_health(scope TEXT PRIMARY KEY,body TEXT);''')

    def publish(self,scope,identity,kind,payload,*,notify=False,critical=False):
        encoded=json.dumps(clean(payload),sort_keys=True,allow_nan=False)
        if len(encoded)>100000:raise ValueError('PRODUCT_EVENT_TOO_LARGE')
        key=scope_key(scope);digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.store.transaction() as db:
            self.store.bind(db,scope)
            old=db.execute('SELECT digest FROM product_latest WHERE scope=? AND object=?',(key,identity)).fetchone()
            if old and old[0]==digest:return
            eid=hashlib.sha256((identity+digest).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO product_events(scope,id,kind,body,created) VALUES(?,?,?,?,?)',(key,eid,kind,encoded,self.clock()))
            db.execute('INSERT OR REPLACE INTO product_latest VALUES(?,?,?)',(key,identity,digest))
            if notify:db.execute('INSERT OR IGNORE INTO product_outbox VALUES(?,?,?,?,?,?,?,?)',(key,eid,json.dumps({'type':kind,'data':clean(payload)}),'PENDING',0,self.clock(),int(critical),None))
            db.execute('DELETE FROM product_events WHERE scope=? AND seq NOT IN (SELECT seq FROM product_events WHERE scope=? ORDER BY seq DESC LIMIT ?)',(key,key,self.RETENTION))

    def collect(self,snapshot):
        from core.foundation.contracts import Scope
        scope=Scope.model_validate(snapshot['scope'])
        self.publish(scope,'health','HEALTH_UPDATED',snapshot['health'],notify=snapshot['health']['status']=='UNHEALTHY',critical=True)
        self.publish(scope,'manual','MANUAL_COPY_UPDATED',snapshot['manual_copy'])
        for action in (snapshot.get('account') or {}).get('actions',[]):
            payload={k:action[k] for k in ('intent_id','status','correlation_id','timestamp','origin')}
            payload['mode']=action['intent']['execution_mode']
            self.publish(scope,'account:'+action['intent_id'],'MANUAL_COPY_UPDATED' if action['origin']=='MANUAL_LEADER_COPY' else 'EXECUTION_UPDATED',
                payload,notify=True,critical=action['status'] in {'UNKNOWN','PARTIAL'})
        discovery=snapshot['discovery']
        self.publish(scope,'discovery','DISCOVERY_UPDATED',{'counts':discovery['counts'],'status':discovery['status']})
        for leader in discovery['leaders']:
            self.publish(scope,'leader:'+leader['wallet'],'LEADER_PROMOTED' if leader['status']=='ACTIVE' else 'LEADER_DEGRADED',
                {'wallet':leader['wallet'],'status':leader['status']},notify=leader['status'] in {'ACTIVE','PROBATION','RETIRED'})
        for mode in snapshot['runtimes']:
            prefix=mode['mode']+':'
            self.publish(scope,prefix+'mode','MODE_CHANGED',{'configured':mode['configured_mode'],'runtime':mode['runtime_mode']})
            decision=mode.get('latest_decision')
            if decision:
                for field,kind in [('event','LEADER_EVENT'),('agents','AGENT_UPDATED'),('consensus','CONSENSUS_UPDATED'),('authorization','AUTHORIZATION_REQUIRED')]:
                    value=decision.get(field)
                    if value is not None:self.publish(scope,prefix+field+decision['event']['event_id'],kind,value,
                        notify=field=='event' or (field=='consensus' and value.get('decision') in {'COPY_LONG','COPY_SHORT','SKIP'}) or
                            (field=='authorization' and value.get('outcome')=='CONFIRMATION_REQUIRED'))
            for action in mode.get('actions',[]):
                payload={k:action[k] for k in ('intent_id','status','correlation_id','timestamp')}
                payload['mode']=mode['mode']
                self.publish(scope,prefix+action['intent_id'],'EXECUTION_UPDATED',payload,notify=True,critical=action['status'] in {'UNKNOWN','PARTIAL'})
                self.publish(scope,prefix+action['intent_id']+'-intent','ORDER_INTENT',action['intent'])
                if action['risk']:self.publish(scope,prefix+action['intent_id']+'-risk','RISK_UPDATED',action['risk'],notify=True)
            for episode in mode.get('episodes',[]):
                self.publish(scope,prefix+episode['episode_id'],'POSITION_UPDATED',{k:episode[k] for k in ('episode_id','instrument','state','mode','origin','unresolved')})
                if episode['outcome']:self.publish(scope,prefix+episode['episode_id']+'-outcome','OUTCOME_UPDATED',episode['outcome'],notify=True)

    def read(self,scope,after=0,limit=50):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:raise ValueError('CURSOR_BOUND')
        key=scope_key(scope)
        with self.store.transaction() as db:
            first=db.execute('SELECT MIN(seq) FROM product_events WHERE scope=?',(key,)).fetchone()[0]
            records=db.execute('SELECT * FROM product_events WHERE scope=? AND seq>? ORDER BY seq LIMIT ?',(key,after,limit)).fetchall()
        items=[];cursor=after
        for row in records:
            cursor=row['seq']
            try:payload=json.loads(row['body'])
            except (ValueError,TypeError):continue
            items.append({'seq':cursor,'id':row['id'],'type':row['kind'],'timestamp':row['created'],'data':clean(payload)})
        return {'events':items,'cursor':cursor,'reset_required':bool(after and first and after<first-1),'retention':self.RETENTION}

    async def deliver(self,scope,send,limit=4):
        """At-least-once delivery: ack loss can duplicate once; never unbounded retry."""
        if not 1<=limit<=4:raise ValueError('DELIVERY_BOUND')
        key=scope_key(scope);now=self.clock()
        with self.store.transaction() as db:
            db.execute("UPDATE product_outbox SET status='DEAD',last_error='DELIVERY_ACK_UNKNOWN' WHERE scope=? AND status='SENDING' AND due<=? AND attempts>=5",(key,now))
            rows=db.execute("SELECT * FROM product_outbox WHERE scope=? AND status IN ('PENDING','SENDING') AND due<=? AND attempts<5 ORDER BY critical DESC,due LIMIT ?",(key,now,limit)).fetchall()
            # Lease and consume an attempt durably before any external delivery.
            for r in rows:db.execute("UPDATE product_outbox SET status='SENDING',attempts=attempts+1,due=? WHERE scope=? AND id=?",(now+60000,key,r['id']))
        for r in rows:
            try:
                await asyncio.wait_for(send(json.loads(r['body']),r['id']),timeout=8)
                status,error='SENT',None
            except Exception:status,error=('DEAD' if r['attempts']+1>=5 else 'PENDING'),'DELIVERY_UNAVAILABLE'
            with self.store.transaction() as db:
                db.execute('UPDATE product_outbox SET status=?,due=?,last_error=? WHERE scope=? AND id=?',(status,self.clock()+min(3600000,60000*2**r['attempts']),error,key,r['id']))
                db.execute('INSERT OR REPLACE INTO product_delivery_health VALUES(?,?)',(key,json.dumps({'heartbeat_ms':self.clock(),'status':'ACTIVE' if status=='SENT' else 'DEGRADED','last_error':error})))
