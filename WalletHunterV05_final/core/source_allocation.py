"""Pure, conservative source-budget accounting; this module never executes orders.

Each configured source retains exactly one third of the sizing balance. Durable
``source_targets`` are strategy weights, NOT individual exchange-fill ownership.
Only unique, same-direction weights are usable: their absolute signed notionals
allocate the ONE observed market margin proportionally. Mixed/netted provenance
is deliberately unavailable rather than guessed. Missing ``margin_used`` uses
the explicit nominal-margin estimate ``position_value / leverage``; a supplied
invalid value is never silently replaced.

Pending intents reserve the per-source maximum of observed, pre-order and target
requirements, not their sum. An intended close is not evidence of released funds.
``cap`` is a pure query: callers must rebuild the book after EVERY attempted
execution from fresh observations and durable intents. Reversal credit is valid
ONLY for an executor that confirms the old position flat before opening its new
side. No accounting result changes ownership, HOLD state, leverage or slot IDs.
"""

from copy import deepcopy
from dataclasses import dataclass
import math


class AllocationError(ValueError):
    """Source capacity cannot safely support the requested target."""


@dataclass(frozen=True)
class SourceAccount:
    allocation_limit: float
    committed_margin: float
    reserved_margin: float
    available_source_budget: float


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AllocationError(f"{name}: finite numeric value required")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise AllocationError(f"{name}: invalid numeric value") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise AllocationError(f"{name}: invalid nonnegative value")
    return result


