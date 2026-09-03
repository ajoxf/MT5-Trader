"""How many LOTS one spread is, and who gets to say.

Everything the ladder prints in money runs through one number: what a
click of "1 spread" means in leg lots. It was derived and never
exposed — the smallest size at which both legs clear their own minimum
volume, which on this desk's broker is 0.01 — so a trader who wanted to
trade in tenths had nowhere to say so.

Leg B is NOT the second half of that setting. It is the HEDGE of leg A,
`L_A x C_A / (beta x C_B)` rounded down, and a number typed into it
independently is how a "hedged" pair ends up net long one leg.
"""

import pytest

from mt5trader.config import PairConfig
from mt5trader.coordinator import Coordinator


@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


# -- what the engine derives when nobody has said -------------------------

def test_unset_it_is_the_smallest_size_BOTH_legs_can_clear(engine, pair):
    """The control, and the old behaviour: leg B's minimum is ten times
    leg A's on this broker, so the floor is leg B's."""
    assert pair.clip_lots_a == pytest.approx(0.10)
    assert pair.clip_lots_b == pytest.approx(0.10)


def test_the_trader_s_leg_A_is_kept_and_leg_B_is_DERIVED(config, legs):
    """Set leg A; leg B follows from the hedge arithmetic, not from a
    second box."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 0.50, None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()

    assert pair.clip_lots_a == 0.50
    # Equal contract sizes at beta 1: the hedge is one for one.
    assert pair.clip_lots_b == pytest.approx(0.50)


def test_the_hedge_is_ROUNDED_DOWN_to_leg_Bs_step(config, legs,
                                                   gold_symbols):
    """Leg B's step is 0.10 here. Nearest would turn a wanted 0.55 into
    0.60 — a hedge bigger than the position it hedges, net short the
    difference. Short is the recoverable error."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 0.55, None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    assert pair.clip_lots_b == pytest.approx(0.50)


def test_a_leg_A_too_small_to_hedge_leaves_leg_B_UNSET(config, legs):
    """0.01 of leg A needs 0.01 of leg B, and leg B cannot trade under
    0.10. Inventing 0.10 there would silently trade ten times the size
    asked for; the honest state is unsized, and the click is refused
    with the minimum named."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 0.01, None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()

    assert pair.clip_lots_b is None
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, 'BUY', md['long_spread'])
    assert answer['ok'] is False
    # Named, with the number that would fix it — never quietly resized
    # up to the floor, which here is ten times what was asked for.
    assert '0.1' in answer['reason'] and 'minimum' in answer['reason']


# -- changing it, live ----------------------------------------------------

def test_a_saved_lot_size_applies_without_a_restart():
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {},
                                      'clip_lots_a': 0.10,
                                      'clip_lots_b': 0.10})
    changed = pair.apply_hot({'clip_lots_a': 0.50})
    assert 'clip_lots_a' in changed
    assert pair.clip_lots_a == 0.50
    # ...and leg B is DROPPED, so it is re-derived against the new leg
    # A. Keeping the old one would leave two legs that no longer hedge.
    assert pair.clip_lots_b is None


def test_clearing_it_goes_back_to_the_derived_floor(config, legs):
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 0.50, 0.50
    pair.apply_hot({'clip_lots_a': None})

    assert pair.clip_lots_a is None and pair.clip_lots_b is None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    assert pair.clip_lots_a == pytest.approx(0.10)


def test_a_hot_apply_that_does_not_mention_it_leaves_it_alone(config):
    """The control: every save sends the whole form, and a field that
    is not in the payload must not be reset by one that is."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 0.50, 0.50
    pair.apply_hot({'increment': 0.02})
    assert (pair.clip_lots_a, pair.clip_lots_b) == (0.50, 0.50)


# -- how this ladder gets OUT, by default ---------------------------------

def test_the_exit_type_defaults_to_MARKET():
    """The way out crosses now unless the trader says otherwise. A
    default that WAITS is a position nobody is getting out of."""
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {}})
    assert pair.exit_type.value == 'MARKET'


