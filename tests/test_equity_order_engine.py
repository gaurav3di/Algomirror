"""
Tests for the equity order placement engine (app/utils/equity_order_engine.py).

There is no OpenAlgo server and no broker on a development machine, and there
must never be one in a test: this module can move real money. Every broker
interaction therefore goes through the engine's one seam, the client_factory
argument, and every test here passes a FakeBroker. A test that reached a real
broker would fail to construct a client rather than place an order, but the
point is that nothing here even tries.

The app runs against a throwaway SQLite file in a temp directory, created and
dropped per test, so the developer's own instance/algomirror.db is never
touched. The environment is set before the app package is imported, because
config.py and models.py read it at import time.
"""

import ast
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-equity-engine-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'engine.sqlite').replace('\\', '/')
os.environ['SECRET_KEY'] = 'equity-engine-test-key-not-for-production'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
# A fixed Fernet key so models.py does not generate one and warn.
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    EquityAccountAllocation,
    EquityHolding,
    EquityOrder,
    EquityOrderSplit,
    EquitySetting,
    TradingAccount,
    User,
    EQUITY_EXIT_MODE_AUTO,
    EQUITY_EXIT_REASON_MANUAL,
    EQUITY_EXIT_REASON_STOP_LOSS,
    EQUITY_FUNDS_ACTION_ABORT,
    EQUITY_HOLDING_STATUS_ACTIVE,
    EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
    EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE,
    EQUITY_HOLDING_STATUS_EXIT_SUBMITTED,
    EQUITY_ORDER_SOURCE_STOP_LOSS,
    EQUITY_ORDER_STATUS_CANCELLED,
    EQUITY_ORDER_STATUS_PARTIAL,
    EQUITY_ORDER_STATUS_PENDING,
    EQUITY_ORDER_TYPE_GTT,
    EQUITY_ORDER_TYPE_LIMIT,
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_PRODUCT_CNC,
    EQUITY_SIDE_BUY,
    EQUITY_SIDE_SELL,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_INDETERMINATE,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUS_SKIPPED,
    EQUITY_SPLIT_STATUS_UNSUPPORTED,
)
from app.utils import equity_order_engine as engine  # noqa: E402

LAKH = 100_000.0

# The allocation set from the approved mockups: 50L in total, so the ratios are
# 40, 20, 20, 10 and 10 percent.
ACCOUNT_SEED = [
    ('mps', 20.00 * LAKH),
    ('sathya', 10.00 * LAKH),
    ('patsen', 10.00 * LAKH),
    ('Unicorp', 5.00 * LAKH),
    ('suji', 5.00 * LAKH),
]

PLENTY_OF_CASH = 10.00 * LAKH


# ---------------------------------------------------------------------------
# The fake broker. Nothing in these tests talks to a real one.
# ---------------------------------------------------------------------------

def success(order_id='ORD1'):
    return {'status': 'success', 'orderid': order_id}


def gtt_success(trigger_id='GTT1'):
    return {'status': 'success', 'trigger_id': trigger_id}


def rejection(message='Insufficient holdings'):
    """A DEFINITE refusal. Nothing is live at the broker, so it may be re-sent."""
    return {'status': 'error', 'message': message, 'error_type': 'api_error', 'code': 200}


def timeout():
    """The exact envelope ExtendedOpenAlgoAPI._make_request returns on a timeout."""
    return {
        'status': 'error',
        'message': 'Request timed out after 30s. The server took too long to respond.',
        'error_type': 'timeout_error',
    }


def connection_error():
    return {
        'status': 'error',
        'message': 'Failed to connect to the server. Please check if the server is running.',
        'error_type': 'connection_error',
    }


def gtt_not_supported(broker='upstox'):
    """
    What OpenAlgo's GTT capability gate looks like by the time it reaches us.

    The service answers HTTP 501 and ExtendedOpenAlgoAPI._handle_response turns
    any non 200 into this http_error envelope carrying the status code.
    """
    return {
        'status': 'error',
        'message': "HTTP 501: {\"status\": \"error\", \"message\": \"GTT orders are not "
                   "supported for broker '%s' yet\"}" % broker,
        'code': 501,
        'error_type': 'http_error',
    }


class FakeBroker:
    """
    One fake OpenAlgo per test, shared by every account.

    Responses are scripted per (account_id, kind). The script is consumed in
    order and the last entry repeats, so a single rejection means "rejects
    once then succeeds" only if a success follows it.
    """

    def __init__(self):
        self.calls = []
        self._scripts = {}
        self._funds = {}
        self._lock = threading.Lock()
        self.before_call = None

    # -- scripting ------------------------------------------------------
    def script(self, account_id, kind, *responses):
        self._scripts[(account_id, kind)] = list(responses)

    def set_funds(self, account_id, cash):
        self._funds[account_id] = cash

    def calls_for(self, account_id, kind=None):
        return [
            call for call in self.calls
            if call['account_id'] == account_id and (kind is None or call['kind'] == kind)
        ]

    def count(self, kind=None):
        return len([call for call in self.calls if kind is None or call['kind'] == kind])

    # -- the seam -------------------------------------------------------
    def factory(self, credential):
        assert credential.get('api_key'), 'the engine must decrypt the API key before the call'
        assert 'host_url' in credential
        return FakeClient(self, credential)

    def _respond(self, account_id, kind, payload):
        if self.before_call is not None:
            self.before_call(account_id, kind, payload)
        with self._lock:
            self.calls.append({'account_id': account_id, 'kind': kind, 'payload': payload})
            script = self._scripts.get((account_id, kind))
            if not script:
                return self._default(account_id, kind)
            return script.pop(0) if len(script) > 1 else script[0]

    def _default(self, account_id, kind):
        if kind == 'funds':
            cash = self._funds.get(account_id, PLENTY_OF_CASH)
            return {'status': 'success', 'data': {'availablecash': cash}}
        if kind == 'placegttorder':
            return gtt_success('GTT-%s' % account_id)
        if kind == 'placeorder':
            return success('ORD-%s' % account_id)
        return {'status': 'success'}


