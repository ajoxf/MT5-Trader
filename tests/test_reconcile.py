"""Reconciliation: orphans, ghosts, and the numbers that must be right
when the engine is cleaning up after itself.
"""

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import OrderSide, SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def an_orphan(legs, symbol='GC1226', side=OrderSide.SELL, volume=0.1,
              account='acct_b'):
    """Something at the broker that our book has never heard of — a
    position the trader opened in the terminal by hand, say."""
    result = legs[account].broker.send_market_order(symbol, side, volume,
                                                    comment='by hand')
    return result.ticket


def test_an_orphan_is_closed_on_the_third_strike_not_the_first(engine, legs):
    """A single poll can catch the broker mid-fill, and acting on that is
    how a healthy position gets closed for being briefly invisible."""
    coordinator = engine
    ticket = an_orphan(legs)

    for expected in (1, 2):
        report = coordinator.reconciler.run()
        assert report['orphans'][0]['strikes'] == expected
        assert report['closed'] == []
        assert legs['acct_b'].broker.positions.get(ticket)

    report = coordinator.reconciler.run()
    assert report['closed'][0]['ok']
    assert legs['acct_b'].broker.positions.get(ticket) is None


def test_the_orphan_close_pnl_multiplies_by_the_contract_size(engine, legs):
    """Booked at 1% of what they cost for months — $0.81 where the answer
    was $81.10."""
    coordinator = engine
    an_orphan(legs, side=OrderSide.SELL, volume=0.1)
    # It was sold at the bid; the market moves up, so buying it back
    # costs money.
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.40)

    for _ in range(3):
        report = coordinator.reconciler.run()

    booked = report['closed'][0]
    assert booked['contract_size'] == pytest.approx(100.0)
    assert booked['contract_size_assumed'] is False
    # sold 4351.00, bought back at the 4352.40 ask, 0.1 lots x 100/lot.
    assert booked['pnl'] == pytest.approx((4351.00 - 4352.40) * 0.1 * 100.0)
    assert coordinator.reconciler.untracked_closes == [booked]


def test_an_unknown_symbols_contract_size_is_assumed_and_says_so(engine,
                                                                 legs):
    """1.0, and a note — never a silent guess that reads as a
    measurement."""
    coordinator = engine
    from conftest import FakeSymbol
    legs['acct_b'].broker.symbols['SI1226'] = FakeSymbol(
        'SI1226', 38.0, 38.1, contract_size=5000.0)
    an_orphan(legs, symbol='SI1226', volume=0.1)

    for _ in range(3):
        report = coordinator.reconciler.run()

    booked = report['closed'][0]
    assert booked['contract_size'] == 1.0
    assert booked['contract_size_assumed'] is True
    assert 'ASSUMED' in booked['note']
    assert 'lower bound' in booked['note']


def test_our_own_positions_are_never_orphans(engine, pair, legs):
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)

    for _ in range(3):
        report = coordinator.reconciler.run()

    assert report['orphans'] == []
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()


def test_a_position_whose_close_failed_is_still_ours(engine, pair, legs):
    """A close that did not go through leaves the position OPEN and
    ACTIVE. Dropping its tickets here would turn it into an orphan we
    then close twice."""
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    legs['acct_a'].broker.fail_closes.add(pair.symbol_a)

    closed = coordinator.executor.close_position(pair, position,
                                                 coordinator.market[pair.key])
    assert not closed['ok']
    assert position.is_open                      # still ACTIVE

    for _ in range(3):
        report = coordinator.reconciler.run()
    assert report['orphans'] == []               # leg A is still ours


def test_a_ghost_is_force_cleared_after_three_strikes(engine, pair, legs):
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    # Someone closed leg A by hand, in the terminal.
    legs['acct_a'].broker.positions.clear()

    for expected in (1, 2):
        report = coordinator.reconciler.run()
        assert report['ghosts'][0]['strikes'] == expected
        assert position.is_open

    coordinator.reconciler.run()
    assert not position.is_open
    assert 'force-cleared' in position.close_reason
    assert 'leg A' in position.close_reason


def test_an_account_that_cannot_be_read_produces_no_orphans_and_no_ghosts(
        engine, pair, legs):
    """None is UNKNOWN, not flat. Treating it as flat would clear the
    book while the money is still out there."""
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    legs['acct_a'].positions = lambda symbol=None: None

    for _ in range(4):
        report = coordinator.reconciler.run()

    assert report['unknown_accounts'] == ['acct_a']
    assert report['ghosts'] == []
    assert position.is_open

    # The control: once the account reads again, a genuinely missing leg
    # does strike out.
    legs['acct_a'].positions = lambda symbol=None: []
    for _ in range(3):
        report = coordinator.reconciler.run()
    assert report['ghosts'] and not position.is_open


def test_a_close_that_will_not_go_escalates_once_and_stops_hammering(
        engine, legs):
    """After three attempts: CLOSE IT BY HAND, and then leave the broker
    alone."""
    coordinator = engine
    an_orphan(legs)
    legs['acct_b'].broker.fail_closes.add('GC1226')

    def closes():
        return len([e for e in legs['acct_b'].broker.sent
                    if e['action'] == 'close'])

    # Three strikes to notice it, then CLOSE_ATTEMPTS tries at closing it.
    for _ in range(5):
        report = coordinator.reconciler.run()
    assert closes() == 3
    assert report['escalated'] and 'CLOSE IT BY HAND' in \
        report['escalated'][0]['say']

    # And then it stops. The broker is not asked again.
    for _ in range(5):
        report = coordinator.reconciler.run()
    assert closes() == 3
    assert report['escalated'] == []           # escalated ONCE, not each pass


def test_the_reconciler_runs_on_its_own_interval(engine):
    coordinator = engine
    runs = []
    coordinator.reconciler.run = lambda: runs.append(1)

    coordinator.reconcile_if_due()
    coordinator.reconcile_if_due()
    assert len(runs) == 1                       # not on every poll

    coordinator._last_reconcile -= 21.0
    coordinator.reconcile_if_due()
    assert len(runs) == 2
