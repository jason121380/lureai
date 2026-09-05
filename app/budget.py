"""Durable per-call reservations; unknown bills remain charged until reconciled."""
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class BudgetExhausted(ValueError):
    pass


def nonnegative(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('configuration must be finite and nonnegative')
    return value


def month_bounds():
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


SCHEMA = '''CREATE TABLE IF NOT EXISTS model_calls (
 call_id TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT NOT NULL,
 state TEXT NOT NULL, reserved_twd REAL NOT NULL, cost_twd REAL NOT NULL,
 reserved_tokens INTEGER NOT NULL, usage_known INTEGER NOT NULL DEFAULT 0,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 cached_input_tokens INTEGER NOT NULL DEFAULT 0,
 cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
 reconciliation TEXT NOT NULL DEFAULT ''
)'''


class CallLedger:
    def __init__(self, store, pricing, system_budget=0):
        self.store, self.pricing = store, pricing
        self.system_budget = nonnegative(system_budget)

    def reserve(self, call_id, user_id, amount, tokens):
        amount = nonnegative(amount)
        start, end = month_bounds()
        with self.store._lock, self.store.connection:
            # BEGIN IMMEDIATE also serializes separate SQLite connections.
            self.store.connection.execute('BEGIN IMMEDIATE')
            limits = [(None, self.system_budget)]
            if user_id is not None:
                limits.append((user_id, self.pricing.monthly_budget_twd))
            for who, limit in limits:
                if limit and self.store.usage_totals(who, start, end)['spend_twd'] + amount > limit:
                    raise BudgetExhausted('model budget exhausted')
            self.store.connection.execute(
                'INSERT INTO model_calls (call_id,user_id,created_at,state,reserved_twd,cost_twd,reserved_tokens) VALUES (?,?,?,?,?,?,?)',
                (call_id, user_id, datetime.now(timezone.utc).isoformat(), 'reserved', amount, amount, tokens))

    def get(self, call_id):
        with self.store._lock:
            row = self.store.connection.execute('SELECT * FROM model_calls WHERE call_id=?', (call_id,)).fetchone()
            return dict(row) if row else None

    def settle(self, call_id, state, usage, cost=None):
        known = isinstance(usage, dict) and all(k in usage for k in ('input_tokens', 'output_tokens'))
        values = {k: max(0, int((usage or {}).get(k, 0))) for k in
                  ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens')}
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            row = self.get(call_id)
            if row['state'] != 'reserved':
                return
            charge = nonnegative(cost) if cost is not None else self.pricing.cost_twd(**values) if known else row['reserved_twd']
            self.store.connection.execute(
                'UPDATE model_calls SET state=?,cost_twd=?,usage_known=?,input_tokens=?,output_tokens=?,cached_input_tokens=?,cache_write_input_tokens=? WHERE call_id=?',
                (state, charge, int(known), *values.values(), call_id))

    def reconcile(self, call_id, cost, reference, *, allow_pending=False):
        cost = nonnegative(cost)
        if not reference:
            raise ValueError('billing reference required')
        with self.store._lock, self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            row = self.get(call_id)
            if not row or (row['state'] == 'reserved' and not allow_pending):
                raise ValueError('call is not ready for reconciliation')
            if row['reconciliation']:
                if row['reconciliation'] != reference or row['cost_twd'] != cost:
                    raise ValueError('conflicting reconciliation')
                return
            self.store.connection.execute("UPDATE model_calls SET cost_twd=?,reconciliation=?,state='reconciled' WHERE call_id=?", (cost, reference, call_id))
