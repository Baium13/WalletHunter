"""Read-only preflight for paused UI/Telegram deployments, not live activation."""
import json
import pathlib
import sqlite3
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from core.execution_quarantine import read_only_deployment_gate

def check(root):
    root=pathlib.Path(root).resolve()
    profiles=json.loads((root/'data/state.json').read_text())['profiles']
    with sqlite3.connect((root/'data/executions.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        return read_only_deployment_gate(db,profiles)

if __name__=='__main__':print(json.dumps(check(sys.argv[1])))