def test_the_exit_type_is_selectable_and_applies_without_a_restart():
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {}})
    assert 'exit_type' in pair.apply_hot({'exit_type': 'LIMIT'})
    assert pair.exit_type.value == 'LIMIT'
    # ...and it survives a save/load round trip.
    assert PairConfig.from_dict('K', pair.to_dict()).exit_type.value == 'LIMIT'


def test_a_blank_exit_type_is_MARKET_not_an_exception():
    """A config written by an older UI carries no exit type at all, and
    `OrderType(None)` raising inside the launcher is what took the
    engine down for nineteen hours once already."""
    assert PairConfig.from_dict(
        'K', {'leg_a': {}, 'leg_b': {}, 'exit_type': None}
    ).exit_type.value == 'MARKET'
    # The control: a value that is not one of the two is still refused.
    with pytest.raises(ValueError):
        PairConfig.from_dict('K', {'exit_type': 'WHENEVER'})


def test_the_exit_type_never_changes_what_reaches_the_broker(engine, pair):
    """It selects WHEN, not what. Both settings close by TICKET at
    market — a closing PENDING would open a second position on a
    hedging account, which is why there is no third option."""
    pair.exit_type = pair.exit_type.__class__('LIMIT')
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]
    assert engine.click(pair.key, 'BUY', md['long_spread'])['ok']

    # CLOSE ALL still crosses now, with the exit type set to LIMIT.
    position = engine.book.positions(pair.key)[0]
    engine.executor.close_position(pair, position,
                                   engine.market.get(pair.key),
                                   reason='flattened by trader')
    assert position.is_open is False


# -- HOW the two legs are matched, end to end -----------------------------

def test_a_spot_future_pair_is_matched_LOT_FOR_LOT_by_default(config, legs):
    """The desk's rule: the underlying is the same instrument, so a lot
    of one is a lot of the other."""
    pair = config.pairs['XAUUSD_|GC1226']
    assert pair.pair_type == 'SPOT_FUTURE'
    assert pair.sizing_basis == 'AUTO'
    pair.clip_lots_a, pair.clip_lots_b = 0.50, None
    Coordinator(config, legs, sleep=lambda s: None).start()
    assert pair.clip_lots_b == pytest.approx(0.50)


def test_a_RELATED_pair_is_matched_by_NOTIONAL_by_default(config, legs,
                                                           gold_symbols):
    """Two different instruments: nothing shared to match lot for lot,
    so the money on each side is what makes them comparable. Leg A is
    4292 and leg B 4351 here, so the dearer leg needs fewer lots."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.pair_type = 'RELATED'
    pair.clip_lots_a, pair.clip_lots_b = 1.0, None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    coordinator._settle_clip(pair)

    spot, future = gold_symbols
    want = (100.0 * ((spot.bid + spot.ask) / 2)) / \
        (100.0 * ((future.bid + future.ask) / 2))
    # Rounded DOWN to leg B's 0.10 step, as every hedge is.
    assert pair.clip_lots_b == pytest.approx(round(want - want % 0.10, 8),
                                             abs=0.05)
    assert pair.clip_lots_b < 1.0        # the dearer leg, so fewer lots


def test_the_trader_can_override_the_pair_types_rule(config, legs):
    """The control: AUTO is a default, not a rule that cannot be
    changed. Sizing by UNITS is the old behaviour and still on offer —
    it is the only basis that cancels exactly."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.pair_type = 'RELATED'
    pair.sizing_basis = 'SAME_LOTS'
    pair.clip_lots_a, pair.clip_lots_b = 1.0, None
    Coordinator(config, legs, sleep=lambda s: None).start()
    assert pair.clip_lots_b == pytest.approx(1.0)


