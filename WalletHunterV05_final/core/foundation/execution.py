"""Canonical submission boundary for isolated simulation and authorized live routes.

Intent/reservation/event commit precedes any adapter call. Ambiguous effects
are never retried; recovery queries evidence and atomically settles the ledger.
No credential factory or production signer is accepted by this module.
"""
import json
import math
from typing import Literal
from .contracts import Contract, Scope, Fill, PortfolioSnapshot, Position, Contribution, ExecutionReceipt, DomainEvent, OrderIntent
from .ledger import Ledger, Reservation
from .store import Store, encoded, digest, scope_key
from .paper_costs import PaperCosts,costed_effect


class ExchangeReport(Contract):
    intent_hash: str
    scope: Scope
    order_id: str
    terminal: bool
    fills: tuple[Fill, ...]
    after: PortfolioSnapshot
    cost_model: PaperCosts | None = None


def reconcile(intent, before, report, now):
    """Phase 1 proof obligations: order identity + fills + isolated delta.

    A snapshot alone is never ownership. Do not infer rejection from absence.
    """
    def unknown():
        return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="UNKNOWN",
            reconciliation="RECONCILIATION_REQUIRED", received_ms=now, provenance="UNKNOWN")
    try:
        report = ExchangeReport.model_validate_json(report.model_dump_json())
        if (report.scope != intent.scope or report.after.scope != intent.scope or digest(intent) != report.intent_hash
                or not report.order_id or not report.terminal or before.scope != intent.scope):
            return unknown()
        if report.after.completeness != "COMPLETE" or report.after.evidence != "FAKE" or report.after.orders:
            return unknown()
        fills = report.fills
        if len({f.trade_id for f in fills}) != len(fills): return unknown()
        if any(f.intent_id != intent.intent_id or f.instrument != intent.instrument or f.order_id != report.order_id
               or f.side != intent.side or not intent.created_ms <= f.exchange_ms < intent.expires_ms
               or (f.price > intent.limit_price if intent.side == "BUY" else f.price < intent.limit_price) for f in fills):
            return unknown()
        if report.cost_model is not None:
            if report.after!=costed_effect(intent,before,fills,report.after.received_ms,report.cost_model) or report.after.received_ms>now:
                return unknown()
            filled=math.fsum(f.size for f in fills)
            status='REJECTED' if not filled else 'PARTIAL' if filled<intent.size else 'FILLED'
            return ExecutionReceipt(intent_id=intent.intent_id,scope=intent.scope,status=status,order_ids=(report.order_id,),
                fills=fills,reconciliation='CONFIRMED' if status=='FILLED' else status,received_ms=now,provenance='FAKE_EXCHANGE')
        if intent.action in {'ADD','REDUCE','CLOSE'}:
            from .paper_effect import effect
            if report.after != effect(intent,before,fills,report.after.received_ms) or report.after.received_ms>now:
                return unknown()
            filled=math.fsum(f.size for f in fills)
            status='REJECTED' if not filled else 'PARTIAL' if filled<intent.size else 'FILLED'
            return ExecutionReceipt(intent_id=intent.intent_id,scope=intent.scope,status=status,order_ids=(report.order_id,),
                fills=fills,reconciliation='CONFIRMED' if status=='FILLED' else status,received_ms=now,provenance='FAKE_EXCHANGE')
        before_other = tuple(p for p in before.positions if p.instrument != intent.instrument)
        after_other = tuple(p for p in report.after.positions if p.instrument != intent.instrument)
        if before_other != after_other or any(p.instrument == intent.instrument for p in before.positions): return unknown()
        matches = [p for p in report.after.positions if p.instrument == intent.instrument]
        filled = math.fsum(f.size for f in fills)
        if not math.isfinite(filled) or filled > intent.size: return unknown()
        if report.after.revision != before.revision+1 or report.after.received_ms > now: return unknown()
        if report.after.exchange_ms is None or report.after.exchange_ms < (before.exchange_ms or 0): return unknown()
        if report.after.equity != before.equity or report.after.sizing_capital != before.sizing_capital:
            return unknown()  # Fake-only conservation; no implicit deposits or profit.
        if not fills:
            if matches or report.after.available_collateral != before.available_collateral: return unknown()
            return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="REJECTED", order_ids=(report.order_id,),
                reconciliation="REJECTED", received_ms=now, provenance="FAKE_EXCHANGE")
        if len(matches) != 1: return unknown()
        row = matches[0]
        notional = math.fsum(f.size*f.price for f in fills)
        if (row.side != ("LONG" if intent.side == "BUY" else "SHORT") or row.leverage != intent.leverage
            or not math.isclose(row.size, filled, rel_tol=1e-10) or not math.isclose(row.notional, notional, rel_tol=1e-10)
            or not math.isclose(row.entry_price, notional/filled, rel_tol=1e-10)
            or row.margin is None or not math.isclose(row.margin, notional/intent.leverage, rel_tol=1e-10)
            or row.evidence != "VERIFIED" or row.order_ids != (report.order_id,)
            or row.contributions != (Contribution(source=intent.source, notional=notional),)
            or report.after.exchange_ms < max(f.exchange_ms for f in fills)
            or not math.isclose(report.after.available_collateral, before.available_collateral-row.margin, rel_tol=1e-10, abs_tol=1e-10)):
            return unknown()
        partial = filled < intent.size
        return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="PARTIAL" if partial else "FILLED",
            order_ids=(report.order_id,), fills=fills, reconciliation="PARTIAL" if partial else "CONFIRMED",
            received_ms=now, provenance="FAKE_EXCHANGE")
    except (ValueError, AttributeError, TypeError, OverflowError):
        return unknown()


