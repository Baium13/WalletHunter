import unittest
from core.ai_policy import ReviewPolicy, source_budget


class PolicyTests(unittest.TestCase):
    def test_user_confirmed_limits(self):
        p=ReviewPolicy()
        self.assertEqual((p.target_roe_pct,p.failure_roe_pct,p.max_extra_slot_fraction,p.max_additions),(3,-120,.5,4))

    def test_no_cross_source_borrowing_and_max_additions(self):
        position={"coin":"BTC","side":"SHORT","size":1}
        profile={"leaders":["leader"],"runtime":{}}
        owned={"BTC|":{"managed":True,"position":position,"source_targets":[{"wallet":"leader","margin":80}]}}
        budget=source_budget(profile,position,300,owned)
        self.assertEqual(budget["slot_usdc"],100)
        self.assertEqual(budget["remaining_extra_usdc"],20)
        profile["runtime"]["ai_budget_usage"]={"BTC|":{"extra_margin_usdc":10,"additions":4}}
        self.assertEqual(source_budget(profile,position,300,owned)["remaining_extra_usdc"],0)

    def test_missing_or_shared_source_is_unavailable(self):
        p={"coin":"BTC","size":1,"side":"LONG"}
        self.assertFalse(source_budget({},p,300,{})["available"])
        self.assertFalse(source_budget({},p,float("nan"),{})["available"])

    def test_two_positions_share_one_source_extra_allowance(self):
        position={"coin":"BTC","side":"SHORT","size":1}
        owned={"BTC|":{"managed":True,"position":position,"source_targets":[{"wallet":"leader","margin":10}]}}
        profile={"leaders":["leader"],"runtime":{"ai_budget_usage":{
            "ETH|":{"source_wallet":"leader","extra_margin_usdc":40,"additions":3}}}}
        budget=source_budget(profile,position,300,owned)
        self.assertEqual(budget["remaining_extra_usdc"],10)
        self.assertEqual(budget["additions_remaining"],1)
