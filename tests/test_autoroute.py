"""AutoRouting: on a fill, rest the exit the trader would have placed.

Per-ladder, DEFAULT OFF. It is not a strategy — it places one closing
order, at a level derived from settings the trader typed, on a position
that already exists. No signal, no re-entry, no loop.

The dangerous parts, each with a test here:

- the closing pending must carry `position=<ticket>`, or on a hedging
  account it OPENS a second position instead of closing one;
- it must never be merged into an opening pending at the same level;
- it must be PULLED before its position goes, because an auto-TP left
  resting is an orphan pending with a guaranteed fill;
- it must be sized to what actually filled;
- it arms a target and NO stop.
"""

import pytest

from mt5trader.coordinator import Coordinator
from mt5trader.models import OrderState, SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def market_entry(coordinator, pair, side=SpreadSide.BUY):
    """Get a pair ON the fast way, so the test is about the exit."""
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, side, md['long_spread'])
    assert answer.get('ok'), answer
    return coordinator.book.positions(pair.key)[0]


def test_off_by_default_nothing_is_armed(engine, pair, legs):
    """The control for every test below. A switch that is on by default
    is a system that places orders nobody asked for."""
    coordinator = engine
    assert pair.auto_route is False

    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    assert coordinator.book.orders_for_position(position.position_id) == []
    assert not [p for p in legs['acct_b'].broker.pendings.values()]


def test_a_fill_arms_a_closing_order_at_the_take_profit(engine, pair, legs):
    """Entry, then a working order resting at the TP — and the level is
    anchored on the price the position was ENTERED at, not on the quote
    the click was taken at. Levels anchored on the quote while P&L is
    measured from the fill name a level the engine would not fire at."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    coordinator.config.settings['TP_TARGET_PCT_OF_MARGIN'] = 2.0
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0

    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    armed = coordinator.book.orders_for_position(position.position_id)
    assert len(armed) == 1
    order = armed[0]
    # A long is closed by SELLING the spread.
    assert order.side is SpreadSide.SELL
    assert order.level > position.entry_spread      # a target, above cost
    assert order.position_id == position.position_id


def test_a_take_profit_rests_NOTHING_at_the_broker(engine, pair, legs):
    """A closing order CANNOT be a broker pending. MT5 honours
    `position` on TRADE_ACTION_DEAL and ignores it on
    TRADE_ACTION_PENDING, so a "closing" limit rests as an ordinary one
    and OPENS a second position on a hedging account — live 2026-09-02,
    ticket 2092 filled as a BUY beside the SELL it was meant to close.

    Attaching a broker take-profit to the leg instead is not the answer
    either: one leg closing alone converts the hedge into a naked
    outright. So the level is held HERE."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    assert not legs['acct_a'].broker.pendings
    assert not legs['acct_b'].broker.pendings, (
        'a closing pending is at the broker, and it OPENS')
    watching = [g for g in coordinator.quoter.snapshot(pair.key)
                if g['intent'] == 'CLOSE']
    assert len(watching) == 1
    assert watching[0]['position_id'] == position.position_id
    assert watching[0]['ticket'] is None


def test_a_take_profit_is_NEVER_merged_into_an_entry_at_the_same_level(
        engine, pair, legs):
    """The aggregation that turns three clicks into one pending must key
    on the position too — otherwise an auto-TP close is merged into an
    entry order and one of them silently changes meaning."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    level = coordinator.book.orders_for_position(
        position.position_id)[0].level

    # The trader clicks SELL at exactly the same level. With
    # CLOSE_FIRST OFF that is a plain opening order, and it must NOT be
    # merged into the take-profit sitting at the same price — which is
    # what this test was written for.
    coordinator.config.settings['CLOSE_FIRST'] = False
    pair.order_type = pair.order_type.__class__('LIMIT')
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()

    groups = coordinator.quoter.snapshot(pair.key)
    assert len(groups) == 2, groups
    intents = sorted(g['intent'] for g in groups)
    assert intents == ['CLOSE', 'OPEN']


def test_with_CLOSE_FIRST_that_same_click_REDUCES_and_keeps_its_queue_place(
        engine, pair, legs):
    """The other half of the same click.

    With CLOSE_FIRST on, a SELL click while LONG means "close my long",
    so it rests a CLOSE and no entry is created at all. And when a
    target is ALREADY resting at that level, the click must leave it
    exactly where it is: pulling a live pending and putting an
    identical one back loses its place in the broker's queue, which
    costs the trader a fill for no change whatsoever.
    """
    coordinator = engine
    pair.auto_route = True
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    coordinator.config.settings['CLOSE_FIRST'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    armed = coordinator.book.orders_for_position(position.position_id)[0]
    level = armed.level

    pair.order_type = pair.order_type.__class__('LIMIT')
    answer = coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()

    assert answer.get('reducing') is True, answer
    groups = coordinator.quoter.snapshot(pair.key)
    assert [g['intent'] for g in groups] == ['CLOSE'], (
        'the click opened something instead of reducing')
    # THE SAME order, not a replacement: its id is its place in the
    # queue.
    still = coordinator.book.orders_for_position(position.position_id)
    assert [o.order_id for o in still] == [armed.order_id], (
        'the resting order was pulled and re-placed, losing its queue '
        'position for nothing')


def test_the_take_profit_filling_closes_the_position_both_legs(engine, pair,
                                                               legs,
                                                               gold_symbols):
    """The quoting leg is closed by the pending itself; the other leg is
    closed BY TICKET, at market, immediately — between the two it is an
    outright position in gold, not a basis trade."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    level = [g for g in coordinator.quoter.snapshot(pair.key)
             if g['intent'] == 'CLOSE'][0]['level']

    reach_level(coordinator, pair, gold_symbols, level, SpreadSide.SELL)
    coordinator.poll_once()

    assert position.is_open is False
    assert position.close_reason == 'auto take-profit'
    # Both accounts are flat, and leg A was closed by TICKET rather than
    # offset with an opposite order.
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    closes = [e for e in legs['acct_a'].broker.sent if e['action'] == 'close']
    assert closes and closes[-1]['ticket'] == \
        position.leg_a.position_tickets[0]