class FakeExchange:
    """Durable fake exchange: independent commit makes acknowledgement loss real."""
    def __init__(self, path):
        self.storage = Store(path)
        self.behavior = "FILLED"
        self.calls = 0
        self.cost_model=None
        with self.storage.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS fake_orders(id TEXT PRIMARY KEY,intent TEXT,report TEXT)")
            db.execute('CREATE TABLE IF NOT EXISTS fake_cost_model(id INTEGER PRIMARY KEY,body TEXT)')
            saved=db.execute('SELECT body FROM fake_cost_model WHERE id=1').fetchone()
            if saved:self.cost_model=PaperCosts.model_validate_json(saved[0])

    def configure_costs(self,model):
        model=PaperCosts.model_validate_json(model.model_dump_json())
        with self.storage.transaction() as db:
            old=db.execute('SELECT body FROM fake_cost_model WHERE id=1').fetchone()
            if old and old[0]!=encoded(model):raise ValueError('New cost model requires a separate simulation namespace')
            db.execute('INSERT OR IGNORE INTO fake_cost_model VALUES(1,?)',(encoded(model),))
        self.cost_model=model

    def submit(self, intent, before, now):
        if intent.execution_mode not in {"FAKE", "PAPER"}: raise ValueError("Fake exchange only")
        self.calls += 1
        if self.behavior == "TIMEOUT_BEFORE": raise TimeoutError("Synthetic timeout")
        with self.storage.transaction() as db:
            old = db.execute("SELECT intent,report FROM fake_orders WHERE id=?", (intent.intent_id,)).fetchone()
            if old:
                if old["intent"] != encoded(intent): raise ValueError("Fake intent collision")
                return ExchangeReport.model_validate_json(old["report"])
            size = intent.size*(.5 if self.behavior == "PARTIAL" else 0. if self.behavior == "REJECTED" else 1.)
            oid = "fake-"+digest(intent)[:32]
            fills, positions, margin = (), before.positions, 0.
            if size:
                fills = (Fill(intent_id=intent.intent_id, instrument=intent.instrument, order_id=oid, trade_id=oid,
                    side=intent.side, size=size, price=intent.limit_price, exchange_ms=now),)
                notional = size*intent.limit_price
                margin = notional/intent.leverage
                positions += (Position(instrument=intent.instrument, side="LONG" if intent.side == "BUY" else "SHORT",
                    size=size+(1 if self.behavior == "EXTERNAL_CHANGE" else 0), entry_price=intent.limit_price,
                    notional=notional, margin=margin, leverage=intent.leverage, evidence="VERIFIED", order_ids=(oid,),
                    contributions=(Contribution(source=intent.source, notional=notional),)),)
            if intent.action in {'ADD','REDUCE','CLOSE'}:
                from .paper_effect import effect
                after=effect(intent,before,fills,now)
                if self.behavior=='EXTERNAL_CHANGE':
                    after=after.model_copy(update={'available_collateral':after.available_collateral+1})
            else:
                after = PortfolioSnapshot.model_validate(dict(before.model_dump(), positions=positions,
                    available_collateral=before.available_collateral-margin, revision=before.revision+1, received_ms=now, exchange_ms=now))
            report = ExchangeReport(intent_hash=digest(intent), scope=intent.scope, order_id=oid, terminal=True, fills=fills, after=after)
            if self.cost_model is not None:
                after=costed_effect(intent,before,fills,now,self.cost_model)
                if self.behavior=='EXTERNAL_CHANGE':after=after.model_copy(update={'available_collateral':after.available_collateral+1})
                report=report.model_copy(update={'after':after,'cost_model':self.cost_model})
            db.execute("INSERT INTO fake_orders VALUES(?,?,?)", (intent.intent_id, encoded(intent), encoded(report)))
        if self.behavior == "ACK_LOSS": raise TimeoutError("Synthetic acknowledgement loss")
        return report

    def query(self, intent):
        with self.storage.transaction() as db:
            row = db.execute("SELECT intent,report FROM fake_orders WHERE id=?", (intent.intent_id,)).fetchone()
            if row and row["intent"] == encoded(intent): return ExchangeReport.model_validate_json(row["report"])
        return None


