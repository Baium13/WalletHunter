"""Enable ONLY paper trader and read-only rescue review for one existing user.

Never changes copy_enabled, live trading flags, keys or exchange positions.
Default is a dry run; --apply persists only the two independent mode switches.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.settings import load
from core.storage import Storage
from core.ai_modes import AiModes
from core.ai_review import account_guard


def enable(root,store,uid,apply=False):
    if str(uid) not in store.load()['profiles']:
        raise ValueError('Existing profile required')
    with account_guard(str(root),f'telegram-profile:{uid}'):
        _,p=store.profile(uid)
        if p.get('copy_enabled'):
            raise ValueError('Do not change modes while copying is running in this deployment')
        address=(p.get('account') or {}).get('address')
        if not address:raise ValueError('Existing account required')
        with account_guard(str(root),address):
            before={k:p.get(k) for k in ('account','leaders','copy_enabled','ai_slot_selected','ai_live_enabled','ai_auto_trading')}
            AiModes.set_enabled(p,'trader',True)
            AiModes.set_enabled(p,'rescue',True)
            if before!={k:p.get(k) for k in before}:
                raise RuntimeError('Unexpected financial settings mutation')
            if apply:store.update_profile(uid,p)
    return {'applied':apply,'user_suffix':str(uid)[-3:],'trader':'PAPER','rescue':'READ_ONLY_REVIEW',
            'copy_enabled':False,'autonomous_real_orders':False,'orders_sent':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(enable(ROOT,Storage(str(ROOT),load().master_key),args.uid,args.apply)))
