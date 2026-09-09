"""Shared bounded REST foundation; optional stream ingress never owns a socket.

The existing reader has bounded REST transport and deliberately disables SDK WS.
Do not create per-dashboard pollers. A gap requires a new REST snapshot.
"""
import threading
import math
from .contracts import DomainEvent, InstrumentId, MarketSnapshot, PortfolioSnapshot, Position, OpenOrder
from .store import digest


class DataUnavailable(ValueError):
    pass


class MarketData:
    def __init__(self, reader, store, clock_ms, *, max_age_ms, min_request_ms, capacity=128):
        if min(max_age_ms, min_request_ms, capacity) <= 0:
            raise ValueError("Explicit positive resource limits required")
        self.reader, self.store, self.clock = reader, store, clock_ms
        self.max_age, self.min_request, self.capacity = max_age_ms, min_request_ms, capacity
        self._cache, self._sequences, self._gaps = {}, {}, set()
        self._last_request = None
        self._lock = threading.RLock()

    def _scope(self, scope, instrument):
        if scope.network != instrument.network or instrument.network != self.reader.network:
            raise DataUnavailable("NETWORK_MISMATCH")
        if instrument not in self._cache and instrument not in self._gaps and len(set(self._cache) | self._gaps) >= self.capacity:
            raise DataUnavailable("SUBSCRIPTION_CAPACITY")

    def _publish(self, scope, snapshot):
        event = DomainEvent(event_id=digest(snapshot), event_type="MARKET_SNAPSHOT",
            correlation_id="market", scope=scope, event_ms=snapshot.exchange_ms if snapshot.exchange_ms is not None else snapshot.received_ms,
            received_ms=snapshot.received_ms, payload=snapshot)
        # Public market cache may be shared; durable event IDs must include tenant.
        event = event.model_copy(update={"event_id": digest(event)})
        self.store.append(event)

    def get(self, scope, instrument):
        with self._lock:
            self._scope(scope, instrument)
            now = self.clock()
            current = self._cache.get(instrument)
            if current and instrument not in self._gaps and self._fresh(current, now):
                self._publish(scope, current)
                return current
            if self._last_request is not None and now-self._last_request < self.min_request:
                raise DataUnavailable("REST_RATE_LIMIT")
            self._last_request = now
            try:
                coin = (instrument.dex+":" if instrument.dex else "")+instrument.symbol
                raw = self.reader._info({"type": "l2Book", "coin": coin})
                levels = raw["levels"]
                # Conversion uses the same finite parser as the production adapters.
                from core.capital_snapshot import finite_amount
                bid = finite_amount(levels[0][0]["px"], "bid")
                ask = finite_amount(levels[1][0]["px"], "ask")
                received = self.clock()
                result = MarketSnapshot(instrument=instrument, exchange_ms=raw.get("time"), received_ms=received,
                    price=(bid+ask)/2, bid=bid, ask=ask, completeness="COMPLETE", freshness="FRESH", source="REST", source_version="l2Book-v1")
                if not self._fresh(result, received): raise DataUnavailable("STALE_REST")
                if current and current.exchange_ms is not None and result.exchange_ms < current.exchange_ms:
                    raise DataUnavailable("WATERMARK_REGRESSION")
                self._publish(scope, result)
                self._cache[instrument] = result
                self._gaps.discard(instrument)
                self._sequences.pop(instrument, None)
                return result
            except Exception:
                self._gaps.add(instrument)
                raise DataUnavailable("REST_RECONCILIATION_REQUIRED") from None

    def _fresh(self, row, now):
        return (row.completeness == "COMPLETE" and row.freshness == "FRESH" and row.exchange_ms is not None
            and 0 <= now-row.received_ms <= self.max_age and 0 <= now-row.exchange_ms <= self.max_age)

    def disconnect(self):
        with self._lock: self._gaps.update(self._cache)

    def ingest(self, scope, snapshot, sequence):
        """Bounded callback; caller must attest a monotonic sequence, not invent it."""
        snapshot = MarketSnapshot.model_validate_json(snapshot.model_dump_json())
        if type(sequence) is not int or sequence < 0: raise DataUnavailable("INVALID_SEQUENCE")
        with self._lock:
            key = snapshot.instrument
            self._scope(scope, key)
            previous = self._cache.get(key)
            last = self._sequences.get(key)
            if previous is None or key in self._gaps: raise DataUnavailable("REST_REQUIRED")
            if last is not None and sequence <= last: return False
            if last is not None and sequence != last+1:
                self._gaps.add(key)
                raise DataUnavailable("STREAM_GAP")
            if not self._fresh(snapshot, self.clock()) or snapshot.exchange_ms < previous.exchange_ms:
                return False
            self._publish(scope, snapshot)
            self._cache[key], self._sequences[key] = snapshot, sequence
            return True


