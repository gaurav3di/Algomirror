"""
Background Service for Supertrend-based Exit Monitoring
Monitors strategies with Supertrend exit enabled and triggers exits on signal
"""

import threading
import logging
from datetime import datetime, time, timedelta
import time as time_module
from typing import Dict, Any, List
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from app.models import Strategy, StrategyExecution, TradingAccount
from app.utils.supertrend import calculate_supertrend
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class SupertrendExitService:
    """
    Background service that monitors strategies with Supertrend exit enabled
    and triggers parallel exits when breakout/breakdown occurs
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
        self.is_running = False
        self.monitoring_strategies = {}  # strategy_id -> last_check_time
        self._initialized = True

        logger.info("Supertrend Exit Service initialized")

    def start_service(self):
        """Start the background service"""
        if not self.is_running:
            self.scheduler.start()
            self.is_running = True

            # Schedule monitoring every minute (will check strategies and monitor at appropriate intervals)
            self.scheduler.add_job(
                func=self.monitor_strategies,
                trigger='interval',
                minutes=1,
                id='supertrend_monitor',
                replace_existing=True
            )

            logger.info("Supertrend Exit Service started - monitoring every minute")

    def stop_service(self):
        """Stop the background service"""
        if self.is_running:
            self.scheduler.shutdown(wait=False)
            self.is_running = False
            logger.info("Supertrend Exit Service stopped")

    def monitor_strategies(self):
        """
        Monitor all strategies with Supertrend exit enabled
        Check each strategy based on its configured timeframe
        """
        try:
            from app import create_app

            # Create Flask app context
            app = create_app()
            with app.app_context():
                from app import db

                # Get all strategies with Supertrend exit enabled and not yet triggered
                strategies = Strategy.query.filter_by(
                    supertrend_exit_enabled=True,
                    supertrend_exit_triggered=False,
                    is_active=True
                ).all()

                if not strategies:
                    return

                logger.info(f"Monitoring {len(strategies)} strategies with Supertrend exit enabled")

                for strategy in strategies:
                    try:
                        # Check if we should monitor this strategy based on timeframe
                        if self.should_check_strategy(strategy):
                            logger.info(f"Checking Supertrend for strategy {strategy.id} ({strategy.name})")
                            self.check_supertrend_exit(strategy, app)
                    except Exception as e:
                        logger.error(f"Error monitoring strategy {strategy.id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in monitor_strategies: {e}", exc_info=True)

    def should_check_strategy(self, strategy: Strategy) -> bool:
        """
        Determine if enough time has passed to check this strategy
        based on its timeframe (to check on candle close)
        """
        now = datetime.now(pytz.timezone('Asia/Kolkata'))

        # Get last check time
        last_check = self.monitoring_strategies.get(strategy.id)

        # Map timeframe to minutes
        timeframe_minutes = {
            '1m': 1,
            '5m': 5,
            '15m': 15
        }

        interval_minutes = timeframe_minutes.get(strategy.supertrend_timeframe, 5)

        # Check on candle close (when current minute aligns with timeframe)
        # For example, 5m candle closes at :00, :05, :10, :15, etc.
        current_minute = now.minute

        # Check if current minute aligns with the timeframe
        # For 1m: every minute
        # For 5m: at :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
        # For 15m: at :00, :15, :30, :45
        is_candle_close = (current_minute % interval_minutes) == 0

        if not is_candle_close:
            return False

        # If we haven't checked yet, or if enough time has passed since last check
        if last_check is None:
            return True

        time_since_last_check = (now - last_check).total_seconds() / 60

        # Only check if at least the timeframe interval has passed
        return time_since_last_check >= interval_minutes

    def check_supertrend_exit(self, strategy: Strategy, app):
        """
        Check if Supertrend exit condition is met for a strategy
        """
        with app.app_context():
            from app import db

            try:
                # Update last check time
                self.monitoring_strategies[strategy.id] = datetime.now(pytz.timezone('Asia/Kolkata'))

                # Check if strategy has open positions
                open_positions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).all()

                # Filter out rejected/cancelled
                open_positions = [
                    pos for pos in open_positions
                    if not (hasattr(pos, 'broker_order_status') and
                           pos.broker_order_status in ['rejected', 'cancelled'])
                ]

                if not open_positions:
                    logger.info(f"Strategy {strategy.id} has no open positions, skipping")
                    return

                # Fetch combined spread data
                spread_data = self.fetch_combined_spread_data(strategy)

                if spread_data is None or len(spread_data) < strategy.supertrend_period + 5:
                    logger.warning(f"Insufficient data for Supertrend calculation for strategy {strategy.id}")
                    return

                # Calculate Supertrend
                high = spread_data['high'].values
                low = spread_data['low'].values
                close = spread_data['close'].values

                trend, direction, long, short = calculate_supertrend(
                    high, low, close,
                    period=strategy.supertrend_period,
                    multiplier=strategy.supertrend_multiplier
                )

                # Get latest values (last candle)
                latest_close = close[-1]
                latest_supertrend = trend[-1]
                latest_direction = direction[-1]

                # Check for exit signal based on type
                should_exit = False
                exit_reason = None

                if strategy.supertrend_exit_type == 'breakout':
                    # Upper breakout: close above Supertrend (direction = 1)
                    if latest_direction > 0:
                        should_exit = True
                        exit_reason = f'supertrend_breakout (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'
                        logger.info(f"Strategy {strategy.id}: Supertrend BREAKOUT detected")

                elif strategy.supertrend_exit_type == 'breakdown':
                    # Downside breakdown: close below Supertrend (direction = -1)
                    if latest_direction < 0:
                        should_exit = True
                        exit_reason = f'supertrend_breakdown (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'
                        logger.info(f"Strategy {strategy.id}: Supertrend BREAKDOWN detected")

                if should_exit:
                    logger.info(f"Triggering parallel exit for strategy {strategy.id} - Reason: {exit_reason}")
                    self.trigger_parallel_exit(strategy, exit_reason, app)
                else:
                    logger.debug(f"Strategy {strategy.id}: No exit signal (Direction: {latest_direction}, Type: {strategy.supertrend_exit_type})")

            except Exception as e:
                logger.error(f"Error checking Supertrend exit for strategy {strategy.id}: {e}", exc_info=True)

    def fetch_combined_spread_data(self, strategy: Strategy) -> pd.DataFrame:
        """
        Fetch real-time combined spread data for strategy legs
        Similar to tradingview routes but optimized for exit monitoring
        """
        try:
            from app.models import StrategyLeg, StrategyExecution

            # Get strategy legs
            legs = StrategyLeg.query.filter_by(strategy_id=strategy.id).all()

            if not legs:
                logger.error(f"Strategy {strategy.id} has no legs")
                return None

            # Get a trading account
            account_ids = strategy.selected_accounts or []
            account = None

            if account_ids:
                account = TradingAccount.query.filter_by(
                    id=account_ids[0],
                    is_active=True
                ).first()

            if not account:
                account = TradingAccount.query.filter_by(
                    user_id=strategy.user_id,
                    is_active=True
                ).first()

            if not account:
                logger.error(f"No active trading account for strategy {strategy.id}")
                return None

            # Initialize OpenAlgo client
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            # Get actual placed symbols from executions
            executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id
            ).filter(
                StrategyExecution.symbol.isnot(None)
            ).all()

            # Map leg_id to actual symbol
            leg_symbols = {}
            for execution in executions:
                if execution.leg_id not in leg_symbols:
                    leg_symbols[execution.leg_id] = {
                        'symbol': execution.symbol,
                        'exchange': execution.exchange or 'NSE'
                    }

            # Fetch historical data for each leg
            end_date = datetime.now()
            start_date = end_date - timedelta(days=2)  # 2 days should be enough for intraday

            start_date_str = start_date.strftime('%Y-%m-%d')
            end_date_str = end_date.strftime('%Y-%m-%d')

            leg_data_dict = {}

            for leg in legs:
                try:
                    # Use actual placed symbol if available
                    if leg.id in leg_symbols:
                        symbol = leg_symbols[leg.id]['symbol']
                        exchange = leg_symbols[leg.id]['exchange']
                    else:
                        symbol = leg.instrument
                        exchange = 'NSE'

                    # Fetch historical data
                    response = client.history(
                        symbol=symbol,
                        exchange=exchange,
                        interval=strategy.supertrend_timeframe,
                        start_date=start_date_str,
                        end_date=end_date_str
                    )

                    if isinstance(response, pd.DataFrame) and not response.empty:
                        required_cols = ['open', 'high', 'low', 'close']
                        if all(col in response.columns for col in required_cols):
                            leg_data_dict[f"{leg.leg_number}_{symbol}"] = response
                            logger.debug(f"Fetched {len(response)} bars for leg {leg.leg_number} ({symbol})")
                    elif isinstance(response, dict):
                        # Try fallback to base instrument
                        logger.warning(f"Option symbol {symbol} failed, trying {leg.instrument}")
                        fallback_response = client.history(
                            symbol=leg.instrument,
                            exchange='NSE',
                            interval=strategy.supertrend_timeframe,
                            start_date=start_date_str,
                            end_date=end_date_str
                        )
                        if isinstance(fallback_response, pd.DataFrame) and not fallback_response.empty:
                            leg_data_dict[f"{leg.leg_number}_{leg.instrument}"] = fallback_response

                except Exception as e:
                    logger.error(f"Error fetching data for leg {leg.leg_number}: {e}")
                    continue

            if not leg_data_dict:
                logger.error(f"No data fetched for strategy {strategy.id}")
                return None

            # Combine OHLC data into spread
            combined_df = None
            for leg_name, df in leg_data_dict.items():
                if combined_df is None:
                    combined_df = df[['open', 'high', 'low', 'close']].copy()
                else:
                    combined_df['open'] = combined_df['open'].add(df['open'], fill_value=0)
                    combined_df['high'] = combined_df['high'].add(df['high'], fill_value=0)
                    combined_df['low'] = combined_df['low'].add(df['low'], fill_value=0)
                    combined_df['close'] = combined_df['close'].add(df['close'], fill_value=0)

            logger.info(f"Combined spread data: {len(combined_df)} bars for strategy {strategy.id}")
            return combined_df

        except Exception as e:
            logger.error(f"Error fetching combined spread data: {e}", exc_info=True)
            return None

    def trigger_parallel_exit(self, strategy: Strategy, exit_reason: str, app):
        """
        Trigger parallel exit for all open positions in the strategy
        Reuses the close_all_positions logic from strategy routes
        """
        with app.app_context():
            from app import db
            import threading
            from app.utils.openalgo_client import ExtendedOpenAlgoAPI
            from datetime import datetime

            try:
                logger.info(f"[SUPERTREND EXIT] Initiating parallel exit for strategy {strategy.id}")

                # Mark strategy as triggered to prevent re-triggering
                strategy.supertrend_exit_triggered = True
                db.session.commit()

                # Get all open positions
                open_positions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).all()

                # Filter out rejected/cancelled
                open_positions = [
                    pos for pos in open_positions
                    if not (hasattr(pos, 'broker_order_status') and
                           pos.broker_order_status in ['rejected', 'cancelled'])
                ]

                if not open_positions:
                    logger.warning(f"No open positions found for strategy {strategy.id}")
                    return

                logger.info(f"[SUPERTREND EXIT] Closing {len(open_positions)} positions in parallel")

                # Thread-safe results collection
                results = []
                results_lock = threading.Lock()

                def close_position_worker(position, strategy_name, product_type, thread_index, user_id):
                    """Worker to close a single position"""
                    import time

                    # Staggered delay to prevent race conditions
                    delay = thread_index * 0.3
                    if delay > 0:
                        time.sleep(delay)

                    # Create Flask app context for this thread
                    app_ctx = create_app()
                    with app_ctx.app_context():
                        try:
                            # Reverse action for closing
                            close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

                            client = ExtendedOpenAlgoAPI(
                                api_key=position.account.get_api_key(),
                                host=position.account.host_url
                            )

                            # Place close order with freeze awareness
                            from app.utils.freeze_quantity_handler import place_order_with_freeze_check

                            response = place_order_with_freeze_check(
                                client=client,
                                user_id=user_id,
                                strategy=strategy_name,
                                symbol=position.symbol,
                                exchange=position.exchange,
                                action=close_action,
                                quantity=position.quantity,
                                price_type='MARKET',
                                product=product_type or 'MIS'
                            )

                            if response and response.get('status') == 'success':
                                # Update position
                                position_to_update = StrategyExecution.query.get(position.id)
                                if position_to_update:
                                    position_to_update.status = 'exited'
                                    position_to_update.exit_order_id = response.get('orderid')
                                    position_to_update.exit_time = datetime.utcnow()
                                    position_to_update.exit_reason = exit_reason
                                    position_to_update.broker_order_status = 'complete'

                                    # Get exit price
                                    try:
                                        quote = client.quotes(symbol=position.symbol, exchange=position.exchange)
                                        position_to_update.exit_price = float(quote.get('data', {}).get('ltp', 0))
                                    except:
                                        position_to_update.exit_price = position_to_update.entry_price

                                    # Calculate realized P&L
                                    if position.leg.action == 'BUY':
                                        position_to_update.realized_pnl = (position_to_update.exit_price - position_to_update.entry_price) * position_to_update.quantity
                                    else:
                                        position_to_update.realized_pnl = (position_to_update.entry_price - position_to_update.exit_price) * position_to_update.quantity

                                    db.session.commit()

                                    with results_lock:
                                        results.append({
                                            'symbol': position.symbol,
                                            'account': position.account.account_name,
                                            'status': 'success',
                                            'pnl': position_to_update.realized_pnl
                                        })
                            else:
                                error_msg = response.get('message', 'Unknown error') if response else 'No response'
                                with results_lock:
                                    results.append({
                                        'symbol': position.symbol,
                                        'account': position.account.account_name,
                                        'status': 'failed',
                                        'error': error_msg
                                    })

                        except Exception as e:
                            logger.error(f"Error closing position {position.symbol}: {e}")
                            with results_lock:
                                results.append({
                                    'symbol': position.symbol,
                                    'account': getattr(position.account, 'account_name', 'unknown'),
                                    'status': 'error',
                                    'error': str(e)
                                })

                # Import create_app here to avoid circular import
                from app import create_app

                # Create and start threads
                threads = []
                for idx, position in enumerate(open_positions):
                    thread = threading.Thread(
                        target=close_position_worker,
                        args=(position, strategy.name, strategy.product_order_type, idx, strategy.user_id),
                        name=f"SupertrendExit_{position.symbol}"
                    )
                    threads.append(thread)
                    thread.start()

                # Wait for all threads
                for thread in threads:
                    thread.join(timeout=30)

                # Log results
                successful = len([r for r in results if r.get('status') == 'success'])
                failed = len([r for r in results if r.get('status') in ['failed', 'error']])
                total_pnl = sum(r.get('pnl', 0) for r in results if r.get('status') == 'success')

                logger.info(f"[SUPERTREND EXIT] Completed: {successful}/{len(open_positions)} positions closed, Total P&L: {total_pnl:.2f}")

            except Exception as e:
                logger.error(f"Error triggering parallel exit: {e}", exc_info=True)


# Global service instance
supertrend_exit_service = SupertrendExitService()
