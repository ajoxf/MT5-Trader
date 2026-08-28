"""Reconciliation: orphans, ghosts, and the numbers that must be right
when the engine is cleaning up after itself.
"""

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import OrderSide, SpreadSide


@pytest.fixture
def engine(config, pair, legs, tmp_path):
    """A coordinator with a database, started cleanly.

    The database is what makes the book COMPLETE: without one, recovery
    cannot know what was open, and the reconciler holds its fire rather
    than call a live position an orphan.
    """
    from mt5trader.database import Store
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()
    assert coordinator.reconciler.book_complete
    return coordinator


def an_orphan(legs, symbol='GC1226', side=OrderSide.SELL, volume=0.1,
              account='acct_b'):
    """A position carrying OUR magic that the book has never heard of.

    That is what an orphan is: a fill of ours we lost track of — not the
    trader's own terminal click, which carries a different magic and is
    never ours to touch.
    """
    result = legs[account].broker.send_market_order(symbol, side, volume,
                                                    comment='LADDER-lost')
    return result.ticket


def a_manual_trade(legs, symbol='GC1226', side=OrderSide.SELL, volume=0.1,
                   account='acct_b'):
    """The trader's own click in the terminal. Different magic."""
    result = legs[account].broker.send_market_order(symbol, side, volume,
                                                    comment='by hand')
    return result.ticket


def test_the_traders_own_terminal_position_is_never_touched(engine, legs):
    """Magic-scoped, always. Every query, every sweep, every close."""
    coordinator = engine
    ticket = a_manual_trade(legs)

    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['orphans'] == []
    assert report['closed'] == []
    assert legs['acct_b'].broker.positions.get(ticket)


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


def test_a_position_already_there_at_startup_is_never_auto_closed(
        config, pair, legs, tmp_path):
    """A position we cannot explain is exactly the one an automatic
    close must not touch. It goes on the screen for a person instead."""
    from mt5trader.database import Store
    an_orphan(legs)                      # it is there BEFORE we start
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()

    assert len(coordinator.recovery['unclaimed']) == 1
    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['closed'] == []
    assert legs['acct_b'].broker.open_positions()          # still there
    assert report['unclaimed'][0]['symbol'] == 'GC1226'
    # And it is in the snapshot the monitor renders from.
    assert coordinator.snapshot()['reconciler']['unclaimed']


def test_without_a_database_nothing_is_auto_closed_at_all(config, pair, legs):
    """No database means the book cannot be known to be complete, and an
    orphan is only an orphan if we are sure it is not ours."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    an_orphan(legs)

    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['book_complete'] is False
    assert report['closed'] == []
    assert 'incomplete' in report['orphans'][0]['held']
    assert legs['acct_b'].broker.open_positions()
    assert 'no database' in coordinator.recovery['error']


def test_an_unclaimed_pair_can_be_adopted_and_is_then_managed(
        config, pair, legs, tmp_path):
    """For what the operator can see and we cannot: two legs that went
    on before this build. Adopted, it is marked and closable like any
    other position."""
    from mt5trader.database import Store
    from mt5trader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.BUY, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()
    assert len(coordinator.recovery['unclaimed']) == 2

    adopted = coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)

    assert adopted['ok']
    position = coordinator.book.positions(pair.key)[0]
    assert position.side.value == 'SELL'          # short B = short the spread
    assert position.leg_b.position_tickets == [ticket_b]
    assert coordinator.reconciler.unclaimed == {}
    # It survives a restart now, like anything else in the book.
    assert Store(str(tmp_path / 'test.db')).open_positions()


def test_adopting_two_tickets_that_are_not_a_hedge_is_refused(
        config, pair, legs, tmp_path):
    """The control: adopting two positions on the SAME side would book
    a spread that does not exist and mark it against a hedge nobody
    holds."""
    from mt5trader.database import Store
    from mt5trader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.SELL, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()

    refused = coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)

    assert not refused['ok']
    assert 'not a hedge' in refused['reason']
    assert coordinator.book.positions(pair.key) == []


def test_an_unclaimed_position_can_be_closed_by_hand(config, pair, legs,
                                                      tmp_path):
    from mt5trader.database import Store
    ticket = an_orphan(legs)
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()

    result = coordinator.close_unclaimed('acct_b', ticket)

    assert result['ok']
    assert legs['acct_b'].broker.open_positions() == []
    assert coordinator.reconciler.unclaimed == {}


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


def test_our_own_leg_seen_through_a_second_account_is_not_an_orphan(engine,
                                                                    legs):
    """Two configured accounts attached to ONE MT5 terminal see each
    other's positions. Every leg we open then appears on the other
    account with no entry in our book — and the reconciler closed it
    within seconds of it being placed. On a live box that ate every
    trade the desk tried to put on.

    A ticket we already hold is ours, whichever account name it comes
    back under: tickets are unique inside a terminal, and two accounts
    reporting the same one are one terminal.
    """
    coordinator = engine
    pair = list(coordinator.config.pairs.values())[0]
    from mt5trader.models import OrderType
    pair.order_type = OrderType.MARKET          # a POSITION, not an order
    answer = coordinator.click(
        pair.key, SpreadSide.BUY,
        coordinator.market[pair.key]['long_spread'])
    assert answer.get('ok'), answer
    assert coordinator.book.positions()

    # The other terminal connection now reports the same tickets.
    for name, leg in legs.items():
        for other_name, other in legs.items():
            if other_name == name:
                continue
            for ticket, position in list(other.broker.positions.items()):
                leg.broker.positions.setdefault(ticket, dict(position))

    closed = []
    for _ in range(5):                       # well past the strike count
        closed += coordinator.reconciler.run()['closed']

    assert closed == [], closed
    assert coordinator.book.positions(), 'our own position was cleared'


def test_a_genuine_orphan_is_still_closed(engine, legs):
    """The control. Without it the fix above would pass on a reconciler
    that never closes anything — and an unhedged leg left by a crash is
    exactly what this machinery is for."""
    coordinator = engine
    an_orphan(legs)

    closed = []
    for _ in range(5):
        closed += coordinator.reconciler.run()['closed']

    assert closed, 'a real orphan was left at the broker'
