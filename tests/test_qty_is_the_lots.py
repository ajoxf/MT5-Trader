"""Qty is the lots, and the trader types both legs.

The desk's own words, 2026-09-03:

    Leg A is Gold, Leg B is Gold Future. When 1 lot is placed on the
    ladder it should place 1 lot of Leg A and 1 lot of Leg B. The
    system picks up the contract size from MT5 (say 100oz). A Qty of 1
    lot would be 1 x 100 oz on each leg. A Qty of 100 lots would be
    100 x 100 oz on each leg.

    Ignore the concept of notional value and remove it from the
    settings.

So `clip_lots_a` and `clip_lots_b` are what ONE unit of Qty is on each
leg, both typed, both defaulting to 1. Leg B is no longer derived from
anything, and the three sizing bases that used to derive it — the units
hedge, lot for lot, by notional — are gone.

This file is the end-to-end proof: what is typed is what the two
brokers receive, on a click and on a fill, and nothing in between
recomputes it.
"""

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import SpreadSide


@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def sent(legs, account):
    """The volumes that actually reached one broker."""
    return [e for e in legs[account].broker.sent if e.get('volume')]


# -- a click ---------------------------------------------------------------

def test_a_click_sends_QTY_LOTS_to_each_broker(engine, pair, legs):
    """1 and 1, Qty 3: three lots of leg A and three of leg B. Not a
    ratio of three, not three of something derived — three lots."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 1.0
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.BUY, md['long_spread'], 3.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(3.0)
    assert position.leg_b.volume == pytest.approx(3.0)


def test_an_unequal_ratio_reaches_the_brokers_exactly_as_typed(engine, pair):
    """1 and 2, Qty 10: ten lots of A against twenty of B. Nothing
    second-guesses it against the beta or the contract sizes."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.BUY, md['long_spread'],
                        10.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(10.0)
    assert position.leg_b.volume == pytest.approx(20.0)


def test_a_fractional_leg_is_taken_as_typed_too(engine, pair):
    """The desk trades in tenths on this pair. 0.10 and 0.10 at Qty 1
    is a tenth a side, which is the smallest both brokers will take."""
    pair.clip_lots_a, pair.clip_lots_b = 0.10, 0.10
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.SELL, md['short_spread'],
                        1.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(0.10)
    assert position.leg_b.volume == pytest.approx(0.10)


# -- a LIMIT fill crosses the other leg on the same ratio ------------------

def test_a_fill_crosses_the_other_leg_on_the_TYPED_ratio(engine, pair, legs):
    """A click and a fill must cross the same pair of sizes, or the
    pair is hedged on one route and not on the other. The quoting leg
    rests 2 lots for a Qty of 1 at 1:2; when it fills, leg A is crossed
    for 1."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    md = engine.market[pair.key]

    answer = engine.click(pair.key, SpreadSide.BUY, md['long_spread'], 1.0)
    assert answer['ok']
    engine.poll_once()

    quote = engine.quoter.snapshot(pair.key)[0]
    assert quote['leg'] == 'B'
    assert quote['volume'] == pytest.approx(2.0)

    legs['acct_b'].broker.fill_pending(quote['ticket'])
    engine.poll_once()

    position = engine.book.positions(pair.key)[0]
    assert position.leg_b.volume == pytest.approx(2.0)
    assert position.leg_a.volume == pytest.approx(1.0)


# -- the money multiplier follows the typed lots --------------------------

def test_the_snapshot_prices_one_QTY_not_one_lot(engine, pair):
    """`k` is leg B's units: what a 1.00 move in the spread is worth
    for ONE unit of Qty. The exit levels are priced per unit and must
    not move when the keypad is touched."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    row = engine.snapshot()['pairs'][pair.key]
    contract_b = pair.meta_b['contract_size']
    assert row['spread_units'] == pytest.approx(2.0 * contract_b)
    assert row['clip_lots_a'] == 1.0 and row['clip_lots_b'] == 2.0


# -- and what is no longer there ------------------------------------------

def test_nothing_derives_leg_B_any_more(engine, pair):
    """The control for the whole change. Leg B used to be computed —
    from the hedge arithmetic, or lot for lot, or by equal notional —
    and each of those could round it to zero, size a pair the trader
    had not asked for, or refuse to settle at all. Setting leg A must
    now leave leg B exactly where the trader put it."""
    pair.clip_lots_a, pair.clip_lots_b = 5.0, 1.0
    engine.poll_once()
    engine.resolve_symbols()
    assert (pair.clip_lots_a, pair.clip_lots_b) == (5.0, 1.0)


def test_a_blank_leg_is_ONE_through_the_hot_apply_too(pair):
    """Clearing a box is not clearing the leg: with nothing left to
    derive, a blank has one honest reading."""
    pair.apply_hot({'clip_lots_a': None, 'clip_lots_b': ''})
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 1.0)


def test_both_legs_apply_without_a_restart(pair):
    changed = pair.apply_hot({'clip_lots_a': 1.0, 'clip_lots_b': 2.0})
    assert set(changed) >= {'clip_lots_a', 'clip_lots_b'}
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 2.0)
