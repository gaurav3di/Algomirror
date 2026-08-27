"""
Migration: Add the equity transactional state that increment 2 needs

Increment 1 shipped the equity tables read-only. Increment 2 places real
orders, and the schema is missing the columns that keep that safe:

  equity_holdings        an exit claim (exit_status plus the in-flight broker
                         order id) so the stop loss monitor and a manual sell
                         cannot both sell the same shares, plus the stop loss
                         and target breach records that let a CONFIRM mode
                         holding wait for approval without alerting again
  equity_order_splits    the vocabulary and evidence for a per-account result:
                         the raw broker error_type behind an INDETERMINATE
                         outcome, the attempt count, when the request actually
                         reached that broker, and the GTT id kept apart from
                         the order id
  equity_orders          GTT trigger price, trade nature, what raised the
                         order, the rounding leftover, and the insufficient
                         funds policy that was in force
  equity_watchlist_items alert direction and the alert fired marker, so a
                         crossed alert price does not re-alert every refresh
  equity_settings        new table, module wide equity preferences per user
  equity_trades          unique index on (split_id, broker_trade_id) so a
                         repeated fill poll cannot book the same fill twice

Idempotency and portability. Existing columns are detected with the SQLAlchemy
inspector rather than PRAGMA table_info, which is SQLite only, so re-running
this on either database adds nothing twice. Column types are chosen per
dialect, because PostgreSQL has TIMESTAMP and no DATETIME. Every ALTER commits
on its own, so a failure part way through leaves the columns already added in
place and a re-run picks up from there.
"""

from sqlalchemy import text, inspect


def _column_names(db, table_name):
    """Existing column names for a table, empty when the table is absent"""
    try:
        return {col['name'] for col in inspect(db.engine).get_columns(table_name)}
    except Exception:
        return set()


def _add_columns(db, table_name, columns):
    """
    Add each missing column to a table, committing one at a time.

    Args:
        db: the SQLAlchemy object
        table_name: table to alter
        columns: list of (column_name, sql_type_and_constraints) tuples

    Returns:
        Number of columns actually added.
    """
    existing = _column_names(db, table_name)
    if not existing:
        print(f"  Table {table_name} not present, skipping (run 013 first)")
        return 0

    added = 0
    for column_name, column_sql in columns:
        if column_name in existing:
            print(f"  {table_name}.{column_name} already exists, skipping")
            continue

        try:
            db.session.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            ))
            db.session.commit()
            print(f"  Added {table_name}.{column_name}")
            added += 1
        except Exception:
            db.session.rollback()
            raise

    return added


