"""Explicit public OBSERVE worker. Does not load profiles, secrets or settings.

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
    parser=argparse.ArgumentParser(description='Wallet Hunter public OBSERVE research; never submits orders')
    parser.add_argument('--network',required=True,choices=('TESTNET','MAINNET'))
    parser.add_argument('--database',required=True,help='Dedicated research DB; do not use a financial-state database')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    engine=WalletDiscoveryEngine(args.database,args.network)
    reader=HyperliquidReader(args.network)
    stream=PublicTrades(args.network)
    try:
        while True:
            started=time.monotonic()
            try:
                ok=tick(engine,reader,stream,int(time.time()*1000),clock=lambda:int(time.time()*1000))
                print('OBSERVE HEALTHY' if ok else 'OBSERVE DEGRADED',flush=True)
            except Exception:
                print('OBSERVE UNHEALTHY',flush=True)
            if args.once: break
            time.sleep(max(1,30-(time.monotonic()-started)))
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        reader.s.close()


if __name__=='__main__': main()
