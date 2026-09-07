"""One source book for new routes; reuse Phase 1.2 instead of redefining thirds."""
import math
from dataclasses import dataclass
from core.source_allocation import SourceAllocationBook
from .contracts import Allocation, PortfolioSnapshot, Scope


@dataclass(frozen=True)
class Reservation:
    intent_id: str
    source: str
    margin: float
    account_capacity: float


class Ledger:
    def __init__(self, portfolio: PortfolioSnapshot, sources: tuple[str, ...], reservations=()):
        portfolio = PortfolioSnapshot.model_validate_json(portfolio.model_dump_json())
        reservations = tuple(reservations)
        self.portfolio = portfolio
        self.errors = []
        self.allocations = {}
        self.available_capacity = None
        if portfolio.completeness != "COMPLETE" or portfolio.evidence == "LEGACY_UNKNOWN":
            self.errors.append("PORTFOLIO_UNKNOWN")
            return
        actual, owned, uncertain = {}, {}, set()
        for p in portfolio.positions:
            key = p.instrument.market_key
            if p.margin is None:
                self.errors.append("MARGIN_UNKNOWN")
                continue
            actual[key] = dict(side=p.side, size=p.size, entry_price=p.entry_price,
                position_value=p.notional, leverage=float(p.leverage), margin_used=p.margin)
            if p.evidence == "VERIFIED":
                owned[key] = dict(managed=True, side=p.side, size=p.size, position=actual[key], source_targets=[
                    dict(wallet=c.source, signed_notional=c.notional * (1 if p.side == "LONG" else -1),
                         margin=c.notional/p.leverage) for c in p.contributions])
            else:
                # Even known external exposure is not assignable to a source.
                # The foundation conservatively forbids new risk until resolved.
                uncertain.add(key)
        try:
            book = SourceAllocationBook(portfolio.sizing_capital, list(sources), actual, owned,
                set(owned), {}, uncertain=uncertain)
            self.errors.extend("ATTRIBUTION_UNKNOWN" for _ in book.errors)
            held = {}
            ids = set()
            for row in reservations:
                if (row.intent_id in ids or row.source not in sources or
                    any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0
                        for v in (row.margin, row.account_capacity)) or row.account_capacity < row.margin):
                    raise ValueError("Invalid reservation")
                ids.add(row.intent_id)
                held[row.source] = held.get(row.source, 0.) + row.margin
            total_reserved = math.fsum(r.account_capacity for r in reservations)
            self.available_capacity = max(0., portfolio.available_collateral - total_reserved)
            for source, row in book.accounts.items():
                reserved = row.reserved_margin + held.get(source, 0.)
                self.allocations[source] = Allocation(scope=portfolio.scope, source=source,
                    limit=row.allocation_limit, committed=row.committed_margin, reserved=reserved,
                    available=max(0., row.allocation_limit-row.committed_margin-reserved),
                    revision=portfolio.revision, received_ms=portfolio.received_ms)
        except (ValueError, OverflowError):
            self.errors.append("LEDGER_INVALID")
        if self.errors:
            self.available_capacity = None

    def allocation(self, source):
        if self.errors or source not in self.allocations:
            raise ValueError("Ledger requires reconciliation")
        return self.allocations[source]
