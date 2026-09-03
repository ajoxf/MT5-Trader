"""The UI, in a real browser.

Python tests cannot see a temporal-dead-zone `ReferenceError` that
aborts an entire script block and silently unregisters a handler —
which is exactly what happened in the system this is ported from, where
clicking Save did a native form submit and reset the page. So this
drives Chromium under Playwright and reads `pageerror`.

It also cross-checks every number input's min/max/step against the
shipped default: two of them once rejected the engine's own defaults,
so Chrome refused to submit and fired no event at all.
"""

import json
import re
import os
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip('playwright.sync_api')

from mt5trader.webapp import create_app                        # noqa: E402


#: How long a browser test waits for a publish to reach the screen.
#: Generous on purpose: the assertion is about WHAT the page shows, not
#: how fast, and a container under load runs this suite two-thirds
#: slower — where a 5s wait fails tests that are perfectly correct.
WAIT = 10000


class Publisher:
    """Stands in for the coordinator: it republishes the snapshot on a
    timer, exactly as the real one does. Without that the file ages, the
    web process correctly calls the engine stalled, and every click is
    refused — which is the behaviour under test elsewhere, not here."""

    def __init__(self, path, every=0.2):
        self.path = path
        self.every = every
        self.order_type = 'LIMIT'
        self.confirm = False
        #: Two accounts reporting one MT5 login, as the engine publishes
        #: it — the banner under test is driven from the SNAPSHOT, not
        #: from a value poked into the page that the next poll erases.
        self.same_login = None
        #: One leg frozen, as the age tracker reports it.
        self.stale_leg = False
        #: Orders the broker refused, as the engine republishes them.
        self.dead_orders = None
        #: Break-even and take-profit, and any open position.
        self.exits = None
        self.positions = None
        #: Positions at the broker that our book cannot explain.
        self.unclaimed = None
        #: What the carry says the basis should be, as the engine
        #: publishes it — including the case where the reading is
        #: REPLACED by a warning about the input it came from.
        self.fair = None
        #: AutoRouting: the switch, and what is ACTUALLY resting.
        self.auto_route = False
        self.auto_route_armed = None
        #: The system-wide switch, off by default in the engine. The
        #: fixture leaves it ON so the per-ladder tick is what is under
        #: test; the test that turns it off is testing the master.
        self.auto_route_master = True
        #: The fair-value window is per pair and off by default; the
        #: fixture turns it on so the panels it holds can be read.
        self.show_fair_window = True
        #: Which algo the ladder is running, and what it says. NONE by
        #: default — the state every test that is not about an algo
        #: must see, because an algo must change nothing else.
        self.algo = 'NONE'
        self.algo_block = None
        #: Working orders and their quote groups, as the engine
        #: publishes them. Poked into `state.snapshot` instead, the
        #: next poll — 200ms away — erases them, and the test passes or
        #: fails on which side of that it lands.
        self.orders = None
        self.quotes = None
        #: The counts the cancel buttons are enabled from.
        self.working_buys = 0
        self.working_sells = 0
        #: What is ON. Close @ Limit refuses on a flat ladder, so a
        #: test of it has to publish a position first.
        self.net_position = 0.0
        #: Which leg LIMIT mode rests its real pending on. The other
        #: leg is crossed at market when it fills, and a broker window
        #: showing nothing on that leg is the mode, not a fault.
        self.quoting_leg = 'b'
        self.live = threading.Event()
        self.live.set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            if self.live.is_set():
                self.publish()
            time.sleep(self.every)

    def publish(self, at=None):
        payload = snapshot(self.order_type, self.confirm, self.same_login,
                           self.stale_leg, self.dead_orders, self.exits,
                           self.positions, self.unclaimed, self.fair,
                           self.auto_route, self.auto_route_armed,
                           self.auto_route_master,
                           self.show_fair_window, self.orders,
                           self.quotes, self.working_buys,
                           self.working_sells, self.algo, self.algo_block,
                           self.net_position, self.quoting_leg)
        if at is not None:
            payload['at'] = at
        # A tmp name of its OWN. The timer thread and the test publish
        # at the same moment often enough to matter: sharing one tmp
        # file, the slower writer's file is gone before it renames it,
        # the timer thread dies of the FileNotFoundError, the snapshot
        # stops arriving, and the UI — correctly — refuses every click
        # into a screen with nothing behind it. That looked like a
        # flaky click test and was a flaky FIXTURE.
        tmp = '%s.%d.tmp' % (self.path, threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, self.path)


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    """The real Flask app, on a real port, over a published snapshot."""
    tmp = tmp_path_factory.mktemp('ui')
    paths = {name: str(tmp / f'{name}.json') for name in
             ('status', 'results', 'config')}
    paths['commands'] = str(tmp / 'commands.jsonl')

    publisher = Publisher(paths['status'])
    publisher.publish()
    publisher.thread.start()
    paths['publisher'] = publisher
    with open(paths['config'], 'w', encoding='utf-8') as f:
        json.dump({'accounts': {}, 'pairs': {}}, f)

    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    from werkzeug.serving import make_server
    httpd = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}', paths
    publisher.stop.set()
    httpd.shutdown()


def snapshot(order_type='LIMIT', confirm=False, same_login=None,
             stale_leg=False, dead_orders=None, exits=None,
             positions=None, unclaimed=None, fair=None,
             auto_route=False, auto_route_armed=None,
             auto_route_master=True, show_fair_window=True, orders=None, quotes=None,
             working_buys=0, working_sells=0, algo='NONE', algo_block=None,
             net_position=0.0, quoting_leg='b'):
    rows = []
    for step in range(20, -21, -1):
        level = round(59.10 + step * 0.01, 4)
        rows.append({'level': level, 'work_buy': None, 'work_sell': None,
                     'is_best_bid': step == -1, 'is_best_ask': step == 1,
                     # The mid, which is where the heavy rule goes and
                     # what the ladder centres on.
                     'is_mid': step == 0})
    return {
        'at': time.time(), 'loop_interval_sec': 0.31,
        'confirm_market_clicks': confirm, 'row_height_px': 17,
        'command_poll_sec': 0.02,
        'accounts': {'acct_a': {'profit': 0.0}, 'acct_b': {'profit': 0.0}},
        'account_health': {
            'warn_level': 200.0, 'weakest': 'acct_b', 'weakest_level': 125.0,
            'unknown': [], 'same_login': same_login or {},
            'accounts': {
                'acct_a': {'account': 'acct_a', 'known': True, 'login': 5001,
                           'server': 'CFI-Live', 'currency': 'USD',
                           'leverage': 100, 'balance': 0.0, 'credit': 5000.0,
                           'equity': 5000.0, 'profit': 0.0, 'margin': 500.0,
                           'margin_free': 4500.0, 'margin_level': 1000.0,
                           'so_call': 100.0, 'so_so': 50.0, 'our_lots': 0.1,
                           'our_units': 10.0, 'our_legs': 1, 'tight': False},
                'acct_b': {'account': 'acct_b', 'known': True, 'login': 5002,
                           'server': 'CFI-Live', 'currency': 'USD',
                           'leverage': 100, 'balance': 1000.0, 'credit': 0.0,
                           'equity': 1000.0, 'profit': -20.0, 'margin': 800.0,
                           'margin_free': 200.0, 'margin_level': 125.0,
                           'so_call': 100.0, 'so_so': 50.0, 'our_lots': 0.1,
                           'our_units': 10.0, 'our_legs': 1, 'tight': True}}},
        'reconciler': {'untracked_closes': [], 'escalated': [],
                       'unknown_accounts': [],
                       'unclaimed': unclaimed or []},
        'hedge_times_ms': [], 'click_to_on_ms': [],
        'pairs': {
            'XAUUSD_|GC1226': {
                'key': 'XAUUSD_|GC1226', 'name': 'Gold basis', 'enabled': True,
                'account_a': 'acct_a', 'account_b': 'acct_b',
                'symbol_a': 'XAUUSD_', 'symbol_b': 'GC1226',
                'hedge_ratio': 1.0, 'hedge_ratio_for': 'XAUUSD_|GC1226',
                'pair_type': 'SPOT_FUTURE',
                'increment': 0.01, 'increment_derived': 0.01,
                'order_type': order_type, 'time_in_force': 'DAY',
                'overnight': 'ALLOW', 'default_quantity': 1.0,
                'auto_route': auto_route,
                'auto_route_on': bool(auto_route and auto_route_master),
                'auto_route_master': bool(auto_route_master),
                'algo': algo,
                'algo_window': show_fair_window,
                'algo_block': algo_block or {'algo': algo,
                                             'window': show_fair_window},
                'show_fair_window': show_fair_window,
                'auto_route_armed': auto_route_armed or [],
                'clip_lots_a': 0.1, 'clip_lots_b': 0.1, 'spread_units': 10.0,
                'short_spread': 59.09, 'long_spread': 59.11,
                'market': {'spread': 59.10, 'short_spread': 59.09,
                           'long_spread': 59.11, 'net_change': -0.61,
                           # The two legs' own books, as the engine
                           # always publishes them.
                           'leg_a_bid': 4607.38, 'leg_a_ask': 4607.63,
                           'leg_b_bid': 4660.40, 'leg_b_ask': 4660.80,
                           'leg_a_quote_age_sec': 0.3,
                           'leg_b_quote_age_sec': 9.0 if stale_leg else 1.5,
                           'feed_badge': 'OK (oldest leg 0.2s)',
                           'session': {'open': 59.71, 'high': 59.80,
                                       'low': 58.90, 'volume': 0.0,
                                       'ours': True}},
                'rows': rows, 'row_count': 30, 'orders': orders or [],
                'quotes': quotes or [],
                'positions': positions or [],
                'dead_orders': dead_orders or [],
                'exit': exits or {},
                'fair': fair or {},
                'working_buys': working_buys,
                'working_sells': working_sells,
                'net_position': net_position,
                'quoting_leg_effective': quoting_leg,
                'avg_entry': None, 'open_pnl': None, 'last_print': None,
                'errors': [],
            }
        },
    }


#: Intercept the pair save so a test can read exactly what the form
#: sent — a blank that arrives as 0, or not at all, is the bug.
SPY_ON_PAIR_SAVE = """() => {
    window.__realFetch = window.__realFetch || window.fetch;
    window.__sent = null;
    window.fetch = function (url, options) {
        if (String(url).indexOf('/api/pairs/') >= 0 && options &&
            options.method === 'POST') {
          window.__sent = JSON.parse(options.body);
          return Promise.resolve(new Response(
            JSON.stringify({ok: true, notes: []}),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return window.__realFetch(url, options);
    };
}"""


def chromium_path():
    """The browser this machine actually has.

    Playwright pins a build number per release; a box whose browsers were
    installed for a different release has a perfectly good Chromium under
    another name, and pointing at it beats downloading a second copy.
    None lets Playwright find its own.
    """
    root = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers'))
    for candidate in sorted(root.glob('chromium-*/chrome-linux/chrome')):
        return str(candidate)
    return None


@pytest.fixture(scope='module')
def page(server):
    url, paths = server
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:                    # no browser on this box
            pytest.skip(f'chromium unavailable: {e}')
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text)
                if message.type == 'error' else None)
        page.goto(url)
        page.wait_for_selector('.ladder .grid tbody tr')
        page.errors = errors
        page.paths = paths
        yield page
        browser.close()


def test_the_page_loads_without_a_single_script_error(page):
    """A TDZ ReferenceError aborts the whole script block and silently
    unregisters every handler after it. Nothing in Python can see that."""
    page.wait_for_timeout(900)                    # several refresh cycles
    assert page.errors == [], '\n'.join(page.errors)


def test_the_ladder_draws_the_reference_screens_columns(page):
    # Scoped to the GRID: the footer's leg table has headers of its own
    # now, and they are not these.
    headers = page.eval_on_selector_all(
        '.ladder .grid thead th', 'nodes => nodes.map(n => n.textContent)')
    assert headers == ['Work', 'Bids', 'Price', 'Asks', 'LTQ']
    assert page.locator('.ladder .grid tbody tr').count() == 41


def test_one_rule_marks_the_market_and_it_is_at_the_mid(page):
    """Two black rules — one at the inside, one at the mid — meant
    neither read as "the market is here"."""
    line = page.locator('.ladder tr.mid-line')
    assert line.count() == 1
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.mid-line td')).borderTopWidth") == '3px'
    # The inside market is still marked — by the two bands meeting, not
    # by a second line: the best-bid row carries the ordinary 1px grid
    # rule and nothing heavier.
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.market-line td')).borderTopWidth") == '1px'
    assert page.locator('.ladder tr.in-bid td.bid').count() > 0
    assert page.locator('.ladder tr.in-ask td.ask').count() > 0


def rgb_parts(value):
    return [int(n) for n in re.findall(r'\d+', value)[:3]]


def is_blue(value):
    r, g, b = rgb_parts(value)
    return b > r and b > g


def is_red(value):
    r, g, b = rgb_parts(value)
    return r > g and r > b


def css_var_rgb(locator, name):
    """What a CSS variable resolves to, as the browser reports
    backgroundColor — so a test can name the variable, not a hex."""
    return locator.evaluate(
        """(n, v) => {
            const probe = document.createElement('span');
            probe.style.backgroundColor = getComputedStyle(n)
                .getPropertyValue(v).trim();
            document.body.appendChild(probe);
            const out = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return out;
        }""", name)


def test_bid_is_blue_and_ask_is_red_on_the_rendered_page(page):
    # The ladder repaints three times a second; wait for a painted row
    # rather than racing one.
    page.wait_for_selector('.ladder tr.in-bid td.bid')
    page.wait_for_selector('.ladder tr.in-ask td.ask')
    bid = page.locator('.ladder tr.in-bid td.bid').first
    ask = page.locator('.ladder tr.in-ask td.ask').first
    # Softened for a light frame, and the shade is free to be tuned —
    # the CONVENTION is what is pinned: bid is blue, ask is red, in
    # every table on the screen. Asserting the hex meant that nudging
    # the palette broke a test about which colour means which side.
    assert is_blue(bid.evaluate('n => getComputedStyle(n).backgroundColor'))
    assert is_red(ask.evaluate('n => getComputedStyle(n).backgroundColor'))
    # And each really is drawn from the variable it is supposed to be.
    assert bid.evaluate('n => getComputedStyle(n).backgroundColor') == \
        css_var_rgb(bid, '--bid')
    assert ask.evaluate('n => getComputedStyle(n).backgroundColor') == \
        css_var_rgb(ask, '--ask')


def test_a_limit_click_places_one_order_and_asks_nothing(page):
    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(5).click()
    page.wait_for_timeout(250)
    assert page.locator('#modal.hidden').count() == 1     # no confirmation
    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['kind'] == 'click'
    # The BIDS column SELLS the spread — sell leg B, buy leg A — and the
    # Asks column buys it. Getting this pair of columns the wrong way
    # round is the most expensive mistake this screen could make.
    assert command['payload']['side'] == 'SELL'


def test_the_asks_column_buys_the_spread_and_the_bids_column_sells_it(page):
    """Stated as its own test because it is a definition, not a
    preference: buying the spread is buying leg B and selling leg A,
    and that is what the ASK side of the ladder offers."""
    page.locator('.ladder .grid tbody tr td.ask').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'BUY'

    page.locator('.ladder .grid tbody tr td.bid').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'SELL'

    # ...and the buttons carry the colour of the column they act on.
    buy = page.locator('.ladder .buy-touch')
    sell = page.locator('.ladder .sell-touch')
    # One step deeper than the bands: a button carries white text and
    # needs the contrast, while a band is data and gets out of the way.
    assert is_red(buy.evaluate('n => getComputedStyle(n).backgroundColor'))
    assert is_blue(sell.evaluate('n => getComputedStyle(n).backgroundColor'))


