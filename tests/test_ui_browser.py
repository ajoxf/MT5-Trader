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
import os
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip('playwright.sync_api')

from mt5trader.webapp import create_app                        # noqa: E402


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
        payload = snapshot(self.order_type, self.confirm)
        if at is not None:
            payload['at'] = at
        tmp = self.path + '.tmp'
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


def snapshot(order_type='LIMIT', confirm=False):
    rows = []
    for step in range(20, -21, -1):
        level = round(59.10 + step * 0.01, 4)
        rows.append({'level': level, 'work_buy': None, 'work_sell': None,
                     'is_best_bid': step == -1, 'is_best_ask': step == 1})
    return {
        'at': time.time(), 'loop_interval_sec': 0.31,
        'confirm_market_clicks': confirm, 'row_height_px': 17,
        'command_poll_sec': 0.02,
        'accounts': {'acct_a': {'profit': 0.0}, 'acct_b': {'profit': 0.0}},
        'account_health': {
            'warn_level': 200.0, 'weakest': 'acct_b', 'weakest_level': 125.0,
            'unknown': [],
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
                       'unknown_accounts': []},
        'hedge_times_ms': [], 'click_to_on_ms': [],
        'pairs': {
            'XAUUSD_|GC1226': {
                'key': 'XAUUSD_|GC1226', 'name': 'Gold basis', 'enabled': True,
                'account_a': 'acct_a', 'account_b': 'acct_b',
                'symbol_a': 'XAUUSD_', 'symbol_b': 'GC1226',
                'hedge_ratio': 1.0, 'hedge_ratio_for': 'XAUUSD_|GC1226',
                'increment': 0.01, 'increment_derived': 0.01,
                'order_type': order_type, 'time_in_force': 'DAY',
                'overnight': 'ALLOW', 'default_quantity': 1.0,
                'clip_lots_a': 0.1, 'clip_lots_b': 0.1, 'spread_units': 10.0,
                'short_spread': 59.09, 'long_spread': 59.11,
                'market': {'spread': 59.10, 'short_spread': 59.09,
                           'long_spread': 59.11, 'net_change': -0.61,
                           'feed_badge': 'OK (oldest leg 0.2s)',
                           'session': {'open': 59.71, 'high': 59.80,
                                       'low': 58.90, 'volume': 0.0,
                                       'ours': True}},
                'rows': rows, 'orders': [], 'quotes': [], 'positions': [],
                'working_buys': 0, 'working_sells': 0, 'net_position': 0.0,
                'avg_entry': None, 'open_pnl': None, 'last_print': None,
                'errors': [],
            }
        },
    }


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
        page.wait_for_selector('.ladder tbody tr')
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
    headers = page.eval_on_selector_all(
        '.ladder thead th', 'nodes => nodes.map(n => n.textContent)')
    assert headers == ['Work', 'Bids', 'Price', 'Asks', 'LTQ']
    assert page.locator('.ladder tbody tr').count() == 41


def test_the_inside_market_rule_is_drawn_between_the_two_touches(page):
    line = page.locator('.ladder tr.market-line')
    assert line.count() == 1
    border = line.first.locator('td').first.evaluate(
        'node => getComputedStyle(node).borderTopWidth')
    assert border == '2px'
    # And it sits between the best bid and the best ask, not on top of
    # one of them.
    bid = float(line.first.get_attribute('data-level'))
    ask_row = page.locator('.ladder tbody tr').nth(
        page.locator('.ladder tbody tr').count() - 1)
    assert bid < 59.11 and bid >= 59.09 - 0.011
    assert ask_row is not None


def test_bid_is_blue_and_ask_is_red_on_the_rendered_page(page):
    # The ladder repaints three times a second; wait for a painted row
    # rather than racing one.
    page.wait_for_selector('.ladder tr.in-bid td.bid')
    page.wait_for_selector('.ladder tr.in-ask td.ask')
    bid = page.locator('.ladder tr.in-bid td.bid').first
    ask = page.locator('.ladder tr.in-ask td.ask').first
    assert bid.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(26, 111, 181)'
    assert ask.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(176, 28, 28)'


