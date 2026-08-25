"""
Unit tests for the equity ratio engine (app/utils/equity_ratio.py).

The engine is loaded straight from its file path rather than with
"from app.utils.equity_ratio import ...", because importing the app package would
pull in Flask and SQLAlchemy. Loading by path keeps these tests runnable on any
machine and doubles as proof that the engine really is standalone.
"""

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "equity_ratio.py"

ALLOWED_IMPORTS = {"__future__", "math", "dataclasses", "typing"}


def _load_engine(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because dataclasses resolves the postponed
    # annotations of a frozen dataclass through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


equity_ratio = _load_engine("equity_ratio_engine", ENGINE_PATH)

compute_order_qty_ratios = equity_ratio.compute_order_qty_ratios
split_quantity_by_ratio = equity_ratio.split_quantity_by_ratio
invested_percent = equity_ratio.invested_percent
stake_percent = equity_ratio.stake_percent
stake_percent_for_view = equity_ratio.stake_percent_for_view
stake_denominator = equity_ratio.stake_denominator
stock_at_cost = equity_ratio.stock_at_cost
total_allocation = equity_ratio.total_allocation

LAKH = 100_000.0

# Allocation set from the approved mockups, 50L in total.
MOCKUP_ALLOCATIONS = {
    "mps": 20.00 * LAKH,
    "sathya": 10.00 * LAKH,
    "patsen": 10.00 * LAKH,
    "Unicorp": 5.00 * LAKH,
    "suji": 5.00 * LAKH,
}


def test_engine_imports_nothing_from_flask_sqlalchemy_or_app():
    """The engine must stay pure Python so it can be tested without the app."""
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED_IMPORTS, "unexpected imports: %s" % sorted(imported - ALLOWED_IMPORTS)


class TestOrderQtyRatio:
    def test_mockup_allocation_set_gives_expected_ratios(self):
        ratios = compute_order_qty_ratios(MOCKUP_ALLOCATIONS)
        assert ratios["mps"] == pytest.approx(40.0)
        assert ratios["sathya"] == pytest.approx(20.0)
        assert ratios["patsen"] == pytest.approx(20.0)
        assert ratios["Unicorp"] == pytest.approx(10.0)
        assert ratios["suji"] == pytest.approx(10.0)

    def test_ratios_total_one_hundred_percent(self):
        ratios = compute_order_qty_ratios(MOCKUP_ALLOCATIONS)
        assert sum(ratios.values()) == pytest.approx(100.0)

    def test_ratio_recomputes_after_an_allocation_changes(self):
        changed = dict(MOCKUP_ALLOCATIONS)
        changed["mps"] = 30.00 * LAKH
        ratios = compute_order_qty_ratios(changed)
        assert total_allocation(changed) == pytest.approx(60.00 * LAKH)
        assert ratios["mps"] == pytest.approx(50.0)
        assert ratios["sathya"] == pytest.approx(100.0 / 6.0)
        assert sum(ratios.values()) == pytest.approx(100.0)

    def test_zero_total_allocation_yields_all_zero_ratios(self):
        ratios = compute_order_qty_ratios({"mps": 0.0, "suji": 0.0})
        assert ratios == {"mps": 0.0, "suji": 0.0}

    def test_one_account_with_zero_allocation_gets_zero_and_others_still_total_one_hundred(self):
        allocations = {"mps": 20.00 * LAKH, "suji": 0.0, "patsen": 20.00 * LAKH}
        ratios = compute_order_qty_ratios(allocations)
        assert ratios["suji"] == 0.0
        assert ratios["mps"] == pytest.approx(50.0)
        assert ratios["patsen"] == pytest.approx(50.0)
        assert sum(ratios.values()) == pytest.approx(100.0)

    def test_empty_account_set_returns_empty_mapping(self):
        assert compute_order_qty_ratios({}) == {}
        assert total_allocation({}) == 0.0

    def test_single_account_takes_the_whole_ratio(self):
        assert compute_order_qty_ratios({"mps": 7.5 * LAKH}) == {"mps": pytest.approx(100.0)}

    def test_missing_allocation_is_treated_as_zero(self):
        ratios = compute_order_qty_ratios({"mps": 10.0 * LAKH, "suji": None})
        assert ratios["suji"] == 0.0
        assert ratios["mps"] == pytest.approx(100.0)


class TestSplitQuantityByRatio:
    def test_splits_by_ratio_with_default_lot_size_of_one(self):
        ratios = compute_order_qty_ratios(MOCKUP_ALLOCATIONS)
        split = split_quantity_by_ratio(1000, ratios)
        assert split.quantities == {
            "mps": 400,
            "sathya": 200,
            "patsen": 200,
            "Unicorp": 100,
            "suji": 100,
        }
        assert split.allocated == 1000
        assert split.leftover == 0

    def test_rounds_down_to_the_lot_and_reports_the_leftover(self):
        ratios = {"mps": 40.0, "sathya": 30.0, "patsen": 30.0}
        lot_sizes = {"mps": 25, "sathya": 25, "patsen": 25}
        split = split_quantity_by_ratio(100, ratios, lot_sizes)
        # 40, 30 and 30 shares each round DOWN to a single 25 share lot.
        assert split.quantities == {"mps": 25, "sathya": 25, "patsen": 25}
        assert split.allocated == 75
        assert split.leftover == 25

    def test_lot_size_larger_than_the_share_allocates_nothing_to_that_account(self):
        split = split_quantity_by_ratio(100, {"mps": 90.0, "suji": 10.0}, {"mps": 1, "suji": 50})
        assert split.quantities == {"mps": 90, "suji": 0}
        assert split.leftover == 10

    def test_per_account_lot_sizes_are_applied_independently(self):
        ratios = {"mps": 50.0, "suji": 50.0}
        split = split_quantity_by_ratio(101, ratios, {"mps": 7, "suji": 1})
        # mps share 50.5 floors to 50 shares, then down to 49 (7 lots of 7).
        assert split.quantities == {"mps": 49, "suji": 50}
        assert split.leftover == 2

    def test_fractional_total_quantity_rounds_down_before_splitting(self):
        split = split_quantity_by_ratio(10.9, {"mps": 100.0})
        assert split.quantities == {"mps": 10}
        assert split.leftover == 0

    def test_zero_quantity_yields_zero_everywhere(self):
        ratios = compute_order_qty_ratios(MOCKUP_ALLOCATIONS)
        split = split_quantity_by_ratio(0, ratios)
        assert set(split.quantities.values()) == {0}
        assert split.allocated == 0
        assert split.leftover == 0

    def test_negative_quantity_yields_zero_everywhere(self):
        split = split_quantity_by_ratio(-500, {"mps": 100.0})
        assert split.quantities == {"mps": 0}
        assert split.leftover == 0

    def test_empty_ratio_set_leaves_the_whole_quantity_as_leftover(self):
        split = split_quantity_by_ratio(100, {})
        assert split.quantities == {}
        assert split.allocated == 0
        assert split.leftover == 100

    def test_all_zero_ratios_leave_the_whole_quantity_as_leftover(self):
        split = split_quantity_by_ratio(100, {"mps": 0.0, "suji": 0.0})
        assert split.quantities == {"mps": 0, "suji": 0}
        assert split.leftover == 100

    def test_leftover_is_never_negative_when_ratios_exceed_one_hundred(self):
        split = split_quantity_by_ratio(100, {"mps": 60.0, "suji": 60.0})
        assert split.allocated <= 100
        assert split.leftover >= 0
        assert split.allocated + split.leftover == 100

    def test_leftover_is_not_redistributed_to_another_account(self):
        # suji cannot take its 10 share slice because its lot is 50, and that
        # quantity must not be handed to mps.
        split = split_quantity_by_ratio(100, {"mps": 90.0, "suji": 10.0}, {"mps": 10, "suji": 50})
        assert split.quantities["mps"] == 90
        assert split.leftover == 10


class TestInvestedPercent:
    @pytest.mark.parametrize(
        "account, allocation, holdings_value, expected",
        [
            ("mps", 20.00 * LAKH, 14.20 * LAKH, 71.0),
            ("sathya", 10.00 * LAKH, 6.50 * LAKH, 65.0),
            ("patsen", 10.00 * LAKH, 5.80 * LAKH, 58.0),
            ("Unicorp", 5.00 * LAKH, 3.10 * LAKH, 62.0),
            ("suji", 5.00 * LAKH, 2.60 * LAKH, 52.0),
        ],
    )
    def test_mockup_rows(self, account, allocation, holdings_value, expected):
        assert MOCKUP_ALLOCATIONS[account] == pytest.approx(allocation)
        assert invested_percent(holdings_value, allocation) == pytest.approx(expected)

    def test_zero_allocation_yields_zero_not_infinity(self):
        assert invested_percent(14.20 * LAKH, 0.0) == 0.0

    def test_zero_holdings_yields_zero(self):
        assert invested_percent(0.0, 20.00 * LAKH) == 0.0

    def test_available_cash_is_not_an_input(self):
        # Invested percent is holdings over ALLOCATION. The live Available Cash of
        # an account must never creep into the formula, so the signature stays at
        # exactly two arguments.
        parameters = list(inspect.signature(invested_percent).parameters)
        assert parameters == ["holdings_value", "allocation"]

    def test_fully_invested_account_reports_one_hundred(self):
        assert invested_percent(20.00 * LAKH, 20.00 * LAKH) == pytest.approx(100.0)


class TestStakePercent:
    @pytest.mark.parametrize(
        "symbol, quantity, avg_cost, expected",
        [
            ("NIFTYBEES", 500, 227.60, 2.28),
            ("RELIANCE", 385, 1298.40, 10.0),
            ("GOLDBEES", 800, 60.90, 0.97),
            ("TCS", 60, 4150.00, 4.98),
        ],
    )
    def test_mockup_rows_all_accounts_view(self, symbol, quantity, avg_cost, expected):
        denominator = stake_denominator(MOCKUP_ALLOCATIONS)
        assert denominator == pytest.approx(50.00 * LAKH)
        at_cost = stock_at_cost(avg_cost, quantity)
        assert round(stake_percent(at_cost, denominator), 2) == pytest.approx(expected)

    def test_product_owner_worked_check_single_account(self):
        # "MPS investable fund is Rs.20L and in Reliance MPS invested 2L, then
        # MPS-Reliance Stake is 10%."
        result = stake_percent_for_view(2.00 * LAKH, MOCKUP_ALLOCATIONS, account_key="mps")
        assert result == pytest.approx(10.0)

    def test_product_owner_worked_check_all_accounts(self):
        # "In Accounts total fund is 50L and total Reliance investment is 5L, then
        # combined Reliance stake is 10%."
        result = stake_percent_for_view(5.00 * LAKH, MOCKUP_ALLOCATIONS)
        assert result == pytest.approx(10.0)

    def test_denominator_switches_between_view_modes(self):
        assert stake_denominator(MOCKUP_ALLOCATIONS) == pytest.approx(50.00 * LAKH)
        assert stake_denominator(MOCKUP_ALLOCATIONS, "mps") == pytest.approx(20.00 * LAKH)
        assert stake_denominator(MOCKUP_ALLOCATIONS, "suji") == pytest.approx(5.00 * LAKH)

    def test_helper_matches_manual_denominator_choice(self):
        at_cost = stock_at_cost(1298.40, 385)
        manual = stake_percent(at_cost, stake_denominator(MOCKUP_ALLOCATIONS, "mps"))
        assert stake_percent_for_view(at_cost, MOCKUP_ALLOCATIONS, "mps") == pytest.approx(manual)

    def test_unknown_account_key_yields_zero(self):
        assert stake_denominator(MOCKUP_ALLOCATIONS, "nobody") == 0.0
        assert stake_percent_for_view(2.00 * LAKH, MOCKUP_ALLOCATIONS, "nobody") == 0.0

    def test_zero_denominator_yields_zero_not_infinity(self):
        assert stake_percent(2.00 * LAKH, 0.0) == 0.0
        assert stake_percent_for_view(2.00 * LAKH, {}) == 0.0

    def test_zero_quantity_holding_has_no_stake(self):
        assert stock_at_cost(1298.40, 0) == 0.0
        assert stake_percent_for_view(stock_at_cost(1298.40, 0), MOCKUP_ALLOCATIONS) == 0.0

    def test_stock_at_cost_sums_across_accounts_in_view(self):
        per_account = [stock_at_cost(1298.40, 200), stock_at_cost(1298.40, 185)]
        assert sum(per_account) == pytest.approx(stock_at_cost(1298.40, 385))


class TestSignedPercentOf:
    """
    Regression cover for the P&L percent column on Holdings (M7).

    percent_of treats both sides as magnitudes, which is right for a cost basis
    or a pledged quantity but wrong for a profit and loss figure. Routing a loss
    through it rendered every losing position as 0.0 percent, which reads as
    flat rather than down. These tests pin the signed behaviour.
    """

    def test_loss_keeps_its_sign(self):
        # TCS from the approved mockup: 60 qty, avg cost 4150.00, LTP 3900.00.
        at_cost = stock_at_cost(4150.00, 60)
        gross = (3900.00 - 4150.00) * 60
        assert gross == pytest.approx(-15000.0)
        assert equity_ratio.signed_percent_of(gross, at_cost) == pytest.approx(-6.0241, abs=1e-4)

    def test_profit_matches_unsigned_helper(self):
        at_cost = stock_at_cost(227.60, 500)
        gross = (229.10 - 227.60) * 500
        assert equity_ratio.signed_percent_of(gross, at_cost) == pytest.approx(
            equity_ratio.percent_of(gross, at_cost)
        )

    def test_unsigned_helper_still_clamps_a_negative_numerator(self):
        # Documents the exact trap: percent_of must not be used for signed input.
        assert equity_ratio.percent_of(-37884.0, 499884.0) == 0.0
        assert equity_ratio.signed_percent_of(-37884.0, 499884.0) == pytest.approx(
            -7.5786, abs=1e-4
        )

    def test_zero_and_missing_denominator_yield_zero(self):
        assert equity_ratio.signed_percent_of(-15000.0, 0.0) == 0.0
        assert equity_ratio.signed_percent_of(-15000.0, None) == 0.0
        assert equity_ratio.signed_percent_of(-15000.0, -5.0) == 0.0

    def test_junk_numerator_is_zero_not_an_exception(self):
        assert equity_ratio.signed_percent_of(None, 100.0) == 0.0
        assert equity_ratio.signed_percent_of("nonsense", 100.0) == 0.0
        assert equity_ratio.signed_percent_of(float("nan"), 100.0) == 0.0
        assert equity_ratio.signed_percent_of(float("-inf"), 100.0) == 0.0

    def test_zero_pnl_is_flat_not_negative(self):
        assert equity_ratio.signed_percent_of(0.0, 499884.0) == 0.0
