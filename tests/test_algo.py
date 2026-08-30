"""The two algos — and the line they must not cross.

This system is a MANUAL ladder. Every rule it is built on says so, and
the rule that matters most here is the one an algo is most likely to
break: nothing places, modifies or cancels an order by itself.

So these tests are in two halves. The first says the arithmetic is
right. The second says that whatever the arithmetic concludes, NOTHING
HAPPENS — no order, no change to a click, no difference to the manual
path at all.
"""

import pytest

from mt5trader import algo


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


# -- what KIND of pair this is, which decides the fair-value arithmetic


def test_a_spot_and_a_future_carries_to_the_futures_expiry(pair):
    """Spot does not expire; the future converges to it on its own
    date, so that is where the carry runs to."""
    kind = algo.pair_kind(pair, expiry_a=None, expiry_b='2026-09-26')
    assert kind == algo.SPOT_FUTURE
    nights, note = algo.carry_nights(kind, None, 30)
    assert nights == 30 and 'spot vs a future' in note


def test_a_calendar_carries_to_the_NEAR_expiry(pair):
    """Two futures: the spread is decided when the FIRST one expires.
    Running the carry to the far leg prices a trade that is over."""
    kind = algo.pair_kind(pair, expiry_a='2026-09-26', expiry_b='2026-12-26')
    assert kind == algo.FUTURE_FUTURE

    nights, note = algo.carry_nights(kind, 30, 121)

    assert nights == 30
    assert 'NEAR' in note


def test_a_calendar_missing_one_leg_prices_nothing(pair):
    """Half a calendar is not a shorter calendar."""
    nights, note = algo.carry_nights(algo.FUTURE_FUTURE, None, 121)
    assert nights is None and 'BOTH' in note


def test_two_different_instruments_have_no_date_at_all(pair):
    pair.pair_type = 'RELATED'
    kind = algo.pair_kind(pair, expiry_a='2026-09-26', expiry_b='2026-12-26')
    assert kind == algo.RELATED
    nights, note = algo.carry_nights(kind, 30, 121)
    assert nights is None and 'no date' in note


def test_the_expiries_narrow_what_the_operator_said(pair):
    """A pair labelled spot/future whose leg A turns out to have an
    expiry IS a calendar, and the arithmetic follows the contracts. It
    never invents a basis between two instruments that have none."""
    pair.pair_type = 'SPOT_FUTURE'
    assert algo.pair_kind(pair, '2026-09-26', '2026-12-26') == \
        algo.FUTURE_FUTURE
    pair.pair_type = 'RELATED'
    assert algo.pair_kind(pair, '2026-09-26', '2026-12-26') == algo.RELATED


# -- the statistics ------------------------------------------------------


def test_the_window_counts_QUOTES_not_polls():
    """The fault that produced a z of +53,026 on a spread of 9.13. The
    coordinator polls faster than either broker ticks, so counting
    polls fills the window with the same quote over and over: sigma
    collapses toward zero and z explodes."""
    clock = Clock()
    series = algo.Series(lookback_sec=600, clock=clock)

    for value, quote in ((10.0, 'q1'), (10.5, 'q2'), (11.0, 'q3')):
        series.observe(value, quote)
    real = series.sigma

    # ...and now a hundred polls that saw nothing new.
    for _ in range(100):
        clock.tick(0.3)
        series.observe(11.0, 'q3')

    assert series.sigma == pytest.approx(real)
    assert len(series.samples) == 3


def test_a_feed_that_stops_ticking_goes_COLD_rather_than_freezing():
    """Ageing runs on every call, sample or not. A frozen mean is worse
    than no mean: it goes on quoting a z off history nobody is in."""
    clock = Clock()
    series = algo.Series(lookback_sec=60, clock=clock)
    for i in range(5):
        clock.tick(1)
        series.observe(10.0 + i, 'q%d' % i)
    assert series.ready

    clock.tick(120)                      # two minutes of silence
    series.observe(None)

    assert series.ready is False
    assert series.z(10.0) is None


