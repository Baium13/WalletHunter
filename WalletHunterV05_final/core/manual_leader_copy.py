"""Manual Leader Copy policy and proportional target calculation.

This module is deliberately separate from autonomous intelligence allocation.
It only produces deterministic copy targets; execution remains owned by the
canonical risk/execution gateway.
"""
import math
import hashlib
import os
import sqlite3
import time
from contextlib import closing
from typing import Literal
from pydantic import Field
from core.foundation.contracts import Contract, Scope, Name, Amount, Millis


class ManualLeaderConfig(Contract):
    scope: Scope
    leader: Name
    alias: Name
    allocation_pct: float = Field(gt=0, le=100, allow_inf_nan=False)
    enabled: bool = False
    created_ms: Millis
    updated_ms: Millis
    strategy: Literal['MANUAL_LEADER_COPY'] = 'MANUAL_LEADER_COPY'

    def capital_limit(self, allocatable_capital: Amount) -> float:
        capital = float(allocatable_capital)
        if not math.isfinite(capital) or capital < 0:
            raise ValueError('Allocatable capital unavailable')
        return capital * self.allocation_pct / 100.0


class ManualLeaderCopy:
    def __init__(self, config: ManualLeaderConfig):
        self.config = ManualLeaderConfig.model_validate_json(config.model_dump_json())

    def start(self, now: int) -> ManualLeaderConfig:
        return self.config.model_copy(update={'enabled': True, 'updated_ms': now})

    def stop(self, now: int, *, close_positions: bool = False) -> tuple[ManualLeaderConfig, bool]:
        # False means retain existing positions as HOLD; caller must explicitly
        # route close_positions through Risk/Gateway when requested.
        return self.config.model_copy(update={'enabled': False, 'updated_ms': now}), bool(close_positions)

    def switch(self, leader: str, alias: str | None = None, now: int | None = None) -> ManualLeaderConfig:
        """Select one new leader without transferring existing provenance.

        A switch intentionally disables the new policy until the caller starts
        it again. Existing positions remain owned by their original source and
        therefore stay HOLD rather than being adopted by the replacement.
        """
        if not isinstance(leader, str) or not leader:
            raise ValueError('Leader identity required')
        stamp = int(time.time() * 1000) if now is None else int(now)
        return self.config.model_copy(update={
            'leader': leader.lower(), 'alias': alias or leader.lower(),
            'enabled': False, 'updated_ms': stamp,
        })

    def proportional_margin(self, *, leader_margin: Amount, leader_capital: Amount,
                            allocatable_capital: Amount, fee_reserve_pct: float = 0.0) -> float:
        values = [float(leader_margin), float(leader_capital), float(allocatable_capital), float(fee_reserve_pct)]
        if any(not math.isfinite(v) for v in values) or leader_margin < 0 or leader_capital <= 0 or allocatable_capital < 0 or fee_reserve_pct < 0 or fee_reserve_pct >= 100:
            raise ValueError('Invalid leader or allocation evidence')
        base = self.config.capital_limit(allocatable_capital)
        return base * (float(leader_margin) / float(leader_capital)) * (1.0 - fee_reserve_pct / 100.0)

    def target_margin(self, *, leader_margin: Amount, leader_capital: Amount,
                      allocatable_capital: Amount, fee_reserve_pct: float = 0.0,
                      max_margin: Amount | None = None) -> float:
        target = self.proportional_margin(leader_margin=leader_margin, leader_capital=leader_capital,
            allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct)
        if max_margin is not None:
            cap = float(max_margin)
            if not math.isfinite(cap) or cap < 0: raise ValueError('Invalid margin cap')
            target = min(target, cap)
        return target

    def plan_event(self, event_id: Name, action: Literal['OPEN','ADD','REDUCE','CLOSE','REVERSE'],
                   *, leader_margin: Amount, leader_capital: Amount,
                   allocatable_capital: Amount, current_margin: Amount = 0.0,
                   fee_reserve_pct: float = 0.0, max_margin: Amount | None = None) -> dict:
        """Return a proportional delta; this object never submits an order."""
        if not self.config.enabled:
            return {'event_id': event_id, 'status': 'STOPPED', 'target_margin': float(current_margin), 'delta_margin': 0.0}
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('Leader event identity required')
        current = float(current_margin)
        if not math.isfinite(current) or current < 0:
            raise ValueError('Current attributable margin unavailable')
        target = 0.0 if action == 'CLOSE' else self.target_margin(
            leader_margin=leader_margin, leader_capital=leader_capital,
            allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct,
            max_margin=max_margin)
        if action == 'REDUCE': target = min(target, current)
        if action == 'REVERSE':
            target = self.target_margin(leader_margin=leader_margin, leader_capital=leader_capital,
                allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct, max_margin=max_margin)
        delta = target-current
        return {'event_id': event_id, 'status': 'READY' if abs(delta)>1e-12 else 'NO_CHANGE',
            'target_margin': target, 'delta_margin': delta, 'strategy': self.config.strategy,
            'leader': self.config.leader, 'allocation_pct': self.config.allocation_pct}