class FakeClient:
    """The surface of ExtendedOpenAlgoAPI that the engine actually uses."""

    def __init__(self, broker, credential):
        self._broker = broker
        self._account_id = credential['account_id']

    def placeorder(self, **kwargs):
        return self._broker._respond(self._account_id, 'placeorder', kwargs)

    def modifyorder(self, **kwargs):
        return self._broker._respond(self._account_id, 'modifyorder', kwargs)

    def cancelorder(self, **kwargs):
        return self._broker._respond(self._account_id, 'cancelorder', kwargs)

    def funds(self):
        return self._broker._respond(self._account_id, 'funds', {})

    def _make_request(self, endpoint, payload):
        return self._broker._respond(self._account_id, endpoint, payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def app():
    application = create_app('development')
    application.config['TESTING'] = True
    application.config['WTF_CSRF_ENABLED'] = False
    return application


@pytest.fixture
def ctx(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def world(ctx):
    """One admin user with the five mockup accounts and their allocations."""
    user = User(username='admin', email='admin@example.com', is_admin=True)
    user.set_password('EngineTest#1')
    db.session.add(user)
    db.session.commit()

    accounts = []
    for index, (name, allocation) in enumerate(ACCOUNT_SEED, start=1):
        account = TradingAccount(
            user_id=user.id,
            account_name=name,
            broker_name='zerodha',
            host_url='http://127.0.0.1:%d' % (5000 + index),
            websocket_url='ws://127.0.0.1:%d' % (8765 + index),
            is_active=True,
        )
        account.set_api_key('api-key-%s' % name)
        db.session.add(account)
        db.session.flush()
        db.session.add(EquityAccountAllocation(
            account_id=account.id,
            user_id=user.id,
            equity_fund_allocation=allocation,
            is_active=True,
        ))
        accounts.append(account)
    db.session.commit()

    return {
        'user_id': user.id,
        'accounts': accounts,
        'account_ids': [account.id for account in accounts],
    }


@pytest.fixture
def broker():
    return FakeBroker()


def place(world, broker, **overrides):
    """Place the standard 100 share MARKET BUY across every account."""
    params = dict(
        user_id=world['user_id'],
        symbol='RELIANCE',
        exchange='NSE',
        side=EQUITY_SIDE_BUY,
        total_quantity=100,
        order_type=EQUITY_ORDER_TYPE_MARKET,
        account_ids=world['account_ids'],
        reference_price=100.0,
        client_factory=broker.factory,
        retry_delay_seconds=0,
        sleeper=lambda _seconds: None,
    )
    params.update(overrides)
    return engine.place_multi_account_order(**params)


def splits_by_account(order_id):
    return {
        split.account_id: split
        for split in EquityOrderSplit.query.filter_by(equity_order_id=order_id).all()
    }


# ---------------------------------------------------------------------------
# A clean fan-out
# ---------------------------------------------------------------------------

class TestCleanFanOut:
    def test_five_accounts_get_their_ratio_share(self, world, broker):
        result = place(world, broker)

        assert result['status'] == 'success'
        assert result['accounts_selected'] == 5
        assert result['accounts_placed'] == 5
        assert result['accounts_failed'] == 0
        assert result['parent_status'] == EQUITY_ORDER_STATUS_PENDING

        splits = splits_by_account(result['order_id'])
        assert len(splits) == 5
        quantities = [splits[account_id].quantity for account_id in world['account_ids']]
        assert quantities == [40, 20, 20, 10, 10]

        for split in splits.values():
            assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
            assert split.broker_order_id
            assert split.attempt_count == 1
            assert split.placed_at is not None

    def test_one_broker_call_per_account_with_cnc_and_no_split_order(self, world, broker):
        place(world, broker)

        placements = [call for call in broker.calls if call['kind'] == 'placeorder']
        assert len(placements) == 5
        assert {call['account_id'] for call in placements} == set(world['account_ids'])
        for call in placements:
            payload = call['payload']
            assert payload['product'] == EQUITY_PRODUCT_CNC
            assert payload['price_type'] == EQUITY_ORDER_TYPE_MARKET
            assert payload['action'] == EQUITY_SIDE_BUY
            assert payload['exchange'] == 'NSE'
            # Rule 5: equity delivery never goes through splitorder.
            assert 'splitsize' not in payload
        assert broker.count('splitorder') == 0

    def test_point_in_time_snapshot_is_written_once(self, world, broker):
        result = place(world, broker)
        splits = splits_by_account(result['order_id'])
        first = splits[world['account_ids'][0]]

        assert first.qty_ratio_at_order == pytest.approx(40.0)
        assert first.ratio_quantity == 40
        assert first.cash_balance_at_order == pytest.approx(PLENTY_OF_CASH)
        assert first.est_value == pytest.approx(4000.0)
        assert first.qty_overridden is False

        # Changing the allocation afterwards must not rewrite the order.
        allocation = EquityAccountAllocation.query.filter_by(
            account_id=first.account_id
        ).first()
        allocation.equity_fund_allocation = 1.0
        db.session.commit()

        db.session.refresh(first)
        assert first.qty_ratio_at_order == pytest.approx(40.0)
        assert first.ratio_quantity == 40

    def test_stop_loss_and_target_are_stored_but_never_sent_to_the_broker(self, world, broker):
        result = place(world, broker, stop_loss=90.0, target=130.0)

        order = EquityOrder.query.get(result['order_id'])
        assert order.stop_loss == pytest.approx(90.0)
        assert order.target == pytest.approx(130.0)

        for call in broker.calls:
            if call['kind'] != 'placeorder':
                continue
            assert 'stoploss' not in call['payload']
            assert 'target' not in call['payload']

    def test_limit_order_carries_its_price(self, world, broker):
        result = place(
            world, broker, order_type=EQUITY_ORDER_TYPE_LIMIT, price=1250.5, reference_price=None
        )
        assert result['status'] == 'success'
        for call in broker.calls_for(world['account_ids'][0], 'placeorder'):
            assert call['payload']['price_type'] == EQUITY_ORDER_TYPE_LIMIT
            assert call['payload']['price'] == pytest.approx(1250.5)


# ---------------------------------------------------------------------------
# Per-account quantity override
# ---------------------------------------------------------------------------

class TestQuantityOverride:
    def test_override_changes_only_that_account(self, world, broker):
        first, second = world['account_ids'][0], world['account_ids'][1]
        result = place(world, broker, quantity_overrides={first: 25})

        splits = splits_by_account(result['order_id'])
        assert splits[first].quantity == 25
        assert splits[first].ratio_quantity == 40
        assert splits[first].qty_overridden is True

        assert splits[second].quantity == 20
        assert splits[second].qty_overridden is False
        assert [splits[account_id].quantity for account_id in world['account_ids']] == \
            [25, 20, 20, 10, 10]

        # The 15 shares the override gave back are reported, not reassigned.
        assert result['leftover_quantity'] == 15
        assert EquityOrder.query.get(result['order_id']).leftover_quantity == 15

    def test_overrides_may_not_exceed_the_total_quantity(self, world, broker):
        result = place(world, broker, quantity_overrides={world['account_ids'][0]: 95})

        assert result['status'] == 'error'
        assert 'more than the total quantity' in result['message']
        assert result['order_id'] is None
        # Nothing was created and nothing was sent.
        assert EquityOrder.query.count() == 0
        assert broker.count('placeorder') == 0

    def test_override_for_an_unselected_account_is_refused(self, world, broker):
        result = place(
            world, broker,
            account_ids=world['account_ids'][:2],
            quantity_overrides={world['account_ids'][4]: 5},
        )
        assert result['status'] == 'error'
        assert 'not part of this order' in result['message']
        assert broker.count('placeorder') == 0


# ---------------------------------------------------------------------------
# Pre-trade validation
# ---------------------------------------------------------------------------

class TestPreTradeValidation:
    def test_one_account_short_of_cash_is_skipped_and_the_rest_proceed(self, world, broker):
        poor = world['account_ids'][1]          # 20 shares at 100 needs 2000
        broker.set_funds(poor, 500.0)

        result = place(world, broker)

        assert result['status'] == 'partial'
        assert result['accounts_placed'] == 4
        assert result['accounts_skipped'] == 1
        assert result['parent_status'] == EQUITY_ORDER_STATUS_PARTIAL

        splits = splits_by_account(result['order_id'])
        assert splits[poor].fill_status == EQUITY_SPLIT_STATUS_SKIPPED
        assert splits[poor].broker_order_id is None
        # quantity is what was actually sent, and nothing was.
        assert splits[poor].quantity == 0
        assert splits[poor].ratio_quantity == 20
        assert 'short of' in splits[poor].error_message

        # No broker call was made for the skipped account, and every other
        # account still got exactly one.
        assert broker.calls_for(poor, 'placeorder') == []
        assert broker.count('placeorder') == 4
        for account_id in world['account_ids']:
            if account_id == poor:
                continue
            assert splits[account_id].broker_order_id

    def test_abort_policy_places_nothing_at_all(self, world, broker):
        broker.set_funds(world['account_ids'][1], 500.0)

        result = place(world, broker, insufficient_funds_action=EQUITY_FUNDS_ACTION_ABORT)

        assert result['status'] == 'error'
        assert result['parent_status'] == EQUITY_ORDER_STATUS_CANCELLED
        assert broker.count('placeorder') == 0

        order = EquityOrder.query.get(result['order_id'])
        assert order.insufficient_funds_action == EQUITY_FUNDS_ACTION_ABORT
        assert 'Aborted before any order was placed' in order.error_message
        assert order.cancelled_at is not None
        splits = splits_by_account(result['order_id'])
        assert all(split.fill_status == EQUITY_SPLIT_STATUS_SKIPPED for split in splits.values())
        assert all(split.quantity == 0 for split in splits.values())

    def test_the_policy_defaults_to_skip_from_the_user_settings(self, world, broker):
        setting = EquitySetting.get_or_create(world['user_id'])
        setting.insufficient_funds_action = EQUITY_FUNDS_ACTION_ABORT
        db.session.commit()

        broker.set_funds(world['account_ids'][1], 500.0)
        result = place(world, broker)

        assert result['parent_status'] == EQUITY_ORDER_STATUS_CANCELLED
        assert broker.count('placeorder') == 0

    def test_a_sell_is_never_blocked_on_cash(self, world, broker):
        for account_id in world['account_ids']:
            broker.set_funds(account_id, 0.0)

        result = place(world, broker, side=EQUITY_SIDE_SELL)

        assert result['accounts_placed'] == 5
        # A sell releases cash, so the funds read is not even made.
        assert broker.count('funds') == 0

    def test_a_market_order_without_a_reference_price_still_places(self, world, broker):
        result = place(world, broker, reference_price=None)

        assert result['accounts_placed'] == 5
        splits = splits_by_account(result['order_id'])
        # The value of the order is unknown, so it is left unknown rather than
        # guessed, and the funds check is not pretended to have happened.
        assert all(split.est_value is None for split in splits.values())

    def test_an_account_with_no_allocation_is_skipped_with_a_reason(self, world, broker):
        idle = world['account_ids'][4]
        allocation = EquityAccountAllocation.query.filter_by(account_id=idle).first()
        allocation.equity_fund_allocation = 0.0
        db.session.commit()

        result = place(world, broker)

        splits = splits_by_account(result['order_id'])
        assert splits[idle].fill_status == EQUITY_SPLIT_STATUS_SKIPPED
        assert 'No equity allocation' in splits[idle].error_message
        assert broker.calls_for(idle, 'placeorder') == []


# ---------------------------------------------------------------------------
# Failure isolation, retries and the timeout rule
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_one_broker_rejecting_leaves_the_others_placed(self, world, broker):
        bad = world['account_ids'][2]
        broker.script(bad, 'placeorder', rejection('Symbol is not tradable today'))

        result = place(world, broker)

        assert result['status'] == 'partial'
        assert result['accounts_placed'] == 4
        assert result['parent_status'] == EQUITY_ORDER_STATUS_PARTIAL

        splits = splits_by_account(result['order_id'])
        assert splits[bad].fill_status == EQUITY_SPLIT_STATUS_REJECTED
        assert splits[bad].broker_order_id is None
        assert 'not tradable' in splits[bad].error_message
        for account_id in world['account_ids']:
            if account_id == bad:
                continue
            assert splits[account_id].fill_status == EQUITY_SPLIT_STATUS_PENDING
            assert splits[account_id].broker_order_id

    def test_a_definite_rejection_is_retried(self, world, broker):
        flaky = world['account_ids'][3]
        broker.script(flaky, 'placeorder', rejection('Momentary broker error'), success('ORD-RETRY'))

        result = place(world, broker)

        assert result['accounts_placed'] == 5
        assert len(broker.calls_for(flaky, 'placeorder')) == 2

        splits = splits_by_account(result['order_id'])
        assert splits[flaky].fill_status == EQUITY_SPLIT_STATUS_PENDING
        assert splits[flaky].broker_order_id == 'ORD-RETRY'
        assert splits[flaky].attempt_count == 2

    def test_a_rejection_that_keeps_rejecting_stops_at_max_attempts(self, world, broker):
        bad = world['account_ids'][0]
        broker.script(bad, 'placeorder', rejection())

        result = place(world, broker, max_attempts=3)

        assert len(broker.calls_for(bad, 'placeorder')) == 3
        splits = splits_by_account(result['order_id'])
        assert splits[bad].fill_status == EQUITY_SPLIT_STATUS_REJECTED
        assert splits[bad].attempt_count == 3
        assert splits[bad].is_safe_to_retry is True

    @pytest.mark.parametrize('response,expected_type', [
        (timeout(), 'timeout_error'),
        (connection_error(), 'connection_error'),
    ])
    def test_a_timeout_is_never_retried_and_lands_indeterminate(self, world, broker,
                                                                response, expected_type):
        lost = world['account_ids'][1]
        broker.script(lost, 'placeorder', response)

        result = place(world, broker, max_attempts=3)

        # THE rule: an indeterminate outcome is sent exactly once. The order may
        # already be at the broker, and a second send is how it gets bought twice.
        assert len(broker.calls_for(lost, 'placeorder')) == 1

        splits = splits_by_account(result['order_id'])
        assert splits[lost].fill_status == EQUITY_SPLIT_STATUS_INDETERMINATE
        assert splits[lost].error_type == expected_type
        assert splits[lost].attempt_count == 1
        assert splits[lost].is_safe_to_retry is False
        assert splits[lost].is_terminal is True

        # Every other account is untouched by it.
        assert result['accounts_placed'] == 4
        assert result['accounts_indeterminate'] == 1
        assert result['parent_status'] == EQUITY_ORDER_STATUS_PARTIAL

    def test_success_without_an_order_id_is_treated_as_indeterminate(self, world, broker):
        nameless = world['account_ids'][0]
        broker.script(nameless, 'placeorder', {'status': 'success'})

        result = place(world, broker, max_attempts=3)

        assert len(broker.calls_for(nameless, 'placeorder')) == 1
        splits = splits_by_account(result['order_id'])
        assert splits[nameless].fill_status == EQUITY_SPLIT_STATUS_INDETERMINATE
        assert 'no order id' in splits[nameless].error_message

    def test_a_client_that_raises_is_indeterminate_not_a_rejection(self, world, broker):
        exploding = world['account_ids'][2]

        def blow_up(account_id, kind, payload):
            if account_id == exploding and kind == 'placeorder':
                raise RuntimeError('socket closed mid request')

        broker.before_call = blow_up

        result = place(world, broker, max_attempts=3)

        splits = splits_by_account(result['order_id'])
        assert splits[exploding].fill_status == EQUITY_SPLIT_STATUS_INDETERMINATE
        assert splits[exploding].attempt_count == 1
        assert result['accounts_placed'] == 4

    def test_a_placed_order_is_never_rolled_back_by_another_accounts_failure(self, world, broker):
        broker.script(world['account_ids'][0], 'placeorder', rejection())
        broker.script(world['account_ids'][1], 'placeorder', timeout())
        broker.script(world['account_ids'][2], 'placeorder', rejection())

        result = place(world, broker)

        splits = splits_by_account(result['order_id'])
        survivors = [
            split for split in splits.values()
            if split.fill_status == EQUITY_SPLIT_STATUS_PENDING
        ]
        assert len(survivors) == 2
        assert all(split.broker_order_id for split in survivors)


# ---------------------------------------------------------------------------
# The round-down leftover
# ---------------------------------------------------------------------------

class TestLeftover:
    def test_the_leftover_is_reported_and_not_silently_dropped(self, world, broker):
        # 33 shares at 40/20/20/10/10 percent gives 13, 6, 6, 3, 3 after each
        # account is rounded DOWN, so 2 shares belong to nobody.
        result = place(world, broker, total_quantity=33)

        splits = splits_by_account(result['order_id'])
        quantities = [splits[account_id].quantity for account_id in world['account_ids']]
        assert quantities == [13, 6, 6, 3, 3]
        assert sum(quantities) == 31

        assert result['ratio_leftover'] == 2
        assert result['leftover_quantity'] == 2
        assert EquityOrder.query.get(result['order_id']).leftover_quantity == 2
        assert '2 shares left over' in result['message']

    def test_the_preview_reports_the_same_leftover_before_anything_is_placed(self, world, broker):
        preview = engine.preview_order_split(
            user_id=world['user_id'],
            symbol='RELIANCE',
            exchange='NSE',
            side=EQUITY_SIDE_BUY,
            total_quantity=33,
            account_ids=world['account_ids'],
            reference_price=100.0,
            client_factory=broker.factory,
        )

        assert preview['status'] == 'success'
        assert preview['leftover_quantity'] == 2
        assert [row['quantity'] for row in preview['rows']] == [13, 6, 6, 3, 3]
        assert preview['accounts_flagged'] == 0
        # A preview never places anything.
        assert broker.count('placeorder') == 0
        assert EquityOrder.query.count() == 0

    def test_the_preview_flags_an_account_that_cannot_cover_its_share(self, world, broker):
        poor = world['account_ids'][0]
        broker.set_funds(poor, 100.0)

        preview = engine.preview_order_split(
            user_id=world['user_id'],
            symbol='RELIANCE',
            exchange='NSE',
            side=EQUITY_SIDE_BUY,
            total_quantity=100,
            account_ids=world['account_ids'],
            reference_price=100.0,
            client_factory=broker.factory,
        )

        flagged = [row for row in preview['rows'] if not row['check_ok']]
        assert len(flagged) == 1
        assert flagged[0]['account_id'] == poor
        assert flagged[0]['required_cash'] == pytest.approx(4000.0)
        assert flagged[0]['cash_balance'] == pytest.approx(100.0)
        assert preview['accounts_flagged'] == 1


# ---------------------------------------------------------------------------
# GTT
# ---------------------------------------------------------------------------

class TestGTT:
    def test_a_broker_without_gtt_fails_alone_while_the_others_proceed(self, world, broker):
        no_gtt = world['account_ids'][2]
        broker.script(no_gtt, 'placegttorder', gtt_not_supported('upstox'))

        result = place(
            world, broker,
            order_type=EQUITY_ORDER_TYPE_GTT,
            price=1200.0,
            trigger_price=1190.0,
            reference_price=None,
            max_attempts=3,
        )

        assert result['status'] == 'partial'
        assert result['accounts_placed'] == 4
        assert result['accounts_unsupported'] == 1

        splits = splits_by_account(result['order_id'])
        assert splits[no_gtt].fill_status == EQUITY_SPLIT_STATUS_UNSUPPORTED
        assert 'does not support GTT' in splits[no_gtt].error_message
        # Permanent, so it is not retried.
        assert len(broker.calls_for(no_gtt, 'placegttorder')) == 1

        for account_id in world['account_ids']:
            if account_id == no_gtt:
                continue
            assert splits[account_id].fill_status == EQUITY_SPLIT_STATUS_PENDING
            assert splits[account_id].broker_gtt_id
            # The trigger id is kept apart from broker_order_id, which the
            # order the GTT eventually fires will use.
            assert splits[account_id].broker_order_id is None

    def test_the_gtt_payload_is_the_flat_shape_openalgo_expects(self, world, broker):
        place(
            world, broker,
            order_type=EQUITY_ORDER_TYPE_GTT,
            side=EQUITY_SIDE_SELL,
            price=1200.0,
            trigger_price=1190.0,
            reference_price=None,
        )

        call = broker.calls_for(world['account_ids'][0], 'placegttorder')[0]
        payload = call['payload']
        assert payload['trigger_type'] == 'SINGLE'
        assert payload['product'] == EQUITY_PRODUCT_CNC
        assert payload['price'] == pytest.approx(1200.0)
        # Exactly one of the two trigger slots carries the level.
        assert payload['triggerprice_sl'] == pytest.approx(1190.0)
        assert payload['triggerprice_tg'] == 0.0
        assert payload['apikey']

    def test_a_gtt_without_a_trigger_price_is_refused_before_anything_is_created(self, world, broker):
        result = place(
            world, broker, order_type=EQUITY_ORDER_TYPE_GTT, price=1200.0, reference_price=None
        )
        assert result['status'] == 'error'
        assert 'trigger price' in result['message']
        assert EquityOrder.query.count() == 0
        assert broker.count('placegttorder') == 0


# ---------------------------------------------------------------------------
# Ownership scoping
# ---------------------------------------------------------------------------

class TestOwnership:
    def test_an_account_belonging_to_somebody_else_is_refused(self, world, broker):
        stranger = User(username='stranger', email='stranger@example.com')
        stranger.set_password('EngineTest#2')
        db.session.add(stranger)
        db.session.commit()

        other = TradingAccount(
            user_id=stranger.id,
            account_name='not mine',
            broker_name='dhan',
            host_url='http://127.0.0.1:5999',
            websocket_url='ws://127.0.0.1:8999',
            is_active=True,
        )
        other.set_api_key('someone-elses-key')
        db.session.add(other)
        db.session.commit()

        result = place(world, broker, account_ids=[other.id])

        assert result['status'] == 'error'
        assert 'not available for this user' in result['message']
        assert broker.count('placeorder') == 0

    def test_an_inactive_account_stops_the_instruction(self, world, broker):
        world['accounts'][1].is_active = False
        db.session.commit()

        result = place(world, broker)

        assert result['status'] == 'error'
        assert 'inactive' in result['message']
        assert broker.count('placeorder') == 0


# ---------------------------------------------------------------------------
# Modify and cancel
# ---------------------------------------------------------------------------

def _holding(world, account_index=0, **overrides):
    account = world['accounts'][account_index]
    values = dict(
        user_id=world['user_id'],
        account_id=account.id,
        symbol='RELIANCE',
        exchange='NSE',
        quantity=40,
        avg_cost=1200.0,
        pledged_quantity=0,
        exit_mode=EQUITY_EXIT_MODE_AUTO,
        exit_status=EQUITY_HOLDING_STATUS_ACTIVE,
    )
    values.update(overrides)
    holding = EquityHolding(**values)
    db.session.add(holding)
    db.session.commit()
    return holding


class TestModifyAndCancel:
    def test_cancel_marks_every_account_cancelled_and_rolls_the_parent_up(self, world, broker):
        result = place(world, broker)

        cancelled = engine.cancel_order(
            world['user_id'], result['order_id'], client_factory=broker.factory
        )

        assert cancelled['status'] == 'success'
        assert cancelled['accounts_ok'] == 5
        assert cancelled['parent_status'] == EQUITY_ORDER_STATUS_CANCELLED

        splits = splits_by_account(result['order_id'])
        assert all(
            split.fill_status == EQUITY_SPLIT_STATUS_CANCELLED for split in splits.values()
        )
        order = EquityOrder.query.get(result['order_id'])
        assert order.cancelled_at is not None

    def test_one_cancel_failing_leaves_the_other_accounts_cancelled(self, world, broker):
        result = place(world, broker)
        stubborn = world['account_ids'][1]
        broker.script(stubborn, 'cancelorder', rejection('Order is already complete'))

        cancelled = engine.cancel_order(
            world['user_id'], result['order_id'], client_factory=broker.factory
        )

        assert cancelled['status'] == 'partial'
        assert cancelled['accounts_ok'] == 4
        splits = splits_by_account(result['order_id'])
        assert splits[stubborn].fill_status == EQUITY_SPLIT_STATUS_PENDING
        assert 'already complete' in splits[stubborn].error_message
        assert cancelled['parent_status'] == EQUITY_ORDER_STATUS_PARTIAL

    def test_a_cancel_that_times_out_does_not_pretend_the_order_is_gone(self, world, broker):
        result = place(world, broker)
        lost = world['account_ids'][0]
        broker.script(lost, 'cancelorder', timeout())

        cancelled = engine.cancel_order(
            world['user_id'], result['order_id'], client_factory=broker.factory
        )

        assert cancelled['accounts_indeterminate'] == 1
        splits = splits_by_account(result['order_id'])
        # We do not know whether that order is still live, so its status is
        # left exactly as it was and the reason is recorded.
        assert splits[lost].fill_status == EQUITY_SPLIT_STATUS_PENDING
        assert splits[lost].error_type == 'timeout_error'
        # And it was not retried.
        assert len(broker.calls_for(lost, 'cancelorder')) == 1

    def test_modify_sends_the_new_price_to_every_open_account(self, world, broker):
        result = place(world, broker, order_type=EQUITY_ORDER_TYPE_LIMIT,
                       price=1200.0, reference_price=None)

        modified = engine.modify_order(
            world['user_id'], result['order_id'], price=1210.0, client_factory=broker.factory
        )

        assert modified['status'] == 'success'
        assert modified['accounts_ok'] == 5
        for account_id in world['account_ids']:
            call = broker.calls_for(account_id, 'modifyorder')[0]
            assert call['payload']['price'] == pytest.approx(1210.0)
            assert call['payload']['product'] == EQUITY_PRODUCT_CNC
        assert EquityOrder.query.get(result['order_id']).price == pytest.approx(1210.0)

    def test_modify_resplits_a_new_total_on_the_recorded_ratios(self, world, broker):
        result = place(world, broker, order_type=EQUITY_ORDER_TYPE_LIMIT,
                       price=1200.0, reference_price=None)

        # The allocations move after the order. The re-split must ignore that.
        for allocation in EquityAccountAllocation.query.all():
            allocation.equity_fund_allocation = 1.0 * LAKH
        db.session.commit()

        modified = engine.modify_order(
            world['user_id'], result['order_id'], total_quantity=50,
            client_factory=broker.factory
        )

        assert modified['status'] == 'success'
        splits = splits_by_account(result['order_id'])
        assert [splits[account_id].quantity for account_id in world['account_ids']] == \
            [20, 10, 10, 5, 5]
        assert [splits[account_id].ratio_quantity for account_id in world['account_ids']] == \
            [40, 20, 20, 10, 10]

    def test_a_completed_order_cannot_be_modified_or_cancelled(self, world, broker):
        result = place(world, broker)
        order = EquityOrder.query.get(result['order_id'])
        order.status = 'COMPLETED'
        db.session.commit()

        modified = engine.modify_order(
            world['user_id'], result['order_id'], price=10.0, client_factory=broker.factory
        )
        cancelled = engine.cancel_order(
            world['user_id'], result['order_id'], client_factory=broker.factory
        )

        assert modified['status'] == 'error'
        assert 'Only a PENDING or PARTIAL order' in modified['message']
        assert cancelled['status'] == 'error'
        assert broker.count('modifyorder') == 0
        assert broker.count('cancelorder') == 0

    def test_another_users_order_cannot_be_cancelled(self, world, broker):
        result = place(world, broker)
        stranger = User(username='stranger2', email='stranger2@example.com')
        stranger.set_password('EngineTest#3')
        db.session.add(stranger)
        db.session.commit()

        cancelled = engine.cancel_order(
            stranger.id, result['order_id'], client_factory=broker.factory
        )

        assert cancelled['status'] == 'error'
        assert 'not available for this user' in cancelled['message']
        assert broker.count('cancelorder') == 0

    def test_a_gtt_is_cancelled_through_the_gtt_endpoint(self, world, broker):
        result = place(
            world, broker, order_type=EQUITY_ORDER_TYPE_GTT, price=1200.0,
            trigger_price=1190.0, reference_price=None,
        )

        cancelled = engine.cancel_order(
            world['user_id'], result['order_id'], client_factory=broker.factory
        )

        assert cancelled['status'] == 'success'
        assert broker.count('cancelgttorder') == 5
        assert broker.count('cancelorder') == 0
        call = broker.calls_for(world['account_ids'][0], 'cancelgttorder')[0]
        assert call['payload']['trigger_id'] == 'GTT-%s' % world['account_ids'][0]


# ---------------------------------------------------------------------------
# The exit path and the claim
# ---------------------------------------------------------------------------

class TestExitHolding:
    def test_a_clean_exit_claims_then_places_and_links_everything_up(self, world, broker):
        holding = _holding(world)

        result = engine.exit_holding(
            world['user_id'], holding.id, reason=EQUITY_EXIT_REASON_STOP_LOSS,
            client_factory=broker.factory, retry_delay_seconds=0,
        )

        assert result['status'] == 'success'
        assert result['quantity'] == 40
        assert result['broker_order_id']

        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
        assert holding.exit_broker_order_id == result['broker_order_id']
        assert holding.exit_split_id == result['split_id']
        assert holding.exit_quantity == 40

        order = EquityOrder.query.get(result['order_id'])
        assert order.side == EQUITY_SIDE_SELL
        assert order.source == EQUITY_ORDER_SOURCE_STOP_LOSS
        assert order.product == EQUITY_PRODUCT_CNC

        call = broker.calls_for(holding.account_id, 'placeorder')[0]
        assert call['payload']['action'] == EQUITY_SIDE_SELL
        assert call['payload']['quantity'] == 40
        assert call['payload']['product'] == EQUITY_PRODUCT_CNC

    def test_pledged_shares_are_never_sold(self, world, broker):
        holding = _holding(world, quantity=40, pledged_quantity=15)

        result = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )

        assert result['quantity'] == 25
        assert broker.calls_for(holding.account_id, 'placeorder')[0]['payload']['quantity'] == 25

    def test_the_claim_is_committed_before_the_broker_is_called(self, world, broker):
        """
        The claim has to be VISIBLE to another worker before the order goes out,
        not merely written in an uncommitted session. The probe runs while the
        fake broker is mid call and reads the row through a fresh query.
        """
        holding = _holding(world)
        seen = {}

        def probe(account_id, kind, payload):
            if kind != 'placeorder':
                return
            db.session.expire_all()
            row = EquityHolding.query.get(holding.id)
            seen['status'] = row.exit_status
            seen['quantity'] = row.exit_quantity

        broker.before_call = probe

        engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )

        assert seen['status'] == 'EXIT_PENDING'
        assert seen['quantity'] == 40

    def test_two_concurrent_exits_on_one_holding_produce_exactly_one_broker_call(
            self, ctx, world, broker):
        """
        The race the claim exists for: the stop loss monitor and a manual Sell
        both decide to exit the same holding.

        The second attempt is deliberately released the moment the first is
        INSIDE its broker call, which is the exact window the claim has to
        cover. That is what makes the assertion below deterministic rather than
        a coin toss: whatever the scheduler does, only one thread may ever get
        as far as the broker.
        """
        holding = _holding(world)
        app = ctx
        inside_broker_call = threading.Event()
        second_attempt_done = threading.Event()
        results = {}

        def hold_the_call(account_id, kind, payload):
            if kind != 'placeorder':
                return
            inside_broker_call.set()
            # Do not leave the first thread parked for ever if the second one
            # dies before it can signal.
            second_attempt_done.wait(timeout=10)

        broker.before_call = hold_the_call

        def attempt(name, wait_for_first):
            with app.app_context():
                try:
                    if wait_for_first:
                        inside_broker_call.wait(timeout=10)
                    results[name] = engine.exit_holding(
                        world['user_id'], holding.id,
                        reason=EQUITY_EXIT_REASON_MANUAL,
                        client_factory=broker.factory,
                        retry_delay_seconds=0,
                    )
                finally:
                    if wait_for_first:
                        second_attempt_done.set()
                    db.session.remove()

        monitor = threading.Thread(target=attempt, args=('monitor', False))
        manual = threading.Thread(target=attempt, args=('manual', True))
        monitor.start()
        manual.start()
        monitor.join(timeout=20)
        manual.join(timeout=20)

        assert not monitor.is_alive() and not manual.is_alive()

        # ONE broker call. This is the whole point of the claim.
        assert broker.count('placeorder') == 1

        statuses = sorted(result['status'] for result in results.values())
        assert statuses == ['skipped', 'success']

        loser = next(r for r in results.values() if r['status'] == 'skipped')
        assert 'exit' in loser['message'].lower()

        db.session.expire_all()
        assert EquityHolding.query.get(holding.id).exit_status == \
            EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
        # Exactly one sell order exists for that holding.
        assert EquityOrder.query.filter_by(side=EQUITY_SIDE_SELL).count() == 1

    def test_an_exit_already_in_flight_is_refused_without_a_broker_call(self, world, broker):
        holding = _holding(world)
        first = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )
        assert first['status'] == 'success'

        second = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )

        assert second['status'] == 'skipped'
        assert second['claimed'] is False
        assert broker.count('placeorder') == 1

    def test_a_definite_refusal_releases_the_claim(self, world, broker):
        holding = _holding(world)
        broker.script(holding.account_id, 'placeorder', rejection('Scrip is under ban'))

        result = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0, sleeper=lambda _s: None,
        )

        assert result['status'] == 'error'
        db.session.refresh(holding)
        # The claim is given back, because nothing is live at the broker.
        assert holding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
        assert holding.exit_broker_order_id is None
        assert 'under ban' in holding.exit_error

        # So the holding can be exited again.
        broker.script(holding.account_id, 'placeorder', success('ORD-SECOND'))
        again = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )
        assert again['status'] == 'success'

    def test_an_indeterminate_exit_keeps_the_claim_for_ever(self, world, broker):
        holding = _holding(world)
        broker.script(holding.account_id, 'placeorder', timeout())

        result = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0, max_attempts=3,
        )

        assert result['status'] == 'indeterminate'
        assert result['indeterminate'] is True
        # Sent once and once only.
        assert broker.count('placeorder') == 1

        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE
        assert holding.exit_quantity == 40
        assert 'unknown' in holding.exit_error.lower()

        # And nothing will sell it again until a human reconciles it.
        retry = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )
        assert retry['status'] == 'skipped'
        assert broker.count('placeorder') == 1

    def test_an_approval_may_only_fire_on_a_holding_that_was_actually_alerted(self, world, broker):
        holding = _holding(world)

        refused = engine.exit_holding(
            world['user_id'], holding.id,
            allow_from=(EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,),
            client_factory=broker.factory, retry_delay_seconds=0,
        )
        assert refused['status'] == 'skipped'
        assert broker.count('placeorder') == 0

        holding.exit_status = EQUITY_HOLDING_STATUS_AWAITING_CONFIRM
        db.session.commit()

        approved = engine.exit_holding(
            world['user_id'], holding.id,
            allow_from=(EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,),
            client_factory=broker.factory, retry_delay_seconds=0,
        )
        assert approved['status'] == 'success'

    def test_another_users_holding_cannot_be_exited(self, world, broker):
        holding = _holding(world)
        stranger = User(username='stranger3', email='stranger3@example.com')
        stranger.set_password('EngineTest#4')
        db.session.add(stranger)
        db.session.commit()

        result = engine.exit_holding(
            stranger.id, holding.id, client_factory=broker.factory, retry_delay_seconds=0
        )

        assert result['status'] == 'skipped'
        assert broker.count('placeorder') == 0
        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE

    def test_exiting_several_holdings_isolates_their_failures(self, world, broker):
        holdings = [_holding(world, account_index=index) for index in range(3)]
        broker.script(holdings[1].account_id, 'placeorder', rejection('No holdings'))

        result = engine.exit_holdings(
            world['user_id'], [holding.id for holding in holdings],
            client_factory=broker.factory, retry_delay_seconds=0,
            sleeper=lambda _s: None,
        )

        assert result['status'] == 'partial'
        assert result['placed'] == 2
        assert result['failed'] == 1

        db.session.expire_all()
        assert EquityHolding.query.get(holdings[0].id).exit_status == \
            EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
        assert EquityHolding.query.get(holdings[1].id).exit_status == \
            EQUITY_HOLDING_STATUS_ACTIVE
        assert EquityHolding.query.get(holdings[2].id).exit_status == \
            EQUITY_HOLDING_STATUS_EXIT_SUBMITTED


