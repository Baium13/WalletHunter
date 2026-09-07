"""Core architecture: disposable storage and fake transport only."""
import unittest
import tempfile
from pathlib import Path
from pydantic import ValidationError
from core.foundation.contracts import *


def scope(network="TESTNET", tenant="one"):
    return Scope(tenant=tenant, account="0x"+"a"*40, network=network)


def instrument(network="TESTNET", symbol="BTC"):
    return InstrumentId(network=network, symbol=symbol)


def intent(**changes):
    values = dict(intent_id="intent-1", scope=scope(), instrument=instrument(), source="a", action="OPEN",
        side="BUY", size=1., limit_price=100., leverage=1, slippage_pct=.5, authorization="PAPER_TEST",
        execution_mode="FAKE", correlation_id="trace-1", created_ms=1000, expires_ms=2000)
    return OrderIntent(**dict(values, **changes))


class ContractTests(unittest.TestCase):
    def test_roundtrip_and_frozen(self):
        row = intent()
        self.assertEqual(OrderIntent.model_validate_json(row.model_dump_json()), row)
        with self.assertRaises(ValidationError): row.size = 2.

    def test_invalid_numbers_and_versions(self):
        for value in (float("nan"), float("inf"), -1., 0., True, "1"):
            with self.subTest(value=value), self.assertRaises(ValidationError): intent(size=value)
        with self.assertRaises(ValidationError): intent(version=2)
        with self.assertRaises(ValidationError): intent(private_key="forbidden")

    def test_network_identity_and_unknown(self):
        with self.assertRaises(ValidationError): instrument("PAPER")
        with self.assertRaises(ValidationError): intent(instrument=instrument("MAINNET"))
        with self.assertRaises(ValidationError):
            MarketSnapshot(instrument=instrument(), exchange_ms=None, received_ms=1000, price=None,
                bid=None, ask=None, completeness="COMPLETE", freshness="FRESH", source="REST", source_version="1")


def portfolio(**changes):
    return PortfolioSnapshot(**dict(dict(scope=scope(), revision=1, exchange_ms=1000, received_ms=1000,
        equity=3000., sizing_capital=3000., available_collateral=3000., positions=(), orders=(),
        completeness="COMPLETE", evidence="FAKE"), **changes))


def position(**changes):
    return Position(**dict(dict(instrument=instrument(), side="LONG", size=7., entry_price=100., notional=700.,
        margin=700., leverage=1, held=True, evidence="VERIFIED", order_ids=("oid-1",),
        contributions=(Contribution(source="a", notional=700.),)), **changes))


class LedgerTests(unittest.TestCase):
    def test_thirds_held_and_reserved(self):
        from core.foundation.ledger import Ledger, Reservation
        p = portfolio(positions=(position(),), available_collateral=2300.)
        ledger = Ledger(p, ("a", "b", "c"), (Reservation("pending", "a", 100., 101.),))
        self.assertEqual(ledger.allocation("a").available, 200.)
        self.assertEqual(ledger.allocation("b").available, 1000.)
        self.assertEqual(ledger.available_capacity, 2199.)

    def test_shared_exposure_is_not_double_counted(self):
        from core.foundation.ledger import Ledger
        p = position(contributions=(Contribution(source="a", notional=350.), Contribution(source="b", notional=350.)))
        ledger = Ledger(portfolio(positions=(p,)), ("a", "b", "c"))
        self.assertEqual(sum(a.committed for a in ledger.allocations.values()), 700.)

    def test_unknown_attribution_and_malformed_reservations_fail_closed(self):
        from core.foundation.ledger import Ledger, Reservation
        p = position(evidence="UNKNOWN", contributions=(), order_ids=())
        with self.assertRaises(ValueError): Ledger(portfolio(positions=(p,)), ("a",)).allocation("a")
        for value in (float("nan"), float("inf"), -1., True):
            with self.assertRaises(ValueError):
                Ledger(portfolio(), ("a",), (Reservation("pending", "a", value, value),)).allocation("a")

    def test_restart_roundtrip_preserves_allocation(self):
        from core.foundation.ledger import Ledger
        p = portfolio(positions=(position(),))
        self.assertEqual(Ledger(p, ("a",)).allocations,
            Ledger(PortfolioSnapshot.model_validate_json(p.model_dump_json()), ("a",)).allocations)


