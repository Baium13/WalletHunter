"""Public OBSERVE worker with optional explicitly configured isolated PAPER.

Run with an explicit network and a dedicated research SQLite path. This is a
separate low-priority process, not another trading loop. No service is enabled
by importing this module or by installing the code.
"""
import argparse
import time
import math
from core.hyperliquid import HyperliquidReader
from .service import PublicTrades, UserFillsStream, WalletDiscoveryEngine


def tick(engine, reader, stream, now, clock=None,on_decision=None,leader_stream=None):
    if hasattr(stream,'buffer'):
        with engine.store.transaction() as db:
            active={r[0] for r in db.execute("SELECT wallet FROM candidates WHERE network=? AND status='ACTIVE'",(engine.network,))}
        stream.buffer.protected=frozenset(active|set(getattr(engine,'position_owned_leaders',())))
    try:
        trades=stream.poll()
    except Exception:
        # Continue pending research/watchlist recovery during socket failure,
        # but never advertise a healthy discovery connection.
        engine.cycle(reader,[],now,clock=clock,on_decision=on_decision)
        with engine.store.transaction() as db:
            db.execute("UPDATE intelligence_health SET error='PUBLIC_STREAM_UNAVAILABLE',errors=errors+1 WHERE network=?",(engine.network,))
        engine.health_observation('public_data',clock() if clock else now,error='PUBLIC_STREAM_UNAVAILABLE')
        return False
    received=clock() if clock else now
    stamps=[]
    for row in trades:
        try:
            stamp=row['time'];price=float(row['px']);size=float(row['sz'])
            if type(stamp) is int and 0<=received-stamp<=300000 and math.isfinite(price) and math.isfinite(size) and price>0 and size>0:
                stamps.append(stamp)
        except (KeyError,ValueError,TypeError):pass
    telemetry=stream.metrics() if hasattr(stream,'metrics') else {}
    engine.health_observation('public_data',received,details={**telemetry,'source':'PUBLIC_TRADES_WEBSOCKET',
        'exchange_ms':max(stamps) if stamps else None,'subscriptions':list(getattr(stream,'coins',())),
        'activity':'RECEIVED' if stamps else 'WAITING_FOR_TRADE'})
    return engine.cycle(reader,trades,now,clock=clock,on_decision=on_decision,leader_stream=leader_stream)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Wallet Hunter research and optional isolated PAPER; never submits live orders')
    parser.add_argument('--network',required=True,choices=('TESTNET','MAINNET'))
    parser.add_argument('--database',required=True,help='Dedicated research DB; do not use a financial-state database')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--paper-config',help='Explicit isolated PAPER policy JSON; omitted means research only')
    parser.add_argument('--paper-state-directory',help='Dedicated PAPER state directory')
    parser.add_argument('--discovery-config',help='Operational resource/segmentation JSON; not PAPER/scoring policy')
    args=parser.parse_args(argv)
    if bool(args.paper_config)!=bool(args.paper_state_directory): parser.error('Both PAPER options are required together')
    from .discovery_operations import DiscoveryOperations
    from pathlib import Path
    operations=DiscoveryOperations.model_validate_json(Path(args.discovery_config).read_text()) if args.discovery_config else DiscoveryOperations()
    engine=WalletDiscoveryEngine(args.database,args.network,operations=operations)
    reader=HyperliquidReader(args.network)
    stream=PublicTrades(args.network)
    leader_stream=UserFillsStream(args.network)
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
                        from core.position_episodes import PositionEpisode
                        engine.position_owned_leaders=set()
                        for row in episodes:
                            e=PositionEpisode.model_validate_json(row['body'])
                            proven=backend.episodes.active_in(db,e.scope,e.mode,e.leader,e.instrument)
                            if proven is not None:engine.position_owned_leaders.add(e.leader)
                # Recover financial work first. Newly captured actionable data
                # reaches PAPER before optional historical research can age it.
                if backend is not None:backend.drain(engine)
                ok=tick(engine,reader,stream,int(time.time()*1000),clock=lambda:int(time.time()*1000),
                    on_decision=(lambda:backend.drain(engine)) if backend is not None else None,
                    leader_stream=leader_stream)
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
        leader_stream.close()
        reader.s.close()


if __name__=='__main__': main()