class ManualLeaderEventBook:
    """Idempotent leader event identity book.

    The default in-memory mode keeps the small policy object useful in pure
    unit tests. Production callers pass the execution-journal path so event
    identity survives process restart and cannot cause a second order.
    """
    def __init__(self, path: str | None = None):
        self._seen = set()
        self.path = path
        if path is not None:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with closing(sqlite3.connect(path, timeout=10)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS manual_leader_events('
                           'event_hash TEXT PRIMARY KEY,event_id TEXT NOT NULL,created_ms INTEGER NOT NULL)')
                db.commit()

    def accept(self, event_id: str) -> bool:
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            raise ValueError('Invalid leader event identity')
        key = hashlib.sha256(event_id.encode()).hexdigest()
        if self.path is not None:
            try:
                with closing(sqlite3.connect(self.path, timeout=10)) as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO manual_leader_events VALUES(?,?,?)',
                               (key, event_id, int(time.time() * 1000)))
                    db.commit()
                    return True
            except sqlite3.IntegrityError:
                return False
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


class ManualLeaderCopyService:
    """Durable controller for the one manually selected public leader.

    Configuration and event watermarks are public strategy state only.  The
    service delegates every executable action to :func:`execute_manual_leader`,
    so it never owns signing credentials or an alternate writer.
    """
    def __init__(self, engine):
        if not engine or not engine.journal:
            raise ValueError('Durable journal required')
        self.engine = engine
        self.path = engine.journal.path
        self.events = ManualLeaderEventBook(self.path)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS manual_leader_configs('
                       'scope TEXT PRIMARY KEY,body TEXT NOT NULL)')
            db.commit()

    @staticmethod
    def _scope(account, client):
        from core.settings import validated_network
        from core.foundation.contracts import Scope
        network = validated_network(getattr(client, 'network', None))
        address = str(account.get('address', '')).lower()
        return Scope(tenant=str(account['_tenant']), account=address, network=network)

    def _read(self, scope):
        from core.foundation.contracts import Scope
        scope_json = scope.model_dump_json()
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            row = db.execute('SELECT body FROM manual_leader_configs WHERE scope=?', (scope_json,)).fetchone()
        return ManualLeaderConfig.model_validate_json(row[0]) if row else None

    def _write(self, config):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('INSERT INTO manual_leader_configs(scope,body) VALUES(?,?) '
                       'ON CONFLICT(scope) DO UPDATE SET body=excluded.body',
                       (config.scope.model_dump_json(), config.model_dump_json()))
            db.commit()
        return config

    def configure(self, account, client, leader, allocation_pct, *, alias=None, now=None):
        scope = self._scope(account, client)
        stamp = int(time.time() * 1000) if now is None else int(now)
        old = self._read(scope)
        changed = old is not None and old.leader != str(leader).lower()
        config = ManualLeaderConfig(scope=scope, leader=str(leader).lower(),
            alias=str(alias or leader), allocation_pct=float(allocation_pct),
            enabled=False if changed else bool(old.enabled) if old else False,
            created_ms=old.created_ms if old else stamp, updated_ms=stamp)
        return self._write(config)

    def config(self, account, client):
        return self._read(self._scope(account, client))

    def start(self, account, client, *, now=None):
        config = self.config(account, client)
        if config is None:
            raise ValueError('Manual Leader is not configured')
        stamp = int(time.time() * 1000) if now is None else int(now)
        return self._write(ManualLeaderCopy(config).start(stamp))

    def stop(self, account, client, *, now=None, close_positions=False):
        config = self.config(account, client)
        if config is None:
            raise ValueError('Manual Leader is not configured')
        stamp = int(time.time() * 1000) if now is None else int(now)
        stopped, should_close = ManualLeaderCopy(config).stop(stamp, close_positions=close_positions)
        return self._write(stopped), should_close

    def process(self, account, client, *, event_id, action, leader_margin,
                leader_capital, allocatable_capital, current_margin=0.0,
                spec=None, before_position=None, fee_reserve_pct=0.0,
                side=None):
        config = self.config(account, client)
        if config is None:
            raise ValueError('Manual Leader is not configured')
        values = dict(spec or {}, leader=config.leader, alias=config.alias,
                      allocation_pct=config.allocation_pct, enabled=config.enabled)
        return execute_manual_leader(engine=self.engine, account=account,
            client=client, operation=None, event_id=event_id, action=action,
            leader_margin=leader_margin, leader_capital=leader_capital,
            allocatable_capital=allocatable_capital, current_margin=current_margin,
            spec=values, before_position=before_position,
            fee_reserve_pct=fee_reserve_pct, side=side, event_book=self.events)


