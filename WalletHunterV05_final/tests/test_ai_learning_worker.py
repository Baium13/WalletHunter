import tempfile
import unittest
from unittest.mock import patch
from core.ai_learning_worker import AiLearningWorker, BAR_MS
from core.ai_review import account_guard

NOW = 2000000 * BAR_MS + 1000


class Learner:
    def __init__(self): self.ingested=[]; self.trained=[]; self.fail=False
    def ingest(self, coin, rows, now_ms):
        if self.fail: raise ValueError('bad historical revision')
        self.ingested.append((coin, rows, now_ms))
    def train(self, now_ms): self.trained.append(now_ms)
    def summary(self, now_ms=None): return {'status':'COLLECTING', 'counts':{'candles':len(self.ingested)}, 'model':None}


class Reader:
    def __init__(self): self.calls=[];self.fail=set();self.stale=False
    def _info(self, payload):
        self.calls.append(payload)
        req=payload['req'];coin=req['coin']
        if coin in self.fail: raise TimeoutError('sensitive arbitrary error URL')
        end=req['endTime']//BAR_MS*BAR_MS
        if self.stale:end-=BAR_MS
        return [{'t':end-(65-i)*BAR_MS,'T':end-(64-i)*BAR_MS-1,
                 'o':'100','h':'102','l':'99','c':'101','v':'10'} for i in range(65 if self.stale else 66)]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.learner=Learner();self.reader=Reader()
        self.worker=AiLearningWorker(self.tmp.name,self.learner)

    def test_public_only_once_per_candle_with_restart_and_no_current_bar(self):
        result=self.worker.cycle(self.reader,NOW)
        self.assertEqual(result['collector']['status'],'OK')
        self.assertFalse(result['real_execution_available'])
        self.assertEqual(len(self.reader.calls),2)
        self.assertEqual(self.learner.trained,[NOW])
        self.assertTrue(all(row['T']<NOW for _,rows,_ in self.learner.ingested for row in rows))
        self.assertTrue(all('user' not in call and call['type']=='candleSnapshot' for call in self.reader.calls))
        AiLearningWorker(self.tmp.name,self.learner).cycle(self.reader,NOW+1000)
        self.assertEqual(len(self.reader.calls),2)
        self.worker.cycle(self.reader,NOW+BAR_MS)
        self.assertEqual(len(self.reader.calls),4)
        self.assertLess(self.reader.calls[-1]['req']['endTime']-self.reader.calls[-1]['req']['startTime'],100*BAR_MS)

    def test_partial_failure_never_trains_or_claims_current_success(self):
        self.worker.cycle(self.reader,NOW)
        self.reader.fail={'ETH'}
        result=self.worker.cycle(self.reader,NOW+BAR_MS)
        self.assertEqual(result['collector']['status'],'PARTIAL_DATA')
        self.assertEqual(result['collector']['last_success_ms'],NOW)
        self.assertEqual(self.learner.trained,[NOW])
        self.assertNotIn('sensitive',str(result))
        self.assertEqual(AiLearningWorker(self.tmp.name,self.learner).summary()['collector'],result['collector'])

    def test_bad_or_stale_history_does_not_train(self):
        self.learner.fail=True
        self.assertEqual(self.worker.cycle(self.reader,NOW)['collector']['status'],'UNAVAILABLE')
        self.assertEqual(self.learner.trained,[])
        self.learner.fail=False;self.reader.stale=True
        self.assertEqual(self.worker.cycle(self.reader,NOW+BAR_MS)['collector']['status'],'UNAVAILABLE')
        self.assertEqual(self.learner.trained,[])

    def test_global_lock_blocks_duplicate_worker_without_fetch(self):
        with account_guard(self.tmp.name,'ai-learning-public-collector'):
            self.assertEqual(self.worker.cycle(self.reader,NOW)['collector']['status'],'BUSY')
        self.assertEqual(self.reader.calls,[])

    def test_summary_never_fetches_or_trains_and_invalid_clock_rejected(self):
        self.assertEqual(self.worker.summary()['collector']['status'],'NOT_STARTED')
        for value in (True, 0, float('nan'), '100'):
            with self.assertRaises(ValueError):self.worker.cycle(self.reader,value)
        self.assertEqual(self.reader.calls,[]);self.assertEqual(self.learner.trained,[])

    def test_stopped_collector_does_not_display_old_success_as_current(self):
        self.worker.cycle(self.reader,NOW)
        status=self.worker.summary(NOW+3*BAR_MS)['collector']
        self.assertEqual(status['status'],'STALE')
        self.assertEqual(status['last_success_ms'],NOW)
        self.assertEqual(self.worker._read_status()['status'],'OK')
        self.assertEqual(len(self.reader.calls),2)

    def test_live_cycle_dates_training_after_fetch_and_rejects_crossed_bar(self):
        with patch('core.ai_learning_worker.time.time', side_effect=[NOW/1000,(NOW+30000)/1000]):
            result=self.worker.cycle(self.reader)
        self.assertEqual(self.learner.trained,[NOW+30000])
        self.assertEqual(result['collector']['last_success_ms'],NOW+30000)
        with patch('core.ai_learning_worker.time.time', side_effect=[(NOW+BAR_MS)/1000,(NOW+2*BAR_MS)/1000]):
            result=self.worker.cycle(self.reader)
        self.assertEqual(result['collector']['status'],'UNAVAILABLE')
        self.assertEqual(self.learner.trained,[NOW+30000])
