"""Versioned, finite, immutable intelligence contracts; confidence is heuristic."""
from typing import Literal, Annotated
from pydantic import Field, model_validator
from core.foundation.contracts import Contract, InstrumentId, Name, Millis, Positive, Amount, PortfolioSnapshot, MarketSnapshot, Allocation
from pydantic import StrictBool

Wallet = Annotated[str, Field(pattern=r'^0x[0-9a-f]{40}$')]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class LiquidityThresholds(Contract):
    """Spread and depth floors for one market or one whole collateral pool.

    ``symbol`` omitted matches every market on that DEX; the most specific
    entry wins. Without this a single floor written for BTC's book judges an
    xyz stock and every mid-cap perp, which excludes them outright rather than
    trading them smaller - the opposite of following whatever a leader trades.
    """
    dex: Literal['','xyz']
    symbol: Name | None = None
    max_spread_bps: Positive
    minimum_depth_usd: Positive


class IntelligencePolicy(Contract):
    policy_id: Name = 'intelligence-v1'
    registry_limit: int = Field(default=2000, ge=1, le=10000)
    watch_limit: int = Field(default=8, ge=1, le=32)
    # Base and ceiling for deep analyses per cycle. The base is what a
    # constrained budget still guarantees; the ceiling is what an idle one is
    # allowed to reach. A single fixed slot capped pool growth at roughly one
    # wallet per cycle no matter how much budget was going unused.
    deep_per_cycle: int = Field(default=1, ge=0, le=16)
    deep_max_per_cycle: int = Field(default=4, ge=1, le=16)
    request_limit: int = Field(default=32, ge=1, le=128)
    history_requests: int = Field(default=8, ge=1, le=32)
    cheap_min_fills: int = Field(default=5, ge=1)
    min_closes: int = Field(default=40, ge=10)
    min_activity_days: int = Field(default=7, ge=2)
    promotion_score: Unit = .65
    promotion_confidence: Unit = .4
    reevaluate_ms: int = Field(default=21600000, ge=60000)
    max_signal_age_ms: int = Field(default=60000, ge=1000, le=300000)
    # An exit is not an entry. A stale ENTRY signal means the price has moved
    # and the setup is gone, so refusing is right. A stale EXIT means the
    # leader has already left a position we still hold and have proven: being
    # late is a reason to hurry, not a reason to stay in. Sharing one 60s
    # window meant every CLOSE that waited in the research queue was refused,
    # which is why episodes opened and none ever resolved. Set this equal to
    # max_signal_age_ms to restore the single-window behaviour.
    max_exit_signal_age_ms: int = Field(default=900000, ge=1000, le=3600000)
    # Live research per cycle. The base is what a quiet queue uses; the
    # ceiling is what a backlog may reach. Expired events are cleared without
    # a market read and do not count against either.
    research_per_cycle: int = Field(default=4, ge=1, le=32)
    research_max_per_cycle: int = Field(default=12, ge=1, le=32)
    research_scan_limit: int = Field(default=64, ge=1, le=512)
    max_market_age_ms: int = Field(default=30000, ge=1000, le=60000)
    max_spread_bps: Positive = 20.
    minimum_depth_usd: Positive = 10000.
    consensus_threshold: Unit = .6
    # STRICT keeps the original behaviour, where a disagreeing 15m trend or a
    # blended score under consensus_threshold vetoes the entry outright. In
    # WEIGHTED the same signals scale the position DOWN instead of refusing it,
    # and consensus_threshold is replaced by min_entry_confidence as the floor.
    # Nothing here relaxes a financial guard: every BLOCK, every stale or
    # missing agent and every risk_context check still vetoes in both modes.
    consensus_mode: Literal['STRICT','WEIGHTED'] = 'WEIGHTED'
    trend_weight: Unit = .35
    volatility_penalty: Unit = .5
    context_caution_penalty: Unit = .25
    min_entry_confidence: Unit = .1
    depth_penalty: Unit = .35
    flow_weight: Unit = .25
    flow_min_prints: int = Field(default=20, ge=1, le=10000)
    flow_max_age_ms: int = Field(default=120000, ge=1000, le=900000)
    flow_reference_usd: Positive = 250000.
    liquidity_overrides: tuple[LiquidityThresholds,...] = ()
    weights: tuple[float, float, float, float, float] = (.3,.2,.2,.15,.15)

    @model_validator(mode='after')
    def weights_valid(self):
        if any(w < 0 for w in self.weights) or abs(sum(self.weights)-1) > 1e-9:
            raise ValueError('Invalid scoring weights')
        seen=set()
        for entry in self.liquidity_overrides:
            key=(entry.dex,entry.symbol)
            if key in seen: raise ValueError('Duplicate liquidity override')
            seen.add(key)
        return self

    def signal_window_ms(self, action):
        """How long this action stays actionable after the leader's fill."""
        return self.max_exit_signal_age_ms if action in ('REDUCE','CLOSE') else self.max_signal_age_ms

    def liquidity_for(self, instrument):
        """(max_spread_bps, minimum_depth_usd) for one market.

        Exact market beats whole-DEX beats the policy default, so a stock or a
        thin perp is judged against a book it can actually have.
        """
        best=None
        for entry in self.liquidity_overrides:
            if entry.dex!=(instrument.dex or ''): continue
            if entry.symbol is not None and entry.symbol!=instrument.symbol: continue
            rank=1 if entry.symbol is not None else 0
            if best is None or rank>best[0]: best=(rank,entry)
        if best is None: return self.max_spread_bps,self.minimum_depth_usd
        return best[1].max_spread_bps,best[1].minimum_depth_usd


