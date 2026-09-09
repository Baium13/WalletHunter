"""Durable inbox and auditable cursor. No execution or credentials here."""
import json
from core.foundation.store import scope_key


class JobStore:
    def __init__(self,store,scope,clock):
        self.store,self.scope,self.clock=store,scope_key(scope),clock
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS autonomous_jobs(scope TEXT,event_id TEXT,record TEXT,stage TEXT,
                    started_ms INTEGER,updated_ms INTEGER,attempts INTEGER,reason TEXT,PRIMARY KEY(scope,event_id));
                CREATE TABLE IF NOT EXISTS autonomous_quarantine(scope TEXT,seq INTEGER,reason TEXT,record TEXT,
                    recorded_ms INTEGER,PRIMARY KEY(scope,seq));
                CREATE TABLE IF NOT EXISTS autonomous_progress(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT,event_id TEXT,stage TEXT,recorded_ms INTEGER);
                CREATE TABLE IF NOT EXISTS autonomous_health(scope TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS autonomous_analysis(scope TEXT,event_id TEXT,body TEXT,PRIMARY KEY(scope,event_id));
                CREATE TABLE IF NOT EXISTS autonomous_deliveries(scope TEXT,record_id TEXT,research_seq INTEGER,event_id TEXT,
                    PRIMARY KEY(scope,record_id));
                CREATE TABLE IF NOT EXISTS historical_job_classification(scope TEXT,event_id TEXT,reason TEXT,classified_ms INTEGER,
                    PRIMARY KEY(scope,event_id));
            ''')
    def claim(self,event_id,record):
        text=json.dumps(record,sort_keys=True,allow_nan=False)
        now=self.clock()
        with self.store.transaction() as db:
            old=db.execute('SELECT * FROM autonomous_jobs WHERE scope=? AND event_id=?',(self.scope,event_id)).fetchone()
            if old:
                if old['record']!=text:raise ValueError('EVENT_IDENTITY_COLLISION')
                return dict(old)
            db.execute('INSERT INTO autonomous_jobs VALUES(?,?,?,?,?,?,?,?)',
                (self.scope,event_id,text,'CLAIMED',now,now,1,None))
            db.execute('INSERT INTO autonomous_progress(scope,event_id,stage,recorded_ms) VALUES(?,?,?,?)',
                (self.scope,event_id,'CLAIMED',now))
        return {'stage':'CLAIMED','started_ms':now}
    def stage(self,event_id,stage,reason=None):
        with self.store.transaction() as db:
            db.execute('UPDATE autonomous_jobs SET stage=?,updated_ms=?,reason=? WHERE scope=? AND event_id=?',
                (stage,self.clock(),reason,self.scope,event_id))
            db.execute('INSERT INTO autonomous_progress(scope,event_id,stage,recorded_ms) VALUES(?,?,?,?)',
                (self.scope,event_id,stage,self.clock()))
    def quarantine(self,seq,record,reason):
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO autonomous_quarantine VALUES(?,?,?,?,?)',
                (self.scope,seq,reason,json.dumps(record,sort_keys=True,default=str),self.clock()))
    def advance(self,seq):
        with self.store.transaction() as db:
            db.execute('INSERT INTO autonomous_cursors VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET seq=MAX(seq,excluded.seq)',(self.scope,seq))
            db.execute('INSERT INTO autonomous_progress(scope,event_id,stage,recorded_ms) VALUES(?,?,?,?)',
                (self.scope,str(seq),'CURSOR_COMMITTED',self.clock()))

    def quarantine_summary_in(self,db):
        ids={r[0] for r in db.execute("SELECT event_id FROM autonomous_jobs WHERE scope=? AND stage='QUARANTINED'",(self.scope,))}
        historical={r[0] for r in db.execute('SELECT event_id FROM historical_job_classification WHERE scope=?',(self.scope,))}
        for row in db.execute('SELECT seq,record FROM autonomous_quarantine WHERE scope=?',(self.scope,)):
            try:
                body=json.loads(row['record'])
                if isinstance(body,str):body=json.loads(body)
                event_id=body['event']['event_id']
            except (KeyError,TypeError,ValueError):event_id='delivery:'+str(row['seq'])
            if event_id not in historical:ids.add(event_id)
        return dict(quarantine_count=len(ids),historical_quarantine_count=len(historical),quarantined_jobs=len(ids))

    def classify_nonfinancial_adds(self):
        """Audited no-retry classification; never changes financial evidence.

        Only failed ADD preparation with no persisted intent/episode action can
        be classified. Ambiguous records and any financial identity stay held.
        """
        count=0
        with self.store.transaction() as db:
            rows=db.execute("SELECT event_id,record FROM autonomous_jobs WHERE scope=? AND stage='QUARANTINED' AND reason='ValueError' LIMIT 128",(self.scope,)).fetchall()
            for row in rows:
                body=json.loads(row['record']);event=body.get('event',{})
                if event.get('action')!='ADD':continue
                event_id=row['event_id']
                # Reject classification if any canonical identity exists.
                if db.execute('SELECT 1 FROM autonomous_decisions WHERE scope=? AND event_id=? AND intent IS NOT NULL',(self.scope,event_id)).fetchone():continue
                if db.execute("SELECT 1 FROM intents WHERE scope=? AND json_extract(body,'$.correlation_id')=?",(self.scope,event_id)).fetchone():continue
                if db.execute("SELECT 1 FROM position_episodes WHERE scope=? AND json_extract(body,'$.leader')=? AND json_extract(body,'$.instrument')=json(?) AND json_extract(body,'$.state')!='REJECTED'",(self.scope,event.get('wallet'),json.dumps(event.get('instrument')))).fetchone():continue
                db.execute('INSERT OR IGNORE INTO historical_job_classification VALUES(?,?,?,?)',
                    (self.scope,event_id,'NO_FOLLOWER_EPISODE_NO_CANONICAL_INTENT',self.clock()))
                db.execute("UPDATE autonomous_jobs SET stage='SKIPPED_HISTORICAL',updated_ms=? WHERE scope=? AND event_id=?",(self.clock(),self.scope,event_id))
                db.execute('INSERT INTO autonomous_progress(scope,event_id,stage,recorded_ms) VALUES(?,?,?,?)',
                    (self.scope,event_id,'SKIPPED_HISTORICAL',self.clock()))
                count+=1
        return count
