"""
Equity (CNC delivery) module routes.

Increment 1 is READ ONLY. There is no code path in this module that can place,
modify or cancel a broker order. The only broker calls made here are funds(),
holdings(), quotes() and multiquotes(), all of which are pure reads. The only
writes this module performs go to AlgoMirror's own tables: the equity fund
allocation, the brokerage rate versions and the cached broker payloads that
already exist on TradingAccount.

Every business formula lives in the two pure engines, app.utils.equity_ratio and
app.utils.equity_costs. This module converts ORM rows and broker payloads into
plain numbers, calls the engines and serialises the result. No PRD formula is
reimplemented here.

Screens served in increment 1: M1 Dashboard, M2 Accounts, M7 Holdings and
Settings. Watch List, Place Order, Order Book and Trade Book are not part of
this increment.

WHERE THE DATA COMES FROM, and why the screens are not three round trips deep:
    Prices are event driven. They are read from app.utils.equity_price_feed,
        which is backed by the single shared OpenAlgo WebSocket manager that
        already serves the F&O screens. A warm feed costs zero broker calls. The
        REST quote helpers below are a bounded fallback for symbols that have
        not ticked yet, never the primary path.
    Funds and holdings stay on REST, because OpenAlgo does not push them, but
        they are off the critical path in two ways: the two reads for one
        account are issued concurrently rather than one after the other, and a
        payload cached inside BROKER_CACHE_TTL_SECONDS is served without calling
        the broker at all.
"""

import csv
import io
import math
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import date, datetime

from flask import Response, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app import db
from app.equity import equity_bp
from app.models import (
    ActivityLog,
    EquityAccountAllocation,
    EquityBrokerageRate,
    EquityHolding,
    EquityOrder,
    EquityTradeNature,
    TradingAccount,
    EQUITY_EXIT_MODE_AUTO,
    EQUITY_EXIT_MODE_CONFIRM,
    EQUITY_ORDER_STATUS_PARTIAL,
    EQUITY_ORDER_STATUS_PENDING,
    EQUITY_PRODUCT_CNC,
    EQUITY_SIDE_SELL,
)
from app.utils.equity_costs import (
    BrokerageRates,
    estimate_costs,
    gross_pnl,
    net_pnl,
    turnover,
)
from app.utils.equity_price_feed import equity_price_feed
from app.utils.equity_ratio import (
    collateral_from_margin,
    compute_order_qty_ratios,
    invested_percent,
    percent_of,
    pledge_percent,
    signed_percent_of,
    stake_percent_for_view,
    stock_at_cost,
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit

# Short timeout for every interactive broker read. The equity screens poll, so a
# slow broker must fail fast and fall back to the cached payload rather than
# holding the page open.
BROKER_TIMEOUT_SECONDS = 8

# Upper bound on the fan-out pool. One worker per account, capped.
MAX_FETCH_WORKERS = 10

# Funds and holdings for one account are issued together instead of one after
# the other, so an account costs one broker round trip rather than two. Only the
# second read is handed to a thread, the first runs on the account's own fan-out
# worker, so a request adds at most MAX_FETCH_WORKERS extra threads and the
# inner concurrency cannot multiply with the outer fan-out.
MAX_INNER_WORKERS = 1

# Freshness window for the broker payloads cached on TradingAccount. Inside this
# window the screens serve last_funds_data and last_holdings_data and do not
# call the broker at all. 30 seconds is the house precedent: the F&O funds
# screen gates the same columns on the same window (app/trading/routes.py). The
# equity screens poll on that cadence, so the extra readers around one poll (a
# second tab, a manual refresh, the CSV export) cost nothing at the broker, and
# a poll only reaches the broker once the cache has actually aged out.
BROKER_CACHE_TTL_SECONDS = 30

# Upper bound on symbols sent to multiquotes in one call, and on the per-symbol
# quote fallback that runs when multiquotes is unavailable.
MAX_QUOTE_SYMBOLS = 100
MAX_QUOTE_FALLBACK_SYMBOLS = 25
MAX_QUOTE_FALLBACK_WORKERS = 5

# Wall clock ceiling for the whole REST quote fallback in one request. Prices
# come from the push feed, so the fallback only ever covers symbols that have
# not ticked yet, and it must never dominate the request: 25 symbols against an
# 8 second per call timeout with 5 workers can reach 40 seconds, which is longer
# than the browser's own abort. Every stage is therefore bounded by this
# deadline as well as by the symbol counts above.
MAX_QUOTE_FALLBACK_SECONDS = 6.0

# A broker call with less than this left in the fallback budget is not started.
MIN_QUOTE_CALL_SECONDS = 0.5

# Exit mode tags shown next to the stop loss and target on the Holdings screen.
EXIT_MODE_TAGS = {
    EQUITY_EXIT_MODE_AUTO: 'AE',
    EQUITY_EXIT_MODE_CONFIRM: 'CE',
}

# Broker payload key aliases. Different OpenAlgo broker adapters spell these
# differently, so each value is resolved from the first key that carries a
# number.
_AVG_COST_KEYS = ('average_price', 'avgprice', 'avg_price', 'averageprice')
_LTP_KEYS = ('ltp', 'last_price', 'lastprice')
_PNL_PCT_KEYS = ('pnlpercent', 'pnl_percent', 'pnlpercentage', 'pnl_percentage')
_PLEDGED_KEYS = ('collateralquantity', 'collateral_quantity', 'pledgedquantity', 'pledged_quantity')
# Fallback for a broker adapter that reports a combined available margin rather
# than a separate collateral figure. Collateral is then margin minus raw cash.
_AVAILABLE_MARGIN_KEYS = ('availablemargin', 'available_margin', 'netmargin', 'net_margin')
_PREV_CLOSE_KEYS = ('prev_close', 'previous_close', 'prevclose', 'previousclose', 'close')

# Illustrative rates from the approved Settings mockup. They are offered to the
# form as a prefill only and are NEVER used in a cost calculation: an account
# with no saved rate row is costed at zero and flagged as unconfigured, so a
# number the admin never entered can never end up in a P&L figure.
SUGGESTED_RATE_DEFAULTS = {
    'brokerage_per_order': 20.0,
    'stt_pct': 0.1,
    'exchange_txn_pct': 0.00297,
    'sebi_pct': 0.0001,
    'stamp_duty_pct': 0.015,
    'gst_pct': 18.0,
    'dp_amc_charge': 13.5,
}

# Rules panel copy for M2 Accounts. Served from here so the screen and the
# behaviour implemented in this module cannot drift apart.
ALLOCATION_RULES = [
    'Order Qty Ratio is derived: an account ratio is its equity fund allocation '
    'divided by the total allocation of all active accounts.',
    'Equity Fund Allocation is the investable corpus you set by hand. It is '
    'independent of Available Cash and of the F&O module.',
    'On insufficient funds the default is to skip that account and continue with '
    'the rest. Aborting the whole order instead is a configuration option.',
    'Quantity is rounded down to the nearest tradable lot per account. The '
    'leftover is shown and is not carried over to another account.',
    'Accounts are added and connected in the main Accounts screen. This screen '
    'only sets how much of each account is earmarked for equity.',
    'Allocation changes are future dated only. Past orders keep the ratio and '
    'cash balance recorded against them and are never recalculated.',
]

# Est. Costs formula restated for the Settings footer.
COST_FORMULA_NOTES = [
    'Est. Costs = brokerage + STT on turnover + exchange transaction charge on '
    'turnover + SEBI charge on turnover + stamp duty on BUY turnover only + GST '
    'at the configured percent of (brokerage + exchange transaction charge) + '
    'DP/AMC on SELL only, per scrip.',
    'Gross P&L = (LTP - Avg Cost) x Qty. Net P&L = Gross P&L - Est. Costs.',
    'Percentage fields are percent values, so 0.10 means 0.10 percent.',
    'Saving rates inserts a new effective-dated version. Changes apply to future '
    'calculations only and past cost figures stay reproducible.',
]


# ---------------------------------------------------------------------------
# Process local caches
#
# All three are small, bounded and safe to lose. A fresh process simply makes
# one more broker read than it strictly had to, which is the safe way to be
# wrong. Under more than one worker process each worker keeps its own copy, so
# the worst case is one extra read per worker, never a wrong number.
# ---------------------------------------------------------------------------

# When each account's holdings payload was last refreshed from the broker.
# TradingAccount carries a single cache timestamp column, last_data_update, and
# the F&O funds screen reads it as the age of last_funds_data, so a holdings
# only read must not advance it (see _refresh_account_cache). Holdings therefore
# keep their own timestamp here rather than in a new column.
_HOLDINGS_REFRESHED_AT = {}
# Funds gets its own stamp for the same reason holdings does: the shared
# TradingAccount.last_data_update column is also advanced by the trading and
# accounts blueprints after a POSITIONS or HOLDINGS read, so trusting it here
# would serve arbitrarily old cash as fresh and never mark it stale.
_FUNDS_REFRESHED_AT = {}

# Previous close for the current trading day, keyed (symbol, exchange). The push
# feed subscribes in LTP mode and carries no previous close, but the value does
# not move during the day, so one REST quote per symbol per day is enough to
# keep Today's P&L alive. A recorded 0.0 means the broker was asked and reported
# nothing, which is what stops the fallback asking again on every poll.
# A previous close the broker would not answer is retried after this long
# rather than being written off for the day.
PREV_CLOSE_RETRY_SECONDS = 300.0
_PREV_CLOSE = {}
_PREV_CLOSE_DAY = None

# The symbol set the equity screens last asked the price feed to hold, so
# symbols that are no longer held can be unsubscribed instead of accumulating
# against the feed's own ceiling.
_FEED_SYMBOLS = set()

# One lock for all three. Every critical section below is a dict or set
# operation on plain values, so a single lock is cheaper than three.
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Small conversion helpers
# ---------------------------------------------------------------------------

def _to_float(value, default=0.0):
    """Coerce a broker payload value (often a string) to a finite float."""
    if value is None or value == '':
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _to_int(value, default=0):
    """Coerce a value to a whole number, truncating toward zero."""
    number = _to_float(value, float(default))
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def _money(value):
    """Round a rupee figure for JSON output. Presentation only."""
    number = _to_float(value)
    return round(number, 2)


def _pct(value):
    """Round a percent figure for JSON output. Presentation only."""
    number = _to_float(value)
    return round(number, 2)


def _safe_divide(numerator, denominator):
    """
    Guarded division used only for display averages such as the blended average
    cost of a symbol held across several accounts. This is not one of the PRD
    formulas, those all live in the two engine modules.
    """
    denom = _to_float(denominator)
    if denom == 0.0:
        return 0.0
    return _to_float(numerator) / denom


def _iso(value):
    """Serialise a datetime or date, or None."""
    if value is None:
        return None
    return value.isoformat()


def _first_number(row, keys):
    """Read the first key in keys that carries a usable number."""
    for key in keys:
        if key in row:
            value = _to_float(row.get(key))
            if value:
                return value
    return 0.0


def _json_error(message, http_status=400):
    """Standard error envelope. The frontend always checks the status field."""
    return jsonify({'status': 'error', 'message': message}), http_status


# ---------------------------------------------------------------------------
# Cache freshness and the process local stores
# ---------------------------------------------------------------------------

def _cache_age_seconds(timestamp):
    """Age of a cache timestamp in seconds, or None when it was never written."""
    if timestamp is None:
        return None
    try:
        return (datetime.utcnow() - timestamp).total_seconds()
    except (TypeError, ValueError):
        return None


def _is_fresh(payload, timestamp):
    """
    True when a cached broker payload exists and is inside the freshness window.

    An empty payload is never fresh: there is nothing to serve from it, so the
    broker is called instead.
    """
    if not payload:
        return False
    age = _cache_age_seconds(timestamp)
    return age is not None and age < BROKER_CACHE_TTL_SECONDS


def _funds_refreshed_at(account_id):
    """When this account's funds payload was last read live, or None."""
    with _CACHE_LOCK:
        return _FUNDS_REFRESHED_AT.get(account_id)


def _mark_funds_refreshed(account_id, when):
    """Record a live funds read, which is what the funds freshness gate reads."""
    with _CACHE_LOCK:
        _FUNDS_REFRESHED_AT[account_id] = when


def _holdings_refreshed_at(account_id):
    """When this account's holdings payload was last read live, or None."""
    with _CACHE_LOCK:
        return _HOLDINGS_REFRESHED_AT.get(account_id)


def _mark_holdings_refreshed(account_id, when):
    """Record a live holdings read, which is what the freshness gate reads."""
    with _CACHE_LOCK:
        _HOLDINGS_REFRESHED_AT[account_id] = when


def _prev_close_cached(key):
    """
    Previous close remembered for today, 0.0 when it is not known.

    The store is reset when the calendar day rolls, so yesterday's close can
    never be reused as today's reference.
    """
    global _PREV_CLOSE_DAY
    today = date.today()
    with _CACHE_LOCK:
        if _PREV_CLOSE_DAY != today:
            _PREV_CLOSE.clear()
            _PREV_CLOSE_DAY = today
            return 0.0
        stored = _PREV_CLOSE.get(key)
        if isinstance(stored, tuple):
            # A retry marker, not a price.
            return 0.0
        return _to_float(stored)


def _prev_close_asked(key):
    """True when the REST fallback already asked for this symbol's close today."""
    with _CACHE_LOCK:
        stored = _PREV_CLOSE.get(key)
        if stored is None:
            return False
        if isinstance(stored, tuple):
            # Asked and unanswered. Counts as asked only until the
            # backoff expires, then the symbol is eligible again.
            return time.monotonic() < stored[1]
        return True


def _remember_prev_close(key, value):
    """
    Record a previous close the broker actually answered.

    A real close is remembered for the rest of the day, since it does not move
    and re-asking would cost a round trip per poll.

    A zero is NOT remembered as final. An unanswered close is usually transient,
    and treating one zero as permanent would silently disable Today's P&L for
    that symbol for the whole session. Zero is recorded as a retry-after stamp
    instead, so the question is asked again a little later rather than on every
    poll or never again.
    """
    global _PREV_CLOSE_DAY
    today = date.today()
    number = _to_float(value)
    with _CACHE_LOCK:
        if _PREV_CLOSE_DAY != today:
            _PREV_CLOSE.clear()
            _PREV_CLOSE_DAY = today
        if number > 0:
            _PREV_CLOSE[key] = number
        else:
            _PREV_CLOSE[key] = ('retry', time.monotonic() + PREV_CLOSE_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# Ownership scoped lookups
# ---------------------------------------------------------------------------

def _active_accounts():
    """Every active trading account owned by the current user."""
    return TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).order_by(TradingAccount.id).all()


def _owned_account(account_id):
    """One account, scoped by BOTH id and owner. Returns None when not owned."""
    return TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id
    ).first()


