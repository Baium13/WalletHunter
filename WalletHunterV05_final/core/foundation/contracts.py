"""Immutable wire contracts. No free-form event dictionaries or credentials."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator, model_serializer

Amount = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Millis = Annotated[int, Field(strict=True, ge=0)]
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Network = Literal["MAINNET", "TESTNET"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1: raise ValueError("Unsupported schema version")
        return value


class Scope(Contract):
    tenant: Name
    account: Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
    network: Network


class InstrumentId(Contract):
    network: Network
    venue: Literal["HYPERLIQUID"] = "HYPERLIQUID"
    dex: Annotated[str, Field(pattern=r"^[a-z0-9_-]{0,24}$")] = ""
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,31}$")]

    @property
    def market_key(self):
        return f"{self.dex+':' if self.dex else ''}{self.symbol}|{self.dex}"


class MarketSnapshot(Contract):
    instrument: InstrumentId
    exchange_ms: Millis | None
    received_ms: Millis
    price: Positive | None
    bid: Positive | None
    ask: Positive | None
    completeness: Literal["COMPLETE", "UNKNOWN", "GAP"]
    freshness: Literal["FRESH", "STALE"]
    source: Literal["REST", "STREAM", "FAKE"]
    source_version: Name

    @model_validator(mode="after")
    def complete(self):
        if self.completeness == "COMPLETE" and any(x is None for x in (self.exchange_ms, self.price, self.bid, self.ask)):
            raise ValueError("Complete snapshot requires explicit evidence")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("Crossed book")
        return self


class Contribution(Contract):
    source: Name
    notional: Positive


class Position(Contract):
    instrument: InstrumentId
    side: Literal["LONG", "SHORT"]
    size: Positive
    entry_price: Positive
    notional: Positive
    margin: Amount | None
    leverage: Annotated[int, Field(strict=True, ge=1)]
    # Exchange position basis/equity evidence.  These are optional because
    # PAPER and older persisted snapshots do not always expose them.
    initial_entry_price: Positive | None = None
    liquidation_price: Positive | None = None
    margin_mode: Literal["cross", "isolated"] | None = None
    held: bool = False
    evidence: Literal["VERIFIED", "EXTERNAL", "UNKNOWN"]
    order_ids: tuple[Name, ...] = ()
    contributions: tuple[Contribution, ...] = ()

    @model_validator(mode="after")
    def provenance(self):
        if self.evidence != "VERIFIED" and self.contributions:
            raise ValueError("Unproven attribution")
        if self.evidence == "VERIFIED" and (not self.order_ids or not self.contributions):
            raise ValueError("Missing ownership evidence")
        if len({c.source for c in self.contributions}) != len(self.contributions):
            raise ValueError("Duplicate source contribution")
        return self


class OpenOrder(Contract):
    instrument: InstrumentId
    order_id: Name
    size: Positive
    reduce_only: bool


class PortfolioSnapshot(Contract):
    scope: Scope
    revision: Annotated[int, Field(strict=True, ge=1)]
    exchange_ms: Millis | None
    received_ms: Millis
    equity: Amount | None
    sizing_capital: Amount | None
    available_collateral: Amount | None
    collateral_dex: str | None = None
    positions: tuple[Position, ...] = ()
    orders: tuple[OpenOrder, ...] = ()
    completeness: Literal["COMPLETE", "UNKNOWN"]
    evidence: Literal["EXCHANGE", "FAKE", "LEGACY_UNKNOWN"]

    @model_validator(mode="after")
    def coherent(self):
        if any(p.instrument.network != self.scope.network for p in (*self.positions, *self.orders)):
            raise ValueError("Portfolio network mismatch")
        if len({p.instrument for p in self.positions}) != len(self.positions):
            raise ValueError("Duplicate position")
        if self.completeness == "COMPLETE" and any(x is None for x in (self.exchange_ms, self.equity, self.sizing_capital, self.available_collateral)):
            raise ValueError("Unknown collateral cannot be complete")
        if self.equity is not None and self.available_collateral is not None and self.available_collateral > self.equity:
            raise ValueError("Available collateral exceeds equity")
        return self


class Allocation(Contract):
    scope: Scope
    source: Name
    limit: Amount
    committed: Amount
    reserved: Amount
    available: Amount
    revision: Annotated[int, Field(strict=True, ge=1)]
    received_ms: Millis


class AnalysisResult(Contract):
    """Research only: deliberately has no authorization or executable order."""
    instrument: InstrumentId
    correlation_id: Name
    created_ms: Millis
    evidence_ids: tuple[Name, ...]
    conclusion: Literal["NO_OPINION", "RESEARCH_ONLY"]


class SourceContribution(Contract):
    """Strategy target weights, NOT claims of individual source exchange fills."""
    source: Name
    target_notional: Positive
    target_margin: Positive

    @model_validator(mode="after")
    def bounded_margin(self):
        if self.target_margin > self.target_notional:
            raise ValueError("Contribution margin exceeds notional")
        return self


class OrderIntent(Contract):
    version: Literal[1, 2, 3, 4] = 1
    intent_id: Name
    scope: Scope
    instrument: InstrumentId
    source: Name
    action: Literal["OPEN", "ADD", "REDUCE", "CLOSE", "LEVERAGE_UPDATE", "PLACE_STOP", "CANCEL_OWNED"]
    side: Literal["BUY", "SELL"]
    size: Positive
    limit_price: Positive
    order_type: Literal["IOC", "LEVERAGE", "STOP_MARKET", "CANCEL"] = "IOC"
    leverage: Annotated[int, Field(strict=True, ge=1)]
    slippage_pct: Annotated[float, Field(strict=True, gt=0, le=10, allow_inf_nan=False)]
    authorization: Literal["USER_CONFIRMED", "COPY_POLICY", "PAPER_TEST", "PAPER_POLICY"]
    execution_mode: Literal["FAKE", "PAPER", "LIVE"]
    correlation_id: Name
    created_ms: Millis
    expires_ms: Millis
    source_contributions: tuple[SourceContribution, ...] = ()
    parent_intent_id: Name | None = None
    configure_leverage: bool = False
    owned_order_id: Name | None = None
    exchange_client_id: Annotated[str, Field(pattern=r"^0x[a-f0-9]{32}$")] | None = None

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value not in (1, 2, 3, 4):
            raise ValueError("Unsupported intent schema version")
        return value

    @model_serializer(mode="wrap")
    def serialize_version(self, handler):
        data = handler(self)
        if self.version < 4:
            data.pop('owned_order_id', None)
            data.pop('exchange_client_id', None)
        if self.version == 1:
            # Existing persisted intent hashes/client IDs must not change merely
            # because optional v2 fields were added to the Python model.
            for key in ("source_contributions", "parent_intent_id", "configure_leverage"):
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def coherent(self):
        if self.instrument.network != self.scope.network or self.expires_ms <= self.created_ms:
            raise ValueError("Invalid intent identity/lifetime")
        if self.version < 4 and (self.owned_order_id or self.exchange_client_id or self.action in {'PLACE_STOP','CANCEL_OWNED'}):
            raise ValueError('Scoped confirmed operations require version 4')
        if self.version == 1:
            if self.source_contributions or self.parent_intent_id is not None or self.configure_leverage or self.action == "LEVERAGE_UPDATE" or self.order_type != "IOC":
                raise ValueError("Copy extensions require intent version 2")
        elif self.version == 3:
            if (self.authorization != 'USER_CONFIRMED' or self.execution_mode != 'LIVE'
                    or self.order_type not in ('IOC','LEVERAGE')
                    or self.source_contributions or self.parent_intent_id is not None):
                raise ValueError('Version 3 requires explicitly confirmed live entry')
        elif self.version == 4:
            if self.authorization != 'USER_CONFIRMED' or self.execution_mode != 'LIVE' or not self.parent_intent_id:
                raise ValueError('Confirmed operation requires durable parent authority')
            if (self.action == 'CANCEL_OWNED') != (self.owned_order_id is not None):
                raise ValueError('Cancellation requires exact owned order identity')
            expected_type = {'PLACE_STOP':'STOP_MARKET','CANCEL_OWNED':'CANCEL','LEVERAGE_UPDATE':'LEVERAGE'}.get(self.action,'IOC')
            if self.order_type != expected_type:
                raise ValueError('Confirmed action/order type mismatch')
        else:
            if self.authorization != "COPY_POLICY" or not self.source_contributions or self.parent_intent_id is None:
                raise ValueError("Version 2 requires copy authority, parent and explicit sources")
            if len(self.source_contributions) > 3 or len({c.source for c in self.source_contributions}) != len(self.source_contributions):
                raise ValueError("Invalid or duplicate copy sources")
            if self.parent_intent_id == self.intent_id:
                raise ValueError("Parent and executable child identities must differ")
            if (self.action == "LEVERAGE_UPDATE") != (self.order_type == "LEVERAGE"):
                raise ValueError("Leverage operation must not represent a market fill")
        return self


class RiskDecision(Contract):
    intent_id: Name
    intent_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    market_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    outcome: Literal["APPROVED", "REDUCED", "REJECTED"]
    reasons: tuple[Name, ...]
    approved_size: Amount
    approved_limit: Amount
    portfolio_revision: Annotated[int, Field(strict=True, ge=1)]
    created_ms: Millis


class Fill(Contract):
    intent_id: Name
    instrument: InstrumentId
    order_id: Name
    trade_id: Name
    side: Literal["BUY", "SELL"]
    size: Positive
    price: Positive
    exchange_ms: Millis


class ExecutionReceipt(Contract):
    intent_id: Name
    scope: Scope
    status: Literal["SUBMITTING", "FILLED", "PARTIAL", "UNKNOWN", "REJECTED", "CONFIGURED"]
    order_ids: tuple[Name, ...] = ()
    fills: tuple[Fill, ...] = ()
    reconciliation: Literal["CONFIRMED", "PARTIAL", "RECONCILIATION_REQUIRED", "REJECTED"]
    received_ms: Millis
    provenance: Literal["FAKE_EXCHANGE", "EXCHANGE", "UNKNOWN"]

    @model_validator(mode="after")
    def scoped_fills(self):
        if any(f.instrument.network != self.scope.network or f.intent_id != self.intent_id or f.order_id not in self.order_ids for f in self.fills):
            raise ValueError("Receipt fill identity mismatch")
        if self.status in {"FILLED", "PARTIAL"} and (not self.fills or self.provenance == "UNKNOWN"):
            raise ValueError("Filled receipt requires evidence")
        return self


Payload = MarketSnapshot | PortfolioSnapshot | OrderIntent | RiskDecision | ExecutionReceipt | AnalysisResult


class DomainEvent(Contract):
    event_id: Name
    event_type: Literal["MARKET_SNAPSHOT", "PORTFOLIO_SNAPSHOT", "LEADER_EVENT", "ORDER_INTENT_CREATED",
        "RISK_APPROVED", "RISK_REJECTED", "ORDER_SUBMITTED", "ORDER_PARTIALLY_FILLED", "ORDER_FILLED", "ORDER_REJECTED",
        "EXECUTION_UNKNOWN", "POSITION_OPENED", "POSITION_INCREASED", "POSITION_REDUCED", "LEVERAGE_CONFIGURED", "PROTECTION_CONFIGURED", "ORDER_CANCELLED", "POSITION_CHANGED", "POSITION_CLOSED", "RECONCILIATION_REQUIRED"]
    correlation_id: Name
    scope: Scope
    event_ms: Millis
    received_ms: Millis
    payload: Payload

    @model_validator(mode="after")
    def scoped(self):
        scope = getattr(self.payload, "scope", self.scope)
        instrument = getattr(self.payload, "instrument", None)
        if scope != self.scope or (instrument and instrument.network != self.scope.network):
            raise ValueError("Event scope mismatch")
        expected = {"MARKET_SNAPSHOT": MarketSnapshot, "PORTFOLIO_SNAPSHOT": PortfolioSnapshot,
            "LEADER_EVENT": AnalysisResult, "ORDER_INTENT_CREATED": OrderIntent,
            "RISK_APPROVED": RiskDecision, "RISK_REJECTED": RiskDecision}
        if not isinstance(self.payload, expected.get(self.event_type, ExecutionReceipt)):
            raise ValueError("Event type/payload mismatch")
        return self
