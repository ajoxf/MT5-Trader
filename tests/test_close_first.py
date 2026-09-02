"""A click the opposite way REDUCES before it opens.

Reported from the desk: click the red column to sell, then the blue
column to buy back, and the second click opened a SECOND position with
a new ticket instead of closing the first. Both stayed live, both paid
carry, and the ticket count was not what anyone expected.

That is MT5 hedging mode doing exactly what it does: an opposite order
always opens a second position, and the broker will never net for us.
So the netting is done here, by closing tickets OLDEST FIRST.

Two places can create a position, and both go through the same
`book.reduce_first`: a MARKET click closes before it opens, and a
resting LIMIT order closes when it FILLS — because until the price the
trader named actually trades, there is nothing to close against.
"""

import pytest

from mt5trader.book import Book, reduce_first
from mt5trader.models import SpreadSide


class Position:
    """Only what the reduce path reads."""

    def __init__(self, pid, side, quantity, opened_at):
        self.position_id = pid
        self.pair_key = 'P'
        self.side = side
        self.quantity = quantity
        self.opened_at = opened_at
        self.is_open = True


class Pair:
    key = 'P'


class Executor:
    """Records what it was asked to close, and can refuse."""

    def __init__(self, refuse=()):
        self.closed = []
        self.refuse = set(refuse)

    def close_position(self, pair, position, md=None, reason=None):
        if position.position_id in self.refuse:
            return {'ok': False, 'reason': 'broker said no'}
        self.closed.append(position.position_id)
        position.is_open = False
        return {'ok': True}


def book_with(*positions):
    book = Book()
    for position in positions:
        book._positions[position.position_id] = position
    return book


BUY, SELL = SpreadSide.BUY, SpreadSide.SELL


# -- which tickets does a click cover? --------------------------------

def test_it_picks_the_OLDEST_opposite_position_first():
    book = book_with(Position('new', SELL, 1.0, 300),
                     Position('old', SELL, 1.0, 100),
                     Position('mid', SELL, 1.0, 200))
    picked = book.positions_to_reduce('P', BUY, 2.0)
    assert [p.position_id for p in picked] == ['old', 'mid']


def test_it_never_touches_a_position_on_the_SAME_side():
    """The control that matters: a second buy while long must stack,
    not eat the position the trader is building."""
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', BUY, 5.0) == []


def test_it_never_over_closes():
    """`close_position` closes WHOLE tickets. Taking one bigger than
    the click would close more than was asked and flip the net the
    other way, which is worse than not reducing at all."""
    book = book_with(Position('big', SELL, 5.0, 100))
    assert book.positions_to_reduce('P', BUY, 1.0) == []


def test_it_stops_at_the_first_ticket_too_big_to_fit():
    book = book_with(Position('a', SELL, 1.0, 100),
                     Position('big', SELL, 9.0, 200),
                     Position('c', SELL, 1.0, 300))
    picked = book.positions_to_reduce('P', BUY, 3.0)
    # 'c' would fit, but taking it would jump the queue past 'big'.
    assert [p.position_id for p in picked] == ['a']


def test_a_closed_position_is_not_reduced_again():
    shut = Position('done', SELL, 1.0, 100)
    shut.is_open = False
    book = book_with(shut, Position('live', SELL, 1.0, 200))
    picked = book.positions_to_reduce('P', BUY, 5.0)
    assert [p.position_id for p in picked] == ['live']


def test_a_position_can_be_excluded_from_its_own_reduction():
    """The resting-fill path: the position just opened must not close
    itself."""
    book = book_with(Position('mine', BUY, 1.0, 300),
                     Position('old', SELL, 1.0, 100))
    picked = book.positions_to_reduce('P', SELL, 5.0, exclude='mine')
    assert [p.position_id for p in picked] == []


# -- what actually gets closed ----------------------------------------

def test_a_click_that_exactly_covers_leaves_nothing_to_open():
    book = book_with(Position('old', SELL, 1.0, 100))
    ex = Executor()
    closed, left = reduce_first(book, ex, Pair(), BUY, 1.0, None)
    assert closed == ['old']
    assert left == pytest.approx(0.0)
    assert ex.closed == ['old']


def test_a_bigger_click_closes_what_it_covers_and_opens_the_rest():
    book = book_with(Position('old', SELL, 1.0, 100))
    ex = Executor()
    closed, left = reduce_first(book, ex, Pair(), BUY, 3.0, None)
    assert closed == ['old']
    assert left == pytest.approx(2.0), 'the remainder must still open'