def _get_or_create_allocations(accounts):
    """
    Return account_id to EquityAccountAllocation for the given accounts,
    creating a zero row for any account that does not have one yet. Idempotent,
    and it commits once only when something was actually added.
    """
    rows = EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
    by_account = {row.account_id: row for row in rows}

    created = False
    for account in accounts:
        if account.id not in by_account:
            row = EquityAccountAllocation(
                account_id=account.id,
                user_id=current_user.id,
                equity_fund_allocation=0.0
            )
            db.session.add(row)
            by_account[account.id] = row
            created = True

    if created:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(f'Could not seed equity allocations: {exc}')
            rows = EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
            by_account = {row.account_id: row for row in rows}

    return {account.id: by_account.get(account.id) for account in accounts}


def _allocation_amounts(accounts, allocation_rows):
    """
    Plain account_id to rupee allocation mapping for the ACTIVE equity accounts,
    in account order. This is the input to the ratio engine and the denominator
    for stake percent, so an account excluded here is excluded from both.

    Active means the trading account is active (already filtered by the caller)
    and its allocation row has not been deactivated.
    """
    amounts = {}
    for account in accounts:
        row = allocation_rows.get(account.id)
        if row is not None and row.is_active is False:
            continue
        amounts[account.id] = _to_float(row.equity_fund_allocation) if row is not None else 0.0
    return amounts