def _canonical_receipt(engine, intent_id):
    """Read a receipt written by the canonical gateway without retrying it."""
    from core.foundation.contracts import ExecutionReceipt
    try:
        with closing(engine.journal.connect()) as db:
            row = db.execute('SELECT receipt FROM intents WHERE id=?', (intent_id,)).fetchone()
        return ExecutionReceipt.model_validate_json(row[0]) if row and row[0] else None
    except Exception:
        return None


def _manual_position(client, coin, dex):
    rows = client.positions(True, True)
    if not isinstance(rows, list):
        raise ValueError('Position evidence unavailable')
    key_coin = coin.split(':')[-1]
    matches = [row for row in rows if isinstance(row, dict)
               and row.get('coin', '').split(':')[-1] == key_coin
               and (row.get('dex') or '') == (dex or '')
               and float(row.get('size', 0) or 0) > 0]
    if len(matches) > 1:
        raise ValueError('Position evidence ambiguous')
    return matches[0] if matches else None


def _finish_manual(engine, operation, receipt, client, spec, coin, dex, *, finalize=True):
    """Project only proven terminal fills into the legacy provenance journal."""
    if not finalize or not operation or not receipt:
        return
    if receipt.status in {'UNKNOWN', 'PARTIAL', 'SUBMITTING'}:
        # The canonical reservation and parent journal intentionally remain
        # pending until a later query proves a terminal outcome.
        return
    position = _manual_position(client, coin, dex)
    managed = position is not None and float(position.get('size', 0) or 0) > 0
    if receipt.status == 'REJECTED':
        outcome = {'ok': False, 'action': spec.get('action', 'MANUAL_LEADER'),
                   'execution_evidence': {'intent_id': receipt.intent_id,
                                          'network': receipt.scope.network}}
        engine.journal.finish(operation, outcome)
        return
    ownership = {
        'managed': managed,
        'size': float(position.get('size', 0)) if managed else 0.0,
        'side': position.get('side', spec.get('side', 'LONG')) if managed else spec.get('side', 'LONG'),
        'position': position if managed else None,
        'source_targets': list(spec.get('sources') or ()),
        'strategy': 'MANUAL_LEADER_COPY',
        'attribution': 'manual_leader_copy_provenance',
        'execution_evidence': {'intent_id': receipt.intent_id,
                               'order_ids': list(receipt.order_ids),
                               'trade_ids': [fill.trade_id for fill in receipt.fills],
                               'network': receipt.scope.network},
    }
    engine.journal.finish(operation, {'ok': True, 'action': spec.get('action', 'MANUAL_LEADER'),
                                      'execution_evidence': ownership['execution_evidence']}, ownership)