def test_changing_the_basis_re_derives_leg_B(config):
    """Leg B is the hedge. Left where it was, the two legs would no
    longer be matched on the basis the trader just chose."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 1.0
    assert 'sizing_basis' in pair.apply_hot({'sizing_basis': 'NOTIONAL'})
    assert pair.clip_lots_b is None


def test_a_fill_on_the_quoting_leg_crosses_the_other_on_the_SAME_basis(
        config, legs, gold_symbols):
    """A LIMIT fill hedges through the quoter and a MARKET click through
    the executor. Two paths sizing the same trade differently is a pair
    that is hedged on one route and not on the other."""
    from mt5trader import sizing
    pair = config.pairs['XAUUSD_|GC1226']
    pair.pair_type = 'RELATED'                  # so the basis is NOTIONAL
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()

    md = coordinator.market[pair.key]
    basis = sizing.basis_for(pair.pair_type, pair.sizing_basis)
    assert basis == 'NOTIONAL'
    per_lot = sizing.hedge_per_lot(
        basis, pair.meta_a['contract_size'], pair.meta_b['contract_size'],
        pair.hedge_ratio, md['leg_a_mid'], md['leg_b_mid'])
    assert per_lot != pytest.approx(1.0)        # or the test proves nothing
    assert pair.clip_lots_b / pair.clip_lots_a == pytest.approx(
        per_lot, rel=0.05)


# -- the size that cannot be hedged at all --------------------------------

def test_a_clip_too_small_for_the_BASIS_names_the_size_that_works():
    """Live 2026-09-03, the desk's oil pair. WTI against Brent is
    RELATED, so AUTO matches the legs BY NOTIONAL — and the ratio is
    0.919, so a leg A of 0.01 needs 0.0092 of leg B. That is under a
    0.01 minimum and rounds DOWN to nothing.

    Leg B never settled, so `clip_plan` refused every click. The ladder
    looked entirely normal: the click landed, the order joined the book
    and the Work cell filled in — and nothing ever reached either
    broker. W:1 (broker 0), in 9px, was the only sign.

    The refusal must therefore carry the number that fixes it. "the
    hedge is under the minimum" is true and useless: 0.11 is not
    guessable from it.
    """
    from mt5trader import sizing
    meta = {'contract_size': 1000.0, 'volume_min': 0.01,
            'volume_step': 0.01, 'volume_max': 100.0}
    pair = PairConfig.from_dict('USOILX6|UKOILX6', {
        'leg_a': {}, 'leg_b': {}, 'pair_type': 'RELATED',
        'hedge_ratio': 1.0, 'clip_lots_a': 0.01})
    assert sizing.basis_for(pair.pair_type, pair.sizing_basis) == 'NOTIONAL'
    # The hedge for the smallest clip rounds away entirely.
    assert sizing.hedge_lots(0.01, 1000.0, 1000.0, 1.0, 0.01, 0.01,
                             basis='NOTIONAL', price_a=88.63,
                             price_b=96.475) == 0.0

    plan = sizing.clip_plan(pair, meta, meta, 88.63, 96.475, 100.0)

    assert plan['leg_a_lots'] == 0.0 and plan['leg_b_lots'] == 0.0
    assert '0.11' in plan['reason'], plan['reason']
    assert 'Lots/spread A' in plan['reason']
    assert plan['workable_lots_a'] == pytest.approx(0.11)
    # ...and 0.11 really is workable, or the message sends the trader
    # somewhere that fails again.
    pair.clip_lots_a = plan['workable_lots_a']
    pair.clip_lots_b = sizing.hedge_lots(
        pair.clip_lots_a, 1000.0, 1000.0, 1.0, 0.01, 0.01,
        basis='NOTIONAL', price_a=88.63, price_b=96.475)
    assert sizing.clip_plan(pair, meta, meta, 88.63, 96.475,
                            1.0)['reason'] is None


def test_the_same_pair_matched_LOT_FOR_LOT_sizes_at_the_minimum():
    """The control, and the way out: the ratio is what cannot be
    expressed at 0.01, not the pair. Lot for lot needs no ratio."""
    from mt5trader import sizing
    meta = {'contract_size': 1000.0, 'volume_min': 0.01,
            'volume_step': 0.01, 'volume_max': 100.0}
    pair = PairConfig.from_dict('USOILX6|UKOILX6', {
        'leg_a': {}, 'leg_b': {}, 'pair_type': 'RELATED',
        'sizing_basis': 'SAME_LOTS', 'hedge_ratio': 1.0,
        'clip_lots_a': 0.01, 'clip_lots_b': 0.01})
    plan = sizing.clip_plan(pair, meta, meta, 88.63, 96.475, 100.0)
    assert plan['reason'] is None
    assert plan['leg_a_lots'] == pytest.approx(1.0)
    assert plan['leg_b_lots'] == pytest.approx(1.0)
