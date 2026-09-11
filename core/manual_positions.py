"""Verified, explicit manual position actions; no background order submission.

The caller MUST hold ``account_guard`` and provide a current runtime Snapshot.
``persist`` commits that same Snapshot with Storage.update_runtime. Every action
is recorded before submission. Unknown outcomes are never automatically retried.
Exchange-owned reduce-only stops remain active until a replacement is verified
or a market close is verified flat, avoiding a cancel-then-close protection gap.
"""
from copy import deepcopy
import math
import time
import uuid

from core.ai_review import market_key


class ManualActionError(RuntimeError):
    """Action rejected/incomplete; persisted state explains what is still live."""


def _number(value, label):
    result = float(value)
    if not math.isfinite(result):
        raise ManualActionError(f"Invalid {label}")
    return result


class ManualPositions:
    def __init__(self, client, runtime, persist, *, attempts=3, delay=0.2,
                 sleep=time.sleep, now=time.time, canonical_context=None,
                 legacy_test_executor=None):
        if not callable(persist):
            raise ValueError("A persistent runtime callback is required")
        self.client, self.runtime, self.persist = client, runtime, persist
        self.attempts, self.delay = max(1, attempts), max(0, delay)
        self.sleep, self.now = sleep, now
        self.canonical_context = canonical_context
        # Legacy execution is dependency-injected only by isolated fixtures.
        # Normal application construction leaves this unset and therefore
        # fails closed when canonical account evidence is unavailable.
        self.legacy_test_executor = legacy_test_executor

    @staticmethod
    def _market(coin, dex):
        key = market_key({"coin": str(coin), "dex": dex or ""})
        return key.split("|", 1)[0], key.split("|", 1)[1], key

    def _positions(self):
        rows = self.client.positions(True, True)
        if not isinstance(rows, list):
            raise ManualActionError("Cannot verify exchange positions")
        return rows

    def _position(self, key):
        rows = [p for p in self._positions() if market_key(p) == key
                and abs(_number(p.get("size", 0), "position size")) > 0]
        if len(rows) > 1:
            raise ManualActionError("Ambiguous exchange position")
        return rows[0] if rows else None

    def _orders(self, dex):
        # frontendOpenOrders includes isTrigger/reduceOnly/orderType/triggerPx;
        # the terse openOrders response cannot safely distinguish SL from TP.
        if callable(getattr(self.client, "frontend_open_orders", None)):
            rows = self.client.frontend_open_orders(dex)
        else:
            rows = self.client.info.post("/info", {
                "type": "frontendOpenOrders", "user": self.client.address,
                "dex": dex,
            })
        if not isinstance(rows, list):
            raise ManualActionError("Cannot verify exchange open orders")
        return rows

    def _market_orders(self, coin, dex):
        _, _, key = self._market(coin, dex)
        return [o for o in self._orders(dex)
                if market_key({"coin": o.get("coin", ""), "dex": dex}) == key]

    def _stop_orders(self, coin, dex, saved=None):
        ids = {str(x) for x in ((saved or {}).get("extra_oids") or [])}
        if (saved or {}).get("oid") is not None:
            ids.add(str(saved["oid"]))
        result = []
        for order in self._market_orders(coin, dex):
            kind = str(order.get("orderType", "")).lower()
            identified_stop = (bool(order.get("reduceOnly"))
                               and kind in {"stop market", "stop limit"})
            if order.get("oid") is not None and (str(order["oid"]) in ids or (self.legacy_test_executor and identified_stop)):
                # A locally tracked ID is evidence of ownership; market equality
                # is always checked above, so corrupt IDs cannot cancel another asset.
                result.append(order)
        return result

    def _record(self, key, **fields):
        record = self.runtime.setdefault("manual_actions", {}).setdefault(key, {})
        record.update(fields, updated_ms=int(self.now() * 1000))
        self.persist()
        return record

    def _begin(self, key, action, **fields):
        previous = self.runtime.setdefault("manual_actions", {}).get(key, {})
        if action != "delete_stop" and previous.get("status") in {"submitting", "unknown"}:
            raise ManualActionError("Previous manual action is unconfirmed; reconcile it before retrying")
        self.runtime["manual_actions"][key] = {
            "id": uuid.uuid4().hex, "action": action, "status": "submitting",
            "created_ms": int(self.now() * 1000), **fields,
        }
        self.persist()

    def _wait(self, read, predicate):
        latest = None
        for attempt in range(self.attempts):
            latest = read()
            if predicate(latest):
                return latest, True
            if attempt + 1 < self.attempts:
                self.sleep(self.delay)
        return latest, False

    @staticmethod
    def _accepted_oid(response):
        if not isinstance(response, dict) or response.get("status") != "ok":
            return None
        payload = response.get("response")
        if not isinstance(payload, dict):
            return None
        for status in (payload.get("data") or {}).get("statuses", []):
            if isinstance(status, dict) and isinstance(status.get("resting"), dict):
                return status["resting"].get("oid")
        return None

    @staticmethod
    def _rejection(response):
        if hasattr(response, 'reconciliation'):
            return '' if response.status in {'FILLED','PARTIAL','CONFIGURED'} else response.status
        if not isinstance(response, dict) or response.get("status") != "ok":
            return str(response)
        payload = response.get("response")
        if isinstance(payload, dict):
            errors = [str(s["error"]) for s in (payload.get("data") or {}).get("statuses", [])
                      if isinstance(s, dict) and "error" in s]
            return "; ".join(errors)
        return ""

    def _cancel(self, coin, dex, orders):
        errors = []
        ids = {str(o["oid"]) for o in orders}
        for order in orders:
            try:
                if self.canonical_context is not None:
                    from core.confirmed_execution_adapter import execute_manual_request
                    key=self._market(coin,dex)[2]
                    receipt=execute_manual_request(self.canonical_context,coin=coin,dex=dex,action='CANCEL_OWNED',
                        side='BUY' if order.get('side')=='B' else 'SELL',size=float(order['sz']),
                        price=float(order.get('triggerPx') or order['limitPx']),
                        identity=self.runtime['manual_actions'][key]['id'],owned_order_id=str(order['oid']),
                        owned_order_ids=tuple(ids))
                    error=self._rejection(receipt)
                elif self.legacy_test_executor is not None:
                    error=self._rejection(self.legacy_test_executor.cancel(self.client,coin,order['oid'],dex))
                else:
                    raise ManualActionError('Canonical cancellation context required')
                if error:
                    errors.append(error)
            except Exception as exc:
                errors.append(str(exc))
        # The exchange state, not an HTTP acknowledgement, is the postcondition.
        current, gone = self._wait(lambda: self._market_orders(coin, dex),
                                   lambda rows: not any(str(o.get("oid")) in ids for o in rows))
        remaining = [o for o in current if str(o.get("oid")) in ids]
        return remaining, gone, errors

    def set_stop_loss(self, coin, dex, price):
        coin, dex, key = self._market(coin, dex)
        price = _number(price, "stop price")
        if callable(getattr(self.client, "round_price", None)):
            price = _number(self.client.round_price(coin, price, dex), "normalized stop price")
        position = self._position(key)
        if not position:
            raise ManualActionError("Position is already closed")
        current = _number(self.client.mid(coin, dex), "current price")
        side = str(position.get("side", "")).upper()
        if side not in {"LONG", "SHORT"} or price <= 0 or current <= 0:
            raise ManualActionError("Invalid stop side or price")
        if (side == "LONG" and price >= current) or (side == "SHORT" and price <= current):
            raise ManualActionError("LONG stop must be below market; SHORT stop must be above market")
        saved = deepcopy(self.runtime.setdefault("manual_stops", {}).get(key))
        old_orders = self._stop_orders(coin, dex, saved)
        self._begin(key, "set_stop", before_oids=[o["oid"] for o in old_orders], requested_price=price)
        try:
            if self.canonical_context is not None:
                from core.confirmed_execution_adapter import execute_manual_request
                response=execute_manual_request(self.canonical_context,coin=coin,dex=dex,action='PLACE_STOP',
                    side='SELL' if side=='LONG' else 'BUY',size=float(position['size']),price=price,
                    identity=self.runtime['manual_actions'][key]['id'],expected_position=position,
                    leverage=int(position['leverage']))
                oid=int(response.order_ids[0]) if response.status=='CONFIGURED' and response.order_ids else None
            elif self.legacy_test_executor is not None:
                response=self.legacy_test_executor.protect(self.client,coin,side,float(position['size']),price,dex)
                oid=self._accepted_oid(response)
            else:
                raise ManualActionError('Canonical protection context required')
            if oid is None:
                error = self._rejection(response)
                definitive = getattr(response,'status',None)=='REJECTED' if hasattr(response,'reconciliation') else bool(error)
                self._record(key, status="rejected" if definitive else "unknown", error=error or "No resting stop ID")
                raise ManualActionError(error or "Stop outcome is unconfirmed")
            self._record(key, new_oid=oid)
            orders, visible = self._wait(lambda: self._market_orders(coin, dex),
                                         lambda rows: any(str(o.get("oid")) == str(oid) for o in rows))
            if not visible:
                self._record(key, status="unknown", error="New stop not visible on exchange")
                raise ManualActionError("New stop is unconfirmed; previous stop was not cancelled")
            actual = next(o for o in orders if str(o.get("oid")) == str(oid))
            if not actual.get("reduceOnly"):
                self._record(key, status="unknown", error="Returned stop is not reduce-only")
                raise ManualActionError("New order protection is unconfirmed; previous stop was not cancelled")
            actual_price = _number(actual.get("triggerPx", price), "accepted stop price")
            stop = {"coin": coin, "dex": dex, "side": side, "price": actual_price,
                    "size": float(position["size"]), "oid": oid, "created": int(self.now() * 1000),
                    "extra_oids": [o["oid"] for o in old_orders], "verified": True}
            self.runtime["manual_stops"][key] = stop
            self._record(key, status="cleanup", stop=deepcopy(stop))
            remaining, gone, errors = self._cancel(coin, dex, old_orders)
            # Storage.update_runtime refreshes the Snapshot in place. Re-fetch
            # nested objects after each persist, never mutate a detached reference.
            self.runtime["manual_stops"][key]["extra_oids"] = [o["oid"] for o in remaining]
            self._record(key, status="complete" if gone else "cleanup_required", errors=errors)
            if not gone:
                raise ManualActionError("New stop is active, but previous stop cancellation is incomplete")
            return {"ok": True, "stop": deepcopy(self.runtime["manual_stops"][key])}
        except ManualActionError:
            raise
        except Exception as exc:
            self._record(key, status="unknown", error=str(exc))
            raise ManualActionError("Stop outcome is unconfirmed; do not submit it again automatically") from exc

    def delete_stop_loss(self, coin, dex):
        coin, dex, key = self._market(coin, dex)
        saved = self.runtime.setdefault("manual_stops", {}).get(key)
        orders = self._stop_orders(coin, dex, saved)
        self._begin(key, "delete_stop", before_oids=[o["oid"] for o in orders])
        try:
            remaining, gone, errors = self._cancel(coin, dex, orders)
            if not gone:
                self._record(key, status="cleanup_required", remaining_oids=[o["oid"] for o in remaining], errors=errors)
                raise ManualActionError("Stop is still on the exchange; local marker was preserved")
            self.runtime["manual_stops"].pop(key, None)
            self._record(key, status="complete", errors=errors)
            return {"ok": True, "already_removed": not orders}
        except ManualActionError:
            raise
        except Exception as exc:
            self._record(key, status="unknown", error=str(exc))
            raise ManualActionError("Cannot confirm stop removal; local marker was preserved") from exc

    def close_position(self, coin, dex):
        coin, dex, key = self._market(coin, dex)
        position = self._position(key)
        saved = self.runtime.setdefault("manual_stops", {}).get(key)
        stops = self._stop_orders(coin, dex, saved)
        holds = self.runtime.setdefault("manual_hold_keys", [])
        if key not in holds:
            holds.append(key)
        self._begin(key, "close", position=deepcopy(position))
        try:
            if position:
                from core.confirmed_execution_adapter import execute_manual_request
                if self.canonical_context is not None:
                    response = execute_manual_request(self.canonical_context, coin=coin, dex=dex,
                        side='SELL' if position.get('side') == 'LONG' else 'BUY', size=abs(float(position['size'])),
                        price=None, action='CLOSE', source='manual',identity=self.runtime['manual_actions'][key]['id'],
                        expected_position=position,leverage=int(position['leverage']))
                    if response.status not in {'FILLED','PARTIAL'}:
                        self._record(key,status='rejected' if response.status=='REJECTED' else 'unknown',error=response.status)
                        raise ManualActionError('Canonical close '+response.status)
                elif callable(self.legacy_test_executor):
                    response = self.legacy_test_executor(self.client, coin, dex)
                else:
                    raise ManualActionError(
                        "Canonical execution context unavailable; no order submitted"
                    )
                remaining, flat = self._wait(lambda: self._position(key), lambda p: p is None)
                if not flat:
                    self._record(key, status="incomplete", remaining=remaining,
                                 error=self._rejection(response))
                    raise ManualActionError("Position is not fully closed; stops remain active and copying is held")
            # Reduce-only SL cannot reverse a flat position. Cancel only after
            # confirming flat, so partial/failed closes retain exchange protection.
            remaining, gone, errors = self._cancel(coin, dex, stops)
            if not gone:
                self._record(key, status="cleanup_required", flat=True,
                             remaining_oids=[o["oid"] for o in remaining], errors=errors)
                raise ManualActionError("Position is closed, but exchange stop cleanup is incomplete")
            self.runtime["manual_stops"].pop(key, None)
            managed = self.runtime.get("managed", [])
            if key in managed:
                managed.remove(key)
            self._record(key, status="complete", flat=True, errors=errors)
            return {"ok": True, "closed": True, "copying_held": True}
        except ManualActionError:
            raise
        except Exception as exc:
            self._record(key, status="unknown", error=str(exc))
            raise ManualActionError("Close outcome is unconfirmed; copying is held and the order will not be retried automatically") from exc