class StoreTests(unittest.TestCase):
    def setUp(self):
        from core.foundation.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"core.sqlite3"
        self.store = Store(self.path)

    def test_durability_order_replay_scope_and_idempotence(self):
        from core.foundation.store import Store, ReplayReader
        self.store.publish_portfolio(portfolio(), "trace")
        self.assertFalse(self.store.publish_portfolio(portfolio(), "trace"))
        self.store.publish_portfolio(portfolio(revision=2, received_ms=1001), "trace")
        rows = self.store.replay(scope())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows, ReplayReader(Store(self.path), scope()).read())
        self.assertEqual(self.store.replay(scope(tenant="other")), ())
        self.assertEqual(self.store.replay(scope(network="MAINNET")), ())
        with self.assertRaises(ValueError): self.store.replay(scope(), limit=501)

    def test_out_of_order_collision_and_owner_conflict(self):
        self.store.publish_portfolio(portfolio(), "trace")
        with self.assertRaises(ValueError): self.store.publish_portfolio(portfolio(equity=3100.), "trace")
        with self.assertRaises(ValueError): self.store.publish_portfolio(portfolio(scope=scope(tenant="other")), "trace")
        seq, event = self.store.replay(scope())[0]
        with self.assertRaises(ValueError): self.store.append(event.model_copy(update={"correlation_id": "other"}))
        self.assertEqual(self.store.replay(scope())[0][0], seq)

    def test_consumer_failure_does_not_lose_events_or_advance_cursor(self):
        self.store.publish_portfolio(portfolio(), "trace")
        def failed(db, event): raise RuntimeError("consumer unavailable")
        with self.assertRaises(RuntimeError): self.store.consume(scope(), "reader", failed)
        seen = []
        self.assertEqual(self.store.consume(scope(), "reader", lambda db,e: seen.append(e.event_id)), 1)
        self.assertEqual(self.store.consume(scope(), "reader", lambda db,e: seen.append(e.event_id)), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self.store.replay(scope())), 1)


class DataTests(unittest.TestCase):
    # Reuse setup only, without inheriting the parent tests.
    setUp = StoreTests.setUp
    def test_cache_gap_reconnect_order_and_stale(self):
        from types import SimpleNamespace
        from core.foundation.data import MarketData, DataUnavailable
        now = [1000]
        calls = []
        def read(query):
            calls.append(query)
            return {"time": now[0], "levels": [[{"px": "99"}], [{"px": "101"}]]}
        hub = MarketData(SimpleNamespace(network="TESTNET", _info=read), self.store, lambda: now[0], max_age_ms=100, min_request_ms=10)
        first = hub.get(scope(), instrument())
        self.assertEqual(hub.get(scope(), instrument()), first)
        self.assertEqual(len(calls), 1)
        self.assertTrue(hub.ingest(scope(), first, 1))
        self.assertFalse(hub.ingest(scope(), first, 1))
        with self.assertRaises(DataUnavailable): hub.ingest(scope(), first, 3)
        now[0] += 10
        hub.get(scope(), instrument())
        hub.disconnect()
        with self.assertRaises(DataUnavailable): hub.get(scope(), instrument())
        now[0] += 101
        hub.get(scope(), instrument())
        self.assertEqual(len(calls), 3)

    def test_invalid_rest_never_returns_cached_fresh_data(self):
        from types import SimpleNamespace
        from core.foundation.data import MarketData, DataUnavailable
        reader = SimpleNamespace(network="TESTNET", _info=lambda q: {"time": 1000, "levels": [[{"px": "NaN"}], [{"px": "1"}]]})
        hub = MarketData(reader, self.store, lambda: 1000, max_age_ms=100, min_request_ms=10, capacity=1)
        with self.assertRaises(DataUnavailable): hub.get(scope(), instrument())
        with self.assertRaises(DataUnavailable): hub.get(scope("MAINNET"), instrument("MAINNET"))