def _trade_natures():
    """
    Active trade natures for the current user, seeding the defaults the first
    time. Trade Nature is admin configurable, the four seeds are only seeds.
    """
    natures = EquityTradeNature.query.filter_by(
        user_id=current_user.id
    ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()

    if not natures:
        EquityTradeNature.get_or_create_defaults(current_user.id)
        natures = EquityTradeNature.query.filter_by(
            user_id=current_user.id
        ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()

    return [nature for nature in natures if nature.is_active is not False]


def _brokerage_rates_by_account(account_ids, on_date=None):
    """
    Resolve the effective BrokerageRates for each account.

    An account with no rate row in effect is costed at zero and reported in the
    second return value, so the UI can say the rates are not configured yet
    rather than showing a silently wrong Net P&L.
    """
    rates = {}
    unconfigured = []
    for account_id in account_ids:
        row = EquityBrokerageRate.get_effective_rate(current_user.id, account_id, on_date)
        if row is None:
            rates[account_id] = BrokerageRates()
            unconfigured.append(account_id)
            continue
        rates[account_id] = BrokerageRates(
            brokerage_per_order=_to_float(row.brokerage_per_order),
            stt_pct=_to_float(row.stt_pct),
            exchange_txn_pct=_to_float(row.exchange_txn_pct),
            sebi_pct=_to_float(row.sebi_pct),
            stamp_duty_pct=_to_float(row.stamp_duty_pct),
            gst_pct=_to_float(row.gst_pct),
            dp_amc_charge=_to_float(row.dp_amc_charge),
        )
    return rates, unconfigured


# ---------------------------------------------------------------------------
# Broker fan-out. Read only: funds, holdings and quotes.
# ---------------------------------------------------------------------------

def _account_credentials(accounts):
    """
    Extract plain credential and cache tuples BEFORE any thread is spawned. No
    ORM object and no lazy load ever crosses a thread boundary in this module.

    The two freshness flags are resolved here as well, for the same reason: the
    fan-out decides whether to call the broker at all from plain values, without
    touching an ORM row from a worker thread.
    """
    creds = []
    for account in accounts:
        try:
            api_key = account.get_api_key()
        except Exception as exc:
            current_app.logger.error(f'Could not read API key for account {account.id}: {exc}')
            api_key = None

        cached_funds = account.last_funds_data if isinstance(account.last_funds_data, dict) else None
        cached_holdings = account.last_holdings_data if isinstance(account.last_holdings_data, dict) else None

        creds.append({
            'account_id': account.id,
            'api_key': api_key,
            'host_url': account.host_url,
            'cached_funds': dict(cached_funds) if cached_funds else None,
            'cached_holdings': dict(cached_holdings) if cached_holdings else None,
            'funds_fresh': _is_fresh(cached_funds, _funds_refreshed_at(account.id)),
            'holdings_fresh': _is_fresh(
                cached_holdings,
                _holdings_refreshed_at(account.id)
            ),
        })
    return creds


def _new_snapshot(account_id):
    """An empty snapshot. Nothing read, nothing cached, nothing stale."""
    return {
        'account_id': account_id,
        'funds': None,
        'holdings_data': None,
        'funds_live': False,
        'holdings_live': False,
        'from_cache': False,
        'is_stale': False,
        'error': None,
    }


def _read_one_broker_call(app, cred, name):
    """
    Make one read only broker call by name and return its raw response.

    Never raises: a transport failure comes back as the same error envelope the
    broker itself would return, so the caller has one shape to handle. Each call
    builds its own client, so two concurrent reads never share an HTTP session.
    """
    with app.app_context():
        try:
            client = ExtendedOpenAlgoAPI(
                api_key=cred['api_key'],
                host=cred['host_url'],
                timeout=BROKER_TIMEOUT_SECONDS
            )
            return getattr(client, name)()
        except Exception as exc:
            return {'status': 'error', 'message': str(exc)}


def _read_broker_calls(app, cred, names):
    """
    Issue the wanted reads for one account CONCURRENTLY and return name to
    response.

    The first read runs on the caller's own thread and every other read gets one
    thread from a pool of at most MAX_INNER_WORKERS, so an account costs one
    round trip instead of two while the request adds at most one thread per
    account. If a thread cannot be started the reads simply run in sequence,
    which is slower and still correct.
    """
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: _read_one_broker_call(app, cred, names[0])}

    inline, deferred = names[0], names[1:]

    executor = None
    futures = {}
    try:
        executor = ThreadPoolExecutor(max_workers=min(MAX_INNER_WORKERS, len(deferred)))
        futures = {
            name: executor.submit(_read_one_broker_call, app, cred, name)
            for name in deferred
        }
    except Exception as exc:
        futures = {}
        current_app.logger.warning(
            f'Equity concurrent broker read unavailable, falling back to sequential: {exc}',
            extra={'event': 'equity_inner_pool_unavailable'}
        )

    responses = {inline: _read_one_broker_call(app, cred, inline)}
    for name in deferred:
        future = futures.get(name)
        if future is None:
            responses[name] = _read_one_broker_call(app, cred, name)
            continue
        try:
            responses[name] = future.result()
        except Exception as exc:
            responses[name] = {'status': 'error', 'message': str(exc)}

    if executor is not None:
        executor.shutdown(wait=False)

    return responses


def _fetch_account_snapshot(app, cred, want_funds, want_holdings):
    """
    Read funds and holdings for one account. Never raises: a broker failure
    degrades to the cached payload and marks the account stale, so one bad
    account cannot break the page.

    want_funds and want_holdings are what this account still has to fetch. A
    side already covered by a fresh cache is filled in by _apply_fresh_cache
    after the read, and is not stale.
    """
    snapshot = _new_snapshot(cred['account_id'])

    with app.app_context():
        try:
            if not cred.get('api_key'):
                raise ValueError('API key is not available for this account')

            names = []
            if want_funds:
                names.append('funds')
            if want_holdings:
                names.append('holdings')
            responses = _read_broker_calls(app, cred, names)

            if want_funds:
                response = responses.get('funds')
                if isinstance(response, dict) and response.get('status') == 'success':
                    data = response.get('data')
                    snapshot['funds'] = data if isinstance(data, dict) else {}
                    snapshot['funds_live'] = True
                else:
                    snapshot['error'] = (response or {}).get('message') or 'Failed to fetch funds'

            if want_holdings:
                response = responses.get('holdings')
                if isinstance(response, dict) and response.get('status') == 'success':
                    data = response.get('data')
                    snapshot['holdings_data'] = data if isinstance(data, dict) else {}
                    snapshot['holdings_live'] = True
                else:
                    snapshot['error'] = (
                        snapshot['error']
                        or (response or {}).get('message')
                        or 'Failed to fetch holdings'
                    )
        except Exception as exc:
            snapshot['error'] = str(exc)
            current_app.logger.error(
                f'Equity snapshot failed for account {cred["account_id"]}: {exc}'
            )

    # Degrade to the cached payload for anything that did not come back live.
    if want_funds and snapshot['funds'] is None:
        snapshot['funds'] = cred.get('cached_funds') or {}
        snapshot['is_stale'] = True
    if want_holdings and snapshot['holdings_data'] is None:
        snapshot['holdings_data'] = cred.get('cached_holdings') or {}
        snapshot['is_stale'] = True

    return snapshot


def _apply_fresh_cache(snapshot, cred, want_funds, want_holdings):
    """
    Fill the sides that were served from the freshness window.

    This is NOT the stale path. The payload is inside BROKER_CACHE_TTL_SECONDS,
    so it is current data that simply did not need a broker call, and the
    account is not flagged stale for it.
    """
    if want_funds and cred.get('funds_fresh') and snapshot.get('funds') is None:
        snapshot['funds'] = cred.get('cached_funds') or {}
        snapshot['from_cache'] = True
    if want_holdings and cred.get('holdings_fresh') and snapshot.get('holdings_data') is None:
        snapshot['holdings_data'] = cred.get('cached_holdings') or {}
        snapshot['from_cache'] = True
    return snapshot


def _fan_out(creds, want_funds=False, want_holdings=False):
    """
    Read every account in parallel, skipping the broker for anything a fresh
    cache already answers. Returns account_id to snapshot.
    """
    snapshots = {}
    if not creds:
        return snapshots

    app = current_app._get_current_object()

    # Freshness gate first, so an account fully covered by cache never reaches a
    # thread, let alone the broker.
    live = []
    for cred in creds:
        needs_funds = bool(want_funds) and not cred.get('funds_fresh')
        needs_holdings = bool(want_holdings) and not cred.get('holdings_fresh')
        if not needs_funds and not needs_holdings:
            snapshots[cred['account_id']] = _apply_fresh_cache(
                _new_snapshot(cred['account_id']), cred, want_funds, want_holdings
            )
            continue
        live.append((cred, needs_funds, needs_holdings))

    if not live:
        current_app.logger.debug(
            f'Equity fan-out served {len(creds)} accounts from cache, no broker call',
            extra={'event': 'equity_fanout_cached'}
        )
        return snapshots

    if len(live) == 1:
        cred, needs_funds, needs_holdings = live[0]
        snapshot = _fetch_account_snapshot(app, cred, needs_funds, needs_holdings)
        _apply_fresh_cache(snapshot, cred, want_funds, want_holdings)
        snapshots[snapshot['account_id']] = snapshot
        return snapshots

    creds_by_account = {cred['account_id']: cred for cred, _, _ in live}
    with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(live))) as executor:
        futures = [
            executor.submit(_fetch_account_snapshot, app, cred, needs_funds, needs_holdings)
            for cred, needs_funds, needs_holdings in live
        ]
        for future in as_completed(futures):
            try:
                snapshot = future.result()
            except Exception as exc:
                current_app.logger.error(f'Equity account fan-out worker failed: {exc}')
                continue
            cred = creds_by_account.get(snapshot['account_id'])
            if cred is not None:
                _apply_fresh_cache(snapshot, cred, want_funds, want_holdings)
            snapshots[snapshot['account_id']] = snapshot

    return snapshots


def _refresh_account_cache(accounts, snapshots):
    """
    Write live broker payloads back into the TradingAccount cache columns.

    last_data_update is advanced only when funds came back live, because the
    F&O funds screen treats that column as the age of last_funds_data. Writing
    it after a holdings-only read would make stale cash look fresh over there.
    A live holdings read is timestamped in _HOLDINGS_REFRESHED_AT instead, which
    is the only thing the holdings freshness gate reads.
    """
    changed = False
    now = datetime.utcnow()
    refreshed_holdings = []
    refreshed_funds = []

    for account in accounts:
        snapshot = snapshots.get(account.id)
        if not snapshot:
            continue
        if snapshot.get('funds_live') and isinstance(snapshot.get('funds'), dict):
            account.last_funds_data = snapshot['funds']
            account.last_data_update = now
            refreshed_funds.append(account.id)
            changed = True
        if snapshot.get('holdings_live') and isinstance(snapshot.get('holdings_data'), dict):
            account.last_holdings_data = snapshot['holdings_data']
            refreshed_holdings.append(account.id)
            changed = True

    if not changed:
        return

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f'Could not cache equity broker payloads: {exc}')
        return

    # Stamped only after the payload is actually on disk. Stamping a write that
    # rolled back would gate the next request onto the previous payload.
    for account_id in refreshed_holdings:
        _mark_holdings_refreshed(account_id, now)
    for account_id in refreshed_funds:
        _mark_funds_refreshed(account_id, now)


def _quote_credential(creds, snapshots):
    """
    Pick one account to read quotes through. Prefer an account that just
    answered live, otherwise any account with a usable key.
    """
    for cred in creds:
        snapshot = snapshots.get(cred['account_id'])
        if cred.get('api_key') and snapshot and not snapshot.get('is_stale'):
            return cred
    for cred in creds:
        if cred.get('api_key'):
            return cred
    return None


def _seconds_left(deadline):
    """Wall clock seconds left in a fallback budget, or None when unbounded."""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _call_timeout(deadline):
    """
    Per call timeout for a fallback broker read: the smaller of the interactive
    timeout and what is left of the budget. Returns 0.0 when there is too little
    left to be worth a call.
    """
    left = _seconds_left(deadline)
    if left is None:
        return float(BROKER_TIMEOUT_SECONDS)
    if left < MIN_QUOTE_CALL_SECONDS:
        return 0.0
    return min(float(BROKER_TIMEOUT_SECONDS), left)


