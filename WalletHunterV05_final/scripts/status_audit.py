"""Sanitised read-only operational check; no keys, signing or order submission."""
import json
import os
import sqlite3
import sys
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.settings import load
from core.storage import Storage
from core.execution_journal import ExecutionJournal
from integrations.hyperliquid import HyperliquidAccount

settings=load()
storage=Storage(ROOT, settings.master_key)
profiles=[]
for uid,p in storage.load()["profiles"].items():
    account=p.get("account")
    item={"user_suffix":uid[-3:],"has_account":bool(account),"copy_enabled":p.get("copy_enabled"),
          "sources":[w[-6:] for w in p.get("leaders",[])],"managed":p.get("runtime",{}).get("managed",[])}
    if account:
        public=HyperliquidAccount(account["address"],None,settings.hl_mode)
        item["positions"]=[{k:v.get(k) for k in ("coin","side","size","entry_price","leverage","margin_mode")}
                           for v in public.positions(True,True)]
        journal=ExecutionJournal(ROOT)
        item["ownership"]={k:{"managed":v.get("managed"),"source_suffixes":[s["wallet"][-6:] for s in v.get("source_targets",[])],
                                "basis":v.get("attribution")} for k,v in journal.owned(account["address"]).items()}
        item["unresolved_executions"]=sorted(journal.pending(account["address"]))
    profiles.append(item)
checks={}
for name in ("executions.sqlite3","ai_reviews.sqlite3","ai_research.sqlite3","ai_outcomes.sqlite3","ai_shadow.sqlite3","ai_trader.sqlite3","ai_modes.sqlite3","ai_learning.sqlite3","ai_user_orders.sqlite3","ai_position_actions.sqlite3","ai_entry_observer.sqlite3"):
    path=os.path.join(ROOT,"data",name)
    if os.path.exists(path):
        with closing(sqlite3.connect(f"file:{path}?mode=ro",uri=True)) as db:
            checks[name]=db.execute("PRAGMA integrity_check").fetchone()[0]
print(json.dumps({"profiles":profiles,"sqlite_integrity":checks,"orders_sent":0},ensure_ascii=False))
