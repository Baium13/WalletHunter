"""Public-only observer and notification formatting, without Telegram startup."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock
from core.ai_position_observer import AiPositionObserver, position_notice, position_app_url
from core.ai_review import account_guard
from tests import test_engine_safety as fixtures


class PositionObserverTests(unittest.TestCase):
    setUp=fixtures.EngineSafetyTests.setUp
    patch=fixtures.EngineSafetyTests.patch

    def observer(self):
        self.actions=Mock()
        self.actions.prepare.return_value={'pending':[]}
        self.actions.pending_for_notification.return_value=[]
        self.public=Mock(return_value=object())
        return AiPositionObserver(self.directory.name,self.store,self.actions,self.public,object())

    def test_watch_prepares_but_does_not_execute_or_change_profile(self):
        observer=self.observer();before=self.store.load()
        observer.prepare(1)
        self.assertEqual(self.actions.prepare.call_count,1)
        self.assertFalse(self.actions.prepare.call_args.kwargs['force_refresh'])
        self.assertEqual(self.store.load(),before)
        self.actions.decide.assert_not_called()

    def test_disabled_observer_does_not_read_market_or_prepare(self):
        observer=self.observer();self.patch(ai_review_enabled=False)
        self.assertIsNone(observer.prepare(1));self.public.assert_not_called();self.actions.prepare.assert_not_called()

    def test_profile_and_account_guards_serialize_prepare(self):
        observer=self.observer()
        for target in ('telegram-profile:1',fixtures.ACCOUNT):
            with account_guard(self.directory.name,target):
                with self.assertRaises(OSError):observer.prepare(1)
        self.actions.prepare.assert_not_called()

    def test_muting_notifications_keeps_preparation_enabled(self):
        observer=self.observer();self.patch(notifications=False)
        observer.prepare(1)
        self.assertEqual(observer.notices(1)[0],[])
        self.actions.pending_for_notification.assert_not_called()
        self.assertEqual(self.actions.prepare.call_count,1)

    def test_notifications_group_by_market_not_action(self):
        observer=self.observer()
        self.actions.pending_for_notification.return_value=[{'payload':{'coin':coin,'dex':'','action':action}}
            for coin,action in (('ETH','REDUCE'),('ETH','AVERAGE'),('BTC','REDUCE'))]
        batches,_=observer.notices(1)
        self.assertEqual([len(b) for b in batches],[2,1])

    def test_notice_is_bilingual_no_private_values_no_profit_claim(self):
        rows=[{'payload':{'coin':'ETH','action':'REDUCE','position_before':{'roe':-53.5},'source_wallet':'private-wallet','source_slot_usdc':123456}}]
        for en in (False,True):
            value=position_notice(rows,en)
            self.assertNotIn('private-wallet',value);self.assertNotIn('123456',value)
            self.assertIn('-53.50%',value)
            self.assertNotIn('60%',value)
            if en:self.assertFalse(any('\u0400'<=c<='\u04ff' for c in value))

    def test_deep_link_preserves_host_and_query_no_external_redirect(self):
        self.assertEqual(position_app_url('https://example.test/?v=10'),'https://example.test/?v=10#ai-position')
        for url in ('javascript:alert(1)','http://example.test','https://user:pass@example.test'):
            with self.assertRaises(ValueError):position_app_url(url)

    def test_desktop_background_path_contains_no_signer_or_decision(self):
        path=Path(__file__).resolve().parents[1]/'desktop/main.py'
        tree=ast.parse(path.read_text(encoding='utf-8-sig'))
        for watcher in ('ai_position_watcher','ai_entry_watcher'):
            node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==watcher)
            attrs={n.attr for n in ast.walk(node) if isinstance(n,ast.Attribute)}
            names={n.id for n in ast.walk(node) if isinstance(n,ast.Name)}
            self.assertFalse({'decide','submit_position_ioc','submit_user_ioc','market_open','market_reduce','decrypt'}&attrs)
            self.assertNotIn('account_client',names)