def _fetch_quotes_individually(app, cred, symbol_keys, deadline=None):
    """
    Per-symbol quote fallback for brokers or SDKs without multiquotes.

    Bounded twice over: by MAX_QUOTE_FALLBACK_SYMBOLS and by the wall clock
    deadline. Whatever has answered when the budget runs out is returned and the
    rest is abandoned, because the caller has cheaper prices to fall back on and
    the browser has an abort of its own.
    """
    quotes = {}
    keys = list(symbol_keys)[:MAX_QUOTE_FALLBACK_SYMBOLS]
    if not keys:
        return quotes

    timeout = _call_timeout(deadline)
    if timeout <= 0:
        return quotes

    def fetch_one(key):
        symbol, exchange = key
        with app.app_context():
            try:
                client = ExtendedOpenAlgoAPI(
                    api_key=cred['api_key'],
                    host=cred['host_url'],
                    timeout=timeout
                )
                response = client.quotes(symbol=symbol, exchange=exchange)
            except Exception:
                return (key, None)
            if isinstance(response, dict) and response.get('status') == 'success':
                data = response.get('data')
                if isinstance(data, dict):
                    return (key, data)
            return (key, None)

    executor = ThreadPoolExecutor(max_workers=min(MAX_QUOTE_FALLBACK_WORKERS, len(keys)))
    try:
        futures = [executor.submit(fetch_one, key) for key in keys]
        try:
            for future in as_completed(futures, timeout=_seconds_left(deadline)):
                try:
                    key, data = future.result()
                except Exception:
                    continue
                if data:
                    quotes[key] = {
                        'ltp': _to_float(data.get('ltp')),
                        'prev_close': _to_float(data.get('prev_close')),
                    }
        except FuturesTimeoutError:
            current_app.logger.debug(
                f'Equity quote fallback stopped at its {MAX_QUOTE_FALLBACK_SECONDS}s '
                f'budget with {len(quotes)} of {len(keys)} symbols answered',
                extra={'event': 'equity_quote_fallback_timeout'}
            )
    finally:
        # Never wait here. Waiting would hand the request back exactly the wall
        # clock the deadline exists to prevent. Calls already running carry the
        # per call timeout above and end on their own.
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    return quotes


def _fetch_quotes(cred, symbol_keys, deadline=None):
    """
    REST FALLBACK ONLY. Read the last traded price and previous close for the
    symbols the push feed cannot answer yet.

    Pure read. Returns (symbol, exchange) to {'ltp', 'prev_close'}, and simply
    returns fewer entries when the broker is unavailable or the budget runs out.
    Callers fall back to the price implied by the holdings payload.
    """
    quotes = {}
    keys = list(symbol_keys)[:MAX_QUOTE_SYMBOLS]
    if not cred or not cred.get('api_key') or not keys:
        return quotes

    app = current_app._get_current_object()
    timeout = _call_timeout(deadline)
    if timeout <= 0:
        return quotes

    response = None
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=cred['api_key'],
            host=cred['host_url'],
            timeout=timeout
        )
        response = client.multiquotes(
            symbols=[{'symbol': symbol, 'exchange': exchange} for symbol, exchange in keys]
        )
    except Exception as exc:
        current_app.logger.debug(f'Equity multiquotes unavailable: {exc}')
        response = None

    if isinstance(response, dict) and response.get('status') == 'success':
        entries = response.get('results')
        if not isinstance(entries, list):
            entries = response.get('data')
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = str(entry.get('symbol') or '').strip().upper()
                if not symbol:
                    continue
                exchange = str(entry.get('exchange') or 'NSE').strip().upper()
                payload = entry.get('data') if isinstance(entry.get('data'), dict) else entry
                quotes[(symbol, exchange)] = {
                    'ltp': _to_float(payload.get('ltp')),
                    'prev_close': _to_float(payload.get('prev_close')),
                }

    missing = [key for key in keys if key not in quotes]
    if missing:
        quotes.update(_fetch_quotes_individually(app, cred, missing, deadline))

    return quotes


# ---------------------------------------------------------------------------
# Prices. Pushed by the shared WebSocket feed, with REST as a bounded backstop.
# ---------------------------------------------------------------------------

def _prune_feed_subscriptions(keys):
    """
    Release the symbols the equity screens no longer hold.

    Without this the feed's subscription set only ever grows as holdings change,
    until it reaches its own ceiling and starts refusing new symbols. Only a
    view that covers every account may prune, because a filtered view has not
    seen the symbols it is about to release.
    """
    current = set(keys)
    with _CACHE_LOCK:
        stale = _FEED_SYMBOLS - current
        _FEED_SYMBOLS.clear()
        _FEED_SYMBOLS.update(current)

    if not stale:
        return

    try:
        released = equity_price_feed.release(sorted(stale))
    except Exception as exc:
        current_app.logger.warning(
            f'Equity price feed release failed: {exc}',
            extra={'event': 'equity_feed_release_failed'}
        )
        return

    if released:
        current_app.logger.debug(
            f'Equity price feed released {released} symbols that are no longer held',
            extra={'event': 'equity_feed_released'}
        )


def _feed_status_block(stats):
    """
    Compact feed health for the JSON payloads, so a screen can say whether the
    prices it is showing were pushed or fetched.

    source is 'websocket' when every symbol in view came from the push feed,
    'rest' when none did, 'mixed' in between and 'none' when nothing is held.
    """
    try:
        status = equity_price_feed.status()
    except Exception as exc:
        current_app.logger.debug(f'Equity price feed status unavailable: {exc}')
        status = {}

    requested = stats.get('requested', 0)
    from_feed = stats.get('from_feed', 0)

    if requested <= 0:
        source = 'none'
    elif from_feed >= requested:
        source = 'websocket'
    elif from_feed <= 0:
        source = 'rest'
    else:
        source = 'mixed'

    return {
        'available': bool(status.get('available')),
        'authenticated': bool(status.get('authenticated')),
        'live': bool(status.get('authenticated')) and from_feed > 0,
        'source': source,
        'subscribed': _to_int(status.get('subscribed')),
        'pending': _to_int(status.get('pending')),
        'priced': _to_int(status.get('priced')),
        'ticks': _to_int(status.get('ticks')),
        'last_tick_at': status.get('last_tick_at'),
        'last_tick_age_seconds': status.get('last_tick_age_seconds'),
        'symbols_requested': requested,
        'symbols_from_feed': from_feed,
        'symbols_from_rest': stats.get('from_rest', 0),
        'rest_fallback_symbols': stats.get('fallback_symbols', 0),
    }


def _resolve_prices(creds, snapshots, symbol_keys, row_closes=None,
                    want_prev_close=False, prune=False):
    """
    Resolve the price of every symbol in view, event driven first.

    The shared WebSocket feed is the primary source and costs no broker call:
    the symbols in view are subscribed once (idempotent, so this is cheap on
    every poll) and read straight out of the pushed cache. REST is a backstop
    for two gaps only, both bounded by MAX_QUOTE_FALLBACK_SECONDS:
        a symbol the feed has no price for yet, normally just the first poll
            after it was subscribed,
        a previous close that is not known for today, because the feed
            subscribes in LTP mode and does not carry one. It is asked for once
            per symbol per day and then remembered, so Today's P&L survives
            without a quote round trip on every poll.
    With a warm feed and the closes already known, this makes zero broker calls.

    Returns (quotes, feed_block), where quotes keeps the shape the callers
    already expect: (symbol, exchange) to {'ltp', 'prev_close'}.
    """
    keys = sorted(set(symbol_keys))
    stats = {
        'requested': len(keys),
        'from_feed': 0,
        'from_rest': 0,
        'fallback_symbols': 0,
    }

    if not keys:
        # An empty view is usually a broker read that failed rather than a
        # portfolio that emptied, so nothing is released on it.
        return {}, _feed_status_block(stats)

    if prune:
        _prune_feed_subscriptions(keys)

    feed_prices = {}
    try:
        equity_price_feed.ensure_subscribed(keys)
        feed_prices = equity_price_feed.get_prices(keys)
    except Exception as exc:
        current_app.logger.warning(
            f'Equity price feed unavailable, falling back to REST quotes: {exc}',
            extra={'event': 'equity_feed_unavailable'}
        )
        feed_prices = {}

    row_closes = row_closes or {}
    quotes = {}
    for key in keys:
        ltp = _to_float(feed_prices.get(key))
        if ltp > 0:
            stats['from_feed'] += 1

        prev_close = _prev_close_cached(key)
        if prev_close <= 0:
            # Last resort for the day's reference price: the close the broker
            # put on the holding row itself. Free, and better than showing no
            # Today's P&L at all.
            prev_close = _to_float(row_closes.get(key))

        quotes[key] = {'ltp': ltp, 'prev_close': prev_close}

    missing_ltp = [key for key in keys if quotes[key]['ltp'] <= 0]
    missing_close = []
    if want_prev_close:
        missing_close = [
            key for key in keys
            if quotes[key]['ltp'] > 0
            and quotes[key]['prev_close'] <= 0
            and not _prev_close_asked(key)
        ]

    # Prices first: a missing price is visible on every screen, a missing
    # previous close costs one KPI. Whatever does not fit the budget is retried
    # on the next poll.
    fallback_keys = missing_ltp + missing_close
    if not fallback_keys:
        return quotes, _feed_status_block(stats)

    stats['fallback_symbols'] = len(fallback_keys)
    fetched = _fetch_quotes(
        _quote_credential(creds, snapshots),
        fallback_keys,
        deadline=time.monotonic() + MAX_QUOTE_FALLBACK_SECONDS
    )

    for key, data in fetched.items():
        entry = quotes.get(key)
        if entry is None:
            continue
        ltp = _to_float(data.get('ltp'))
        if ltp > 0 and entry['ltp'] <= 0:
            entry['ltp'] = ltp
            stats['from_rest'] += 1
        prev_close = _to_float(data.get('prev_close'))
        if prev_close > 0:
            entry['prev_close'] = prev_close
        # Recorded even when it is zero: the broker answered, and asking again
        # on every poll for a close it does not publish is pure latency.
        _remember_prev_close(key, prev_close)

    return quotes, _feed_status_block(stats)


