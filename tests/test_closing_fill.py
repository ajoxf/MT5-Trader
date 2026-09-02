"""What happens when a CLOSING order fills — and when it half fills.

A reducing click rests a CLOSING order: its pending carries
`position=<ticket>`, so executing it closes that ticket rather than
opening a second one on a hedging account. Everything here is about the
two ways that goes wrong once the broker only does PART of it.

Reported from the desk, live: clicking the blue ladder to close punched
orders onto one account, left the other with none, and the legs went out
of step. Both causes are pinned below.

- A closing pending fills PARTIALLY like any other. Closing the other
  leg in FULL against half a close takes the whole hedge off and leaves
  the quoting leg naked, with the book reporting the position flat.
- Once the quoting leg's ticket is gone, a closing pending against it
  cannot close anything. It rests, it is refused when it executes, and
  the next click rests another one beside it — which is the pile-up the
  desk saw.

Every guard here has a CONTROL that turns the condition off and asserts
the opposite, so none of them can pass by doing nothing.
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


def sell_one(coordinator, pair):
    """A SELL spread ON, the fast way, so the test is about the exit."""
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'])
    assert answer.get('ok'), answer
    coordinator.poll_once()
    return coordinator.book.positions(pair.key)[0]


def click_to_close(coordinator, pair, offset=0.50):
    """The blue ladder, away from the market: rest a close at my price."""
    md = coordinator.market[pair.key]
    return coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'] - offset)


def resting(legs, account='acct_b'):
    return list(legs[account].broker.pendings.values())


def volumes(legs):
    """(leg A lots on, leg B lots on) at the two brokers."""
    return (sum(p['volume'] for p in legs['acct_a'].broker.open_positions()),
            sum(p['volume'] for p in legs['acct_b'].broker.open_positions()))


# -- a closing order that half fills -------------------------------------

def test_a_HALF_filled_close_takes_BOTH_legs_down_together(engine, pair, legs):
    """THE BUG THE DESK FOUND, half of it.

    The quoting leg's pending closed 0.05 of 0.10. The other leg was
    closed IN FULL — the whole hedge off against half a close — so the
    account still holding 0.05 was left naked while the book said the
    position was flat.
    """
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    ticket = list(legs['acct_b'].broker.pendings)[0]
    half = legs['acct_b'].broker.pendings[ticket]['volume'] / 2.0
    legs['acct_b'].broker.part_fill_pending(ticket, half)
    coordinator.poll_once()

    lots_a, lots_b = volumes(legs)
    assert lots_a == pytest.approx(0.05)
    assert lots_b == pytest.approx(0.05)
    assert lots_a == pytest.approx(lots_b), (
        'the legs came down by different amounts — that is a naked leg, '
        'not a spread')
    # ...and the book agrees with the broker rather than claiming flat.
    assert position.is_open is True
    assert position.quantity == pytest.approx(0.5)


def test_the_CONTROL_a_whole_fill_still_closes_the_whole_position(engine,
                                                                  pair, legs):
    """The control for the test above. A close that fills completely
    must still take both legs off and mark the position closed —
    otherwise the partial handling could pass by never closing anything.
    """
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    legs['acct_b'].broker.fill_pending(list(legs['acct_b'].broker.pendings)[0])
    coordinator.poll_once()

    assert volumes(legs) == (0.0, 0.0)
    assert position.is_open is False
    assert not resting(legs)


def test_a_HALF_filled_close_re_rests_the_REMAINDER_and_only_that(engine,
                                                                   pair, legs):
    """The other half of the desk's report: the pile-up.

    The residual of a partially filled closing pending used to be left
    at the broker, untracked, carrying a position ticket the fill had
    just reduced — and the next click rested another one beside it. One
    order, at the size still on, is the only correct answer.
    """
    coordinator = engine
    sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    ticket = list(legs['acct_b'].broker.pendings)[0]
    half = legs['acct_b'].broker.pendings[ticket]['volume'] / 2.0
    legs['acct_b'].broker.part_fill_pending(ticket, half)
    coordinator.poll_once()

    rested = resting(legs)
    assert len(rested) == 1, ('the residual was left resting beside its own '
                              'replacement')
    assert rested[0]['volume'] == pytest.approx(0.05)
    # Still a CLOSE, not an order that would open a second position.
    assert rested[0]['position_ticket'] is not None
    # And nothing was punched at the account that quotes nothing.
    assert not resting(legs, 'acct_a')
    # THE POINT. One pending is not enough on its own — the old code left
    # exactly one too, but it was an ORPHAN: the group was dropped, the
    # position was marked closed, and nobody could pull that order again.
    # An orphan pending is the incident with a GUARANTEED fill.
    tracked = [g for g in coordinator.quoter.snapshot(pair.key)
               if g['ticket'] == rested[0]['ticket']]
    assert tracked, 'the resting order is at the broker and in no group'
    assert tracked[0]['intent'] == 'CLOSE'
    assert coordinator.book.position(tracked[0]['position_id']).is_open


def test_a_HALF_filled_close_banks_only_the_HALF_it_closed(engine, pair,
                                                            legs):
    """P&L is accumulated, not replaced: a position closed in two pieces
    earned it in two pieces, and the first piece is not the trade."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    ticket = list(legs['acct_b'].broker.pendings)[0]
    half = legs['acct_b'].broker.pendings[ticket]['volume'] / 2.0
    legs['acct_b'].broker.part_fill_pending(ticket, half)
    coordinator.poll_once()
    banked = position.realized_pnl
    assert banked is not None

    legs['acct_b'].broker.fill_pending(list(legs['acct_b'].broker.pendings)[0])
    coordinator.poll_once()

    assert position.is_open is False
    assert position.realized_pnl != banked, (
        'the second piece replaced the first instead of adding to it')


