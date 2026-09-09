"""Read-only Hyperliquid-compatible proof for future gateway-owned IOC orders.

This never signs, retries, assigns legacy ownership, or releases a reservation.
Only orders carrying the deterministic canonical client ID are accepted. Existing
legacy client IDs require an explicit durable identity mapping before migration.
"""
import math
from core.capital_snapshot import finite_amount
from .contracts import OrderIntent, PortfolioSnapshot, ExecutionReceipt, Fill
from .store import digest

# Explicit documented placement rejections, not arbitrary '*Rejected' strings.
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
# Identity, complete fill history, open orders and position delta are still proven.
PLACEMENT_REJECTIONS = frozenset({
    'rejected', 'tickRejected', 'minTradeNtlRejected', 'perpMarginRejected',
    'reduceOnlyRejected', 'badAloPxRejected', 'iocCancelRejected',
    'badTriggerPxRejected', 'marketOrderNoLiquidityRejected',
    'positionIncreaseAtOpenInterestCapRejected', 'positionFlipAtOpenInterestCapRejected',
    'tooAggressiveAtOpenInterestCapRejected', 'openInterestIncreaseRejected',
    'insufficientSpotBalanceRejected', 'oracleRejected', 'perpMaxPositionRejected',
})
TERMINAL_STATUSES = PLACEMENT_REJECTIONS | {'filled', 'canceled', 'iocCancel'}


def client_order_id(intent):
    intent = OrderIntent.model_validate_json(intent.model_dump_json())
    return intent.exchange_client_id or "0x" + digest(intent)[:32]


