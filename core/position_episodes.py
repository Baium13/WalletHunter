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
    # Every mode the authorization policy can hold must be nameable here.
    # LIVE_AUTO was added to the policy without being added to this list,
    # so every unattended live OPEN raised ValidationError, rolled the
    # transaction back and quarantined the job. It failed closed - nothing
    # was ever submitted - but the mode could not work at all.
    mode: Literal['PAPER_AUTO','SHADOW','LIVE_CONFIRM','LIVE_AUTO','OBSERVE']
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
                CREATE TABLE IF NOT EXISTS hypothetical_executions(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episode_transitions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                    episode TEXT NOT NULL,state TEXT NOT NULL,evidence TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS episode_history_immutable BEFORE UPDATE ON episode_transitions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
                CREATE TRIGGER IF NOT EXISTS episode_history_retained BEFORE DELETE ON episode_transitions BEGIN SELECT RAISE(ABORT,'immutable history'); END;
                CREATE TRIGGER IF NOT EXISTS outcome_immutable BEFORE UPDATE ON autonomous_outcomes BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
                CREATE TRIGGER IF NOT EXISTS outcome_retained BEFORE DELETE ON autonomous_outcomes BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
                CREATE TRIGGER IF NOT EXISTS calibration_immutable BEFORE UPDATE ON calibration_records BEGIN SELECT RAISE(ABORT,'immutable evaluation'); END;
                CREATE TRIGGER IF NOT EXISTS calibration_retained BEFORE DELETE ON calibration_records BEGIN SELECT RAISE(ABORT,'immutable evaluation'); END;
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
        matches=[]
        for r in rows:
            e=PositionEpisode.model_validate_json(r[0])
            if e.mode!=mode or e.leader!=leader or e.instrument!=instrument or e.state=='CLOSED':continue
            if e.state=='REJECTED':
                # Read-through repair for the former action/episode conflation.
                # Only exact canonical order evidence can recover attribution;
                # an aggregate same-symbol position is not sufficient.
                portfolio=self.store.portfolio_in(db,scope)
                position=next((p for p in portfolio.positions if p.instrument==instrument and p.evidence=='VERIFIED'),None)
                proofs=db.execute("SELECT i.receipt FROM episode_actions a JOIN intents i ON i.id=a.id WHERE a.episode=? AND i.status IN ('FILLED','PARTIAL')",(e.episode_id,)).fetchall()
                if not position or not any(set(json.loads(p[0])['order_ids']) & set(position.order_ids) for p in proofs):continue
                e=e.model_copy(update={'state':'RECONCILIATION_REQUIRED'})
            matches.append(e)
        if len(matches)>1:
            # Two non-closed episodes for one (mode, leader, instrument) used to
            # raise, which the worker recorded as a bare "ValueError" and
            # quarantined - 37 ADD jobs in one deployment, with nothing in the
            # record to say why. Ambiguity is not the same as unknowability:
            # the position itself says which episode owns it. Prefer the
            # episode whose FILLED/PARTIAL order ids are actually in the held
            # position; that is the same exact-order evidence the REJECTED
            # repair above demands, so this widens nothing. Only when the
            # evidence cannot single one out is the call still refused.
            owner=[e for e in matches if self._owns_in(db,scope,e,instrument)]
            if len(owner)==1: return owner[0]
            if not owner:
                live=[e for e in matches if e.state!='REJECTED']
                if len(live)==1: return live[0]
            raise ValueError('Ambiguous position episode: %d candidates for %s/%s, %d with order evidence'
                             %(len(matches),leader,instrument.symbol,len(owner)))
        return matches[0] if matches else None

    def _owns_in(self,db,scope,episode,instrument):
        """Does the held position carry an order id this episode actually filled?"""
        portfolio=self.store.portfolio_in(db,scope)
        position=next((p for p in portfolio.positions
                       if p.instrument==instrument and p.evidence=='VERIFIED'),None)
        if not position: return False
        proofs=db.execute("SELECT i.receipt FROM episode_actions a JOIN intents i ON i.id=a.id "
                          "WHERE a.episode=? AND i.status IN ('FILLED','PARTIAL')",(episode.episode_id,)).fetchall()
        for proof in proofs:
            try: ids=set(json.loads(proof[0])['order_ids'])
            except (TypeError,ValueError,KeyError): continue
            if ids&set(position.order_ids): return True
        return False

    def transition_in(self,db,episode,action_id,state,evidence):
        key=action_id+'-'+state
        value=json.dumps(evidence,sort_keys=True,allow_nan=False)
        old=db.execute('SELECT evidence FROM episode_transitions WHERE id=?',(key,)).fetchone()
        if old: return # Projection is idempotent, never appends repeated UNKNOWN.
        db.execute('INSERT INTO episode_transitions(id,episode,state,evidence) VALUES(?,?,?,?)',
            (key,episode.episode_id,state,value))
        current=PositionEpisode.model_validate_json(db.execute('SELECT body FROM position_episodes WHERE id=?',(episode.episode_id,)).fetchone()[0])
        # These are action stages/outcomes, not evidence that an existing
        # position disappeared. Only reconciled effects change its lifecycle.
        if action_id!=episode.episode_id and state in {'PROPOSED','AUTHORIZED','RESERVED','SUBMITTED','REJECTED'}:
            if state=='REJECTED' and current.state in {'UNKNOWN','RECONCILIATION_REQUIRED'}:
                stable=db.execute("SELECT state FROM episode_transitions WHERE episode=? AND id NOT LIKE ? AND state IN ('OPEN','INCREASED','REDUCED','PARTIAL') ORDER BY seq DESC LIMIT 1",
                    (episode.episode_id,action_id+'-%')).fetchone()
                if stable:
                    db.execute('UPDATE position_episodes SET body=? WHERE id=?',
                        (encoded(current.model_copy(update={'state':stable[0]})),episode.episode_id))
            return
        updated=PositionEpisode.model_validate(dict(current.model_dump(),state=state))
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
                from core.autonomous_outcomes import record_outcome
                record_outcome(db,episode)
            return episode
