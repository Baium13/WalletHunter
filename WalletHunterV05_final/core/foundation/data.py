"""Shared bounded REST foundation; optional stream ingress never owns a socket.

The existing reader has bounded REST transport and deliberately disables SDK WS.
Do not create per-dashboard pollers. A gap requires a new REST snapshot.
"""
import threading
from .contracts import DomainEvent, InstrumentId, MarketSnapshot, PortfolioSnapshot, Position
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
        if instrument not in self._cache and len(self._cache) >= self.capacity:
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


def account_snapshot(client, scope, revision, clock_ms):
    """Read-only bridge. REST account API provides no atomic exchange watermark.

    Preserve that limitation as UNKNOWN: this adapter cannot authorize execution
    until a coherent exchange/account watermark contract is supplied. Existing
    routes retain their validated capital adapter; no financial history is adopted.
    """
    if client.network != scope.network: raise DataUnavailable("NETWORK_MISMATCH")
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
