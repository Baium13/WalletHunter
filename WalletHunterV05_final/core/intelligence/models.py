"""Versioned, finite, immutable intelligence contracts; confidence is heuristic."""
from typing import Literal, Annotated
from pydantic import Field, model_validator
from core.foundation.contracts import Contract, InstrumentId, Name, Millis, Positive, Amount

Wallet = Annotated[str, Field(pattern=r'^0x[0-9a-f]{40}$')]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class IntelligencePolicy(Contract):
    policy_id: Name = 'intelligence-v1'
    registry_limit: int = Field(default=2000, ge=1, le=10000)
    watch_limit: int = Field(default=8, ge=1, le=32)
    deep_per_cycle: int = Field(default=1, ge=0, le=4)
    request_limit: int = Field(default=32, ge=1, le=128)
    history_requests: int = Field(default=8, ge=1, le=32)
    cheap_min_fills: int = Field(default=5, ge=1)
    min_closes: int = Field(default=40, ge=10)
    min_activity_days: int = Field(default=7, ge=2)
    promotion_score: Unit = .65
    promotion_confidence: Unit = .4
    reevaluate_ms: int = Field(default=21600000, ge=60000)
    max_signal_age_ms: int = Field(default=60000, ge=1000, le=300000)
    max_market_age_ms: int = Field(default=30000, ge=1000, le=60000)
    max_spread_bps: Positive = 20.
    minimum_depth_usd: Positive = 10000.
    consensus_threshold: Unit = .6
    weights: tuple[float, float, float, float, float] = (.3,.2,.2,.15,.15)

    @model_validator(mode='after')
    def weights_valid(self):
        if any(w < 0 for w in self.weights) or abs(sum(self.weights)-1) > 1e-9:
            raise ValueError('Invalid scoring weights')
        return self


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
    direction: Literal['LONG','SHORT','PASS','CAUTION','WAIT']
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
