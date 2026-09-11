"""Private account construction boundary for exact user-confirmed proposals only."""
import json
import time
from pathlib import Path
from integrations.hyperliquid import HyperliquidAccount,verify_account_control
from core.foundation.contracts import Scope
from core.foundation.store import Store
from core.foundation.authorization import AuthorizationPolicy
from core.foundation.autonomous_allocation import AutonomousAllocationPolicy
from core.foundation.risk import RiskPolicy
from core.foundation.copy_execution import HyperliquidExecutionAdapter
from core.autonomous import AutonomousBackend


def confirmed_backend(root,binding,storage,settings):
    scope=binding.scope
    _,profile=storage.profile(int(scope.tenant));account=profile.get('account') or {}
    if account.get('address','').lower()!=scope.account or settings.hl_mode!=scope.network or binding.mode!='LIVE_CONFIRM':raise ValueError('ACCOUNT_BINDING_MISMATCH')
    path=lambda p:Path(p) if Path(p).is_absolute() else Path(root)/p
    config=json.loads(path(binding.config_path).read_text(encoding='utf-8'))
    auth=AuthorizationPolicy.model_validate(config['authorization'])
    if auth.scope!=scope or auth.mode!='LIVE_CONFIRM':raise ValueError('CONFIG_SCOPE_MISMATCH')
    # Secret is confined here and the existing HL signing integration. No read model sees it.
    try:
        secret=storage.decrypt(account['private_key'])
        client=HyperliquidAccount(scope.account,secret,scope.network,slippage_pct=settings.max_slippage_pct)
        verify_account_control(scope.account,secret,lambda query:client.info.post('/info',query))
    except Exception:raise ValueError('ACCOUNT_CONTROL_UNAVAILABLE') from None
    store=Store(path(binding.state_path));before=store.portfolio(scope);clock=lambda:int(time.time()*1000)
    adapter=HyperliquidExecutionAdapter(client,scope,clock,before)
    return AutonomousBackend(store,adapter,AutonomousAllocationPolicy.model_validate(config['allocation']),auth,
        RiskPolicy.model_validate(config['risk']),clock)