def account_snapshot(client, scope, revision, clock_ms, *, dex=None, require_collateral=True):
    """Read-only bridge. REST account API provides no atomic exchange watermark.

    Preserve that limitation as UNKNOWN: this adapter cannot authorize execution
    until a coherent exchange/account watermark contract is supplied. Existing
    routes retain their validated capital adapter; no financial history is adopted.
    """
    if client.network != scope.network: raise DataUnavailable("NETWORK_MISMATCH")
    if dex is not None:
        return copy_account_snapshot(client, scope, revision, clock_ms, dex, require_collateral)
    capital = client.capital_snapshot()
    rows = client.positions(True, True)
    if any(not isinstance(p["leverage"], (int, float)) or isinstance(p["leverage"], bool)
           or p["leverage"] != int(p["leverage"]) for p in rows):
        raise DataUnavailable("INVALID_LEVERAGE")
    positions = tuple(Position(instrument=InstrumentId(
        network=scope.network, dex=p.get("dex") or "", symbol=p["coin"].split(":")[-1]),
        side=p["side"], size=p["size"], entry_price=p["entry_price"], notional=p["position_value"],
        margin=p.get("margin_used"), leverage=int(p["leverage"]), evidence="EXTERNAL") for p in rows)
    return PortfolioSnapshot(scope=scope, revision=revision, exchange_ms=None, received_ms=clock_ms(),
        equity=None, sizing_capital=capital.sizing_base_usdc, available_collateral=None,
        positions=positions, completeness="UNKNOWN", evidence="EXCHANGE")


def reader_account_snapshot(reader, scope, revision, clock_ms):
    """Compose account evidence through the existing public Hyperliquid reader.

    The product read path must be able to refresh a follower account while the
    Manual Copy writer is paused.  This bridge is deliberately read-only: it
    uses the reader's existing bounded ``_info`` calls, never credentials or a
    signing adapter, and leaves ownership/provenance UNKNOWN until the journal
    proves it.
    """
    from core.capital_snapshot import finite_amount, strict_spot_usdc
    if reader.network != scope.network:
        raise DataUnavailable("NETWORK_MISMATCH")
    if not isinstance(revision, int) or revision < 1:
        raise DataUnavailable("INVALID_REVISION")
    now = clock_ms()
    if not isinstance(now, int) or now < 0:
        raise DataUnavailable("INVALID_RECEIPT_TIME")
    states = {}
    stamps = []
    for dex in ("", "xyz"):
        state = reader.state(scope.account, dex)
        if not isinstance(state, dict):
            raise DataUnavailable("ACCOUNT_STATE_UNAVAILABLE")
        stamp = state.get("time")
        if type(stamp) is not int or stamp < 0:
            raise DataUnavailable("ACCOUNT_WATERMARK_UNAVAILABLE")
        states[dex] = state
        stamps.append(stamp)

    positions = []
    for dex, market_type in (("", "CRYPTO"), ("xyz", "STOCKS")):
        limits = reader.leverage_limits(dex)
        for row in reader._positions(states[dex], market_type, dex, limits):
            lev = finite_amount(row.get("leverage"), "position leverage")
            if lev != int(lev):
                raise DataUnavailable("INVALID_LEVERAGE")
            positions.append(Position(
                instrument=InstrumentId(network=scope.network, dex=dex,
                    symbol=str(row["coin"]).split(":")[-1]),
                side=row["side"], size=row["size"], entry_price=row["entry_price"],
                notional=row["position_value"], margin=row.get("margin_used"),
                leverage=int(lev), evidence="UNKNOWN"))

    orders = []
    for dex in ("", "xyz"):
        for row in reader.frontend_open_orders(scope.account, dex):
            if type(row.get("reduceOnly")) is not bool:
                raise DataUnavailable("ORDER_SCOPE_UNKNOWN")
            oid = row.get("oid")
            if oid is None:
                raise DataUnavailable("ORDER_ID_UNAVAILABLE")
            orders.append(OpenOrder(
                instrument=InstrumentId(network=scope.network, dex=dex,
                    symbol=str(row["coin"]).split(":")[-1]),
                order_id=str(oid), size=finite_amount(row.get("sz"), "order size"),
                reduce_only=row["reduceOnly"]))

    # Reuse the canonical capital semantics.  Unified accounts use the single
    # USDC pool; standard accounts sum the supported DEX account values while
    # available collateral remains the requested core pool's withdrawable.
    mode = reader._info({"type": "userAbstraction", "user": scope.account})
    if mode == "unifiedAccount":
        spot = strict_spot_usdc(reader._info({"type": "spotClearinghouseState", "user": scope.account}))
        sizing = finite_amount(spot.total, "sizing capital")
        capacity = finite_amount(spot.available, "available collateral")
    elif mode == "disabled":
        values = [finite_amount((states[dex].get("marginSummary") or {}).get("accountValue"), "account value")
                  for dex in ("", "xyz")]
        sizing = math.fsum(values)
        capacity = finite_amount((states[""].get("withdrawable")), "available collateral")
    else:
        raise DataUnavailable("CAPITAL_MODE_UNSUPPORTED")
    if sizing < 0 or capacity < 0 or capacity > sizing:
        raise DataUnavailable("INCONSISTENT_COLLATERAL")
    return PortfolioSnapshot(scope=scope, revision=revision,
        exchange_ms=min(stamps), received_ms=now, equity=sizing,
        sizing_capital=sizing, available_collateral=capacity,
        collateral_dex="", positions=tuple(positions), orders=tuple(orders),
        completeness="COMPLETE", evidence="EXCHANGE")