def test_a_piece_too_small_for_the_OTHER_legs_step_books_NOTHING(engine, pair,
                                                                  legs):
    """The other leg is the constraint, and its step can be ten times the
    quoting leg's — spot 0.01 against a future's 0.10, which is this
    desk's real shape. A half close on the quoting leg then rounds to
    NOTHING on the other, and reducing our own books by what was ASKED
    would take lots off the record that are still at the broker.
    """
    coordinator = engine
    pair.quoting_leg = 'a'          # so the OTHER leg is the coarse one
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    ticket = list(legs['acct_a'].broker.pendings)[0]
    legs['acct_a'].broker.part_fill_pending(ticket, 0.05)
    coordinator.poll_once()

    # Leg B could not come down by 0.05 — its minimum is 0.10.
    assert volumes(legs)[1] == pytest.approx(0.1)
    # So NOTHING is booked: the record keeps both legs whole, which errs
    # towards believing more is on than there is.
    assert position.is_open is True
    assert position.quantity == pytest.approx(1.0)


# -- a closing order armed against a leg that is already gone -------------

def strand_the_quoting_leg(coordinator, pair, legs):
    """Leave the position half on: leg B gone, leg A still there.

    Live this is a close that went through on one leg and was refused on
    the other — the NAKED LEG the executor already names.
    """
    position = sell_one(coordinator, pair)
    for ticket in list(position.leg_b.position_tickets):
        legs['acct_b'].broker.positions.pop(int(ticket), None)
    return position


def test_a_click_never_rests_against_a_ticket_the_broker_has_lost(engine,
                                                                   pair, legs):
    """THE PILE-UP THE DESK REPORTED, at its source.

    With leg B's ticket gone, a closing pending on leg B can close
    nothing. It used to be rested anyway, and every further click rested
    another one — orders stacking on the quoting account while the
    account actually holding the leg got none of them, and clicking
    again never got the trader out.

    What is left is one naked leg, and a naked leg cannot wait at a
    price.
    """
    coordinator = engine
    position = strand_the_quoting_leg(coordinator, pair, legs)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('ok') is True
    # Said out loud: a resting click that went to market is exactly the
    # surprise the other direction already guards against.
    assert answer.get('at_market') is True
    assert position.position_id in (answer.get('closed') or [])
    assert 'NAKED' in answer['reason']
    # Nothing rested anywhere, and the leg that was on is off.
    assert not resting(legs) and not resting(legs, 'acct_a')
    assert volumes(legs) == (0.0, 0.0)
    assert position.is_open is False


