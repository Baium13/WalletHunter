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