def test_the_z_is_measured_on_each_executable_side_separately():
    """A z-score off a midpoint is measured against a price nobody
    fills at. A buy is judged on what it would PAY and a sell on what
    it would RECEIVE."""
    clock = Clock()
    stats = algo.PairStats(lookback_sec=600, clock=clock)
    for i in range(30):
        clock.tick(1)
        stats.observe({'long_spread': 10.0 + (i % 2) * 0.1,
                       'short_spread': 9.0 + (i % 2) * 0.1,
                       'leg_a_tick_time': i, 'leg_b_tick_time': i})

    body = algo.stat_arb(stats, {'long_spread': 10.05, 'short_spread': 9.05})

    assert body['mu_buy'] == pytest.approx(10.05, abs=0.01)
    assert body['mu_sell'] == pytest.approx(9.05, abs=0.01)
    assert body['mu_buy'] != body['mu_sell']


def test_it_says_BUY_when_the_offer_is_cheap_and_SELL_when_the_bid_is_rich():
    """The sign that everyone gets backwards once. You BUY what is
    below its own mean and SELL what is above it."""
    clock = Clock()
    stats = algo.PairStats(lookback_sec=600, clock=clock)
    for i in range(40):
        clock.tick(1)
        value = 10.0 + (0.05 if i % 2 else -0.05)
        stats.observe({'long_spread': value, 'short_spread': value - 1.0,
                       'leg_a_tick_time': i, 'leg_b_tick_time': i})

    cheap = algo.stat_arb(stats, {'long_spread': 9.0, 'short_spread': 8.0},
                          entry_z=2.5)
    rich = algo.stat_arb(stats, {'long_spread': 12.0, 'short_spread': 11.0},
                         entry_z=2.5)
    quiet = algo.stat_arb(stats, {'long_spread': 10.0, 'short_spread': 9.0},
                          entry_z=2.5)

    assert cheap['verdict'] == 'BUY'
    assert rich['verdict'] == 'SELL'
    assert quiet['verdict'] == 'WAIT'


def test_a_window_with_nothing_in_it_says_WARMING_not_a_number():
    """A z-score off two prices is not a z-score, and a screen that
    quotes one is a screen that will be acted on."""
    clock = Clock()
    stats = algo.PairStats(lookback_sec=600, clock=clock)
    stats.observe({'long_spread': 10.0, 'short_spread': 9.0,
                   'leg_a_tick_time': 1, 'leg_b_tick_time': 1})

    body = algo.stat_arb(stats, {'long_spread': 10.0, 'short_spread': 9.0})

    assert body['verdict'] == 'WARMING'
    assert body['z_buy'] is None
    assert 'not a z-score' in body['note']


def test_the_exit_it_names_is_the_MANUAL_take_profit(pair):
    """One exit arithmetic on this screen, not two. What the algo would
    leave at is what the Exit box already says."""
    clock = Clock()
    stats = algo.PairStats(lookback_sec=600, clock=clock)
    for i in range(40):
        clock.tick(1)
        value = 10.0 + (0.05 if i % 2 else -0.05)
        stats.observe({'long_spread': value, 'short_spread': value - 1.0,
                       'leg_a_tick_time': i, 'leg_b_tick_time': i})

    body = algo.stat_arb(stats, {'long_spread': 9.0, 'short_spread': 8.0},
                         entry_z=2.5,
                         exit_levels={'tp_buy': 11.5, 'tp_sell': 7.5})

    assert body['verdict'] == 'BUY'
    assert body['exit_level'] == 11.5


# -- the line an algo must not cross -------------------------------------


