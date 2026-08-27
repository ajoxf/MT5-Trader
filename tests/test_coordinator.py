"""The coordinator: one poll, one snapshot, and the sweeps either side
of the process's life.
"""

import json

import pytest

from mt5trader.coordinator import Coordinator, ladder_rows
from mt5trader.models import OrderSide, SpreadSide


def test_the_ladder_and_the_grid_read_one_snapshot(config, pair, legs,
                                                   tmp_path):
    """Every panel renders from this one dict, so a number cannot
    disagree with itself between them — and the grid costs no extra MT5
    round trips."""
    coordinator = Coordinator(config, legs,
                              status_path=str(tmp_path / 'status.json'),
                              sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    coordinator.publish()

    published = json.load(open(coordinator.status_path, encoding='utf-8'))
    row = published['pairs'][pair.key]
    assert row['short_spread'] < row['market']['spread'] < row['long_spread']
    assert row['increment'] == pytest.approx(0.01)
    assert row['spread_units'] == pytest.approx(10.0)
    assert published['accounts']['acct_a']['login'] == 12345


def test_account_info_is_cached_not_polled(config, legs):
    """An IPC round trip three times a second, for a number that moves
    slowly."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    calls = []
    real = legs['acct_a'].account_info
    legs['acct_a'].account_info = lambda: (calls.append(1), real())[1]

    for _ in range(5):
        coordinator.account_info('acct_a')
    assert len(calls) == 1


def test_two_pairs_on_one_symbol_cost_one_tick_each_poll(config, pair, legs):
    """The poll budget is what decides whether the ladder keeps up."""
    from mt5trader.config import PairConfig
    twin = PairConfig('twin', leg_a={'account': 'acct_a', 'symbol': 'XAUUSD_'},
                      leg_b={'account': 'acct_b', 'symbol': 'GC1226'},
                      increment=0.01)
    config.pairs['twin'] = twin
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    calls = []
    real = legs['acct_a'].tick
    legs['acct_a'].tick = lambda symbol: (calls.append(symbol), real(symbol))[1]

    coordinator.poll_once()
    assert calls == ['XAUUSD_']


def test_the_achieved_loop_interval_is_published(config, legs):
    """"Is it the engine or the browser?" is answered, not argued."""
    stamps = [100.0, 100.4]

    def clock():
        return stamps[min(len(coordinator.market), len(stamps) - 1)]

    coordinator = Coordinator(config, legs, clock=clock,
                              sleep=lambda s: None)
    coordinator.poll_once()
    coordinator.poll_once()
    assert coordinator.snapshot()['loop_interval_sec'] == pytest.approx(0.4)


def test_the_startup_sweep_cancels_our_pendings_from_a_previous_life(
        config, pair, legs):
    """A pending of ours still resting is from a process that is gone —
    and one that fills while we are down is an unhedged outright
    position nobody is watching."""
    legs['acct_b'].place_limit('GC1226', OrderSide.BUY.value, 0.1, 4300.0)
    assert legs['acct_b'].pending_orders()

    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    report = coordinator.start()

    assert len(report['cancelled']) == 1
    assert legs['acct_b'].pending_orders() == []


def test_a_leg_that_cannot_be_read_is_unknown_not_swept_clean(config, legs):
    """None means the account could not be read — NOT that nothing of
    ours is resting there."""
    legs['acct_a'].pending_orders = lambda symbol=None: None
    coordinator = Coordinator(config, legs, sleep=lambda s: None)

    report = coordinator.sweep_pendings('startup')
    assert report['unknown'] == ['acct_a']


def test_shutdown_cancels_the_working_orders_it_cannot_keep(config, pair,
                                                            legs):
    """Neither DAY nor GTC survives this process stopping: while it is
    down nobody is watching the spread."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.book.add_order(pair, SpreadSide.BUY, 58.40, 1.0)
    coordinator.book.add_order(pair, SpreadSide.SELL, 58.60, 1.0)

    coordinator.stop()

    assert coordinator.book.orders(pair.key) == []
    assert all('do not survive' in o.reason
               for o in coordinator.book.orders(pair.key, working_only=False))


def test_an_unresolvable_symbol_says_what_the_account_actually_offers(
        config, pair, legs):
    """Brokers spell gold XAUUSD, GOLD, XAUUSD.r. An account that offers
    none of them is probably the wrong leg — and the row stays VISIBLE
    with the reason on it."""
    pair.leg_b['symbol'] = 'GCZ4'
    coordinator = Coordinator(config, legs, sleep=lambda s: None)

    errors = coordinator.resolve_symbols()[pair.key]

    assert errors and "'GCZ4' is not on account 'acct_b'" in errors[0]
    assert 'GC1226' in errors[0]
    assert coordinator.snapshot()['pairs'][pair.key]['errors'] == errors


def test_ladder_rows_centre_on_the_spread_and_carry_the_work_column(
        config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    md = coordinator.market[pair.key]
    level = round(md['spread'] / 0.01) * 0.01
    coordinator.book.add_order(pair, SpreadSide.BUY, round(level, 10), 2.0)

    rows = ladder_rows(pair, md, coordinator.book, rows=40)

    assert len(rows) == 81                       # 40 either side of centre
    assert rows[0]['level'] > rows[-1]['level']  # highest price at the top
    centre = [r for r in rows if abs(r['level'] - level) < 1e-9][0]
    assert centre['work_buy'] == pytest.approx(2.0)
    # The inside market: exactly the two rows the black rule sits
    # between, and they are never the same row.
    best_bid = [r for r in rows if r['is_best_bid']]
    best_ask = [r for r in rows if r['is_best_ask']]
    assert best_bid and best_ask
    assert best_bid[0]['level'] < best_ask[0]['level']


def test_the_session_stats_are_ours_and_say_so(config, pair, legs):
    """MT5 has no session statistics for a spread that does not exist.
    A borrowed number here would be read as the exchange's."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    legs['acct_b'].broker.quote('GC1226', 4360.0, 4360.4)
    coordinator.poll_once()

    md = coordinator.market[pair.key]
    assert md['session']['ours'] is True
    assert md['session']['high'] > md['session']['open']
    assert md['net_change'] == pytest.approx(md['spread']
                                             - md['session']['open'])
