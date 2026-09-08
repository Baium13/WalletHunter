"""Public OBSERVE worker with optional explicitly configured isolated PAPER.

Run with an explicit network and a dedicated research SQLite path. This is a
separate low-priority process, not another trading loop. No service is enabled
by importing this module or by installing the code.
"""
import argparse
import time
from core.hyperliquid import HyperliquidReader
from .service import PublicTrades, WalletDiscoveryEngine


def tick(engine, reader, stream, now, clock=None):
    try:
        trades=stream.poll()
    except Exception:
        # Continue pending research/watchlist recovery during socket failure,
        # but never advertise a healthy discovery connection.
        engine.cycle(reader,[],now,clock=clock)
        with engine.store.transaction() as db:
            db.execute("UPDATE intelligence_health SET error='PUBLIC_STREAM_UNAVAILABLE',errors=errors+1 WHERE network=?",(engine.network,))
        return False
    return engine.cycle(reader,trades,now,clock=clock)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Wallet Hunter research and optional isolated PAPER; never submits live orders')
    parser.add_argument('--network',required=True,choices=('TESTNET','MAINNET'))
    parser.add_argument('--database',required=True,help='Dedicated research DB; do not use a financial-state database')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--paper-config',help='Explicit isolated PAPER policy JSON; omitted means research only')
    parser.add_argument('--paper-state-directory',help='Dedicated PAPER state directory')
    args=parser.parse_args(argv)
    if bool(args.paper_config)!=bool(args.paper_state_directory): parser.error('Both PAPER options are required together')
    engine=WalletDiscoveryEngine(args.database,args.network)
    reader=HyperliquidReader(args.network)
    stream=PublicTrades(args.network)
    backend=None
    if args.paper_config:
        from core.autonomous import load_paper_backend
        backend=load_paper_backend(args.paper_config,args.paper_state_directory,args.network,lambda:int(time.time()*1000))
    try:
        while True:
            started=time.monotonic()
            try:
                if backend is not None:
                    from core.foundation.store import scope_key
                    import json
                    with backend.store.transaction() as db:
                        episodes=db.execute("SELECT body FROM position_episodes WHERE scope=?",(scope_key(backend.auth_policy.scope),)).fetchall()
                    engine.position_owned_leaders={e['leader'] for row in episodes if (e:=json.loads(row['body']))['state']!='CLOSED'}
                ok=tick(engine,reader,stream,int(time.time()*1000),clock=lambda:int(time.time()*1000))
                if backend is not None: backend.drain(engine)
                mode=backend.auth_policy.mode if backend is not None else 'OBSERVE'
                print(mode+(' HEALTHY' if ok else ' DEGRADED'),flush=True)
            except Exception:
                print('WORKER UNHEALTHY',flush=True)
            if args.once: break
            time.sleep(max(1,30-(time.monotonic()-started)))
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        reader.s.close()


if __name__=='__main__': main()