def copy_account_snapshot(client, scope, revision, clock_ms, dex, require_collateral=True):
    """Bounded live read: a pool capacity is never presented as all-pool cash.

    Position existence comes from the exchange; attribution is supplied separately
    by the journal book, never inferred from these rows. Missing timestamps fail
    closed rather than being replaced with the local receipt time.
    """
    from core.capital_snapshot import finite_amount
    from .contracts import OpenOrder
    if client.network != scope.network or client.address.lower() != scope.account:
        raise DataUnavailable("ACCOUNT_NETWORK_MISMATCH")
    stamps = []
    for pool in ("", "xyz"):
        state = client.info.user_state(scope.account, pool)
        stamp = state.get("time")
        if type(stamp) is not int or stamp < 0:
            raise DataUnavailable("ACCOUNT_WATERMARK_UNAVAILABLE")
        stamps.append(stamp)
    rows = client.positions(True, True)
    positions = []
    for p in rows:
        lev = finite_amount(p.get("leverage"), "leverage")
        if lev != int(lev): raise DataUnavailable("INVALID_LEVERAGE")
        positions.append(Position(instrument=InstrumentId(network=scope.network,
            dex=p.get("dex") or "", symbol=p["coin"].split(":")[-1]),
            side=p["side"], size=p["size"], entry_price=p["entry_price"],
            notional=p["position_value"], margin=p.get("margin_used"), leverage=int(lev), evidence="UNKNOWN"))
    orders = []
    for pool in ("", "xyz"):
        raw = client.frontend_open_orders(pool)
        if not isinstance(raw, list): raise DataUnavailable("ORDERS_UNAVAILABLE")
        for row in raw:
            if type(row.get("reduceOnly")) is not bool: raise DataUnavailable("ORDER_SCOPE_UNKNOWN")
            orders.append(OpenOrder(instrument=InstrumentId(network=scope.network, dex=pool,
                symbol=row["coin"].split(":")[-1]), order_id=str(row["oid"]),
                size=finite_amount(row.get("sz"), "order size"), reduce_only=row["reduceOnly"]))
    equity = capacity = sizing = None
    try:
        if not require_collateral: raise ValueError('Collateral not required for proven reduction')
        sizing = finite_amount(client.capital_snapshot().sizing_base_usdc, "sizing capital")
        capacity = finite_amount(client.available_margin(dex), "pool capacity")
        # Existing capital semantics are the sizing/collateral basis, not a
        # fabricated sum of unified spot plus duplicated perp account balances.
        equity = sizing
        if min(sizing, capacity) < 0 or capacity > equity: raise ValueError("Inconsistent collateral")
    except Exception:
        equity = capacity = sizing = None
    return PortfolioSnapshot(scope=scope, revision=revision, exchange_ms=min(stamps), received_ms=clock_ms(),
        equity=equity, sizing_capital=sizing, available_collateral=capacity, collateral_dex=dex,
        positions=tuple(positions), orders=tuple(orders),
        completeness="COMPLETE" if sizing is not None else "UNKNOWN", evidence="EXCHANGE")