def _signed(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AllocationError(f"{name}: finite signed value required")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise AllocationError(f"{name}: invalid signed value") from exc
    if not math.isfinite(result) or result == 0:
        raise AllocationError(f"{name}: nonzero finite signed value required")
    return result


def _total(values, name):
    try:
        return _number(math.fsum(values), name)
    except OverflowError as exc:
        raise AllocationError(f"{name}: numeric overflow") from exc


def _wallet(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AllocationError("Invalid source wallet")
    return value.lower()


def _market(value):
    if not isinstance(value, str) or value.strip() != value or value.count("|") != 1 or not value.split("|", 1)[0]:
        raise AllocationError("Invalid encoded market")
    return value


def _close(a, b):
    # No fixed dollar/quantity epsilon: it would accept orders-of-magnitude
    # mismatches for very small but valid positive quantities.
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=0.)


def _position(value):
    if not isinstance(value, dict) or value.get("side") not in ("LONG", "SHORT"):
        raise AllocationError("Invalid position side or shape")
    size = _number(value.get("size"), "position size", positive=True)
    notional = _number(value.get("position_value"), "position notional", positive=True)
    leverage = _number(value.get("leverage"), "position leverage", positive=True)
    if leverage < 1:
        raise AllocationError("Position leverage below one")
    nominal = _number(notional / leverage, "nominal position margin", positive=True)
    supplied = _number(value["margin_used"], "position margin") if "margin_used" in value else nominal
    return {"side": value["side"], "size": size, "notional": notional,
            "leverage": leverage, "nominal": nominal, "margin": max(supplied, nominal)}


class SourceAllocationBook:
    """Read-only snapshot of commitments and incremental pending reservations.

    Base argument malformation raises ``AllocationError``. Individual uncertain
    records are retained in ``errors``; valid commitments remain inspectable in
    ``accounts``, but any such error makes ``cap`` fail closed. Configured paused
    sources must still be supplied in ``sources``. ``excluded`` is ONLY for
    independently owned positions, never source-owned manual/AI HOLD positions.
    """

    def __init__(self, balance, sources, actual, owned, managed, pending, excluded=(), uncertain=()):
        self.balance = _number(balance, "sizing balance")
        if not isinstance(sources, list) or len(sources) > 3:
            raise AllocationError("At most three configured source wallets required")
        wallets = [_wallet(wallet) for wallet in sources]
        if len(set(wallets)) != len(wallets):
            raise AllocationError("Duplicate configured source")
        if not all(isinstance(rows, dict) for rows in (actual, owned, pending)):
            raise AllocationError("Position, ownership and pending mappings required")
        self._configured = set(wallets)
        self._known = set(wallets)
        self._actual = deepcopy(actual)
        self._owned = deepcopy(owned)
        self._pending = deepcopy(pending)
        try:
            self._managed = {_market(market) for market in managed}
            self._excluded = {_market(market) for market in excluded}
            self._uncertain = {_market(market) for market in uncertain}
        except TypeError as exc:
            raise AllocationError("Managed, excluded and uncertain market collections required") from exc
        self._positions = {}
        self._charges = {}
        self._reservations = {}
        self.errors = []
        for market in self._uncertain:
            self._error(market, "Uncertain ownership or recovery")
        for market, row in self._actual.items():
            try:
                _market(market)
                self._positions[market] = _position(row)
            except AllocationError as exc:
                self._error(str(market), str(exc))
        for market in set(self._actual) | set(self._owned) | self._managed:
            try:
                self._commit(_market(market))
            except AllocationError as exc:
                self._error(str(market), str(exc))
        for market, intent in self._pending.items():
            try:
                self._reserve(_market(market), intent)
            except AllocationError as exc:
                self._error(str(market), str(exc))
        self.accounts = {}
        for wallet in self._known:
            committed = _total((rows.get(wallet, 0.) for rows in self._charges.values()), "source committed margin")
            reserved = _total((rows.get(wallet, 0.) for rows in self._reservations.values()), "source reserved margin")
            limit = self.balance / 3 if wallet in self._configured else 0.
            self.accounts[wallet] = SourceAccount(limit, committed, reserved, max(0., limit - committed - reserved))

    def _error(self, market, reason):
        message = f"{market}: {reason}"
        if message not in self.errors:
            self.errors.append(message)

    def _weights(self, sources, side, market, *, record_errors=True):
        if not isinstance(sources, list) or not sources:
            raise AllocationError("Missing durable source targets")
        signed = {}
        for source in sources:
            if not isinstance(source, dict):
                raise AllocationError("Malformed source target")
            wallet = _wallet(source.get("wallet"))
            if wallet in signed:
                raise AllocationError("Duplicate source attribution")
            key = "signed_notional" if "signed_notional" in source else "signed"
            contribution = _signed(source.get(key), "source signed notional")
            if "signed_notional" in source and "signed" in source and not _close(contribution, _signed(source["signed"], "legacy signed notional")):
                raise AllocationError("Conflicting signed source fields")
            if (contribution > 0) != (side == "LONG"):
                raise AllocationError("Mixed, opposing or inconsistent source attribution")
            margin = _number(source.get("margin"), "source target margin", positive=True)
            if margin > abs(contribution) and not _close(margin, abs(contribution)):
                raise AllocationError("Source margin exceeds its signed notional")
            if "slot_budget" in source:
                _number(source["slot_budget"], "historical slot budget")
            if record_errors:
                self._known.add(wallet)
            if wallet not in self._configured:
                if not record_errors:
                    raise AllocationError("Source removed; stable slot attribution is unavailable")
                self._error(market, "Source removed; stable slot attribution is unavailable")
            signed[wallet] = abs(contribution)
        total = _total(signed.values(), "source contribution total")
        return {wallet: _number(value / total, "source weight", positive=True) for wallet, value in signed.items()}, total

    @staticmethod
    def _charge(weights, margin):
        return {wallet: _number(weight * margin, "allocated market margin") for wallet, weight in weights.items()}

    def _commit(self, market):
        if market in self._excluded:
            return
        record = self._owned.get(market)
        if record is not None and (not isinstance(record, dict) or not isinstance(record.get("managed"), bool)):
            raise AllocationError("Malformed durable ownership")
        if not record or not record["managed"]:
            if market in self._managed:
                raise AllocationError("Managed position has no durable source ownership")
            return  # Valid external positions are not assigned to any source.
        if market in self._actual:
            position = self._positions.get(market)
            if position is None:
                raise AllocationError("Invalid observed managed position")
        else:
            position = _position(record.get("position"))
            self._positions[market] = position  # Last evidence, never zero credit.
        if record.get("side", position["side"]) != position["side"]:
            raise AllocationError("Ownership side does not match position evidence")
        if "size" in record and not _close(_number(record["size"], "ownership size", positive=True), position["size"]):
            raise AllocationError("Ownership size does not match position evidence")
        if "position" in record:
            saved = _position(record["position"])
            if saved["side"] != position["side"] or not _close(saved["size"], position["size"]):
                raise AllocationError("Saved ownership position does not match position evidence")
        weights, _ = self._weights(record.get("source_targets"), position["side"], market)
        self._charges[market] = self._charge(weights, position["margin"])

    def _target(self, market, spec, *, record_errors=True):
        if not isinstance(spec, dict) or spec.get("side") not in ("LONG", "SHORT"):
            raise AllocationError("Invalid target side or shape")
        notional = _number(spec.get("target_notional"), "target notional", positive=True)
        leverage = _number(spec.get("leverage"), "target leverage", positive=True)
        if leverage < 1:
            raise AllocationError("Target leverage below one")
        nominal = _number(notional / leverage, "target nominal margin", positive=True)
        if "signed_notional" in spec:
            signed = _signed(spec["signed_notional"], "signed target notional")
            if (signed > 0) != (spec["side"] == "LONG") or not _close(abs(signed), notional):
                raise AllocationError("Inconsistent signed target")
        if "target_margin" in spec and not _close(_number(spec["target_margin"], "target margin"), nominal):
            raise AllocationError("Inconsistent target nominal margin")
        if "capital_pct" in spec:
            _number(spec["capital_pct"], "target capital percentage")
        weights, source_total = self._weights(spec.get("sources"), spec["side"], market, record_errors=record_errors)
        if not _close(source_total, notional):
            raise AllocationError("Source contributions do not match target notional")
        return weights, nominal, notional

    def _reserve(self, market, intent):
        if not isinstance(intent, dict) or intent.get("action") not in ("RECONCILE", "CLOSE"):
            raise AllocationError("Malformed or unsupported pending intent")
        before_row = intent.get("before")
        before = _position(before_row) if before_row is not None else None
        current = self._charges.get(market, {})
        envelope = dict(current)
        record = self._owned.get(market) or {}
        before_sources = record.get("source_targets") if isinstance(record, dict) else None
        if not before_sources and isinstance(before_row, dict):
            before_sources = before_row.get("source_targets")
        if before is not None:
            # Historical weights remain useful even if the latest snapshot has
            # already observed part of an unknown reduction or reversal.
            weights, _ = self._weights(before_sources, before["side"], market)
            for wallet, charge in self._charge(weights, before["margin"]).items():
                envelope[wallet] = max(envelope.get(wallet, 0.), charge)
        if intent["action"] == "CLOSE":
            if before is None or not envelope:
                raise AllocationError("Pending close lacks attributable pre-order evidence")
        else:
            target = intent.get("target")
            weights, nominal, _ = self._target(market, target)
            retained = max(0., before["margin"] - before["nominal"]) if before and before["side"] == target["side"] else 0.
            target_margin = _number(nominal + retained, "pending target margin")
            observed = self._positions.get(market)
            if observed is not None and market in self._actual:
                if observed["side"] == target["side"]:
                    target_margin = max(target_margin, observed["margin"])
                elif before is None or observed["side"] != before["side"]:
                    raise AllocationError("Observed pending position has unexplained direction")
            for wallet, charge in self._charge(weights, target_margin).items():
                envelope[wallet] = max(envelope.get(wallet, 0.), charge)
        self._reservations[market] = {wallet: max(0., charge - current.get(wallet, 0.)) for wallet, charge in envelope.items()}

    def cap(self, market, spec, existing=None):
        """Return a proportional target cap, never an accidental reduction.

        The caller may separately allow a proved reduction after an accounting
        error. This method itself never bypasses an error or unsettled intent.
        """
        market = _market(market)
        if self.errors:
            raise AllocationError("; ".join(self.errors))
        if market in self._pending or market in self._excluded or market in self._uncertain:
            raise AllocationError("Market has unresolved or independently owned exposure")
        result = deepcopy(spec)
        weights, nominal, notional = self._target(market, result, record_errors=False)
        known = self._positions.get(market)
        current = _position(existing) if existing is not None else known
        if existing is not None:
            if known is None or any(not _close(current[field], known[field]) for field in ("size", "notional", "leverage", "margin")) or current["side"] != known["side"]:
                raise AllocationError("Position changed since allocation snapshot")
        if current is not None and market not in self._charges:
            raise AllocationError("Existing position is not source-owned")
        if market in self._charges and market not in self._actual:
            raise AllocationError("Last managed position is not currently observed")
        same_side = current is not None and current["side"] == result["side"]
        retained = max(0., current["margin"] - current["nominal"]) if same_side else 0.
        current_charges = self._charges.get(market, {})
        factor = 1.
        for wallet, weight in weights.items():
            account = self.accounts.get(wallet)
            if account is None:
                raise AllocationError("Target source has no allocated budget")
            capacity = account.available_source_budget + current_charges.get(wallet, 0.)
            weighted_nominal = _number(nominal * weight, "weighted target margin", positive=True)
            factor = min(factor, (capacity - retained * weight) / weighted_nominal)
        if not math.isfinite(factor) or factor <= 0:
            raise AllocationError("No available source budget")
        factor = min(1., factor)
        capped_notional = _number(notional * factor, "capped target notional", positive=True)
        # Especially important for lower-leverage requests: shrinking the order
        # to make its collateral fit must not silently sell the current position.
        requested_reduction = same_side and notional < current["notional"] and not _close(notional, current["notional"])
        if requested_reduction and factor < 1. - 1e-9:
            raise AllocationError("Allocation cap would enlarge the requested reduction")
        if same_side and not requested_reduction:
            if capped_notional < current["notional"] and not _close(capped_notional, current["notional"]):
                raise AllocationError("Allocation cap would turn an increase into a reduction")
            if notional > current["notional"] and not _close(notional, current["notional"]) and (capped_notional <= current["notional"] or _close(capped_notional, current["notional"])):
                raise AllocationError("No source budget for the requested increase")
        for field in ("target_notional", "signed_notional", "target_margin", "capital_pct"):
            if field in result:
                result[field] *= factor
        for source in result["sources"]:
            for field in ("margin", "signed_notional", "signed"):
                if field in source:
                    source[field] *= factor
        return result