# ---------------------------------------------------------------------------
# Bookkeeping under a failing database
# ---------------------------------------------------------------------------

class TestBookkeepingFailures:
    def test_a_failed_bulk_write_records_each_order_on_its_own(self, world, broker, monkeypatch):
        """
        The orders are already at the broker by the time the results are
        written. One bad row must not lose the other four broker order ids.
        """
        result = place(world, broker)
        order = EquityOrder.query.get(result['order_id'])
        splits = splits_by_account(order.id)
        outcomes = [
            {
                'account_id': account_id,
                'ok': True,
                'fill_status': EQUITY_SPLIT_STATUS_PENDING,
                'broker_order_id': 'RECOVERED-%s' % account_id,
                'broker_gtt_id': None,
                'error_message': None,
                'error_type': None,
                'broker_order_status': 'open',
                'attempts': 1,
                'placed_at': None,
            }
            for account_id in world['account_ids']
        ]

        real_commit = db.session.commit
        attempts = {'count': 0}

        def flaky_commit():
            attempts['count'] += 1
            if attempts['count'] == 1:
                raise RuntimeError('database is locked')
            return real_commit()

        monkeypatch.setattr(db.session, 'commit', flaky_commit)
        engine._persist_outcomes(order, splits, outcomes)
        monkeypatch.undo()

        db.session.expire_all()
        written = splits_by_account(order.id)
        for account_id in world['account_ids']:
            assert written[account_id].broker_order_id == 'RECOVERED-%s' % account_id

    def test_a_write_failure_before_the_send_gives_the_claim_back(self, world, broker, monkeypatch):
        holding = _holding(world)

        def explode(**_kwargs):
            raise RuntimeError('disk full')

        monkeypatch.setattr(engine, '_create_parent_order', explode)

        result = engine.exit_holding(
            world['user_id'], holding.id, client_factory=broker.factory,
            retry_delay_seconds=0,
        )
        monkeypatch.undo()

        assert result['status'] == 'error'
        # Nothing was sent, so nothing can be duplicated.
        assert broker.count('placeorder') == 0
        db.session.expire_all()
        reloaded = EquityHolding.query.get(holding.id)
        assert reloaded.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
        assert reloaded.exit_broker_order_id is None


