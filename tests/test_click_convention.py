"""Which COLUMN sends which side, and nothing else.

The desk arrives from TT, where clicking the BIDS column joins the bid
and is a resting BUY. This app read the other way — asks buy — so every
click a trader made was the opposite of the one they intended.
"""

from mt5trader.commands import CommandRunner
from mt5trader.config import DEFAULT_SETTINGS


def coerce(value):
    return CommandRunner.HOT_SETTINGS['CLICK_CONVENTION'](value)


def test_the_default_changes_nothing_that_was_already_true():
    """Adding the switch must not move a single existing click.
    TOUCH is what this app has always done, so the whole suite —
    the end-to-end money path included — goes on asserting the
    same thing. Flipping the default to TT is a separate
    decision, made on its own."""
    assert DEFAULT_SETTINGS['CLICK_CONVENTION'] == 'TOUCH'


def test_tt_is_reachable_because_that_is_the_point():
    assert coerce('TT') == 'TT'
    assert coerce('tt') == 'TT'


def test_it_is_hot_so_the_desk_can_be_shown_both():
    """A restart to settle an argument about a click is a restart
    nobody makes; it has to change under them."""
    assert 'CLICK_CONVENTION' in CommandRunner.HOT_SETTINGS


def test_only_touch_turns_it_off():
    assert coerce('TOUCH') == 'TOUCH'
    assert coerce('touch') == 'TOUCH'
    assert coerce('  Touch  ') == 'TOUCH'


def test_anything_unrecognised_falls_back_to_tt():
    """A ladder with no convention at all is a ladder whose clicks mean
    nothing. Garbage picks the safe reading rather than breaking."""
    for junk in ('', None, 'nonsense', 0, 'BUY'):
        assert coerce(junk) == 'TT', junk


def test_the_engine_never_sees_a_column():
    """The guarantee the whole change rests on: the side reaches the
    coordinator already decided, so sizing, hedging and execution are
    identical under either convention."""
    import inspect
    from mt5trader.coordinator import Coordinator
    source = inspect.getsource(Coordinator._click)
    assert "'ask'" not in source and "'bid'" not in source