def upgrade(db):
    """Add the equity transactional state columns, table and indexes"""

    # Imported here so the migration module can be loaded outside an app context
    from app.models import (
        EquitySetting,
        EQUITY_ORDER_SOURCE_MANUAL,
        EQUITY_FUNDS_ACTION_SKIP,
        EQUITY_EXIT_MODE_CONFIRM,
        EQUITY_HOLDING_STATUS_ACTIVE,
    )

    is_postgres = db.engine.dialect.name == 'postgresql'

    # PostgreSQL has no DATETIME type and SQLite has no real boolean literal
    # handling worth relying on, so pick the spelling per dialect.
    dt = 'TIMESTAMP' if is_postgres else 'DATETIME'
    bool_false = 'BOOLEAN NOT NULL DEFAULT FALSE' if is_postgres else 'BOOLEAN NOT NULL DEFAULT 0'

    total_added = 0

    # ------------------------------------------------------------------
    # equity_watchlist_items: M3 price alert state
    # ------------------------------------------------------------------
    total_added += _add_columns(db, 'equity_watchlist_items', [
        ('alert_direction', 'VARCHAR(10)'),
        ('alert_triggered_at', dt),
        ('alert_triggered_price', 'FLOAT'),
    ])

    # ------------------------------------------------------------------
    # equity_orders: M4 order instruction and M4b order status
    # ------------------------------------------------------------------
    total_added += _add_columns(db, 'equity_orders', [
        ('trigger_price', 'FLOAT'),
        ('trade_nature_id', 'INTEGER REFERENCES equity_trade_natures(id)'),
        ('source', f"VARCHAR(20) NOT NULL DEFAULT '{EQUITY_ORDER_SOURCE_MANUAL}'"),
        ('leftover_quantity', 'INTEGER NOT NULL DEFAULT 0'),
        ('insufficient_funds_action', f"VARCHAR(10) NOT NULL DEFAULT '{EQUITY_FUNDS_ACTION_SKIP}'"),
        ('cancelled_at', dt),
        ('error_message', 'TEXT'),
    ])

    # ------------------------------------------------------------------
    # equity_order_splits: per-account outcome, including the evidence that
    # separates an indeterminate result from a rejection
    # ------------------------------------------------------------------
    total_added += _add_columns(db, 'equity_order_splits', [
        ('ratio_quantity', 'INTEGER'),
        ('qty_overridden', bool_false),
        ('broker_gtt_id', 'VARCHAR(100)'),
        ('error_type', 'VARCHAR(50)'),
        ('broker_order_status', 'VARCHAR(50)'),
        ('placed_at', dt),
        ('last_synced_at', dt),
        ('attempt_count', 'INTEGER NOT NULL DEFAULT 0'),
    ])

    # ------------------------------------------------------------------
    # equity_holdings: the exit claim and the breach records
    # ------------------------------------------------------------------
    total_added += _add_columns(db, 'equity_holdings', [
        ('exit_status', f"VARCHAR(20) NOT NULL DEFAULT '{EQUITY_HOLDING_STATUS_ACTIVE}'"),
        ('exit_reason', 'VARCHAR(20)'),
        ('exit_quantity', 'INTEGER'),
        ('exit_claimed_at', dt),
        ('exit_submitted_at', dt),
        ('exit_completed_at', dt),
        ('exit_broker_order_id', 'VARCHAR(100)'),
        ('exit_split_id', 'INTEGER REFERENCES equity_order_splits(id)'),
        ('exit_error', 'TEXT'),
        ('sl_hit_at', dt),
        ('sl_hit_price', 'FLOAT'),
        ('tp_hit_at', dt),
        ('tp_hit_price', 'FLOAT'),
        ('last_monitored_at', dt),
    ])

    print(f"\nEquity state columns: {total_added} added")

    # ------------------------------------------------------------------
    # Backfill. A column added as NOT NULL DEFAULT is already filled on every
    # existing row, so these only matter when the column was created some other
    # way (db.create_all on a database made between two runs of this script).
    # ------------------------------------------------------------------
    backfills = [
        ("UPDATE equity_orders SET source = :value WHERE source IS NULL",
         {'value': EQUITY_ORDER_SOURCE_MANUAL}),
        ("UPDATE equity_orders SET insufficient_funds_action = :value "
         "WHERE insufficient_funds_action IS NULL",
         {'value': EQUITY_FUNDS_ACTION_SKIP}),
        ("UPDATE equity_orders SET leftover_quantity = 0 WHERE leftover_quantity IS NULL", {}),
        ("UPDATE equity_order_splits SET attempt_count = 0 WHERE attempt_count IS NULL", {}),
        ("UPDATE equity_holdings SET exit_status = :value WHERE exit_status IS NULL",
         {'value': EQUITY_HOLDING_STATUS_ACTIVE}),
        ("UPDATE equity_holdings SET exit_mode = :value WHERE exit_mode IS NULL",
         {'value': EQUITY_EXIT_MODE_CONFIRM}),
    ]

    for statement, params in backfills:
        try:
            db.session.execute(text(statement), params)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"  Backfill skipped: {e}")

    # ------------------------------------------------------------------
    # equity_settings: new table for module wide equity preferences
    # ------------------------------------------------------------------
    existing_tables = set(inspect(db.engine).get_table_names())
    if EquitySetting.__tablename__ in existing_tables:
        print(f"  Table {EquitySetting.__tablename__} already exists, skipping")
    else:
        db.metadata.create_all(
            bind=db.engine,
            tables=[EquitySetting.__table__],
            checkfirst=True
        )
        print(f"  Created table {EquitySetting.__tablename__}")

    # ------------------------------------------------------------------
    # Indexes for the new columns. CREATE INDEX IF NOT EXISTS works on both
    # SQLite 3.3+ and PostgreSQL 9.5+, the same way migration 013 does it.
    # ------------------------------------------------------------------
    indexes = [
        # equity_orders
        ('ix_equity_orders_trade_nature_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_trade_nature_id ON equity_orders(trade_nature_id)'),
        ('ix_equity_orders_source',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_source ON equity_orders(source)'),

        # equity_order_splits
        ('ix_equity_order_splits_broker_gtt_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_broker_gtt_id ON equity_order_splits(broker_gtt_id)'),
        ('ix_equity_order_splits_placed_at',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_placed_at ON equity_order_splits(placed_at)'),

        # equity_holdings, including the composite the monitor scan uses
        ('ix_equity_holdings_exit_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_exit_status ON equity_holdings(exit_status)'),
        ('ix_equity_holdings_exit_broker_order_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_exit_broker_order_id ON equity_holdings(exit_broker_order_id)'),
        ('ix_equity_holdings_exit_split_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_exit_split_id ON equity_holdings(exit_split_id)'),
        ('ix_equity_holdings_user_exit_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_user_exit_status ON equity_holdings(user_id, exit_status)'),

        # equity_settings
        ('ix_equity_settings_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_settings_user_id ON equity_settings(user_id)'),
    ]

    created_count = 0
    for index_name, create_sql in indexes:
        try:
            db.session.execute(text(create_sql))
            db.session.commit()
            created_count += 1
        except Exception as e:
            db.session.rollback()
            print(f"  Failed to create {index_name}: {e}")

    print(f"Equity state indexes: {created_count} created or already present")

    # ------------------------------------------------------------------
    # Fill de-duplication. Broker fills are read by polling, and the same fill
    # comes back on every poll, so without this a repeated poll doubles the
    # recorded quantity. Repeated NULLs are allowed in a unique index on both
    # databases, so a broker that returns no trade id is unaffected.
    # ------------------------------------------------------------------
    try:
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_equity_trades_split_broker_uc '
            'ON equity_trades(split_id, broker_trade_id)'
        ))
        db.session.commit()
        print("Equity trade de-duplication index created or already present")
    except Exception as e:
        db.session.rollback()
        print(f"  WARNING: could not create ix_equity_trades_split_broker_uc: {e}")
        print("  Duplicate (split_id, broker_trade_id) rows must be removed, then re-run this migration.")

    # ------------------------------------------------------------------
    # Seed one equity settings row per user, idempotent
    # ------------------------------------------------------------------
    try:
        result = db.session.execute(text("SELECT id FROM users"))
        user_ids = [row[0] for row in result.fetchall()]
        for user_id in user_ids:
            EquitySetting.get_or_create(user_id)
        print(f"Equity settings present for {len(user_ids)} user(s)")
    except Exception as e:
        db.session.rollback()
        print(f"  Skipped equity settings seeding: {e}")