class ExecutionGateway:
    def __init__(self, store, risk, exchange, clock_ms):
        from .copy_execution import HyperliquidExecutionAdapter
        if type(exchange) not in (FakeExchange, HyperliquidExecutionAdapter): raise ValueError("Uncontrolled execution adapter")
        self.store, self.risk, self.__exchange, self.clock = store, risk, exchange, clock_ms

    def authorize_fake(self, intent):
        """Explicit test/operator setup; events and analysis cannot mint grants."""
        intent = OrderIntent.model_validate_json(intent.model_dump_json())
        if intent.execution_mode not in {"FAKE", "PAPER"} or intent.authorization != "PAPER_TEST":
            raise ValueError("Live authorization unavailable")
        with self.store.transaction() as db:
            self.store.bind(db, intent.scope)
            row = db.execute("SELECT intent_hash FROM grants WHERE id=?", (intent.intent_id,)).fetchone()
            if row and row[0] != digest(intent): raise ValueError("Grant identity collision")
            db.execute("INSERT OR IGNORE INTO grants VALUES(?,?,?)", (intent.intent_id, scope_key(intent.scope), digest(intent)))

    def _event(self, db, intent, kind, payload, now):
        return self.store.append_in(db, DomainEvent(event_id=digest(intent)[:32]+"-"+kind,
            event_type=kind, correlation_id=intent.correlation_id, scope=intent.scope, event_ms=now, received_ms=now, payload=payload))

    def _unknown(self, intent, now, status="UNKNOWN"):
        return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status=status,
            reconciliation="RECONCILIATION_REQUIRED", received_ms=now, provenance="UNKNOWN")

    def authorize_copy(self, intent):
        from .copy_execution import HyperliquidExecutionAdapter
        if type(self.__exchange) is not HyperliquidExecutionAdapter:
            raise ValueError('Copy adapter required')
        self.__exchange.validate_scope(intent)
        with self.store.transaction() as db:
            self.store.bind(db, intent.scope)
            row = db.execute('SELECT account,intent,status FROM operations WHERE id=?', (intent.parent_intent_id,)).fetchone()
            if not row or row['account'] != intent.scope.account or row['status'] != 'PREPARED' or json.loads(row['intent']).get('network') != intent.scope.network:
                raise ValueError('Durable copy authority unavailable')
            db.execute('INSERT OR IGNORE INTO grants VALUES(?,?,?)', (intent.intent_id, scope_key(intent.scope), digest(intent)))

    def authorize_paper(self,intent,authorization):
        from .authorization import AuthorizationService
        if type(self.__exchange) is not FakeExchange or intent.execution_mode!='PAPER' or intent.authorization!='PAPER_POLICY':
            raise ValueError('Paper adapter and policy required')
        if (authorization.execution_mode!='PAPER' or authorization.scope!=intent.scope or authorization.event_id!=intent.correlation_id
            or intent.intent_id!=authorization.decision_id
            or intent.expires_ms>authorization.expires_ms or not AuthorizationService(self.store).verify(authorization,self.clock())):
            raise ValueError('Durable PAPER authorization required')
        with self.store.transaction() as db:
            row=db.execute('SELECT consensus,event FROM authorization_requests WHERE id=?',(authorization.decision_id,)).fetchone()
            if not row or json.loads(row[0])['decision']!=('COPY_LONG' if intent.side=='BUY' else 'COPY_SHORT'):
                raise ValueError('Consensus direction mismatch')
            event=json.loads(row['event']) if row['event'] else {}
            if event.get('instrument')!=intent.instrument.model_dump(mode='json') or event.get('action')!=intent.action:
                raise ValueError('Authorized event scope mismatch')
            self.store.bind(db,intent.scope)
            old=db.execute('SELECT intent_hash FROM grants WHERE id=?',(intent.intent_id,)).fetchone()
            if old and old[0]!=digest(intent): raise ValueError('Grant identity collision')
            db.execute('INSERT OR IGNORE INTO grants VALUES(?,?,?)',(intent.intent_id,scope_key(intent.scope),digest(intent)))

    def authorize_live(self, intent, authorization):
        """Durable, tenant-confirmed proposal; never an agent-produced grant."""
        from .authorization import AuthorizationService
        from .copy_execution import HyperliquidExecutionAdapter
        if type(self.__exchange) is not HyperliquidExecutionAdapter or intent.version != 3:
            raise ValueError('Confirmed live adapter required')
        self.__exchange.validate_scope(intent)
        if (authorization.execution_mode != 'LIVE' or authorization.scope != intent.scope
                or authorization.event_id != intent.correlation_id or authorization.decision_id != intent.intent_id
                or intent.expires_ms > authorization.expires_ms
                or not AuthorizationService(self.store).verify(authorization, self.clock())):
            raise ValueError('Durable live confirmation required')
        with self.store.transaction() as db:
            row = db.execute('SELECT consensus,event FROM authorization_requests WHERE id=?', (intent.intent_id,)).fetchone()
            proposal = db.execute('SELECT intent FROM autonomous_decisions WHERE id=? AND scope=?',
                (intent.intent_id, scope_key(intent.scope))).fetchone()
            if not row or not proposal or not proposal[0]: raise ValueError('Confirmed proposal unavailable')
            expected = OrderIntent.model_validate_json(proposal[0])
            # Only the dispatch timestamp can change at confirmation. Exact size,
            # limit, instrument and configuration remain those shown in proposal.
            if expected.model_copy(update={'created_ms': intent.created_ms}) != intent:
                raise ValueError('Confirmed proposal changed')
            event = json.loads(row['event'])
            if (event.get('instrument') != intent.instrument.model_dump(mode='json') or event.get('action') != intent.action
                    or json.loads(row['consensus'])['decision'] != ('COPY_LONG' if intent.side == 'BUY' else 'COPY_SHORT')):
                raise ValueError('Confirmed event mismatch')
            self.store.bind(db, intent.scope)
            old = db.execute('SELECT intent_hash FROM grants WHERE id=?', (intent.intent_id,)).fetchone()
            if old and old[0] != digest(intent): raise ValueError('Grant identity collision')
            db.execute('INSERT OR IGNORE INTO grants VALUES(?,?,?)', (intent.intent_id, scope_key(intent.scope), digest(intent)))

    def execute(self, intent, market, *, copy_ledger=None, autonomous_ledger=None):
        intent = OrderIntent.model_validate_json(intent.model_dump_json())
        with self.store.transaction() as db:
            now = self.clock()
            self.store.bind(db, intent.scope)
            old = db.execute("SELECT * FROM intents WHERE id=?", (intent.intent_id,)).fetchone()
            if old:
                if old["scope"] != scope_key(intent.scope) or old["body"] != encoded(intent): raise ValueError("Immutable intent collision")
                return ExecutionReceipt.model_validate_json(old["receipt"]) if old["receipt"] else self._unknown(intent, now, "SUBMITTING")
            before = self.store.portfolio_in(db, intent.scope)
            pending = db.execute("SELECT reservation FROM intents WHERE scope=? AND (status IN ('SUBMITTING','UNKNOWN') OR (status='PARTIAL' AND (json_extract(body,'$.authorization')='PAPER_POLICY' OR json_extract(body,'$.version') IN (3,4))))", (scope_key(intent.scope),)).fetchall()
            reservations = [Reservation(**json.loads(r[0])) for r in pending] if intent.version != 2 else []
            grant = db.execute("SELECT intent_hash FROM grants WHERE id=? AND scope=?", (intent.intent_id, scope_key(intent.scope))).fetchone()
            ledger = Ledger(before, self.risk.policy.sources, reservations) if intent.version == 1 else copy_ledger
            if autonomous_ledger is not None:
                from .autonomous_allocation import AutonomousLedger
                if type(autonomous_ledger) is not AutonomousLedger or not (
                        (intent.authorization=='PAPER_POLICY' and intent.version==1)
                        or (intent.authorization=='USER_CONFIRMED' and intent.version==3)):
                    raise ValueError('Autonomous ledger scope invalid')
                # Rebuild from durable pending reservations inside the same
                # transaction; caller cannot omit a concurrent UNKNOWN intent.
                ledger=AutonomousLedger(before,autonomous_ledger.policy,reservations)
            if ledger is None or ledger.portfolio != before: raise ValueError('Ledger snapshot mismatch')
            decision = self.risk.evaluate(intent, market, ledger, now, authorized=bool(grant and grant[0] == digest(intent)), unresolved=bool(pending))
            approved = decision.outcome == "APPROVED"
            receipt = self._unknown(intent, now, "SUBMITTING") if approved else ExecutionReceipt(intent_id=intent.intent_id,
                scope=intent.scope, status="REJECTED", reconciliation="REJECTED", received_ms=now, provenance="UNKNOWN")
            notional = intent.size*max(market.price or intent.limit_price, intent.limit_price) if approved else 0.
            reservation = dict(intent_id=intent.intent_id, source=intent.source, margin=notional/intent.leverage,
                account_capacity=notional/intent.leverage+notional*self.risk.policy.fee_buffer_pct/100)
            db.execute("INSERT INTO intents VALUES(?,?,?,?,?,?,?)", (intent.intent_id, scope_key(intent.scope), encoded(intent),
                receipt.status, encoded(decision), json.dumps(reservation), encoded(receipt)))
            db.execute("INSERT INTO intent_prestate VALUES(?,?)", (intent.intent_id, encoded(before)))
            if intent.version in (2,4):
                # Same SQLite transaction as reservation. Keep the rich legacy
                # envelope, adding only stable child identity links.
                parent = db.execute('SELECT intent FROM operations WHERE id=?', (intent.parent_intent_id,)).fetchone()
                if not parent: raise ValueError('Missing parent journal')
                envelope = json.loads(parent[0])
                children = envelope.setdefault('canonical_intents', [])
                if intent.intent_id not in children: children.append(intent.intent_id)
                envelope['correlation_id'] = intent.correlation_id
                db.execute('UPDATE operations SET intent=? WHERE id=?', (json.dumps(envelope), intent.parent_intent_id))
            db.execute("INSERT OR IGNORE INTO policies VALUES(?,?)", (digest(self.risk.policy), encoded(self.risk.policy)))
            db.execute("DELETE FROM grants WHERE id=?", (intent.intent_id,))
            self._event(db, intent, "MARKET_SNAPSHOT", market, now)
            self._event(db, intent, "ORDER_INTENT_CREATED", intent, now)
            self._event(db, intent, "RISK_APPROVED" if approved else "RISK_REJECTED", decision, now)
            if approved: self._event(db, intent, "ORDER_SUBMITTED", receipt, now)
        if not approved: return receipt
        # Crash from here onward leaves a durable reservation. Never auto-resubmit.
        try:
            dispatch_ms = self.clock()
            if not intent.created_ms <= dispatch_ms < intent.expires_ms: raise TimeoutError("Intent expired before dispatch")
            report = self.__exchange.submit(intent, before, dispatch_ms)
        except Exception: report = None
        return self._settle(intent, before, report)

    def _settle(self, intent, before, report):
        now = self.clock()
        if intent.version==1 and report is not None and report.cost_model!=self.__exchange.cost_model:
            report=None  # A response cannot select a cheaper simulation model.
        receipt = (report.receipt if report is not None else self._unknown(intent, now)) if intent.version in (2,3,4) else reconcile(intent, before, report, now)
        with self.store.transaction() as db:
            row = db.execute("SELECT status,receipt FROM intents WHERE id=? AND scope=?", (intent.intent_id, scope_key(intent.scope))).fetchone()
            if not row: raise ValueError("No durable intent")
            if row["status"] not in {"SUBMITTING", "UNKNOWN", "PARTIAL"}:
                return ExecutionReceipt.model_validate_json(row["receipt"])
            current = self.store.portfolio_in(db, intent.scope)
            if current != before and intent.version == 1:
                previous=ExecutionReceipt.model_validate_json(row['receipt']) if row['receipt'] else None
                if previous and previous.status=='PARTIAL' and report is not None:
                    # Repeated partial evidence is query-only and idempotent.
                    if current==report.after:return previous
                    prior=(costed_effect(intent,before,previous.fills,current.received_ms,report.cost_model)
                        if report.cost_model else __import__('core.foundation.paper_effect',fromlist=['effect']).effect(intent,before,previous.fills,current.received_ms))
                    if current!=prior.model_copy(update={'revision':current.revision}):receipt=self._unknown(intent,now)
                else:receipt = self._unknown(intent, now)
            if intent.version in (2,3,4) and report is not None and report.after.revision <= current.revision:
                # A repeated query may return the same durable partial
                # snapshot. Preserve that proven partial state instead of
                # downgrading it or pretending the remainder was filled.
                previous = json.loads(row["receipt"]) if row["receipt"] else {}
                if not (row["status"] == "PARTIAL" and previous.get("status") == "PARTIAL"
                        and report.after == current):
                    receipt = self._unknown(intent, now)
            if intent.version in (3,4) and current != before: receipt = self._unknown(intent, now)
            if receipt.status != "UNKNOWN":
                after = report.after
                if intent.version==1 and after.revision<=current.revision:
                    after=after.model_copy(update={'revision':current.revision+1})
                if intent.version==4 and receipt.status in {'FILLED','PARTIAL'}:
                    from .confirmed_ledger import project_receipt_in
                    after=project_receipt_in(db,intent,before,after,receipt)
                if intent.version == 3 and receipt.status in {'FILLED','PARTIAL'}:
                    # Order/fill/delta proof, not address similarity, grants ownership.
                    after = after.model_copy(update={'positions': tuple(
                        p.model_copy(update={'evidence':'VERIFIED','order_ids':receipt.order_ids,
                            'contributions':(Contribution(source=intent.source,notional=p.notional),)})
                        if p.instrument == intent.instrument else p for p in after.positions)})
                self.store.publish_portfolio_in(db, after, intent.correlation_id)
            kind = {"FILLED": "ORDER_FILLED", "PARTIAL": "ORDER_PARTIALLY_FILLED", "REJECTED": "ORDER_REJECTED", "UNKNOWN": "EXECUTION_UNKNOWN", "CONFIGURED": "LEVERAGE_CONFIGURED"}[receipt.status]
            if receipt.status == 'CONFIGURED':
                kind = {'PLACE_STOP':'PROTECTION_CONFIGURED','CANCEL_OWNED':'ORDER_CANCELLED'}.get(intent.action,kind)
            # Repeated UNKNOWN queries must not collide with the original timestamp.
            status_changed = receipt.status != row["status"]
            if status_changed:
                self._event(db, intent, kind, receipt, now)
            if status_changed and receipt.status in {"FILLED", "PARTIAL"}:
                self._event(db, intent, {'OPEN':'POSITION_OPENED','ADD':'POSITION_INCREASED','REDUCE':'POSITION_REDUCED','CLOSE':'POSITION_CLOSED' if receipt.status == 'FILLED' else 'POSITION_REDUCED'}[intent.action], receipt, now)
            if receipt.status=='PARTIAL' and intent.authorization=='PAPER_POLICY':
                remaining=max(0.,intent.size-math.fsum(f.size for f in receipt.fills))
                value=remaining*intent.limit_price
                reservation=dict(intent_id=intent.intent_id,source=intent.source,margin=value/intent.leverage,
                    account_capacity=value/intent.leverage+value*self.risk.policy.fee_buffer_pct/100)
                db.execute('UPDATE intents SET reservation=? WHERE id=?',(json.dumps(reservation),intent.intent_id))
            db.execute("UPDATE intents SET status=?,receipt=? WHERE id=?", (receipt.status, encoded(receipt), intent.intent_id))
        return receipt

    def recover(self, intent):
        """Query only: missing order data never authorizes submission or release."""
        with self.store.transaction() as db:
            row = db.execute("SELECT body,status,receipt FROM intents WHERE id=? AND scope=?", (intent.intent_id, scope_key(intent.scope))).fetchone()
            if not row or row["body"] != encoded(intent): raise ValueError("Unknown intent identity")
            if row["status"] not in {"SUBMITTING", "UNKNOWN", "PARTIAL"}:
                return ExecutionReceipt.model_validate_json(row["receipt"])
            prestate = db.execute("SELECT body FROM intent_prestate WHERE id=?", (intent.intent_id,)).fetchone()
            if not prestate: raise ValueError("Missing pre-execution evidence; no retry")
            before = PortfolioSnapshot.model_validate_json(prestate[0])
            if intent.version in (2,3,4):
                self.__exchange.before = before
                self.__exchange.next_revision = self.store.portfolio_in(db, intent.scope).revision+1
        try: report = self.__exchange.query(intent)
        except Exception: report = None
        return self._settle(intent, before, report)

    def authorize_confirmed(self, intent):
        """Existing product confirmation, not an autonomous consensus grant.

        The parent journal must durably bind exact intent bytes to the tenant.
        Merely constructing a context or reading a proposal creates no grant.
        """
        from .copy_execution import HyperliquidExecutionAdapter
        if type(self.__exchange) is not HyperliquidExecutionAdapter or intent.version != 4:
            raise ValueError('Confirmed adapter required')
        self.__exchange.validate_scope(intent)
        with self.store.transaction() as db:
            self.store.bind(db,intent.scope)
            row=db.execute('SELECT account,intent,status FROM operations WHERE id=?',(intent.parent_intent_id,)).fetchone()
            envelope=json.loads(row['intent']) if row else {}
            if (not row or row['account']!=intent.scope.account or row['status']!='PREPARED'
                    or envelope.get('network')!=intent.scope.network
                    or envelope.get('confirmed_tenant')!=intent.scope.tenant
                    or envelope.get('confirmed_intents',{}).get(intent.intent_id)!=digest(intent)):
                raise ValueError('Exact durable product confirmation required')
            old=db.execute('SELECT intent_hash FROM grants WHERE id=?',(intent.intent_id,)).fetchone()
            if old and old[0]!=digest(intent): raise ValueError('Immutable confirmation collision')
            db.execute('INSERT OR IGNORE INTO grants VALUES(?,?,?)',(intent.intent_id,scope_key(intent.scope),digest(intent)))
