"""
Exit Order Manager
Handles exit order placement with duplicate prevention and broker verification.

Key Features:
- Prevents duplicate exit orders through status tracking
- Verifies with broker before retrying failed exits
- 10-second cooldown between exit attempts
- Checks tradebook/orderbook for existing exit orders

This module solves the duplicate exit order problem by:
1. Keeping status as 'exit_pending' even on failures (not reverting to 'entered')
2. Waiting 10 seconds before allowing retry
3. Verifying with broker if exit order already exists before placing new one
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from sqlalchemy import update as sa_update, func
from app import db
from app.models import StrategyExecution, TradingAccount
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# Configuration
EXIT_RETRY_DELAY_SECONDS = 10  # Wait 10 seconds before allowing retry
MAX_EXIT_ATTEMPTS = 5  # Maximum number of exit attempts before marking as failed


def atomic_claim_exit(execution_id: int, exit_reason: str) -> bool:
    """
    Atomically claim an execution for exit processing.

    Uses a single SQL UPDATE with WHERE clause to ensure only ONE thread/service
    can transition an execution from 'entered' to 'exit_pending'.

    WHY THIS EXISTS:
    The previous approach used two separate operations:
      1. can_attempt_exit() - READ status
      2. mark_exit_pending() - WRITE status
    This created a TOCTOU (Time-of-Check to Time-of-Use) race condition where
    multiple threads could read status='entered' simultaneously and all proceed
    to place exit orders, causing DUPLICATE ORDERS.

    HOW IT WORKS:
    - SQLite serializes ALL writes at the database level (one writer at a time)
    - PostgreSQL uses row-level locking in UPDATE
    - The WHERE clause ensures only rows matching ALL conditions are updated
    - If Thread A updates first, Thread B's WHERE won't match (status changed)
    - Thread B gets rowcount=0 and skips - NO DUPLICATE

    Args:
        execution_id: ID of the StrategyExecution to claim
        exit_reason: Reason for the exit (e.g., 'max_loss', 'supertrend_breakout')

    Returns:
        True if this caller successfully claimed the execution for exit.
        False if another thread already claimed it or conditions not met.
    """
    now = datetime.utcnow()

    try:
        result = db.session.execute(
            sa_update(StrategyExecution)
            .where(StrategyExecution.id == execution_id)
            .where(StrategyExecution.status == 'entered')
            .where(StrategyExecution.exit_order_id.is_(None))
            .values(
                status='exit_pending',
                exit_reason=exit_reason,
                exit_pending_since=now,
                exit_attempt_count=func.coalesce(StrategyExecution.exit_attempt_count, 0) + 1,
                exit_retry_after=now + timedelta(seconds=EXIT_RETRY_DELAY_SECONDS)
            )
        )
        db.session.commit()

        claimed = result.rowcount > 0
        if claimed:
            logger.info(f"[ATOMIC_CLAIM] Execution {execution_id} claimed for exit: {exit_reason}")
        else:
            logger.info(f"[ATOMIC_CLAIM] Execution {execution_id} NOT claimed "
                       f"(already claimed or not in 'entered' state)")

        return claimed

    except Exception as e:
        logger.error(f"[ATOMIC_CLAIM] Error claiming execution {execution_id}: {e}")
        db.session.rollback()
        return False


def atomic_claim_exit_retry(execution_id: int, exit_reason: str) -> bool:
    """
    Atomically claim an exit_pending execution for retry.

    Only succeeds if:
    - Status is 'exit_pending' (previous attempt failed)
    - No exit_order_id (no successful order placed)
    - Retry timer has expired (cooldown passed)
    - Max attempts not exceeded

    Args:
        execution_id: ID of the StrategyExecution to retry
        exit_reason: Reason for the retry

    Returns:
        True if this caller successfully claimed the retry.
    """
    now = datetime.utcnow()

    try:
        result = db.session.execute(
            sa_update(StrategyExecution)
            .where(StrategyExecution.id == execution_id)
            .where(StrategyExecution.status == 'exit_pending')
            .where(StrategyExecution.exit_order_id.is_(None))
            .where(StrategyExecution.exit_retry_after <= now)
            .where(StrategyExecution.exit_attempt_count < MAX_EXIT_ATTEMPTS)
            .values(
                exit_attempt_count=func.coalesce(StrategyExecution.exit_attempt_count, 0) + 1,
                exit_retry_after=now + timedelta(seconds=EXIT_RETRY_DELAY_SECONDS),
                exit_reason=exit_reason
            )
        )
        db.session.commit()

        claimed = result.rowcount > 0
        if claimed:
            logger.info(f"[ATOMIC_RETRY] Execution {execution_id} claimed for retry: {exit_reason}")
        else:
            logger.info(f"[ATOMIC_RETRY] Execution {execution_id} NOT claimed for retry "
                       f"(timer not expired, max attempts, or already has exit_order_id)")

        return claimed

    except Exception as e:
        logger.error(f"[ATOMIC_RETRY] Error claiming retry for execution {execution_id}: {e}")
        db.session.rollback()
        return False


def verify_exit_order_at_broker(
    client: ExtendedOpenAlgoAPI,
    execution: StrategyExecution,
    strategy_name: str
) -> Dict:
    """
    Verify if an exit order exists at the broker for this position.

    Checks:
    1. Orderbook - for pending/open exit orders
    2. Tradebook - for completed exit trades
    3. Positionbook - for actual position quantity

    Returns:
        Dict with:
        - has_exit_order: bool - True if exit order found
        - order_status: str - 'complete', 'open', 'cancelled', 'rejected', None
        - order_id: str - Order ID if found
        - position_quantity: int - Current position quantity at broker
        - can_place_exit: bool - True if safe to place new exit order
        - message: str - Human-readable status
    """
    result = {
        'has_exit_order': False,
        'order_status': None,
        'order_id': None,
        'position_quantity': 0,
        'can_place_exit': False,
        'message': ''
    }

    try:
        symbol = execution.symbol
        exchange = execution.exchange

        # 1. Check positionbook for actual position
        position_response = client.positionbook()
        if position_response.get('status') == 'success':
            positions = position_response.get('data', [])
            for pos in positions:
                if pos.get('symbol') == symbol:
                    result['position_quantity'] = abs(int(pos.get('quantity', 0)))
                    break

        # If no position exists at broker, exit is already done
        if result['position_quantity'] == 0:
            result['has_exit_order'] = True
            result['order_status'] = 'complete'
            result['message'] = 'Position already closed at broker (quantity=0)'
            result['can_place_exit'] = False
            return result

        # 2. Check orderbook for pending exit orders
        orderbook_response = client.orderbook()
        if orderbook_response.get('status') == 'success':
            orders = orderbook_response.get('data', [])

            # Determine exit action (opposite of entry)
            entry_action = execution.leg.action.upper() if execution.leg else 'BUY'
            exit_action = 'SELL' if entry_action == 'BUY' else 'BUY'

            for order in orders:
                order_symbol = order.get('symbol', '')
                order_action = order.get('transaction_type', order.get('transactiontype', '')).upper()
                order_status = order.get('order_status', order.get('status', '')).lower()
                order_id = str(order.get('orderid', order.get('order_id', '')))

                # Check if this is an exit order for our symbol
                if order_symbol == symbol and order_action == exit_action:
                    result['has_exit_order'] = True
                    result['order_status'] = order_status
                    result['order_id'] = order_id

                    if order_status == 'complete':
                        result['message'] = f'Exit order {order_id} already completed'
                        result['can_place_exit'] = False
                    elif order_status in ['open', 'pending', 'trigger_pending']:
                        result['message'] = f'Exit order {order_id} is {order_status}'
                        result['can_place_exit'] = False
                    elif order_status in ['cancelled', 'rejected']:
                        result['message'] = f'Previous exit order {order_id} was {order_status}'
                        result['can_place_exit'] = True
                    else:
                        result['message'] = f'Exit order {order_id} has status: {order_status}'
                        result['can_place_exit'] = False

                    return result

        # 3. Check tradebook for completed exit trades
        tradebook_response = client.tradebook()
        if tradebook_response.get('status') == 'success':
            trades = tradebook_response.get('data', [])

            entry_action = execution.leg.action.upper() if execution.leg else 'BUY'
            exit_action = 'SELL' if entry_action == 'BUY' else 'BUY'

            for trade in trades:
                trade_symbol = trade.get('symbol', '')
                trade_action = trade.get('transaction_type', trade.get('transactiontype', '')).upper()
                trade_id = str(trade.get('orderid', trade.get('order_id', '')))

                if trade_symbol == symbol and trade_action == exit_action:
                    # Found exit trade - check if it matches our expected quantity
                    trade_qty = abs(int(trade.get('quantity', trade.get('filled_quantity', 0))))
                    if trade_qty >= execution.quantity:
                        result['has_exit_order'] = True
                        result['order_status'] = 'complete'
                        result['order_id'] = trade_id
                        result['message'] = f'Exit trade {trade_id} found in tradebook'
                        result['can_place_exit'] = False
                        return result

        # No exit order found - safe to place new one
        result['can_place_exit'] = True
        result['message'] = f'No exit order found for {symbol}, position quantity={result["position_quantity"]}'

    except Exception as e:
        logger.error(f"[EXIT_VERIFY] Error verifying exit order for {execution.symbol}: {e}")
        result['message'] = f'Verification error: {str(e)}'
        # On error, be cautious - don't allow placing if we can't verify
        result['can_place_exit'] = False

    return result


def can_attempt_exit(execution: StrategyExecution) -> Tuple[bool, str]:
    """
    Check if we can attempt to place an exit order for this execution.

    Rules:
    1. If status is 'entered' with no exit_order_id -> can exit
    2. If status is 'exit_pending' and exit_retry_after has passed -> need broker verification
    3. If status is 'exit_pending' and exit_retry_after not passed -> wait
    4. If status is 'exited' or has exit_order_id -> already done
    5. If max attempts exceeded -> need manual intervention

    Returns:
        Tuple of (can_attempt: bool, reason: str)
    """
    now = datetime.utcnow()

    # Already exited
    if execution.status == 'exited':
        return False, 'Position already exited'

    # Has exit order ID - check if it succeeded
    if execution.exit_order_id:
        return False, f'Exit order {execution.exit_order_id} already placed'

    # Check max attempts
    attempt_count = execution.exit_attempt_count or 0
    if attempt_count >= MAX_EXIT_ATTEMPTS:
        return False, f'Max exit attempts ({MAX_EXIT_ATTEMPTS}) exceeded - needs manual intervention'

    # Fresh entry - no exit attempted yet
    if execution.status == 'entered' and not execution.exit_pending_since:
        return True, 'Fresh position - can place exit'

    # Exit pending - check retry timer
    if execution.status == 'exit_pending':
        if execution.exit_retry_after:
            if now < execution.exit_retry_after:
                wait_seconds = (execution.exit_retry_after - now).total_seconds()
                return False, f'Retry not allowed yet - wait {wait_seconds:.0f}s'

        # Retry timer expired - need broker verification before proceeding
        return True, 'Retry timer expired - needs broker verification'

    # Status is 'entered' but has exit_pending_since - shouldn't happen, but allow
    if execution.status == 'entered':
        return True, 'Status is entered - can place exit'

    return False, f'Unknown status: {execution.status}'


def mark_exit_pending(
    execution: StrategyExecution,
    exit_reason: str,
    increment_attempt: bool = True
) -> None:
    """
    Mark an execution as exit pending with retry tracking.

    This is called BEFORE attempting to place the exit order.
    Sets status to 'exit_pending' and records the attempt.
    """
    now = datetime.utcnow()

    execution.status = 'exit_pending'
    execution.exit_reason = exit_reason

    # Set pending since on first attempt only
    if not execution.exit_pending_since:
        execution.exit_pending_since = now

    # Increment attempt count
    if increment_attempt:
        execution.exit_attempt_count = (execution.exit_attempt_count or 0) + 1

    # Set retry timer - can't retry before this timestamp
    execution.exit_retry_after = now + timedelta(seconds=EXIT_RETRY_DELAY_SECONDS)

    # Don't set exit_time until order is confirmed
    # execution.exit_time = now  # Removed - only set when order confirmed

    db.session.commit()

    logger.info(f"[EXIT_PENDING] Execution {execution.id} ({execution.symbol}) marked exit_pending, "
               f"attempt #{execution.exit_attempt_count}, retry after {execution.exit_retry_after}")


def mark_exit_success(
    execution: StrategyExecution,
    order_id: str
) -> None:
    """
    Mark an exit order as successfully placed.

    Note: Status stays as 'exit_pending' until order_status_poller
    confirms the order is complete and updates to 'exited'.
    """
    now = datetime.utcnow()

    execution.exit_order_id = order_id
    execution.broker_order_status = 'open'
    execution.exit_time = now
    execution.exit_broker_verified = False  # Will be verified by poller

    db.session.commit()

    logger.info(f"[EXIT_SUCCESS] Execution {execution.id} ({execution.symbol}) exit order placed: {order_id}")


def mark_exit_failed(
    execution: StrategyExecution,
    error_message: str,
    needs_broker_verification: bool = True
) -> None:
    """
    Mark an exit attempt as failed.

    IMPORTANT: Status stays as 'exit_pending' (NOT reverted to 'entered').
    This prevents duplicate exit orders from being placed on the next risk check.

    The retry mechanism will check exit_retry_after and verify with broker
    before attempting another exit.

    Args:
        execution: The execution to update
        error_message: Error message from the failed attempt
        needs_broker_verification: If True, next retry must verify with broker first
    """
    now = datetime.utcnow()

    # CRITICAL: Keep status as 'exit_pending' - DO NOT revert to 'entered'
    # This is the key fix for the duplicate exit order problem
    execution.status = 'exit_pending'

    # Update error message
    execution.error_message = error_message[:500] if error_message else None

    # Set retry timer
    execution.exit_retry_after = now + timedelta(seconds=EXIT_RETRY_DELAY_SECONDS)

    # Mark that broker verification is needed before next attempt
    execution.exit_broker_verified = not needs_broker_verification

    db.session.commit()

    logger.warning(f"[EXIT_FAILED] Execution {execution.id} ({execution.symbol}) exit failed: {error_message}. "
                  f"Retry allowed after {execution.exit_retry_after}")


def mark_exit_confirmed(
    execution: StrategyExecution,
    broker_status: str,
    order_id: str = None
) -> None:
    """
    Mark an exit as confirmed by broker verification.

    Called when broker verification shows the exit order exists and is complete.
    This updates the execution to 'exited' status even if order_status_poller
    hasn't processed it yet.
    """
    now = datetime.utcnow()

    execution.status = 'exited'
    execution.broker_order_status = broker_status
    if order_id:
        execution.exit_order_id = order_id
    if not execution.exit_time:
        execution.exit_time = now
    execution.exit_broker_verified = True

    db.session.commit()

    logger.info(f"[EXIT_CONFIRMED] Execution {execution.id} ({execution.symbol}) exit confirmed via broker verification")


def get_pending_exit_retries(strategy_id: int) -> list:
    """
    Get executions that are ready for exit retry.

    Returns executions where:
    - status = 'exit_pending'
    - exit_order_id IS NULL (no successful order placed)
    - exit_retry_after has passed (cooldown expired)
    - exit_attempt_count < MAX_EXIT_ATTEMPTS
    """
    now = datetime.utcnow()

    executions = StrategyExecution.query.filter(
        StrategyExecution.strategy_id == strategy_id,
        StrategyExecution.status == 'exit_pending',
        StrategyExecution.exit_order_id.is_(None),
        StrategyExecution.exit_retry_after <= now,
        StrategyExecution.exit_attempt_count < MAX_EXIT_ATTEMPTS
    ).all()

    return executions