def test_three_clicks_at_one_price_send_three_orders(page):
    before = command_count(page)
    cell = page.locator('.ladder .grid tbody tr td.ask').nth(3)
    for _ in range(3):
        cell.click()
        page.wait_for_timeout(120)
    assert command_count(page) == before + 3
    levels = {json.loads(line)['payload']['level']
              for line in commands(page)[-3:]}
    assert len(levels) == 1                       # same price, three orders


def test_a_market_click_fires_on_ONE_click(page):
    """One click is one order. The arming carries the weight instead:
    the mode badge, the tinted columns, the cursor."""
    # The selector sends a command; the UI arms itself from what the
    # ENGINE says came back, never from the click.
    page.select_option('.ladder .order-type', 'MARKET')
    page.paths['publisher'].order_type = 'MARKET'
    page.wait_for_selector('.window.mode-market', timeout=WAIT)
    # Read in ONE evaluate: the ladder repaints on every publish, and a
    # node resolved in Python can be replaced before the style is read.
    # The M is on every price cell — it names the MODE, and the mode is
    # the same at the touch and ten rows away. The touch rows are told
    # apart by their box instead.
    cursors = page.evaluate(
        "() => ({touch: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tr.in-bid td.bid')).cursor,"
        " away: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tbody td.bid')).cursor,"
        " box: getComputedStyle(document.querySelector("
        "'.window.mode-market .grid tr.market-line td.bid')).boxShadow})")
    assert 'svg+xml' in cursors['touch'] and 'crosshair' in cursors['touch']
    assert 'svg+xml' in cursors['away']
    assert cursors['box'] != 'none'

    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(6).click()
    page.wait_for_timeout(250)

    assert command_count(page) == before + 1        # no second gesture
    assert page.locator('#modal.hidden').count() == 1
    assert last_command(page)['kind'] == 'click'


def test_the_click_shows_up_before_the_engine_answers(page):
    """A trader who cannot see their click clicks again. The ghost is
    drawn immediately and is never added into the working total."""
    # Earlier tests in this file have clicked too; start from nothing in
    # flight so the count means what it says.
    page.evaluate('() => { window.MT5Trader.state.pending.length = 0; }')
    page.locator('.ladder .grid tbody tr td.ask').nth(8).click()
    # No waiting: it is on the screen in the same frame.
    page.wait_for_selector('.ladder td.work.pending', timeout=500)
    assert page.locator('.ladder td.work.pending .ghost').first.text_content() \
        == '1'


def test_a_desk_that_wants_the_extra_gesture_can_have_it(page):
    """The control: CONFIRM_MARKET_CLICKS turns the one click back into
    a confirmed one — through OUR modal, never a native dialog."""
    page.paths['publisher'].confirm = True
    page.wait_for_timeout(400)

    before = command_count(page)
    page.locator('.ladder .grid tbody tr td.bid').nth(6).click()
    page.wait_for_selector('#modal:not(.hidden)')
    assert command_count(page) == before          # nothing sent yet
    page.click('#modal-confirm')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1

    page.paths['publisher'].confirm = False
    page.paths['publisher'].order_type = 'LIMIT'
    page.wait_for_timeout(400)


def test_every_number_input_accepts_the_value_the_engine_shipped(page):
    """Two of these once rejected the engine's own defaults, so Chrome
    refused to submit and fired no event at all."""
    bad = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input[type=number]').forEach(input => {
            if (input.value !== '' && !input.checkValidity()) {
                out.push([input.className, input.value, input.min, input.max,
                          input.step]);
            }
        });
        return out;
    }""")
    assert bad == []


def test_the_engine_banner_appears_when_the_snapshot_goes_stale(page):
    """A dead coordinator must never read as a quiet market."""
    publisher = page.paths['publisher']
    publisher.live.clear()                 # the engine stops publishing
    publisher.publish(at=0)
    page.wait_for_selector('#engine-banner:not(.hidden)', timeout=WAIT)
    assert 'Nothing on this screen is live' in \
        page.text_content('#engine-banner')

    publisher.live.set()                   # ...and comes back
    # `state='attached'`: a hidden element is never "visible", which is
    # the point of asserting on it.
    page.wait_for_selector('#engine-banner.hidden', state='attached',
                           timeout=WAIT)
    assert page.locator('#engine-banner.hidden').count() == 1


def commands(page):
    try:
        with open(page.paths['commands'], encoding='utf-8') as f:
            return [line for line in f.read().splitlines() if line.strip()]
    except OSError:
        return []


def command_count(page):
    return len(commands(page))


def last_command(page):
    return json.loads(commands(page)[-1])


# -- the settings page ---------------------------------------------------

def test_the_quick_buttons_hit_the_touch_without_hunting_for_a_row(page):
    """BUY lifts the offer, SELL hits the bid — the price the market is
    actually offering for that direction, never the mid."""
    before = command_count(page)
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    assert last_command(page)['payload']['level'] == 59.11      # long spread
    assert last_command(page)['payload']['side'] == 'BUY'

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['level'] == 59.09      # short spread
    assert last_command(page)['payload']['side'] == 'SELL'


def test_one_key_is_one_order_too(page):
    """B, S, and the quantity presets — a hand that never leaves the
    keyboard is faster than one that hunts for a button."""
    page.click('.ladder .titlebar')                 # focus this ladder
    before = command_count(page)

    page.keyboard.press('3')                        # arm 10 spreads
    page.wait_for_timeout(120)
    assert page.input_value('.ladder .armed') == '10'
    assert page.input_value('.ladder .sell-qty') == '10'

    page.keyboard.press('b')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['payload']['side'] == 'BUY'
    assert command['payload']['quantity'] == 10

    page.keyboard.press('s')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'SELL'

    page.keyboard.press('0')                        # back to the default
    page.wait_for_timeout(120)
    # Never blank: the box says what ONE CLICK sends, which with
    # nothing armed is this ladder's own default size.
    assert page.input_value('.ladder .armed') == '1'


def test_x_pulls_this_ladders_orders_and_l_locks_the_scroll(page):
    page.click('.ladder .titlebar')
    before = command_count(page)
    page.keyboard.press('x')
    page.wait_for_timeout(250)
    assert last_command(page)['kind'] == 'cancel_where'
    assert command_count(page) == before + 1

    assert page.evaluate("() => !!window.MT5Trader.state.locked['XAUUSD_|GC1226']") \
        is False
    page.keyboard.press('l')
    page.wait_for_timeout(120)
    assert page.evaluate("() => !!window.MT5Trader.state.locked['XAUUSD_|GC1226']") \
        is True
    page.keyboard.press('l')


def test_a_key_typed_into_a_field_is_never_an_order(page):
    """The one way a shortcut becomes dangerous: typing a quantity and
    having the B in 'BUY' fire."""
    before = command_count(page)
    page.click('.ladder .sell-qty')
    page.keyboard.type('b')
    page.keyboard.press('s')
    page.wait_for_timeout(250)
    assert command_count(page) == before
    page.keyboard.press('Escape')


def test_the_shortcuts_are_on_the_screen_not_in_a_manual(page):
    page.click('#help')
    page.wait_for_selector('#help-overlay:not(.hidden)')
    text = page.text_content('#help-overlay')
    assert 'buy the spread at the offer' in text
    assert 'no confirmation, by design' in text
    page.keyboard.press('Escape')
    page.wait_for_selector('#help-overlay.hidden', state='attached')


def test_flatten_asks_once_and_then_goes(page):
    """Flattening is irreversible and it is the button pressed in a
    hurry — so it asks, but with one key and only once."""
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.snapshot.pairs['XAUUSD_|GC1226'].net_position = -2;
        window.MT5Trader.render();
    }""")
    before = command_count(page)
    page.click('.ladder .flatten')
    page.wait_for_selector('#modal:not(.hidden)')
    assert 'cannot be undone' in page.text_content('#modal-body')
    page.click('#modal-confirm')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    assert last_command(page)['kind'] == 'flatten_pair'


def test_flatten_says_so_when_there_is_nothing_to_flatten(page):
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.snapshot.pairs['XAUUSD_|GC1226'].net_position = 0;
        window.MT5Trader.render();
    }""")
    before = command_count(page)
    page.click('.ladder .flatten')
    page.wait_for_selector('.toast')
    assert 'already flat' in page.text_content('.toast')
    assert command_count(page) == before
    page.click('.toast')


def test_the_monitor_shows_margin_per_account_and_names_the_weakest(page):
    """With two brokers there is no combined margin: the pair can only
    be carried by the weaker of the two, and a total would read
    comfortable while one side sits at its stop-out."""
    page.evaluate("""() => {
        const s = window.MT5Trader.state;
        s.open = ['monitor:'];
        s.monitorTab = 'accounts';
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.monitor .pane table', timeout=WAIT)

    text = page.text_content('.monitor .pane')
    assert 'The weakest account governs' in text
    assert 'acct_b' in text and '125.0%' in text
    assert 'it is this account that stops the pair' in text
    # The tight account is marked, the comfortable one is not.
    assert page.locator('.monitor tr.mismatch').count() >= 1
    # Equity, balance and credit side by side: a demo funded with CREDIT
    # shows a balance of 0.00 against real equity.
    assert '$5,000.00' in text and '$0.00' in text
    assert 'fund a demo with CREDIT' in text
    # And our own exposure on each account, in the units it was sized in.
    assert 'Our lots' in text and 'Our units' in text

    page.evaluate("""() => {
        window.MT5Trader.state.monitorTab = 'positions';
        window.MT5Trader.render();
    }""")


def test_the_settings_page_edits_accounts_and_pairs(page):
    """The one panel that must work with the ENGINE down: the
    coordinator will not start until the symbols are right, and this is
    where they get right."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')

    # A new account, saved from the blank row at the bottom.
    add_account(page, 'CFI Spot', '127.0.0.1:9101', login='5001')
    page.wait_for_selector('tr[data-account="CFI Spot"]')

    # And a second one that tries to take the same port.
    add_account(page, 'CFI Futures', '127.0.0.1:9101')
    # An ERROR toast, which is a different thing from the success one
    # above: it has no timer on it and waits to be dismissed by hand.
    page.wait_for_selector('.toast:not(.ok)')
    toast = page.text_content('.toast:not(.ok)')
    # The refusal names the account holding the port AND the one to use.
    assert "belongs to account 'CFI Spot'" in toast
    assert '9102' in toast
    page.click('.toast:not(.ok)')

    add_account(page, 'CFI Futures', '127.0.0.1:9102')
    page.wait_for_selector('tr[data-account="CFI Futures"]')

    assert page.locator('.window.settings tbody tr[data-account]').count() >= 2


def test_a_clash_already_in_the_config_is_shown_on_the_row(page):
    """A clash was once only discoverable by running a connectivity
    check, so an operator could look straight at two rows holding one
    terminal and see nothing wrong with either."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    rows = page.locator('.window.settings tr[data-account]')
    # Give both accounts the same terminal folder, through the API the
    # page itself uses, then reopen.
    page.evaluate("""async () => {
        await fetch('/api/accounts/CFI Spot', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({terminal_path: 'C:/MT5/terminal64.exe'})});
        await window.MT5Settings.refresh();
    }""")
    page.wait_for_timeout(200)
    assert rows.count() >= 2
    # The second account claiming it is refused at SAVE, with the reason
    # on the row rather than in a log.
    page.evaluate("""async () => {
        const r = await fetch('/api/accounts/CFI Futures', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({terminal_path: 'C:/MT5/terminal64.exe'})});
        window.__clash = (await r.json()).error;
    }""")
    assert 'One terminal serves ONE login' in page.evaluate('() => window.__clash')


def test_the_exchanges_page_carries_connect_test_and_diagnose(page):
    """The three questions an operator asks, in the order they ask them."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    page.wait_for_selector('tr[data-account] .connect-account', timeout=WAIT)

    row = page.locator('tr[data-account]').first
    for name in ('connect-account', 'test-account', 'diagnose-account'):
        assert row.locator('.' + name).count() == 1
    assert 'Exchanges' in page.text_content('.window.settings .accounts h3')


def test_the_exit_costs_belong_to_ONE_LADDER_and_the_override_CLEARS(page):
    """Commission, the allowance, the nights and the target are not one
    trade repeated: a gold basis and an oil differential are charged
    differently and held for different lengths of time, so a single set
    of numbers is wrong for at least one of them. They live behind the
    cog on each ladder, and the Exchanges page says where.

    And 0 is a real number: a form that reads a typed zero as "unset"
    charges a commission nobody agreed to.
    """
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ladder-settings .ls-tp', timeout=WAIT)

    for field in ('.ls-comm-a', '.ls-comm-b', '.ls-slip',
                  '.ls-nights', '.ls-tp', '.ls-carry-rate',
                  '.ls-auto-route', '.ls-overnight'):
        assert page.locator(
            '.ladder .ladder-settings ' + field).count() == 1, field

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.fill('.ladder .ladder-settings .ls-slip', '2.5')
    page.fill('.ladder .ladder-settings .ls-comm-a', '0')
    page.click('.ladder .ladder-settings .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)

    sent = page.evaluate('() => window.__sent')
    assert sent['slippage_allowance'] == 2.5
    assert sent['commission_per_lot_a'] == 0             # ...and 0 is real
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    # Leave the desk as it was found: this test's own "applied" toast is
    # a message a LATER test reads as its own.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.wait_for_selector('.ladder .grid tbody tr')


def test_every_setting_shows_the_value_ACTUALLY_IN_FORCE(page):
    """A form that cannot tell you what the ladder is doing.

    Mode, Time in force and Overnight came up BLANK whenever the config
    had no saved value — while the title bar said LIMIT · DAY and the
    engine ran exactly that. And the cost fields said "default", which
    is the NAME of a number you cannot see. Both are now the value in
    force: the running one for what the ladder is doing, the system
    default for what it is inheriting, marked as inherited rather than
    hidden behind a word.
    """
    open_ladder(page)
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            const get = !(options && options.method === 'POST');
            if (String(url).indexOf('/api/pairs') >= 0 && get) {
              // A config with almost nothing in it, which is the case
              // that broke: the engine is running defaults and the
              // file has no opinion at all.
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, pairs: {'XAUUSD_|GC1226': {
                  pair_type: 'SPOT_FUTURE', commission_per_lot_b: 3.5}}}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
            }
            if (String(url).indexOf('/api/settings') >= 0 && get) {
              return Promise.resolve(new Response(JSON.stringify({
                settings: {COMMISSION_PER_LOT_A: 2.0,
                           COMMISSION_PER_LOT_B: 2.0,
                           TP_TARGET_PCT_OF_MARGIN: 2.0,
                           SLIPPAGE_ALLOWANCE: 0.0,
                           BREAK_EVEN_NIGHTS: 0.0}}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")

    try:
        page.click('.ladder .ladder-cog')
        page.wait_for_function(
            "() => document.querySelector('.ladder .ls-comm-a').value !== ''",
            timeout=WAIT)

        # What the ladder is RUNNING, from the snapshot — never blank.
        assert page.input_value('.ladder .ls-order-type') == 'LIMIT'
        assert page.input_value('.ladder .ls-tif') == 'DAY'
        assert page.input_value('.ladder .ls-overnight') == 'ALLOW'
        assert page.input_value('.ladder .ls-increment') == '0.01'
        # Rows read the ROW COUNT, not the snapshot's `rows` — which is
        # the ladder's forty price rows, and left this field blank on
        # every ladder.
        assert page.input_value('.ladder .ls-rows') == '30'

        # A cost this pair has never been given shows the built-in
        # number, plainly — there is no defaults page, so what is in
        # the box is what this pair uses.
        assert page.input_value('.ladder .ls-comm-a') == '2'
        assert 'own' in page.get_attribute('.ladder .ls-comm-a', 'title')

        # ...and one it HAS been given shows that.
        assert page.input_value('.ladder .ls-comm-b') == '3.5'

        # Applying sends the RUNNING value for the live fields — never a
        # null, which raises on the engine — and writes every cost to
        # this pair, so nothing is left implicit.
        page.evaluate(SPY_ON_PAIR_SAVE)
        page.click('.ladder .ls-save')
        page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
        sent = page.evaluate('() => window.__sent')

        assert sent['order_type'] == 'LIMIT'
        assert sent['commission_per_lot_a'] == 2         # written explicitly
        assert sent['commission_per_lot_b'] == 3.5       # its own
    finally:
        page.evaluate('() => { window.fetch = window.__realFetch; }')
        page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    page.wait_for_selector('.ladder .grid tbody tr')


def test_a_cost_typed_on_one_ladder_is_saved_to_THAT_pair(page):
    """Every ladder carries its own numbers. There is no defaults page
    to fall back to, so what is typed here is what this pair uses."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-tp', timeout=WAIT)

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.fill('.ladder .ls-tp', '4.5')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    assert page.evaluate('() => window.__sent')['tp_target_pct_of_margin'] \
        == 4.5

    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-tp', timeout=WAIT)
    page.evaluate("() => { window.__sent = null; }")
    page.fill('.ladder .ls-tp', '0')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)
    # 0 is a real target: break-even alone, and no profit on top.
    assert page.evaluate('() => window.__sent')['tp_target_pct_of_margin'] == 0

    page.evaluate('() => { window.fetch = window.__realFetch; }')
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")