# ---------------------------------------------------------------------------
# Broker payload normalisation
# ---------------------------------------------------------------------------

def _normalise_broker_holdings(holdings_data):
    """
    Extract the CNC delivery rows from an OpenAlgo holdings payload.

    Holdings are delivery by definition, so a broker adapter that leaves the
    product field empty is kept. A row that explicitly reports a product other
    than CNC belongs to another module and is skipped.
    """
    if isinstance(holdings_data, dict):
        rows = holdings_data.get('holdings') or []
    elif isinstance(holdings_data, list):
        rows = holdings_data
    else:
        rows = []

    normalised = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        product = str(row.get('product') or '').strip().upper()
        if product and product != EQUITY_PRODUCT_CNC:
            continue

        symbol = str(row.get('symbol') or '').strip().upper()
        quantity = _to_int(row.get('quantity'))
        if not symbol or quantity <= 0:
            continue

        avg_cost = _first_number(row, _AVG_COST_KEYS)
        pnl = _to_float(row.get('pnl'))
        pnl_percent = _first_number(row, _PNL_PCT_KEYS)

        # The documented OpenAlgo holdings row carries only symbol, exchange,
        # product, quantity, pnl and pnlpercent: no average price and no LTP.
        # Without a cost basis, Stake percent, Avg Cost, Investment Value and
        # Gross P&L would every one of them render as a real-looking 0.00, so
        # reconstruct it from the P&L pair the way the existing F&O holdings
        # screen already does (app/trading/routes.py, totalinvvalue).
        cost_basis_derived = False
        if avg_cost <= 0.0 and pnl_percent:
            try:
                invested = abs(pnl / (pnl_percent / 100.0))
            except (TypeError, ValueError, ZeroDivisionError):
                invested = 0.0
            if invested > 0.0:
                avg_cost = invested / quantity
                cost_basis_derived = True

        normalised.append({
            'symbol': symbol,
            'exchange': str(row.get('exchange') or 'NSE').strip().upper() or 'NSE',
            'quantity': quantity,
            'avg_cost': avg_cost,
            'broker_ltp': _first_number(row, _LTP_KEYS),
            # Previous close, when the broker adapter publishes one on the
            # holding row. The push feed subscribes in LTP mode and carries no
            # close, so this is a free source for Today's P&L. It is only ever
            # used when no quote has answered for the symbol today.
            'prev_close': _first_number(row, _PREV_CLOSE_KEYS),
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            # True when the broker gave no average price and the figure above was
            # implied from pnl and pnlpercent rather than reported directly.
            'cost_basis_derived': cost_basis_derived,
            # True when no cost basis could be established at all. The screen must
            # show a dash for these rather than a zero that reads as a real value.
            'cost_basis_missing': avg_cost <= 0.0,
            'pledged_quantity': _to_int(_first_number(row, _PLEDGED_KEYS)),
        })

    return normalised


def _resolve_ltp(row, quote, fallback_price=0.0):
    """
    Resolve the last traded price for one holding row.

    Preference order: a live quote, then an LTP the broker put on the holding
    row, then the price implied by the P&L the broker already reported, then a
    stored fallback price.

    The third step is not a business formula. It reconstructs a number the
    broker itself published: it inverts the P&L the broker computed from its
    own last price, so the screen agrees with the broker instead of showing a
    blank.
    """
    if quote and _to_float(quote.get('ltp')) > 0:
        return _to_float(quote['ltp'])

    if _to_float(row.get('broker_ltp')) > 0:
        return _to_float(row['broker_ltp'])

    quantity = _to_int(row.get('quantity'))
    avg_cost = _to_float(row.get('avg_cost'))
    if quantity > 0 and avg_cost > 0:
        return avg_cost + (_to_float(row.get('pnl')) / quantity)

    if _to_float(fallback_price) > 0:
        return _to_float(fallback_price)

    return avg_cost


def _holding_meta_map(account_ids):
    """
    AlgoMirror's own side of a holding (trade nature, stop loss, target, exit
    mode, pledged quantity), keyed by (account_id, symbol, exchange).
    """
    if not account_ids:
        return {}

    rows = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.account_id.in_(list(account_ids))
    ).all()

    return {
        (row.account_id, (row.symbol or '').strip().upper(), (row.exchange or 'NSE').strip().upper()): row
        for row in rows
    }


# ---------------------------------------------------------------------------
# Payload builders shared by the JSON endpoints, the pages and the CSV export
# ---------------------------------------------------------------------------

def _account_context(fetch_funds=False, fetch_holdings=False, fetch_account_ids=None):
    """
    Load accounts, allocations, ratios and (optionally) live broker data once,
    so every builder below works from the same snapshot.

    Allocations always cover every active account, because the ratio and the
    stake denominator are defined across all of them. fetch_account_ids narrows
    only the broker fan-out, so a Holdings view filtered to one account does not
    poll the other brokers.
    """
    accounts = _active_accounts()
    allocation_rows = _get_or_create_allocations(accounts)
    allocation_amounts = _allocation_amounts(accounts, allocation_rows)
    ratios = compute_order_qty_ratios(allocation_amounts)

    snapshots = {}
    creds = []
    if accounts and (fetch_funds or fetch_holdings):
        wanted = accounts
        if fetch_account_ids is not None:
            wanted = [account for account in accounts if account.id in set(fetch_account_ids)]
        creds = _account_credentials(wanted)
        snapshots = _fan_out(creds, want_funds=fetch_funds, want_holdings=fetch_holdings)
        _refresh_account_cache(wanted, snapshots)

    return {
        'accounts': accounts,
        'allocation_rows': allocation_rows,
        'allocation_amounts': allocation_amounts,
        'ratios': ratios,
        'snapshots': snapshots,
        'creds': creds,
    }


def _available_cash(snapshot):
    """Raw broker cash balance. Never an input to any equity formula."""
    funds = (snapshot or {}).get('funds') or {}
    return _to_float(funds.get('availablecash'))


