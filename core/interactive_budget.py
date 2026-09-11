"""Short authenticated Manual Copy review lease, not a trading policy.

1150 is the TOTAL planned rolling-minute ceiling. Interactive reads may use
760; the remaining capacity stays available to reconciliation / position
safety. Other background REST reads are narrowed to 300 while the lease is
held, not stopped. No worker/process is killed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid

_owner=ContextVar('manual_review_budget_owner',default=None)


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS interactive_budget(id INTEGER PRIMARY KEY,owner TEXT,expires REAL,safety_until REAL)')


@contextmanager
def manual_review_budget():
    from core.hl_budget import configured,BudgetUnavailable
    budget=configured()
    if budget is None:
        yield
        return
    owner=uuid.uuid4().hex
    with budget.db() as db:
        db.execute('BEGIN IMMEDIATE');schema(db)
        row=db.execute('SELECT owner,expires FROM interactive_budget WHERE id=1').fetchone()
        if row and row[1]>time.time():raise BudgetUnavailable('MANUAL_REVIEW_BUSY')
        db.execute('INSERT OR REPLACE INTO interactive_budget VALUES(1,?,?,?)',(owner,time.time()+120,time.time()+180))
    token=_owner.set(owner)
    try:yield
    finally:
        _owner.reset(token)
        # Keep safety headroom until the raised rolling-minute usage decays;
        # background traffic resumes under its normal low ceiling immediately.
        with budget.db() as db:db.execute('UPDATE interactive_budget SET expires=?,safety_until=? WHERE id=1 AND owner=?',
            (time.time(),time.time()+60,owner))


def admission(db,now,priority):
    from core.hl_budget import HARD_PLANNED_CEILING
    schema(db)
    row=db.execute('SELECT owner,expires,safety_until FROM interactive_budget WHERE id=1').fetchone()
    if not row:return None
    # Track the planned ceiling rather than repeating a literal: this branch
    # used to hand P0/P1 a hard-coded 1180 and would have kept planning to the
    # old limit after the ceiling moved.
    if priority<=1 and row[2]>now:return HARD_PLANNED_CEILING,False
    if row[1]<=now:return None
    if row[0]==_owner.get():return 760,False
    # A held lease used to return a ceiling of 0, which stopped active-watchlist
    # detection (P2) outright for the whole 120s lease. A small ceiling degrades
    # that traffic instead of halting it; 760 + 300 still sits under the 1150
    # plan, and P0/P1 keep the full ceiling through the branch above.
    return 300,False
