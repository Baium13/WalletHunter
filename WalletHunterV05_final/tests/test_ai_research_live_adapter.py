"""Offline integration of live-read review inputs into immutable research.

The actual history reconstruction, candidate arithmetic and research database
are exercised. All client reads are fake; any attempted order raises immediately.
"""
from contextlib import closing
from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch

from core.ai_review import AiReview, market_key, identity


HOUR = 3_600_000
START = 1_800_000_000_000
OPEN = START + 1000
NOW = START + 2*HOUR + 1000
ACCOUNT = "0x" + "a"*40
SOURCE = "0x" + "b"*40


class Client:
    def __init__(self, position):
        self.row = position
        self.reads = 0
    def positions(self, *args):
        self.reads += 1
        return [deepcopy(self.row)]
    def size_step(self, *args):
        return .00001
    def market_open(self, *args):
        raise AssertionError("Research must never open real orders")
    def market_reduce(self, *args):
        raise AssertionError("Research must never reduce real orders")
    def set_leverage(self, *args):
        raise AssertionError("Research must never change leverage")


class Reader:
    def __init__(self, coin="BTC", side="LONG"):
        sign = "1" if side == "LONG" else "-1"
        self.fills = [{"coin": coin, "time": OPEN, "startPosition": "0", "sz": "1",
                       "side": "B" if side == "LONG" else "A", "px": "100",
                       "closedPnl": "0", "fee": ".1", "feeToken": "USDC", "oid": 10, "tid": 20,
                       "dir": "Open Long" if side == "LONG" else "Open Short"}]
        self.funding = [{"time": START+i*HOUR+100, "delta": {
            "type": "funding", "coin": coin, "usdc": "-.01" if side == "LONG" else ".01", "szi": sign}}
            for i in (1, 2)]
        self.fees = {"userCrossRate": ".00045"}
        self.metadata = {"universe": [{"name": coin, "szDecimals": 5}]}
        self.requests = []
    def _info(self, payload):
        self.requests.append(deepcopy(payload))
        typ = payload["type"]
        if typ == "userFillsByTime": return deepcopy(self.fills)
        if typ == "userFunding": return deepcopy(self.funding)
        if typ == "userFees": return deepcopy(self.fees)
        if typ == "meta": return deepcopy(self.metadata)
        raise AssertionError("Unexpected public request: " + typ)


class AiResearchLiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clock = patch("time.time", return_value=NOW/1000)
        clock.start(); self.addCleanup(clock.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        network.start(); self.addCleanup(network.stop)
        self.review = AiReview(self.tmp.name)
        self.pid = "offline-adapter-review"
        self.configure()

    def configure(self, coin="BTC", side="LONG"):
        dex = coin.split(":")[0] if ":" in coin else ""
        price = 90. if side == "LONG" else 110.
        self.position = {"coin": coin, "dex": dex, "side": side, "size": 1.,
                         "entry_price": 100., "leverage": 5., "margin_used": 20.,
                         "roe": -50., "liquidation_price": 80. if side == "LONG" else 120.,
                         "margin_mode": "cross"}
        self.client, self.reader = Client(deepcopy(self.position)), Reader(coin, side)
        self.profile = {"user_id": 1, "account": {"address": ACCOUNT}, "runtime": {}}
        self.payload = {"position": identity(self.position), "price": price, "action": "HOLD",
                        "factors": {"asof_ms": NOW, "candle_close_ms": NOW//900000*900000-1,
                                    "funding_bps_hour": 1., "open_interest": 1000.,
                                    "trend_ema20_50": {"ema20": 99., "ema50": 100.},
                                    "rsi14": 40., "macd_hist": -.1, "atr14_pct": 1.2,
                                    "volume_ratio20": 1., "levels20": {"support": 85, "resistance": 105}},
                        "gate": {"allowed": False, "source_budget": {
                            "available": True, "source": SOURCE, "slot_usdc": 100., "reserved_usdc": 20.,
                            "extra_spent_usdc": 0., "additions_count": 0}}}
        with closing(self.review.connect()) as db:
            db.execute("INSERT OR REPLACE INTO proposals(id,user_id,account,market,created,expires,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                       (self.pid, "1", ACCOUNT, market_key(self.position), NOW/1000, NOW/1000+300,
                        "INFORMATION", json.dumps(self.payload)))
            db.commit()
        op = self.review.execution_journal.prepare(ACCOUNT, market_key(self.position), {"action": "OPEN"})
        self.review.execution_journal.finish(op, {"ok": True}, {
            "managed": True, "side": side, "size": 1., "position": deepcopy(self.position),
            "source_targets": [{"wallet": SOURCE, "signed_notional": price*(1 if side == "LONG" else -1),
                                "margin": 20., "slot_budget": 100.}]})

    def run_adapter(self):
        return self.review.register_research(1, self.profile, self.client, self.reader,
                                            self.position, self.pid, self.payload, NOW)

    def proposal_payload(self):
        with closing(self.review.connect()) as db:
            return json.loads(db.execute("SELECT payload FROM proposals WHERE id=?", (self.pid,)).fetchone()[0])

    def test_full_raw_crypto_history_registers_waiting_study_and_offline_candidates(self):
        result = self.run_adapter()
        self.assertEqual(result["status"], "WAITING_START", result)
        self.assertEqual(result["start_ms"], (NOW//900000+1)*900000)
        risk, costs = result["payload"]["risk"], result["payload"]["costs"]
        self.assertAlmostEqual(risk["risk_capital_usdc"], 20.)
        self.assertAlmostEqual(risk["paid_costs_usdc"], .12)
        self.assertAlmostEqual(risk["realized_pnl_usdc"], 0.)
        self.assertAlmostEqual(costs["exit_fee_bps"], 4.5)
        self.assertAlmostEqual(costs["funding_usdc_hour"], .009)
        payload = self.proposal_payload()
        self.assertEqual(payload["research_input_status"], "VERIFIED")
        self.assertTrue(payload["candidates"])
        self.assertTrue(all(row["executable"] is False and row["probability"] is None for row in payload["candidates"]))
        self.assertTrue({"HOLD", "REDUCE", "AVERAGE"} <= {r["action"] for r in payload["candidates"]})
        self.assertEqual([p["type"] for p in self.reader.requests], ["userFillsByTime", "userFunding", "userFees", "meta"])

    def test_readonly_non_signing_client_uses_public_precision_not_zero_sdk_step(self):
        self.client.exchange = None
        def no_signing_precision(*args):
            raise AssertionError("A read-only observer must not depend on Exchange precision helpers")
        self.client.size_step = no_signing_precision
        result = self.run_adapter()
        self.assertEqual(result["status"], "WAITING_START", result)
        payload = self.proposal_payload()
        self.assertEqual(payload["research_input_status"], "VERIFIED")
        self.assertTrue(payload["candidates"])
        self.assertTrue(all(candidate["executable"] is False for candidate in payload["candidates"]))
        self.assertIn({"type": "meta"}, self.reader.requests)
        self.assertIsNone(self.client.exchange)

    def test_malformed_public_precision_is_unavailable_never_guessed(self):
        malformed = [None, {}, {"universe": {}}, {"universe": [None]},
                     {"universe": [{"name": "ETH", "szDecimals": 5}]},
                     {"universe": [{"name": "BTC", "szDecimals": 5}] * 2}]
        malformed += [{"universe": [{"name": "BTC", "szDecimals": value}]}
                      for value in (None, True, -1, 7, 5.5, "5", float("nan"))]
        for index, metadata in enumerate(malformed):
            with self.subTest(metadata=metadata):
                self.pid = f"invalid-precision-{index}"
                self.configure()
                self.reader.metadata = metadata
                result = self.run_adapter()
                self.assertEqual(result["status"], "UNAVAILABLE")
                payload = self.proposal_payload()
                self.assertEqual(payload["research_input_status"], "UNAVAILABLE")
                self.assertEqual(payload["candidates"], [])
                self.assertNotIn("risk", result["payload"])
                self.assertNotIn("costs", result["payload"])
                self.assertTrue(payload["research_unavailable_reason"].startswith("research_"))

    def test_positive_funding_is_payment_for_long_credit_for_short(self):
        self.configure(side="SHORT")
        result = self.run_adapter()
        self.assertEqual(result["status"], "WAITING_START", result)
        self.assertAlmostEqual(result["payload"]["costs"]["funding_usdc_hour"], -.011)
        self.assertAlmostEqual(result["payload"]["risk"]["realized_pnl_usdc"], .02)
        self.assertAlmostEqual(result["payload"]["risk"]["paid_costs_usdc"], .1)

    def test_negative_funding_reverses_the_forward_cashflow_sign(self):
        self.payload["factors"]["funding_bps_hour"] = -1.
        result = self.run_adapter()
        self.assertAlmostEqual(result["payload"]["costs"]["funding_usdc_hour"], -.009)

    def test_position_changed_after_review_is_unavailable_not_refrozen(self):
        self.client.row["size"] = 2
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        payload = self.proposal_payload()
        self.assertEqual(payload["research_unavailable_reason"], "position_changed_during_review")
        self.assertEqual(payload["candidates"], [])
        self.assertNotIn("risk", result["payload"])

    def test_external_opening_after_verified_ownership_is_rejected(self):
        key = market_key(self.position)
        record = self.review.execution_journal.owned(ACCOUNT)[key]
        record["verified_at_ms"] = OPEN-1
        with closing(self.review.execution_journal.connect()) as db:
            db.execute("UPDATE ownership SET record=? WHERE account=? AND market=?", (json.dumps(record), ACCOUNT, key))
            db.commit()
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.proposal_payload()["research_unavailable_reason"], "fill_after_verified_ownership")

    def test_unknown_historical_fee_cannot_become_zero_costs(self):
        self.reader.fills[0].pop("fee")
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.proposal_payload()["research_input_status"], "UNAVAILABLE")
        self.assertNotIn("costs", result["payload"])

    def test_non_usdc_historical_fee_is_unavailable(self):
        self.reader.fills[0]["feeToken"] = "HYPE"
        self.assertEqual(self.run_adapter()["status"], "UNAVAILABLE")

    def test_missing_funding_hour_is_not_assumed_zero(self):
        self.reader.funding.pop()
        self.assertEqual(self.run_adapter()["status"], "UNAVAILABLE")
        self.assertIn("funding_hour_coverage", self.proposal_payload()["research_unavailable_reason"])

    def test_missing_current_fee_rate_is_not_assumed_zero(self):
        self.reader.fees = {}
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotIn("costs", result["payload"])

    def test_xyz_fee_model_is_explicitly_unavailable(self):
        self.configure(coin="xyz:INTC")
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.proposal_payload()["research_unavailable_reason"], "builder_perp_cost_model_not_verified")

    def test_prior_budget_usage_prevents_new_baseline(self):
        self.profile["runtime"]["ai_budget_usage"] = {market_key(self.position): {"extra_spent_usdc": 10}}
        result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.proposal_payload()["research_unavailable_reason"], "prior_intervention_requires_frozen_lifecycle_ledger")
        self.assertEqual(self.reader.requests, [])

    def test_prior_executed_proposal_prevents_new_baseline(self):
        with closing(self.review.connect()) as db:
            db.execute("INSERT INTO proposals(id,user_id,account,market,created,expires,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                       ("older", "1", ACCOUNT, market_key(self.position), NOW/1000-100, NOW/1000,
                        "EXECUTED", "{}"))
            db.commit()
        self.assertEqual(self.run_adapter()["status"], "UNAVAILABLE")
        self.assertEqual(self.reader.requests, [])

    def test_review_crossing_candle_boundary_cannot_be_backdated(self):
        with patch("time.time", return_value=(NOW+900000)/1000):
            result = self.run_adapter()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.proposal_payload()["research_unavailable_reason"], "review_crossed_forward_candle_boundary")

    def test_research_registration_does_not_mutate_account_position(self):
        before = deepcopy(self.client.row)
        self.run_adapter()
        self.assertEqual(self.client.row, before)


if __name__ == "__main__":
    unittest.main()