def _build_accounts_payload(fetch_live=True):
    """
    M2 Accounts: one entry per account with live cash, the rupee allocation and
    the derived Order Qty Ratio, plus the footer totals.
    """
    context = _account_context(fetch_funds=fetch_live)
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_rows = context['allocation_rows']
    allocation_amounts = context['allocation_amounts']
    ratios = context['ratios']

    entries = []
    for account in accounts:
        snapshot = snapshots.get(account.id)
        row = allocation_rows.get(account.id)
        entries.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'connection_status': account.connection_status,
            'is_active': bool(account.is_active),
            'is_equity_active': True if row is None else row.is_active is not False,
            'available_cash': _money(_available_cash(snapshot)) if fetch_live else None,
            'is_stale': bool(snapshot.get('is_stale')) if snapshot else True,
            'error': (snapshot or {}).get('error'),
            'equity_fund_allocation': _money(allocation_amounts.get(account.id, 0.0)),
            'order_qty_ratio_pct': _pct(ratios.get(account.id, 0.0)),
        })

    total_allocation = sum(allocation_amounts.values())
    ratio_total = sum(ratios.values())

    return {
        'accounts': entries,
        'totals': {
            'total_equity_fund_allocation': _money(total_allocation),
            'order_qty_ratio_pct_total': _pct(ratio_total),
            'active_accounts': len(allocation_amounts),
            'total_available_cash': _money(
                sum(_available_cash(snapshots.get(account.id)) for account in accounts)
            ) if fetch_live else None,
        },
        'live_cash': bool(fetch_live),
        'rules': ALLOCATION_RULES,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_dashboard_payload():
    """
    M1 Dashboard.

    KPI DEFINITIONS. The approved mockup's KPI strip does not reconcile with its
    own account cards (Total Portfolio Value 224.60L against card holdings
    summing to 32.20L, Available Cash 105.60L against card cash summing to
    182.84L), so those figures are treated as illustrative sample data. Every
    KPI below is the honest sum of the per-account values actually shown on the
    cards:
        total_portfolio_value = sum of each card's Holdings Value (LTP x qty of
            the CNC delivery holdings).
        available_cash        = sum of each card's Available Cash.
        unrealised_pnl        = sum of each card's Unrealised P&L.
        todays_pnl            = sum of each card's Today's P&L.
        active_accounts       = number of active equity accounts included below.
        open_orders           = equity orders still PENDING or PARTIAL. Always
            zero in increment 1 because nothing can place an order yet.
    Correct these definitions here if the owner intended something else.
    """
    context = _account_context(fetch_funds=True, fetch_holdings=True)
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_amounts = context['allocation_amounts']
    ratios = context['ratios']

    per_account_rows = {}
    symbol_keys = set()
    row_closes = {}
    for account in accounts:
        rows = _normalise_broker_holdings((snapshots.get(account.id) or {}).get('holdings_data'))
        per_account_rows[account.id] = rows
        for row in rows:
            key = (row['symbol'], row['exchange'])
            symbol_keys.add(key)
            close = _to_float(row.get('prev_close'))
            if close > 0 and key not in row_closes:
                row_closes[key] = close

    # Prices come from the pushed feed. The dashboard sees every account, so it
    # is also the view that may release symbols that are no longer held.
    quotes, price_feed = _resolve_prices(
        context['creds'],
        snapshots,
        symbol_keys,
        row_closes=row_closes,
        want_prev_close=True,
        prune=True
    )
    meta_map = _holding_meta_map([account.id for account in accounts])

    cards = []
    stale_account_ids = []
    for account in accounts:
        snapshot = snapshots.get(account.id) or {}
        rows = per_account_rows.get(account.id, [])

        holdings_value = 0.0
        unrealised = 0.0
        todays = 0.0
        todays_known = False
        pledged_quantity = 0

        for row in rows:
            key = (row['symbol'], row['exchange'])
            meta = meta_map.get((account.id, row['symbol'], row['exchange']))
            quote = quotes.get(key)
            ltp = _resolve_ltp(row, quote, meta.last_price if meta else 0.0)
            quantity = row['quantity']

            # Fall back to AlgoMirror's own cost basis when the broker adapter
            # does not report an average price. Without this an account with a
            # live quote and no average price would read as pure profit.
            avg_cost = row['avg_cost']
            if avg_cost <= 0 and meta is not None:
                avg_cost = _to_float(meta.avg_cost)

            holdings_value += turnover(ltp, quantity)
            unrealised += gross_pnl(ltp, avg_cost, quantity) if avg_cost > 0 else 0.0

            prev_close = _to_float((quote or {}).get('prev_close'))
            if prev_close > 0:
                # Today's move times quantity. Structurally the same calculation
                # as gross P&L, with the previous close standing in for the
                # average cost, so the engine is reused rather than duplicated.
                todays += gross_pnl(ltp, prev_close, quantity)
                todays_known = True

            pledged = row['pledged_quantity']
            if pledged <= 0 and meta is not None:
                pledged = _to_int(meta.pledged_quantity)
            pledged_quantity += max(pledged, 0)

        allocation = allocation_amounts.get(account.id, 0.0)
        if snapshot.get('is_stale'):
            stale_account_ids.append(account.id)

        # Pledge percent. Pledged stock is lodged as collateral at a haircut, so
        # the collateral the broker reports is smaller than the stock behind it.
        # Collateral is the account's available margin minus its raw cash, which
        # is exactly how those two figures are quoted on the F&O dashboard.
        funds = snapshot.get('funds') or {}
        cash = _available_cash(snapshot)
        collateral = _to_float(funds.get('collateral'))
        if collateral <= 0:
            # Some broker adapters report an available margin instead of a
            # separate collateral figure. Derive it from the pair in that case.
            collateral = collateral_from_margin(
                _first_number(funds, _AVAILABLE_MARGIN_KEYS), cash
            )

        cards.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'connection_status': account.connection_status,
            'order_qty_ratio_pct': _pct(ratios.get(account.id, 0.0)),
            'equity_fund_allocation': _money(allocation),
            'available_cash': _money(_available_cash(snapshot)),
            'holdings_value': _money(holdings_value),
            'invested_pct': _pct(invested_percent(holdings_value, allocation)),
            'pledged_quantity': pledged_quantity,
            'pledge_pct': _pct(pledge_percent(collateral, holdings_value)),
            'collateral': _money(collateral),
            'unrealised_pnl': _money(unrealised),
            'todays_pnl': _money(todays),
            'todays_pnl_available': todays_known,
            'holdings_count': len(rows),
            'is_stale': bool(snapshot.get('is_stale')),
            'error': snapshot.get('error'),
        })

    open_orders = EquityOrder.query.filter(
        EquityOrder.user_id == current_user.id,
        EquityOrder.status.in_([EQUITY_ORDER_STATUS_PENDING, EQUITY_ORDER_STATUS_PARTIAL])
    ).count()

    kpi = {
        'active_accounts': len(allocation_amounts),
        'connected_accounts': sum(1 for card in cards if not card['is_stale']),
        'total_portfolio_value': _money(sum(card['holdings_value'] for card in cards)),
        'available_cash': _money(sum(card['available_cash'] for card in cards)),
        'unrealised_pnl': _money(sum(card['unrealised_pnl'] for card in cards)),
        'todays_pnl': _money(sum(card['todays_pnl'] for card in cards)),
        'todays_pnl_available': any(card['todays_pnl_available'] for card in cards),
        'open_orders': open_orders,
        'total_equity_fund_allocation': _money(sum(allocation_amounts.values())),
    }

    return {
        'kpi': kpi,
        'accounts': cards,
        'todays_orders': _build_todays_orders(),
        'stale_account_ids': stale_account_ids,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_todays_orders():
    """
    Today's equity orders. Always empty in increment 1 because no code path can
    place an order yet, so the screen must render an empty state.

    The day boundary is UTC. Indian market hours (09:15 to 15:30 IST) map to
    03:45 to 10:00 UTC on the same calendar date, so a trading day never
    straddles the UTC boundary.
    """
    start_of_day = datetime.combine(datetime.utcnow().date(), datetime.min.time())

    orders = EquityOrder.query.filter(
        EquityOrder.user_id == current_user.id,
        EquityOrder.placed_at >= start_of_day
    ).order_by(EquityOrder.placed_at.desc()).all()

    payload = []
    for order in orders:
        splits = order.splits.all()
        payload.append({
            'order_id': order.id,
            'symbol': order.symbol,
            'exchange': order.exchange,
            'side': order.side,
            'order_type': order.order_type,
            'product': order.product,
            'total_quantity': order.total_quantity,
            'filled_quantity': sum(_to_int(split.filled_quantity) for split in splits),
            'price': _money(order.price) if order.price is not None else None,
            'stop_loss': _money(order.stop_loss) if order.stop_loss is not None else None,
            'target': _money(order.target) if order.target is not None else None,
            'status': order.status,
            'placed_at': _iso(order.placed_at),
            'accounts_count': len(splits),
        })
    return payload


def _selected_account_id():
    """
    Read the account filter. Returns (account_id_or_None, error_message_or_None).
    An unknown or unowned id is an error, never silently widened to all
    accounts.
    """
    raw = (request.args.get('account') or '').strip()
    if not raw or raw.lower() == 'all':
        return None, None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        return None, 'Invalid account filter'
    account = TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id,
        is_active=True
    ).first()
    if not account:
        return None, 'Account not found'
    return account_id, None


def _selected_trade_nature_id():
    """Read the trade nature filter. Returns (nature_id_or_None, error_or_None)."""
    raw = (request.args.get('trade_nature') or '').strip()
    if not raw or raw.lower() == 'all':
        return None, None
    try:
        nature_id = int(raw)
    except (TypeError, ValueError):
        return None, 'Invalid trade nature filter'
    nature = EquityTradeNature.query.filter_by(
        id=nature_id,
        user_id=current_user.id
    ).first()
    if not nature:
        return None, 'Trade nature not found'
    return nature_id, None