def test_the_rail_carries_no_form_the_market_can_outrun(page):
    """The rail is read top to bottom while the market moves. The
    overnight rule and the AutoRoute switch are exit logic, not
    something pressed at the touch, so they are in this ladder's
    settings — and the three cancels are one row, not three."""
    open_ladder(page)

    assert page.locator('.ladder .rail .overnight').count() == 0
    assert page.locator('.ladder .rail .auto-route').count() == 0
    assert page.locator('.ladder .rail .cxl-row .cxl').count() == 3

    # ...and the rail fits without scrolling at the default size.
    fits = page.evaluate("""() => {
        const rail = document.querySelector('.window.ladder .rail');
        return rail.scrollHeight <= rail.clientHeight + 1;
    }""")
    assert fits


def test_the_exchanges_page_says_where_the_per_ladder_settings_are(page):
    """A generic Exits page would be four numbers that are right for
    whichever pair the operator had in mind when they typed them."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .trading-fields', timeout=WAIT)

    assert page.locator('.window.settings .s-comm-a').count() == 0
    assert 'per LADDER' in page.text_content('.window.settings .where-exits')
    page.click('.window.settings .close')
    page.wait_for_selector('.ladder .grid tbody tr')


def test_a_check_shows_its_answer_as_a_checklist_with_fixes(page):
    """A checklist that only says FAIL is one that sends the operator
    to a forum."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/connect') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: false, overall: 'FAIL', passed: 1, warnings: 0,
                    failed: 1, connected: false,
                    checks: [
                        {scope: 'CFI Spot', name: 'MT5 terminal',
                         status: 'PASS', message: 'attached', fix: []},
                        {scope: 'CFI Spot', name: 'Algo Trading',
                         status: 'FAIL',
                         message: 'OFF — MT5 will refuse every order with '
                             + '"10027 AutoTrading disabled by client"',
                         fix: ['Press the Algo Trading button in THIS '
                               + "terminal's toolbar (it turns green)"]}
                    ]}), {status: 200,
                          headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")

    page.locator('tr[data-account] .connect-account').first.click()
    page.wait_for_selector('tr.checklist', timeout=WAIT)

    text = page.text_content('tr.checklist')
    assert '10027' in text                       # the broker's own words
    assert 'turns green' in text                 # ...and what to do
    assert page.locator('tr.c-fail').count() == 1
    assert page.locator('tr.c-pass').count() == 1
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_operator_is_told_when_the_system_is_connected(page):
    """Said once, when it becomes true — a banner that never changes is
    a banner nobody reads."""
    page.click('#open-settings')
    page.wait_for_selector('.conn', timeout=WAIT)
    # Nothing is really connected in this fixture, and it says so
    # plainly rather than showing a green light.
    assert 'NOT READY' in page.text_content('.conn')

    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/connection') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, connected: true, blockers: [],
                    accounts: [], feeds: [],
                    broker_clock: {broker_time: '19:55:02', offset_sec: 10800,
                                   cutoff: '16:55',
                                   note: "broker time, +3.0h from this "
                                       + "machine — the 16:55 cutoff is on "
                                       + "the broker's clock"},
                    summary: 'Connected — both accounts logged in, Algo '
                        + 'Trading on, and prices arriving. You can trade.'
                }), {status: 200,
                     headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        window.MT5Settings.refresh();
    }""")
    page.wait_for_selector('.conn.up', timeout=6000)

    assert 'CONNECTED' in page.text_content('.conn.up')
    assert 'You can trade' in page.text_content('.conn.up')
    # And which clock the session cutoff is on.
    assert 'Broker time 19:55:02' in page.text_content('.conn.up')
    assert "the 16:55 cutoff is on the broker's clock" in \
        page.text_content('.conn.up')
    # It is also said once, out loud.
    page.wait_for_selector('.toast.ok', timeout=WAIT)
    assert 'You can trade' in page.text_content('.toast.ok')
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_pair_form_offers_the_number_it_derived_as_a_click(page):
    """A warning nobody can act on is not a fix — but nothing is
    corrected silently either. The derived value is offered as a
    button."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    page.click('.window.settings .new-pair')
    page.wait_for_selector('.pair-form')

    page.fill('.p-key', 'XAUUSD_|GC1226')
    page.fill('.p-beta', '10')            # a beta from another instrument
    page.evaluate("""() => {
        // Stand in for the leg runners: this page talks to them
        // directly, and there are none in a browser test.
        window.__realFetch = window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/derive') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, suggested_beta: 1.0,
                    beta_reason: 'SPOT_FUTURE: the two legs are the same '
                        + 'underlying, so the spread IS the basis and beta is 1',
                    increment: 0.01,
                    increment_derivation: 'max(tick B 0.01, beta 1 x tick A 0.01)',
                    clip_lots_a: 0.1, clip_lots_b: 0.1,
                    clip_derivation: '1 spread = 0.1 A / 0.1 B',
                    spread_units: 10, min_notional_usd: 42921,
                    spread_now: 59.1, widths: {a: 0.2, b: 0.4},
                    quoting_leg_suggestion: 'b',
                    quoting_note: 'the wider bid-ask is the spread you earn, '
                        + 'and usually the less liquid leg',
                    specs: {a: {contract_size: 100}, b: {contract_size: 100}},
                    stamped_for: 'XAUUSD_|GC1226'
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.click('.derive-pair')
    page.wait_for_selector('.use-beta')

    # The wrong beta is still in the field — nothing was changed for us.
    assert page.input_value('.p-beta') == '10'
    assert 'the spread IS the basis' in page.text_content('.pair-form')
    page.click('.use-beta')
    page.wait_for_timeout(150)
    assert page.input_value('.p-beta') == '1'

    # And the derivation is on the screen beside the number.
    assert 'max(tick B 0.01' in page.text_content('.pair-form')
    assert '$42,921' in page.text_content('.derived') or \
        '42921' in page.text_content('.derived')
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def add_account(page, name, endpoint, login=''):
    """Fill the blank row at the bottom and save it.

    The row is re-created on every refresh, so the fields are filled and
    then CHECKED before the click — a value typed into a row that has
    since been replaced is exactly the class of fault these browser
    tests exist to catch.
    """
    row = page.locator('.window.settings tr.new')
    row.locator('.f-name').fill(name)
    row.locator('.f-endpoint').fill(endpoint)
    if login:
        row.locator('.f-login').fill(login)
    assert row.locator('.f-name').input_value() == name
    row.locator('.save-account').click()


# -- moving the windows ---------------------------------------------------

def drag(page, selector, dx, dy, steps=8):
    """Drag one window by its title bar, the way a hand does it."""
    bar = page.locator(selector + ' .titlebar').first
    box = bar.bounding_box()
    start = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0] + dx, start[1] + dy, steps=steps)
    page.mouse.up()


def tidy(page):
    page.click('#tidy')


def open_ladder(page):
    """The settings tests leave the desk showing their own panel; these
    are about a window, so put one on it.

    And put it back to a KNOWN state while we are here. These tests run
    in a random order against one shared page, so whatever the last one
    left behind is what this one starts from — a settings pane left
    open sits over the ladder and swallows the click a click test is
    about, which is a real failure of the test and not of the code.
    """
    page.wait_for_function(
        "() => Object.keys((window.MT5Trader.state.snapshot || {}).pairs "
        "|| {}).length > 0", timeout=WAIT)
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        const key = Object.keys(state.snapshot.pairs || {})[0];
        state.open = [window.MT5Trader.panelId('ladder', key)];
        // A window CLOSED by a previous test stays closed — that is
        // the product's rule, and it is remembered in this browser. So
        // the desk is cleared here rather than each test inheriting
        // whatever the last one decided.
        state.closed = {};
        window.MT5Trader.render();
        // A pane over the ladder, a stubbed fetch and a stale toast are
        // all things one test leaves for the next one.
        document.querySelectorAll('.ladder-settings').forEach(
          function (pane) { pane.hidden = true; });
        if (window.__realFetch) { window.fetch = window.__realFetch; }
        const toasts = document.getElementById('toasts');
        if (toasts) { toasts.innerHTML = ''; }
    }""")
    page.wait_for_selector('.window.ladder .titlebar', timeout=WAIT)


def test_a_window_goes_where_it_is_dragged_and_is_still_there_after_a_reload(
        page):
    """The layout is a preference about this screen, so it lives in the
    browser — but it has to SURVIVE, or every reload rearranges the desk
    under the trader."""
    open_ladder(page)
    tidy(page)
    before = page.locator('.window.ladder').first.bounding_box()

    drag(page, '.window.ladder', 220, 130)

    after = page.locator('.window.ladder').first.bounding_box()
    assert after['x'] - before['x'] == pytest.approx(220, abs=6)
    assert after['y'] - before['y'] == pytest.approx(130, abs=6)
    assert page.locator('.window.ladder.floating').count() == 1

    page.reload()
    page.wait_for_selector('.ladder .grid tbody tr')
    open_ladder(page)
    restored = page.locator('.window.ladder').first.bounding_box()
    assert restored['x'] == pytest.approx(after['x'], abs=2)
    assert restored['y'] == pytest.approx(after['y'], abs=2)
    tidy(page)


def test_a_window_can_never_be_dropped_where_it_cannot_be_got_back(page):
    """A window dragged off the edge with no title bar left on screen is
    a window that cannot be moved, closed or focused again — and the
    only way back would be clearing browser storage."""
    open_ladder(page)
    tidy(page)

    drag(page, '.window.ladder', -4000, -4000)

    bar = page.locator('.window.ladder .titlebar').first.bounding_box()
    desktop = page.locator('#desktop').bounding_box()
    assert bar['y'] >= desktop['y'] - 1
    assert bar['x'] + bar['width'] > desktop['x'] + 40   # still grabbable

    drag(page, '.window.ladder', 4000, 4000)
    bar = page.locator('.window.ladder .titlebar').first.bounding_box()
    assert bar['x'] < desktop['x'] + desktop['width']
    assert bar['y'] < desktop['y'] + desktop['height']
    tidy(page)


def test_dragging_a_title_bar_is_never_an_order(page):
    """The drag passes right over the ladder, which is where a click
    places an order. It must not be one."""
    open_ladder(page)
    tidy(page)
    before = command_count(page)

    drag(page, '.window.ladder', 60, 220)

    page.wait_for_timeout(300)
    assert command_count(page) == before
    tidy(page)


