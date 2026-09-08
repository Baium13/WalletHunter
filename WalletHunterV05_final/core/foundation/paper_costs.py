"""Versioned simulation assumptions. Limit fills include the observed spread.

Fees are applied on every filled notional; no additional invented depth/slippage.
These are simulated costs, not exchange production performance certification.
"""
from pydantic import Field
from .contracts import Contract,Name,Amount

class PaperCosts(Contract):
    model_id: Name='paper-limit-taker-v1'
    fee_bps: Amount=5.
    additional_slippage_bps: Amount=0.
    fill_price: str=Field(default='APPROVED_LIMIT',pattern='^APPROVED_LIMIT$')
    partial_semantics: str=Field(default='PROVEN_FILLED_ONLY',pattern='^PROVEN_FILLED_ONLY$')

    def fee(self,fills):
        import math
        if self.additional_slippage_bps!=0:raise ValueError('Unsupported slippage model; never widen approved limit')
        return math.fsum(f.size*f.price for f in fills)*self.fee_bps/10000

def costed_effect(intent,before,fills,now,costs):
    from .paper_effect import effect
    from .contracts import PortfolioSnapshot
    after=effect(intent,before,fills,now)
    fee=costs.fee(fills)
    return PortfolioSnapshot.model_validate(dict(after.model_dump(),equity=after.equity-fee,
        available_collateral=after.available_collateral-fee))