def market(**changes):
    return MarketSnapshot(**dict(dict(instrument=instrument(), exchange_ms=1000, received_ms=1000,
        price=100., bid=99., ask=101., completeness="COMPLETE", freshness="FRESH", source="FAKE", source_version="1"), **changes))


def policy(**changes):
    from core.foundation.risk import RiskPolicy
    return RiskPolicy(**dict(dict(scope=scope(), instrument=instrument(), sources=("a", "b", "c"), enabled=True,
        max_leverage=5, min_notional=10., max_notional=1000., max_symbol_notional=1000., max_total_notional=3000.,
        max_slippage_pct=1., max_price_deviation_pct=1., fee_buffer_pct=.1, size_step=.01,
        max_market_age_ms=100, max_portfolio_age_ms=100, max_intent_age_ms=500), **changes))


class RiskTests(unittest.TestCase):
    def decide(self, order=None, data=None, snapshot=None, config=None, **kwargs):
        from core.foundation.ledger import Ledger
        from core.foundation.risk import RiskGateway
        p = config or policy()
        return RiskGateway(p).evaluate(order or intent(), data or market(), Ledger(snapshot or portfolio(), p.sources),
            1000, **dict(dict(authorized=True), **kwargs))

    def test_valid_and_unauthorized(self):
        self.assertEqual(self.decide().outcome, "APPROVED")
        self.assertIn("AUTHORIZATION_REQUIRED", self.decide(authorized=False).reasons)

    def test_stale_unknown_disabled_and_live_never_approve(self):
        variants = [dict(data=market(exchange_ms=1)), dict(snapshot=portfolio(received_ms=1, exchange_ms=1)),
            dict(config=policy(enabled=False)), dict(order=intent(execution_mode="LIVE")), dict(unresolved=True),
            dict(data=market(exchange_ms=None, price=None, completeness="UNKNOWN"))]
        for args in variants:
            with self.subTest(args=args): self.assertEqual(self.decide(**args).outcome, "REJECTED")

    def test_limits_normalization_network_and_budget(self):
        for order in (intent(leverage=6), intent(size=1.001), intent(size=11.), intent(limit_price=105.),
                      intent(scope=scope(tenant="other")), intent(source="unallocated"), intent(expires_ms=1500, created_ms=1400)):
            self.assertEqual(self.decide(order=order).outcome, "REJECTED")
        self.assertIn("ACCOUNT_CAPACITY", self.decide(snapshot=portfolio(available_collateral=99.)).reasons)


