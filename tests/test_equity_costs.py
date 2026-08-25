"""
Unit tests for the equity brokerage and statutory cost engine
(app/utils/equity_costs.py).

The engine is loaded straight from its file path rather than with
"from app.utils.equity_costs import ...", because importing the app package would
pull in Flask and SQLAlchemy. Loading by path keeps these tests runnable on any
machine and doubles as proof that the engine really is standalone.

Rate units under test: every "_pct" field is a PERCENT value, so stt_pct=0.10
means 0.10 percent of turnover and gst_pct=18.0 means 18 percent.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ENGINE_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "equity_costs.py"

ALLOWED_IMPORTS = {"__future__", "math", "dataclasses", "typing"}


def _load_engine(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because dataclasses resolves the postponed
    # annotations of a frozen dataclass through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


equity_costs = _load_engine("equity_costs_engine", ENGINE_PATH)

BrokerageRates = equity_costs.BrokerageRates
CostBreakdown = equity_costs.CostBreakdown
estimate_costs = equity_costs.estimate_costs
estimate_total_costs = equity_costs.estimate_total_costs
gross_pnl = equity_costs.gross_pnl
net_pnl = equity_costs.net_pnl
normalise_side = equity_costs.normalise_side
turnover = equity_costs.turnover
SIDE_BUY = equity_costs.SIDE_BUY
SIDE_SELL = equity_costs.SIDE_SELL

# Rates from the approved Settings mockup. Rupee amounts for brokerage and DP,
# PERCENT values for the rest.
RATES = BrokerageRates(
    brokerage_per_order=20.00,
    stt_pct=0.10,
    exchange_txn_pct=0.00297,
    sebi_pct=0.0001,
    stamp_duty_pct=0.015,
    gst_pct=18.0,
    dp_amc_charge=20.00,
)

# The Upstox row on the same mockup, on the higher exchange transaction slab and
# a zero brokerage plan.
UPSTOX_RATES = BrokerageRates(
    brokerage_per_order=0.00,
    stt_pct=0.10,
    exchange_txn_pct=0.00325,
    sebi_pct=0.0001,
    stamp_duty_pct=0.015,
    gst_pct=18.0,
    dp_amc_charge=13.50,
)

TURNOVER = 100_000.0


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


class TestPercentUnits:
    def test_percent_fields_are_percent_values_not_fractions(self):
        # 0.10 percent of 1,00,000 is 100 rupees. Reading the rate as a fraction
        # would give 10,000, reading it as already divided would give 0.10.
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert costs.stt == pytest.approx(100.0)
        assert costs.exchange_txn == pytest.approx(2.97)
        assert costs.sebi == pytest.approx(0.10)
        assert costs.stamp_duty == pytest.approx(15.0)

    def test_gst_eighteen_percent_is_applied_as_eighteen_percent(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert costs.gst == pytest.approx(0.18 * (20.00 + 2.97))


class TestBuyCosts:
    def test_full_buy_breakdown(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert costs.brokerage == pytest.approx(20.00)
        assert costs.stt == pytest.approx(100.00)
        assert costs.exchange_txn == pytest.approx(2.97)
        assert costs.sebi == pytest.approx(0.10)
        assert costs.stamp_duty == pytest.approx(15.00)
        assert costs.gst == pytest.approx(4.1346)
        assert costs.dp_amc == 0.0
        assert costs.total == pytest.approx(142.2046)

    def test_stamp_duty_is_charged_on_buy(self):
        assert estimate_costs(TURNOVER, SIDE_BUY, RATES).stamp_duty > 0.0

    def test_dp_amc_is_not_charged_on_buy(self):
        assert estimate_costs(TURNOVER, SIDE_BUY, RATES, scrip_count=5).dp_amc == 0.0

    def test_total_equals_the_sum_of_the_line_items(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        line_items = (
            costs.brokerage
            + costs.stt
            + costs.exchange_txn
            + costs.sebi
            + costs.stamp_duty
            + costs.gst
            + costs.dp_amc
        )
        assert costs.total == pytest.approx(line_items)

    def test_estimate_total_costs_matches_the_breakdown_total(self):
        assert estimate_total_costs(TURNOVER, SIDE_BUY, RATES) == pytest.approx(142.2046)


class TestSellCosts:
    def test_full_sell_breakdown(self):
        costs = estimate_costs(TURNOVER, SIDE_SELL, RATES)
        assert costs.brokerage == pytest.approx(20.00)
        assert costs.stt == pytest.approx(100.00)
        assert costs.exchange_txn == pytest.approx(2.97)
        assert costs.sebi == pytest.approx(0.10)
        assert costs.stamp_duty == 0.0
        assert costs.gst == pytest.approx(4.1346)
        assert costs.dp_amc == pytest.approx(20.00)
        assert costs.total == pytest.approx(147.2046)

    def test_stamp_duty_is_not_charged_on_sell(self):
        assert estimate_costs(TURNOVER, SIDE_SELL, RATES).stamp_duty == 0.0

    def test_dp_amc_is_charged_per_scrip_on_sell(self):
        assert estimate_costs(TURNOVER, SIDE_SELL, RATES, scrip_count=1).dp_amc == pytest.approx(20.0)
        assert estimate_costs(TURNOVER, SIDE_SELL, RATES, scrip_count=3).dp_amc == pytest.approx(60.0)

    def test_scrip_count_defaults_to_one(self):
        assert estimate_costs(TURNOVER, SIDE_SELL, RATES).dp_amc == pytest.approx(
            estimate_costs(TURNOVER, SIDE_SELL, RATES, scrip_count=1).dp_amc
        )

    def test_zero_scrip_count_charges_no_dp_amc(self):
        assert estimate_costs(TURNOVER, SIDE_SELL, RATES, scrip_count=0).dp_amc == 0.0

    def test_buy_and_sell_differ_only_by_stamp_duty_and_dp_amc(self):
        buy = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        sell = estimate_costs(TURNOVER, SIDE_SELL, RATES)
        assert sell.total - buy.total == pytest.approx(sell.dp_amc - buy.stamp_duty)
        assert buy.brokerage == sell.brokerage
        assert buy.stt == sell.stt
        assert buy.exchange_txn == sell.exchange_txn
        assert buy.sebi == sell.sebi
        assert buy.gst == sell.gst


class TestGstScope:
    def test_gst_covers_brokerage_and_exchange_transaction_charge_only(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert costs.gst == pytest.approx(0.18 * (costs.brokerage + costs.exchange_txn))

    def test_gst_ignores_stt_sebi_and_stamp_duty(self):
        taxed_base = RATES.brokerage_per_order + 2.97
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert costs.gst == pytest.approx(0.18 * taxed_base)
        assert costs.gst != pytest.approx(0.18 * (costs.total - costs.gst))

    def test_raising_stt_does_not_change_gst(self):
        higher_stt = BrokerageRates(
            brokerage_per_order=RATES.brokerage_per_order,
            stt_pct=1.0,
            exchange_txn_pct=RATES.exchange_txn_pct,
            sebi_pct=RATES.sebi_pct,
            stamp_duty_pct=RATES.stamp_duty_pct,
            gst_pct=RATES.gst_pct,
            dp_amc_charge=RATES.dp_amc_charge,
        )
        assert estimate_costs(TURNOVER, SIDE_BUY, higher_stt).gst == pytest.approx(
            estimate_costs(TURNOVER, SIDE_BUY, RATES).gst
        )

    def test_zero_brokerage_plan_taxes_the_exchange_charge_only(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, UPSTOX_RATES)
        assert costs.brokerage == 0.0
        assert costs.exchange_txn == pytest.approx(3.25)
        assert costs.gst == pytest.approx(0.18 * 3.25)


class TestSideHandling:
    @pytest.mark.parametrize("side", ["BUY", "buy", " Buy ", "B"])
    def test_buy_aliases(self, side):
        assert normalise_side(side) == SIDE_BUY

    @pytest.mark.parametrize("side", ["SELL", "sell", " Sell ", "S"])
    def test_sell_aliases(self, side):
        assert normalise_side(side) == SIDE_SELL

    @pytest.mark.parametrize("side", ["", None, "HOLD", "SQUAREOFF"])
    def test_unknown_side_raises(self, side):
        with pytest.raises(ValueError):
            estimate_costs(TURNOVER, side, RATES)

    def test_case_does_not_change_the_estimate(self):
        assert estimate_costs(TURNOVER, "sell", RATES).total == pytest.approx(
            estimate_costs(TURNOVER, SIDE_SELL, RATES).total
        )


class TestTurnoverEdges:
    def test_zero_turnover_leaves_only_the_flat_charges(self):
        costs = estimate_costs(0, SIDE_SELL, RATES)
        assert costs.stt == 0.0
        assert costs.exchange_txn == 0.0
        assert costs.sebi == 0.0
        assert costs.stamp_duty == 0.0
        assert costs.gst == pytest.approx(0.18 * 20.00)
        assert costs.dp_amc == pytest.approx(20.00)
        assert costs.total == pytest.approx(20.00 + 3.60 + 20.00)

    def test_zero_rates_cost_nothing(self):
        assert estimate_costs(TURNOVER, SIDE_BUY, BrokerageRates()).total == 0.0

    def test_negative_turnover_is_treated_as_zero(self):
        assert estimate_costs(-TURNOVER, SIDE_BUY, RATES).stt == 0.0

    def test_turnover_helper_multiplies_price_by_quantity(self):
        assert turnover(1298.40, 385) == pytest.approx(499_884.0)
        assert turnover(1298.40, -385) == pytest.approx(499_884.0)
        assert turnover(None, 385) == 0.0


class TestPnl:
    def test_gross_profit(self):
        assert gross_pnl(1400.00, 1298.40, 385) == pytest.approx(39_116.0)

    def test_gross_loss_is_negative(self):
        assert gross_pnl(1200.00, 1298.40, 385) == pytest.approx(-37_884.0)

    def test_zero_quantity_has_no_pnl(self):
        assert gross_pnl(1400.00, 1298.40, 0) == 0.0

    def test_net_pnl_subtracts_costs_from_a_profit(self):
        gross = gross_pnl(1400.00, 1298.40, 385)
        costs = estimate_costs(turnover(1400.00, 385), SIDE_SELL, RATES)
        net = net_pnl(gross, costs)
        assert net == pytest.approx(gross - costs.total)
        assert net < gross

    def test_negative_gross_flows_through_to_a_more_negative_net(self):
        gross = gross_pnl(90.00, 100.00, 100)
        assert gross == pytest.approx(-1000.0)
        costs = estimate_costs(TURNOVER, SIDE_SELL, RATES)
        net = net_pnl(gross, costs)
        assert net == pytest.approx(-1147.2046)
        assert net < gross

    def test_net_pnl_accepts_a_plain_number_or_a_breakdown(self):
        costs = estimate_costs(TURNOVER, SIDE_BUY, RATES)
        assert net_pnl(1000.0, costs) == pytest.approx(net_pnl(1000.0, costs.total))
        assert net_pnl(1000.0, 142.2046) == pytest.approx(857.7954)

    def test_net_pnl_with_no_costs_equals_gross(self):
        assert net_pnl(1000.0, None) == pytest.approx(1000.0)
        assert net_pnl(1000.0, CostBreakdown()) == pytest.approx(1000.0)
