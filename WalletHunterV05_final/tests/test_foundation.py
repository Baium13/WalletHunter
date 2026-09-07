"""Core architecture: disposable storage and fake transport only."""
import unittest
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