def _build_holdings_payload(account_filter, nature_filter):
    """
    M7 Holdings.

    Rows are aggregated by (symbol, exchange, trade nature) across the accounts
    in view. Grouping by trade nature as well as symbol means a symbol held
    under two different natures is shown honestly as two rows, each with its own
    stop loss, target and exit mode, instead of one row picking a winner. In the
    normal case, where a symbol carries one nature, this is exactly one row per
    symbol.

    Est. Costs is the cost of exiting the position: side SELL, one order and one
    scrip per contributing account, priced at the current LTP. That is what has
    to be paid to realise the P&L shown next to it.
    """
    context = _account_context(
        fetch_holdings=True,
        fetch_account_ids=None if account_filter is None else [account_filter]
    )
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_amounts = context['allocation_amounts']

    accounts_in_view = [
        account for account in accounts
        if account_filter is None or account.id == account_filter
    ]
    account_ids = [account.id for account in accounts_in_view]
    account_names = {account.id: account.account_name for account in accounts_in_view}
    broker_names = {account.id: account.broker_name for account in accounts_in_view}

    natures = _trade_natures()
    nature_names = {nature.id: nature.name for nature in natures}
    nature_order = {nature.id: index for index, nature in enumerate(natures)}

    meta_map = _holding_meta_map(account_ids)
    rates_by_account, unconfigured_rate_accounts = _brokerage_rates_by_account(account_ids)

    per_account_rows = {}
    symbol_keys = set()
    for account in accounts_in_view:
        rows = _normalise_broker_holdings((snapshots.get(account.id) or {}).get('holdings_data'))
        per_account_rows[account.id] = rows
        for row in rows:
            symbol_keys.add((row['symbol'], row['exchange']))

    # context['creds'] already covers exactly the accounts in view. Prices come
    # from the pushed feed, and only an unfiltered view may release symbols,
    # because a view filtered to one account has not seen the rest.
    quotes, price_feed = _resolve_prices(
        context['creds'],
        snapshots,
        symbol_keys,
        prune=account_filter is None
    )

    buckets = {}
    stale_account_ids = []
    for account in accounts_in_view:
        snapshot = snapshots.get(account.id) or {}
        if snapshot.get('is_stale'):
            stale_account_ids.append(account.id)

        for row in per_account_rows.get(account.id, []):
            meta = meta_map.get((account.id, row['symbol'], row['exchange']))
            nature_id = meta.trade_nature_id if meta is not None else None

            if nature_filter is not None and nature_id != nature_filter:
                continue

            key = (row['symbol'], row['exchange'], nature_id)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    'symbol': row['symbol'],
                    'exchange': row['exchange'],
                    'trade_nature_id': nature_id,
                    'trade_nature': nature_names.get(nature_id) if nature_id else None,
                    'total_quantity': 0,
                    'pledged_quantity': 0,
                    'at_cost': 0.0,
                    'current_value': 0.0,
                    'gross_pnl': 0.0,
                    'est_costs': 0.0,
                    'ltp': 0.0,
                    'accounts': [],
                    'levels': set(),
                    'is_stale': False,
                }
                buckets[key] = bucket

            quantity = row['quantity']
            avg_cost = row['avg_cost']
            if avg_cost <= 0 and meta is not None:
                avg_cost = _to_float(meta.avg_cost)

            quote = quotes.get((row['symbol'], row['exchange']))
            ltp = _resolve_ltp(row, quote, meta.last_price if meta is not None else 0.0)

            at_cost = stock_at_cost(avg_cost, quantity)
            value = turnover(ltp, quantity)
            # With no cost basis at all there is no P&L to report. Reporting one
            # would make the whole market value look like profit.
            gross = gross_pnl(ltp, avg_cost, quantity) if avg_cost > 0 else 0.0
            costs = estimate_costs(
                value,
                EQUITY_SIDE_SELL,
                rates_by_account.get(account.id, BrokerageRates()),
                scrip_count=1
            )

            pledged = row['pledged_quantity']
            if pledged <= 0 and meta is not None:
                pledged = _to_int(meta.pledged_quantity)
            pledged = max(pledged, 0)

            exit_mode = (meta.exit_mode if meta is not None and meta.exit_mode else EQUITY_EXIT_MODE_CONFIRM)
            stop_loss = _to_float(meta.stop_loss) if meta is not None and meta.stop_loss is not None else None
            target = _to_float(meta.target) if meta is not None and meta.target is not None else None

            bucket['total_quantity'] += quantity
            bucket['pledged_quantity'] += pledged
            bucket['at_cost'] += at_cost
            bucket['current_value'] += value
            bucket['gross_pnl'] += gross
            bucket['est_costs'] += costs.total
            bucket['ltp'] = ltp
            bucket['levels'].add((stop_loss, target, exit_mode))
            bucket['is_stale'] = bucket['is_stale'] or bool(snapshot.get('is_stale'))
            bucket['accounts'].append({
                'account_id': account.id,
                'account_name': account_names.get(account.id),
                'broker_name': broker_names.get(account.id),
                'quantity': quantity,
                'avg_cost': _money(avg_cost),
                'pledged_quantity': pledged,
                'stop_loss': _money(stop_loss) if stop_loss is not None else None,
                'target': _money(target) if target is not None else None,
                'exit_mode': exit_mode,
                'est_costs': _money(costs.total),
            })

    rows = []
    for bucket in buckets.values():
        quantity = bucket['total_quantity']
        at_cost = bucket['at_cost']

        # The stop loss, target and exit mode shown on the aggregated row come
        # from the largest contributing account. levels_mixed says the
        # contributors do not agree, which the per-account breakdown spells out.
        primary = max(bucket['accounts'], key=lambda entry: entry['quantity']) if bucket['accounts'] else {}
        exit_mode = primary.get('exit_mode') or EQUITY_EXIT_MODE_CONFIRM

        rows.append({
            'symbol': bucket['symbol'],
            'exchange': bucket['exchange'],
            'trade_nature_id': bucket['trade_nature_id'],
            'trade_nature': bucket['trade_nature'] or 'Unassigned',
            'total_quantity': quantity,
            'stake_pct': _pct(stake_percent_for_view(at_cost, allocation_amounts, account_filter)),
            'avg_cost': _money(_safe_divide(at_cost, quantity)),
            'ltp': _money(bucket['ltp']),
            # Gross P&L is signed, so this must keep the sign. percent_of would
            # clamp a loss to 0.0 and every losing row would read as flat.
            'pnl_pct': _pct(signed_percent_of(bucket['gross_pnl'], at_cost)),
            'stop_loss': primary.get('stop_loss'),
            'target': primary.get('target'),
            'exit_mode': exit_mode,
            'exit_mode_tag': EXIT_MODE_TAGS.get(exit_mode, EXIT_MODE_TAGS[EQUITY_EXIT_MODE_CONFIRM]),
            'levels_mixed': len(bucket['levels']) > 1,
            'pledged_quantity': bucket['pledged_quantity'],
            'pledged_pct': _pct(percent_of(bucket['pledged_quantity'], quantity)),
            'investment_value': _money(at_cost),
            'current_value': _money(bucket['current_value']),
            'gross_pnl': _money(bucket['gross_pnl']),
            'est_costs': _money(bucket['est_costs']),
            'net_pnl': _money(net_pnl(bucket['gross_pnl'], bucket['est_costs'])),
            'accounts': bucket['accounts'],
            'is_stale': bucket['is_stale'],
        })

    rows.sort(key=lambda row: (
        nature_order.get(row['trade_nature_id'], len(nature_order)),
        row['symbol']
    ))

    grouped = nature_filter is None
    groups = []
    for row in rows:
        if groups and groups[-1]['trade_nature_id'] == row['trade_nature_id']:
            groups[-1]['rows'].append(row)
        else:
            groups.append({
                'trade_nature_id': row['trade_nature_id'],
                'trade_nature': row['trade_nature'],
                'rows': [row],
            })

    total_investment = sum(row['investment_value'] for row in rows)
    total_current = sum(row['current_value'] for row in rows)
    total_gross = sum(row['gross_pnl'] for row in rows)
    total_costs = sum(row['est_costs'] for row in rows)

    return {
        'kpi': {
            'total_holdings': len(rows),
            'total_investment': _money(total_investment),
            'current_value': _money(total_current),
            'gross_pnl': _money(total_gross),
            'est_costs': _money(total_costs),
            'net_pnl': _money(net_pnl(total_gross, total_costs)),
        },
        'holdings': rows,
        'groups': groups,
        'grouped': grouped,
        'filters': {
            'account': account_filter if account_filter is not None else 'all',
            'trade_nature': nature_filter if nature_filter is not None else 'all',
        },
        'accounts': [
            {
                'account_id': account.id,
                'account_name': account.account_name,
                'broker_name': account.broker_name,
            }
            for account in accounts
        ],
        'trade_natures': [
            {'id': nature.id, 'name': nature.name}
            for nature in natures
        ],
        'stake_denominator': _money(
            sum(allocation_amounts.values()) if account_filter is None
            else allocation_amounts.get(account_filter, 0.0)
        ),
        'stale_account_ids': stale_account_ids,
        'accounts_missing_rates': unconfigured_rate_accounts,
        'exit_mode_tags': EXIT_MODE_TAGS,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_rates_payload():
    """Settings: the rate version in effect today for each account."""
    accounts = _active_accounts()
    today = date.today()

    entries = []
    for account in accounts:
        row = EquityBrokerageRate.get_effective_rate(current_user.id, account.id, today)
        entries.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'rate_id': row.id if row else None,
            'is_configured': row is not None,
            'effective_from': _iso(row.effective_from) if row else None,
            'brokerage_per_order': _money(row.brokerage_per_order) if row else 0.0,
            'stt_pct': _to_float(row.stt_pct) if row else 0.0,
            'exchange_txn_pct': _to_float(row.exchange_txn_pct) if row else 0.0,
            'sebi_pct': _to_float(row.sebi_pct) if row else 0.0,
            'stamp_duty_pct': _to_float(row.stamp_duty_pct) if row else 0.0,
            'gst_pct': _to_float(row.gst_pct) if row else 0.0,
            'dp_amc_charge': _money(row.dp_amc_charge) if row else 0.0,
        })

    return {
        'rates': entries,
        'today': _iso(today),
        'suggested_defaults': SUGGESTED_RATE_DEFAULTS,
        'notes': COST_FORMULA_NOTES,
        'generated_at': _iso(datetime.utcnow()),
    }


def _log_activity(action, details=None, account_id=None):
    """Audit trail entry. Never lets a logging failure break the request."""
    try:
        entry = ActivityLog(
            user_id=current_user.id,
            account_id=account_id,
            action=action,
            details=details,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            status='success'
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f'Could not write equity activity log: {exc}')


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@equity_bp.route('/')
@login_required
def dashboard():
    """M1 Dashboard. Data is loaded by the browser from /equity/api/dashboard."""
    return render_template(
        'equity/dashboard.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/accounts')
@login_required
def accounts():
    """M2 Accounts. Read only against the broker, it writes only the allocation."""
    account_rows = _active_accounts()
    _get_or_create_allocations(account_rows)
    return render_template(
        'equity/accounts.html',
        accounts=account_rows,
        trade_natures=_trade_natures(),
        allocation_rules=ALLOCATION_RULES
    )


@equity_bp.route('/holdings')
@login_required
def holdings():
    """M7 Holdings."""
    return render_template(
        'equity/holdings.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/settings')
@login_required
def settings():
    """Settings: brokerage and statutory charges, versioned by effective date."""
    return render_template(
        'equity/settings.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures(),
        cost_formula_notes=COST_FORMULA_NOTES
    )


# ---------------------------------------------------------------------------
# JSON routes
# ---------------------------------------------------------------------------

@equity_bp.route('/api/dashboard')
@login_required
@heavy_rate_limit()
def api_dashboard():
    """
    KPI strip plus one card per account.

    Prices come from the pushed WebSocket feed and funds and holdings from the
    account cache, so a poll inside the freshness window makes no broker call at
    all. The price_feed block in the response says how many of the prices in
    view were pushed and how many fell back to REST.
    """
    try:
        payload = _build_dashboard_payload()
    except Exception as exc:
        current_app.logger.error(f'Equity dashboard failed: {exc}')
        return _json_error(f'Failed to build equity dashboard: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/accounts')
@login_required
@heavy_rate_limit()
def api_accounts():
    """Allocations with the derived Order Qty Ratio and live Available Cash."""
    try:
        payload = _build_accounts_payload(fetch_live=True)
    except Exception as exc:
        current_app.logger.error(f'Equity accounts failed: {exc}')
        return _json_error(f'Failed to load equity accounts: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/accounts/allocation', methods=['POST'])
