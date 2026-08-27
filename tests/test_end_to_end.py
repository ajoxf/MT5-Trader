"""The whole thing, end to end.

Two leg runners on real sockets, a coordinator with a real database, the
Flask process, and Chromium clicking the ladder. Only MetaTrader5 itself
is faked — and it is faked as a HEDGING-mode broker that keeps a real
book and a real deal history, so the parts that have cost money in the
past behave here the way they behave there.

What this covers that the unit tests cannot: the click actually crossing
four process boundaries (browser → Flask → command file → coordinator →
leg runner → broker), the position surviving a coordinator restart, and
the journal being written from what the broker reports rather than from
what we intended.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip('playwright.sync_api')

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeBroker, FakeSymbol                      # noqa: E402
from mt5trader.commands import CommandRunner                     # noqa: E402
from mt5trader.config import PairConfig, TraderConfig            # noqa: E402
from mt5trader.coordinator import Coordinator                    # noqa: E402
from mt5trader.database import Store                             # noqa: E402
from mt5trader.leg_runner import LegServer                       # noqa: E402
from mt5trader.legs import RemoteLeg                             # noqa: E402
from mt5trader.models import OrderSide                           # noqa: E402
from mt5trader.webapp import create_app                          # noqa: E402
from test_ui_browser import chromium_path                        # noqa: E402


class Desk:
    """One installation: two accounts, two runners, and the files."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.paths = {
            'status': str(tmp / 'status.json'),
            'commands': str(tmp / 'commands.jsonl'),
            'results': str(tmp / 'results.json'),
            'config': str(tmp / 'config.json'),
            'db': str(tmp / 'trader.db'),
        }
        self.spot = FakeSymbol('XAUUSD_', 4292.00, 4292.20, contract_size=100.0,
                               volume_min=0.01, volume_step=0.01)
        self.future = FakeSymbol('GC1226', 4351.00, 4351.40,
                                 contract_size=100.0, volume_min=0.10,
                                 volume_step=0.10)
        self.brokers = {'spot': FakeBroker('spot', [self.spot]),
                        'fut': FakeBroker('fut', [self.future])}
        self.servers = {}
        for name, broker in self.brokers.items():
            broker.initialize()
            server = LegServer(broker, '127.0.0.1', 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers[name] = server

        with open(self.paths['config'], 'w', encoding='utf-8') as f:
            json.dump({'accounts': {
                name: {'endpoint': f'127.0.0.1:{server.port}'}
                for name, server in self.servers.items()}, 'pairs': {}}, f)

        self.coordinator = None
        self.runner = None
        self.threads = []
        self.stopped = threading.Event()

    # -- the engine, startable and stoppable like the real launcher -----

    def config(self):
        pair = PairConfig(
            'XAUUSD_|GC1226', name='Gold basis',
            leg_a={'account': 'spot', 'symbol': 'XAUUSD_'},
            leg_b={'account': 'fut', 'symbol': 'GC1226'},
            hedge_ratio=1.0, hedge_ratio_for='XAUUSD_|GC1226',
            increment=0.01, default_quantity=1.0, order_type='MARKET')
        config = TraderConfig(pairs={pair.key: pair})
        config.settings.update({'POLL_INTERVAL_SEC': 0.05,
                                'COMMAND_POLL_SEC': 0.01,
                                'JOURNAL_INTERVAL_SEC': 0.5,
                                'RECONCILE_INTERVAL_SEC': 0.5,
                                'LEG_DEADLINE_SEC': 0.5})
        return config

    def start_engine(self):
        """Exactly what run_coordinator.py does, in-process."""
        legs = {}
        for name, server in self.servers.items():
            leg = RemoteLeg(name, f'127.0.0.1:{server.port}', timeout=5.0)
            assert leg.connect(retries=3, delay=0.1), f'{name} did not answer'
            legs[name] = leg
        self.coordinator = Coordinator(self.config(), legs,
                                       status_path=self.paths['status'],
                                       store=Store(self.paths['db']))
        self.runner = CommandRunner(self.coordinator, self.paths['commands'],
                                    self.paths['results'])
        self.runner.prime()
        self.coordinator.commands = self.runner
        self.coordinator.start()
        for target in (self.coordinator.run, self.coordinator.serve_commands):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.wait_for(lambda: os.path.exists(self.paths['status']),
                      'the engine never published a snapshot')
        return self.coordinator

    def stop_engine(self):
        """Stop as the launcher does — the sweep runs, the book stays at
        the broker."""
        self.coordinator._stop.set()
        for thread in self.threads:
            thread.join(timeout=2.0)
        self.threads = []
        for leg in self.coordinator.legs.values():
            leg.close()

    def serve_web(self):
        from werkzeug.serving import make_server
        app = create_app(self.paths['status'], self.paths['commands'],
                         self.paths['results'], self.paths['config'],
                         self.paths['db'])
        self.httpd = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.httpd.server_port}'

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        for server in self.servers.values():
            server.stop()

    @staticmethod
    def wait_for(predicate, message, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        raise AssertionError(message)


@pytest.fixture(scope='module')
def desk(tmp_path_factory):
    desk = Desk(tmp_path_factory.mktemp('e2e'))
    desk.start_engine()
    yield desk
    desk.close()


@pytest.fixture(scope='module')
def browser_page(desk):
    url = desk.serve_web()
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:
            pytest.skip(f'chromium unavailable: {e}')
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_selector('.ladder tbody tr', timeout=15000)
        page.errors = errors
        yield page
        browser.close()


def test_a_click_in_the_browser_reaches_both_brokers(desk, browser_page):
    """Browser → Flask → command file → coordinator → two leg runners →
    two brokers. Every boundary is real except MT5 itself."""
    page = browser_page
    assert page.locator('.window.mode-market').count() == 1   # armed
    started = time.time()

    # At the touch, which is where a market order belongs. One click.
    page.click('.ladder .buy-touch')

    desk.wait_for(lambda: desk.brokers['spot'].open_positions()
                  and desk.brokers['fut'].open_positions(),
                  'the click never reached both brokers')
    elapsed = time.time() - started

    spot = desk.brokers['spot'].open_positions()[0]
    future = desk.brokers['fut'].open_positions()[0]
    # BUY the spread = buy leg B, sell leg A.
    assert spot['side'] == 'SELL' and future['side'] == 'BUY'
    assert spot['volume'] == pytest.approx(0.1)
    assert future['volume'] == pytest.approx(0.1)
    # One click, one order, and it does not wait for a poll.
    assert elapsed < 2.0, f'the click took {elapsed:.2f}s to reach the broker'
    assert page.errors == []


def test_a_click_far_through_the_touch_is_refused_in_words(desk,
                                                            browser_page):
    """Market-WITH-protection, end to end: the refusal is made by the
    engine, and the words that come back are the engine's own."""
    page = browser_page
    before = len(desk.brokers['spot'].sent)

    # A BUY far BELOW the touch: the market would fill it well through
    # the price named, which is exactly what the protection is for.
    # (Above the touch is not a breach — the market there is BETTER than
    # the price clicked.)
    rows = page.locator('.ladder tbody tr')
    rows.nth(rows.count() - 3).locator('td.bid').click()
    page.wait_for_selector('.toast:not(.ok)', timeout=8000)

    toast = page.text_content('.toast:not(.ok)')
    assert 'protection' in toast
    assert 'Nothing was sent' in toast
    assert len(desk.brokers['spot'].sent) == before      # and nothing was
    page.click('.toast:not(.ok)')


def test_the_position_and_its_legs_appear_on_the_screen(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor', timeout=5000)
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('XAUUSD_')", timeout=8000)

    text = page.text_content('.monitor .pane')
    assert 'Gold basis' in text
    assert 'GC1226' in text                     # both legs, with tickets
    # Marked where it would actually CLOSE, so it shows the round turn
    # as a loss the instant it opens.
    assert '-$' in text


def test_the_journal_records_what_the_broker_reported(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'fills';
        window.MT5Trader.render();
    }""")
    page.wait_for_function(
        "() => document.querySelector('.monitor .pane').textContent"
        ".includes('GC1226')", timeout=8000)

    fills = Store(desk.paths['db']).fills()
    assert len(fills) == 2
    assert {fill['leg'] for fill in fills} == {'A', 'B'}
    assert all(fill['is_ours'] == 1 for fill in fills)
    assert all(fill['pair_key'] == 'XAUUSD_|GC1226' for fill in fills)

    # And the same rows are on the screen, with the broker's commission.
    text = page.text_content('.monitor .pane')
    assert 'commission' in text
    assert 'Export CSV' in text


def test_a_manual_trade_in_the_terminal_is_journalled_but_not_ours(desk):
    """A fill on the account is a fill on the account — and it is never
    mistaken for one of ours, in the book or in the journal."""
    desk.brokers['fut'].send_market_order(
        'GC1226', OrderSide.BUY, 0.2, comment='by hand')

    desk.wait_for(
        lambda: any(fill['is_ours'] == 0
                    for fill in Store(desk.paths['db']).fills()),
        'the manual trade never reached the journal')

    theirs = [fill for fill in Store(desk.paths['db']).fills()
              if fill['is_ours'] == 0]
    assert len(theirs) == 1 and theirs[0]['volume'] == pytest.approx(0.2)
    # It is not in our book, and the reconciler leaves it alone.
    assert len(desk.coordinator.book.positions()) == 1


def test_the_position_survives_a_coordinator_restart(desk):
    """The blocker this was built for: an empty book at startup makes
    every live position look like an orphan, and sixty seconds later the
    reconciler closes them."""
    before = desk.coordinator.book.positions()[0]
    tickets = {
        'spot': before.leg_a.position_tickets,
        'fut': before.leg_b.position_tickets,
    }
    desk.stop_engine()

    # The positions are still at the broker, as they would be.
    assert desk.brokers['spot'].open_positions()
    assert desk.brokers['fut'].open_positions()

    desk.start_engine()

    recovered = desk.coordinator.book.positions()
    assert len(recovered) == 1
    assert recovered[0].position_id == before.position_id
    assert recovered[0].leg_a.position_tickets == tickets['spot']
    assert recovered[0].recovered is True
    assert desk.coordinator.recovery['recovered'] == 1

    # Now let the reconciler run several times over. It must NOT close
    # the position it just recovered.
    time.sleep(2.5)
    assert desk.brokers['spot'].open_positions()
    assert desk.brokers['fut'].open_positions()
    assert desk.coordinator.book.positions()


def test_closing_from_the_screen_leaves_both_books_empty(desk, browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'positions';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .close-position', timeout=8000)
    page.click('.monitor .close-position')

    desk.wait_for(lambda: not desk.brokers['spot'].positions_by_magic(),
                  'leg A never closed')
    desk.wait_for(lambda: not desk.brokers['fut'].positions_by_magic(),
                  'leg B never closed')

    # Closed by TICKET: one market order in, one close out, per leg —
    # never an opposite order that would open a second position.
    for name in ('spot', 'fut'):
        actions = [entry['action'] for entry in desk.brokers[name].sent
                   if entry['action'] in ('market', 'close')]
        assert actions.count('close') == 1
    # The manual position is untouched by any of it.
    assert desk.brokers['fut'].open_positions()

    position = desk.coordinator.book.positions(open_only=False)[0]
    assert not position.is_open
    assert position.realized_pnl is not None
    # And the round trip is in the journal, both ends, both legs.
    desk.wait_for(lambda: len(Store(desk.paths['db']).fills()) >= 5,
                  'the closing fills never reached the journal')


def test_the_closed_trade_is_in_the_record_after_everything(desk):
    store = Store(desk.paths['db'])
    assert store.open_positions() == []
    closed = store.closed_positions()
    assert len(closed) == 1
    assert closed[0]['close_reason']
    assert closed[0]['entry_spread'] is not None

    # Two legs, in and out — plus the trader's own click on leg B's
    # symbol, which belongs to that pair's totals as much as ours does.
    # The broker's statement does not separate them, and neither does
    # this.
    totals = store.fill_totals('XAUUSD_|GC1226')
    assert totals['fills'] == 5
    assert totals['commission'] < 0
    ours = store.fills(pair_key='XAUUSD_|GC1226', ours_only=True)
    assert len(ours) == 4
    assert sorted(fill['entry'] for fill in ours) == \
        ['close', 'close', 'open', 'open']
    # The audit trail answers "what happened" without being the place
    # the operator was ever SENT to.
    assert store.events('recovery')
