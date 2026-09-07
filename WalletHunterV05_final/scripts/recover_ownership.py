"""Import user-confirmed ownership while copying is PAUSED; never sends orders.

Run without --apply for a read-only validation. The exact current positions and
public fill history are checked before changing state. Secrets are not printed.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.settings import load
from core.storage import Storage
from core.execution_journal import ExecutionJournal
from core.ai_policy import ReviewPolicy
from integrations.hyperliquid import HyperliquidAccount


def info(payload):
    request=urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps(payload).encode(),
                                   headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request, timeout=20) as response: return json.load(response)


def validate_positions(actual):
    expected={"BTC":(-.00058,79477.,40), "ETH":(-.0188,2444.6,20)}
    for coin,(size,entry,lev) in expected.items():
        row=actual.get(coin,{})
        observed=(float(row.get("szi",0)),float(row.get("entryPx",0)),float((row.get("leverage") or {}).get("value",0)))
        if (not all(math.isfinite(v) for v in observed) or
            abs(observed[0]-size)>1e-12 or abs(observed[1]-entry)>1e-6 or observed[2]!=lev or
            (row.get("leverage") or {}).get("type")!="cross"):
            raise RuntimeError("Position changed since audited snapshot; recovery aborted")
    return expected


def run(apply=False):
    store=Storage(ROOT, load().master_key)
    uid=739370139
    _, p=store.profile(uid)
    account=p.get("account")
    if not account: raise RuntimeError("Recovery account missing")
    if p.get("copy_enabled"): raise RuntimeError("Copying must be explicitly paused before recovery")
    matches=[w for w in p.get("leaders",[]) if w.endswith("4473e0")]
    if len(matches)!=1: raise RuntimeError("Confirmed source is not uniquely configured")
    rows=info({"type":"clearinghouseState", "user":account["address"], "dex":""})["assetPositions"]
    actual={x["position"]["coin"]:x["position"] for x in rows}
    expected=validate_positions(actual)
    since=int(datetime(2026,9,5,0,0,3,tzinfo=timezone.utc).timestamp()*1000)
    fills=info({"type":"userFillsByTime","user":account["address"],"startTime":since,"aggregateByTime":False})
    if not isinstance(fills,list) or len(fills)>=2000 or any(f.get("coin") in expected for f in fills):
        raise RuntimeError("New executions or incomplete history; recovery needs review")
    if not apply:
        print(json.dumps({"validated":True,"positions":list(expected),"source_suffix":"4473e0","applied":False}));return
    log=ExecutionJournal(ROOT)
    equity=HyperliquidAccount(account["address"], None, load().hl_mode).balance()
    if not 0 < equity < 1e12: raise RuntimeError("Invalid equity for recovery slot allocation")
    for coin,(size,entry,lev) in expected.items():
        value=abs(float(actual[coin].get("positionValue") or size*entry))
        position={"coin":coin,"dex":"","side":"SHORT","size":abs(size),"entry_price":entry,"leverage":lev,"margin_mode":"cross"}
        key=coin+"|"
        oid=log.prepare(account["address"],key,{"action":"RECOVERY_IMPORT","evidence":"backup20260905 + no subsequent fills + user confirmed source"})
        log.finish(oid,{"ok":True,"action":"RECOVERY_IMPORT"},{"managed":True,"size":abs(size),"side":"SHORT","position":position,
                   "source_targets":[{"wallet":matches[0],"signed_notional":-value,"margin":float(actual[coin].get("marginUsed") or value/lev),"slot_budget":equity/3}],
                   "attribution":"user_confirmed_recovery","recovered_ms":int(time.time()*1000)})
    runtime=p["runtime"]
    runtime["managed"]=sorted(set(runtime.get("managed",[]))|{"BTC|","ETH|"})
    runtime["ownership_recovery"]={"source":matches[0],"positions":["BTC|","ETH|"],"basis":"user_confirmed_2026-09-06"}
    store.update_runtime(uid,runtime)
    _,p=store.profile(uid)
    p["ai_review_policy"]=ReviewPolicy().as_dict()
    store.update_profile(uid,p)
    print(json.dumps({"applied":True,"positions":list(expected),"copy_enabled":False,"orders_sent":0}))


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--apply",action="store_true")
    run(parser.parse_args().apply)