def test_a_button_in_the_title_bar_still_works_while_windows_move(page):
    """The close button lives IN the title bar. A drag handler that
    swallowed its click would make a window impossible to close."""
    tidy(page)
    page.evaluate("""() => {
        window.MT5Trader.state.open = ['monitor:'];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.window.monitor', timeout=WAIT)

    page.click('.window.monitor .titlebar .close')

    page.wait_for_selector('.window.monitor', state='detached', timeout=WAIT)


def test_tidy_puts_every_window_back_in_the_row(page):
    """The way out of a mess, and the one instruction that fixes any
    arrangement a non-technical operator can get into."""
    open_ladder(page)
    drag(page, '.window.ladder', 180, 90)
    assert page.locator('.window.floating').count() >= 1

    tidy(page)

    assert page.locator('.window.floating').count() == 0
    assert page.evaluate(
        "() => window.localStorage.getItem('mt5trader.windows.v1')") in (
            None, '{}')


def seed_config(page, pairs=True):
    """A saved account and pair, as a configured install has."""
    config = {'accounts': {'acct_a': {'endpoint': '127.0.0.1:9101'},
                           'acct_b': {'endpoint': '127.0.0.1:9102'}},
              'pairs': {}}
    if pairs:
        config['pairs'] = {'XAUUSD_|GC1226': {
            'name': 'Gold basis', 'enabled': True, 'hedge_ratio': 1.0,
            'hedge_ratio_for': 'XAUUSD_|GC1226',
            'leg_a': {'account': 'acct_a', 'symbol': 'XAUUSD_'},
            'leg_b': {'account': 'acct_b', 'symbol': 'GC1226'}}}
    with open(page.paths['config'], 'w', encoding='utf-8') as f:
        json.dump(config, f)
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .pairs', timeout=WAIT)
    page.evaluate('() => window.MT5Settings.refresh()')
    page.wait_for_timeout(300)


def test_a_window_is_freely_expandable_from_its_corner(page):
    """A ladder is a price column: how many rows fit on screen is the
    difference between seeing the market and scrolling for it."""
    open_ladder(page)
    tidy(page)
    before = page.locator('.window.ladder').first.bounding_box()
    grip = page.locator('.window.ladder .grip').first.bounding_box()

    # Wider, and SHORTER: a window is never taller than the desktop it
    # is on — a ladder whose bottom rows are off screen is a ladder the
    # trader cannot click.
    page.mouse.move(grip['x'] + 6, grip['y'] + 6)
    page.mouse.down()
    page.mouse.move(grip['x'] + 206, grip['y'] - 144, steps=8)
    page.mouse.up()

    after = page.locator('.window.ladder').first.bounding_box()
    assert after['width'] - before['width'] == pytest.approx(200, abs=8)
    assert before['height'] - after['height'] == pytest.approx(150, abs=10)

    page.reload()
    page.wait_for_selector('.ladder .grid tbody tr')
    open_ladder(page)
    restored = page.locator('.window.ladder').first.bounding_box()
    assert restored['width'] == pytest.approx(after['width'], abs=2)
    assert restored['height'] == pytest.approx(after['height'], abs=2)
    tidy(page)
    assert page.locator('.window.ladder').first.bounding_box()['width'] == \
        pytest.approx(before['width'], abs=2)


def test_the_panels_are_not_redrawn_when_the_engine_published_nothing(page):
    """The screen polls three times a second. Redrawing every ladder,
    the grid and the monitor on a snapshot the coordinator has not
    republished is wasted work — and it is worst exactly when the
    coordinator is stalled or restarting, which is when the operator is
    trying to click things to fix it."""
    open_ladder(page)
    frozen = page.evaluate("""() => {
        const snapshot = JSON.parse(JSON.stringify(
            window.MT5Trader.state.snapshot));
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/status') >= 0) {
                return Promise.resolve(new Response(JSON.stringify(snapshot),
                    {status: 200,
                     headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        document.querySelector('.window.ladder .grid').dataset.stamp = 'kept';
        return snapshot.at;
    }""")
    assert frozen

    page.wait_for_timeout(1200)              # four polls, same snapshot

    assert page.get_attribute('.window.ladder .grid', 'data-stamp') == 'kept'
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def test_the_pairs_table_says_whether_each_ladder_is_actually_quoting(page):
    """"Saved" is not "connected". A pair whose symbol does not exist at
    the broker looks perfect in the config and cannot trade — the engine
    knows, so the row says so."""
    seed_config(page)

    cell = page.locator('td.pair-status').first
    assert 'CONNECTED' in cell.text_content()

    # ...and when the engine reports a problem on that pair, the row
    # carries the engine's own words rather than a green light.
    page.evaluate("""() => {
        const pairs = window.MT5Trader.state.snapshot.pairs;
        const key = Object.keys(pairs)[0];
        pairs[key].errors = ["leg A: 'XAUUSD_' is not on account 'leg_a'"];
        window.MT5Settings.render();
    }""")
    text = page.text_content('td.pair-status')
    assert "is not on account" in text
    assert page.locator('td.pair-status.c-fail').count() >= 1


def test_a_repaint_under_the_pointer_does_not_swallow_the_click(page):
    """A click is mousedown AND mouseup on the SAME element. The
    connection poll rebuilds this whole section every five seconds with
    `innerHTML =`; landing between the two, it destroyed the button the
    operator was pressing and the browser fired NO click — Save pair did
    nothing, said nothing and sent nothing."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')

    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.__sent = [];
        window.fetch = function (url, options) {
            window.__sent.push((options && options.method) + ' ' + String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
        };
    }""")

    page.locator('.save-pair').scroll_into_view_if_needed()
    box = page.locator('.save-pair').bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.down()
    # Mark the button the operator is actually pressing.
    page.evaluate("() => { document.querySelector('.save-pair').__me = 1; }")
    # ...and the poll fires, with the button still held down.
    page.evaluate('() => window.MT5Settings.render()')
    assert page.evaluate("() => !!document.querySelector('.save-pair').__me"), \
        'the button was replaced under the pointer — the click is lost'
    page.mouse.up()
    page.wait_for_timeout(400)

    sent = page.evaluate('() => window.__sent')
    page.evaluate('() => { window.fetch = window.__realFetch; }')
    assert any(u.startswith('POST /api/pairs') for u in sent), sent
    # ...and the repaint it held off is not lost, only deferred.
    page.wait_for_timeout(100)
    assert page.evaluate('() => window.MT5Settings.state.pressing') is False


def test_a_click_that_throws_says_so(page):
    """A handler that raised did nothing and SAID nothing — from the
    operator's side, identical to a button that is not wired up."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function () { throw new Error('boom'); };
    }""")
    page.click('.save-pair')
    page.wait_for_selector('.toast', timeout=WAIT)
    assert 'that click failed' in page.text_content('#toasts')
    assert 'boom' in page.text_content('#toasts')
    page.evaluate("""() => {
        window.fetch = window.__realFetch;
        document.getElementById('toasts').innerHTML = '';
        window.MT5Settings.state.editing = null;
        window.MT5Settings.render();
    }""")


def test_a_key_typed_with_spaces_is_saved_as_one_pair(page):
    """`XAUUSD.f | GCZ6.f` is what a person types. It is an IDENTIFIER,
    matched exactly by the snapshot and by every panel, so it is tidied
    to the one spelling everything else writes rather than becoming a
    second pair nothing can find."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)

    page.fill('.p-key', '  XAUUSD.f | GCZ6.f ')
    page.fill('.p-symbol-a', 'XAUUSD.f')
    page.fill('.p-symbol-b', 'GCZ6.f')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    saved = page.evaluate("""async () => {
        window.__realFetch = window.__realFetch || window.fetch;
        const seen = [];
        window.fetch = function (url, options) {
            seen.push((options && options.method) + ' ' + String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x'}),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
        };
        document.querySelector('.save-pair').click();
        await new Promise(function (r) { window.setTimeout(r, 400); });
        window.fetch = window.__realFetch;
        return seen;
    }""")
    posts = [u for u in saved if u.startswith('POST')]
    assert posts, saved
    assert 'XAUUSD.f%7CGCZ6.f' in posts[0], posts
    assert '%20' not in posts[0], posts


def test_the_pair_type_is_the_one_the_ladder_also_shows(page):
    """The Exchanges page offered DIFFERENT and the ladder offers
    RELATED. A pair saved under the first left the ladder's Pair type
    box showing nothing selected — and saving that box wrote back
    whatever it happened to be showing."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)
    options = page.eval_on_selector_all(
        '.p-type option', 'els => els.map(e => e.value)')
    assert options == ['SPOT_FUTURE', 'FUTURE_FUTURE', 'RELATED'], options


def test_a_new_pairs_key_is_built_from_its_two_symbols(page):
    """Typing `XAUUSD|GCZ6` by hand is not something a trading screen
    should ask for — and a key typed wrong is a pair that never loads."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=WAIT)

    page.fill('.p-symbol-a', 'XAUUSD')
    page.fill('.p-symbol-b', 'GCZ6')
    page.select_option('.p-account-a', 'acct_a')
    page.select_option('.p-account-b', 'acct_b')
    saved = page.evaluate("""async () => {
        window.__realFetch = window.__realFetch || window.fetch;
        const seen = [];
        window.fetch = function (url, options) {
            seen.push(String(url));
            return Promise.resolve(new Response(
                JSON.stringify({ok: true, pair: 'x', restart_required: true}),
                {status: 200,
                 headers: {'Content-Type': 'application/json'}}));
        };
        document.querySelector('.save-pair').click();
        await new Promise(function (r) { window.setTimeout(r, 400); });
        window.fetch = window.__realFetch;
        return seen;
    }""")

    assert any('XAUUSD%7CGCZ6' in url or 'XAUUSD|GCZ6' in url
               for url in saved), saved


def test_the_ladder_centres_on_the_mid_and_leaves_a_hand_scroll_alone(page):
    """Both halves matter: the market belongs in the middle, and a
    trader reading a level twenty rows away must not have it snatched
    back under the cursor."""
    open_ladder(page)
    tidy(page)
    page.evaluate("() => { window.MT5Trader.state.locked = {}; }")
    page.wait_for_timeout(600)

    centred = page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        const rows = [...document.querySelectorAll('.ladder .grid tbody tr')];
        const mid = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market.spread;
        const row = rows.reduce((best, r) =>
            Math.abs(r.dataset.level - mid) < Math.abs(best.dataset.level - mid)
                ? r : best);
        const top = row.offsetTop - grid.scrollTop;
        return {top: top, height: grid.clientHeight};
    }""")
    # The mid row sits in the middle third of the window, not at an edge.
    assert centred['height'] / 3 < centred['top'] < \
        centred['height'] * 2 / 3

    # Now scroll by hand and hold it there through several polls.
    page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        grid.scrollTop = 0;
        grid.dispatchEvent(new Event('scroll'));
    }""")
    page.wait_for_timeout(1200)
    assert page.evaluate(
        "() => document.querySelector('.ladder .grid').scrollTop") == 0


def test_the_taskbar_says_whether_the_whole_thing_is_working(page):
    """One line, always on screen: both accounts attached AND every
    enabled ladder quoting. Anything less is not green."""
    page.wait_for_timeout(400)
    badge = page.locator('#link-badge')
    assert 'LIVE' in badge.text_content()
    assert 'ok' in (badge.get_attribute('class') or '')

    # A ladder with no quote is not "connected", whatever the accounts
    # say — the pair cannot be traded.
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        state.snapshot.pairs['XAUUSD_|GC1226'].market = null;
        window.MT5Trader.render();
    }""")
    assert 'NO QUOTE' in page.text_content('#link-badge')


def test_the_keyboard_can_be_turned_off_entirely(page):
    """B and S are ORDERS. A desk that does not want a keyboard near
    them can have none — and the buttons still work."""
    open_ladder(page)
    page.click('.ladder .titlebar')
    page.click('#keys-toggle')
    before = command_count(page)

    page.keyboard.press('b')
    page.wait_for_timeout(250)

    assert command_count(page) == before
    assert 'off' in page.text_content('#keys-toggle')
    # The button beside it still sends: turning the keys off is not
    # turning trading off.
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    page.click('#keys-toggle')


def test_every_ladder_is_reachable_from_one_menu(page):
    """A desk trades several spreads at once — Spot vs Future, WTI vs
    Brent — and each one is a window on this desktop."""
    page.click('#add-panel')
    page.wait_for_selector('#add-menu:not(.hidden)', timeout=WAIT)

    text = page.text_content('#add-menu')
    assert 'Gold basis' in text and 'XAUUSD_' in text
    assert 'Market Grid' in text and 'Trading Monitor' in text

    page.click('#add-menu button[data-panel="monitor:"]')
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    assert page.locator('#add-menu.hidden').count() == 1
    page.click('.window.monitor .titlebar .close')


def test_the_sounds_are_generated_here_and_can_be_silenced(page):
    """No files, nothing fetched: a blocked network must not be able to
    silence a fill. And silence is a first-class setting."""
    played = page.evaluate("""() => {
        const seen = [];
        const Ctor = window.AudioContext || window.webkitAudioContext;
        const original = Ctor.prototype.createOscillator;
        Ctor.prototype.createOscillator = function () {
            const osc = original.call(this);
            seen.push(true);
            return osc;
        };
        window.MT5Trader.sound('filled');
        window.MT5Trader.state.soundOff = true;
        window.MT5Trader.sound('filled');
        window.MT5Trader.state.soundOff = false;
        Ctor.prototype.createOscillator = original;
        return seen.length;
    }""")
    # Two notes for a fill, and nothing at all while it is off.
    assert played == 2
    assert page.locator('#sound-toggle').count() == 1


def test_a_pair_added_while_the_screen_is_open_gets_its_own_ladder(page):
    """A pair configured on the Exchanges page and then nowhere to be
    seen is the whole setup looking broken. It appears beside the
    others, and a ladder the trader CLOSED stays closed."""
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        state.closed = {};
        const pairs = state.snapshot.pairs;
        const copy = JSON.parse(JSON.stringify(pairs['XAUUSD_|GC1226']));
        copy.key = 'EURUSD|GBPUSD';
        copy.name = 'EURUSD - GBPUSD';
        copy.symbol_a = 'EURUSD';
        copy.symbol_b = 'GBPUSD';
        pairs['EURUSD|GBPUSD'] = copy;
        window.MT5Trader.render();
    }""")
    # The snapshot the publisher writes does not have it, so drive one
    # poll's worth of the same logic the poller runs.
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        Object.keys(state.snapshot.pairs).forEach(function (key) {
            const id = window.MT5Trader.panelId('ladder', key);
            if (state.open.indexOf(id) < 0 && !state.closed[id]) {
                state.open.unshift(id);
            }
        });
        window.MT5Trader.render();
    }""")

    assert page.locator('.window.ladder').count() == 2
    assert 'EURUSD - GBPUSD' in page.text_content('#tabs')

    # Closed stays closed.
    page.evaluate("""() => window.MT5Trader.closePanel(
        window.MT5Trader.panelId('ladder', 'EURUSD|GBPUSD'))""")
    page.wait_for_timeout(700)
    assert page.locator('.window.ladder').count() == 1
    page.evaluate("""() => {
        delete window.MT5Trader.state.snapshot.pairs['EURUSD|GBPUSD'];
        window.MT5Trader.render();
    }""")


def test_a_window_opened_at_the_end_of_the_row_is_scrolled_into_view(page):
    """The desktop is a row that scrolls. With ladders and the grid
    already on it, a window opened at the end sits off the right-hand
    edge — and "Positions opens into nothing" is what that looks like
    from the outside."""
    tidy(page)
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.state.closed = {};
        UI.closePanel(UI.panelId('monitor'));
        // A desktop wide enough that the end of the row is off screen.
        document.querySelectorAll('.window.ladder').forEach(function (node) {
            node.style.width = '900px';
        });
        UI.state.open = [UI.panelId('ladder', 'XAUUSD_|GC1226'),
                         UI.panelId('grid')];
        UI.render();
        document.getElementById('desktop').scrollLeft = 0;
    }""")

    page.evaluate("() => window.MT5Trader.openPanel('monitor:')")
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    page.wait_for_timeout(200)

    visible = page.evaluate("""() => {
        const node = document.querySelector('.window.monitor');
        const desktop = document.getElementById('desktop');
        const box = node.getBoundingClientRect();
        const frame = desktop.getBoundingClientRect();
        return box.left >= frame.left - 8 && box.left < frame.right;
    }""")
    assert visible

    page.evaluate("""() => {
        document.querySelectorAll('.window.ladder').forEach(function (node) {
            node.style.width = '';
        });
    }""")
    tidy(page)


def test_a_pair_row_names_the_account_each_leg_trades_on(page):
    """Two accounts is the architecture, and which login a leg is on is
    the first question anyone asks of a spread that has gone wrong."""
    seed_config(page)

    # Scoped to the settings window: the Market Grid marks its rows
    # with data-pair too.
    row = page.locator('.window.settings tr[data-pair] td').nth(2)  # leg A

    assert 'XAUUSD_' in row.text_content()
    assert 'acct_a' in row.text_content()


def test_the_tool_windows_float_over_the_ladders_instead_of_queueing(page):
    """The Market Grid, Positions and Exchanges are wide and are opened
    all day. At the end of the desktop row they push every ladder off
    the screen, and reaching one means scrolling sideways past the
    lot."""
    tidy(page)
    open_ladder(page)
    page.evaluate("() => window.MT5Trader.openPanel('monitor:')")
    page.wait_for_selector('.window.monitor', timeout=WAIT)
    page.wait_for_timeout(200)

    assert page.locator('.window.monitor.floating').count() == 1
    inside = page.evaluate("""() => {
        const box = document.querySelector('.window.monitor')
            .getBoundingClientRect();
        const frame = document.getElementById('desktop')
            .getBoundingClientRect();
        return box.left >= frame.left - 2 && box.right <= frame.right + 2;
    }""")
    assert inside
    # ...and the ladder is still where it was, in the row behind it.
    assert page.locator('.window.ladder.floating').count() == 0
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")
    tidy(page)


def test_the_journal_colours_what_was_made_and_what_was_lost(page):
    """A column of black numbers is one nobody reads twice. An OPENING
    deal books nothing, though, so 0.00 stays black — colouring it green
    would make every entry look like a winner."""
    page.evaluate("""() => {
        window.__realFetch = window.__realFetch || window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/api/fills') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true,
                    totals: {fills: 3, volume: 3, commission: -2,
                             swap: 0, profit: 64.9},
                    fills: [
                        {deal_id: '1', account: 'a', symbol: 'X',
                         side: 'buy', entry: 'close', volume: 1,
                         price: 10, profit: 81.1, is_ours: 1},
                        {deal_id: '2', account: 'a', symbol: 'X',
                         side: 'sell', entry: 'close', volume: 1,
                         price: 10, profit: -16.2, is_ours: 1},
                        {deal_id: '3', account: 'a', symbol: 'X',
                         side: 'buy', entry: 'open', volume: 1,
                         price: 10, profit: 0.0, is_ours: 1}
                    ]}), {status: 200,
                          headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
        const UI = window.MT5Trader;
        UI.state.fills = null;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'fills';
        UI.render();
    }""")
    page.wait_for_function(
        "() => document.querySelectorAll('.monitor td.up').length > 0",
        timeout=WAIT)

    # Read in ONE evaluate: the monitor repaints on every publish, and a
    # node resolved in Python can be detached before its style is read —
    # which comes back as an empty string, not as a wrong colour.
    colours = page.evaluate("""() => {
        const up = [...document.querySelectorAll('.monitor td.up')];
        const down = [...document.querySelectorAll('.monitor td.down')];
        return {ups: up.length, downs: down.length,
                made: up[0] ? getComputedStyle(up[0]).color : null,
                lost: down[0] ? getComputedStyle(down[0]).color : null};
    }""")

    assert colours['ups'] == 1                                # the winner
    assert colours['downs'] == 1                              # the loser
    assert colours['made'] == 'rgb(74, 156, 93)'
    assert colours['lost'] == 'rgb(192, 80, 77)'
    page.evaluate("""() => {
        window.fetch = window.__realFetch;
        window.MT5Trader.state.fills = null;
        window.MT5Trader.state.monitorTab = 'positions';
    }""")