def test_the_CONTROL_a_whole_position_still_RESTS_where_it_is_clicked(
        engine, pair, legs):
    """The control. With both legs where they should be, the click must
    still rest at the trader's level and must NOT go to market —
    otherwise the guard above could pass by flattening everything."""
    coordinator = engine
    sell_one(coordinator, pair)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('ok') is True
    assert answer.get('at_market') is not True
    assert answer.get('reducing') is True
    assert len(resting(legs)) == 1
    assert volumes(legs) == (0.1, 0.1)


def test_a_leg_that_cannot_be_READ_is_not_treated_as_flat(engine, pair, legs,
                                                           monkeypatch):
    """`positions()` returns None for "the leg could not be read", which
    is NOT "no position" (spec §7). Reading it as flat would market-close
    a live spread on no evidence at all — the opposite mistake, and the
    worse one."""
    coordinator = engine
    sell_one(coordinator, pair)
    monkeypatch.setattr(legs['acct_b'], 'positions', lambda symbol=None: None)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('at_market') is not True, (
        'an unreadable leg was read as flat and the position was closed '
        'at market on no evidence')
    assert volumes(legs) == (0.1, 0.1)


# -- whose close was it ---------------------------------------------------

def test_the_traders_own_close_is_not_recorded_as_a_take_profit(engine, pair,
                                                                 legs):
    """A close the trader clicked is not automation. Reporting it as a
    take-profit is the same mistake the AutoRouting switch made when it
    swept their order away."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    legs['acct_b'].broker.fill_pending(list(legs['acct_b'].broker.pendings)[0])
    coordinator.poll_once()

    assert position.close_reason == 'closed by the trader'


def test_the_CONTROL_autoroutings_close_still_says_take_profit(engine, pair,
                                                               legs):
    """The control for the test above."""
    coordinator = engine
    pair.auto_route = True
    coordinator.config.settings['AUTO_ROUTE_ENABLED'] = True
    coordinator.config.settings['TP_TARGET_PCT_OF_MARGIN'] = 2.0
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0

    position = sell_one(coordinator, pair)
    coordinator.poll_once()
    armed = coordinator.book.orders_for_position(position.position_id)
    assert armed and armed[0].auto_armed is True

    legs['acct_b'].broker.fill_pending(list(legs['acct_b'].broker.pendings)[0])
    coordinator.poll_once()

    assert position.close_reason == 'auto take-profit'


# -- a cancel that races a closing fill -----------------------------------

def test_a_cancel_that_races_a_closing_fill_keeps_the_TICKET(engine, pair,
                                                              legs,
                                                              monkeypatch):
    """`_on_fill` takes the ticket off the group itself. Clearing it in
    `_pull` FIRST handed it None, so the fill was reported as having
    closed no order at all — the one number a diagnostic needs to match
    our close against the broker's."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    ticket = list(legs['acct_b'].broker.pendings)[0]

    seen = {}
    real = coordinator.executor.close_other_leg

    def spy(pair_, position_, quote_leg, quote_result, **kw):
        seen.update(quote_result)
        return real(pair_, position_, quote_leg, quote_result, **kw)

    monkeypatch.setattr(coordinator.executor, 'close_other_leg', spy)

    # It fills at the moment the trader's disarm reaches the broker.
    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.quoter.disarm(position.position_id, 'cancelled by trader')

    assert position.is_open is False, 'the raced fill was tidied away'
    assert seen.get('closed'), 'the raced fill was never booked as a close'
    assert seen['closed'][0]['ticket'] == ticket


def test_the_synthetic_stops_working_once_its_close_is_whole(engine, pair,
                                                             legs):
    """A partial leaves the click WORKING for the rest; a whole fill must
    not. The control for the partial-settlement path."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    order = coordinator.book.orders_for_position(position.position_id)[0]

    ticket = list(legs['acct_b'].broker.pendings)[0]
    half = legs['acct_b'].broker.pendings[ticket]['volume'] / 2.0
    legs['acct_b'].broker.part_fill_pending(ticket, half)
    coordinator.poll_once()
    assert order.is_working is True, 'the remainder stopped working'

    legs['acct_b'].broker.fill_pending(list(legs['acct_b'].broker.pendings)[0])
    coordinator.poll_once()
    assert order.state is OrderState.FILLED