def test_closing_the_position_by_hand_PULLS_the_take_profit_first(engine,
                                                                   pair, legs):
    """A target left armed after its position is gone would fire at the
    next tick that reaches it and close something that is not there."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    assert coordinator.book.orders_for_position(position.position_id)

    coordinator.executor.close_position(
        pair, position, coordinator.market[pair.key], reason='by hand')

    # The watch is gone, and the order says so.
    assert not [g for g in coordinator.quoter.snapshot(pair.key)
                if g['intent'] == 'CLOSE']
    order = coordinator.book.orders_for_position(position.position_id,
                                                 working_only=False)[0]
    assert order.state is OrderState.CANCELLED


def reach_level(coordinator, pair, gold_symbols, level, side):
    """Walk leg B until the spread reaches `level` on `side`.

    The spread is B - beta*A, so moving leg B moves it one-for-one.
    """
    _spot, future = gold_symbols
    md = coordinator.market[pair.key]
    have = md['short_spread'] if side is SpreadSide.SELL else md['long_spread']
    move = (level - have) + (0.01 if side is SpreadSide.SELL else -0.01)
    future.bid += move
    future.ask += move
    coordinator.poll_once()


def test_a_position_that_vanished_takes_its_take_profit_with_it(engine, pair,
                                                                 legs):
    """Belt and braces for the paths that do not go through the
    executor at all — a reconciler force-clear, a ghost. The order is
    pulled on the next pass rather than left resting."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    # Force-cleared behind the executor's back.
    position.closed_at = coordinator.clock()
    coordinator.poll_once()

    assert not legs['acct_b'].broker.pendings
    assert coordinator.book.orders_for_position(position.position_id) == []


def test_a_target_closes_the_POSITION_however_much_of_it_filled(engine, pair,
                                                                legs):
    """A close targets TICKETS and takes their size from the broker, so
    a partially filled entry needs no separately sized target — the one
    thing the old sized pending got right, now free."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    armed = coordinator.book.orders_for_position(position.position_id)
    assert len(armed) == 1
    assert armed[0].quantity == pytest.approx(position.quantity)


def test_a_restart_re_arms_from_the_frozen_levels_and_SAYS_SO(engine, pair,
                                                              legs):
    """This deliberately differs from the rule that nothing placing
    orders by itself may resume after a restart. That rule is about a
    replayed ENTRY, which creates risk nobody chose. A re-armed target
    places a CLOSING order on a position that already exists — it
    reduces exposure — and the worse failure here is SILENT: a trader
    who believes a target is armed when it is not."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    position.recovered = True          # as `recover()` marks it

    events = coordinator.work_auto_route(pair, coordinator.market[pair.key])

    armed = [e for e in events if e['action'] == 'auto_route_armed']
    assert len(armed) == 1
    assert armed[0]['recovered'] is True
    assert armed[0]['position'] == position.position_id


def test_a_target_the_trader_cancelled_is_not_quietly_re_armed(engine, pair,
                                                               legs):
    """A cancel is an instruction. Re-arming it on the next poll would
    be the system arguing with the trader three times a second."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    order = coordinator.book.orders_for_position(position.position_id)[0]

    coordinator.cancel_order(order.order_id)
    coordinator.poll_once()

    assert coordinator.book.orders_for_position(position.position_id) == []


def test_no_take_profit_level_means_nothing_is_armed(engine, pair, legs):
    """No margin priced, no target — and therefore no order. A closing
    order at a level built on half a number is worse than none."""
    coordinator = engine
    pair.auto_route = True
    # The desk's master switch, which every ladder's own box sits under.
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    coordinator.config.settings['TP_TARGET_PCT_OF_MARGIN'] = 0
    legs['acct_a'].broker.margin_per_lot = None
    legs['acct_b'].broker.margin_per_lot = None
    for leg in legs.values():
        leg.broker.leverage = None

    position = market_entry(coordinator, pair)
    coordinator.poll_once()

    # Break-even alone is not a target, and nothing rests on it.
    assert coordinator.book.orders_for_position(position.position_id) == []


def test_the_master_switch_stands_every_ladder_down(engine, pair, legs):
    """One switch for the desk, not one per ladder. Off, no ladder arms
    a target however its own box is ticked — and anything already
    resting is PULLED, because standing automation down must never
    leave an order behind it."""
    coordinator = engine
    pair.auto_route = True
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    position = market_entry(coordinator, pair)
    coordinator.poll_once()
    assert coordinator.book.orders_for_position(position.position_id)

    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = False
    coordinator.poll_once()

    assert coordinator.book.orders_for_position(position.position_id) == []
    assert not legs['acct_b'].broker.pendings


def test_off_by_default_the_master_switch_is_off(config):
    """A desk that has never heard of AutoRouting has none of it."""
    assert config.get('AUTO_ROUTE_ENABLED') is False