def test_the_armed_size_applies_to_both_sides(page):
    """One number, both directions: a keypad that armed only the buy
    side would be a size that means something different depending on
    which button is pressed."""
    open_ladder(page)
    page.click('.ladder .titlebar')
    page.click('.ladder .keypad button[data-qty="10"]')
    page.wait_for_timeout(150)
    assert page.input_value('.ladder .armed') == '10'
    assert page.input_value('.ladder .sell-qty') == '10'

    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 10
    assert last_command(page)['payload']['side'] == 'BUY'

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 10
    assert last_command(page)['payload']['side'] == 'SELL'

    # Armed is not the ladder's default, so it does not look like it.
    colour = page.locator('.ladder .armed').evaluate(
        'n => getComputedStyle(n).backgroundColor')
    assert colour == 'rgb(12, 133, 153)'
    page.click('.ladder .keypad button.clr')


def test_the_cancel_buttons_are_dead_when_there_is_nothing_to_cancel(page):
    """Three buttons that look pressable and do nothing are three
    buttons the trader wonders about while the market moves."""
    open_ladder(page)
    # Wait for a render off a fresh snapshot rather than a fixed pause:
    # the button's state comes from the published working counts.
    page.wait_for_function(
        "() => document.querySelector('.ladder .cxl-all')"
        " && document.querySelector('.ladder .cxl-all').disabled",
        timeout=WAIT)

    assert page.locator('.ladder .cxl-all').is_disabled()
    assert 'CXL All' in page.text_content('.ladder .cxl-all')

    # Published, not poked into the page: the next snapshot is 200ms
    # away and erases anything written into it by hand.
    page.paths['publisher'].working_buys = 2
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => !document.querySelector('.ladder .cxl-b').disabled",
        timeout=WAIT)

    assert '2' in page.text_content('.ladder .cxl-b')
    page.paths['publisher'].working_buys = 0
    page.paths['publisher'].publish()


def test_the_mid_carries_a_rule_of_its_own(page):
    """On a wide spread the two touches are many rows apart, and "where
    is the market" is a question the inside rule alone cannot answer."""
    open_ladder(page)
    page.wait_for_selector('.ladder tr.mid-line', timeout=WAIT)

    border = page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.ladder tr.mid-line td')).borderTopWidth")
    assert border == '3px'
    assert page.locator('.ladder tr.mid-line').count() == 1


def test_each_side_can_carry_its_own_size(page):
    """Usually they are the same — the keypad sets both — but a desk
    that wants to lift 1 and offer 5 types each one, in the box beside
    the button that will send it."""
    open_ladder(page)
    page.click('.ladder .keypad button[data-qty="1"]')
    page.fill('.ladder .sell-qty', '5')
    page.wait_for_timeout(150)

    assert 'BUY 1' in page.text_content('.ladder .buy-touch')
    assert 'SELL 5' in page.text_content('.ladder .sell-touch')

    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 1

    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['quantity'] == 5

    # ...and a ladder click takes the size of the side it is on: the
    # Asks column buys, the Bids column sells.
    page.locator('.ladder .grid tbody tr td.ask').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload'] ['side'] == 'BUY'
    assert last_command(page)['payload']['quantity'] == 1

    page.locator('.ladder .grid tbody tr td.bid').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'SELL'
    assert last_command(page)['payload']['quantity'] == 5

    page.click('.ladder .keypad button.clr')


def test_any_size_from_0_01_can_be_typed_into_the_qty_box(page):
    """The keypad is five common sizes; the box is every other one. A
    lot is 0.01 at most brokers, and a ladder that only offers 1, 5, 10,
    50 and 100 cannot trade the size the account is sized for."""
    open_ladder(page)
    page.fill('.ladder .armed', '0.25')
    page.wait_for_timeout(150)

    before = command_count(page)
    page.click('.ladder .buy-touch')
    page.wait_for_timeout(250)

    assert command_count(page) == before + 1
    assert last_command(page)['payload']['quantity'] == 0.25
    # Emptying it goes back to the ladder's own default, not to zero —
    # a click that sends nothing is worse than one that sends the
    # default.
    page.fill('.ladder .armed', '')
    page.wait_for_timeout(150)
    page.click('.ladder .sell-touch')
    page.wait_for_timeout(250)
    assert last_command(page)['payload'].get('quantity') in (None, 1)


def test_the_shared_account_notice_can_be_put_away_and_comes_back(page):
    """It is an FYI, not a blocker: it names something worth knowing and
    the trader keeps working. Dismissing it is for the SITUATION they
    read — a different one brings it back."""
    page.evaluate('() => { window.MT5Trader.state.dismissed = {}; }')
    page.paths['publisher'].same_login = {'100006': ['leg_a', 'leg_b']}
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner:not(.hidden)', timeout=WAIT)
    assert '#100006' in page.text_content('#same-login-banner')
    # One LINE on the screen; the full reading of it is the tooltip, and
    # the button goes where it is fixed.
    assert 'One account, not two' in page.text_content('#same-login-banner')
    assert 'one pool' in page.get_attribute('#same-login-banner', 'title')
    assert page.locator('#same-login-banner .banner-open').count() == 1

    page.click('#same-login-banner .banner-close')
    page.wait_for_selector('#same-login-banner.hidden', state='attached',
                           timeout=WAIT)

    # A DIFFERENT login is a different situation, and it is said again.
    page.paths['publisher'].same_login = {'200007': ['leg_a', 'leg_b']}
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner:not(.hidden)', timeout=WAIT)

    page.paths['publisher'].same_login = None
    page.paths['publisher'].publish()
    page.wait_for_selector('#same-login-banner.hidden', state='attached',
                           timeout=WAIT)


def test_nothing_in_the_window_is_cropped_when_it_is_made_short(page):
    """The rail is taller than a short window, and the footer carries
    the feed badge and the position. Neither may be cut off: the ladder
    scrolls instead."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.classList.add('sized');
        node.style.height = '320px';
    }""")
    page.wait_for_timeout(200)

    fits = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const box = node.getBoundingClientRect();
        const footer = node.querySelector('.footer').getBoundingClientRect();
        const rail = node.querySelector('.rail');
        return {footerInside: footer.bottom <= box.bottom + 1,
                railScrolls: rail.scrollHeight > rail.clientHeight
                    ? getComputedStyle(rail).overflowY === 'auto' : true};
    }""")
    assert fits['footerInside']
    assert fits['railScrolls']
    tidy(page)


def test_each_legs_book_is_laid_out_with_its_width(page):
    """Bid, ask and the width each leg is charging — and the same three
    for the spread, whose width IS the round turn. Read by comparing
    them, so it is a table."""
    open_ladder(page)
    page.wait_for_selector('table.legbook', timeout=WAIT)

    rows = page.locator('table.legbook tbody tr')
    assert rows.count() == 3                       # A, B, and the spread
    assert '0.2500' in rows.nth(0).text_content()  # leg A's own width
    assert '0.4000' in rows.nth(1).text_content()
    # A leg that has stopped is marked — on its AGE cell, not down the
    # whole line: painting the row red made a quiet market look like a
    # fault.
    assert page.locator('table.legbook td.age').count() == 3
    page.paths['publisher'].stale_leg = True
    page.paths['publisher'].publish()
    page.wait_for_selector('table.legbook td.age.bad', timeout=WAIT)
    assert page.locator('table.legbook td.age.bad').count() == 1
    page.paths['publisher'].stale_leg = False
    page.paths['publisher'].publish()
    spread = rows.nth(2).text_content()
    assert '59.0900' in spread and '59.1100' in spread
    assert '0.0200' in spread                      # one round turn


def test_an_order_the_broker_refused_is_said_out_loud_and_kept_on_screen(page):
    """The failure this was built for: the broker refuses the pending
    behind a synthetic, the order leaves the book in the same instant
    the click was accepted, and the trader is left with a green toast
    and an empty Work column."""
    open_ladder(page)
    page.evaluate('() => { window.MT5Trader.state.reported = {}; }')
    page.paths['publisher'].dead_orders = [{
        'order_id': 'SO-DEAD', 'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY',
        'level': 59.05, 'quantity': 2, 'filled_quantity': 0,
        'order_type': 'LIMIT', 'time_in_force': 'DAY', 'state': 'REJECTED',
        'pending_ticket': None,
        'reason': "10016 Invalid stops — the price is inside the broker's "
                  "stops level"}]
    page.paths['publisher'].publish()

    # Said once, in the broker's own words, and errors do not auto-hide.
    page.wait_for_selector('.toast', timeout=WAIT)
    assert '10016' in page.text_content('.toast')
    assert 'REJECTED' in page.text_content('.toast').upper()

    # ...and it keeps its place in the Working Orders tab with the
    # reason, rather than vanishing.
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.state.open = [UI.panelId('monitor')];
        UI.state.monitorTab = 'orders';
        UI.render();
    }""")
    page.wait_for_selector('.monitor tr.dead', timeout=WAIT)
    text = page.text_content('.monitor tr.dead')
    assert 'REJECTED' in text and '10016' in text

    page.click('.toast')
    page.paths['publisher'].dead_orders = None
    page.paths['publisher'].publish()
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")

def test_the_leg_panel_never_runs_off_the_edge_of_the_ladder(page):
    """It was laid out in `ch` units, which is fine until the prices are
    four digits and a symbol is long — then it ran off the right-hand
    edge and the AGE column went first, which is the column that says
    which leg stopped."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const market = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market;
        market.leg_a_bid = 4609.79; market.leg_a_ask = 4609.99;
        market.leg_b_bid = 4662.75; market.leg_b_ask = 4663.14;
        market.leg_a_quote_age_sec = 0.9;
        market.leg_b_quote_age_sec = 0.6;
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('table.legbook', timeout=WAIT)

    fits = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const table = node.querySelector('table.legbook');
        const win = node.getBoundingClientRect();
        const box = table.getBoundingClientRect();
        const age = table.querySelector('td.c-age').getBoundingClientRect();
        return {inside: box.right <= win.right + 1 && box.left >= win.left - 1,
                ageVisible: age.right <= win.right + 1 && age.width > 0};
    }""")

    assert fits['inside'], 'the leg panel is wider than the ladder'
    assert fits['ageVisible'], 'the Age column fell off the edge'


def test_the_exit_price_is_on_the_rail_for_both_directions(page):
    """The trader wants one number back when they click: where do I get
    out? Break-even first, then break-even plus the target — and both
    directions, because a long leaves on the bid and a short buys back
    on the offer. At the LADDER's precision: the rail is 100px wide and
    two seven-character numbers side by side collide."""
    open_ladder(page)
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 500.0,
        'note': '2% of 500.00 margin per spread = 10.00, over commission'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .tp-buy')"
        " && document.querySelector('.fairwin .tp-buy').textContent"
        " === '60.21'", timeout=WAIT)

    assert page.text_content('.fairwin .be-buy') == '59.21'
    assert page.text_content('.fairwin .tp-sell') == '57.99'
    # The long wording is the tooltip; the rail carries the short form.
    assert '2% of' in page.text_content('.fairwin .exit-note')
    assert 'over commission' in page.get_attribute('.fairwin .exit-note',
                                                    'title')

    # A position that is ON gets its own line, anchored on the price it
    # was ENTERED at.
    page.paths['publisher'].positions = [{
        'position_id': 'P1', 'side': 'BUY', 'quantity': 1,
        'entry_spread': 59.11, 'spread_units': 10, 'order_type': 'MARKET',
        'leg_a': None, 'leg_b': None, 'net_pnl': None, 'gross_pnl': None,
        'closing_spread': None,
        'exit': {'break_even': 59.21, 'tp': 60.21, 'side': 'BUY'}}]
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .exit-note')"
        ".textContent.indexOf('out') >= 0", timeout=WAIT)
    assert 'flat at 59.2100' in page.get_attribute('.fairwin .exit-note',
                                                    'title')

    page.paths['publisher'].exits = None
    page.paths['publisher'].positions = None
    page.paths['publisher'].publish()

def test_the_fair_spread_is_quoted_for_both_directions(page):
    """Buying the spread pays the offer and selling it receives the bid,
    and the two are charged different swaps on different legs. One fair
    value here would be right half the time — and a gap measured off a
    midpoint compares the market against a price nobody fills at."""
    open_ladder(page)
    page.paths['publisher'].fair = {
        'source': 'swap', 'days_to_expiry': 30, 'expects_expiry': True,
        'fair_buy': 0.60, 'fair_sell': 0.40,
        'gap_buy': 0.15, 'gap_sell': -0.20,
        'warning': None, 'fix': None,
        'note': "30 nights of the broker's own swap, over k"}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .fair-buy')"
        " && document.querySelector('.fairwin .fair-buy').textContent"
        " === '0.60'", timeout=WAIT)

    assert page.text_content('.fairwin .fair-sell') == '0.40'
    # Rich on the buy side, cheap on the sell side — and the sign is
    # said in colour as well, because the direction of a basis is the
    # thing everyone gets backwards once.
    assert page.text_content('.fairwin .gap-buy') == '+0.15'
    assert 'down' in (page.get_attribute('.fairwin .gap-buy', 'class') or '')
    assert 'up' in (page.get_attribute('.fairwin .gap-sell', 'class') or '')
    assert '30d' in page.text_content('.fairwin .fair-note')
    assert page.is_hidden('.fairwin .fair-warn')

    page.paths['publisher'].fair = None
    page.paths['publisher'].publish()


def test_a_disputed_swap_replaces_the_reading_and_offers_the_correction(page):
    """A conclusion drawn from an input that can be proven wrong should
    not render at all. The warning stands where the number was, names
    the field, and offers the corrected value as ONE CLICK — never
    applied behind the operator, because a sign the engine flipped by
    itself is a sign nobody would ever notice was wrong."""
    open_ladder(page)
    page.paths['publisher'].fair = {
        'source': 'swap', 'days_to_expiry': 30, 'expects_expiry': True,
        'fair_buy': None, 'fair_sell': None,
        'gap_buy': None, 'gap_sell': None,
        'warning': 'XAGUSD is the leg you would be LONG and its swap is a '
                   'CREDIT (+58.00 a night). A long leg is normally charged '
                   '— check the sign.',
        'fix': {'field': 'swap_a_long_per_lot', 'value': -58.0,
                'symbol': 'XAGUSD'},
        'disputed': {'fair_buy': -51.82, 'fair_sell': -51.82}}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .fair-warn')"
        " && !document.querySelector('.fairwin .fair-warn').hidden",
        timeout=WAIT)

    # The number is GONE, not printed beneath the warning.
    assert page.text_content('.fairwin .fair-buy') == '—'
    assert 'CREDIT' in page.text_content('.fairwin .fair-warn-text')
    assert 'swap a long per lot' in page.text_content('.fairwin .fair-fix')
    assert '-58.00' in page.text_content('.fairwin .fair-fix')

    page.paths['publisher'].fair = None
    page.paths['publisher'].publish()