class PipelineTests(unittest.TestCase):
    setUp = StoreTests.setUp

    def pipeline(self):
        from core.foundation.execution import FakeExchange, ExecutionGateway
        from core.foundation.risk import RiskGateway
        self.store.publish_portfolio(portfolio(), "initial")
        exchange = FakeExchange(Path(self.tmp.name)/"exchange.sqlite3")
        gateway = ExecutionGateway(self.store, RiskGateway(policy()), exchange, lambda: 1000)
        return gateway, exchange

    def test_e2e_fill_reservation_ledger_and_trace(self):
        from core.foundation.ledger import Ledger
        gateway, exchange = self.pipeline()
        order = intent()
        gateway.authorize_fake(order)
        receipt = gateway.execute(order, market())
        self.assertEqual(receipt.status, "FILLED")
        self.assertEqual(exchange.calls, 1)
        after = self.store.portfolio(scope())
        self.assertEqual(Ledger(after, policy().sources).allocation("a").available, 900.)
        events = [e for _,e in self.store.replay(scope()) if e.correlation_id == order.correlation_id]
        self.assertEqual([e.event_type for e in events], ["ORDER_INTENT_CREATED", "RISK_APPROVED", "ORDER_SUBMITTED",
            "PORTFOLIO_SNAPSHOT", "ORDER_FILLED", "POSITION_OPENED"])

    def test_duplicate_and_collision_cannot_resubmit(self):
        gateway, exchange = self.pipeline()
        order = intent()
        gateway.authorize_fake(order)
        first = gateway.execute(order, market())
        self.assertEqual(first, gateway.execute(order, market()))
        self.assertEqual(exchange.calls, 1)
        with self.assertRaises(ValueError): gateway.execute(intent(size=2.), market())
        self.assertEqual(exchange.calls, 1)

    def test_partial_fill_commits_only_actual_margin(self):
        from core.foundation.ledger import Ledger
        gateway, exchange = self.pipeline()
        exchange.behavior = "PARTIAL"
        gateway.authorize_fake(intent())
        receipt = gateway.execute(intent(), market())
        self.assertEqual(receipt.status, "PARTIAL")
        self.assertEqual(Ledger(self.store.portfolio(scope()), policy().sources).allocation("a").committed, 50.)

    def test_ack_loss_recovers_by_query_after_restart_without_resubmission(self):
        from core.foundation.execution import FakeExchange, ExecutionGateway
        from core.foundation.risk import RiskGateway
        from core.foundation.store import Store
        gateway, exchange = self.pipeline()
        exchange.behavior = "ACK_LOSS"
        gateway.authorize_fake(intent())
        self.assertEqual(gateway.execute(intent(), market()).status, "UNKNOWN")
        self.assertEqual(gateway.execute(intent(), market()).status, "UNKNOWN")
        self.assertEqual(exchange.calls, 1)
        restarted_exchange = FakeExchange(Path(self.tmp.name)/"exchange.sqlite3")
        restarted = ExecutionGateway(Store(self.path), RiskGateway(policy()), restarted_exchange, lambda: 1001)
        self.assertEqual(restarted.recover(intent()).status, "FILLED")
        self.assertEqual(restarted_exchange.calls, 0)

    def test_missing_evidence_remains_reserved_and_blocks_other_intent(self):
        gateway, exchange = self.pipeline()
        exchange.behavior = "TIMEOUT_BEFORE"
        gateway.authorize_fake(intent())
        self.assertEqual(gateway.execute(intent(), market()).status, "UNKNOWN")
        self.assertEqual(gateway.recover(intent()).status, "UNKNOWN")
        second = intent(intent_id="second", source="b")
        gateway.authorize_fake(second)
        self.assertEqual(gateway.execute(second, market()).status, "REJECTED")
        self.assertEqual(exchange.calls, 1)

    def test_external_mutation_cannot_be_adopted(self):
        gateway, exchange = self.pipeline()
        exchange.behavior = "EXTERNAL_CHANGE"
        gateway.authorize_fake(intent())
        self.assertEqual(gateway.execute(intent(), market()).status, "UNKNOWN")
        self.assertEqual(self.store.portfolio(scope()).positions, ())
        self.assertEqual(gateway.recover(intent()).status, "UNKNOWN")

    def test_no_grant_and_live_backend_are_blocked(self):
        from core.foundation.execution import ExecutionGateway
        from core.foundation.risk import RiskGateway
        gateway, exchange = self.pipeline()
        self.assertEqual(gateway.execute(intent(), market()).status, "REJECTED")
        self.assertEqual(exchange.calls, 0)
        with self.assertRaises(ValueError): gateway.authorize_fake(intent(execution_mode="LIVE"))
        with self.assertRaises(ValueError): ExecutionGateway(self.store, RiskGateway(policy()), object(), lambda: 1000)

    def test_replay_cannot_execute_and_reconstructs_latest_portfolio(self):
        from core.foundation.store import ReplayReader
        gateway, exchange = self.pipeline()
        gateway.authorize_fake(intent())
        gateway.execute(intent(), market())
        reader = ReplayReader(self.store, scope())
        latest = None
        for _, event in reader.read():
            if event.event_type == "PORTFOLIO_SNAPSHOT": latest = event.payload
        self.assertEqual(latest, self.store.portfolio(scope()))
        self.assertFalse(hasattr(reader, "execute"))
        self.assertFalse(hasattr(reader, "authorize_fake"))
        self.assertEqual(exchange.calls, 1)