def downgrade(db):
    """
    Drop the equity settings table and the indexes this migration created.

    The columns are deliberately left in place. They carry the exit claim, and
    dropping exit_status or exit_broker_order_id from a database that has live
    holdings would remove the only record that a sell is in flight. Older
    SQLite builds also cannot drop a column at all. Removing them is a manual,
    considered operation, not something a downgrade should do on its own.
    """
    index_names = [
        'ix_equity_orders_trade_nature_id',
        'ix_equity_orders_source',
        'ix_equity_order_splits_broker_gtt_id',
        'ix_equity_order_splits_placed_at',
        'ix_equity_holdings_exit_status',
        'ix_equity_holdings_exit_broker_order_id',
        'ix_equity_holdings_exit_split_id',
        'ix_equity_holdings_user_exit_status',
        'ix_equity_trades_split_broker_uc',
        'ix_equity_settings_user_id',
    ]

    for index_name in index_names:
        try:
            db.session.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"  Failed to drop {index_name}: {e}")

    try:
        db.session.execute(text("DROP TABLE IF EXISTS equity_settings"))
        db.session.commit()
        print("  Dropped table equity_settings")
    except Exception as e:
        db.session.rollback()
        print(f"  Failed to drop equity_settings: {e}")

    print("  Columns added by this migration were left in place on purpose")
