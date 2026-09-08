"""Content-address the three primary assets; no credentials/runtime state read."""
import argparse
import hashlib
import json
import re
from pathlib import Path

def build(check=False):
    root=Path(__file__).resolve().parents[1]/'webapp/static'
    names=('product-model.js','product.js','product.css')
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    release=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:16]
    path=root/'index.html';old=path.read_text(encoding='utf-8')
    new=re.sub(r'content="terminal-v1"|(?<=name="wh-build" )content="[a-f0-9]{16}"',f'content="{release}"',old)
    for name in names:
        new=re.sub(re.escape('/static/'+name)+r'\?v=[^"\s]+','/static/'+name+'?v='+hashes[name][:16],new)
    manifest=json.dumps({'release':release,'contract':'product-v1','assets':hashes},indent=2)+'\n'
    target=root/'product-release.json'
    if check:
        if new!=old or not target.exists() or target.read_text(encoding='utf-8')!=manifest:raise SystemExit('STALE_FRONTEND_BUILD')
    else:
        path.write_text(new,encoding='utf-8',newline='\n');target.write_text(manifest,encoding='utf-8',newline='\n')
    print(release)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');build(parser.parse_args().check)