def test_fair_value_and_the_exit_live_in_their_OWN_window(page):
    """The ladder is for the price. A panel of derived figures beside it
    is a panel between the trader and the market, so fair value and the
    exit are a separate window — off by default, per pair, and floating
    where it is put.

    And the pair is named across the top of it: two of these open at
    once are otherwise two identical tables of figures with nothing to
    say which instrument either belongs to.
    """
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .exit', timeout=WAIT)

    assert page.locator('.window.ladder .fair').count() == 0
    assert page.locator('.window.ladder .exit').count() == 0
    assert page.text_content('.window.fairwin .fw-pair') == 'Gold basis'
    assert 'XAUUSD_' in page.get_attribute('.window.fairwin .fw-pair', 'title')


def test_the_fair_window_is_no_bigger_than_the_figures_in_it(page):
    """Screen beside a ladder is not spare. Two short tables do not
    need a pane the size of a ladder — the labels are read down the
    left and the numbers down the right, and everything between them is
    room a price could have had.

    Its HEIGHT is the content's, not a number: a fixed one leaves a
    panel of empty grey under two short tables, which is what it did.
    """
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .exit', timeout=WAIT)

    box = page.evaluate("""() => {
        const win = document.querySelector('.window.fairwin');
        const body = win.querySelector('.fw-body');
        const rect = win.getBoundingClientRect();
        const chrome = win.querySelector('.titlebar').offsetHeight;
        return {width: rect.width, height: rect.height,
                content: body.scrollHeight + chrome,
                ladder: document.querySelector('.window.ladder')
                    .getBoundingClientRect().width};
    }""")

    # Narrower than a ladder, and no taller than what is in it.
    assert box['width'] <= box['ladder'] * 0.75, box
    assert box['height'] <= box['content'] + 8, box

    # And no CORRIDOR: on every row the figure follows its label rather
    # than sitting against the far edge with nothing in between. That
    # empty middle was most of the window.
    gaps = page.evaluate("""() => {
        const rows = [...document.querySelectorAll(
            '.window.fairwin table.exits tr')];
        return rows.map(function (tr) {
            const label = tr.querySelector('th');
            const value = tr.querySelector('td');
            if (!label || !value) { return 0; }
            return Math.round(value.getBoundingClientRect().left
                              - label.getBoundingClientRect().right);
        });
    }""")
    assert max(gaps) <= 40, gaps

    # A long note does not set the width: it wraps inside whatever the
    # figures need. `width: max-content` sizes to the longest line in
    # the window, and the derivation is a sentence.
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 135.8,
        'note': '2% of 135.80 margin per spread = 10.00 = 1.0000 of '
                'spread, against a 0.3000 session range (margin from '
                'the terminal)'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => (document.querySelector('.window.fairwin .exit-note')"
        ".title || '').indexOf('session range') >= 0", timeout=WAIT)

    wide = page.evaluate(
        "() => document.querySelector('.window.fairwin')"
        ".getBoundingClientRect().width")
    # Room for the figures themselves to be wider than em dashes, and
    # nothing like the width of the sentence underneath them.
    assert wide <= box['width'] + 24, (box['width'], wide)
    page.paths['publisher'].exits = None
    page.paths['publisher'].publish()
    # ...and the figures still fit: nothing is clipped to achieve it.
    clipped = page.evaluate("""() => {
        const cells = [...document.querySelectorAll(
            '.window.fairwin table.exits td, .window.fairwin table.exits th')];
        return cells.filter(c => c.scrollWidth > c.clientWidth + 1).length;
    }""")
    assert clipped == 0


def test_the_fair_window_is_ONE_tick_on_the_ladder_it_belongs_to(page):
    """Per ladder, off by default, and one control for one decision.

    There was a dropdown here (None / Fair spread) beside a Show window
    tick, back when a second algo was being built. That algo was taken
    out; two controls for one decision stayed, and either one alone did
    nothing anybody could see."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-algo-window', timeout=WAIT)

    assert page.locator('.ladder .ls-algo').count() == 0
    assert page.locator('.ladder .ls-algo-window').count() == 1
    # It lives with the Carry fields it is a reading of, not in a group
    # of its own.
    assert page.locator(
        '.ladder .ls-group:has(.ls-pair-type) .ls-algo-window').count() == 1
    note = ' '.join(page.text_content('.ladder .lsf-note').split())
    assert 'it does not trade' in note
    page.click('.ladder .ls-close')


def test_turning_the_window_off_closes_it_and_KEEPS_it_closed(page):
    """Closing it IS turning it off. A window that comes back on the
    next poll — because the pair's setting still says so — is a window
    the trader cannot get rid of."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    page.click('.window.fairwin .close')
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)

    # ...and it stays gone across the polls that follow, with the
    # engine still publishing the setting as ON.
    page.wait_for_timeout(700)
    assert page.locator('.window.fairwin').count() == 0

    page.evaluate(
        "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', true)")
    page.wait_for_selector('.window.fairwin', timeout=WAIT)


def test_the_settings_behind_the_exit_are_one_click_from_it(page):
    """"Where do I change these?" is a question the panel showing the
    numbers should answer itself — and answer with THIS ladder\'s own
    settings, because the next ladder is a different instrument."""
    open_ladder(page)

    page.click('.fairwin .exit .open-exits')
    page.wait_for_selector('.ladder .ladder-settings .ls-tp', timeout=WAIT)

    assert page.text_content('.ladder .ls-pair') == 'XAUUSD_|GC1226'
    page.click('.ladder .ls-close')
    assert page.locator('.ladder .ladder-settings').is_hidden()


def test_the_leg_books_width_column_is_called_Spread(page):
    """The operator's word for it. The spread row's own figure is the
    two legs' summed — one round turn of both books."""
    open_ladder(page)
    page.wait_for_selector('.ladder table.legbook', timeout=WAIT)

    assert page.text_content('.ladder table.legbook th.c-width') == 'Spread'


def test_GTC_carries_its_caveat_on_the_screen(page):
    """A synthetic order lives in THIS process. Nothing at the broker
    knows what a spread is, so GTC cannot mean what it means on an
    exchange — and an operator who reads it as exchange-resident
    believes an order is watching a market that nothing is watching.
    The caveat belongs on the screen, not only in the code."""
    open_ladder(page)

    caveat = page.get_attribute('.ladder .tif', 'title')
    assert 'UNTIL THIS SYSTEM STOPS' in caveat
    assert 'does not resume' in caveat or 'none resumes' in caveat
    assert 'until this system stops' in page.get_attribute(
        '.ladder .tif option[value="GTC"]', 'title')


def test_autorouting_says_what_is_armed_and_that_there_is_no_stop(page):
    """The switch is not the state. A trader who believes a target is
    armed when it is not is the worse failure, so the Exit panel shows
    what is ACTUALLY resting — while the switch itself lives in this
    ladder\'s settings, because one ladder can arm AutoRouting and the
    next not."""
    open_ladder(page)
    assert page.text_content('.fairwin .auto-route-state') == 'off'

    page.paths['publisher'].auto_route = True
    page.paths['publisher'].auto_route_armed = [
        {'position_id': 'POS1', 'level': 60.21, 'order_id': 'SO1',
         'quantity': 1.0}]
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .auto-route-state')"
        ".textContent.indexOf('60.21') >= 0", timeout=WAIT)
    assert 'no stop' in page.get_attribute('.fairwin .auto-route-state',
                                           'title')

    # On, but nothing resting yet — and it says which of the two it is.
    page.paths['publisher'].auto_route_armed = []
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .auto-route-state')"
        ".textContent === 'on'", timeout=WAIT)
    assert 'next fill' in page.get_attribute('.fairwin .auto-route-state',
                                             'title')

    # ...and the switch, with the NO STOP caveat, is in this ladder\'s
    # own settings.
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-auto-route', timeout=WAIT)
    assert 'NO STOP' in page.get_attribute('.ladder .lsf.check-row', 'title')
    page.click('.ladder .ls-close')

    page.paths['publisher'].auto_route = False
    page.paths['publisher'].auto_route_armed = None
    page.paths['publisher'].publish()


def test_a_ladder_ticked_with_the_master_off_does_not_claim_AUTO(page):
    """The tick alone used to put AUTO in the title bar. With the
    system switch off nothing arms on a fill, and a badge saying
    otherwise is the screen promising an exit that will not be there."""
    open_ladder(page)
    page.paths['publisher'].auto_route = True
    page.paths['publisher'].auto_route_master = False
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .mode-badge')"
        ".textContent.indexOf('AUTO OFF') >= 0", timeout=WAIT)
    assert 'switched off' in page.get_attribute('.ladder .mode-badge', 'title')
    assert page.text_content('.fairwin .auto-route-state') == 'off'

    # The control: the same tick with the master ON does say AUTO.
    page.paths['publisher'].auto_route_master = True
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => { var t = document.querySelector('.ladder .mode-badge')"
        ".textContent; return t.indexOf('AUTO') >= 0"
        " && t.indexOf('AUTO OFF') < 0; }", timeout=WAIT)
    assert page.text_content('.fairwin .auto-route-state') == 'on'

    page.paths['publisher'].auto_route = False
    page.paths['publisher'].publish()


def test_nothing_about_the_take_profit_is_sent_to_the_broker():
    """It is a price on the SCREEN. No bracket order, no broker-side
    stop — one leg stopping alone converts the hedge into a naked
    position, which is the rule this whole system is built around."""
    source = (Path(__file__).resolve().parent.parent / 'mt5trader'
              / 'takeprofit.py').read_text(encoding='utf-8')
    for forbidden in ('place_limit', 'order(', 'send_market_order',
                      'sl=', 'tp='):
        assert forbidden not in source, forbidden


def test_the_ladder_grows_in_both_directions_and_the_grid_takes_the_room(page):
    """Wider means a wider price column and a wider leg table under it;
    taller means more price rows on screen, which is the whole point of
    a ladder."""
    open_ladder(page)
    tidy(page)
    page.evaluate("""() => {
        const market = window.MT5Trader.state.snapshot
            .pairs['XAUUSD_|GC1226'].market;
        market.leg_a_bid = 4607.38; market.leg_a_ask = 4607.63;
        market.leg_b_bid = 4660.40; market.leg_b_ask = 4660.80;
        market.leg_a_quote_age_sec = 0.3;
        market.leg_b_quote_age_sec = 1.5;
        window.MT5Trader.render();
    }""")
    before = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const rows = node.querySelectorAll('.grid tbody tr').length;
        const grid = node.querySelector('.grid').getBoundingClientRect();
        return {w: node.getBoundingClientRect().width,
                gridW: grid.width, gridH: grid.height, rows: rows};
    }""")

    grip = page.locator('.window.ladder .grip').first.bounding_box()
    page.mouse.move(grip['x'] + 6, grip['y'] + 6)
    page.mouse.down()
    page.mouse.move(grip['x'] + 246, grip['y'] - 106, steps=8)
    page.mouse.up()

    after = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const grid = node.querySelector('.grid').getBoundingClientRect();
        const table = node.querySelector('table.legbook')
            .getBoundingClientRect();
        const win = node.getBoundingClientRect();
        return {w: win.width, gridW: grid.width, gridH: grid.height,
                tableInside: table.right <= win.right + 1};
    }""")

    assert after['w'] - before['w'] == pytest.approx(240, abs=10)
    # The extra width goes to the ladder itself, not to the rail.
    assert after['gridW'] - before['gridW'] == pytest.approx(240, abs=12)
    assert after['gridH'] < before['gridH']          # shorter, as dragged
    assert after['tableInside']
    tidy(page)


def test_the_exit_box_shows_the_figures_it_is_built_from(page):
    """Break-even with no workings is a number to be trusted or not.
    With them it can be checked: the round turn the market charges, the
    commission the broker charges, and the profit the target asks for —
    and every one of them fits in a 100px rail."""
    open_ladder(page)
    page.paths['publisher'].exits = {
        'break_even_buy': 59.21, 'break_even_sell': 58.99,
        'tp_buy': 60.21, 'tp_sell': 57.99, 'target_money': 10.0,
        'target_pct': 2.0, 'margin_per_spread': 500.0,
        'spread_width': 0.02, 'commission': 1.0,
        'note': '2% of 500.00 margin per spread = 10.00, over commission'}
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.querySelector('.fairwin .x-comm')"
        " && document.querySelector('.fairwin .x-comm').textContent"
        " !== '\\u2014'", timeout=WAIT)

    assert page.text_content('.fairwin .x-width') == '0.02'
    assert page.text_content('.fairwin .x-comm') == '$1.00'
    assert page.text_content('.fairwin .x-target') == '$10.00'
    assert '2% of' in page.get_attribute('.fairwin .x-target', 'title')

    # Nothing overflows the rail it lives in.
    fits = page.evaluate("""() => {
        const rail = document.querySelector('.ladder .rail');
        const box = rail.getBoundingClientRect();
        return [...rail.querySelectorAll('.exits td, .exits th')].every(
            cell => cell.getBoundingClientRect().right <= box.right + 1
                 && cell.scrollWidth <= cell.clientWidth + 1);
    }""")
    assert fits, 'a figure in the Exit box is wider than the rail'
    page.paths['publisher'].exits = None
    page.paths['publisher'].publish()


def test_the_leg_book_is_moved_to_the_bottom_whatever_the_markup_says(page):
    """Where this panel sits is not a detail: beside the ladder's own
    prices it reads as part of them. A page served from a template an
    older process cached can put it anywhere, so the position is made
    true at render time rather than assumed from the markup."""
    open_ladder(page)
    page.wait_for_selector('.ladder .footer table.legbook', timeout=WAIT)

    # Put it back in the quote strip, as an older page had it, and let
    # one render pass happen.
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.querySelector('.quotestrip').appendChild(
            node.querySelector('.legs'));
        window.MT5Trader.render();
    }""")

    assert page.locator('.ladder .footer .legs').count() == 1
    assert page.locator('.ladder .quotestrip .legs').count() == 0
    # ...and it is still the last thing in the window, under the ladder.
    assert page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const grid = node.querySelector('.grid').getBoundingClientRect();
        const legs = node.querySelector('.legs').getBoundingClientRect();
        return legs.top >= grid.top;
    }""")


def test_the_unclaimed_notice_is_one_line_and_the_table_is_where_it_acts(page):
    """A position at the broker our book cannot explain is exactly the
    one an automatic close must never touch, so it has to be said. A
    wall of table across the top of the screen is not how to say it:
    the line names the number, and the table lives with the buttons
    that act on it."""
    page.evaluate('() => { window.MT5Trader.state.dismissed = {}; }')
    page.paths['publisher'].unclaimed = [
        {'account': 'acct_a', 'ticket': 1326, 'symbol': 'GCZ6',
         'side': 'BUY', 'volume': 0.01, 'price_open': 4631.84}]
    page.paths['publisher'].publish()
    page.wait_for_selector('#unclaimed-banner:not(.hidden)', timeout=WAIT)

    text = page.text_content('#unclaimed-banner')
    assert '1 position(s)' in text
    assert 'Nothing is closed automatically' in text
    assert 'GCZ6' not in text                     # the table is elsewhere
    assert 'GCZ6' in page.get_attribute('#unclaimed-banner', 'title')

    # Review opens the Reconciler, where the position and its Close it
    # button are.
    page.click('#unclaimed-banner .banner-open')
    page.wait_for_selector('.monitor table.unclaimed', timeout=WAIT)
    row = page.text_content('.monitor table.unclaimed')
    assert 'GCZ6' in row and '1326' in row
    assert page.locator('.monitor .close-unclaimed').count() == 1

    # ...and the notice can be put away.
    page.click('#unclaimed-banner .banner-close')
    page.wait_for_selector('#unclaimed-banner.hidden', state='attached',
                           timeout=WAIT)
    page.paths['publisher'].unclaimed = None
    page.paths['publisher'].publish()
    page.evaluate("() => window.MT5Trader.closePanel('monitor:')")