# ---------------------------------------------------------------------------
# Parent status roll-up
# ---------------------------------------------------------------------------

class TestParentStatusRollup:
    def _set(self, order_id, statuses):
        splits = EquityOrderSplit.query.filter_by(
            equity_order_id=order_id
        ).order_by(EquityOrderSplit.account_id).all()
        for split, status in zip(splits, statuses):
            split.fill_status = status
        db.session.commit()

    def test_all_filled_is_completed(self, world, broker):
        result = place(world, broker)
        self._set(result['order_id'], ['COMPLETED'] * 5)
        assert engine.recompute_parent_status(result['order_id'], world['user_id']) == 'COMPLETED'
        # And the roll-up is ownership scoped like everything else.
        assert engine.recompute_parent_status(result['order_id'], 9999) is None

    def test_all_open_is_pending(self, world, broker):
        result = place(world, broker)
        assert engine.recompute_parent_status(result['order_id'], world['user_id']) == \
            EQUITY_ORDER_STATUS_PENDING

    def test_some_filled_and_some_failed_is_partial_with_a_reason(self, world, broker):
        result = place(world, broker)
        self._set(result['order_id'], [
            'COMPLETED', 'COMPLETED', 'COMPLETED', 'COMPLETED', EQUITY_SPLIT_STATUS_REJECTED,
        ])
        assert engine.recompute_parent_status(result['order_id'], world['user_id']) == \
            EQUITY_ORDER_STATUS_PARTIAL

        order = EquityOrder.query.get(result['order_id'])
        counts = engine.summarise_splits(order.splits.all())
        assert counts['filled'] == 4
        assert counts['failed'] == 1
        assert counts['reason'] == '1 failed'

    def test_all_cancelled_is_cancelled(self, world, broker):
        result = place(world, broker)
        self._set(result['order_id'], [EQUITY_SPLIT_STATUS_CANCELLED] * 5)
        assert engine.recompute_parent_status(result['order_id'], world['user_id']) == \
            EQUITY_ORDER_STATUS_CANCELLED

    def test_every_account_failing_is_partial_not_cancelled(self, world, broker):
        result = place(world, broker)
        self._set(result['order_id'], [EQUITY_SPLIT_STATUS_REJECTED] * 5)
        # Nobody cancelled this order, it simply did not work.
        assert engine.recompute_parent_status(result['order_id'], world['user_id']) == \
            EQUITY_ORDER_STATUS_PARTIAL