def reconcile_order(intent, before, after, client, *, now_ms, max_age_ms):
    """One bounded order-status and fill-history query; exceptions stay UNKNOWN.

    The supplied client must be the bound account's read adapter. Status uses the
    existing HyperliquidAccount.query_order_by_cloid wrapper; fill transport is the
    SDK read API with its existing timeout. No aggregate delta alone proves a fill.
    """
    intent = OrderIntent.model_validate_json(intent.model_dump_json())
    def unknown():
        return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="UNKNOWN",
            reconciliation="RECONCILIATION_REQUIRED", received_ms=now_ms, provenance="UNKNOWN")
    try:
        before = PortfolioSnapshot.model_validate_json(before.model_dump_json())
        after = PortfolioSnapshot.model_validate_json(after.model_dump_json())
        if (type(now_ms) is not int or type(max_age_ms) is not int or max_age_ms <= 0
                or intent.execution_mode != "LIVE" or client.network != intent.scope.network
                or client.address.lower() != intent.scope.account or before.scope != intent.scope or after.scope != intent.scope
                or before.evidence != "EXCHANGE" or after.evidence != "EXCHANGE"
                or (intent.action not in {'REDUCE','CLOSE'} and (before.completeness != "COMPLETE" or after.completeness != "COMPLETE"))
                or before.exchange_ms is None or after.exchange_ms is None
                or not 0 <= now_ms-after.received_ms <= max_age_ms
                or not 0 <= now_ms-after.exchange_ms <= max_age_ms
                or before.received_ms > intent.created_ms or before.exchange_ms > intent.created_ms
                or after.exchange_ms < before.exchange_ms or after.revision <= before.revision):
            return unknown()
        coin = intent.instrument.market_key.split("|")[0]
        expected_side = "B" if intent.side == "BUY" else "A"
        cloid = client_order_id(intent)
        response = client.query_order_by_cloid(cloid)
        wrapper, order = response["order"], response["order"]["order"]
        oid = order["oid"]
        if (response["status"] != "order" or wrapper["status"] not in TERMINAL_STATUSES
                or type(oid) is not int or oid < 0 or order.get("cloid") != cloid
                or order.get("coin") != coin or order.get("side") != expected_side
                or type(order.get("reduceOnly")) is not bool
                or order["reduceOnly"] != (intent.action in {"REDUCE", "CLOSE"})
                or finite_amount(order.get("origSz"), "order size") != intent.size
                or finite_amount(order.get("limitPx"), "order price") != intent.limit_price
                or type(order.get("timestamp")) is not int
                or not intent.created_ms <= order["timestamp"] < intent.expires_ms
                or type(wrapper.get("statusTimestamp")) is not int
                or not order["timestamp"] <= wrapper["statusTimestamp"] <= after.exchange_ms):
            return unknown()
        if any(row.order_id == str(oid) for row in after.orders): return unknown()
        rows = client.info.user_fills_by_time(intent.scope.account, intent.created_ms, now_ms)
        if not isinstance(rows, list) or len(rows) >= 2000: return unknown()
        fills = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("coin"), str): return unknown()
            if row["coin"] != coin: continue
            if (row.get("oid") != oid or type(row.get("oid")) is not int
                    or row.get("side") != expected_side or type(row.get("tid")) is not int or row["tid"] < 0
                    or type(row.get("time")) is not int
                    or not order["timestamp"] <= row["time"] <= min(after.exchange_ms, wrapper["statusTimestamp"])):
                return unknown()
            fill = Fill(intent_id=intent.intent_id, instrument=intent.instrument, order_id=str(oid),
                trade_id=str(row["tid"]), side=intent.side, size=finite_amount(row.get("sz"), "fill size"),
                price=finite_amount(row.get("px"), "fill price"), exchange_ms=row["time"])
            if (fill.price > intent.limit_price if intent.side == "BUY" else fill.price < intent.limit_price):
                return unknown()
            fills.append(fill)
        if len({row.trade_id for row in fills}) != len(fills): return unknown()
        filled = math.fsum(row.size for row in fills)
        if filled > intent.size or not math.isfinite(filled): return unknown()
        b = next((p for p in before.positions if p.instrument == intent.instrument), None)
        a = next((p for p in after.positions if p.instrument == intent.instrument), None)
        signed = lambda p: 0. if p is None else p.size*(1 if p.side == "LONG" else -1)
        expected_delta = filled*(1 if intent.side == "BUY" else -1)
        if not math.isclose(signed(a)-signed(b), expected_delta, rel_tol=1e-9, abs_tol=0.): return unknown()
        if intent.action == "OPEN" and b is not None: return unknown()
        if intent.action == "ADD" and (b is None or (b.side == "LONG") != (intent.side == "BUY")): return unknown()
        if intent.action in {"REDUCE", "CLOSE"}:
            if b is None or (b.side == "LONG") == (intent.side == "BUY") or intent.size > b.size: return unknown()
            if a is not None and (a.side != b.side or a.entry_price != b.entry_price): return unknown()
            if intent.action == "CLOSE" and intent.size != b.size: return unknown()
        if a is not None and intent.action in {"OPEN", "ADD"}:
            total = (b.size if b else 0.) + filled
            cost = (b.size*b.entry_price if b else 0.) + math.fsum(f.size*f.price for f in fills)
            if a.leverage != intent.leverage or not math.isclose(a.entry_price, cost/total, rel_tol=1e-9): return unknown()
        if not fills:
            if wrapper["status"] == "filled": return unknown()
            return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="REJECTED",
                order_ids=(str(oid),), reconciliation="REJECTED", received_ms=now_ms, provenance="EXCHANGE")
        if wrapper["status"] in PLACEMENT_REJECTIONS: return unknown()
        partial = filled < intent.size
        return ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope, status="PARTIAL" if partial else "FILLED",
            order_ids=(str(oid),), fills=tuple(sorted(fills, key=lambda f: (f.exchange_ms, f.trade_id))),
            reconciliation="PARTIAL" if partial else "CONFIRMED", received_ms=now_ms, provenance="EXCHANGE")
    except Exception:
        # SDK exceptions can contain request details. Do not emit their text.
        return unknown()
