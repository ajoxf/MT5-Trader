"""Closing at a PRICE, not at the market.

CLOSE ALL crosses now. This is the other half: name a spread level and
rest one working order per open position, each carrying that position's
ticket — on a hedging account an opposite order without the ticket
OPENS a second position instead of closing the first.

It shares AutoRouting's machinery, and that is exactly where it can go
wrong: turning AutoRouting off pulls what AutoRouting armed, and an
exit the TRADER rested by hand must survive that. `armed_by` is the
whole of the distinction, so most of this file is about it.
"""

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def market_entry(coordinator, pair, side=SpreadSide.BUY):
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, side, md['long_spread'])
    assert answer.get('ok'), answer
    return coordinator.book.positions(pair.key)[0]


# -- what it places --------------------------------------------------------

def test_it_rests_one_order_per_position_at_the_price_asked(engine, pair):
    coordinator = engine
    position = market_entry(coordinator, pair)

    answer = coordinator.close_at_limit(pair.key, 12.5)

    assert answer['ok'] and len(answer['armed']) == 1
    resting = coordinator.book.orders_for_position(position.position_id)
    assert len(resting) == 1
    assert resting[0].level == 12.5
    # A long is closed by SELLING the spread.
    assert resting[0].side is SpreadSide.SELL


def test_the_closing_order_carries_the_position_it_closes(engine, pair, legs):
    """Without the ticket this is an ENTRY on a hedging account."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    coordinator.poll_once()

    order = coordinator.book.orders_for_position(position.position_id)[0]
    assert order.position_id == position.position_id
    pendings = list(legs['acct_a'].broker.pendings.values()) + \
        list(legs['acct_b'].broker.pendings.values())
    assert pendings, 'nothing reached either broker'
    assert any(p.get('position_ticket') for p in pendings)


def test_it_is_marked_as_the_TRADER_s_order(engine, pair):
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    assert coordinator.book.orders_for_position(
        position.position_id)[0].armed_by == 'trader'


def test_nothing_crosses_now(engine, pair, legs):
    """A target, and no stop — and above all, no market order. The
    control against this quietly becoming CLOSE ALL."""
    coordinator = engine
    market_entry(coordinator, pair)
    before = len(legs['acct_b'].broker.positions)
    coordinator.close_at_limit(pair.key, 12.5)
    coordinator.poll_once()
    assert len(legs['acct_b'].broker.positions) == before


# -- what it refuses -------------------------------------------------------

def test_flat_is_refused_and_says_so(engine, pair):
    answer = engine.close_at_limit(pair.key, 12.5)
    assert answer['ok'] is False
    assert 'flat' in answer['reason']


def test_a_price_that_is_not_a_number_is_refused(engine, pair):
    coordinator = engine
    market_entry(coordinator, pair)
    for value in (None, '', 'later'):
        answer = coordinator.close_at_limit(pair.key, value)
        assert answer['ok'] is False, value
        assert 'needs a price' in answer['reason']


def test_an_unknown_pair_is_refused(engine):
    assert engine.close_at_limit('nothing|here', 1.0)['ok'] is False


def test_a_position_already_working_an_exit_is_not_given_a_second(engine,
                                                                  pair):
    """Two closing orders on one position is a position closed twice —
    the second fill opens the opposite side."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    assert coordinator.close_at_limit(pair.key, 12.5)['ok']

    again = coordinator.close_at_limit(pair.key, 13.5)

    assert again['ok'] is False
    assert again['already_working'] == [position.position_id]
    assert 'pull it first' in again['reason']
    assert len(coordinator.book.orders_for_position(
        position.position_id)) == 1


# -- and what must NOT take it away ----------------------------------------

def test_turning_AutoRouting_off_leaves_the_trader_s_exit_alone(engine, pair):
    """The one that matters. Standing AutoRouting down pulls what
    AutoRouting armed; a hand-rested exit is the trader's, and taking
    it away leaves a live position with no target and nobody told."""
    coordinator = engine
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    pair.auto_route = True
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)

    pair.auto_route = False
    coordinator.work_auto_route(pair, coordinator.market.get(pair.key))

    resting = coordinator.book.orders_for_position(position.position_id)
    assert len(resting) == 1 and resting[0].level == 12.5


def test_turning_AutoRouting_off_DOES_pull_what_AutoRouting_armed(engine,
                                                                  pair, legs):
    """The control: the filter must not turn the stand-down into a
    no-op. An automation switched off that leaves its order resting has
    stood nothing down."""
    coordinator = engine
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    coordinator.config.settings['TP_TARGET_PCT_OF_MARGIN'] = 2.0
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    pair.auto_route = True

    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    armed = coordinator.book.orders_for_position(position.position_id)
    assert len(armed) == 1 and armed[0].armed_by == 'auto'

    pair.auto_route = False
    coordinator.work_auto_route(pair, coordinator.market.get(pair.key))

    assert coordinator.book.orders_for_position(position.position_id) == []


def test_a_closing_order_still_goes_when_its_POSITION_goes(engine, pair):
    """Whoever rested it. An exit whose position is gone would fill
    into nothing and open a naked one."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)

    position.closed_at = 1_700_000_000.0
    coordinator.work_auto_route(pair, coordinator.market.get(pair.key))

    assert coordinator.book.orders_for_position(position.position_id) == []


def test_it_is_written_to_the_session_log(engine, pair):
    coordinator = engine
    market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    assert any(event['action'] == 'close_at_limit'
               for event in coordinator.session_events)