# ---------------------------------------------------------------------------
# Module level invariants
# ---------------------------------------------------------------------------

class TestModuleInvariants:
    def test_no_other_equity_file_writes_to_a_broker(self):
        """
        This engine owns every equity broker write. If another equity file ever
        calls placeorder, modifyorder, cancelorder, splitorder or a GTT
        endpoint, the safety rules stop being auditable in one place.

        Matched on the parse tree rather than on the text, so a docstring that
        describes what the engine does is not mistaken for a call that does it.
        """
        forbidden_calls = {
            'placeorder', 'modifyorder', 'cancelorder', 'splitorder',
            'placesmartorder', 'basketorder', 'closeposition', 'cancelallorder',
            'place_order_with_freeze_check',
        }
        forbidden_endpoints = {'placegttorder', 'modifygttorder', 'cancelgttorder'}

        candidates = list((REPO_ROOT / 'app' / 'equity').glob('*.py'))
        candidates += [
            path for path in (REPO_ROOT / 'app' / 'utils').glob('equity_*.py')
            if path.name != 'equity_order_engine.py'
        ]

        offenders = []
        for path in candidates:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                    if name in forbidden_calls:
                        offenders.append('%s calls %s' % (path.name, name))
                    # A raw endpoint posted through _make_request is a write too.
                    for argument in node.args:
                        if (isinstance(argument, ast.Constant)
                                and argument.value in forbidden_endpoints):
                            offenders.append('%s posts %s' % (path.name, argument.value))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, 'module', '') or ''
                    names = [alias.name for alias in node.names]
                    if 'freeze_quantity_handler' in module or \
                            any('freeze_quantity_handler' in name for name in names):
                        offenders.append('%s imports the freeze quantity handler' % path.name)
        assert offenders == [], offenders

    def test_the_engine_never_sends_an_fno_product(self):
        text = (REPO_ROOT / 'app' / 'utils' / 'equity_order_engine.py').read_text(encoding='utf-8')
        assert "'NRML'" not in text
        assert "'MIS'" not in text
        assert 'EQUITY_PRODUCT_CNC' in text

    def test_the_client_factory_is_the_only_way_a_client_is_built(self):
        text = (REPO_ROOT / 'app' / 'utils' / 'equity_order_engine.py').read_text(encoding='utf-8')
        assert text.count('ExtendedOpenAlgoAPI(') == 1, \
            'a broker client must only ever be constructed in default_client_factory'