def test_a_refused_close_STOPS_the_queue():
    """Stepping over a refusal and opening the remainder on top of a
    position that would not close is how a reduce becomes a BIGGER
    position."""
    book = book_with(Position('a', SELL, 1.0, 100),
                     Position('b', SELL, 1.0, 200))
    ex = Executor(refuse=['a'])
    closed, left = reduce_first(book, ex, Pair(), BUY, 2.0, None)
    assert closed == []
    assert ex.closed == []
    assert left == pytest.approx(2.0)


def test_nothing_open_means_nothing_closed_and_the_click_opens_whole():
    """The CONTROL. Without it every test above passes on a reduce that
    fires unconditionally."""
    ex = Executor()
    closed, left = reduce_first(book_with(), ex, Pair(), BUY, 2.0, None)
    assert closed == []
    assert left == pytest.approx(2.0)
    assert ex.closed == []


def test_the_setting_off_restores_the_old_behaviour_exactly():
    """CLOSE_FIRST off must open every click, as before."""
    from mt5trader.config import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS['CLOSE_FIRST'] is True

    from mt5trader.commands import CommandRunner
    coerce = CommandRunner.HOT_SETTINGS['CLOSE_FIRST']
    assert coerce('false') is False and coerce('off') is False
    assert coerce('true') is True and coerce(1) is True


# -- wiring -----------------------------------------------------------
#
# The tests above exercise `reduce_first` directly. That is not enough
# on its own: an earlier draft of this change called it from the quoter
# WITHOUT importing it, which every test above still passed and which
# would have raised NameError on the first live fill. These check the
# two call sites actually reach it.

def test_the_quoter_can_reach_reduce_first_at_all():
    from mt5trader import quoter
    assert hasattr(quoter, 'book_module'), (
        'quoter does not import the book module — the fill path would '
        'raise NameError on the first fill')
    assert hasattr(quoter.book_module, 'reduce_first')


def test_the_fill_path_reduces_and_excludes_the_position_it_just_opened():
    import inspect
    from mt5trader.quoter import Quoter

    source = inspect.getsource(Quoter)
    assert 'reduce_first' in source, 'a resting fill never reduces'
    assert 'CLOSE_FIRST' in source, 'the fill path ignores the setting'
    # Without the exclude the position just opened is its own oldest
    # opposite ticket on the next click, and a fill would close itself.
    assert 'exclude=position.position_id' in source


def test_the_market_click_reduces_before_it_opens():
    import inspect
    from mt5trader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    assert '_reduce_first' in source
    # Before market_entry, not after: closing first never holds both
    # sides at once, so a disconnect cannot catch it with double on.
    assert source.index('_reduce_first') < source.index('market_entry')


def test_the_coordinator_and_the_quoter_share_ONE_implementation():
    """Two answers to "which tickets does this click cover" is two
    answers to reconcile the day they disagree, on a live book."""
    import inspect
    from mt5trader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._reduce_first)
    assert 'book_module.reduce_first' in source, (
        'the coordinator has its own copy of the reduce loop')


# -- the side arrives as a STRING too ---------------------------------
#
# The bug this section exists for: `side` comes from the command bridge
# as the plain string 'BUY' and from the quoter as a SpreadSide enum.
# The first draft compared with `is not`, which against a string is
# ALWAYS true — so every open position looked opposite and a second buy
# while long CLOSED the trader's own position instead of adding to it.
#
# Every test above passed, because they all passed enums. These do not.

@pytest.mark.parametrize('side', ['BUY', 'buy', ' Buy '])
def test_a_STRING_side_never_reduces_a_position_on_the_same_side(side):
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', side, 5.0) == [], (
        'a same-side click would have closed the trader\'s own position')


@pytest.mark.parametrize('side', ['SELL', 'sell'])
def test_a_STRING_side_still_reduces_the_real_opposite(side):
    """The control. Without it the fix above could just be "never
    reduce anything", which passes the test above and breaks the
    feature."""
    book = book_with(Position('long', BUY, 1.0, 100))
    picked = book.positions_to_reduce('P', side, 5.0)
    assert [p.position_id for p in picked] == ['long']


def test_a_side_nobody_can_read_reduces_NOTHING():
    """Unmeasured is not zero. An unreadable side must not be treated
    as "opposite to everything"."""
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', None, 5.0) == []
    assert book.positions_to_reduce('P', '', 5.0) == []


def test_a_position_whose_side_is_unreadable_is_left_alone():
    odd = Position('odd', BUY, 1.0, 100)
    odd.side = None
    book = book_with(odd)
    assert book.positions_to_reduce('P', 'SELL', 5.0) == []
