"""The fair-value algo — and the line an algo must not cross.

This system is a MANUAL ladder. Every rule it is built on says so, and
the rule that matters most here is the one an algo is most likely to
break: nothing places, modifies or cancels an order by itself.

So these tests are in two halves. The first says the arithmetic is
right — which KIND of pair this is, and therefore what its carry runs
to. The second says that whatever it concludes, NOTHING HAPPENS: no
order, no change to a click, no difference to the manual path at all.
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
    on_ok, on_sent = click_once('FAIR_SPREAD')

    assert off_ok and on_ok
    assert off_sent == on_sent, (off_sent, on_sent)


def test_a_ladder_running_NONE_computes_nothing_at_all(config, pair, legs):
    """"Nothing is incorporated until it is enabled" has to be true of
    the computing as well as the acting: no reading, nothing on the
    wire, nothing to go wrong."""
    from mt5trader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    for _ in range(5):
        coordinator.poll_once()

    assert pair.algo == 'NONE'
    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block == {'algo': 'NONE', 'window': False}

    # The control: select it, and the reading appears.
    pair.algo = 'FAIR_SPREAD'
    coordinator.poll_once()
    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block['algo'] == 'FAIR_SPREAD' and 'fair' in block
