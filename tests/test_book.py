"""Working orders: one click, one order — however many land on a price."""

import pytest

from mt5trader.book import Book
from mt5trader.models import SpreadSide


def test_three_clicks_at_one_price_are_three_cancellable_orders(pair):
    """Not one order of 3. The trader must be able to pull one of them
    and leave the other two working."""
    book = Book()
    orders = [book.add_order(pair, SpreadSide.BUY, 58.40, 1.0)
              for _ in range(3)]

    assert len({o.order_id for o in orders}) == 3
    assert book.working_at(pair.key, 58.40) == (3.0, 0.0)

    book.cancel(orders[1].order_id)

    assert len(book.orders(pair.key)) == 2
    assert book.working_at(pair.key, 58.40) == (2.0, 0.0)
    # The cancelled one is gone from the working set but still readable,
    # with the reason it went.
    assert book.order(orders[1].order_id).reason


def test_cancelling_the_same_order_twice_does_not_pull_another(pair):
    book = Book()
    order = book.add_order(pair, SpreadSide.SELL, 58.40, 1.0)
    book.add_order(pair, SpreadSide.SELL, 58.40, 1.0)

    assert book.cancel(order.order_id) is not None
    assert book.cancel(order.order_id) is None
    assert len(book.orders(pair.key)) == 1


def test_cxl_b_and_cxl_s_pull_only_their_own_side(pair):
    book = Book()
    for _ in range(2):
        book.add_order(pair, SpreadSide.BUY, 58.40, 1.0)
    book.add_order(pair, SpreadSide.SELL, 58.60, 1.0)
    assert book.working_counts(pair.key) == (2, 1)

    pulled = book.cancel_where(pair.key, SpreadSide.BUY)

    assert len(pulled) == 2                      # the button's superscript
    assert book.working_counts(pair.key) == (0, 1)


def test_the_global_kill_reaches_every_ladder(pair):
    book = Book()
    book.add_order(pair, SpreadSide.BUY, 58.40, 1.0)
    other = type(pair)('OTHER|PAIR', leg_a={'account': 'a', 'symbol': 'X'},
                       leg_b={'account': 'b', 'symbol': 'Y'})
    book.add_order(other, SpreadSide.SELL, 12.0, 1.0)

    pulled = book.cancel_where(reason='global kill')

    assert len(pulled) == 2
    assert book.orders() == []


def test_net_position_is_signed_and_the_average_is_none_when_flat(pair):
    """An average of no trades is not zero (spec §11)."""
    book = Book()
    assert book.net_position(pair.key) == (0.0, None)


def test_the_average_entry_is_weighted_by_quantity(pair, config, legs):
    from mt5trader.coordinator import _meta_from_report
    from mt5trader.executor import PairExecutor
    from mt5trader.spread import compute_spread

    pair.meta_a = _meta_from_report(legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    book = Book()

    md = compute_spread(pair, legs['acct_a'].tick(pair.symbol_a),
                        legs['acct_b'].tick(pair.symbol_b), pair.hedge_ratio)
    first = executor.market_entry(pair, SpreadSide.BUY, md, 1.0)
    book.add_position(first.position)
    legs['acct_b'].broker.quote('GC1226', 4353.0, 4353.4)
    md = compute_spread(pair, legs['acct_a'].tick(pair.symbol_a),
                        legs['acct_b'].tick(pair.symbol_b), pair.hedge_ratio)
    second = executor.market_entry(pair, SpreadSide.BUY, md, 3.0)
    book.add_position(second.position)

    net, average = book.net_position(pair.key)
    assert net == pytest.approx(4.0)
    assert average == pytest.approx(
        (first.position.entry_spread * 1.0
         + second.position.entry_spread * 3.0) / 4.0)
