"""Bounded public discovery buffer; execution fills remain REST-cursor owned."""
from collections import deque, OrderedDict
import threading
import time
import json


class TradeBuffer:
    def __init__(self,capacity=4096):
        self.capacity=capacity
        self.normal=deque();self.critical=deque();self.seen=OrderedDict()
        self.condition=threading.Condition()
        self.protected=frozenset();self.dropped=0;self.dedup=0;self.received=0
        self.last_exchange_ms=None;self.started=time.monotonic()

    def put(self,row,stop):
        key=json.dumps([row.get('coin'),row.get('tid'),row.get('hash'),row.get('time'),row.get('users'),row.get('px'),row.get('sz')],sort_keys=True)
        with self.condition:
            self.received+=1
            if key in self.seen:self.dedup+=1;return
            important=bool(set(row.get('users') or ()) & self.protected)
            while len(self.normal)+len(self.critical)>=self.capacity:
                if self.normal:self.normal.popleft();self.dropped+=1;break
                if not important:self.dropped+=1;return
                # Never discard a protected observation. Apply bounded-memory
                # backpressure; REST fill cursors independently recover fills.
                if stop.is_set():return
                self.condition.wait(.2)
            self.seen[key]=True
            while len(self.seen)>self.capacity*2:self.seen.popitem(last=False)
            (self.critical if important else self.normal).append((time.time(),row))
            if type(row.get('time')) is int:self.last_exchange_ms=max(self.last_exchange_ms or 0,row['time'])
            self.condition.notify_all()

    def take(self,limit=1280):
        with self.condition:
            rows=[]
            for queue in (self.critical,self.normal):
                while queue and len(rows)<limit:rows.append(queue.popleft()[1])
            self.condition.notify_all()
        return sorted(rows,key=lambda row:row.get('time',0))

    def metrics(self):
        with self.condition:
            heads=[q[0][0] for q in (self.normal,self.critical) if q]
            return dict(queue_depth=len(self.normal)+len(self.critical),oldest_queued_ms=int((time.time()-min(heads))*1000) if heads else 0,
                received=self.received,received_per_second=self.received/max(1,time.monotonic()-self.started),
                dropped_discovery=self.dropped,dropped_critical=0,dedup=self.dedup,exchange_ms=self.last_exchange_ms)