def execute_manual_leader(*, engine, account, client, operation, event_id, action,
                          leader_margin, leader_capital, allocatable_capital,
                          current_margin, spec, before_position=None,
                          fee_reserve_pct=0.0, side=None, event_book=None):
    """Route a proportional Manual Leader action through the proven COPY gateway.

    The leader quantity is never submitted: only the calculated follower delta
    enters the canonical OrderIntent. Provenance is tagged by the source wallet.
    """
    from core.settings import validated_network
    from core.foundation.contracts import Scope
    from core.foundation.copy_execution import execute_copy

    network = validated_network(getattr(client, 'network', None))
    if not isinstance(account, dict) or not isinstance(account.get('_tenant'), (str, int)):
        raise ValueError('Manual Leader account scope required')
    policy = ManualLeaderConfig(scope=Scope(
        tenant=str(account['_tenant']), account=account['address'].lower(), network=client.network),
        leader=str(spec['leader']).lower(), alias=str(spec.get('alias') or spec['leader']),
        allocation_pct=float(spec['allocation_pct']), enabled=True,
        created_ms=int(spec.get('created_ms', 0)), updated_ms=int(spec.get('updated_ms', 0)))
    planner = ManualLeaderCopy(policy)
    if action not in {'OPEN', 'ADD', 'REDUCE', 'CLOSE', 'REVERSE'}:
        raise ValueError('Unsupported manual leader action')
    leader_side = str(side or spec.get('side') or spec.get('leader_side') or 'LONG').upper()
    if leader_side not in {'LONG', 'SHORT'}:
        raise ValueError('Manual Leader direction unavailable')
    if spec.get('enabled', True) is not True:
        return {'event_id': event_id, 'status': 'STOPPED',
                'target_margin': float(current_margin), 'delta_margin': 0.0,
                'strategy': policy.strategy, 'leader': policy.leader,
                'allocation_pct': policy.allocation_pct}
    if before_position and current_margin <= 0:
        current_margin = before_position.get('margin_used') or (
            float(before_position.get('position_value', 0)) /
            max(1.0, float(before_position.get('leverage', spec.get('leverage', 1)))))
    now = int(time.time() * 1000)
    book = event_book or ManualLeaderEventBook(engine.journal.path if engine and engine.journal else None)
    if not book.accept(event_id):
        return {'event_id': event_id, 'status': 'DUPLICATE', 'action': action}
    plan = planner.plan_event(event_id, action, leader_margin=leader_margin,
        leader_capital=leader_capital, allocatable_capital=allocatable_capital,
        current_margin=current_margin, fee_reserve_pct=fee_reserve_pct,
        max_margin=spec.get('max_margin'))
    if action == 'REVERSE':
        # REVERSE is a direction change even when the proportional margin is
        # unchanged; force the two-step close/open sequence below.
        plan = dict(plan, status='READY', delta_margin=-float(current_margin))
    if plan['status'] != 'READY':
        return plan
    leverage = int(spec['leverage'])
    if leverage < 1:
        raise ValueError('Invalid leverage')
    price = float(client.mid(spec['coin'], spec.get('dex') or ''))
    if not math.isfinite(price) or price <= 0: raise ValueError('Price unavailable')
    manual_limit = policy.capital_limit(allocatable_capital)
    if operation is None:
        if not engine or not engine.journal:
            raise ValueError('Durable journal required')
        dex = spec.get('dex') or ''
        symbol = str(spec['coin']).split(':')[-1]
        market = f"{dex+':' if dex else ''}{symbol}|{dex}"
        operation = engine.journal.prepare(account['address'], market, {
            'action': f'MANUAL_LEADER_{action}', 'network': network,
            'strategy': 'MANUAL_LEADER_COPY', 'leader': policy.leader,
            'event_id': event_id, 'allocation_pct': policy.allocation_pct,
            'before': before_position,
        })

    def prepared(target_margin, target_side, executable_action, executable_operation, *, finalize=True):
        # SourceAllocationBook validates target_notional against the exact
        # source contribution.  Close uses the proven current notional because
        # a zero target cannot be represented as a positive contribution.
        contribution_margin = max(float(target_margin), float(current_margin) if executable_action == 'CLOSE' else 0.0)
        contribution_notional = contribution_margin * leverage
        if before_position and executable_action == 'CLOSE':
            contribution_notional = max(contribution_notional, float(before_position.get('position_value', 0) or 0))
            contribution_margin = contribution_notional / leverage
        signed = contribution_notional if target_side == 'LONG' else -contribution_notional
        common = dict(spec, coin=spec['coin'], dex=spec.get('dex') or '', side=target_side,
                      leverage=leverage, target_notional=contribution_notional,
                      signed_notional=signed, target_margin=contribution_margin,
                      capital_pct=(contribution_margin / float(allocatable_capital) * 100.0
                                   if float(allocatable_capital) > 0 else 0.0),
                      sources=[{'wallet': policy.leader, 'signed_notional': signed,
                                'margin': contribution_margin}],
                      _allocation_sources=(policy.leader,),
                      _allocation_limits={policy.leader: manual_limit},
                      _ownership_sources=(policy.leader,), _ownership_strategy='MANUAL_LEADER_COPY',
                      action=executable_action,
                      configure_leverage=bool(spec.get('configure_leverage', executable_action in {'OPEN', 'ADD'})))
        delta_margin = abs(float(plan['delta_margin'])) if executable_action != 'CLOSE' else float(current_margin)
        size = delta_margin * leverage / price
        buy = (target_side == 'LONG') if executable_action in {'OPEN', 'ADD'} else (target_side == 'SHORT')
        receipt = None
        try:
            receipt = execute_copy(engine, account, client, executable_operation, executable_action,
                                   size, buy, common, before_position,
                                   configure=bool(common.get('configure_leverage', False)))
        except Exception:
            receipt = _canonical_receipt(engine, executable_operation + '-' + executable_action.lower())
            if receipt is None:
                raise
        _finish_manual(engine, executable_operation, receipt, client, common,
                       common['coin'], common.get('dex') or '', finalize=finalize)
        return receipt

    # A reverse is deliberately two independently reconciled intents sharing
    # the same durable parent operation.  The opposite entry is forbidden if
    # the close is unknown or the exchange still reports exposure.
    if action == 'REVERSE':
        old_side = (before_position or {}).get('side')
        if old_side not in {'LONG', 'SHORT'}:
            old_side = 'SHORT' if leader_side == 'LONG' else 'LONG'
        close_plan = dict(plan, delta_margin=-float(current_margin))
        original_plan = plan
        plan = close_plan
        close_receipt = prepared(float(current_margin), old_side, 'CLOSE', operation, finalize=False)
        plan = original_plan
        if not close_receipt or close_receipt.status != 'FILLED':
            return close_receipt
        remaining = _manual_position(client, spec['coin'], spec.get('dex') or '')
        if remaining is not None:
            raise ValueError('Manual Leader reverse close not proven flat')
        return prepared(float(original_plan['target_margin']), leader_side, 'OPEN', operation)

    target_side = leader_side
    return prepared(float(plan['target_margin']), target_side, action, operation)