@login_required
@api_rate_limit()
def api_save_allocation():
    """
    Save the rupee equity fund allocation per account and recompute the ratios.

    Writes to AlgoMirror's own table only, no broker call. The change is future
    dated: it re-derives the ratio used from now on and never touches the ratio
    already recorded against a past order.

    Request body, either form is accepted:
        {"allocations": [{"account_id": 1, "equity_fund_allocation": 2000000}]}
        {"allocations": {"1": 2000000, "2": 1000000}}
    """
    data = request.get_json(silent=True) or {}
    raw = data.get('allocations')

    if isinstance(raw, dict):
        items = [
            {'account_id': key, 'equity_fund_allocation': value}
            for key, value in raw.items()
        ]
    elif isinstance(raw, list):
        items = raw
    else:
        return _json_error('No allocations supplied')

    if not items:
        return _json_error('No allocations supplied')

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            return _json_error('Each allocation must be an object')
        try:
            account_id = int(item.get('account_id'))
        except (TypeError, ValueError):
            return _json_error('Invalid account id in allocations')

        amount = item.get('equity_fund_allocation')
        if amount is None:
            amount = item.get('allocation')
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return _json_error(f'Invalid allocation amount for account {account_id}')
        if not math.isfinite(amount) or amount < 0:
            return _json_error(f'Allocation must be zero or more for account {account_id}')

        # Ownership scoping: both the id and the owner, never the id alone.
        account = _owned_account(account_id)
        if not account:
            return _json_error(f'Account {account_id} not found', 404)

        parsed.append((account, amount))

    try:
        existing = {
            row.account_id: row
            for row in EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
        }
        changes = []
        for account, amount in parsed:
            row = existing.get(account.id)
            if row is None:
                row = EquityAccountAllocation(
                    account_id=account.id,
                    user_id=current_user.id,
                    equity_fund_allocation=amount
                )
                db.session.add(row)
            else:
                previous = _to_float(row.equity_fund_allocation)
                if previous != amount:
                    changes.append({
                        'account_id': account.id,
                        'account_name': account.account_name,
                        'from': previous,
                        'to': amount,
                    })
                row.equity_fund_allocation = amount
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity allocation save failed: {exc}')
        return _json_error(f'Failed to save equity allocation: {exc}', 500)

    if changes:
        _log_activity('equity_allocation_updated', {'changes': changes})

    # Rebuild without a broker call so saving stays fast. The available_cash
    # fields come back null and live_cash is false, so the screen keeps the cash
    # figures it already has and refreshes only the allocation and the ratio.
    payload = _build_accounts_payload(fetch_live=False)
    payload['status'] = 'success'
    payload['message'] = 'Equity allocation saved and ratios recomputed'
    return jsonify(payload)


@equity_bp.route('/api/holdings')
@login_required
@heavy_rate_limit()
def api_holdings():
    """Holdings with stake percent, gross P&L, estimated costs and net P&L."""
    account_filter, error = _selected_account_id()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    nature_filter, error = _selected_trade_nature_id()
    if error:
        return _json_error(error, 404 if error == 'Trade nature not found' else 400)

    try:
        payload = _build_holdings_payload(account_filter, nature_filter)
    except Exception as exc:
        current_app.logger.error(f'Equity holdings failed: {exc}')
        return _json_error(f'Failed to load equity holdings: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/holdings/export')
@login_required
@heavy_rate_limit()
def api_holdings_export():
    """
    CSV export of the Holdings screen. A pure read: it runs the same builder as
    /equity/api/holdings and serialises the result.
    """
    account_filter, error = _selected_account_id()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    nature_filter, error = _selected_trade_nature_id()
    if error:
        return _json_error(error, 404 if error == 'Trade nature not found' else 400)

    try:
        payload = _build_holdings_payload(account_filter, nature_filter)
    except Exception as exc:
        current_app.logger.error(f'Equity holdings export failed: {exc}')
        return _json_error(f'Failed to export equity holdings: {exc}', 500)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'Symbol', 'Exchange', 'Trade Nature', 'Total Qty', 'Stake %', 'Avg Cost',
        'LTP', 'P&L %', 'Stop Loss', 'Target', 'Exit Mode', 'Pledged Qty',
        'Pledged %', 'Investment', 'Current Value', 'Gross P&L', 'Est. Costs',
        'Net P&L'
    ])
    for row in payload['holdings']:
        writer.writerow([
            row['symbol'],
            row['exchange'],
            row['trade_nature'],
            row['total_quantity'],
            row['stake_pct'],
            row['avg_cost'],
            row['ltp'],
            row['pnl_pct'],
            '' if row['stop_loss'] is None else row['stop_loss'],
            '' if row['target'] is None else row['target'],
            row['exit_mode_tag'],
            row['pledged_quantity'],
            row['pledged_pct'],
            row['investment_value'],
            row['current_value'],
            row['gross_pnl'],
            row['est_costs'],
            row['net_pnl'],
        ])

    kpi = payload['kpi']
    writer.writerow([])
    writer.writerow([
        'TOTAL', '', '', '', '', '', '', '', '', '', '', '', '',
        kpi['total_investment'], kpi['current_value'], kpi['gross_pnl'],
        kpi['est_costs'], kpi['net_pnl']
    ])

    filename = f'equity_holdings_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@equity_bp.route('/api/settings/rates')
@login_required
@api_rate_limit()
def api_settings_rates():
    """The brokerage and statutory rate version currently in effect per account."""
    try:
        payload = _build_rates_payload()
    except Exception as exc:
        current_app.logger.error(f'Equity rates load failed: {exc}')
        return _json_error(f'Failed to load brokerage rates: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


def _parse_rate_field(item, field, maximum=None):
    """Read one rate field as a non-negative finite number. Raises ValueError."""
    value = item.get(field, 0)
    if value is None or value == '':
        value = 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid value for {field}')
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{field} must be zero or more')
    if maximum is not None and number > maximum:
        raise ValueError(f'{field} must not exceed {maximum}')
    return number


@equity_bp.route('/api/settings/rates', methods=['POST'])
@login_required
@api_rate_limit()
def api_save_settings_rates():
    """
    Save brokerage and statutory rates as a NEW effective-dated version.

    A historical row is never updated in place, so past cost figures stay
    reproducible. The only row this can overwrite is one whose effective_from
    equals the requested date: that row is the version being authored for that
    date, not history, and the unique constraint on (account_id, effective_from)
    allows only one of them. Backdating is rejected, because a rate change
    applies to future calculations only.

    Request body:
        {"effective_from": "2026-08-25",
         "rates": [{"account_id": 1, "brokerage_per_order": 20,
                    "stt_pct": 0.1, "exchange_txn_pct": 0.00297,
                    "sebi_pct": 0.0001, "stamp_duty_pct": 0.015,
                    "gst_pct": 18, "dp_amc_charge": 13.5}]}
    """
    data = request.get_json(silent=True) or {}
    items = data.get('rates')
    if not isinstance(items, list) or not items:
        return _json_error('No rates supplied')

    today = date.today()
    raw_date = (data.get('effective_from') or '').strip()
    if raw_date:
        try:
            effective_from = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return _json_error('Invalid effective_from, expected YYYY-MM-DD')
        if effective_from < today:
            return _json_error(
                'Rates can be dated today or later only. Rate changes apply to '
                'future calculations, past cost figures are never rewritten.'
            )
    else:
        effective_from = today

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            return _json_error('Each rate entry must be an object')
        try:
            account_id = int(item.get('account_id'))
        except (TypeError, ValueError):
            return _json_error('Invalid account id in rates')

        # Ownership scoping: both the id and the owner.
        account = _owned_account(account_id)
        if not account:
            return _json_error(f'Account {account_id} not found', 404)

        try:
            values = {
                'brokerage_per_order': _parse_rate_field(item, 'brokerage_per_order'),
                'stt_pct': _parse_rate_field(item, 'stt_pct', maximum=100),
                'exchange_txn_pct': _parse_rate_field(item, 'exchange_txn_pct', maximum=100),
                'sebi_pct': _parse_rate_field(item, 'sebi_pct', maximum=100),
                'stamp_duty_pct': _parse_rate_field(item, 'stamp_duty_pct', maximum=100),
                'gst_pct': _parse_rate_field(item, 'gst_pct', maximum=100),
                'dp_amc_charge': _parse_rate_field(item, 'dp_amc_charge'),
            }
        except ValueError as exc:
            return _json_error(f'Account {account.account_name}: {exc}')

        parsed.append((account, values))

    try:
        saved = []
        for account, values in parsed:
            existing = EquityBrokerageRate.query.filter_by(
                user_id=current_user.id,
                account_id=account.id,
                effective_from=effective_from
            ).first()

            if existing is None:
                row = EquityBrokerageRate(
                    user_id=current_user.id,
                    account_id=account.id,
                    broker_name=account.broker_name,
                    effective_from=effective_from,
                    is_active=True,
                    **values
                )
                db.session.add(row)
            else:
                existing.broker_name = account.broker_name
                existing.is_active = True
                for field, value in values.items():
                    setattr(existing, field, value)

            saved.append({'account_id': account.id, 'account_name': account.account_name})

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity rate save failed: {exc}')
        return _json_error(f'Failed to save brokerage rates: {exc}', 500)

    _log_activity('equity_brokerage_rates_saved', {
        'effective_from': effective_from.isoformat(),
        'accounts': saved
    })

    payload = _build_rates_payload()
    payload['status'] = 'success'
    payload['message'] = (
        f'Rates saved as a new version effective {effective_from.isoformat()}. '
        'Existing versions are unchanged.'
    )
    return jsonify(payload)
