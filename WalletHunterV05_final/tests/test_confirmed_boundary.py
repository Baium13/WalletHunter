"""Real product services and gateway; only Hyperliquid transport is fake."""
import json
import tempfile
import time
import unittest
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from core.confirmed_execution_adapter import build_context, execute_confirmed_ai
from core.execution_journal import ExecutionJournal
from core.manual_positions import ManualPositions, ManualActionError
from core.foundation.store import Store
from test_engine_safety import ExchangeBoundary, position, ACCOUNT


class Boundary(ExchangeBoundary):
    def __init__(self,journal):
        super().__init__(); self.journal=journal; self.protection=[]; self.reject=False; self.lose_ack=False; self.bad_book=False
    def post(self,path,request):
        if self.bad_book: raise ValueError('unavailable')
        return {'time':int(time.time()*1000),'levels':[[{'px':'99.99','sz':'100'}],[{'px':'100.01','sz':'100'}]]}
    def frontend_open_orders(self,dex=''):
        return deepcopy([o for o in self.protection if (':' in o['coin']) == bool(dex)])
    def durable(self):
        with closing(self.journal.connect()) as db:
            rows=db.execute("SELECT i.body,i.reservation,o.intent FROM intents i JOIN operations o ON o.id=json_extract(i.body,'$.parent_intent_id') WHERE i.status='SUBMITTING'").fetchall()
        assert rows, 'reservation must precede adapter mutation'
        intent=json.loads(rows[-1][0]); envelope=json.loads(rows[-1][2])
        assert intent['intent_id'] in envelope['canonical_intents']
    def submit_copy_ioc(self,*args,**kwargs):
        self.durable()
        if self.reject: self.fill_fraction=0.
        result=super().submit_copy_ioc(*args,**kwargs)
        if self.lose_ack: raise TimeoutError('synthetic private data must not escape')
        return result
    def _trade(self,action,coin,buy,size,dex):
        key=(coin,dex);before=deepcopy(self.rows.get(key))
        if before:self.leverages[key]=before['leverage']
        result=super()._trade(action,coin,buy,size,dex)
        after=self.rows.get(key)
        if before and after and before['side']==after['side']:
            after['entry_price']=(before['entry_price'] if action=='reduce' else
                (before['size']*before['entry_price']+(after['size']-before['size'])*self.mid(coin,dex))/after['size'])
        return result
    def place_stop_loss(self,coin,side,size,price,dex='',*,cloid=None):
        self.durable(); oid=len(self.protection)+10
        self.protection.append({'coin':coin,'oid':oid,'side':'A' if side=='LONG' else 'B','sz':str(size),
            'cloid':cloid,'reduceOnly':True,'isTrigger':True,'orderType':'Stop Market','triggerPx':str(price)})
        self.calls.append(('protect',oid))
        return {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':oid}}]}}}
    def cancel_order(self,coin,oid,dex=''):
        self.durable(); self.protection=[o for o in self.protection if o['oid']!=oid]
        self.calls.append(('cancel',oid)); return {'status':'ok'}


class ConfirmedBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.journal=ExecutionJournal(self.tmp.name); self.client=Boundary(self.journal)
        self.settings=SimpleNamespace(hl_mode='TESTNET',max_leverage=40,max_slippage_pct=.5,
            max_total_exposure_usd=100000.,entry_price_tolerance_pct=.5)
        self.profile={'account':{'address':ACCOUNT},'runtime':{}}
        self.stamp=(20000*86400000+3600000+1000)/1000
        self.clock=patch('time.time',lambda:self.stamp);self.clock.start(); self.addCleanup(self.clock.stop)
    def context(self,request):
        return build_context(self.client,tenant='1',coin=request['coin'],dex=request.get('dex',''),action=request['action'],
            source=request.get('source','manual'),settings=self.settings,profile=self.profile,journal=self.journal,request=request)
    def manual(self):
        return ManualPositions(self.client,self.profile['runtime'],lambda:None,canonical_context=self.context,delay=0)
    def test_actual_manual_close_gateway(self):
        self.client.seed(position(leverage=5))
        result=self.manual().close_position('BTC','')
        self.assertTrue(result['closed']);self.assertEqual(self.client.positions(),[])
        with closing(self.journal.connect()) as db: row=db.execute('SELECT body,status FROM intents').fetchone()
        self.assertEqual(row['status'],'FILLED');self.assertEqual(json.loads(row['body'])['version'],4)
    def test_stop_replace_cancel_leaves_unrelated_orders(self):
        self.client.seed(position(leverage=5)); m=self.manual()
        m.set_stop_loss('BTC','',90.)
        self.client.protection.append(dict(self.client.protection[0],oid=999,cloid='external'))
        self.stamp+=1; m.set_stop_loss('BTC','',89.)
        self.assertIn(999,[o['oid'] for o in self.client.protection])
        self.stamp+=1; m.delete_stop_loss('BTC','')
        self.assertEqual([o['oid'] for o in self.client.protection],[999])
    def test_close_preserves_unrelated_protection(self):
        self.client.seed(position(leverage=5)); m=self.manual();m.set_stop_loss('BTC','',90.)
        self.stamp+=1;m.close_position('BTC','')
        self.assertEqual(self.client.protection,[])
    def test_reduction_survives_missing_entry_book_and_capital(self):
        self.client.seed(position(leverage=5));self.client.bad_book=True
        self.client.available_margin=lambda dex: (_ for _ in ()).throw(ValueError('missing'))
        self.assertTrue(self.manual().close_position('BTC','')['closed'])
    def test_unknown_close_keeps_hold_and_cannot_retry(self):
        self.client.seed(position(leverage=5));self.client.lose_ack=True
        with self.assertRaises(ManualActionError): self.manual().close_position('BTC','')
        calls=len(self.client.calls)
        with self.assertRaises(ManualActionError): self.manual().close_position('BTC','')
        self.assertEqual(len(self.client.calls),calls)
        self.assertTrue(self.journal.pending(ACCOUNT))
    def test_explicit_risk_rejection_is_not_unknown(self):
        self.client.seed(position(leverage=5));self.settings.max_slippage_pct=.5
        m=self.manual(); original=self.context
        def wrong(request):
            context=original(request)
            context['before']=context['before'].model_copy(update={'received_ms':context['now']-60000})
            return context
        m.canonical_context=wrong
        with self.assertRaises(ManualActionError):m.close_position('BTC','')
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.profile['runtime']['manual_actions']['BTC|']['status'],'rejected')
        self.assertEqual(self.journal.pending(ACCOUNT),set())
    def test_invalid_network_never_mutates(self):
        self.client.seed(position(leverage=5));self.client.network='MAINNET'
        with self.assertRaises(ManualActionError):self.manual().close_position('BTC','')
        self.assertEqual(self.client.calls,[])
    def test_confirmed_entry_exact_cloid_and_budget(self):
        parent=self.journal.prepare(ACCOUNT,'BTC|',{'network':'TESTNET','proposal_id':'proposal-a'})
        response=execute_confirmed_ai(self.context,coin='BTC',side='BUY',size=.4,price=100.5,
            identity='proposal-a',operation_id=parent,leverage=5,allocation_limit=30.,exchange_client_id='0x'+'a'*32)
        self.assertEqual(response.status,'FILLED');self.assertIn('0x'+'a'*32,self.client.orders)
        with closing(self.journal.connect()) as db:row=db.execute('SELECT body FROM intents').fetchone()
        self.assertEqual(json.loads(row[0])['correlation_id'],'proposal-a')
    def test_missing_book_blocks_entry(self):
        self.client.bad_book=True
        parent=self.journal.prepare(ACCOUNT,'BTC|',{'network':'TESTNET'})
        response=execute_confirmed_ai(self.context,coin='BTC',side='BUY',size=.4,price=100.5,
            identity='proposal-a',operation_id=parent,leverage=5,allocation_limit=30.)
        self.assertEqual(response.status,'REJECTED');self.assertEqual(self.client.calls,[])

    def test_actual_confirmed_ai_service_prepares_and_executes_once(self):
        from core.ai_user_orders import AiUserOrders
        from test_ai_user_orders import learning
        self.client.mode='TESTNET'
        self.client.cash=3000.
        self.client.meta=lambda:{'universe':[{'name':'BTC','szDecimals':6,'maxLeverage':40}]}
        self.client.capital_snapshot=lambda:SimpleNamespace(mode='unifiedAccount',sizing_base_usdc=self.client.cash)
        self.profile.update(leaders=[],ai_slot_selected=True)
        service=AiUserOrders(self.tmp.name,monotonic=lambda:0.)
        now=int(self.stamp*1000)
        proposal=service.prepare('1',self.profile,self.client,None,learning(now=now),now)['pending'][0]
        def context(payload):
            return build_context(self.client,tenant='1',coin=payload['coin'],action=payload['action'],source='ai',
                settings=self.settings,profile=self.profile,journal=self.journal,request=payload)
        self.stamp+=1
        result=service.decide('1',proposal['id'],True,self.profile,self.client,lambda:self.client,lambda:None,
            int(self.stamp*1000),canonical_context=context)
        self.assertEqual(result['status'],'FILLED',result)
        calls=len(self.client.calls)
        again=service.decide('1',proposal['id'],True,self.profile,self.client,lambda:self.client,lambda:None,
            int(self.stamp*1000),canonical_context=context)
        self.assertEqual(again['status'],'FILLED');self.assertEqual(len(self.client.calls),calls)
        self.assertIn(proposal['payload']['cloid'],self.client.orders)

    def test_actual_ai_position_service_uses_gateway_and_preserves_fill_proof(self):
        from test_ai_position_actions import PositionActionTests, NOW
        for action in ('REDUCE','AVERAGE'):
            with self.subTest(action=action):
                case=PositionActionTests();case.setUp()
                try:
                    case.service.legacy_test_executor=None
                    case.public.base='https://api.hyperliquid-testnet.xyz'
                    case.public.network='TESTNET'
                    boundary=Boundary(case.service.journal)
                    boundary.address=case.public.address
                    boundary.cash=3000.
                    row=dict(case.public.live[0],position_value=case.public.live[0]['size']*case.public.price)
                    boundary.seed(row);boundary.prices[('ETH','')]=case.public.price
                    boundary.post=lambda *args:{'time':NOW+1000,'levels':[[{'px':'2509.99'}],[{'px':'2510.01'}]]}
                    saved=case.service.journal.owned(boundary.address)['ETH|']
                    op=case.service.journal.prepare(boundary.address,'ETH|',{'network':'TESTNET'})
                    case.service.journal.finish(op,{'ok':True},dict(saved,network='TESTNET'))
                    proposal=case.proposal(action)
                    def context(payload):
                        return build_context(boundary,tenant='139',coin='ETH',action=payload['action'],source=payload['source_wallet'],
                            settings=self.settings,profile=case.profile,journal=case.service.journal,request=payload)
                    with patch('time.time',lambda:(NOW+1000)/1000):
                        result=case.service.decide('139',proposal['id'],True,case.profile,case.public,lambda:boundary,
                            case.persist,NOW+1000,canonical_context=context)
                    self.assertEqual(result['status'],'FILLED',result)
                    saved=case.service.journal.owned(boundary.address)['ETH|']
                    self.assertEqual(saved['execution_evidence']['intent_id'],proposal['id']+('-add' if action=='AVERAGE' else '-reduce'))
                    self.assertTrue(saved['execution_evidence']['trade_ids'])
                    self.assertEqual(saved['source_targets'][0]['wallet'],proposal['payload']['source_wallet'])
                finally:case.doCleanups()
