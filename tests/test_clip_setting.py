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
