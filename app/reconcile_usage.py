"""Operator billing reconciliation; never invokes the generation provider."""
import argparse
import json
from pathlib import Path
from .budget import CallLedger, nonnegative
from .replica import PostgresReplica, connection_string
from .storage import KnowledgeStore
from .usage import UsagePricing


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect or reconcile billed calls. Stop serving before reconciling pending calls. PostgreSQL mode acquires the exclusive writer lease and restores before editing.')
    parser.add_argument('--db', required=True, help='SQLite working database path')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='List unknown or pending charges')
    settle = commands.add_parser('settle', help='Apply a provider-confirmed total charge in TWD')
    settle.add_argument('call_id')
    settle.add_argument('--cost-twd', type=nonnegative, required=True)
    settle.add_argument('--reference', required=True, help='Unique invoice or billing evidence reference')
    settle.add_argument('--allow-pending', action='store_true', help='Explicitly reconcile a crashed call; serving must be stopped')
    args = parser.parse_args(argv)
    dsn = connection_string()
    if not dsn and not Path(args.db).is_file():
        parser.error('SQLite database does not exist')
    store = KnowledgeStore(args.db)
    replica = PostgresReplica(dsn) if dsn else None
    try:
        if replica:
            replica.restore(store)
        ledger = CallLedger(store, UsagePricing.from_env())
        if args.command == 'list':
            with store._lock:
                rows = [dict(row) for row in store.connection.execute(
                    "SELECT * FROM model_calls WHERE usage_known=0 AND reconciliation='' ORDER BY created_at")]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            ledger.reconcile(args.call_id, args.cost_twd, args.reference, allow_pending=args.allow_pending)
            if replica:
                replica.backup(store)
            print(json.dumps(ledger.get(args.call_id), ensure_ascii=False, indent=2))
        return 0
    finally:
        if replica:
            replica.stop()
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