class LeaderScore(Contract):
    wallet: Wallet
    network: Literal['MAINNET','TESTNET']
    policy_id: Name
    computed_ms: Millis
    score: Unit
    confidence: Unit
    profit_quality: Unit
    consistency: Unit
    drawdown_quality: Unit
    sample_quality: Unit
    recent_quality: Unit
    anomaly: Unit
    qualified: bool
    reasons: tuple[Name,...]


class LeaderTradeEvent(Contract):
    event_id: Name
    wallet: Wallet
    instrument: InstrumentId
    action: Literal['OPEN','ADD','REDUCE','CLOSE','REVERSE']
    side: Literal['BUY','SELL']
    size: Positive
    before_size: float = Field(allow_inf_nan=False)
    after_size: float = Field(allow_inf_nan=False)
    exchange_ms: Millis
    received_ms: Millis
    fill_id: Name
    evidence: Literal['EXCHANGE_FILL'] = 'EXCHANGE_FILL'


class AgentResult(Contract):
    agent_id: Name
    instrument: InstrumentId
    created_ms: Millis
    direction: Literal['LONG','SHORT','PASS','CAUTION','WAIT','BLOCK']
    confidence: Unit
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    evidence: tuple[Name,...]
    invalidation: tuple[Name,...] = ()
    freshness: Literal['FRESH','STALE','UNKNOWN']
    feature_version: Name = 'features-v1'
    strategy_version: Name = 'agents-v1'


class ConsensusDecision(Contract):
    event_id: Name
    policy_id: Name
    created_ms: Millis
    decision: Literal['COPY_LONG','COPY_SHORT','WAIT','SKIP','REJECT']
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    confidence: Unit
    participants: tuple[Name,...]
    blockers: tuple[Name,...]
    supporting: tuple[Name,...]
    opposing: tuple[Name,...]
    # Signals that reduced conviction without refusing the trade. These are
    # deliberately NOT blockers: authorization rejects on any blocker, while
    # attenuation only makes the resulting position smaller.
    attenuation: tuple[Name,...] = ()


class RiskContextEvidence(Contract):
    portfolio: PortfolioSnapshot
    market: MarketSnapshot
    allocation: Allocation
    unresolved: StrictBool | None
    required_margin: Amount
    required_capacity: Amount
    slippage_pct: Amount
    max_slippage_pct: Positive
