"""Operator-owned store registry. Never derives autonomous mode from legacy flags."""
import json
from pathlib import Path
from core.product_read import RuntimeBinding,ProductReadModel
from core.foundation.contracts import Scope


def bindings(root):
    path=Path(root)/'data/product-runtime.json'
    if not path.exists():return ()
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('REGISTRY_PATH_UNSAFE')
    raw=json.loads(path.read_text(encoding='utf-8'))
    if raw.get('version')!=1 or not isinstance(raw.get('runtimes'),list) or len(raw['runtimes'])>256:raise ValueError('REGISTRY_INVALID')
    result=tuple(RuntimeBinding.model_validate(r) for r in raw['runtimes'])
    ids=[(r.scope.tenant,r.scope.account,r.scope.network,r.mode) for r in result]
    if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_RUNTIME_BINDING')
    return result


def view_for(root,tenant,profile,network):
    address=(profile.get('account') or {}).get('address')
    if not address:raise ValueError('ACCOUNT_NOT_BOUND')
    scope=Scope(tenant=str(tenant),account=address.lower(),network=network)
    return ProductReadModel(root,scope,bindings(root),sources=tuple(profile.get('leaders',[])[:3]))
