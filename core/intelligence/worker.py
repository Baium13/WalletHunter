"""Public OBSERVE worker with optional explicitly configured isolated PAPER.

Run with an explicit network and a dedicated research SQLite path. This is a
separate low-priority process, not another trading loop. No service is enabled
by importing this module or by installing the code.
"""
import argparse
import time
import math
from core.hyperliquid import HyperliquidReader, MarketResolvers
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
    # Queue occupancy AFTER the take, so admission backs off on a queue that is
    # genuinely not draining rather than on one busy batch.
    pressure=stream.buffer.pressure() if hasattr(stream,'buffer') else None
    return engine.cycle(reader,trades,now,clock=clock,on_decision=on_decision,
        leader_stream=leader_stream,pressure=pressure)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Wallet Hunter research and optional isolated PAPER; never submits live orders')
    parser.add_argument('--network',required=True,choices=('TESTNET','MAINNET'))
    parser.add_argument('--database',required=True,help='Dedicated research DB; do not use a financial-state database')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--paper-config',help='Explicit isolated PAPER policy JSON; omitted means research only')
    parser.add_argument('--paper-state-directory',help='Dedicated PAPER state directory')
    parser.add_argument('--backend',nargs=2,action='append',metavar=('CONFIG','STATE_DIRECTORY'),default=[],
        help='Additional simulated consumer (repeatable): PAPER and SHADOW can run side by side, '
             'each on its own state directory. This worker holds no credentials, so a LIVE_AUTO '
             'configuration is refused here and belongs in the service that owns the signing client.')
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
    markets=MarketResolvers(reader)
    pairs=list(args.backend)
    if args.paper_config: pairs.insert(0,(args.paper_config,args.paper_state_directory))
    backends=[]
    if pairs:
        from core.autonomous import load_paper_backend
        directories=set()
        for config_path,state_directory in pairs:
            resolved=str(Path(state_directory).resolve())
            # Two consumers sharing a state directory would share a ledger and
            # an episode table; the mode guard inside would then refuse anyway.
            if resolved in directories: parser.error('Each consumer needs its own state directory')
            directories.add(resolved)
            backend=load_paper_backend(config_path,state_directory,args.network,
                lambda:int(time.time()*1000),precision=markets.lot_step,
                ceilings=markets.ceiling,leader_leverage=markets.leader_leverage)
            if backend.auth_policy.mode=='LIVE_AUTO':
                parser.error('This worker cannot sign; run a LIVE_AUTO consumer where the signing client lives')
            backends.append(backend)
    try:
        while True:
            started=time.monotonic()
            try:
                # A leader owned by ANY consumer keeps its lifecycle reads, so
                # demoting it in one mode never blinds another to its exits.
                owned=set()
                for backend in backends:
                    from core.foundation.store import scope_key
                    with backend.store.transaction() as db:
                        episodes=db.execute("SELECT body FROM position_episodes WHERE scope=?",(scope_key(backend.auth_policy.scope),)).fetchall()
                        from core.position_episodes import PositionEpisode
                        for row in episodes:
                            e=PositionEpisode.model_validate_json(row['body'])
                            proven=backend.episodes.active_in(db,e.scope,e.mode,e.leader,e.instrument)
                            if proven is not None: owned.add(e.leader)
                engine.position_owned_leaders=owned
                # Recover financial work first. Newly captured actionable data
                # reaches the consumers before optional research can age it.
                def drain_all():
                    for backend in backends: backend.drain(engine)
                drain_all()
                ok=tick(engine,reader,stream,int(time.time()*1000),clock=lambda:int(time.time()*1000),
                    on_decision=drain_all if backends else None,
                    leader_stream=leader_stream)
                drain_all()
                mode='+'.join(b.auth_policy.mode for b in backends) or 'OBSERVE'
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
