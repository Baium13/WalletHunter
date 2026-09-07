"""Read-through legacy reservations. Never writes or upgrades the legacy journal.

Journal ownership is not fresh exchange proof. Only an already VERIFIED canonical
position can be attributed here. Unsupported AI/manual pending records block
capacity instead of being dropped; their lifecycle remains with the legacy owner.
Callers must hold the existing account guard while collecting all input evidence.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import math

from core.source_allocation import SourceAllocationBook
from .contracts import Allocation, PortfolioSnapshot, Scope
from .ledger import Ledger


@dataclass(frozen=True)
class JournalEvidence:
    scope: Scope
    owned: dict = field(repr=False)
    pending: dict = field(repr=False)


def _json_object(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result: raise ValueError("Duplicate journal field")
            result[key] = item
        return result
    def invalid(_): raise ValueError("Nonfinite journal number")
    result = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict): raise ValueError("Journal object required")
    return result


def read_journal(path, scope, *, limit=1000):
    """One bounded read transaction; a missing database is UNKNOWN, never empty.

    Tenant binding must already have been verified by the account boundary. The
    legacy schema is account-scoped, not tenant-scoped; this does not invent one.
    """
    scope = Scope.model_validate_json(scope.model_dump_json())
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Invalid journal read limit")
    path = Path(path).absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise ValueError("Journal evidence unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=10)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            owned_rows = db.execute("SELECT market,record FROM ownership WHERE account=? LIMIT ?",
                (scope.account, limit+1)).fetchall()
            pending_rows = db.execute("SELECT market,intent FROM operations WHERE account=? AND status IN ('PREPARED','UNKNOWN') LIMIT ?",
                (scope.account, limit+1)).fetchall()
            if max(len(owned_rows), len(pending_rows)) > limit:
                raise ValueError("Journal evidence truncated")
            owned = {key: _json_object(body) for key, body in owned_rows}
            pending = {key: _json_object(body) for key, body in pending_rows}
            if len(owned) != len(owned_rows) or len(pending) != len(pending_rows):
                raise ValueError("Duplicate journal identity")
            return JournalEvidence(scope, owned, pending)
    except (sqlite3.Error, ValueError, TypeError):
        raise ValueError("Journal evidence requires reconciliation") from None


class SynchronizedLedger:
    """Conservative bridge, reusing the P1.2 reservation envelope unchanged.

    No timestamps are refreshed, no stale reservations are released, and journal
    weights never turn an EXTERNAL/UNKNOWN exchange position into VERIFIED.
    Independent reservations must have been deduplicated by their lifecycle owner;
    any simultaneous legacy pending record and independent reservation fails closed
    until their identity relationship is proved by a future migration adapter.
    """
    def __init__(self, portfolio, sources, journal, *, reservations=(), now_ms, max_age_ms):
        self.portfolio = PortfolioSnapshot.model_validate_json(portfolio.model_dump_json())
        self.errors, self.allocations, self.available_capacity = [], {}, None
        reservations = tuple(reservations)
        base = Ledger(self.portfolio, sources, reservations)
        self.errors.extend(base.errors)
        self.allocations = dict(base.allocations)
        if (type(now_ms) is not int or type(max_age_ms) is not int or max_age_ms <= 0
                or self.portfolio.exchange_ms is None
                or not 0 <= now_ms-self.portfolio.exchange_ms <= max_age_ms
                or not 0 <= now_ms-self.portfolio.received_ms <= max_age_ms):
            self.errors.append("ACCOUNT_EVIDENCE_STALE")
        if not isinstance(journal, JournalEvidence) or journal.scope != portfolio.scope:
            self.errors.append("JOURNAL_SCOPE_MISMATCH")
            return
        owned, pending = deepcopy(journal.owned), deepcopy(journal.pending)
        if not isinstance(owned, dict) or not isinstance(pending, dict):
            self.errors.append("JOURNAL_INVALID")
            return
        actual, canonical_owned = {}, {}
        for p in self.portfolio.positions:
            key = p.instrument.market_key
            actual[key] = dict(side=p.side, size=p.size, entry_price=p.entry_price,
                position_value=p.notional, leverage=float(p.leverage), margin_used=p.margin)
            if p.evidence == "VERIFIED":
                canonical_owned[key] = dict(managed=True, side=p.side, size=p.size, position=actual[key],
                    source_targets=[dict(wallet=c.source, signed_notional=c.notional*(1 if p.side == "LONG" else -1),
                        margin=c.notional/p.leverage) for c in p.contributions])
        for key, record in owned.items():
            if not isinstance(record, dict) or type(record.get("managed")) is not bool:
                self.errors.append("JOURNAL_OWNERSHIP_INVALID")
                continue
            if not record["managed"]: continue
            if record.get("network") != self.portfolio.scope.network:
                self.errors.append("LEGACY_OR_NETWORK_MISMATCH")
            if key not in canonical_owned:
                self.errors.append("OWNERSHIP_NOT_CURRENTLY_PROVEN")
                continue
            # Verify legacy allocation weights independently. P1.2 checks size,
            # direction and saved position; compare resulting source commitments.
        for record in pending.values():
            if not isinstance(record, dict) or record.get("network") != self.portfolio.scope.network:
                self.errors.append("LEGACY_OR_NETWORK_MISMATCH")
        if pending and reservations:
            self.errors.append("RESERVATION_IDENTITY_UNRESOLVED")
        try:
            # Journal weights are not allowed to supersede canonical proof.
            checked = SourceAllocationBook(self.portfolio.sizing_capital, list(sources), actual,
                owned, {k for k, v in owned.items() if isinstance(v, dict) and v.get("managed") is True}, pending)
            self.errors.extend("JOURNAL_RECONCILIATION_REQUIRED" for _ in checked.errors)
            for source, account in checked.accounts.items():
                row = base.allocations.get(source)
                if row is None or not math.isclose(account.committed_margin, row.committed, rel_tol=1e-9, abs_tol=0.):
                    self.errors.append("ATTRIBUTION_DISAGREEMENT")
                    continue
                reserved = row.reserved + account.reserved_margin
                self.allocations[source] = Allocation(scope=portfolio.scope, source=source, limit=row.limit,
                    committed=row.committed, reserved=reserved, available=max(0., row.limit-row.committed-reserved),
                    revision=portfolio.revision, received_ms=portfolio.received_ms)
            if not self.errors:
                extra = math.fsum(row.reserved_margin for row in checked.accounts.values())
                self.available_capacity = max(0., base.available_capacity-extra)
        except (ValueError, TypeError, OverflowError):
            self.errors.append("JOURNAL_RECONCILIATION_REQUIRED")
        self.errors = list(dict.fromkeys(self.errors))

    def allocation(self, source):
        if self.errors or source not in self.allocations:
            raise ValueError("Ledger requires reconciliation")
        return self.allocations[source]
