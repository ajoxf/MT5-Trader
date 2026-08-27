"""Getting a pair on and off again — the part that must be exactly right
before anything is allowed to rest.
"""

import pytest

from mt5trader.executor import PairExecutor, mark_position, slippage
from mt5trader.models import OrderSide, SpreadSide
from mt5trader.spread import compute_spread


def snapshot(pair, legs):
    tick_a = legs['acct_a'].tick(pair.symbol_a)
    tick_b = legs['acct_b'].tick(pair.symbol_b)
    return compute_spread(pair, tick_a, tick_b, pair.hedge_ratio)


def resolved(pair, legs):
    """The metadata the coordinator reads out of MT5 at startup."""
    from mt5trader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(
        legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(
        legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a, pair.clip_lots_b = 0.1, 0.1
    return pair


def test_a_market_click_puts_both_legs_on(config, pair, legs):
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)

    result = executor.market_entry(pair, SpreadSide.SELL, md, 1.0,
                                   md['short_spread'])

    assert result.ok, result.reason
    position = result.position
    # SELL the spread = sell B, buy A.
    assert position.leg_a.side is OrderSide.BUY
    assert position.leg_b.side is OrderSide.SELL
    # Entry is anchored on the EXECUTED fills, and those are the two
    # touches a short crosses — so it IS the short spread.
    assert position.entry_spread == pytest.approx(md['short_spread'])
    assert position.entry_slippage == pytest.approx(0.0)
    assert position.click_to_on_ms is not None


def test_the_harder_to_fill_leg_goes_first(config, pair, legs, timeline):
    """Filling the easy leg and then discovering the hard one will not
    fill is how you end up naked. Leg B's minimum is ten times leg A's."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    assert executor.crossing_order(pair) == ('b', 'a')

    executor.market_entry(pair, SpreadSide.BUY, snapshot(pair, legs), 1.0)
    arrived = [e['symbol'] for e in timeline if e['action'] == 'market']
    assert arrived == [pair.symbol_b, pair.symbol_a]


def test_a_rejected_crossing_leg_unwinds_by_ticket_and_never_offsets(
        config, pair, legs):
    """These accounts are HEDGING mode: an opposite market order opens a
    SECOND position and leaves the first live. That has happened on a
    real account — the reconciler found both of them 60 seconds later.
    """
    resolved(pair, legs)
    legs['acct_a'].broker.reject_orders[pair.symbol_a] = \
        '10027 - AutoTrading disabled by client'
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    # The broker's own words reach the ladder, not "check the log".
    assert '10027' in result.reason
    # Leg B's book is EMPTY, not "flat by offsetting".
    assert legs['acct_b'].broker.open_positions() == []
    actions = [e['action'] for e in legs['acct_b'].broker.sent]
    assert actions == ['market', 'close']       # never a second 'market'
    assert result.naked is None


def test_an_unwind_that_fails_leaves_a_naked_banner_not_a_silent_gap(
        config, pair, legs):
    resolved(pair, legs)
    legs['acct_a'].broker.reject_orders[pair.symbol_a] = 'no margin'
    legs['acct_b'].broker.fail_closes.add(pair.symbol_b)   # unwind fails too
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    assert result.naked and result.naked['leg'] == 'B'
    assert result.naked['volume'] == pytest.approx(0.1)
    assert legs['acct_b'].broker.open_positions()          # still on


def test_flat_and_back_to_flat_costs_exactly_one_round_turn(
        config, pair, legs):
    """Enter at short_spread, exit at long_spread, nothing moves in
    between: the pair is down EXACTLY the modelled round trip, no more.
    Charging the crossing twice would double it."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)

    result = executor.market_entry(pair, SpreadSide.SELL, md, 1.0)
    position = result.position
    executor.close_position(pair, position, md, reason='test')

    expected = -md['spread_cost'] * position.spread_units * position.quantity
    assert position.realized_pnl == pytest.approx(expected)
    # And both books are actually empty — closed, not offset.
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


def test_a_position_shows_that_round_turn_as_a_loss_the_instant_it_opens(
        config, pair, legs):
    """Marked where it would actually CLOSE. A mid mark shows a profit
    that cannot be taken."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0)

    gross, net, closing = mark_position(result.position, md, config.settings)
    assert closing == md['short_spread']
    assert gross == pytest.approx(
        -md['spread_cost'] * result.position.spread_units)
    assert net == pytest.approx(gross)      # no commission configured


def test_market_protection_refuses_a_fill_through_the_clicked_price(
        config, pair, legs):
    """A market order on a ladder is market-WITH-PROTECTION. Without it
    a click fills at whatever the touch happens to be."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    clicked = md['long_spread'] - 10 * pair.increment   # 10 ticks better

    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0, clicked)

    assert result.refused and not result.ok
    assert 'protection' in result.reason
    assert legs['acct_a'].broker.sent == []      # nothing was sent
    assert legs['acct_b'].broker.sent == []


def test_a_click_within_protection_still_goes(config, pair, legs):
    """The control: the guard must not refuse everything."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    clicked = md['long_spread'] - 1 * pair.increment

    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0, clicked)
    assert result.ok, result.reason


def test_a_click_is_refused_before_anything_moves_when_a_leg_is_missing(
        config, pair, legs):
    resolved(pair, legs)
    executor = PairExecutor(config, {'acct_a': legs['acct_a']},
                            sleep=lambda s: None)
    result = executor.market_entry(pair, SpreadSide.BUY,
                                   snapshot(pair, legs), 1.0)
    assert result.refused
    assert "acct_b" in result.reason
    assert legs['acct_a'].broker.sent == []


def test_slippage_is_positive_when_it_costs_at_both_ends():
    """A short BUYS the spread back, so the sign flips between entry and
    exit. Getting that backwards reports every exit's cost as a gain."""
    # Entry: sold at 55.80 where 55.93 was showing -> it cost 0.13.
    assert slippage(55.93, 55.80, SpreadSide.SELL) == pytest.approx(0.13)
    # Exit of that short: bought back at 56.10 against 55.97 -> also 0.13.
    assert slippage(55.97, 56.10, SpreadSide.SELL,
                    closing=True) == pytest.approx(0.13)
    # Unmeasured is not zero.
    assert slippage(None, 56.10, SpreadSide.SELL) is None