def test_the_size_is_on_the_two_buttons_as_well_as_in_the_box(page):
    """A trader who arms 10 and then presses SELL should not have to
    look back at a box on the other side of the rail to know what they
    are about to send."""
    open_ladder(page)
    page.click('.ladder .keypad button[data-qty="10"]')
    page.wait_for_timeout(150)

    assert 'BUY 10' in page.text_content('.ladder .buy-touch')
    assert 'SELL 10' in page.text_content('.ladder .sell-touch')

    page.click('.ladder .keypad button.clr')
    page.wait_for_function(
        "() => document.querySelector('.ladder .buy-touch')"
        ".textContent.trim() === 'BUY 1'", timeout=WAIT)
    # CLR is the ladder's own default, not a blank button.
    assert 'SELL 1' in page.text_content('.ladder .sell-touch')


def test_the_feed_can_be_refreshed_from_the_ladder(page):
    """"How do I refresh it?" needs a button, not an explanation. It
    sits beside the badge that reports the trouble."""
    open_ladder(page)
    before = command_count(page)

    page.click('.ladder .refresh-feed')
    page.wait_for_timeout(300)

    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['kind'] == 'refresh_feed'
    assert command['payload']['pair'] == 'XAUUSD_|GC1226'


def test_the_two_columns_say_what_they_are(page):
    """Work and LTQ are the two easiest things on a ladder to confuse:
    one is an order that is still live, the other is a trade that
    already happened."""
    open_ladder(page)

    work = page.get_attribute('.ladder .grid th.c-work', 'title')
    ltq = page.get_attribute('.ladder .grid th.c-ltq', 'title')

    assert 'resting orders' in work
    # ...and what the two buttons on it do. Clicking a level you are
    # working ADDS one; the pull is the right button.
    assert 'CLICK to add one more' in work
    assert 'RIGHT-CLICK to pull one' in work
    assert 'most recent FILL' in ltq
    assert 'not an order' in ltq

    page.click('#help')
    page.wait_for_selector('#help-overlay:not(.hidden)')
    text = page.text_content('#help-overlay')
    assert 'RESTING orders' in text and 'last FILL' in text
    page.click('#help-close')


def test_an_unapplied_setting_survives_the_connection_repaint(page):
    """The 5s connection poll repaints the Trading form from the SAVED
    settings, and isTyping() stops protecting a field the moment focus
    leaves it. So a number typed and then clicked away from reverted —
    and Apply, which reads the form, saved the OLD value back over it.
    Live: the stale-quote limit was set to 15 three times and stayed 5.
    """
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .trading .s-stale')

    # A value that differs from the saved one, or the assertion below
    # would pass on a form that reverted perfectly.
    was = page.input_value('.window.settings .s-stale')
    assert was != '22', 'pick a value the config does not already hold'
    page.fill('.window.settings .s-stale', '22')
    # Focus LEAVES the field — the operator clicks Apply, or tabs away,
    # or the window loses focus. isTyping() protects nothing from here.
    page.evaluate('() => document.activeElement.blur()')

    # The repaint the poll would do.
    page.evaluate('() => window.MT5Settings.render()')

    assert page.input_value('.window.settings .s-stale') == '22'

    # And the CONTROL: a field nobody touched still follows the server,
    # or this would be a form frozen against its own data.
    rendered = page.evaluate(
        '() => document.querySelector(".window.settings .s-repeg")'
        '.dataset.rendered')
    assert page.input_value('.window.settings .s-repeg') == rendered


def test_the_close_button_sits_at_the_right_of_the_title_bar(page):
    """It merely followed the title, which looks right only while the
    window is narrow enough for the title to fill the bar. The wide
    Exchanges window put it against the title, on the left."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .titlebar .winbtns')

    bar = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar")'
        '.getBoundingClientRect().right')
    button = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar .winbtns")'
        '.getBoundingClientRect().right')
    title = page.evaluate(
        '() => document.querySelector(".window.settings .titlebar .title")'
        '.getBoundingClientRect().right')

    # Hard against the right edge, and nowhere near the title it used to
    # sit beside.
    assert bar - button < 12, f'close button is {bar - button:.0f}px from the right'
    assert button - title > 100


def test_the_dialog_outranks_a_window_however_often_it_was_clicked(page):
    """Windows were stacked by a counter that climbed with every raise
    and never came back down. Past enough of them a window passed the
    modal, and the Delete confirmation opened BEHIND the window that
    asked for it — a question nobody can answer and nothing to dismiss.
    """
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')

    # Float the window, then raise it a day's worth of times through the
    # real drag path — raise() does nothing for a window still in the row.
    top = page.evaluate("""() => {
        const win = document.querySelector('.window.settings');
        const bar = win.querySelector('.titlebar');
        for (let i = 0; i < 300; i++) {
            bar.dispatchEvent(new PointerEvent('pointerdown',
                {bubbles: true, clientX: 200, clientY: 100}));
            document.dispatchEvent(new PointerEvent('pointermove',
                {bubbles: true, clientX: 260 + (i % 5), clientY: 160}));
            document.dispatchEvent(new PointerEvent('pointerup',
                {bubbles: true}));
        }
        return Math.max(...Array.from(document.querySelectorAll('.window'))
            .map(n => parseInt(n.style.zIndex, 10) || 0));
    }""")
    modal = page.evaluate("""() => parseInt(getComputedStyle(
        document.getElementById('modal')).zIndex, 10)""")

    assert top > 10, 'the window never actually rose; the test proves nothing'
    assert top < modal, f'a window reached z={top}, the dialog is at {modal}'


def test_the_screen_does_not_repaint_under_a_drag(page):
    """The whole screen is rebuilt three times a second. Doing that
    under the pointer is what made a window judder and lag behind the
    cursor."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings .accounts')

    # Wiped by hand, so "was it rebuilt?" has an answer even when the
    # data behind it has not changed — comparing the HTML with itself
    # would pass whether the repaint ran or not.
    held = page.evaluate("""() => {
        const win = document.querySelector('.window.settings');
        const section = win.querySelector('.accounts');
        win.classList.add('dragging');
        section.innerHTML = '<!--wiped-->';
        window.MT5Settings.render();
        window.MT5Trader.render();
        const still = section.innerHTML.indexOf('wiped') >= 0;
        win.classList.remove('dragging');
        return still;
    }""")
    assert held, 'the table was rebuilt mid-drag'

    # CONTROL: with the drag over, the very same wipe is repainted away.
    repainted = page.evaluate("""() => {
        const section = document.querySelector('.window.settings .accounts');
        section.innerHTML = '<!--wiped-->';
        window.MT5Settings.render();
        return section.innerHTML.indexOf('wiped') < 0;
    }""")
    assert repainted, 'the table stopped repainting even when not dragging'


def test_an_order_held_back_from_the_broker_does_not_look_like_one_resting(page):
    """A synthetic order joins the book the instant it is clicked, but
    the real pending on the quoting leg is only placed once the guards
    are clear (quoter._rest_or_repeg holds off on stale or desynced). So
    while the feed is bad the order exists HERE and nowhere else — and
    it used to be drawn exactly like one resting at the broker. The only
    hint was W:n (broker 0) in small text in the footer, and a trader
    watched a level they believed was working while nothing of theirs
    was in the market.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr td.work', timeout=WAIT)

    def put_order(ticket):
        """One working order at a level, with its quote group either
        holding a broker ticket or not — PUBLISHED, not poked in. The
        publisher republishes every 200ms, and a snapshot edited in the
        page is gone by the next one."""
        level = page.evaluate(
            """() => window.MT5Trader.state.snapshot
                 .pairs['XAUUSD_|GC1226'].rows[5].level""")
        publisher = page.paths['publisher']
        publisher.orders = [{'order_id': 'O1', 'level': level,
                             'side': 'BUY', 'quantity': 1,
                             'filled_quantity': 0, 'state': 'WORKING'}]
        publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'BUY',
                             'level': level, 'leg': 'B', 'ticket': ticket,
                             'reason': None if ticket else
                             'the spread is stale — holding off',
                             'orders': ['O1']}]
        publisher.publish()

    # Held back: no ticket at the broker.
    put_order(None)
    page.wait_for_selector('.ladder .grid td.work.held', timeout=WAIT)
    held = page.locator('.ladder .grid td.work.held')
    assert held.count() == 1, 'a held-off order is not marked'
    assert 'NOT at the broker' in (held.first.get_attribute('title') or '')
    assert 'holding off' in (held.first.get_attribute('title') or '')

    # CONTROL: the same order, now actually resting, is NOT marked — or
    # the mark would mean nothing.
    put_order(987654)
    page.wait_for_function(
        "() => document.querySelectorAll('.ladder .grid td.work.held')"
        ".length === 0", timeout=WAIT)
    assert page.locator('.ladder .grid td.work[data-order-id]').count() == 1

    page.paths['publisher'].orders = None
    page.paths['publisher'].quotes = None
    page.paths['publisher'].publish()


def test_a_price_row_is_the_same_element_across_a_repaint(page):
    """The tbody was rebuilt with innerHTML three times a second. That
    threw away the row the pointer was over — losing :hover and the
    pressed state — and a click landing mid-replacement hit a detached
    element or whatever had just slid into that position."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr[data-level]', timeout=WAIT)

    same = page.evaluate("""() => {
        const body = document.querySelector('.ladder .grid tbody');
        const row = body.querySelector('tr[data-level]');
        const level = row.dataset.level;
        row.dataset.witness = 'marked';       // survives only if reused
        // Sizes change; the rows do not.
        const state = window.MT5Trader.state;
        const pair = state.snapshot.pairs['XAUUSD_|GC1226'];
        pair.rows.forEach((r, i) => { r.bid_size = 100 + i; });
        window.MT5Trader.render();
        const after = body.querySelector('tr[data-level="' + level + '"]');
        return after && after.dataset.witness === 'marked';
    }""")
    assert same, 'the row was destroyed and rebuilt instead of updated'


def test_the_ladder_holds_still_while_the_pointer_is_on_it(page):
    """Re-centring between a mousedown and the mouseup is how the wrong
    price gets sent."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr', timeout=WAIT)

    held = page.evaluate("""() => {
        const grid = document.querySelector('.ladder .grid');
        grid.dispatchEvent(new PointerEvent('pointerenter', {bubbles: true}));
        const before = grid.scrollTop;
        grid.scrollTop = before + 60;          // the trader looked away
        const moved = grid.scrollTop;
        const state = window.MT5Trader.state;
        state.centredAt['XAUUSD_|GC1226'] = 0;   // long overdue a centre
        window.MT5Trader.render();
        return grid.scrollTop === moved;
    }""")
    assert held, 'the ladder re-centred while the pointer was over it'


def test_a_click_sends_the_price_that_was_on_the_row(page):
    """Never the index: an index moves when the window does."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody td.ask', timeout=WAIT)

    cell = page.locator('.ladder .grid tbody tr[data-level] td.ask').nth(4)
    level = float(cell.evaluate('n => n.closest("tr").dataset.level'))
    before = command_count(page)
    cell.click()
    page.wait_for_timeout(300)

    assert command_count(page) == before + 1
    sent = last_command(page)
    assert sent['kind'] == 'click'
    assert sent['payload']['level'] == level, (
        f"clicked {level}, sent {sent['payload']['level']}")
    assert sent['payload']['side'] == 'BUY'      # the ask side buys


def test_ticking_the_setting_OPENS_the_fair_window(page):
    """The whole path, as the trader walks it: open this ladder's
    settings, tick Fair Spread window, press Apply — and the window is
    there. Every piece of this was tested on its own and the walk
    itself was not, which is exactly where it broke."""
    open_ladder(page)
    # Start from OFF, as a fresh config does — closed HERE, because a
    # snapshot alone no longer takes a window off the desk.
    page.paths['publisher'].show_fair_window = False
    page.paths['publisher'].publish()
    page.evaluate(
        "() => window.MT5Trader.showFairWindow('XAUUSD_|GC1226', false)")
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)

    page.evaluate(SPY_ON_PAIR_SAVE)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-algo-window', timeout=WAIT)
    page.check('.ladder .ls-algo-window')
    page.click('.ladder .ls-save')
    page.wait_for_function("() => window.__sent !== null", timeout=WAIT)

    sent = page.evaluate('() => window.__sent')
    assert sent['algo_window'] is True
    # The tick is the whole decision: `algo` is derived on the engine,
    # so the form does not send one.
    assert 'algo' not in sent
    # ...and it is on the screen NOW, not a poll later and not only once
    # the engine has written the file back.
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    page.evaluate('() => { window.fetch = window.__realFetch; }')
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
    # Left as it was found, for whichever test runs next.
    page.paths['publisher'].show_fair_window = True
    page.paths['publisher'].publish()


def test_the_ladder_cannot_be_made_shorter_than_its_own_rail(page):
    """The durable half of it. A window dragged short enough puts the
    rail behind a scrollbar however tight the rail is, so the window
    has a floor: it stops at the height its own controls need, and the
    LADDER scrolls instead — which is what a ladder is for."""
    measured = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const floor = parseInt(getComputedStyle(node).minHeight, 10) || 0;
        // Drag it well under the floor and read what the browser
        // actually gives back.
        node.classList.add('sized');
        node.style.height = '200px';
        const rail = node.querySelector('.rail');
        const footer = node.querySelector('.footer');
        return {floor: floor, height: node.getBoundingClientRect().height,
                railOver: rail.scrollHeight - rail.clientHeight,
                footerOver: footer.scrollHeight - footer.clientHeight};
    }""")

    assert measured['floor'] > 0, 'the ladder has no floor at all'
    assert measured['height'] >= measured['floor'] - 1, measured
    assert measured['railOver'] <= 1, measured
    assert measured['footerOver'] <= 1, measured


def test_the_ladder_is_never_cropped_sideways(page):
    """A ladder wide enough to hold its columns, whatever is done to
    it. The grid used to be a flex item at its default "never narrower
    than my content": drag the rail wider, or render the fonts larger,
    and it pushed past the window's right edge — where `.body`'s
    overflow cut the LTQ column off silently. A cropped ladder is a
    column of the market that is simply not there."""
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr', timeout=WAIT)

    def cropping():
        return page.evaluate("""() => {
            const node = document.querySelector('.window.ladder');
            const body = node.querySelector('.body');
            const grid = node.querySelector('.grid');
            const box = node.getBoundingClientRect();
            const cells = [...node.querySelectorAll('.grid thead th')];
            return {
                bodyOver: body.scrollWidth - body.clientWidth,
                gridRight: Math.round(
                    grid.getBoundingClientRect().right - box.right),
                clipped: cells.filter(
                    c => c.getBoundingClientRect().right > box.right + 1).length,
            };
        }""")

    assert cropping()['bodyOver'] <= 1, cropping()

    # Now shove the rail as wide as it will go — the operator drags it.
    page.evaluate("""() => {
        document.querySelector('.window.ladder .rail').style.width = '400px';
    }""")
    page.wait_for_timeout(120)
    wide = cropping()

    assert wide['bodyOver'] <= 1, wide
    assert wide['gridRight'] <= 1, wide
    assert wide['clipped'] == 0, wide
    page.evaluate("""() => {
        document.querySelector('.window.ladder .rail').style.width = '';
    }""")


def test_nothing_in_the_rail_is_clipped_at_any_rail_width(page):
    """`overflow-x: hidden` on the rail means anything too wide for it
    disappears in SILENCE — the Centre button rendered as "Ce", a
    button with half a word on it, and nothing anywhere said so.

    So this asks the rail itself: is every control inside your width?
    At the narrowest the rail can be dragged, where it is hardest.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .rail .centre-row', timeout=WAIT)

    for width in (96, 116, 160):
        over = page.evaluate("""(width) => {
            const rail = document.querySelector('.window.ladder .rail');
            rail.style.width = width + 'px';
            const box = rail.getBoundingClientRect();
            const room = rail.clientWidth;
            const bad = [];
            rail.querySelectorAll('*').forEach(function (el) {
                if (el.offsetParent === null) { return; }   // hidden
                const r = el.getBoundingClientRect();
                if (r.width > room + 1 || r.right > box.right + 1) {
                    bad.push((el.className || el.tagName) + ' ' +
                             Math.round(r.width) + '/' + room);
                }
            });
            return {bad: bad, scroll: rail.scrollWidth - rail.clientWidth};
        }""", width)

        assert over['bad'] == [], (width, over['bad'])
        assert over['scroll'] <= 1, (width, over)

    page.evaluate(
        "() => { document.querySelector('.ladder .rail').style.width = ''; }")