def test_no_algo_module_can_reach_the_broker():
    """Read as CODE, not as prose. An algo that never places an order
    is a claim that should be checkable without running it, because
    the way this breaks is a later edit that looks harmless.

    Names, calls and imports only — the comments are free to talk
    about brokers and orders, and they should."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path('mt5trader/algo.py').read_text())
    forbidden = {'place_limit', 'send_market_order', 'close_ticket',
                 'cancel_order', 'modify_order', 'order_send', 'executor',
                 'broker', 'legs', 'book', 'quoter', 'click'}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.add(node.attr)
        if isinstance(node, ast.Name) and node.id in forbidden:
            found.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                assert alias.name in ('math', 'time', 'collections', 'deque'), \
                    alias.name

    assert found == set(), found


def test_selecting_an_algo_changes_NOTHING_about_a_click(config, pair, legs):
    """The whole promise. The same click, on the same market, with the
    algo off and then with it on and shouting — and the two must be
    indistinguishable at the broker."""
    from mt5trader.coordinator import Coordinator
    from mt5trader.models import SpreadSide

    def click_once(algo_name):
        coordinator = Coordinator(config, legs, sleep=lambda s: None)
        pair.algo = algo_name
        pair.order_type = pair.order_type.__class__('MARKET')
        coordinator.start()
        coordinator.poll_once()
        legs['acct_a'].broker.sent.clear()
        legs['acct_b'].broker.sent.clear()
        answer = coordinator.click(pair.key, SpreadSide.BUY,
                                   coordinator.market[pair.key]['long_spread'])
        sent = [dict(e) for e in legs['acct_a'].broker.sent
                + legs['acct_b'].broker.sent]
        for entry in sent:
            entry.pop('comment', None)          # carries a unique id
        return answer.get('ok'), sent

    off_ok, off_sent = click_once('NONE')
    on_ok, on_sent = click_once('STAT_ARB')

    assert off_ok and on_ok
    assert off_sent == on_sent, (off_sent, on_sent)


def test_an_algo_that_says_ENTER_still_sends_nothing(config, pair, legs):
    """Left alone with a screaming signal, poll after poll, the engine
    does nothing at all. This is the test that would fail the day
    somebody wires the other half in without saying so."""
    from mt5trader.coordinator import Coordinator
    pair.algo = 'STAT_ARB'
    pair.entry_z = 0.0001              # everything is a signal
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    spot = legs['acct_a'].broker.symbols['XAUUSD_']
    for i in range(40):
        spot.quote(4292.00 + (0.05 if i % 2 else -0.05), 4292.20)
        coordinator.poll_once()
    legs['acct_a'].broker.sent.clear()
    legs['acct_b'].broker.sent.clear()

    for _ in range(20):
        spot.quote(4200.00, 4200.20)   # a mile from the mean
        coordinator.poll_once()

    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['algo_block']['stat']['verdict'] in ('BUY', 'SELL')
    # ...and not one order, position or working order came of it.
    assert legs['acct_a'].broker.sent == []
    assert legs['acct_b'].broker.sent == []
    assert coordinator.book.positions() == []
    assert coordinator.book.orders(pair.key) == []


def test_a_ladder_running_NONE_measures_nothing_at_all(config, pair, legs):
    """"Nothing is incorporated until it is enabled" has to be true of
    the measuring as well as the acting: no window, no statistics, no
    memory, no cost."""
    from mt5trader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    for _ in range(5):
        coordinator.poll_once()

    assert pair.algo == 'NONE'
    assert coordinator._stats == {}
    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block == {'algo': 'NONE', 'window': False}

    # The control: select it, and the window starts filling.
    pair.algo = 'STAT_ARB'
    coordinator.poll_once()
    assert coordinator._stats[pair.key].buy.samples


def test_only_one_algo_runs_at_a_time(config, pair, legs):
    """One selector, so the question cannot arise. Selecting the other
    one drops the first, statistics and all."""
    from mt5trader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    pair.algo = 'STAT_ARB'
    coordinator.poll_once()
    assert pair.key in coordinator._stats

    pair.algo = 'FAIR_SPREAD'
    coordinator.poll_once()

    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block['algo'] == 'FAIR_SPREAD'
    assert 'stat' not in block
    assert coordinator._stats == {}
