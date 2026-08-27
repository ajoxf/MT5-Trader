"""The session cutoff: DAY orders die, and each ladder's overnight rule
decides what happens to its position.
"""

from datetime import datetime

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import OvernightMode, SpreadSide, TimeInForce
from mt5trader.session import gtc_caveat, overnight_action

BEFORE = datetime(2026, 8, 27, 16, 54)
AFTER = datetime(2026, 8, 27, 16, 55)


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def test_allow_keeps_the_position_and_pays_the_swap():
    """This is a CARRY decision, not a risk rule. Holding a rich basis
    over the swap is often the whole trade."""
    assert overnight_action(OvernightMode.ALLOW, 100.0, AFTER, 16, 55) is None


def test_exit_always_flattens_whatever_the_pnl_is():
    assert overnight_action(OvernightMode.EXIT_ALWAYS, -500.0, AFTER, 16,
                            55) == 'OVERNIGHT_CLOSE'
    # ...but not before the cutoff. The control.
    assert overnight_action(OvernightMode.EXIT_ALWAYS, -500.0, BEFORE, 16,
                            55) is None


def test_exit_if_profit_reads_net_pnl_and_treats_unmeasured_as_not_profit():
    """Marked at the mid it would flatten trades that are not actually in
    profit; marked at nothing it must not flatten at all."""
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, 12.0, AFTER, 16,
                            55) == 'OVERNIGHT_CLOSE'
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, -1.0, AFTER, 16,
                            55) is None
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, None, AFTER, 16,
                            55) is None


def test_the_cutoff_cancels_day_orders_and_leaves_gtc_alone(engine, pair,
                                                            legs):
    coordinator = engine
    pair.time_in_force = TimeInForce.DAY
    day = coordinator.click(pair.key, SpreadSide.BUY, 58.40)['order']
    pair.time_in_force = TimeInForce.GTC
    gtc = coordinator.click(pair.key, SpreadSide.BUY, 58.30)['order']
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['count'] == 1
    assert coordinator.book.order(day['order_id']).is_working is False
    assert coordinator.book.order(gtc['order_id']).is_working is True


def test_the_cutoff_fires_once_a_day_not_on_every_poll(engine, pair):
    """A rule that re-fires every poll after 16:55 would cancel an order
    the trader deliberately placed at 16:56."""
    coordinator = engine
    coordinator.session_clock.now = lambda: AFTER
    coordinator.click(pair.key, SpreadSide.BUY, 58.40)

    assert coordinator.run_session_cutoff()
    coordinator.click(pair.key, SpreadSide.BUY, 58.40)   # placed at 16:56
    assert coordinator.run_session_cutoff() == []
    assert len(coordinator.book.orders(pair.key)) == 1


def test_an_overnight_close_flattens_by_ticket(engine, pair, legs):
    coordinator = engine
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['action'] == 'overnight_close'
    assert events[0]['ok']
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    # Closed by ticket, never by an offsetting order.
    assert [e['action'] for e in legs['acct_b'].broker.sent] == \
        ['market', 'close']


def test_no_guard_withholds_an_overnight_close(engine, pair, legs):
    """It reads no price level, so the staleness and jump guards have no
    say — a trade must always be closable."""
    coordinator = engine
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)
    coordinator.market[pair.key]['guard_reason'] = 'Leg A stale'
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['ok']
    assert legs['acct_a'].broker.open_positions() == []


def test_gtc_says_what_it_can_and_cannot_promise():
    """It is never to be misread as exchange-resident."""
    caveat = gtc_caveat()
    assert 'until this system stops' in caveat