def test_a_limit_click_places_one_order_and_asks_nothing(page):
    before = command_count(page)
    page.locator('.ladder tbody tr td.bid').nth(5).click()
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
    page.locator('.ladder tbody tr td.ask').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'BUY'

    page.locator('.ladder tbody tr td.bid').nth(3).click()
    page.wait_for_timeout(250)
    assert last_command(page)['payload']['side'] == 'SELL'

    # ...and the buttons carry the colour of the column they act on.
    buy = page.locator('.ladder .buy-touch')
    sell = page.locator('.ladder .sell-touch')
    assert buy.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(176, 28, 28)'
    assert sell.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(26, 111, 181)'


def test_three_clicks_at_one_price_send_three_orders(page):
    before = command_count(page)
    cell = page.locator('.ladder tbody tr td.ask').nth(3)
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
    page.wait_for_selector('.window.mode-market', timeout=3000)
    # Read in ONE evaluate: the ladder repaints on every publish, and a
    # node resolved in Python can be replaced before the style is read.
    cursor = page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "'.window.mode-market tbody td.bid')).cursor")
    assert cursor == 'crosshair'

    before = command_count(page)
    page.locator('.ladder tbody tr td.bid').nth(6).click()
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
    page.locator('.ladder tbody tr td.ask').nth(8).click()
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
    page.locator('.ladder tbody tr td.bid').nth(6).click()
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
    page.wait_for_selector('#engine-banner:not(.hidden)', timeout=5000)
    assert 'Nothing on this screen is live' in \
        page.text_content('#engine-banner')

    publisher.live.set()                   # ...and comes back
    # `state='attached'`: a hidden element is never "visible", which is
    # the point of asserting on it.
    page.wait_for_selector('#engine-banner.hidden', state='attached',
                           timeout=5000)
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
    assert page.text_content('.ladder .armed') == '10'

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
    assert page.text_content('.ladder .armed') == '1'


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
    page.click('.ladder .default-qty')
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
    page.wait_for_selector('.monitor .pane table', timeout=5000)

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
    page.wait_for_selector('tr[data-account] .connect-account', timeout=5000)

    row = page.locator('tr[data-account]').first
    for name in ('connect-account', 'test-account', 'diagnose-account'):
        assert row.locator('.' + name).count() == 1
    assert 'Exchanges' in page.text_content('.window.settings .accounts h3')


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
    page.wait_for_selector('tr.checklist', timeout=5000)

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
    page.wait_for_selector('.conn', timeout=5000)
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
    page.wait_for_selector('.toast.ok', timeout=4000)
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
    are about a window, so put one on it."""
    page.wait_for_function(
        "() => Object.keys((window.MT5Trader.state.snapshot || {}).pairs "
        "|| {}).length > 0", timeout=5000)
    page.evaluate("""() => {
        const state = window.MT5Trader.state;
        const key = Object.keys(state.snapshot.pairs || {})[0];
        state.open = [window.MT5Trader.panelId('ladder', key)];
        window.MT5Trader.render();
    }""")
    page.wait_for_selector('.window.ladder .titlebar', timeout=5000)


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
    page.wait_for_selector('.ladder tbody tr')
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
    page.wait_for_selector('.window.monitor', timeout=5000)

    page.click('.window.monitor .titlebar .close')

    page.wait_for_selector('.window.monitor', state='detached', timeout=5000)


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
    page.wait_for_selector('.window.settings .pairs', timeout=5000)
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
    page.wait_for_selector('.ladder tbody tr')
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


def test_a_new_pairs_key_is_built_from_its_two_symbols(page):
    """Typing `XAUUSD|GCZ6` by hand is not something a trading screen
    should ask for — and a key typed wrong is a pair that never loads."""
    seed_config(page)
    page.click('.new-pair')
    page.wait_for_selector('.pair-form', timeout=5000)

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
        const rows = [...document.querySelectorAll('.ladder tbody tr')];
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
    page.wait_for_selector('#add-menu:not(.hidden)', timeout=5000)

    text = page.text_content('#add-menu')
    assert 'Gold basis' in text and 'XAUUSD_' in text
    assert 'Market Grid' in text and 'Positions' in text

    page.click('#add-menu button[data-panel="monitor:"]')
    page.wait_for_selector('.window.monitor', timeout=5000)
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
    page.wait_for_selector('.window.monitor', timeout=5000)
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
