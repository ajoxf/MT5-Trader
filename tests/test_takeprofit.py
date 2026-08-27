"""Where to get out: break-even, and break-even plus a target.

The trader clicking a spread wants one number back — where do I exit? —
and every figure it is built from is one this system already holds. The
tests here pin the arithmetic and, more importantly, the two rules that
make it safe to read: unmeasured is not zero, and nothing is ever sent
to the broker from it.
"""

import pytest

from mt5trader import takeprofit
from mt5trader.models import SpreadSide


SETTINGS = {'COMMISSION_PER_LOT_A': 2.0, 'COMMISSION_PER_LOT_B': 3.0,
            'TP_TARGET_PCT_OF_MARGIN': 2.0}


def market(short=59.09, long_=59.11):
    return {'short_spread': short, 'long_spread': long_,
            'spread': (short + long_) / 2.0}


def test_break_even_is_the_entry_plus_commission_both_legs_both_ends(pair):
    """The round turn of both legs' bid-ask is already inside the two
    prices being compared — bought at the offer, sold on the bid. What
    is left to recover is commission."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=None, quantity=1.0,
                               spread_units=10.0)

    # 2 x (2.00 x 0.1 + 3.00 x 0.1) = 1.00, over k = 10 -> 0.10 of spread.
    assert body['break_even_buy'] == pytest.approx(59.21)
    assert body['break_even_sell'] == pytest.approx(58.99)


def test_the_target_is_a_percentage_of_the_margin_the_spread_ties_up(pair):
    """Margin is what the trade actually costs to hold, per account and
    per broker — so the target is a return on THAT, not on notional."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, quantity=1.0,
                               spread_units=10.0)

    # 2% of 500 = 10.00, over k = 10 -> 1.00 of spread, on top of B/E.
    assert body['target_money'] == pytest.approx(10.0)
    assert body['tp_buy'] == pytest.approx(60.21)
    assert body['tp_sell'] == pytest.approx(57.99)


def test_the_two_directions_move_opposite_ways(pair):
    """A long leaves on the bid ABOVE what it paid; a short buys back
    BELOW what it sold. One number here would be right half the time."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, spread_units=10.0)

    assert body['tp_buy'] > body['break_even_buy'] > market()['long_spread']
    assert body['tp_sell'] < body['break_even_sell'] < market()['short_spread']


def test_without_a_margin_figure_there_is_a_break_even_and_no_target(pair):
    """Unmeasured is not zero. A TP of 0.00 would read as "get out at
    break-even", which is a different instruction — so the target is
    absent and the box says what is missing."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=None, spread_units=10.0)

    assert body['break_even_buy'] is not None
    assert body['tp_buy'] is None and body['tp_sell'] is None
    assert 'margin' in body['note']


def test_a_zero_target_shows_break_even_alone(pair):
    """The control for the case above: a desk that wants break-even and
    nothing else sets the percentage to zero, and that is not the same
    as a target nobody could compute."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    settings = dict(SETTINGS, TP_TARGET_PCT_OF_MARGIN=0.0)

    body = takeprofit.describe(pair, market(), settings,
                               margin_per_spread=500.0, spread_units=10.0)

    assert body['break_even_buy'] is not None
    assert body['tp_buy'] is None
    assert 'Settings' in body['note']


def test_a_position_is_anchored_on_what_it_was_entered_at(pair, config, legs):
    """A take-profit that moves with the market is not a take-profit."""
    from mt5trader.executor import PairExecutor
    from mt5trader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    from mt5trader.spread import compute_spread
    md = compute_spread(pair, legs['acct_a'].tick(pair.symbol_a),
                        legs['acct_b'].tick(pair.symbol_b), pair.hedge_ratio)
    position = executor.market_entry(pair, SpreadSide.BUY, md, 1.0).position

    body = takeprofit.for_position(position, md, pair, SETTINGS,
                                   margin_per_spread=500.0)

    assert body['side'] == 'BUY'
    assert body['break_even'] > position.entry_spread     # commission
    assert body['tp'] > body['break_even']                # the target
    # And it does NOT move when the market does.
    legs['acct_b'].broker.symbols[pair.symbol_b].bid += 5.0
    again = takeprofit.for_position(position, md, pair, SETTINGS,
                                    margin_per_spread=500.0)
    assert again['tp'] == pytest.approx(body['tp'])


def test_the_engine_prices_the_margin_from_both_terminals(config, pair, legs):
    """One spread ties up margin on BOTH accounts, and each terminal
    prices its own — leverage, margin mode and account group are the
    broker's, not something to derive here."""
    from mt5trader.coordinator import Coordinator
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    coordinator = Coordinator(config, legs)
    coordinator.start()

    per_spread = coordinator.margin_per_spread(pair)

    # 0.1 lots a side: 300 + 200.
    assert per_spread == pytest.approx(500.0)


def test_a_broker_that_cannot_price_margin_leaves_the_target_unmeasured(
        config, pair, legs):
    """The control. Half a margin figure is not a margin figure, and a
    target built on one leg would be wrong by the other leg's whole
    contribution."""
    from mt5trader.coordinator import Coordinator
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = None
    coordinator = Coordinator(config, legs)
    coordinator.start()

    assert coordinator.margin_per_spread(pair) is None

    coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['exit']['tp_buy'] is None
    assert row['exit']['break_even_buy'] is not None
