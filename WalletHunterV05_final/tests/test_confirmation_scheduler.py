"""Run just the scheduling functions against fake loops; no credentials/network."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

class StopLoop(BaseException):pass

class ConfirmationSchedulerTests(unittest.TestCase):
    def test_copy_cycle_isolates_failure_and_keeps_other_users_running(self):
        source=Path(__file__).resolve().parents[1]/'desktop/main.py'
        node=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='watcher_cycle')
        calls=[]; messages=[]
        profiles={str(uid):{'copy_enabled':True,'account':{'address':str(uid)},'leaders':[str(uid)],'notifications':False} for uid in (1,2)}
        async def sync(uid,p,c,snapshots,notify):
            calls.append(uid)
            if uid==1:raise ValueError('SECRET failure')
            self.assertEqual(snapshots[0]['wallet'],'2')
        from datetime import datetime
        env={'asyncio':asyncio,'datetime':datetime,'store':SimpleNamespace(load=lambda:{'profiles':profiles},update_runtime=lambda uid,r:None),
             'account_client':lambda uid,p:object(),'reader':SimpleNamespace(positions=lambda *args:[],balance=lambda *args:300),
             'leader_is_enabled':lambda *args:True,'engine':SimpleNamespace(sync_profile=sync),'notify':lambda *args:None,
             'print':lambda *args:messages.append(args)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),env)
        asyncio.run(env['watcher_cycle']())
        self.assertCountEqual(calls,[1,2])
        self.assertNotIn('SECRET',str(messages))
        self.assertIn('ValueError',str(messages))

    def run_worker(self,name,issue=None):
        source=Path(__file__).resolve().parents[1]/'desktop/main.py'
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
        waits=[];checks=[];messages=[]
        async def sleep(seconds):
            waits.append(seconds)
            if len(waits)==2:raise StopLoop()
        async def to_thread(function,*args):return function(*args)
        class Observer:
            def __init__(self,*args):pass
            def prepare(self,uid):
                checks.append(uid)
                if issue:raise issue
                return {'status':'IDLE','reason':'no_signal'}
            def notices(self,uid):return [],{}
        env={'asyncio':SimpleNamespace(sleep=sleep,to_thread=to_thread),
             'ROOT':'fake','store':SimpleNamespace(load=lambda:{'profiles':{'139':{'account':{},'ai_review_enabled':True,'ai_trader_enabled':True}}}),
             'ai_position_actions':None,'ai_user_orders':None,'ai_learning':None,'reader':None,
             'public_account_reader':None,'AiPositionObserver':Observer,'AiEntryObserver':Observer,
             'print':lambda *args:messages.append(args)}
        env['store'].load=lambda:{'profiles':{'139':{'account':{'address':'fake'},'ai_review_enabled':True,'ai_trader_enabled':True}}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),env)
        with self.assertRaises(StopLoop):asyncio.run(env[name]())
        self.assertEqual(checks,[139])
        return waits,messages

    def test_staggered_success_uses_normal_cadence_and_health_record(self):
        for name,start in [('ai_position_watcher',7),('ai_entry_watcher',13)]:
            waits,messages=self.run_worker(name)
            self.assertEqual(waits,[start,60]);self.assertTrue(messages)

    def test_busy_lock_retries_earlier_without_bypassing_guard(self):
        for name,start,retry in [('ai_position_watcher',7,7),('ai_entry_watcher',13,11)]:
            waits,_=self.run_worker(name,BlockingIOError('busy'))
            self.assertEqual(waits,[start,retry])

    def test_network_io_is_visible_and_not_mislabelled_as_busy(self):
        waits,messages=self.run_worker('ai_entry_watcher',OSError('SECRET network text'))
        self.assertEqual(waits,[13,15]);self.assertIn('IO',messages[0][0])
        self.assertNotIn('SECRET',str(messages))
