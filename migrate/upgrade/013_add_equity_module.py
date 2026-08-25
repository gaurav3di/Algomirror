"""
Migration: Add the Equity Trading module tables

Creates the eight equity tables that back the equity module:
  equity_account_allocations, equity_trade_natures, equity_watchlist_items,
  equity_orders, equity_order_splits, equity_trades, equity_holdings,
  equity_brokerage_rates

Tables are created from the SQLAlchemy metadata so the DDL is correct on both
SQLite and PostgreSQL, then the indexes are (re)created with
CREATE INDEX IF NOT EXISTS so the migration is safe to run again on a database
where db.create_all() already made the tables.
"""

from sqlalchemy import text, inspect


# Table creation order matters for foreign keys: parents before children
EQUITY_TABLES = [
    'equity_trade_natures',
    'equity_account_allocations',
    'equity_watchlist_items',
    'equity_orders',
    'equity_order_splits',
    'equity_trades',
    'equity_holdings',
    'equity_brokerage_rates',
]


def upgrade(db):
    """Create the equity module tables and their indexes"""

    # Imported here so the migration module can be loaded outside an app context
    from app.models import (
        EquityAccountAllocation,
        EquityTradeNature,
        EquityWatchlistItem,
        EquityOrder,
        EquityOrderSplit,
        EquityTrade,
        EquityHolding,
        EquityBrokerageRate,
    )

    models = [
        EquityTradeNature,
        EquityAccountAllocation,
        EquityWatchlistItem,
        EquityOrder,
        EquityOrderSplit,
        EquityTrade,
        EquityHolding,
        EquityBrokerageRate,
    ]

    existing_tables = set(inspect(db.engine).get_table_names())

    tables_to_create = []
    for model in models:
        if model.__tablename__ in existing_tables:
            print(f"  Table {model.__tablename__} already exists, skipping")
        else:
            tables_to_create.append(model.__table__)

    if tables_to_create:
        db.metadata.create_all(bind=db.engine, tables=tables_to_create, checkfirst=True)
        for table in tables_to_create:
            print(f"  Created table {table.name}")

    indexes = [
        # equity_account_allocations
        ('ix_equity_account_allocations_account_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_account_allocations_account_id ON equity_account_allocations(account_id)'),
        ('ix_equity_account_allocations_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_account_allocations_user_id ON equity_account_allocations(user_id)'),
        ('ix_equity_account_allocations_is_active',
         'CREATE INDEX IF NOT EXISTS ix_equity_account_allocations_is_active ON equity_account_allocations(is_active)'),
        ('ix_equity_account_allocations_user_active',
         'CREATE INDEX IF NOT EXISTS ix_equity_account_allocations_user_active ON equity_account_allocations(user_id, is_active)'),

        # equity_trade_natures
        ('ix_equity_trade_natures_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_trade_natures_user_id ON equity_trade_natures(user_id)'),
        ('ix_equity_trade_natures_name',
         'CREATE INDEX IF NOT EXISTS ix_equity_trade_natures_name ON equity_trade_natures(name)'),
        ('ix_equity_trade_natures_display_order',
         'CREATE INDEX IF NOT EXISTS ix_equity_trade_natures_display_order ON equity_trade_natures(display_order)'),
        ('ix_equity_trade_natures_is_active',
         'CREATE INDEX IF NOT EXISTS ix_equity_trade_natures_is_active ON equity_trade_natures(is_active)'),

        # equity_watchlist_items
        ('ix_equity_watchlist_items_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_user_id ON equity_watchlist_items(user_id)'),
        ('ix_equity_watchlist_items_symbol',
         'CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_symbol ON equity_watchlist_items(symbol)'),
        ('ix_equity_watchlist_items_exchange',
         'CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_exchange ON equity_watchlist_items(exchange)'),
        ('ix_equity_watchlist_items_trade_nature_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_trade_nature_id ON equity_watchlist_items(trade_nature_id)'),
        ('ix_equity_watchlist_items_price_alert_enabled',
         'CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_price_alert_enabled ON equity_watchlist_items(price_alert_enabled)'),

        # equity_orders
        ('ix_equity_orders_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_user_id ON equity_orders(user_id)'),
        ('ix_equity_orders_symbol',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_symbol ON equity_orders(symbol)'),
        ('ix_equity_orders_exchange',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_exchange ON equity_orders(exchange)'),
        ('ix_equity_orders_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_status ON equity_orders(status)'),
        ('ix_equity_orders_placed_at',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_placed_at ON equity_orders(placed_at)'),
        ('ix_equity_orders_user_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_user_status ON equity_orders(user_id, status)'),
        ('ix_equity_orders_user_placed_at',
         'CREATE INDEX IF NOT EXISTS ix_equity_orders_user_placed_at ON equity_orders(user_id, placed_at)'),

        # equity_order_splits
        ('ix_equity_order_splits_equity_order_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_equity_order_id ON equity_order_splits(equity_order_id)'),
        ('ix_equity_order_splits_account_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_account_id ON equity_order_splits(account_id)'),
        ('ix_equity_order_splits_broker_order_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_broker_order_id ON equity_order_splits(broker_order_id)'),
        ('ix_equity_order_splits_fill_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_fill_status ON equity_order_splits(fill_status)'),
        ('ix_equity_order_splits_account_status',
         'CREATE INDEX IF NOT EXISTS ix_equity_order_splits_account_status ON equity_order_splits(account_id, fill_status)'),

        # equity_trades
        ('ix_equity_trades_split_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_trades_split_id ON equity_trades(split_id)'),
        ('ix_equity_trades_exchange',
         'CREATE INDEX IF NOT EXISTS ix_equity_trades_exchange ON equity_trades(exchange)'),
        ('ix_equity_trades_executed_at',
         'CREATE INDEX IF NOT EXISTS ix_equity_trades_executed_at ON equity_trades(executed_at)'),
        ('ix_equity_trades_broker_trade_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_trades_broker_trade_id ON equity_trades(broker_trade_id)'),

        # equity_holdings
        ('ix_equity_holdings_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_user_id ON equity_holdings(user_id)'),
        ('ix_equity_holdings_account_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_account_id ON equity_holdings(account_id)'),
        ('ix_equity_holdings_symbol',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_symbol ON equity_holdings(symbol)'),
        ('ix_equity_holdings_exchange',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_exchange ON equity_holdings(exchange)'),
        ('ix_equity_holdings_trade_nature_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_trade_nature_id ON equity_holdings(trade_nature_id)'),
        ('ix_equity_holdings_user_symbol',
         'CREATE INDEX IF NOT EXISTS ix_equity_holdings_user_symbol ON equity_holdings(user_id, symbol)'),

        # equity_brokerage_rates
        ('ix_equity_brokerage_rates_user_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_user_id ON equity_brokerage_rates(user_id)'),
        ('ix_equity_brokerage_rates_account_id',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_account_id ON equity_brokerage_rates(account_id)'),
        ('ix_equity_brokerage_rates_broker_name',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_broker_name ON equity_brokerage_rates(broker_name)'),
        ('ix_equity_brokerage_rates_effective_from',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_effective_from ON equity_brokerage_rates(effective_from)'),
        ('ix_equity_brokerage_rates_is_active',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_is_active ON equity_brokerage_rates(is_active)'),
        ('ix_equity_brokerage_rates_account_effective',
         'CREATE INDEX IF NOT EXISTS ix_equity_brokerage_rates_account_effective ON equity_brokerage_rates(account_id, effective_from)'),
    ]

    created_count = 0
    skipped_count = 0

    for index_name, create_sql in indexes:
        try:
            db.session.execute(text(create_sql))
            created_count += 1
        except Exception as e:
            error_msg = str(e).lower()
            if 'already exists' in error_msg or 'duplicate' in error_msg:
                skipped_count += 1
            else:
                print(f"  Failed to create {index_name}: {e}")

    db.session.commit()
    print(f"\nEquity indexes: {created_count} created or already present, {skipped_count} skipped")

    # Seed the default trade natures for every existing user (idempotent)
    try:
        result = db.session.execute(text("SELECT id FROM users"))
        user_ids = [row[0] for row in result.fetchall()]
        for user_id in user_ids:
            EquityTradeNature.get_or_create_defaults(user_id)
        print(f"Seeded default trade natures for {len(user_ids)} user(s)")
    except Exception as e:
        db.session.rollback()
        print(f"  Skipped trade nature seeding: {e}")


def downgrade(db):
    """Drop the equity module tables (children before parents)"""

    for table_name in reversed(EQUITY_TABLES):
        try:
            db.session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            print(f"  Dropped table {table_name}")
        except Exception as e:
            print(f"  Failed to drop {table_name}: {e}")

    db.session.commit()