def test_the_desk_ends_exactly_where_the_taskbar_begins(page):
    """The desk was `calc(100vh - 30px)` against a taskbar 33px tall,
    so the last three pixels of every ladder sat behind a fixed bar —
    and what lives there is the spread row of the two legs' books."""
    fits = page.evaluate("""() => {
        const desk = document.getElementById('desktop')
            .getBoundingClientRect();
        const bar = document.getElementById('taskbar')
            .getBoundingClientRect();
        return {gap: Math.round(bar.top - desk.bottom),
                barBottom: Math.round(window.innerHeight - bar.bottom)};
    }""")

    # Flush: no overlap, and no strip of nothing between them.
    assert -1 <= fits['gap'] <= 1, fits
    assert -1 <= fits['barBottom'] <= 1, fits


def test_a_ladder_is_never_taller_than_the_desktop_it_is_on(page):
    """`min-height` beats `max-height` in CSS, so a measured floor
    larger than the screen would push the two legs' books off the
    bottom of a desktop that does not scroll vertically — gone, with no
    way to get them back."""
    open_ladder(page)
    page.wait_for_selector('.ladder .footer', timeout=WAIT)

    fits = page.evaluate("""() => {
        const desk = document.getElementById('desktop')
            .getBoundingClientRect();
        const node = document.querySelector('.window.ladder');
        const floor = parseInt(getComputedStyle(node).minHeight, 10) || 0;
        return {floor: floor, desk: Math.floor(desk.height),
                bottom: Math.round(
                    node.querySelector('.footer').getBoundingClientRect().bottom
                    - desk.bottom)};
    }""")

    assert fits['floor'] <= fits['desk'], fits
    assert fits['bottom'] <= 1, fits


def test_the_left_pane_does_not_scroll_on_a_short_window(page):
    """The trader reads the rail top to bottom while the market moves.
    A scrollbar there means the button they want is behind a scroll —
    and the two legs' books, which live under the ladder, are what got
    pushed out of the frame to make room for it."""
    page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        node.classList.add('sized');
        node.style.height = '480px';
    }""")
    page.wait_for_timeout(250)

    measured = page.evaluate("""() => {
        const node = document.querySelector('.window.ladder');
        const rail = node.querySelector('.rail');
        const footer = node.querySelector('.footer');
        const box = node.getBoundingClientRect();
        return {railOver: rail.scrollHeight - rail.clientHeight,
                footerOver: footer.scrollHeight - footer.clientHeight,
                footerInside:
                    footer.getBoundingClientRect().bottom <= box.bottom + 1};
    }""")

    assert measured['railOver'] <= 1, measured
    assert measured['footerOver'] <= 1, measured
    assert measured['footerInside']


def test_the_fair_window_survives_being_clicked_and_dragged(page):
    """It vanished when it was moved. A window that disappears when the
    trader touches it is worse than one that never opened: they now
    have to work out whether they closed it, whether it crashed, or
    whether the setting came undone."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin .titlebar', timeout=WAIT)

    bar = page.locator('.window.fairwin .titlebar').bounding_box()
    before = page.locator('.window.fairwin').bounding_box()

    # A click on it first — focus, not a drag.
    page.mouse.click(bar['x'] + 40, bar['y'] + 8)
    page.wait_for_timeout(400)
    assert page.locator('.window.fairwin').count() == 1, \
        'clicking the title bar closed it'

    # ...and now drag it somewhere else.
    page.mouse.move(bar['x'] + 40, bar['y'] + 8)
    page.mouse.down()
    page.mouse.move(bar['x'] - 120, bar['y'] + 90, steps=10)
    page.mouse.up()
    page.wait_for_timeout(600)

    assert page.locator('.window.fairwin').count() == 1, \
        'dragging the window closed it'
    after = page.locator('.window.fairwin').bounding_box()
    assert abs(after['x'] - before['x']) > 40, (before, after)

    # And it is still there several polls later.
    page.wait_for_timeout(800)
    assert page.locator('.window.fairwin').count() == 1, \
        'the window went away on a later poll'


def test_an_engine_that_has_not_caught_up_does_not_take_the_window_away(page):
    """THE fault. Whether this window is open is a decision about this
    screen, and the engine's copy of the setting is where it STARTS,
    not who owns it. Owned by the snapshot, a poll arriving before the
    save has landed — or with the engine down, or restarted, or writing
    a config the web process is not reading — takes the window away
    under the trader's hands, a third of a second after they opened
    it."""
    open_ladder(page)
    page.wait_for_selector('.window.fairwin', timeout=WAIT)

    # The engine says nothing of the sort, poll after poll.
    page.paths['publisher'].show_fair_window = False
    page.paths['publisher'].publish()
    page.wait_for_timeout(900)

    assert page.locator('.window.fairwin').count() == 1, \
        'a snapshot without the setting closed a window the trader opened'

    # Closing it by hand still closes it, and it STAYS closed.
    page.click('.window.fairwin .close')
    page.wait_for_function(
        "() => !document.querySelector('.window.fairwin')", timeout=WAIT)
    page.paths['publisher'].show_fair_window = True
    page.paths['publisher'].publish()
    page.wait_for_timeout(700)
    assert page.locator('.window.fairwin').count() == 0, \
        'a closed window came back on the next poll'

    # ...until it is asked for again.
    page.evaluate("""() => {
        const UI = window.MT5Trader;
        UI.showFairWindow('XAUUSD_|GC1226', true);
    }""")
    page.wait_for_selector('.window.fairwin', timeout=WAIT)


def test_a_stalled_engine_is_not_called_a_dead_one(page):
    """Two different faults with two different fixes: a coordinator
    that has stopped publishing while this page still reads its last
    file, against no file at all. Both said ENGINE DOWN — and it is the
    first one where every price on the screen is a photograph, which is
    the more dangerous of the two to misread."""
    open_ladder(page)

    # A snapshot from ten minutes ago: the file is there, the engine is
    # not running.
    page.paths['publisher'].live.clear()
    page.paths['publisher'].publish(at=time.time() - 600)
    page.wait_for_function(
        "() => document.getElementById('link-badge')"
        ".textContent.indexOf('STALLED') >= 0", timeout=WAIT)

    detail = page.get_attribute('#link-badge', 'title') or ''
    assert 's old' in detail and 'Nothing on this screen is live' in detail

    page.paths['publisher'].live.set()
    page.paths['publisher'].publish()
    page.wait_for_function(
        "() => document.getElementById('link-badge')"
        ".textContent.indexOf('STALLED') < 0", timeout=WAIT)


def test_the_pair_type_is_declared_and_the_expiries_follow_it(page):
    """Two futures with two expiries are NOT a calendar. UKOILV6
    against USOILV6 is Brent against WTI: two different instruments
    with no carry between them, and nothing in an expiry says whether
    two contracts share an underlying. So the operator declares it —
    and the form follows the declaration as it is changed, not only
    when it opens."""
    open_ladder(page)
    page.click('.ladder .ladder-cog')
    page.wait_for_selector('.ladder .ls-pair-type', timeout=WAIT)

    page.select_option('.ladder .ls-pair-type', 'FUTURE_FUTURE')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('two futures') >= 0", timeout=WAIT)
    assert page.locator('.ladder .ls-expiry-a').is_enabled()
    assert 'NEAR' in page.text_content('.ladder .ls-pairtype')

    page.select_option('.ladder .ls-pair-type', 'RELATED')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('no fair spread') >= 0", timeout=WAIT)
    assert page.locator('.ladder .ls-expiry-a').is_disabled()
    assert page.locator('.ladder .ls-expiry').is_disabled()

    page.select_option('.ladder .ls-pair-type', 'SPOT_FUTURE')
    page.wait_for_function(
        "() => document.querySelector('.ladder .ls-pairtype')"
        ".textContent.indexOf('only leg B') >= 0", timeout=WAIT)
    page.click('.ladder .ls-close')


# -- the order-execution pass ---------------------------------------------

def test_clicking_a_working_level_ADDS_one_instead_of_pulling_it(page):
    """A trader who clicks a level they are already working wants MORE
    of it. Pulling was the wrong default, and the toast that came back
    ('resting at … to close position') read as an error on an ordinary
    click.

    Right-click is the pull, and it is in the tooltip.
    """
    open_ladder(page)
    page.wait_for_selector('.ladder .grid tbody tr td.work', timeout=WAIT)
    publisher = page.paths['publisher']
    level = page.evaluate(
        """() => window.MT5Trader.state.snapshot
             .pairs['XAUUSD_|GC1226'].rows[5].level""")
    publisher.orders = [{'order_id': 'O9', 'level': level, 'side': 'SELL',
                         'quantity': 1, 'filled_quantity': 0,
                         'state': 'WORKING'}]
    publisher.quotes = [{'pair_key': 'XAUUSD_|GC1226', 'side': 'SELL',
                         'level': level, 'leg': 'B', 'ticket': 5150,
                         'price': 4660.4, 'symbol': 'GC1226',
                         'crosses_leg': 'A', 'orders': ['O9']}]
    publisher.publish()
    page.wait_for_selector('.ladder .grid td.work[data-order-id]',
                           timeout=WAIT)

    cell = page.locator('.ladder .grid td.work[data-order-id]').first
    title = cell.get_attribute('title') or ''
    assert 'Click to add another' in title
    assert 'right-click to pull one' in title

    before = command_count(page)
    cell.click()
    page.wait_for_timeout(250)
    command = last_command(page)
    assert command_count(page) == before + 1
    # ONE MORE ORDER at that level, on the side already resting there —
    # not a cancel, and not the other side.
    assert command['kind'] == 'click'
    assert command['payload']['side'] == 'SELL'
    assert abs(command['payload']['level'] - level) < 1e-9

    # The control: the pull still exists, on the right button.
    page.locator('.ladder .grid td.work[data-order-id]').first.click(
        button='right')
    page.wait_for_timeout(250)
    pull = last_command(page)
    assert pull['kind'] == 'cancel_order'
    assert pull['payload']['order_id'] == 'O9'

    publisher.orders = None
    publisher.quotes = None
    publisher.publish()


def test_the_ladder_says_which_leg_the_real_pending_rests_on(page):
    """LIMIT quotes ONE leg and crosses the other at market on the
    fill. Leg A showing no order at the broker is the mode working —
    and nothing on the screen said so, so it read as leg A having
    failed to place."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.order_type = 'LIMIT'
    publisher.quoting_leg = 'b'
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .quoting-note')"
        ".textContent.indexOf('leg B') >= 0", timeout=WAIT)
    note = page.text_content('.ladder .quoting-note')
    assert 'GC1226' in note                       # the symbol, by name
    assert 'leg A crosses on fill' in note        # ...and what leg A does

    # The control: in MARKET mode neither leg rests anything, and the
    # note must not go on naming a quoting leg.
    publisher.order_type = 'MARKET'
    publisher.publish()
    page.wait_for_function(
        "() => document.querySelector('.ladder .quoting-note')"
        ".textContent.indexOf('both legs') >= 0", timeout=WAIT)
    publisher.order_type = 'LIMIT'
    publisher.publish()


def test_MARKET_mode_carries_the_M_over_every_price_cell(page):
    """The mode is what the M names, and the mode does not change
    halfway down the ladder. It used to appear on the two touch rows
    only, so nine rows in ten the cursor said 'cell' while the badge
    said MARKET."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.order_type = 'MARKET'
    publisher.publish()
    page.wait_for_selector('.window.mode-market', timeout=WAIT)

    def cursor(nth):
        return page.eval_on_selector_all(
            '.ladder .grid tbody tr td.bid',
            "(cells, n) => getComputedStyle(cells[n]).cursor", nth)

    assert 'svg+xml' in cursor(1), 'no M away from the touch'
    assert 'svg+xml' in cursor(15)

    # The control: LIMIT mode carries no M anywhere.
    publisher.order_type = 'LIMIT'
    publisher.publish()
    page.wait_for_function(
        "() => !document.querySelector('.window.mode-market')", timeout=WAIT)
    assert 'svg+xml' not in cursor(1)


def test_the_three_cancels_are_labelled_as_what_they_act_on(page):
    """S / B / CXL All are the WORKING ORDER controls. Unlabelled, three
    initials sat under a column of size boxes and read as sides."""
    open_ladder(page)
    label = page.evaluate("""() => {
        const row = document.querySelector('.ladder .cxl-row');
        const previous = row.previousElementSibling;
        return previous ? previous.textContent.trim() : null;
    }""")
    assert label == 'Working orders'


def test_a_position_can_be_closed_at_a_PRICE_not_only_at_the_market(page):
    """CLOSE ALL crosses now. This rests one working order per position
    at a level the trader names — and it asks first, because it is an
    exit that WAITS and a trader who thought it crossed is a trader
    holding a position they believe is closed."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.net_position = 2.0
    publisher.publish()
    page.wait_for_function(
        "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        " || {}).net_position === 2", timeout=WAIT)

    before = command_count(page)
    page.fill('.ladder .close-limit', '59.40')
    page.click('.ladder .close-limit-go')
    page.wait_for_selector('#modal:not(.hidden)')
    assert command_count(page) == before, 'it sent before it asked'
    body = page.text_content('#modal')
    assert 'WAITS' in body and 'no stop' in body
    page.click('#modal-confirm')
    page.wait_for_timeout(250)

    command = last_command(page)
    assert command['kind'] == 'close_at_limit'
    assert command['payload']['level'] == 59.40
    assert command['payload']['pair'] == 'XAUUSD_|GC1226'

    publisher.net_position = 0.0
    publisher.publish()


def test_closing_at_a_price_is_refused_on_a_flat_ladder(page):
    """The control: nothing to close is not an order to place."""
    open_ladder(page)
    publisher = page.paths['publisher']
    publisher.net_position = 0.0
    publisher.publish()
    page.wait_for_function(
        "() => (window.MT5Trader.state.snapshot.pairs['XAUUSD_|GC1226']"
        " || {}).net_position === 0", timeout=WAIT)

    before = command_count(page)
    page.fill('.ladder .close-limit', '59.40')
    page.click('.ladder .close-limit-go')
    page.wait_for_selector('#toasts .toast', timeout=WAIT)
    assert 'already flat' in page.text_content('#toasts .toast')
    assert command_count(page) == before
    # An error toast stays until it is dismissed, and this page is
    # shared with every other test in the file.
    page.evaluate("() => document.getElementById('toasts').innerHTML = ''")
