"""The hedge arithmetic — the part that inverts if you guess."""

import pytest

from mt5trader.config import PairConfig
from mt5trader.sizing import (clip_plan, hedge_lots, matched_minimum_lots,
                              minimum_notional, spread_units)


def pnl_of_pure_beta_move(lots_a, contract_a, lots_b, contract_b, beta,
                          move_a=1.0):
    """A move that leaves the SPREAD unchanged: dP_B = beta x dP_A.

    A correctly hedged short-spread pair (long A, short B) nets exactly
    zero across it. That is the definition of the hedge, so it is the
    test that catches an inverted one.
    """
    move_b = beta * move_a
    return move_a * lots_a * contract_a - move_b * lots_b * contract_b


@pytest.mark.parametrize('beta,contract_a,contract_b', [
    (1.0, 100.0, 100.0),      # the only shape the stat-arb engine ran in
    (2.0, 100.0, 100.0),      # beta != 1 INVERTS the naive rule
    (0.5, 100.0, 100.0),
    (1.0, 1000.0, 100.0),     # 1,000 bbl CFD against a 100 bbl future
    (66.93, 100.0, 5000.0),   # gold against silver
])
def test_a_pure_beta_move_nets_to_zero(beta, contract_a, contract_b):
    lots_a = 1.0
    lots_b = hedge_lots(lots_a, contract_a, contract_b, beta, step=0.0)
    assert pnl_of_pure_beta_move(lots_a, contract_a, lots_b, contract_b,
                                 beta) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize('beta,contract_a,contract_b', [
    (2.0, 100.0, 100.0),
    (1.0, 1000.0, 100.0),
])
def test_the_naive_hedge_does_not_net_to_zero(beta, contract_a, contract_b):
    """The control. `L_B = L_A x beta` is identical only at beta 1 with
    equal contract sizes — which is why it hid for months. Without this
    assertion the test above could pass for an unrelated reason."""
    lots_a = 1.0
    naive = lots_a * beta
    assert pnl_of_pure_beta_move(lots_a, contract_a, naive, contract_b,
                                 beta) != pytest.approx(0.0, abs=1e-6)


def test_k_is_leg_bs_units_not_leg_as():
    """`k = L_B x C_B` is the ONE multiplier every spread-to-money
    conversion uses. Leg A's units are equal only at beta 1 with equal
    contracts."""
    lots_b = hedge_lots(1.0, 1000.0, 100.0, beta=1.0, step=0.0)
    assert spread_units(lots_b, 100.0) == pytest.approx(1000.0)


def test_the_hedge_rounds_down_short_is_the_recoverable_error():
    """Nearest would turn a wanted 0.05 into 0.1 on a 0.1-step leg — a
    hedge twice the position it is hedging, net short the difference."""
    lots = hedge_lots(0.05, 100.0, 100.0, beta=1.0, step=0.1, minimum=0.0)
    assert lots == 0.0                      # under the step: not 0.1


def test_matched_minimum_clears_both_legs_and_actually_hedges():
    """CFI's real shape: spot minimum 0.01, futures minimum 0.10. Using
    each leg's own minimum is not a hedge — it is 9 oz net short."""
    lots_a, lots_b = matched_minimum_lots(
        min_a=0.01, min_b=0.10, step_a=0.01, step_b=0.10, beta=1.0,
        contract_a=100.0, contract_b=100.0)
    assert lots_a == pytest.approx(0.10)
    assert lots_b == pytest.approx(0.10)
    assert pnl_of_pure_beta_move(lots_a, 100.0, lots_b, 100.0,
                                 1.0) == pytest.approx(0.0)


def test_matched_minimum_walks_up_until_the_steps_can_express_the_ratio():
    """Silver (5,000/lot) against gold (100/lot) at beta 66.93: the floor
    is 0.02/0.01, a third out. The smallest genuinely MATCHED pair is
    further up."""
    lots_a, lots_b = matched_minimum_lots(
        min_a=0.01, min_b=0.01, step_a=0.01, step_b=0.01, beta=66.93,
        contract_a=5000.0, contract_b=100.0)
    exact = lots_a * 5000.0 / (66.93 * 100.0)
    assert abs(lots_b - exact) / exact <= 0.02


def test_minimum_notional_names_the_number_the_operator_can_act_on():
    floor = minimum_notional(contract_a=100.0, contract_b=100.0,
                             price_a=4292.0, price_b=4351.0, beta=1.0,
                             min_a=0.01, min_b=0.10)
    # Leg B's 0.10-lot minimum binds, not leg A's 0.01.
    assert floor == pytest.approx(0.10 * 100.0 * 4292.0)


def _pair(**kwargs):
    base = dict(leg_a={'account': 'a', 'symbol': 'A'},
                leg_b={'account': 'b', 'symbol': 'B'},
                hedge_ratio=1.0, increment=0.01, clip_lots_a=0.1,
                clip_lots_b=0.1)
    base.update(kwargs)
    return PairConfig('A|B', **base)


META_A = {'contract_size': 100.0, 'volume_min': 0.01, 'volume_step': 0.01,
          'volume_max': 100.0}
META_B = {'contract_size': 100.0, 'volume_min': 0.10, 'volume_step': 0.10,
          'volume_max': 100.0}


def test_a_click_is_quoted_in_spreads_and_shows_its_derivation():
    plan = clip_plan(_pair(), META_A, META_B, 4292.0, 4351.0, spreads=1.0)
    assert plan['reason'] is None
    assert plan['leg_a_lots'] == pytest.approx(0.1)
    assert plan['leg_b_lots'] == pytest.approx(0.1)
    assert plan['spread_units'] == pytest.approx(10.0)
    # A number with no unit is not checkable (spec §11).
    assert '$10.00 per 1.00 of spread' in plan['derivation']


def test_a_click_under_leg_bs_minimum_is_refused_before_anything_moves():
    plan = clip_plan(_pair(clip_lots_a=0.01, clip_lots_b=0.01),
                     META_A, META_B, 4292.0, 4351.0, spreads=1.0)
    assert plan['reason'] is not None
    assert '0.1-lot minimum' in plan['reason']
    assert plan['leg_b_lots'] == 0.0


def test_an_inflated_hedge_is_refused_against_the_brokers_ceiling():
    """An inverted beta once sized leg B at 5,167 lots of gold — $2.25bn
    — and the plan reported it as fine. MT5 would have rejected it AFTER
    leg A was already on."""
    plan = clip_plan(_pair(hedge_ratio=0.0149, clip_lots_a=1.0,
                           clip_lots_b=5167.0),
                     META_A, META_B, 4292.0, 4351.0, spreads=1.0)
    assert plan['reason'] is not None
    assert "broker's maximum" in plan['reason']
