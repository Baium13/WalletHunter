"""Durable causal projection. Canonical intents remain reservation authority.

Episodes do not submit or release capital. Predictions are immutable and stored
in the same transaction as the decision, before any possible submission.
"""
import json
from typing import Literal
from core.foundation.contracts import Contract,Name,Scope,InstrumentId,Millis
from core.foundation.store import encoded,digest,scope_key


class PositionEpisode(Contract):
    episode_id: Name
    scope: Scope
    mode: Literal['PAPER_AUTO','SHADOW','LIVE_CONFIRM','OBSERVE']
    leader: str
    instrument: InstrumentId
    first_event_id: Name
    created_ms: Millis
    state: Literal['PROPOSED','AUTHORIZED','RESERVED','SUBMITTED','PARTIAL','OPEN','INCREASED','REDUCED',
        'CLOSED','REJECTED','UNKNOWN','RECONCILIATION_REQUIRED']


class EpisodeService:
    def __init__(self,store):
        self.store=store
        with store.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS autonomous_predictions(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS prediction_immutable BEFORE UPDATE ON autonomous_predictions BEGIN SELECT RAISE(ABORT,'immutable prediction'); END;
                CREATE TRIGGER IF NOT EXISTS prediction_retained BEFORE DELETE ON autonomous_predictions BEGIN SELECT RAISE(ABORT,'immutable prediction'); END;
                CREATE TABLE IF NOT EXISTS position_episodes(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episode_actions(id TEXT PRIMARY KEY,episode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS autonomous_outcomes(id TEXT PRIMARY KEY,scope TEXT NOT NULL,mode TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calibration_records(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episode_transitions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                    episode TEXT NOT NULL,state TEXT NOT NULL,evidence TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS episode_history_immutable BEFORE UPDATE ON episode_transitions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
                CREATE TRIGGER IF NOT EXISTS episode_history_retained BEFORE DELETE ON episode_transitions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
            """)

    def prepare_in(self,db,body,intent):
        if intent is None: return
        original=json.dumps(body,sort_keys=True,allow_nan=False)
        old=db.execute('SELECT body FROM autonomous_predictions WHERE id=?',(intent.intent_id,)).fetchone()
        if old:
            if old[0]!=original: raise ValueError('Immutable prediction collision')
            return
        db.execute('INSERT INTO autonomous_predictions VALUES(?,?,?)',(intent.intent_id,scope_key(intent.scope),original))
        event=body['event']
        previous=self.active_in(db,intent.scope,body['mode'],event['wallet'],intent.instrument)
        if intent.action!='OPEN':
            if previous is None: raise ValueError('Position episode unavailable')
            episode=previous
        else:
            episode=PositionEpisode(episode_id=intent.intent_id,scope=intent.scope,mode=body['mode'],
            leader=event['wallet'],instrument=intent.instrument,first_event_id=intent.correlation_id,
            created_ms=intent.created_ms,state='PROPOSED')
            db.execute('INSERT INTO position_episodes VALUES(?,?,?)',(episode.episode_id,scope_key(intent.scope),encoded(episode)))
        db.execute('INSERT INTO episode_actions VALUES(?,?)',(intent.intent_id,episode.episode_id))
        self.transition_in(db,episode,intent.intent_id,'PROPOSED',{'prediction_id':intent.intent_id})
        if body['authorization']['outcome']=='AUTHORIZED':
            self.transition_in(db,episode,intent.intent_id,'AUTHORIZED',body['authorization'])

    def active_in(self,db,scope,mode,leader,instrument):
        rows=db.execute('SELECT body FROM position_episodes WHERE scope=?',(scope_key(scope),)).fetchall()
        matches=[e for r in rows if (e:=PositionEpisode.model_validate_json(r[0])).mode==mode and e.leader==leader
            and e.instrument==instrument and e.state not in {'CLOSED','REJECTED'}]
        if len(matches)>1: raise ValueError('Ambiguous position episode')
        return matches[0] if matches else None

    def transition_in(self,db,episode,action_id,state,evidence):
        key=action_id+'-'+state
        value=json.dumps(evidence,sort_keys=True,allow_nan=False)
        old=db.execute('SELECT evidence FROM episode_transitions WHERE id=?',(key,)).fetchone()
        if old: return # Projection is idempotent, never appends repeated UNKNOWN.
        db.execute('INSERT INTO episode_transitions(id,episode,state,evidence) VALUES(?,?,?,?)',
            (key,episode.episode_id,state,value))
        updated=PositionEpisode.model_validate(dict(episode.model_dump(),state=state))
        db.execute('UPDATE position_episodes SET body=? WHERE id=?',(encoded(updated),episode.episode_id))

    def sync(self,decision_id,scope):
        with self.store.transaction() as db:
            row=db.execute('SELECT p.body FROM position_episodes p JOIN episode_actions a ON a.episode=p.id WHERE a.id=? AND p.scope=?',
                (decision_id,scope_key(scope))).fetchone()
            if not row: return None
            episode=PositionEpisode.model_validate_json(row[0])
            execution=db.execute('SELECT * FROM intents WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone()
            if execution:
                intent=json.loads(execution['body']); risk=json.loads(execution['decision'])
                if risk['outcome']=='APPROVED':
                    for state in ('AUTHORIZED','RESERVED','SUBMITTED'):
                        self.transition_in(db,episode,decision_id,state,{'intent_id':decision_id,'risk':risk,
                            'reservation':json.loads(execution['reservation'])})
                status=execution['status']
                state={'SUBMITTING':'SUBMITTED','UNKNOWN':'UNKNOWN','REJECTED':'REJECTED','PARTIAL':'PARTIAL',
                    'CONFIGURED':'OPEN','FILLED':{'OPEN':'OPEN','ADD':'INCREASED','REDUCE':'REDUCED','CLOSE':'CLOSED'}.get(intent['action'],'RECONCILIATION_REQUIRED')}[status]
                self.transition_in(db,episode,decision_id,state,{'receipt':json.loads(execution['receipt'])})
            final=db.execute('SELECT body FROM position_episodes WHERE id=?',(episode.episode_id,)).fetchone()
            episode=PositionEpisode.model_validate_json(final[0])
            if episode.state=='CLOSED':
                oid=episode.episode_id+'-outcome'
                outcome={'episode_id':episode.episode_id,'mode':episode.mode,'state':'CLOSED',
                    'decision_id':episode.episode_id,'recorded_ms':episode.created_ms,'evidence':'PAPER_OR_SHADOW'}
                db.execute('INSERT OR IGNORE INTO autonomous_outcomes VALUES(?,?,?,?)',
                    (oid,scope_key(scope),episode.mode,json.dumps(outcome,sort_keys=True,allow_nan=False)))
                db.execute('INSERT OR IGNORE INTO calibration_records VALUES(?,?,?)',
                    (oid,scope_key(scope),json.dumps({'outcome_id':oid,'version':'calibration-v1',
                        'leader':episode.leader,'decision':episode.episode_id,'sample_count':1},sort_keys=True)))
            return episode
