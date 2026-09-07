import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing

from core.ai_outcomes import OutcomeCosts, OutcomePolicy
from core.ai_research import AiResearch, INTERVAL_MS, USER_RESEARCH_POLICY
from core.execution_journal import ExecutionJournal


class Reader:
    def __init__(self, bars=()):
        self.bars, self.calls, self.fail = list(bars), [], False
    def _info(self, request):
        self.calls.append(request)
        if self.fail: raise TimeoutError("public data unavailable")
        return copy.deepcopy(self.bars)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = OutcomePolicy(3, -120, 2*INTERVAL_MS, INTERVAL_MS, 2100000)
        self.research = AiResearch(self.tmp.name, self.policy)
        self.journal = ExecutionJournal(self.tmp.name)
        self.position = {"coin": "BTC", "dex": "", "side": "LONG", "size": 1,
                         "entry_price": 100, "roe": -50, "margin_used": 10, "liquidation_price": 91}
        self.now = INTERVAL_MS+1
        self.start = 2*INTERVAL_MS
        self.review = {"price": 95, "factors": {"rsi14": 30, "asof_ms": self.now,
                                                "candle_close_ms": INTERVAL_MS-1}}
        self.risk = {"basis": "original_pre_intervention", "risk_capital_usdc": 10,
                     "realized_pnl_usdc": 0, "paid_costs_usdc": 0,
                     "evidence": "TEST FIXTURE: known first-intervention snapshot, no prior costs"}
        self.costs = OutcomeCosts(0, 10, 10, .01)
        self.ownership = {"managed": True, "size": 1, "side": "LONG", "source_targets": [
            {"wallet": "source1", "signed_notional": 95, "slot_budget": 30, "margin": 10}]}
        self.own(self.ownership)

    def own(self, record, account="account1", market="BTC|"):
        operation = self.journal.prepare(account, market, {"test": True})
        self.journal.finish(operation, {"ok": True}, record)

    def register(self, rid="r1", owner="1", account="account1", position=None, review=None, **kwargs):
        values = {"risk_basis": self.risk, "costs": self.costs,
                  "costs_source": "explicit_test_cost_model_not_actual_fills", "now_ms": self.now}
        values.update(kwargs)
        return self.research.register(owner, account, rid, position or self.position,
                                      review or self.review, self.journal, **values)

    def bar(self, start=None, o=95, h=96, l=94, c=95):
        start = self.start if start is None else start
        return {"t": start, "T": start+INTERVAL_MS-1, "o": o, "h": h, "l": l, "c": c}

    def test_user_policy_is_explicit_and_not_an_execution_gate(self):
        self.assertEqual(USER_RESEARCH_POLICY.target_roe_pct, 3)
        self.assertEqual(USER_RESEARCH_POLICY.loss_roe_pct, -120)
        self.assertEqual(USER_RESEARCH_POLICY.horizon_ms, 86400000)
        self.assertFalse(self.research.summary("1")["ready_for_live_trading"])
        self.assertIsNone(self.research.summary("1")["probability"])

    def test_register_freezes_next_boundary_not_review_time(self):
        result = self.register()
        self.assertEqual(result["status"], "WAITING_START")
        self.assertEqual(result["start_ms"], self.start)
        self.assertEqual(result["payload"]["position"]["size"], 1)
        self.assertFalse(result["payload"]["cost_assumptions_are_actual_fills"])
        self.review["factors"]["rsi14"] = 99
        self.position["size"] = 2
        frozen = self.research.get("1", result["id"])
        self.assertEqual(frozen["payload"]["features"]["rsi14"], 30)
        self.assertEqual(frozen["payload"]["position"]["size"], 1)

    def test_idempotency_and_restart_of_intent(self):
        result = self.register()
        self.assertEqual(self.register()["id"], result["id"])
        self.assertEqual(AiResearch(self.tmp.name, self.policy).get("1", result["id"]), result)
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.register(costs=OutcomeCosts(0, 20, 0, 0))
        with closing(self.research.connect()) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE research_intents SET payload='{}'")

    def test_missing_original_risk_or_costs_is_recorded_not_assumed(self):
        risk = self.register("risk", risk_basis=None)
        costs = self.register("costs", costs=None)
        source = self.register("costsource", costs_source=None)
        self.assertEqual(risk["reason"], "original_risk_basis_unavailable")
        self.assertEqual(costs["reason"], "explicit_cost_model_unavailable")
        self.assertEqual(source["status"], "UNAVAILABLE")
        reader = Reader()
        self.assertEqual(self.research.poll("1", reader, self.start+INTERVAL_MS), [])
        self.assertEqual(reader.calls, [])

    def test_missing_or_mixed_ownership_is_not_guessed(self):
        self.own(dict(self.ownership, managed=False))
        self.assertEqual(self.register("unmanaged")["reason"], "source_ownership_unavailable")
        mixed = copy.deepcopy(self.ownership)
        mixed["source_targets"].append({"wallet": "source2", "signed_notional": -20, "slot_budget": 30})
        self.own(mixed)
        self.assertEqual(self.register("mixed")["reason"], "mixed_or_missing_source_attribution")

    def test_stale_ownership_size_and_opposite_direction_rejected(self):
        self.own(dict(self.ownership, size=2))
        self.assertEqual(self.register("size")["reason"], "ownership_position_mismatch")
        invalid = copy.deepcopy(self.ownership)
        invalid["source_targets"][0]["signed_notional"] = -95
        self.own(invalid)
        self.assertEqual(self.register("direction")["reason"], "source_direction_conflict")

    def test_nan_missing_liquidation_and_invalid_margin_unavailable(self):
        for name, value in (("size", float("nan")), ("liquidation_price", 0), ("margin_used", None)):
            result = self.register(name, position=dict(self.position, **{name: value}))
            self.assertEqual(result["status"], "UNAVAILABLE")

    def test_future_or_stale_features_never_collected_for_research(self):
        future = copy.deepcopy(self.review)
        future["factors"]["asof_ms"] = self.now+1
        self.assertEqual(self.register("future", review=future)["reason"], "future_or_unclosed_features")
        self.assertEqual(self.register("stale", now_ms=9999999)["reason"], "stale_review_features")

    def test_no_network_before_first_complete_forward_candle(self):
        self.register()
        reader = Reader([self.bar(h=110)])
        self.assertEqual(self.research.poll("1", reader, self.start+INTERVAL_MS-1), [])
        self.assertEqual(len(reader.calls), 0)

    def test_delayed_open_binding_does_not_update_original_features(self):
        registered = self.register()
        reader = Reader([self.bar(o=94, l=93, c=94)])
        result = self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(result[0]["status"], "OBSERVING")
        progress = self.research.get("1", registered["id"])
        snapshot = self.research.outcomes.get("1", progress["scenario_id"])["snapshot"]
        self.assertEqual(snapshot["reference_price"], 94)
        self.assertEqual(snapshot["signal_ms"], self.start)
        self.assertEqual(snapshot["features_asof_ms"], self.now)
        self.assertEqual(snapshot["features"]["rsi14"], 30)
        self.assertEqual(snapshot["original_review_ms"], self.now)
        self.assertEqual(snapshot["risk_capital_usdc"], 10)

    def test_recovery_during_wait_is_not_credited_as_a_success(self):
        self.register()
        reader = Reader([self.bar(o=105, h=106, l=104, c=105)])
        result = self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(result[0]["reason"], "already_outside_barriers_at_delayed_start")
        self.assertEqual(self.research.summary("1")["outcomes"]["scenarios"], 0)

    def test_liquidation_before_delayed_start_is_excluded_not_success(self):
        self.register()
        reader = Reader([self.bar(o=90, h=91, l=89, c=90)])
        result = self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(result[0]["status"], "UNAVAILABLE")

    def test_complete_target_after_delayed_start_survives_restart(self):
        registered = self.register()
        reader = Reader([self.bar(h=101)])
        result = self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(result[0]["outcome"]["status"], "TARGET")
        restarted = AiResearch(self.tmp.name, self.policy)
        self.assertEqual(restarted.get("1", registered["id"])["status"], "COMPLETE")
        self.assertEqual(restarted.poll("1", reader, self.start+2*INTERVAL_MS), [])
        self.assertEqual(len(reader.calls), 1)

    def test_restart_continues_existing_open_study(self):
        registered = self.register()
        reader = Reader([self.bar()])
        self.research.poll("1", reader, self.start+INTERVAL_MS)
        restarted = AiResearch(self.tmp.name, self.policy)
        reader.bars.append(self.bar(self.start+INTERVAL_MS))
        result = restarted.poll("1", reader, self.start+2*INTERVAL_MS+5001)
        self.assertEqual(result[0]["outcome"]["status"], "HORIZON")
        self.assertEqual(restarted.get("1", registered["id"])["status"], "COMPLETE")

    def test_market_batching_and_persistent_poll_throttling(self):
        self.register("a")
        self.register("b")
        reader = Reader([self.bar()])
        result = self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(reader.calls), 1)
        AiResearch(self.tmp.name, self.policy).poll("1", reader, self.start+INTERVAL_MS+100)
        self.assertEqual(len(reader.calls), 1)

    def test_public_data_failure_is_retryable_and_throttled(self):
        registered = self.register()
        reader = Reader(); reader.fail = True
        self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(self.research.get("1", registered["id"])["status"], "WAITING_DATA")
        self.research.poll("1", reader, self.start+INTERVAL_MS+100)
        self.assertEqual(len(reader.calls), 1)
        reader.fail = False; reader.bars = [self.bar(h=101)]
        result = self.research.poll("1", reader, self.start+2*INTERVAL_MS+5001)
        self.assertEqual(result[0]["status"], "COMPLETE")

    def test_missing_first_candle_can_be_backfilled(self):
        self.register()
        reader = Reader([self.bar(self.start+INTERVAL_MS, h=101)])
        result = self.research.poll("1", reader, self.start+2*INTERVAL_MS)
        self.assertEqual(result[0]["status"], "WAITING_DATA")
        reader.bars.insert(0, self.bar())
        result = self.research.poll("1", reader, self.start+3*INTERVAL_MS+5001)
        self.assertEqual(result[0]["outcome"]["status"], "TARGET")

    def test_permanently_missing_data_stops_polling(self):
        registered = self.register()
        reader = Reader()
        result = self.research.poll("1", reader, self.start+2*INTERVAL_MS+86400001)
        self.assertEqual(result[0]["status"], "UNAVAILABLE")
        self.assertEqual(self.research.get("1", registered["id"])["status"], "UNAVAILABLE")

    def test_later_feature_mutation_cannot_leak_into_outcome(self):
        registered = self.register()
        self.review["factors"]["rsi14"] = 90
        reader = Reader([self.bar(h=101)])
        self.research.poll("1", reader, self.start+INTERVAL_MS)
        progress = self.research.get("1", registered["id"])
        features = self.research.outcomes.get("1", progress["scenario_id"])["snapshot"]["features"]
        self.assertEqual(features["rsi14"], 30)

    def test_owner_isolation_covers_intents_and_background_poll(self):
        registered = self.register()
        with self.assertRaises(ValueError): self.research.get("2", registered["id"])
        reader = Reader([self.bar(h=101)])
        self.assertEqual(self.research.poll("2", reader, self.start+INTERVAL_MS), [])
        self.assertEqual(len(reader.calls), 0)
        self.assertEqual(self.research.summary("2")["studies"], 0)

    def test_stock_symbol_preserves_its_dex_in_public_request(self):
        self.own(self.ownership, market="xyz:INTC|xyz")
        self.register(position=dict(self.position, coin="INTC", dex="xyz"))
        reader = Reader([self.bar(h=101)])
        self.research.poll("1", reader, self.start+INTERVAL_MS)
        self.assertEqual(reader.calls[0]["req"]["coin"], "xyz:INTC")


if __name__ == "__main__": unittest.main()
